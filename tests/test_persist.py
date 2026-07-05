"""Tests for file-backed state persistence (aci_sim/control/persist.py).

Covers both planes:
  (a) MITStore round-trip (APIC plane) — serialize_store/deserialize_store
  (b) NdoState round-trip (NDO plane) — serialize_ndo/apply_ndo
  (c) APIC app integration — /_sim/save + /_sim/load over the real REST API
  (d) NDO app integration — same, on the NDO app

All tests set SIM_STATE_DIR to an isolated tmp_path so nothing is ever
written under the real ~/.aci-sim/state.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aci_sim.build.orchestrator import build_site
from aci_sim.control.persist import (
    apply_ndo,
    deserialize_store,
    serialize_ndo,
    serialize_store,
)
from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.ndo.app import make_ndo_app
from aci_sim.ndo.model import build_ndo_model
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology

TOPO_PATH = "topology.yaml"


# ---------------------------------------------------------------------------
# (a) MITStore round-trip
# ---------------------------------------------------------------------------


def test_store_round_trip_preserves_all_dns_and_subtree(monkeypatch, tmp_path):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))

    store = MITStore()
    store.add(MO("fvTenant", dn="uni/tn-PERSIST", name="PERSIST"))
    store.add(
        MO(
            "fvAp",
            dn="uni/tn-PERSIST/ap-APP1",
            name="APP1",
        )
    )
    # Deep nested child MO under the tenant's subtree.
    store.add(
        MO(
            "fvAEPg",
            dn="uni/tn-PERSIST/ap-APP1/epg-WEB",
            name="WEB",
        )
    )

    before_dns = {mo.dn for mo in store.all()}

    data = serialize_store(store)
    restored = deserialize_store(data)

    after_dns = {mo.dn for mo in restored.all()}
    assert after_dns == before_dns

    # Subtree query on the tenant must still return the deeply nested child.
    subtree_dns = {mo.dn for mo in restored.subtree("uni/tn-PERSIST")}
    assert "uni/tn-PERSIST/ap-APP1/epg-WEB" in subtree_dns
    assert "uni/tn-PERSIST/ap-APP1" in subtree_dns


def test_store_round_trip_via_real_topology(monkeypatch, tmp_path):
    """Round-trip a store built from the real topology (not just a toy one)."""
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    topo = load_topology(TOPO_PATH)
    site = topo.sites[0]
    store = build_site(topo, site)

    before_dns = {mo.dn for mo in store.all()}
    data = serialize_store(store)
    restored = deserialize_store(data)
    after_dns = {mo.dn for mo in restored.all()}

    assert after_dns == before_dns
    assert len(before_dns) > 0


# ---------------------------------------------------------------------------
# (b) NdoState round-trip
# ---------------------------------------------------------------------------


def test_ndo_state_round_trip_onto_different_state(monkeypatch, tmp_path):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    topo = load_topology(TOPO_PATH)

    source_state = build_ndo_model(topo)
    # Mutate tenants + schemas so the persisted snapshot differs from a fresh build.
    source_state.tenants.append(
        {"id": "tenant-extra", "name": "EXTRA", "displayName": "EXTRA", "siteAssociations": []}
    )
    source_state.schemas.append(
        {"id": "schema-extra", "displayName": "EXTRA-schema", "name": "EXTRA-schema", "templates": []}
    )

    data = serialize_ndo(source_state)

    # Apply onto a DIFFERENT fresh state object.
    target_state = build_ndo_model(topo)
    apply_ndo(target_state, data)

    for f in (
        "tenants",
        "schemas",
        "schema_details",
        "template_summaries",
        "tenant_policy_templates",
        "policy_states",
        "audit_records",
        "local_users",
        "remote_users",
        "extra_schemas",
        "sites",
        "fabric_connectivity",
    ):
        assert getattr(target_state, f) == getattr(source_state, f), f"field {f} mismatch"

    # Confirm mutation actually landed (not just equal-because-untouched).
    assert any(t["id"] == "tenant-extra" for t in target_state.tenants)
    assert any(s["id"] == "schema-extra" for s in target_state.schemas)


def test_apply_ndo_mutates_in_place_not_a_new_object(monkeypatch, tmp_path):
    """apply_ndo must mutate the SAME object (setattr), since make_ndo_app
    closes over the state object identity."""
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    topo = load_topology(TOPO_PATH)
    state = build_ndo_model(topo)
    state_id = id(state)

    data = serialize_ndo(state)
    data["tenants"] = data["tenants"] + [{"id": "t2", "name": "T2", "displayName": "T2", "siteAssociations": []}]
    apply_ndo(state, data)

    assert id(state) == state_id
    assert any(t["id"] == "t2" for t in state.tenants)


# ---------------------------------------------------------------------------
# (c) APIC app integration
# ---------------------------------------------------------------------------


@pytest.fixture
def apic_client_and_state():
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


def test_apic_save_reset_load_round_trip(monkeypatch, tmp_path, apic_client_and_state):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    client, state = apic_client_and_state

    # Create a new tenant via the REST API.
    client.post(
        "/api/mo/uni/tn-ITEST.json",
        json={"fvTenant": {"attributes": {"name": "ITEST", "dn": "uni/tn-ITEST"}}},
    )
    resp = client.get("/api/class/fvTenant.json")
    names = [i["fvTenant"]["attributes"]["name"] for i in resp.json()["imdata"]]
    assert "ITEST" in names

    # Save.
    r = client.post("/_sim/save/itest")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["count"] > 0
    saved_path = Path(body["file"])
    assert saved_path.exists()
    assert saved_path.parent == tmp_path

    # Reset wipes the tenant.
    client.post("/_sim/reset")
    resp2 = client.get("/api/class/fvTenant.json")
    names2 = [i["fvTenant"]["attributes"]["name"] for i in resp2.json()["imdata"]]
    assert "ITEST" not in names2

    # Load restores it.
    r2 = client.post("/_sim/load/itest")
    assert r2.status_code == 200

    resp3 = client.get("/api/class/fvTenant.json")
    names3 = [i["fvTenant"]["attributes"]["name"] for i in resp3.json()["imdata"]]
    assert "ITEST" in names3


def test_apic_load_missing_state_returns_404(monkeypatch, tmp_path, apic_client_and_state):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    client, _state = apic_client_and_state

    r = client.post("/_sim/load/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert body["imdata"][0]["error"]["attributes"]["text"]


# ---------------------------------------------------------------------------
# (d) NDO app integration
# ---------------------------------------------------------------------------


@pytest.fixture
def ndo_client_and_state():
    topo = load_topology(TOPO_PATH)
    state = build_ndo_model(topo)
    app = make_ndo_app(state)
    with TestClient(app) as c:
        yield c, state


def test_ndo_save_load_round_trip(monkeypatch, tmp_path, ndo_client_and_state):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    client, state = ndo_client_and_state

    # Create a new tenant via the REST API.
    resp = client.post(
        "/mso/api/v1/tenants",
        json={"displayName": "NDO-ITEST", "name": "NDO-ITEST"},
    )
    assert resp.status_code == 200
    new_id = resp.json()["id"]

    r = client.post("/_sim/save/nditest")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    saved_path = Path(body["file"])
    assert saved_path.exists()
    assert saved_path.parent == tmp_path

    # Mutate away the tenant to prove load actually restores state (no reset
    # endpoint exists on the NDO app, so directly clear the list in place).
    state.tenants.clear()
    resp2 = client.get("/mso/api/v1/tenants")
    ids2 = [t["id"] for t in resp2.json()["tenants"]]
    assert new_id not in ids2

    r2 = client.post("/_sim/load/nditest")
    assert r2.status_code == 200

    resp3 = client.get("/mso/api/v1/tenants")
    ids3 = [t["id"] for t in resp3.json()["tenants"]]
    assert new_id in ids3


def test_ndo_load_missing_state_returns_404(monkeypatch, tmp_path, ndo_client_and_state):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    client, _state = ndo_client_and_state

    r = client.post("/_sim/load/does-not-exist")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]
