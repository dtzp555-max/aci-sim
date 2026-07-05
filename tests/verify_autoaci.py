"""
verify_autoaci.py — end-to-end verification harness for aci-sim.

Dual mode:
  pytest tests/verify_autoaci.py -v         (Tier 1 = must-pass, Tier 2 = best-effort)
  python  tests/verify_autoaci.py           (standalone — prints coverage table)

Architecture:
  • Module-level setup builds the topology, per-site MITStores, and NdoState once.
  • SimConnector / SimNDOConnector wrap FastAPI TestClient with the same async
    interface as the real ACI/NDO connectors, so autoACI plugins run unmodified.
  • _run() bridges async plugin coroutines into the synchronous pytest context.
  • Tier 1 (17 tests) must all pass — they cover the APIC REST surface, the /_sim
    control API, and the NDO plane.
  • Tier 2 (plugin smoke tests) are best-effort; SKIP if the autoACI repo is
    unavailable.
"""

from __future__ import annotations

import asyncio
import copy
import sys
import traceback
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# ─── Path setup (needed for standalone / different cwd) ──────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ─── Sim imports ─────────────────────────────────────────────────────────────
from aci_sim.topology.loader import load_topology
from aci_sim.build.orchestrator import build_all, build_site
from aci_sim.ndo.model import build_ndo_model
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.ndo.app import make_ndo_app

# ─── Module-level setup (runs once per pytest session) ───────────────────────
TOPO_PATH = PROJECT_ROOT / "topology.yaml"
_topo = load_topology(TOPO_PATH)
_stores = build_all(_topo)          # {site_name: MITStore}


def _fresh_apic_client(site_name: str = "LAB1") -> tuple[ApicSiteState, TestClient]:
    """Return a *fresh* (deep-copied store) APIC state + TestClient pair.

    The client is pre-authenticated (aaaLogin admin/cisco) so its cookie jar
    already carries a valid APIC-cookie — every data route now requires one.
    """
    site = next(s for s in _topo.sites if s.name == site_name)
    store = copy.deepcopy(_stores[site_name])
    state = ApicSiteState(
        name=site.name,
        site=site,
        topo=_topo,
        store=store,
        baseline=copy.deepcopy(store),
    )
    client = TestClient(make_apic_app(state))
    login = client.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    assert login.status_code == 200, f"pre-auth login failed: {login.status_code} {login.text}"
    return state, client


def _fresh_ndo_client() -> TestClient:
    """Return a fresh NDO TestClient backed by a newly derived NdoState."""
    ndo_state = build_ndo_model(_topo)
    return TestClient(make_ndo_app(ndo_state))


# Shared read-only clients (tests that only read use these for speed)
_state_a, _apic_a = _fresh_apic_client("LAB1")
_ndo_client = _fresh_ndo_client()


# ─── SimConnector ─────────────────────────────────────────────────────────────
class SimConnector:
    """Async interface mirroring the real ACI connector, backed by TestClient.

    URL-building matches aci_connector.py exactly so autoACI plugins work
    without any modification.
    """

    def __init__(self, client: TestClient, *, site_name: str = "sim"):
        self._c = client
        self.site_name = site_name

    async def query_class(
        self,
        class_name: str,
        filter_expr: str = "",
        subtree_class: str = "",
        query_target: str = "",
        subtree: str = "",
        page: int = 0,
        page_size: int = 500,
    ) -> dict:
        path = f"/api/class/{class_name}.json?"
        params = [f"page-size={page_size}", f"page={page}"]
        if filter_expr:
            params.append(f"query-target-filter={filter_expr}")
        if subtree:
            params.append(f"rsp-subtree={subtree}")
            if subtree_class:
                params.append(f"rsp-subtree-class={subtree_class}")
        else:
            if subtree_class:
                params.append(f"target-subtree-class={subtree_class}")
                if not query_target:
                    query_target = "subtree"
        if query_target:
            params.append(f"query-target={query_target}")
        path += "&".join(params)
        return self._c.get(path).json()

    async def query_dn(
        self, dn: str, subtree: bool = False, subtree_class: str = ""
    ) -> dict:
        path = f"/api/mo/{dn}.json"
        params: list[str] = []
        if subtree:
            params.append("query-target=subtree")
        if subtree_class:
            params.append(f"target-subtree-class={subtree_class}")
            if not subtree:
                params.append("query-target=subtree")
        if params:
            path += "?" + "&".join(params)
        return self._c.get(path).json()

    async def query_class_scoped(
        self, dn: str, class_name: str, filter_expr: str = ""
    ) -> dict:
        path = f"/api/node/class/{dn}/{class_name}.json"
        if filter_expr:
            path += f"?query-target-filter={filter_expr}"
        return self._c.get(path).json()

    async def _raw_get(self, path: str) -> dict:
        return self._c.get(path).json()


# ─── SimNDOConnector ─────────────────────────────────────────────────────────
class SimNDOConnector:
    """Async interface mirroring the real NDO connector, backed by TestClient."""

    def __init__(self, client: TestClient):
        self._c = client

    async def _get(self, path: str) -> dict:
        return self._c.get(path).json()

    async def get_sites(self) -> list:
        data = await self._get("/mso/api/v1/sites")
        return data.get("sites", [])

    async def get_tenants(self) -> list:
        data = await self._get("/mso/api/v1/tenants")
        return data.get("tenants", [])

    async def get_schemas(self) -> list:
        data = await self._get("/mso/api/v1/schemas")
        return data.get("schemas", [])

    async def get_schema(self, schema_id: str) -> dict:
        return await self._get(f"/mso/api/v1/schemas/{schema_id}")

    async def get_dhcp_policy_map(self) -> dict:
        raw = await self._get("/mso/api/v1/templates/summaries")
        summaries = raw if isinstance(raw, list) else raw.get("templates", [])
        dhcp_map: dict[str, str] = {}
        for s in summaries:
            if s.get("templateType") != "tenantPolicy":
                continue
            tmpl = await self._get(f"/mso/api/v1/templates/{s['templateId']}")
            inner = tmpl.get("tenantPolicyTemplate", {}).get("template", {})
            for p in inner.get("dhcpRelayPolicies", []):
                if p.get("uuid") and p.get("name"):
                    dhcp_map[p["uuid"]] = p["name"]
        return dhcp_map


def _run(coro):
    """Run a coroutine in a fresh event loop (sync bridge for Tier 2 + standalone)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# =============================================================================
# Tier 1 — MUST PASS
# =============================================================================

# ── APIC auth ─────────────────────────────────────────────────────────────────

def test_t1_apic_login():
    """APIC aaaLogin returns a token and sets APIC-cookie."""
    resp = _apic_a.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "aaaLogin" in data["imdata"][0]
    token = data["imdata"][0]["aaaLogin"]["attributes"]["token"]
    assert token, "token must be non-empty"
    assert "APIC-cookie" in resp.cookies


def test_t1_apic_fabric_nodes_count():
    """SiteA fabricNodes: 6 switches (2 spine + 2 leaf + 2 border-leaf) + 1 APIC controller
    (PR-21 single-APIC default) = 7."""
    resp = _apic_a.get("/api/class/fabricNode.json")
    assert resp.status_code == 200
    data = resp.json()
    roles = [i["fabricNode"]["attributes"]["role"] for i in data["imdata"]]
    switches = [r for r in roles if r in ("spine", "leaf")]
    controllers = [r for r in roles if r == "controller"]
    assert len(switches) == 6, f"Expected 6 switch nodes, got {len(switches)} ({roles})"
    assert len(controllers) == 1, f"Expected 1 controller node, got {len(controllers)} ({roles})"
    assert data["totalCount"] == "7", f"Expected totalCount='7', got {data['totalCount']!r}"


def test_t1_apic_tenant_mo_query():
    """GET /api/mo/uni/tn-MS-TN1.json returns the MS-TN1 fvTenant MO."""
    resp = _apic_a.get("/api/mo/uni/tn-MS-TN1.json")
    assert resp.status_code == 200
    data = resp.json()
    assert int(data["totalCount"]) >= 1
    assert "fvTenant" in data["imdata"][0]
    assert data["imdata"][0]["fvTenant"]["attributes"]["name"] == "MS-TN1"


def test_t1_apic_missing_mo_returns_200_empty():
    """A non-existent DN returns HTTP 200 + empty imdata (PR-9 FEATURE 6).

    Real APIC (and cisco.aci's GET-before-POST existence check in
    module_utils/aci.py, which treats any non-200 as fatal) requires this —
    not 404. See tests/test_pr9_ansible.py for the verified rationale.
    """
    resp = _apic_a.get("/api/mo/uni/tn-NOSUCHTENANTXYZ.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["totalCount"] == "0"
    assert data["imdata"] == []


def test_t1_apic_fault_severity_filter():
    """Filter by severity=minor returns only minor-severity faultInst objects."""
    resp = _apic_a.get(
        '/api/class/faultInst.json?query-target-filter=eq(faultInst.severity,"minor")'
    )
    assert resp.status_code == 200
    data = resp.json()
    # topology.yaml seeds F0532/minor on node-101
    assert int(data["totalCount"]) >= 1, "Expected at least one seeded minor fault"
    for item in data["imdata"]:
        assert item["faultInst"]["attributes"]["severity"] == "minor", (
            f"Non-minor fault slipped through filter: {item}"
        )


def test_t1_apic_node_scoped_fault_on_101():
    """Node-scoped fault query on node-101 returns the seeded fault.

    topology.yaml seeds ``code: "F0532"`` on node 101. The materialized faultInst
    must carry that exact, valid ACI fault code (guards the double-prefix regression).
    """
    resp = _apic_a.get(
        "/api/node/class/topology/pod-1/node-101/faultInst.json"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert int(data["totalCount"]) >= 1, "node-101 must carry the seeded fault"
    codes = [
        item["faultInst"]["attributes"].get("code", "")
        for item in data["imdata"]
    ]
    assert "F0532" in codes, (
        f"Expected exact fault code 'F0532' on node-101; found: {codes}"
    )


def test_t1_apic_mo_subtree_flat_bd():
    """query-target=subtree + target-subtree-class=fvBD returns BDs at imdata top level."""
    resp = _apic_a.get(
        "/api/mo/uni/tn-MS-TN1.json"
        "?query-target=subtree&target-subtree-class=fvBD"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert int(data["totalCount"]) >= 1, "MS-TN1 must have at least one BD"
    for item in data["imdata"]:
        assert "fvBD" in item, (
            f"Non-fvBD entry in subtree response: {list(item.keys())}"
        )


def test_t1_apic_write_and_readback():
    """POST a new fvTenant MO and verify it appears in /api/class/fvTenant.json."""
    _, wc = _fresh_apic_client("LAB1")
    resp = wc.post(
        "/api/mo/uni/tn-VERIFY.json",
        json={"fvTenant": {"attributes": {"name": "VERIFY", "dn": "uni/tn-VERIFY"}}},
    )
    assert resp.status_code == 200

    names = [
        item["fvTenant"]["attributes"]["name"]
        for item in wc.get("/api/class/fvTenant.json").json()["imdata"]
    ]
    assert "VERIFY" in names, f"VERIFY tenant not found after write; list: {names}"


def test_t1_apic_fabric_node_reaction():
    """POST fabricNodeIdentP causes a fabricNode MO to materialize at topology/.../node-901."""
    _, wc = _fresh_apic_client("LAB1")
    resp = wc.post(
        "/api/mo/uni/controller/nodeidentpol/nodep-901.json",
        json={
            "fabricNodeIdentP": {
                "attributes": {
                    "dn": "uni/controller/nodeidentpol/nodep-901",
                    "name": "leaf-901",
                    "nodeType": "leaf",
                }
            }
        },
    )
    assert resp.status_code == 200

    ids = [
        item["fabricNode"]["attributes"]["id"]
        for item in wc.get("/api/class/fabricNode.json").json()["imdata"]
    ]
    assert "901" in ids, (
        f"fabricNode 901 not materialized after fabricNodeIdentP write; ids: {ids}"
    )


# ── Control API ───────────────────────────────────────────────────────────────

def test_t1_sim_reset():
    """POST /_sim/reset restores the store to baseline; a written tenant disappears."""
    _, cc = _fresh_apic_client("LAB1")

    # Write a sentinel tenant
    cc.post(
        "/api/mo/uni/tn-RESETME.json",
        json={"fvTenant": {"attributes": {"name": "RESETME", "dn": "uni/tn-RESETME"}}},
    )
    before = [
        item["fvTenant"]["attributes"]["name"]
        for item in cc.get("/api/class/fvTenant.json").json()["imdata"]
    ]
    assert "RESETME" in before, "Sentinel write must succeed before reset"

    # Reset
    resp = cc.post("/_sim/reset")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # Sentinel must be gone
    after = [
        item["fvTenant"]["attributes"]["name"]
        for item in cc.get("/api/class/fvTenant.json").json()["imdata"]
    ]
    assert "RESETME" not in after, (
        "RESETME tenant must disappear after /_sim/reset"
    )


def test_t1_sim_snapshot_restore():
    """/_sim/snapshot + /_sim/restore round-trip preserves exact store state."""
    _, cc = _fresh_apic_client("LAB1")

    # Snapshot of clean state
    resp = cc.post("/_sim/snapshot/checkpoint-1")
    assert resp.status_code == 200

    # Dirty the store
    cc.post(
        "/api/mo/uni/tn-SNAP.json",
        json={"fvTenant": {"attributes": {"name": "SNAP", "dn": "uni/tn-SNAP"}}},
    )
    mid = [
        item["fvTenant"]["attributes"]["name"]
        for item in cc.get("/api/class/fvTenant.json").json()["imdata"]
    ]
    assert "SNAP" in mid, "Write must succeed before restore"

    # Restore snapshot
    resp = cc.post("/_sim/restore/checkpoint-1")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # Dirty tenant must be gone
    after = [
        item["fvTenant"]["attributes"]["name"]
        for item in cc.get("/api/class/fvTenant.json").json()["imdata"]
    ]
    assert "SNAP" not in after, (
        "SNAP tenant must disappear after /_sim/restore/checkpoint-1"
    )


# ── NDO ───────────────────────────────────────────────────────────────────────

def test_t1_ndo_login():
    """NDO /api/v1/auth/login returns a non-empty bearer token."""
    resp = _ndo_client.post(
        "/api/v1/auth/login",
        json={"userName": "admin", "userPasswd": "cisco123", "domain": "local"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "token" in data, f"Login response missing 'token': {data}"
    assert data["token"], "NDO token must be non-empty"


def test_t1_ndo_sites_match_topology():
    """NDO /mso/api/v1/sites returns one entry per topology site with correct names."""
    resp = _ndo_client.get("/mso/api/v1/sites")
    assert resp.status_code == 200
    sites = resp.json()["sites"]
    assert len(sites) == len(_topo.sites), (
        f"Expected {len(_topo.sites)} NDO sites, got {len(sites)}"
    )
    topo_names = {s.name for s in _topo.sites}
    ndo_names = {s["name"] for s in sites}
    assert topo_names == ndo_names, (
        f"NDO site names {ndo_names} differ from topology {topo_names}"
    )


def test_t1_ndo_tenants_match_topology():
    """NDO /mso/api/v1/tenants returns one entry per topology tenant."""
    resp = _ndo_client.get("/mso/api/v1/tenants")
    assert resp.status_code == 200
    tenants = resp.json()["tenants"]
    assert len(tenants) == len(_topo.tenants), (
        f"Expected {len(_topo.tenants)} tenants, got {len(tenants)}"
    )
    topo_names = {t.name for t in _topo.tenants}
    ndo_names = {t["name"] for t in tenants}
    assert topo_names == ndo_names


def test_t1_ndo_schemas_list():
    """NDO /mso/api/v1/schemas returns at least one schema per topology tenant."""
    resp = _ndo_client.get("/mso/api/v1/schemas")
    assert resp.status_code == 200
    schemas = resp.json()["schemas"]
    assert len(schemas) >= len(_topo.tenants), (
        f"Expected >= {len(_topo.tenants)} schemas, got {len(schemas)}"
    )
    for s in schemas:
        assert "id" in s, f"Schema entry missing 'id': {s}"
        assert "name" in s, f"Schema entry missing 'name': {s}"


def test_t1_ndo_corp_schema_vrf_bd_match():
    """MS-TN1 schema detail: VRF and BD names match topology exactly."""
    schemas = _ndo_client.get("/mso/api/v1/schemas").json()["schemas"]
    corp_summary = next(s for s in schemas if "MS-TN1" in s["name"])
    detail = _ndo_client.get(
        f"/mso/api/v1/schemas/{corp_summary['id']}"
    ).json()

    tmpl = detail["templates"][0]
    corp_topo = next(t for t in _topo.tenants if t.name == "MS-TN1")

    topo_vrfs = {v.name for v in corp_topo.vrfs}
    ndo_vrfs = {v["name"] for v in tmpl["vrfs"]}
    assert topo_vrfs == ndo_vrfs, f"VRF mismatch: topo={topo_vrfs} ndo={ndo_vrfs}"

    topo_bds = {b.name for b in corp_topo.bds}
    ndo_bds = {b["name"] for b in tmpl["bds"]}
    assert topo_bds == ndo_bds, f"BD mismatch: topo={topo_bds} ndo={ndo_bds}"


def test_t1_ndo_post_schema_readback():
    """POST /mso/api/v1/schemas creates a schema that appears in the list."""
    fc = _fresh_ndo_client()
    resp = fc.post(
        "/mso/api/v1/schemas",
        json={
            "displayName": "verify-schema",
            "name": "verify-schema",
            "templates": [{"name": "t1", "templateType": "application"}],
        },
    )
    assert resp.status_code == 200
    created = resp.json()
    assert "id" in created, f"POST /schemas response missing 'id': {created}"
    assert created.get("status") == "active", (
        f"Expected status='active', got {created.get('status')!r}"
    )

    schema_id = created["id"]
    list_ids = {s["id"] for s in fc.get("/mso/api/v1/schemas").json()["schemas"]}
    assert schema_id in list_ids, (
        f"Schema {schema_id!r} not found in list after POST"
    )


# =============================================================================
# Tier 2 — best-effort plugin smoke tests
# =============================================================================

_AUTOACI_BACKEND = PROJECT_ROOT.parent / "autoACI" / "backend"


def _import_plugin(module: str, class_name: str):
    """Try to import a plugin class from the autoACI backend; return (cls, None) or (None, reason)."""
    backend = _AUTOACI_BACKEND
    if not backend.exists():
        return None, f"autoACI backend not found at {backend}"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    try:
        mod = __import__(module, fromlist=[class_name])
        return getattr(mod, class_name), None
    except Exception as exc:
        return None, str(exc)


def _run_plugin(plugin_cls, connector, params: dict) -> str:
    """Execute a plugin and return a status string."""
    try:
        result = _run(plugin_cls().execute(connector, params))
        return f"OK ({len(result.rows)} rows)"
    except Exception as exc:
        return f"FAIL ({exc})"


def _tier2_epg_detail() -> str:
    cls, err = _import_plugin("plugins.epg_detail", "EpgDetail")
    if cls is None:
        return f"SKIP ({err})"
    _, client = _fresh_apic_client("LAB1")
    connector = SimConnector(client, site_name="LAB1")
    return _run_plugin(cls, connector, {"tenant_name": "MS-TN1"})


def _tier2_l3out_detail() -> str:
    cls, err = _import_plugin("plugins.l3out_detail", "L3OutDetail")
    if cls is None:
        return f"SKIP ({err})"
    _, client = _fresh_apic_client("LAB1")
    connector = SimConnector(client, site_name="LAB1")
    return _run_plugin(cls, connector, {"tenant_name": "MS-TN1"})


# =============================================================================
# Coverage table
# =============================================================================

_TIER1_TESTS: list[tuple[str, str, Any]] = [
    ("T1-01", "APIC login (token + cookie)",          test_t1_apic_login),
    ("T1-02", "APIC fabricNode count = 6 (LAB1)",     test_t1_apic_fabric_nodes_count),
    ("T1-03", "APIC MO query — MS-TN1 fvTenant",     test_t1_apic_tenant_mo_query),
    ("T1-04", "APIC 200-empty for unknown DN",         test_t1_apic_missing_mo_returns_200_empty),
    ("T1-05", "APIC fault severity filter (minor)",    test_t1_apic_fault_severity_filter),
    ("T1-06", "APIC node-scoped fault node-101 F0532",test_t1_apic_node_scoped_fault_on_101),
    ("T1-07", "APIC subtree flat BD query",            test_t1_apic_mo_subtree_flat_bd),
    ("T1-08", "APIC write + readback (fvTenant)",      test_t1_apic_write_and_readback),
    ("T1-09", "APIC fabricNodeIdentP → fabricNode",    test_t1_apic_fabric_node_reaction),
    ("T1-10", "/_sim/reset clears written MOs",        test_t1_sim_reset),
    ("T1-11", "/_sim/snapshot + restore round-trip",   test_t1_sim_snapshot_restore),
    ("T1-12", "NDO login → bearer token",              test_t1_ndo_login),
    ("T1-13", "NDO sites match topology",              test_t1_ndo_sites_match_topology),
    ("T1-14", "NDO tenants match topology",            test_t1_ndo_tenants_match_topology),
    ("T1-15", "NDO schemas list (>= 1 per tenant)",    test_t1_ndo_schemas_list),
    ("T1-16", "NDO MS-TN1 schema VRF+BD names match",  test_t1_ndo_corp_schema_vrf_bd_match),
    ("T1-17", "NDO POST schema + readback",            test_t1_ndo_post_schema_readback),
]

_TIER2_TESTS: list[tuple[str, str, Any]] = [
    ("T2-01", "epg_detail plugin smoke (MS-TN1, LAB1)",  _tier2_epg_detail),
    ("T2-02", "l3out_detail plugin smoke (MS-TN1, LAB1)", _tier2_l3out_detail),
]


def _print_coverage(results: list[tuple[str, str, str]]) -> None:
    W_ID, W_DESC, W_RES = 6, 42, 24
    sep = f"+{'-'*(W_ID+2)}+{'-'*(W_DESC+2)}+{'-'*(W_RES+2)}+"
    print()
    print("=" * (W_ID + W_DESC + W_RES + 10))
    print("  aci-sim  ·  autoACI Verification Coverage")
    print("=" * (W_ID + W_DESC + W_RES + 10))
    print(sep)
    print(f"| {'ID':<{W_ID}} | {'Description':<{W_DESC}} | {'Result':<{W_RES}} |")
    print(sep)

    tier1_pass = tier1_fail = tier2_ok = tier2_skip = tier2_fail = 0
    for id_, desc, result in results:
        print(f"| {id_:<{W_ID}} | {desc:<{W_DESC}} | {result[:W_RES]:<{W_RES}} |")
        if id_.startswith("T1"):
            if result.startswith("PASS"):
                tier1_pass += 1
            else:
                tier1_fail += 1
        else:
            if result.startswith("OK"):
                tier2_ok += 1
            elif result.startswith("SKIP"):
                tier2_skip += 1
            else:
                tier2_fail += 1

    print(sep)
    print(f"  Tier 1: {tier1_pass}/{tier1_pass + tier1_fail} passed"
          + (f", {tier1_fail} FAILED" if tier1_fail else ""))
    print(f"  Tier 2: {tier2_ok} OK, {tier2_skip} skipped, {tier2_fail} failed")
    print()


# =============================================================================
# Standalone entry-point
# =============================================================================

if __name__ == "__main__":
    results: list[tuple[str, str, str]] = []
    all_pass = True

    print("\nTier 1 — must-pass tests")
    print("-" * 52)
    for id_, desc, fn in _TIER1_TESTS:
        try:
            fn()
            status = "PASS"
            print(f"  {id_}  PASS  {desc}")
        except Exception as exc:
            status = f"FAIL ({exc})"
            print(f"  {id_}  FAIL  {desc}")
            print(f"         {exc}")
            all_pass = False
        results.append((id_, desc, status))

    print("\nTier 2 — plugin smoke tests (best-effort)")
    print("-" * 52)
    for id_, desc, fn in _TIER2_TESTS:
        status = fn()
        print(f"  {id_}  {status}  {desc}")
        results.append((id_, desc, status))

    _print_coverage(results)

    if all_pass:
        print("All Tier 1 tests passed.")
        sys.exit(0)
    else:
        t1_fails = sum(
            1 for id_, _, r in results
            if id_.startswith("T1") and not r.startswith("PASS")
        )
        print(f"FAILED: {t1_fails} Tier 1 test(s) did not pass.")
        sys.exit(1)
