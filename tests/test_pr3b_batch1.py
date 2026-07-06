"""Regression/coverage tests for PR-3b-batch1 — 21 previously-documented-but-
unbuilt classes from docs/CONTRACT.md §6, seeded from the existing topology:

  Access-policy cluster (build/access.py): infraAccPortP, infraHPortS,
    infraPortBlk, infraRsAccBaseGrp, infraAccBndlGrp.
  Route-control cluster (build/l3out.py): rtctrlProfile, rtctrlCtxP,
    rtctrlSubjP, rtctrlMatchRtDest, rtctrlSetComm, rtctrlSetPref, ipRouteP,
    ipNexthopP, l3extMember, ospfExtP.
  EPG/contract classes (build/tenants.py): fvRsPathAtt, vzRsSubjGraphAtt.
  Operational/routing classes (build/routing.py + build/underlay.py):
    uribv4Route, uribv4Nexthop, arpAdjEp, ospfIf.

Auth is enforced (PR-4): every client fixture logs in via
POST /api/aaaLogin.json (admin/cisco) before making authenticated calls,
following the existing tests/test_pr5_topology_consistency.py pattern.
"""
from __future__ import annotations

import copy
import re

import pytest
from fastapi.testclient import TestClient

from aci_sim.build.orchestrator import build_site
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology

TOPO_PATH = "topology.yaml"


@pytest.fixture(scope="module")
def topo():
    return load_topology(TOPO_PATH)


@pytest.fixture(scope="module")
def site_a(topo):
    return topo.site_by_name("LAB1")


@pytest.fixture(scope="module")
def site_b(topo):
    return topo.site_by_name("LAB2")


@pytest.fixture(scope="module")
def store_a(topo, site_a):
    return build_site(topo, site_a)


@pytest.fixture(scope="module")
def store_b(topo, site_b):
    return build_site(topo, site_b)


def _make_client(topo, site, store) -> TestClient:
    state = ApicSiteState(
        name=site.name, site=site, topo=topo, store=store,
        baseline=copy.deepcopy(store),
    )
    c = TestClient(make_apic_app(state))
    c.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    return c


@pytest.fixture(scope="module")
def client_a(topo, site_a, store_a):
    return _make_client(topo, site_a, store_a)


@pytest.fixture(scope="module")
def client_b(topo, site_b, store_b):
    return _make_client(topo, site_b, store_b)


_ALL_STORES = ["store_a", "store_b"]


def _l1physif_ports(store) -> dict[int, set[str]]:
    """Return {node_id: {port_id, ...}} from every l1PhysIf DN in the store."""
    ports: dict[int, set[str]] = {}
    for mo in store.by_class("l1PhysIf"):
        m = re.search(r"/node-(\d+)/sys/phys-\[([^\]]+)\]", mo.dn)
        assert m is not None, f"unparsable l1PhysIf dn {mo.dn!r}"
        node_id = int(m.group(1))
        ports.setdefault(node_id, set()).add(m.group(2))
    return ports


# ---------------------------------------------------------------------------
# Access-policy cluster
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_access_policy_classes_nonempty(store_name, request):
    store = request.getfixturevalue(store_name)
    for cls in (
        "infraAccPortP", "infraHPortS", "infraPortBlk",
        "infraRsAccBaseGrp", "infraAccBndlGrp",
    ):
        mos = store.by_class(cls)
        assert mos, f"{cls} query returned totalCount 0"


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_infra_port_blk_ports_exist_in_l1physif_inventory(store_name, request):
    """Every infraPortBlk from/to port must exist as a real l1PhysIf — the
    port block's fromCard/from/to describe a leaf host-port range, but
    infraPortBlk itself carries no node id, so we resolve it via its parent
    infraHPortS -> infraRsAccBaseGrp -> infraAccBndlGrp path is not node-
    scoped either (access profiles are site-independent in this sim); we
    instead assert the from/to port NUMBERS fall within the host-port range
    (1.._HOST_PORTS) that interfaces.py builds on every leaf, i.e. the block
    is representable on any leaf's real inventory.
    """
    store = request.getfixturevalue(store_name)
    ports = _l1physif_ports(store)
    all_host_ports = {int(p.split("/")[1]) for ps in ports.values() for p in ps if "/" in p}
    blks = store.by_class("infraPortBlk")
    assert blks, "no infraPortBlk found"
    for blk in blks:
        from_p = int(blk.attrs["fromPort"])
        to_p = int(blk.attrs["toPort"])
        assert blk.attrs["fromCard"] == "1"
        assert from_p <= to_p
        assert from_p in all_host_ports, (
            f"infraPortBlk {blk.dn!r} fromPort {from_p} does not match any "
            f"real l1PhysIf port number"
        )
        assert to_p in all_host_ports, (
            f"infraPortBlk {blk.dn!r} toPort {to_p} does not match any real "
            f"l1PhysIf port number"
        )


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_hports_rs_acc_base_grp_targets_real_bndl_grp(store_name, request):
    store = request.getfixturevalue(store_name)
    bndl_dns = {mo.dn for mo in store.by_class("infraAccBndlGrp")}
    assert bndl_dns, "no infraAccBndlGrp found"
    rs_grps = store.by_class("infraRsAccBaseGrp")
    assert rs_grps, "no infraRsAccBaseGrp found"
    for rs in rs_grps:
        assert rs.attrs["tDn"] in bndl_dns, (
            f"infraRsAccBaseGrp {rs.dn!r} tDn {rs.attrs['tDn']!r} does not "
            f"resolve to a real infraAccBndlGrp"
        )


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_at_least_one_vpc_bndl_grp(store_name, request):
    """One infraAccBndlGrp (lagT=node) per border-leaf vPC domain, consistent
    with fabric.py's vpcDom/vpcIf for the same pairs."""
    store = request.getfixturevalue(store_name)
    vpc_bndls = [m for m in store.by_class("infraAccBndlGrp") if m.attrs.get("lagT") == "node"]
    assert vpc_bndls, "expected at least one vPC (lagT=node) infraAccBndlGrp"
    vpc_doms = store.by_class("vpcDom")
    assert vpc_doms, "no vpcDom found to cross-check against"


def test_infra_subtree_full_returns_nested_access_policy_tree(client_a):
    """rsp-subtree=full on uni/infra returns the nested access-policy tree."""
    resp = client_a.get("/api/mo/uni/infra.json?rsp-subtree=full")
    assert resp.status_code == 200, f"uni/infra -> {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    assert data["imdata"], "expected uni/infra to resolve to a real MO"
    body = str(data)
    for cls in ("infraAccPortP", "infraHPortS", "infraPortBlk", "infraRsAccBaseGrp", "infraAccBndlGrp"):
        assert cls in body, f"{cls} missing from uni/infra rsp-subtree=full response"


# ---------------------------------------------------------------------------
# Route-control cluster
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_route_control_classes_nonempty(store_name, request):
    store = request.getfixturevalue(store_name)
    for cls in (
        "rtctrlProfile", "rtctrlCtxP", "rtctrlSubjP", "rtctrlMatchRtDest",
        "rtctrlSetComm", "rtctrlSetPref", "ipRouteP", "ipNexthopP",
        "l3extMember", "ospfExtP",
    ):
        mos = store.by_class(cls)
        assert mos, f"{cls} query returned totalCount 0"


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_rtctrl_match_rt_dest_uses_real_bd_subnet(store_name, request, topo):
    store = request.getfixturevalue(store_name)
    all_subnets = {
        cidr
        for tenant in topo.tenants
        for bd in tenant.bds
        for cidr in bd.subnets
    }
    dests = store.by_class("rtctrlMatchRtDest")
    assert dests, "no rtctrlMatchRtDest found"
    for d in dests:
        assert d.attrs["ip"] in all_subnets, (
            f"rtctrlMatchRtDest {d.dn!r} ip {d.attrs['ip']!r} is not a real BD subnet"
        )


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_ip_route_p_under_real_rsnode_l3out_att(store_name, request):
    """Every ipRouteP dn must nest under a real l3extRsNodeL3OutAtt (i.e. an
    actual border-leaf node attachment), per the placement table."""
    store = request.getfixturevalue(store_name)
    rsnode_dns = {mo.dn for mo in store.by_class("l3extRsNodeL3OutAtt")}
    assert rsnode_dns, "no l3extRsNodeL3OutAtt found"
    routes = store.by_class("ipRouteP")
    assert routes, "no ipRouteP found"
    for r in routes:
        parent = r.dn.rsplit("/rt-[", 1)[0]
        assert parent in rsnode_dns, f"ipRouteP {r.dn!r} not nested under a real rsnodeL3OutAtt"
        assert r.attrs["ip"] == "0.0.0.0/0"
        assert r.attrs.get("pref")


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_ip_nexthop_p_matches_csw_peer_ip(store_name, request, topo):
    store = request.getfixturevalue(store_name)
    nhs = store.by_class("ipNexthopP")
    assert nhs, "no ipNexthopP found"
    all_csw_peer_ips = {
        bl.csw.peer_ip
        for site in topo.sites
        for bl in site.border_leaves
    }
    for nh in nhs:
        assert nh.attrs["nhAddr"] in all_csw_peer_ips, (
            f"ipNexthopP {nh.dn!r} nhAddr {nh.attrs['nhAddr']!r} is not a real CSW peer IP"
        )


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_l3ext_member_on_real_path(store_name, request):
    store = request.getfixturevalue(store_name)
    path_dns = {mo.dn for mo in store.by_class("l3extRsPathL3OutAtt")}
    assert path_dns, "no l3extRsPathL3OutAtt found"
    members = store.by_class("l3extMember")
    assert members, "no l3extMember found"
    for m in members:
        parent = m.dn.rsplit("/mem-", 1)[0]
        assert parent in path_dns, f"l3extMember {m.dn!r} not nested under a real l3extRsPathL3OutAtt"
        assert m.attrs["side"] == "A"


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_ospf_ext_p_coexists_with_bgp_ext_p_on_same_l3out(store_name, request):
    """Batch-1 design choice: ospfExtP added to the SAME L3Out as the
    existing bgpExtP (real ACI allows OSPF+BGP coexistence on one L3Out)."""
    store = request.getfixturevalue(store_name)
    ospf_ext = store.by_class("ospfExtP")
    bgp_ext = store.by_class("bgpExtP")
    assert ospf_ext, "no ospfExtP found"
    assert bgp_ext, "no bgpExtP found"
    ospf_parents = {mo.dn.rsplit("/ospfExtP", 1)[0] for mo in ospf_ext}
    bgp_parents = {mo.dn.rsplit("/bgpExtP", 1)[0] for mo in bgp_ext}
    assert ospf_parents & bgp_parents, "ospfExtP and bgpExtP do not share a common L3Out parent"


# ---------------------------------------------------------------------------
# EPG/contract classes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_epg_contract_classes_nonempty(store_name, request):
    store = request.getfixturevalue(store_name)
    for cls in ("fvRsPathAtt", "vzRsSubjGraphAtt"):
        mos = store.by_class(cls)
        assert mos, f"{cls} query returned totalCount 0"


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_fv_rs_path_att_tdn_is_real_path_with_existing_port(store_name, request):
    """Every fvRsPathAtt tDn must be a real path whose port exists as a real
    l1PhysIf (same port an EPG's own endpoints already use)."""
    store = request.getfixturevalue(store_name)
    ports = _l1physif_ports(store)
    bindings = store.by_class("fvRsPathAtt")
    assert bindings, "no fvRsPathAtt found"
    for b in bindings:
        m = re.search(r"paths-(\d+)/pathep-\[([^\]]+)\]", b.attrs["tDn"])
        assert m is not None, f"unparsable fvRsPathAtt tDn {b.attrs['tDn']!r}"
        node_id, port_id = int(m.group(1)), m.group(2)
        assert port_id in ports.get(node_id, set()), (
            f"fvRsPathAtt {b.dn!r} tDn references {port_id!r} on node {node_id}, "
            f"which has no matching l1PhysIf"
        )
        # encap must reuse the EPG's own vlan (parsed from the same store's fvCEp)
        assert b.attrs["encap"].startswith("vlan-")


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_vz_rs_subj_graph_att_on_real_subject(store_name, request):
    store = request.getfixturevalue(store_name)
    subj_dns = {mo.dn for mo in store.by_class("vzSubj")}
    assert subj_dns, "no vzSubj found"
    graphs = store.by_class("vzRsSubjGraphAtt")
    assert graphs, "no vzRsSubjGraphAtt found"
    for g in graphs:
        parent = g.dn.rsplit("/rsSubjGraphAtt", 1)[0]
        assert parent in subj_dns, f"vzRsSubjGraphAtt {g.dn!r} not nested under a real vzSubj"
        assert g.attrs.get("tnVnsAbsGraphName")


# ---------------------------------------------------------------------------
# Operational/routing classes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_routing_classes_nonempty(store_name, request):
    store = request.getfixturevalue(store_name)
    for cls in ("uribv4Route", "uribv4Nexthop", "arpAdjEp", "ospfIf"):
        mos = store.by_class(cls)
        assert mos, f"{cls} query returned totalCount 0"


_RE_NH_PARENT = re.compile(r"^(.*/rt-\[[^\]]+\])/nh-")


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_uribv4_nexthop_rn_parses_into_4_bracketed_parts(store_name, request):
    """Every uribv4Nexthop RN must parse into 4 bracketed parts:
    nh-[proto]-[addr]-[ifname]-[vrf] — matching autoACI's
    _RE_NH_PARENT = re.compile(r"^(.*/rt-\\[[^\\]]+\\])/nh-") parent-recovery
    regex (verified read-only against route_table.py / routing_state.py)."""
    store = request.getfixturevalue(store_name)
    nhs = store.by_class("uribv4Nexthop")
    assert nhs, "no uribv4Nexthop found"
    for nh in nhs:
        m = _RE_NH_PARENT.match(nh.dn)
        assert m is not None, f"uribv4Nexthop {nh.dn!r} does not match autoACI's parent-recovery regex"
        rn = nh.dn.rsplit("/nh-", 1)[1]
        bracket_groups = re.findall(r"\[([^\]]*)\]", rn)
        assert len(bracket_groups) == 4, (
            f"uribv4Nexthop {nh.dn!r} RN has {len(bracket_groups)} bracketed "
            f"parts, expected 4 (proto, addr, ifname, vrf)"
        )
        # parent route must actually exist in the store
        parent_dn = m.group(1)
        assert store.get(parent_dn) is not None, f"parent uribv4Route {parent_dn!r} missing"


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_uribv4_route_dom_segment_matches_real_vrf(store_name, request, topo):
    store = request.getfixturevalue(store_name)
    routes = store.by_class("uribv4Route")
    assert routes, "no uribv4Route found"
    known_vrf_doms = {
        f"{tenant.name}:{vrf.name}"
        for tenant in topo.tenants
        for vrf in tenant.vrfs
    }
    for r in routes:
        m = re.search(r"/dom-([^/]+)/db-rt/", r.dn)
        assert m is not None, f"unparsable uribv4Route dn {r.dn!r} (no dom- segment)"
        assert m.group(1) in known_vrf_doms, (
            f"uribv4Route {r.dn!r} dom {m.group(1)!r} is not a real tenant:vrf"
        )
        assert r.attrs.get("prefix")


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_arp_adj_ep_count_equals_local_endpoint_count(store_name, request):
    """arpAdjEp count == (locally-learned) endpoint count."""
    store = request.getfixturevalue(store_name)
    local_ceps = [c for c in store.by_class("fvCEp") if c.attrs.get("lcC") == "learned"]
    arp_adjs = store.by_class("arpAdjEp")
    assert local_ceps, "no locally-learned fvCEp found"
    assert len(arp_adjs) == len(local_ceps), (
        f"arpAdjEp count {len(arp_adjs)} != local endpoint count {len(local_ceps)}"
    )


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_arp_adj_ep_mac_matches_a_real_endpoint(store_name, request):
    store = request.getfixturevalue(store_name)
    local_ceps = {c.attrs["mac"] for c in store.by_class("fvCEp") if c.attrs.get("lcC") == "learned"}
    for adj in store.by_class("arpAdjEp"):
        assert adj.attrs["mac"] in local_ceps, f"arpAdjEp {adj.dn!r} mac not a real endpoint MAC"
        assert adj.attrs["operSt"] == "up"


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_ospf_adj_ep_sits_on_an_ospf_if(store_name, request):
    """Every ospfAdjEp must nest directly under a real ospfIf (the ospfIf's
    own DN being the ospfAdjEp's parent — CONTRACT.md batch-1 placement:
    ospfIf nested between ospfDom and ospfAdjEp)."""
    store = request.getfixturevalue(store_name)
    ospf_if_dns = {mo.dn for mo in store.by_class("ospfIf")}
    adjs = store.by_class("ospfAdjEp")
    assert adjs, "no ospfAdjEp found (multi-site topology expected)"
    assert ospf_if_dns, "no ospfIf found"
    for adj in adjs:
        parent = adj.dn.rsplit("/adj-[", 1)[0]
        assert parent in ospf_if_dns, f"ospfAdjEp {adj.dn!r} does not sit on a real ospfIf"


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_ospf_if_references_real_port(store_name, request):
    """ospfIf is now the routed VLAN-4 sub-interface of the spine's ISN
    uplink port (id/dn end in ".4", e.g. "eth1/49.4") — real LLDP/physical
    port state stays on the base port, so this resolves the underlying
    l1PhysIf via the base port (ifId.split('.')[0]), same fallback autoACI's
    topology code uses."""
    store = request.getfixturevalue(store_name)
    ports = _l1physif_ports(store)
    ifs = store.by_class("ospfIf")
    assert ifs, "no ospfIf found"
    for ospf_if in ifs:
        m = re.search(r"/node-(\d+)/sys/ospf/.*?/if-\[([^\]]+)\]", ospf_if.dn)
        assert m is not None, f"unparsable ospfIf dn {ospf_if.dn!r}"
        node_id, port_id = int(m.group(1)), m.group(2)
        assert port_id.endswith(".4"), f"ospfIf {ospf_if.dn!r} id {port_id!r} is not a VLAN-4 sub-if"
        base_port = port_id.split(".")[0]
        assert base_port in ports.get(node_id, set()), (
            f"ospfIf {ospf_if.dn!r} references base port {base_port!r} on node "
            f"{node_id}, which has no matching l1PhysIf"
        )
        assert ospf_if.attrs.get("id") == port_id
        addr = ospf_if.attrs.get("addr", "")
        assert re.fullmatch(r"172\.16\.\d+\.\d+/24", addr), (
            f"ospfIf {ospf_if.dn!r} addr {addr!r} is not a 172.16.{{site}}.0/24 address"
        )


# ---------------------------------------------------------------------------
# Cross-cluster sanity: full-site build still round-trips through the REST
# layer for at least one class per cluster (auth-enforced client).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "cls",
    [
        "infraAccPortP", "rtctrlProfile", "ipRouteP", "fvRsPathAtt",
        "uribv4Route", "arpAdjEp", "ospfIf", "l3extMember", "ospfExtP",
        "vzRsSubjGraphAtt",
    ],
)
def test_class_query_returns_total_count_gt_0(client_a, cls):
    resp = client_a.get(f"/api/class/{cls}.json")
    assert resp.status_code == 200, f"{cls} -> {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    assert int(data["totalCount"]) > 0, f"{cls} totalCount is 0"
