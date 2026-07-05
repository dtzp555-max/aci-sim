"""PR-15 — gaps exposed by a real `aci-py` (pure-Python Ansible-to-ACI
compiler) run against the sandbox sim on real ACI hardware.

GAP 1 — APIC login rejects a domain-qualified login name. `aci-py` posts
`POST /api/aaaLogin.json` with `aaaUser.attributes.name =
"apic:local\\admin"` — a valid real-APIC login format
(`apic:<loginDomain>\\<username>`). The sim's `aaaLogin` handler
(`aci_sim/rest_aci/auth.py`) exact-matched the raw `name` attribute
against `SIM_USERNAME`, so every domain-qualified login 401'd even with the
correct password — confirmed live against the hardware sandbox
(`name="apic:local\\admin"` -> 401, `name="admin"` -> 200 before this fix).
This PR strips an optional `apic:<domain>\\` / `<domain>\\` prefix before
comparing the bare username (`_bare_username()`), so both forms authenticate
identically; a wrong username in EITHER form still 401s.

GAP 2 — NDO schema PATCH 400 mid `create_tenant`/`create_bd`. `aci-py`'s
`mso_schema_site_bd` shim (`shims/mso_mso_bd_vrf.py`) always PATCHes a
site-local BD shadow with `op: replace` (never `add`), because real NDO 4.x
auto-creates that shadow (the "BDDelta") the moment a template-level BD is
added to a template that already has sites attached — a fresh `add` there
409s on real hardware ("Multiple BDDelta entries"), so the shim's own
docstring documents doing a GET-then-replace instead. The sim never mirrored
a template BD `add` into `sites[].bds[]` (unlike ANPs/EPGs since PR-12 and
contracts since PR-13 — the exact same "auto-mirror on template add" pattern,
just missing one collection), so the site-BD `replace` 400'd with
`"replace: 'bd-FW_LAB0' not found at
'/sites/1-LAB1-LAB2/bds/bd-FW_LAB0'"` — confirmed verbatim against a real
hardware aci-py MS-TN2 `create_tenant`/`create_bd` run before this fix (the
sim's own log + the NdoError body). This PR adds
`_mirror_template_bd_to_sites` (PATCH-time, parallel to
`_mirror_template_anp_to_sites`/`_mirror_template_contract_to_sites`) plus
the equivalent boot-time mirror in `build_ndo_model`, closing this fully —
a real hardware MS-TN2 run now reaches 13/13 steps rc=0 (was 9 ok / 4 fail
before this PR; the 4 failures were the `replace: 'bd-*' not found` 400 in
`create_tenant` pass1, `create_bd`, `create_tenant` pass2, and
`migrate_existing_vlan_gateway`), and MS-TN1 stays 11/11 rc=0 (1 skip, no
regression).

VERDICT: both gaps are genuine SIM FIDELITY gaps, not aci-py bugs — aci-py's
domain-qualified login name and its GET-then-replace site-BD shim both mirror
documented real-APIC/real-NDO behavior (the latter cited verbatim in aci-py's
own shim docstring, matching the exact rationale this codebase already
accepted for ANPs/EPGs/contracts in PR-11/12/13).
"""
from __future__ import annotations

import copy
from pathlib import Path

from fastapi.testclient import TestClient

from aci_sim.build.orchestrator import build_site
from aci_sim.ndo.app import make_ndo_app
from aci_sim.ndo.model import build_ndo_model
from aci_sim.ndo.patch import apply_json_patch
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology

TOPOLOGY_YAML = Path(__file__).parent.parent / "topology.yaml"


# ---------------------------------------------------------------------------
# GAP 1 — domain-qualified aaaLogin name
# ---------------------------------------------------------------------------


def _apic_client() -> TestClient:
    topo = load_topology(TOPOLOGY_YAML)
    site = topo.sites[0]
    store = build_site(topo, site)
    state = ApicSiteState(name=site.name, site=site, topo=topo, store=store,
                          baseline=copy.deepcopy(store))
    return TestClient(make_apic_app(state))


def _login(client: TestClient, name: str, pwd: str = "cisco"):
    return client.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": name, "pwd": pwd}}},
    )


def test_plain_username_login_still_works():
    c = _apic_client()
    resp = _login(c, "admin")
    assert resp.status_code == 200
    assert "APIC-cookie" in resp.cookies


def test_domain_qualified_apic_prefixed_login_works():
    """`apic:local\\admin` — the exact shape aci-py's APIC connector sends."""
    c = _apic_client()
    resp = _login(c, "apic:local\\admin")
    assert resp.status_code == 200
    data = resp.json()
    token = data["imdata"][0]["aaaLogin"]["attributes"]["token"]
    assert token
    assert "APIC-cookie" in resp.cookies


def test_domain_qualified_bare_domain_login_works():
    """`local\\admin` (no `apic:` scheme prefix) — the other real-APIC shape."""
    c = _apic_client()
    resp = _login(c, "local\\admin")
    assert resp.status_code == 200
    assert "APIC-cookie" in resp.cookies


def test_domain_qualified_wrong_user_still_401s():
    c = _apic_client()
    resp = _login(c, "apic:local\\bob")
    assert resp.status_code == 401
    data = resp.json()
    assert "imdata" in data
    assert data["imdata"][0]["error"]["attributes"]["code"] == "401"


def test_domain_qualified_correct_user_wrong_password_still_401s():
    c = _apic_client()
    resp = _login(c, "apic:local\\admin", pwd="wrong")
    assert resp.status_code == 401


def test_domain_qualified_login_session_authenticates_subsequent_queries():
    """The minted session must work exactly like a plain-username session —
    no lingering domain-qualified string leaking into the session's user."""
    c = _apic_client()
    resp = _login(c, "apic:local\\admin")
    assert resp.status_code == 200
    q = c.get("/api/class/fvTenant.json")
    assert q.status_code == 200


# ---------------------------------------------------------------------------
# GAP 2 — template BD add mirrors into every associated site (PATCH-time)
# ---------------------------------------------------------------------------


def test_template_bd_add_mirrors_into_associated_site():
    """Adding a template-level BD to a template that already has a site
    attached must auto-create a matching site-local BD shadow — mirrors
    `test_template_contract_add_mirrors_into_associated_site` (PR-13) /
    `test_template_anp_add_mirrors_into_associated_site` (PR-12) for bds[]."""
    doc = {
        "id": "schema-abc",
        "templates": [{"name": "LAB1-LAB2", "bds": []}],
        "sites": [{"siteId": "1", "templateName": "LAB1-LAB2"}],
    }
    ops = [
        {
            "op": "add",
            "path": "/templates/LAB1-LAB2/bds/-",
            "value": {"name": "bd-FW_LAB0", "displayName": "FW_LAB0"},
        }
    ]
    apply_json_patch(doc, ops)
    site = doc["sites"][0]
    assert len(site["bds"]) == 1
    mirrored = site["bds"][0]
    assert mirrored["bdRef"] == "/schemas/schema-abc/templates/LAB1-LAB2/bds/bd-FW_LAB0"
    # Real NDO's auto-created shadow defaults hostBasedRouting=False and
    # carries the full child-collection shape (mso_schema_site_bd_subnet.py
    # bare-subscripts `subnets`).
    assert mirrored["hostBasedRouting"] is False
    assert mirrored["subnets"] == []


def test_template_bd_add_mirror_is_idempotent():
    doc = {
        "id": "schema-abc",
        "templates": [{"name": "LAB1-LAB2", "bds": []}],
        "sites": [{"siteId": "1", "templateName": "LAB1-LAB2"}],
    }
    ops = [{"op": "add", "path": "/templates/LAB1-LAB2/bds/-", "value": {"name": "bd-FW_LAB0"}}]
    apply_json_patch(doc, ops)
    apply_json_patch(doc, ops)
    assert len(doc["sites"][0]["bds"]) == 1


def test_template_bd_add_mirrors_into_multiple_sites():
    doc = {
        "id": "schema-abc",
        "templates": [{"name": "LAB1-LAB2", "bds": []}],
        "sites": [
            {"siteId": "1", "templateName": "LAB1-LAB2"},
            {"siteId": "2", "templateName": "LAB1-LAB2"},
        ],
    }
    ops = [{"op": "add", "path": "/templates/LAB1-LAB2/bds/-", "value": {"name": "bd-App1_LAB0"}}]
    apply_json_patch(doc, ops)
    for site in doc["sites"]:
        assert len(site["bds"]) == 1
        assert site["bds"][0]["bdRef"].endswith("/bds/bd-App1_LAB0")


def test_template_bd_add_does_not_mirror_into_unassociated_site():
    doc = {
        "id": "schema-abc",
        "templates": [{"name": "LAB1-LAB2", "bds": []}, {"name": "OtherTpl", "bds": []}],
        "sites": [{"siteId": "1", "templateName": "OtherTpl"}],
    }
    ops = [{"op": "add", "path": "/templates/LAB1-LAB2/bds/-", "value": {"name": "bd-FW_LAB0"}}]
    apply_json_patch(doc, ops)
    assert doc["sites"][0].get("bds", []) == []


def test_site_bd_replace_succeeds_after_template_bd_add():
    """The end-to-end sequence aci-py actually emits: template BD `add`
    (mso_schema_template_bd) followed by a site-local BD `replace`
    (mso_schema_site_bd) in a LATER, separate PATCH call — exactly
    reproducing the real hardware 400 this PR fixes, then proving it is gone."""
    doc = {
        "id": "schema-abc",
        "templates": [{"name": "LAB1-LAB2", "bds": []}],
        "sites": [{"siteId": "1", "templateName": "LAB1-LAB2"}],
    }
    add_ops = [
        {
            "op": "add",
            "path": "/templates/LAB1-LAB2/bds/-",
            "value": {"name": "bd-FW_LAB0", "displayName": "FW_LAB0"},
        }
    ]
    apply_json_patch(doc, add_ops)

    # Site-BD replace, exactly as `mso_schema_site_bd.py`/aci-py's shim sends
    # it: {"op": "replace", "path": "/sites/{siteId}-{tpl}/bds/{bd}", ...}.
    replace_ops = [
        {
            "op": "replace",
            "path": "/sites/1-LAB1-LAB2/bds/bd-FW_LAB0",
            "value": {
                "bdRef": {
                    "schemaId": "schema-abc",
                    "templateName": "LAB1-LAB2",
                    "bdName": "bd-FW_LAB0",
                },
                "hostBasedRouting": False,
            },
        }
    ]
    # Before this PR: PatchError("replace: 'bd-FW_LAB0' not found at
    # '/sites/1-LAB1-LAB2/bds/bd-FW_LAB0'") — the site shadow never existed.
    apply_json_patch(doc, replace_ops)
    site = doc["sites"][0]
    assert len(site["bds"]) == 1
    assert site["bds"][0]["bdRef"] == "/schemas/schema-abc/templates/LAB1-LAB2/bds/bd-FW_LAB0"


# ---------------------------------------------------------------------------
# GAP 2 — boot-time mirror (topology.yaml tenants that ship with BDs already)
# ---------------------------------------------------------------------------


def test_build_ndo_model_seeds_site_local_bds_for_topology_tenants():
    """A topology.yaml tenant that already ships with BDs configured must
    boot with its site-local `sites[].bds[]` already mirrored — otherwise
    the FIRST site-BD PATCH (`replace`) against a freshly-booted sim would
    hit the same 'not found in list' 400 this PR fixes for the PATCH-built
    case. Mirrors `test_build_ndo_model_seeds_site_local_contracts_for_topology_tenants`
    (PR-13)."""
    topo = load_topology(TOPOLOGY_YAML)
    state = build_ndo_model(topo, sandbox=True)
    found_any = False
    for detail in state.schema_details.values():
        for tmpl in detail.get("templates", []):
            if not tmpl.get("bds"):
                continue
            for site in detail.get("sites", []):
                if site.get("templateName") != tmpl["name"]:
                    continue
                assert "bds" in site
                for bd in tmpl["bds"]:
                    matches = [b for b in site["bds"] if b.get("bdRef", "").endswith(f"/bds/{bd['name']}")]
                    assert matches, f"site missing mirrored bdRef for {bd['name']}"
                    found_any = True
    assert found_any, "expected at least one topology tenant with a mirrored site-local BD"


def test_boot_seeded_site_bd_replace_end_to_end():
    """Full HTTP round trip: boot the NDO app from topology.yaml, find a
    schema/template/site that already has a BD, then PATCH a `replace` on
    the site-local shadow exactly like `mso_schema_site_bd.py` would on a
    re-run — must succeed against the BOOT-seeded schema, not just one built
    up entirely via runtime PATCH ops."""
    topo = load_topology(TOPOLOGY_YAML)
    state = build_ndo_model(topo, sandbox=True)
    app = make_ndo_app(state)
    with TestClient(app) as client:
        schema_id, tmpl_name, site_id, bd_name = None, None, None, None
        for sid, detail in state.schema_details.items():
            for tmpl in detail.get("templates", []):
                if not tmpl.get("bds"):
                    continue
                for site in detail.get("sites", []):
                    if site.get("templateName") == tmpl["name"] and site.get("bds"):
                        schema_id = sid
                        tmpl_name = tmpl["name"]
                        site_id = site["siteId"]
                        bd_name = tmpl["bds"][0]["name"]
                        break
                if schema_id:
                    break
            if schema_id:
                break
        assert schema_id is not None, "expected a boot-seeded schema/template/site with a BD"

        site_seg = f"{site_id}-{tmpl_name}"
        replace_ops = [
            {
                "op": "replace",
                "path": f"/sites/{site_seg}/bds/{bd_name}",
                "value": {
                    "bdRef": {"schemaId": schema_id, "templateName": tmpl_name, "bdName": bd_name},
                    "hostBasedRouting": False,
                },
            }
        ]
        resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=replace_ops)
        assert resp.status_code == 200, resp.text

        fresh = client.get(f"/mso/api/v1/schemas/{schema_id}").json()
        site = next(s for s in fresh["sites"] if s.get("templateName") == tmpl_name and s.get("siteId") == site_id)
        matches = [b for b in site["bds"] if b.get("bdRef", "").endswith(f"/bds/{bd_name}")]
        assert matches
