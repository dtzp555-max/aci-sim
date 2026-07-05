"""Orchestrator — assemble a full per-site MITStore from the topology.

Every sub-builder is a pure function of (topology, site) that only ADDS MOs and
never reads another builder's output, so the order below is for readability only.
"""

from __future__ import annotations

from aci_sim.build import (
    access,
    cabling,
    endpoints,
    fabric,
    health_faults,
    interfaces,
    l3out,
    mgmt,
    overlay,
    routing,
    tenants,
    underlay,
    userprofile,
    zoning,
)
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import Site, Topology

# fabric first (nodes), then wiring/underlay/overlay, then tenant config,
# then L3Out (needs border leaves), endpoints, and finally health/faults.
#
# Batch-1 note: `endpoints` now runs BEFORE `tenants` (moved from after
# `l3out`) so tenants.py's fvRsPathAtt (static binding) can read back the
# real fvCEp/fabricPathDn each EPG's endpoints already got from the store,
# instead of re-deriving port assignment logic independently (which risked
# silently drifting out of sync with endpoints.py). Every other builder
# remains a pure "only ADDS MOs, never reads another builder's output"
# function per the orchestrator's original contract — endpoints.py itself
# still only adds and doesn't read from tenants/access/l3out, so moving it
# earlier is safe (its only cross-module dependency, _HOST_PORTS, comes from
# `interfaces`, which still runs first).
#
# Batch-2 note: `zoning` runs AFTER both `tenants` (needs fvAEPg.pcTag/
# fvCtx.scope, and reuses tenants.py's pctag_for/scope_id_for helpers) and
# `endpoints` (needs fvCEp to know which leaves host each EPG's local
# endpoints) — same read-back pattern as tenants.py's fvRsPathAtt, so zoning
# is placed right after tenants in the list (endpoints already ran earlier).
# `overlay.build_vpn_routes` (bgpVpnRoute/bgpPath) has the same fvRsBd/fvCEp
# read-back dependency, so it also runs post-tenants/endpoints — as a SECOND
# call into the `overlay` module, alongside (not instead of) its early
# `overlay.build` call which only needs fabric/topology data and stays in
# its original early slot for BGP peer setup.
#
# OOB-gateway PR: `mgmt` builds the mgmt-tenant OOB scaffolding
# (fvTenant(mgmt)/mgmtMgmtP/mgmtOoB/mgmtRsOoBStNode) that carries
# site.oob_gateway on a real MO. It only needs topology data (site.all_nodes(),
# oob_ip(), site.controller_oob_ips()) — no read-back from another builder's
# store output — so, like every other builder here, it is placed for
# readability only; it is grouped right after `fabric` since both build
# node-identity/OOB data.
_BUILDERS = [
    fabric,
    mgmt,
    cabling,
    underlay,
    overlay,
    interfaces,
    endpoints,
    tenants,
    zoning,
    access,
    l3out,
    routing,
    health_faults,
    userprofile,
]


def build_site(topo: Topology, site: Site) -> MITStore:
    """Build and return a fully-populated MITStore for one site."""
    store = MITStore()
    for mod in _BUILDERS:
        mod.build(topo, site, store)
    overlay.build_vpn_routes(topo, site, store)
    return store


def build_all(topo: Topology) -> dict[str, MITStore]:
    """Build every site → ``{site_name: MITStore}``."""
    return {site.name: build_site(topo, site) for site in topo.sites}
