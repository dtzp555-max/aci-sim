"""Tests for the APIC REST simulation layer (Phase 5)."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from aci_sim.build.orchestrator import build_site
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology

TOPO_PATH = "topology.yaml"  # run from project root


@pytest.fixture(scope="module")
def client():
    topo = load_topology(TOPO_PATH)
    site = topo.sites[0]
    store = build_site(topo, site)
    state = ApicSiteState(
        name=site.name,
        site=site,
        topo=topo,
        store=store,
        baseline=copy.deepcopy(store),
    )
    app = make_apic_app(state)
    return TestClient(app)


def test_login(client):
    resp = client.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "aaaLogin" in data["imdata"][0]
    token = data["imdata"][0]["aaaLogin"]["attributes"]["token"]
    assert token
    assert "APIC-cookie" in resp.cookies


def test_refresh(client):
    resp = client.get("/api/aaaRefresh.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "aaaLogin" in data["imdata"][0]


def test_class_fabric_node(client):
    resp = client.get("/api/class/fabricNode.json")
    assert resp.status_code == 200
    data = resp.json()
    # 6 switches (2 spine + 2 leaf + 2 border-leaf) + 1 APIC controller (PR-21 single-APIC default) = 7
    assert data["totalCount"] == "7"
    assert len(data["imdata"]) == 7
    roles = [i["fabricNode"]["attributes"]["role"] for i in data["imdata"]]
    assert sum(r in ("spine", "leaf") for r in roles) == 6
    assert roles.count("controller") == 1


def test_class_fault_filter(client):
    resp = client.get(
        '/api/class/faultInst.json?query-target-filter=eq(faultInst.severity,"minor")'
    )
    assert resp.status_code == 200
    data = resp.json()
    # Topology seeds exactly one "minor" faultInst (F0532 on node-101) — a
    # regression that silently empties imdata (e.g. 200-with-empty-imdata)
    # must not pass silently.
    assert int(data["totalCount"]) == 1
    assert len(data["imdata"]) == 1
    for item in data["imdata"]:
        assert item["faultInst"]["attributes"]["severity"] == "minor"


def test_fault_inst_rows_carry_seeded_severities(client):
    # Stable premise for the PR-1 subtree+filter regression tests: the
    # topology seeds exactly two faultInst rows (severity=minor F0532 on
    # node-101, severity=warning F0554 on node-102). Assert both are present
    # with their seeded severity so a regression that drops/renames severity
    # values is caught here rather than only in the filter-specific tests.
    resp = client.get("/api/class/faultInst.json")
    assert resp.status_code == 200
    data = resp.json()
    assert int(data["totalCount"]) == 2
    assert len(data["imdata"]) == 2
    severities = {i["faultInst"]["attributes"]["severity"] for i in data["imdata"]}
    assert severities == {"minor", "warning"}
    codes = {i["faultInst"]["attributes"]["code"] for i in data["imdata"]}
    assert codes == {"F0532", "F0554"}


def test_mo_subtree_flat(client):
    resp = client.get(
        "/api/mo/uni/tn-MS-TN1.json?query-target=subtree&target-subtree-class=fvBD"
    )
    assert resp.status_code == 200
    data = resp.json()
    # tn-MS-TN1 seeds at least one BD in the topology fixture; a regression
    # returning 200-with-empty-imdata must fail loudly rather than pass this
    # now-vacuous loop.
    assert int(data["totalCount"]) > 0
    assert len(data["imdata"]) > 0
    for item in data["imdata"]:
        assert "fvBD" in item


def test_bracket_dn(client):
    resp = client.get("/api/class/fvSubnet.json")
    assert resp.status_code == 200
    data = resp.json()
    if not data["imdata"]:
        pytest.skip("no subnets in topology")
    dn = data["imdata"][0]["fvSubnet"]["attributes"]["dn"]
    resp2 = client.get(f"/api/mo/{dn}.json")
    assert resp2.status_code == 200
    d2 = resp2.json()
    assert len(d2["imdata"]) == 1


def test_node_scoped_fault(client):
    resp = client.get("/api/node/class/topology/pod-1/node-101/faultInst.json")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["imdata"], list)
    assert isinstance(data["totalCount"], str)


def test_write_readback(client):
    resp = client.post(
        "/api/mo/uni/tn-NEW.json",
        json={"fvTenant": {"attributes": {"name": "NEW", "dn": "uni/tn-NEW", "descr": "test"}}},
    )
    assert resp.status_code == 200
    resp2 = client.get("/api/class/fvTenant.json")
    names = [item["fvTenant"]["attributes"]["name"] for item in resp2.json()["imdata"]]
    assert "NEW" in names


def test_add_leaf_reaction(client):
    resp = client.post(
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
    resp2 = client.get("/api/class/fabricNode.json")
    ids = [item["fabricNode"]["attributes"]["id"] for item in resp2.json()["imdata"]]
    assert "901" in ids


def test_missing_mo_returns_200_empty(client):
    # PR-9 FEATURE 6: real APIC (and cisco.aci's GET-before-POST existence
    # check) requires 200 + empty imdata for a well-formed-but-absent DN, not
    # 404 — see tests/test_pr9_ansible.py for the verified module-source
    # rationale. This was test_missing_mo_404 pre-PR-9.
    resp = client.get("/api/mo/uni/tn-DOESNOTEXIST.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["totalCount"] == "0"
    assert data["imdata"] == []


# -- B2 regression: comparison filters work, malformed filters are 400 not 500 --


def test_ne_filter_returns_200_not_500(client):
    # ne/gt/lt/ge/le/bw used to hit `raise ValueError` -> uncaught -> HTTP 500.
    resp = client.get(
        '/api/class/fabricNode.json?query-target-filter=ne(fabricNode.role,"controller")'
    )
    assert resp.status_code == 200
    roles = [i["fabricNode"]["attributes"]["role"] for i in resp.json()["imdata"]]
    assert roles and "controller" not in roles


def test_gt_numeric_filter_returns_200(client):
    resp = client.get(
        '/api/class/fabricNode.json?query-target-filter=gt(fabricNode.id,"200")'
    )
    assert resp.status_code == 200
    ids = [int(i["fabricNode"]["attributes"]["id"]) for i in resp.json()["imdata"]]
    assert ids and all(i > 200 for i in ids)


def test_malformed_filter_returns_apic_400_not_500(client):
    resp = client.get(
        '/api/class/fabricNode.json?query-target-filter=frobnicate(fabricNode.id,"1")'
    )
    assert resp.status_code == 400
    err = resp.json()["imdata"][0]["error"]["attributes"]
    assert err["code"] == "107"
    assert "frobnicate" in err["text"]


# -- PR-1 F1 regression: query-target=subtree + query-target-filter on a
# descendant-only attribute must filter the EXPANDED scope, not the root --


def test_subtree_filter_on_descendant_attr_mo_query(client):
    # topology/pod-1/node-101 has a seeded faultInst descendant (severity=minor)
    # at .../local/svc-policyelem-id-0/uni/fault-F0532. Filtering on
    # faultInst.severity used to pre-filter the fabricNode root (which lacks a
    # "severity" attr → False) BEFORE subtree expansion, always returning empty
    # imdata regardless of matching descendants.
    resp = client.get(
        '/api/mo/topology/pod-1/node-101.json'
        '?query-target=subtree&target-subtree-class=faultInst'
        '&query-target-filter=eq(faultInst.severity,"minor")'
    )
    assert resp.status_code == 200
    data = resp.json()
    assert int(data["totalCount"]) > 0
    for item in data["imdata"]:
        assert "faultInst" in item
        assert item["faultInst"]["attributes"]["severity"] == "minor"


def test_subtree_filter_on_descendant_attr_class_query(client):
    resp = client.get(
        '/api/class/faultInst.json?query-target-filter=eq(faultInst.severity,"minor")'
    )
    assert resp.status_code == 200
    data = resp.json()
    assert int(data["totalCount"]) > 0
    for item in data["imdata"]:
        assert item["faultInst"]["attributes"]["severity"] == "minor"


# -- PR-1 F2 regression: non-numeric / out-of-range page & page-size -> APIC 400 --


@pytest.mark.parametrize(
    "query",
    [
        "page=abc",
        "page-size=abc",
        "page-size=-1",
        "page-size=0",
    ],
)
def test_bad_page_params_return_apic_400(client, query):
    resp = client.get(f"/api/class/fabricNode.json?{query}")
    assert resp.status_code == 400
    body = resp.json()
    assert "imdata" in body
    assert "detail" not in body
    assert "error" in body["imdata"][0]


def test_bad_page_size_return_apic_400_on_mo_query(client):
    resp = client.get("/api/mo/uni/tn-MS-TN1.json?page-size=-1")
    assert resp.status_code == 400
    body = resp.json()
    assert "imdata" in body
    assert "detail" not in body


def test_bad_page_size_return_apic_400_on_node_scoped_query(client):
    resp = client.get(
        "/api/node/class/topology/pod-1/node-101/faultInst.json?page-size=abc"
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "imdata" in body
    assert "detail" not in body


# -- PR-1 F3 regression: strict filter parsing (trailing tokens, zero-arg composites) --


@pytest.mark.parametrize(
    "expr",
    [
        'eq(fabricNode.role,"spine"),eq(fabricNode.role,"leaf")',  # comma-joined double filter
        'eq(fabricNode.role,"spine"))',  # unbalanced trailing paren
        "or()",  # zero-arg or
        "and()",  # zero-arg and
    ],
)
def test_strict_filter_parsing_returns_apic_400(client, expr):
    resp = client.get(f"/api/class/fabricNode.json?query-target-filter={expr}")
    assert resp.status_code == 400
    err = resp.json()["imdata"][0]["error"]["attributes"]
    assert err["code"] == "107"


# -- PR-1 numeric comparison guard: kills lexical-only mutation on bw/gt --


def test_bw_wide_numeric_range_matches_non_controller_nodes(client):
    # "150" < "99" lexically, so a lexical-only bw would empty the range;
    # numerically 99 <= id <= 150 must match all 4 LAB1 leaf/border-leaf nodes.
    resp = client.get(
        '/api/class/fabricNode.json?query-target-filter=bw(fabricNode.id,"99","150")'
    )
    assert resp.status_code == 200
    ids = {i["fabricNode"]["attributes"]["id"] for i in resp.json()["imdata"]}
    assert ids == {"101", "102", "103", "104"}


def test_gt_numeric_filter_matches_all_non_controller_nodes(client):
    resp = client.get(
        '/api/class/fabricNode.json?query-target-filter=gt(fabricNode.id,"99")'
    )
    assert resp.status_code == 200
    data = resp.json()
    # Controllers are id 1..N (<99); every id > 99 must be a non-controller node.
    # (Don't assert an exact count here — the shared module-scoped `client`
    # fixture may have accumulated extra nodes from earlier write tests.)
    roles = [i["fabricNode"]["attributes"]["role"] for i in data["imdata"]]
    assert roles
    assert "controller" not in roles


# -- PR-2 R1: atomic validate-then-commit POST (no partial writes on 400) --


def test_malformed_child_rejects_with_400_and_writes_nothing(client):
    resp = client.post(
        "/api/mo/uni/tn-AtomT.json",
        json={
            "fvTenant": {
                "attributes": {"name": "AtomT", "dn": "uni/tn-AtomT"},
                "children": ["not-a-dict"],
            }
        },
    )
    assert resp.status_code == 400
    err = resp.json()["imdata"][0]["error"]["attributes"]
    assert err["code"] == "103"

    resp2 = client.get("/api/mo/uni/tn-AtomT.json")
    assert resp2.status_code == 200
    assert resp2.json()["totalCount"] == "0"


def test_valid_first_child_then_malformed_second_child_rejects_everything(client):
    resp = client.post(
        "/api/mo/uni/tn-AtomT2.json",
        json={
            "fvTenant": {
                "attributes": {"name": "AtomT2", "dn": "uni/tn-AtomT2"},
                "children": [
                    {"fvCtx": {"attributes": {"name": "goodctx"}}},
                    ["not-a-dict"],
                ],
            }
        },
    )
    assert resp.status_code == 400
    err = resp.json()["imdata"][0]["error"]["attributes"]
    assert err["code"] == "103"

    # Neither the tenant nor the valid first child persisted.
    resp2 = client.get("/api/mo/uni/tn-AtomT2.json")
    assert resp2.status_code == 200
    assert resp2.json()["totalCount"] == "0"
    resp3 = client.get("/api/mo/uni/tn-AtomT2/ctx-goodctx.json")
    assert resp3.status_code == 200
    assert resp3.json()["totalCount"] == "0"


def test_malformed_grandchild_key_count_rejects_with_400_and_writes_nothing(client):
    # A child_entry dict with != 1 key is also a shape violation.
    resp = client.post(
        "/api/mo/uni/tn-AtomT3.json",
        json={
            "fvTenant": {
                "attributes": {"name": "AtomT3", "dn": "uni/tn-AtomT3"},
                "children": [{"fvCtx": {"attributes": {"name": "c1"}}, "fvBD": {"attributes": {"name": "b1"}}}],
            }
        },
    )
    assert resp.status_code == 400
    resp2 = client.get("/api/mo/uni/tn-AtomT3.json")
    assert resp2.status_code == 200
    assert resp2.json()["totalCount"] == "0"


# -- PR-2 R2: canonical RN synthesis for dn-less children --


def test_child_bd_without_dn_lands_on_canonical_BD_dn(client):
    resp = client.post(
        "/api/mo/uni/tn-RnT.json",
        json={
            "fvTenant": {
                "attributes": {"name": "RnT", "dn": "uni/tn-RnT"},
                "children": [{"fvBD": {"attributes": {"name": "bd1"}}}],
            }
        },
    )
    assert resp.status_code == 200

    ok = client.get("/api/mo/uni/tn-RnT/BD-bd1.json")
    assert ok.status_code == 200
    assert ok.json()["imdata"][0]["fvBD"]["attributes"]["name"] == "bd1"

    # The old '{cls}-{name}' heuristic must NOT have been used.
    stale = client.get("/api/mo/uni/tn-RnT/fvBD-bd1.json")
    assert stale.status_code == 200
    assert stale.json()["totalCount"] == "0"


def test_nested_ap_epg_without_dn_land_on_canonical_dns(client):
    resp = client.post(
        "/api/mo/uni/tn-RnT2.json",
        json={
            "fvTenant": {
                "attributes": {"name": "RnT2", "dn": "uni/tn-RnT2"},
                "children": [
                    {
                        "fvAp": {
                            "attributes": {"name": "ap1"},
                            "children": [{"fvAEPg": {"attributes": {"name": "epg1"}}}],
                        }
                    }
                ],
            }
        },
    )
    assert resp.status_code == 200

    ap = client.get("/api/mo/uni/tn-RnT2/ap-ap1.json")
    assert ap.status_code == 200

    epg = client.get("/api/mo/uni/tn-RnT2/ap-ap1/epg-epg1.json")
    assert epg.status_code == 200
    assert epg.json()["imdata"][0]["fvAEPg"]["attributes"]["name"] == "epg1"

    # Old heuristics must not have created a shadow object.
    stale_ap = client.get("/api/mo/uni/tn-RnT2/fvAp-ap1.json")
    assert stale_ap.status_code == 200
    assert stale_ap.json()["totalCount"] == "0"
    stale_epg = client.get("/api/mo/uni/tn-RnT2/ap-ap1/fvAEPg-epg1.json")
    assert stale_epg.status_code == 200
    assert stale_epg.json()["totalCount"] == "0"


def test_subnet_without_dn_uses_bracketed_ip_rn(client):
    resp = client.post(
        "/api/mo/uni/tn-RnT3.json",
        json={
            "fvTenant": {
                "attributes": {"name": "RnT3", "dn": "uni/tn-RnT3"},
                "children": [
                    {
                        "fvBD": {
                            "attributes": {"name": "bd1"},
                            "children": [
                                {"fvSubnet": {"attributes": {"ip": "10.20.30.1/24"}}}
                            ],
                        }
                    }
                ],
            }
        },
    )
    assert resp.status_code == 200

    subnet = client.get("/api/mo/uni/tn-RnT3/BD-bd1/subnet-[10.20.30.1/24].json")
    assert subnet.status_code == 200
    assert subnet.json()["imdata"][0]["fvSubnet"]["attributes"]["ip"] == "10.20.30.1/24"


def test_dnless_child_then_explicit_canonical_dn_does_not_duplicate(client):
    # First POST without dn (synthesizes canonical dn), then POST again with
    # the explicit canonical dn — must upsert the SAME object, not duplicate.
    resp1 = client.post(
        "/api/mo/uni/tn-RnT4.json",
        json={
            "fvTenant": {
                "attributes": {"name": "RnT4", "dn": "uni/tn-RnT4"},
                "children": [{"fvBD": {"attributes": {"name": "bd1"}}}],
            }
        },
    )
    assert resp1.status_code == 200

    resp2 = client.post(
        "/api/mo/uni/tn-RnT4/BD-bd1.json",
        json={
            "fvBD": {
                "attributes": {
                    "dn": "uni/tn-RnT4/BD-bd1",
                    "name": "bd1",
                    "descr": "explicit dn write",
                }
            }
        },
    )
    assert resp2.status_code == 200

    cls_resp = client.get('/api/class/fvBD.json?query-target-filter=eq(fvBD.name,"bd1")')
    assert cls_resp.status_code == 200
    matches = [
        i for i in cls_resp.json()["imdata"] if i["fvBD"]["attributes"]["dn"] == "uni/tn-RnT4/BD-bd1"
    ]
    assert len(matches) == 1
    assert matches[0]["fvBD"]["attributes"]["descr"] == "explicit dn write"


# -- PR-2: valid multi-level write still works end-to-end --


def test_valid_multilevel_write_reads_back_every_level(client):
    resp = client.post(
        "/api/mo/uni/tn-FullT.json",
        json={
            "fvTenant": {
                "attributes": {"name": "FullT", "dn": "uni/tn-FullT"},
                "children": [
                    {
                        "fvCtx": {
                            "attributes": {"name": "vrf1"},
                        }
                    },
                    {
                        "fvBD": {
                            "attributes": {"name": "bd1"},
                            "children": [
                                {"fvRsCtx": {"attributes": {"tnFvCtxName": "vrf1"}}},
                                {"fvSubnet": {"attributes": {"ip": "192.168.1.1/24"}}},
                            ],
                        }
                    },
                    {
                        "fvAp": {
                            "attributes": {"name": "ap1"},
                            "children": [
                                {
                                    "fvAEPg": {
                                        "attributes": {"name": "epg1"},
                                        "children": [
                                            {"fvRsBd": {"attributes": {"tnFvBDName": "bd1"}}}
                                        ],
                                    }
                                }
                            ],
                        }
                    },
                ],
            }
        },
    )
    assert resp.status_code == 200

    tenant = client.get("/api/mo/uni/tn-FullT.json")
    assert tenant.status_code == 200

    ctx = client.get("/api/mo/uni/tn-FullT/ctx-vrf1.json")
    assert ctx.status_code == 200
    assert ctx.json()["imdata"][0]["fvCtx"]["attributes"]["name"] == "vrf1"

    bd = client.get("/api/mo/uni/tn-FullT/BD-bd1.json")
    assert bd.status_code == 200

    rsctx = client.get("/api/mo/uni/tn-FullT/BD-bd1/rsctx.json")
    assert rsctx.status_code == 200
    assert rsctx.json()["imdata"][0]["fvRsCtx"]["attributes"]["tnFvCtxName"] == "vrf1"

    subnet = client.get("/api/mo/uni/tn-FullT/BD-bd1/subnet-[192.168.1.1/24].json")
    assert subnet.status_code == 200

    ap = client.get("/api/mo/uni/tn-FullT/ap-ap1.json")
    assert ap.status_code == 200

    epg = client.get("/api/mo/uni/tn-FullT/ap-ap1/epg-epg1.json")
    assert epg.status_code == 200

    rsbd = client.get("/api/mo/uni/tn-FullT/ap-ap1/epg-epg1/rsbd.json")
    assert rsbd.status_code == 200
    assert rsbd.json()["imdata"][0]["fvRsBd"]["attributes"]["tnFvBDName"] == "bd1"


# -- fvBD create-time defaults (fidelity fix: real APIC fills object defaults
# -- on CREATE so a sparse POST still reads back full BD-detail attributes) --


def test_posted_bd_gets_create_time_defaults(client):
    resp = client.post(
        "/api/mo/uni/tn-BdDefT.json",
        json={
            "fvTenant": {
                "attributes": {"name": "BdDefT", "dn": "uni/tn-BdDefT"},
                "children": [
                    {
                        "fvBD": {
                            "attributes": {
                                "name": "bd1",
                                "unicastRoute": "yes",
                                "unkMacUcastAct": "proxy",
                            }
                        }
                    }
                ],
            }
        },
    )
    assert resp.status_code == 200

    bd = client.get("/api/mo/uni/tn-BdDefT/BD-bd1.json")
    assert bd.status_code == 200
    attrs = bd.json()["imdata"][0]["fvBD"]["attributes"]
    assert attrs["epMoveDetectMode"] == ""
    assert attrs["hostBasedRouting"] == "no"
    assert attrs["arpFlood"] == "no"
    assert attrs["unkMcastAct"] == "flood"
    assert attrs["v6unkMcastAct"] == "nd"
    assert attrs["multiDstPktAct"] == "bd-flood"
    assert attrs["limitIpLearnToSubnets"] == "yes"
    assert attrs["ipLearning"] == "yes"
    assert attrs["mac"] == "00:22:BD:F8:19:FF"
    assert attrs["type"] == "regular"
    # Posted attrs still present alongside the filled-in defaults.
    assert attrs["unicastRoute"] == "yes"
    assert attrs["unkMacUcastAct"] == "proxy"


def test_posted_bd_explicit_attr_wins_over_default(client):
    resp = client.post(
        "/api/mo/uni/tn-BdDefT2.json",
        json={
            "fvTenant": {
                "attributes": {"name": "BdDefT2", "dn": "uni/tn-BdDefT2"},
                "children": [
                    {"fvBD": {"attributes": {"name": "bd1", "arpFlood": "yes"}}}
                ],
            }
        },
    )
    assert resp.status_code == 200

    bd = client.get("/api/mo/uni/tn-BdDefT2/BD-bd1.json")
    assert bd.status_code == 200
    assert bd.json()["imdata"][0]["fvBD"]["attributes"]["arpFlood"] == "yes"


def test_bd_partial_update_does_not_reset_to_defaults(client):
    # Create with an explicit non-default arpFlood.
    resp1 = client.post(
        "/api/mo/uni/tn-BdDefT3.json",
        json={
            "fvTenant": {
                "attributes": {"name": "BdDefT3", "dn": "uni/tn-BdDefT3"},
                "children": [
                    {"fvBD": {"attributes": {"name": "bd1", "arpFlood": "yes"}}}
                ],
            }
        },
    )
    assert resp1.status_code == 200

    # Re-POST the SAME dn without arpFlood — a partial update that changes
    # only descr. Real APIC preserves the prior arpFlood="yes"; it must NOT
    # be reset to the create-time default "no".
    resp2 = client.post(
        "/api/mo/uni/tn-BdDefT3/BD-bd1.json",
        json={
            "fvBD": {
                "attributes": {
                    "dn": "uni/tn-BdDefT3/BD-bd1",
                    "name": "bd1",
                    "descr": "partial update",
                }
            }
        },
    )
    assert resp2.status_code == 200

    bd = client.get("/api/mo/uni/tn-BdDefT3/BD-bd1.json")
    assert bd.status_code == 200
    attrs = bd.json()["imdata"][0]["fvBD"]["attributes"]
    assert attrs["arpFlood"] == "yes"
    assert attrs["descr"] == "partial update"


def test_deleted_bd_is_removed_not_resurrected_with_defaults(client):
    resp1 = client.post(
        "/api/mo/uni/tn-BdDefT4.json",
        json={
            "fvTenant": {
                "attributes": {"name": "BdDefT4", "dn": "uni/tn-BdDefT4"},
                "children": [{"fvBD": {"attributes": {"name": "bd1"}}}],
            }
        },
    )
    assert resp1.status_code == 200

    resp2 = client.post(
        "/api/mo/uni/tn-BdDefT4/BD-bd1.json",
        json={
            "fvBD": {
                "attributes": {
                    "dn": "uni/tn-BdDefT4/BD-bd1",
                    "status": "deleted",
                }
            }
        },
    )
    assert resp2.status_code == 200

    gone = client.get("/api/mo/uni/tn-BdDefT4/BD-bd1.json")
    assert gone.status_code == 200
    assert gone.json()["totalCount"] == "0"
