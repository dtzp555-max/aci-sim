"""PR-14: classic ND bare /login + /logout endpoints (used by aci-py, cisco.nd)."""
from __future__ import annotations

import copy

from fastapi.testclient import TestClient

from aci_sim.ndo.app import make_ndo_app
from aci_sim.ndo.model import build_ndo_model
from aci_sim.topology.loader import load_topology


def _client():
    topo = load_topology("topology.yaml")
    state = build_ndo_model(topo, sandbox=False)
    return TestClient(make_ndo_app(copy.deepcopy(state)), raise_server_exceptions=False)


def test_bare_login_returns_token_and_jwttoken():
    c = _client()
    r = c.post("/login", json={"userName": "admin", "userPasswd": "cisco", "domain": "local"})
    assert r.status_code == 200
    body = r.json()
    assert body["token"] and body["token"] == body["jwttoken"]
    assert body["userName"] == "admin"


def test_bare_logout_ok():
    c = _client()
    assert c.post("/logout").status_code == 200


def test_new_auth_login_still_works():
    c = _client()
    r = c.post("/api/v1/auth/login", json={"userName": "admin", "userPasswd": "cisco"})
    assert r.status_code == 200 and r.json()["token"]
