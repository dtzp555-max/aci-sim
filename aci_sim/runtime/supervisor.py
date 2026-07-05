"""Entry point: python -m aci_sim.runtime.supervisor

Loads topology → build_all + build_ndo_model → two ApicSiteStates + NdoState
→ make_apic_app×2 + make_ndo_app → three uvicorn servers with TLS, gathered
with asyncio.
"""

from __future__ import annotations

import asyncio
import copy

import uvicorn

from aci_sim.build.orchestrator import build_all
from aci_sim.ndo.app import make_ndo_app
from aci_sim.ndo.model import build_ndo_model
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.runtime.config import (
    APIC_A_PORT,
    APIC_B_PORT,
    CERT_FILE,
    KEY_FILE,
    NDO_PORT,
    SANDBOX,
    SANDBOX_PORT,
    SIM_BIND,
    TOPOLOGY_PATH,
)
from aci_sim.topology.loader import load_topology


def apic_port_for_site(site_index: int) -> int:
    """Effective APIC port for a site, by index (#25).

    - site 0 → APIC_A_PORT (the primary/first fabric's port env var)
    - site 1 → APIC_B_PORT (so APIC_B_PORT, defined in config.py, is actually
      honored instead of being silently shadowed by an APIC_A_PORT+i formula)
    - site >1 → APIC_A_PORT + site_index, a documented fallback for topologies
      with more than two sites (there is no APIC_C_PORT etc. env var; this
      keeps ports contiguous and collision-free for the common 2-site case
      while still supporting N sites).
    """
    if site_index == 0:
        return APIC_A_PORT
    if site_index == 1:
        return APIC_B_PORT
    return APIC_A_PORT + site_index


async def run_servers() -> None:
    topo = load_topology(TOPOLOGY_PATH)
    stores = build_all(topo)
    ndo_state = build_ndo_model(topo, sandbox=SANDBOX)

    def _cfg(app, host, port):
        return uvicorn.Config(
            app, host=host, port=port,
            ssl_certfile=CERT_FILE, ssl_keyfile=KEY_FILE, log_level="info",
        )

    # One APIC app per site (supports single-fabric or N sites).
    #   default  → SIM_BIND (default 127.0.0.1, see #23) on distinct high ports (8443, 8444, …)
    #   sandbox  → each site's own mgmt_ip on :443 (real-gear addressing, no ports)
    configs = []
    # site.id -> ApicSiteState, so the NDO app's deploy handler can mirror a
    # deployed multi-site template's VRFs/BDs/ANPs/EPGs straight into the
    # target sites' APIC MITStores (see ndo/deploy_mirror.py) — closing the
    # confirmed SIM GAP where POST /mso/api/v1/task was a pure ack no-op and
    # multi-site EPGs never appeared on the APIC side of the sim.
    apic_states: dict = {}
    for i, site in enumerate(topo.sites):
        store = stores[site.name]
        state = ApicSiteState(
            name=site.name, site=site, topo=topo,
            store=store, baseline=copy.deepcopy(store),
        )
        apic_states[site.id] = state
        if SANDBOX:
            host, port = (site.mgmt_ip or "127.0.0.1"), SANDBOX_PORT
        else:
            host, port = SIM_BIND, apic_port_for_site(i)
        configs.append(_cfg(make_apic_app(state), host, port))
        print(f"[sim] APIC {site.name} (asn {site.asn}) → https://{host if SANDBOX else SIM_BIND}:{port}")

    if SANDBOX:
        ndo_host, ndo_port = (topo.fabric.ndo_mgmt_ip or "127.0.0.1"), SANDBOX_PORT
    else:
        # NDO on NDO_PORT, shifted past the APIC ports if they would collide (>=3 sites).
        ndo_host = SIM_BIND
        last_apic = apic_port_for_site(len(topo.sites) - 1)
        ndo_port = NDO_PORT if NDO_PORT > last_apic else last_apic + 1
    configs.append(_cfg(make_ndo_app(ndo_state, apic_states), ndo_host, ndo_port))
    print(f"[sim] NDO → https://{topo.fabric.ndo_mgmt_ip if SANDBOX else SIM_BIND}:{ndo_port}")

    servers = [uvicorn.Server(cfg) for cfg in configs]
    await asyncio.gather(*[s.serve() for s in servers])


def main() -> None:
    asyncio.run(run_servers())


if __name__ == "__main__":
    main()
