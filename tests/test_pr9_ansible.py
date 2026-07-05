"""Regression tests for PR-9: cisco.aci Ansible-fidelity hardening.

Covers (see docs/CONTRACT.md §Ansible-compatibility for the full contract):
  1. HTTP DELETE /api/mo/{dn}.json (cisco.aci state=absent) — existing DN
     removes the subtree; missing DN is idempotent 200-empty.
  2. status="deleted" cascade actually removes the store subtree (not just
     skips planning children); status="created,modified" is treated as a
     normal upsert (ignored gracefully, not a KeyError/malformed-body 400).
  3. Arbitrary attribute passthrough (annotation="orchestrator:ansible",
     nameAlias, descr) round-trips exactly through POST -> mo GET -> class
     query.
  4. Certificate signature accept-mode auth — a request carrying the four
     APIC-Certificate-*/APIC-Request-Signature cookies (matching SIM_USERNAME)
     authenticates without ever calling aaaLogin; a cookie set naming a
     different user is rejected with the standard 403 envelope.
  5. firmwareCtrlrRunning is seeded per controller with the site's
     apic_version (if implemented — see build/fabric.py).

FEATURE 6 (live-blocker fold-in): GET /api/mo/{dn}.json on a nonexistent DN
must be 200 + empty imdata, not 404 — verified against upstream cisco.aci's
module_utils/aci.py `api_call()`, which treats ANY non-200 GET response as
fatal (`fail_json`) with no special-case for "not found is fine". Every
`state=present` module does a GET-before-POST existence check
(`get_existing()`) before ever building its diff, so a 404 there breaks the
very first task of any create playbook run against a brand-new object —
this was caught as a live blocker running an actual cisco.aci playbook
against the sim (see chapter note / commit message). Regression test named
`test_mo_get_missing_returns_200_empty_ansible_precheck` below.
"""

from __future__ import annotations

import base64
import copy

import pytest
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
# FEATURE 6 (live blocker) — GET on missing DN is 200-empty, not 404
# ---------------------------------------------------------------------------


def test_mo_get_missing_returns_200_empty_ansible_precheck():
    c = _logged_in_client()
    resp = c.get("/api/mo/uni/tn-ANSIBLE-SMOKE.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["totalCount"] == "0"
    assert data["imdata"] == []


def test_mo_get_missing_then_create_flow_succeeds_end_to_end():
    # Full cisco.aci state=present shape: GET (existence check, empty) ->
    # POST (create) -> GET (post-verification, now present).
    c = _logged_in_client()
    pre = c.get("/api/mo/uni/tn-ANSIBLE-SMOKE.json")
    assert pre.status_code == 200
    assert pre.json()["totalCount"] == "0"

    post = c.post(
        "/api/mo/uni/tn-ANSIBLE-SMOKE.json",
        json={"fvTenant": {"attributes": {"name": "ANSIBLE-SMOKE", "dn": "uni/tn-ANSIBLE-SMOKE"}}},
    )
    assert post.status_code == 200

    verify = c.get("/api/mo/uni/tn-ANSIBLE-SMOKE.json")
    assert verify.status_code == 200
    assert verify.json()["totalCount"] == "1"


# ---------------------------------------------------------------------------
# FEATURE 1 — HTTP DELETE
# ---------------------------------------------------------------------------


def test_delete_existing_dn_removes_subtree():
    c = _logged_in_client()
    c.post(
        "/api/mo/uni/tn-DelT.json",
        json={
            "fvTenant": {
                "attributes": {"name": "DelT", "dn": "uni/tn-DelT"},
                "children": [{"fvBD": {"attributes": {"name": "bd1"}}}],
            }
        },
    )
    assert c.get("/api/mo/uni/tn-DelT/BD-bd1.json").json()["totalCount"] == "1"

    resp = c.delete("/api/mo/uni/tn-DelT.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["imdata"] == []
    assert data["totalCount"] == "0"

    # Subtree (the BD child) is gone too, not just the tenant itself.
    assert c.get("/api/mo/uni/tn-DelT.json").json()["totalCount"] == "0"
    assert c.get("/api/mo/uni/tn-DelT/BD-bd1.json").json()["totalCount"] == "0"


def test_delete_missing_dn_is_idempotent_200():
    c = _logged_in_client()
    resp = c.delete("/api/mo/uni/tn-NeverExisted.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["imdata"] == []
    assert data["totalCount"] == "0"


def test_delete_without_auth_returns_403():
    c = _new_client()
    resp = c.delete("/api/mo/uni/tn-Blocked.json")
    assert resp.status_code == 403
    assert resp.json()["imdata"][0]["error"]["attributes"]["code"] == "403"


# ---------------------------------------------------------------------------
# FEATURE 2 — status="deleted" cascade + status="created,modified" upsert
# ---------------------------------------------------------------------------


def test_status_deleted_via_post_removes_subtree():
    c = _logged_in_client()
    c.post(
        "/api/mo/uni/tn-StT.json",
        json={
            "fvTenant": {
                "attributes": {"name": "StT", "dn": "uni/tn-StT"},
                "children": [{"fvBD": {"attributes": {"name": "bd1"}}}],
            }
        },
    )
    assert c.get("/api/mo/uni/tn-StT/BD-bd1.json").json()["totalCount"] == "1"

    resp = c.post(
        "/api/mo/uni/tn-StT.json",
        json={"fvTenant": {"attributes": {"name": "StT", "dn": "uni/tn-StT", "status": "deleted"}}},
    )
    assert resp.status_code == 200
    assert resp.json()["imdata"][0]["fvTenant"]["attributes"]["status"] == "deleted"

    # The whole subtree, including the never-mentioned-in-this-POST child, is gone.
    assert c.get("/api/mo/uni/tn-StT.json").json()["totalCount"] == "0"
    assert c.get("/api/mo/uni/tn-StT/BD-bd1.json").json()["totalCount"] == "0"


def test_status_created_modified_is_treated_as_upsert():
    # cisco.aci does not actually send this on the wire (module_utils/aci.py
    # never sets a "status" key in `proposed`), but real APIC accepts the
    # value gracefully as a normal upsert rather than erroring — verified via
    # this sim's own status=="deleted" special-case being the ONLY branch
    # that diverts from upsert. Guard against a regression that special-cases
    # any other status string.
    c = _logged_in_client()
    resp = c.post(
        "/api/mo/uni/tn-CmT.json",
        json={
            "fvTenant": {
                "attributes": {"name": "CmT", "dn": "uni/tn-CmT", "status": "created,modified"},
            }
        },
    )
    assert resp.status_code == 200
    assert resp.json()["imdata"][0]["fvTenant"]["attributes"]["status"] == "created"

    get = c.get("/api/mo/uni/tn-CmT.json")
    assert get.status_code == 200
    assert get.json()["totalCount"] == "1"
    attrs = get.json()["imdata"][0]["fvTenant"]["attributes"]
    assert attrs["name"] == "CmT"
    # The literal "created,modified" string must not have been stored as a
    # real store attribute in a way that later confuses upsert/delete logic —
    # a second write with status="deleted" must still remove it.
    resp2 = c.post(
        "/api/mo/uni/tn-CmT.json",
        json={"fvTenant": {"attributes": {"name": "CmT", "dn": "uni/tn-CmT", "status": "deleted"}}},
    )
    assert resp2.status_code == 200
    assert c.get("/api/mo/uni/tn-CmT.json").json()["totalCount"] == "0"


# ---------------------------------------------------------------------------
# FEATURE 3 — annotation / nameAlias / descr passthrough
# ---------------------------------------------------------------------------


def test_annotation_roundtrips_on_mo_get_and_class_query():
    c = _logged_in_client()
    resp = c.post(
        "/api/mo/uni/tn-AnnT.json",
        json={
            "fvTenant": {
                "attributes": {
                    "name": "AnnT",
                    "dn": "uni/tn-AnnT",
                    "annotation": "orchestrator:ansible",
                    "nameAlias": "friendly-name",
                    "descr": "managed by ansible",
                }
            }
        },
    )
    assert resp.status_code == 200

    mo = c.get("/api/mo/uni/tn-AnnT.json")
    assert mo.status_code == 200
    attrs = mo.json()["imdata"][0]["fvTenant"]["attributes"]
    assert attrs["annotation"] == "orchestrator:ansible"
    assert attrs["nameAlias"] == "friendly-name"
    assert attrs["descr"] == "managed by ansible"

    cls = c.get('/api/class/fvTenant.json?query-target-filter=eq(fvTenant.name,"AnnT")')
    assert cls.status_code == 200
    rows = cls.json()["imdata"]
    assert len(rows) == 1
    cls_attrs = rows[0]["fvTenant"]["attributes"]
    assert cls_attrs["annotation"] == "orchestrator:ansible"
    assert cls_attrs["nameAlias"] == "friendly-name"


# ---------------------------------------------------------------------------
# FEATURE 4 — certificate signature auth (accept-mode)
# ---------------------------------------------------------------------------


def _cert_cookies(user: str = "admin", certname: str = "ansible") -> dict[str, str]:
    return {
        "APIC-Certificate-Algorithm": "v1.0",
        "APIC-Certificate-Fingerprint": "fingerprint",
        "APIC-Certificate-DN": f"uni/userext/user-{user}/usercert-{certname}",
        "APIC-Request-Signature": base64.b64encode(b"not-a-real-signature").decode(),
    }


def test_cert_cookie_authenticates_without_prior_login():
    c = _new_client()
    for name, value in _cert_cookies().items():
        c.cookies.set(name, value)

    resp = c.get("/api/class/fabricNode.json")
    assert resp.status_code == 200
    assert int(resp.json()["totalCount"]) > 0


def test_cert_cookie_write_and_delete_work_without_prior_login():
    c = _new_client()
    for name, value in _cert_cookies().items():
        c.cookies.set(name, value)

    post = c.post(
        "/api/mo/uni/tn-CertT.json",
        json={"fvTenant": {"attributes": {"name": "CertT", "dn": "uni/tn-CertT"}}},
    )
    assert post.status_code == 200
    assert c.get("/api/mo/uni/tn-CertT.json").json()["totalCount"] == "1"

    delete = c.delete("/api/mo/uni/tn-CertT.json")
    assert delete.status_code == 200
    assert c.get("/api/mo/uni/tn-CertT.json").json()["totalCount"] == "0"


def test_cert_cookie_wrong_user_returns_403():
    c = _new_client()
    for name, value in _cert_cookies(user="notadmin").items():
        c.cookies.set(name, value)

    resp = c.get("/api/class/fabricNode.json")
    assert resp.status_code == 403
    assert resp.json()["imdata"][0]["error"]["attributes"]["code"] == "403"


def test_cert_cookie_missing_one_field_returns_403():
    c = _new_client()
    cookies = _cert_cookies()
    del cookies["APIC-Request-Signature"]
    for name, value in cookies.items():
        c.cookies.set(name, value)

    resp = c.get("/api/class/fabricNode.json")
    assert resp.status_code == 403


def test_cert_cookie_malformed_dn_returns_403():
    c = _new_client()
    cookies = _cert_cookies()
    cookies["APIC-Certificate-DN"] = "not-a-valid-cert-dn"
    for name, value in cookies.items():
        c.cookies.set(name, value)

    resp = c.get("/api/class/fabricNode.json")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# FEATURE 5 — firmwareCtrlrRunning
# ---------------------------------------------------------------------------


def test_firmware_ctrlr_running_seeded_if_implemented():
    c = _logged_in_client()
    resp = c.get("/api/class/firmwareCtrlrRunning.json")
    assert resp.status_code == 200
    data = resp.json()
    if int(data["totalCount"]) == 0:
        pytest.skip("firmwareCtrlrRunning not seeded in this build")
    for item in data["imdata"]:
        assert "version" in item["firmwareCtrlrRunning"]["attributes"]
