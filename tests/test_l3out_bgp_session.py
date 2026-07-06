"""Runtime tenant-L3Out eBGP session materialization (writes.py reaction).

A tenant L3Out pushed live via POST /api/mo (cisco.aci playbook / aci-py
hidden_push) lands ``bgpPeerP`` (config) in writes.py — NOT via the topology
builders, so build/l3out.py's boot-time ``bgpPeer``/``bgpPeerEntry`` emission
never runs for it. The ``_materialize_l3out_bgp_session`` reaction closes that
gap: every runtime ``bgpPeerP`` also materializes the operational session MOs
(``bgpPeer``/``bgpPeerEntry``/``bgpPeerAfEntry``) under a ``dom-{tenant}:{vrf}``
BGP domain, which is exactly what autoACI's L3Out-BGP session table parses
(``_dn_dom`` → ``if ":" not in dom: skip`` → ``tenant, _, vrf = partition(":")``).
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from aci_sim.build.orchestrator import build_site
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology

TOPO_PATH = "topology.yaml"  # run from project root

TENANT = "BGPSESS-T1"
L3OUT = "l3o-bgp-test"
VRF = "vrf-L3_TEST"
PEER_IP = "10.9.9.1"
PEER_ASN = "65100"
PEERP_DN = (
    f"uni/tn-{TENANT}/out-{L3OUT}/lnodep-lnp-Core/lifp-lip-Core/"
    f"rspathL3OutAtt-[topology/pod-1/paths-101/pathep-[eth1/9]]/peerP-[{PEER_IP}]"
)


@pytest.fixture(scope="module")
def client():
    topo = load_topology(TOPO_PATH)
    site = topo.sites[0]
    store = build_site(topo, site)
    state = ApicSiteState(
        name=site.name,
        site=site,
        topo=topo,
        store=store,
        baseline=copy.deepcopy(store),
    )
    app = make_apic_app(state)
    c = TestClient(app)
    resp = c.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    assert resp.status_code == 200
    return c


def _post_mo(client, dn: str, body: dict):
    resp = client.post(f"/api/mo/{dn}.json", json=body)
    assert resp.status_code == 200, resp.text
    return resp


def _class_dns(client, cls: str) -> list[str]:
    data = client.get(f"/api/class/{cls}.json").json()
    return [next(iter(i.values()))["attributes"]["dn"] for i in data["imdata"]]


def test_runtime_bgp_peerp_materializes_operational_session(client):
    # 1. Tenant, then the L3Out at its EXPLICIT DN with the VRF binding
    #    (l3extRsEctx) nested — mirroring how cisco.aci / aci-py actually
    #    post (per-MO, dn in attributes; nested l3extOut would get an
    #    auto-numbered RN since l3extOut has no _RN_PREFIXES entry).
    _post_mo(client, f"uni/tn-{TENANT}", {
        "fvTenant": {"attributes": {"name": TENANT}},
    })
    l3out_dn = f"uni/tn-{TENANT}/out-{L3OUT}"
    _post_mo(client, l3out_dn, {
        "l3extOut": {"attributes": {"name": L3OUT, "dn": l3out_dn}, "children": [
            {"l3extRsEctx": {"attributes": {"tnFvCtxName": VRF}}},
        ]},
    })
    # 2. The eBGP peer config, nested bgpAsP carrying the remote (CSW) ASN —
    #    same single-POST shape the cisco.aci tenant task produces.
    _post_mo(client, PEERP_DN, {
        "bgpPeerP": {"attributes": {"addr": PEER_IP, "dn": PEERP_DN}, "children": [
            {"bgpAsP": {"attributes": {"asn": PEER_ASN}}},
        ]},
    })

    dom = f"dom-{TENANT}:{VRF}"
    exp_peer = f"topology/pod-1/node-101/sys/bgp/inst/{dom}/peer-[{PEER_IP}]"

    peer_dns = [d for d in _class_dns(client, "bgpPeer") if dom in d]
    assert peer_dns == [exp_peer]

    # bgpPeer carries the derived remote ASN (from the nested bgpAsP).
    data = client.get("/api/class/bgpPeer.json").json()
    peer_attrs = next(
        i["bgpPeer"]["attributes"] for i in data["imdata"]
        if i["bgpPeer"]["attributes"]["dn"] == exp_peer
    )
    assert peer_attrs["asn"] == PEER_ASN
    assert peer_attrs["type"] == "ebgp"

    # bgpPeerEntry (operSt) + bgpPeerAfEntry (prefix counters) exist under it.
    entry_dns = [d for d in _class_dns(client, "bgpPeerEntry") if dom in d]
    assert entry_dns == [f"{exp_peer}/ent-[{PEER_IP}]"]
    entry_attrs = next(
        i["bgpPeerEntry"]["attributes"]
        for i in client.get("/api/class/bgpPeerEntry.json").json()["imdata"]
        if dom in i["bgpPeerEntry"]["attributes"]["dn"]
    )
    assert entry_attrs["operSt"] == "established"

    af_dns = [d for d in _class_dns(client, "bgpPeerAfEntry") if dom in d]
    assert af_dns == [f"{exp_peer}/ent-[{PEER_IP}]/af-[ipv4-ucast]"]


def test_bgp_peerp_without_vrf_binding_is_skipped(client):
    """No l3extRsEctx on the L3Out → no tenant:vrf dom to place the session
    under; the reaction must skip rather than fudge a dom that autoACI would
    misclassify (no ':' → treated as fabric/overlay)."""
    tenant2 = "BGPSESS-T2"
    peerp2 = (
        f"uni/tn-{tenant2}/out-l3o-novrf/lnodep-lnp/lifp-lip/"
        f"rspathL3OutAtt-[topology/pod-1/paths-101/pathep-[eth1/10]]/peerP-[10.9.9.2]"
    )
    _post_mo(client, f"uni/tn-{tenant2}", {
        "fvTenant": {"attributes": {"name": tenant2}, "children": [
            {"l3extOut": {"attributes": {"name": "l3o-novrf"}}},
        ]},
    })
    _post_mo(client, peerp2, {
        "bgpPeerP": {"attributes": {"addr": "10.9.9.2", "dn": peerp2}, "children": [
            {"bgpAsP": {"attributes": {"asn": "65100"}}},
        ]},
    })
    assert not [d for d in _class_dns(client, "bgpPeer") if tenant2 in d]


def test_deleted_bgp_peerp_does_not_materialize(client):
    """status=deleted bgpPeerP must not (re)create session MOs."""
    tenant3 = "BGPSESS-T3"
    peerp3 = (
        f"uni/tn-{tenant3}/out-{L3OUT}/lnodep-lnp/lifp-lip/"
        f"rspathL3OutAtt-[topology/pod-1/paths-101/pathep-[eth1/11]]/peerP-[10.9.9.3]"
    )
    _post_mo(client, f"uni/tn-{tenant3}", {
        "fvTenant": {"attributes": {"name": tenant3}},
    })
    # L3Out at its explicit DN WITH a VRF binding — so the only thing
    # stopping materialization below is the deleted status, not a missing VRF.
    l3out3_dn = f"uni/tn-{tenant3}/out-{L3OUT}"
    _post_mo(client, l3out3_dn, {
        "l3extOut": {"attributes": {"name": L3OUT, "dn": l3out3_dn}, "children": [
            {"l3extRsEctx": {"attributes": {"tnFvCtxName": VRF}}},
        ]},
    })
    _post_mo(client, peerp3, {
        "bgpPeerP": {"attributes": {"addr": "10.9.9.3", "dn": peerp3, "status": "deleted"}},
    })
    assert not [d for d in _class_dns(client, "bgpPeer") if tenant3 in d]
