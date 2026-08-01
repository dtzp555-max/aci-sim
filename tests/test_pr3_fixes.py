"""Regression tests for PR-3 P0 findings.

FIX 1 — node IDs > 255 (LAB2: 301-402) previously produced invalid IPv4
addresses (e.g. 10.1.401.1 has a 401 octet, 192.168.1.401 likewise) via
build/fabric.py's loopback_ip/oob_ip. Now every derived address must parse as
valid IPv4, stay unique per node within a site, and node IDs <= 255 (LAB1)
must keep their exact legacy addresses unchanged.

FIX 2 — GET /api/mo/uni/userprofile-<user>.json?query-target=subtree&
target-subtree-class=aaaUserDomain (CONTRACT.md §1 login probe) 404'd because
no userprofile MO was ever built. build/userprofile.py now seeds a static
uni/userprofile-admin (aaaUserEp) + aaaUserDomain child in every site's
baseline store.
"""
from __future__ import annotations

import copy
import ipaddress

import pytest
from fastapi.testclient import TestClient

from aci_sim.build.fabric import loopback_ip, oob_ip
from aci_sim.build.l3out import _build_node_profile
from aci_sim.build.orchestrator import build_site
from aci_sim.mit.store import MITStore
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology

TOPO_PATH = "topology.yaml"  # run from project root


@pytest.fixture(scope="module")
def topo():
    return load_topology(TOPO_PATH)


@pytest.fixture(scope="module")
def site_a(topo):
    return topo.site_by_name("LAB1")


@pytest.fixture(scope="module")
def site_b(topo):
    return topo.site_by_name("LAB2")


@pytest.fixture(scope="module")
def store_a(topo, site_a) -> MITStore:
    return build_site(topo, site_a)


@pytest.fixture(scope="module")
def store_b(topo, site_b) -> MITStore:
    return build_site(topo, site_b)


@pytest.fixture(scope="module")
def client_a(topo, site_a, store_a):
    state = ApicSiteState(
        name=site_a.name, site=site_a, topo=topo, store=store_a,
        baseline=copy.deepcopy(store_a),
    )
    c = TestClient(make_apic_app(state))
    c.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    return c


# ---------------------------------------------------------------------------
# FIX 1 — Test 1: all topSystem address/oobMgmtAddr parse as valid IPv4 and
# are unique within each site (both sites, with Site B's 301-402 explicit).
# ---------------------------------------------------------------------------

def _topsystem_addrs(store: MITStore) -> list[tuple[str, str, str]]:
    """Return (node_id, address, oobMgmtAddr) for every topSystem MO."""
    out = []
    for mo in store.by_class("topSystem"):
        out.append((mo.attrs["id"], mo.attrs["address"], mo.attrs["oobMgmtAddr"]))
    return out


def test_site_a_addresses_valid_and_unique(store_a):
    rows = _topsystem_addrs(store_a)
    assert rows, "no topSystem MOs found for LAB1"
    addrs = [r[1] for r in rows]
    oobs = [r[2] for r in rows]
    for a in addrs:
        ipaddress.ip_address(a)
    for o in oobs:
        ipaddress.ip_address(o)
    assert len(addrs) == len(set(addrs)), f"duplicate loopback addresses in LAB1: {addrs}"
    assert len(oobs) == len(set(oobs)), f"duplicate oob addresses in LAB1: {oobs}"


def test_site_b_addresses_valid_and_unique(store_b):
    """LAB2 node IDs are 301-402 — the previously-invalid range."""
    rows = _topsystem_addrs(store_b)
    node_ids = {r[0] for r in rows}
    # sanity: LAB2's high-numbered nodes are actually present in this fixture
    assert {"301", "302", "303", "304", "401", "402"} <= node_ids, node_ids

    addrs = [r[1] for r in rows]
    oobs = [r[2] for r in rows]
    for _node_id, addr, oob in rows:
        # Every address must parse as valid IPv4 -- this is the core regression
        # check: pre-fix, node 401 produced "10.1.401.1" / "192.168.1.401",
        # both of which ipaddress.ip_address() rejects (octet > 255).
        ipaddress.ip_address(addr)
        ipaddress.ip_address(oob)

    assert len(addrs) == len(set(addrs)), f"duplicate loopback addresses in LAB2: {addrs}"
    assert len(oobs) == len(set(oobs)), f"duplicate oob addresses in LAB2: {oobs}"


# ---------------------------------------------------------------------------
# FIX 1 — Test 2: node IDs <= 255 keep the exact legacy addresses
# (backward compatibility for LAB1: nodes 1-3, 101-104, 201-202).
# ---------------------------------------------------------------------------

def test_legacy_node_ids_unchanged(site_a):
    """Node IDs <= 255 must keep the pre-fix 10.<pod>.<id>.1 / 192.168.<pod>.<id> form."""
    pod = site_a.pod
    assert loopback_ip(pod, 101) == "10.1.101.1"
    assert oob_ip(pod, 101) == "192.168.1.101"
    assert loopback_ip(pod, 201) == "10.1.201.1"
    assert oob_ip(pod, 201) == "192.168.1.201"


def test_legacy_top_system_addresses_in_store(store_a, site_a):
    """The actual built topSystem MOs for LAB1 (all node IDs <= 255) must show
    the unchanged legacy addresses."""
    pod = site_a.pod
    node = next(n for n in site_a.all_nodes() if n.id == 101)
    ts_dn = f"topology/pod-{pod}/node-{node.id}/sys"
    mo = store_a.get(ts_dn)
    assert mo is not None
    assert mo.attrs["address"] == "10.1.101.1"
    assert mo.attrs["oobMgmtAddr"] == "192.168.1.101"


# ---------------------------------------------------------------------------
# FIX 1 — Test 4: refactored producers (l3out.py rtr_id) also emit valid IPv4
# ---------------------------------------------------------------------------

def test_l3out_node_profile_rtr_id_valid_ipv4(topo, site_b):
    """l3out.py's _build_node_profile derives rtr_id via loopback_ip for
    border leaves; LAB2's border leaves (303/304) are <= 255, but this
    verifies the refactored call-site (no more inline f"10.{pod}.{id}.1")
    still produces valid IPv4, and stays consistent with fabric.py."""
    tenant = next(t for t in topo.tenants if t.l3outs)
    l3out = next(lo for lo in tenant.l3outs if lo.site == site_b.name or
                 any(bl in {b.id for b in site_b.border_leaves} for bl in lo.border_leaves))
    np_mo = _build_node_profile(f"uni/tn-{tenant.name}/out-{l3out.name}", l3out, site_b, site_b.pod)
    rs_atts = [c for c in np_mo.children if c.class_name == "l3extRsNodeL3OutAtt"]
    assert rs_atts, "no l3extRsNodeL3OutAtt children built"
    for att in rs_atts:
        ipaddress.ip_address(att.attrs["rtrId"])


def test_l3out_ebgp_session_rtr_id_valid_ipv4(topo, site_b, store_b):
    """_upsert_ebgp_sessions's rtr_id (also refactored onto loopback_ip) must
    be valid IPv4 in the built store."""
    bgp_peer_entries = [
        mo for mo in store_b.by_class("bgpPeerEntry")
        if mo.attrs.get("type") == "ebgp"
    ]
    assert bgp_peer_entries, "no ebgp bgpPeerEntry found for LAB2"
    for mo in bgp_peer_entries:
        ipaddress.ip_address(mo.attrs["rtrId"])


def test_cabling_lldp_cdp_mgmt_ip_valid_ipv4(store_b):
    """cabling.py's lldpAdjEp/cdpAdjEp mgmtIp (built via oob_ip) must be valid
    IPv4 even for LAB2 nodes > 255."""
    adj_mos = list(store_b.by_class("lldpAdjEp")) + list(store_b.by_class("cdpAdjEp"))
    assert adj_mos, "no lldp/cdp adjacency MOs found for LAB2"
    for mo in adj_mos:
        ipaddress.ip_address(mo.attrs["mgmtIp"])


def test_overlay_isn_peer_addr_valid_ipv4(store_a):
    """overlay.py's ISN bgpPeer addr (loopback_ip + '/32') must have a valid
    IPv4 host part even when the remote site's spines are > 255 (LAB2: 401/402)."""
    isn_peers = [
        mo for mo in store_a.by_class("bgpPeer")
        if mo.attrs.get("type") == "inter-site"
    ]
    assert isn_peers, "no ISN bgpPeer found on LAB1 spines"
    for mo in isn_peers:
        addr = mo.attrs["addr"]
        assert addr.endswith("/32")
        host = addr.rsplit("/", 1)[0]
        ipaddress.ip_address(host)


# ---------------------------------------------------------------------------
# FIX 2 — Test 3: login probe target resolves with totalCount >= 1
# ---------------------------------------------------------------------------

def test_userprofile_login_probe(client_a):
    resp = client_a.get(
        "/api/mo/uni/userprofile-admin.json"
        "?query-target=subtree&target-subtree-class=aaaUserDomain"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert int(data["totalCount"]) >= 1
    assert data["imdata"], "expected at least one aaaUserDomain row"
    for item in data["imdata"]:
        assert "aaaUserDomain" in item
