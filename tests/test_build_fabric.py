"""Phase 3 — fabric builder tests.

Loads the default topology, builds SiteA into a fresh MITStore, and asserts:
- 2 spines + 2 leaves + 2 border-leaves present as fabricNode
- Every fabricLink n1/n2 references a real fabricNode
- lldpAdjEp + cdpAdjEp neighbor set matches the cabling graph
- Each spine's sys/bgp/inst subtree has ISN bgpPeer with /32 addr + remote ASN
- healthInst per node present with correct DN
- Seeded faultInst under node-local DNs (contain /node-{id}/)
- topSystem has the correct loopback address formula
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from aci_sim.topology.loader import load_topology
from aci_sim.topology.schema import Topology
from aci_sim.mit.store import MITStore
from aci_sim.build import fabric, cabling, underlay, overlay, interfaces, health_faults
from aci_sim.build.cabling import cabling_links
from aci_sim.build.fabric import loopback_ip, oob_ip

TOPO_YAML = Path(__file__).parent.parent / "topology.yaml"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def topo() -> Topology:
    return load_topology(TOPO_YAML)


@pytest.fixture(scope="module")
def site_a(topo):
    return topo.site_by_name("LAB1")


@pytest.fixture(scope="module")
def store(topo, site_a) -> MITStore:
    """Build SiteA into a fresh store using all Phase-3 builders."""
    s = MITStore()
    fabric.build(topo, site_a, s)
    cabling.build(topo, site_a, s)
    underlay.build(topo, site_a, s)
    overlay.build(topo, site_a, s)
    interfaces.build(topo, site_a, s)
    health_faults.build(topo, site_a, s)
    return s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_attrs(store: MITStore, cls: str) -> list[dict]:
    return [mo.attrs for mo in store.by_class(cls)]


# ---------------------------------------------------------------------------
# Test: node counts
# ---------------------------------------------------------------------------

def test_node_counts(store, site_a):
    """SiteA default: 2 spines + 2 leaves + 2 border-leaves = 6 switches, plus a
    1-node APIC cluster (role=controller, PR-21 single-APIC default) → 7 fabricNodes."""
    nodes = get_attrs(store, "fabricNode")

    spines = [n for n in nodes if n["role"] == "spine"]
    leaves = [n for n in nodes if n["role"] == "leaf"]
    controllers = [n for n in nodes if n["role"] == "controller"]
    assert len(spines) == 2, f"Expected 2 spines, got {len(spines)}"
    # Leaves include regular leaves (2) + border-leaves (2) — all role="leaf"
    assert len(leaves) == 4, f"Expected 4 leaf-role nodes, got {len(leaves)}"
    assert len(controllers) == 1, f"Expected 1 controller, got {len(controllers)}"
    assert len(nodes) == 7, f"Expected 7 fabricNodes (6 switch + 1 APIC), got {len(nodes)}: {[n['name'] for n in nodes]}"


# ---------------------------------------------------------------------------
# Test: fabricLink n1/n2 reference real nodes
# ---------------------------------------------------------------------------

def test_fabric_links_reference_real_nodes(store, site_a):
    """Every fabricLink n1 and n2 must be a real fabricNode id in this site."""
    node_ids = {mo.attrs["id"] for mo in store.by_class("fabricNode")}
    links = store.by_class("fabricLink")
    assert len(links) > 0, "No fabricLinks found"

    for link in links:
        n1 = link.attrs.get("n1", "")
        n2 = link.attrs.get("n2", "")
        assert n1 in node_ids, f"fabricLink n1={n1!r} not in fabricNodes {node_ids}"
        assert n2 in node_ids, f"fabricLink n2={n2!r} not in fabricNodes {node_ids}"

    # Expected link count: (2 spines) × (2 leaves + 2 border-leaves) = 8
    # 8 spine↔leaf (2 spines × 4 downlinks) + 2 APIC↔leaf (1 controller × 2 leaves, PR-21 default)
    assert len(links) == 10, f"Expected 10 fabricLinks (8 fabric + 2 APIC), got {len(links)}"


# ---------------------------------------------------------------------------
# Test: LLDP/CDP neighbor set matches cabling graph
# ---------------------------------------------------------------------------

def test_lldp_cdp_matches_cabling(store, site_a):
    """lldpAdjEp sysName values on each node match what the cabling graph predicts."""
    pod = site_a.pod
    node_map = {n.id: n for n in site_a.all_nodes()}

    # Build expected neighbor pairs from cabling_links
    # For each link (n1, s1, p1, n2, s2, p2):
    #   node n1 expects neighbor n2.name on port eth{s1}/{p1}
    #   node n2 expects neighbor n1.name on port eth{s2}/{p2}
    expected_lldp: set[tuple[int, str]] = set()  # (node_id, neighbor_name)
    for n1, s1, p1, n2, s2, p2 in cabling_links(site_a):
        expected_lldp.add((n1, node_map[n2].name))
        expected_lldp.add((n2, node_map[n1].name))

    # APIC↔leaf: the first two leaves each see every APIC (leaf-side lldpAdjEp)
    for cid in range(1, site_a.controllers + 1):
        for leaf in site_a.leaf_nodes()[:2]:
            expected_lldp.add((leaf.id, f"{site_a.name}-ACI-APIC{cid:02d}"))

    actual_lldp: set[tuple[int, str]] = set()
    for mo in store.by_class("lldpAdjEp"):
        dn = mo.attrs["dn"]
        m = re.search(r"/node-(\d+)/", dn)
        if m:
            actual_lldp.add((int(m.group(1)), mo.attrs.get("sysName", "")))

    assert expected_lldp == actual_lldp, (
        f"LLDP mismatch\n  missing: {expected_lldp - actual_lldp}\n"
        f"  extra:   {actual_lldp - expected_lldp}"
    )

    # CDP must be symmetric
    cdp_pairs: set[tuple[int, str]] = set()
    for mo in store.by_class("cdpAdjEp"):
        dn = mo.attrs["dn"]
        m = re.search(r"/node-(\d+)/", dn)
        if m:
            cdp_pairs.add((int(m.group(1)), mo.attrs.get("sysName", "")))
    assert expected_lldp == cdp_pairs, "CDP neighbor set does not match cabling"


# ---------------------------------------------------------------------------
# Test: ISN bgpPeer — /32 addr + remote ASN on each spine
# ---------------------------------------------------------------------------

def test_isn_bgp_peers(store, topo, site_a):
    """Each SiteA spine's bgp/inst subtree must contain ISN bgpPeers:
    - addr ends in /32
    - asn == remote site's ASN (not the local site's ASN)
    """
    pod = site_a.pod
    local_asn = str(site_a.asn)
    remote_site = topo.other_site(site_a.name)
    remote_asn = str(remote_site.asn)
    remote_spines = remote_site.spine_nodes()
    remote_pod = remote_site.pod

    for spine in site_a.spine_nodes():
        inst_dn = f"topology/pod-{pod}/node-{spine.id}/sys/bgp/inst"
        # Collect all bgpPeer descendants under this inst
        bgp_peers = store.subtree(inst_dn, {"bgpPeer"})
        assert bgp_peers, f"No bgpPeer found under {inst_dn}"

        isn_peers = [
            p for p in bgp_peers
            if "/32" in p.attrs.get("addr", "")
            and p.attrs.get("asn", "") != local_asn
        ]
        assert isn_peers, (
            f"No ISN bgpPeer (/32 + remote ASN) found on spine {spine.id}. "
            f"Peers found: {[(p.attrs.get('addr'), p.attrs.get('asn')) for p in bgp_peers]}"
        )

        # Verify one ISN peer per remote spine
        isn_addrs = {p.attrs["addr"] for p in isn_peers}
        for rs in remote_spines:
            expected_addr = f"{loopback_ip(remote_pod, rs.id)}/32"
            assert expected_addr in isn_addrs, (
                f"Missing ISN peer to remote spine {rs.id} ({expected_addr}) "
                f"on spine {spine.id}. Found: {isn_addrs}"
            )
            # Verify the remote ASN
            for p in isn_peers:
                if p.attrs["addr"] == expected_addr:
                    assert p.attrs["asn"] == remote_asn, (
                        f"ISN peer {expected_addr} has asn={p.attrs['asn']!r}, "
                        f"expected {remote_asn!r}"
                    )


# ---------------------------------------------------------------------------
# Test: healthInst per node
# ---------------------------------------------------------------------------

def test_health_inst_per_node(store, site_a, topo):
    """Every node in SiteA must have a healthInst at topology/.../sys/health."""
    pod = site_a.pod
    hd = topo.faults.health_defaults
    for node in site_a.all_nodes():
        dn = f"topology/pod-{pod}/node-{node.id}/sys/health"
        mo = store.get(dn)
        assert mo is not None, f"healthInst missing for node {node.id} at {dn}"
        assert mo.class_name == "healthInst"
        assert mo.attrs["cur"] == str(hd.node), (
            f"healthInst cur={mo.attrs['cur']!r} != {hd.node!r} for node {node.id}"
        )


# ---------------------------------------------------------------------------
# Test: seeded faultInst under node-local DNs
# ---------------------------------------------------------------------------

def test_seeded_fault_insts(store, site_a, topo):
    """Seeded faultInst MOs must exist under node-local DNs (contain /node-{id}/)."""
    site_node_ids = {n.id for n in site_a.all_nodes()}

    for seed in topo.faults.seed:
        if seed.node not in site_node_ids:
            continue
        # Find a faultInst with the right code under the right node
        fault_dn_pattern = re.compile(rf"/node-{seed.node}/")
        found = [
            mo for mo in store.by_class("faultInst")
            if fault_dn_pattern.search(mo.attrs.get("dn", ""))
            and seed.severity == mo.attrs.get("severity", "")
        ]
        assert found, (
            f"No faultInst found for seed code={seed.code} node={seed.node} "
            f"severity={seed.severity}"
        )
        # Verify the DN contains /node-{id}/ (required for topology.py grouping)
        for mo in found:
            assert f"/node-{seed.node}/" in mo.attrs["dn"], (
                f"faultInst DN {mo.attrs['dn']!r} does not contain /node-{seed.node}/"
            )


# ---------------------------------------------------------------------------
# Test: topSystem loopback address
# ---------------------------------------------------------------------------

def test_top_system_loopback(store, site_a):
    """topSystem.address must follow the formula 10.<pod>.<node_id>.1"""
    pod = site_a.pod
    for node in site_a.all_nodes():
        ts_dn = f"topology/pod-{pod}/node-{node.id}/sys"
        mo = store.get(ts_dn)
        assert mo is not None, f"topSystem missing for node {node.id} at {ts_dn}"
        expected_lb = loopback_ip(pod, node.id)
        assert mo.attrs["address"] == expected_lb, (
            f"topSystem.address={mo.attrs['address']!r} != {expected_lb!r} "
            f"for node {node.id}"
        )


# ---------------------------------------------------------------------------
# Test: fabricLink DN format parseable by topology.py
# ---------------------------------------------------------------------------

def test_fabric_link_dn_format(store, site_a):
    """fabricLink DNs must be parseable by topology.py's port-extraction logic."""
    pod = site_a.pod
    for mo in store.by_class("fabricLink"):
        dn = mo.attrs["dn"]
        source_port = target_port = ""
        for seg in dn.split("/"):
            if seg.startswith("lnk-"):
                lnk_part = seg[4:]
                if "-to-" in lnk_part:
                    sp, dp = lnk_part.split("-to-", 1)
                    spc = sp.split("-")
                    dpc = dp.split("-")
                    if len(spc) >= 3:
                        source_port = f"eth{spc[1]}/{spc[2]}"
                    if len(dpc) >= 3:
                        target_port = f"eth{dpc[1]}/{dpc[2]}"
        assert source_port, f"Could not parse source_port from fabricLink DN: {dn}"
        assert target_port, f"Could not parse target_port from fabricLink DN: {dn}"
        # Ports must follow ethN/M format
        assert re.match(r"eth\d+/\d+", source_port), f"Bad source_port {source_port!r}"
        assert re.match(r"eth\d+/\d+", target_port), f"Bad target_port {target_port!r}"
