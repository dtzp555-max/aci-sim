"""Regression tests for F8a: real created/modified/unchanged write-status.

Before F8a, ``writes.py::apply()`` hard-coded ``status="created"`` on every
non-delete write with no comparison against stored state.
``cisco.aci.aci_rest`` derives its ``changed`` result from a recursive scan
of the response ``imdata`` for any ``status`` in
{created,modified,deleted} at any depth — so every aci_rest-driven write
reported ``changed=true`` forever, breaking pass2 idempotency.

F8a computes a real per-MO status at the write choke point
(``writes._upsert_recursive``) by diffing the caller's RAW posted attrs
(excluding "dn"/"status") against the pre-existing stored MO, and
``apply()`` now builds ``imdata`` from ONLY the changed MOs — empty
imdata (+ totalCount "0") when nothing changed.

These tests assert on the HTTP response shape the same way
``tests/test_pr9_ansible.py`` and ``tests/test_subscriptions.py`` already
do (no cisco.aci module is available in this environment to call its
``changed()`` directly) — an empty ``imdata`` is the shape that makes
``aci_rest``'s recursive status-scan return False (see
``_F8A_CHANGED_STATUS_DESIGN.md`` §2.2), and a non-empty ``imdata`` with a
``status`` in {created,modified,deleted} anywhere is the shape that makes
it return True.
"""

from __future__ import annotations

import copy

from fastapi.testclient import TestClient

from aci_sim.build.orchestrator import build_site
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology

TOPO_PATH = "topology.yaml"


def _new_client() -> TestClient:
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


def _logged_in_client() -> TestClient:
    c = _new_client()
    resp = c.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    assert resp.status_code == 200
    return c


# ---------------------------------------------------------------------------
# 1. Byte-identical re-push -> empty imdata (the core idempotency fix)
# ---------------------------------------------------------------------------


def test_repush_identical_mo_is_unchanged():
    c = _logged_in_client()
    body = {
        "fvTenant": {
            "attributes": {"name": "F8AProbe1", "dn": "uni/tn-F8AProbe1", "descr": "probe"},
        }
    }
    first = c.post("/api/mo/uni/tn-F8AProbe1.json?rsp-subtree=modified", json=body)
    assert first.status_code == 200
    assert first.json()["imdata"][0]["fvTenant"]["attributes"]["status"] == "created"

    second = c.post("/api/mo/uni/tn-F8AProbe1.json?rsp-subtree=modified", json=body)
    assert second.status_code == 200
    data = second.json()
    assert data["imdata"] == []
    assert data["totalCount"] == "0"


# ---------------------------------------------------------------------------
# 2. Re-push with one attr changed -> status=="modified"
# ---------------------------------------------------------------------------


def test_repush_with_changed_attr_reports_modified():
    c = _logged_in_client()
    c.post(
        "/api/mo/uni/tn-F8AProbe2.json?rsp-subtree=modified",
        json={"fvTenant": {"attributes": {"name": "F8AProbe2", "dn": "uni/tn-F8AProbe2", "descr": "before"}}},
    )
    resp = c.post(
        "/api/mo/uni/tn-F8AProbe2.json?rsp-subtree=modified",
        json={"fvTenant": {"attributes": {"name": "F8AProbe2", "dn": "uni/tn-F8AProbe2", "descr": "after"}}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["totalCount"] == "1"
    entry = data["imdata"][0]["fvTenant"]["attributes"]
    assert entry["status"] == "modified"
    assert entry["dn"] == "uni/tn-F8AProbe2"


# ---------------------------------------------------------------------------
# 3. First create -> status=="created" (guard against over-eager unchanged)
# ---------------------------------------------------------------------------


def test_first_create_reports_created():
    c = _logged_in_client()
    resp = c.post(
        "/api/mo/uni/tn-F8AProbe3.json?rsp-subtree=modified",
        json={"fvTenant": {"attributes": {"name": "F8AProbe3", "dn": "uni/tn-F8AProbe3"}}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["totalCount"] == "1"
    assert data["imdata"][0]["fvTenant"]["attributes"]["status"] == "created"


# ---------------------------------------------------------------------------
# 4. Partial re-push (subset of original attrs, same values) -> unchanged
# ---------------------------------------------------------------------------


def test_partial_repush_same_values_is_unchanged():
    c = _logged_in_client()
    c.post(
        "/api/mo/uni/tn-F8AProbe4.json?rsp-subtree=modified",
        json={
            "fvTenant": {
                "attributes": {"name": "F8AProbe4", "dn": "uni/tn-F8AProbe4", "descr": "kept-in-store"}
            }
        },
    )
    # Re-push omits "descr" entirely (subset of the original posted attrs),
    # but "name" (the only key still posted, besides dn) has the same value.
    # The stored "descr" (never re-posted) must NOT be read/diffed here —
    # only posted keys are compared.
    resp = c.post(
        "/api/mo/uni/tn-F8AProbe4.json?rsp-subtree=modified",
        json={"fvTenant": {"attributes": {"name": "F8AProbe4", "dn": "uni/tn-F8AProbe4"}}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["imdata"] == []
    assert data["totalCount"] == "0"
    # The store still has the original descr (upsert never removes keys).
    get = c.get("/api/mo/uni/tn-F8AProbe4.json")
    assert get.json()["imdata"][0]["fvTenant"]["attributes"]["descr"] == "kept-in-store"


# ---------------------------------------------------------------------------
# 5. THE critical defaults-overlay guard: create-then-repush of a class WITH
#    _CLASS_DEFAULTS (fvBD) must stay unchanged, not spuriously "modified"
#    because the diff would otherwise see create-time defaults (mac/mtu/...)
#    the caller never posted.
# ---------------------------------------------------------------------------


def test_repush_class_with_defaults_fvbd_is_unchanged():
    c = _logged_in_client()
    body = {
        "fvTenant": {
            "attributes": {"name": "F8AProbe5", "dn": "uni/tn-F8AProbe5"},
            "children": [{"fvBD": {"attributes": {"name": "bd1"}}}],
        }
    }
    first = c.post("/api/mo/uni/tn-F8AProbe5.json?rsp-subtree=modified", json=body)
    assert first.status_code == 200
    # Sanity: fvBD's create-time defaults (mac/mtu/...) were actually applied
    # to the store, so this test would catch a regression that silently
    # dropped _CLASS_DEFAULTS instead of merely excluding it from the diff.
    bd_get = c.get("/api/mo/uni/tn-F8AProbe5/BD-bd1.json")
    bd_attrs = bd_get.json()["imdata"][0]["fvBD"]["attributes"]
    assert bd_attrs["mac"] == "00:22:BD:F8:19:FF"
    assert bd_attrs["mtu"] == "9000"

    # Re-push the EXACT same body (fvTenant unchanged, fvBD posted with only
    # "name" — never mac/mtu, which only exist in the store as defaults).
    second = c.post("/api/mo/uni/tn-F8AProbe5.json?rsp-subtree=modified", json=body)
    assert second.status_code == 200
    data = second.json()
    assert data["imdata"] == []
    assert data["totalCount"] == "0"


# ---------------------------------------------------------------------------
# 6. Parent unchanged + child attr changed -> imdata contains the child,
#    labelled "modified"; the unchanged parent is NOT in imdata.
# ---------------------------------------------------------------------------


def test_child_modified_parent_unchanged_included_in_imdata():
    c = _logged_in_client()
    c.post(
        "/api/mo/uni/tn-F8AProbe6.json?rsp-subtree=modified",
        json={
            "fvTenant": {
                "attributes": {"name": "F8AProbe6", "dn": "uni/tn-F8AProbe6"},
                "children": [{"fvBD": {"attributes": {"name": "bd1", "descr": "before"}}}],
            }
        },
    )
    resp = c.post(
        "/api/mo/uni/tn-F8AProbe6.json?rsp-subtree=modified",
        json={
            "fvTenant": {
                "attributes": {"name": "F8AProbe6", "dn": "uni/tn-F8AProbe6"},
                "children": [{"fvBD": {"attributes": {"name": "bd1", "descr": "after"}}}],
            }
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["totalCount"] == "1"
    entry = data["imdata"][0]
    assert "fvBD" in entry
    assert entry["fvBD"]["attributes"]["status"] == "modified"
    assert entry["fvBD"]["attributes"]["dn"] == "uni/tn-F8AProbe6/BD-bd1"
    # The unchanged fvTenant parent must not appear anywhere in imdata.
    assert not any("fvTenant" in e for e in data["imdata"])


# ---------------------------------------------------------------------------
# 7. Delete still reports "deleted" (unchanged behavior)
# ---------------------------------------------------------------------------


def test_delete_still_reports_deleted():
    c = _logged_in_client()
    c.post(
        "/api/mo/uni/tn-F8AProbe7.json?rsp-subtree=modified",
        json={"fvTenant": {"attributes": {"name": "F8AProbe7", "dn": "uni/tn-F8AProbe7"}}},
    )
    resp = c.post(
        "/api/mo/uni/tn-F8AProbe7.json?rsp-subtree=modified",
        json={"fvTenant": {"attributes": {"name": "F8AProbe7", "dn": "uni/tn-F8AProbe7", "status": "deleted"}}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["totalCount"] == "1"
    assert data["imdata"][0]["fvTenant"]["attributes"]["status"] == "deleted"
    assert c.get("/api/mo/uni/tn-F8AProbe7.json").json()["totalCount"] == "0"


# ---------------------------------------------------------------------------
# 8. No-param path guard: ?rsp-subtree absent still returns "created" on
#    first create (this is also the path every existing pre-F8a test in
#    this repo already exercises — none of them append ?rsp-subtree).
# ---------------------------------------------------------------------------


def test_rsp_subtree_absent_first_create_still_created():
    c = _logged_in_client()
    resp = c.post(
        "/api/mo/uni/tn-F8AProbe8.json",
        json={"fvTenant": {"attributes": {"name": "F8AProbe8", "dn": "uni/tn-F8AProbe8"}}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["totalCount"] == "1"
    assert data["imdata"][0]["fvTenant"]["attributes"]["status"] == "created"


# ---------------------------------------------------------------------------
# 9. Delete of an already-absent DN -> unchanged (F1 fold-in; store pop is a
#    no-op, so real APIC reports idempotent no-change).
# ---------------------------------------------------------------------------


def test_delete_of_missing_dn_is_unchanged():
    c = _logged_in_client()
    resp = c.post(
        "/api/mo/uni/tn-F8ANeverExisted.json?rsp-subtree=modified",
        json={"fvTenant": {"attributes": {"name": "F8ANeverExisted", "dn": "uni/tn-F8ANeverExisted", "status": "deleted"}}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["imdata"] == []
    assert data["totalCount"] == "0"


def test_delete_of_existing_dn_reports_deleted():
    c = _logged_in_client()
    c.post(
        "/api/mo/uni/tn-F8AToDelete.json?rsp-subtree=modified",
        json={"fvTenant": {"attributes": {"name": "F8AToDelete", "dn": "uni/tn-F8AToDelete"}}},
    )
    resp = c.post(
        "/api/mo/uni/tn-F8AToDelete.json?rsp-subtree=modified",
        json={"fvTenant": {"attributes": {"name": "F8AToDelete", "dn": "uni/tn-F8AToDelete", "status": "deleted"}}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["totalCount"] == "1"
    assert data["imdata"][0]["fvTenant"]["attributes"]["status"] == "deleted"


# ---------------------------------------------------------------------------
# 10. status-pollution on RE-PUSH: posting status="created,modified" a second
#     time must NOT force a false "modified" (status excluded from the diff).
# ---------------------------------------------------------------------------


def test_repush_status_pollution_is_unchanged():
    c = _logged_in_client()
    body = {"fvTenant": {"attributes": {"name": "F8APoll", "dn": "uni/tn-F8APoll", "status": "created,modified", "descr": "x"}}}
    first = c.post("/api/mo/uni/tn-F8APoll.json?rsp-subtree=modified", json=body)
    assert first.status_code == 200
    assert first.json()["imdata"][0]["fvTenant"]["attributes"]["status"] == "created"
    second = c.post("/api/mo/uni/tn-F8APoll.json?rsp-subtree=modified", json=body)
    assert second.status_code == 200
    data = second.json()
    assert data["imdata"] == []
    assert data["totalCount"] == "0"


# ---------------------------------------------------------------------------
# 11. ?rsp-subtree=no -> empty imdata even on a genuine create (matches real
#     APIC, which echoes nothing when not asked for the subtree).
# ---------------------------------------------------------------------------


def test_rsp_subtree_no_returns_empty_on_create():
    c = _logged_in_client()
    resp = c.post(
        "/api/mo/uni/tn-F8ANoSubtree.json?rsp-subtree=no",
        json={"fvTenant": {"attributes": {"name": "F8ANoSubtree", "dn": "uni/tn-F8ANoSubtree"}}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["imdata"] == []
    assert data["totalCount"] == "0"
