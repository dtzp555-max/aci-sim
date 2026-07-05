"""build/endpoints.py — fvCEp, fvIp, epmMacEp, epmIpEp, fvIfConn.

Port allocation avoids the APIC-controller-reserved port range: fabric.py wires
each APIC-<cid> (cid 1..site.controllers) to eth1/<cid> on the first two leaves
(site.leaf_nodes()[:2]) — the same leaves endpoints are placed on. Endpoint
host ports therefore start right AFTER that reserved range so no port ever
hosts both a controller lldpAdjEp and an endpoint fabricPathDn (see
interfaces.py's _HOST_PORTS=8 host-port inventory for the ceiling).

Multi-site (stretched-tenant) endpoint semantics (see docs/DESIGN.md):
A stretched tenant (tenant.stretch=true, sites: [A, B]) is built independently
per site by this same build() function, once per site's MITStore. Real ACI
never shows the identical endpoint as locally-learned-on-a-front-panel-port at
BOTH sites simultaneously: exactly one site has it physically attached (HOME,
lcC contains "learned", fabricPathDn = a real front-panel pathep); the other
site only knows about it via the multi-site overlay/ISN tunnel (REMOTE,
lcC="learned,vxlan", fabricPathDn = a tunnelIf path toward the home site's
spine-proxy TEP). The HOME site is chosen deterministically per endpoint
(alternating over tenant.sites by the endpoint's position in the per-tenant
sequence) so both sites' independent builds agree on which one is home
without needing to share state.
"""
from __future__ import annotations

import ipaddress
import zlib

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import BD, Site, Tenant, Topology
from aci_sim.build.fabric import loopback_ip
from aci_sim.build.interfaces import _HOST_PORTS


def _is_deployed(tenant: Tenant, site: Site) -> bool:
    return site.name in tenant.sites


def _vnid(name: str, base: int) -> int:
    return base + (zlib.crc32(name.encode()) & 0xFFFF)


def _mac(counter: int) -> str:
    aa = (counter >> 16) & 0xFF
    bb = (counter >> 8) & 0xFF
    cc = counter & 0xFF
    return f"00:50:56:{aa:02X}:{bb:02X}:{cc:02X}"


def _host_ip(bd: BD, index: int) -> str:
    """Return host at position 10+index within the first BD subnet."""
    if not bd.subnets:
        return "0.0.0.0"
    net = ipaddress.ip_network(bd.subnets[0], strict=False)
    hosts = list(net.hosts())
    pos = 10 + index
    return str(hosts[pos % len(hosts)])


def _emit_epm_mos(
    store: MITStore,
    mac: str,
    ip: str,
    epg_vlan: int,
    pod: int,
    leaf_id: int,
    port_ifid: str,
    epg_ref: str,
    vrf_vnid: int,
    bd_vnid: int,
    flags: str = "local",
) -> None:
    """Emit epmMacEp, epmIpEp, and (for real front-panel ports) fvIfConn."""
    ctx_bd = f"ctx-[vxlan-{vrf_vnid}]/bd-[vxlan-{bd_vnid}]"
    node_sys = f"topology/pod-{pod}/node-{leaf_id}/sys"
    store.add(MO(
        "epmMacEp",
        dn=f"{node_sys}/{ctx_bd}/db-ep/mac-{mac}",
        addr=mac,
        ifId=port_ifid,
        flags=flags,
        createTs="2026-01-01T00:00:00.000+00:00",
        encap=f"vlan-{epg_vlan}",
    ))
    store.add(MO(
        "epmIpEp",
        dn=f"{node_sys}/{ctx_bd}/db-ep/ip-[{ip}]",
        addr=ip,
        ifId=port_ifid,
        flags=flags,
        createTs="2026-01-01T00:00:00.000+00:00",
    ))
    if flags == "local":
        store.add(MO(
            "fvIfConn",
            dn=(
                f"{node_sys}/phys-[{port_ifid}]"
                f"/epgconn-[{epg_ref}]"
            ),
            encap=f"vlan-{epg_vlan}",
        ))


def _emit_endpoint(
    store: MITStore,
    cep_dn: str,
    mac: str,
    ip: str,
    epg_vlan: int,
    pod: int,
    leaf_id: int,
    port: int,
    epg_ref: str,
    vrf_vnid: int,
    bd_vnid: int,
) -> None:
    """Emit fvCEp+fvIp+epmMacEp+epmIpEp+fvIfConn for one LOCAL (home-site)
    endpoint — physically attached to a real front-panel leaf port."""
    cep_mo = MO(
        "fvCEp",
        dn=cep_dn,
        mac=mac,
        ip="0.0.0.0",
        encap=f"vlan-{epg_vlan}",
        lcC="learned",
        fabricPathDn=(
            f"topology/pod-{pod}/paths-{leaf_id}/pathep-[eth1/{port}]"
        ),
    )
    cep_mo.add_child(MO(
        "fvIp",
        dn=f"{cep_dn}/ip-[{ip}]",
        addr=ip,
    ))
    store.add(cep_mo)
    _emit_epm_mos(
        store, mac, ip, epg_vlan, pod, leaf_id, f"eth1/{port}",
        epg_ref, vrf_vnid, bd_vnid, flags="local",
    )


def _ensure_tunnel_if(
    store: MITStore,
    pod: int,
    spine_id: int,
    remote_pod: int,
    remote_spine_id: int,
    seen: set[tuple[int, int]],
) -> str:
    """Ensure a tunnelIf MO exists on *spine_id* toward the remote site's
    spine-proxy TEP (represented by remote_spine_id's loopback); return its
    interface id (e.g. "tunnel401"), used to build the fvCEp fabricPathDn.

    Real ACI multi-site: remote-learned endpoints are reachable via a VXLAN
    tunnel from the local spine-proxy to the remote site's spine-proxy anycast
    TEP. We model one tunnelIf per (local spine, remote spine) pair, numbered
    deterministically by the remote spine's id so the DN is stable and
    idempotent across multiple endpoints sharing the same tunnel.
    """
    key = (spine_id, remote_spine_id)
    tunnel_id = f"tunnel{remote_spine_id}"
    if key not in seen:
        seen.add(key)
        dest_tep = loopback_ip(remote_pod, remote_spine_id)
        store.add(MO(
            "tunnelIf",
            dn=f"topology/pod-{pod}/node-{spine_id}/sys/tunnel-[{tunnel_id}]",
            id=tunnel_id,
            dest=dest_tep,
            src=loopback_ip(pod, spine_id),
            operSt="up",
            type="logical-l3-multicast",
            layer="l3-multicast",
        ))
    return tunnel_id


def _emit_remote_endpoint(
    store: MITStore,
    cep_dn: str,
    mac: str,
    ip: str,
    epg_vlan: int,
    pod: int,
    tunnel_spine_id: int,
    tunnel_id: str,
    epg_ref: str,
    vrf_vnid: int,
    bd_vnid: int,
) -> None:
    """Emit fvCEp+fvIp+epmMacEp+epmIpEp for one REMOTE (peer-site) endpoint —
    learned via the multi-site overlay tunnel, not a real front-panel port."""
    fabric_path_dn = (
        f"topology/pod-{pod}/paths-{tunnel_spine_id}/pathep-[{tunnel_id}]"
    )
    cep_mo = MO(
        "fvCEp",
        dn=cep_dn,
        mac=mac,
        ip="0.0.0.0",
        encap=f"vlan-{epg_vlan}",
        lcC="learned,vxlan",
        fabricPathDn=fabric_path_dn,
    )
    cep_mo.add_child(MO(
        "fvIp",
        dn=f"{cep_dn}/ip-[{ip}]",
        addr=ip,
    ))
    store.add(cep_mo)
    _emit_epm_mos(
        store, mac, ip, epg_vlan, pod, tunnel_spine_id, tunnel_id,
        epg_ref, vrf_vnid, bd_vnid, flags="learned,vxlan",
    )


def build(topo: Topology, site: Site, store: MITStore) -> None:
    """Emit endpoint + EPM MOs for every EPG deployed at *site*.

    MAC/VLAN sequences are per-store (per site/APIC) and assume the intended
    one-MITStore-per-APIC model; merging two site stores would collide DNs/MACs.
    """
    pod = site.pod
    leaves = site.leaf_nodes()
    per_epg = topo.endpoints.per_epg
    first_vlan = topo.access.vlan_pools[0].from_vlan if topo.access.vlan_pools else 100

    # APIC-{cid} controllers (cid 1..site.controllers) are wired to eth1/{cid}
    # on the first two leaves (fabric.py: apic_leaves = site.leaf_nodes()[:2]).
    # Reserve that port range on those leaves; endpoints start right after it.
    apic_reserved_leaf_ids = {n.id for n in site.leaf_nodes()[:2]}
    apic_port_start = 1 + site.controllers
    free_host_ports = max(_HOST_PORTS - site.controllers, 1)

    global_epg_index = 0
    ep_counter = 0
    tunnels_seen: set[tuple[int, int]] = set()

    for tenant in topo.tenants:
        if not _is_deployed(tenant, site):
            continue
        t = tenant.name
        bd_by_name = {bd.name: bd for bd in tenant.bds}

        # A stretched tenant's endpoints are HOME on exactly one of its sites
        # (see module docstring). Non-stretched (site-local) tenants have no
        # remote counterpart — every endpoint is simply local, as before.
        is_stretched = tenant.stretch and len(tenant.sites) > 1
        remote_site = None
        if is_stretched:
            try:
                remote_site = topo.other_site(site.name) if site.name in tenant.sites else None
            except KeyError:
                remote_site = None

        for ap in tenant.aps:
            for epg in ap.epgs:
                bd = bd_by_name.get(epg.bd)
                epg_vlan = first_vlan + global_epg_index
                global_epg_index += 1

                if bd is None or not bd.subnets:
                    continue

                vrf_vnid = _vnid(f"{t}:{bd.vrf}", 2900000)
                bd_vnid = _vnid(f"{t}:{bd.name}", 15000000)
                epg_ref = f"uni/tn-{t}/ap-{ap.name}/epg-{epg.name}"

                for i in range(per_epg):
                    # Deterministic HOME-site assignment: alternates over
                    # tenant.sites by this endpoint's position in the
                    # per-tenant sequence, so LAB1's and LAB2's independent
                    # build() calls agree on which site is home without
                    # sharing state.
                    home_site_name = (
                        tenant.sites[ep_counter % len(tenant.sites)]
                        if is_stretched else site.name
                    )
                    mac = _mac(ep_counter)
                    ep_counter += 1
                    ip = _host_ip(bd, i)
                    cep_dn = f"uni/tn-{t}/ap-{ap.name}/epg-{epg.name}/cep-{mac}"

                    if not is_stretched or home_site_name == site.name:
                        leaf = leaves[i % len(leaves)]
                        leaf_id = leaf.id
                        if leaf_id in apic_reserved_leaf_ids:
                            # Skip past the APIC-reserved eth1/1..eth1/{controllers}
                            # range so no endpoint double-books a controller port.
                            port = apic_port_start + (i % free_host_ports)
                        else:
                            port = (i % _HOST_PORTS) + 1
                        _emit_endpoint(
                            store, cep_dn, mac, ip, epg_vlan, pod,
                            leaf_id, port, epg_ref, vrf_vnid, bd_vnid,
                        )
                    elif remote_site is not None:
                        # This site is the PEER for this endpoint: learned via
                        # the multi-site overlay tunnel, not a front-panel port.
                        local_spines = site.spine_nodes()
                        home_spines = remote_site.spine_nodes()
                        if not local_spines or not home_spines:
                            continue
                        tunnel_spine = local_spines[i % len(local_spines)]
                        home_spine = home_spines[i % len(home_spines)]
                        tunnel_id = _ensure_tunnel_if(
                            store, pod, tunnel_spine.id,
                            remote_site.pod, home_spine.id, tunnels_seen,
                        )
                        _emit_remote_endpoint(
                            store, cep_dn, mac, ip, epg_vlan, pod,
                            tunnel_spine.id, tunnel_id, epg_ref, vrf_vnid, bd_vnid,
                        )
