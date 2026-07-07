"""Malformed-property write validation (APIC-style 400) + query-target=children.

Both surfaced 2026-07-07 by a TN2 push whose var file was missing the
firewall block: the unguarded J2 template rendered ``fvSubnet ip=".1/24"``
and ``vnsRedirectDest ip="."`` and the sim ACCEPTED both (a real APIC 400s),
and the verification that should have caught it used ``query-target=children``
— which the sim silently treated as unsupported and answered with empty
imdata (a false negative)."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from aci_sim.build.orchestrator import build_site
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology

TOPO_PATH = "topology.yaml"
TENANT = "VALTEST-T1"
BD_DN = f"uni/tn-{TENANT}/BD-bd-Val1"


@pytest.fixture(scope="module")
def client():
    topo = load_topology(TOPO_PATH)
    site = topo.sites[0]
    store = build_site(topo, site)
    state = ApicSiteState(
        name=site.name, site=site, topo=topo,
        store=store, baseline=copy.deepcopy(store),
    )
    c = TestClient(make_apic_app(state))
    resp = c.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    assert resp.status_code == 200
    _post(c, f"uni/tn-{TENANT}", {"fvTenant": {"attributes": {"name": TENANT}}})
    _post(c, BD_DN, {"fvBD": {"attributes": {"name": "bd-Val1", "dn": BD_DN}}})
    return c


def _post(client, dn: str, body: dict):
    return client.post(f"/api/mo/{dn}.json", json=body)


def test_malformed_fvsubnet_ip_rejected_like_real_apic(client):
    dn = f"{BD_DN}/subnet-[.1/24]"
    r = _post(client, dn, {"fvSubnet": {"attributes": {"ip": ".1/24", "dn": dn}}})
    assert r.status_code == 400
    err = r.json()["imdata"][0]["error"]["attributes"]
    assert ".1/24" in err["text"]
    q = client.get("/api/class/fvSubnet.json").json()
    assert not any(
        ".1/24" in i["fvSubnet"]["attributes"]["dn"] for i in q["imdata"]
    ), "malformed subnet must not land in the MIT"


def test_malformed_vnsredirectdest_rejected(client):
    pol_dn = f"uni/tn-{TENANT}/svcCont/svcRedirectPol-pbr-Val"
    r = _post(client, pol_dn, {"vnsSvcRedirectPol": {"attributes": {"name": "pbr-Val", "dn": pol_dn}}})
    assert r.status_code == 200
    dest_dn = f"{pol_dn}/RedirectDest_ip-[.]"
    r = _post(client, dest_dn, {"vnsRedirectDest": {"attributes": {"ip": ".", "dn": dest_dn}}})
    assert r.status_code == 400


def test_valid_subnet_accepted_and_delete_skips_validation(client):
    sub_dn = f"{BD_DN}/subnet-[10.99.1.1/24]"
    r = _post(client, sub_dn, {"fvSubnet": {"attributes": {"ip": "10.99.1.1/24", "dn": sub_dn}}})
    assert r.status_code == 200
    # A delete carries no ip attribute — validation must not block cleanup.
    r = _post(client, sub_dn, {"fvSubnet": {"attributes": {"dn": sub_dn, "status": "deleted"}}})
    assert r.status_code == 200


def test_query_target_children_returns_direct_children_flat(client):
    # Re-create the subnet so the BD has a child to find.
    sub_dn = f"{BD_DN}/subnet-[10.99.1.1/24]"
    _post(client, sub_dn, {"fvSubnet": {"attributes": {"ip": "10.99.1.1/24", "dn": sub_dn}}})
    r = client.get(f"/api/node/mo/uni/tn-{TENANT}.json?query-target=children")
    data = r.json()
    classes = [next(iter(i)) for i in data["imdata"]]
    assert "fvBD" in classes, f"direct child missing: {classes}"
    assert "fvTenant" not in classes, "root must be excluded"
    r2 = client.get(
        f"/api/node/mo/uni/tn-{TENANT}.json?query-target=children&target-subtree-class=fvBD"
    )
    classes2 = [next(iter(i)) for i in r2.json()["imdata"]]
    assert classes2 and set(classes2) == {"fvBD"}
    # Grandchildren (the subnet) must NOT appear — children is one level only.
    r3 = client.get(f"/api/node/mo/{BD_DN}.json?query-target=children")
    classes3 = [next(iter(i)) for i in r3.json()["imdata"]]
    assert "fvSubnet" in classes3
