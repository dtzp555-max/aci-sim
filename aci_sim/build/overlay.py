"""build/overlay.py — intra-site BGP-EVPN + ISN inter-site eBGP peers.

Intra-site:  each leaf (and border-leaf) has bgpPeer/bgpPeerEntry/bgpPeerAfEntry
             to EVERY spine loopback (spines are route-reflectors).

ISN (critical):  on EACH spine's sys/bgp/inst, add bgpPeer for every remote-site
             spine loopback with addr=<remote_loopback>/32 and asn=<remote_site.asn>.

ISN detection in autoACI topology.py (lines 493-511):
    addr_raw = attrs.get("addr", "")     # e.g. "10.1.401.1/32"
    peer_asn = attrs.get("asn", "")      # e.g. "65002" (remote site ASN)
    if "/32" in addr_raw and peer_asn and peer_asn != spine_local_asn:
        # → synthesize ISN cloud node + spine→ISN links

Domain used for all EVPN peers: dom-overlay-1 (no ":" → skipped by L3Out BGP table).

IMPORTANT: store.subtree() traverses via the _children index.  Every intermediate
DN in the path must have its own MO registered so the parent→child link exists.
We therefore add a bgpDom MO at each "…/inst/dom-overlay-1" before adding peers.

Batch-2 additions (CONTRACT.md §6 "Overlay/BGP"): bgpVpnRoute + bgpPath — the
EVPN route family autoACI's fabric_bgp_evpn.py "vpn_routes" view two-pass
parses (verified read-only against that plugin: pass 1 collects bgpVpnRoute
by dn under a node-scoped subtree query; pass 2 matches bgpPath children back
to their parent route via `path_dn.rsplit("/path-", 1)[0]`, so a bgpPath's RN
MUST start with "path-" directly under its route's own dn — no extra bracket
segments in between). DN family:
  bgpVpnRoute   {spine_inst_dn}/dom-overlay-1/af-{afi}/rt-[{prefix}]
  bgpPath       {route_dn}/path-{nexthop-slug}
Seeded per-spine (the EVPN route-reflector role, per this module's own
`evpn_rr` convention) for each BD subnet deployed at this site, with one path
per leaf/border-leaf that actually hosts a locally-learned endpoint for that
BD (read back from endpoints.py's fvCEp — same read-back technique
tenants.py/zoning.py already use) — a spine's VPN route table only carries
paths toward leaves it has real reachability for, not a leaf with zero
endpoints in that BD. Runs regardless of ISN enablement (real ACI's
per-VRF EVPN route table exists in a single-site fabric too, unlike the
ISN-specific inter-site peer section below).
"""
from __future__ import annotations

import re

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import Site, Topology
from aci_sim.build.fabric import loopback_ip

_DOM = "overlay-1"
_CEP_NODE_RE = re.compile(r"/paths-(\d+)/pathep-\[eth\d+/\d+\]$")


def _ensure_dom(inst_dn: str, store: MITStore, seen: set[str]) -> str:
    """Add bgpDom at inst_dn/dom-{_DOM} once; return the dom DN."""
    dom_dn = f"{inst_dn}/dom-{_DOM}"
    if dom_dn not in seen:
        store.add(MO("bgpDom", dn=dom_dn, name=_DOM))
        seen.add(dom_dn)
    return dom_dn


def _add_peer(
    store: MITStore,
    dom_dn: str,
    peer_addr: str,
    peer_asn: str,
    peer_type: str,
    rtr_id: str,
    accepted_paths: str = "10",
) -> None:
    """Emit bgpPeer + bgpPeerEntry + bgpPeerAfEntry for one BGP session."""
    peer_dn = f"{dom_dn}/peer-[{peer_addr}]"
    entry_dn = f"{peer_dn}/ent-[{peer_addr}]"
    af_dn = f"{entry_dn}/af-[l2vpn-evpn]"

    store.add(MO(
        "bgpPeer",
        dn=peer_dn,
        addr=peer_addr,
        asn=peer_asn,
        type=peer_type,
        peerRole="internal",
    ))
    store.add(MO(
        "bgpPeerEntry",
        dn=entry_dn,
        addr=peer_addr,
        operSt="established",
        rtrId=rtr_id,
        lastFlapTs="00:00:00:00.000",
        connEst="1",
        connDrop="0",
        type=peer_type,
    ))
    store.add(MO(
        "bgpPeerAfEntry",
        dn=af_dn,
        type="l2vpn-evpn",
        afId="l2vpn-evpn",
        acceptedPaths=accepted_paths,
        pfxSent="5",
        tblVer="1",
    ))


def _route_reflectors(topo: Topology, site: Site) -> list:
    """Resolve `topo.fabric.evpn_rr` (#27) to the actual RR node list.

    Only "spine" is a meaningful topology for this simulator today — Site has
    no other role that could sanely act as a BGP-EVPN route-reflector (leaves
    and border-leaves are always RR clients in a Clos fabric). Honoring the
    field means failing fast on an unsupported value instead of silently
    building spine-RR topology regardless of what the YAML says.
    """
    if topo.fabric.evpn_rr == "spine":
        return site.spine_nodes()
    raise ValueError(
        f"fabric.evpn_rr={topo.fabric.evpn_rr!r} is not supported — only "
        f"'spine' is implemented as a BGP-EVPN route-reflector role."
    )


def build(topo: Topology, site: Site, store: MITStore) -> None:
    """Emit intra-site EVPN peers + ISN inter-site peers."""
    pod = site.pod
    spines = _route_reflectors(topo, site)
    clients = site.leaf_nodes() + site.border_leaf_nodes()
    seen_doms: set[str] = set()

    # -------------------------------------------------------------------------
    # Intra-site: each client leaf → each spine loopback
    # -------------------------------------------------------------------------
    for leaf in clients:
        leaf_inst_dn = f"topology/pod-{pod}/node-{leaf.id}/sys/bgp/inst"
        leaf_lb = loopback_ip(pod, leaf.id)
        dom_dn = _ensure_dom(leaf_inst_dn, store, seen_doms)
        for spine in spines:
            spine_lb = loopback_ip(pod, spine.id)
            _add_peer(
                store, dom_dn,
                peer_addr=spine_lb,
                peer_asn=str(site.asn),
                peer_type="internal",
                rtr_id=leaf_lb,
            )

    # Spines also hold the reverse view of each leaf peer (RR perspective)
    for spine in spines:
        spine_inst_dn = f"topology/pod-{pod}/node-{spine.id}/sys/bgp/inst"
        spine_lb = loopback_ip(pod, spine.id)
        dom_dn = _ensure_dom(spine_inst_dn, store, seen_doms)
        for leaf in clients:
            leaf_lb = loopback_ip(pod, leaf.id)
            _add_peer(
                store, dom_dn,
                peer_addr=leaf_lb,
                peer_asn=str(site.asn),
                peer_type="internal",
                rtr_id=spine_lb,
            )

    # -------------------------------------------------------------------------
    # ISN: each local spine → each remote-site spine loopback (/32 + remote ASN)
    # -------------------------------------------------------------------------
    if not topo.isn.enabled:
        return
    try:
        remote_site = topo.other_site(site.name)
    except KeyError:
        return  # single-site topology → nothing to do

    # #27 / Tier-2 (PR-19): isn.peer_mesh is otherwise read by no builder
    # (topology/schema.py's validator only checks it == "full" to gate the
    # ">=2 sites" rule). Every local spine peers every remote spine below IS
    # the "full" mesh. `partial` mesh (a subset of spine pairs peering,
    # e.g. for large N-site fabrics wanting to limit ISN fan-out) has NO
    # sensible default partition without additional per-site config this
    # schema doesn't carry (which spines pair with which — that's not
    # inferable from spine count alone), so PR-19 keeps this a documented
    # validate-and-reject rather than inventing a partition scheme no
    # topology.yaml author asked for: fail fast rather than silently
    # building full-mesh (or an arbitrary subset) for an unimplemented value.
    if topo.isn.peer_mesh != "full":
        raise ValueError(
            f"isn.peer_mesh={topo.isn.peer_mesh!r} is not supported — only "
            f"'full' (every local spine peers every remote spine) is implemented. "
            f"'partial' is intentionally rejected (see build/overlay.py module "
            f"docs) rather than silently built as full-mesh or an undefined subset."
        )

    remote_asn = str(remote_site.asn)
    remote_pod = remote_site.pod

    for spine in spines:
        spine_inst_dn = f"topology/pod-{pod}/node-{spine.id}/sys/bgp/inst"
        spine_lb = loopback_ip(pod, spine.id)
        # dom_dn is already ensured above (intra-site peers were added first)
        dom_dn = _ensure_dom(spine_inst_dn, store, seen_doms)

        for remote_spine in remote_site.spine_nodes():
            remote_lb = loopback_ip(remote_pod, remote_spine.id)
            # ISN peer addr MUST end with /32 — topology.py checks:
            #   if "/32" in addr_raw and peer_asn != spine_local_asn → ISN cloud
            isn_addr = f"{remote_lb}/32"
            _add_peer(
                store, dom_dn,
                peer_addr=isn_addr,
                peer_asn=remote_asn,
                peer_type="inter-site",
                rtr_id=spine_lb,
            )


def _leaves_hosting_bd(store: MITStore, tenant_name: str, bd_name: str) -> set[int]:
    """Return leaf node ids with >=1 locally-learned endpoint in *bd_name*,
    scanning fvCEp dns for any EPG bound to that BD (fvRsBd.tnFvBDName) —
    same read-back technique tenants.py/zoning.py already use against
    endpoints.py's store output."""
    epg_dns: set[str] = set()
    tn_prefix = f"uni/tn-{tenant_name}/"
    for rsbd in store.by_class("fvRsBd"):
        if rsbd.attrs.get("tnFvBDName") != bd_name:
            continue
        if not rsbd.dn.startswith(tn_prefix):
            continue
        epg_dns.add(rsbd.dn.rsplit("/rsbd", 1)[0])

    leaves: set[int] = set()
    for epg_dn in epg_dns:
        prefix = f"{epg_dn}/cep-"
        for cep in store.by_class("fvCEp"):
            if not cep.dn.startswith(prefix):
                continue
            m = _CEP_NODE_RE.search(cep.attrs.get("fabricPathDn", ""))
            if m:
                leaves.add(int(m.group(1)))
    return leaves


def build_vpn_routes(topo: Topology, site: Site, store: MITStore) -> None:
    """Emit bgpVpnRoute + bgpPath (batch-2) — the per-spine EVPN VPN route
    table for this site's deployed BD subnets.

    Runs AFTER `tenants` and `endpoints` in the orchestrator order (unlike
    `build()` above, which only needs fabric/topology data and stays early)
    because it reads back fvRsBd/fvCEp to find which leaves actually host
    each BD's endpoints — see module docstring for the two-pass DN shape
    fabric_bgp_evpn.py's "vpn_routes" view requires.
    """
    pod = site.pod
    spines = _route_reflectors(topo, site)

    for tenant in topo.tenants:
        if site.name not in tenant.sites:
            continue
        for bd in tenant.bds:
            if not bd.subnets:
                continue
            target_leaves = _leaves_hosting_bd(store, tenant.name, bd.name)
            if not target_leaves:
                continue
            for spine in spines:
                spine_inst_dn = f"topology/pod-{pod}/node-{spine.id}/sys/bgp/inst"
                dom_dn = f"{spine_inst_dn}/dom-{_DOM}"
                for cidr in bd.subnets:
                    afi = "vpnv4"  # IPv4-unicast EVPN route family (real ACI: af-vpnv4/af-vpnv6)
                    route_dn = f"{dom_dn}/af-{afi}/rt-[{cidr}]"
                    route_mo = MO(
                        "bgpVpnRoute",
                        dn=route_dn,
                        pfx=cidr,
                        rd=f"{site.asn}:{tenant.name}",
                    )
                    for leaf_id in sorted(target_leaves):
                        leaf_lb = loopback_ip(pod, leaf_id)
                        route_mo.add_child(MO(
                            "bgpPath",
                            dn=f"{route_dn}/path-{leaf_id}",
                            nh=leaf_lb,
                            asPath="",
                            localPref="100",
                            metric="0",
                            origin="igp",
                            type="internal",
                        ))
                    store.add(route_mo)
