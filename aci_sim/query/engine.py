"""ACI query engine — maps APIC query patterns to MITStore lookups.

Implements three query shapes (CONTRACT §2):
1. ``run_class_query``     — GET /api/class/{cls}.json
2. ``run_mo_query``        — GET /api/mo/{dn}.json
3. ``run_node_scoped``     — GET /api/node/class/{node_dn}/{cls}.json

All three return ``(imdata: list[dict], total: int)`` where *total* is the
full pre-pagination matched count and *imdata* is the current page slice.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.query.filters import FilterParseError, Predicate, parse_filter


# ---------------------------------------------------------------------------
# QueryParams
# ---------------------------------------------------------------------------


@dataclass
class QueryParams:
    """Carries all APIC query-string parameters for a single request."""

    filter_expr: str | None = None
    rsp_subtree: str | None = None              # "children" | "full"
    rsp_subtree_class: list[str] | None = None
    query_target: str | None = None             # "subtree" (legacy)
    target_subtree_class: list[str] | None = None
    page: int = 0
    page_size: int = 500


def params_from_dict(raw: dict[str, str]) -> QueryParams:
    """Build a :class:`QueryParams` from raw APIC query-string key names.

    APIC key names:
      ``query-target-filter``, ``rsp-subtree``, ``rsp-subtree-class``,
      ``query-target``, ``target-subtree-class``, ``page``, ``page-size``.
    Class-list params are comma-split.
    ``page-size`` is clamped to ``[1, 5000]``; ``page`` must be ``>= 0``.
    Non-numeric or out-of-range ``page``/``page-size`` raise
    :class:`~aci_sim.query.filters.FilterParseError` (mapped by the REST
    layer to an APIC 400 error envelope — never a bare 500 or silent
    truncation).
    """

    def _classes(key: str) -> list[str] | None:
        val = raw.get(key, "").strip()
        if not val:
            return None
        return [c.strip() for c in val.split(",") if c.strip()]

    def _int_param(key: str, default: int) -> int:
        raw_val = raw.get(key)
        if raw_val is None or raw_val == "":
            return default
        try:
            return int(raw_val)
        except ValueError:
            raise FilterParseError(
                f"invalid {key} value {raw_val!r}: must be an integer"
            ) from None

    page = _int_param("page", 0)
    if page < 0:
        raise FilterParseError(f"invalid page value {page!r}: must be >= 0")

    page_size = _int_param("page-size", 500)
    if page_size < 1:
        raise FilterParseError(f"invalid page-size value {page_size!r}: must be >= 1")
    page_size = min(page_size, 5000)

    return QueryParams(
        filter_expr=raw.get("query-target-filter") or None,
        rsp_subtree=raw.get("rsp-subtree") or None,
        rsp_subtree_class=_classes("rsp-subtree-class"),
        query_target=raw.get("query-target") or None,
        target_subtree_class=_classes("target-subtree-class"),
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _predicate(params: QueryParams) -> Predicate:
    return parse_filter(params.filter_expr)


def _wants_subtree(params: QueryParams) -> bool:
    return (
        params.rsp_subtree in ("children", "full")
        or params.query_target == "subtree"
    )


def _subtree_classes(params: QueryParams) -> set[str] | None:
    classes = params.rsp_subtree_class or params.target_subtree_class
    return set(classes) if classes else None


def _nested_children(
    store: MITStore, dn: str, cls_filter: set[str] | None
) -> list[dict[str, Any]]:
    """Build imdata child entries for *dn*, nested recursively by DN parentage.

    Mirrors APIC ``rsp-subtree=full``: every descendant stays nested under its
    real parent (grandchildren under children, not hoisted to the root). When
    *cls_filter* is set, a node is emitted iff its own class is in the filter OR
    it has an emitted descendant — so structural containers on the path to a
    matching grandchild are preserved (matching ``rsp-subtree-class`` semantics).
    MO-less structural intermediates are traversed through: their emitted
    descendants hoist up to this level (there is no MO to emit for them).
    """
    out: list[dict[str, Any]] = []
    for cdn in store.child_dns(dn):
        child_mo = store.get(cdn)
        nested = _nested_children(store, cdn, cls_filter)
        if child_mo is None:
            out.extend(nested)  # structural intermediate → hoist its matches up
            continue
        if cls_filter is None or child_mo.class_name in cls_filter or nested:
            body: dict[str, Any] = {"attributes": dict(child_mo.attrs)}
            if nested:
                body["children"] = nested
            out.append({child_mo.class_name: body})
    return out


def _attach_subtree(store: MITStore, mo: MO, params: QueryParams) -> dict[str, Any]:
    """Produce the imdata entry for *mo* with its descendants nested as children."""
    cls_filter = _subtree_classes(params)
    if params.rsp_subtree == "children":
        # rsp-subtree=children → one level only (direct children)
        children = [d.to_imdata() for d in store.children(mo.dn, classes=cls_filter)]
    else:
        # rsp-subtree=full → full recursive tree, NESTED (hierarchy preserved).
        # Real APIC keeps grandchildren under their parent; flattening them into
        # direct children of the root silently empties multi-level consumers like
        # autoACI contract_map (vzBrCP→vzSubj→vzRsSubjFiltAtt).
        children = _nested_children(store, mo.dn, cls_filter)
    body: dict[str, Any] = {"attributes": dict(mo.attrs)}
    if children:
        body["children"] = children
    return {mo.class_name: body}


def _paginate(items: list[Any], page: int, page_size: int) -> list[Any]:
    start = page * page_size
    return items[start: start + page_size]


def _build_result(
    store: MITStore,
    top_level: list[MO],
    params: QueryParams,
) -> tuple[list[dict], int]:
    """Filter, optionally attach subtrees, then paginate.

    Returns ``(imdata_page, total_matched)``.  *total* is always the full
    pre-pagination count (the caller converts it to a string for the wire
    ``totalCount`` field).
    """
    pred = _predicate(params)

    # Legacy subtree (query-target=subtree, used by autoACI's query_dn(subtree=True)):
    # real APIC returns the matched descendants as FLAT top-level imdata elements
    # (filtered by target-subtree-class; root included only if it matches). autoACI
    # consumers read the target class at the TOP LEVEL of imdata and never recurse into
    # `children` (topology.py ISN/node-detail, fabric_bgp_evpn, l3out_detail, ~60 sites).
    #
    # ``query-target=subtree`` first sets the SCOPE to root+descendants (unfiltered),
    # THEN both the class filter and ``query-target-filter`` predicate are evaluated
    # per scoped MO — never pre-filtering the roots with `pred`. Otherwise a filter on
    # a descendant-only attribute (e.g. faultInst.severity under a topSystem root)
    # would exclude the root before subtree expansion ever runs, returning empty
    # imdata even though matching descendants exist (attr-missing→False on the root
    # is a legitimate non-match for the root itself, but must not gate expansion).
    if params.query_target == "subtree" and params.rsp_subtree is None:
        cls_filter = _subtree_classes(params)
        flat: list[MO] = []
        for root in top_level:
            for mo in [root, *store.subtree(root.dn)]:
                if cls_filter is None or mo.class_name in cls_filter:
                    flat.append(mo)
        flat = [mo for mo in flat if pred(mo)]
        total = len(flat)
        page_items = _paginate(flat, params.page, params.page_size)
        return [mo.to_imdata() for mo in page_items], total

    # Modern rsp-subtree=children|full: NESTED children under each matched object.
    filtered = [mo for mo in top_level if pred(mo)]
    total = len(filtered)
    page_items = _paginate(filtered, params.page, params.page_size)
    if params.rsp_subtree in ("children", "full"):
        imdata = [_attach_subtree(store, mo, params) for mo in page_items]
    else:
        imdata = [mo.to_imdata() for mo in page_items]

    return imdata, total


# ---------------------------------------------------------------------------
# Public query functions
# ---------------------------------------------------------------------------


def run_class_query(
    store: MITStore,
    cls: str,
    params: QueryParams,
) -> tuple[list[dict], int]:
    """Query all MOs of class *cls* in the store."""
    return _build_result(store, store.by_class(cls), params)


def run_mo_query(
    store: MITStore,
    dn: str,
    params: QueryParams,
) -> tuple[list[dict], int]:
    """Query the single MO at *dn* (plus subtree if requested)."""
    mo = store.get(dn)
    if mo is None:
        return [], 0
    return _build_result(store, [mo], params)


def run_node_scoped(
    store: MITStore,
    node_dn: str,
    cls: str,
    params: QueryParams,
) -> tuple[list[dict], int]:
    """Return instances of *cls* whose DN is under *node_dn*.

    Mirrors ``GET /api/node/class/{node_dn}/{cls}.json`` — used for
    node-local MOs such as ``faultInst``.
    """
    prefix = node_dn.rstrip("/") + "/"
    top_level = [mo for mo in store.by_class(cls) if mo.dn.startswith(prefix)]
    return _build_result(store, top_level, params)
