"""Minimal JSON-Patch (RFC 6902) applier for the NDO schema PATCH surface — PR-11.

`cisco.mso`'s schema-object modules (mso_schema_template, mso_schema_template_bd,
mso_schema_template_anp, mso_schema_template_anp_epg, mso_schema_site, ...) all
mutate a schema the same way: look the schema up by displayName/id, then send
`PATCH /mso/api/v1/schemas/{id}` with a JSON body that is a *list* of ops:

    [{"op": "add", "path": "/templates/LAB1/bds/-", "value": {...}}]
    [{"op": "replace", "path": "/templates/LAB1/anps/0/epgs/2", "value": {...}}]
    [{"op": "remove", "path": "/templates/LAB1/bds/bd-App1_LAB1"}]

Two path shapes appear in ansible-mso source:
  - numeric array index / "-" (append) — standard RFC 6902.
  - a *named* segment used as a dict-list lookup key ("bds/bd-App1_LAB1",
    "anps/AP1"): the module resolves those to a numeric index in Python and
    sends the resolved path in the common case (see mso_schema_template_bd.py
    `bd_path = "/templates/{0}/bds/{1}".format(template, bd)` used directly in
    ops when `mso.existing` was truthy), but every module also computes
    `template` as a *name*, not an index — "/templates/LAB1/..." — so the verb
    is really "the templates list, resolved by matching name field", not
    "index 0". We replicate that: any path segment that isn't a valid array
    index and isn't "-" is resolved by scanning the current list for a dict
    whose `name` (or `displayName`) attribute equals that segment.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

#: Every collection key a schema template can carry. Real NDO always serves
#: these back as (at minimum) empty lists even when a caller's create/add
#: payload omitted them — e.g. `mso_schema_template.py`'s "schema does not
#: exist yet" POST payload is only `{name, displayName, tenantId}`, no
#: `vrfs`/`bds`/... . `MSOSchema.set_template_vrf()` (ansible-mso
#: module_utils/schema.py) then does
#: `enumerate(self.schema_objects["template"].details.get("vrfs"))`
#: unconditionally — a missing key there is `None`, not `[]`, and blows up
#: with "'NoneType' object is not iterable" (PR-11, observed on a real
#: ACI-hardware create_tenant → create VRF run). Normalize on every
#: template create so every reader sees a real list.
TEMPLATE_COLLECTION_KEYS: tuple[str, ...] = (
    "vrfs",
    "bds",
    "anps",
    "contracts",
    "filters",
    "externalEpgs",
    # PR-11: mso_schema_template_l3out.py / _service_graph.py subscript
    # these directly (`schema_obj.get("templates")[idx]["intersiteL3outs"]`,
    # no `.get()` fallback) — a missing key is a hard KeyError, confirmed on
    # a real hardware create_tenant run's "Create L3Out" task.
    "intersiteL3outs",
    "serviceGraphs",
)

#: Child-object collection defaults, one level down from a template's own
#: arrays. Every cisco.mso "add a child object" module writes a payload with
#: only the fields *it* cares about (e.g. mso_schema_template_external_epg.py
#: never sets `subnets`), then a *sibling* module
#: (mso_schema_template_external_epg_subnet.py) reads
#: `externalEpgs[idx]["subnets"]` with a bare subscript — a real-hardware run
#: hit `KeyError: 'subnets'` on exactly this path. Real NDO must default
#: these server-side; replicate that here so every object born via PATCH
#: carries the collection keys its sibling modules expect.
#: Maps: template-array-key -> {default-key: default-value}.
CHILD_OBJECT_DEFAULTS: dict[str, dict[str, Any]] = {
    "bds": {"subnets": [], "dhcpLabels": []},
    "anps": {"epgs": []},
    "epgs": {"subnets": [], "contractRelationships": [], "staticPorts": [], "staticLeafs": [], "domains": []},
    "externalEpgs": {"subnets": [], "contractRelationships": [], "selectors": []},
    "contracts": {"filterRelationships": []},
    "filters": {"entries": []},
    "intersiteL3outs": {},
    "serviceGraphs": {},
    "vrfs": {"rpConfigs": []},
}


class PatchError(Exception):
    """Raised when a JSON-Patch op cannot be applied to the stored document."""


def _normalize_object(obj: dict, array_key: str, schema_id: str, template_name: str, anp_name: str = "") -> None:
    """Backfill *obj*'s missing collection-default keys for its container
    array (e.g. array_key="bds" -> obj gets subnets/dhcpLabels defaults),
    then recurse into any nested arrays this object type carries (anps.epgs).

    PR-12: template-level `anps[]`/`epgs[]` entries also get a
    self-referencing `anpRef`/`epgRef` string field backfilled — real NDO
    stores one on every template ANP/EPG object itself (confirmed by
    `ansible-mso`'s `mso_schema_template_anp.py`/`_anp_epg.py` explicitly
    stripping it client-side before an idempotency comparison: `if "anpRef"
    in mso.previous: del mso.previous["anpRef"]` — dead code unless the
    server actually sends one back). `MSOSchema.set_site_anp()`/
    `set_site_anp_epg()` (`module_utils/schema.py`) key their site-local
    lookup off exactly this field (`template_anp.details.get("anpRef")`) —
    without it those lookups compare against `None` and never match,
    crashing `mso_schema_site_anp_epg_staticport.py` with `'NoneType'
    object has no attribute 'details'` (confirmed on a real MS-TN1
    bind_epg_to_static_port run on ACI hardware before this fix).

    PR-13: `epgs`/`externalEpgs` entries also get a stable `uuid` field.
    Real NDO assigns one to every schema-template EPG/external-EPG; the
    NDO-plane `cisco.mso.ndo_dhcp_relay_policy` module reads it as
    `schema_objects["template_anp_epg"].details.get("uuid")` (via
    `MSOSchema.set_template_anp_epg()`) to build a DHCP relay policy
    provider's `epgRef` — without it the sim always hands back `None`,
    and the relay-policy-add PATCH round-trips a provider that can never
    resolve back to a real EPG (confirmed against `ansible-mso`'s
    `plugins/modules/ndo_dhcp_relay_policy.py::get_providers_payload()`).
    """
    for key, default in CHILD_OBJECT_DEFAULTS.get(array_key, {}).items():
        if obj.get(key) is None:
            obj[key] = [] if isinstance(default, list) else default
    if array_key == "anps":
        anp_name = obj.get("name", anp_name)
        if not obj.get("anpRef"):
            obj["anpRef"] = "/schemas/{}/templates/{}/anps/{}".format(schema_id, template_name, anp_name)
        for epg in obj.get("epgs", []):
            if isinstance(epg, dict):
                _normalize_object(epg, "epgs", schema_id, template_name, anp_name)
    elif array_key == "epgs":
        epg_name = obj.get("name", "")
        if not obj.get("epgRef"):
            obj["epgRef"] = "/schemas/{}/templates/{}/anps/{}/epgs/{}".format(
                schema_id, template_name, anp_name, epg_name
            )
        if not obj.get("uuid"):
            seed = "epg-uuid-{}-{}-{}-{}".format(schema_id, template_name, anp_name, epg_name)
            obj["uuid"] = hashlib.sha256(seed.encode()).hexdigest()[:32]
    elif array_key == "externalEpgs":
        if not obj.get("uuid"):
            ext_epg_name = obj.get("name", "")
            seed = "extepg-uuid-{}-{}-{}".format(schema_id, template_name, ext_epg_name)
            obj["uuid"] = hashlib.sha256(seed.encode()).hexdigest()[:32]


def normalize_template(template: dict, schema_id: str = "") -> dict:
    """Backfill missing collection keys + templateID on a template dict, and
    recursively normalize every child object already present in its arrays.

    Mutates and returns *template* in place. Safe to call repeatedly
    (idempotent — only fills keys that are absent or None). *schema_id* is
    used to build the self-referencing anpRef/epgRef strings PR-12 backfills
    on every anps[]/epgs[] entry — see _normalize_object's docstring.
    """
    template_name = template.get("name", "")
    for key in TEMPLATE_COLLECTION_KEYS:
        if template.get(key) is None:
            template[key] = []
        for obj in template[key]:
            if isinstance(obj, dict):
                _normalize_object(obj, key, schema_id, template_name)

    # `MSOSchema.set_template()` reads `match.details.get("templateID")` —
    # a capital-ID sibling of the lowercase `tenantId`/`id` keys the create
    # payload sends. Real NDO assigns this server-side; derive a stable one
    # from (schema-scoped) template name so repeated GETs are consistent.
    if not template.get("templateID"):
        seed = f"template-{template.get('name', '')}"
        template["templateID"] = hashlib.sha256(seed.encode()).hexdigest()[:24]
    return template


#: Site-array (top-level schema `sites[]`) collection defaults — a distinct
#: namespace from TEMPLATE_COLLECTION_KEYS/CHILD_OBJECT_DEFAULTS because
#: `mso_schema_site_bd.py`'s add payload is only `{bdRef, hostBasedRouting}`
#: (no `subnets`), yet `mso_schema_site_bd_subnet.py` reads
#: `site_bd.details.get("subnets")` and iterates it — a missing key there is
#: `None`, not `[]` (PR-11, same "iterate a None" family of bug as the
#: template-level fix above).
#:
#: The `epgs` entry is the canonical *full* site-local EPG collection shape.
#: A site-local EPG is created by `mso_schema_site_anp_epg.py` (or the
#: staticport/domain modules' own inline fallback) with a payload of only
#: `{epgRef}` — real NDO backfills every child collection array server-side,
#: and every sibling `mso_schema_site_anp_epg_*` module then reads its own
#: array with a **bare subscript** (not `.get()`), so a missing key is a hard
#: `KeyError`, not a 4xx:
#:   - `staticPorts`  — `mso_schema_site_anp_epg_staticport.py`
#:     (`set_existing_static_ports`: `...["epgs"][idx].get("staticPorts")` /
#:     bulk module iterates it).
#:   - `staticLeafs`  — `mso_schema_site_anp_epg_staticleaf.py`
#:     (`...["epgs"][epg_idx]["staticLeafs"]`).
#:   - `domainAssociations` — `mso_schema_site_anp_epg_domain.py`
#:     (`domains = [dom.get("dn") for dom in ...["epgs"][epg_idx]
#:     ["domainAssociations"]]`) — a real MS-TN1 hardware
#:     `bind_epg_to_physical_domain`/`_vmm_domain` run hit
#:     `KeyError: 'domainAssociations'` on exactly this line once PR-12
#:     started auto-creating the site-EPG (pre-PR-12 no site-EPG existed at
#:     all, so the module took a non-crashing branch).
#:   - `subnets` — `mso_schema_site_anp_epg_subnet.py`.
SITE_OBJECT_DEFAULTS: dict[str, dict[str, Any]] = {
    "bds": {"subnets": []},
    "anps": {"epgs": []},
    "epgs": {"subnets": [], "staticPorts": [], "staticLeafs": [], "domainAssociations": []},
}

#: Top-level collection keys on a schema `sites[]` ENTRY ITSELF (a sibling
#: of `bds`/`anps`, not a nested child-object default like
#: SITE_OBJECT_DEFAULTS above) — PR-13. `custom_mso_schema_service_graph.py`
#: (the "Create service graph" task's module, `mso-model/library/`) reads
#: `schema_obj.get('sites')[site_idx]['serviceGraphs']` with a **bare
#: subscript** to find/append the site-local service-graph-to-device
#: binding; a schema born via `POST /schemas` or PATCHed via
#: `mso_schema_site.py`'s "add site" op never carries this key, so a real
#: hardware MS-TN2 `create_tenant` pass-2 run (`-e automate_contract_graph=true`)
#: hit `KeyError: 'serviceGraphs'` on exactly this line — the same class of
#: bug as the template-level `serviceGraphs`/`intersiteL3outs` gap PR-11
#: fixed and the site-local `domainAssociations` gap PR-12 fixed, just one
#: level up (the site entry itself, not one of its child arrays).
SITE_TOP_LEVEL_DEFAULTS: dict[str, Any] = {
    "bds": [],
    "anps": [],
    "serviceGraphs": [],
    "contracts": [],
}


def _new_site_epg(epg_ref: str) -> dict:
    """Build a full-shaped site-local EPG dict from its canonical `epgRef`
    string — every child-collection array real NDO backfills server-side,
    so every sibling `mso_schema_site_anp_epg_*` module's bare subscript
    (`["domainAssociations"]`/`["staticLeafs"]`/...) finds a real list.
    Single source of truth: keys come from SITE_OBJECT_DEFAULTS["epgs"]."""
    epg = {"epgRef": epg_ref}
    for dkey, default in SITE_OBJECT_DEFAULTS["epgs"].items():
        epg[dkey] = [] if isinstance(default, list) else default
    return epg


def normalize_site(site: dict) -> dict:
    """Backfill missing collection-default keys on a schema `sites[]` entry
    and its child bds/anps.epgs objects. Mutates and returns *site*."""
    # PR-13: backfill the site's OWN top-level collection keys (bds/anps/
    # serviceGraphs) before descending into their per-object defaults below
    # — see SITE_TOP_LEVEL_DEFAULTS docstring for the serviceGraphs
    # bare-subscript crash this closes.
    for key, default in SITE_TOP_LEVEL_DEFAULTS.items():
        if site.get(key) is None:
            site[key] = [] if isinstance(default, list) else default

    for key, defaults in SITE_OBJECT_DEFAULTS.items():
        if key not in ("bds", "anps"):
            continue
        for obj in site.get(key, []):
            if not isinstance(obj, dict):
                continue
            for dkey, default in defaults.items():
                if obj.get(dkey) is None:
                    obj[dkey] = [] if isinstance(default, list) else default
            if key == "anps":
                for epg in obj.get("epgs", []):
                    if isinstance(epg, dict):
                        for dkey, default in SITE_OBJECT_DEFAULTS["epgs"].items():
                            if epg.get(dkey) is None:
                                epg[dkey] = [] if isinstance(default, list) else default
    return site


def _is_list_index(seg: str) -> bool:
    return seg.isdigit()


#: `*Ref`-keyed objects — site-local bds/anps/epgs carry no `name` field of
#: their own (their whole identity is the `*Ref` string pointing back at the
#: template-level object), yet sibling modules still address them by that
#: template-level object's bare name — e.g.
#: `/sites/1-LAB1/bds/bd-App1_LAB1/subnets/-` (mso_schema_site_bd_subnet.py
#: /custom_mso_schema_site_bd_subnet.py: `bds_path = "/sites/{0}/bds".format(
#: site_template)`, then indexes by matching `bd_ref in bds`). Match by the
#: trailing path segment of the ref string.
_REF_LOOKUP_KEYS: tuple[str, ...] = ("bdRef", "vrfRef", "l3outRef", "anpRef", "epgRef", "contractRef")

#: The subset of `*Ref` keys that constitute a *nameless site-local shadow's*
#: whole IDENTITY — i.e. the one ref that IS the object (`bds[]` entry → its
#: `bdRef`, `anps[]` → `anpRef`, `epgs[]` → `epgRef`, `contracts[]` →
#: `contractRef`). A shadow object carries no `name`/`displayName` of its
#: own; its identity is exactly this single ref pointing back at the
#: template-level object it mirrors.
#:
#: PR-16 (corrected): the other `*Ref` keys in `_REF_LOOKUP_KEYS`
#: (`vrfRef`, `l3outRef`) are *properties* an object may carry, NOT its
#: identity — every template BD under an L3-attached VRF shares the same
#: `vrfRef` (e.g. `vrf-L3_LAB0`). Matching on those would collapse many
#: distinct BDs into one. So ref-based identity matching is restricted to
#: this map, keyed by the *list's* collection name so a `bds[]` list only
#: ever matches on `bdRef` (never a sibling's shared `vrfRef`).
_IDENTITY_REF_BY_COLLECTION: dict[str, str] = {
    "bds": "bdRef",
    "anps": "anpRef",
    "epgs": "epgRef",
    "contracts": "contractRef",
}
_IDENTITY_REF_KEYS: frozenset[str] = frozenset(_IDENTITY_REF_BY_COLLECTION.values())


def _bare_ref_name(ref_val: Any) -> str | None:
    """Extract the bare object name from a `*Ref` field value, regardless
    of whether it's already NDO's canonical string form
    (`/schemas/.../bds/bd-X`) or one of the dict forms cisco.mso's write
    payloads send (`{schemaId, templateName, bdName}` — see
    `_REF_NAME_FIELDS`/`_stringify_refs`). Returns None if no name can be
    extracted (e.g. an empty dict).

    PR-16: the single bare-name extractor for a shadow object's identity
    ref. Handles both ref shapes so a dict-form `bdRef` (a site-module
    payload's own shape, possibly a bare `{bdName: ...}`) and a string-form
    `bdRef` (the mirror's canonical shape) resolve to the same bare name —
    the property that lets mirror + site-module add/replace converge on one
    entry. Deliberately used ONLY on identity refs (see
    `_IDENTITY_REF_BY_COLLECTION`), never on property refs like `vrfRef`."""
    if isinstance(ref_val, str) and ref_val:
        return ref_val.rsplit("/", 1)[-1]
    if isinstance(ref_val, dict):
        for name_field, _category in _REF_NAME_FIELDS.values():
            name = ref_val.get(name_field)
            if name:
                return name
    return None


def _find_by_name(items: list, seg: str) -> int | None:
    """Return the index of the dict in *items* matching path segment *seg*.

    Three addressing conventions appear across cisco.mso's schema-object
    modules:
      - most template-level objects (bds/anps/epgs/contracts/filters/
        externalEpgs/vrfs) are addressed by their own `name` (or
        `displayName`) field — e.g. `/templates/LAB1/bds/bd-App1_LAB1`. A
        NAMED object is matched ONLY by name/displayName, never by any
        `*Ref` field.
      - the top-level schema `sites` array is instead addressed by a
        synthetic composite key `"{siteId}-{templateName}"` that isn't a
        field the object itself carries — every `mso_schema_site_*` module
        builds this literally: `site_template = "{0}-{1}".format(site_id,
        template)` then `"/sites/{0}/bds".format(site_template)` (see
        mso_schema_site_bd.py / _anp.py / _anp_epg.py). Detect that shape by
        checking for `siteId`+`templateName` keys on the candidate items and
        matching the composite key instead of name/displayName.
      - site-local child objects (sites[idx].bds / .anps / .contracts /
        .anps[].epgs) carry no `name` of their own at all — only an IDENTITY
        `*Ref` field (string OR dict form, PR-16) pointing at the template
        object — yet are still addressed by that referenced object's bare
        name (confirmed on a real hardware run: `/sites/1-LAB1/bds/
        bd-App1_LAB1/subnets/-`). Only for such a NAMELESS item, match by its
        identity ref's resolved bare name — and only via the identity refs
        (`bdRef`/`anpRef`/`epgRef`/`contractRef`), NEVER a property ref like
        `vrfRef` (which many distinct BDs share, PR-16 regression fix). This
        is what lets a dict-form bdRef (a site-module payload's own shape)
        and a string-form bdRef (the mirror's canonical shape) resolve to the
        SAME entry without ever cross-matching two distinct objects.
    """
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if item.get("name") == seg or item.get("displayName") == seg:
            return i
        if "siteId" in item and "templateName" in item:
            composite = f"{item.get('siteId')}-{item.get('templateName')}"
            if composite == seg:
                return i
        # Ref-matching is ONLY for nameless shadow entries, and ONLY via an
        # identity ref — a named object above already returned; a property
        # ref (vrfRef/l3outRef) must never be used to establish identity.
        if item.get("name") is None and item.get("displayName") is None:
            for ref_key in _IDENTITY_REF_KEYS:
                if ref_key in item and _bare_ref_name(item.get(ref_key)) == seg:
                    return i
    return None


def _find_shadow_by_identity(items: list, value: Any, collection: str) -> int | None:
    """Append-path dedup for a NAMELESS site-local shadow being added to the
    *collection*-named list (`bds`/`anps`/`epgs`/`contracts`).

    A JSON-Patch `add` whose path ends in the literal `"-"` (append)
    bypasses `_find_by_name` entirely (there's no path segment to resolve),
    so a site-module `add` for a BD/ANP/contract that the template-add
    mirror (`_mirror_template_bd_to_sites` et al.) already created as a
    site-local shadow would otherwise append a SECOND entry instead of
    updating the existing one — confirmed against a reference NDO
    schema (`MS-TN1-LAB0`) where `sites[0].bds` carried two entries for the
    same BD after `create_tenant` (the empty full-path mirror) then
    `create_bd`'s site-module subnet PATCH (a dict-bdRef entry carrying the
    subnet).

    Matches STRICTLY by the value's own identity ref for THIS collection
    (`bds`→`bdRef`, etc.), resolved to a bare name, against existing entries'
    same identity ref. Never matches:
      - a NAMED value (a template-level object add — those append normally),
      - via a non-identity/property ref (`vrfRef`/`l3outRef` — many distinct
        BDs share one; the PR-16 regression that collapsed every template's
        BDs to a single entry),
      - across different bd_names.
    """
    if not isinstance(value, dict):
        return None
    # A value carrying its own name is a template-level object, not a
    # nameless shadow — it appends normally (distinct objects stay distinct).
    if value.get("name") is not None or value.get("displayName") is not None:
        return None
    ref_key = _IDENTITY_REF_BY_COLLECTION.get(collection)
    if ref_key is None or ref_key not in value:
        return None
    seg = _bare_ref_name(value.get(ref_key))
    if seg is None:
        return None
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if item.get("name") is not None or item.get("displayName") is not None:
            continue
        if ref_key in item and _bare_ref_name(item.get(ref_key)) == seg:
            return i
    return None


def _resolve_container(doc: dict, tokens: list[str]) -> tuple[Any, str]:
    """Walk *tokens[:-1]* from *doc*, returning (container, last_token).

    The container is either a dict (last_token is a key) or a list
    (last_token is "-", a numeric index, or a name to resolve).
    """
    node: Any = doc
    middle = tokens[:-1]
    for i, tok in enumerate(middle):
        if isinstance(node, list):
            if _is_list_index(tok):
                idx = int(tok)
            else:
                idx = _find_by_name(node, tok)
                if idx is None:
                    raise PatchError(f"path segment '{tok}' not found in list")
            if idx >= len(node):
                raise PatchError(f"index {idx} out of range")
            node = node[idx]
        elif isinstance(node, dict):
            if tok not in node:
                # Auto-vivify a missing intermediate container. Every
                # cisco.mso schema-object module PATCHes into a collection
                # the schema-create/normalize_* pass already seeded as a
                # list (vrfs/bds/anps/.../subnets/epgs/...), so this should
                # not normally trigger — but if it does, look at the NEXT
                # token to pick the right shape instead of guessing a dict:
                # a numeric index or "-" (append) means the caller expects
                # a list; anything else means a nested dict key.
                next_tok = tokens[i + 1] if i + 1 < len(tokens) else None
                if next_tok is not None and (next_tok == "-" or _is_list_index(next_tok)):
                    node[tok] = []
                else:
                    node[tok] = {}
            node = node[tok]
        else:
            raise PatchError(f"cannot descend into non-container at '{tok}'")
    return node, tokens[-1]


#: `*Ref` field name -> (name-field-in-payload, url-category-segment).
#: Real NDO always serves these back as canonical STRING refs
#: (`/schemas/{schemaId}/templates/{templateName}/{category}/{name}`) even
#: though several cisco.mso write payloads send a *dict* form instead — see
#: `mso_schema_site_bd.py`'s add payload (`bdRef=dict(schemaId=...,
#: templateName=..., bdName=...)`) versus its OWN query-side comparison
#: (`mso.bd_ref(...)` builds and compares against the string form, and
#: `mso.dict_from_ref()` exists specifically to convert the server's string
#: back to a dict client-side). A real hardware run proved the server must
#: store the string form: a sibling module
#: (`custom_mso_schema_site_bd_subnet.py`) reads `[v.get('bdRef') for v in
#: ...]` and does `bd_ref_string in bds` / `', '.join(bds)` — if the sim
#: stored the dict form verbatim, that `join()` crashes with "sequence item
#: 0: expected str instance, dict found" (confirmed on a real ACI-hardware "Add
#: site-local subnet to BD" run).
_REF_NAME_FIELDS: dict[str, tuple[str, str]] = {
    "bdRef": ("bdName", "bds"),
    "vrfRef": ("vrfName", "vrfs"),
    "l3outRef": ("l3outName", "l3outs"),
    "filterRef": ("filterName", "filters"),
    "contractRef": ("contractName", "contracts"),
    "anpRef": ("anpName", "anps"),
    "serviceGraphRef": ("serviceGraphName", "serviceGraphs"),
}

#: `epgRef` is the one `*Ref` field whose canonical string form nests TWO
#: name segments under the template, not one — confirmed against
#: `ansible-mso`'s `plugins/module_utils/mso.py` `epg_ref()`:
#: `"/schemas/{schema_id}/templates/{template}/anps/{anp}/epgs/{epg}"`
#: (`anp_ref()` alone is the single-segment `.../anps/{anp}` form used by
#: `_REF_NAME_FIELDS["anpRef"]` above). `mso_schema_site_anp_epg_staticport.py`
#: sends the dict form `epgRef=dict(schemaId=..., templateName=..., anpName=...,
#: epgName=...)` when auto-creating a site-epg (PR-12) — handled separately
#: from the generic single-category table since it needs both name fields.
_EPG_REF_NAME_FIELDS: tuple[str, str] = ("anpName", "epgName")


def _stringify_refs(value: Any) -> Any:
    """Recursively convert any `*Ref` dict field to NDO's canonical string
    ref form, leaving already-string refs and everything else untouched."""
    if isinstance(value, dict):
        for key, (name_field, category) in _REF_NAME_FIELDS.items():
            ref_val = value.get(key)
            if isinstance(ref_val, dict) and "schemaId" in ref_val and "templateName" in ref_val:
                name = ref_val.get(name_field, ref_val.get("name", ""))
                value[key] = "/schemas/{}/templates/{}/{}/{}".format(
                    ref_val["schemaId"], ref_val["templateName"], category, name
                )
        epg_ref_val = value.get("epgRef")
        if isinstance(epg_ref_val, dict) and "schemaId" in epg_ref_val and "templateName" in epg_ref_val:
            anp_name = epg_ref_val.get(_EPG_REF_NAME_FIELDS[0], "")
            epg_name = epg_ref_val.get(_EPG_REF_NAME_FIELDS[1], epg_ref_val.get("name", ""))
            value["epgRef"] = "/schemas/{}/templates/{}/anps/{}/epgs/{}".format(
                epg_ref_val["schemaId"], epg_ref_val["templateName"], anp_name, epg_name
            )
        # `serviceNodeRef` is the other TWO-segment ref (serviceGraphs/{sg}/serviceNodes/{node})
        # — `custom_mso_schema_service_graph.py` writes the dict form when it creates the graph
        # node; the STOCK `mso_schema_template_contract_service_graph`'s idempotency re-read then
        # does a string op on it and dies with "expected string ... got 'dict'" (F3). Real NDO
        # returns the resolved string ref, so resolve it here like serviceGraphRef/epgRef.
        sgn_ref_val = value.get("serviceNodeRef")
        if isinstance(sgn_ref_val, dict) and "schemaId" in sgn_ref_val and "templateName" in sgn_ref_val:
            value["serviceNodeRef"] = "/schemas/{}/templates/{}/serviceGraphs/{}/serviceNodes/{}".format(
                sgn_ref_val["schemaId"], sgn_ref_val["templateName"],
                sgn_ref_val.get("serviceGraphName", ""),
                sgn_ref_val.get("serviceNodeName", sgn_ref_val.get("name", "")),
            )
        for v in value.values():
            _stringify_refs(v)
    elif isinstance(value, list):
        for item in value:
            _stringify_refs(item)
    return value


#: Matches `add /templates/{template}/bds/-` — a brand-new template-level BD
#: (`mso_schema_template_bd.py`'s "BD does not exist" branch). PR-15: mirrored
#: into every associated site's `bds[]` the same way template ANPs/EPGs/
#: contracts are mirrored (PR-11/PR-12/PR-13), because
#: `mso_schema_site_bd.py` always PATCHes its site-local shadow with
#: `op: replace` (never `add`) — real NDO 4.x auto-creates the site BDDelta
#: entry as soon as the template BD is added to a template that already has
#: sites attached, so by the time the site-BD module runs the shadow already
#: exists and a fresh `add` would 409 with "Multiple BDDelta entries" (see
#: aci-py's `shims/mso_mso_bd_vrf.py::mso_schema_site_bd` docstring, which
#: documents this exact real-hardware behavior). Without this mirror, the
#: `replace` 400s with "'bd-X' not found at '/sites/{siteId}-{tpl}/bds/bd-X'"
#: — confirmed on a real-hardware aci-py `create_tenant`/`create_bd` run
#: (MS-TN2), the same class of gap PR-11/12/13 closed for anps/epgs/contracts.
_TEMPLATE_BD_ADD_RE = re.compile(r"^templates/([^/]+)/bds/-$")

#: Matches `add /templates/{template}/anps/-` — a brand-new template-level
#: ANP (`mso_schema_template_anp.py`'s "ANP does not exist" branch).
_TEMPLATE_ANP_ADD_RE = re.compile(r"^templates/([^/]+)/anps/-$")

#: Matches `add /templates/{template}/anps/{anp}/epgs/-` — a brand-new
#: template-level EPG under an existing ANP (`mso_schema_template_anp_epg.py`).
_TEMPLATE_EPG_ADD_RE = re.compile(r"^templates/([^/]+)/anps/([^/]+)/epgs/-$")

#: Matches `add /templates/{template}/contracts/-` — a brand-new
#: template-level contract (`mso_schema_template_contract_filter.py`'s
#: "contract does not exist" branch). PR-13: mirrored into every associated
#: site's `contracts[]` the same way template ANPs are mirrored, because
#: `mso_rest`-driven raw-PATCH tasks (e.g. the mso-model role's
#: "Atomic PATCH — bind service-graph redirect on ALL fabrics" task) address
#: a site-local contract by bare name at
#: `/sites/{siteId}-{template}/contracts/{contract}/serviceGraphRelationship`
#: — a real hardware MS-TN2 `create_tenant` pass-2 run (with
#: `automate_contract_graph=true automate_site_redirect=true`) hit `"path
#: segment 'con-Firewall_LAB0' not found in list"` on exactly this path
#: because the site never carried a mirrored `contracts[]` array at all.
_TEMPLATE_CONTRACT_ADD_RE = re.compile(r"^templates/([^/]+)/contracts/-$")


def _new_site_bd(bd_ref: str) -> dict:
    """Build a full-shaped site-local BD dict from its canonical `bdRef`
    string — mirrors `_new_site_epg` for BDs. `hostBasedRouting` defaults to
    `False` (the value `mso_schema_site_bd.py`'s own "BD does not exist yet"
    `add` payload would send), so a subsequent `replace` (the module's normal
    path once the shadow exists) reads a real bool, not a missing key."""
    bd = {"bdRef": bd_ref, "hostBasedRouting": False}
    for dkey, default in SITE_OBJECT_DEFAULTS["bds"].items():
        bd[dkey] = [] if isinstance(default, list) else default
    return bd


def _mirror_template_bd_to_sites(doc: dict, template_name: str, bd_name: str, schema_id: str | None) -> None:
    """Auto-create a site-local BD shadow entry in every site associated
    with *template_name*, mirroring what real NDO 4.x does automatically
    when a template-level BD is added to a template that already has sites
    attached — see `_TEMPLATE_BD_ADD_RE`'s comment for the full rationale
    and the aci-py shim docstring it cites. Idempotent: skips sites that
    already carry this bdRef."""
    bd_ref = "/schemas/{}/templates/{}/bds/{}".format(schema_id or doc.get("id", ""), template_name, bd_name)
    for site in doc.get("sites", []):
        if not isinstance(site, dict) or site.get("templateName") != template_name:
            continue
        site.setdefault("bds", [])
        if _find_by_name(site["bds"], bd_name) is not None:
            continue
        site["bds"].append(_new_site_bd(bd_ref))


def _mirror_template_anp_to_sites(doc: dict, template_name: str, anp_name: str, schema_id: str | None) -> None:
    """Auto-create a site-local ANP entry in every site associated with
    *template_name*, mirroring what real NDO 4.x does automatically when a
    template-level ANP is added to a template that already has sites
    attached (`mso_schema_site.py` having already run in create_tenant's
    flow — PR-11). Idempotent: skips sites that already carry this anpRef.

    Without this, `mso_schema_site_anp_epg_staticport.py`'s own fallback
    "create site anp/epg if missing" branch is what fires instead — and that
    fallback crashes with `'NoneType' object has no attribute 'details'` on
    a subsequent step (see module docstring / CONTRACT.md §7a), matching a
    real hardware "coverage misses this on 4.x and above" code comment in the
    module itself: on real hardware this mirroring already happened, so the
    fallback path is never exercised.
    """
    anp_ref = "/schemas/{}/templates/{}/anps/{}".format(schema_id or doc.get("id", ""), template_name, anp_name)
    for site in doc.get("sites", []):
        if not isinstance(site, dict) or site.get("templateName") != template_name:
            continue
        site.setdefault("anps", [])
        if _find_by_name(site["anps"], anp_name) is not None:
            continue
        site["anps"].append({"anpRef": anp_ref, "epgs": []})


def _mirror_template_epg_to_sites(
    doc: dict, template_name: str, anp_name: str, epg_name: str, schema_id: str | None
) -> None:
    """Auto-create a site-local EPG entry (under its mirrored site-anp) in
    every site associated with *template_name*, mirroring real NDO 4.x's
    automatic site-epg creation on template EPG add. See
    `_mirror_template_anp_to_sites` for the full rationale. Idempotent."""
    sid = schema_id or doc.get("id", "")
    epg_ref = "/schemas/{}/templates/{}/anps/{}/epgs/{}".format(sid, template_name, anp_name, epg_name)
    for site in doc.get("sites", []):
        if not isinstance(site, dict) or site.get("templateName") != template_name:
            continue
        site.setdefault("anps", [])
        anp_idx = _find_by_name(site["anps"], anp_name)
        if anp_idx is None:
            # Template-level ANP add should have mirrored the site-anp
            # already; auto-vivify defensively so EPG add never crashes on
            # an out-of-order/partial patch batch.
            anp_ref = "/schemas/{}/templates/{}/anps/{}".format(sid, template_name, anp_name)
            site["anps"].append({"anpRef": anp_ref, "epgs": []})
            anp_idx = len(site["anps"]) - 1
        site_anp = site["anps"][anp_idx]
        site_anp.setdefault("epgs", [])
        if _find_by_name(site_anp["epgs"], epg_name) is not None:
            continue
        site_anp["epgs"].append(_new_site_epg(epg_ref))


def _mirror_template_contract_to_sites(doc: dict, template_name: str, contract_name: str, schema_id: str | None) -> None:
    """Auto-create a site-local contract entry (`{contractRef}`) in every
    site associated with *template_name*, mirroring real NDO 4.x's
    automatic site-local mirroring for a template-level contract add — the
    same family of behavior PR-12 implemented for template ANPs/EPGs (see
    `_mirror_template_anp_to_sites`'s docstring for the general rationale).

    Without this, a raw-PATCH task addressing a site-local contract by its
    bare name (e.g. `/sites/{siteId}-{template}/contracts/{contract}/
    serviceGraphRelationship`, the mso-model role's redirect-policy bind)
    hits `_find_by_name`'s "not found in list" `PatchError` — confirmed on
    a real hardware MS-TN2 `create_tenant` pass-2 run (PR-13).
    """
    contract_ref = "/schemas/{}/templates/{}/contracts/{}".format(schema_id or doc.get("id", ""), template_name, contract_name)
    for site in doc.get("sites", []):
        if not isinstance(site, dict) or site.get("templateName") != template_name:
            continue
        site.setdefault("contracts", [])
        if _find_by_name(site["contracts"], contract_name) is not None:
            continue
        site["contracts"].append({"contractRef": contract_ref})


def apply_json_patch(doc: dict, ops: list[dict]) -> dict:
    """Apply a list of RFC-6902-ish ops to *doc* in place and return it.

    Supports op in {"add", "replace", "remove"}. Unknown ops are ignored
    (forward-compatible with cisco.mso module versions we haven't seen).
    """
    for entry in ops:
        op = entry.get("op")
        path = entry.get("path", "")
        value = entry.get("value")
        if op in ("add", "replace"):
            value = _stringify_refs(value)

        tokens = [t for t in path.split("/") if t != ""]
        if not tokens:
            continue

        container, last = _resolve_container(doc, tokens)

        if op == "add":
            if isinstance(container, list):
                if last == "-":
                    # A bare "-" (append) skips _find_by_name entirely (no
                    # path segment to resolve against). For a NAMELESS
                    # site-local shadow (`sites[].bds`/`anps`/`epgs`/
                    # `contracts` entries, whose whole identity is a single
                    # `*Ref`), this previously let a site-module `add` create
                    # a SECOND entry for an object the template-add mirror
                    # already shadowed — see _find_shadow_by_identity for the
                    # real-NDO duplicate-BD symptom this closes. Dedup ONLY a
                    # nameless shadow, ONLY via its identity ref for THIS
                    # collection (tokens[-2]); a named object (template-level
                    # BD add) always appends so distinct objects stay
                    # distinct (PR-16 regression fix).
                    collection = tokens[-2] if len(tokens) >= 2 else ""
                    existing_idx = _find_shadow_by_identity(container, value, collection)
                    if existing_idx is None:
                        container.append(value)
                    else:
                        container[existing_idx] = value
                elif _is_list_index(last):
                    idx = int(last)
                    container.insert(idx, value)
                else:
                    idx = _find_by_name(container, last)
                    if idx is None:
                        container.append(value)
                    else:
                        container[idx] = value
            elif isinstance(container, dict):
                container[last] = value
            else:
                raise PatchError(f"add: unsupported container type at '{path}'")

            # PR-12: mirror a brand-new template-level ANP/EPG into every
            # site already associated with this template — see
            # _mirror_template_anp_to_sites for the real-NDO-4.x rationale.
            normalized_path = "/".join(tokens)
            bd_match = _TEMPLATE_BD_ADD_RE.match(normalized_path)
            if bd_match and isinstance(value, dict):
                bd_name = value.get("name")
                if bd_name:
                    _mirror_template_bd_to_sites(doc, bd_match.group(1), bd_name, doc.get("id"))
            anp_match = _TEMPLATE_ANP_ADD_RE.match(normalized_path)
            if anp_match and isinstance(value, dict):
                anp_name = value.get("name")
                if anp_name:
                    _mirror_template_anp_to_sites(doc, anp_match.group(1), anp_name, doc.get("id"))
            epg_match = _TEMPLATE_EPG_ADD_RE.match(normalized_path)
            if epg_match and isinstance(value, dict):
                epg_name = value.get("name")
                if epg_name:
                    _mirror_template_epg_to_sites(doc, epg_match.group(1), epg_match.group(2), epg_name, doc.get("id"))
            contract_match = _TEMPLATE_CONTRACT_ADD_RE.match(normalized_path)
            if contract_match and isinstance(value, dict):
                contract_name = value.get("name")
                if contract_name:
                    _mirror_template_contract_to_sites(doc, contract_match.group(1), contract_name, doc.get("id"))

        elif op == "replace":
            if isinstance(container, list):
                if _is_list_index(last):
                    idx = int(last)
                else:
                    idx = _find_by_name(container, last)
                if idx is None or idx >= len(container):
                    raise PatchError(f"replace: '{last}' not found at '{path}'")
                container[idx] = value
            elif isinstance(container, dict):
                container[last] = value
            else:
                raise PatchError(f"replace: unsupported container type at '{path}'")

        elif op == "remove":
            if isinstance(container, list):
                if _is_list_index(last):
                    idx = int(last)
                else:
                    idx = _find_by_name(container, last)
                if idx is not None and idx < len(container):
                    container.pop(idx)
            elif isinstance(container, dict):
                container.pop(last, None)

        # else: unknown op — ignore rather than fail the whole batch.

    return doc
