"""build/builtin_tenants.py — the `common` and `infra` tenants every APIC ships.

A real APIC boots with three built-in tenants; this sim only built `mgmt`
(build/mgmt.py, which needs the tenant as a parent for the OOB scaffolding), so
`GET /api/class/fvTenant.json` returned two fewer objects than any real fabric
and `uni/tn-common` — the tenant shared policies are conventionally defined in,
and which var files legitimately reference — did not resolve at all.

    uni/tn-common     fvTenant  (name="common")   shared contracts/filters/L3Outs
    uni/tn-infra      fvTenant  (name="infra")    fabric infrastructure/overlay
    uni/tn-mgmt       fvTenant  (name="mgmt")     built by build/mgmt.py

Scope is deliberately minimal, matching build/mgmt.py's stance: the tenant MOs
themselves, not the policy trees real APIC pre-populates underneath them. The
sim's fidelity claim is about the objects automation touches, and automation
addresses these two by DN — it does not enumerate their built-in children.
`mgmt` is intentionally NOT emitted here so build/mgmt.py stays the single
owner of that tenant and its OOB tree.
"""
from __future__ import annotations

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import Site, Topology

#: name -> descr, mirroring what a factory APIC reports.
BUILTIN_TENANTS = {
    "common": "",
    "infra": "",
}


def build(topo: Topology, site: Site, store: MITStore) -> None:
    """Emit the built-in `common` and `infra` tenant MOs for *site*."""
    for name, descr in BUILTIN_TENANTS.items():
        store.upsert(MO("fvTenant", dn=f"uni/tn-{name}", name=name, descr=descr))
