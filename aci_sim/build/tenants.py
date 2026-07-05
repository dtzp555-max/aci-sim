"""build/tenants.py — fvTenant, fvCtx, fvBD, fvAp/fvAEPg, vzBrCP, vzFilter.

MAC/VLAN sequences and endpoint addresses are per-store (per site/APIC); this
module assumes the intended one-MITStore-per-APIC model.  Merging two site
stores would collide DNs and attribute values.

Tier-3 (PR-20): `fvBD.mac` now reflects `bd.mac` (per-BD override) falling
back to `fabric.default_bd_mac` (fabric-wide default, itself defaulting to
the real ACI default gateway MAC `00:22:BD:F8:19:FF` — the literal this
module hardcoded for every BD pre-PR-20; see `_build_bd`).

Batch-1 additions (CONTRACT.md §6 "Rels", "Contracts"):
  fvRsPathAtt        uni/tn-{t}/ap-{ap}/epg-{epg}/rspathAtt-[{pathDn}]
  vzRsSubjGraphAtt   {vzSubj}/rsSubjGraphAtt

fvRsPathAtt needs a static binding whose tDn is the SAME front-panel path an
EPG's own endpoints already use (endpoints.py assigns EP ports), so this
module reads back the real fvCEp.fabricPathDn the endpoints builder already
wrote for that EPG rather than re-deriving port-assignment logic
independently (see orchestrator.py: `endpoints` now runs before `tenants`
specifically to make this store read-back possible; a stretched EPG that has
no locally-learned (front-panel) endpoint at this site — i.e. every endpoint
here is REMOTE/tunnel-learned — gets no fvRsPathAtt, since a static binding
must reference a real local port, not a tunnel).

Batch-2 additions (CONTRACT.md §6 "Tenant (control)"): fvAEPg now carries a
stable `pcTag` (real ACI's per-EPG "sclass" used for zoning-rule matching) —
previously absent from this sim entirely (verified: no prior pcTag anywhere
in aci_sim). Assigned deterministically per (tenant, ap, epg) via
`_pctag_for` (a small CRC32-derived integer in a believable ACI sclass range,
avoiding the well-known reserved system pcTags 1/13/14/15/16384 autoACI's
zoning_rules.py hardcodes) so it is stable across rebuilds without needing
external state. `fvEpP` (uni/epp/fv-[{epg_dn}]) is seeded alongside each EPG,
carrying the SAME pcTag plus a per-VRF `scopeId` (build/zoning.py derives its
own actrlRule.scopeId from the identical per-VRF scheme, so the two line up —
see that module's docstring) and `epgPKey` = the EPG's own dn (zoning_rules.py
extracts the friendly EPG name back out of epgPKey via its trailing "epg-"
segment).
"""
from __future__ import annotations

import re
import zlib

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import (
    BD,
    Contract,
    EPG,
    Filter,
    FilterEntry,
    L3Out,
    Site,
    Tenant,
    Topology,
)

_FRONT_PANEL_PATH_RE = re.compile(r"paths-\d+/pathep-\[eth\d+/\d+\]$")

# Batch-2 pcTag/scopeId scheme (shared with build/zoning.py's actrlRule —
# import these two helpers from here rather than re-deriving the formula, so
# an actrlRule's sPcTag/dPcTag/scopeId always match the fvAEPg/fvEpP/fvCtx
# values a zoning-rule consumer would resolve them against).
#
# Real ACI pcTags ("sclass") are 16-bit-ish integers; autoACI's
# zoning_rules.py hardcodes 5 well-known SYSTEM_PCTAGS (1, 13, 14, 15, 16384)
# that must never collide with a real EPG/VRF pcTag, so both helpers below
# add a fixed offset clear of that reserved set.
_PCTAG_BASE = 32770        # clear of the highest reserved system pcTag (16384)
_SCOPE_BASE = 2097152      # clear of any plausible pcTag range


def pctag_for(key: str) -> str:
    """Deterministic pcTag ("sclass") for an EPG/VRF/ExtEPG, keyed by a
    stable string (e.g. the object's own dn). CRC32-derived so it's stable
    across rebuilds without external state, same technique endpoints.py's
    `_vnid` already uses for VNIDs."""
    return str(_PCTAG_BASE + (zlib.crc32(key.encode()) & 0xFFFF))


def scope_id_for(tenant: str, vrf: str) -> str:
    """Deterministic scopeId (real ACI: the VRF's zoning-scope id) for a
    (tenant, vrf) pair — every EPG/actrlRule sharing that VRF shares this
    scopeId, matching real ACI's per-VRF zoning scope semantics."""
    return str(_SCOPE_BASE + (zlib.crc32(f"{tenant}:{vrf}".encode()) & 0xFFFF))


def _is_deployed(tenant: Tenant, site: Site) -> bool:
    return site.name in tenant.sites


def _owning_site(l3out: L3Out, topo: Topology) -> str | None:
    """Return the site name that owns this L3Out."""
    if l3out.site is not None:
        return l3out.site
    for s in topo.sites:
        site_bl_ids = {bl.id for bl in s.border_leaves}
        if any(bl_id in site_bl_ids for bl_id in l3out.border_leaves):
            return s.name
    return None


def _entry_name(entry: FilterEntry, idx: int) -> str:
    if entry.from_port != "unspecified":
        return f"{entry.proto}-{entry.from_port}"
    return f"{entry.proto}-{idx}"


def _build_bd(topo: Topology, site: Site, t: str, bd: BD, l3outs: list[L3Out]) -> MO:
    """Build fvBD + fvRsCtx, fvSubnet, and fvRsBDToOut children."""
    l2s = bd.l2stretch
    # unicastRoute reflects whether the BD actually routes traffic, i.e. has
    # fvSubnet children — NOT l2stretch. A stretched (l2stretch) BD can still
    # carry routed subnets (this topology's stretched BDs do); NDO's schema
    # side (ndo/model.py) always reports unicastRouting=True for every BD with
    # subnets, so the two planes must agree. A true L2-only BD (no subnets)
    # correctly stays unicastRoute="no".
    routed = bool(bd.subnets)
    # Tier-3 (PR-20): bd.mac (per-BD override) falls back to
    # fabric.default_bd_mac, which itself defaults to the real ACI default
    # gateway MAC "00:22:BD:F8:19:FF" — the literal this module hardcoded
    # for every BD pre-PR-20. An unset bd.mac + unset fabric.default_bd_mac
    # produces the identical "00:22:BD:F8:19:FF" as before this PR.
    bd_mac = bd.mac or topo.fabric.default_bd_mac
    bd_mo = MO(
        "fvBD",
        dn=f"uni/tn-{t}/BD-{bd.name}",
        name=bd.name,
        arpFlood="yes" if l2s else "no",
        unicastRoute="yes" if routed else "no",
        ipLearning="yes",
        unkMacUcastAct="flood" if l2s else "proxy",
        unkMcastAct="flood",
        v6unkMcastAct="nd",
        multiDstPktAct="bd-flood",
        epMoveDetectMode="garp",
        hostBasedRouting="no",
        limitIpLearnToSubnets="yes",
        mtu="9000",
        mac=bd_mac,
        intersiteBumTrafficAllow="yes" if l2s else "no",
        type="regular",
    )
    bd_mo.add_child(MO(
        "fvRsCtx",
        dn=f"uni/tn-{t}/BD-{bd.name}/rsctx",
        tnFvCtxName=bd.vrf,
    ))
    for cidr in bd.subnets:
        bd_mo.add_child(MO(
            "fvSubnet",
            dn=f"uni/tn-{t}/BD-{bd.name}/subnet-[{cidr}]",
            ip=cidr,
            scope="public,shared",
        ))
    for l3out in l3outs:
        if l3out.vrf != bd.vrf:
            continue
        if _owning_site(l3out, topo) != site.name:
            continue
        bd_mo.add_child(MO(
            "fvRsBDToOut",
            dn=f"uni/tn-{t}/BD-{bd.name}/rsBDToOut-{l3out.name}",
            tnL3extOutName=l3out.name,
        ))
    return bd_mo


def _find_static_binding_path(store: MITStore, epg_ref: str) -> tuple[str, str] | None:
    """Return (pathDn, encap) for one LOCAL (front-panel) endpoint already
    written for *epg_ref* by endpoints.py, or None if this EPG has no
    locally-learned endpoint at this site (e.g. a stretched EPG that is
    entirely REMOTE/tunnel-learned here).

    fvCEp dn is f"{epg_ref}/cep-{mac}"; we scan the store's fvCEp class list
    (cheap — endpoint counts are small) rather than requiring endpoints.py to
    expose an index, keeping the two modules loosely coupled.
    """
    prefix = f"{epg_ref}/cep-"
    for cep in store.by_class("fvCEp"):
        if not cep.dn.startswith(prefix):
            continue
        path_dn = cep.attrs.get("fabricPathDn", "")
        if _FRONT_PANEL_PATH_RE.search(path_dn):
            return path_dn, cep.attrs.get("encap", "")
    return None


def _build_epg(t: str, ap_name: str, epg: EPG, phys_dom: str, store: MITStore) -> MO:
    """Build fvAEPg + fvRsBd, fvRsDomAtt, fvRsProv, fvRsCons, fvRsPathAtt children.

    Batch-2: fvAEPg.pcTag is assigned here (keyed by the EPG's own dn via
    `pctag_for`) so it is available the moment the EPG exists; the matching
    fvEpP (which needs the owning VRF for its scopeId) is built separately in
    _build_tenant, where the BD->VRF mapping is already in scope.
    """
    epg_dn = f"uni/tn-{t}/ap-{ap_name}/epg-{epg.name}"
    epg_mo = MO(
        "fvAEPg",
        dn=epg_dn,
        name=epg.name,
        prefGrMemb="exclude",
        pcEnfPref="unenforced",
        isAttrBasedEPg="no",
        floodOnEncap="disabled",
        pcTag=pctag_for(epg_dn),
    )
    epg_mo.add_child(MO(
        "fvRsBd",
        dn=f"{epg_dn}/rsbd",
        tnFvBDName=epg.bd,
    ))
    epg_mo.add_child(MO(
        "fvRsDomAtt",
        dn=f"{epg_dn}/rsdomAtt-[uni/phys-{phys_dom}]",
        tDn=f"uni/phys-{phys_dom}",
        instrImedcy="lazy",
    ))
    for c in epg.contracts.provide:
        epg_mo.add_child(MO(
            "fvRsProv",
            dn=f"{epg_dn}/rsprov-{c}",
            tnVzBrCPName=c,
        ))
    for c in epg.contracts.consume:
        epg_mo.add_child(MO(
            "fvRsCons",
            dn=f"{epg_dn}/rscons-{c}",
            tnVzBrCPName=c,
        ))

    # Batch-1: fvRsPathAtt — static binding to the SAME front-panel path this
    # EPG's own (already-built) endpoints use.
    binding = _find_static_binding_path(store, epg_dn)
    if binding is not None:
        path_dn, encap = binding
        epg_mo.add_child(MO(
            "fvRsPathAtt",
            dn=f"{epg_dn}/rspathAtt-[{path_dn}]",
            tDn=path_dn,
            encap=encap,
            mode="regular",
            instrImedcy="lazy",
        ))
    return epg_mo


def _build_contract(t: str, contract: Contract, attach_graph: bool) -> MO:
    """Build vzBrCP + vzSubj + vzRsSubjFiltAtt (+ batch-1 vzRsSubjGraphAtt) children.

    *attach_graph*: attach a vzRsSubjGraphAtt to this ONE contract's subject
    (per the batch-1 placement note — only one existing subject gets a
    service-graph attachment; vnsAbsGraph itself is out of scope, so
    tnVnsAbsGraphName references a plausible graph name only).
    """
    subj_dn = f"uni/tn-{t}/brc-{contract.name}/subj-{contract.name}"
    brc_mo = MO(
        "vzBrCP",
        dn=f"uni/tn-{t}/brc-{contract.name}",
        name=contract.name,
        scope=contract.scope,
        prio="unspecified",
        targetDscp="unspecified",
    )
    subj_mo = MO(
        "vzSubj",
        dn=subj_dn,
        name=contract.name,
        revFltPorts="yes",
        prio="unspecified",
    )
    for flt in contract.filters:
        subj_mo.add_child(MO(
            "vzRsSubjFiltAtt",
            dn=f"{subj_dn}/rssubjFiltAtt-{flt.name}",
            tnVzFilterName=flt.name,
        ))
    if attach_graph:
        subj_mo.add_child(MO(
            "vzRsSubjGraphAtt",
            dn=f"{subj_dn}/rsSubjGraphAtt",
            tnVnsAbsGraphName=f"{contract.name}_graph",
        ))
    brc_mo.add_child(subj_mo)
    return brc_mo


def _build_filter(t: str, flt: Filter) -> MO:
    """Build vzFilter + vzEntry children."""
    flt_mo = MO("vzFilter", dn=f"uni/tn-{t}/flt-{flt.name}", name=flt.name)
    for idx, entry in enumerate(flt.entries):
        ename = _entry_name(entry, idx)
        flt_mo.add_child(MO(
            "vzEntry",
            dn=f"uni/tn-{t}/flt-{flt.name}/e-{ename}",
            name=ename,
            etherT=entry.ether_type,
            prot=entry.proto,
            dFromPort=entry.from_port,
            dToPort=entry.to_port,
            sFromPort="unspecified",
            sToPort="unspecified",
            stateful="yes",
        ))
    return flt_mo


def _build_tenant(topo: Topology, site: Site, tenant: Tenant, store: MITStore) -> MO:
    t = tenant.name
    phys_dom = topo.access.phys_domains[0].name if topo.access.phys_domains else "phys-dom-1"

    tenant_mo = MO("fvTenant", dn=f"uni/tn-{t}", name=t, descr="")

    # fvCtx per VRF — batch-2: pcTag (VRF-level sclass; zoning_rules.py's
    # vzAny-scoped lookup falls back to this when no EPG-level pcTag matches)
    # + scopeId (this VRF's own zoning scope — every actrlRule/fvEpP sharing
    # this VRF shares the same scopeId, per scope_id_for's docstring).
    for vrf in tenant.vrfs:
        tenant_mo.add_child(MO(
            "fvCtx",
            dn=f"uni/tn-{t}/ctx-{vrf.name}",
            name=vrf.name,
            pcEnfPref="enforced",
            bdEnforcedEnable="no",
            ipDataPlaneLearning="enabled",
            pcEnfDir="ingress",
            pcTag=pctag_for(f"uni/tn-{t}/ctx-{vrf.name}"),
            scope=scope_id_for(t, vrf.name),
        ))

    # fvBD per BD
    for bd in tenant.bds:
        tenant_mo.add_child(_build_bd(topo, site, t, bd, tenant.l3outs))

    # fvAp / fvAEPg (+ batch-2 fvEpP, a separate top-level MO under uni/epp/,
    # NOT nested under the tenant tree — real ACI's EPG-profile class lives
    # in its own uni/epp/ subtree, mirrored here as a sibling store.add call
    # rather than a tenant_mo child).
    bd_vrf = {bd.name: bd.vrf for bd in tenant.bds}
    for ap in tenant.aps:
        ap_mo = MO("fvAp", dn=f"uni/tn-{t}/ap-{ap.name}", name=ap.name)
        for epg in ap.epgs:
            epg_mo = _build_epg(t, ap.name, epg, phys_dom, store)
            ap_mo.add_child(epg_mo)
            vrf_name = bd_vrf.get(epg.bd, "")
            store.add(MO(
                "fvEpP",
                dn=f"uni/epp/fv-[{epg_mo.dn}]",
                epgPKey=epg_mo.dn,
                pcTag=epg_mo.attrs["pcTag"],
                scopeId=scope_id_for(t, vrf_name) if vrf_name else "",
            ))
        tenant_mo.add_child(ap_mo)

    # vzBrCP contracts — batch-1: attach vzRsSubjGraphAtt to exactly ONE
    # existing contract subject (the tenant's first contract, if any).
    graph_target = tenant.contracts[0].name if tenant.contracts else None
    for contract in tenant.contracts:
        tenant_mo.add_child(_build_contract(t, contract, attach_graph=(contract.name == graph_target)))

    # vzFilter (deduplicated by name)
    seen: set[str] = set()
    for contract in tenant.contracts:
        for flt in contract.filters:
            if flt.name in seen:
                continue
            seen.add(flt.name)
            tenant_mo.add_child(_build_filter(t, flt))

    return tenant_mo


def build(topo: Topology, site: Site, store: MITStore) -> None:
    """Emit fvTenant trees for every tenant deployed at *site*."""
    for tenant in topo.tenants:
        if not _is_deployed(tenant, site):
            continue
        store.add(_build_tenant(topo, site, tenant, store))
