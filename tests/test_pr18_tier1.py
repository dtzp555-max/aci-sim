"""Tests for PR-18 — Tier-1 fabric configuration parameters.

Covers each Tier-1 param defaulting correctly when absent from
topology.yaml, overriding when set, the deterministic-serial fallback,
`site.pod` DN correctness, N-site scaffold collision-freedom, and
`infraWiNode` count tracking `site.controllers`.

See docs/DESIGN.md's "PR-18 — Tier-1 fabric configuration parameters"
section for the full design rationale, including the documented N-site
ISN/`other_site()` limitation this file's `TestNSite` class verifies
(builds without collision; does NOT assert a true N-site ISN full mesh,
which is explicitly out of scope for this PR).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from aci_sim.build import orchestrator
from aci_sim.build.fabric import default_serial
from aci_sim.cli import generate_topology
from aci_sim.topology.loader import load_topology
from aci_sim.topology.schema import Fabric, Topology

REPO_ROOT = Path(__file__).parent.parent
TOPOLOGY_YAML = REPO_ROOT / "topology.yaml"


@pytest.fixture(scope="module")
def repo_topo() -> Topology:
    return load_topology(TOPOLOGY_YAML)


# ---------------------------------------------------------------------------
# Defaults — absent from topology.yaml, each param resolves to its default
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_controllers_defaults_to_1(self, repo_topo: Topology) -> None:
        """PR-21: single-APIC-per-site is now the default (was 3 pre-PR-21)."""
        for site in repo_topo.sites:
            assert site.controllers == 1

    def test_pod_defaults_to_1(self, repo_topo: Topology) -> None:
        for site in repo_topo.sites:
            assert site.pod == 1

    def test_tep_pool_defaults(self, repo_topo: Topology) -> None:
        assert repo_topo.fabric.tep_pool == "10.0.0.0/16"

    def test_infra_vlan_defaults(self, repo_topo: Topology) -> None:
        assert repo_topo.fabric.infra_vlan == 3967

    def test_gipo_pool_defaults(self, repo_topo: Topology) -> None:
        assert repo_topo.fabric.gipo_pool == "225.0.0.0/15"

    def test_repo_topology_yaml_still_validates_unchanged(self) -> None:
        """The backward-compat acceptance bar: the real topology.yaml, with
        no Tier-1 fields added, must still load + validate cleanly."""
        topo = load_topology(TOPOLOGY_YAML)
        assert len(topo.sites) == 2


# ---------------------------------------------------------------------------
# Overrides — each param actually takes effect when set
# ---------------------------------------------------------------------------


class TestOverrides:
    def test_controllers_override(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["sites"][0]["controllers"] = 5
        topo = Topology.model_validate(topo_dict)
        assert topo.sites[0].controllers == 5

    def test_pod_override(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["sites"][0]["pod"] = 2
        topo = Topology.model_validate(topo_dict)
        assert topo.sites[0].pod == 2

    def test_tep_pool_override(self) -> None:
        fab = Fabric(name="x", tep_pool="172.16.0.0/16")
        assert fab.tep_pool == "172.16.0.0/16"

    def test_infra_vlan_override(self) -> None:
        fab = Fabric(name="x", infra_vlan=100)
        assert fab.infra_vlan == 100

    def test_gipo_pool_override(self) -> None:
        fab = Fabric(name="x", gipo_pool="226.0.0.0/15")
        assert fab.gipo_pool == "226.0.0.0/15"

    def test_infra_vlan_out_of_range_rejected(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["fabric"]["infra_vlan"] = 4095
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)

    def test_infra_vlan_zero_rejected(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["fabric"]["infra_vlan"] = 0
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)

    def test_tep_pool_bad_cidr_rejected(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["fabric"]["tep_pool"] = "not-a-cidr"
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)

    def test_gipo_pool_bad_cidr_rejected(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["fabric"]["gipo_pool"] = "garbage"
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)


# ---------------------------------------------------------------------------
# Deterministic serials
# ---------------------------------------------------------------------------


class TestSerials:
    def test_default_serial_format(self) -> None:
        assert default_serial("1", 101) == "SAL10101"
        assert default_serial("2", 301) == "SAL20301"

    def test_default_serial_deterministic(self) -> None:
        assert default_serial("1", 101) == default_serial("1", 101)

    def test_auto_node_gets_nonempty_serial_in_built_fabricNode(self, repo_topo: Topology) -> None:
        site = repo_topo.site_by_name("LAB1")
        store = orchestrator.build_site(repo_topo, site)
        for mo in store.by_class("fabricNode"):
            assert mo.attrs.get("serial"), f"empty serial for {mo.dn}"

    def test_auto_node_serial_matches_default_serial_scheme(self, repo_topo: Topology) -> None:
        site = repo_topo.site_by_name("LAB1")
        store = orchestrator.build_site(repo_topo, site)
        node101 = next(mo for mo in store.by_class("fabricNode") if mo.attrs.get("id") == "101")
        assert node101.attrs["serial"] == default_serial(site.id, 101)

    def test_explicit_yaml_serial_overrides_default(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["sites"][0]["leaves"][0]["serial"] = "EXPLICIT-SERIAL-1"
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        leaf_id = site.leaf_nodes()[0].id
        mo = next(m for m in store.by_class("fabricNode") if m.attrs.get("id") == str(leaf_id))
        assert mo.attrs["serial"] == "EXPLICIT-SERIAL-1"

    def test_fabricNodeIdentP_serial_matches_fabricNode_serial(self, repo_topo: Topology) -> None:
        site = repo_topo.site_by_name("LAB1")
        store = orchestrator.build_site(repo_topo, site)
        fn_serials = {mo.attrs["id"]: mo.attrs["serial"] for mo in store.by_class("fabricNode")}
        for mo in store.by_class("fabricNodeIdentP"):
            assert mo.attrs["serial"] == fn_serials[mo.attrs["nodeId"]]


# ---------------------------------------------------------------------------
# Pod DN correctness
# ---------------------------------------------------------------------------


class TestPod:
    def test_pod2_site_builds_pod2_dns(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["sites"][0]["pod"] = 2
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        fabric_nodes = store.by_class("fabricNode")
        assert fabric_nodes, "expected at least one fabricNode"
        for mo in fabric_nodes:
            assert mo.dn.startswith("topology/pod-2/"), mo.dn
        for mo in store.by_class("topSystem"):
            assert mo.dn.startswith("topology/pod-2/"), mo.dn
            assert mo.attrs["podId"] == "2"


# ---------------------------------------------------------------------------
# Controllers -> infraWiNode count
# ---------------------------------------------------------------------------


class TestControllersWiring:
    @pytest.mark.parametrize("n_controllers", [1, 3, 5])
    def test_infraWiNode_count_matches_controllers_squared(self, n_controllers: int) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["sites"][0]["controllers"] = n_controllers
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        wi = store.by_class("infraWiNode")
        assert len(wi) == n_controllers * n_controllers

    def test_controller_fabricNode_count_matches_controllers(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["sites"][0]["controllers"] = 4
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        controllers = [mo for mo in store.by_class("fabricNode") if mo.attrs.get("role") == "controller"]
        assert len(controllers) == 4


# ---------------------------------------------------------------------------
# N-site — collision-free build; documented ISN limit not asserted as a bug
# ---------------------------------------------------------------------------


class TestNSite:
    def test_3site_scaffold_builds_without_node_id_collision(self) -> None:
        topo_dict = generate_topology(sites=3, leaves_per_site=2, spines_per_site=2, border_pairs=1)
        topo = Topology.model_validate(topo_dict)
        assert len(topo.sites) == 3

        all_ids: list[int] = []
        for site in topo.sites:
            all_ids.extend(n.id for n in site.all_nodes())
        assert len(all_ids) == len(set(all_ids))

    def test_3site_scaffold_distinct_pods_serials_controllers(self) -> None:
        topo_dict = generate_topology(sites=3, leaves_per_site=2, spines_per_site=2, border_pairs=1)
        # Give each site a distinct pod to prove pod-per-site isn't collapsed.
        for i, site_def in enumerate(topo_dict["sites"], start=1):
            site_def["pod"] = i
            site_def["controllers"] = i + 2  # 3, 4, 5
        topo = Topology.model_validate(topo_dict)
        assert [s.pod for s in topo.sites] == [1, 2, 3]
        assert [s.controllers for s in topo.sites] == [3, 4, 5]

        for site in topo.sites:
            store = orchestrator.build_site(topo, site)
            fabric_nodes = store.by_class("fabricNode")
            for mo in fabric_nodes:
                assert mo.dn.startswith(f"topology/pod-{site.pod}/")
                assert mo.attrs["serial"]  # non-empty
            controllers = [mo for mo in fabric_nodes if mo.attrs.get("role") == "controller"]
            assert len(controllers) == site.controllers

    def test_3site_new_cli_controllers_flag(self) -> None:
        topo_dict = generate_topology(sites=3, leaves_per_site=2, spines_per_site=2, border_pairs=1, controllers=5)
        topo = Topology.model_validate(topo_dict)
        assert len(topo.sites) == 3
        for site in topo.sites:
            assert site.controllers == 5
