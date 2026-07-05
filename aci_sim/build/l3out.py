"""build/l3out.py — l3extOut tree + eBGP operational session MOs.

Batch-1 additions (CONTRACT.md §6 "Route-control", "L3Out (control)"):
route-control (rtctrlProfile/rtctrlCtxP/rtctrlSubjP/rtctrlMatchRtDest) +
static routes (ipRouteP/ipNexthopP) + l3extMember + ospfExtP. DN/RN shapes
verified against autoACI's plugins (read-only, prior-audit trust per
CONTRACT.md header; see docs/DESIGN.md "Batch-1" section for the specific
attrs read by route_control.py / l3out_detail.py):
  rtctrlProfile   uni/tn-{t}/out-{l3out}/prof-{name}     (export profile, one per L3Out)
  rtctrlCtxP      {profile}/ctx-{name}
  rtctrlSubjP     uni/tn-{t}/subj-{name}                  (tenant-level match rule —
                  route_control.py queries it with wcard(dn,"tn-{tenant}"), NOT
                  scoped under any one L3Out)
  rtctrlMatchRtDest  {subjp}/dest-[{ip}]
  rtctrlSetComm   under the ctx (real ACI RN: "scomm")
  rtctrlSetPref   under the ctx (real ACI RN: "spref")
  ipRouteP        {rsnodeL3OutAtt}/rt-[{prefix}]          (static route toward the CSW peer)
  ipNexthopP      {ipRouteP}/nh-[{addr}]
  l3extMember     {l3extRsPathL3OutAtt}/mem-{A|B}         (only if the path is vPC-style;
                  this topology's L3Out path is a single port per border-leaf — see
                  _build_node_profile's path_tdn — so we emit one member, side="A",
                  on that existing single path, per the batch-1 placement note.)
  ospfExtP        uni/tn-{t}/out-{l3out}/ospfExtP         (added to the SAME L3Out as
                  the existing bgpExtP — real ACI allows OSPF+BGP coexistence on one
                  L3Out, and this topology has no OSPF-only L3Out, so extending the
                  existing one is the simpler faithful choice; see DESIGN.md.)
"""
from __future__ import annotations

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import L3Out, Site, Tenant, Topology
from aci_sim.build.fabric import loopback_ip


def _is_deployed(tenant: Tenant, site: Site) -> bool:
    return site.name in tenant.sites


def _owning_site(l3out: L3Out, topo: Topology) -> str | None:
    if l3out.site is not None:
        return l3out.site
    for s in topo.sites:
        site_bl_ids = {bl.id for bl in s.border_leaves}
        if any(bl_id in site_bl_ids for bl_id in l3out.border_leaves):
            return s.name
    return None


def _build_node_profile(l3out_dn: str, l3out: L3Out, site: Site, pod: int) -> MO:
    """Build l3extLNodeP (with node attachments) + l3extLIfP (paths + BGP peer)."""
    site_bl_ids = {bl.id: bl for bl in site.border_leaves}
    bl_ids_here = [bid for bid in l3out.border_leaves if bid in site_bl_ids]

    np_name = f"{l3out.name}_nodeProfile"
    np_dn = f"{l3out_dn}/lnodep-{np_name}"
    np_mo = MO("l3extLNodeP", dn=np_dn, name=np_name)

    for bl_id in bl_ids_here:
        rtr_id = loopback_ip(pod, bl_id)
        rsnode_dn = f"{np_dn}/rsnodeL3OutAtt-[topology/pod-{pod}/node-{bl_id}]"
        rsnode_mo = MO(
            "l3extRsNodeL3OutAtt",
            dn=rsnode_dn,
            tDn=f"topology/pod-{pod}/node-{bl_id}",
            rtrId=rtr_id,
        )
        # Batch-1: static default route toward this border-leaf's CSW peer.
        # ipRouteP.ip carries the prefix (autoACI's l3out_detail.py/route_control.py
        # read attrs["ip"], not "prefix" — that's uribv4Route); ipNexthopP.nhAddr is
        # the CSW interconnect IP the eBGP session builder (_upsert_ebgp_sessions)
        # already peers with, so the static route is consistent with the live BGP
        # session on the same border leaf.
        if l3out.csw_peer:
            prefix = "0.0.0.0/0"
            route_dn = f"{rsnode_dn}/rt-[{prefix}]"
            route_mo = MO("ipRouteP", dn=route_dn, ip=prefix, pref="1")
            nh_addr = l3out.csw_peer.peer_ip
            route_mo.add_child(MO(
                "ipNexthopP",
                dn=f"{route_dn}/nh-[{nh_addr}]",
                nhAddr=nh_addr,
            ))
            rsnode_mo.add_child(route_mo)
        np_mo.add_child(rsnode_mo)

    ip_name = f"{l3out.name}_ifProfile"
    ip_dn = f"{np_dn}/lifp-{ip_name}"
    ip_mo = MO("l3extLIfP", dn=ip_dn, name=ip_name)

    for bl_id in bl_ids_here:
        bl_obj = site_bl_ids[bl_id]
        csw = bl_obj.csw
        path_tdn = f"topology/pod-{pod}/paths-{bl_id}/pathep-[eth1/48]"
        path_dn = f"{ip_dn}/rspathL3OutAtt-[{path_tdn}]"
        path_mo = MO(
            "l3extRsPathL3OutAtt",
            dn=path_dn,
            tDn=path_tdn,
            encap=f"vlan-{csw.vlan}",
            addr=f"{csw.local_ip}/30",
            ifInstT="sub-interface",
        )
        # Batch-1: this L3Out's path is a single port per border-leaf (not a
        # vPC-bundled path — see path_tdn above), so per the batch-1 placement
        # note we emit exactly one l3extMember, side="A", on the existing path.
        path_mo.add_child(MO(
            "l3extMember",
            dn=f"{path_dn}/mem-A",
            side="A",
            addr=f"{csw.local_ip}/30",
        ))
        ip_mo.add_child(path_mo)

    if l3out.csw_peer:
        peer_ip = l3out.csw_peer.peer_ip
        peer_dn = f"{ip_dn}/peerP-[{peer_ip}]"
        # Tier-3 (PR-20): keepalive/hold — real ACI defaults 60s/180s,
        # previously not carried anywhere on this MO. l3out.csw_peer's
        # keepalive/hold feed bgpPeerP.privateAsCtrl-adjacent timer attrs.
        peer_mo = MO(
            "bgpPeerP",
            dn=peer_dn,
            addr=peer_ip,
            peerCtrl="",
            keepAliveIntvl=str(l3out.csw_peer.keepalive),
            holdIntvl=str(l3out.csw_peer.hold),
        )
        peer_mo.add_child(MO(
            "bgpAsP",
            dn=f"{peer_dn}/as",
            asn=str(l3out.csw_peer.asn),
        ))
        ip_mo.add_child(peer_mo)

    np_mo.add_child(ip_mo)
    return np_mo


def _build_route_control(t: str, l3out: L3Out, l3out_dn: str) -> MO:
    """Build rtctrlProfile (export profile) + rtctrlCtxP + rtctrlSetComm/SetPref
    children, one per L3Out. RN scheme (verified against this module's own
    uni/tn-{t}/out-{l3out}/... conventions; "scomm"/"spref" are the real ACI
    RNs for rtctrlSetComm/rtctrlSetPref — Cisco's fixed short-form RNs, not
    name-suffixed like most other RN templates in this file):
      rtctrlProfile  {l3out_dn}/prof-{name}
      rtctrlCtxP     {profile}/ctx-{name}
      rtctrlSetComm  {ctx}/scomm
      rtctrlSetPref  {ctx}/spref
    """
    prof_name = f"{l3out.name}_export"
    prof_dn = f"{l3out_dn}/prof-{prof_name}"
    prof_mo = MO("rtctrlProfile", dn=prof_dn, name=prof_name)

    ctx_name = f"{l3out.name}_ctx"
    ctx_dn = f"{prof_dn}/ctx-{ctx_name}"
    ctx_mo = MO("rtctrlCtxP", dn=ctx_dn, name=ctx_name, action="permit")
    ctx_mo.add_child(MO("rtctrlSetComm", dn=f"{ctx_dn}/scomm", community=f"no-advertise:{t}"))
    ctx_mo.add_child(MO("rtctrlSetPref", dn=f"{ctx_dn}/spref", localPref="200"))
    prof_mo.add_child(ctx_mo)
    return prof_mo


def _build_tenant_subj(t: str, l3out: L3Out, bd_subnets: list[str]) -> MO | None:
    """Build one tenant-level rtctrlSubjP (match rule) + rtctrlMatchRtDest
    children, referenced from this L3Out's rtctrlCtxP.

    rtctrlSubjP is tenant-level (uni/tn-{t}/subj-{name}), NOT nested under any
    one L3Out's DN — autoACI's route_control.py queries it with
    wcard(rtctrlSubjP.dn, "tn-{tenant}") rather than scoping it to an L3Out
    (verified against the plugin source). Match destinations reuse a real BD
    subnet from the tenant so the redistributed prefix is believable.
    """
    if not bd_subnets:
        return None
    subj_name = f"{l3out.name}_match"
    subj_dn = f"uni/tn-{t}/subj-{subj_name}"
    subj_mo = MO("rtctrlSubjP", dn=subj_dn, name=subj_name)
    subj_mo.add_child(MO(
        "rtctrlMatchRtDest",
        dn=f"{subj_dn}/dest-[{bd_subnets[0]}]",
        ip=bd_subnets[0],
    ))
    return subj_mo


def _build_l3out_tree(t: str, l3out: L3Out, site: Site, l3_dom_name: str, bd_subnets: list[str]) -> MO:
    pod = site.pod
    l3out_dn = f"uni/tn-{t}/out-{l3out.name}"

    l3out_mo = MO("l3extOut", dn=l3out_dn, name=l3out.name, enforceRtctrl="export")
    l3out_mo.add_child(MO(
        "l3extRsEctx",
        dn=f"{l3out_dn}/rsectx",
        tnFvCtxName=l3out.vrf,
        tDn=f"uni/tn-{t}/ctx-{l3out.vrf}",
    ))
    l3out_mo.add_child(MO(
        "l3extRsL3DomAtt",
        dn=f"{l3out_dn}/rsl3DomAtt",
        tDn=f"uni/l3dom-{l3_dom_name}",
    ))
    l3out_mo.add_child(MO("bgpExtP", dn=f"{l3out_dn}/bgpExtP"))
    # Batch-1: ospfExtP added to the SAME L3Out (coexists with bgpExtP above —
    # real ACI supports OSPF+BGP on one L3Out; see module docstring).
    l3out_mo.add_child(MO(
        "ospfExtP",
        dn=f"{l3out_dn}/ospfExtP",
        areaId="0.0.0.1",
        areaType="regular",
    ))
    l3out_mo.add_child(_build_route_control(t, l3out, l3out_dn))

    l3out_mo.add_child(_build_node_profile(l3out_dn, l3out, site, pod))

    for ext_epg in l3out.ext_epgs:
        inst_dn = f"{l3out_dn}/instP-{ext_epg.name}"
        inst_mo = MO("l3extInstP", dn=inst_dn, name=ext_epg.name, prefGrMemb="exclude")
        for cidr in ext_epg.subnets:
            inst_mo.add_child(MO(
                "l3extSubnet",
                dn=f"{inst_dn}/extsubnet-[{cidr}]",
                ip=cidr,
                scope="import-security",
            ))
        l3out_mo.add_child(inst_mo)

    return l3out_mo


def _upsert_ebgp_sessions(
    t: str, l3out: L3Out, site: Site, store: MITStore
) -> None:
    pod = site.pod
    site_bl_ids = {bl.id: bl for bl in site.border_leaves}
    bl_ids_here = [bid for bid in l3out.border_leaves if bid in site_bl_ids]

    for bl_id in bl_ids_here:
        bl_obj = site_bl_ids[bl_id]
        csw = bl_obj.csw
        rtr_id = loopback_ip(pod, bl_id)

        inst_dn = f"topology/pod-{pod}/node-{bl_id}/sys/bgp/inst"
        inst_mo = MO("bgpInst", dn=inst_dn, asn=str(site.asn))

        dom_name = f"{t}:{l3out.vrf}"
        dom_dn = f"{inst_dn}/dom-{dom_name}"
        dom_mo = MO("bgpDom", dn=dom_dn, name=dom_name)

        peer_dn = f"{dom_dn}/peer-[{csw.peer_ip}]"
        peer_mo = MO(
            "bgpPeer",
            dn=peer_dn,
            addr=csw.peer_ip,
            asn=str(csw.asn),
            type="ebgp",
            peerRole="ce",
        )
        entry_dn = f"{peer_dn}/ent-[{csw.peer_ip}]"
        peer_mo.add_child(MO(
            "bgpPeerEntry",
            dn=entry_dn,
            addr=csw.peer_ip,
            operSt="established",
            rtrId=rtr_id,
            connEst="1",
            connDrop="0",
            type="ebgp",
        ))
        dom_mo.add_child(peer_mo)
        inst_mo.add_child(dom_mo)
        store.upsert(inst_mo)


def build(topo: Topology, site: Site, store: MITStore) -> None:
    """Emit l3extOut trees + eBGP session MOs for L3Outs owned by *site*."""
    l3_dom_name = topo.access.l3_domains[0].name if topo.access.l3_domains else "l3dom-1"

    for tenant in topo.tenants:
        if not _is_deployed(tenant, site):
            continue
        t = tenant.name
        bd_subnets = [cidr for bd in tenant.bds for cidr in bd.subnets]
        for l3out in tenant.l3outs:
            if _owning_site(l3out, topo) != site.name:
                continue
            store.add(_build_l3out_tree(t, l3out, site, l3_dom_name, bd_subnets))
            _upsert_ebgp_sessions(t, l3out, site, store)
            # Batch-1: tenant-level match rule (rtctrlSubjP), added independently
            # of the l3out tree (autoACI queries it as tenant-scoped, not nested
            # under any one L3Out — see _build_tenant_subj docstring).
            subj_mo = _build_tenant_subj(t, l3out, bd_subnets)
            if subj_mo is not None:
                store.add(subj_mo)
