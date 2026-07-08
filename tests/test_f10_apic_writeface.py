"""F10 — server-side value validation, end-to-end through the APIC write
face (POST /api/mo/{dn}.json -> aci_sim/rest_aci/writes.py::_validate_planned
-> aci_sim/rest_aci/validators.py::RULES).

Complements tests/test_f10_validators.py (which exercises RULES entries
directly) by proving the FULL plumbing: a bad value 400s with the real APIC
error envelope and never lands in the store; the pre-existing 3
fvSubnet/l3extSubnet/vnsRedirectDest ip checks keep working byte-identically
now that they're migrated into the RULES registry
(tests/test_write_validation_and_children.py already covers that regression
directly — this file adds the NEW P1/P2 rules' end-to-end wiring)."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from aci_sim.build.orchestrator import build_site
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology

TOPO_PATH = "topology.yaml"
TENANT = "F10TEST-T1"


@pytest.fixture(scope="module")
def client():
    topo = load_topology(TOPO_PATH)
    site = topo.sites[0]
    store = build_site(topo, site)
    state = ApicSiteState(
        name=site.name, site=site, topo=topo,
        store=store, baseline=copy.deepcopy(store),
    )
    c = TestClient(make_apic_app(state))
    resp = c.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    assert resp.status_code == 200
    _post(c, f"uni/tn-{TENANT}", {"fvTenant": {"attributes": {"name": TENANT}}})
    return c


def _post(client, dn: str, body: dict):
    return client.post(f"/api/mo/{dn}.json", json=body)


def _assert_not_in_store(client, cls: str, dn: str):
    q = client.get(f"/api/class/{cls}.json").json()
    assert not any(
        i[cls]["attributes"]["dn"] == dn for i in q["imdata"]
    ), f"invalid {cls} must not land in the MIT"


# ---------------------------------------------------------------------------
# The exact F10 probe finding: encap=vlan-9999 / rtrId=999.888.777.x now 400.
# ---------------------------------------------------------------------------


def test_f10_probe_bad_encap_rejected(client):
    dn = f"uni/tn-{TENANT}/out-L3OUT1/lnodep-NP1/lifp-IP1/rspathL3OutAtt-[topology/pod-1/paths-101/pathep-[eth1/1]]"
    r = _post(
        client, dn,
        {"l3extRsPathL3OutAtt": {"attributes": {"encap": "vlan-9999", "dn": dn}}},
    )
    assert r.status_code == 400
    err = r.json()["imdata"][0]["error"]["attributes"]
    assert "vlan-9999" in err["text"]
    # app.py's post_mo handler catches WriteValidationError with code="103"
    # (the design doc's "code 107" citation is for a different catch site —
    # the GET-query FilterParseError handlers at get_class/get_mo).
    assert err["code"] == "103"
    _assert_not_in_store(client, "l3extRsPathL3OutAtt", dn)


def test_f10_probe_bad_rtrid_rejected(client):
    dn = f"uni/tn-{TENANT}/out-L3OUT1/lnodep-NP1/rsnodeL3OutAtt-[topology/pod-1/node-101]"
    r = _post(
        client, dn,
        {"l3extRsNodeL3OutAtt": {"attributes": {"rtrId": "999.888.777.241", "dn": dn}}},
    )
    assert r.status_code == 400
    err = r.json()["imdata"][0]["error"]["attributes"]
    assert "999.888.777.241" in err["text"]
    _assert_not_in_store(client, "l3extRsNodeL3OutAtt", dn)


def test_f10_valid_encap_and_rtrid_accepted(client):
    dn1 = f"uni/tn-{TENANT}/out-L3OUT1/lnodep-NP1/lifp-IP1/rspathL3OutAtt-[topology/pod-1/paths-101/pathep-[eth1/2]]"
    r = _post(
        client, dn1,
        {"l3extRsPathL3OutAtt": {"attributes": {"encap": "vlan-51", "mtu": "9000", "dn": dn1}}},
    )
    assert r.status_code == 200

    dn2 = f"uni/tn-{TENANT}/out-L3OUT1/lnodep-NP1/rsnodeL3OutAtt-[topology/pod-1/node-102]"
    r = _post(
        client, dn2,
        {"l3extRsNodeL3OutAtt": {"attributes": {"rtrId": "10.100.201.241", "dn": dn2}}},
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# All-or-nothing: a bad value nested deep in a subtree must reject the WHOLE
# POST, leaving zero side effects (design §3.2 "preserves the existing
# all-or-nothing guarantee").
# ---------------------------------------------------------------------------


def test_bad_nested_asn_rejects_whole_subtree(client):
    peer_dn = (
        f"uni/tn-{TENANT}/out-L3OUT1/lnodep-NP1/lifp-IP1/"
        f"paths-101/pathep-[eth1/3]]/peerP-[10.100.0.5]"
    )
    body = {
        "bgpPeerP": {
            "attributes": {"addr": "10.100.0.5", "dn": peer_dn},
            "children": [
                {"bgpAsP": {"attributes": {"asn": "0"}}},  # illegal: asn must be >= 1
            ],
        }
    }
    r = _post(client, peer_dn, body)
    assert r.status_code == 400
    # The parent bgpPeerP must NOT have landed either — all-or-nothing.
    _assert_not_in_store(client, "bgpPeerP", peer_dn)


def test_good_nested_asn_accepted(client):
    peer_dn = (
        f"uni/tn-{TENANT}/out-L3OUT1/lnodep-NP1/lifp-IP1/"
        f"paths-101/pathep-[eth1/4]]/peerP-[10.100.0.6]"
    )
    body = {
        "bgpPeerP": {
            "attributes": {"addr": "10.100.0.6", "ttl": "2", "dn": peer_dn},
            "children": [
                {"bgpAsP": {"attributes": {"asn": "65100"}}},
            ],
        }
    }
    r = _post(client, peer_dn, body)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Fail-safe: an unregistered class or property is never validated, even with
# a wild value.
# ---------------------------------------------------------------------------


def test_unregistered_class_prop_passes_through_unvalidated(client):
    dn = f"uni/tn-{TENANT}/ap-AP1"
    r = _post(client, dn, {"fvAp": {"attributes": {"name": "AP1", "someUnknownProp": "!!not-validated!!", "dn": dn}}})
    assert r.status_code == 200


def test_delete_skips_validation_even_with_bad_leftover_shape(client):
    dn = f"uni/tn-{TENANT}/out-L3OUT1/lnodep-NP1/lifp-IP1/rspathL3OutAtt-[topology/pod-1/paths-101/pathep-[eth1/9]]"
    r = _post(client, dn, {"l3extRsPathL3OutAtt": {"attributes": {"encap": "vlan-51", "dn": dn}}})
    assert r.status_code == 200
    # A delete carries only dn/status — no encap attr to (mis)validate.
    r = _post(client, dn, {"l3extRsPathL3OutAtt": {"attributes": {"dn": dn, "status": "deleted"}}})
    assert r.status_code == 200
