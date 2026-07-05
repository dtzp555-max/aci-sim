"""Regression tests for PR-5 topology-consistency findings #13-#18 (all in
aci_sim/build/):

FIX 1 (#13) — l3extRsPathL3OutAtt tDn referenced a nonexistent leaf port
  (eth1/48 on border leaves, outside the eth1/1-8 host range). interfaces.py
  now builds a dedicated routed l1PhysIf at that exact port on border leaves.
FIX 2 (#14) — spine ospfAdjEp referenced ports outside the spine's fabric-
  uplink range (eth1/1-4). interfaces.py now builds a dedicated ISN uplink
  l1PhysIf on spines (multi-site only) at the same eth1/{49+i} ports
  underlay.py already used.
FIX 3 (#15) — endpoint host ports collided with APIC-controller-reserved
  leaf ports (both used eth1/1..3 on the same leaves). endpoints.py now
  starts endpoint port allocation right after the APIC-reserved range.
FIX 4 (#16) — a stretched tenant's endpoint appeared identically
  front-panel-local on BOTH sites. endpoints.py now picks one HOME site per
  endpoint (front-panel port, lcC="learned") and represents it on the peer
  site as learned via a multi-site overlay tunnel (lcC="learned,vxlan",
  fabricPathDn -> tunnelIf). See docs/DESIGN.md.
FIX 5 (#17) — routed BDs (has fvSubnet children) reported unicastRoute="no"
  whenever l2stretch was true, disagreeing with NDO's unicastRouting=true.
  tenants.py now derives unicastRoute from whether the BD has subnets.
FIX 6 (#18) — controller nodes had no healthInst (404 on GET .../sys/health).
  health_faults.py now emits healthInst for controller node ids too.

Auth is enforced (PR-4): every client fixture logs in via
POST /api/aaaLogin.json (admin/cisco) before making authenticated calls,
following the existing tests/test_p5_fixes.py / test_pr3_fixes.py pattern.
"""
from __future__ import annotations

import copy
import re

import pytest
from fastapi.testclient import TestClient

from aci_sim.build.orchestrator import build_site
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology

TOPO_PATH = "topology.yaml"


@pytest.fixture(scope="module")
def topo():
    return load_topology(TOPO_PATH)


@pytest.fixture(scope="module")
def site_a(topo):
    return topo.site_by_name("LAB1")


@pytest.fixture(scope="module")
def site_b(topo):
    return topo.site_by_name("LAB2")


@pytest.fixture(scope="module")
def store_a(topo, site_a):
    return build_site(topo, site_a)


@pytest.fixture(scope="module")
def store_b(topo, site_b):
    return build_site(topo, site_b)


def _make_client(topo, site, store) -> TestClient:
    state = ApicSiteState(
        name=site.name, site=site, topo=topo, store=store,
        baseline=copy.deepcopy(store),
    )
    c = TestClient(make_apic_app(state))
    c.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    return c


@pytest.fixture(scope="module")
def client_a(topo, site_a, store_a):
    return _make_client(topo, site_a, store_a)


@pytest.fixture(scope="module")
def client_b(topo, site_b, store_b):
    return _make_client(topo, site_b, store_b)


# ---------------------------------------------------------------------------
# FIX 1 (#13) — every l3extRsPathL3OutAtt tDn's port exists as a real l1PhysIf
# ---------------------------------------------------------------------------

def _l1physif_ports(store) -> dict[int, set[str]]:
    """Return {node_id: {port_id, ...}} from every l1PhysIf DN in the store."""
    ports: dict[int, set[str]] = {}
    for mo in store.by_class("l1PhysIf"):
        m = re.search(r"/node-(\d+)/sys/phys-\[([^\]]+)\]", mo.dn)
        assert m is not None, f"unparsable l1PhysIf dn {mo.dn!r}"
        node_id = int(m.group(1))
        ports.setdefault(node_id, set()).add(m.group(2))
    return ports


@pytest.mark.parametrize("store_name", ["store_a", "store_b"])
def test_l3out_path_att_references_real_port(store_name, request):
    store = request.getfixturevalue(store_name)
    ports = _l1physif_ports(store)
    atts = list(store.by_class("l3extRsPathL3OutAtt"))
    assert atts, "no l3extRsPathL3OutAtt found"
    for att in atts:
        m = re.search(r"paths-(\d+)/pathep-\[([^\]]+)\]", att.attrs["tDn"])
        assert m is not None, f"unparsable tDn {att.attrs['tDn']!r}"
        node_id, port_id = int(m.group(1)), m.group(2)
        assert port_id in ports.get(node_id, set()), (
            f"l3extRsPathL3OutAtt {att.dn!r} references {port_id!r} on node "
            f"{node_id}, which has no matching l1PhysIf (has {ports.get(node_id)})"
        )


# ---------------------------------------------------------------------------
# FIX 2 (#14) — every ospfAdjEp interface exists as a real l1PhysIf on that node
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("store_name", ["store_a", "store_b"])
def test_ospf_adjacency_references_real_port(store_name, request):
    store = request.getfixturevalue(store_name)
    ports = _l1physif_ports(store)
    adjs = list(store.by_class("ospfAdjEp"))
    assert adjs, "no ospfAdjEp found (multi-site topology expected)"
    for adj in adjs:
        m = re.search(r"/node-(\d+)/sys/ospf/.*?/if-\[([^\]]+)\]", adj.dn)
        assert m is not None, f"unparsable ospfAdjEp dn {adj.dn!r}"
        node_id, port_id = int(m.group(1)), m.group(2)
        assert port_id in ports.get(node_id, set()), (
            f"ospfAdjEp {adj.dn!r} references {port_id!r} on node {node_id}, "
            f"which has no matching l1PhysIf (has {ports.get(node_id)})"
        )


# ---------------------------------------------------------------------------
# FIX 3 (#15) — no port hosts both a controller lldpAdjEp and an endpoint path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("store_name", ["store_a", "store_b"])
def test_no_port_double_booked_between_apic_and_endpoint(store_name, request):
    store = request.getfixturevalue(store_name)

    controller_ports: set[tuple[int, str]] = set()
    for mo in store.by_class("lldpAdjEp"):
        # topology/pod-1/node-101/sys/lldp/inst/if-[eth1/1]/adj-1
        m = re.search(r"/node-(\d+)/sys/lldp/inst/if-\[([^\]]+)\]", mo.dn)
        if not m:
            continue
        if "APIC" in mo.attrs.get("sysName", ""):
            controller_ports.add((int(m.group(1)), m.group(2)))
    assert controller_ports, "no controller lldpAdjEp found"

    endpoint_ports: set[tuple[int, str]] = set()
    for mo in store.by_class("fvCEp"):
        path = mo.attrs.get("fabricPathDn", "")
        m = re.search(r"paths-(\d+)/pathep-\[([^\]]+)\]", path)
        if not m:
            continue
        endpoint_ports.add((int(m.group(1)), m.group(2)))
    assert endpoint_ports, "no endpoint fabricPathDn found"

    collisions = controller_ports & endpoint_ports
    assert not collisions, f"port(s) double-booked between APIC and endpoint: {collisions}"


# ---------------------------------------------------------------------------
# FIX 4 (#16) — each stretched EP is local on exactly one site, vxlan-learned
# on the other (cross-site test: build BOTH sites' stores and compare).
# ---------------------------------------------------------------------------

def test_stretched_endpoint_local_on_exactly_one_site(store_a, store_b):
    ceps_a = {
        m.attrs["mac"]: m for m in store_a.by_class("fvCEp")
        if m.dn.startswith("uni/tn-MS-TN1/")
    }
    ceps_b = {
        m.attrs["mac"]: m for m in store_b.by_class("fvCEp")
        if m.dn.startswith("uni/tn-MS-TN1/")
    }
    common_macs = set(ceps_a) & set(ceps_b)
    assert common_macs, "no stretched (MS-TN1) endpoints found on both sites"

    for mac in common_macs:
        cep_a, cep_b = ceps_a[mac], ceps_b[mac]
        lc_a, lc_b = cep_a.attrs.get("lcC"), cep_b.attrs.get("lcC")

        # Exactly one side is HOME (front-panel-learned)...
        homes = [lc for lc in (lc_a, lc_b) if lc == "learned"]
        assert len(homes) == 1, (
            f"MAC {mac}: expected exactly one site with lcC='learned', "
            f"got A={lc_a!r} B={lc_b!r}"
        )
        # ...and the other is REMOTE (vxlan/tunnel-learned).
        remotes = [lc for lc in (lc_a, lc_b) if lc == "learned,vxlan"]
        assert len(remotes) == 1, (
            f"MAC {mac}: expected exactly one site with lcC='learned,vxlan', "
            f"got A={lc_a!r} B={lc_b!r}"
        )

        home_cep = cep_a if lc_a == "learned" else cep_b
        remote_cep = cep_b if lc_a == "learned" else cep_a

        # HOME: fabricPathDn is a real front-panel port (pathep-[ethX/Y]).
        assert re.search(r"pathep-\[eth\d+/\d+\]", home_cep.attrs["fabricPathDn"]), (
            f"HOME fvCEp {home_cep.dn!r} fabricPathDn not a front-panel port: "
            f"{home_cep.attrs['fabricPathDn']!r}"
        )
        # REMOTE: fabricPathDn points at a tunnel interface, not a front port.
        assert re.search(r"pathep-\[tunnel\d+\]", remote_cep.attrs["fabricPathDn"]), (
            f"REMOTE fvCEp {remote_cep.dn!r} fabricPathDn not a tunnel path: "
            f"{remote_cep.attrs['fabricPathDn']!r}"
        )


def test_stretched_endpoint_remote_tunnel_if_exists(store_a, store_b):
    """The REMOTE fvCEp's tunnelIf must actually exist in that site's store."""
    for store in (store_a, store_b):
        for cep in store.by_class("fvCEp"):
            if cep.attrs.get("lcC") != "learned,vxlan":
                continue
            m = re.search(r"paths-(\d+)/pathep-\[(tunnel\d+)\]", cep.attrs["fabricPathDn"])
            assert m is not None
            spine_id, tunnel_id = m.group(1), m.group(2)
            matches = [
                t for t in store.by_class("tunnelIf")
                if t.attrs.get("id") == tunnel_id and f"/node-{spine_id}/" in t.dn
            ]
            assert matches, (
                f"no tunnelIf with id={tunnel_id!r} on node {spine_id} for "
                f"remote fvCEp {cep.dn!r}"
            )


# ---------------------------------------------------------------------------
# FIX 5 (#17) — every BD with fvSubnet children has unicastRoute="yes"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("store_name", ["store_a", "store_b"])
def test_routed_bd_has_unicast_route_yes(store_name, request):
    store = request.getfixturevalue(store_name)
    bds = list(store.by_class("fvBD"))
    assert bds, "no fvBD found"
    checked = 0
    for bd in bds:
        subnets = store.children(bd.dn, {"fvSubnet"})
        if not subnets:
            continue
        checked += 1
        assert bd.attrs.get("unicastRoute") == "yes", (
            f"fvBD {bd.dn!r} has {len(subnets)} fvSubnet children but "
            f"unicastRoute={bd.attrs.get('unicastRoute')!r} (expected 'yes')"
        )
    assert checked, "no routed (subnet-bearing) BD found to check"


# ---------------------------------------------------------------------------
# FIX 6 (#18) — node-1 health returns 200 on both sites' controllers
# ---------------------------------------------------------------------------

def test_controller_health_returns_200_both_sites(client_a, client_b):
    for client in (client_a, client_b):
        resp = client.get("/api/mo/topology/pod-1/node-1/sys/health.json")
        assert resp.status_code == 200, (
            f"controller node-1 health -> {resp.status_code}: {resp.text[:200]}"
        )
        data = resp.json()
        assert data["imdata"], "expected a healthInst row for controller node-1"
        assert "healthInst" in data["imdata"][0]
