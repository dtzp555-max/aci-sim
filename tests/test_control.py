"""Tests for the /_sim control-plane admin endpoints (Phase 5)."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from aci_sim.build.orchestrator import build_site
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology

TOPO_PATH = "topology.yaml"


@pytest.fixture
def client_and_state():
    topo = load_topology(TOPO_PATH)
    site = topo.sites[0]
    store = build_site(topo, site)
    state = ApicSiteState(
        name=site.name,
        site=site,
        topo=topo,
        store=store,
        baseline=copy.deepcopy(store),
    )
    app = make_apic_app(state)
    c = TestClient(app)
    c.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    return c, state


def test_reset_removes_posted_tenant(client_and_state):
    client, state = client_and_state
    # POST a new tenant
    client.post(
        "/api/mo/uni/tn-TEMP.json",
        json={"fvTenant": {"attributes": {"name": "TEMP", "dn": "uni/tn-TEMP"}}},
    )
    # Verify it's there
    resp = client.get("/api/class/fvTenant.json")
    names = [i["fvTenant"]["attributes"]["name"] for i in resp.json()["imdata"]]
    assert "TEMP" in names
    # Reset
    r = client.post("/_sim/reset")
    assert r.status_code == 200
    # Verify it's gone
    resp2 = client.get("/api/class/fvTenant.json")
    names2 = [i["fvTenant"]["attributes"]["name"] for i in resp2.json()["imdata"]]
    assert "TEMP" not in names2


def test_snapshot_restore(client_and_state):
    client, state = client_and_state
    # POST a tenant
    client.post(
        "/api/mo/uni/tn-SNAP.json",
        json={"fvTenant": {"attributes": {"name": "SNAP", "dn": "uni/tn-SNAP"}}},
    )
    # Snapshot
    client.post("/_sim/snapshot/s1")
    # Reset to baseline (removes SNAP)
    client.post("/_sim/reset")
    # Restore
    client.post("/_sim/restore/s1")
    # Verify SNAP is back
    resp = client.get("/api/class/fvTenant.json")
    names = [i["fvTenant"]["attributes"]["name"] for i in resp.json()["imdata"]]
    assert "SNAP" in names
