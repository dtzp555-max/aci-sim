"""build/health_faults.py — healthInst, faultInst, coopPol/coopInst, configExportP.

healthInst DN (read by topology.py line 160-163):
  topology/pod-{pod}/node-{id}/sys/health
  → connector.query_dn(dn) → item.get("healthInst", {}).get("attributes", {})

faultInst DNs must:
  1. Start with topology/pod-{pod}/node-{id}/  (for node-scoped queries)
  2. Contain /node-{id}/  (for topology.py line 196 grouping regex)
  Required attrs: severity, code, lc, type, subject, descr, created, lastTransition
"""
from __future__ import annotations

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import Site, Topology

_CREATED_TS = "2026-01-01T00:00:00.000+00:00"


def build(topo: Topology, site: Site, store: MITStore) -> None:
    """Emit health/fault/coop/config MOs for *site*."""
    pod = site.pod
    faults_cfg = topo.faults
    hd = faults_cfg.health_defaults

    # ---- Per-node healthInst --------------------------------------------------
    for node in site.all_nodes():
        store.add(MO(
            "healthInst",
            dn=f"topology/pod-{pod}/node-{node.id}/sys/health",
            cur=str(hd.node),
            min=str(hd.node),
            max="100",
            prev=str(hd.node),
        ))

    # ---- Per-controller healthInst --------------------------------------------
    # Real APIC exposes controller (role=controller, node IDs 1..N) health at
    # the same .../sys/health DN shape as switch nodes; fabric.py already
    # builds a topSystem at topology/pod-{pod}/node-{cid}/sys for each
    # controller, so this just adds the matching healthInst there too.
    for cid in range(1, site.controllers + 1):
        store.add(MO(
            "healthInst",
            dn=f"topology/pod-{pod}/node-{cid}/sys/health",
            cur=str(hd.node),
            min=str(hd.node),
            max="100",
            prev=str(hd.node),
        ))

    # ---- Per-tenant healthInst (for tenants deployed in this site) -----------
    for tenant in topo.tenants:
        if site.name not in tenant.sites:
            continue
        store.add(MO(
            "healthInst",
            dn=f"uni/tn-{tenant.name}/health",
            cur=str(hd.tenant),
            min=str(hd.tenant),
            max="100",
            prev=str(hd.tenant),
        ))

    # ---- Seeded faultInst (node-local DNs for node-scoped query support) -----
    # Map node_id → pod for this site
    site_node_pods: dict[int, int] = {n.id: pod for n in site.all_nodes()}

    for seed in faults_cfg.seed:
        node_pod = site_node_pods.get(seed.node)
        if node_pod is None:
            continue  # this fault belongs to a different site's node
        # seed.code already carries the full ACI fault code (e.g. "F0532") —
        # do NOT prepend another "F" (that produced invalid "FF0532").
        fault_dn = (
            f"topology/pod-{node_pod}/node-{seed.node}"
            f"/local/svc-policyelem-id-0/uni/fault-{seed.code}"
        )
        store.add(MO(
            "faultInst",
            dn=fault_dn,
            severity=seed.severity,
            code=seed.code,
            descr=seed.descr,
            created=_CREATED_TS,
            lastTransition=_CREATED_TS,
            lc="raised",
            type="operational",
            subject="node",
        ))

    # ---- coopPol + coopInst --------------------------------------------------
    store.add(MO("coopPol", dn="uni/fabric/pol-coop", name="coop"))
    for node in site.all_nodes():
        store.add(MO(
            "coopInst",
            dn=f"topology/pod-{pod}/node-{node.id}/local/svc-coop-id-0/uni/epp/fv-[coop]/node-{node.id}",
            operSt="formed",
        ))

    # ---- configExportP + configJob (backup/export stubs) ---------------------
    store.add(MO(
        "configExportP",
        dn="uni/fabric/configexp-defaultOneTime",
        name="defaultOneTime",
    ))
    store.add(MO(
        "configJob",
        dn="uni/fabric/configexp-defaultOneTime/job-defaultOneTime",
        operSt="success",
        executeTime=_CREATED_TS,
    ))
