"""Tests for PR-6 — control-plane fixes:

1. /_sim/reload rebuilds from the FRESH post-reload Site object, not the
   stale pre-reload one (finding #11).
2. fabricNodeIdentP write reaction materializes a full node-registration MO
   set (fabricNode + topSystem + healthInst + fabricLink cabling + l1PhysIf
   inventory), fires for a nested (not just top-level) fabricNodeIdentP, and
   derives pod from the site instead of hardcoding 1 (finding #19).
3. GET /api/node/class/{cls}.json (no DN prefix) runs a fabric-wide class
   query, matching /api/class/{cls}.json (finding #12).
"""

from __future__ import annotations

import copy
import ipaddress

import yaml
from fastapi.testclient import TestClient

from aci_sim.build.orchestrator import build_site
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology

TOPO_PATH = "topology.yaml"  # run from project root


def _login(client: TestClient) -> None:
    resp = client.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    assert resp.status_code == 200, resp.text


def _fresh_client(site_name: str = "LAB1") -> tuple[ApicSiteState, TestClient]:
    topo = load_topology(TOPO_PATH)
    site = topo.site_by_name(site_name)
    store = build_site(topo, site)
    state = ApicSiteState(
        name=site.name,
        site=site,
        topo=topo,
        store=store,
        baseline=copy.deepcopy(store),
    )
    client = TestClient(make_apic_app(state))
    _login(client)
    return state, client


# ---------------------------------------------------------------------------
# Fix 1 — /_sim/reload uses the fresh Site, not the stale one
# ---------------------------------------------------------------------------


def test_reload_picks_up_topology_yaml_edit(tmp_path, monkeypatch):
    """Editing topology.yaml (adding a leaf) and POSTing /_sim/reload must
    make the new leaf show up — the old code rebuilt against the STALE
    pre-reload state.site object, so the edit was silently ignored.
    """
    with open(TOPO_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # Add a brand-new leaf to LAB1 in a modified copy of the topology.
    lab1 = next(s for s in raw["sites"] if s["name"] == "LAB1")
    lab1["leaves"].append({"id": 199, "name": "LAB1-ACI-LF199"})

    modified_path = tmp_path / "topology_modified.yaml"
    modified_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    # Build initial state from the ORIGINAL topology (no node 199 yet).
    state, client = _fresh_client("LAB1")
    ids_before = [
        item["fabricNode"]["attributes"]["id"]
        for item in client.get("/api/class/fabricNode.json").json()["imdata"]
    ]
    assert "199" not in ids_before

    # Point TOPOLOGY_PATH at the modified file and reload.
    monkeypatch.setenv("TOPOLOGY_PATH", str(modified_path))
    import aci_sim.runtime.config as config_mod
    monkeypatch.setattr(config_mod, "TOPOLOGY_PATH", str(modified_path))

    resp = client.post("/_sim/reload")
    assert resp.status_code == 200, resp.text

    ids_after = [
        item["fabricNode"]["attributes"]["id"]
        for item in client.get("/api/class/fabricNode.json").json()["imdata"]
    ]
    assert "199" in ids_after, f"reload did not pick up the new leaf; ids: {ids_after}"

    # state.site must now be the fresh Site object (has the new leaf), not
    # the stale one captured at client construction time.
    leaf_ids = {n.id for n in state.site.leaf_nodes()}
    assert 199 in leaf_ids


def test_reload_errors_when_site_removed(tmp_path, monkeypatch):
    """If the reloaded topology no longer contains this site, /_sim/reload
    must return an APIC-style 400 error, not crash or silently keep stale
    state.
    """
    with open(TOPO_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # Remove LAB2 entirely; also drop anything that references it so the
    # remaining topology still validates.
    raw["sites"] = [s for s in raw["sites"] if s["name"] != "LAB2"]
    raw["isn"] = {"enabled": False}
    for tenant in raw.get("tenants", []):
        tenant["sites"] = [s for s in tenant.get("sites", []) if s != "LAB2"]
        if tenant.get("stretch") and len(tenant["sites"]) < 2:
            tenant["stretch"] = False
        tenant["l3outs"] = [
            l3o for l3o in tenant.get("l3outs", []) if l3o.get("site") != "LAB2"
        ]

    modified_path = tmp_path / "topology_no_lab2.yaml"
    modified_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    state, client = _fresh_client("LAB2")

    import aci_sim.runtime.config as config_mod
    monkeypatch.setattr(config_mod, "TOPOLOGY_PATH", str(modified_path))

    resp = client.post("/_sim/reload")
    assert resp.status_code == 400, resp.text
    data = resp.json()
    assert "error" in data["imdata"][0]

    # Original state must be left untouched (no crash / partial mutation).
    assert state.site.name == "LAB2"


# ---------------------------------------------------------------------------
# Fix 2 — enriched fabricNodeIdentP reaction (+ nested form) + /_sim/add-leaf
# ---------------------------------------------------------------------------


def _assert_enriched_node(client: TestClient, node_id: int, pod: int = 1) -> None:
    node_dn = f"topology/pod-{pod}/node-{node_id}"

    # fabricNode
    fn = client.get("/api/class/fabricNode.json").json()["imdata"]
    ids = [item["fabricNode"]["attributes"]["id"] for item in fn]
    assert str(node_id) in ids, f"fabricNode {node_id} missing; ids={ids}"

    # topSystem with valid, parseable IPs
    ts_resp = client.get(f"/api/mo/{node_dn}/sys.json")
    assert ts_resp.status_code == 200, ts_resp.text
    ts_attrs = ts_resp.json()["imdata"][0]["topSystem"]["attributes"]
    ipaddress.IPv4Address(ts_attrs["address"])       # raises if invalid
    ipaddress.IPv4Address(ts_attrs["oobMgmtAddr"])   # raises if invalid

    # healthInst
    health_resp = client.get(f"/api/mo/{node_dn}/sys/health.json")
    assert health_resp.status_code == 200, health_resp.text

    # At least one fabricLink to a spine
    links = client.get("/api/class/fabricLink.json").json()["imdata"]
    node_links = [
        item["fabricLink"]["attributes"]
        for item in links
        if str(node_id) in (item["fabricLink"]["attributes"].get("n1"), item["fabricLink"]["attributes"].get("n2"))
    ]
    assert node_links, f"no fabricLink found for node {node_id}"

    # l1PhysIf inventory present under this node
    resp = client.get(f"/api/node/class/{node_dn}/l1PhysIf.json")
    assert resp.status_code == 200, resp.text
    assert int(resp.json()["totalCount"]) >= 1, "expected at least one l1PhysIf for the new node"


def test_fabric_node_ident_reaction_top_level():
    _, client = _fresh_client("LAB1")
    resp = client.post(
        "/api/mo/uni/controller/nodeidentpol/nodep-910.json",
        json={
            "fabricNodeIdentP": {
                "attributes": {
                    "dn": "uni/controller/nodeidentpol/nodep-910",
                    "name": "leaf-910",
                    "nodeType": "leaf",
                }
            }
        },
    )
    assert resp.status_code == 200, resp.text
    _assert_enriched_node(client, 910, pod=1)


def test_fabric_node_ident_reaction_nested_under_parent():
    """A nested (non-top-level) fabricNodeIdentP must ALSO trigger the
    reaction — previously the reaction only fired when fabricNodeIdentP was
    the top-level POSTed class.
    """
    _, client = _fresh_client("LAB1")
    resp = client.post(
        "/api/mo/uni/controller.json",
        json={
            "fabricInst": {
                "attributes": {"dn": "uni/controller"},
                "children": [
                    {
                        "fabricNodeIdentP": {
                            "attributes": {
                                "dn": "uni/controller/nodeidentpol/nodep-911",
                                "name": "leaf-911",
                                "nodeType": "leaf",
                            }
                        }
                    }
                ],
            }
        },
    )
    assert resp.status_code == 200, resp.text
    _assert_enriched_node(client, 911, pod=1)


def test_add_leaf_produces_same_enriched_set():
    _, client = _fresh_client("LAB1")
    resp = client.post("/_sim/add-leaf", json={"id": 912, "name": "leaf-912"})
    assert resp.status_code == 200, resp.text
    _assert_enriched_node(client, 912, pod=1)


def test_add_leaf_reaction_still_materializes_new_node_class_query():
    """Regression guard for the existing test_add_leaf_reaction /
    verify_autoaci T1-09 behavior: fabricNode must still show up by class
    query after the enrichment."""
    _, client = _fresh_client("LAB1")
    resp = client.post(
        "/api/mo/uni/controller/nodeidentpol/nodep-913.json",
        json={
            "fabricNodeIdentP": {
                "attributes": {
                    "dn": "uni/controller/nodeidentpol/nodep-913",
                    "name": "leaf-913",
                    "nodeType": "leaf",
                }
            }
        },
    )
    assert resp.status_code == 200
    ids = [
        item["fabricNode"]["attributes"]["id"]
        for item in client.get("/api/class/fabricNode.json").json()["imdata"]
    ]
    assert "913" in ids


def test_node_registration_derives_pod_from_site_not_hardcoded():
    """Registering a node on a site whose pod != 1 must use that site's pod,
    not a hardcoded 1. topology.yaml's sites both use pod=1 today, so this
    test builds a synthetic site with pod=2 directly to prove the reaction
    reads site.pod instead of hardcoding.
    """
    from aci_sim.mit.store import MITStore
    from aci_sim.rest_aci.writes import materialize_node_registration
    from aci_sim.topology.loader import load_topology

    topo = load_topology(TOPO_PATH)
    site = topo.site_by_name("LAB1").model_copy(deep=True)
    site.pod = 7

    store = MITStore()
    materialize_node_registration(store, topo=topo, site=site, node_id=950, name="leaf-950", role="leaf")

    assert store.get("topology/pod-7/node-950") is not None
    assert store.get("topology/pod-1/node-950") is None


# ---------------------------------------------------------------------------
# Fix 3 — fabric-wide node/class query (no DN prefix)
# ---------------------------------------------------------------------------


def test_fabric_wide_node_class_query_matches_class_query():
    _, client = _fresh_client("LAB1")
    fabric_wide = client.get("/api/node/class/topSystem.json")
    assert fabric_wide.status_code == 200, fabric_wide.text
    class_query = client.get("/api/class/topSystem.json")
    assert class_query.status_code == 200

    assert fabric_wide.json()["totalCount"] == class_query.json()["totalCount"]
    fw_ids = sorted(item["topSystem"]["attributes"]["id"] for item in fabric_wide.json()["imdata"])
    cq_ids = sorted(item["topSystem"]["attributes"]["id"] for item in class_query.json()["imdata"])
    assert fw_ids == cq_ids


def test_node_scoped_class_query_still_works():
    _, client = _fresh_client("LAB1")
    resp = client.get("/api/node/class/topology/pod-1/node-101/faultInst.json")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["imdata"], list)
    assert isinstance(data["totalCount"], str)
