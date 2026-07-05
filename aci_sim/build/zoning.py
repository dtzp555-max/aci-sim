"""build/zoning.py — actrlRule (batch-2, CONTRACT.md §6 "Contracts").

  actrlRule   topology/pod-{p}/node-{n}/sys/actrl/scope-[{scopeId}]/rule-[{ruleId}]

DN/attribute shapes verified read-only against autoACI's zoning_rules.py,
contract_debug.py, and ep_connectivity.py (all three read actrlRule; see
docs/DESIGN.md "Batch-2" section for the specific attrs each parses):
  - zoning_rules.py: node-scoped subtree query under `.../sys/actrl`
    (target-subtree-class=actrlRule), reads scopeId/sPcTag/dPcTag/fltId/
    action/direction/prio/ctrctName/operSt/dn. Also cross-references fvEpP's
    (scopeId, pcTag) -> friendly-name map (tenants.py already seeds this).
  - contract_debug.py: same node-scoped subtree query, filters rows whose
    ctrctName contains one of the contract names on the debugged path.
  - ep_connectivity.py: FABRIC-WIDE class query with an
    `or(eq(actrlRule.sPcTag,...),eq(actrlRule.dPcTag,...))` filter — i.e.
    actrlRule must also be reachable via a plain GET /api/class/actrlRule.json
    (not only via node-scoped subtree), so this module both nests the rule
    under its owning node's /sys/actrl tree (for the subtree query shape) AND
    the generic class-query index (store.add already registers by class
    regardless of nesting, so both access patterns are satisfied by one MO).

Rule derivation: one permit rule per (contract subject filter, provider EPG,
consumer EPG) triple, emitted on every leaf where BOTH the provider and
consumer EPG have at least one locally-learned endpoint (a real leaf only
compiles zoning rules for EPGs it actually has traffic for) — read back from
the fvCEp data endpoints.py already wrote (same read-back pattern
tenants.py's fvRsPathAtt already established; zoning.py runs after both
tenants and endpoints in the orchestrator, so both stores are populated).
sPcTag/dPcTag/scopeId reuse the exact pctag_for/scope_id_for scheme
tenants.py's fvAEPg/fvEpP/fvCtx already assign, so a zoning-rule consumer's
pcTag/scopeId cross-reference against fvEpP always resolves.

Structural-container fix (same class of gap access.py's batch-1 infraInfra
fix addressed): `.../sys/actrl` and `.../sys/actrl/scope-[{scopeId}]` are
each seeded as a real MO (classes `actrlRuleCont`/`actrlScope` — placeholder
names; no consumer queries them BY class, only actrlRule descendants
underneath, so any believable class name is faithful here) — without a real
MO at the exact `.../sys/actrl` DN, GET /api/mo/{node}/sys/actrl.json (the
precise subtree-query shape all three consumers use) 404s, because the
store's ancestor-walk only links intermediate DNs structurally in the
children index and never materializes a real MO for them on its own (see
mit/store.py's MITStore._register docstring).
"""
from __future__ import annotations

import re

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import Site, Tenant, Topology
from aci_sim.build.tenants import pctag_for, scope_id_for

_CEP_NODE_RE = re.compile(r"/paths-(\d+)/pathep-\[eth\d+/\d+\]$")


def _is_deployed(tenant: Tenant, site: Site) -> bool:
    return site.name in tenant.sites


def _leaves_with_local_endpoints(store: MITStore, epg_dn: str) -> set[int]:
    """Return the set of leaf node ids that have >=1 locally-learned (real
    front-panel) endpoint for *epg_dn*, read back from endpoints.py's fvCEp
    data (same technique tenants.py's _find_static_binding_path uses)."""
    prefix = f"{epg_dn}/cep-"
    leaves: set[int] = set()
    for cep in store.by_class("fvCEp"):
        if not cep.dn.startswith(prefix):
            continue
        path_dn = cep.attrs.get("fabricPathDn", "")
        m = _CEP_NODE_RE.search(path_dn)
        if m:
            leaves.add(int(m.group(1)))
    return leaves


def build(topo: Topology, site: Site, store: MITStore) -> None:
    """Emit actrlRule MOs derived from existing contract/filter/EPG data."""
    pod = site.pod
    # Real MOs for the ".../sys/actrl" and ".../sys/actrl/scope-[...]"
    # structural containers — without these, GET /api/mo/{node}/sys/actrl.json
    # (the exact DN zoning_rules.py/contract_debug.py/ep_connectivity.py query
    # with subtree=True) 404s: the store's ancestor-walk only links these DNs
    # structurally in the children index, it does not materialize a real MO
    # for them (see mit/store.py's _register docstring) — same class of gap
    # access.py's batch-1 infraInfra fix addressed for uni/infra.
    seen_actrl_dn: set[str] = set()
    seen_scope_dn: set[str] = set()

    for tenant in topo.tenants:
        if not _is_deployed(tenant, site):
            continue
        t = tenant.name

        # epg_dn -> EPG (only EPGs actually deployed under this tenant's APs)
        epg_by_name = {
            epg.name: (ap.name, epg)
            for ap in tenant.aps
            for epg in ap.epgs
        }

        for contract in tenant.contracts:
            providers = [
                (ap_name, epg) for name, (ap_name, epg) in epg_by_name.items()
                if contract.name in epg.contracts.provide
            ]
            consumers = [
                (ap_name, epg) for name, (ap_name, epg) in epg_by_name.items()
                if contract.name in epg.contracts.consume
            ]
            if not providers or not consumers:
                continue

            for prov_ap, prov_epg in providers:
                prov_dn = f"uni/tn-{t}/ap-{prov_ap}/epg-{prov_epg.name}"
                prov_vrf = None
                for bd in tenant.bds:
                    if bd.name == prov_epg.bd:
                        prov_vrf = bd.vrf
                if prov_vrf is None:
                    continue
                prov_tag = pctag_for(prov_dn)
                scope_id = scope_id_for(t, prov_vrf)
                prov_leaves = _leaves_with_local_endpoints(store, prov_dn)

                for cons_ap, cons_epg in consumers:
                    cons_dn = f"uni/tn-{t}/ap-{cons_ap}/epg-{cons_epg.name}"
                    cons_tag = pctag_for(cons_dn)
                    cons_leaves = _leaves_with_local_endpoints(store, cons_dn)

                    # Real ACI compiles the (bidirectional-capable) zoning
                    # rule pair onto every leaf hosting either side's
                    # endpoints — the leaf enforces the policy for its own
                    # local traffic regardless of which side it hosts.
                    target_leaves = prov_leaves | cons_leaves
                    if not target_leaves:
                        continue

                    for flt in contract.filters:
                        for leaf_id in sorted(target_leaves):
                            actrl_dn = f"topology/pod-{pod}/node-{leaf_id}/sys/actrl"
                            if actrl_dn not in seen_actrl_dn:
                                seen_actrl_dn.add(actrl_dn)
                                store.add(MO("actrlRuleCont", dn=actrl_dn))
                            scope_dn = f"{actrl_dn}/scope-[{scope_id}]"
                            if scope_dn not in seen_scope_dn:
                                seen_scope_dn.add(scope_dn)
                                store.add(MO("actrlScope", dn=scope_dn, scopeId=scope_id))
                            rule_id = f"{contract.name}-{flt.name}"
                            store.add(MO(
                                "actrlRule",
                                dn=f"{scope_dn}/rule-[{rule_id}]",
                                scopeId=scope_id,
                                sPcTag=prov_tag,
                                dPcTag=cons_tag,
                                fltId=flt.name,
                                action="permit",
                                direction="bi",
                                prio="pol_default",
                                ctrctName=contract.name,
                                operSt="enabled",
                            ))
