"""Sandbox E2E — replicate autoACI's exact NDO-discovery → Connect-All flow."""
import sys
from urllib.parse import urlparse
import httpx

BASE = "http://127.0.0.1:8000"
NDO_IP = "10.192.0.10"
c = httpx.Client(base_url=BASE, timeout=90.0)
results = []


def rec(n, ok, d=""):
    results.append((n, ok, d))
    print(f"  {'PASS' if ok else 'FAIL'}  {n}  {d}")


def csrf():
    return c.cookies.get("csrf_token", "")


def post(p, b, site=None):
    h = {"X-CSRF-Token": csrf(), "Content-Type": "application/json"}
    if site:
        h["X-Active-Site"] = site
    return c.post(p, json=b, headers=h)


def get(p, site=None):
    h = {"X-Active-Site": site} if site else {}
    return c.get(p, headers=h)


c.get("/api/health")

# 1. NDO login — the only thing the user types
r = post("/api/ndo/login", {"host": NDO_IP, "username": "admin", "password": "cisco", "verify_ssl": False})
rec(f"NDO login ({NDO_IP})", r.status_code == 200 and r.json().get("success"), f"HTTP {r.status_code}")

# 2. Discover sites — replicate the frontend's host extraction (new URL(url).hostname)
r = get("/api/ndo/sites")
sites = r.json().get("sites", [])
discovered = []
for s in sites:
    urls = s.get("urls") or s.get("apicUrls") or []
    host = ""
    if urls:
        try:
            host = urlparse(urls[0]).hostname  # == JS new URL(urls[0]).hostname
        except Exception:
            host = urls[0]
    discovered.append({"name": s.get("name"), "apicHost": host})
rec("NDO discovered sites", len(discovered) >= 2, str([(d["name"], d["apicHost"]) for d in discovered]))

# 3. Connect All Sites — log in to each discovered APIC by IP, NO port
for d in discovered:
    r = post("/api/auth/login", {"host": d["apicHost"], "username": "admin", "password": "cisco", "verify_ssl": False})
    j = r.json()
    rec(f"Connect {d['name']} ({d['apicHost']})", r.status_code == 200 and j.get("success"),
        f"fabric={j.get('fabric_name')!r} ver={j.get('apic_version')!r}")

site_a = discovered[0]["apicHost"]

# 4. Topology (aggregates all connectors → both sites + ISN)
r = get("/api/topology", site=site_a)
topo = r.json() if r.status_code == 200 else {}
nodes, links, isn = topo.get("nodes", []), topo.get("links", []), topo.get("isnConnections", [])
rec("Topology", len(nodes) > 0,
    f"{len(nodes)} nodes, {len(links)} links, {len(isn)} ISN links, roles={sorted({n.get('role') for n in nodes})}")

# 5. plugins
for name, params, mode in [
    ("fabric_nodes", {}, "rows"), ("fabric_bgp_evpn", {}, "rows"),
    ("adjacency_health", {}, "ran"), ("l3out_detail", {"tenant_name": "MS-TN1"}, "rows"),
    ("endpoint_search", {}, "rows"),
]:
    r = post(f"/api/query/prebuilt/{name}", {"plugin_name": name, "params": params}, site=site_a)
    qr = r.json() if r.status_code == 200 else {}
    rows = qr.get("rows", [])
    ok = (r.status_code == 200) if mode == "ran" else len(rows) > 0
    rec(f"plugin {name}", ok, f"{len(rows)} rows · {str(qr.get('summary', ''))[:50]}")

# 6. NDO views
r = get("/api/ndo/sites")
rec("NDO /sites view", r.status_code == 200, str([s.get("name") for s in r.json().get("sites", [])]))

passed = sum(1 for _, ok, _ in results if ok)
print("=" * 60)
print(f"  SANDBOX E2E: {passed}/{len(results)} passed")
fails = [n for n, ok, _ in results if not ok]
if fails:
    print("  FAILED:", ", ".join(fails))
print("=" * 60)
sys.exit(0 if not fails else 1)
