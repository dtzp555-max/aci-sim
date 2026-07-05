"""build/underlay.py — bgpInst (local ASN), isisAdjEp + ospfAdjEp.

Intra-fabric underlay IGP is IS-IS: isisAdjEp is emitted between every directly
cabled spine↔leaf pair. OSPF is used ONLY by spines toward the inter-site network
(IPN/ISN) and ONLY in a multi-site fabric — leaves never peer OSPF with the ISN.
bgpInst carries the site ASN and anchors all BGP peers (overlay.py adds children).

Tier-3 (PR-20): ospfIf also carries `helloIntvl`/`deadIntvl`, fed by
`isn.ospf_hello`/`isn.ospf_dead` (real ACI defaults 10/40) — previously not
carried on this MO at all.
"""
from __future__ import annotations

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import Site, Topology
from aci_sim.build.fabric import loopback_ip
from aci_sim.build.cabling import cabling_links


def build(topo: Topology, site: Site, store: MITStore) -> None:
    """Emit bgpInst per node + OSPF/IS-IS adjacencies along fabric links."""
    pod = site.pod
    spine_ids = {n.id for n in site.spine_nodes()}

    # bgpInst per node — carries the site ASN; overlay.py adds bgpPeer children
    for node in site.all_nodes():
        store.add(MO(
            "bgpInst",
            dn=f"topology/pod-{pod}/node-{node.id}/sys/bgp/inst",
            asn=str(site.asn),
        ))

    # isisDom per node (IS-IS is the ACI underlay IGP)
    for node in site.all_nodes():
        store.add(MO(
            "isisDom",
            dn=f"topology/pod-{pod}/node-{node.id}/sys/isis/inst-default/dom-overlay-1",
            name="overlay-1",
            operSt="up",
        ))

    # IS-IS adjacencies — the intra-fabric underlay, between directly-cabled
    # spine↔leaf pairs only. Convention: n1=spine, n2=leaf (same as cabling.py).
    for n1, s1, p1, n2, s2, p2 in cabling_links(site):
        spine_id, leaf_id = (n1, n2) if n1 in spine_ids else (n2, n1)
        spine_port = f"eth{s1}/{p1}" if n1 in spine_ids else f"eth{s2}/{p2}"
        leaf_port  = f"eth{s2}/{p2}" if n1 in spine_ids else f"eth{s1}/{p1}"
        spine_lb = loopback_ip(pod, spine_id)
        leaf_lb  = loopback_ip(pod, leaf_id)

        spine_isis_base = f"topology/pod-{pod}/node-{spine_id}/sys/isis/inst-default/dom-overlay-1"
        store.add(MO(
            "isisAdjEp",
            dn=f"{spine_isis_base}/if-[{spine_port}]/adj-[{leaf_lb}]",
            operSt="up", sysId=leaf_lb, lastTrans="00:00:00", numAdjTrans="1",
        ))
        leaf_isis_base = f"topology/pod-{pod}/node-{leaf_id}/sys/isis/inst-default/dom-overlay-1"
        store.add(MO(
            "isisAdjEp",
            dn=f"{leaf_isis_base}/if-[{leaf_port}]/adj-[{spine_lb}]",
            operSt="up", sysId=spine_lb, lastTrans="00:00:00", numAdjTrans="1",
        ))

    # OSPF adjacencies — ONLY spine↔IPN/ISN, and ONLY in a multi-site fabric.
    # Leaves never run OSPF to the ISN. One adjacency per spine toward the IPN,
    # on eth1/{49+si} — interfaces.py builds a matching dedicated ISN uplink
    # l1PhysIf on each spine (multi-site only) at that exact port, so this
    # ospfAdjEp interface always resolves against a real port inventory entry.
    # autoACI reads: iface = dn.split("/if-[")[1]; neighbor = peerIp or nbrId.
    if len(topo.sites) > 1:
        ipn_ip = f"172.16.{site.id}.254"  # the IPN/ISN router this site's spines peer with
        # Tier-2 (PR-19): isn.ospf_area feeds ospfIf.area/ospfAdjEp.area — was
        # a hardcoded "0.0.0.0" literal; a topology author overriding
        # isn.ospf_area now sees it reflected in the actual OSPF object
        # instead of a value the schema stored but no builder honored.
        ospf_area = topo.isn.ospf_area
        # Tier-3 (PR-20): isn.ospf_hello/ospf_dead (real ACI defaults 10/40)
        # feed ospfIf.helloIntvl/deadIntvl — previously not carried on this
        # MO at all.
        ospf_hello = str(topo.isn.ospf_hello)
        ospf_dead = str(topo.isn.ospf_dead)
        for si, spine in enumerate(site.spine_nodes()):
            ospf_base = f"topology/pod-{pod}/node-{spine.id}/sys/ospf/inst-default/dom-overlay-1"
            store.add(MO("ospfDom", dn=ospf_base, name="overlay-1", operSt="up"))
            # Batch-1: ospfIf nested between ospfDom and ospfAdjEp — same
            # eth1/{49+si} ISN uplink port interfaces.py already builds a
            # dedicated l1PhysIf for, so the existing ospfAdjEp below sits on
            # a real, matching ospfIf (consumer: autoACI's fabric_ospf.py,
            # which reads ospfIf.id (falling back to ifId) + area + operSt).
            if_port = f"eth1/{49 + si}"
            if_dn = f"{ospf_base}/if-[{if_port}]"
            store.add(MO(
                "ospfIf",
                dn=if_dn,
                id=if_port,
                area=ospf_area,
                operSt="up",
                helloIntvl=ospf_hello,
                deadIntvl=ospf_dead,
            ))
            store.add(MO(
                "ospfAdjEp",
                dn=f"{if_dn}/adj-[{ipn_ip}]",
                operSt="full", peerIp=ipn_ip, nbrId=ipn_ip, area=ospf_area,
            ))
