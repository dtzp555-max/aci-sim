"""Phase 6 NDO tests — tests/test_ndo.py

All tests use FastAPI's synchronous TestClient (no real network).
The module-scoped client/state is shared across the whole module so that the
POST-schema write test can be followed by a GET read-back in the same session.

Key assertions:
- every §7 endpoint responds with the right shape
- VRF/BD/EPG/contract names in the schema exactly match the topology
- DHCP relay UUID resolves via the tenantPolicy template
- POST /schemas stores the schema; subsequent GET /schemas shows it
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aci_sim.ndo.app import make_ndo_app
from aci_sim.ndo.model import (
    DHCP_RELAY_UUID,
    TENANT_POLICY_TEMPLATE_ID,
    build_ndo_model,
)
from aci_sim.topology.loader import load_topology

TOPOLOGY_YAML = Path(__file__).parent.parent / "topology.yaml"


# ---------------------------------------------------------------------------
# Module-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def topo():
    return load_topology(TOPOLOGY_YAML)


@pytest.fixture(scope="module")
def client(topo):
    state = build_ndo_model(topo)
    app = make_ndo_app(state)
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def token(client):
    resp = client.post(
        "/api/v1/auth/login",
        json={"userName": "admin", "userPasswd": "cisco123", "domain": "local"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "token" in data, f"Login response missing token: {data}"
    return data["token"]


# ---------------------------------------------------------------------------
# Auth + platform version
# ---------------------------------------------------------------------------


def test_login(client):
    resp = client.post(
        "/api/v1/auth/login",
        json={"userName": "admin", "userPasswd": "cisco123", "domain": "local"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "token" in data
    assert data["token"], "token must be non-empty"


def test_platform_version(client, token):
    resp = client.get(
        "/api/v1/platform/version",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == "4.2.2"


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------


def test_get_sites_matches_topology(client, topo, token):
    """PR-10: NDO site names must be the fabric name (e.g. "LAB1-IT-ACI"),
    not the short topology-internal site name ("LAB1") — verified against a
    real-gear cisco.mso.mso_tenant run where only the fabric-name form was
    accepted (see docs/CONTRACT.md §7).
    """
    resp = client.get(
        "/mso/api/v1/sites",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    sites = resp.json()["sites"]

    assert len(sites) == len(topo.sites)
    site_names = {s["name"] for s in sites}
    for topo_site in topo.sites:
        assert (topo_site.fabric_name or topo_site.name) in site_names

    for s in sites:
        assert "id" in s
        assert s["platform"] == "APIC"
        assert isinstance(s["urls"], list) and len(s["urls"]) > 0
        # URL must embed the site's apic_host
        topo_site = next(
            ts for ts in topo.sites if (ts.fabric_name or ts.name) == s["name"]
        )
        assert topo_site.apic_host in s["urls"][0]


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------


def test_get_tenants_matches_topology(client, topo, token):
    resp = client.get(
        "/mso/api/v1/tenants",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    tenants = resp.json()["tenants"]

    assert len(tenants) == len(topo.tenants)
    ndo_names = {t["name"] for t in tenants}
    for tt in topo.tenants:
        assert tt.name in ndo_names

    for t in tenants:
        assert "id" in t
        assert "siteAssociations" in t
        assert isinstance(t["siteAssociations"], list)


def test_tenant_site_associations(client, topo, token):
    """Stretched tenant must be associated with the correct site IDs."""
    sites_resp = client.get("/mso/api/v1/sites",
                            headers={"Authorization": f"Bearer {token}"})
    # PR-10: /mso/api/v1/sites now reports fabric names, not the topology's
    # short site names — join by fabric_name (falling back to the short
    # name) to map short-name (as used in tenant.sites) → site id.
    fabric_to_short = {
        (ts.fabric_name or ts.name): ts.name for ts in topo.sites
    }
    site_id_map = {
        fabric_to_short.get(s["name"], s["name"]): s["id"]
        for s in sites_resp.json()["sites"]
    }

    tenants_resp = client.get("/mso/api/v1/tenants",
                              headers={"Authorization": f"Bearer {token}"})
    ndo_tenants = {t["name"]: t for t in tenants_resp.json()["tenants"]}

    for tt in topo.tenants:
        ndo_t = ndo_tenants[tt.name]
        ndo_site_ids = {sa["siteId"] for sa in ndo_t["siteAssociations"]}
        expected_ids = {site_id_map[s] for s in tt.sites if s in site_id_map}
        assert ndo_site_ids == expected_ids, (
            f"Tenant {tt.name}: site-id mismatch ndo={ndo_site_ids} "
            f"expected={expected_ids}"
        )


# ---------------------------------------------------------------------------
# Schemas list
# ---------------------------------------------------------------------------


def test_get_schemas_list(client, topo, token):
    resp = client.get(
        "/mso/api/v1/schemas",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    schemas = resp.json()["schemas"]

    # At least one schema per tenant
    assert len(schemas) >= len(topo.tenants)

    for s in schemas:
        assert "id" in s
        assert "displayName" in s
        assert "name" in s
        assert isinstance(s["templates"], list)


# ---------------------------------------------------------------------------
# Schema detail — cross-plane consistency
# ---------------------------------------------------------------------------


def _get_corp_schema_detail(client, token):
    """Helper: fetch the MS-TN1 schema detail (stretched tenant)."""
    schemas = client.get("/mso/api/v1/schemas",
                         headers={"Authorization": f"Bearer {token}"}).json()["schemas"]
    corp_summary = next(s for s in schemas if "MS-TN1" in s["name"])
    resp = client.get(
        f"/mso/api/v1/schemas/{corp_summary['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    return resp.json(), corp_summary["id"]


def test_schema_detail_vrf_names_match_topology(client, topo, token):
    detail, _ = _get_corp_schema_detail(client, token)
    corp_tenant = next(t for t in topo.tenants if t.name == "MS-TN1")

    assert len(detail["templates"]) >= 1
    tmpl = detail["templates"][0]

    topo_vrf_names = {v.name for v in corp_tenant.vrfs}
    ndo_vrf_names = {v["name"] for v in tmpl["vrfs"]}
    assert topo_vrf_names == ndo_vrf_names, (
        f"VRF name mismatch: topo={topo_vrf_names} ndo={ndo_vrf_names}"
    )


def test_schema_detail_bd_names_match_topology(client, topo, token):
    detail, _ = _get_corp_schema_detail(client, token)
    corp_tenant = next(t for t in topo.tenants if t.name == "MS-TN1")
    tmpl = detail["templates"][0]

    topo_bd_names = {b.name for b in corp_tenant.bds}
    ndo_bd_names = {b["name"] for b in tmpl["bds"]}
    assert topo_bd_names == ndo_bd_names, (
        f"BD name mismatch: topo={topo_bd_names} ndo={ndo_bd_names}"
    )


def test_schema_detail_bd_vrf_refs(client, topo, token):
    """Each BD's vrfRef must end with the correct VRF name."""
    detail, _ = _get_corp_schema_detail(client, token)
    corp_tenant = next(t for t in topo.tenants if t.name == "MS-TN1")
    tmpl = detail["templates"][0]

    topo_bd_vrf = {b.name: b.vrf for b in corp_tenant.bds}
    for bd in tmpl["bds"]:
        expected_vrf = topo_bd_vrf[bd["name"]]
        assert bd["vrfRef"].endswith(f"/vrfs/{expected_vrf}"), (
            f"BD {bd['name']}: vrfRef {bd['vrfRef']!r} "
            f"should end /vrfs/{expected_vrf}"
        )
        assert bd["vrfRef"].startswith("/vrfs/")


def test_schema_detail_epg_names_match_topology(client, topo, token):
    detail, _ = _get_corp_schema_detail(client, token)
    corp_tenant = next(t for t in topo.tenants if t.name == "MS-TN1")
    tmpl = detail["templates"][0]

    topo_epg_names: set[str] = {
        epg.name for ap in corp_tenant.aps for epg in ap.epgs
    }
    ndo_epg_names: set[str] = {
        epg["name"]
        for anp in tmpl["anps"]
        for epg in anp["epgs"]
    }
    assert topo_epg_names == ndo_epg_names, (
        f"EPG name mismatch: topo={topo_epg_names} ndo={ndo_epg_names}"
    )


def test_schema_detail_contract_names_match_topology(client, topo, token):
    detail, _ = _get_corp_schema_detail(client, token)
    corp_tenant = next(t for t in topo.tenants if t.name == "MS-TN1")
    tmpl = detail["templates"][0]

    topo_contract_names = {c.name for c in corp_tenant.contracts}
    ndo_contract_names = {c["name"] for c in tmpl["contracts"]}
    assert topo_contract_names == ndo_contract_names


def test_schema_detail_site_associations_match_topology(client, topo, token):
    """Stretched schema sites must exactly match the tenant's site IDs."""
    detail, _ = _get_corp_schema_detail(client, token)
    corp_tenant = next(t for t in topo.tenants if t.name == "MS-TN1")

    sites_resp = client.get("/mso/api/v1/sites",
                            headers={"Authorization": f"Bearer {token}"})
    # PR-10: join by fabric_name since /mso/api/v1/sites now reports fabric
    # names — see test_tenant_site_associations above for the same pattern.
    fabric_to_short = {
        (ts.fabric_name or ts.name): ts.name for ts in topo.sites
    }
    site_id_map = {
        fabric_to_short.get(s["name"], s["name"]): s["id"]
        for s in sites_resp.json()["sites"]
    }

    expected_site_ids = {site_id_map[s] for s in corp_tenant.sites}
    schema_site_ids = {sa["siteId"] for sa in detail.get("sites", [])}
    assert expected_site_ids == schema_site_ids, (
        f"Schema site-id mismatch: expected={expected_site_ids} "
        f"got={schema_site_ids}"
    )


def test_schema_detail_epg_bd_refs(client, topo, token):
    """Each EPG's bdRef must start /bds/ and match a real BD name."""
    detail, _ = _get_corp_schema_detail(client, token)
    corp_tenant = next(t for t in topo.tenants if t.name == "MS-TN1")
    tmpl = detail["templates"][0]
    topo_bd_names = {b.name for b in corp_tenant.bds}

    for anp in tmpl["anps"]:
        for epg in anp["epgs"]:
            bd_ref = epg["bdRef"]
            assert bd_ref.startswith("/bds/"), (
                f"EPG {epg['name']} bdRef {bd_ref!r} must start /bds/"
            )
            bd_name = bd_ref.split("/")[-1]
            assert bd_name in topo_bd_names, (
                f"EPG {epg['name']} bdRef references unknown BD {bd_name!r}"
            )


def test_schema_detail_contract_filter_refs(client, topo, token):
    """Each contract's filterRelationships must reference real filter names."""
    detail, _ = _get_corp_schema_detail(client, token)
    tmpl = detail["templates"][0]
    filter_names = {f["name"] for f in tmpl["filters"]}

    for c in tmpl["contracts"]:
        for fr in c.get("filterRelationships", []):
            f_ref = fr["filterRef"]
            assert f_ref.startswith("/filters/"), (
                f"Contract {c['name']} filterRef {f_ref!r} must start /filters/"
            )
            f_name = f_ref.split("/")[-1]
            assert f_name in filter_names, (
                f"Contract {c['name']} references unknown filter {f_name!r}"
            )


def test_schema_detail_external_epgs(client, topo, token):
    """External EPGs must have l3outRef, vrfRef, and subnets."""
    detail, _ = _get_corp_schema_detail(client, token)
    tmpl = detail["templates"][0]

    # Corp has two l3outs each with one ext_epg → expect 2 externalEpgs
    corp_tenant = next(t for t in topo.tenants if t.name == "MS-TN1")
    expected_count = sum(
        len(l3out.ext_epgs) for l3out in corp_tenant.l3outs
    )
    assert len(tmpl["externalEpgs"]) == expected_count

    for xepg in tmpl["externalEpgs"]:
        assert xepg["vrfRef"].startswith("/vrfs/")
        assert xepg["l3outRef"].startswith("/l3outs/")
        assert isinstance(xepg["subnets"], list)


# ---------------------------------------------------------------------------
# Fabric connectivity
# ---------------------------------------------------------------------------


def test_fabric_connectivity(client, topo, token):
    resp = client.get(
        "/mso/api/v1/sites/fabric-connectivity",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()

    # Accept either a list or {"sites": [...]}
    fc_sites = (
        data
        if isinstance(data, list)
        else data.get("sites", data.get("fabricConnectivity", []))
    )
    assert len(fc_sites) == len(topo.sites), (
        f"Expected {len(topo.sites)} sites in fabric-connectivity, got {len(fc_sites)}"
    )
    for fc in fc_sites:
        # At least one status field must be present
        has_status = fc.get("status") or fc.get("connectivityStatus")
        assert has_status, f"fabric-connectivity entry missing status: {fc}"


# ---------------------------------------------------------------------------
# Policy states
# ---------------------------------------------------------------------------


def test_policy_states_per_schema(client, token):
    schemas = client.get("/mso/api/v1/schemas",
                         headers={"Authorization": f"Bearer {token}"}).json()["schemas"]

    for schema_summary in schemas:
        schema_id = schema_summary["id"]
        resp = client.get(
            f"/mso/api/v1/schemas/{schema_id}/policy-states",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        states = (
            data
            if isinstance(data, list)
            else data.get("policyStates", data.get("states", []))
        )
        assert len(states) > 0, (
            f"Schema {schema_id} returned empty policy-states"
        )
        for state in states:
            assert "status" in state, f"policy-state entry missing 'status': {state}"


# ---------------------------------------------------------------------------
# Template summaries + tenantPolicy DHCP
# ---------------------------------------------------------------------------


def test_template_summaries(client, token):
    resp = client.get(
        "/mso/api/v1/templates/summaries",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()

    # Normalise: handle both list and {"templates": [...]}
    summaries = data if isinstance(data, list) else data.get("templates", [])
    assert len(summaries) > 0

    types = [s.get("templateType") for s in summaries]
    assert "tenantPolicy" in types, (
        f"No tenantPolicy entry in template summaries: {types}"
    )
    for s in summaries:
        assert "templateId" in s
        assert "templateType" in s


def test_tenant_policy_template_dhcp_relay(client, token):
    resp = client.get(
        f"/mso/api/v1/templates/{TENANT_POLICY_TEMPLATE_ID}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    inner = (
        resp.json()
        .get("tenantPolicyTemplate", {})
        .get("template", {})
    )
    relay = inner.get("dhcpRelayPolicies", [])
    assert len(relay) > 0, "tenantPolicy template must have dhcpRelayPolicies"
    for p in relay:
        assert "uuid" in p
        assert "name" in p
    assert relay[0]["uuid"] == DHCP_RELAY_UUID


def test_bd_dhcp_labels_resolve_via_tenant_policy(client, token):
    """Every BD dhcpLabel ref must resolve in the tenantPolicy DHCP map."""
    # Build uuid→name map exactly as ndo_connector.get_dhcp_policy_map() does
    summaries_resp = client.get(
        "/mso/api/v1/templates/summaries",
        headers={"Authorization": f"Bearer {token}"},
    )
    summaries = summaries_resp.json()
    if isinstance(summaries, dict):
        summaries = summaries.get("templates", [])

    dhcp_map: dict[str, str] = {}
    for s in summaries:
        if s.get("templateType") != "tenantPolicy":
            continue
        tmpl_resp = client.get(
            f"/mso/api/v1/templates/{s['templateId']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        inner = (
            tmpl_resp.json()
            .get("tenantPolicyTemplate", {})
            .get("template", {})
        )
        for p in inner.get("dhcpRelayPolicies", []):
            if p.get("uuid") and p.get("name"):
                dhcp_map[p["uuid"]] = p["name"]
        for p in inner.get("dhcpOptionPolicies", []):
            if p.get("uuid") and p.get("name"):
                dhcp_map[p["uuid"]] = p["name"]

    assert dhcp_map, "DHCP policy map must not be empty"

    # Verify every BD dhcpLabel ref is resolvable
    schemas = client.get("/mso/api/v1/schemas",
                         headers={"Authorization": f"Bearer {token}"}).json()["schemas"]
    found_any_label = False
    for s in schemas:
        detail = client.get(
            f"/mso/api/v1/schemas/{s['id']}",
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        for tmpl in detail.get("templates", []):
            for bd in tmpl.get("bds", []):
                for lbl in bd.get("dhcpLabels", []):
                    ref = lbl.get("ref", "")
                    assert ref in dhcp_map, (
                        f"BD {bd['name']} dhcpLabel ref {ref!r} "
                        f"not in DHCP policy map {list(dhcp_map)}"
                    )
                    found_any_label = True

    assert found_any_label, "No BD with dhcpLabels found — test is vacuous"


# ---------------------------------------------------------------------------
# Audit records (both endpoints)
# ---------------------------------------------------------------------------


def test_audit_records_nonempty_primary(client, token):
    resp = client.get(
        "/api/v1/audit-records?count=50",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    records = (
        data
        if isinstance(data, list)
        else data.get("auditRecords", data.get("records", []))
    )
    assert len(records) > 0, "Primary audit-records endpoint returned empty list"


def test_audit_records_fallback_endpoint(client, token):
    resp = client.get(
        "/mso/api/v1/audit-records?count=20",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    records = (
        data
        if isinstance(data, list)
        else data.get("auditRecords", data.get("records", []))
    )
    assert len(records) > 0


def test_audit_record_fields(client, token):
    resp = client.get("/api/v1/audit-records",
                      headers={"Authorization": f"Bearer {token}"})
    records = resp.json().get("auditRecords", [])
    for rec in records:
        # ndo_audit_log reads these fields; all must be present
        assert "timestamp" in rec or "date" in rec, f"No timestamp in {rec}"
        assert "user" in rec or "userName" in rec, f"No user in {rec}"
        assert "action" in rec or "type" in rec, f"No action in {rec}"


# ---------------------------------------------------------------------------
# POST schemas → write + read-back
# ---------------------------------------------------------------------------


def test_post_schema_and_readback(client, token):
    """POST a new schema and verify it appears in the schema list."""
    new_schema = {
        "displayName": "test-post-schema",
        "name": "test-post-schema",
        "templates": [
            {
                "name": "test-tmpl",
                "templateType": "application",
                "vrfs": [],
                "bds": [],
                "anps": [],
                "contracts": [],
                "filters": [],
                "externalEpgs": [],
            }
        ],
    }

    # Create
    resp = client.post(
        "/mso/api/v1/schemas",
        json=new_schema,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    created = resp.json()
    assert "id" in created, f"POST /schemas response missing id: {created}"
    schema_id = created["id"]
    assert created.get("status") == "active"

    # Read back from list
    list_resp = client.get("/mso/api/v1/schemas",
                           headers={"Authorization": f"Bearer {token}"})
    ids_in_list = {s["id"] for s in list_resp.json()["schemas"]}
    assert schema_id in ids_in_list, (
        f"New schema {schema_id!r} not in GET /schemas after POST"
    )

    # Read back full detail
    detail_resp = client.get(
        f"/mso/api/v1/schemas/{schema_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert detail_resp.status_code == 200


# ---------------------------------------------------------------------------
# POST deploy
# ---------------------------------------------------------------------------


def test_post_deploy(client, token):
    resp = client.post(
        "/mso/api/v1/deploy",
        json={"schemaId": "schema-123", "templateName": "tmpl"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["status"] == "in-progress"


# ---------------------------------------------------------------------------
# Auth leniency — no bearer token still works
# ---------------------------------------------------------------------------


def test_auth_lenient_no_token(client):
    """Endpoints must respond even without a Bearer token (lenient auth)."""
    assert client.get("/mso/api/v1/sites").status_code == 200
    assert client.get("/mso/api/v1/tenants").status_code == 200
    assert client.get("/mso/api/v1/schemas").status_code == 200


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_unknown_schema_returns_404(client, token):
    resp = client.get(
        "/mso/api/v1/schemas/does-not-exist-xyzxyz",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


def test_unknown_template_returns_404(client, token):
    resp = client.get(
        "/mso/api/v1/templates/no-such-template-id",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
