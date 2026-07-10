"""F4 — NDO service-graph device-existence gate.

Design: `_F4_F12_VALIDATION_DESIGN.md` §2 (repo root). Confirmed sim gap: a
phase2-only service-graph bind (skip phase1's APIC-side `vnsLDevVip`
create) used to succeed in the sim while real NDO 400s with
`Service graph device <dev> does not exist in tenant <tenant> in Fabric
<site>`.

Replicates the "Create service graph" task's site-local device bind
(`custom_mso_schema_service_graph.py`, design §1.3) against the real MS-TN1
(LAB1+LAB2) topology-derived schema — the same fixture
`tests/test_pr13_ndo_dhcp_svcgraph.py`'s GAP-2 tests use — but now wires a
cross-plane APIC store via `apic_states` (mirrors
`tests/test_deploy_mirror.py`'s `_FakeApicState` fixture shape) so F4 can
prove/disprove `vnsLDevVip` existence on the target site.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.ndo.app import make_ndo_app
from aci_sim.ndo.model import build_ndo_model
from aci_sim.ndo.service_graph_validation import (
    ServiceGraphValidationError,
    _validate_service_graph_device_refs,
)
from aci_sim.topology.loader import load_topology

TOPOLOGY_YAML = Path(__file__).parent.parent / "topology.yaml"

DEVICE_DN = "uni/tn-MS-TN1/lDevVip-fw-FW_LAB0"


@dataclass
class _FakeApicState:
    """Minimal stand-in for rest_aci.app.ApicSiteState — F4 only ever
    touches `.store` — mirrors test_deploy_mirror.py's own fixture."""

    store: MITStore = field(default_factory=MITStore)


@pytest.fixture()
def topo():
    return load_topology(TOPOLOGY_YAML)


@pytest.fixture()
def apic_states():
    return {"1": _FakeApicState(), "2": _FakeApicState()}


@pytest.fixture()
def client(topo, apic_states):
    state = build_ndo_model(topo, sandbox=True)
    app = make_ndo_app(state, apic_states)
    with TestClient(app) as c:
        yield c


def _ms_tn1_schema(client):
    schemas = client.get("/mso/api/v1/schemas/list-identity").json()["schemas"]
    return next(s for s in schemas if s["displayName"] == "MS-TN1-schema")


def _bind_service_graph_ops(schema_id, tmpl_name, site_key, device_dn):
    """Site-local "Create service graph" op — design §1.3 wire shape."""
    return [
        {
            "op": "add",
            "path": f"/sites/{site_key}/serviceGraphs/-",
            "value": {
                "serviceGraphRef": {
                    "schemaId": schema_id,
                    "templateName": tmpl_name,
                    "serviceGraphName": "sgt-FW_LAB0",
                },
                "serviceNodes": [
                    {
                        "serviceNodeRef": {
                            "schemaId": schema_id,
                            "templateName": tmpl_name,
                            "serviceGraphName": "sgt-FW_LAB0",
                            "serviceNodeName": "node1",
                        },
                        "device": {"dn": device_dn},
                    }
                ],
            },
        }
    ]


def _add_template_service_graph(client, schema_id, tmpl_name):
    ops = [
        {
            "op": "add",
            "path": f"/templates/{tmpl_name}/serviceGraphs/-",
            "value": {
                "name": "sgt-FW_LAB0",
                "displayName": "sgt-FW_LAB0",
                "serviceNodes": [
                    {"name": "node1", "serviceNodeTypeId": "svc-node-type-firewall", "index": 1}
                ],
            },
        }
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=ops)
    assert resp.status_code == 200


def _site_key(detail, site_id, tmpl_name):
    site = next(s for s in detail["sites"] if s["siteId"] == site_id)
    return f"{site['siteId']}-{tmpl_name}"


# ---------------------------------------------------------------------------
# App-level: the legal two-stage flow that MUST stay green (design §4.1)
# ---------------------------------------------------------------------------


def test_phase2_bind_passes_when_phase1_device_already_exists(client, apic_states):
    """MS-TN1 two-stage create_tenant: phase1 (aci-model raw APIC POST)
    creates the vnsLDevVip on the target site FIRST; phase2's "Create
    service graph" site-local bind must then succeed."""
    schema = _ms_tn1_schema(client)
    detail = client.get(f"/mso/api/v1/schemas/{schema['id']}").json()
    tmpl_name = detail["templates"][0]["name"]
    site_key = _site_key(detail, "1", tmpl_name)

    # phase1: APIC-side device create (aci-model role's raw aci_rest POST).
    apic_states["1"].store.upsert(MO("vnsLDevVip", dn=DEVICE_DN, name="fw-FW_LAB0"))

    _add_template_service_graph(client, schema["id"], tmpl_name)
    resp = client.patch(
        f"/mso/api/v1/schemas/{schema['id']}",
        json=_bind_service_graph_ops(schema["id"], tmpl_name, site_key, DEVICE_DN),
    )
    assert resp.status_code == 200

    fresh_site = next(s for s in resp.json()["sites"] if s["siteId"] == "1")
    assert fresh_site["serviceGraphs"][0]["serviceNodes"][0]["device"]["dn"] == DEVICE_DN


# ---------------------------------------------------------------------------
# The confirmed sim gap F4 closes: phase2-only (skip phase1)
# ---------------------------------------------------------------------------


def test_phase2_only_bind_400s_with_golden_message(client):
    """Skip phase1 entirely (no vnsLDevVip ever created on site '1''s
    APIC) — real NDO 400s; this is the confirmed sim gap."""
    schema = _ms_tn1_schema(client)
    detail = client.get(f"/mso/api/v1/schemas/{schema['id']}").json()
    tmpl_name = detail["templates"][0]["name"]
    site_key = _site_key(detail, "1", tmpl_name)

    _add_template_service_graph(client, schema["id"], tmpl_name)
    resp = client.patch(
        f"/mso/api/v1/schemas/{schema['id']}",
        json=_bind_service_graph_ops(schema["id"], tmpl_name, site_key, DEVICE_DN),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "Service graph device fw-FW_LAB0 does not exist in tenant MS-TN1 "
        "in Fabric LAB1-IT-ACI"
    )


def test_phase2_only_bind_does_not_mutate_stored_schema(client):
    """The 400 must be all-or-nothing — a rejected bind must not leave a
    partial serviceGraphs entry in the stored schema (design §2.2: the
    pre-scan runs BEFORE apply_json_patch, so raising is naturally
    all-or-nothing)."""
    schema = _ms_tn1_schema(client)
    detail = client.get(f"/mso/api/v1/schemas/{schema['id']}").json()
    tmpl_name = detail["templates"][0]["name"]
    site_key = _site_key(detail, "1", tmpl_name)

    _add_template_service_graph(client, schema["id"], tmpl_name)
    resp = client.patch(
        f"/mso/api/v1/schemas/{schema['id']}",
        json=_bind_service_graph_ops(schema["id"], tmpl_name, site_key, DEVICE_DN),
    )
    assert resp.status_code == 400

    fresh = client.get(f"/mso/api/v1/schemas/{schema['id']}").json()
    fresh_site = next(s for s in fresh["sites"] if s["siteId"] == "1")
    assert fresh_site["serviceGraphs"] == []


def test_device_present_wrong_class_400s(client, apic_states):
    """A DN that resolves but under the WRONG class (not vnsLDevVip) must
    be treated the same as absent — the golden message must still fire."""
    schema = _ms_tn1_schema(client)
    detail = client.get(f"/mso/api/v1/schemas/{schema['id']}").json()
    tmpl_name = detail["templates"][0]["name"]
    site_key = _site_key(detail, "1", tmpl_name)

    apic_states["1"].store.upsert(MO("fvTenant", dn=DEVICE_DN, name="not-a-device"))

    _add_template_service_graph(client, schema["id"], tmpl_name)
    resp = client.patch(
        f"/mso/api/v1/schemas/{schema['id']}",
        json=_bind_service_graph_ops(schema["id"], tmpl_name, site_key, DEVICE_DN),
    )
    assert resp.status_code == 400
    assert "does not exist in tenant MS-TN1" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Fail-safe / default-allow — never reject on ambiguity (design §4)
# ---------------------------------------------------------------------------


def test_no_apic_states_wired_is_fail_safe(topo):
    """make_ndo_app(state) with NO apic_states at all (every pre-existing
    NDO test / call site) — F4 must never fire; this is the exact shape
    tests/test_pr13_ndo_dhcp_svcgraph.py's own service-graph round-trip
    test uses, and it must stay green."""
    state = build_ndo_model(topo, sandbox=True)
    app = make_ndo_app(state)  # no apic_states
    with TestClient(app) as c:
        schema = _ms_tn1_schema(c)
        detail = c.get(f"/mso/api/v1/schemas/{schema['id']}").json()
        tmpl_name = detail["templates"][0]["name"]
        site_key = _site_key(detail, "1", tmpl_name)

        _add_template_service_graph(c, schema["id"], tmpl_name)
        resp = c.patch(
            f"/mso/api/v1/schemas/{schema['id']}",
            json=_bind_service_graph_ops(schema["id"], tmpl_name, site_key, DEVICE_DN),
        )
        assert resp.status_code == 200


def test_site_not_in_apic_states_is_fail_safe(topo):
    """Site '1' is associated with the template but simply isn't present
    in *apic_states* (this sim process isn't simulating that fabric) — F4
    cannot prove absence, so it must pass through."""
    state = build_ndo_model(topo, sandbox=True)
    app = make_ndo_app(state, {})  # apic_states present but empty
    with TestClient(app) as c:
        schema = _ms_tn1_schema(c)
        detail = c.get(f"/mso/api/v1/schemas/{schema['id']}").json()
        tmpl_name = detail["templates"][0]["name"]
        site_key = _site_key(detail, "1", tmpl_name)

        _add_template_service_graph(c, schema["id"], tmpl_name)
        resp = c.patch(
            f"/mso/api/v1/schemas/{schema['id']}",
            json=_bind_service_graph_ops(schema["id"], tmpl_name, site_key, DEVICE_DN),
        )
        assert resp.status_code == 200


def test_unparsable_device_dn_is_fail_safe(client):
    """A device.dn that doesn't match the `uni/tn-<t>/lDevVip-<d>` shape is
    not a ref F4 owns — must never be rejected."""
    schema = _ms_tn1_schema(client)
    detail = client.get(f"/mso/api/v1/schemas/{schema['id']}").json()
    tmpl_name = detail["templates"][0]["name"]
    site_key = _site_key(detail, "1", tmpl_name)

    _add_template_service_graph(client, schema["id"], tmpl_name)
    resp = client.patch(
        f"/mso/api/v1/schemas/{schema['id']}",
        json=_bind_service_graph_ops(
            schema["id"], tmpl_name, site_key, "uni/tn-MS-TN1/not-a-device-dn"
        ),
    )
    assert resp.status_code == 200


def test_template_level_bind_never_triggers_f4(client):
    """Template-level serviceGraphs ops (`/templates/{t}/serviceGraphs/-`)
    also carry `device.dn` (design §1.3) but are NOT site-scoped — F4 must
    only key on the site-local op."""
    schema = _ms_tn1_schema(client)
    detail = client.get(f"/mso/api/v1/schemas/{schema['id']}").json()
    tmpl_name = detail["templates"][0]["name"]

    ops = [
        {
            "op": "add",
            "path": f"/templates/{tmpl_name}/serviceGraphs/-",
            "value": {
                "name": "sgt-FW_LAB0",
                "displayName": "sgt-FW_LAB0",
                "serviceNodes": [
                    {
                        "name": "node1",
                        "serviceNodeTypeId": "svc-node-type-firewall",
                        "index": 1,
                        "device": {"dn": "uni/tn-MS-TN1/lDevVip-does-not-exist"},
                    }
                ],
            },
        }
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema['id']}", json=ops)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Unit level: _validate_service_graph_device_refs directly
# ---------------------------------------------------------------------------


def _bare_detail(site_id="1", tmpl="LAB1"):
    return {"sites": [{"siteId": site_id, "templateName": tmpl, "serviceGraphs": []}]}


def _bare_state(site_id="1", name="LAB1-IT-ACI"):
    return SimpleNamespace(sites=[{"id": site_id, "name": name}])


def _bare_op(site_key, device_dn):
    return {
        "op": "add",
        "path": f"/sites/{site_key}/serviceGraphs/-",
        "value": {"serviceNodes": [{"device": {"dn": device_dn}}]},
    }


def test_unit_empty_ops_is_noop():
    _validate_service_graph_device_refs(
        [], {"1": _FakeApicState()}, _bare_state(), _bare_detail()
    )  # must not raise


def test_unit_no_apic_states_is_noop():
    ops = [_bare_op("1-LAB1", DEVICE_DN)]
    _validate_service_graph_device_refs(ops, None, _bare_state(), _bare_detail())  # must not raise


def test_unit_no_detail_is_noop():
    ops = [_bare_op("1-LAB1", DEVICE_DN)]
    _validate_service_graph_device_refs(ops, {"1": _FakeApicState()}, _bare_state(), None)  # must not raise


def test_unit_unresolvable_site_composite_is_noop():
    """The op's `{siteId}-{templateName}` composite doesn't match ANY
    entry in detail["sites"] — can't prove anything, fail-safe skip."""
    ops = [_bare_op("9-DOES-NOT-EXIST", DEVICE_DN)]
    apic_states = {"1": _FakeApicState()}
    _validate_service_graph_device_refs(ops, apic_states, _bare_state(), _bare_detail())  # must not raise


def test_unit_device_present_passes():
    apic_states = {"1": _FakeApicState()}
    apic_states["1"].store.upsert(MO("vnsLDevVip", dn=DEVICE_DN, name="fw-FW_LAB0"))
    ops = [_bare_op("1-LAB1", DEVICE_DN)]
    _validate_service_graph_device_refs(ops, apic_states, _bare_state(), _bare_detail())  # must not raise


def test_unit_device_absent_raises_golden_message():
    apic_states = {"1": _FakeApicState()}
    ops = [_bare_op("1-LAB1", DEVICE_DN)]
    with pytest.raises(
        ServiceGraphValidationError,
        match=r"Service graph device fw-FW_LAB0 does not exist in tenant MS-TN1 in Fabric LAB1-IT-ACI",
    ):
        _validate_service_graph_device_refs(ops, apic_states, _bare_state(), _bare_detail())


def test_unit_replace_op_also_validated():
    """design §2.3: op in {add, replace} — a `replace` on an existing
    serviceGraphs entry must be checked exactly like an `add`."""
    apic_states = {"1": _FakeApicState()}
    op = _bare_op("1-LAB1", DEVICE_DN)
    op["op"] = "replace"
    op["path"] = "/sites/1-LAB1/serviceGraphs/0"
    with pytest.raises(ServiceGraphValidationError):
        _validate_service_graph_device_refs([op], apic_states, _bare_state(), _bare_detail())


def test_unit_remove_op_never_validated():
    """A `remove` op carries no meaningful `value` to check — must never
    be scanned (op not in {add, replace})."""
    apic_states = {"1": _FakeApicState()}
    op = {"op": "remove", "path": "/sites/1-LAB1/serviceGraphs/0"}
    _validate_service_graph_device_refs([op], apic_states, _bare_state(), _bare_detail())  # must not raise
