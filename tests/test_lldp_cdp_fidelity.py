"""LLDP/CDP fidelity feature tests (v0.12.0):

1. lldpInst/cdpInst materialized (one per switch node with adjacencies);
   lldpIf/cdpIf materialized per adjacency interface.
2. Regression fix: deep-root subtree query
   (`GET /api/mo/.../sys/lldp/inst.json?query-target=subtree&
   target-subtree-class=lldpAdjEp`) returns totalCount>0 (was 0 before
   lldpInst/lldpIf were materialized) — plus /api/class/lldpInst.json and
   /api/class/lldpIf.json both return >0.
3. lldpAdjEp bidirectional portIdV consistency (spine<->leaf link).
4. cdpAdjEp.devId == sysName, cdpAdjEp.portId non-empty.
5. Backward-compat: sysName + mgmtIp unchanged on both lldpAdjEp/cdpAdjEp.
6. Dynamic path: POSTing a fabricNodeIdentP (writes.py's
   materialize_node_registration reaction) gets the same treatment.
7. CLI: `aci-sim lldp` — text, --json, --cdp, and a graceful non-existent
   node id.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aci_sim import cli
from aci_sim.build import cabling, fabric, health_faults, interfaces, overlay, underlay
from aci_sim.build.orchestrator import build_site
from aci_sim.mit.store import MITStore
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology
from aci_sim.topology.schema import Topology

TOPO_YAML = Path(__file__).parent.parent / "topology.yaml"
TOPO_PATH_STR = "topology.yaml"  # run from project root, matches other test modules


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def topo() -> Topology:
    return load_topology(TOPO_YAML)


@pytest.fixture(scope="module")
def site_a(topo):
    return topo.site_by_name("LAB1")


@pytest.fixture(scope="module")
def store(topo, site_a) -> MITStore:
    s = MITStore()
    fabric.build(topo, site_a, s)
    cabling.build(topo, site_a, s)
    underlay.build(topo, site_a, s)
    overlay.build(topo, site_a, s)
    interfaces.build(topo, site_a, s)
    health_faults.build(topo, site_a, s)
    return s


def _login(client: TestClient) -> None:
    resp = client.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    assert resp.status_code == 200, resp.text


def _fresh_client(site_name: str = "LAB1") -> tuple[ApicSiteState, TestClient]:
    topo_obj = load_topology(TOPO_PATH_STR)
    site = topo_obj.site_by_name(site_name)
    built_store = build_site(topo_obj, site)
    state = ApicSiteState(
        name=site.name,
        site=site,
        topo=topo_obj,
        store=built_store,
        baseline=copy.deepcopy(built_store),
    )
    client = TestClient(make_apic_app(state))
    _login(client)
    return state, client


# ---------------------------------------------------------------------------
# 1. Containers materialized
# ---------------------------------------------------------------------------

def test_lldp_inst_materialized_per_switch_node(store, site_a):
    """One lldpInst per switch node that has at least one lldpAdjEp."""
    node_ids_with_adj = set()
    for mo in store.by_class("lldpAdjEp"):
        m = re.search(r"/node-(\d+)/", mo.attrs["dn"])
        if m:
            node_ids_with_adj.add(int(m.group(1)))

    lldp_inst_node_ids = set()
    for mo in store.by_class("lldpInst"):
        m = re.search(r"/node-(\d+)/", mo.attrs["dn"])
        assert m, f"lldpInst dn missing node id: {mo.attrs['dn']}"
        lldp_inst_node_ids.add(int(m.group(1)))

    assert node_ids_with_adj, "expected at least one node with an lldpAdjEp"
    assert lldp_inst_node_ids == node_ids_with_adj, (
        f"lldpInst node set mismatch: adj nodes={node_ids_with_adj} "
        f"lldpInst nodes={lldp_inst_node_ids}"
    )

    inst = store.by_class("lldpInst")[0]
    assert inst.attrs["adminSt"] == "enabled"
    assert inst.attrs["name"] == "default"
    assert inst.attrs["holdTime"] == "120"
    assert inst.attrs["initDelayTime"] == "2"
    assert inst.attrs["txFreq"] == "30"
    assert inst.attrs["ctrl"] == "rx-en,tx-en"
    assert "sys-name" in inst.attrs["optTlvSel"]


def test_cdp_inst_materialized_per_switch_node(store, site_a):
    node_ids_with_adj = set()
    for mo in store.by_class("cdpAdjEp"):
        m = re.search(r"/node-(\d+)/", mo.attrs["dn"])
        if m:
            node_ids_with_adj.add(int(m.group(1)))

    cdp_inst_node_ids = set()
    for mo in store.by_class("cdpInst"):
        m = re.search(r"/node-(\d+)/", mo.attrs["dn"])
        assert m
        cdp_inst_node_ids.add(int(m.group(1)))

    assert cdp_inst_node_ids == node_ids_with_adj

    inst = store.by_class("cdpInst")[0]
    assert inst.attrs["adminSt"] == "enabled"
    assert inst.attrs["name"] == "default"
    assert inst.attrs["holdIntvl"] == "180"
    assert inst.attrs["txFreq"] == "60"
    assert inst.attrs["ver"] == "v2"


def test_lldp_if_cdp_if_materialized_per_adjacency_interface(store, site_a):
    """One lldpIf/cdpIf per (node, local_port) that has an adjacency."""
    expected_if_dns = set()
    for mo in store.by_class("lldpAdjEp"):
        # .../sys/lldp/inst/if-[{port}]/adj-1 -> .../sys/lldp/inst/if-[{port}]
        if_dn = mo.attrs["dn"].rsplit("/", 1)[0]
        expected_if_dns.add(if_dn)

    actual_if_dns = {mo.attrs["dn"] for mo in store.by_class("lldpIf")}
    assert actual_if_dns == expected_if_dns

    lldp_if = store.by_class("lldpIf")[0]
    assert lldp_if.attrs["adminRxSt"] == "enabled"
    assert lldp_if.attrs["adminTxSt"] == "enabled"
    assert lldp_if.attrs["operRxSt"] == "up"
    assert lldp_if.attrs["operTxSt"] == "up"
    assert lldp_if.attrs["wiring"] == "valid"
    assert re.match(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$", lldp_if.attrs["mac"])

    expected_cdp_if_dns = set()
    for mo in store.by_class("cdpAdjEp"):
        expected_cdp_if_dns.add(mo.attrs["dn"].rsplit("/", 1)[0])
    actual_cdp_if_dns = {mo.attrs["dn"] for mo in store.by_class("cdpIf")}
    assert actual_cdp_if_dns == expected_cdp_if_dns

    cdp_if = store.by_class("cdpIf")[0]
    assert cdp_if.attrs["adminSt"] == "enabled"
    assert cdp_if.attrs["operSt"] == "up"


# ---------------------------------------------------------------------------
# 2. Regression fix — deep-root subtree query + class queries
# ---------------------------------------------------------------------------

def test_deep_root_subtree_query_returns_neighbors():
    """The bug this feature fixes: GET .../sys/lldp/inst.json?query-target=
    subtree&target-subtree-class=lldpAdjEp used to return totalCount=0
    because the root DN had no materialized MO (run_mo_query short-circuits
    to ([], 0) when store.get(dn) is None)."""
    _, client = _fresh_client("LAB1")
    resp = client.get(
        "/api/mo/topology/pod-1/node-101/sys/lldp/inst.json"
        "?query-target=subtree&target-subtree-class=lldpAdjEp"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert int(data["totalCount"]) > 0, f"expected totalCount>0, got {data}"
    for item in data["imdata"]:
        assert "lldpAdjEp" in item


def test_deep_root_cdp_subtree_query_returns_neighbors():
    _, client = _fresh_client("LAB1")
    resp = client.get(
        "/api/mo/topology/pod-1/node-101/sys/cdp/inst.json"
        "?query-target=subtree&target-subtree-class=cdpAdjEp"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert int(data["totalCount"]) > 0, f"expected totalCount>0, got {data}"


def test_lldp_inst_class_query_nonzero():
    _, client = _fresh_client("LAB1")
    resp = client.get("/api/class/lldpInst.json")
    assert resp.status_code == 200, resp.text
    assert int(resp.json()["totalCount"]) > 0


def test_lldp_if_class_query_nonzero():
    _, client = _fresh_client("LAB1")
    resp = client.get("/api/class/lldpIf.json")
    assert resp.status_code == 200, resp.text
    assert int(resp.json()["totalCount"]) > 0


def test_cdp_inst_and_if_class_query_nonzero():
    _, client = _fresh_client("LAB1")
    resp_inst = client.get("/api/class/cdpInst.json")
    assert resp_inst.status_code == 200, resp_inst.text
    assert int(resp_inst.json()["totalCount"]) > 0

    resp_if = client.get("/api/class/cdpIf.json")
    assert resp_if.status_code == 200, resp_if.text
    assert int(resp_if.json()["totalCount"]) > 0


# ---------------------------------------------------------------------------
# 3. Bidirectional portIdV consistency (spine<->leaf link)
# ---------------------------------------------------------------------------

def test_lldp_adjep_bidirectional_portidv_consistency(store, site_a):
    """For a spine<->leaf link, the leaf-side adj's portIdV must equal the
    spine's own local port, and vice-versa."""
    from aci_sim.build.cabling import cabling_links

    pod = site_a.pod
    checked = 0
    for n1, s1, p1, n2, s2, p2 in cabling_links(site_a):
        port1 = f"eth{s1}/{p1}"
        port2 = f"eth{s2}/{p2}"

        adj_n1 = store.get(f"topology/pod-{pod}/node-{n1}/sys/lldp/inst/if-[{port1}]/adj-1")
        adj_n2 = store.get(f"topology/pod-{pod}/node-{n2}/sys/lldp/inst/if-[{port2}]/adj-1")
        assert adj_n1 is not None
        assert adj_n2 is not None

        # n1 sees n2 on port1; n2's own port is port2 -> n1's portIdV == port2
        assert adj_n1.attrs["portIdV"] == port2
        # n2 sees n1 on port2; n1's own port is port1 -> n2's portIdV == port1
        assert adj_n2.attrs["portIdV"] == port1
        checked += 1

    assert checked > 0, "no cabling links found to check"


# ---------------------------------------------------------------------------
# 4. cdpAdjEp devId/portId
# ---------------------------------------------------------------------------

def test_cdp_adjep_devid_matches_sysname_and_portid_nonempty(store, site_a):
    cdp_mos = store.by_class("cdpAdjEp")
    assert cdp_mos, "expected at least one cdpAdjEp"
    for mo in cdp_mos:
        assert mo.attrs["devId"] == mo.attrs["sysName"]
        assert mo.attrs["portId"], f"empty portId on {mo.attrs['dn']}"


# ---------------------------------------------------------------------------
# 5. Backward-compat: sysName + mgmtIp unchanged
# ---------------------------------------------------------------------------

def test_backward_compat_sysname_and_mgmtip_present(store, site_a):
    for cls in ("lldpAdjEp", "cdpAdjEp"):
        mos = store.by_class(cls)
        assert mos, f"expected at least one {cls}"
        for mo in mos:
            assert mo.attrs.get("sysName"), f"{cls} missing sysName: {mo.attrs['dn']}"
            assert mo.attrs.get("mgmtIp"), f"{cls} missing mgmtIp: {mo.attrs['dn']}"


def test_backward_compat_matches_cabling_graph(store, site_a, topo):
    """Same assertion test_build_fabric.py's test_lldp_cdp_matches_cabling
    already makes (sysName set matches the cabling graph) — re-verified here
    to prove the enrichment didn't alter the pre-existing neighbor set."""
    from aci_sim.build.cabling import cabling_links

    node_map = {n.id: n for n in site_a.all_nodes()}
    expected: set[tuple[int, str]] = set()
    for n1, _s1, _p1, n2, _s2, _p2 in cabling_links(site_a):
        expected.add((n1, node_map[n2].name))
        expected.add((n2, node_map[n1].name))
    for cid in range(1, site_a.controllers + 1):
        for leaf in site_a.leaf_nodes()[:2]:
            expected.add((leaf.id, f"{site_a.name}-ACI-APIC{cid:02d}"))
    # v0.19.0: multi-site fabrics also see the ISN CSW on each spine's
    # dedicated uplink port (fabric.py's ISN-CSW add_adjacency block).
    if len(topo.sites) > 1:
        for spine in site_a.spine_nodes():
            expected.add((spine.id, f"ISN-CSW{site_a.id}"))

    actual: set[tuple[int, str]] = set()
    for mo in store.by_class("lldpAdjEp"):
        m = re.search(r"/node-(\d+)/", mo.attrs["dn"])
        if m:
            actual.add((int(m.group(1)), mo.attrs.get("sysName", "")))

    assert expected == actual


# ---------------------------------------------------------------------------
# 6. Dynamic path — fabricNodeIdentP registration reaction
# ---------------------------------------------------------------------------

def test_dynamic_node_registration_materializes_lldp_containers():
    """POSTing a fabricNodeIdentP (writes.py's materialize_node_registration
    reaction) must produce the same lldpInst/lldpIf materialization the
    boot-time builders produce, so its subtree query also works."""
    _, client = _fresh_client("LAB1")
    resp = client.post(
        "/api/mo/uni/controller/nodeidentpol/nodep-920.json",
        json={
            "fabricNodeIdentP": {
                "attributes": {
                    "dn": "uni/controller/nodeidentpol/nodep-920",
                    "name": "leaf-920",
                    "nodeType": "leaf",
                }
            }
        },
    )
    assert resp.status_code == 200, resp.text

    node_dn = "topology/pod-1/node-920"
    inst_resp = client.get(f"/api/mo/{node_dn}/sys/lldp/inst.json")
    assert inst_resp.status_code == 200, inst_resp.text
    assert int(inst_resp.json()["totalCount"]) > 0, "new node's lldpInst missing"

    subtree_resp = client.get(
        f"/api/mo/{node_dn}/sys/lldp/inst.json"
        "?query-target=subtree&target-subtree-class=lldpAdjEp"
    )
    assert subtree_resp.status_code == 200, subtree_resp.text
    subtree_data = subtree_resp.json()
    assert int(subtree_data["totalCount"]) > 0, (
        f"new node's lldpAdjEp subtree query returned 0: {subtree_data}"
    )

    # Also verify the enriched fields landed on the dynamic path (not just
    # boot-time build) and the spine-side reciprocal adjacency was created too.
    adj = subtree_data["imdata"][0]["lldpAdjEp"]["attributes"]
    assert adj["sysName"]
    assert adj["mgmtIp"]
    assert adj["portIdV"]
    assert adj["chassisIdV"]

    links_resp = client.get("/api/class/fabricLink.json")
    node_links = [
        item["fabricLink"]["attributes"]
        for item in links_resp.json()["imdata"]
        if "920" in (item["fabricLink"]["attributes"].get("n1"), item["fabricLink"]["attributes"].get("n2"))
    ]
    assert node_links, "expected a fabricLink for the newly registered node"


def test_dynamic_node_registration_cdp_also_materialized():
    _, client = _fresh_client("LAB1")
    resp = client.post(
        "/api/mo/uni/controller/nodeidentpol/nodep-921.json",
        json={
            "fabricNodeIdentP": {
                "attributes": {
                    "dn": "uni/controller/nodeidentpol/nodep-921",
                    "name": "leaf-921",
                    "nodeType": "leaf",
                }
            }
        },
    )
    assert resp.status_code == 200, resp.text

    node_dn = "topology/pod-1/node-921"
    resp2 = client.get(
        f"/api/mo/{node_dn}/sys/cdp/inst.json"
        "?query-target=subtree&target-subtree-class=cdpAdjEp"
    )
    assert resp2.status_code == 200, resp2.text
    data = resp2.json()
    assert int(data["totalCount"]) > 0
    adj = data["imdata"][0]["cdpAdjEp"]["attributes"]
    assert adj["devId"] == adj["sysName"]
    assert adj["portId"]


# ---------------------------------------------------------------------------
# 7. CLI: aci-sim lldp
# ---------------------------------------------------------------------------

def test_cli_lldp_text_shows_neighbor(monkeypatch, capsys):
    monkeypatch.chdir(Path(__file__).parent.parent)
    rc = cli.main(["lldp", TOPO_PATH_STR, "--node", "101"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "node-101" in out
    assert "LAB1-ACI-SP01" in out or "LAB1-ACI-SP02" in out


def test_cli_lldp_json(monkeypatch, capsys):
    monkeypatch.chdir(Path(__file__).parent.parent)
    rc = cli.main(["lldp", TOPO_PATH_STR, "--node", "101", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) > 0
    row = data[0]
    assert "local_port" in row
    assert "neighbor" in row
    assert "neighbor_port" in row
    assert "mgmt_ip" in row
    assert "platform" in row


def test_cli_lldp_cdp_flag(monkeypatch, capsys):
    monkeypatch.chdir(Path(__file__).parent.parent)
    rc = cli.main(["lldp", TOPO_PATH_STR, "--node", "101", "--cdp", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert len(data) > 0
    # cdp platId values are real model/platform strings (e.g. APIC-SERVER-M3
    # or N9K-C9332C), not the lldp sysDesc free-text form.
    assert any(row["platform"] for row in data)


def test_cli_lldp_nonexistent_node_is_graceful(monkeypatch, capsys):
    monkeypatch.chdir(Path(__file__).parent.parent)
    rc = cli.main(["lldp", TOPO_PATH_STR, "--node", "999999"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No neighbors found" in out


def test_cli_lldp_nonexistent_node_json_is_empty_list(monkeypatch, capsys):
    monkeypatch.chdir(Path(__file__).parent.parent)
    rc = cli.main(["lldp", TOPO_PATH_STR, "--node", "999999", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data == []


def test_cli_lldp_missing_topology_file_returns_1(tmp_path, capsys):
    rc = cli.main(["lldp", str(tmp_path / "does-not-exist.yaml")])
    assert rc == 1
