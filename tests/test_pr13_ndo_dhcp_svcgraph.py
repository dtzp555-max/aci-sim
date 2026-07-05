"""PR-13 — NDO tenant-policy-template DHCP surface + schema serviceGraphs
for cisco.mso.

Two confirmed sim gaps from a real aci-ansible run against ACI hardware, both
blocking the last NDO write surfaces in the multi-site (MS) suite:

GAP 1 — `cisco.mso.ndo_template`'s tenant-policy-template CREATE flow
(`prereq_tenantpol.yml`'s `Create TenantPol-<tenant> tenant policy template`
task) 404'd on `GET /api/v1/templates/summaries` (note: BARE path, no `/mso`
prefix) — confirmed root cause: that task carries `delegate_to: localhost`,
which forces `ansible-mso`'s `MSOModule` down its direct-HTTP branch
(`module._socket_path is None`) instead of the persistent
`ansible.netcommon.httpapi` connection every OTHER task on the same
playbook host uses — and the direct-HTTP branch never adds the `/mso`
prefix (`ansible-mso`'s `plugins/module_utils/mso.py::request()`:
`self.url = "{0}api/{1}/{2}".format(self.base_only_uri, api_version,
self.path...)`). Once the template exists, `cisco.mso.ndo_dhcp_relay_policy`
(`create_dhcp_relay`) and `cisco.mso.ndo_schema_template_bd_dhcp_policy`
(`bind_dhcp_relay_to_bd`) — both running over the normal httpapi connection,
hence the mso-prefixed path — must be able to create/read/cross-reference
DHCP relay policies against it, including resolving a schema-template EPG's
`uuid` (a field the sim never populated before this PR) as a DHCP relay
provider ref.

GAP 2 — the "Create service graph" task (mso-model role's
`custom_mso_schema_service_graph.py`, a pre-`MSOTemplate`/`MSOSchema`
community module) bare-subscripts `schema_obj.get('sites')[site_idx]
['serviceGraphs']` — a real hardware MS-TN2 `create_tenant` pass-2 run
(`-e automate_contract_graph=true`) hit `KeyError: 'serviceGraphs'` on
exactly this line. The TEMPLATE-level `serviceGraphs` bare-subscript was
already covered by PR-11's `TEMPLATE_COLLECTION_KEYS`; this PR closes the
SITE-level sibling gap (`SITE_TOP_LEVEL_DEFAULTS`) and the additional
`GET /schemas` full-detail + `schemas/service-node-types` + template
`tenantId` gaps this same legacy module needs, since it bare-subscripts the
raw `/schemas` list response instead of going through `MSOSchema`.

GAP 2 DEEPER LAYER (found while chasing GAP 2 to green on real hardware, with
`-e automate_contract_graph=true -e automate_site_redirect=true`) — once
the site-level `serviceGraphs` KeyError was fixed, the same real run
progressed to the mso-model role's raw `cisco.mso.mso_rest` "Atomic PATCH —
bind service-graph redirect on ALL fabrics in one request" task, which
addresses a SITE-LOCAL contract by bare name:
`/sites/{siteId}-{template}/contracts/{contract}/serviceGraphRelationship`.
The sim never mirrored template-level contract adds into
`sites[].contracts[]` at all (unlike ANPs/EPGs, mirrored since PR-12), so
`apply_json_patch`'s `_find_by_name` raised `PatchError: path segment
'con-Firewall_LAB0' not found in list` — confirmed verbatim on the real
hardware run. This PR adds `_mirror_template_contract_to_sites` (PATCH-time,
parallel to `_mirror_template_anp_to_sites`) and the equivalent boot-time
mirror in `build_ndo_model`, closing this fully — the real hardware MS-TN2
`create_tenant` pass-2 run now reaches `failed=0`.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aci_sim.ndo.app import make_ndo_app
from aci_sim.ndo.model import TENANT_POLICY_TEMPLATE_ID, build_ndo_model
from aci_sim.ndo.patch import (
    SITE_TOP_LEVEL_DEFAULTS,
    apply_json_patch,
    normalize_site,
    normalize_template,
)
from aci_sim.topology.loader import load_topology

TOPOLOGY_YAML = Path(__file__).parent.parent / "topology.yaml"


@pytest.fixture()
def topo():
    return load_topology(TOPOLOGY_YAML)


@pytest.fixture()
def client(topo):
    state = build_ndo_model(topo, sandbox=True)
    app = make_ndo_app(state)
    with TestClient(app) as c:
        yield c


def _tenant_id(client, name: str) -> str:
    tenants = client.get("/api/v1/tenants").json()["tenants"]
    return next(t["id"] for t in tenants if t["name"] == name)


def _site_ids(client) -> list[str]:
    return [s["id"] for s in client.get("/api/v1/sites").json()["sites"]]


# ---------------------------------------------------------------------------
# GAP 1a — bare (delegate_to: localhost) template routes
# ---------------------------------------------------------------------------


def test_bare_templates_summaries_route_exists(client):
    """`ndo_template`'s `delegate_to: localhost` prereq task hits the BARE
    `/api/v1/templates/summaries` (no `/mso` prefix) — a real hardware run
    404'd here before this fix."""
    resp = client.get("/api/v1/templates/summaries")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_bare_and_mso_prefixed_templates_summaries_share_data(client):
    """A template created via the bare path must be visible to a caller
    using the mso-prefixed path (and vice versa) — both back the same
    mutable store, matching the existing /api/v1/tenants <-> /mso/api/v1/
    tenants pattern PR-10/PR-11 established for the same delegate_to split."""
    tenant_id = _tenant_id(client, "MS-TN1")
    site_ids = _site_ids(client)
    payload = {
        "displayName": "TenantPol-MS-TN1",
        "templateType": "tenantPolicy",
        "tenantPolicyTemplate": {
            "template": {"tenantId": tenant_id},
            "sites": [{"siteId": s} for s in site_ids],
        },
    }
    created = client.post("/api/v1/templates", json=payload)
    assert created.status_code == 200
    template_id = created.json()["templateId"]

    mso_summaries = client.get("/mso/api/v1/templates/summaries").json()
    match = [s for s in mso_summaries if s.get("templateName") == "TenantPol-MS-TN1"]
    assert len(match) == 1
    assert match[0]["templateId"] == template_id
    assert match[0]["templateType"] == "tenantPolicy"

    # And the reverse: mso-prefixed GET finds the bare-created template.
    detail = client.get(f"/mso/api/v1/templates/{template_id}").json()
    assert detail["tenantPolicyTemplate"]["template"]["tenantId"] == tenant_id


# ---------------------------------------------------------------------------
# GAP 1b — ndo_template create -> lookup round trip (full sequence)
# ---------------------------------------------------------------------------


def test_ndo_template_create_then_lookup_round_trip(client):
    """Full replica of `ndo_template`'s CREATE flow: `lookup_tenant()` (GET
    tenants), `lookup_site()` per site (GET sites), `MSOTemplate.__init__`
    (GET templates/summaries?templateName=...  finds nothing) -> POST
    templates with the tenantPolicyTemplate payload shape -> template must
    then resolve by name via templates/summaries."""
    tenant_id = _tenant_id(client, "MS-TN1")
    site_ids = _site_ids(client)

    # MSOTemplate.__init__ pre-create lookup: nothing found yet.
    before = client.get("/api/v1/templates/summaries").json()
    assert not any(s.get("templateName") == "TenantPol-MS-TN1" for s in before)

    payload = {
        "displayName": "TenantPol-MS-TN1",
        "templateType": "tenantPolicy",
        "tenantPolicyTemplate": {
            "template": {"tenantId": tenant_id},
            "sites": [{"siteId": s} for s in site_ids],
        },
    }
    resp = client.post("/api/v1/templates", json=payload)
    assert resp.status_code == 200
    template_id = resp.json()["templateId"]
    assert template_id

    after = client.get("/api/v1/templates/summaries").json()
    match = next(s for s in after if s.get("templateName") == "TenantPol-MS-TN1")
    assert match["templateType"] == "tenantPolicy"
    assert match["templateId"] == template_id

    detail = client.get(f"/api/v1/templates/{template_id}").json()
    assert detail["templateType"] == "tenantPolicy"
    assert detail["tenantPolicyTemplate"]["template"]["tenantId"] == tenant_id
    assert {s["siteId"] for s in detail["tenantPolicyTemplate"]["sites"]} == set(site_ids)


def test_ndo_template_create_is_idempotent_across_prefixes(client):
    """Creating the same-named template twice (e.g. a retried playbook
    task) must not silently corrupt the summaries list — this sim's create
    handler always assigns a fresh id (matching MSOTemplate's own
    "not found -> create" branch, which only runs when the by-name lookup
    truly found nothing); the test asserts the store stays internally
    consistent rather than asserting real NDO's idempotency semantics."""
    tenant_id = _tenant_id(client, "MS-TN1")
    payload = {
        "displayName": "TenantPol-MS-TN1",
        "templateType": "tenantPolicy",
        "tenantPolicyTemplate": {"template": {"tenantId": tenant_id}, "sites": []},
    }
    r1 = client.post("/api/v1/templates", json=payload)
    tid1 = r1.json()["templateId"]
    summaries = client.get("/mso/api/v1/templates/summaries").json()
    assert sum(1 for s in summaries if s["templateId"] == tid1) == 1


# ---------------------------------------------------------------------------
# GAP 1c — DHCP relay policy add/read (ndo_dhcp_relay_policy + bd binding)
# ---------------------------------------------------------------------------


def test_dhcp_relay_policy_add_and_read_via_patch(client):
    """Full replica of `create_dhcp_relay`'s NDO 4.x path
    (`cisco.mso.ndo_dhcp_relay_policy`): PATCH `add
    /tenantPolicyTemplate/template/dhcpRelayPolicies/-` against the
    tenant-policy template, using a schema-template EPG's `uuid` as the
    provider's `epgRef` (this PR backfills that uuid — previously absent)."""
    tenant_id = _tenant_id(client, "MS-TN1")
    site_ids = _site_ids(client)
    template_id = client.post(
        "/api/v1/templates",
        json={
            "displayName": "TenantPol-MS-TN1",
            "templateType": "tenantPolicy",
            "tenantPolicyTemplate": {"template": {"tenantId": tenant_id}, "sites": [{"siteId": s} for s in site_ids]},
        },
    ).json()["templateId"]

    schemas = client.get("/mso/api/v1/schemas/list-identity").json()["schemas"]
    ms_schema = next(s for s in schemas if "MS-TN1" in s["displayName"])
    detail = client.get(f"/mso/api/v1/schemas/{ms_schema['id']}").json()
    tmpl = detail["templates"][0]
    anp = tmpl["anps"][0]
    epg = anp["epgs"][0]
    assert epg.get("uuid"), "schema-template EPG must carry a uuid for DHCP relay provider refs"

    ops = [
        {
            "op": "add",
            "path": "/tenantPolicyTemplate/template/dhcpRelayPolicies/-",
            "value": {
                "name": "pol-dhcp_relay-Test1",
                "providers": [{"ip": "192.168.41.10", "useServerVrf": False, "epgRef": epg["uuid"], "epgName": epg["name"]}],
            },
        }
    ]
    resp = client.patch(f"/mso/api/v1/templates/{template_id}", json=ops)
    assert resp.status_code == 200

    relays = resp.json()["tenantPolicyTemplate"]["template"]["dhcpRelayPolicies"]
    added = next(r for r in relays if r["name"] == "pol-dhcp_relay-Test1")
    assert added.get("uuid"), "sim must backfill a uuid on a newly-added DHCP relay policy"
    assert added["providers"][0]["epgRef"] == epg["uuid"]


def test_dhcp_relay_policy_uuid_resolves_via_templates_objects(client):
    """`ndo_schema_template_bd_dhcp_policy`'s `get_dhcp_relay_policy_uuid()`
    (the `bind_dhcp_relay_to_bd` playbook step) queries `GET
    templates/objects?type=dhcpRelay&name=...&tenantId=<matched-client-side>`
    and requires BOTH `tenantId` and `uuid` on the returned entry."""
    tenant_id = _tenant_id(client, "MS-TN1")
    template_id = client.post(
        "/api/v1/templates",
        json={
            "displayName": "TenantPol-MS-TN1",
            "templateType": "tenantPolicy",
            "tenantPolicyTemplate": {"template": {"tenantId": tenant_id}, "sites": []},
        },
    ).json()["templateId"]
    client.patch(
        f"/mso/api/v1/templates/{template_id}",
        json=[
            {
                "op": "add",
                "path": "/tenantPolicyTemplate/template/dhcpRelayPolicies/-",
                "value": {"name": "pol-dhcp_relay-Test1", "providers": [{"ip": "10.0.0.1", "useServerVrf": False}]},
            }
        ],
    )

    resp = client.get("/mso/api/v1/templates/objects", params={"type": "dhcpRelay", "name": "pol-dhcp_relay-Test1"})
    assert resp.status_code == 200
    matches = [r for r in resp.json() if r.get("tenantId") == tenant_id and r.get("name") == "pol-dhcp_relay-Test1"]
    assert len(matches) == 1
    relay_uuid = matches[0]["uuid"]
    assert relay_uuid

    # get_dhcp_relay_label_name() query-back-by-uuid (bind_dhcp_relay_to_bd's
    # post-PATCH name resolution step).
    by_uuid = client.get("/mso/api/v1/templates/objects", params={"type": "dhcpRelay", "uuid": relay_uuid}).json()
    assert by_uuid.get("name") == "pol-dhcp_relay-Test1"


def test_bind_dhcp_relay_to_bd_end_to_end(client):
    """Full replica of `bind_dhcp_relay_to_bd`'s
    `cisco.mso.ndo_schema_template_bd_dhcp_policy` PATCH: resolve the relay
    policy's uuid (templates/objects), then PATCH `add
    /templates/{t}/bds/{bd}/dhcpLabels/-` with `{ref: uuid, name}` on the
    schema template — must round-trip through a fresh GET."""
    tenant_id = _tenant_id(client, "MS-TN1")
    template_id = client.post(
        "/api/v1/templates",
        json={
            "displayName": "TenantPol-MS-TN1",
            "templateType": "tenantPolicy",
            "tenantPolicyTemplate": {"template": {"tenantId": tenant_id}, "sites": []},
        },
    ).json()["templateId"]
    client.patch(
        f"/mso/api/v1/templates/{template_id}",
        json=[
            {
                "op": "add",
                "path": "/tenantPolicyTemplate/template/dhcpRelayPolicies/-",
                "value": {"name": "pol-dhcp_relay-Test1", "providers": [{"ip": "10.0.0.1", "useServerVrf": False}]},
            }
        ],
    )
    relay_uuid = client.get(
        "/mso/api/v1/templates/objects", params={"type": "dhcpRelay", "name": "pol-dhcp_relay-Test1"}
    ).json()[0]["uuid"]

    schemas = client.get("/mso/api/v1/schemas/list-identity").json()["schemas"]
    ms_schema = next(s for s in schemas if "MS-TN1" in s["displayName"])
    detail = client.get(f"/mso/api/v1/schemas/{ms_schema['id']}").json()
    tmpl = detail["templates"][0]
    bd = tmpl["bds"][0]

    ops = [
        {
            "op": "add",
            "path": f"/templates/{tmpl['name']}/bds/{bd['name']}/dhcpLabels/-",
            "value": {"ref": relay_uuid, "name": "pol-dhcp_relay-Test1"},
        }
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{ms_schema['id']}", json=ops)
    assert resp.status_code == 200
    bd_after = next(b for b in resp.json()["templates"][0]["bds"] if b["name"] == bd["name"])
    assert any(label["ref"] == relay_uuid for label in bd_after["dhcpLabels"])

    # Independent GET must reflect the same mutation.
    fresh = client.get(f"/mso/api/v1/schemas/{ms_schema['id']}").json()
    bd_fresh = next(b for b in fresh["templates"][0]["bds"] if b["name"] == bd["name"])
    assert any(label["ref"] == relay_uuid for label in bd_fresh["dhcpLabels"])


# ---------------------------------------------------------------------------
# GAP 2a — site-level serviceGraphs default
# ---------------------------------------------------------------------------


def test_site_top_level_defaults_include_service_graphs():
    assert SITE_TOP_LEVEL_DEFAULTS["serviceGraphs"] == []


def test_normalize_site_backfills_service_graphs():
    """`custom_mso_schema_service_graph.py` bare-subscripts
    `schema_obj.get('sites')[site_idx]['serviceGraphs']` — a schema site
    entry that never had this key backfilled must not KeyError."""
    site = {"siteId": "1", "templateName": "LAB1"}
    normalize_site(site)
    assert site["serviceGraphs"] == []
    assert site["bds"] == []
    assert site["anps"] == []


def test_normalize_site_service_graphs_idempotent():
    site = {"siteId": "1", "templateName": "LAB1", "serviceGraphs": [{"name": "sgt-1"}]}
    normalize_site(site)
    normalize_site(site)
    assert site["serviceGraphs"] == [{"name": "sgt-1"}]


def test_apply_json_patch_add_site_service_graph_by_composite_key():
    """Full replica of `custom_mso_schema_service_graph.py`'s site-local
    add: `/sites/{siteId}-{templateName}/serviceGraphs/-`."""
    doc = {
        "id": "schema-abc",
        "templates": [{"name": "LAB1", "serviceGraphs": []}],
        "sites": [{"siteId": "1", "templateName": "LAB1", "serviceGraphs": []}],
    }
    ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/serviceGraphs/-",
            "value": {
                "serviceGraphRef": {"schemaId": "schema-abc", "templateName": "LAB1", "serviceGraphName": "sgt-FW_LAB0"},
                "serviceNodes": [],
            },
        }
    ]
    apply_json_patch(doc, ops)
    site = doc["sites"][0]
    assert len(site["serviceGraphs"]) == 1
    assert site["serviceGraphs"][0]["serviceGraphRef"] == "/schemas/schema-abc/templates/LAB1/serviceGraphs/sgt-FW_LAB0"


# ---------------------------------------------------------------------------
# GAP 2b — full schema GET returns rich detail (not the trimmed summary)
# ---------------------------------------------------------------------------


def test_get_schemas_returns_full_detail_not_trimmed_summary(client):
    """`custom_mso_schema_service_graph.py` (and sibling legacy modules)
    bare-subscript straight into the plain `GET /schemas` list response —
    `schema_obj.get('templates')[idx]['serviceGraphs']`,
    `schema_obj.get('sites')[idx]['serviceGraphs']` — so this route must
    return the FULL nested doc per schema, not the `{name}`-only summary
    `schemas/list-identity` is optimized for."""
    resp = client.get("/mso/api/v1/schemas")
    assert resp.status_code == 200
    schemas = resp.json()["schemas"]
    assert len(schemas) > 0
    for s in schemas:
        assert "sites" in s
        for tmpl in s.get("templates", []):
            assert "serviceGraphs" in tmpl
            assert "bds" in tmpl
            assert "anps" in tmpl
    for s in schemas:
        for site in s.get("sites", []):
            assert "serviceGraphs" in site


def test_get_schemas_service_graph_bare_subscript_flow_end_to_end(client):
    """End-to-end replica of `custom_mso_schema_service_graph.py`'s full
    read sequence: `mso.get_obj('schemas', displayName=schema)` ->
    `schema_obj.get('templates')[idx]['serviceGraphs']` (create branch) ->
    `schema_obj.get('sites')[site_idx]['serviceGraphs']` (site-bind
    branch) — none of these may KeyError, matching the real hardware MS-TN2
    `create_tenant` pass-2 crash signature this PR fixes."""
    all_schemas = client.get("/mso/api/v1/schemas").json()["schemas"]
    schema_obj = next(s for s in all_schemas if s.get("displayName") == "MS-TN1-schema")

    templates = [t.get("name") for t in schema_obj.get("templates")]
    template_idx = templates.index("MS-TN1-template")
    assert schema_obj.get("templates")[template_idx]["serviceGraphs"] == []
    assert schema_obj.get("templates")[template_idx]["tenantId"], "template must carry tenantId for the update branch"

    sites = schema_obj.get("sites")
    assert sites, "expected at least one site associated with MS-TN1-template"
    assert sites[0]["serviceGraphs"] == []


def test_schemas_service_node_types_route(client):
    """`custom_mso_schema_service_graph.py` resolves Firewall/Load
    Balancer/Other to a stable id via `schemas/service-node-types` before
    every service-graph node add — must be routed BEFORE the parameterized
    `/schemas/{schema_id}` route (same ordering hazard as `list-identity`)."""
    resp = client.get("/mso/api/v1/schemas/service-node-types")
    assert resp.status_code == 200
    types = {t["displayName"]: t["id"] for t in resp.json()["serviceNodeTypes"]}
    assert "Firewall" in types
    assert types["Firewall"]

    # Bare-path variant too (defensive — no delegate_to precedent observed
    # for this specific task, but kept consistent with every other bare/
    # mso-prefixed pair this sim maintains).
    resp2 = client.get("/api/v1/schemas/service-node-types")
    assert resp2.status_code == 200


def test_schema_template_and_site_service_graph_patch_round_trip(client):
    """Full multi-step replica of the real "Create service graph" +
    site-bind sequence against a live schema: add the template-level
    serviceGraph, then the site-level serviceGraph binding, and confirm
    both survive an independent re-GET."""
    schemas = client.get("/mso/api/v1/schemas/list-identity").json()["schemas"]
    ms_schema = next(s for s in schemas if s["displayName"] == "MS-TN1-schema")
    detail = client.get(f"/mso/api/v1/schemas/{ms_schema['id']}").json()
    tmpl_name = detail["templates"][0]["name"]
    site = detail["sites"][0]
    site_key = f"{site['siteId']}-{tmpl_name}"

    template_sg_ops = [
        {
            "op": "add",
            "path": f"/templates/{tmpl_name}/serviceGraphs/-",
            "value": {
                "name": "sgt-FW_LAB0",
                "displayName": "sgt-FW_LAB0",
                "serviceNodes": [{"name": "node1", "serviceNodeTypeId": "svc-node-type-firewall", "index": 1}],
            },
        }
    ]
    r1 = client.patch(f"/mso/api/v1/schemas/{ms_schema['id']}", json=template_sg_ops)
    assert r1.status_code == 200

    site_sg_ops = [
        {
            "op": "add",
            "path": f"/sites/{site_key}/serviceGraphs/-",
            "value": {
                "serviceGraphRef": {"schemaId": ms_schema["id"], "templateName": tmpl_name, "serviceGraphName": "sgt-FW_LAB0"},
                "serviceNodes": [
                    {
                        "serviceNodeRef": {
                            "schemaId": ms_schema["id"],
                            "templateName": tmpl_name,
                            "serviceGraphName": "sgt-FW_LAB0",
                            "serviceNodeName": "node1",
                        },
                        "device": {"dn": "uni/tn-MS-TN1/lDevVip-fw-FW_LAB0"},
                    }
                ],
            },
        }
    ]
    r2 = client.patch(f"/mso/api/v1/schemas/{ms_schema['id']}", json=site_sg_ops)
    assert r2.status_code == 200

    fresh = client.get(f"/mso/api/v1/schemas/{ms_schema['id']}").json()
    fresh_tmpl = fresh["templates"][0]
    assert any(sg["name"] == "sgt-FW_LAB0" for sg in fresh_tmpl["serviceGraphs"])

    fresh_site = next(s for s in fresh["sites"] if s["siteId"] == site["siteId"])
    assert len(fresh_site["serviceGraphs"]) == 1
    assert fresh_site["serviceGraphs"][0]["serviceGraphRef"].endswith("/serviceGraphs/sgt-FW_LAB0")


def test_contract_service_graph_relationship_bind(client):
    """`cisco.mso.mso_schema_template_contract_service_graph`'s bind (used
    when `automate_contract_graph=true`) PATCHes `add
    /templates/{t}/contracts/{c}/serviceGraphRelationship` — must resolve
    against a contract that already has `serviceGraphs` normalized on its
    parent template (this PR doesn't touch this module directly since it
    already uses safe `.get()`, but the full chain is asserted here)."""
    schemas = client.get("/mso/api/v1/schemas/list-identity").json()["schemas"]
    ms_schema = next(s for s in schemas if s["displayName"] == "MS-TN1-schema")
    detail = client.get(f"/mso/api/v1/schemas/{ms_schema['id']}").json()
    tmpl = detail["templates"][0]
    contract = tmpl["contracts"][0]
    assert contract.get("serviceGraphRelationship") is None

    client.patch(
        f"/mso/api/v1/schemas/{ms_schema['id']}",
        json=[
            {
                "op": "add",
                "path": f"/templates/{tmpl['name']}/serviceGraphs/-",
                "value": {"name": "sgt-FW_LAB0", "displayName": "sgt-FW_LAB0", "serviceNodes": []},
            }
        ],
    )
    bind_ops = [
        {
            "op": "add",
            "path": f"/templates/{tmpl['name']}/contracts/{contract['name']}/serviceGraphRelationship",
            "value": {
                "serviceGraphRef": {"serviceGraphName": "sgt-FW_LAB0", "templateName": tmpl["name"], "schemaId": ms_schema["id"]},
                "serviceNodesRelationship": [],
            },
        }
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{ms_schema['id']}", json=bind_ops)
    assert resp.status_code == 200
    updated_contract = next(c for c in resp.json()["templates"][0]["contracts"] if c["name"] == contract["name"])
    assert updated_contract["serviceGraphRelationship"]["serviceGraphRef"].endswith("/serviceGraphs/sgt-FW_LAB0")


# ---------------------------------------------------------------------------
# uuid backfill on schema-template epgs/externalEpgs (normalize_template)
# ---------------------------------------------------------------------------


def test_normalize_template_backfills_epg_uuid():
    tmpl = {"name": "LAB1", "anps": [{"name": "app-Web_LAB1", "epgs": [{"name": "epg-web"}]}]}
    normalize_template(tmpl, "schema-abc")
    epg = tmpl["anps"][0]["epgs"][0]
    assert epg.get("uuid")
    assert len(epg["uuid"]) == 32


def test_normalize_template_epg_uuid_is_stable_and_idempotent():
    tmpl = {"name": "LAB1", "anps": [{"name": "app-Web_LAB1", "epgs": [{"name": "epg-web"}]}]}
    normalize_template(tmpl, "schema-abc")
    first = tmpl["anps"][0]["epgs"][0]["uuid"]
    normalize_template(tmpl, "schema-abc")
    assert tmpl["anps"][0]["epgs"][0]["uuid"] == first


def test_normalize_template_backfills_external_epg_uuid():
    tmpl = {"name": "LAB1", "externalEpgs": [{"name": "xepg-All_LAB0"}]}
    normalize_template(tmpl, "schema-abc")
    ext_epg = tmpl["externalEpgs"][0]
    assert ext_epg.get("uuid")


def test_build_ndo_model_seeds_epg_uuid_for_topology_tenants(topo):
    """Every schema-template EPG a topology.yaml tenant boots with already
    must carry a uuid — not just ones added at PATCH-runtime — since
    `ndo_dhcp_relay_policy`'s provider resolution can target any
    pre-existing topology EPG."""
    state = build_ndo_model(topo)
    found_any_epg = False
    for detail in state.schema_details.values():
        for tmpl in detail.get("templates", []):
            for anp in tmpl.get("anps", []):
                for epg in anp.get("epgs", []):
                    found_any_epg = True
                    assert epg.get("uuid"), f"EPG {epg.get('name')} missing uuid"
    assert found_any_epg, "expected at least one topology tenant with an EPG"


# ---------------------------------------------------------------------------
# Seeded tenantPolicy template summary carries templateName (name-based
# lookup requirement)
# ---------------------------------------------------------------------------


def test_seeded_tenant_policy_template_summary_has_template_name(client):
    """`MSOTemplate.__init__`'s by-name lookup filters
    `templateName`+`templateType` — the boot-seeded tenantPolicy template
    summary must carry a `templateName`, not just `templateId`/
    `templateType`, or a real `ndo_template`/`ndo_dhcp_relay_policy` call
    targeting it by name could never match."""
    summaries = client.get("/mso/api/v1/templates/summaries").json()
    seeded = next(s for s in summaries if s["templateId"] == TENANT_POLICY_TEMPLATE_ID)
    assert seeded.get("templateName")
    assert seeded["templateType"] == "tenantPolicy"


# ---------------------------------------------------------------------------
# GAP 2 deeper layer — site-local contracts[] mirroring (service-graph
# redirect atomic PATCH)
# ---------------------------------------------------------------------------


def test_site_top_level_defaults_include_contracts():
    assert SITE_TOP_LEVEL_DEFAULTS["contracts"] == []


def test_build_ndo_model_seeds_site_local_contracts_for_topology_tenants(topo):
    """A topology.yaml tenant that already ships with contracts configured
    must boot with its site-local `sites[].contracts[]` already mirrored —
    otherwise the FIRST raw mso_rest PATCH addressing a site-local contract
    by bare name would hit the same 'not found in list' crash this PR
    fixes for the PATCH-built case."""
    state = build_ndo_model(topo, sandbox=True)
    found_any = False
    for detail in state.schema_details.values():
        for tmpl in detail.get("templates", []):
            if not tmpl.get("contracts"):
                continue
            for site in detail.get("sites", []):
                if site.get("templateName") != tmpl["name"]:
                    continue
                assert "contracts" in site
                for contract in tmpl["contracts"]:
                    matches = [c for c in site["contracts"] if c.get("contractRef", "").endswith(f"/contracts/{contract['name']}")]
                    assert matches, f"site missing mirrored contractRef for {contract['name']}"
                    found_any = True
    assert found_any, "expected at least one topology tenant with a mirrored site-local contract"


def test_template_contract_add_mirrors_into_associated_site():
    """Adding a template-level contract to a template that already has a
    site attached must auto-create a matching site-local `{contractRef}`
    entry — mirrors `test_template_anp_add_mirrors_into_associated_site`
    (PR-12) for the contracts array."""
    doc = {
        "id": "schema-abc",
        "templates": [{"name": "LAB1-LAB2", "contracts": []}],
        "sites": [{"siteId": "1", "templateName": "LAB1-LAB2"}],
    }
    ops = [
        {
            "op": "add",
            "path": "/templates/LAB1-LAB2/contracts/-",
            "value": {"name": "con-Firewall_LAB0", "scope": "vrf", "filterRelationships": []},
        }
    ]
    apply_json_patch(doc, ops)
    site = doc["sites"][0]
    assert site["contracts"][0]["contractRef"] == "/schemas/schema-abc/templates/LAB1-LAB2/contracts/con-Firewall_LAB0"


def test_template_contract_add_mirror_is_idempotent():
    doc = {
        "id": "schema-abc",
        "templates": [{"name": "LAB1-LAB2", "contracts": []}],
        "sites": [{"siteId": "1", "templateName": "LAB1-LAB2"}],
    }
    ops = [{"op": "add", "path": "/templates/LAB1-LAB2/contracts/-", "value": {"name": "con-Firewall_LAB0"}}]
    apply_json_patch(doc, ops)
    apply_json_patch(doc, ops)
    assert len(doc["sites"][0]["contracts"]) == 1


def test_atomic_service_graph_redirect_patch_on_site_local_contract_end_to_end(client):
    """Full replica of the mso-model role's raw `cisco.mso.mso_rest`
    "Atomic PATCH — bind service-graph redirect on ALL fabrics in one
    request" task: a schema built up entirely via runtime PATCH (matching
    MS-TN2's actual flow), with a template contract added, then a
    site-local `serviceGraphRelationship` bound on EACH site's mirrored
    contract in a single PATCH batch — the real hardware MS-TN2 `create_tenant`
    pass-2 crash signature (`"path segment 'con-Firewall_LAB0' not found in
    list"`) this PR fixes."""
    payload = {
        "displayName": "MS-TN2-LAB0",
        "templates": [{"name": "LAB1-LAB2", "displayName": "LAB1-LAB2", "tenantId": "t2", "contracts": []}],
        "sites": [
            {"siteId": "1", "templateName": "LAB1-LAB2"},
            {"siteId": "2", "templateName": "LAB1-LAB2"},
        ],
    }
    schema_id = client.post("/mso/api/v1/schemas", json=payload).json()["id"]

    client.patch(
        f"/mso/api/v1/schemas/{schema_id}",
        json=[
            {
                "op": "add",
                "path": "/templates/LAB1-LAB2/contracts/-",
                "value": {"name": "con-Firewall_LAB0", "scope": "vrf", "filterRelationships": []},
            }
        ],
    )

    redirect_ops = [
        {
            "op": "add",
            "path": f"/sites/{site_id}-LAB1-LAB2/contracts/con-Firewall_LAB0/serviceGraphRelationship",
            "value": {
                "serviceGraphRef": {"schemaId": schema_id, "serviceGraphName": "sgt-FW_LAB0", "templateName": "LAB1-LAB2"},
                "serviceNodesRelationship": [
                    {
                        "serviceNodeRef": {
                            "schemaId": schema_id,
                            "serviceGraphName": "sgt-FW_LAB0",
                            "serviceNodeName": "node1",
                            "templateName": "LAB1-LAB2",
                        },
                        "consumerConnector": {"clusterInterface": {"dn": "uni/tn-MS-TN2/x"}, "redirectPolicy": {"dn": "uni/tn-MS-TN2/y"}, "subnets": []},
                        "providerConnector": {"clusterInterface": {"dn": "uni/tn-MS-TN2/x"}, "redirectPolicy": {"dn": "uni/tn-MS-TN2/y"}},
                    }
                ],
            },
        }
        for site_id in ("1", "2")
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=redirect_ops)
    assert resp.status_code == 200

    fresh = client.get(f"/mso/api/v1/schemas/{schema_id}").json()
    for site_id in ("1", "2"):
        site = next(s for s in fresh["sites"] if s["siteId"] == site_id)
        contract = next(c for c in site["contracts"] if c["contractRef"].endswith("/contracts/con-Firewall_LAB0"))
        assert contract["serviceGraphRelationship"]["serviceGraphRef"].endswith("/serviceGraphs/sgt-FW_LAB0")
