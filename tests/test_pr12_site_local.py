"""PR-12 — NDO site-local ANP/EPG + static-port schema surface.

Closes the last MS-TN1 gap noted in docs/CONTRACT.md §7a: `bind_epg_to_
static_port` (`cisco.mso.mso_schema_site_anp_epg_staticport`) crashed
client-side with `'NoneType' object has no attribute 'details'` because the
sim never mirrored a template-level ANP/EPG add into the schema's site-local
`sites[].anps[].epgs[]` structure — so `MSOSchema.set_site_anp()`/
`set_site_anp_epg()` always found nothing (`None`) for a site that already
had the template attached, exactly the site-local surface a real NDO 4.x
controller auto-populates when a template ANP/EPG is added to a template
that already has sites associated (verified against `ansible-mso`'s
`plugins/module_utils/schema.py` `set_site_anp()`/`set_site_anp_epg()` and
`plugins/modules/mso_schema_site_anp_epg_staticport.py`'s literal source,
and confirmed end-to-end on a real-hardware multi-site E2E run).

These tests exercise `apply_json_patch`'s new auto-mirror behavior directly
(unit level) and via the full FastAPI PATCH/GET round trip (integration
level), replicating the exact real-playbook request sequence:
  1. `mso_schema_template_anp` PATCHes `add /templates/{t}/anps/-`.
  2. `mso_schema_template_anp_epg` PATCHes `add /templates/{t}/anps/{a}/epgs/-`.
  3. `mso_schema_site_anp_epg_staticport` (having found a non-None site_anp/
     site_anp_epg — the crash never triggers) PATCHes
     `add /sites/{siteId}-{t}/anps/{a}/epgs/{e}/staticPorts/-`.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aci_sim.ndo.app import make_ndo_app
from aci_sim.ndo.model import build_ndo_model
from aci_sim.ndo.patch import apply_json_patch, normalize_site, normalize_template
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
# normalize_template — template-level self-referencing anpRef/epgRef
# ---------------------------------------------------------------------------


def test_normalize_template_backfills_self_referencing_anp_ref():
    """Real NDO stores a self-referencing `anpRef` on every template-level
    ANP object itself — confirmed by `ansible-mso`'s
    `mso_schema_template_anp.py` explicitly stripping it client-side before
    an idempotency comparison (`if "anpRef" in mso.previous: del
    mso.previous["anpRef"]` — dead code unless the server sends one back).
    `MSOSchema.set_site_anp()` keys its site-local lookup off exactly this
    field (`template_anp.details.get("anpRef")`); without it that lookup
    always compares against `None` and never matches, crashing
    `mso_schema_site_anp_epg_staticport.py` (confirmed on a real
    MS-TN1 bind_epg_to_static_port run on ACI hardware before this fix)."""
    tmpl = {"name": "LAB1", "anps": [{"name": "app-Web_LAB1", "epgs": []}]}
    normalize_template(tmpl, "schema-abc")
    assert tmpl["anps"][0]["anpRef"] == "/schemas/schema-abc/templates/LAB1/anps/app-Web_LAB1"


def test_normalize_template_backfills_self_referencing_epg_ref():
    """Same rationale as the ANP case, one level down: `set_site_anp_epg()`
    keys off `template_anp_epg.details.get("epgRef")`."""
    tmpl = {"name": "LAB1", "anps": [{"name": "app-Web_LAB1", "epgs": [{"name": "epg-web"}]}]}
    normalize_template(tmpl, "schema-abc")
    epg = tmpl["anps"][0]["epgs"][0]
    assert epg["epgRef"] == "/schemas/schema-abc/templates/LAB1/anps/app-Web_LAB1/epgs/epg-web"


def test_normalize_template_self_ref_backfill_is_idempotent():
    tmpl = {"name": "LAB1", "anps": [{"name": "app-Web_LAB1", "epgs": [{"name": "epg-web"}]}]}
    normalize_template(tmpl, "schema-abc")
    first_anp_ref = tmpl["anps"][0]["anpRef"]
    first_epg_ref = tmpl["anps"][0]["epgs"][0]["epgRef"]
    normalize_template(tmpl, "schema-abc")
    assert tmpl["anps"][0]["anpRef"] == first_anp_ref
    assert tmpl["anps"][0]["epgs"][0]["epgRef"] == first_epg_ref


# ---------------------------------------------------------------------------
# apply_json_patch — auto-mirror unit tests
# ---------------------------------------------------------------------------


def test_template_anp_add_mirrors_into_associated_site():
    """Adding a template-level ANP to a template that already has a site
    attached must auto-create a matching site-local anp entry (anpRef
    pointing back at the template ANP, epgs starting empty) — real NDO 4.x
    behavior; without it `set_site_anp()` always returns None."""
    doc = {
        "id": "schema-abc",
        "templates": [{"name": "LAB1", "anps": []}],
        "sites": [{"siteId": "1", "templateName": "LAB1"}],
    }
    ops = [
        {
            "op": "add",
            "path": "/templates/LAB1/anps/-",
            "value": {"name": "app-Web_LAB1", "displayName": "app-Web_LAB1", "epgs": []},
        }
    ]
    apply_json_patch(doc, ops)

    site = doc["sites"][0]
    assert site["anps"][0]["anpRef"] == "/schemas/schema-abc/templates/LAB1/anps/app-Web_LAB1"
    assert site["anps"][0]["epgs"] == []


def test_template_anp_add_does_not_mirror_into_unrelated_template_site():
    """A site associated with a DIFFERENT template must be untouched."""
    doc = {
        "id": "schema-abc",
        "templates": [{"name": "LAB1", "anps": []}, {"name": "LAB2", "anps": []}],
        "sites": [
            {"siteId": "1", "templateName": "LAB1"},
            {"siteId": "2", "templateName": "LAB2"},
        ],
    }
    ops = [{"op": "add", "path": "/templates/LAB1/anps/-", "value": {"name": "app-Web_LAB1", "epgs": []}}]
    apply_json_patch(doc, ops)

    assert doc["sites"][0]["anps"][0]["anpRef"].endswith("/anps/app-Web_LAB1")
    assert doc["sites"][1].get("anps", []) == []


def test_template_epg_add_mirrors_into_site_anp_epgs():
    """Adding a template-level EPG under an existing ANP must auto-create a
    matching site-local epg entry under the already-mirrored site-anp
    (epgRef pointing back at the template EPG, staticPorts starting empty)
    — without it `set_site_anp_epg()` always returns None."""
    doc = {
        "id": "schema-abc",
        "templates": [{"name": "LAB1", "anps": [{"name": "app-Web_LAB1", "epgs": []}]}],
        "sites": [{"siteId": "1", "templateName": "LAB1"}],
    }
    apply_json_patch(
        doc,
        [{"op": "add", "path": "/templates/LAB1/anps/-", "value": {"name": "app-Web_LAB1", "epgs": []}}],
    )
    apply_json_patch(
        doc,
        [{"op": "add", "path": "/templates/LAB1/anps/app-Web_LAB1/epgs/-", "value": {"name": "epg-web"}}],
    )

    site_anp = doc["sites"][0]["anps"][0]
    site_epg = site_anp["epgs"][0]
    assert site_epg["epgRef"] == "/schemas/schema-abc/templates/LAB1/anps/app-Web_LAB1/epgs/epg-web"
    # A mirrored site-EPG must carry the FULL child-collection shape every
    # sibling `mso_schema_site_anp_epg_*` module bare-subscripts — a missing
    # key is a hard KeyError, not a 4xx (see SITE_OBJECT_DEFAULTS["epgs"]).
    assert site_epg["staticPorts"] == []
    assert site_epg["subnets"] == []
    assert site_epg["staticLeafs"] == []
    assert site_epg["domainAssociations"] == []


def test_mirror_is_idempotent():
    """Re-running the same template ANP/EPG add ops (e.g. a retried
    playbook task) must not duplicate the mirrored site-local entries."""
    doc = {
        "id": "schema-abc",
        "templates": [{"name": "LAB1", "anps": [{"name": "app-Web_LAB1", "epgs": []}]}],
        "sites": [{"siteId": "1", "templateName": "LAB1"}],
    }
    anp_ops = [{"op": "add", "path": "/templates/LAB1/anps/-", "value": {"name": "app-Web_LAB1", "epgs": []}}]
    epg_ops = [{"op": "add", "path": "/templates/LAB1/anps/app-Web_LAB1/epgs/-", "value": {"name": "epg-web"}}]
    apply_json_patch(doc, anp_ops)
    apply_json_patch(doc, epg_ops)
    apply_json_patch(doc, anp_ops)
    apply_json_patch(doc, epg_ops)

    site = doc["sites"][0]
    assert len(site["anps"]) == 1
    assert len(site["anps"][0]["epgs"]) == 1


def test_site_anp_epg_static_port_add_by_ref_name_resolution():
    """Full replica of the real bind_epg_to_static_port sequence tail: once
    the site-anp/site-epg are mirrored (ref-only, no name), a static port is
    addressed by the ANP/EPG's bare template names —
    `/sites/1-LAB1/anps/app-Web_LAB1/epgs/epg-web/staticPorts/-` — exactly
    what `mso_schema_site_anp_epg_staticport.py` sends once `site_anp`/
    `site_anp_epg` are found (the crash path this PR eliminates)."""
    doc = {
        "id": "schema-abc",
        "templates": [{"name": "LAB1", "anps": [{"name": "app-Web_LAB1", "epgs": [{"name": "epg-web"}]}]}],
        "sites": [{"siteId": "1", "templateName": "LAB1"}],
    }
    apply_json_patch(doc, [{"op": "add", "path": "/templates/LAB1/anps/-", "value": {"name": "app-Web_LAB1", "epgs": []}}])
    apply_json_patch(
        doc, [{"op": "add", "path": "/templates/LAB1/anps/app-Web_LAB1/epgs/-", "value": {"name": "epg-web"}}]
    )

    static_port_ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/anps/app-Web_LAB1/epgs/epg-web/staticPorts/-",
            "value": {
                "path": "topology/pod-1/protpaths-101-102/pathep-[ipg-vpc-LegacySW01]",
                "portEncapVlan": 100,
                "mode": "regular",
                "deploymentImmediacy": "immediate",
                "type": "vpc",
            },
        }
    ]
    apply_json_patch(doc, static_port_ops)

    static_ports = doc["sites"][0]["anps"][0]["epgs"][0]["staticPorts"]
    assert len(static_ports) == 1
    assert static_ports[0]["portEncapVlan"] == 100


def test_site_anp_epg_domain_association_add_by_ref_name_resolution():
    """Full replica of the real bind_epg_to_physical_domain / _vmm_domain
    sequence tail (`mso_schema_site_anp_epg_domain.py`): once the site-anp/
    site-epg are mirrored, a domain association is added at
    `/sites/1-LAB1/anps/app-Web_LAB1/epgs/epg-web/domainAssociations/-`.
    The module reads that array with a BARE subscript
    (`domains = [dom.get("dn") for dom in ...["epgs"][epg_idx]
    ["domainAssociations"]]`), so a missing key is a hard `KeyError:
    'domainAssociations'` — a real MS-TN1 hardware run regressed here after
    PR-12 started auto-creating the site-EPG (pre-PR-12 no site-EPG existed
    so the module took a non-crashing branch)."""
    doc = {
        "id": "schema-abc",
        "templates": [{"name": "LAB1", "anps": [{"name": "app-Web_LAB1", "epgs": [{"name": "epg-web"}]}]}],
        "sites": [{"siteId": "1", "templateName": "LAB1"}],
    }
    apply_json_patch(doc, [{"op": "add", "path": "/templates/LAB1/anps/-", "value": {"name": "app-Web_LAB1", "epgs": []}}])
    apply_json_patch(
        doc, [{"op": "add", "path": "/templates/LAB1/anps/app-Web_LAB1/epgs/-", "value": {"name": "epg-web"}}]
    )

    # The mirrored site-EPG must already carry an (empty) domainAssociations
    # array — the module bare-subscripts it before deciding to append.
    assert doc["sites"][0]["anps"][0]["epgs"][0]["domainAssociations"] == []

    domain_ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/anps/app-Web_LAB1/epgs/epg-web/domainAssociations/-",
            "value": {
                "dn": "uni/phys-phy-general",
                "domainType": "physicalDomain",
                "deployImmediacy": "immediate",
                "resolutionImmediacy": "immediate",
            },
        }
    ]
    apply_json_patch(doc, domain_ops)

    domains = doc["sites"][0]["anps"][0]["epgs"][0]["domainAssociations"]
    assert len(domains) == 1
    assert domains[0]["dn"] == "uni/phys-phy-general"
    assert domains[0]["domainType"] == "physicalDomain"


def test_epg_ref_dict_stringified_to_canonical_two_segment_form():
    """`epgRef` is the one `*Ref` field whose canonical string form nests
    TWO name segments under the template (anps/{anp}/epgs/{epg}), not one —
    confirmed against ansible-mso's module_utils/mso.py `epg_ref()`. A dict
    payload embedding it (as `mso_schema_site_anp_epg_staticport.py`'s
    create-site-anp fallback sends) must be stored in that exact form."""
    doc = {"sites": [{"siteId": "1", "templateName": "LAB1", "anps": []}]}
    ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/anps/-",
            "value": {
                "epgRef": {
                    "schemaId": "schema-abc",
                    "templateName": "LAB1",
                    "anpName": "app-Web_LAB1",
                    "epgName": "epg-web",
                }
            },
        }
    ]
    apply_json_patch(doc, ops)
    assert doc["sites"][0]["anps"][0]["epgRef"] == "/schemas/schema-abc/templates/LAB1/anps/app-Web_LAB1/epgs/epg-web"


def test_normalize_site_backfills_all_epg_child_collections():
    """normalize_site() must backfill the FULL site-EPG child-collection
    shape (subnets/staticPorts/staticLeafs/domainAssociations) on a
    ref-only site-epg — each is bare-subscripted by a sibling
    mso_schema_site_anp_epg_* module."""
    site = {
        "siteId": "1",
        "templateName": "LAB1",
        "anps": [{"anpRef": "/schemas/s/templates/LAB1/anps/app-Web_LAB1", "epgs": [{"epgRef": "/schemas/s/templates/LAB1/anps/app-Web_LAB1/epgs/epg-web"}]}],
    }
    normalize_site(site)
    epg = site["anps"][0]["epgs"][0]
    assert epg["subnets"] == []
    assert epg["staticPorts"] == []
    assert epg["staticLeafs"] == []
    assert epg["domainAssociations"] == []


# ---------------------------------------------------------------------------
# End-to-end FastAPI round trip — full real-playbook sequence
# ---------------------------------------------------------------------------


def test_schema_patch_site_local_anp_epg_staticport_end_to_end(client):
    """Full replica of the real MS-TN1 create_application + bind_epg_to_
    static_port sequence: schema already has a site attached to the
    template (mso_schema_site.py, PR-11's create_tenant flow), then
    mso_schema_template_anp / _anp_epg add the template ANP/EPG (already
    supported since PR-11), and the site-local mirrors must already exist
    by the time mso_schema_site_anp_epg_staticport PATCHes the static port
    — matching real NDO 4.x, where the crash this PR fixes never triggers."""
    payload = {
        "displayName": "MS-TN1-LAB0",
        "templates": [{"name": "LAB1", "displayName": "LAB1", "tenantId": "t1", "anps": []}],
        "sites": [{"siteId": "1", "templateName": "LAB1"}],
    }
    schema_id = client.post("/mso/api/v1/schemas", json=payload).json()["id"]

    anp_ops = [
        {
            "op": "add",
            "path": "/templates/LAB1/anps/-",
            "value": {"name": "app-Web_LAB1", "displayName": "app-Web_LAB1", "epgs": []},
        }
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=anp_ops)
    assert resp.status_code == 200
    site_anp = resp.json()["sites"][0]["anps"][0]
    assert site_anp["anpRef"] == f"/schemas/{schema_id}/templates/LAB1/anps/app-Web_LAB1"

    epg_ops = [
        {
            "op": "add",
            "path": "/templates/LAB1/anps/app-Web_LAB1/epgs/-",
            "value": {"name": "epg-web", "displayName": "epg-web"},
        }
    ]
    resp2 = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=epg_ops)
    assert resp2.status_code == 200
    site_epg = resp2.json()["sites"][0]["anps"][0]["epgs"][0]
    assert site_epg["epgRef"] == f"/schemas/{schema_id}/templates/LAB1/anps/app-Web_LAB1/epgs/epg-web"
    assert site_epg["staticPorts"] == []
    assert site_epg["domainAssociations"] == []

    static_port_ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/anps/app-Web_LAB1/epgs/epg-web/staticPorts/-",
            "value": {
                "path": "topology/pod-1/protpaths-101-102/pathep-[ipg-vpc-LegacySW01]",
                "portEncapVlan": 100,
                "mode": "regular",
                "deploymentImmediacy": "immediate",
                "type": "vpc",
            },
        }
    ]
    resp3 = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=static_port_ops)
    assert resp3.status_code == 200
    static_ports = resp3.json()["sites"][0]["anps"][0]["epgs"][0]["staticPorts"]
    assert len(static_ports) == 1
    assert static_ports[0]["portEncapVlan"] == 100

    # Independent GET after the PATCH must reflect the same mutation
    # (get_site_anp-equivalent lookup: a fresh GET's sites[].anps[].epgs[]
    # must resolve non-None for the static-port module's own lookup chain).
    detail = client.get(f"/mso/api/v1/schemas/{schema_id}").json()
    got_site = detail["sites"][0]
    got_anp = got_site["anps"][0]
    got_epg = got_anp["epgs"][0]
    assert got_anp["anpRef"].endswith("/anps/app-Web_LAB1")
    assert got_epg["epgRef"].endswith("/epgs/epg-web")
    assert got_epg["staticPorts"][0]["portEncapVlan"] == 100


def test_schema_patch_site_local_anp_epg_survives_repeated_get(client):
    """Regression guard: a plain GET (no PATCH in between) must keep
    reporting the mirrored site-anp/site-epg — i.e. normalization isn't a
    PATCH-only side effect that a bare GET-only reader would miss."""
    payload = {
        "displayName": "MS-TN1-LAB0",
        "templates": [{"name": "LAB1", "anps": []}],
        "sites": [{"siteId": "1", "templateName": "LAB1"}],
    }
    schema_id = client.post("/mso/api/v1/schemas", json=payload).json()["id"]
    client.patch(
        f"/mso/api/v1/schemas/{schema_id}",
        json=[{"op": "add", "path": "/templates/LAB1/anps/-", "value": {"name": "app-Web_LAB1", "epgs": []}}],
    )

    first = client.get(f"/mso/api/v1/schemas/{schema_id}").json()
    second = client.get(f"/mso/api/v1/schemas/{schema_id}").json()
    assert first["sites"][0]["anps"][0]["anpRef"] == second["sites"][0]["anps"][0]["anpRef"]


# ---------------------------------------------------------------------------
# build_ndo_model — boot-time seed already has the site-local mirror
# ---------------------------------------------------------------------------


def test_build_ndo_model_seeds_site_local_anp_epg_for_topology_tenants(topo):
    """A topology.yaml tenant that already ships with ANPs/EPGs configured
    (not built up via PATCH at runtime) must boot with its site-local
    sites[].anps[].epgs[] already mirrored too — otherwise a fresh sim
    instance would hit the same set_site_anp()-returns-None crash on its
    very first bind_epg_to_static_port run, before any PATCH ever ran."""
    state = build_ndo_model(topo)

    found_any_site_anp = False
    for detail in state.schema_details.values():
        for tmpl in detail.get("templates", []):
            if not tmpl.get("anps"):
                continue
            # Every template-level anp/epg must carry its self-referencing ref.
            for anp in tmpl["anps"]:
                assert anp.get("anpRef", "").endswith(f"/anps/{anp['name']}")
                for epg in anp.get("epgs", []):
                    assert epg.get("epgRef", "").endswith(f"/epgs/{epg['name']}")

            for site in detail.get("sites", []):
                if site.get("templateName") != tmpl["name"]:
                    continue
                for site_anp in site.get("anps", []):
                    found_any_site_anp = True
                    assert site_anp.get("anpRef")
                    for site_epg in site_anp.get("epgs", []):
                        assert site_epg.get("epgRef")
                        assert site_epg.get("staticPorts") == []
                        assert site_epg.get("domainAssociations") == []

    assert found_any_site_anp, "expected at least one topology tenant with a mirrored site-local anp"
