"""build/interfaces.py — l1PhysIf, ethpmPhysIf, eqptcapacityPolUsage5min per node.

Port layout:
  Fabric uplink ports:  same assignment as cabling.py (derived independently)
    - Spines:           eth1/1, eth1/2, ... (one per downlink node)
    - Leaves/BLs:       eth1/49, eth1/50, ... (one per spine)
  Host-facing ports on leaves/border-leaves: eth1/1 – eth1/8 (simulated access)
  L3Out routed sub-interface port on border-leaves ONLY: eth1/48 (dedicated,
    outside the host-access range — matches real ACI convention of a dedicated
    front-panel port for the L3Out CSW uplink rather than sharing a host port).
  ISN/IPN uplink ports on spines ONLY, multi-site fabrics only: eth1/49, 50, ...
    (one per spine, dedicated — outside the fabric-uplink range eth1/1-4, which
    is fully consumed by intra-fabric spine↔leaf links). Real ACI spines facing
    a multi-site ISN use dedicated front-panel ports for the IPN uplink, distinct
    from the leaf-facing fabric ports; underlay.py's ospfAdjEp references these.

ethpmPhysIf DN must contain "phys-[eth{s}/{p}]" — read by topology.py line 730:
  port = dn.split("phys-[")[1].split("]")[0]   →  "eth1/1"

l1PhysIf DN is under the same sys/ subtree; id attribute carries the port name.

Batch-2 addition (CONTRACT.md §6 "Interfaces"): ethpmFcot — SFP/transceiver
info, sibling of ethpmPhysIf under the same phys-[eth{s}/{p}] port DN (own RN
"fcot", per real ACI's ethpmFcot placement one level under ethpmPhysIf's own
"phys" RN's parent). Attrs verified read-only against autoACI's
interface_comprehensive.py: typeName, guiName, vendorName, vendorSn,
actualType (decoded via that plugin's own `_decode_sfp_field`/`_port_from_dn`
helpers, which just strip a "phys-[" bracket segment off the dn — same
placement l1PhysIf/ethpmPhysIf already use). Only emitted for fabric-uplink
and L3Out routed ports (real optics-bearing ports); plain host-access ports
are left without an ethpmFcot, matching real ACI labs where copper/DAC host
ports often report no transceiver inventory while fabric/WAN-facing optical
ports do — a deliberate realism choice, not an omission.
"""
from __future__ import annotations

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import Site, Topology
from aci_sim.build.cabling import cabling_links

_UPLINK_START = 49   # first leaf-side uplink port index
_HOST_PORTS   = 8    # number of host-facing ports on leaves
_L3OUT_PORT   = 48   # dedicated border-leaf L3Out routed sub-interface port


def _add_port(
    store: MITStore,
    pod: int,
    node_id: int,
    slot: int,
    port: int,
    descr: str = "",
    mode: str = "trunk",
    usage: str = "fabric",
    with_fcot: bool = False,
    mtu: str = "9216",
) -> None:
    """Add l1PhysIf + ethpmPhysIf (+ optional ethpmFcot) for one port.

    `mtu` defaults to the fabric intra-fabric MTU (9216); the ISN/IPN uplink
    port (multi-site only) passes `topo.isn.mtu` instead (Tier-2, PR-19) —
    real ACI's inter-pod/inter-site default is 9150, distinct from the
    intra-fabric 9216 ceiling.
    """
    port_id = f"eth{slot}/{port}"
    sys_dn = f"topology/pod-{pod}/node-{node_id}/sys"
    phys_dn = f"{sys_dn}/phys-[{port_id}]"
    # l1PhysIf — administrative config
    store.add(MO(
        "l1PhysIf",
        dn=phys_dn,
        id=port_id,
        adminSt="up",
        descr=descr,
        mtu=mtu,
        mode=mode,
    ))
    # ethpmPhysIf — operational state; DN must contain "phys-[eth…]"
    store.add(MO(
        "ethpmPhysIf",
        dn=f"{phys_dn}/phys",
        operSt="up",
        operSpeed="100G",
        usage=usage,
        lastLinkStChg="00:00:00:00.000",
        resetCtr="0",
    ))
    # Batch-2: ethpmFcot — SFP/transceiver inventory, sibling of ethpmPhysIf
    # (own "fcot" RN under the same phys-[eth…] port DN — interface_comprehensive.py's
    # _port_from_dn strips the same "phys-[" bracket segment this DN shares with
    # ethpmPhysIf, so both resolve to the same port string). Only real
    # optics-bearing ports (fabric uplinks, ISN uplinks, L3Out routed ports)
    # get one — see module docstring for the realism rationale.
    if with_fcot:
        store.add(MO(
            "ethpmFcot",
            dn=f"{phys_dn}/fcot",
            typeName="QSFP-100G-SR4",
            guiName="QSFP-100G-SR4",
            vendorName="CISCO-FINISAR",
            vendorSn=f"FNS{pod:02d}{node_id:04d}{port:02d}",
            actualType="qsfp-100g-sr4",
            operSt="up",
            operSpeed="100G",
        ))


def build(topo: Topology, site: Site, store: MITStore) -> None:
    """Emit l1PhysIf + ethpmPhysIf for all nodes, plus eqptcapacityPolUsage5min."""
    pod = site.pod
    spine_ids = {n.id for n in site.spine_nodes()}

    # Collect port assignments from the cabling graph
    # spine_ports[node_id] = set of (slot, port) used as fabric uplinks
    # leaf_ports[node_id]  = set of (slot, port) used as fabric uplinks
    spine_uplinks: dict[int, list[tuple[int, int]]] = {n.id: [] for n in site.spine_nodes()}
    leaf_uplinks:  dict[int, list[tuple[int, int]]] = {
        n.id: [] for n in site.leaf_nodes() + site.border_leaf_nodes()
    }

    for n1, s1, p1, n2, s2, p2 in cabling_links(site):
        if n1 in spine_ids:
            spine_uplinks[n1].append((s1, p1))
            leaf_uplinks[n2].append((s2, p2))
        else:
            spine_uplinks[n2].append((s2, p2))
            leaf_uplinks[n1].append((s1, p1))

    # Spines — fabric uplinks, plus (multi-site only) dedicated ISN uplinks
    for si, spine in enumerate(site.spine_nodes()):
        for slot, port in spine_uplinks[spine.id]:
            _add_port(
                store, pod, spine.id, slot, port,
                descr=f"Fabric uplink eth{slot}/{port}",
                mode="trunk",
                with_fcot=True,
            )
        if len(topo.sites) > 1:
            isn_port = _UPLINK_START + si
            _add_port(
                store, pod, spine.id, 1, isn_port,
                descr=f"ISN/IPN uplink eth1/{isn_port}",
                mode="routed",
                usage="isn",
                with_fcot=True,
                mtu=str(topo.isn.mtu),
            )

    border_leaf_ids = {bl.id for bl in site.border_leaves}

    # Leaves + border-leaves — fabric uplinks + host-facing ports
    for leaf in site.leaf_nodes() + site.border_leaf_nodes():
        # Fabric uplink ports
        for slot, port in leaf_uplinks[leaf.id]:
            _add_port(
                store, pod, leaf.id, slot, port,
                descr="Fabric uplink to spine",
                mode="trunk",
                with_fcot=True,
            )
        # Host-facing access ports: eth1/1 … eth1/_HOST_PORTS
        for hp in range(1, _HOST_PORTS + 1):
            _add_port(
                store, pod, leaf.id, 1, hp,
                descr=f"Host port eth1/{hp}",
                mode="access",
                usage="access",
            )
        # Border-leaves only: dedicated L3Out routed sub-interface port,
        # outside the host-access range — l3out.py's l3extRsPathL3OutAtt
        # tDn references this exact port so it resolves against a real
        # l1PhysIf (real ACI convention: dedicated front-panel port for the
        # L3Out CSW uplink, not shared with regular host/EPG traffic).
        if leaf.id in border_leaf_ids:
            _add_port(
                store, pod, leaf.id, 1, _L3OUT_PORT,
                descr=f"L3Out routed uplink eth1/{_L3OUT_PORT}",
                mode="routed",
                usage="l3out",
                with_fcot=True,
            )
        # eqptcapacityPolUsage5min — capacity stats (contract-rendering metrics)
        store.add(MO(
            "eqptcapacityPolUsage5min",
            dn=f"topology/pod-{pod}/node-{leaf.id}/sys/eqptcapacity/polUsage5min",
            polUsage="42",
            polUsageCap="100",
            polUsageCum="38",
            polUsageCapCum="100",
        ))
