"""F12 — NDO uniform per-fabric service-graph redirect gate.

See aci_sim/ndo/service_graph_validation.py's docstring for the full F12
rule. Confirmed sim gap: a single-fabric (partial) `serviceGraphRelationship`
redirect write used to succeed in the sim while real NDO 400s with `must
have uniform redirect policy configured on all fabrics` — NDO validates the
FINAL POST-REQUEST state, so a legal atomic multi-op PATCH covering every
fabric of a template in ONE request must still pass.

Replicates the mso-model role's "Atomic PATCH — bind service-graph redirect
on ALL fabrics in one request" task (design §1.4) — the exact shape
`tests/test_pr13_ndo_dhcp_svcgraph.py::
test_atomic_service_graph_redirect_patch_on_site_local_contract_end_to_end`
already exercises (that pre-existing test's own atomic 2-op PATCH must stay
green under F12 — this file duplicates its shape as a dedicated regression
anchor), then adds the partial-write / single-fabric-topology / fail-safe
cases design §4.1's evidence-gate table calls for.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aci_sim.ndo.app import make_ndo_app
from aci_sim.ndo.model import build_ndo_model
from aci_sim.ndo.service_graph_validation import (
    ServiceGraphValidationError,
    _validate_uniform_site_redirect,
    service_graph_relevant,
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


def _redirect_value(schema_id, tmpl_name, consumer_dn, provider_dn):
    """A per-fabric redirect bind — design §1.4 wire shape. Consumer/
    provider redirect-policy DNs are deliberately DISTINCT per call site,
    matching the real per-fabric DNs the design's §3.4 "coverage, not
    equality" rule is built around."""
    return {
        "serviceGraphRef": {
            "schemaId": schema_id,
            "serviceGraphName": "sgt-FW_LAB0",
            "templateName": tmpl_name,
        },
        "serviceNodesRelationship": [
            {
                "serviceNodeRef": {
                    "schemaId": schema_id,
                    "serviceGraphName": "sgt-FW_LAB0",
                    "serviceNodeName": "node1",
                    "templateName": tmpl_name,
                },
                "consumerConnector": {
                    "clusterInterface": {"dn": "uni/tn-MS-TN2/x"},
                    "redirectPolicy": {"dn": consumer_dn},
                    "subnets": [],
                },
                "providerConnector": {
                    "clusterInterface": {"dn": "uni/tn-MS-TN2/x"},
                    "redirectPolicy": {"dn": provider_dn},
                },
            }
        ],
    }


def _two_site_schema(client):
    payload = {
        "displayName": "F12-TEST-MS",
        "templates": [
            {"name": "LAB1-LAB2", "displayName": "LAB1-LAB2", "tenantId": "t-f12", "contracts": []}
        ],
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
    return schema_id


def _one_site_schema(client):
    payload = {
        "displayName": "F12-TEST-SF",
        "templates": [
            {"name": "LAB1-only", "displayName": "LAB1-only", "tenantId": "t-f12-sf", "contracts": []}
        ],
        "sites": [{"siteId": "1", "templateName": "LAB1-only"}],
    }
    schema_id = client.post("/mso/api/v1/schemas", json=payload).json()["id"]
    client.patch(
        f"/mso/api/v1/schemas/{schema_id}",
        json=[
            {
                "op": "add",
                "path": "/templates/LAB1-only/contracts/-",
                "value": {"name": "con-Firewall_LAB0", "scope": "vrf", "filterRelationships": []},
            }
        ],
    )
    return schema_id


# ---------------------------------------------------------------------------
# App-level: the legal atomic flow that MUST stay green (design §4.1)
# ---------------------------------------------------------------------------


def test_atomic_all_fabric_redirect_in_one_patch_passes(client):
    schema_id = _two_site_schema(client)
    ops = [
        {
            "op": "add",
            "path": f"/sites/{site_id}-LAB1-LAB2/contracts/con-Firewall_LAB0/serviceGraphRelationship",
            "value": _redirect_value(
                schema_id, "LAB1-LAB2", f"uni/tn-MS-TN2/rp-{site_id}-c", f"uni/tn-MS-TN2/rp-{site_id}-p"
            ),
        }
        for site_id in ("1", "2")
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=ops)
    assert resp.status_code == 200

    fresh = client.get(f"/mso/api/v1/schemas/{schema_id}").json()
    for site_id in ("1", "2"):
        site = next(s for s in fresh["sites"] if s["siteId"] == site_id)
        contract = next(
            c for c in site["contracts"] if c["contractRef"].endswith("/contracts/con-Firewall_LAB0")
        )
        assert contract["serviceGraphRelationship"]["serviceGraphRef"].endswith(
            "/serviceGraphs/sgt-FW_LAB0"
        )


# ---------------------------------------------------------------------------
# The confirmed violation F12 closes: a single-fabric PARTIAL write
# ---------------------------------------------------------------------------


def test_single_fabric_partial_write_400s_with_golden_message(client):
    schema_id = _two_site_schema(client)
    ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1-LAB2/contracts/con-Firewall_LAB0/serviceGraphRelationship",
            "value": _redirect_value(schema_id, "LAB1-LAB2", "uni/tn-MS-TN2/rp-1-c", "uni/tn-MS-TN2/rp-1-p"),
        }
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=ops)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "must have uniform redirect policy configured on all fabrics"


def test_single_fabric_partial_write_does_not_mutate_stored_schema(client):
    """All-or-nothing: a rejected partial redirect must not leave site '1'
    carrying the redirect either — the copy-validate-commit path only
    commits `working` back into `state.schema_details` on success (design
    §3.2, incidentally also fixing the pre-existing partial-write-on-
    PatchError bug for these PATCHes)."""
    schema_id = _two_site_schema(client)
    ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1-LAB2/contracts/con-Firewall_LAB0/serviceGraphRelationship",
            "value": _redirect_value(schema_id, "LAB1-LAB2", "uni/tn-MS-TN2/rp-1-c", "uni/tn-MS-TN2/rp-1-p"),
        }
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=ops)
    assert resp.status_code == 400

    fresh = client.get(f"/mso/api/v1/schemas/{schema_id}").json()
    site1 = next(s for s in fresh["sites"] if s["siteId"] == "1")
    contract = next(
        c for c in site1["contracts"] if c["contractRef"].endswith("/contracts/con-Firewall_LAB0")
    )
    assert contract.get("serviceGraphRelationship") is None


def test_second_atomic_patch_after_rejected_partial_still_succeeds(client):
    """A retried, now-complete atomic PATCH (covering both fabrics) after
    an earlier rejected partial write must still succeed cleanly — proves
    the rejected write left no residue that could poison a later uniform
    check."""
    schema_id = _two_site_schema(client)
    bad_ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1-LAB2/contracts/con-Firewall_LAB0/serviceGraphRelationship",
            "value": _redirect_value(schema_id, "LAB1-LAB2", "uni/tn-MS-TN2/rp-1-c", "uni/tn-MS-TN2/rp-1-p"),
        }
    ]
    assert client.patch(f"/mso/api/v1/schemas/{schema_id}", json=bad_ops).status_code == 400

    good_ops = [
        {
            "op": "add",
            "path": f"/sites/{site_id}-LAB1-LAB2/contracts/con-Firewall_LAB0/serviceGraphRelationship",
            "value": _redirect_value(
                schema_id, "LAB1-LAB2", f"uni/tn-MS-TN2/rp-{site_id}-c", f"uni/tn-MS-TN2/rp-{site_id}-p"
            ),
        }
        for site_id in ("1", "2")
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=good_ops)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Single-fabric topology — trivially uniform (design §3.4 / §4)
# ---------------------------------------------------------------------------


def test_single_fabric_topology_redirect_write_passes(client):
    schema_id = _one_site_schema(client)
    ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1-only/contracts/con-Firewall_LAB0/serviceGraphRelationship",
            "value": _redirect_value(schema_id, "LAB1-only", "uni/tn-SF/rp-c", "uni/tn-SF/rp-p"),
        }
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=ops)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Cross-template scope regression (reviewer-confirmed MEDIUM false-reject)
#
# `_touched_redirect_contracts` used to collect BARE contract names, losing
# which template the touched op actually belonged to.
# `_validate_uniform_site_redirect`'s coverage grouping then evaluated
# EVERY template's same-named contract, not just the touched op's own
# template — so a legal atomic full-fabric redirect PATCH on template A's
# `con-X` could be 400'd purely because an UNRELATED template B also had a
# `con-X` sitting in a pre-existing non-uniform state. Fixed by scoping the
# touched set to `(templateName, contract)` pairs (design §3.3 step 3's
# scope invariant — "only contracts touched by THIS request... never
# retroactively reject unrelated state" — extended to also mean "never a
# different template's same-named contract").
# ---------------------------------------------------------------------------


def _two_template_schema_with_non_uniform_sibling(client):
    """A schema born (via a single `POST /schemas`, the body stored
    verbatim per `create_schema`'s own docstring) already carrying TWO
    templates that both have a contract named `con-Firewall_LAB0`:

      - "LAB1-LAB2": 2 fabrics (sites "1","2"), NEITHER configured yet —
        the template under test.
      - "LAB1": 2 fabrics (sites "1","2" again — a site can legitimately
        be associated with more than one template in the same schema),
        already non-uniform: fabric "1" carries a redirect, fabric "2"
        does not.

    This is the reviewer's probe scenario reached the only way it's
    actually reachable: as the schema's STARTING state (e.g. an
    imported/migrated schema), never by hand-mutating in-process Python
    state — F12's own write gate makes a non-uniform state unreachable via
    any PATCH once a contract has 2+ associated fabrics, so
    "LAB1"'s non-uniform shadow can only be POSTed directly. The exact
    schema_id prefix on each `contractRef` is irrelevant to `_basename`
    (design: bare trailing segment only), so a placeholder is fine.
    """
    contract_tmpl = {"name": "con-Firewall_LAB0", "scope": "vrf", "filterRelationships": []}
    payload = {
        "displayName": "F12-XTMPL-TEST",
        "templates": [
            {"name": "LAB1-LAB2", "displayName": "LAB1-LAB2", "tenantId": "t-f12x", "contracts": [contract_tmpl]},
            {"name": "LAB1", "displayName": "LAB1", "tenantId": "t-f12x", "contracts": [contract_tmpl]},
        ],
        "sites": [
            {
                "siteId": "1",
                "templateName": "LAB1-LAB2",
                "contracts": [{"contractRef": "/schemas/x/templates/LAB1-LAB2/contracts/con-Firewall_LAB0"}],
            },
            {
                "siteId": "2",
                "templateName": "LAB1-LAB2",
                "contracts": [{"contractRef": "/schemas/x/templates/LAB1-LAB2/contracts/con-Firewall_LAB0"}],
            },
            {
                "siteId": "1",
                "templateName": "LAB1",
                "contracts": [
                    {
                        "contractRef": "/schemas/x/templates/LAB1/contracts/con-Firewall_LAB0",
                        "serviceGraphRelationship": _redirect_value(
                            "x", "LAB1", "uni/tn-X/rp-b1-c", "uni/tn-X/rp-b1-p"
                        ),
                    }
                ],
            },
            {
                "siteId": "2",
                "templateName": "LAB1",
                "contracts": [{"contractRef": "/schemas/x/templates/LAB1/contracts/con-Firewall_LAB0"}],
            },
        ],
    }
    return client.post("/mso/api/v1/schemas", json=payload).json()["id"]


def test_atomic_full_fabric_redirect_not_rejected_by_unrelated_template_same_contract_name(client):
    """The reviewer's confirmed MEDIUM false-reject: a second template
    ("LAB1", same schema) also carries a `con-Firewall_LAB0` contract and
    is already non-uniform (1-of-2 fabrics configured). A legal atomic
    full-fabric redirect PATCH on the UNRELATED template "LAB1-LAB2"'s own
    `con-Firewall_LAB0` must still succeed — pre-fix this 400'd, sunk by
    template "LAB1"'s unrelated non-uniform state leaking into the
    coverage check via the old bare-contract-name touched set."""
    schema_id = _two_template_schema_with_non_uniform_sibling(client)

    ops = [
        {
            "op": "add",
            "path": f"/sites/{site_id}-LAB1-LAB2/contracts/con-Firewall_LAB0/serviceGraphRelationship",
            "value": _redirect_value(
                schema_id, "LAB1-LAB2", f"uni/tn-MS-TN2/rp-{site_id}-c", f"uni/tn-MS-TN2/rp-{site_id}-p"
            ),
        }
        for site_id in ("1", "2")
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=ops)
    assert resp.status_code == 200

    fresh = client.get(f"/mso/api/v1/schemas/{schema_id}").json()
    for site_id in ("1", "2"):
        site = next(s for s in fresh["sites"] if s["siteId"] == site_id and s["templateName"] == "LAB1-LAB2")
        contract = next(
            c for c in site["contracts"] if c["contractRef"].endswith("/contracts/con-Firewall_LAB0")
        )
        assert contract["serviceGraphRelationship"]["serviceGraphRef"].endswith(
            "/serviceGraphs/sgt-FW_LAB0"
        )

    # Template "LAB1"'s own unrelated non-uniform state is untouched by any
    # of this — still 1-of-2, never retroactively "fixed" or re-evaluated,
    # and never what actually gated the request above.
    lab1_sites = [s for s in fresh["sites"] if s["templateName"] == "LAB1"]
    configured_count = sum(
        1
        for s in lab1_sites
        for c in s.get("contracts", [])
        if c.get("contractRef", "").endswith("/contracts/con-Firewall_LAB0")
        and c.get("serviceGraphRelationship") is not None
    )
    assert configured_count == 1


def test_own_template_partial_write_still_rejected_with_unrelated_sibling_non_uniform(client):
    """Reverse check, same fixture: with template "LAB1" ALSO already
    non-uniform, a PARTIAL (single-fabric) redirect write on template
    "LAB1-LAB2"'s own `con-Firewall_LAB0` must still 400 with the golden
    message — the (templateName, contract) scoping fix must narrow F12's
    check to the touched op's own template, not accidentally widen it into
    "never raise for this contract name again"."""
    schema_id = _two_template_schema_with_non_uniform_sibling(client)

    ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1-LAB2/contracts/con-Firewall_LAB0/serviceGraphRelationship",
            "value": _redirect_value(schema_id, "LAB1-LAB2", "uni/tn-MS-TN2/rp-1-c", "uni/tn-MS-TN2/rp-1-p"),
        }
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=ops)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "must have uniform redirect policy configured on all fabrics"


# ---------------------------------------------------------------------------
# Fail-safe / default-allow — never reject on ambiguity (design §4)
# ---------------------------------------------------------------------------


def test_template_level_graph_bind_never_triggers_f12(client):
    """The template-level "Bind Service Graph to Contract" task (no
    redirect, `/templates/...`) must never trip F12 (design §3.3/§4) —
    even though its path also ends `/serviceGraphRelationship` (so the
    sg_relevant gate DOES engage the copy path), F12's own contract-touch
    scan requires a `/sites/...` prefix and must find nothing to check."""
    schema_id = _two_site_schema(client)
    ops = [
        {
            "op": "add",
            "path": "/templates/LAB1-LAB2/contracts/con-Firewall_LAB0/serviceGraphRelationship",
            "value": {
                "serviceGraphRef": {
                    "schemaId": schema_id,
                    "serviceGraphName": "sgt-FW_LAB0",
                    "templateName": "LAB1-LAB2",
                },
                "serviceNodesRelationship": [],
            },
        }
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=ops)
    assert resp.status_code == 200


def test_non_service_graph_patch_untouched(client):
    """A plain BD add must never engage the copy-validate-commit path, and
    must succeed exactly as before F4/F12 existed — a named regression
    anchor for the "byte-identical fast path" guarantee (design §3.2/§4),
    on top of the full pre-existing NDO suite this same wiring is verified
    against."""
    schema_id = _two_site_schema(client)
    ops = [
        {
            "op": "add",
            "path": "/templates/LAB1-LAB2/bds/-",
            "value": {"name": "bd-App1_LAB0", "displayName": "bd-App1_LAB0", "subnets": []},
        }
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=ops)
    assert resp.status_code == 200


def test_service_graph_relevant_gate_scope():
    assert (
        service_graph_relevant([{"op": "add", "path": "/sites/1-LAB1/serviceGraphs/-", "value": {}}])
        is True
    )
    assert (
        service_graph_relevant(
            [{"op": "add", "path": "/sites/1-LAB1/contracts/c/serviceGraphRelationship", "value": {}}]
        )
        is True
    )
    assert service_graph_relevant([{"op": "add", "path": "/templates/LAB1/bds/-", "value": {}}]) is False
    assert service_graph_relevant([]) is False
    assert service_graph_relevant(None) is False


# ---------------------------------------------------------------------------
# Unit level: _validate_uniform_site_redirect directly
# ---------------------------------------------------------------------------


def _contract_entry(configured: bool, rp_suffix: str) -> dict:
    entry = {"contractRef": "/schemas/s/templates/LAB1-LAB2/contracts/con-Firewall_LAB0"}
    if configured:
        entry["serviceGraphRelationship"] = _redirect_value(
            "s", "LAB1-LAB2", f"uni/tn-X/rp-{rp_suffix}-c", f"uni/tn-X/rp-{rp_suffix}-p"
        )
    return entry


def _working_doc(site1_configured: bool, site2_configured: bool) -> dict:
    return {
        "sites": [
            {"siteId": "1", "templateName": "LAB1-LAB2", "contracts": [_contract_entry(site1_configured, "1")]},
            {"siteId": "2", "templateName": "LAB1-LAB2", "contracts": [_contract_entry(site2_configured, "2")]},
        ]
    }


def _touch_op(site_id: str) -> dict:
    return {
        "op": "add",
        "path": f"/sites/{site_id}-LAB1-LAB2/contracts/con-Firewall_LAB0/serviceGraphRelationship",
        "value": _redirect_value("s", "LAB1-LAB2", "uni/tn-X/rp-c", "uni/tn-X/rp-p"),
    }


def test_unit_uniform_both_configured_passes():
    working = _working_doc(True, True)
    _validate_uniform_site_redirect(working, [_touch_op("1"), _touch_op("2")])  # must not raise


def test_unit_partial_one_configured_raises():
    working = _working_doc(True, False)
    with pytest.raises(
        ServiceGraphValidationError, match="must have uniform redirect policy configured on all fabrics"
    ):
        _validate_uniform_site_redirect(working, [_touch_op("1")])


def test_unit_pre_existing_non_uniform_state_not_retroactively_rejected():
    """Design §3.3 step 3: only contracts TOUCHED by this request's ops are
    checked — pre-existing (already non-uniform) state on an untouched
    contract must never be retroactively rejected."""
    working = _working_doc(True, False)  # already non-uniform pre-existing state
    unrelated_op = {"op": "add", "path": "/templates/LAB1-LAB2/bds/-", "value": {}}
    _validate_uniform_site_redirect(working, [unrelated_op])  # must not raise


def test_unit_empty_ops_is_noop():
    working = _working_doc(True, False)
    _validate_uniform_site_redirect(working, [])  # must not raise


def test_unit_single_fabric_group_never_raises():
    working = {
        "sites": [
            {"siteId": "1", "templateName": "LAB1-only", "contracts": [_contract_entry(True, "1")]},
        ]
    }
    op = {
        "op": "add",
        "path": "/sites/1-LAB1-only/contracts/con-Firewall_LAB0/serviceGraphRelationship",
        "value": _redirect_value("s", "LAB1-only", "uni/tn-X/rp-c", "uni/tn-X/rp-p"),
    }
    _validate_uniform_site_redirect(working, [op])  # must not raise


# ---------------------------------------------------------------------------
# Unit level: cross-template scope regression, minimal reproduction
#
# Same reviewer-confirmed bug as the app-level
# `test_atomic_full_fabric_redirect_not_rejected_by_unrelated_template_
# same_contract_name` above, isolated to the pure function with a
# hand-built `working` doc — the fastest, most direct probe of
# `_touched_redirect_contracts`'s (templateName, contract) scoping fix.
# ---------------------------------------------------------------------------


def _cross_template_working_doc(
    a1_configured: bool, a2_configured: bool, b1_configured: bool, b2_configured: bool
) -> dict:
    """Two templates, same schema, same-named contract `con-Firewall_LAB0`:
    - "LAB1-LAB2" (template A, sites "1"/"2"): the template under test —
      *a1_configured*/*a2_configured* must reflect the POST-PATCH state
      (i.e. already include whatever the touched ops under test would have
      applied — `_validate_uniform_site_redirect` checks `working` AFTER
      `apply_json_patch`, it never applies *ops* itself; same convention
      `_working_doc` above uses).
    - "LAB1" (template B, sites "3"/"4"): an UNTOUCHED sibling template,
      whose configured/unconfigured shape is caller-controlled — used to
      seed a non-uniform sibling that must never leak into template A's
      coverage check.
    """
    return {
        "sites": [
            {"siteId": "1", "templateName": "LAB1-LAB2", "contracts": [_contract_entry(a1_configured, "a1")]},
            {"siteId": "2", "templateName": "LAB1-LAB2", "contracts": [_contract_entry(a2_configured, "a2")]},
            {"siteId": "3", "templateName": "LAB1", "contracts": [_contract_entry(b1_configured, "b1")]},
            {"siteId": "4", "templateName": "LAB1", "contracts": [_contract_entry(b2_configured, "b2")]},
        ]
    }


def _cross_touch_op(template_name: str, site_id: str) -> dict:
    return {
        "op": "add",
        "path": f"/sites/{site_id}-{template_name}/contracts/con-Firewall_LAB0/serviceGraphRelationship",
        "value": _redirect_value("s", template_name, f"uni/tn-X/rp-{site_id}-c", f"uni/tn-X/rp-{site_id}-p"),
    }


def test_unit_atomic_full_fabric_write_not_rejected_by_sibling_template_non_uniform_state():
    """The reviewer's minimal probe: template "LAB1" (untouched by this
    request) is non-uniform (1-of-2 fabrics configured) for
    `con-Firewall_LAB0`. An atomic, full-fabric redirect write on the
    UNRELATED template "LAB1-LAB2"'s own `con-Firewall_LAB0` (post-patch:
    both its fabrics now configured) must not raise — pre-fix, the
    bare-name touched set caused this same-named-but-different-template
    contract to be swept into the same coverage check, and raised."""
    working = _cross_template_working_doc(
        a1_configured=True, a2_configured=True, b1_configured=True, b2_configured=False
    )
    ops = [_cross_touch_op("LAB1-LAB2", "1"), _cross_touch_op("LAB1-LAB2", "2")]
    _validate_uniform_site_redirect(working, ops)  # must not raise


def test_unit_own_template_partial_write_still_rejected_with_sibling_present():
    """Reverse check: with the exact same non-uniform sibling template
    "LAB1" present, a PARTIAL (single-fabric) write on template
    "LAB1-LAB2"'s own `con-Firewall_LAB0` (post-patch: only fabric "1"
    configured, fabric "2" still not) must still raise — the
    (templateName, contract) scoping must narrow the check to the touched
    op's own template, not silently widen it to "never raise for this
    contract name again"."""
    working = _cross_template_working_doc(
        a1_configured=True, a2_configured=False, b1_configured=True, b2_configured=False
    )
    ops = [_cross_touch_op("LAB1-LAB2", "1")]
    with pytest.raises(
        ServiceGraphValidationError, match="must have uniform redirect policy configured on all fabrics"
    ):
        _validate_uniform_site_redirect(working, ops)
