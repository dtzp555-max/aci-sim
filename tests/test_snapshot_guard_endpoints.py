"""The 409 guard at the HTTP boundary, on both plane kinds.

The stamping tests cover wrap/unwrap/compatibility as pure functions. Nothing
covered the endpoints that call them, so deleting either `if not ok and not
force: 409` left the whole suite green — the guard's actual observable
behaviour rested on a manual check written up in a PR body.

The two planes report the refusal differently on purpose: NDO uses FastAPI's
{"detail": …} and APIC wraps it in the imdata/error envelope a real APIC
returns. Both shapes are pinned here because `scripts/sim-state.sh` parses
each of them.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from aci_sim.control.persist import ENVELOPE_VERSION, sim_version, topology_fingerprint


def _write_snapshot(path, payload, meta):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"_meta": meta, "data": payload}), encoding="utf-8")


def _good_meta():
    return {
        "envelope": ENVELOPE_VERSION,
        "sim_version": sim_version(),
        "topology": topology_fingerprint(),
        "saved_at": "2026-08-01T00:00:00+00:00",
    }


# ── NDO plane ───────────────────────────────────────────────────────────────

@pytest.fixture
def ndo(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    from aci_sim.ndo.app import make_ndo_app
    from aci_sim.ndo.model import NdoState

    state = NdoState(
        sites=[], tenants=[], schemas=[], schema_details={},
        template_summaries=[], tenant_policy_templates={},
        fabric_connectivity={}, policy_states={}, audit_records=[],
    )
    return TestClient(make_ndo_app(state)), tmp_path


def test_ndo_load_accepts_a_matching_snapshot(ndo):
    client, state_dir = ndo
    client.post("/_sim/save/ok")
    r = client.post("/_sim/load/ok")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ndo_load_refuses_a_different_sim_version(ndo):
    client, state_dir = ndo
    meta = _good_meta() | {"sim_version": "0.1.0"}
    _write_snapshot(state_dir / "old.ndo.json", {"tenants": []}, meta)

    r = client.post("/_sim/load/old")
    assert r.status_code == 409
    assert "0.1.0" in r.json()["detail"]


def test_ndo_load_refuses_a_different_topology(ndo):
    client, state_dir = ndo
    meta = _good_meta() | {"topology": "0000deadbeef"}
    _write_snapshot(state_dir / "other.ndo.json", {"tenants": []}, meta)
    assert client.post("/_sim/load/other").status_code == 409


def test_ndo_load_refuses_an_unstamped_snapshot(ndo):
    client, state_dir = ndo
    # pre-envelope: a bare document with no _meta at all
    (state_dir / "legacy.ndo.json").write_text(json.dumps({"tenants": []}), encoding="utf-8")
    r = client.post("/_sim/load/legacy")
    assert r.status_code == 409
    assert "predates" in r.json()["detail"]


def test_ndo_force_restores_a_refused_snapshot(ndo):
    client, state_dir = ndo
    meta = _good_meta() | {"sim_version": "0.1.0"}
    _write_snapshot(state_dir / "old.ndo.json", {"tenants": []}, meta)

    assert client.post("/_sim/load/old").status_code == 409
    assert client.post("/_sim/load/old?force=1").status_code == 200


def test_ndo_missing_snapshot_is_404_not_409(ndo):
    """404 and 409 mean different things to sim-state.sh — it exits 3 vs 2."""
    client, _ = ndo
    assert client.post("/_sim/load/never-saved").status_code == 404


# ── APIC plane: same guard, different error envelope ────────────────────────

@pytest.fixture
def apic(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_STATE_DIR", str(tmp_path))
    import copy

    from aci_sim.build.orchestrator import build_site
    from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
    from aci_sim.topology.loader import load_topology

    topo = load_topology("topology.yaml")
    site = topo.sites[0]
    store = build_site(topo, site)
    state = ApicSiteState(name=site.name, site=site, topo=topo, store=store,
                          baseline=copy.deepcopy(store))
    client = TestClient(make_apic_app(state))
    client.post("/api/aaaLogin.json",
                json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}})
    return client, tmp_path


def _apic_snapshot_name(state_dir):
    """Whatever suffix this plane saves under — read it back rather than guess."""
    return next(state_dir.glob("*.apic.json"))


def test_apic_load_refuses_and_uses_the_imdata_error_envelope(apic):
    client, state_dir = apic
    r = client.post("/_sim/save/ok")
    assert r.status_code == 200

    path = _apic_snapshot_name(state_dir)
    doc = json.loads(path.read_text())
    doc["_meta"]["sim_version"] = "0.1.0"
    path.write_text(json.dumps(doc), encoding="utf-8")

    r = client.post("/_sim/load/ok")
    assert r.status_code == 409
    # sim-state.sh reads exactly this path out of the response
    text = r.json()["imdata"][0]["error"]["attributes"]["text"]
    assert "0.1.0" in text


def test_apic_force_restores_a_refused_snapshot(apic):
    client, state_dir = apic
    client.post("/_sim/save/ok")
    path = _apic_snapshot_name(state_dir)
    doc = json.loads(path.read_text())
    doc["_meta"]["sim_version"] = "0.1.0"
    path.write_text(json.dumps(doc), encoding="utf-8")

    assert client.post("/_sim/load/ok").status_code == 409
    assert client.post("/_sim/load/ok?force=1").status_code == 200


def test_apic_load_accepts_a_matching_snapshot(apic):
    client, _ = apic
    client.post("/_sim/save/ok")
    assert client.post("/_sim/load/ok").status_code == 200
