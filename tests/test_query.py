"""Tests for query/ — filters and engine."""

import pytest
from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.query.filters import FilterParseError, parse_filter
from aci_sim.query.engine import (
    QueryParams,
    params_from_dict,
    run_class_query,
    run_mo_query,
    run_node_scoped,
)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


def _make_store() -> MITStore:
    """Minimal but representative store covering all query shapes."""
    s = MITStore()

    # Tenants
    for name in ["T1", "T2", "T3"]:
        s.add(MO("fvTenant", dn=f"uni/tn-{name}", name=name))

    # BDs + subnets under T1
    for bd_name in ["bd1", "bd2"]:
        s.add(MO("fvBD", dn=f"uni/tn-T1/BD-{bd_name}", name=bd_name))
        s.add(MO(
            "fvSubnet",
            dn=f"uni/tn-T1/BD-{bd_name}/subnet-[10.0.0.1/24]",
            ip="10.0.0.1/24",
        ))

    # Fabric nodes
    for node_id, role in [("101", "spine"), ("102", "spine"), ("103", "leaf"), ("104", "leaf")]:
        s.add(MO(
            "fabricNode",
            dn=f"topology/pod-1/node-{node_id}",
            id=node_id,
            role=role,
            name=f"node-{node_id}",
        ))

    # Node-local faults
    s.add(MO(
        "faultInst",
        dn="topology/pod-1/node-103/local/svc-x/fault-F0001",
        severity="critical",
        code="F0001",
    ))
    s.add(MO(
        "faultInst",
        dn="topology/pod-1/node-104/local/svc-y/fault-F0002",
        severity="major",
        code="F0002",
    ))

    return s


# ---------------------------------------------------------------------------
# filters.py
# ---------------------------------------------------------------------------


class TestFilters:
    def setup_method(self):
        self.s = _make_store()

    # -- eq --

    def test_eq_spine(self):
        pred = parse_filter('eq(fabricNode.role,"spine")')
        spines = [mo for mo in self.s.by_class("fabricNode") if pred(mo)]
        assert len(spines) == 2
        assert all(mo.attrs["role"] == "spine" for mo in spines)

    def test_eq_no_match(self):
        pred = parse_filter('eq(fabricNode.role,"controller")')
        assert [mo for mo in self.s.by_class("fabricNode") if pred(mo)] == []

    def test_eq_dn(self):
        pred = parse_filter('eq(fvTenant.dn,"uni/tn-T1")')
        result = [mo for mo in self.s.by_class("fvTenant") if pred(mo)]
        assert len(result) == 1
        assert result[0].dn == "uni/tn-T1"

    # -- wcard --

    def test_wcard_dn_substring(self):
        pred = parse_filter('wcard(fvBD.dn,"tn-T1/")')
        bds = [mo for mo in self.s.by_class("fvBD") if pred(mo)]
        assert len(bds) == 2

    def test_wcard_no_match(self):
        pred = parse_filter('wcard(fvBD.dn,"tn-T2/")')
        assert [mo for mo in self.s.by_class("fvBD") if pred(mo)] == []

    # -- and --

    def test_and_filter(self):
        pred = parse_filter('and(eq(fvBD.name,"bd1"),wcard(fvBD.dn,"tn-T1/"))')
        bds = [mo for mo in self.s.by_class("fvBD") if pred(mo)]
        assert len(bds) == 1
        assert bds[0].attrs["name"] == "bd1"

    # -- or --

    def test_or_filter(self):
        pred = parse_filter(
            'or(eq(faultInst.severity,"critical"),eq(faultInst.severity,"major"))'
        )
        faults = [mo for mo in self.s.by_class("faultInst") if pred(mo)]
        assert len(faults) == 2

    # -- nested --

    def test_nested_and_or(self):
        pred = parse_filter(
            'and('
            '  or(eq(fabricNode.role,"spine"),eq(fabricNode.role,"leaf")),'
            '  eq(fabricNode.id,"101")'
            ')'
        )
        nodes = [mo for mo in self.s.by_class("fabricNode") if pred(mo)]
        assert len(nodes) == 1
        assert nodes[0].attrs["id"] == "101"

    def test_deeply_nested_or_inside_and(self):
        # Exactly mirrors the APIC example wcard(fvCtx.dn,"tn-Tenant1/")
        pred = parse_filter('wcard(fvTenant.dn,"tn-T")')
        all_tenants = self.s.by_class("fvTenant")
        result = [mo for mo in all_tenants if pred(mo)]
        assert len(result) == 3  # T1, T2, T3 all contain "tn-T"

    # -- edge cases --

    def test_unknown_attr_is_false(self):
        pred = parse_filter('eq(fvBD.nonexistent,"x")')
        assert not any(pred(mo) for mo in self.s.by_class("fvBD"))

    def test_empty_expr_matches_all(self):
        pred = parse_filter("")
        mo = self.s.get("uni/tn-T1")
        assert pred(mo) is True

    def test_none_expr_matches_all(self):
        pred = parse_filter(None)
        mo = self.s.get("uni/tn-T1")
        assert pred(mo) is True


# ---------------------------------------------------------------------------
# engine.py — QueryParams / params_from_dict
# ---------------------------------------------------------------------------


class TestParamsFromDict:
    def test_basic_filter_and_page(self):
        p = params_from_dict({
            "query-target-filter": 'eq(fabricNode.role,"spine")',
            "page-size": "100",
            "page": "2",
        })
        assert p.filter_expr == 'eq(fabricNode.role,"spine")'
        assert p.page_size == 100
        assert p.page == 2

    def test_rsp_subtree_class_split(self):
        p = params_from_dict({
            "rsp-subtree": "children",
            "rsp-subtree-class": "fvBD,fvSubnet",
        })
        assert p.rsp_subtree == "children"
        assert p.rsp_subtree_class == ["fvBD", "fvSubnet"]

    def test_legacy_query_target_subtree(self):
        p = params_from_dict({
            "query-target": "subtree",
            "target-subtree-class": "bgpPeer",
        })
        assert p.query_target == "subtree"
        assert p.target_subtree_class == ["bgpPeer"]

    def test_page_size_clamped_to_5000(self):
        p = params_from_dict({"page-size": "99999"})
        assert p.page_size == 5000

    def test_defaults(self):
        p = params_from_dict({})
        assert p.filter_expr is None
        assert p.rsp_subtree is None
        assert p.page == 0
        assert p.page_size == 500

    # -- defensive page/page-size parsing (PR-1 F2 regression) --

    def test_non_numeric_page_raises(self):
        with pytest.raises(FilterParseError):
            params_from_dict({"page": "abc"})

    def test_non_numeric_page_size_raises(self):
        with pytest.raises(FilterParseError):
            params_from_dict({"page-size": "abc"})

    def test_negative_page_size_raises(self):
        with pytest.raises(FilterParseError):
            params_from_dict({"page-size": "-1"})

    def test_zero_page_size_raises(self):
        with pytest.raises(FilterParseError):
            params_from_dict({"page-size": "0"})

    def test_negative_page_raises(self):
        with pytest.raises(FilterParseError):
            params_from_dict({"page": "-1"})


# ---------------------------------------------------------------------------
# engine.py — run_class_query
# ---------------------------------------------------------------------------


class TestRunClassQuery:
    def setup_method(self):
        self.s = _make_store()

    def test_basic_all_tenants(self):
        imdata, total = run_class_query(self.s, "fvTenant", QueryParams())
        assert total == 3
        assert len(imdata) == 3
        assert all("fvTenant" in d for d in imdata)

    def test_with_eq_filter(self):
        params = QueryParams(filter_expr='eq(fabricNode.role,"spine")')
        imdata, total = run_class_query(self.s, "fabricNode", params)
        assert total == 2
        assert len(imdata) == 2

    def test_no_children_by_default(self):
        imdata, _ = run_class_query(self.s, "fvBD", QueryParams())
        for item in imdata:
            body = list(item.values())[0]
            assert "children" not in body

    # -- pagination --

    def test_pagination_page0(self):
        params = QueryParams(page=0, page_size=2)
        imdata, total = run_class_query(self.s, "fvTenant", params)
        assert total == 3
        assert len(imdata) == 2

    def test_pagination_page1(self):
        params = QueryParams(page=1, page_size=2)
        imdata, total = run_class_query(self.s, "fvTenant", params)
        assert total == 3
        assert len(imdata) == 1   # 3 total → page 1 has 1 item

    def test_pagination_beyond_end(self):
        params = QueryParams(page=5, page_size=10)
        imdata, total = run_class_query(self.s, "fvTenant", params)
        assert total == 3
        assert imdata == []

    def test_total_unaffected_by_page(self):
        """total is always pre-pagination count."""
        p0 = QueryParams(page=0, page_size=1)
        p1 = QueryParams(page=1, page_size=1)
        _, total0 = run_class_query(self.s, "fvTenant", p0)
        _, total1 = run_class_query(self.s, "fvTenant", p1)
        assert total0 == total1 == 3

    # -- subtree --

    def test_rsp_subtree_children_attaches_descendants(self):
        params = QueryParams(rsp_subtree="children")
        imdata, total = run_class_query(self.s, "fvBD", params)
        assert total == 2
        for item in imdata:
            body = list(item.values())[0]
            assert "children" in body
            child_classes = {list(c.keys())[0] for c in body["children"]}
            assert "fvSubnet" in child_classes

    def test_rsp_subtree_class_filters_descendants(self):
        params = QueryParams(rsp_subtree="full", rsp_subtree_class=["fvSubnet"])
        imdata, _ = run_class_query(self.s, "fvBD", params)
        for item in imdata:
            body = list(item.values())[0]
            children = body.get("children", [])
            # Every attached descendant must be fvSubnet
            for c in children:
                assert list(c.keys())[0] == "fvSubnet"

    def test_rsp_subtree_no_match_class_no_children_key(self):
        """When rsp-subtree-class matches nothing, 'children' key absent."""
        params = QueryParams(rsp_subtree="full", rsp_subtree_class=["fvAp"])
        imdata, _ = run_class_query(self.s, "fvBD", params)
        for item in imdata:
            body = list(item.values())[0]
            assert "children" not in body

    # -- envelope shape --

    def test_envelope_shape(self):
        imdata, total = run_class_query(self.s, "fabricNode", QueryParams())
        for item in imdata:
            assert len(item) == 1
            cls = list(item.keys())[0]
            assert cls == "fabricNode"
            body = item[cls]
            assert "attributes" in body
            assert "dn" in body["attributes"]


# ---------------------------------------------------------------------------
# engine.py — run_mo_query
# ---------------------------------------------------------------------------


class TestRunMoQuery:
    def setup_method(self):
        self.s = _make_store()

    def test_get_single_mo(self):
        imdata, total = run_mo_query(self.s, "uni/tn-T1", QueryParams())
        assert total == 1
        assert len(imdata) == 1
        assert "fvTenant" in imdata[0]
        assert imdata[0]["fvTenant"]["attributes"]["dn"] == "uni/tn-T1"

    def test_not_found_returns_empty(self):
        imdata, total = run_mo_query(self.s, "uni/tn-MISSING", QueryParams())
        assert total == 0
        assert imdata == []

    def test_subtree_returns_descendants_flat(self):
        # legacy query-target=subtree → FLAT top-level imdata (root + all descendants),
        # NOT nested under the root (matches real APIC + autoACI's top-level reads).
        params = QueryParams(query_target="subtree")
        imdata, total = run_mo_query(self.s, "uni/tn-T1", params)
        classes = [next(iter(e)) for e in imdata]
        assert total == 5  # fvTenant + 2 fvBD + 2 fvSubnet
        assert classes.count("fvTenant") == 1
        assert classes.count("fvBD") == 2
        assert classes.count("fvSubnet") == 2
        for e in imdata:  # flat, never nested
            assert "children" not in next(iter(e.values()))

    def test_subtree_class_filter_flat(self):
        params = QueryParams(query_target="subtree", target_subtree_class=["fvBD"])
        imdata, total = run_mo_query(self.s, "uni/tn-T1", params)
        classes = {next(iter(e)) for e in imdata}
        assert classes == {"fvBD"}  # root + subnets excluded by the class filter
        assert total == 2

    def test_subtree_class_filter_deep(self):
        """Class filter on legacy subtree reaches deeply-nested descendants, flat at top level."""
        params = QueryParams(query_target="subtree", target_subtree_class=["fvSubnet"])
        imdata, total = run_mo_query(self.s, "uni/tn-T1", params)
        classes = [next(iter(e)) for e in imdata]
        assert total == 2
        assert all(c == "fvSubnet" for c in classes)

    def test_bracket_dn(self):
        imdata, total = run_mo_query(
            self.s, "uni/tn-T1/BD-bd1/subnet-[10.0.0.1/24]", QueryParams()
        )
        assert total == 1
        assert "fvSubnet" in imdata[0]


# ---------------------------------------------------------------------------
# engine.py — run_node_scoped
# ---------------------------------------------------------------------------


class TestRunNodeScoped:
    def setup_method(self):
        self.s = _make_store()

    def test_returns_faults_under_node(self):
        imdata, total = run_node_scoped(
            self.s, "topology/pod-1/node-103", "faultInst", QueryParams()
        )
        assert total == 1
        assert imdata[0]["faultInst"]["attributes"]["code"] == "F0001"

    def test_no_match_different_node(self):
        imdata, total = run_node_scoped(
            self.s, "topology/pod-1/node-101", "faultInst", QueryParams()
        )
        assert total == 0
        assert imdata == []

    def test_with_eq_filter(self):
        params = QueryParams(filter_expr='eq(faultInst.severity,"major")')
        imdata, total = run_node_scoped(
            self.s, "topology/pod-1/node-104", "faultInst", params
        )
        assert total == 1
        assert imdata[0]["faultInst"]["attributes"]["severity"] == "major"

    def test_filter_excludes_non_matching(self):
        params = QueryParams(filter_expr='eq(faultInst.severity,"critical")')
        imdata, total = run_node_scoped(
            self.s, "topology/pod-1/node-104", "faultInst", params
        )
        assert total == 0

    def test_envelope_shape(self):
        imdata, _ = run_node_scoped(
            self.s, "topology/pod-1/node-103", "faultInst", QueryParams()
        )
        item = imdata[0]
        assert "faultInst" in item
        assert "attributes" in item["faultInst"]
        assert "dn" in item["faultInst"]["attributes"]
        assert "children" not in item["faultInst"]  # no subtree requested


# ---------------------------------------------------------------------------
# engine.py — rsp-subtree=full MUST preserve multi-level nesting (B1 regression)
# ---------------------------------------------------------------------------


class TestRspSubtreeFullNesting:
    """rsp-subtree=full must keep grandchildren nested under their parent, not
    hoist them flat onto the root. autoACI contract_map/access_policy read two
    levels (vzBrCP.children→vzSubj, vzSubj.children→vzRsSubjFiltAtt); flattening
    silently empties the filter/entry column."""

    def _contract_store(self) -> MITStore:
        s = MITStore()
        s.add(MO("vzBrCP", dn="uni/tn-T1/brc-Web", name="Web"))
        s.add(MO("vzSubj", dn="uni/tn-T1/brc-Web/subj-S1", name="S1"))
        s.add(MO(
            "vzRsSubjFiltAtt",
            dn="uni/tn-T1/brc-Web/subj-S1/rssubjFiltAtt-http",
            tnVzFilterName="http",
        ))
        s.add(MO(
            "vzRsSubjFiltAtt",
            dn="uni/tn-T1/brc-Web/subj-S1/rssubjFiltAtt-https",
            tnVzFilterName="https",
        ))
        return s

    def test_full_with_class_filter_keeps_grandchildren_nested(self):
        s = self._contract_store()
        params = QueryParams(
            rsp_subtree="full",
            rsp_subtree_class=["vzSubj", "vzRsSubjFiltAtt"],
        )
        imdata, total = run_class_query(s, "vzBrCP", params)
        assert total == 1
        brc = imdata[0]["vzBrCP"]
        # vzSubj is the ONLY direct child of vzBrCP (filter attrs never hoisted)
        assert [next(iter(c)) for c in brc["children"]] == ["vzSubj"]
        subj = brc["children"][0]["vzSubj"]
        # the two filter bindings are nested UNDER vzSubj, not under vzBrCP
        filt_classes = [next(iter(c)) for c in subj["children"]]
        assert filt_classes == ["vzRsSubjFiltAtt", "vzRsSubjFiltAtt"]

    def test_full_no_class_filter_nests_every_level(self):
        s = MITStore()
        s.add(MO("fvTenant", dn="uni/tn-T1", name="T1"))
        s.add(MO("fvBD", dn="uni/tn-T1/BD-bd1", name="bd1"))
        s.add(MO("fvSubnet", dn="uni/tn-T1/BD-bd1/subnet-[10.0.0.1/24]", ip="10.0.0.1/24"))
        imdata, _ = run_mo_query(s, "uni/tn-T1", QueryParams(rsp_subtree="full"))
        tn = imdata[0]["fvTenant"]
        assert next(iter(tn["children"][0])) == "fvBD"
        bd = tn["children"][0]["fvBD"]
        # grandchild nested under BD, not flattened onto the tenant
        assert next(iter(bd["children"][0])) == "fvSubnet"
        assert all(next(iter(c)) != "fvSubnet" for c in tn["children"])


# ---------------------------------------------------------------------------
# filters.py — comparison operators + parse errors (B2 regression)
# ---------------------------------------------------------------------------


class TestComparisonFilters:
    def setup_method(self):
        self.s = _make_store()

    def _ids(self, expr: str) -> set[str]:
        pred = parse_filter(expr)
        return {mo.attrs["id"] for mo in self.s.by_class("fabricNode") if pred(mo)}

    def test_ne(self):
        pred = parse_filter('ne(fvTenant.name,"T1")')
        names = {mo.attrs["name"] for mo in self.s.by_class("fvTenant") if pred(mo)}
        assert names == {"T2", "T3"}

    def test_gt_numeric(self):
        assert self._ids('gt(fabricNode.id,"102")') == {"103", "104"}

    def test_lt_numeric(self):
        assert self._ids('lt(fabricNode.id,"102")') == {"101"}

    def test_ge_numeric(self):
        assert self._ids('ge(fabricNode.id,"103")') == {"103", "104"}

    def test_le_numeric(self):
        assert self._ids('le(fabricNode.id,"102")') == {"101", "102"}

    def test_bw_inclusive_range(self):
        assert self._ids('bw(fabricNode.id,"102","103")') == {"102", "103"}

    def test_compare_missing_attr_is_false(self):
        assert self._ids('gt(fabricNode.nonexistent,"1")') == set()

    def test_ne_nested_in_and(self):
        assert self._ids('and(ne(fabricNode.role,"spine"),gt(fabricNode.id,"100"))') == {"103", "104"}

    def test_string_fallback_when_non_numeric(self):
        # role is not numeric → lexical compare still works, doesn't raise
        pred = parse_filter('gt(fabricNode.role,"leaf")')
        roles = {mo.attrs["role"] for mo in self.s.by_class("fabricNode") if pred(mo)}
        assert roles == {"spine"}  # "spine" > "leaf" lexically

    def test_unknown_operator_raises(self):
        from aci_sim.query.filters import FilterParseError
        with pytest.raises(FilterParseError):
            parse_filter('frobnicate(fvTenant.name,"x")')

    def test_truncated_expression_raises(self):
        from aci_sim.query.filters import FilterParseError
        with pytest.raises(FilterParseError):
            parse_filter('eq(fvTenant.name')

    # -- numeric comparison guard (kills lexical-only mutation) --

    def test_bw_numeric_wide_range_matches_all_nodes(self):
        # "150" < "99" lexically, so a lexical-only bw would empty the range;
        # numerically 99 <= id <= 150 must match every node (101-104).
        assert self._ids('bw(fabricNode.id,"99","150")') == {"101", "102", "103", "104"}

    def test_gt_numeric_low_bound_matches_all_nodes(self):
        assert self._ids('gt(fabricNode.id,"99")') == {"101", "102", "103", "104"}

    # -- strict parsing: trailing tokens / zero-arg composites (regression) --

    def test_comma_joined_double_filter_raises(self):
        with pytest.raises(FilterParseError):
            parse_filter('eq(a.b,"x"),eq(a.b,"y")')

    def test_unbalanced_trailing_paren_raises(self):
        with pytest.raises(FilterParseError):
            parse_filter('eq(a.b,"x"))')

    def test_zero_arg_or_raises(self):
        with pytest.raises(FilterParseError):
            parse_filter('or()')

    def test_zero_arg_and_raises(self):
        with pytest.raises(FilterParseError):
            parse_filter('and()')


# ---------------------------------------------------------------------------
# engine.py — subtree scope filtering (PR-1 F1 regression)
# ---------------------------------------------------------------------------


class TestSubtreeScopeFiltering:
    """query-target=subtree + query-target-filter must filter the EXPANDED
    subtree scope (root + descendants), not pre-filter the root before
    expansion. A filter on a descendant-only attribute (e.g. faultInst.severity
    under a topSystem root) must still reach matching descendants."""

    def _make_topsystem_store(self) -> MITStore:
        s = MITStore()
        s.add(MO("topSystem", dn="topology/pod-1/node-101/sys", id="101", role="leaf"))
        s.add(MO(
            "faultInst",
            dn="topology/pod-1/node-101/sys/faults/fault-F1",
            severity="minor",
            code="F1",
        ))
        s.add(MO(
            "faultInst",
            dn="topology/pod-1/node-101/sys/faults/fault-F2",
            severity="major",
            code="F2",
        ))
        return s

    def test_run_mo_query_subtree_filter_on_descendant_attr(self):
        s = self._make_topsystem_store()
        params = QueryParams(
            query_target="subtree",
            target_subtree_class=["faultInst"],
            filter_expr='eq(faultInst.severity,"minor")',
        )
        imdata, total = run_mo_query(s, "topology/pod-1/node-101/sys", params)
        assert total == 1
        assert imdata[0]["faultInst"]["attributes"]["code"] == "F1"
        assert imdata[0]["faultInst"]["attributes"]["severity"] == "minor"

    def test_run_class_query_subtree_filter_on_descendant_attr(self):
        s = self._make_topsystem_store()
        params = QueryParams(filter_expr='eq(faultInst.severity,"minor")')
        imdata, total = run_class_query(s, "faultInst", params)
        assert total == 1
        assert imdata[0]["faultInst"]["attributes"]["code"] == "F1"
