"""build/neighbors.py — shared LLDP/CDP adjacency + container-MO helper.

There are THREE call sites that construct lldpAdjEp/cdpAdjEp adjacencies:
  - build/cabling.py   (spine<->leaf fabric links, both directions)
  - build/fabric.py    (APIC<->leaf attachment, leaf-side adjacency)
  - rest_aci/writes.py (dynamic leaf<->spine adjacencies for a POST-registered
                         node, via store.upsert on an already-built store)

All three must emit an IDENTICAL, consistent field set (real-APIC-shaped
lldpAdjEp/cdpAdjEp) AND materialize the lldpInst/lldpIf/cdpInst/cdpIf
container MOs the adjacency hangs off of — without this, the container's own
DN (".../sys/lldp/inst") has no MO of its own, and a deep-root subtree query
(`GET .../sys/lldp/inst.json?query-target=subtree&target-subtree-class=
lldpAdjEp`) returns totalCount=0 even though the adjacency itself is fully
reachable via a plain class query. See `aci_sim.query.engine.
run_mo_query`: `store.get(dn) is None` short-circuits to `([], 0)` before the
subtree walk even starts.

This module centralizes both concerns behind one function
(`add_adjacency`) so the three call sites can never drift apart (e.g. one
site adding `portIdV` and another forgetting it).

Store-method choice: every write below goes through ``store.upsert(...)``,
never ``store.add``. Verified against ``aci_sim/mit/store.py``:
``upsert`` on a DN that doesn't exist yet takes the exact same path ``add``
does (construct a fresh ``MO``, store it, call ``_register``) — the two are
behaviorally identical on an empty/fresh store. ``upsert`` additionally
merges into an existing entry instead of clobbering it, which is required
here because lldpInst/lldpIf/cdpInst/cdpIf are shared containers written
once per node/port but visited twice (once per directed adjacency at a
spine<->leaf link, per-APIC at the controller-attach site). Callers that
already exclusively use ``store.add`` (cabling.py, fabric.py boot-time build)
lose nothing by switching: the store is empty at those DNs on first visit,
so ``upsert`` behaves exactly like ``add`` there too.
"""
from __future__ import annotations

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import Node

# APIC controllers report this fixed platform id on real hardware.
APIC_PLATFORM_ID = "APIC-SERVER-M3"


def node_mac(pod: int, node_id: int) -> str:
    """Deterministic, unique, valid MAC for *node_id* in *pod*.

    Used as lldpAdjEp.chassisIdV (the neighbor's chassis MAC) and as
    lldpIf.mac (the local interface's reported MAC) — real LLDP always
    carries a chassis MAC, so a believable, stable value beats an empty
    string. Scheme: 00:1B:0D:<pod>:<node_id high byte>:<node_id low byte>.
    00:1B:0D is an arbitrary-but-fixed OUI-shaped prefix (not a real
    allocation) chosen purely so the value looks like a plausible MAC;
    pod/node_id are masked into single bytes so the address is always
    well-formed regardless of how large either input is.
    """
    return f"00:1B:0D:{pod & 0xFF:02X}:{(node_id >> 8) & 0xFF:02X}:{node_id & 0xFF:02X}"


def _lldp_inst_dn(pod: int, node_id: int) -> str:
    return f"topology/pod-{pod}/node-{node_id}/sys/lldp/inst"


def _cdp_inst_dn(pod: int, node_id: int) -> str:
    return f"topology/pod-{pod}/node-{node_id}/sys/cdp/inst"


def ensure_lldp_containers(store: MITStore, pod: int, node_id: int, local_port: str) -> None:
    """Idempotently materialize lldpInst (per-node) + lldpIf (per-port).

    Safe to call once per directed adjacency that lands on this node/port —
    ``store.upsert`` merges into the same DN instead of duplicating it.
    """
    inst_dn = _lldp_inst_dn(pod, node_id)
    store.upsert(MO(
        "lldpInst",
        dn=inst_dn,
        adminSt="enabled",
        name="default",
        holdTime="120",
        initDelayTime="2",
        txFreq="30",
        ctrl="rx-en,tx-en",
        optTlvSel="mgmt-addr,port-desc,port-vlan,sys-cap,sys-desc,sys-name",
    ))
    store.upsert(MO(
        "lldpIf",
        dn=f"{inst_dn}/if-[{local_port}]",
        id=local_port,
        adminRxSt="enabled",
        adminTxSt="enabled",
        operRxSt="up",
        operTxSt="up",
        mac=node_mac(pod, node_id),
        portDesc="",
        portVlan="0",
        sysDesc="",
        wiring="valid",
    ))


def ensure_cdp_containers(store: MITStore, pod: int, node_id: int, local_port: str) -> None:
    """Idempotently materialize cdpInst (per-node) + cdpIf (per-port)."""
    inst_dn = _cdp_inst_dn(pod, node_id)
    store.upsert(MO(
        "cdpInst",
        dn=inst_dn,
        adminSt="enabled",
        name="default",
        holdIntvl="180",
        txFreq="60",
        ver="v2",
        ctrl="",
    ))
    store.upsert(MO(
        "cdpIf",
        dn=f"{inst_dn}/if-[{local_port}]",
        id=local_port,
        adminSt="enabled",
        operSt="up",
    ))


def add_adjacency(
    store: MITStore,
    *,
    pod: int,
    local_node_id: int,
    local_port: str,
    remote_port: str,
    neighbor_name: str,
    neighbor_mgmt_ip: str,
    neighbor_kind: str,
    neighbor_model: str = "",
    neighbor_version: str = "",
    neighbor_mac: str = "",
) -> None:
    """Emit one directed lldpAdjEp+cdpAdjEp (local node sees *neighbor* on
    *local_port*) AND ensure the lldpInst/lldpIf/cdpInst/cdpIf containers for
    (local_node_id, local_port) exist.

    *neighbor_kind* is ``"switch"`` (spine/leaf/border-leaf) or
    ``"controller"`` (APIC) — drives the sysDesc/capability/platId/cap field
    shapes below (real APIC neighbors don't report bridge/router capability).
    *neighbor_mac* is the neighbor's chassis MAC (chassisIdV). Every call site
    passes one (derived via ``node_mac`` from the neighbor's node id), so the
    ``""`` default is never hit in practice; it exists only so an omitted MAC
    yields a syntactically valid (if uninformative) empty chassisIdV rather
    than a KeyError.
    """
    base = f"topology/pod-{pod}/node-{local_node_id}/sys"

    if neighbor_kind == "controller":
        sys_desc = "Cisco APIC"
        capability = ""
        plat_id = APIC_PLATFORM_ID
        cap = ""
    else:
        sys_desc = f"{neighbor_model} running {neighbor_version}"
        capability = "bridge,router"
        plat_id = neighbor_model
        cap = "bridge,router"

    store.upsert(MO(
        "lldpAdjEp",
        dn=f"{base}/lldp/inst/if-[{local_port}]/adj-1",
        sysName=neighbor_name,
        mgmtIp=neighbor_mgmt_ip,
        portIdV=remote_port,
        # 802.1AB Port-ID subtype 5: portIdV carries a named interface
        # (e.g. "eth1/1"), which real ACI reports as interfaceName — NOT
        # locallyAssigned (subtype 7), which is for opaque port-id strings.
        portIdT="interfaceName",
        chassisIdT="mac",
        chassisIdV=neighbor_mac,
        sysDesc=sys_desc,
        capability=capability,
        id="1",
        ttl="120",
    ))
    store.upsert(MO(
        "cdpAdjEp",
        dn=f"{base}/cdp/inst/if-[{local_port}]/adj-1",
        sysName=neighbor_name,
        mgmtIp=neighbor_mgmt_ip,
        devId=neighbor_name,
        portId=remote_port,
        platId=plat_id,
        ver=neighbor_version,
        cap=cap,
    ))

    ensure_lldp_containers(store, pod, local_node_id, local_port)
    ensure_cdp_containers(store, pod, local_node_id, local_port)


def add_switch_adjacency(
    store: MITStore,
    *,
    pod: int,
    local_node_id: int,
    local_port: str,
    remote_port: str,
    neighbor: Node,
    neighbor_mgmt_ip: str,
) -> None:
    """Convenience wrapper for the common switch<->switch case (cabling.py,
    fabric.py's dynamic-registration mirror in writes.py): derives
    model/version/mac from the neighbor ``Node`` directly instead of making
    every call site repeat ``neighbor_model=neighbor.model,
    neighbor_version=neighbor.version, neighbor_mac=node_mac(pod, neighbor.id)``.
    ``neighbor_mgmt_ip`` is still passed explicitly because callers derive it
    via ``oob_ip()`` (or, for a dynamically-registered node, a value already
    computed by the caller) rather than something derivable from ``Node``
    alone.
    """
    add_adjacency(
        store,
        pod=pod,
        local_node_id=local_node_id,
        local_port=local_port,
        remote_port=remote_port,
        neighbor_name=neighbor.name,
        neighbor_mgmt_ip=neighbor_mgmt_ip,
        neighbor_kind="switch",
        neighbor_model=neighbor.model,
        neighbor_version=neighbor.version,
        neighbor_mac=node_mac(pod, neighbor.id),
    )
