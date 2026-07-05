"""Regression tests for P1 review findings F1 (tree-add children leak/dup) and
F2 (rsp-subtree=children must be one level, full must be recursive).

Builders add MOs as TREES (MO.add_child), the exact pattern the original
flat-only tests never exercised.
"""

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.query.engine import QueryParams, run_class_query, run_mo_query


def _tenant_tree() -> MO:
    """uni/tn-T ▷ BD-b ▷ subnet-[10.0.0.1/24]  (a 3-level tree)."""
    tn = MO("fvTenant", dn="uni/tn-T", name="T")
    bd = MO("fvBD", dn="uni/tn-T/BD-b", name="b")
    sn = MO("fvSubnet", dn="uni/tn-T/BD-b/subnet-[10.0.0.1/24]", ip="10.0.0.1/24")
    bd.add_child(sn)
    tn.add_child(bd)
    return tn


# --- F1: adding a tree must not leak/duplicate children ---------------------


def test_tree_add_no_children_leak_on_nonsubtree_class_query():
    s = MITStore()
    s.add(_tenant_tree())
    imdata, total = run_class_query(s, "fvBD", QueryParams())  # no subtree requested
    assert total == 1
    assert "children" not in imdata[0]["fvBD"], "children must be absent without a subtree mode (CONTRACT §2)"


def test_tree_add_no_children_leak_on_nonsubtree_mo_query():
    s = MITStore()
    s.add(_tenant_tree())
    imdata, _ = run_mo_query(s, "uni/tn-T", QueryParams())
    assert "children" not in imdata[0]["fvTenant"]


def test_tree_add_subtree_query_lists_each_descendant_once():
    s = MITStore()
    s.add(_tenant_tree())
    imdata, _ = run_mo_query(s, "uni/tn-T", QueryParams(query_target="subtree"))
    # Flatten every dn that appears anywhere in the imdata payload.
    seen: list[str] = []

    def walk(entry: dict):
        for _cls, body in entry.items():
            dn = body["attributes"].get("dn")
            if dn:
                seen.append(dn)
            for child in body.get("children", []):
                walk(child)

    for entry in imdata:
        walk(entry)
    # root + bd + subnet, each exactly once
    assert seen.count("uni/tn-T") == 1
    assert seen.count("uni/tn-T/BD-b") == 1
    assert seen.count("uni/tn-T/BD-b/subnet-[10.0.0.1/24]") == 1


# --- F2: children == one level, full == recursive ---------------------------


def _child_classes(imdata_entry: dict) -> set[str]:
    body = next(iter(imdata_entry.values()))
    return {next(iter(c)) for c in body.get("children", [])}


def test_rsp_subtree_children_is_one_level():
    s = MITStore()
    s.add(_tenant_tree())
    imdata, _ = run_class_query(s, "fvTenant", QueryParams(rsp_subtree="children"))
    classes = _child_classes(imdata[0])
    assert "fvBD" in classes
    assert "fvSubnet" not in classes, "children mode is direct children only"


def test_rsp_subtree_full_is_recursive():
    s = MITStore()
    s.add(_tenant_tree())
    imdata, _ = run_class_query(s, "fvTenant", QueryParams(rsp_subtree="full"))
    tn = imdata[0]["fvTenant"]
    # full mode returns the WHOLE subtree, and it stays NESTED — the grandchild
    # (fvSubnet) hangs under its parent (fvBD), NOT flattened onto the tenant.
    # (B1 regression: flattening silently emptied autoACI's two-level readers
    # like contract_map vzBrCP→vzSubj→vzRsSubjFiltAtt.)
    assert _child_classes(imdata[0]) == {"fvBD"}, "grandchildren must not be hoisted to the root"
    bd = tn["children"][0]["fvBD"]
    assert {next(iter(c)) for c in bd["children"]} == {"fvSubnet"}, "full mode is recursive"


# --- Legacy query-target=subtree must be FLAT top-level (P3 review Finding 1) ----


def _bgp_inst_tree() -> MO:
    """spine sys/bgp/inst ▷ dom-overlay-1 ▷ peer (ISN /32) — the real ISN shape."""
    inst = MO("bgpInst", dn="topology/pod-1/node-201/sys/bgp/inst", asn="65001")
    dom = MO("bgpDom", dn="topology/pod-1/node-201/sys/bgp/inst/dom-overlay-1", name="overlay-1")
    peer = MO(
        "bgpPeer",
        dn="topology/pod-1/node-201/sys/bgp/inst/dom-overlay-1/peer-[10.1.401.1]",
        addr="10.1.401.1/32",
        asn="65002",
    )
    dom.add_child(peer)
    inst.add_child(dom)
    return inst


def test_legacy_subtree_returns_target_class_flat_at_top_level():
    """autoACI's query_dn(subtree=True, subtree_class="bgpPeer") reads item.get('bgpPeer')
    at the TOP level of imdata — the ISN detection depends on this."""
    s = MITStore()
    s.add(_bgp_inst_tree())
    imdata, total = run_mo_query(
        s,
        "topology/pod-1/node-201/sys/bgp/inst",
        QueryParams(query_target="subtree", target_subtree_class=["bgpPeer"]),
    )
    assert total == 1
    assert list(imdata[0].keys()) == ["bgpPeer"], "legacy subtree = class flat at top level, not nested"
    assert imdata[0]["bgpPeer"]["attributes"]["addr"] == "10.1.401.1/32"
    assert "children" not in imdata[0]["bgpPeer"]
    assert all("bgpInst" not in e for e in imdata), "root excluded when it doesn't match the class filter"


def test_legacy_subtree_no_class_filter_is_flat_root_plus_descendants():
    s = MITStore()
    s.add(_bgp_inst_tree())
    imdata, total = run_mo_query(
        s,
        "topology/pod-1/node-201/sys/bgp/inst",
        QueryParams(query_target="subtree"),
    )
    classes = {next(iter(e)) for e in imdata}
    assert classes == {"bgpInst", "bgpDom", "bgpPeer"}
    assert total == 3
    for e in imdata:
        assert "children" not in next(iter(e.values())), "legacy subtree is flat, never nested"


# --- Store hardening: subtree traverses MO-less structural intermediates ------
# (ep_tracker queries epmMacEp via a node/sys subtree; the epm containers
#  ctx-[]/bd-[]/db-ep have no MO of their own — reachability must still hold.)


def test_subtree_reaches_deep_mo_without_intermediate_mos():
    s = MITStore()
    node_sys = "topology/pod-1/node-101/sys"
    s.add(MO("topSystem", dn=node_sys, role="leaf"))
    deep = f"{node_sys}/ctx-[vxlan-2900001]/bd-[vxlan-15000001]/db-ep/mac-00:50:56:00:00:01"
    s.add(MO("epmMacEp", dn=deep, addr="00:50:56:00:00:01"))
    found = s.subtree(node_sys, classes={"epmMacEp"})
    assert [m.dn for m in found] == [deep], "deep MO reachable via subtree despite MO-less containers"
    # the MO-less containers are NOT surfaced as direct children
    assert s.children(node_sys) == []


def test_legacy_subtree_reaches_deep_epm_via_engine():
    s = MITStore()
    node_sys = "topology/pod-1/node-101/sys"
    s.add(MO("topSystem", dn=node_sys, role="leaf"))
    deep = f"{node_sys}/ctx-[vxlan-2900001]/bd-[vxlan-15000001]/db-ep/mac-00:50:56:00:00:01"
    s.add(MO("epmMacEp", dn=deep, addr="00:50:56:00:00:01"))
    imdata, total = run_mo_query(
        s, node_sys, QueryParams(query_target="subtree", target_subtree_class=["epmMacEp"])
    )
    assert total == 1
    assert imdata[0]["epmMacEp"]["attributes"]["addr"] == "00:50:56:00:00:01"
