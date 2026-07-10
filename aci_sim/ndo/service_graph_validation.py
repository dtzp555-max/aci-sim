"""F4 + F12 — NDO server-side service-graph fidelity enforcements.

See `_F4_F12_VALIDATION_DESIGN.md` (repo root) for the full design. Summary:

- **F4 — device-existence gate.** When a site-local service-graph binding op
  (`/sites/{siteId}-{templateName}/serviceGraphs/...`) references an L4-L7
  device (`serviceNodes[].device.dn = uni/tn-<tenant>/lDevVip-<dev>`), that
  device must already exist as a `vnsLDevVip` MO on the TARGET SITE's APIC
  (cross-plane check via `apic_states`). A phase2-only bind (device never
  created in phase1) is the confirmed sim gap this closes.
- **F12 — uniform per-fabric redirect gate.** A per-fabric redirect
  (`serviceGraphRelationship` carrying a `redirectPolicy`) written onto a
  site-local contract must land on EVERY fabric of that contract's template
  in the SAME request (NDO validates the final post-request state, not
  per-op) — a partial (single-fabric) write is illegal; one atomic
  multi-op PATCH covering every fabric is legal.

Both enforcements are fail-safe / default-allow (design §4): they only raise
on a *proven* violation with the real-gear golden string; every ambiguity
(unparsable DN, unsimulated site, missing key, empty op set, single-fabric
topology) passes through untouched. Mounted from `ndo/app.py::patch_schema`
only — `apply_json_patch` (`ndo/patch.py`) stays pure and unaware of either
rule (the template PATCH route reuses it and has no service graphs).
"""
from __future__ import annotations

import re
from typing import Any


class ServiceGraphValidationError(Exception):
    """Raised when a service-graph PATCH violates an NDO server-side
    structural fidelity rule (F4 device-existence or F12 uniform redirect).

    Caught by `ndo/app.py::patch_schema` and mapped to
    `HTTPException(status_code=400, detail=str(exc))` — the same 400
    envelope `PatchError` already uses on that route.
    """


# ---------------------------------------------------------------------------
# Shared gate — does this PATCH's op list touch service-graph surface at all?
# ---------------------------------------------------------------------------
#
# `patch_schema` uses this to decide whether to take the gated
# copy-validate-commit path (needed for F12's post-request-state check) or
# leave the ~95% of PATCHes that are BD/EPG/subnet/contract-filter on the
# current in-place fast path, byte-identical (design §3.2/§4).


def _touches_service_graph(op: Any) -> bool:
    """True if a single op's path names either service-graph surface:
    the site-local `serviceGraphs` collection (F4's trigger) or a
    site-local contract's `serviceGraphRelationship` (F12's trigger)."""
    if not isinstance(op, dict):
        return False
    path = op.get("path")
    if not isinstance(path, str):
        return False
    return "/serviceGraphs" in path or path.endswith("/serviceGraphRelationship")


def service_graph_relevant(ops: list[dict] | None) -> bool:
    """True if any op in *ops* touches service-graph surface — the gate for
    `patch_schema`'s copy-validate-commit path."""
    return any(_touches_service_graph(op) for op in (ops or []))


# ---------------------------------------------------------------------------
# F4 — device-existence gate at service-graph bind
# ---------------------------------------------------------------------------

#: `serviceNodes[].device.dn` shape (design §1.3):
#: `uni/tn-<tenant>/lDevVip-<dev>`. Anything that doesn't match this exact
#: device-ref shape is not a ref F4 owns — skip it (fail-safe).
_DEVICE_DN_RE = re.compile(r"^uni/tn-(?P<tenant>[^/]+)/lDevVip-(?P<dev>.+)$")


def _iter_device_dns(value: Any):
    """Yield every `device.dn` string carried by a service-graph op's
    *value* — both the whole-object shape (`serviceNodes[*].device.dn`) and
    a top-level `value["device"]["dn"]` (forward-safety for a narrower
    single-node add/replace, design §2.3 step 3)."""
    if not isinstance(value, dict):
        return
    device = value.get("device")
    if isinstance(device, dict):
        dn = device.get("dn")
        if isinstance(dn, str) and dn:
            yield dn
    nodes = value.get("serviceNodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_device = node.get("device")
            if isinstance(node_device, dict):
                dn = node_device.get("dn")
                if isinstance(dn, str) and dn:
                    yield dn


def _fabric_name(state: Any, site_id: str) -> str:
    """Resolve *site_id* to its NDO fabric display name
    (`NdoState.sites[].name`, e.g. `"LAB1-IT-ACI"`), falling back to the
    bare site_id if the site isn't found (should not normally happen, but
    keep the golden-message builder total)."""
    for entry in getattr(state, "sites", None) or []:
        if isinstance(entry, dict) and str(entry.get("id")) == site_id:
            name = entry.get("name")
            if name:
                return name
    return site_id


def _validate_service_graph_device_refs(
    ops: list[dict], apic_states: dict | None, state: Any, detail: dict | None
) -> None:
    """Pre-scan *ops* (BEFORE `apply_json_patch` mutates *detail*) for
    site-local service-graph device bindings and raise
    `ServiceGraphValidationError` if a referenced device doesn't exist as a
    `vnsLDevVip` MO on the target site's APIC store.

    *detail* is the CURRENT (pre-mutation) schema doc — needed to resolve
    the `{siteId}-{templateName}` composite path segment to a concrete
    `siteId` via `detail["sites"]`, the same composite `_find_by_name`
    (`ndo/patch.py`) uses. Not part of the design doc's abbreviated 3-arg
    signature shorthand, but required by its own §2.3 algorithm (which
    reads `detail["sites"]`) — see this module's docstring / the
    implementer's report for this minor, functionally-necessary deviation.

    Fail-safe on every ambiguity (design §4): missing ops/apic_states/
    detail, an unresolvable site composite, an unparsable device DN, a
    site absent from *apic_states* (not simulated) — all pass through.
    Only raises when a device DN parses AND its site IS simulated AND the
    store proves the device absent (or present under the wrong class).
    """
    if not ops or not apic_states or not detail:
        return

    sites = detail.get("sites")
    if not isinstance(sites, list) or not sites:
        return

    for entry in ops:
        if not isinstance(entry, dict) or entry.get("op") not in ("add", "replace"):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path.startswith("/sites/") or "/serviceGraphs" not in path:
            continue

        # design §2.3 step 1: `seg = path.split("/")[2]` — the
        # "{siteId}-{templateName}" composite. Never split on "-"
        # (template names may contain it).
        segments = path.split("/")
        if len(segments) < 3:
            continue
        seg = segments[2]

        site_entry = None
        for candidate in sites:
            if not isinstance(candidate, dict):
                continue
            if f"{candidate.get('siteId')}-{candidate.get('templateName')}" == seg:
                site_entry = candidate
                break
        if site_entry is None:
            # Unresolvable composite — can't prove anything. Fail-safe.
            continue

        site_id = str(site_entry.get("siteId", ""))
        if not site_id:
            continue

        for dn in _iter_device_dns(entry.get("value")):
            match = _DEVICE_DN_RE.match(dn)
            if not match:
                # Not a device ref this rule owns — fail-safe skip.
                continue

            apic_state = apic_states.get(site_id)
            if apic_state is None:
                # Site not simulated in this process — can't prove
                # absence. Fail-safe skip (design §2.3 step 3 bullet 2).
                continue
            store = getattr(apic_state, "store", None)
            if store is None:
                continue

            mo = store.get(dn)
            if mo is not None and getattr(mo, "class_name", None) == "vnsLDevVip":
                continue  # device exists — legal (phase1 already built it)

            tenant = match.group("tenant")
            dev = match.group("dev")
            fabric_name = _fabric_name(state, site_id)
            raise ServiceGraphValidationError(
                f"Service graph device {dev} does not exist in tenant {tenant} "
                f"in Fabric {fabric_name}"
            )


# ---------------------------------------------------------------------------
# F12 — uniform per-fabric redirect gate
# ---------------------------------------------------------------------------

#: Matches a site-local contract's redirect bind path exactly:
#: `/sites/{siteId}-{templateName}/contracts/{contract}/serviceGraphRelationship`.
#: Template-level graph binds (`/templates/...`) never match this (they
#: don't start with `/sites/`), so the template-level "Bind Service Graph to
#: Contract" task never trips F12 (design §3.3 step 1 / §4).
#:
#: The composite `{siteId}-{templateName}` segment is captured too (not just
#: split off) — see `_resolve_template_name`, which resolves it back to a
#: templateName so a touched op can be scoped to ITS OWN template (fixes the
#: cross-template false-reject below).
_CONTRACT_REDIRECT_PATH_RE = re.compile(
    r"^/sites/(?P<site_tmpl>[^/]+)/contracts/(?P<contract>[^/]+)/serviceGraphRelationship$"
)


def _basename(ref: Any) -> str:
    """Bare object name from a canonical NDO string ref
    (`/schemas/.../contracts/con-X` -> `con-X`). `working` is the
    POST-`apply_json_patch` document, so every `contractRef` has already
    been through `_stringify_refs` — no dict-form handling needed here
    (unlike `deploy_mirror._basename`, which must also read PRE-patch dict
    refs)."""
    if isinstance(ref, str) and ref:
        return ref.rsplit("/", 1)[-1]
    return ""


def _value_has_redirect_policy(value: Any) -> bool:
    """True if a `serviceGraphRelationship` *value* carries at least one
    `serviceNodesRelationship[*].{consumerConnector|providerConnector}
    .redirectPolicy` — i.e. it's an actual redirect bind, not merely a
    graph-to-contract bind with no redirect (design §3.3 step 1 / §1.4).

    Used BOTH to decide whether an incoming op counts as "touching" a
    redirect (trigger detection) AND, on the post-state `working` doc, to
    decide whether a given fabric's site-local contract shadow already
    carries a redirect (coverage detection) — one shared predicate keeps
    the two checks from silently drifting apart.
    """
    if not isinstance(value, dict):
        return False
    nodes = value.get("serviceNodesRelationship")
    if not isinstance(nodes, list):
        return False
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for connector_key in ("consumerConnector", "providerConnector"):
            connector = node.get(connector_key)
            if isinstance(connector, dict) and connector.get("redirectPolicy"):
                return True
    return False


def _resolve_template_name(sites: list, seg: str) -> str | None:
    """Resolve a `{siteId}-{templateName}` composite path segment (*seg*)
    back to its bare templateName via *sites* (`working["sites"]`) — the
    same composite `_find_by_name` (`ndo/patch.py`) uses to address a
    site-local op's target entry in the first place, and the same
    resolution `_validate_service_graph_device_refs` (F4, above) already
    does against `detail["sites"]`. Never split on "-" (template names may
    legitimately contain it). Returns None if the composite can't be
    resolved (fail-safe — see caller)."""
    for candidate in sites or []:
        if not isinstance(candidate, dict):
            continue
        if f"{candidate.get('siteId')}-{candidate.get('templateName')}" == seg:
            return candidate.get("templateName")
    return None


def _touched_redirect_contracts(sites: list, ops: list[dict] | None) -> set[tuple[str, str]]:
    """`(templateName, contract)` pairs touched by a redirect-carrying
    site-local `serviceGraphRelationship` op in *ops* (design §3.3 step 1).

    Carries the owning templateName alongside the bare contract name —
    two DIFFERENT templates in the same schema may legitimately carry a
    same-named contract (e.g. `con-X` in both "LAB1-LAB2" and "LAB1"), and
    a touched op only proves ITS OWN template was touched. A bare-name-only
    touched set (pre-fix) let `_validate_uniform_site_redirect` sweep every
    template's same-named contract into one coverage check, so a legal
    atomic write on template A's `con-X` could be 400'd by template B's
    unrelated, pre-existing non-uniform `con-X` state — a scope-invariant
    violation (design §3.3 step 3: "only contracts touched by THIS
    request... never retroactively reject unrelated/pre-existing state").
    *sites* (`working["sites"]`) resolves each op's composite path segment
    to a concrete templateName; an unresolvable composite is fail-safe
    skipped (can't prove which template was touched, so can't prove a
    violation either)."""
    touched: set[tuple[str, str]] = set()
    for op in ops or []:
        if not isinstance(op, dict) or op.get("op") not in ("add", "replace"):
            continue
        path = op.get("path")
        if not isinstance(path, str):
            continue
        match = _CONTRACT_REDIRECT_PATH_RE.match(path)
        if not match:
            continue
        if not _value_has_redirect_policy(op.get("value")):
            continue
        template_name = _resolve_template_name(sites, match.group("site_tmpl"))
        if template_name is None:
            continue
        touched.add((template_name, match.group("contract")))
    return touched


def _site_configures_redirect(site_entry: dict, contract: str) -> bool:
    """True if *site_entry*'s mirrored `contracts[]` shadow for *contract*
    already carries a redirect-policy-bearing `serviceGraphRelationship`."""
    for candidate in site_entry.get("contracts", []) or []:
        if not isinstance(candidate, dict):
            continue
        if _basename(candidate.get("contractRef")) != contract:
            continue
        return _value_has_redirect_policy(candidate.get("serviceGraphRelationship"))
    return False


def _validate_uniform_site_redirect(working: dict, ops: list[dict]) -> None:
    """Post-request-state check (design §3.1/§3.3): after *ops* have been
    applied into *working* (a deepcopy — see `patch_schema`'s
    copy-validate-commit), every (template, contract) pair touched by a
    redirect bind in *ops* must carry that redirect on ALL fabrics of ITS
    OWN template — never on only SOME. Raises `ServiceGraphValidationError`
    with the golden message on a partial (non-uniform) write.

    "Uniform" is COVERAGE (presence of a redirectPolicy on every fabric),
    never DN equality — redirect-policy DNs are legitimately per-fabric
    (design §3.4). Only the (template, contract) pairs actually touched by
    THIS request's ops are checked, so pre-existing state, unrelated
    contracts, AND a differently-scoped same-named contract in a sibling
    template are never retroactively rejected (design §3.3 step 3 — see
    `_touched_redirect_contracts`'s docstring for the cross-template
    false-reject this scoping fixes). A template with only one associated
    fabric is trivially uniform (design §3.4 / §4).
    """
    sites = working.get("sites")
    if not isinstance(sites, list):
        return

    touched = _touched_redirect_contracts(sites, ops)
    if not touched:
        return

    for template_name, contract in touched:
        # The coverage set is every site entry belonging to THIS touched
        # op's OWN template that also carries a mirrored contracts[] shadow
        # for this contract — never a same-named contract in a different
        # template (that was the bug: grouping by templateName across ALL
        # site entries carrying the bare contract name, regardless of
        # which template the incoming op actually touched).
        group = [
            site_entry
            for site_entry in sites
            if isinstance(site_entry, dict)
            and site_entry.get("templateName") == template_name
            and any(
                isinstance(c, dict) and _basename(c.get("contractRef")) == contract
                for c in (site_entry.get("contracts") or [])
            )
        ]

        total = len(group)
        if total <= 1:
            # Single-fabric topology — trivially uniform (design §3.4).
            continue
        configured = sum(1 for s in group if _site_configures_redirect(s, contract))
        if 0 < configured < total:
            raise ServiceGraphValidationError(
                "must have uniform redirect policy configured on all fabrics"
            )
