"""Tests for PR-21 — single-APIC default + per-controller OOB IPs.

Design intent (owner directive, see README §6a / docs/DESIGN.md "PR-21"):
APIC cluster size is irrelevant to Ansible playbook *deployment* testing (a
playbook connects to and pushes config through exactly one APIC endpoint);
cluster size only matters for cluster-health/appliance-vector tooling (e.g.
autoACI's health_score.py, which reads infraWiNode across the cluster).
`Site.controllers` therefore now defaults to 1 (was 3 pre-PR-21), while
remaining fully configurable in the validated range 1-5.

Covers:
  - `site.controllers` defaults to 1 (both the repo topology.yaml and a
    freshly-scaffolded `generate_topology()` dict).
  - `controllers` range validation: 1-5 accepted, 0/6 rejected.
  - `site.controller_ips` unset (default): sequential OOB-IP derivation from
    `site.mgmt_ip` (controller 1 = mgmt_ip, controller 2 = mgmt_ip+1, ...),
    reflected on the built controller `topSystem.oobMgmtAddr`.
  - `site.controller_ips` explicit: those exact IPs used verbatim, honored
    on the built controller `topSystem.oobMgmtAddr`; length mismatch
    against `controllers` is rejected; an invalid IP entry is rejected.
  - `infraWiNode` count continues to track `controllers` (== controllers²)
    regardless of default/override — this was already true pre-PR-21
    (test_pr3b_batch2.py asserts it dynamically), verified again here at
    the new default.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from aci_sim.build import orchestrator
from aci_sim.cli import generate_topology
from aci_sim.topology.loader import load_topology
from aci_sim.topology.schema import Topology

REPO_ROOT = Path(__file__).parent.parent
TOPOLOGY_YAML = REPO_ROOT / "topology.yaml"


def _base_topo_dict(**kwargs) -> dict:
    return generate_topology(sites=1, leaves_per_site=2, spines_per_site=2, border_pairs=0, **kwargs)


# ---------------------------------------------------------------------------
# Default: controllers == 1
# ---------------------------------------------------------------------------


class TestDefaultIsSingleApic:
    def test_repo_topology_defaults_to_1_controller_per_site(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        for site in topo.sites:
            assert site.controllers == 1

    def test_scaffolded_topology_defaults_to_1_controller_per_site(self) -> None:
        topo_dict = generate_topology(sites=2, leaves_per_site=2, spines_per_site=2, border_pairs=1)
        topo = Topology.model_validate(topo_dict)
        for site in topo.sites:
            assert site.controllers == 1

    def test_single_apic_default_yields_7_fabricnodes_for_2x2x2_site(self) -> None:
        """2 spines + 2 leaves + 2 border-leaves + 1 controller (new default) = 7
        (was 9 pre-PR-21 at the old controllers=3 default)."""
        topo_dict = generate_topology(sites=1, leaves_per_site=2, spines_per_site=2, border_pairs=1)
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        nodes = store.by_class("fabricNode")
        assert len(nodes) == 7, f"Expected 7 fabricNodes, got {len(nodes)}"
        controllers = [n for n in nodes if n.attrs["role"] == "controller"]
        assert len(controllers) == 1


# ---------------------------------------------------------------------------
# Range validation: 1-5
# ---------------------------------------------------------------------------


class TestControllersRangeValidation:
    @pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
    def test_in_range_accepted(self, n: int) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["controllers"] = n
        topo = Topology.model_validate(topo_dict)
        assert topo.sites[0].controllers == n

    @pytest.mark.parametrize("n", [0, -1, 6, 10])
    def test_out_of_range_rejected(self, n: int) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["controllers"] = n
        with pytest.raises(ValidationError, match="controllers"):
            Topology.model_validate(topo_dict)


# ---------------------------------------------------------------------------
# controller_ips unset (default): sequential derivation from mgmt_ip
# ---------------------------------------------------------------------------


class TestSequentialDerivationFromMgmtIp:
    def test_controller_oob_ips_helper_sequential(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["mgmt_ip"] = "10.192.0.11"
        topo_dict["sites"][0]["controllers"] = 3
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        assert site.controller_oob_ips() == ["10.192.0.11", "10.192.0.12", "10.192.0.13"]

    def test_built_topSystem_oob_addrs_are_sequential(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["mgmt_ip"] = "10.192.0.11"
        topo_dict["sites"][0]["controllers"] = 3
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)

        ctrl_top_systems = {
            mo.attrs["id"]: mo.attrs["oobMgmtAddr"]
            for mo in store.by_class("topSystem")
            if mo.attrs.get("role") == "controller"
        }
        assert ctrl_top_systems == {
            "1": "10.192.0.11",
            "2": "10.192.0.12",
            "3": "10.192.0.13",
        }

    def test_sequential_derivation_rolls_over_octet(self) -> None:
        """mgmt_ip near the end of a /24-style boundary still derives
        correctly via plain ipaddress integer arithmetic (no wraparound bug)."""
        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["mgmt_ip"] = "10.192.0.254"
        topo_dict["sites"][0]["controllers"] = 3
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        assert site.controller_oob_ips() == ["10.192.0.254", "10.192.0.255", "10.192.1.0"]

    def test_unset_mgmt_ip_falls_back_to_legacy_oob_ip_scheme(self) -> None:
        """No mgmt_ip and no controller_ips -> controller_oob_ips() is empty,
        and build/fabric.py falls back to the legacy oob_ip(pod, cid) scheme
        (pre-PR-21 behavior), unchanged for topologies that never set mgmt_ip."""
        from aci_sim.build.fabric import oob_ip

        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["mgmt_ip"] = ""
        topo_dict["sites"][0]["controllers"] = 3
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        assert site.controller_oob_ips() == []

        store = orchestrator.build_site(topo, site)
        ctrl_top_systems = {
            mo.attrs["id"]: mo.attrs["oobMgmtAddr"]
            for mo in store.by_class("topSystem")
            if mo.attrs.get("role") == "controller"
        }
        assert ctrl_top_systems == {
            "1": oob_ip(site.pod, 1),
            "2": oob_ip(site.pod, 2),
            "3": oob_ip(site.pod, 3),
        }


# ---------------------------------------------------------------------------
# controller_ips explicit: honored verbatim + validated
# ---------------------------------------------------------------------------


class TestExplicitControllerIps:
    def test_explicit_controller_ips_honored_on_built_topSystem(self) -> None:
        explicit_ips = ["172.20.0.5", "172.20.0.9", "172.20.0.20"]
        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["controllers"] = 3
        topo_dict["sites"][0]["controller_ips"] = explicit_ips
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        assert site.controller_oob_ips() == explicit_ips

        store = orchestrator.build_site(topo, site)
        ctrl_top_systems = {
            mo.attrs["id"]: mo.attrs["oobMgmtAddr"]
            for mo in store.by_class("topSystem")
            if mo.attrs.get("role") == "controller"
        }
        assert ctrl_top_systems == {"1": explicit_ips[0], "2": explicit_ips[1], "3": explicit_ips[2]}

    def test_explicit_controller_ips_also_honored_on_lldp_adjep(self) -> None:
        """The leaf-side lldpAdjEp.mgmtIp must match the controller's own
        topSystem.oobMgmtAddr — a real LLDP neighbor report always announces
        its own mgmt IP; a mismatch would mean the sim's topology view is
        internally inconsistent."""
        explicit_ips = ["172.20.0.5", "172.20.0.9"]
        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["controllers"] = 2
        topo_dict["sites"][0]["controller_ips"] = explicit_ips
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)

        lldp_ips = {mo.attrs["mgmtIp"] for mo in store.by_class("lldpAdjEp") if "APIC" in mo.attrs.get("sysName", "")}
        assert lldp_ips == set(explicit_ips)

    def test_controller_ips_length_mismatch_rejected(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["controllers"] = 3
        topo_dict["sites"][0]["controller_ips"] = ["10.0.0.1", "10.0.0.2"]  # only 2, need 3
        with pytest.raises(ValidationError, match="controller_ips"):
            Topology.model_validate(topo_dict)

    def test_controller_ips_invalid_ip_rejected(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["controllers"] = 1
        topo_dict["sites"][0]["controller_ips"] = ["not-an-ip"]
        with pytest.raises(ValidationError, match="controller_ips"):
            Topology.model_validate(topo_dict)


# ---------------------------------------------------------------------------
# infraWiNode count still tracks controllers (unchanged wiring, new default)
# ---------------------------------------------------------------------------


class TestInfraWiNodeTracksControllers:
    @pytest.mark.parametrize("n_controllers", [1, 2, 3, 5])
    def test_infraWiNode_count_equals_controllers_squared(self, n_controllers: int) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["controllers"] = n_controllers
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        wi = store.by_class("infraWiNode")
        assert len(wi) == n_controllers * n_controllers

    def test_infraWiNode_count_at_new_default(self) -> None:
        """At the new default (controllers=1), infraWiNode is a 1x1 matrix —
        a single controller's self-health-check entry."""
        topo_dict = _base_topo_dict()
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        assert site.controllers == 1
        store = orchestrator.build_site(topo, site)
        wi = store.by_class("infraWiNode")
        assert len(wi) == 1
        assert wi[0].attrs["health"] == "fully-fit"
