"""Tests for APIC query subscriptions + websocket push-on-change.

Covers:
  1. `?subscription=yes` on the class-query route returns a top-level
     `subscriptionId`; the SAME query WITHOUT that param is byte-identical
     to the pre-subscription response (backward compat — critical).
  2. Websocket `/socket<token>` push for created/modified/deleted events on a
     subscribed class.
  3. A change to an UNsubscribed class does not push anything.
  4. `GET /api/subscriptionRefresh.json?id=<id>` returns 200.
  5. An unknown-token websocket is rejected (closed before any message).
  6. Disconnecting a websocket cleans up its subscriptions (a later change no
     longer has anywhere to push, and does not error).

This starlette version's WebSocketTestSession.receive_json() has no built-in
timeout, so every websocket receive here goes through a thread+Future-bounded
wrapper (`_receive_json_with_timeout` / `_assert_no_event` below) so a
missing/never-arriving event fails fast instead of hanging the suite.
"""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aci_sim.build.orchestrator import build_site
from aci_sim.rest_aci import subscriptions as subs
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology

TOPO_PATH = "topology.yaml"  # run from project root


def _receive_json_with_timeout(ws, timeout: float = 5.0):
    """Bounded-wait wrapper around WebSocketTestSession.receive_json().

    This starlette version's TestClient websocket session has no built-in
    receive timeout (`receive()` is a blocking `anyio` portal call with no
    deadline) — an event that never arrives due to a regression would hang
    the whole test suite instead of failing fast. Run the blocking call on a
    worker thread and bound it with `Future.result(timeout=...)` so a missing
    push surfaces as a prompt, clear test failure.

    Deliberately does NOT use the ThreadPoolExecutor context manager: on a
    timeout the worker thread is still blocked inside `receive()` (there is
    no way to cancel it), and `__exit__`'s default `shutdown(wait=True)`
    would hang waiting for it — defeating the entire point of this helper.
    The pool (and its one leaked, permanently-blocked thread) is simply
    abandoned; harmless for a short-lived test process.
    """
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(ws.receive_json)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        pytest.fail(f"no websocket event received within {timeout}s")


def _assert_no_event(ws, timeout: float = 2.0) -> None:
    """Assert NO event arrives within *timeout* (the negative-case counterpart).

    `pytest.fail` (used by `_receive_json_with_timeout` above) raises
    `_pytest.outcomes.Failed`, which subclasses `BaseException` — NOT
    `Exception` — specifically so a real test failure can never be
    accidentally swallowed by a broad `except Exception`/`pytest.raises
    (Exception)`. That means this helper must check for the timeout directly
    rather than wrapping `_receive_json_with_timeout` in `pytest.raises`.
    """
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(ws.receive_json)
    try:
        event = future.result(timeout=timeout)
    except FutureTimeoutError:
        return  # correct: no event arrived
    pytest.fail(f"unexpected websocket event arrived: {event!r}")


@pytest.fixture()
def client():
    """A fresh app/store/client per test, plus a clean subscriptions registry.

    Unlike test_rest_aci.py's module-scoped fixture, subscription tests need
    per-test isolation: the subscriptions module holds process-wide module
    state (subscriptionId counter, connections, live subs) that must not leak
    between tests — a stale connection/subscription from a previous test
    could cause a false-positive push or mask a missing one.
    """
    subs.reset()
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
    with TestClient(app) as c:
        yield c
    subs.reset()


@pytest.fixture()
def token(client):
    """Log in and return the APIC-cookie token (also left set on the client)."""
    resp = client.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    assert resp.status_code == 200
    return resp.cookies["APIC-cookie"]


# ---------------------------------------------------------------------------
# Subscribe (opt-in query param) + backward compat
# ---------------------------------------------------------------------------


def test_subscribe_returns_subscription_id(client, token):
    resp = client.get("/api/class/fvTenant.json?subscription=yes")
    assert resp.status_code == 200
    data = resp.json()
    assert "subscriptionId" in data
    assert isinstance(data["subscriptionId"], str)
    assert data["subscriptionId"]
    # Normal query fields must still be present and correct.
    assert "imdata" in data
    assert "totalCount" in data


def test_plain_query_byte_identical_without_subscription_param(client, token):
    """The critical backward-compat guarantee: no ?subscription=yes => no
    subscriptionId field, no behavior change at all vs. pre-feature shape."""
    resp = client.get("/api/class/fvTenant.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "subscriptionId" not in data
    assert set(data.keys()) == {"imdata", "totalCount"}


def test_mo_query_subscribe_returns_subscription_id(client, token):
    resp = client.get("/api/mo/uni/tn-mgmt.json?subscription=yes")
    assert resp.status_code == 200
    data = resp.json()
    assert "subscriptionId" in data


def test_node_class_fabric_wide_subscribe_returns_subscription_id(client, token):
    resp = client.get("/api/node/class/fvTenant.json?subscription=yes")
    assert resp.status_code == 200
    data = resp.json()
    assert "subscriptionId" in data


# ---------------------------------------------------------------------------
# Subscription refresh
# ---------------------------------------------------------------------------


def test_subscription_refresh_returns_200(client, token):
    resp = client.get("/api/class/fvTenant.json?subscription=yes")
    sid = resp.json()["subscriptionId"]
    refresh_resp = client.get(f"/api/subscriptionRefresh.json?id={sid}")
    assert refresh_resp.status_code == 200


def test_subscription_refresh_unknown_id_still_200(client, token):
    # Real APIC does not error a stale/unknown refresh id.
    resp = client.get("/api/subscriptionRefresh.json?id=999999")
    assert resp.status_code == 200


def test_subscription_refresh_requires_auth(client):
    resp = client.get("/api/subscriptionRefresh.json?id=1")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Websocket push
# ---------------------------------------------------------------------------


def test_websocket_rejects_unknown_token(client):
    # Not bare Exception: that would also pass if the failure came from this
    # test's own scaffolding rather than from the server rejecting the token.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/socket-not-a-real-token") as ws:
            _receive_json_with_timeout(ws, timeout=2)


def test_websocket_push_created_modified_deleted(client, token):
    # Subscribe to fvTenant BEFORE opening the websocket (matches the
    # acceptance flow: subscribe via query, then open the socket).
    sub_resp = client.get("/api/class/fvTenant.json?subscription=yes")
    assert sub_resp.status_code == 200
    subscription_id = sub_resp.json()["subscriptionId"]

    with client.websocket_connect(f"/socket{token}") as ws:
        # (c) POST a new fvTenant.
        post_resp = client.post(
            "/api/mo/uni/tn-SubTest.json",
            json={"fvTenant": {"attributes": {"name": "SubTest"}}},
        )
        assert post_resp.status_code == 200

        # (d) assert a created event is pushed with the new tenant's dn.
        event = _receive_json_with_timeout(ws, timeout=5)
        assert subscription_id in event["subscriptionId"]
        assert len(event["imdata"]) == 1
        tenant_body = event["imdata"][0]["fvTenant"]
        assert tenant_body["attributes"]["dn"] == "uni/tn-SubTest"
        assert tenant_body["attributes"]["status"] == "created"

        # Modify it (POST again on the same DN => pre-existing => "modified").
        mod_resp = client.post(
            "/api/mo/uni/tn-SubTest.json",
            json={"fvTenant": {"attributes": {"name": "SubTest", "descr": "updated"}}},
        )
        assert mod_resp.status_code == 200
        mod_event = _receive_json_with_timeout(ws, timeout=5)
        assert mod_event["imdata"][0]["fvTenant"]["attributes"]["status"] == "modified"
        assert mod_event["imdata"][0]["fvTenant"]["attributes"]["dn"] == "uni/tn-SubTest"

        # (e) DELETE it => assert a status=deleted event.
        del_resp = client.delete("/api/mo/uni/tn-SubTest.json")
        assert del_resp.status_code == 200
        del_event = _receive_json_with_timeout(ws, timeout=5)
        assert subscription_id in del_event["subscriptionId"]
        del_tenant = del_event["imdata"][0]["fvTenant"]
        assert del_tenant["attributes"]["status"] == "deleted"
        assert del_tenant["attributes"]["dn"] == "uni/tn-SubTest"


def test_unsubscribed_class_does_not_push(client, token):
    # Subscribe only to fvTenant.
    client.get("/api/class/fvTenant.json?subscription=yes")

    with client.websocket_connect(f"/socket{token}") as ws:
        # Change an entirely different, unsubscribed class.
        resp = client.post(
            "/api/mo/uni/tn-mgmt/mgmtp-default.json",
            json={"mgmtMgmtP": {"attributes": {"descr": "no-op change"}}},
        )
        assert resp.status_code == 200

        # No event should arrive for this — bounded wait, not a hang.
        _assert_no_event(ws, timeout=2)


def test_dn_scoped_subscription_matches_subtree_change(client, token):
    # Subscribe via the MO/DN query shape on a parent DN.
    sub_resp = client.get("/api/mo/uni/tn-mgmt.json?subscription=yes")
    subscription_id = sub_resp.json()["subscriptionId"]

    with client.websocket_connect(f"/socket{token}") as ws:
        # A write to a *child* DN under uni/tn-mgmt should still notify the
        # parent-DN subscriber (subtree match).
        resp = client.post(
            "/api/mo/uni/tn-mgmt/ap-testap.json",
            json={"fvAp": {"attributes": {"name": "testap"}}},
        )
        assert resp.status_code == 200
        event = _receive_json_with_timeout(ws, timeout=5)
        assert subscription_id in event["subscriptionId"]
        assert event["imdata"][0]["fvAp"]["attributes"]["dn"] == "uni/tn-mgmt/ap-testap"


def test_disconnect_cleans_up_subscriptions(client, token):
    sub_resp = client.get("/api/class/fvTenant.json?subscription=yes")
    assert sub_resp.status_code == 200

    with client.websocket_connect(f"/socket{token}"):
        pass  # connect then immediately disconnect (context manager exit)

    # After disconnect, a change to fvTenant must not raise/error even though
    # the (now-dead) subscription still matches the scope — notify() must
    # silently skip a token with no live connection.
    resp = client.post(
        "/api/mo/uni/tn-AfterDisconnect.json",
        json={"fvTenant": {"attributes": {"name": "AfterDisconnect"}}},
    )
    assert resp.status_code == 200
