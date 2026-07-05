"""Regression tests for PR-10: cisco.aci/cisco.mso E2E-run gap fixes.

Triaged from a live aci-ansible E2E run whose failures were checked against
real-gear logs (see docs/CONTRACT.md §7/§11 and CHANGELOG 0.2.1). Covers:

  1. GET/POST/DELETE via /api/node/mo/{dn} behave identically to the
     canonical /api/mo/{dn} — real APIC serves /api/node/mo/{dn}.json as a
     full alias (API Inspector / aci_rest form); the sim only registered
     /api/mo/*, so cisco.aci calls hitting the alias got FastAPI's default
     404 {"detail":"Not Found"} (11 E2E failures).
  2. An unmatched /api/* path returns an APIC-shaped 400 error envelope
     (imdata/error/code), never FastAPI's {"detail":...} shape.
  3. NDO /mso/api/v1/sites site names equal the topology's fabric names
     (e.g. "LAB1-IT-ACI"), not the short internal site name ("LAB1") —
     verified against a real-gear cisco.mso.mso_tenant run that only
     accepted the fabric-name form.
  4. GET /mso/api/v1/schemas/list-identity returns every seeded schema with
     id + displayName, and is NOT shadowed by the parameterized
     /mso/api/v1/schemas/{schema_id} route.
  5. GET /api/v1/tenants (cisco.mso.ndo_template's tenant prereq lookup)
     returns the NDO tenants list.
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from aci_sim.build.orchestrator import build_site
from aci_sim.ndo.app import make_ndo_app
from aci_sim.ndo.model import build_ndo_model
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology

TOPO_PATH = "topology.yaml"


# ---------------------------------------------------------------------------
# APIC fixtures
# ---------------------------------------------------------------------------


def _new_apic_client() -> TestClient:
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


def _logged_in_apic_client() -> TestClient:
    c = _new_apic_client()
    resp = c.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    assert resp.status_code == 200
    return c


# ---------------------------------------------------------------------------
# NDO fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def topo():
    return load_topology(TOPO_PATH)


@pytest.fixture(scope="module")
def ndo_client(topo):
    state = build_ndo_model(topo)
    app = make_ndo_app(state)
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def ndo_token(ndo_client):
    resp = ndo_client.post(
        "/api/v1/auth/login",
        json={"userName": "admin", "userPasswd": "cisco123", "domain": "local"},
    )
    assert resp.status_code == 200
    return resp.json()["token"]


# ---------------------------------------------------------------------------
# FIX A — /api/node/mo alias
# ---------------------------------------------------------------------------


def test_node_mo_alias_get_post_delete_round_trip():
    """POST/GET/DELETE via /api/node/mo/{dn} must behave identically to the
    canonical /api/mo/{dn} route — create via the node-alias, read back via
    the canonical path, then delete via the alias and confirm removal.
    """
    c = _logged_in_apic_client()

    # Create via the node-alias.
    post_resp = c.post(
        "/api/node/mo/uni/tn-PR10AliasTest.json",
        json={
            "fvTenant": {
                "attributes": {"name": "PR10AliasTest", "dn": "uni/tn-PR10AliasTest"}
            }
        },
    )
    assert post_resp.status_code == 200, post_resp.text
    assert post_resp.json()["imdata"][0]["fvTenant"]["attributes"]["status"] == "created"

    # Read back via the canonical path.
    get_resp = c.get("/api/mo/uni/tn-PR10AliasTest.json")
    assert get_resp.status_code == 200
    assert get_resp.json()["totalCount"] == "1"
    assert (
        get_resp.json()["imdata"][0]["fvTenant"]["attributes"]["name"]
        == "PR10AliasTest"
    )

    # Also readable via the alias itself.
    get_alias_resp = c.get("/api/node/mo/uni/tn-PR10AliasTest.json")
    assert get_alias_resp.status_code == 200
    assert get_alias_resp.json()["totalCount"] == "1"

    # Delete via the alias.
    del_resp = c.delete("/api/node/mo/uni/tn-PR10AliasTest.json")
    assert del_resp.status_code == 200
    assert del_resp.json()["totalCount"] == "0"

    # Confirm removal via the canonical path.
    confirm_resp = c.get("/api/mo/uni/tn-PR10AliasTest.json")
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["totalCount"] == "0"


def test_node_mo_alias_requires_auth():
    """The alias must be gated by the same session check as /api/mo/*."""
    c = _new_apic_client()
    resp = c.get("/api/node/mo/uni/tn-Whatever.json")
    assert resp.status_code == 403
    assert resp.json()["imdata"][0]["error"]["attributes"]["code"] == "403"


def test_node_mo_alias_does_not_shadow_node_class():
    """/api/node/mo/* and /api/node/class/* are distinct literal prefixes —
    the fabric-wide node/class form must still resolve correctly.
    """
    c = _logged_in_apic_client()
    resp = c.get("/api/node/class/fabricNode.json")
    assert resp.status_code == 200
    assert int(resp.json()["totalCount"]) > 0


# ---------------------------------------------------------------------------
# FIX A — unmatched /api/* fallback
# ---------------------------------------------------------------------------


def test_unmatched_api_path_returns_apic_shaped_400():
    c = _logged_in_apic_client()
    resp = c.get("/api/bogus/nonexistent/path")
    assert resp.status_code == 400
    body = resp.json()
    assert "detail" not in body
    assert body["imdata"][0]["error"]["attributes"]["code"] == "400"
    assert "Invalid request path" in body["imdata"][0]["error"]["attributes"]["text"]


def test_unmatched_non_api_path_unaffected():
    """Paths outside /api/* (e.g. control-plane) keep the default 404 shape —
    the fallback is scoped to the APIC REST contract only.
    """
    c = _logged_in_apic_client()
    resp = c.get("/_sim/does-not-exist")
    assert resp.status_code == 404
    assert "detail" in resp.json()


# ---------------------------------------------------------------------------
# FIX B — NDO site names == fabric names
# ---------------------------------------------------------------------------


def test_ndo_sites_use_fabric_names(ndo_client, ndo_token, topo):
    resp = ndo_client.get(
        "/mso/api/v1/sites",
        headers={"Authorization": f"Bearer {ndo_token}"},
    )
    assert resp.status_code == 200
    sites = resp.json()["sites"]
    reported_names = {s["name"] for s in sites}

    expected_fabric_names = {(s.fabric_name or s.name) for s in topo.sites}
    assert reported_names == expected_fabric_names, (
        f"NDO site names {reported_names} must equal topology fabric names "
        f"{expected_fabric_names}, per real-gear cisco.mso.mso_tenant "
        f"acceptance (see docs/CONTRACT.md §7)."
    )
    # And explicitly NOT the short internal names (guards against a
    # regression that silently reverts to site.name).
    short_names = {s.name for s in topo.sites}
    assert reported_names.isdisjoint(short_names - expected_fabric_names)


# ---------------------------------------------------------------------------
# FIX C — schema list-identity
# ---------------------------------------------------------------------------


def test_schema_list_identity_returns_all_seeded_schemas(ndo_client, ndo_token):
    resp = ndo_client.get(
        "/mso/api/v1/schemas/list-identity",
        headers={"Authorization": f"Bearer {ndo_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "schemas" in data
    schemas = data["schemas"]
    assert len(schemas) >= 3, "expected at least the 3 seeded schemas"

    display_names = {s["displayName"] for s in schemas}
    for expected in ("MS-TN1-schema", "SF-TN1-LAB1-schema", "SF-TN1-LAB2-schema"):
        assert expected in display_names, f"{expected} missing from list-identity"

    for s in schemas:
        assert "id" in s
        assert "displayName" in s


def test_schema_list_identity_not_shadowed_by_id_route(ndo_client, ndo_token):
    """Route-ordering regression guard: list-identity must be declared before
    the parameterized /schemas/{schema_id} route, or FastAPI matches
    "list-identity" as a schema_id and 404s with
    {"detail": "Schema 'list-identity' not found"}.
    """
    resp = ndo_client.get(
        "/mso/api/v1/schemas/list-identity",
        headers={"Authorization": f"Bearer {ndo_token}"},
    )
    assert resp.status_code == 200
    assert "detail" not in resp.json()
    assert "schemas" in resp.json()


# ---------------------------------------------------------------------------
# FIX C — /api/v1/tenants
# ---------------------------------------------------------------------------


def test_bare_api_v1_tenants_returns_tenant_list(ndo_client, ndo_token, topo):
    resp = ndo_client.get(
        "/api/v1/tenants",
        headers={"Authorization": f"Bearer {ndo_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "tenants" in data
    tenants = data["tenants"]
    assert len(tenants) == len(topo.tenants)

    tenant_names = {t["name"] for t in tenants}
    for tt in topo.tenants:
        assert tt.name in tenant_names

    for t in tenants:
        assert "id" in t
        assert "siteAssociations" in t
