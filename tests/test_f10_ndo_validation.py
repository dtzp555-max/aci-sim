"""F10 batch 3 — NDO value validation (aci_sim/ndo/patch.py::NDO_RULES /
_validate_ndo_op), per design doc §3.3.

NDO stores schema JSON (not APIC MOs); values arrive as JSON-Patch op values
rather than (class, prop) MO attrs, so this is validated separately from the
APIC RULES registry (tests/test_f10_validators.py,
tests/test_f10_apic_writeface.py) even though it reuses the same ``fmt``/
``rng`` factories. Design §3.3 scope is deliberately narrow: subnet ``ip``
and static-port VLAN (the real cisco.mso wire field is ``portEncapVlan`` —
see NDO_RULES's own comment in patch.py for why the design doc's "vlan"
placeholder was resolved to that name).

Both the whole-object-add shape (``.../subnets/-`` with a dict value) and
the single-field-replace shape (``.../subnets/0/ip`` with a scalar value)
are covered, since both are legal JSON-Patch forms even though only the
whole-object shape is what the real ansible-mso client sends today (per this
repo's existing PR-11/PR-12 test fixtures)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aci_sim.ndo.app import make_ndo_app
from aci_sim.ndo.model import build_ndo_model
from aci_sim.ndo.patch import NDO_RULES, PatchError, apply_json_patch
from aci_sim.topology.loader import load_topology

TOPO_PATH = "topology.yaml"


# ---------------------------------------------------------------------------
# Unit level: apply_json_patch directly against a bare doc dict.
# ---------------------------------------------------------------------------


def _bd_doc_with_subnets() -> dict:
    return {
        "sites": [
            {
                "siteId": "1",
                "templateName": "LAB1",
                "bds": [
                    {
                        "bdRef": "/schemas/schema-abc/templates/LAB1/bds/bd-App1_LAB1",
                        "subnets": [],
                    }
                ],
            }
        ]
    }


def test_ndo_subnet_ip_valid_accepted_whole_object_add():
    doc = _bd_doc_with_subnets()
    ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/bds/bd-App1_LAB1/subnets/-",
            "value": {"ip": "192.168.41.1/24", "scope": "public"},
        }
    ]
    apply_json_patch(doc, ops)  # must not raise
    assert doc["sites"][0]["bds"][0]["subnets"] == [{"ip": "192.168.41.1/24", "scope": "public"}]


def test_ndo_subnet_ip_zero_route_accepted():
    """0.0.0.0/0 (external-EPG default route) is a real canonical value —
    see design §6 corpus table."""
    doc = _bd_doc_with_subnets()
    ops = [
        {"op": "add", "path": "/sites/1-LAB1/bds/bd-App1_LAB1/subnets/-", "value": {"ip": "0.0.0.0/0"}}
    ]
    apply_json_patch(doc, ops)


def test_ndo_subnet_bad_ip_rejected_whole_object_add():
    doc = _bd_doc_with_subnets()
    ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/bds/bd-App1_LAB1/subnets/-",
            "value": {"ip": "999.888.777.17/30", "scope": "public"},
        }
    ]
    with pytest.raises(PatchError, match="999.888.777.17/30"):
        apply_json_patch(doc, ops)
    # Reject must not have mutated the subnets list.
    assert doc["sites"][0]["bds"][0]["subnets"] == []


def test_ndo_subnet_ip_single_field_replace_shape():
    """Direct scalar replace on an EXISTING item's field
    (.../subnets/0/ip) — the other JSON-Patch shape _validate_ndo_op
    supports (container is a dict, last=='ip')."""
    doc = _bd_doc_with_subnets()
    doc["sites"][0]["bds"][0]["subnets"] = [{"ip": "192.168.41.1/24", "scope": "public"}]
    ops = [{"op": "replace", "path": "/sites/1-LAB1/bds/bd-App1_LAB1/subnets/0/ip", "value": "192.168.41.2/24"}]
    apply_json_patch(doc, ops)
    assert doc["sites"][0]["bds"][0]["subnets"][0]["ip"] == "192.168.41.2/24"

    ops_bad = [{"op": "replace", "path": "/sites/1-LAB1/bds/bd-App1_LAB1/subnets/0/ip", "value": "999.888.777.2/24"}]
    with pytest.raises(PatchError):
        apply_json_patch(doc, ops_bad)
    # Reject must not have overwritten the previously-good value.
    assert doc["sites"][0]["bds"][0]["subnets"][0]["ip"] == "192.168.41.2/24"


def _epg_doc_with_static_ports() -> dict:
    return {
        "sites": [
            {
                "siteId": "1",
                "templateName": "LAB1",
                "anps": [
                    {
                        "anpRef": "/schemas/s/templates/LAB1/anps/app-Web_LAB1",
                        "epgs": [
                            {
                                "epgRef": "/schemas/s/templates/LAB1/anps/app-Web_LAB1/epgs/epg-web",
                                "staticPorts": [],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def test_ndo_static_port_vlan_valid_accepted():
    doc = _epg_doc_with_static_ports()
    ops = [
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
    apply_json_patch(doc, ops)
    static_ports = doc["sites"][0]["anps"][0]["epgs"][0]["staticPorts"]
    assert len(static_ports) == 1
    assert static_ports[0]["portEncapVlan"] == 100


def test_ndo_static_port_vlan_out_of_range_rejected():
    """F10-probe-shaped bad value: vlan-9999 equivalent on the NDO side."""
    doc = _epg_doc_with_static_ports()
    ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/anps/app-Web_LAB1/epgs/epg-web/staticPorts/-",
            "value": {
                "path": "topology/pod-1/protpaths-101-102/pathep-[ipg-vpc-LegacySW01]",
                "portEncapVlan": 9999,
                "mode": "regular",
            },
        }
    ]
    with pytest.raises(PatchError, match="9999"):
        apply_json_patch(doc, ops)
    assert doc["sites"][0]["anps"][0]["epgs"][0]["staticPorts"] == []


def test_ndo_static_port_vlan_string_form_also_validated():
    """cisco.mso's own JSON payload uses a plain int (see the accept test
    above) but the sim must not assume that — a numeric STRING is just as
    real a wire value elsewhere in this codebase (ACI side is all-string)."""
    doc = _epg_doc_with_static_ports()
    ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/anps/app-Web_LAB1/epgs/epg-web/staticPorts/-",
            "value": {"path": "topology/pod-1/pathep-[eth1/1]", "portEncapVlan": "0"},
        }
    ]
    with pytest.raises(PatchError):
        apply_json_patch(doc, ops)


# ---------------------------------------------------------------------------
# Fail-safe: an unregistered collection/field is never validated.
# ---------------------------------------------------------------------------


def test_ndo_unregistered_collection_field_passes_through():
    doc = {"templates": [{"name": "LAB1", "vrfs": []}]}
    ops = [
        {
            "op": "add",
            "path": "/templates/LAB1/vrfs/-",
            "value": {"name": "vrf1", "someWildField": "!!not validated!!"},
        }
    ]
    apply_json_patch(doc, ops)  # must not raise — ("vrfs", *) is not in NDO_RULES
    assert doc["templates"][0]["vrfs"] == [{"name": "vrf1", "someWildField": "!!not validated!!"}]


def test_ndo_subnet_scope_field_not_validated_only_ip_is():
    """Design §3.3 scope is deliberately narrow (subnet ip + static-port
    vlan only) — an arbitrary 'scope' value on a subnet must NOT be
    rejected by this batch even though it's semantically wrong, since
    ("subnets", "scope") has no NDO_RULES entry."""
    doc = _bd_doc_with_subnets()
    ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/bds/bd-App1_LAB1/subnets/-",
            "value": {"ip": "192.168.41.1/24", "scope": "totally-not-a-real-scope"},
        }
    ]
    apply_json_patch(doc, ops)  # must not raise


def test_ndo_rules_table_scope():
    """Locks the batch-3 scope: exactly the two design-doc-named rules."""
    assert set(NDO_RULES.keys()) == {("subnets", "ip"), ("staticPorts", "portEncapVlan")}


# ---------------------------------------------------------------------------
# Integration level: full FastAPI PATCH /mso/api/v1/schemas/{id} round trip
# (mirrors tests/test_pr12_site_local.py's own end-to-end pattern).
# ---------------------------------------------------------------------------


@pytest.fixture()
def topo():
    return load_topology(TOPO_PATH)


@pytest.fixture()
def client(topo):
    state = build_ndo_model(topo)
    app = make_ndo_app(state)
    with TestClient(app) as c:
        yield c


def test_schema_patch_bad_subnet_ip_400s_end_to_end(client):
    payload = {
        "displayName": "F10-TEST",
        "templates": [{"name": "LAB1", "displayName": "LAB1", "tenantId": "t1", "bds": []}],
        "sites": [{"siteId": "1", "templateName": "LAB1"}],
    }
    schema_id = client.post("/mso/api/v1/schemas", json=payload).json()["id"]

    bd_ops = [
        {"op": "add", "path": "/templates/LAB1/bds/-", "value": {"name": "bd-App1_LAB1", "displayName": "bd-App1_LAB1", "subnets": []}}
    ]
    resp = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=bd_ops)
    assert resp.status_code == 200

    bad_subnet_ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/bds/bd-App1_LAB1/subnets/-",
            "value": {"ip": "999.888.777.17/30", "scope": "public"},
        }
    ]
    resp2 = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=bad_subnet_ops)
    assert resp2.status_code == 400
    assert "999.888.777.17/30" in resp2.json()["detail"]

    good_subnet_ops = [
        {
            "op": "add",
            "path": "/sites/1-LAB1/bds/bd-App1_LAB1/subnets/-",
            "value": {"ip": "192.168.41.1/24", "scope": "public"},
        }
    ]
    resp3 = client.patch(f"/mso/api/v1/schemas/{schema_id}", json=good_subnet_ops)
    assert resp3.status_code == 200
    assert resp3.json()["sites"][0]["bds"][0]["subnets"] == [{"ip": "192.168.41.1/24", "scope": "public"}]
