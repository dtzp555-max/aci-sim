"""build/fabric.py — fabricNode, topSystem, fabricHealthTotal, vpcDom/vpcIf.

PR-22 addition: ISN CSW lldpAdjEp/cdpAdjEp — one per spine, multi-site only,
on the spine's dedicated ISN uplink PHYSICAL port eth1/{49+si} (interfaces.py
builds the l1PhysIf; underlay.py builds the OSPF sub-interface
eth1/{49+si}.4 on the same base port). This is the third `add_adjacency`
call site the module docstring in neighbors.py already accounts for
(fabric.py) — it lives right after the existing APIC↔leaf adjacency loop in
`build()` below, not a new module, per that docstring's warning against a
fourth ad-hoc adjacency-construction site.

Batch-2 additions (CONTRACT.md §6 "Fabric/topology", "Infra write targets"; DN/
attribute shapes verified read-only against autoACI's vpc_status.py/
health_score.py — see docs/DESIGN.md "Batch-2" section):
  pcAggrIf      {vpcDom}/aggr-[po{bl.id}]         (port-channel details; vpc_status.py
                matches it to vpcIf by (node, name==ipg_name) to get the po id)
  pcRsMbrIfs    {pcAggrIf}/rsmbrIfs-[{ifDn}]      (member-port relation; CONTRACT.md
                catalogued this as "vpcRsMbrIfs" — WRONG, corrected to pcRsMbrIfs,
                the real ACI class vpc_status.py actually queries; see CONTRACT.md §6
                changelog note)
  infraWiNode   topology/pod-{p}/node-{m}/av/node-{c} (appliance-vector: this
                controller's view of cluster member {c}'s health)
  fabricNodeIdentP  uni/controller/nodeidentpol/nodep-{id}  (boot-seeded registration
                for every real fabricNode, so GETs return registered nodes without a
                prior POST; consistent with writes.py's existing add-leaf reaction,
                which also keys nodep-{id} on the node id, not the serial)

Address derivation (see loopback_ip/oob_ip docstrings for the full scheme).
node_id is expected in [1, 4095] (ACI node IDs are 3-4 decimal digits; this
covers every ID our topology.yaml or a hand-authored one could reasonably use).

  node_id <= 255 (legacy/unchanged — BACKWARD COMPATIBLE, existing labs keep
                  their addresses):
    loopback / topSystem.address:  10.<pod>.<node_id>.1
    OOB mgmt / topSystem.oobMgmtAddr:  192.168.<pod>.<node_id>

  node_id > 255 (new — needed once a site's node IDs exceed 255, e.g. LAB2's
                 301-402, which would otherwise overflow the last IPv4 octet
                 and produce invalid addresses like 10.1.401.1):
    loopback:  10.<pod>.<node_id % 256>.<1 + node_id // 256>
      - third octet reuses the legacy last-octet slot (node_id % 256)
      - fourth octet is bumped to 2, 3, ... instead of the legacy "1", so it
        can never collide with a node_id <= 255 loopback (those always end
        in ".1"); node_id // 256 is <= 15 for node_id <= 4095, keeping the
        fourth octet in [2, 16], well inside the valid range.
    OOB mgmt:  192.<168 + node_id // 256>.<pod>.<node_id % 256>
      - second octet is bumped from 168 to 169, 170, ... (never 168, which
        is reserved for the legacy node_id <= 255 scheme), so it can never
        collide with a legacy oob address either.
      - node_id // 256 is <= 15 for node_id <= 4095, keeping the second
        octet in [169, 183], still a valid unicast IPv4 octet.

  Both branches are unique per (pod, node_id): within a single pod the pair
  (node_id % 256, node_id // 256) is a bijection of node_id, and the fourth
  octet (loopback) / second octet (oob) values used by the > 255 branch never
  overlap with the values the <= 255 branch produces.
"""
from __future__ import annotations

import zlib

from aci_sim.build.neighbors import add_adjacency, node_mac
from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import Site, Topology

_LEGACY_MAX_NODE_ID = 255


def loopback_ip(pod: int, node_id: int) -> str:
    """Node loopback address.

    node_id <= 255: 10.<pod>.<node_id>.1                 (legacy, unchanged)
    node_id >  255: 10.<pod>.<node_id % 256>.<1 + node_id // 256>

    See module docstring for why this can never produce an invalid octet
    (>255) or collide with the legacy scheme.
    """
    if node_id <= _LEGACY_MAX_NODE_ID:
        return f"10.{pod}.{node_id}.1"
    return f"10.{pod}.{node_id % 256}.{1 + node_id // 256}"


def oob_ip(pod: int, node_id: int) -> str:
    """OOB management address.

    node_id <= 255: 192.168.<pod>.<node_id>              (legacy, unchanged)
    node_id >  255: 192.<168 + node_id // 256>.<pod>.<node_id % 256>

    See module docstring for why this can never produce an invalid octet
    (>255) or collide with the legacy scheme.
    """
    if node_id <= _LEGACY_MAX_NODE_ID:
        return f"192.168.{pod}.{node_id}"
    return f"192.{168 + node_id // 256}.{pod}.{node_id % 256}"


def default_serial(site_id: str, node_id: int) -> str:
    """Deterministic non-empty serial for an auto-generated node (PR-18).

    Format: SAL{site_id}{node_id:04d} — e.g. site "1" node 101 -> "SAL10101".
    Ansible's `cisco.aci.aci_fabric_node` keys node registration on serial;
    prior to PR-18 an unset `Node.serial` fell back to the non-deterministic-
    looking (but actually deterministic) "SIM{node.id:08d}". This helper
    replaces that fallback with a scheme that also encodes the site, so two
    sites' node-101 (impossible today given the 200-offset ID scheme, but a
    hand-authored topology could still reuse ids across unrelated tools)
    produce visibly distinct serials. Explicit YAML `serial:` values on a
    Node always take precedence — this is only the fallback for `serial=""`.
    """
    return f"SAL{site_id}{node_id:04d}"


def build(topo: Topology, site: Site, store: MITStore) -> None:
    """Emit fabricNode, topSystem, fabricHealthTotal, vpcDom/vpcIf for *site*."""
    pod = site.pod
    fabric_name = site.fabric_name or topo.fabric.name  # per-site fabricDomain (site grouping)
    spine_ids = {n.id for n in site.spine_nodes()}

    for node in site.all_nodes():
        role = "spine" if node.id in spine_ids else "leaf"
        node_dn = f"topology/pod-{pod}/node-{node.id}"

        store.add(MO(
            "fabricNode",
            dn=node_dn,
            id=str(node.id),
            name=node.name,
            role=role,
            model=node.model,
            serial=node.serial or default_serial(site.id, node.id),
            version=node.version,
            adSt="on",
            fabricSt="active",
        ))

        store.add(MO(
            "topSystem",
            dn=f"{node_dn}/sys",
            id=str(node.id),
            name=node.name,
            role=role,
            version=node.version,
            address=loopback_ip(pod, node.id),
            oobMgmtAddr=oob_ip(pod, node.id),
            fabricDomain=fabric_name,
            state="in-service",
            podId=str(pod),
        ))

    # APIC controllers — the APIC cluster (role=controller, node IDs 1..N).
    # Real ACI exposes these as fabricNode + topSystem role=controller; autoACI
    # reads fabricDomain + version from the controller topSystem at login and
    # renders the controllers as nodes in Topology.
    fabric_domain = site.fabric_name or fabric_name
    # Tier-3 (PR-20): fabric.default_apic_version is a fabric-wide override
    # for the APIC controller version, taking precedence over this site's
    # own site.apic_version when set (None/omitted = unchanged pre-PR-20
    # behavior: each site keeps using its own site.apic_version, already
    # independently settable from switch-node Node.version since PR-18).
    apic_version = topo.fabric.default_apic_version or site.apic_version
    # PR-21: per-controller OOB mgmt IP. Site.controller_oob_ips() returns
    # either the explicit `controller_ips` list, or a sequential derivation
    # from `mgmt_ip` (controller 1 = mgmt_ip, controller 2 = mgmt_ip+1, ...).
    # An empty list means neither is available (mgmt_ip unset) — fall back
    # to the legacy per-node oob_ip(pod, cid) scheme, unchanged pre-PR-21
    # behavior for topologies that never set mgmt_ip. The sim's REST server
    # always binds to `mgmt_ip` regardless of `controllers` (controller-1 /
    # cluster VIP) — this loop only affects the *simulated* fabricNode/
    # topSystem/infraWiNode data, never which address is actually served.
    controller_oob_ips = site.controller_oob_ips()
    for cid in range(1, site.controllers + 1):
        ctrl_dn = f"topology/pod-{pod}/node-{cid}"
        apic_name = f"{site.name}-ACI-APIC{cid:02d}"
        oob_addr = controller_oob_ips[cid - 1] if controller_oob_ips else oob_ip(pod, cid)
        store.add(MO(
            "fabricNode",
            dn=ctrl_dn,
            id=str(cid),
            name=apic_name,
            role="controller",
            model="APIC-SERVER-M3",
            serial=f"FCH00APIC{cid:03d}",
            version=apic_version,
            adSt="on",
            fabricSt="active",
        ))
        store.add(MO(
            "topSystem",
            dn=f"{ctrl_dn}/sys",
            id=str(cid),
            name=apic_name,
            role="controller",
            version=apic_version,
            address=f"10.0.{pod}.{cid}",
            oobMgmtAddr=oob_addr,
            fabricDomain=fabric_domain,
            state="in-service",
            podId=str(pod),
        ))

        # PR-9 FEATURE 5 — firmwareCtrlrRunning: cisco.aci and other clients
        # read the running APIC firmware version off this class (real ACI
        # RN "ctrlrfwstatuscont/ctrlrrunning", one per controller), rather
        # than (or in addition to) topSystem.version. Trivially seedable
        # from the same apic_version topSystem already uses above (Tier-3,
        # PR-20: fabric.default_apic_version or site.apic_version), so the
        # two never drift apart.
        store.add(MO(
            "firmwareCtrlrRunning",
            dn=f"{ctrl_dn}/ctrlrfwstatuscont/ctrlrrunning",
            version=apic_version,
            node=str(cid),
        ))

    # Batch-2: infraWiNode — appliance vector. Each APIC in the cluster carries
    # its OWN view of every cluster member's health (real ACI: the "av"
    # subtree under each controller's topSystem); health_score.py's cluster-
    # fitness check reads infraWiNode.health across ALL of these, so every
    # (viewer, member) pair is seeded "fully-fit" — a healthy, fully-formed
    # cluster, matching this sim's other all-healthy defaults (fabricHealthTotal
    # cur=100, healthInst cur=hd.node).
    for viewer_cid in range(1, site.controllers + 1):
        for member_cid in range(1, site.controllers + 1):
            store.add(MO(
                "infraWiNode",
                dn=f"topology/pod-{pod}/node-{viewer_cid}/av/node-{member_cid}",
                health="fully-fit",
                state="in-service",
            ))

    # APIC ↔ leaf attachments. Real APICs are dual-homed to a leaf pair; expose
    # them as fabricLink (autoACI draws Topology edges from fabricLink n1/n2) plus
    # a leaf-side lldpAdjEp so the leaf "sees" the controller.
    apic_leaves = site.leaf_nodes()[:2]
    for cid in range(1, site.controllers + 1):
        oob_addr = controller_oob_ips[cid - 1] if controller_oob_ips else oob_ip(pod, cid)
        for li, leaf in enumerate(apic_leaves):
            apic_port = li + 1   # APIC NIC: eth1/1 → first leaf, eth1/2 → second leaf
            leaf_port = cid      # leaf downlink to APIC-<cid>: eth1/<cid>
            store.add(MO(
                "fabricLink",
                dn=f"topology/pod-{pod}/lnk-{cid}-1-{apic_port}-to-{leaf.id}-1-{leaf_port}",
                n1=str(cid),
                n2=str(leaf.id),
                operSt="up",
                operSpeed="10G",
                linkType="controller",
            ))
            add_adjacency(
                store,
                pod=pod,
                local_node_id=leaf.id,
                local_port=f"eth1/{leaf_port}",
                # The APIC's own NIC port (its "eth1/{apic_port}") — real ACI
                # LLDP/CDP always reports the remote port on the neighbor's
                # side, even when the neighbor is a controller rather than a
                # switch. No fabricLink-style back-edge exists for the APIC
                # side to look this up from, so it's derived the same way
                # the fabricLink DN above already encodes it (apic_port).
                remote_port=f"eth1/{apic_port}",
                neighbor_name=f"{site.name}-ACI-APIC{cid:02d}",
                neighbor_mgmt_ip=oob_addr,
                neighbor_kind="controller",
                neighbor_mac=node_mac(pod, cid),
                # cdpAdjEp.ver: APIC firmware version is readily available
                # here (the same `apic_version` this function already
                # resolved above for the controller's own topSystem/
                # firmwareCtrlrRunning), so surface it rather than "".
                neighbor_version=apic_version,
            )

    # ISN CSW LLDP neighbor — multi-site only, one per spine's dedicated ISN
    # uplink PHYSICAL port (eth1/{49+si} — see interfaces.py/underlay.py: the
    # OSPF logical interface on this port is a routed VLAN-4 sub-interface,
    # "eth1/{49+si}.{49+si}" per the ACI naming convention, but LLDP itself is
    # advertised per physical port, so this adjacency targets the base port).
    # The CSW-side port-id follows the IPN/ISN Nexus convention — a dot1q-4
    # routed sub-interface named after the VLAN ("Ethernet1/x.4", e.g. real
    # IPN configs `interface Ethernet1/1.4` + `encapsulation dot1q 4`).
    # autoACI's Topology "Fabric (physical)" view derives this uplink row from
    # ospfIf (port + local IP) + ospfAdjEp (remote IP) + this lldpAdjEp
    # (CSW-side port), so without it the neighbor port/name show up blank.
    if len(topo.sites) > 1:
        isn_ip = f"172.16.{site.id}.254"
        for si, spine in enumerate(site.spine_nodes()):
            add_adjacency(
                store,
                pod=pod,
                local_node_id=spine.id,
                local_port=f"eth1/{49 + si}",
                remote_port=f"Ethernet1/{si + 1}.4",
                neighbor_name=f"ISN-CSW{site.id}",
                neighbor_mgmt_ip=isn_ip,
                neighbor_kind="switch",
                neighbor_model="N9K-C9364C",
                neighbor_version="n9000-10.2(5)",
                # Synthetic chassis MAC for the (simulated, off-fabric) ISN
                # CSW device — deliberately a DIFFERENT OUI-shaped prefix
                # ("02:1B:0D", locally-administered per IEEE 802 bit-1-of-
                # first-octet convention) than node_mac()'s "00:1B:0D" used
                # for real fabric nodes, so this can never collide with a
                # spine/leaf/APIC chassis MAC regardless of site.id/node id
                # overlap. Keyed by site.id (not node id — the ISN CSW is
                # one device per site, not per spine). site.id is schema-typed
                # as an unconstrained str, so hash it instead of int()-casting
                # (a non-numeric id like "east" must not crash the build).
                neighbor_mac=(
                    f"02:1B:0D:{zlib.crc32(str(site.id).encode()) & 0xFF:02X}:00:01"
                ),
            )

    # fabricHealthTotal — fabric-wide health summary
    store.add(MO("fabricHealthTotal", dn="topology/health", cur="100"))

    # vpcDom + vpcIf per border-leaf; group by vpc_domain to assign stable IDs
    #
    # Batch-2: pcAggrIf (port-channel details) + pcRsMbrIfs (member-port
    # relation) per vPC leg, consistent with the vpcIf this loop already
    # builds for the same (bl, dom_num) pair. vpc_status.py matches pcAggrIf
    # to vpcIf by (node, name) — name is the IPG name, id is "poN" — so
    # pcAggrIf.name reuses vpcIf's own "po{bl.id}" string as the IPG name
    # (this sim has no separate infraAccBndlGrp-derived IPG name to borrow;
    # access.py's vPC infraAccBndlGrp is named "{vpc_domain}-vpc", a
    # different string, so pcAggrIf.name intentionally matches vpcIf.name
    # instead — the field vpc_status.py actually joins on). Member ports:
    # two real host-facing l1PhysIf per leg (eth1/1, eth1/2 — interfaces.py
    # always builds these on every leaf/border-leaf), giving each vPC leg a
    # believable 2-member port-channel without colliding with the dedicated
    # L3Out port (eth1/48) or fabric uplinks.
    _VPC_MEMBER_PORTS = ("eth1/1", "eth1/2")
    domain_ids: dict[str, int] = {}
    for bl in site.border_leaves:
        vdom = bl.vpc_domain
        if vdom not in domain_ids:
            domain_ids[vdom] = len(domain_ids) + 1
        dom_num = domain_ids[vdom]
        vpc_dn = (
            f"topology/pod-{pod}/node-{bl.id}"
            f"/local/svc-vpc-stabdoms/domainid-{dom_num}"
        )
        store.add(MO(
            "vpcDom",
            dn=vpc_dn,
            id=str(dom_num),
            peerSt="peerOk",
        ))
        po_name = f"po{bl.id}"
        store.add(MO(
            "vpcIf",
            dn=f"{vpc_dn}/vpcif-[{po_name}]",
            name=po_name,
            operSt="up",
        ))

        pc_aggr_dn = f"{vpc_dn}/aggr-[{po_name}]"
        store.add(MO(
            "pcAggrIf",
            dn=pc_aggr_dn,
            name=po_name,
            id=po_name,
            operSt="up",
            operSpeed="100G",
            descr=f"vPC {po_name} on {bl.name}",
        ))
        for member_port in _VPC_MEMBER_PORTS:
            member_if_dn = f"topology/pod-{pod}/node-{bl.id}/sys/phys-[{member_port}]"
            store.add(MO(
                "pcRsMbrIfs",
                dn=f"{pc_aggr_dn}/rsmbrIfs-[{member_if_dn}]",
                tDn=member_if_dn,
                parentSKey=po_name,
            ))

    # Batch-2: fabricNodeIdentP — boot-seeded node registration for every
    # real switch fabricNode (spines/leaves/border-leaves; NOT controllers —
    # real ACI's nodeidentpol/Fabric Membership registers switches joining
    # the fabric, not the APICs themselves), so a GET returns the already-
    # registered nodes without requiring a prior POST. RN "nodep-{id}" keys
    # on the node id (not serial) to stay consistent with writes.py's
    # existing _materialize_fabric_node reaction, which parses the id back
    # out of this same "nodep-(\\d+)$" pattern — seeding here and the POST
    # reaction in writes.py must agree on the identifier scheme or a boot-
    # seeded nodep-{id} and a later POSTed nodep-{serial} would silently
    # diverge into two different DNs for logically the same registration.
    for node in site.all_nodes():
        store.add(MO(
            "fabricNodeIdentP",
            dn=f"uni/controller/nodeidentpol/nodep-{node.id}",
            serial=node.serial or default_serial(site.id, node.id),
            nodeId=str(node.id),
            name=node.name,
            role="leaf" if node.id not in spine_ids else "spine",
        ))
