"""F5 — NDO DELETE endpoints: schemas and tenants.

`cisco.mso.mso_schema` / `cisco.mso.mso_tenant` with `state=absent` send
`DELETE /mso/api/v1/schemas/{id}` / `DELETE /mso/api/v1/tenants/{id}`;
before this fix neither route existed (observed 405), blocking E2E
baseline cleanup.

Semantics under test (aligned with real NDO 4.x):
- DELETE schema: 200 + gone from GET /schemas/{id} (404), the full list
  and list-identity; unknown id -> 404.
- DELETE tenant: refused with 400 while any schema template's `tenantId`
  still references it; 200 once the referencing schemas are deleted;
  unknown id -> 404. Bare `/api/v1/tenants/{id}` alias works too
  (same dual-shape pair as GET /tenants).
- Deleting a schema does NOT cascade into the APIC deploy mirror — real
  NDO leaves already-deployed objects orphaned on the sites' APICs.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from aci_sim.mit.store import MITStore
from aci_sim.ndo.app import make_ndo_app
from aci_sim.ndo.model import NdoState

TENANT_ID = "tenant-ms-tn1-id"
TENANT_NAME = "ANS-MS_TN1"
SCHEMA_ID = "schema-del-1"
SCHEMA_NAME = "MS-TN1-LAB1"
TEMPLATE_NAME = "MS-TN1-template"


@dataclass
class _FakeApicState:
    """Minimal stand-in for rest_aci.app.ApicSiteState — the deploy mirror
    only ever touches `.store` (same shim as tests/test_deploy_mirror.py)."""

    store: MITStore = field(default_factory=MITStore)


def _build_state() -> NdoState:
    """One tenant, one schema whose single template references the tenant,
    associated with site '1' so the deploy mirror has a target."""
    template = {
        "name": TEMPLATE_NAME,
        "tenantId": TENANT_ID,
        "vrfs": [{"name": "VRF1"}],
        "bds": [],
        "anps": [],
        "contracts": [],
    }
    detail = {
        "id": SCHEMA_ID,
        "displayName": SCHEMA_NAME,
        "name": SCHEMA_NAME,
        "templates": [template],
        "sites": [
            {"siteId": "1", "templateName": TEMPLATE_NAME, "bds": [], "anps": []}
        ],
    }
    summary = {
        "id": SCHEMA_ID,
        "displayName": SCHEMA_NAME,
        "name": SCHEMA_NAME,
        "templates": [{"name": TEMPLATE_NAME}],
    }
    return NdoState(
        sites=[],
        tenants=[{"id": TENANT_ID, "name": TENANT_NAME, "displayName": TENANT_NAME}],
        schemas=[summary],
        schema_details={SCHEMA_ID: detail},
        template_summaries=[],
        tenant_policy_templates={},
        fabric_connectivity={},
        policy_states={SCHEMA_ID: [{"status": "synced", "drift": False}]},
        audit_records=[],
    )


@pytest.fixture()
def state():
    return _build_state()


@pytest.fixture()
def client(state):
    app = make_ndo_app(state)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# DELETE /mso/api/v1/schemas/{id}
# ---------------------------------------------------------------------------


def test_delete_schema_removes_from_all_read_paths(client):
    assert client.get(f"/mso/api/v1/schemas/{SCHEMA_ID}").status_code == 200

    resp = client.delete(f"/mso/api/v1/schemas/{SCHEMA_ID}")
    assert resp.status_code == 200

    # detail GET now 404s with the standard not-found body
    resp = client.get(f"/mso/api/v1/schemas/{SCHEMA_ID}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == f"Schema '{SCHEMA_ID}' not found"

    # gone from the full list and from list-identity
    full = client.get("/mso/api/v1/schemas").json()["schemas"]
    assert all(s["id"] != SCHEMA_ID for s in full)
    identity = client.get("/mso/api/v1/schemas/list-identity").json()["schemas"]
    assert all(s["id"] != SCHEMA_ID for s in identity)


def test_delete_schema_unknown_404(client):
    resp = client.delete("/mso/api/v1/schemas/no-such-schema")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Schema 'no-such-schema' not found"


def test_delete_runtime_posted_schema(client):
    """A schema POSTed at runtime lands in `state.extra_schemas` — DELETE
    must remove it from that list too (not just the seeded one)."""
    resp = client.post(
        "/mso/api/v1/schemas",
        json={"displayName": "RUNTIME-SCHEMA", "templates": [], "sites": []},
    )
    assert resp.status_code == 200
    new_id = resp.json()["id"]

    assert client.delete(f"/mso/api/v1/schemas/{new_id}").status_code == 200
    assert client.get(f"/mso/api/v1/schemas/{new_id}").status_code == 404
    identity = client.get("/mso/api/v1/schemas/list-identity").json()["schemas"]
    assert all(s["id"] != new_id for s in identity)


def test_delete_schema_does_not_undeploy_mirrored_objects(state):
    """Real-NDO fidelity: deleting a schema does NOT undeploy — objects the
    deploy mirror already materialized on a site's APIC stay orphaned."""
    apic_states = {"1": _FakeApicState()}
    app = make_ndo_app(state, apic_states)
    with TestClient(app) as client:
        resp = client.post(
            "/mso/api/v1/task",
            json={"schemaId": SCHEMA_ID, "templateName": TEMPLATE_NAME},
        )
        assert resp.status_code == 200

        store = apic_states["1"].store
        ctx_dn = f"uni/tn-{TENANT_NAME}/ctx-VRF1"
        assert store.get(ctx_dn) is not None
        assert store.get(f"uni/tn-{TENANT_NAME}") is not None

        assert client.delete(f"/mso/api/v1/schemas/{SCHEMA_ID}").status_code == 200

        # schema is gone from NDO ...
        assert client.get(f"/mso/api/v1/schemas/{SCHEMA_ID}").status_code == 404
        # ... but the mirrored APIC objects survive (orphaned, like real gear)
        assert store.get(ctx_dn) is not None
        assert store.get(f"uni/tn-{TENANT_NAME}") is not None


# ---------------------------------------------------------------------------
# DELETE /mso/api/v1/tenants/{id} (+ bare /api/v1 alias)
# ---------------------------------------------------------------------------


def test_delete_tenant_referenced_by_schema_400(client):
    resp = client.delete(f"/mso/api/v1/tenants/{TENANT_ID}")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "referenced by schema(s)" in detail
    assert SCHEMA_NAME in detail
    # tenant must still be there
    tenants = client.get("/mso/api/v1/tenants").json()["tenants"]
    assert any(t["id"] == TENANT_ID for t in tenants)


def test_delete_tenant_after_schema_removed_200(client):
    """解除引用后删 — delete the referencing schema first, then the tenant."""
    assert client.delete(f"/mso/api/v1/schemas/{SCHEMA_ID}").status_code == 200

    resp = client.delete(f"/mso/api/v1/tenants/{TENANT_ID}")
    assert resp.status_code == 200

    tenants = client.get("/mso/api/v1/tenants").json()["tenants"]
    assert all(t["id"] != TENANT_ID for t in tenants)
    # bare-path enumeration reflects the same store
    tenants_bare = client.get("/api/v1/tenants").json()["tenants"]
    assert all(t["id"] != TENANT_ID for t in tenants_bare)


def test_delete_tenant_unknown_404(client):
    resp = client.delete("/mso/api/v1/tenants/no-such-tenant")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Tenant 'no-such-tenant' not found"


def test_delete_tenant_bare_path_alias(client):
    """`delegate_to: localhost` cisco.mso tasks hit the un-prefixed
    /api/v1 shape — same handler, same guard + delete semantics."""
    # unreferenced tenant created at runtime
    resp = client.post(
        "/mso/api/v1/tenants",
        json={"name": "SCRATCH-TN", "displayName": "SCRATCH-TN"},
    )
    assert resp.status_code == 200
    new_id = resp.json()["id"]

    # referenced tenant is refused on the bare path too
    assert client.delete(f"/api/v1/tenants/{TENANT_ID}").status_code == 400

    assert client.delete(f"/api/v1/tenants/{new_id}").status_code == 200
    tenants = client.get("/mso/api/v1/tenants").json()["tenants"]
    assert all(t["id"] != new_id for t in tenants)
    # second delete -> 404 (it's really gone)
    assert client.delete(f"/api/v1/tenants/{new_id}").status_code == 404
