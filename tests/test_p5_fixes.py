"""Regression tests for the 6 Phase-5 cold-review findings.

P1-A empty-subtree-on-existing-MO -> 200; P1-B non-dict body -> 400 (not 500);
P2-A child without dn kept (no dn="" clobber); P2-B delete-with-children removes
subtree; P3 restore-unknown -> 404. (P4 single-site supervisor covered by inspection.)

PR-9 update: GET /api/mo/{dn}.json on a nonexistent DN now returns 200 + empty
imdata (not 404) — see tests/test_pr9_ansible.py for the full rationale
(cisco.aci's GET-before-POST existence check treats any non-200 as fatal).
test_p1a_nonexistent_mo_returns_404 renamed/updated accordingly.
"""
import copy

import pytest
from fastapi.testclient import TestClient

from aci_sim.build.orchestrator import build_site
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology


@pytest.fixture()
def client_and_store():
    topo = load_topology("topology.yaml")
    site = topo.sites[0]
    store = build_site(topo, site)
    state = ApicSiteState(name=site.name, site=site, topo=topo, store=store, baseline=copy.deepcopy(store))
    c = TestClient(make_apic_app(state))
    c.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    return c, store


def test_p1a_existing_mo_empty_subtree_returns_200(client_and_store):
    c, store = client_and_store
    node = next(m for m in store.by_class("fabricNode") if m.attrs.get("role") == "leaf")
    r = c.get(f"/api/mo/{node.dn}.json", params={"query-target": "subtree", "target-subtree-class": "bgpVpnRoute"})
    assert r.status_code == 200
    assert r.json()["totalCount"] == "0"


def test_p1a_nonexistent_mo_returns_200_empty(client_and_store):
    c, _ = client_and_store
    r = c.get("/api/mo/uni/tn-GHOST.json")
    assert r.status_code == 200
    assert r.json()["totalCount"] == "0"
    assert r.json()["imdata"] == []


@pytest.mark.parametrize("bad", ["str", None, 123, [1, 2]])
def test_p1b_non_dict_body_returns_400(client_and_store, bad):
    c, _ = client_and_store
    assert c.post("/api/mo/uni/tn-X.json", json={"fvTenant": bad}).status_code == 400


def test_p2a_children_without_dn_are_both_kept(client_and_store):
    c, store = client_and_store
    for nm in ("FIRST", "SECOND"):
        c.post("/api/mo/uni/tn-CA.json", json={"fvTenant": {"attributes": {"name": "CA"}, "children": [{"fvBD": {"attributes": {"name": nm}}}]}})
    r = c.get("/api/class/fvBD.json", params={"query-target-filter": 'or(eq(fvBD.name,"FIRST"),eq(fvBD.name,"SECOND"))'})
    names = {i["fvBD"]["attributes"].get("name") for i in r.json()["imdata"]}
    assert {"FIRST", "SECOND"} <= names
    assert store.get("") is None


def test_p2b_delete_with_children_removes_subtree(client_and_store):
    c, _ = client_and_store
    c.post("/api/mo/uni/tn-DP.json", json={"fvTenant": {"attributes": {"name": "DP"}, "children": [{"fvBD": {"attributes": {"name": "b", "dn": "uni/tn-DP/BD-b"}}}]}})
    c.post("/api/mo/uni/tn-DP.json", json={"fvTenant": {"attributes": {"name": "DP", "status": "deleted"}, "children": [{"fvBD": {"attributes": {"dn": "uni/tn-DP/BD-b"}}}]}})
    r = c.get("/api/mo/uni/tn-DP/BD-b.json")
    assert r.status_code == 200
    assert r.json()["totalCount"] == "0"


def test_p3_restore_unknown_snapshot_returns_404(client_and_store):
    c, _ = client_and_store
    assert c.post("/_sim/restore/nope").status_code == 404
