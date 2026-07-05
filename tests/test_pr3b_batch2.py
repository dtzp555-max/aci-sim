"""Regression/coverage tests for PR-3b-batch2 — remaining previously-
catalogued-but-unbuilt classes from docs/CONTRACT.md §6 (batches 2+3),
seeded from the existing topology:

  Port-channel/vPC (build/fabric.py): pcAggrIf, pcRsMbrIfs (CONTRACT.md
    corrected: was catalogued as "vpcRsMbrIfs", real ACI/autoACI class is
    pcRsMbrIfs — see docs/CONTRACT.md §6 changelog note).
  Optics (build/interfaces.py): ethpmFcot.
  Cluster health (build/fabric.py): infraWiNode.
  Zoning-rule / pcTag plumbing (build/tenants.py + new build/zoning.py):
    fvEpP, actrlRule (+ fvAEPg.pcTag / fvCtx.pcTag+scope, not separately
    catalogued but required for the above to cross-reference).
  EVPN routes (build/overlay.py): bgpVpnRoute, bgpPath.
  Node registration (build/fabric.py + rest_aci/writes.py): fabricNodeIdentP.

infraNodeIdentP is intentionally NOT built — see docs/CONTRACT.md §6 for the
decision (no autoACI consumer, and real ACI's infraNodeIdentP is a node
*provisioning policy* object, not the same thing as fabricNodeIdentP's
Fabric Membership registration; building it under the same semantics as
fabricNodeIdentP would be a faithfulness regression, not a coverage gain).

Auth is enforced (PR-4): every client fixture logs in via
POST /api/aaaLogin.json (admin/cisco) before making authenticated calls,
following the existing tests/test_pr3b_batch1.py pattern.
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


def _l1physif_dns(store) -> set[str]:
    return {mo.dn for mo in store.by_class("l1PhysIf")}


# ---------------------------------------------------------------------------
# Non-empty coverage — every batch-2 class must have totalCount > 0
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_batch2_classes_nonempty(store_name, request):
    store = request.getfixturevalue(store_name)
    for cls in (
        "pcAggrIf", "pcRsMbrIfs", "ethpmFcot", "infraWiNode",
        "fvEpP", "actrlRule", "bgpVpnRoute", "bgpPath", "fabricNodeIdentP",
    ):
        mos = store.by_class(cls)
        assert mos, f"{cls} query returned totalCount 0"


@pytest.mark.parametrize(
    "cls",
    [
        "pcAggrIf", "pcRsMbrIfs", "ethpmFcot", "infraWiNode",
        "fvEpP", "actrlRule", "bgpVpnRoute", "bgpPath", "fabricNodeIdentP",
    ],
)
def test_class_query_returns_total_count_gt_0(client_a, cls):
    resp = client_a.get(f"/api/class/{cls}.json")
    assert resp.status_code == 200, f"{cls} -> {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    assert int(data["totalCount"]) > 0, f"{cls} totalCount is 0"


# ---------------------------------------------------------------------------
# pcAggrIf / pcRsMbrIfs — vPC port-channel + member ports
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_pc_aggr_if_matches_a_real_vpc_if(store_name, request):
    """vpc_status.py matches pcAggrIf to vpcIf by (node, name==ipg_name)."""
    store = request.getfixturevalue(store_name)
    vpc_by_node_name = set()
    for vif in store.by_class("vpcIf"):
        m = re.search(r"/node-(\d+)/", vif.dn)
        assert m is not None
        vpc_by_node_name.add((m.group(1), vif.attrs["name"]))

    aggrs = store.by_class("pcAggrIf")
    assert aggrs, "no pcAggrIf found"
    for aggr in aggrs:
        m = re.search(r"/node-(\d+)/", aggr.dn)
        assert m is not None, f"unparsable pcAggrIf dn {aggr.dn!r}"
        key = (m.group(1), aggr.attrs["name"])
        assert key in vpc_by_node_name, (
            f"pcAggrIf {aggr.dn!r} (node={key[0]}, name={key[1]}) has no matching vpcIf"
        )
        assert aggr.attrs["id"].startswith("po")


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_pc_rs_mbr_ifs_member_dns_exist_in_l1physif(store_name, request):
    """Every pcRsMbrIfs.tDn must resolve to a real l1PhysIf — vpc_status.py
    parses the member port out of tDn's phys-[...] segment and expects it to
    be a real interface."""
    store = request.getfixturevalue(store_name)
    phys_dns = _l1physif_dns(store)
    assert phys_dns, "no l1PhysIf found"
    members = store.by_class("pcRsMbrIfs")
    assert members, "no pcRsMbrIfs found"
    for mbr in members:
        t_dn = mbr.attrs["tDn"]
        assert "phys-[" in t_dn, f"pcRsMbrIfs {mbr.dn!r} tDn {t_dn!r} has no phys-[...] segment"
        assert t_dn in phys_dns, (
            f"pcRsMbrIfs {mbr.dn!r} tDn {t_dn!r} does not resolve to a real l1PhysIf"
        )
        assert mbr.attrs["parentSKey"], "pcRsMbrIfs missing parentSKey (po id)"


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_pc_rs_mbr_ifs_nested_under_real_pc_aggr_if(store_name, request):
    store = request.getfixturevalue(store_name)
    aggr_dns = {mo.dn for mo in store.by_class("pcAggrIf")}
    assert aggr_dns, "no pcAggrIf found"
    for mbr in store.by_class("pcRsMbrIfs"):
        parent = mbr.dn.rsplit("/rsmbrIfs-[", 1)[0]
        assert parent in aggr_dns, f"pcRsMbrIfs {mbr.dn!r} not nested under a real pcAggrIf"


# ---------------------------------------------------------------------------
# ethpmFcot — SFP/transceiver inventory
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_ethpm_fcot_sits_on_real_port(store_name, request):
    store = request.getfixturevalue(store_name)
    phys_dns = _l1physif_dns(store)
    assert phys_dns, "no l1PhysIf found"
    fcots = store.by_class("ethpmFcot")
    assert fcots, "no ethpmFcot found"
    for fcot in fcots:
        parent = fcot.dn.rsplit("/fcot", 1)[0]
        assert parent in phys_dns, f"ethpmFcot {fcot.dn!r} does not sit on a real l1PhysIf port"
        assert fcot.attrs.get("typeName")
        assert fcot.attrs.get("vendorName")


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_ethpm_fcot_only_on_ethpm_phys_if_ports(store_name, request):
    """Every ethpmFcot DN must share its port with a real ethpmPhysIf (sibling
    placement, per the module docstring) — i.e. ethpmFcot is a subset of the
    ports that have ethpmPhysIf, never a port that has no ethpmPhysIf."""
    store = request.getfixturevalue(store_name)
    ethpm_ports = {mo.dn.rsplit("/phys", 1)[0] for mo in store.by_class("ethpmPhysIf")}
    for fcot in store.by_class("ethpmFcot"):
        port_dn = fcot.dn.rsplit("/fcot", 1)[0]
        assert port_dn in ethpm_ports, f"ethpmFcot {fcot.dn!r} has no sibling ethpmPhysIf"


# ---------------------------------------------------------------------------
# infraWiNode — APIC cluster fitness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_infra_wi_node_all_fully_fit(store_name, request):
    store = request.getfixturevalue(store_name)
    wi_nodes = store.by_class("infraWiNode")
    assert wi_nodes, "no infraWiNode found"
    for wi in wi_nodes:
        assert wi.attrs["health"] == "fully-fit", f"infraWiNode {wi.dn!r} not fully-fit"


@pytest.mark.parametrize("store_name,site_name", [("store_a", "site_a"), ("store_b", "site_b")])
def test_infra_wi_node_count_matches_cluster_squared(store_name, site_name, request):
    """Every controller has its OWN view of every other controller — an NxN
    appliance-vector matrix (N = site.controllers)."""
    store = request.getfixturevalue(store_name)
    site = request.getfixturevalue(site_name)
    n = site.controllers
    wi_nodes = store.by_class("infraWiNode")
    assert len(wi_nodes) == n * n, (
        f"infraWiNode count {len(wi_nodes)} != controllers^2 ({n}^2={n*n})"
    )


# ---------------------------------------------------------------------------
# fvEpP / fvAEPg.pcTag / fvCtx.pcTag+scope — zoning-rule EPG name resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_fv_epp_epg_pkey_matches_a_real_epg(store_name, request):
    store = request.getfixturevalue(store_name)
    epg_dns = {mo.dn for mo in store.by_class("fvAEPg")}
    assert epg_dns, "no fvAEPg found"
    epps = store.by_class("fvEpP")
    assert epps, "no fvEpP found"
    for epp in epps:
        epg_pkey = epp.attrs.get("epgPKey", "")
        assert epg_pkey in epg_dns, f"fvEpP {epp.dn!r} epgPKey {epg_pkey!r} is not a real fvAEPg"
        assert epp.dn == f"uni/epp/fv-[{epg_pkey}]"


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_fv_epp_pctag_matches_owning_epg(store_name, request):
    store = request.getfixturevalue(store_name)
    epg_pctag = {mo.dn: mo.attrs.get("pcTag") for mo in store.by_class("fvAEPg")}
    for epp in store.by_class("fvEpP"):
        epg_dn = epp.attrs["epgPKey"]
        assert epp.attrs.get("pcTag"), f"fvEpP {epp.dn!r} missing pcTag"
        assert epp.attrs["pcTag"] == epg_pctag.get(epg_dn), (
            f"fvEpP {epp.dn!r} pcTag does not match its owning fvAEPg's pcTag"
        )


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_fv_aepg_pctag_is_stable_and_nonempty(store_name, request):
    store = request.getfixturevalue(store_name)
    epgs = store.by_class("fvAEPg")
    assert epgs, "no fvAEPg found"
    seen_tags = set()
    for epg in epgs:
        tag = epg.attrs.get("pcTag")
        assert tag, f"fvAEPg {epg.dn!r} missing pcTag"
        assert tag not in seen_tags, f"pcTag {tag!r} collides across EPGs"
        seen_tags.add(tag)


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_fv_ctx_has_pctag_and_scope(store_name, request):
    store = request.getfixturevalue(store_name)
    ctxs = store.by_class("fvCtx")
    assert ctxs, "no fvCtx found"
    for ctx in ctxs:
        assert ctx.attrs.get("pcTag"), f"fvCtx {ctx.dn!r} missing pcTag"
        assert ctx.attrs.get("scope"), f"fvCtx {ctx.dn!r} missing scope"


# ---------------------------------------------------------------------------
# actrlRule — compiled zoning rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_actrl_rule_pctags_match_fv_aepg(store_name, request):
    """actrlRule.sPcTag/dPcTag must both resolve to real fvAEPg.pcTag values —
    i.e. the rule's provider/consumer EPG pcTags are consistent with the
    fvAEPg/fvEpP data zoning_rules.py cross-references against."""
    store = request.getfixturevalue(store_name)
    real_pctags = {mo.attrs["pcTag"] for mo in store.by_class("fvAEPg")}
    assert real_pctags, "no fvAEPg pcTags found"
    rules = store.by_class("actrlRule")
    assert rules, "no actrlRule found"
    for rule in rules:
        assert rule.attrs["sPcTag"] in real_pctags, (
            f"actrlRule {rule.dn!r} sPcTag {rule.attrs['sPcTag']!r} is not a real EPG pcTag"
        )
        assert rule.attrs["dPcTag"] in real_pctags, (
            f"actrlRule {rule.dn!r} dPcTag {rule.attrs['dPcTag']!r} is not a real EPG pcTag"
        )
        assert rule.attrs["action"] == "permit"
        assert rule.attrs.get("ctrctName")


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_actrl_rule_scope_matches_a_real_vrf_scope(store_name, request):
    store = request.getfixturevalue(store_name)
    real_scopes = {mo.attrs["scope"] for mo in store.by_class("fvCtx")}
    assert real_scopes, "no fvCtx scopes found"
    for rule in store.by_class("actrlRule"):
        assert rule.attrs["scopeId"] in real_scopes, (
            f"actrlRule {rule.dn!r} scopeId {rule.attrs['scopeId']!r} is not a real VRF scope"
        )


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_actrl_rule_dn_under_real_node(store_name, request):
    store = request.getfixturevalue(store_name)
    node_ids = set()
    for node in store.by_class("fabricNode"):
        if node.attrs.get("role") != "controller":
            node_ids.add(node.attrs["id"])
    rules = store.by_class("actrlRule")
    assert rules, "no actrlRule found"
    for rule in rules:
        m = re.search(r"/node-(\d+)/sys/actrl/", rule.dn)
        assert m is not None, f"unparsable actrlRule dn {rule.dn!r}"
        assert m.group(1) in node_ids, f"actrlRule {rule.dn!r} references a non-real node"


# ---------------------------------------------------------------------------
# bgpVpnRoute / bgpPath — EVPN route table (two-pass parseable)
# ---------------------------------------------------------------------------

_RE_PATH_PARENT = re.compile(r"^(.*)/path-[^/]+$")


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_bgp_path_is_child_of_bgp_vpn_route_two_pass_parseable(store_name, request):
    """Mirrors autoACI's fabric_bgp_evpn.py two-pass parse: pass 1 collects
    bgpVpnRoute by dn, pass 2 recovers the parent route dn from a bgpPath dn
    via `path_dn.rsplit("/path-", 1)[0]`."""
    store = request.getfixturevalue(store_name)
    routes = {mo.dn for mo in store.by_class("bgpVpnRoute")}
    assert routes, "no bgpVpnRoute found"
    paths = store.by_class("bgpPath")
    assert paths, "no bgpPath found"
    for path in paths:
        assert "/path-" in path.dn, f"bgpPath {path.dn!r} has no /path- segment"
        parent_dn = path.dn.rsplit("/path-", 1)[0]
        assert parent_dn in routes, (
            f"bgpPath {path.dn!r} parent {parent_dn!r} is not a real bgpVpnRoute"
        )
        assert path.attrs.get("nh"), f"bgpPath {path.dn!r} missing nh"


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_bgp_vpn_route_prefix_is_real_bd_subnet(store_name, request, topo):
    store = request.getfixturevalue(store_name)
    all_subnets = {
        cidr for tenant in topo.tenants for bd in tenant.bds for cidr in bd.subnets
    }
    routes = store.by_class("bgpVpnRoute")
    assert routes, "no bgpVpnRoute found"
    for route in routes:
        assert route.attrs["pfx"] in all_subnets, (
            f"bgpVpnRoute {route.dn!r} pfx {route.attrs['pfx']!r} is not a real BD subnet"
        )
        assert route.attrs.get("rd")


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_bgp_vpn_route_dn_under_a_real_spine(store_name, request):
    store = request.getfixturevalue(store_name)
    spine_ids = {
        mo.attrs["id"] for mo in store.by_class("fabricNode") if mo.attrs.get("role") == "spine"
    }
    assert spine_ids, "no spine fabricNode found"
    for route in store.by_class("bgpVpnRoute"):
        m = re.search(r"/node-(\d+)/sys/bgp/inst/", route.dn)
        assert m is not None, f"unparsable bgpVpnRoute dn {route.dn!r}"
        assert m.group(1) in spine_ids, f"bgpVpnRoute {route.dn!r} not seeded on a real spine"


# ---------------------------------------------------------------------------
# fabricNodeIdentP — boot-seeded node registration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_fabric_node_ident_p_count_equals_real_switch_count(store_name, request):
    """One fabricNodeIdentP per real switch fabricNode (spines/leaves/
    border-leaves) — controllers are NOT registered via nodeidentpol (real
    ACI: that's Fabric Membership for switches joining the fabric, not the
    APICs themselves)."""
    store = request.getfixturevalue(store_name)
    switch_nodes = [
        mo for mo in store.by_class("fabricNode") if mo.attrs.get("role") != "controller"
    ]
    assert switch_nodes, "no switch fabricNode found"
    idents = store.by_class("fabricNodeIdentP")
    assert idents, "no fabricNodeIdentP found"
    assert len(idents) == len(switch_nodes), (
        f"fabricNodeIdentP count {len(idents)} != real switch fabricNode count {len(switch_nodes)}"
    )


@pytest.mark.parametrize("store_name", _ALL_STORES)
def test_fabric_node_ident_p_nodeid_matches_real_fabric_node(store_name, request):
    store = request.getfixturevalue(store_name)
    switch_ids = {
        mo.attrs["id"] for mo in store.by_class("fabricNode") if mo.attrs.get("role") != "controller"
    }
    for ident in store.by_class("fabricNodeIdentP"):
        m = re.search(r"nodep-(\d+)$", ident.dn)
        assert m is not None, f"unparsable fabricNodeIdentP dn {ident.dn!r}"
        assert m.group(1) in switch_ids, f"fabricNodeIdentP {ident.dn!r} not a real switch node"
        assert ident.attrs.get("nodeId") == m.group(1)
        assert ident.attrs.get("serial")


def test_add_leaf_reaction_still_materializes_new_node(client_a):
    """Regression guard (per task instructions): a POSTed new nodeidentpol
    must still materialize a fabricNode, exactly as
    tests/test_rest_aci.py::test_add_leaf_reaction already verifies — batch-2
    boot-seeding fabricNodeIdentP for EXISTING nodes must not interfere with
    the write-path reaction for a NEW node id."""
    resp = client_a.post(
        "/api/mo/uni/controller/nodeidentpol/nodep-950.json",
        json={
            "fabricNodeIdentP": {
                "attributes": {
                    "dn": "uni/controller/nodeidentpol/nodep-950",
                    "name": "leaf-950",
                    "nodeType": "leaf",
                }
            }
        },
    )
    assert resp.status_code == 200, f"POST fabricNodeIdentP -> {resp.status_code}: {resp.text[:200]}"
    resp2 = client_a.get("/api/class/fabricNode.json")
    ids = [item["fabricNode"]["attributes"]["id"] for item in resp2.json()["imdata"]]
    assert "950" in ids, "add-leaf reaction did not materialize the new fabricNode"


# ---------------------------------------------------------------------------
# Cross-cluster sanity: full-site build still round-trips through the REST
# layer with rsp-subtree=full for the node-scoped actrlRule shape.
# ---------------------------------------------------------------------------

def test_actrl_subtree_query_returns_rules(client_a, store_a):
    """Mirrors zoning_rules.py's node-scoped subtree query shape."""
    rule = store_a.by_class("actrlRule")[0]
    node_m = re.search(r"topology/pod-(\d+)/node-(\d+)/sys/actrl", rule.dn)
    assert node_m is not None
    pod, node_id = node_m.group(1), node_m.group(2)
    actrl_dn = f"topology/pod-{pod}/node-{node_id}/sys/actrl"
    resp = client_a.get(
        f"/api/mo/{actrl_dn}.json?query-target=subtree&target-subtree-class=actrlRule"
    )
    assert resp.status_code == 200, f"{actrl_dn} -> {resp.status_code}: {resp.text[:200]}"
    data = resp.json()
    assert data["imdata"], f"expected actrlRule descendants under {actrl_dn}"
    assert any("actrlRule" in item for item in data["imdata"])
