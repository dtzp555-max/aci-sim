"""Regression tests for PR-4 auth fidelity.

Covers: unauthenticated/garbage/expired-cookie query rejection, credential
checking on aaaLogin, session expiry + aaaRefresh renewal, aaaLogout
invalidation, and the 422-leak fix for malformed aaaLogin bodies. See
docs/CONTRACT.md §1 (login) and §5 (error shapes) for the contract this
enforces.
"""

from __future__ import annotations

import copy

from fastapi.testclient import TestClient

import aci_sim.rest_aci.auth as auth_module
from aci_sim.build.orchestrator import build_site
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology

TOPO_PATH = "topology.yaml"


def _new_client() -> TestClient:
    """Fresh, unauthenticated TestClient backed by its own store/state."""
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
    return TestClient(make_apic_app(state))


def _login(client: TestClient, *, user: str = "admin", pwd: str = "cisco"):
    return client.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": user, "pwd": pwd}}},
    )


def _assert_apic_error_envelope(resp, code: str) -> None:
    data = resp.json()
    assert "detail" not in data, f"FastAPI detail leak: {data}"
    assert "imdata" in data
    assert data["imdata"][0]["error"]["attributes"]["code"] == code


# ---------------------------------------------------------------------------
# No-cookie / garbage-cookie / expired-cookie query rejection
# ---------------------------------------------------------------------------


def test_no_cookie_query_returns_403():
    c = _new_client()
    resp = c.get("/api/class/fabricNode.json")
    assert resp.status_code == 403
    _assert_apic_error_envelope(resp, "403")


def test_garbage_cookie_query_returns_403():
    c = _new_client()
    c.cookies.set("APIC-cookie", "not-a-real-token")
    resp = c.get("/api/class/fabricNode.json")
    assert resp.status_code == 403
    _assert_apic_error_envelope(resp, "403")


def test_expired_token_query_returns_403():
    c = _new_client()
    login = _login(c)
    assert login.status_code == 200
    token = c.cookies.get("APIC-cookie")
    assert token in auth_module._sessions

    # Force expiry without sleeping — patch the session's expires_at directly.
    auth_module._sessions[token].expires_at = auth_module._now() - 1

    resp = c.get("/api/class/fabricNode.json")
    assert resp.status_code == 403
    _assert_apic_error_envelope(resp, "403")


def test_no_cookie_mo_query_returns_403():
    c = _new_client()
    resp = c.get("/api/mo/uni/tn-MS-TN1.json")
    assert resp.status_code == 403
    _assert_apic_error_envelope(resp, "403")


def test_no_cookie_node_scoped_query_returns_403():
    c = _new_client()
    resp = c.get("/api/node/class/topology/pod-1/node-101/faultInst.json")
    assert resp.status_code == 403
    _assert_apic_error_envelope(resp, "403")


def test_no_cookie_write_returns_403():
    c = _new_client()
    resp = c.post(
        "/api/mo/uni/tn-Blocked.json",
        json={"fvTenant": {"attributes": {"name": "Blocked", "dn": "uni/tn-Blocked"}}},
    )
    assert resp.status_code == 403
    _assert_apic_error_envelope(resp, "403")
    # And the write must not have landed.
    c2 = _new_client()
    _login(c2)
    names = [
        i["fvTenant"]["attributes"]["name"]
        for i in c2.get("/api/class/fvTenant.json").json()["imdata"]
    ]
    assert "Blocked" not in names


# ---------------------------------------------------------------------------
# Credential checking
# ---------------------------------------------------------------------------


def test_wrong_password_returns_401_envelope():
    c = _new_client()
    resp = _login(c, pwd="wrong-password")
    assert resp.status_code == 401
    _assert_apic_error_envelope(resp, "401")
    assert "APIC-cookie" not in resp.cookies


def test_wrong_username_returns_401_envelope():
    c = _new_client()
    resp = _login(c, user="notadmin")
    assert resp.status_code == 401
    _assert_apic_error_envelope(resp, "401")


def test_correct_credentials_return_200_with_token():
    c = _new_client()
    resp = _login(c)
    assert resp.status_code == 200
    data = resp.json()
    token = data["imdata"][0]["aaaLogin"]["attributes"]["token"]
    assert token
    assert "APIC-cookie" in resp.cookies


# ---------------------------------------------------------------------------
# Malformed aaaLogin body -> 400 imdata envelope (never FastAPI {"detail":...})
# ---------------------------------------------------------------------------


def test_malformed_login_body_non_dict_top_level():
    c = _new_client()
    resp = c.post("/api/aaaLogin.json", content=b'"just a string"',
                   headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    data = resp.json()
    assert "imdata" in data
    assert "detail" not in data


def test_malformed_login_body_missing_aaauser():
    c = _new_client()
    resp = c.post("/api/aaaLogin.json", json={"wrongKey": {}})
    assert resp.status_code == 400
    data = resp.json()
    assert "imdata" in data
    assert "detail" not in data


def test_malformed_login_body_non_json():
    c = _new_client()
    resp = c.post(
        "/api/aaaLogin.json",
        content=b"not json at all {{{",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert "imdata" in data
    assert "detail" not in data


# ---------------------------------------------------------------------------
# aaaRefresh
# ---------------------------------------------------------------------------


def test_refresh_without_login_returns_403():
    c = _new_client()
    resp = c.get("/api/aaaRefresh.json")
    assert resp.status_code == 403
    _assert_apic_error_envelope(resp, "403")


def test_refresh_with_garbage_cookie_returns_403():
    c = _new_client()
    c.cookies.set("APIC-cookie", "garbage")
    resp = c.get("/api/aaaRefresh.json")
    assert resp.status_code == 403
    _assert_apic_error_envelope(resp, "403")


def test_refresh_extends_expiry():
    c = _new_client()
    _login(c)
    token = c.cookies.get("APIC-cookie")
    sess = auth_module._sessions[token]

    # Move expiry to the near future (still valid) and confirm refresh pushes
    # it back out to a full lifetime.
    sess.expires_at = auth_module._now() + 1
    old_expiry = sess.expires_at

    resp = c.get("/api/aaaRefresh.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "aaaLogin" in data["imdata"][0]

    new_token = c.cookies.get("APIC-cookie")
    assert new_token in auth_module._sessions
    assert auth_module._sessions[new_token].expires_at > old_expiry

    # And a subsequent query with the refreshed cookie still works.
    q = c.get("/api/class/fabricNode.json")
    assert q.status_code == 200


def test_refresh_on_expired_token_returns_403():
    c = _new_client()
    _login(c)
    token = c.cookies.get("APIC-cookie")
    auth_module._sessions[token].expires_at = auth_module._now() - 1

    resp = c.get("/api/aaaRefresh.json")
    assert resp.status_code == 403
    _assert_apic_error_envelope(resp, "403")


# ---------------------------------------------------------------------------
# aaaLogout
# ---------------------------------------------------------------------------


def test_logout_then_query_returns_403():
    c = _new_client()
    _login(c)
    token = c.cookies.get("APIC-cookie")
    assert token in auth_module._sessions

    resp = c.post("/api/aaaLogout.json")
    assert resp.status_code == 200
    assert token not in auth_module._sessions

    q = c.get("/api/class/fabricNode.json")
    assert q.status_code == 403
    _assert_apic_error_envelope(q, "403")


# ---------------------------------------------------------------------------
# /_sim control plane must remain unauthenticated
# ---------------------------------------------------------------------------


def test_sim_reset_works_without_auth():
    c = _new_client()
    resp = c.post("/_sim/reset")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
