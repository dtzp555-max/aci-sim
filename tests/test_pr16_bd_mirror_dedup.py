"""PR-16 — NDO site-local BD duplication (bdRef normalization).

CONFIRMED PRODUCTION SYMPTOM: after deploying the Acme App1-Net tenant through
`create_tenant.yml` + `create_bd.yml` against a real NDO schema
(`App1-Net-LAB0`), `sites[0].bds` carried TWO entries for the same template
BD `App1-Net_VLAN10` — one empty mirror
(`bdRef=".../templates/LAB1/bds/App1-Net_VLAN10"`, `subnets=[]`)
created by `_mirror_template_bd_to_sites` (PR-15) when the template BD was
added, and a SECOND entry carrying the real subnet
(`10.10.10.1/24`) added later by `mso_schema_site_bd`'s own
`add /sites/{siteId}-{tmpl}/bds/-` (append) payload — confirmed by the
pre-existing `test_patch_add_site_bd_by_composite_key` (test_pr11_ndo_write.py)
to use the DICT bdRef form (`{bdName: ...}`, no `schemaId`/`templateName`),
a form `_find_by_name`'s ref-lookup never matched because the append (`-`)
branch of `apply_json_patch` didn't consult `_find_by_name`/ref-resolution
AT ALL before appending — it always appended unconditionally.

ROOT CAUSE: `apply_json_patch`'s `op == "add"` branch has three
sub-branches keyed by the *last path token* — `"-"` (append),
a numeric index (`insert`), or a name (resolved via `_find_by_name`, which
DOES already dedup by matching a `*Ref` field's bare trailing name). Only
the named-segment sub-branch deduped. The append (`"-"`) sub-branch — the
exact form `mso_schema_site_bd.py`/`mso_schema_site_anp.py`/
`mso_schema_template_contract_filter.py`'s "does not exist yet" branches
all use when addressing a site-local `bds`/`anps`/`contracts` collection —
never checked for an existing entry, so a template-add mirror followed by
a site-module add for the SAME underlying object always produced a
duplicate.

FIX: `_find_shadow_by_identity()` resolves a candidate append *value*'s own
IDENTITY ref for the target collection (`bds`→`bdRef`, `anps`→`anpRef`,
`epgs`→`epgRef`, `contracts`→`contractRef`), in either string or dict form,
to a bare name and looks for an existing NAMELESS shadow entry with that
same identity ref. The `add`+`-` branch calls it before appending: a match
REPLACES that entry in place (real NDO's "one site-BD row per template BD"
invariant and cisco.mso's GET-then-write idiom); only a genuinely new
shadow appends.

CRITICAL SCOPING (the regression the first PR-16 cut had): identity
matching is restricted to NAMELESS shadows and to the ONE identity ref per
collection. A NAMED object (a template-level BD add, which carries its own
`name`) always appends — it is never deduped by a `*Ref`. And a *property*
ref like `vrfRef` (which every template BD under one L3-attached VRF shares,
e.g. `vrf-L3_LAB0`) is NEVER used to establish identity. Matching on shared
property refs collapsed every template's BDs into a single entry in the
first cut; `test_distinct_template_bds_sharing_vrfref_do_not_collapse`
guards against any recurrence.
"""
from __future__ import annotations

from aci_sim.ndo.patch import apply_json_patch


def _mirror_bd(doc: dict) -> None:
    """Template BD add — fires _mirror_template_bd_to_sites (PR-15)."""
    ops = [
        {
            "op": "add",
            "path": "/templates/LAB1/bds/-",
            "value": {"name": "App1-Net_VLAN10", "displayName": "App1-Net_VLAN10"},
        }
    ]
    apply_json_patch(doc, ops)


def _base_doc() -> dict:
    return {
        "id": "schema-app1",
        "templates": [{"name": "LAB1", "bds": []}],
        "sites": [{"siteId": "101", "templateName": "LAB1"}],
    }


def test_site_module_add_by_dict_bdref_updates_mirrored_entry_not_duplicates():
    """The exact production sequence: template BD add (mirror fires, string
    bdRef, subnets=[]) THEN a site-module-style `add /sites/{sid}-{tmpl}/
    bds/-` addressed by the OTHER bdRef form (a dict, no schemaId/
    templateName — `mso_schema_site_bd.py`'s literal "BD does not exist"
    payload per test_patch_add_site_bd_by_composite_key). Must converge on
    ONE site-bd entry, not two."""
    doc = _base_doc()
    _mirror_bd(doc)
    site = doc["sites"][0]
    assert len(site["bds"]) == 1, "mirror must create exactly one shadow"
    assert site["bds"][0]["bdRef"] == "/schemas/schema-app1/templates/LAB1/bds/App1-Net_VLAN10"
    assert site["bds"][0]["subnets"] == []

    site_add_ops = [
        {
            "op": "add",
            "path": "/sites/101-LAB1/bds/-",
            "value": {
                "bdRef": {"bdName": "App1-Net_VLAN10"},
                "hostBasedRouting": False,
            },
        }
    ]
    apply_json_patch(doc, site_add_ops)

    # No duplicate entry: the site-module add must have resolved to the
    # SAME list slot the mirror created (updating it in place), not
    # appended a second row. The incoming bare {"bdName": ...} form (no
    # schemaId/templateName) isn't stringified — matching pre-existing,
    # unchanged `_stringify_refs` behavior — so the stored bdRef becomes
    # whatever the caller sent; what matters is there is exactly ONE row.
    assert len(site["bds"]) == 1, (
        f"expected exactly one site-bd entry after site-module add, got {len(site['bds'])}: {site['bds']}"
    )
    assert site["bds"][0]["bdRef"] == {"bdName": "App1-Net_VLAN10"}
    assert site["bds"][0]["hostBasedRouting"] is False


def test_site_module_add_by_full_schema_dict_bdref_then_subnet_converges_on_one_entry():
    """Same as above but the dict form carries schemaId+templateName (the
    OTHER real cisco.mso payload shape, see test_pr11_ndo_write.py's
    `test_patch_add_site_bd_subnet_by_ref_name_resolution` family), and a
    subsequent subnet add lands on the SAME entry the mirror created — this
    is the exact Acme App1-Net symptom (empty mirror + subnet-carrying entry
    must be ONE row, not two)."""
    doc = _base_doc()
    _mirror_bd(doc)

    site_add_ops = [
        {
            "op": "add",
            "path": "/sites/101-LAB1/bds/-",
            "value": {
                "bdRef": {
                    "schemaId": "schema-app1",
                    "templateName": "LAB1",
                    "bdName": "App1-Net_VLAN10",
                },
                "hostBasedRouting": False,
            },
        }
    ]
    apply_json_patch(doc, site_add_ops)

    site = doc["sites"][0]
    assert len(site["bds"]) == 1, f"expected one entry after site add, got {site['bds']}"

    subnet_ops = [
        {
            "op": "add",
            "path": "/sites/101-LAB1/bds/App1-Net_VLAN10/subnets/-",
            "value": {"ip": "10.10.10.1/24", "scope": "private"},
        }
    ]
    apply_json_patch(doc, subnet_ops)

    assert len(site["bds"]) == 1, (
        f"subnet add must not create a second entry, got {len(site['bds'])}: {site['bds']}"
    )
    only_bd = site["bds"][0]
    assert only_bd["bdRef"] == "/schemas/schema-app1/templates/LAB1/bds/App1-Net_VLAN10"
    assert only_bd["subnets"] == [{"ip": "10.10.10.1/24", "scope": "private"}]


def test_site_module_add_before_mirror_still_dedups_on_later_mirror_call():
    """Order-independence: if the site-module add somehow lands BEFORE a
    (re-applied/idempotent) template mirror call, the mirror's own
    _find_by_name dedup (unchanged by this fix) still recognizes the
    site-module's entry and does not append a second one."""
    doc = _base_doc()
    site_add_ops = [
        {
            "op": "add",
            "path": "/sites/101-LAB1/bds/-",
            "value": {"bdRef": {"bdName": "App1-Net_VLAN10"}, "hostBasedRouting": False},
        }
    ]
    apply_json_patch(doc, site_add_ops)
    assert len(doc["sites"][0]["bds"]) == 1

    _mirror_bd(doc)
    assert len(doc["sites"][0]["bds"]) == 1, "re-mirroring must not duplicate an existing site-bd"


def test_unrelated_appends_without_ref_field_still_append_normally():
    """Values with no identity `*Ref` (e.g. a template-level BD add, which
    carries its own name, or a subnet add's own value) must keep appending
    unconditionally — the dedup-on-append behavior is scoped to nameless
    site-local shadow objects only, via _find_shadow_by_identity returning
    None for named / non-shadow values."""
    doc = {"templates": [{"name": "LAB1", "bds": []}], "sites": []}
    ops = [
        {"op": "add", "path": "/templates/LAB1/bds/-", "value": {"name": "bd-A"}},
        {"op": "add", "path": "/templates/LAB1/bds/-", "value": {"name": "bd-B"}},
    ]
    apply_json_patch(doc, ops)
    assert [b["name"] for b in doc["templates"][0]["bds"]] == ["bd-A", "bd-B"]


def test_multiple_distinct_bds_in_same_site_all_kept_separate():
    """Dedup must match by bare name, not accidentally collapse different
    BDs mirrored into the same site."""
    doc = {
        "id": "schema-app1",
        "templates": [{"name": "LAB1", "bds": []}],
        "sites": [{"siteId": "101", "templateName": "LAB1"}],
    }
    apply_json_patch(doc, [{"op": "add", "path": "/templates/LAB1/bds/-", "value": {"name": "bd-A"}}])
    apply_json_patch(doc, [{"op": "add", "path": "/templates/LAB1/bds/-", "value": {"name": "bd-B"}}])
    site = doc["sites"][0]
    assert len(site["bds"]) == 2

    apply_json_patch(
        doc,
        [
            {
                "op": "add",
                "path": "/sites/101-LAB1/bds/-",
                "value": {"bdRef": {"bdName": "bd-B"}, "hostBasedRouting": True},
            }
        ],
    )
    assert len(site["bds"]) == 2, f"expected bd-A untouched, bd-B updated in place, got {site['bds']}"

    def _name(bd: dict) -> str | None:
        ref = bd["bdRef"]
        if isinstance(ref, str):
            return ref.rsplit("/", 1)[-1]
        return ref.get("bdName")

    names = {_name(b) for b in site["bds"]}
    assert names == {"bd-A", "bd-B"}
    bd_b = next(b for b in site["bds"] if _name(b) == "bd-B")
    assert bd_b["hostBasedRouting"] is True


# ---------------------------------------------------------------------------
# REGRESSION GUARD — the first PR-16 cut collapsed every template's BDs to
# one entry because it deduped named objects via a SHARED non-identity ref.
# ---------------------------------------------------------------------------


def test_distinct_template_bds_sharing_vrfref_do_not_collapse():
    """CATASTROPHIC REGRESSION GUARD. In the real Acme create_bd run, every
    template BD carries the SAME `vrfRef` (`vrf-L3_LAB0`). The first PR-16
    cut resolved an appended value's identity by iterating ALL
    `_REF_LOOKUP_KEYS` (including `vrfRef`), so two DISTINCT template BDs
    (`bd-App2-...` and `bd-App3-...`) both matched via their shared
    `vrfRef` and collapsed — each `add /templates/{T}/bds/-` overwrote the
    previous BD, leaving exactly ONE BD per template. A NAMED object must
    match ONLY by name, and identity refs must exclude property refs like
    `vrfRef`."""
    doc = {"id": "s1", "templates": [{"name": "T", "bds": []}], "sites": []}
    shared_vrf = {"schemaId": "s1", "templateName": "T", "vrfName": "vrf-L3_LAB0"}
    for name in ("bd-App2-Net", "bd-App3-Hst", "bd-App4-Bkp"):
        apply_json_patch(
            doc,
            [{"op": "add", "path": "/templates/T/bds/-", "value": {"name": name, "vrfRef": dict(shared_vrf)}}],
        )
    names = [b["name"] for b in doc["templates"][0]["bds"]]
    assert names == ["bd-App2-Net", "bd-App3-Hst", "bd-App4-Bkp"], (
        f"distinct template BDs sharing a vrfRef must NOT collapse — got {names}"
    )


def test_template_bd_add_with_shared_vrfref_mirrors_all_into_site():
    """The site-mirror side of the same regression: three distinct template
    BDs sharing one `vrfRef`, added to a template with a site attached, must
    each produce their OWN site-local shadow (3 shadows, not 1)."""
    doc = {
        "id": "s1",
        "templates": [{"name": "LAB1", "bds": []}],
        "sites": [{"siteId": "101", "templateName": "LAB1"}],
    }
    shared_vrf = {"schemaId": "s1", "templateName": "LAB1", "vrfName": "vrf-L3_LAB0"}
    for name in ("bd-One", "bd-Two", "bd-Three"):
        apply_json_patch(
            doc,
            [{"op": "add", "path": "/templates/LAB1/bds/-", "value": {"name": name, "vrfRef": dict(shared_vrf)}}],
        )
    assert len(doc["templates"][0]["bds"]) == 3
    site = doc["sites"][0]
    mirrored = {b["bdRef"].rsplit("/", 1)[-1] for b in site["bds"]}
    assert mirrored == {"bd-One", "bd-Two", "bd-Three"}, f"each BD needs its own shadow, got {site['bds']}"


def test_site_shadow_with_vrfref_property_still_dedups_only_on_bdref():
    """A site-BD shadow may legitimately carry a `vrfRef` property in
    addition to its identity `bdRef`. Two site-module adds for DIFFERENT
    BDs that happen to share a `vrfRef` must still stay separate (dedup is
    on `bdRef` identity, not the shared `vrfRef` property)."""
    doc = {
        "id": "s1",
        "templates": [{"name": "LAB1", "bds": []}],
        "sites": [{"siteId": "101", "templateName": "LAB1"}],
    }
    apply_json_patch(doc, [{"op": "add", "path": "/templates/LAB1/bds/-", "value": {"name": "bd-P"}}])
    apply_json_patch(doc, [{"op": "add", "path": "/templates/LAB1/bds/-", "value": {"name": "bd-Q"}}])
    site = doc["sites"][0]
    assert len(site["bds"]) == 2
    vrf = "/schemas/s1/templates/LAB1/vrfs/vrf-Shared"
    apply_json_patch(
        doc,
        [{"op": "add", "path": "/sites/101-LAB1/bds/-", "value": {"bdRef": {"bdName": "bd-P"}, "vrfRef": vrf}}],
    )
    apply_json_patch(
        doc,
        [{"op": "add", "path": "/sites/101-LAB1/bds/-", "value": {"bdRef": {"bdName": "bd-Q"}, "vrfRef": vrf}}],
    )
    assert len(site["bds"]) == 2, f"distinct shadows sharing a vrfRef must not merge, got {site['bds']}"


def test_replace_named_object_still_resolves_by_name_only():
    """A `replace` addressing a named template BD by name must still work,
    and must not accidentally resolve to a sibling via a shared ref."""
    doc = {"id": "s1", "templates": [{"name": "T", "bds": []}], "sites": []}
    shared_vrf = {"schemaId": "s1", "templateName": "T", "vrfName": "vrf-Shared"}
    apply_json_patch(doc, [{"op": "add", "path": "/templates/T/bds/-", "value": {"name": "bd-A", "vrfRef": dict(shared_vrf)}}])
    apply_json_patch(doc, [{"op": "add", "path": "/templates/T/bds/-", "value": {"name": "bd-B", "vrfRef": dict(shared_vrf)}}])
    apply_json_patch(
        doc,
        [{"op": "replace", "path": "/templates/T/bds/bd-B", "value": {"name": "bd-B", "displayName": "B-updated"}}],
    )
    bds = {b["name"]: b for b in doc["templates"][0]["bds"]}
    assert set(bds) == {"bd-A", "bd-B"}
    assert bds["bd-B"].get("displayName") == "B-updated"
    assert "displayName" not in bds["bd-A"], "replace must not have touched the sibling BD"
