"""build/mgmt.py — mgmt tenant OOB scaffolding: fvTenant(mgmt)/mgmtMgmtP/mgmtOoB/mgmtRsOoBStNode.

Real APIC models the OOB management address + gateway for every fabric node
in the special `mgmt` tenant, NOT on the node's own topSystem (topSystem only
carries the address — `oobMgmtAddr` — never a gateway; see build/fabric.py).
The real object tree (verified against APIC's own MIT, CONTRACT.md-style):

    uni/tn-mgmt                                       fvTenant   (name="mgmt")
      mgmtp-default                                    mgmtMgmtP  (name="default")
        oob-default                                    mgmtOoB    (name="default")
          rsooBStNode-[topology/pod-<p>/node-<id>]      mgmtRsOoBStNode
              tDn=topology/pod-<p>/node-<id>
              addr=<node's OOB IP>/<mask>
              gw=<site.oob_gateway or "">

This is the ONLY place a real ACI object carries an OOB gateway attribute —
prior to this builder, `Site`/the init wizard collected an OOB gateway
(cli.py's `run_wizard`) but discarded it (no schema field, no MO); this
builder is what makes that value land on a real MO instead.

Scope (deliberately minimal/faithful): one `mgmtRsOoBStNode` per node this
site actually has (switches — spines/leaves/border-leaves — AND controllers),
matching the module list `build/fabric.py` already builds `fabricNode`/
`topSystem` for. No other mgmt-tenant children (no `mgmtInB`, no
`mgmtRsOoBCtx`, no `mgmtInstP`, etc.) — those model in-band mgmt / EPG
contract-scope wiring that no autoACI/aci-py chain audited for this sim reads
(same "store scaffolding a caller doesn't consume is scope-creep" judgment
call as `Fabric.inb_subnet`'s docstring, topology/schema.py).

Mask derivation (`addr=<ip>/<mask>`): real ACI's OOB address is configured
with an explicit prefix at first-boot (the same dialog this sim's `aci-sim
init` wizard mirrors), and is virtually always a /24 (one OOB subnet per
pod/site) even though the fabric-wide address *pool* is much larger. This
sim's own `oob_ip(pod, node_id)` (build/fabric.py) reflects exactly that
shape — every node's OOB address is `192.168.<pod>.<node_id>`, i.e. already
confined to a /24 per pod (the pod occupies the third octet; the pool's
`/16` only bounds the SECOND octet across all pods). So the mask is derived,
in priority order:
  1. `fabric.oob_subnet` (Tier-3, PR-20) if the topology author explicitly
     set it — their stated intent overrides the derived default, even if it
     happens to be wider than /24 (e.g. a deliberately shared /16 OOB VLAN).
  2. `/24` otherwise (default) — matches the real per-pod OOB subnet shape
     `oob_ip()` already produces, NOT `fabric.oob_pool`'s `/16` (which
     describes the whole multi-pod address space, not any single node's
     subnet — using it directly would put every node in a /16 that spans
     pods it was never actually part of).
`fabric.oob_subnet`, when set, is already validated (Topology.
normalize_and_validate) to be a real CIDR inside 192.168.0.0/16, so
`.prefixlen` is always safe to read.

Gateway (`gw=...`): `site.oob_gateway` verbatim if set, else the EMPTY
STRING — never a derived/invented default. A derived default (e.g. ".1" of
the OOB subnet) would look like real config an operator entered, when no
operator entered anything; an empty `gw` faithfully says "not configured",
which is exactly the real APIC behavior before its own first-boot gateway
question is answered. This also keeps the change purely additive: a
topology.yaml that never sets `oob_gateway` builds byte-identical
mgmtRsOoBStNode.gw ("") on every rebuild.
"""
from __future__ import annotations

import ipaddress

from aci_sim.build.fabric import oob_ip
from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import Site, Topology

_DEFAULT_OOB_PREFIXLEN = 24  # real-ACI-shaped per-pod OOB subnet (see module docstring)


def _oob_prefixlen(topo: Topology) -> int:
    """Effective OOB subnet prefix length (see module docstring priority order)."""
    if topo.fabric.oob_subnet is not None:
        return ipaddress.ip_network(topo.fabric.oob_subnet, strict=False).prefixlen
    return _DEFAULT_OOB_PREFIXLEN


def build(topo: Topology, site: Site, store: MITStore) -> None:
    """Emit the mgmt-tenant OOB scaffolding for *site*: fvTenant(mgmt) →
    mgmtMgmtP(default) → mgmtOoB(default) → one mgmtRsOoBStNode per node."""
    prefixlen = _oob_prefixlen(topo)
    gw = site.oob_gateway or ""

    tenant_mo = MO("fvTenant", dn="uni/tn-mgmt", name="mgmt", descr="")
    mgmtp_mo = MO("mgmtMgmtP", dn="uni/tn-mgmt/mgmtp-default", name="default")
    oob_mo = MO("mgmtOoB", dn="uni/tn-mgmt/mgmtp-default/oob-default", name="default")

    pod = site.pod
    # Switches (spines/leaves/border-leaves): OOB address from the same
    # oob_ip(pod, node_id) derivation build/fabric.py used for topSystem.
    for node in site.all_nodes():
        node_dn = f"topology/pod-{pod}/node-{node.id}"
        addr = f"{oob_ip(pod, node.id)}/{prefixlen}"
        oob_mo.add_child(MO(
            "mgmtRsOoBStNode",
            dn=f"{oob_mo.dn}/rsooBStNode-[{node_dn}]",
            tDn=node_dn,
            addr=addr,
            gw=gw,
        ))

    # Controllers (APIC cluster, node IDs 1..N): OOB address from the same
    # controller_oob_ips()/oob_ip(pod, cid) resolution build/fabric.py uses
    # for the controller topSystem (explicit controller_ips, else derived
    # from mgmt_ip, else legacy per-node oob_ip(pod, cid) fallback).
    controller_oob_ips = site.controller_oob_ips()
    for cid in range(1, site.controllers + 1):
        ctrl_dn = f"topology/pod-{pod}/node-{cid}"
        oob_addr = controller_oob_ips[cid - 1] if controller_oob_ips else oob_ip(pod, cid)
        addr = f"{oob_addr}/{prefixlen}"
        oob_mo.add_child(MO(
            "mgmtRsOoBStNode",
            dn=f"{oob_mo.dn}/rsooBStNode-[{ctrl_dn}]",
            tDn=ctrl_dn,
            addr=addr,
            gw=gw,
        ))

    mgmtp_mo.add_child(oob_mo)
    tenant_mo.add_child(mgmtp_mo)
    store.add(tenant_mo)
