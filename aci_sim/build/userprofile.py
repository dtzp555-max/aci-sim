"""build/userprofile.py — aaaUserEp login-probe MO (uni/userprofile-<user>).

CONTRACT.md §1: during/after login autoACI probes
  GET /api/mo/uni/userprofile-<user>.json?query-target=subtree&target-subtree-class=aaaUserDomain
This must not 404 and must return >=1 aaaUserDomain row.

Real APIC exposes a per-user aaaUserEp at uni/userprofile-<user> with an
aaaUserDomain child (name="all" is the default/full-access domain every local
user gets). The simulator only ever authenticates as "admin" (see
rest_aci/auth.py, which defaults aaaLogin's username to "admin"), so the
simplest faithful variant is a single static admin profile rather than
dynamically creating one per logged-in user — this is the same set of MOs a
real APIC would already have provisioned for the built-in admin account, no
matter who authenticates.

Site-independent: call once per topology (site arg accepted for API
uniformity but not used in the selection logic), same convention as
build/access.py.
"""
from __future__ import annotations

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import Site, Topology

_ADMIN_USER = "admin"


def build(topo: Topology, site: Site, store: MITStore) -> None:
    """Emit uni/userprofile-admin (aaaUserEp) + its aaaUserDomain child."""
    profile_dn = f"uni/userprofile-{_ADMIN_USER}"
    profile_mo = MO(
        "aaaUserEp",
        dn=profile_dn,
        name=_ADMIN_USER,
    )
    profile_mo.add_child(MO(
        "aaaUserDomain",
        dn=f"{profile_dn}/userdomain-all",
        name="all",
    ))
    store.add(profile_mo)
