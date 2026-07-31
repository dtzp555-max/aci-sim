"""NDO -> APIC deploy mirror.

Confirmed SIM GAP: `POST /mso/api/v1/task` (the deploy/undeploy request
`cisco.mso.ndo_schema_template_deploy` sends — see ndo/app.py's docstring on
that route) has always been a pure ack no-op: it never materializes the
deployed schema template's VRFs/BDs/ANPs/EPGs/binds into the TARGET SITES'
APIC MITStores. A real E2E Ansible run never direct-POSTs those MOs to the
APIC for a multi-site tenant either — it relies entirely on NDO's deploy to
push them down. So today, multi-site EPGs exist in the NDO schema doc but
never appear on the APIC side of this sim, which breaks any playbook/test
that reads EPGs back from the APIC after an NDO-driven multi-site deploy.

`mirror_template_to_sites()` closes that gap: given the NDO state, a
template name, and a site_id -> ApicSiteState map, it walks the owning
schema's template (vrfs/bds/anps/epgs) plus each associated SITE entry
(hostBasedRouting overlay, domainAssociations, staticPorts) and upserts the
equivalent fvCtx/fvBD/fvAp/fvAEPg (+ children) MOs into every target site's
MITStore — reusing the exact DN/attribute conventions build/tenants.py's
boot-time builders and rest_aci/writes.py's POST-write path already
establish, so a mirrored MO is indistinguishable from a boot-built or
directly-POSTed one.

Pure and HTTP-free: no FastAPI/Request dependency, so it's unit-testable
without spinning up either app.
"""
from __future__ import annotations

from typing import Any

from aci_sim.build.tenants import pctag_for
from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.rest_aci.writes import _CLASS_DEFAULTS

from .model import NdoState


#: Leaf object-name keys inside a DICT-form NDO ref, in priority order. An
#: epgRef carries BOTH anpName and epgName, so epgName is checked first (the
#: EPG's own name wins); every other ref dict carries only its own *Name key.
#: schemaId/templateName are deliberately excluded.
_REF_NAME_KEYS = ("epgName", "bdName", "vrfName", "contractName", "anpName", "name")


def _basename(ref: Any) -> str:
    """Return the object name for an NDO `*Ref`, handling BOTH forms it uses.

    - STRING ref `/schemas/<id>/templates/<tmpl>/bds/<bdName>` -> last segment.
    - DICT ref `{"vrfName": ..., "schemaId": ..., "templateName": ...}` -> the
      leaf object-name key (see `_REF_NAME_KEYS`). aci-ansible's cisco.mso
      modules store the dict shape; aci-py stringifies its refs — so the mirror
      must accept either or it AttributeErrors (`'dict'.rsplit`) on a redeploy
      of an aci-ansible multi-site tenant.

    The last segment / leaf name already matches the APIC MO name (see
    ndo/model.py's docstring: NDO names are derived verbatim from the APIC
    fvBD/fvAp/fvAEPg names at build time).
    """
    if isinstance(ref, dict):
        for key in _REF_NAME_KEYS:
            value = ref.get(key)
            if isinstance(value, str) and value:
                return value
        return ""
    if not ref:
        return ""
    return ref.rsplit("/", 1)[-1]


def _tenant_name(state: NdoState, tenant_id: str) -> str:
    """Resolve a template's `tenantId` to the tenant's APIC-facing name."""
    for tenant in state.tenants:
        if tenant.get("id") == tenant_id:
            return tenant.get("name", "")
    return ""


def _find_template(
    state: NdoState, template_name: str, schema_id: str | None = None
) -> tuple[dict | None, dict | None]:
    """Locate (schema_detail, template_doc) for a template named *template_name*.

    If *schema_id* is given, resolve ONLY within that schema — template names
    are NOT unique across schemas (every multi-site tenant's schema commonly
    has templates literally named e.g. 'LAB1-LAB2'/'LAB1'/'LAB2'), so a
    cross-schema scan risks mirroring the WRONG tenant. When *schema_id* is
    None, fall back to the legacy scan-every-schema behavior (kept for
    existing callers/tests that don't have a schema id handy).
    """
    if schema_id is not None:
        sd = state.schema_details.get(schema_id)
        if sd is None:
            return None, None
        for tmpl in sd.get("templates", []):
            if tmpl.get("name") == template_name:
                return sd, tmpl
        return sd, None

    for schema_detail in state.schema_details.values():
        for tmpl in schema_detail.get("templates", []):
            if tmpl.get("name") == template_name:
                return schema_detail, tmpl
    return None, None


def _site_entries_for_template(schema_detail: dict, template_name: str) -> list[dict]:
    """Return every top-level `sites[]` entry belonging to *template_name*."""
    return [
        site
        for site in schema_detail.get("sites", [])
        if isinstance(site, dict) and site.get("templateName") == template_name
    ]


def _find_site_bd(site_entry: dict, bd_name: str) -> dict | None:
    for bd in site_entry.get("bds", []) or []:
        if isinstance(bd, dict) and _basename(bd.get("bdRef")) == bd_name:
            return bd
    return None


def _find_site_epg(site_entry: dict, anp_name: str, epg_name: str) -> dict | None:
    for anp in site_entry.get("anps", []) or []:
        if not isinstance(anp, dict) or _basename(anp.get("anpRef")) != anp_name:
            continue
        for epg in anp.get("epgs", []) or []:
            if isinstance(epg, dict) and _basename(epg.get("epgRef")) == epg_name:
                return epg
    return None


def _prune_stale_dhcp_labels(store: MITStore, bd_dn: str, keep: set[str]) -> None:
    """Drop dhcpLbl children of *bd_dn* that are no longer declared.

    upsert merges, so a label removed in NDO would otherwise stay on the site
    forever. Scoped to this BD's own children — nothing else is touched.
    """
    for child in store.children(bd_dn, {"dhcpLbl"}):
        if child.attrs.get("name", "") not in keep:
            store.delete(child.dn)


def _prune_orphan_relay_policies(store: MITStore, tenant: str) -> None:
    """Drop dhcpRelayP that no BD on this site labels any more.

    Keyed on what the SITE still references rather than on what any one template
    declares: a policy is here because some BD's label pulled it in, so when no
    label names it, nothing on this site needs it. Deleting by template instead
    would remove a policy a different deployed template's BD still points at.
    """
    prefix = f"uni/tn-{tenant}/"
    referenced = {
        lbl.attrs.get("name", "")
        for lbl in store.by_class("dhcpLbl")
        if lbl.dn.startswith(prefix)
    }
    for relay in store.by_class("dhcpRelayP"):
        if not relay.dn.startswith(prefix):
            continue
        if relay.attrs.get("name", "") not in referenced:
            store.delete(relay.dn)


def _mirror_bd(store: MITStore, tenant: str, bd: dict, site_bd: dict | None) -> None:
    """Upsert fvCtx-referencing fvBD (+ fvRsCtx/fvSubnet children) for one
    template BD, overlaid with the SITE bd's hostBasedRouting."""
    bd_name = bd.get("name", "")
    vrf_name = _basename(bd.get("vrfRef"))
    bd_dn = f"uni/tn-{tenant}/BD-{bd_name}"

    l2_unknown_ucast = bd.get("l2UnknownUnicast", "flood")
    unicast_routing = bool(bd.get("unicastRouting", True))
    host_based_routing = bool(site_bd.get("hostBasedRouting")) if site_bd else False

    attrs: dict[str, Any] = dict(_CLASS_DEFAULTS.get("fvBD", {}))
    attrs.update(
        {
            "dn": bd_dn,
            "name": bd_name,
            "epMoveDetectMode": bd.get("epMoveDetectMode", ""),
            "arpFlood": "yes" if bd.get("arpFlood") else "no",
            "unkMacUcastAct": l2_unknown_ucast,
            "unicastRoute": "yes" if unicast_routing else "no",
            "hostBasedRouting": "yes" if host_based_routing else "no",
            "intersiteBumTrafficAllow": "yes" if bd.get("intersiteBumTrafficAllow") else "no",
        }
    )
    bd_mo = MO("fvBD", **attrs)
    if vrf_name:
        bd_mo.add_child(MO(
            "fvRsCtx",
            dn=f"{bd_dn}/rsctx",
            tnFvCtxName=vrf_name,
        ))
    for subnet in bd.get("subnets", []) or []:
        if not isinstance(subnet, dict):
            continue
        ip = subnet.get("ip")
        if not ip:
            continue
        scope = subnet.get("scope") or "public,shared"
        bd_mo.add_child(MO(
            "fvSubnet",
            dn=f"{bd_dn}/subnet-[{ip}]",
            ip=ip,
            scope=scope,
            preferred="yes" if subnet.get("primary") else "no",
        ))
    declared: set[str] = set()
    for label in bd.get("dhcpLabels", []) or []:
        if not isinstance(label, dict):
            continue
        label_name = label.get("name") or _basename(label.get("ref"))
        if not label_name:
            continue
        declared.add(label_name)
        bd_mo.add_child(MO(
            "dhcpLbl",
            dn=f"{bd_dn}/dhcplbl-{label_name}",
            name=label_name,
            owner="tenant",
        ))
    _prune_stale_dhcp_labels(store, bd_dn, declared)

    # Merge-upsert (NOT delete-then-recreate) on purpose: the BD may carry an
    # externally-POSTed `epClear` attr (the clear_remote_mac stub a playbook
    # posts directly to the APIC) that a delete-then-recreate would drop.
    # store.upsert() merges attrs/children onto any existing MO at this dn,
    # so that attr survives a redeploy. Contrast with _mirror_epg below,
    # which deletes-then-recreates because EPGs have no such externally-set
    # state to preserve.
    store.upsert(bd_mo)


def _encap_of(path: dict) -> str:
    """APIC-style encap string for one NDO staticPorts entry.

    NDO uses `portEncapVlan` (a bare int, e.g. 110); APIC wants "vlan-110".
    An explicit `encap` already in APIC form wins, so a hand-written or
    directly-POSTed entry keeps working.
    """
    encap = path.get("encap")
    if encap:
        return str(encap)
    vlan = path.get("portEncapVlan")
    if vlan in (None, ""):
        return ""
    return str(vlan) if str(vlan).startswith(("vlan-", "vxlan-")) else f"vlan-{vlan}"


def _mirror_epg(store: MITStore, tenant: str, anp_name: str, epg: dict, site_epg: dict | None) -> None:
    """Upsert fvAEPg (+ fvRsBd/fvRsProv/fvRsCons/fvSubnet/fvRsDomAtt/
    fvRsPathAtt children) for one template EPG, overlaid with the SITE epg's
    domainAssociations/staticPorts (bind data — usually empty until a bind
    playbook runs)."""
    epg_name = epg.get("name", "")
    epg_dn = f"uni/tn-{tenant}/ap-{anp_name}/epg-{epg_name}"

    epg_mo = MO(
        "fvAEPg",
        dn=epg_dn,
        name=epg_name,
        prefGrMemb="include" if epg.get("preferredGroup") else "exclude",
        pcTag=pctag_for(epg_dn),
        pcEnfPref="unenforced",
        isAttrBasedEPg="no",
        floodOnEncap="disabled",
    )
    bd_name = _basename(epg.get("bdRef"))
    if bd_name:
        epg_mo.add_child(MO(
            "fvRsBd",
            dn=f"{epg_dn}/rsbd",
            tnFvBDName=bd_name,
        ))

    for rel in epg.get("contractRelationships", []) or []:
        if not isinstance(rel, dict):
            continue
        contract = _basename(rel.get("contractRef"))
        if not contract:
            continue
        if rel.get("relationshipType") == "provider":
            epg_mo.add_child(MO(
                "fvRsProv",
                dn=f"{epg_dn}/rsprov-{contract}",
                tnVzBrCPName=contract,
            ))
        elif rel.get("relationshipType") == "consumer":
            epg_mo.add_child(MO(
                "fvRsCons",
                dn=f"{epg_dn}/rscons-{contract}",
                tnVzBrCPName=contract,
            ))

    for subnet in epg.get("subnets", []) or []:
        if not isinstance(subnet, dict):
            continue
        ip = subnet.get("ip")
        if not ip:
            continue
        epg_mo.add_child(MO(
            "fvSubnet",
            dn=f"{epg_dn}/subnet-[{ip}]",
            ip=ip,
            scope=subnet.get("scope") or "public,shared",
            preferred="yes" if subnet.get("primary") else "no",
        ))

    if site_epg is not None:
        for dom in site_epg.get("domainAssociations", []) or []:
            if not isinstance(dom, dict):
                continue
            t_dn = dom.get("dn") or dom.get("domainRef") or dom.get("tDn")
            if not t_dn:
                continue
            # NDO carries both immediacies per association; a real APIC shows
            # them on the fvRsDomAtt, and cisco.aci's aci_epg_to_domain reads
            # them back. Hardcoding instrImedcy="lazy" and omitting resImedcy
            # made every mirrored binding look unconfigured.
            epg_mo.add_child(MO(
                "fvRsDomAtt",
                dn=f"{epg_dn}/rsdomAtt-[{t_dn}]",
                tDn=t_dn,
                instrImedcy=dom.get("deploymentImmediacy") or "lazy",
                resImedcy=dom.get("resolutionImmediacy") or "lazy",
            ))
        for path in site_epg.get("staticPorts", []) or []:
            if not isinstance(path, dict):
                continue
            path_dn = path.get("path") or path.get("dn") or path.get("tDn")
            if not path_dn:
                continue
            # NDO names the VLAN `portEncapVlan` and stores it as a bare int
            # (110); APIC wants the encap string ("vlan-110"). Reading "encap"
            # meant every mirrored static port landed with an empty encap —
            # invalid on real gear, and blank in any tool that reads it back.
            epg_mo.add_child(MO(
                "fvRsPathAtt",
                dn=f"{epg_dn}/rspathAtt-[{path_dn}]",
                tDn=path_dn,
                encap=_encap_of(path),
                mode=path.get("mode", "regular"),
                instrImedcy=path.get("deploymentImmediacy", "lazy"),
            ))

    # Redeploy idempotency: delete-then-recreate (NOT a merge-upsert like
    # _mirror_bd). store.upsert() merges attrs/children onto an existing MO,
    # so a contract/bind removed in NDO since the last deploy would otherwise
    # leave a stale fvRsProv/fvRsDomAtt/etc. child behind forever. This is
    # safe because multi-site EPGs have NO externally-posted children of
    # their own — every child under this dn comes from this mirror, so
    # wiping the subtree first and rebuilding it clean can't drop anything
    # an outside POST relied on (contrast with the BD's epClear, above).
    store.upsert(MO("fvAEPg", dn=epg_dn, status="deleted"))
    store.upsert(epg_mo)


def _find_extepg_l3out(schema_detail: dict, extepg_name: str) -> str:
    """Which L3Out owns *extepg_name*, across every template in this schema.

    A relay provider names the external EPG but not its L3Out, and the APIC dn
    needs both (`uni/tn-T/out-L/instP-E`). External EPG names are unique within
    a tenant in practice, so a schema-wide scan resolves it.
    """
    for tmpl in schema_detail.get("templates", []) or []:
        for xepg in tmpl.get("externalEpgs", []) or []:
            if isinstance(xepg, dict) and xepg.get("name") == extepg_name:
                return _basename(xepg.get("l3outRef"))
    return ""


def _find_epg_anp(schema_detail: dict, epg_name: str) -> str:
    """Which application profile holds *epg_name*, across this schema."""
    for tmpl in schema_detail.get("templates", []) or []:
        for anp in tmpl.get("anps", []) or []:
            if not isinstance(anp, dict):
                continue
            for epg in anp.get("epgs", []) or []:
                if isinstance(epg, dict) and epg.get("name") == epg_name:
                    return anp.get("name", "")
    return ""


def _provider_tdn(schema_detail: dict, tenant: str, provider: dict) -> str:
    """APIC target dn for one DHCP relay provider entry.

    NDO models the two provider kinds with different keys — an application EPG
    (`epgName`) or an L3Out external EPG (`externalEpgName`), matching the
    DHCP1/DHCP2 split in `create_dhcp_relay.j2.yml`.
    """
    extepg = provider.get("externalEpgName")
    if extepg:
        l3out = _find_extepg_l3out(schema_detail, extepg)
        if not l3out:
            return ""
        return f"uni/tn-{tenant}/out-{l3out}/instP-{extepg}"
    epg = provider.get("epgName")
    if epg:
        anp = _find_epg_anp(schema_detail, epg)
        if not anp:
            return ""
        return f"uni/tn-{tenant}/ap-{anp}/epg-{epg}"
    return ""


def _relay_policies_for_tenant(state: NdoState, tenant_id: str) -> list[dict]:
    """Every DHCP relay policy in this tenant's tenantPolicy template(s)."""
    policies: list[dict] = []
    for doc in getattr(state, "tenant_policy_templates", {}).values():
        if not isinstance(doc, dict):
            continue
        tmpl = doc.get("tenantPolicyTemplate", {}).get("template", {})
        if tmpl.get("tenantId") != tenant_id:
            continue
        for pol in tmpl.get("dhcpRelayPolicies", []) or []:
            if isinstance(pol, dict) and pol.get("name"):
                policies.append(pol)
    return policies


def _mirror_dhcp_relay_policies(
    store: MITStore,
    state: NdoState,
    schema_detail: dict,
    tenant: str,
    tenant_id: str,
    template: dict,
) -> int:
    """Materialize the dhcpRelayP objects that this template's BD labels name.

    Only the REFERENCED policies are mirrored, not the whole tenantPolicy
    template. Nothing deployed that template, so its other policies have no
    business on a site — but a `dhcpLbl` pointing at an absent `dhcpRelayP` is
    a dangling reference of exactly the kind `_mirror_contracts` already exists
    to prevent, so the ones a deployed BD names do get materialized.
    """
    referenced = {
        label.get("name") or _basename(label.get("ref"))
        for bd in template.get("bds", []) or []
        if isinstance(bd, dict)
        for label in bd.get("dhcpLabels", []) or []
        if isinstance(label, dict)
    }
    referenced.discard("")
    referenced.discard(None)
    if not referenced:
        return 0

    written = 0
    for pol in _relay_policies_for_tenant(state, tenant_id):
        name = pol.get("name", "")
        if name not in referenced:
            continue
        relay_dn = f"uni/tn-{tenant}/relayp-{name}"
        relay = MO(
            "dhcpRelayP",
            dn=relay_dn,
            name=name,
            descr=pol.get("description", ""),
            owner="tenant",
            mode="visible",
        )
        for prov in pol.get("providers", []) or []:
            if not isinstance(prov, dict):
                continue
            tdn = _provider_tdn(schema_detail, tenant, prov)
            addr = prov.get("ip", "")
            if not tdn or not addr:
                continue
            relay.add_child(MO(
                "dhcpRsProv",
                dn=f"{relay_dn}/rsprov-[{tdn}]",
                tDn=tdn,
                addr=addr,
            ))
        store.upsert(relay)
        written += 1
    return written


def _template_object_dns(tenant: str, template: dict) -> list[tuple[str, str]]:
    """Every tenant-scoped (dn, class_name) pair this template owns (its
    VRFs/BDs/ANPs/EPGs) — used to scope an undeploy's delete to just this
    template's objects, never the whole `uni/tn-<T>` tenant (a schema may
    hold more than one template against the same tenant).

    Deletion in the store is DN-based (status="deleted" removes by dn
    regardless of the class passed), so the class here only matters for
    fidelity — the emitted delete MO now carries the object's real class
    instead of a hardcoded 'fvBD' for everything.
    """
    dns: list[tuple[str, str]] = []
    for vrf in template.get("vrfs", []) or []:
        name = vrf.get("name") if isinstance(vrf, dict) else None
        if name:
            dns.append((f"uni/tn-{tenant}/ctx-{name}", "fvCtx"))
    for bd in template.get("bds", []) or []:
        name = bd.get("name") if isinstance(bd, dict) else None
        if name:
            dns.append((f"uni/tn-{tenant}/BD-{name}", "fvBD"))
    for anp in template.get("anps", []) or []:
        if not isinstance(anp, dict):
            continue
        anp_name = anp.get("name")
        if not anp_name:
            continue
        dns.append((f"uni/tn-{tenant}/ap-{anp_name}", "fvAp"))
        for epg in anp.get("epgs", []) or []:
            epg_name = epg.get("name") if isinstance(epg, dict) else None
            if epg_name:
                dns.append((f"uni/tn-{tenant}/ap-{anp_name}/epg-{epg_name}", "fvAEPg"))
    for flt in template.get("filters", []) or []:
        name = flt.get("name") if isinstance(flt, dict) else None
        if name:
            dns.append((f"uni/tn-{tenant}/flt-{name}", "vzFilter"))
    for con in template.get("contracts", []) or []:
        name = con.get("name") if isinstance(con, dict) else None
        if name:
            dns.append((f"uni/tn-{tenant}/brc-{name}", "vzBrCP"))
    for graph in template.get("serviceGraphs", []) or []:
        name = graph.get("name") if isinstance(graph, dict) else None
        if name:
            dns.append((f"uni/tn-{tenant}/AbsGraph-{name}", "vnsAbsGraph"))
    # Relay policies are NOT listed here. Deleting relayp-<name> because this
    # template names it would take out a policy another deployed template's BD
    # still labels. _prune_orphan_relay_policies removes the ones nothing on the
    # site references any more, which is the same cleanup without the collateral.
    return dns


def _mirror_contracts(store: MITStore, tenant: str, template: dict) -> int:
    """Mirror the template's filters/contracts/service-graphs into a site
    APIC store as their shadow MOs (vzFilter/vzEntry, vzBrCP/vzSubj + the
    filter and service-graph subject bindings, vnsAbsGraph).

    A real NDO deploy creates these shadow objects on every target site's
    APIC; without them the mirrored EPGs' fvRsProv/fvRsCons are DANGLING
    references — same family as F1 (children/relations exist, the referenced
    object MO doesn't), surfaced 2026-07-07 when `GET /api/class/vzBrCP`
    returned 0 fabric-wide despite deployed MS tenants providing/consuming
    contracts. Shadow-subject naming: one vzSubj per contract, named after
    the contract ("con-X" -> "sub-X"), carrying the filter attachments and
    the service-graph binding (tnVnsAbsGraphName) when the NDO contract has
    a serviceGraphRelationship."""
    written = 0
    for flt in template.get("filters", []) or []:
        if not isinstance(flt, dict) or not flt.get("name"):
            continue
        flt_name = flt["name"]
        flt_dn = f"uni/tn-{tenant}/flt-{flt_name}"
        store.upsert(MO("vzFilter", dn=flt_dn, name=flt_name))
        written += 1
        for entry in flt.get("entries", []) or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            store.upsert(MO(
                "vzEntry",
                dn=f"{flt_dn}/e-{entry['name']}",
                name=entry["name"],
                etherT=entry.get("etherType", "unspecified"),
                prot=entry.get("ipProtocol", "unspecified"),
            ))
            written += 1
    for con in template.get("contracts", []) or []:
        if not isinstance(con, dict) or not con.get("name"):
            continue
        con_name = con["name"]
        con_dn = f"uni/tn-{tenant}/brc-{con_name}"
        store.upsert(MO("vzBrCP", dn=con_dn, name=con_name, scope=con.get("scope", "context")))
        written += 1
        subj_name = f"sub-{con_name[4:]}" if con_name.startswith("con-") else f"sub-{con_name}"
        subj_dn = f"{con_dn}/subj-{subj_name}"
        store.upsert(MO("vzSubj", dn=subj_dn, name=subj_name))
        written += 1
        for rel in con.get("filterRelationships", []) or []:
            ref = rel.get("filterRef", "") if isinstance(rel, dict) else ""
            rel_flt = _basename(ref)
            if not rel_flt:
                continue
            store.upsert(MO(
                "vzRsSubjFiltAtt",
                dn=f"{subj_dn}/rssubjFiltAtt-{rel_flt}",
                tnVzFilterName=rel_flt,
            ))
            written += 1
        graph_rel = con.get("serviceGraphRelationship") or {}
        graph_name = _basename(graph_rel.get("serviceGraphRef", "")) if isinstance(graph_rel, dict) else ""
        if graph_name:
            store.upsert(MO(
                "vzRsSubjGraphAtt",
                dn=f"{subj_dn}/rsSubjGraphAtt",
                tnVnsAbsGraphName=graph_name,
            ))
            written += 1
    for graph in template.get("serviceGraphs", []) or []:
        if not isinstance(graph, dict) or not graph.get("name"):
            continue
        store.upsert(MO(
            "vnsAbsGraph",
            dn=f"uni/tn-{tenant}/AbsGraph-{graph['name']}",
            name=graph["name"],
        ))
        written += 1
    return written


def mirror_template_to_sites(
    state: NdoState,
    template_name: str,
    apic_states: dict,
    *,
    schema_id: str | None = None,
    undeploy: bool = False,
) -> int:
    """Materialize (or, if *undeploy*, tear down) *template_name*'s
    VRFs/BDs/ANPs/EPGs into every associated site's APIC MITStore.

    *schema_id*, when known (the real `POST /mso/api/v1/task` body always
    carries one), scopes template resolution to that ONE schema — template
    names collide across schemas (e.g. every multi-site tenant's schema has
    templates literally named 'LAB1-LAB2'/'LAB1'/'LAB2'), so resolving by
    name alone across ALL schemas risks mirroring the WRONG tenant. Pass
    None only for legacy callers/tests that don't have a schema id handy.

    *apic_states* maps site_id -> an object exposing a `.store` (MITStore) —
    typically `aci_sim.rest_aci.app.ApicSiteState`. Only sites that
    are BOTH associated with this template (schema `sites[].templateName ==
    template_name`) AND present in *apic_states* are touched; every other
    site's store is left completely untouched.

    Returns the number of MOs upserted/deleted (0 if the template or none of
    its sites could be resolved — a safe no-op).
    """
    schema_detail, template = _find_template(state, template_name, schema_id)
    if template is None or schema_detail is None:
        return 0

    tenant = _tenant_name(state, template.get("tenantId", ""))
    if not tenant:
        return 0

    site_entries = _site_entries_for_template(schema_detail, template_name)
    if not site_entries:
        return 0

    written = 0

    if undeploy:
        object_dns = _template_object_dns(tenant, template)
        for site_entry in site_entries:
            site_id = site_entry.get("siteId")
            apic_state = apic_states.get(str(site_id)) if apic_states else None
            if apic_state is None:
                continue
            store = apic_state.store
            for dn, cls in object_dns:
                store.upsert(MO(cls, dn=dn, status="deleted"))
                written += 1
            # The BDs are gone, so their labels went with them; anything those
            # labels were holding on the site is now unreferenced.
            _prune_orphan_relay_policies(store, tenant)
        return written

    for site_entry in site_entries:
        site_id = site_entry.get("siteId")
        apic_state = apic_states.get(str(site_id)) if apic_states else None
        if apic_state is None:
            continue
        store = apic_state.store

        # Materialize the tenant shadow on this site FIRST. An NDO deploy
        # creates the tenant on each target site's APIC; without the fvTenant
        # root MO, a subtree/mo query on `uni/tn-<T>` returns empty even though
        # the mirrored children exist (real F1 root cause: the direct-POST path
        # only creates fvTenant on the --apic site, so the OTHER site had the
        # mirrored VRF/BD/EPG children but no tenant root to query them under).
        store.upsert(MO("fvTenant", dn=f"uni/tn-{tenant}", name=tenant))
        written += 1

        for vrf in template.get("vrfs", []) or []:
            if not isinstance(vrf, dict):
                continue
            vrf_name = vrf.get("name", "")
            if not vrf_name:
                continue
            store.upsert(MO(
                "fvCtx",
                dn=f"uni/tn-{tenant}/ctx-{vrf_name}",
                name=vrf_name,
            ))
            written += 1

        for bd in template.get("bds", []) or []:
            if not isinstance(bd, dict):
                continue
            bd_name = bd.get("name", "")
            if not bd_name:
                continue
            site_bd = _find_site_bd(site_entry, bd_name)
            _mirror_bd(store, tenant, bd, site_bd)
            written += 1

        for anp in template.get("anps", []) or []:
            if not isinstance(anp, dict):
                continue
            anp_name = anp.get("name", "")
            if not anp_name:
                continue
            store.upsert(MO(
                "fvAp",
                dn=f"uni/tn-{tenant}/ap-{anp_name}",
                name=anp_name,
            ))
            written += 1
            for epg in anp.get("epgs", []) or []:
                if not isinstance(epg, dict):
                    continue
                epg_name = epg.get("name", "")
                if not epg_name:
                    continue
                site_epg = _find_site_epg(site_entry, anp_name, epg_name)
                _mirror_epg(store, tenant, anp_name, epg, site_epg)
                written += 1

        # Contract/filter/service-graph shadows for this site — see
        # _mirror_contracts (dangling fvRsProv/fvRsCons fix).
        written += _mirror_contracts(store, tenant, template)

        # DHCP relay policies named by this template's BD labels.
        written += _mirror_dhcp_relay_policies(
            store, state, schema_detail, tenant, template.get("tenantId", ""), template
        )
        _prune_orphan_relay_policies(store, tenant)

    return written
