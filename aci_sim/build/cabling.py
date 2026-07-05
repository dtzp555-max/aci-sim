"""build/cabling.py — fabricLink, lldpAdjEp, cdpAdjEp derived from the cabling graph.

fabricLink DN (verified against autoACI/backend/routers/topology.py lines 381-388):
  topology/pod-{pod}/lnk-{n1}-{s1}-{p1}-to-{n2}-{s2}-{p2}
  where "lnk-{n1}-{s1}-{p1}-to-...".split("-") = [n1, s1, p1, ...]
  → source_port = f"eth{s1}/{p1}"  (spc[1], spc[2])
  n1/n2 attributes carry the plain node IDs.

Port assignment (auto mode):
  Spines: uplinks eth1/1, eth1/2, ... (one per downlink, in order)
  Leaves/border-leaves: uplinks eth1/49, eth1/50, ... (one per spine, in order)
"""
from __future__ import annotations

from aci_sim.build.fabric import oob_ip
from aci_sim.build.neighbors import add_switch_adjacency
from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import Node, Site, Topology


def cabling_links(site: Site) -> list[tuple[int, int, int, int, int, int]]:
    """Return (n1_id, slot1, port1, n2_id, slot2, port2) for every fabric link.

    Convention: n1=spine, n2=leaf/border-leaf.
    Spine port: eth1/{leaf_idx+1}
    Leaf uplink: eth1/{49+spine_idx}
    """
    spines = site.spine_nodes()
    downlinks = site.leaf_nodes() + site.border_leaf_nodes()
    links: list[tuple[int, int, int, int, int, int]] = []
    for l_idx, leaf in enumerate(downlinks):
        for s_idx, spine in enumerate(spines):
            links.append((
                spine.id, 1, l_idx + 1,   # spine eth1/(leaf_idx+1)
                leaf.id,  1, 49 + s_idx,   # leaf  eth1/(49+spine_idx)
            ))
    return links


def build(topo: Topology, site: Site, store: MITStore) -> None:
    """Emit fabricLink + lldpAdjEp + cdpAdjEp derived from the cabling graph."""
    pod = site.pod

    # Build a lookup: node_id -> Node (for name/loopback lookups)
    node_map: dict[int, Node] = {n.id: n for n in site.all_nodes()}

    for n1, s1, p1, n2, s2, p2 in cabling_links(site):
        lnk_dn = (
            f"topology/pod-{pod}"
            f"/lnk-{n1}-{s1}-{p1}-to-{n2}-{s2}-{p2}"
        )
        store.add(MO(
            "fabricLink",
            dn=lnk_dn,
            n1=str(n1),
            n2=str(n2),
            operSt="up",
            operSpeed="100G",
            linkType="leaf",
        ))

        # --- LLDP & CDP adjacencies on BOTH endpoints ---
        node1 = node_map.get(n1)
        node2 = node_map.get(n2)
        if node1 is None or node2 is None:
            continue

        port1 = f"eth{s1}/{p1}"
        port2 = f"eth{s2}/{p2}"
        oob1 = oob_ip(pod, n1)
        oob2 = oob_ip(pod, n2)

        # Node1 sees Node2 as neighbor on port1 (Node2's own port is port2)
        add_switch_adjacency(
            store,
            pod=pod,
            local_node_id=n1,
            local_port=port1,
            remote_port=port2,
            neighbor=node2,
            neighbor_mgmt_ip=oob2,
        )

        # Node2 sees Node1 as neighbor on port2 (Node1's own port is port1)
        add_switch_adjacency(
            store,
            pod=pod,
            local_node_id=n2,
            local_port=port2,
            remote_port=port1,
            neighbor=node1,
            neighbor_mgmt_ip=oob1,
        )
