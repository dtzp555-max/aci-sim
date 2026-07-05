"""build/routing.py — uribv4Route/uribv4Nexthop (per-leaf RIB) + arpAdjEp
(per-endpoint ARP table).

Batch-1 additions (CONTRACT.md §6 "Routing tables/control", "COOP/EPM"):
  uribv4Route     topology/pod-{p}/node-{n}/sys/uribv4/dom-{vrf}/db-rt/rt-[{prefix}]
  uribv4Nexthop   {route}/nh-[{proto}]-[{addr}]-[{ifname}]-[{vrf}]
  arpAdjEp        topology/pod-{p}/node-{n}/sys/arp/inst/dom-{vrf}/db-ip/adj-[{ip}]

DN/attribute shapes verified (read-only) against autoACI's
backend/plugins/route_table.py + routing_state.py (per CONTRACT.md header —
trust, don't re-explore):
  - uribv4Route.attrs["prefix"] is read (NOT "ip" — that's ipRouteP/l3extSubnet/
    fvSubnet's attribute name); VRF is parsed from the `dom-{name}` DN segment
    via `part[4:]` after `part.startswith("dom-")`.
  - uribv4Nexthop's parent-route DN is recovered via
    `_RE_NH_PARENT = re.compile(r"^(.*/rt-\\[[^\\]]+\\])/nh-")` (route_table.py /
    routing_state.py, both files, identical regex) — so the nexthop RN MUST
    start with "nh-" immediately after the route's own "rt-[...]" segment, and
    the "nh-" RN's own bracket content is opaque to this regex (any number of
    "-[...]" groups after "nh-" parses fine); we use the 4-group form the
    module docstring's own comment illustrates:
    ".../rt-[10.100.210.1/32]/nh-[direct]-[10.100.210.1/32]-[lo2]-[vrf]"
    i.e. nh-[{proto}]-[{addr}]-[{ifname}]-[{vrf}].
  - arpAdjEp: routing_state.py reads attrs["ifId"] and attrs["physIfId"]
    (Interface / Physical Interface columns) plus ip/mac/operSt (CONTRACT.md).

VRF "dom" naming reuses the exact `{tenant}:{vrf}` convention l3out.py's
_upsert_ebgp_sessions already established for bgpDom, so uribv4Route's VRF
column lines up with the same dom name a route_table.py VRF-scoped query
would be filtered on.
"""
from __future__ import annotations

import re

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import Site, Topology
from aci_sim.build.fabric import loopback_ip

_CEP_PORT_RE = re.compile(r"/node-(\d+)/sys/.*/db-ep/mac-")
_FRONT_PANEL_IF_RE = re.compile(r"paths-\d+/pathep-\[(eth\d+/\d+)\]$")


def _owning_site(l3out, topo: Topology) -> str | None:
    if l3out.site is not None:
        return l3out.site
    for s in topo.sites:
        site_bl_ids = {bl.id for bl in s.border_leaves}
        if any(bl_id in site_bl_ids for bl_id in l3out.border_leaves):
            return s.name
    return None


def _build_route(node_dn: str, vrf_dom: str, prefix: str, nh_addr: str, ifname: str) -> MO:
    """Build one uribv4Route + a single uribv4Nexthop child.

    Nexthop RN uses the 4-bracket form _RE_NH_PARENT expects:
    nh-[{proto}]-[{addr}]-[{ifname}]-[{vrf}].
    """
    base = f"{node_dn}/uribv4/dom-{vrf_dom}/db-rt"
    route_dn = f"{base}/rt-[{prefix}]"
    route_mo = MO("uribv4Route", dn=route_dn, prefix=prefix, pref="1")
    nh_dn = f"{route_dn}/nh-[static]-[{nh_addr}]-[{ifname}]-[{vrf_dom}]"
    route_mo.add_child(MO(
        "uribv4Nexthop",
        dn=nh_dn,
        nhAddr=nh_addr,
        ifName=ifname,
    ))
    return route_mo


def build(topo: Topology, site: Site, store: MITStore) -> None:
    """Emit per-leaf uribv4Route/Nexthop RIB entries + per-endpoint arpAdjEp.

    Runs after `endpoints` (orchestrator order) so fvCEp/fvIp data already in
    the store can be reused for arpAdjEp instead of re-deriving MAC/IP/port
    assignment independently.
    """
    pod = site.pod

    # ---- uribv4Route/Nexthop -------------------------------------------
    # One VRF-scoped RIB per (tenant, vrf) deployed at this site, seeded on
    # every leaf/border-leaf: BD subnets as directly-connected routes (nexthop
    # = this node's own loopback, ifname = a real BD-facing host port so the
    # interface resolves), plus (border leaves only) a 0.0.0.0/0 default route
    # via the L3Out's CSW peer — matching the ipRouteP static default route
    # l3out.py's _build_node_profile already seeds on the same node.
    all_leaves = site.leaf_nodes() + site.border_leaf_nodes()
    border_leaf_ids = {bl.id for bl in site.border_leaves}
    node_loopback = {n.id: loopback_ip(pod, n.id) for n in all_leaves}

    for tenant in topo.tenants:
        if site.name not in tenant.sites:
            continue
        t = tenant.name
        for vrf in tenant.vrfs:
            vrf_dom = f"{t}:{vrf.name}"
            bd_subnets = [cidr for bd in tenant.bds if bd.vrf == vrf.name for cidr in bd.subnets]

            for leaf in all_leaves:
                node_dn = f"topology/pod-{pod}/node-{leaf.id}/sys"
                lb = node_loopback[leaf.id]

                for cidr in bd_subnets:
                    store.add(_build_route(node_dn, vrf_dom, cidr, lb, "vlan1"))

                if leaf.id in border_leaf_ids:
                    bl = next(bl for bl in site.border_leaves if bl.id == leaf.id)
                    for l3out in tenant.l3outs:
                        if l3out.vrf != vrf.name:
                            continue
                        if _owning_site(l3out, topo) != site.name:
                            continue
                        if leaf.id not in l3out.border_leaves:
                            continue
                        store.add(_build_route(
                            node_dn, vrf_dom, "0.0.0.0/0",
                            bl.csw.peer_ip, "eth1/48",
                        ))

    # ---- arpAdjEp — one per existing (locally-learned) endpoint ---------
    # Reuse the real fvCEp/fvIp MAC+IP data endpoints.py already wrote for
    # this site's store; only front-panel (local) endpoints get an ARP
    # adjacency (a REMOTE/tunnel-learned endpoint has no local ARP entry on
    # this fabric — real ACI resolves those via the overlay, not local ARP).
    for cep in store.by_class("fvCEp"):
        path_dn = cep.attrs.get("fabricPathDn", "")
        m = _FRONT_PANEL_IF_RE.search(path_dn)
        if not m:
            continue
        node_m = re.search(r"/paths-(\d+)/", path_dn)
        if not node_m:
            continue
        node_id = node_m.group(1)
        if int(node_id) not in {n.id for n in all_leaves}:
            continue
        if_name = m.group(1)
        mac = cep.attrs.get("mac", "")

        # fvIp children carry the endpoint's IP(s); dn = f"{cep.dn}/ip-[{ip}]"
        for ip_mo in store.children(cep.dn, {"fvIp"}):
            ip_addr = ip_mo.attrs.get("addr", "")
            if not ip_addr:
                continue
            # tenant/vrf for the dom segment: derive from cep_dn's "tn-{t}" and
            # this site's tenant/BD/VRF config (matches the RIB dom naming above).
            tn_m = re.search(r"uni/tn-([^/]+)/", cep.dn)
            tname = tn_m.group(1) if tn_m else "common"
            tenant = next((tt for tt in topo.tenants if tt.name == tname), None)
            vrf_name = tenant.vrfs[0].name if tenant and tenant.vrfs else "default"
            vrf_dom = f"{tname}:{vrf_name}"

            arp_dn = (
                f"topology/pod-{pod}/node-{node_id}/sys/arp/inst"
                f"/dom-{vrf_dom}/db-ip/adj-[{ip_addr}]"
            )
            store.add(MO(
                "arpAdjEp",
                dn=arp_dn,
                ip=ip_addr,
                mac=mac,
                ifId=if_name,
                physIfId=if_name,
                operSt="up",
            ))
