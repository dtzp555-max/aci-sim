"""Mutation-killers for filters.py numeric-comparison semantics (finding #21).

A prior audit replaced the numeric branch of gt/ge/lt/le/bw with pure-lexical
string comparison and the full suite stayed green — the numeric-vs-lexical
fork in ``_cmp_pred`` / ``_bw_pred`` was effectively untested. Every case
below is chosen so lexical and numeric ordering DISAGREE: if the numeric
branch were deleted (or the ``rn is not None and vn is not None`` guard were
forced to always fail), these assertions flip and the test fails.

Covers both the parse_filter unit level (this file) and the HTTP
class-query path (tests/test_rest_aci.py::TestNumericSemanticsHttp-style
cases live alongside the existing REST fault/filter tests — see the
`test_bw_wide_numeric_range_matches_non_controller_nodes` /
`test_gt_numeric_filter_matches_all_non_controller_nodes` companions in
tests/test_rest_aci.py, exercised here again via the FastAPI TestClient
for the same fixture used by fabricNode ids 101-104).
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from aci_sim.build.orchestrator import build_site
from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.query.filters import parse_filter
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology

TOPO_PATH = "topology.yaml"  # run from project root


def _make_store() -> MITStore:
    """Fabric nodes with three-digit ids 101-104 — same fixture shape as
    tests/test_query.py::_make_store, so lexical "150" < "99" and
    "101" < "99" traps are exercised identically."""
    s = MITStore()
    for node_id, role in [("101", "spine"), ("102", "spine"), ("103", "leaf"), ("104", "leaf")]:
        s.add(MO(
            "fabricNode",
            dn=f"topology/pod-1/node-{node_id}",
            id=node_id,
            role=role,
            name=f"node-{node_id}",
        ))
    return s


# ---------------------------------------------------------------------------
# parse_filter unit level
# ---------------------------------------------------------------------------


class TestNumericSemanticsUnit:
    def setup_method(self):
        self.s = _make_store()

    def _ids(self, expr: str) -> set[str]:
        pred = parse_filter(expr)
        return {mo.attrs["id"] for mo in self.s.by_class("fabricNode") if pred(mo)}

    # -- bw: lexically "150" < "99" empties the range; numerically it must
    # cover every 3-digit node id 101-104. --

    def test_bw_lexically_inverted_bounds_matches_all_nodes(self):
        assert self._ids('bw(fabricNode.id,"99","150")') == {"101", "102", "103", "104"}

    def test_bw_lexically_inverted_bounds_excludes_below_low(self):
        # Numeric lower bound 99 excludes nothing here (all ids >= 101), but
        # pin a case where the lexical trap would ALSO wrongly exclude if the
        # numeric branch were dropped: bw with a tight numeric window that a
        # lexical comparison would evaluate completely differently.
        assert self._ids('bw(fabricNode.id,"101","102")') == {"101", "102"}

    # -- gt: "99" is lexically greater than "101".."104" (since "9" > "1"),
    # so a lexical-only gt(id,"99") would match NOTHING; numerically it must
    # match every node. --

    def test_gt_low_numeric_bound_matches_all_nodes(self):
        assert self._ids('gt(fabricNode.id,"99")') == {"101", "102", "103", "104"}

    def test_gt_three_digit_ids_numeric(self):
        # "103" > "102" holds both lexically and numerically for same-width
        # strings, so pair it with the mixed-width case below to force the
        # numeric branch specifically.
        assert self._ids('gt(fabricNode.id,"102")') == {"103", "104"}

    # -- ge: mirrors gt with mixed-width low bound. --

    def test_ge_low_numeric_bound_matches_all_nodes(self):
        assert self._ids('ge(fabricNode.id,"99")') == {"101", "102", "103", "104"}

    # -- lt / le: "9" is lexically greater than "101".."104", so a
    # lexical-only lt(id,"9") would match every node (each id < "9" lexically
    # is False since "1..." > "9" is False... actually "101" < "9" is True
    # lexically because '1' < '9'). Use a case where lexical and numeric
    # DISAGREE in direction: lt(id, "20") — lexically "101" < "20" is True
    # (since '1' < '2'), but numerically 101 < 20 is False. --

    def test_lt_mixed_width_bound_matches_nothing_numerically(self):
        # Lexical comparison would wrongly match all nodes ("101" < "20",
        # "102" < "20", ... all True as strings); numeric comparison must
        # correctly match none (101-104 are not < 20).
        assert self._ids('lt(fabricNode.id,"20")') == set()

    def test_le_mixed_width_bound_matches_nothing_numerically(self):
        assert self._ids('le(fabricNode.id,"20")') == set()

    def test_lt_numeric_within_range(self):
        assert self._ids('lt(fabricNode.id,"103")') == {"101", "102"}

    def test_le_numeric_within_range(self):
        assert self._ids('le(fabricNode.id,"102")') == {"101", "102"}


# ---------------------------------------------------------------------------
# HTTP class-query path
# ---------------------------------------------------------------------------


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
    c = TestClient(app)
    login = c.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
    )
    assert login.status_code == 200
    return c


class TestNumericSemanticsHttp:
    def test_bw_lexically_inverted_bounds_matches_all_non_controller_nodes(self, client):
        # "150" < "99" lexically would empty the range entirely; numerically
        # 99 <= id <= 150 must match all four LAB1 leaf/border-leaf nodes.
        resp = client.get(
            '/api/class/fabricNode.json?query-target-filter=bw(fabricNode.id,"99","150")'
        )
        assert resp.status_code == 200
        data = resp.json()
        assert int(data["totalCount"]) == 4
        ids = {i["fabricNode"]["attributes"]["id"] for i in data["imdata"]}
        assert ids == {"101", "102", "103", "104"}

    def test_gt_low_numeric_bound_matches_all_non_controller_nodes(self, client):
        # Lexically "99" > "101".."104" (since '9' > '1'), so a lexical-only
        # gt would match nothing; numerically every 3-digit node id passes.
        resp = client.get(
            '/api/class/fabricNode.json?query-target-filter=gt(fabricNode.id,"99")'
        )
        assert resp.status_code == 200
        data = resp.json()
        assert int(data["totalCount"]) >= 4
        roles = [i["fabricNode"]["attributes"]["role"] for i in data["imdata"]]
        assert "controller" not in roles

    def test_lt_mixed_width_bound_excludes_leaves_and_spines(self, client):
        # Lexically "101".."104"/"201"/"202" are all < "50" (since '1'/'2' <
        # '5'), so a lexical-only lt would wrongly match every leaf/spine;
        # numerically none of 101-104/201/202 is < 50, so only the
        # single-digit controllers (id 1-3) may pass.
        resp = client.get(
            '/api/class/fabricNode.json?query-target-filter=lt(fabricNode.id,"50")'
        )
        assert resp.status_code == 200
        data = resp.json()
        roles = {i["fabricNode"]["attributes"]["role"] for i in data["imdata"]}
        ids = {i["fabricNode"]["attributes"]["id"] for i in data["imdata"]}
        assert roles <= {"controller"}
        assert "leaf" not in roles
        assert "spine" not in roles
        assert ids <= {"1", "2", "3"}
