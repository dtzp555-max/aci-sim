"""build/access.py — VLAN pools, physical/L3 domains, AAEPs, access port IPGs,
and (batch-1) the leaf interface-policy cluster: infraAccPortP/infraHPortS/
infraPortBlk/infraRsAccBaseGrp/infraAccBndlGrp.

Site-independent: call once per topology (site arg accepted for API uniformity
but not used in the selection logic).

Interface-policy cluster (CONTRACT.md §6 "Access policy") — real ACI DN/RN
conventions, verified against the existing infra tree this module already
builds (uni/infra/... prefix) and against autoACI's access_policy.py (read via
prior audit, see docs/CONTRACT.md header):
  infraAccPortP   uni/infra/accportprof-{name}
  infraHPortS     {accportp}/hports-{name}-typ-range
  infraPortBlk    {hports}/portblk-block{n}
  infraRsAccBaseGrp  {hports}/rsaccBaseGrp
  infraAccBndlGrp uni/infra/funcprof/accbundle-{name}

Port ranges in infraPortBlk MUST match ports that actually exist in the
l1PhysIf inventory interfaces.py builds (eth1/1.._HOST_PORTS on leaves) — the
whole cluster is built once per border-leaf vPC pair (the topology's only vPC
domains, per fabric.py's vpcDom/vpcIf) plus one profile per regular-leaf pair,
so infraAccBndlGrp(lagT="node") lines up with a real vpcDom this same site
already has.
"""
from __future__ import annotations

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import AAEP, L3Domain, PhysDomain, Site, Topology, VlanPool, VMMDomain
from aci_sim.build.interfaces import _HOST_PORTS


def _build_vlan_pool(pool: VlanPool) -> MO:
    """Build fvnsVlanInstP + fvnsEncapBlk child."""
    pool_dn = f"uni/infra/vlanns-[{pool.name}]-static"
    blk_dn = (
        f"{pool_dn}"
        f"/from-[vlan-{pool.from_vlan}]-to-[vlan-{pool.to_vlan}]"
    )
    pool_mo = MO("fvnsVlanInstP", dn=pool_dn, name=pool.name, allocMode="static")
    blk_mo = MO(
        "fvnsEncapBlk",
        dn=blk_dn,
        allocMode="static",
        **{"from": f"vlan-{pool.from_vlan}", "to": f"vlan-{pool.to_vlan}"},
    )
    pool_mo.add_child(blk_mo)
    return pool_mo


def _build_phys_dom(phys_dom: PhysDomain, first_pool_dn: str) -> MO:
    """Build physDomP + optional infraRsVlanNs child."""
    dom_mo = MO("physDomP", dn=f"uni/phys-{phys_dom.name}", name=phys_dom.name)
    if first_pool_dn:
        dom_mo.add_child(MO(
            "infraRsVlanNs",
            dn=f"uni/phys-{phys_dom.name}/rsvlanNs",
            tDn=first_pool_dn,
        ))
    return dom_mo


def _build_l3_dom(l3_dom: L3Domain, first_pool_dn: str) -> MO:
    """Build l3extDomP + optional infraRsVlanNs child."""
    l3dom_mo = MO("l3extDomP", dn=f"uni/l3dom-{l3_dom.name}", name=l3_dom.name)
    if first_pool_dn:
        l3dom_mo.add_child(MO(
            "infraRsVlanNs",
            dn=f"uni/l3dom-{l3_dom.name}/rsvlanNs",
            tDn=first_pool_dn,
        ))
    return l3dom_mo


def _build_vmm_domain(vmm: VMMDomain, first_pool_dn: str) -> MO:
    """Build vmmDomP (+ vmmCtrlrP + vmmUsrAccP if vcenter_ip set, + infraRsVlanNs).

    Tier-2 (PR-19). Real ACI DN scheme (verified against aci-py's
    `_domain_dn_class` shim — see docs/DESIGN.md PR-19 note):
      vmmDomP    uni/vmmp-VMware/dom-{name}
      vmmCtrlrP  {vmmDomP dn}/ctrlr-{name}      hostOrIp=vcenter_ip, rootContName=datacenter
      vmmUsrAccP {vmmDomP dn}/usracc-{name}     placeholder credential-profile ref (no
                 real credentials in a sim — name-only, matches the pattern
                 phys/l3 domains use for their own no-secret child objects)

    Only "VMware" (vmmProvP=VMware) is modeled — the sole provider the
    aci-py `bind_epg_to_vmm_domain` playbook chain (and the DN map in
    `mso_schema_site_anp_epg_domain.py`, `dn_map["vmmDomain"] =
    "uni/vmmp-VMware/dom-{0}"`) actually exercises today.

    vmmCtrlrP/vmmUsrAccP are only emitted when `vcenter_ip` is set — a bare
    `{name: ...}` VMM domain entry (no vCenter attrs) produces just the
    vmmDomP + optional infraRsVlanNs, same shape as a phys/l3 domain with no
    extra config, so a topology author who only wants a named domain isn't
    forced to supply vCenter connection details.
    """
    dom_dn = f"uni/vmmp-VMware/dom-{vmm.name}"
    dom_mo = MO("vmmDomP", dn=dom_dn, name=vmm.name)
    if vmm.vcenter_ip:
        ctrlr_dn = f"{dom_dn}/ctrlr-{vmm.name}"
        ctrlr_mo = MO(
            "vmmCtrlrP",
            dn=ctrlr_dn,
            name=vmm.name,
            hostOrIp=vmm.vcenter_ip,
            rootContName=vmm.datacenter or vmm.name,
            dvsVersion="unmanaged",
        )
        if vmm.vcenter_dvs:
            ctrlr_mo.attrs["dvsName"] = vmm.vcenter_dvs
        dom_mo.add_child(ctrlr_mo)
        dom_mo.add_child(MO(
            "vmmUsrAccP",
            dn=f"{ctrlr_dn}/usracc-{vmm.name}",
            name=vmm.name,
        ))
    if vmm.vlan_pool and first_pool_dn:
        # first_pool_dn is topo.access.vlan_pools[0] (the same convention
        # phys/l3 domains use below); Tier-2 vmm.vlan_pool is validated
        # against the real pool-name set in schema.py's normalize_and_validate
        # but this builder still binds the module-wide first pool DN (the
        # existing phys/l3-domain convention), matching a dynamic VMM pool
        # binding by name rather than resolving a distinct pool DN per VMM
        # domain — sufficient for the sim's single-pool-per-topology norm.
        dom_mo.add_child(MO(
            "infraRsVlanNs",
            dn=f"{dom_dn}/rsvlanNs",
            tDn=first_pool_dn,
        ))
    return dom_mo


def _build_aaep(
    aaep: AAEP,
    phys_domains: list[PhysDomain],
    l3_domains: list[L3Domain],
) -> MO:
    """Build infraAttEntityP + infraRsDomP children for all domains."""
    aaep_dn = f"uni/infra/attentp-{aaep.name}"
    aaep_mo = MO("infraAttEntityP", dn=aaep_dn, name=aaep.name)
    for phys_dom in phys_domains:
        tdn = f"uni/phys-{phys_dom.name}"
        aaep_mo.add_child(MO(
            "infraRsDomP",
            dn=f"{aaep_dn}/rsdomP-[{tdn}]",
            tDn=tdn,
        ))
    for l3_dom in l3_domains:
        tdn = f"uni/l3dom-{l3_dom.name}"
        aaep_mo.add_child(MO(
            "infraRsDomP",
            dn=f"{aaep_dn}/rsdomP-[{tdn}]",
            tDn=tdn,
        ))
    return aaep_mo


def _build_port_ipg(aaep: AAEP) -> MO:
    """Build infraAccPortGrp + infraRsAttEntP child for one AAEP."""
    ipg_name = f"{aaep.name}-ipg"
    ipg_dn = f"uni/infra/funcprof/accportgrp-{ipg_name}"
    ipg_mo = MO(
        "infraAccPortGrp",
        dn=ipg_dn,
        name=ipg_name,
        lagT="not-aggregated",
    )
    ipg_mo.add_child(MO(
        "infraRsAttEntP",
        dn=f"{ipg_dn}/rsattEntP",
        tDn=f"uni/infra/attentp-{aaep.name}",
    ))
    return ipg_mo


def _build_bndl_grp(name: str, lag_t: str) -> MO:
    """Build infraAccBndlGrp (funcprof bundle IPG) — vPC (lagT=node) or PC (lagT=link)."""
    dn = f"uni/infra/funcprof/accbundle-{name}"
    return MO("infraAccBndlGrp", dn=dn, name=name, lagT=lag_t)


def _build_hports(
    accportp_dn: str,
    sel_name: str,
    from_port: int,
    to_port: int,
    bndl_grp_dn: str,
) -> MO:
    """Build infraHPortS (type=range) + infraPortBlk + infraRsAccBaseGrp children.

    RN scheme verified against this module's own uni/infra/... conventions
    (accportprof-/hports-/portblk-):
      infraHPortS     hports-{sel_name}-typ-range
      infraPortBlk    portblk-block1        (single block covering the range)
      infraRsAccBaseGrp  rsaccBaseGrp       (fixed RN — one target per selector)
    """
    hports_dn = f"{accportp_dn}/hports-{sel_name}-typ-range"
    hports_mo = MO("infraHPortS", dn=hports_dn, name=sel_name, type="range")
    hports_mo.add_child(MO(
        "infraPortBlk",
        dn=f"{hports_dn}/portblk-block1",
        fromPort=str(from_port),
        toPort=str(to_port),
        fromCard="1",
    ))
    hports_mo.add_child(MO(
        "infraRsAccBaseGrp",
        dn=f"{hports_dn}/rsaccBaseGrp",
        tDn=bndl_grp_dn,
    ))
    return hports_mo


def _build_leaf_profile(profile_name: str, sel_name: str, bndl_grp_dn: str) -> MO:
    """Build one infraAccPortP with one infraHPortS selector spanning the
    leaf's host-port range (eth1/1.._HOST_PORTS — the exact inventory
    interfaces.py builds), so every infraPortBlk from/to port exists as a
    real l1PhysIf."""
    accportp_dn = f"uni/infra/accportprof-{profile_name}"
    accportp_mo = MO("infraAccPortP", dn=accportp_dn, name=profile_name)
    accportp_mo.add_child(_build_hports(accportp_dn, sel_name, 1, _HOST_PORTS, bndl_grp_dn))
    return accportp_mo


def build(topo: Topology, site: Site, store: MITStore) -> None:
    """Emit access-policy MOs (site-independent, build once)."""
    # Batch-1 fix: uni/infra previously had no MO of its own — only
    # descendant DNs (uni/infra/vlanns-.../, uni/infra/attentp-.../, etc.),
    # which the store's ancestor-walk (_register) links structurally but
    # never materializes as a real object. That made
    # GET /api/mo/uni/infra.json (any rsp-subtree mode) 404, even though the
    # generic query engine's rsp-subtree=full traversal otherwise works fine
    # (verified: /api/class/infraAccPortP.json?rsp-subtree=full already
    # returns the nested infraHPortS/infraPortBlk/infraRsAccBaseGrp tree).
    # infraInfra is the real ACI class for this container (CONTRACT.md §6
    # "Infra write targets"); seeding it here is a one-line, no-behavior-
    # change fix so `GET /api/mo/uni/infra.json?rsp-subtree=full` (needed to
    # see the whole access-policy tree in one call, same as autoACI's
    # ipg_detail/access_policy plugins would) returns 200 with a real root.
    store.add(MO("infraInfra", dn="uni/infra", name=""))

    first_pool_dn = (
        f"uni/infra/vlanns-[{topo.access.vlan_pools[0].name}]-static"
        if topo.access.vlan_pools else ""
    )

    for pool in topo.access.vlan_pools:
        store.add(_build_vlan_pool(pool))

    for phys_dom in topo.access.phys_domains:
        store.add(_build_phys_dom(phys_dom, first_pool_dn))

    for l3_dom in topo.access.l3_domains:
        store.add(_build_l3_dom(l3_dom, first_pool_dn))

    # Tier-2 (PR-19): VMM domains — optional, defaults to an empty list, so
    # a topology with no `access.vmm_domains` produces zero vmmDomP MOs
    # (byte-identical to pre-PR-19 output).
    for vmm in topo.access.vmm_domains:
        store.add(_build_vmm_domain(vmm, first_pool_dn))

    for aaep in topo.access.aaeps:
        store.add(_build_aaep(aaep, topo.access.phys_domains, topo.access.l3_domains))

    for aaep in topo.access.aaeps:
        store.add(_build_port_ipg(aaep))

    # ---- Batch-1: leaf-interface-policy cluster --------------------------
    # One vPC bundle group per border-leaf vPC domain (fabric.py already
    # builds the matching vpcDom/vpcIf for these same pairs), one leaf
    # interface-profile per leaf pair (regular leaves + each border-leaf
    # vPC pair), each with a single range selector spanning the real
    # host-port inventory (eth1/1.._HOST_PORTS).
    seen_vpc_domains: set[str] = set()
    for bl in site.border_leaves:
        if bl.vpc_domain in seen_vpc_domains:
            continue
        seen_vpc_domains.add(bl.vpc_domain)
        bndl_name = f"{bl.vpc_domain}-vpc"
        store.add(_build_bndl_grp(bndl_name, lag_t="node"))
        bndl_grp_dn = f"uni/infra/funcprof/accbundle-{bndl_name}"
        profile_name = f"{site.name}-{bl.vpc_domain}"
        store.add(_build_leaf_profile(profile_name, f"sel-{bl.vpc_domain}", bndl_grp_dn))

    leaves = site.leaf_nodes()
    if len(leaves) >= 2:
        pair_name = f"{site.name}-leafpair"
        pc_bndl_name = f"{pair_name}-pc"
        store.add(_build_bndl_grp(pc_bndl_name, lag_t="link"))
        pc_bndl_grp_dn = f"uni/infra/funcprof/accbundle-{pc_bndl_name}"
        store.add(_build_leaf_profile(pair_name, f"sel-{pair_name}", pc_bndl_grp_dn))
