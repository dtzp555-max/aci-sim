"""Tests for mit/ — dn, mo, store."""

import copy

from aci_sim.mit.dn import (
    dn_class_hint,
    dn_split,
    name_from_dn,
    parent_dn,
    pod_from_dn,
    rn_of,
)
from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore

# ---------------------------------------------------------------------------
# dn.py
# ---------------------------------------------------------------------------


class TestDnSplit:
    def test_simple_three_parts(self):
        assert dn_split("uni/tn-T/BD-b") == ["uni", "tn-T", "BD-b"]

    def test_subnet_bracket(self):
        # CONTRACT example: slash inside brackets must NOT split
        assert dn_split("uni/tn-T/BD-b/subnet-[10.0.0.1/24]") == [
            "uni", "tn-T", "BD-b", "subnet-[10.0.0.1/24]"
        ]

    def test_pathep_bracket(self):
        # CONTRACT example
        assert dn_split("uni/tn-T/pathep-[eth1/9]") == [
            "uni", "tn-T", "pathep-[eth1/9]"
        ]

    def test_rsdomatt_bracket(self):
        # CONTRACT example: bracket value itself contains a slash
        assert dn_split("uni/phys-X/rsdomAtt-[uni/phys-X]") == [
            "uni", "phys-X", "rsdomAtt-[uni/phys-X]"
        ]

    def test_nested_brackets(self):
        # e.g. rslldpIfAtt-[pathep-[eth1/9]] — nested brackets, one slash inside
        parts = dn_split("uni/infra/rslldpIfAtt-[pathep-[eth1/9]]")
        assert parts == ["uni", "infra", "rslldpIfAtt-[pathep-[eth1/9]]"]

    def test_single_component(self):
        assert dn_split("uni") == ["uni"]

    def test_empty(self):
        assert dn_split("") == []

    def test_no_brackets(self):
        assert dn_split("a/b/c/d") == ["a", "b", "c", "d"]


class TestParentDn:
    def test_three_deep(self):
        assert parent_dn("uni/tn-T/BD-b") == "uni/tn-T"

    def test_top_level(self):
        assert parent_dn("uni") == ""

    def test_bracket_rn(self):
        # parent of a bracket RN is the surrounding path
        assert parent_dn("uni/tn-T/BD-b/subnet-[10.0.0.1/24]") == "uni/tn-T/BD-b"


class TestRnOf:
    def test_basic(self):
        assert rn_of("uni/tn-T/BD-b") == "BD-b"

    def test_bracket(self):
        assert rn_of("uni/tn-T/BD-b/subnet-[10.0.0.1/24]") == "subnet-[10.0.0.1/24]"

    def test_single(self):
        assert rn_of("uni") == "uni"


class TestNameFromDn:
    def test_plain(self):
        assert name_from_dn("uni/tn-T") == "T"

    def test_bd(self):
        assert name_from_dn("uni/tn-T/BD-mybd") == "mybd"

    def test_subnet_bracket(self):
        assert name_from_dn("uni/tn-T/BD-b/subnet-[10.0.0.1/24]") == "10.0.0.1/24"

    def test_pathep_bracket(self):
        # pathep-[eth1/9] → eth1/9
        assert name_from_dn("topology/pod-1/node-101/local/svc-x/pathep-[eth1/9]") == "eth1/9"

    def test_rsdomatt_bracket(self):
        assert name_from_dn("uni/phys-X/rsdomAtt-[uni/phys-X]") == "uni/phys-X"

    def test_no_dash(self):
        assert name_from_dn("uni") == "uni"


class TestPodFromDn:
    def test_pod_1(self):
        assert pod_from_dn("topology/pod-1/node-101") == "1"

    def test_pod_2(self):
        assert pod_from_dn("topology/pod-2/node-201/sys") == "2"

    def test_no_pod_default_1(self):
        assert pod_from_dn("uni/tn-T") == "1"

    def test_pod_at_end(self):
        assert pod_from_dn("topology/pod-3") == "3"

    def test_pod_prefix_dn(self):
        assert pod_from_dn("topology/pod-10/node-1001/sys/health") == "10"


class TestDnClassHint:
    def test_tn(self):
        assert dn_class_hint("tn-T") == "fvTenant"

    def test_uni(self):
        assert dn_class_hint("uni") == "polUni"

    def test_unknown(self):
        assert dn_class_hint("foo-bar") is None


# ---------------------------------------------------------------------------
# mo.py
# ---------------------------------------------------------------------------


class TestMO:
    def test_construction(self):
        mo = MO("fvTenant", dn="uni/tn-T", name="T")
        assert mo.class_name == "fvTenant"
        assert mo.dn == "uni/tn-T"
        assert mo.attrs["name"] == "T"

    def test_add_child(self):
        parent = MO("fvTenant", dn="uni/tn-T")
        child = MO("fvCtx", dn="uni/tn-T/ctx-v1", name="v1")
        parent.add_child(child)
        assert parent.children == [child]

    def test_flat_depth_first(self):
        root = MO("fvTenant", dn="uni/tn-T")
        c1 = MO("fvCtx", dn="uni/tn-T/ctx-v1")
        c2 = MO("fvBD", dn="uni/tn-T/BD-b")
        c2a = MO("fvSubnet", dn="uni/tn-T/BD-b/subnet-[10.0.0.1/24]")
        root.add_child(c1)
        root.add_child(c2)
        c2.add_child(c2a)
        assert root.flat() == [root, c1, c2, c2a]

    def test_to_imdata_no_children(self):
        mo = MO("fvTenant", dn="uni/tn-T", name="T")
        d = mo.to_imdata()
        assert list(d.keys()) == ["fvTenant"]
        assert "children" not in d["fvTenant"]
        assert d["fvTenant"]["attributes"]["dn"] == "uni/tn-T"

    def test_to_imdata_with_children(self):
        parent = MO("fvTenant", dn="uni/tn-T")
        child = MO("fvCtx", dn="uni/tn-T/ctx-v1", name="v1")
        parent.add_child(child)
        d = parent.to_imdata()
        assert "children" in d["fvTenant"]
        child_d = d["fvTenant"]["children"][0]
        assert "fvCtx" in child_d
        assert child_d["fvCtx"]["attributes"]["name"] == "v1"

    def test_to_imdata_attrs_are_copy(self):
        mo = MO("fvTenant", dn="uni/tn-T", name="T")
        d = mo.to_imdata()
        d["fvTenant"]["attributes"]["name"] = "mutated"
        assert mo.attrs["name"] == "T"  # original unchanged


# ---------------------------------------------------------------------------
# store.py
# ---------------------------------------------------------------------------


def _flat_store() -> MITStore:
    """Return a store populated with a small tree (all MOs added individually)."""
    s = MITStore()
    s.add(MO("fvTenant", dn="uni/tn-T", name="T"))
    s.add(MO("fvCtx", dn="uni/tn-T/ctx-v1", name="v1"))
    s.add(MO("fvBD", dn="uni/tn-T/BD-b", name="b"))
    s.add(MO("fvSubnet", dn="uni/tn-T/BD-b/subnet-[10.0.0.1/24]", ip="10.0.0.1/24"))
    return s


class TestMITStoreGet:
    def test_get_existing(self):
        s = _flat_store()
        mo = s.get("uni/tn-T/ctx-v1")
        assert mo is not None
        assert mo.class_name == "fvCtx"

    def test_get_missing(self):
        assert _flat_store().get("uni/tn-X") is None

    def test_get_bracket_dn(self):
        s = _flat_store()
        mo = s.get("uni/tn-T/BD-b/subnet-[10.0.0.1/24]")
        assert mo is not None
        assert mo.attrs["ip"] == "10.0.0.1/24"


class TestMITStoreChildren:
    def test_children_of_tenant(self):
        s = _flat_store()
        kids = s.children("uni/tn-T")
        dns = {mo.dn for mo in kids}
        assert "uni/tn-T/ctx-v1" in dns
        assert "uni/tn-T/BD-b" in dns

    def test_children_class_filter(self):
        s = _flat_store()
        ctxs = s.children("uni/tn-T", classes={"fvCtx"})
        assert len(ctxs) == 1
        assert ctxs[0].class_name == "fvCtx"

    def test_children_leaf_node_empty(self):
        s = _flat_store()
        assert s.children("uni/tn-T/BD-b/subnet-[10.0.0.1/24]") == []


class TestMITStoreSubtree:
    def test_all_descendants(self):
        s = _flat_store()
        desc = s.subtree("uni/tn-T")
        dns = {mo.dn for mo in desc}
        assert "uni/tn-T/ctx-v1" in dns
        assert "uni/tn-T/BD-b" in dns
        assert "uni/tn-T/BD-b/subnet-[10.0.0.1/24]" in dns
        # Root itself must NOT be included
        assert "uni/tn-T" not in dns

    def test_class_filter(self):
        s = _flat_store()
        subs = s.subtree("uni/tn-T", classes={"fvSubnet"})
        assert len(subs) == 1
        assert subs[0].class_name == "fvSubnet"

    def test_class_filter_traverses_full_tree(self):
        """A class filter must still find deeply-nested matches."""
        s = _flat_store()
        # fvSubnet is 3 levels below uni/tn-T; asking for it from the top works
        subs = s.subtree("uni/tn-T", classes={"fvSubnet"})
        assert len(subs) == 1

    def test_empty_subtree(self):
        s = _flat_store()
        assert s.subtree("uni/tn-T/BD-b/subnet-[10.0.0.1/24]") == []


class TestMITStoreByClass:
    def test_by_class_present(self):
        s = _flat_store()
        bds = s.by_class("fvBD")
        assert len(bds) == 1
        assert bds[0].dn == "uni/tn-T/BD-b"

    def test_by_class_absent(self):
        assert _flat_store().by_class("fvAp") == []


class TestMITStoreAll:
    def test_insertion_order(self):
        s = _flat_store()
        dns = [mo.dn for mo in s.all()]
        assert dns == [
            "uni/tn-T",
            "uni/tn-T/ctx-v1",
            "uni/tn-T/BD-b",
            "uni/tn-T/BD-b/subnet-[10.0.0.1/24]",
        ]


class TestMITStoreUpsert:
    def test_upsert_merges_attrs(self):
        s = _flat_store()
        s.upsert(MO("fvTenant", dn="uni/tn-T", name="T", descr="new-desc"))
        mo = s.get("uni/tn-T")
        assert mo.attrs["descr"] == "new-desc"
        assert mo.attrs["name"] == "T"   # original attr preserved

    def test_upsert_inserts_new(self):
        s = _flat_store()
        s.upsert(MO("fvTenant", dn="uni/tn-T2", name="T2"))
        assert s.get("uni/tn-T2") is not None

    def test_upsert_recurses_children(self):
        s = MITStore()
        root = MO("fvTenant", dn="uni/tn-T")
        child = MO("fvCtx", dn="uni/tn-T/ctx-v1", name="v1")
        root.add_child(child)
        s.upsert(root)
        assert s.get("uni/tn-T") is not None
        assert s.get("uni/tn-T/ctx-v1") is not None

    def test_upsert_deleted_removes_dn(self):
        s = _flat_store()
        s.upsert(MO("fvBD", dn="uni/tn-T/BD-b", status="deleted"))
        assert s.get("uni/tn-T/BD-b") is None

    def test_upsert_deleted_removes_subtree(self):
        s = _flat_store()
        s.upsert(MO("fvBD", dn="uni/tn-T/BD-b", status="deleted"))
        assert s.get("uni/tn-T/BD-b/subnet-[10.0.0.1/24]") is None

    def test_upsert_deleted_keeps_siblings(self):
        s = _flat_store()
        s.upsert(MO("fvBD", dn="uni/tn-T/BD-b", status="deleted"))
        assert s.get("uni/tn-T") is not None
        assert s.get("uni/tn-T/ctx-v1") is not None


class TestMITStoreDeepCopy:
    def test_deepcopy_isolates(self):
        s = _flat_store()
        s2 = copy.deepcopy(s)
        s.upsert(MO("fvTenant", dn="uni/tn-T", name="mutated"))
        assert s2.get("uni/tn-T").attrs["name"] == "T"

    def test_deepcopy_is_complete(self):
        s = _flat_store()
        s2 = copy.deepcopy(s)
        assert {mo.dn for mo in s2.all()} == {mo.dn for mo in s.all()}
