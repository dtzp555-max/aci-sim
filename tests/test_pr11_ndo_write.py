"""PR-11 — NDO write surface: schema POST/PATCH round-trip, ND user-class
fallback endpoints, bare /api/v1/sites.

These close the gap between PR-10 (site names, schema list-identity) and a
real multi-site aci-ansible E2E run: the tenant-region schema
(`<tenant>-<region>`, e.g. "MS-TN1-LAB0") that create_tenant's MSO play
creates via `mso_schema_template` must round-trip through
list-identity → GET-by-id → PATCH (add template/BD/ANP/EPG ops) → GET-by-id
reflecting the mutation, exactly the sequence cisco.mso's schema-object
modules perform (see docs/CONTRACT.md §7 and aci_sim/ndo/patch.py).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aci_sim.ndo.app import make_ndo_app
from aci_sim.ndo.model import build_ndo_model
from aci_sim.ndo.patch import (
    PatchError,
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
    state = build_ndo_model(topo)
    app = make_ndo_app(state)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# /api/config/class/remoteusers + localusers — ND fallback shape
# ---------------------------------------------------------------------------


def test_remoteusers_class_query_shape(client):
    """Must be a dict with an "items" list — get_user_from_list_of_users()
    (ansible-mso module_utils/mso.py) only iterates users.get("items") when
    given a dict; anything else (404, bare list) sends mso_tenant into the
    "ND Error: Unknown error" abort observed on the real hardware run.
    """
    resp = client.get("/api/config/class/remoteusers")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    assert "items" in body
    assert isinstance(body["items"], list)


def test_localusers_class_query_shape_has_admin(client):
    resp = client.get("/api/config/class/localusers")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    items = body["items"]
    assert len(items) >= 1
    admin = next(i for i in items if i["spec"]["loginID"] == "admin")
    assert admin["spec"].get("id") or admin["spec"].get("userID")


# ---------------------------------------------------------------------------
# bare /api/v1/sites — ndo_template prereq lookup
# ---------------------------------------------------------------------------


def test_bare_sites_endpoint_matches_mso_prefixed(client):
    """cisco.mso.ndo_template's prereq TenantPol lookup hits the BARE
    /api/v1/sites path (no /mso prefix) — confirmed 404 on a real hardware run
    against the previously mso-only route.
    """
    bare = client.get("/api/v1/sites")
    prefixed = client.get("/mso/api/v1/sites")
    assert bare.status_code == 200
    assert bare.json() == prefixed.json()


# ---------------------------------------------------------------------------
# apply_json_patch — unit-level op semantics
# ---------------------------------------------------------------------------


def test_patch_add_append_to_list():
    doc = {"templates": [{"name": "T1", "bds": []}]}
    ops = [{"op": "add", "path": "/templates/T1/bds/-", "value": {"name": "bd1"}}]
    apply_json_patch(doc, ops)
    assert doc["templates"][0]["bds"] == [{"name": "bd1"}]


def test_patch_add_named_template_slot():
    """/templates/-  appends a whole new template dict (mso_schema_template's
    'template does not exist yet' branch)."""
    doc = {"templates": []}
    ops = [{"op": "add", "path": "/templates/-", "value": {"name": "T1", "bds": []}}]
    apply_json_patch(doc, ops)
    assert doc["templates"][0]["name"] == "T1"


def test_patch_replace_by_name_resolution():
    doc = {"templates": [{"name": "T1", "displayName": "old"}]}
    ops = [{"op": "replace", "path": "/templates/T1/displayName", "value": "new"}]
    apply_json_patch(doc, ops)
    assert doc["templates"][0]["displayName"] == "new"


def test_patch_remove_by_name_resolution():
    doc = {"templates": [{"name": "T1", "bds": [{"name": "bd1"}, {"name": "bd2"}]}]}
    ops = [{"op": "remove", "path": "/templates/T1/bds/bd1"}]
    apply_json_patch(doc, ops)
    assert [b["name"] for b in doc["templates"][0]["bds"]] == ["bd2"]


def test_patch_stringifies_ref_dicts_on_add():
    """A `*Ref` field sent as a dict (schemaId/templateName/xName) must be
    persisted as NDO's canonical string ref — real NDO's storage shape,
    confirmed by a sibling module (custom_mso_schema_site_bd_subnet.py)
    that string-compares/joins bdRef values and crashes on a dict."""
    doc = {"sites": [{"siteId": "1", "templateName": "LAB1", "bds": []}]}
    ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/bds/-",
            "value": {"bdRef": {"schemaId": "schema-abc", "templateName": "LAB1", "bdName": "bd1"}, "hostBasedRouting": False},
        }
    ]
    apply_json_patch(doc, ops)
    assert doc["sites"][0]["bds"][0]["bdRef"] == "/schemas/schema-abc/templates/LAB1/bds/bd1"


def test_patch_leaves_already_string_refs_untouched():
    doc = {"templates": [{"name": "T1", "bds": []}]}
    ops = [{"op": "add", "path": "/templates/T1/bds/-", "value": {"name": "bd1", "vrfRef": "/vrfs/vrf1"}}]
    apply_json_patch(doc, ops)
    assert doc["templates"][0]["bds"][0]["vrfRef"] == "/vrfs/vrf1"


def test_patch_add_autovivifies_missing_collection_as_list():
    """Regression guard for a real bug this suite caught: if an
    intermediate collection key is missing when a PATCH targets it with
    "/-" (append), the resolver must create a LIST, not a dict — a dict
    turns the "-" append marker into a literal key '-': {'-': value},
    silently corrupting the document instead of appending."""
    doc = {"sites": [{"siteId": "1", "templateName": "LAB1"}]}  # no "bds" key at all
    ops = [{"op": "add", "path": "/sites/1-LAB1/bds/-", "value": {"name": "bd1"}}]
    apply_json_patch(doc, ops)
    assert doc["sites"][0]["bds"] == [{"name": "bd1"}]


def test_patch_unresolvable_path_raises():
    doc = {"templates": []}
    with pytest.raises(PatchError):
        apply_json_patch(doc, [{"op": "replace", "path": "/templates/NoSuchTmpl/name", "value": "x"}])


# ---------------------------------------------------------------------------
# normalize_template — server-side collection-key defaulting
# ---------------------------------------------------------------------------


def test_normalize_template_backfills_bare_create_payload():
    """mso_schema_template.py's create payload is only {name, displayName,
    tenantId} — every other collection key (vrfs/bds/anps/.../
    intersiteL3outs/serviceGraphs) must come back as [] or MSOSchema's
    `set_template_vrf()` etc. crash with 'NoneType' object is not iterable
    (observed on a real hardware run's Create VRF task)."""
    tmpl = {"name": "LAB1-LAB2", "displayName": "LAB1-LAB2", "tenantId": "abc"}
    normalize_template(tmpl)
    for key in ("vrfs", "bds", "anps", "contracts", "filters", "externalEpgs", "intersiteL3outs", "serviceGraphs"):
        assert tmpl[key] == [], f"{key} not defaulted to []"
    assert tmpl["templateID"], "templateID must be assigned"


def test_normalize_template_backfills_external_epg_subnets():
    """mso_schema_template_external_epg.py's add payload never sets
    'subnets' — mso_schema_template_external_epg_subnet.py subscripts it
    bare (`externalEpgs[idx]["subnets"]`), a real hardware run hit
    KeyError: 'subnets' on exactly this path."""
    tmpl = {
        "name": "LAB1-LAB2",
        "externalEpgs": [{"name": "xepg-All", "vrfRef": "/vrfs/v1", "l3outRef": "/l3outs/l1"}],
    }
    normalize_template(tmpl)
    assert tmpl["externalEpgs"][0]["subnets"] == []
    assert tmpl["externalEpgs"][0]["contractRelationships"] == []


def test_normalize_template_backfills_nested_epg_under_anp():
    tmpl = {"name": "T1", "anps": [{"name": "AP1", "epgs": [{"name": "web"}]}]}
    normalize_template(tmpl)
    epg = tmpl["anps"][0]["epgs"][0]
    assert epg["subnets"] == []
    assert epg["contractRelationships"] == []
    assert epg["staticPorts"] == []


def test_patch_add_site_bd_by_composite_key():
    """mso_schema_site_bd.py addresses the top-level `sites[]` array by a
    synthetic "{siteId}-{templateName}" composite key, not by name/
    displayName — confirmed on a real hardware create_bd run that hit
    "path segment '1-LAB1' not found in list" before this resolution mode
    was added (site_template = "{siteId}-{templateName}").
    """
    doc = {
        "sites": [
            {"siteId": "1", "templateName": "LAB1", "bds": []},
            {"siteId": "2", "templateName": "LAB1", "bds": []},
        ]
    }
    ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/bds/-",
            "value": {"bdRef": {"bdName": "bd-App1_LAB1"}, "hostBasedRouting": False},
        }
    ]
    apply_json_patch(doc, ops)
    assert doc["sites"][0]["bds"] == [{"bdRef": {"bdName": "bd-App1_LAB1"}, "hostBasedRouting": False}]
    assert doc["sites"][1]["bds"] == []


def test_patch_add_site_bd_subnet_by_ref_name_resolution():
    """Site-local bds[] entries carry no `name` of their own — only a
    bdRef string — yet mso_schema_site_bd_subnet.py addresses them by the
    referenced BD's bare name: `/sites/1-LAB1/bds/bd-App1_LAB1/subnets/-`.
    A real hardware run 400'd with "path segment 'bd-App1_LAB1' not found in
    list" before ref-name resolution was added.
    """
    doc = {
        "sites": [
            {
                "siteId": "1",
                "templateName": "LAB1",
                "bds": [
                    {
                        "bdRef": "/schemas/schema-abc/templates/LAB1/bds/bd-App1_LAB1",
                        "hostBasedRouting": False,
                        "subnets": [],
                    }
                ],
            }
        ]
    }
    ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/bds/bd-App1_LAB1/subnets/-",
            "value": {"ip": "192.168.41.1/24", "scope": "public"},
        }
    ]
    apply_json_patch(doc, ops)
    assert doc["sites"][0]["bds"][0]["subnets"] == [{"ip": "192.168.41.1/24", "scope": "public"}]


def test_normalize_site_backfills_bd_subnets():
    """mso_schema_site_bd.py's add payload is {bdRef, hostBasedRouting} — no
    subnets — but mso_schema_site_bd_subnet.py reads
    site_bd.details.get("subnets") and iterates it."""
    site = {"siteId": "1", "templateName": "LAB1", "bds": [{"bdRef": {}, "hostBasedRouting": False}]}
    normalize_site(site)
    assert site["bds"][0]["subnets"] == []


def test_normalize_template_idempotent():
    tmpl = {"name": "T1"}
    normalize_template(tmpl)
    first_id = tmpl["templateID"]
    normalize_template(tmpl)
    assert tmpl["templateID"] == first_id
    assert tmpl["bds"] == []


# ---------------------------------------------------------------------------
# End-to-end schema write round trip via the FastAPI routes
# ---------------------------------------------------------------------------


def test_schema_create_list_identity_get_roundtrip(client):
    """POST creates a schema whose displayName is resolvable via
    list-identity and whose full doc is servable by GET /schemas/{id} —
    mso_schema_template.py's flow when a tenant-region schema doesn't exist
    yet (schema_path = "schemas"; POST {displayName, templates:[...], sites:[]}).
    """
    payload = {
        "displayName": "MS-TN1-LAB0",
        "templates": [
            {"name": "LAB1-LAB2", "displayName": "LAB1-LAB2", "tenantId": "abc123", "bds": [], "anps": []}
        ],
        "sites": [],
    }
    created = client.post("/mso/api/v1/schemas", json=payload)
    assert created.status_code == 200
    schema_id = created.json()["id"]

    identity = client.get("/mso/api/v1/schemas/list-identity").json()["schemas"]
    match = [s for s in identity if s["displayName"] == "MS-TN1-LAB0"]
    assert len(match) == 1, f"Expected exactly one list-identity match, got {match}"
    assert match[0]["id"] == schema_id

    detail = client.get(f"/mso/api/v1/schemas/{schema_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["templates"][0]["name"] == "LAB1-LAB2"


def test_schema_patch_add_bd_then_get_reflects_it(client):
    """PATCH adds a BD to an existing template; a subsequent GET must show
    it — the mso_schema_template_bd.py 'BD does not exist, add it' flow:
    ops = [{"op":"add","path":"/templates/{tmpl}/bds/-","value":{...}}].
    """
    payload = {
        "displayName": "MS-TN2-LAB0",
        "templates": [{"name": "LAB1", "displayName": "LAB1", "tenantId": "xyz", "bds": [], "anps": []}],
        "sites": [],
    }
    schema_id = client.post("/mso/api/v1/schemas", json=payload).json()["id"]

    bd_ops = [
        {
            "op": "add",
            "path": "/templates/LAB1/bds/-",
            "value": {"name": "bd-App1_LAB1", "vrfRef": "/vrfs/vrf-L3_LAB0", "subnets": []},
        }
    ]
    patched = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=bd_ops)
    assert patched.status_code == 200
    assert patched.json()["templates"][0]["bds"][0]["name"] == "bd-App1_LAB1"

    # Independent GET after the PATCH must reflect the same mutation.
    detail = client.get(f"/mso/api/v1/schemas/{schema_id}").json()
    bd_names = {b["name"] for b in detail["templates"][0]["bds"]}
    assert "bd-App1_LAB1" in bd_names


def test_schema_patch_add_template_to_existing_schema(client):
    """mso_schema_template.py's 'schema exists, template doesn't' branch:
    PATCH ops = [{"op":"add","path":"/templates/-","value":{...}}]."""
    payload = {"displayName": "MS-Multi-Schema", "templates": [], "sites": []}
    schema_id = client.post("/mso/api/v1/schemas", json=payload).json()["id"]

    ops = [{"op": "add", "path": "/templates/-", "value": {"name": "LAB2", "displayName": "LAB2", "tenantId": "t1"}}]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=ops)
    assert resp.status_code == 200
    assert any(t["name"] == "LAB2" for t in resp.json()["templates"])

    # list-identity / GET /schemas summary must reflect the new template too.
    schemas = client.get("/mso/api/v1/schemas").json()["schemas"]
    summary = next(s for s in schemas if s["id"] == schema_id)
    assert {t["name"] for t in summary["templates"]} == {"LAB2"}


def test_schema_patch_external_epg_then_subnet_add(client):
    """End-to-end replica of the real playbook sequence: add an externalEpg
    (mso_schema_template_external_epg.py, no subnets key in its payload),
    then add a subnet to it (mso_schema_template_external_epg_subnet.py,
    which bare-subscripts externalEpgs[idx]["subnets"])."""
    payload = {
        "displayName": "MS-TN1-LAB0",
        "templates": [{"name": "LAB1-LAB2", "displayName": "LAB1-LAB2", "tenantId": "t1"}],
        "sites": [],
    }
    schema_id = client.post("/mso/api/v1/schemas", json=payload).json()["id"]

    add_epg_ops = [
        {
            "op": "add",
            "path": "/templates/LAB1-LAB2/externalEpgs/-",
            "value": {"name": "xepg-All_LAB0", "vrfRef": "/vrfs/vrf-L3_LAB0", "l3outRef": "/l3outs/l3o-bgp-Core_LAB0"},
        }
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=add_epg_ops)
    assert resp.status_code == 200
    assert resp.json()["templates"][0]["externalEpgs"][0]["subnets"] == []

    add_subnet_ops = [
        {
            "op": "add",
            "path": "/templates/LAB1-LAB2/externalEpgs/xepg-All_LAB0/subnets/-",
            "value": {"ip": "0.0.0.0/0"},
        }
    ]
    resp2 = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=add_subnet_ops)
    assert resp2.status_code == 200
    subnets = resp2.json()["templates"][0]["externalEpgs"][0]["subnets"]
    assert subnets == [{"ip": "0.0.0.0/0"}]


def test_schema_validate_route(client):
    """mso_schema_template_deploy.py calls GET schemas/{id}/validate before
    every deploy on ND-platform NDO — a real hardware run 404'd here because
    the route was entirely missing."""
    payload = {"displayName": "MS-Validate-Check", "templates": [], "sites": []}
    schema_id = client.post("/mso/api/v1/schemas", json=payload).json()["id"]
    resp = client.get(f"/mso/api/v1/schemas/{schema_id}/validate")
    assert resp.status_code == 200


def test_schema_validate_unknown_id_404(client):
    resp = client.get("/mso/api/v1/schemas/does-not-exist/validate")
    assert resp.status_code == 404


def test_execute_and_status_schema_template(client):
    """mso_schema_template_deploy.py's actual deploy/status request:
    GET execute|status/schema/{id}/template/{name} — response is splatted
    into mso.exit_json(**status), so it must be a JSON object."""
    payload = {"displayName": "MS-Deploy-Check", "templates": [{"name": "LAB1"}], "sites": []}
    schema_id = client.post("/mso/api/v1/schemas", json=payload).json()["id"]

    execute_resp = client.get(f"/mso/api/v1/execute/schema/{schema_id}/template/LAB1")
    assert execute_resp.status_code == 200
    assert isinstance(execute_resp.json(), dict)

    status_resp = client.get(f"/mso/api/v1/status/schema/{schema_id}/template/LAB1")
    assert status_resp.status_code == 200
    assert isinstance(status_resp.json(), dict)


def test_schema_patch_site_local_bd_end_to_end(client):
    """Full replica of the real create_bd MS-TN1 sequence: schema already
    has a `sites[]` entry (from mso_schema_site.py's earlier "Add site to
    template" run), then mso_schema_site_bd.py PATCHes a site-local BD by
    composite key. A real hardware run 400'd with "path segment '1-LAB1' not
    found in list" before the composite-key resolver was added.

    The write payload's `bdRef` is a DICT (schemaId/templateName/bdName —
    what mso_schema_site_bd.py actually sends), but the server must store
    and serve it back as the canonical STRING ref
    `/schemas/{id}/templates/{tmpl}/bds/{name}` — a sibling module
    (custom_mso_schema_site_bd_subnet.py) does `bd_ref_string in [v.get(
    'bdRef') for v in ...]` and `', '.join(bds)` on a failed match; a dict
    stored verbatim crashes that join with "sequence item 0: expected str
    instance, dict found" (confirmed on a real hardware "Add site-local subnet
    to BD" run).
    """
    payload = {
        "displayName": "MS-TN1-LAB0",
        "templates": [{"name": "LAB1", "displayName": "LAB1", "tenantId": "t1", "bds": [{"name": "bd-App1_LAB1"}]}],
        "sites": [{"siteId": "1", "templateName": "LAB1"}],
    }
    schema_id = client.post("/mso/api/v1/schemas", json=payload).json()["id"]

    ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/bds/-",
            "value": {
                "bdRef": {"schemaId": schema_id, "templateName": "LAB1", "bdName": "bd-App1_LAB1"},
                "hostBasedRouting": False,
            },
        }
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=ops)
    assert resp.status_code == 200
    site_entry = resp.json()["sites"][0]
    assert site_entry["bds"][0]["bdRef"] == f"/schemas/{schema_id}/templates/LAB1/bds/bd-App1_LAB1"
    assert site_entry["bds"][0]["subnets"] == []


def test_schema_patch_site_local_bd_subnet_end_to_end(client):
    """create_bd's full MS-TN1 sequence tail: after the site-local BD exists
    (bdRef-only, no name), add a subnet to it addressed by the BD's bare
    name — real NDO/ACI-hardware request shape."""
    schema_id = client.post(
        "/mso/api/v1/schemas",
        json={
            "displayName": "MS-TN1-LAB0",
            "templates": [{"name": "LAB1", "bds": [{"name": "bd-App1_LAB1"}]}],
            "sites": [{"siteId": "1", "templateName": "LAB1"}],
        },
    ).json()["id"]

    add_bd_ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/bds/-",
            "value": {
                "bdRef": {"schemaId": schema_id, "templateName": "LAB1", "bdName": "bd-App1_LAB1"},
                "hostBasedRouting": False,
            },
        }
    ]
    client.patch(f"/mso/api/v1/schemas/{schema_id}", json=add_bd_ops)

    add_subnet_ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/bds/bd-App1_LAB1/subnets/-",
            "value": {"ip": "192.168.41.1/24", "scope": "public"},
        }
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=add_subnet_ops)
    assert resp.status_code == 200
    site_bd = resp.json()["sites"][0]["bds"][0]
    assert site_bd["subnets"] == [{"ip": "192.168.41.1/24", "scope": "public"}]


def test_post_task_deploy(client):
    """cisco.mso.ndo_schema_template_deploy — the module aci-ansible's
    mso-model role actually invokes — sends deploy/redeploy as
    POST /mso/api/v1/task {schemaId, templateName, isRedeploy}. A real
    real hardware run 404'd here; confirmed against the collection installed there
    (differs from the legacy mso_schema_template_deploy.py's execute/status
    GET routes, which are exercised separately above)."""
    payload = {"displayName": "MS-Task-Deploy-Check", "templates": [{"name": "LAB1"}], "sites": []}
    schema_id = client.post("/mso/api/v1/schemas", json=payload).json()["id"]

    resp = client.post(
        "/mso/api/v1/task",
        json={"schemaId": schema_id, "templateName": "LAB1", "isRedeploy": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["schemaId"] == schema_id
    assert body["templateName"] == "LAB1"


def test_post_task_unknown_schema_404(client):
    resp = client.post(
        "/mso/api/v1/task",
        json={"schemaId": "does-not-exist", "templateName": "LAB1", "isRedeploy": False},
    )
    assert resp.status_code == 404


def test_schema_patch_unknown_id_404(client):
    resp = client.patch("/mso/api/v1/schemas/does-not-exist", json=[{"op": "add", "path": "/x", "value": 1}])
    assert resp.status_code == 404


def test_schema_patch_no_double_listing_after_mutation(client):
    """Regression guard: patching a schema must not create a duplicate
    summary entry in GET /schemas (seeded-schema id collision check)."""
    payload = {"displayName": "MS-Dup-Check", "templates": [], "sites": []}
    schema_id = client.post("/mso/api/v1/schemas", json=payload).json()["id"]
    client.patch(
        f"/mso/api/v1/schemas/{schema_id}",
        json=[{"op": "add", "path": "/templates/-", "value": {"name": "T1"}}],
    )
    schemas = client.get("/mso/api/v1/schemas").json()["schemas"]
    matching = [s for s in schemas if s["id"] == schema_id]
    assert len(matching) == 1, f"Expected exactly one entry, found {len(matching)}"
