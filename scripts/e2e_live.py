"""Full-stack E2E: drive the REAL autoACI backend against the sim.

Proves the acceptance criterion — point local autoACI at the sim and every view
renders internally-consistent data. Unlike tests/verify_autoaci.py (which uses
TestClient in-process), this hits the running HTTP stack end to end.

Prerequisites (two servers running):
  1. the sim:      cd ~/aci-sim && bash scripts/run.sh
  2. autoACI:      cd ~/autoACI/backend && ./.venv/bin/uvicorn main:app --port 8000

Run:  cd ~/autoACI/backend && ./.venv/bin/python ~/aci-sim/scripts/e2e_live.py
It logs autoACI into the sim's two APICs (:8443/:8444) + NDO (:8445), then
exercises /api/topology, ~14 real plugins, and the NDO views. Exit 0 = all pass.
"""
import json
import sys

import httpx

BASE = "http://127.0.0.1:8000"
SITE_A, SITE_B, NDO = "127.0.0.1:8443", "127.0.0.1:8444", "127.0.0.1:8445"
c = httpx.Client(base_url=BASE, timeout=90.0)
results = []  # (name, ok, detail)


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")


def csrf():
    return c.cookies.get("csrf_token", "")


def post(path, body, site=None):
    h = {"X-CSRF-Token": csrf(), "Content-Type": "application/json"}
    if site:
        h["X-Active-Site"] = site
    return c.post(path, json=body, headers=h)


def get(path, site=None):
    h = {}
    if site:
        h["X-Active-Site"] = site
    return c.get(path, headers=h)


print("=== bootstrap: obtain CSRF cookie ===")
c.get("/api/health")
print("  csrf_token:", (csrf()[:12] or "<none>"))

print("\n=== APIC logins (point autoACI at the sim) ===")
for host, label in [(SITE_A, "LAB1"), (SITE_B, "LAB2")]:
    r = post("/api/auth/login", {"host": host, "username": "admin", "password": "cisco", "verify_ssl": False})
    ok = r.status_code == 200 and r.json().get("success")
    d = r.json()
    rec(f"APIC login {label} ({host})", ok,
        f"fabric={d.get('fabric_name')!r} ver={d.get('apic_version')!r} role={d.get('role')!r}")

print("\n=== NDO login ===")
r = post("/api/ndo/login", {"host": NDO, "username": "admin", "password": "cisco", "verify_ssl": False})
rec("NDO login", r.status_code == 200, f"HTTP {r.status_code} {str(r.json())[:80]}")

print("\n=== /api/auth/status (multi-site) ===")
r = get("/api/auth/status")
st = r.json()
rec("auth status authenticated", bool(st.get("authenticated")),
    f"sites={[s.get('host') for s in st.get('sites', [])]}")

print("\n=== /api/topology (the headline view) ===")
r = get("/api/topology", site=SITE_A)
if r.status_code != 200:
    rec("GET /api/topology", False, f"HTTP {r.status_code} {r.text[:200]}")
    topo = {}
else:
    topo = r.json()
    nodes = topo.get("nodes", [])
    links = topo.get("links", [])
    roles = sorted({n.get("role", "?") for n in nodes})
    sites_seen = sorted({n.get("site", n.get("pod", "?")) for n in nodes})
    isn = topo.get("isn") or topo.get("inter_site") or [link for link in links if "isn" in str(link.get("type", "")).lower()]
    rec("GET /api/topology", len(nodes) > 0, f"top-level keys={list(topo.keys())}")
    rec("  topology nodes", len(nodes) >= 6, f"{len(nodes)} nodes, roles={roles}, sites={sites_seen}")
    rec("  topology links", len(links) > 0, f"{len(links)} links")
    print(f"    ISN/inter-site signal: {json.dumps(isn)[:160] if isn else '(inspect keys)'}")

print("\n=== prebuilt plugins (real autoACI plugin code vs the sim) ===")
# mode "rows" = expect >0 rows; mode "ran" = health/anomaly plugin, 0 rows can mean healthy → pass on HTTP 200
PLUGINS = [
    ("fabric_nodes", {}, "rows"), ("fabric_links", {}, "rows"), ("fabric_bgp_evpn", {}, "rows"),
    ("bgp_health", {}, "ran"), ("adjacency_health", {}, "ran"),
    ("fault_summary", {}, "rows"), ("health_score", {}, "rows"), ("fabric_node_summary", {}, "rows"),
    ("tenant_list", {}, "rows"), ("bd_detail", {"tenant_name": "MS-TN1"}, "rows"),
    ("epg_detail", {"tenant_name": "MS-TN1"}, "rows"), ("l3out_detail", {"tenant_name": "MS-TN1"}, "rows"),
    ("endpoint_search", {}, "rows"), ("interface_status", {"node_id": "101"}, "rows"),
]
for name, params, mode in PLUGINS:
    try:
        r = post(f"/api/query/prebuilt/{name}", {"plugin_name": name, "params": params}, site=SITE_A)
        if r.status_code != 200:
            rec(f"plugin {name}", False, f"HTTP {r.status_code} {r.text[:120]}")
            continue
        qr = r.json()
        rows = qr.get("rows", [])
        ok = (r.status_code == 200) if mode == "ran" else (len(rows) > 0)
        rec(f"plugin {name}", ok, f"{len(rows)} rows · {str(qr.get('summary',''))[:60]}")
    except Exception as e:
        rec(f"plugin {name}", False, str(e)[:120])

print("\n=== NDO views ===")
r = get("/api/ndo/sites")
sites = r.json() if r.status_code == 200 else {}
site_names = [s.get("name") for s in (sites.get("sites", sites) if isinstance(sites, (dict, list)) else [])] if r.status_code == 200 else []
rec("NDO /sites", r.status_code == 200 and len(site_names) >= 2, f"{site_names}")
r = get("/api/query/options/schemas", site=SITE_A)
rec("NDO schemas (options)", r.status_code == 200, f"HTTP {r.status_code} {str(r.json())[:100]}")

print("\n" + "=" * 60)
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"  E2E RESULT: {passed}/{total} checks passed")
fails = [n for n, ok, _ in results if not ok]
if fails:
    print("  FAILED:", ", ".join(fails))
print("=" * 60)
sys.exit(0 if not fails else 1)
