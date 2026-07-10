"""F5b — NDO tenant-delete in-use guard: tenant policy templates.

F5 (`ffaf996`, v0.22.0) added the tenant-delete in-use guard, but it only
scanned SCHEMA templates' `tenantId` for references to the tenant being
deleted (`state.schema_details[*].templates[*].tenantId`). It never
scanned tenant policy templates
(`state.tenant_policy_templates[*].tenantPolicyTemplate.template.
tenantId` — confirmed as the real field path against the sim's own
`_get_template_objects`/`_backfill_policy_uuids` traversal of the same
structure in aci_sim/ndo/app.py). So a tenant referenced ONLY by a tenant
policy template deleted cleanly — fails-open. Real NDO refuses to delete
an in-use tenant regardless of which template type holds the reference;
this fixes the gap so the same 400 in-use rejection applies.

Semantics under test:
- Tenant referenced ONLY by a tenant policy template -> 400 (the fix).
- Tenant referenced by nothing -> still 200 (guard doesn't over-reject).
- Tenant referenced by a schema template -> still 400 (F5 guard intact).
- Referencing policy template deleted, then tenant delete -> 200 (guard
  releases once dereferenced).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aci_sim.ndo.app import make_ndo_app
from aci_sim.ndo.model import NdoState

POLICY_TENANT_ID = "tenant-f5b-policy-1"
POLICY_TENANT_NAME = "ANS-POLICY-TN1"
POLICY_TEMPLATE_ID = "template-f5b-policy-1"
POLICY_TEMPLATE_NAME = "POLICY-TN1-template"

SCHEMA_TENANT_ID = "tenant-f5b-schema-1"
SCHEMA_TENANT_NAME = "ANS-SCHEMA-TN1"
SCHEMA_ID = "schema-f5b-1"
SCHEMA_NAME = "F5B-SCHEMA-TN1-LAB1"
SCHEMA_TEMPLATE_NAME = "F5B-SCHEMA-TN1-template"

SCRATCH_TENANT_ID = "tenant-f5b-scratch-1"
SCRATCH_TENANT_NAME = "ANS-SCRATCH-TN1"


def _build_state() -> NdoState:
    """Three tenants, exercising the three reference sources independently:
    - POLICY_TENANT_ID: referenced ONLY by a tenant policy template.
    - SCHEMA_TENANT_ID: referenced ONLY by a schema template (F5 baseline).
    - SCRATCH_TENANT_ID: referenced by nothing.
    """
    policy_doc = {
        "templateId": POLICY_TEMPLATE_ID,
        "displayName": POLICY_TEMPLATE_NAME,
        "templateType": "tenantPolicy",
        "tenantPolicyTemplate": {
            "template": {
                "tenantId": POLICY_TENANT_ID,
                "dhcpRelayPolicies": [],
                "dhcpOptionPolicies": [],
            },
            "sites": [],
        },
    }
    schema_template = {
        "name": SCHEMA_TEMPLATE_NAME,
        "tenantId": SCHEMA_TENANT_ID,
        "vrfs": [],
        "bds": [],
        "anps": [],
        "contracts": [],
    }
    schema_detail = {
        "id": SCHEMA_ID,
        "displayName": SCHEMA_NAME,
        "name": SCHEMA_NAME,
        "templates": [schema_template],
        "sites": [],
    }
    schema_summary = {
        "id": SCHEMA_ID,
        "displayName": SCHEMA_NAME,
        "name": SCHEMA_NAME,
        "templates": [{"name": SCHEMA_TEMPLATE_NAME}],
    }
    return NdoState(
        sites=[],
        tenants=[
            {
                "id": POLICY_TENANT_ID,
                "name": POLICY_TENANT_NAME,
                "displayName": POLICY_TENANT_NAME,
            },
            {
                "id": SCHEMA_TENANT_ID,
                "name": SCHEMA_TENANT_NAME,
                "displayName": SCHEMA_TENANT_NAME,
            },
            {
                "id": SCRATCH_TENANT_ID,
                "name": SCRATCH_TENANT_NAME,
                "displayName": SCRATCH_TENANT_NAME,
            },
        ],
        schemas=[schema_summary],
        schema_details={SCHEMA_ID: schema_detail},
        template_summaries=[],
        tenant_policy_templates={POLICY_TEMPLATE_ID: policy_doc},
        fabric_connectivity={},
        policy_states={},
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


def test_delete_tenant_referenced_only_by_policy_template_400(client):
    """The fix: a tenant referenced ONLY by a tenant policy template's
    `tenantPolicyTemplate.template.tenantId` must be refused, matching the
    F5 schema-template guard's semantics (previously fails-open)."""
    resp = client.delete(f"/mso/api/v1/tenants/{POLICY_TENANT_ID}")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "tenant policy template(s)" in detail
    assert POLICY_TEMPLATE_NAME in detail

    tenants = client.get("/mso/api/v1/tenants").json()["tenants"]
    assert any(t["id"] == POLICY_TENANT_ID for t in tenants)


def test_delete_tenant_no_references_200(client):
    """Guard must not over-reject: a tenant referenced by neither a schema
    template nor a tenant policy template deletes cleanly."""
    resp = client.delete(f"/mso/api/v1/tenants/{SCRATCH_TENANT_ID}")
    assert resp.status_code == 200

    tenants = client.get("/mso/api/v1/tenants").json()["tenants"]
    assert all(t["id"] != SCRATCH_TENANT_ID for t in tenants)


def test_delete_tenant_referenced_by_schema_still_400(client):
    """F5 guard intact: schema-template reference still refuses deletion."""
    resp = client.delete(f"/mso/api/v1/tenants/{SCHEMA_TENANT_ID}")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "schema(s)" in detail
    assert SCHEMA_NAME in detail

    tenants = client.get("/mso/api/v1/tenants").json()["tenants"]
    assert any(t["id"] == SCHEMA_TENANT_ID for t in tenants)


def test_delete_tenant_after_policy_template_removed_200(client):
    """Guard releases once dereferenced: delete the referencing tenant
    policy template first, then the tenant delete succeeds."""
    assert (
        client.delete(f"/mso/api/v1/templates/{POLICY_TEMPLATE_ID}").status_code
        == 200
    )

    resp = client.delete(f"/mso/api/v1/tenants/{POLICY_TENANT_ID}")
    assert resp.status_code == 200

    tenants = client.get("/mso/api/v1/tenants").json()["tenants"]
    assert all(t["id"] != POLICY_TENANT_ID for t in tenants)
