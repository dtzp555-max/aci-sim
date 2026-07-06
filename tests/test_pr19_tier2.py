"""Tests for PR-19 — Tier-2 topology-elasticity parameters.

Covers:
  - ISN ospf_area/mtu: schema defaults+overrides, validation, and that
    build/underlay.py's ospfIf/ospfAdjEp + build/interfaces.py's ISN uplink
    l1PhysIf actually reflect the configured values (not just stored).
  - VMM domain vCenter config: schema defaults+overrides, vlan_pool
    cross-reference validation, and that build/access.py produces a
    vmmDomP/vmmCtrlrP/vmmUsrAccP tree with the right attrs (hostOrIp,
    rootContName, dvsName) only when vcenter_ip is set.
  - Leaf/spine count elasticity: `aci-sim new` generates collision-free
    topologies at larger counts (8 leaves / 4 spines, multiple border
    pairs), and the previously-unguarded ID-scheme overrun (large
    --leaves-per-site/--spines-per-site/--border-pairs) is now rejected
    with a clear error at generation time instead of a confusing
    Topology.normalize_and_validate collision error.
  - isn.peer_mesh: partial is a documented validate-and-reject (unchanged
    from PR-17/18 lineage; re-asserted here as part of PR-19's Tier-2 pass).

See docs/DESIGN.md's "PR-19 — Tier-2 topology-elasticity parameters"
section for the full design rationale, including why VMM domain MOs are a
pure-addition (no existing builder/production-chain reads a vmmDomP/
vmmCtrlrP object — the real aci-py `bind_epg_to_vmm_domain` MS-TN1 chain
writes an NDO schema `domainAssociations` entry referencing the domain by
DN string only).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from aci_sim.build import orchestrator
from aci_sim.cli import generate_topology
from aci_sim.topology.loader import load_topology
from aci_sim.topology.schema import ISN, Topology

REPO_ROOT = Path(__file__).parent.parent
TOPOLOGY_YAML = REPO_ROOT / "topology.yaml"


@pytest.fixture(scope="module")
def repo_topo() -> Topology:
    return load_topology(TOPOLOGY_YAML)


# ---------------------------------------------------------------------------
# ISN ospf_area / mtu — defaults
# ---------------------------------------------------------------------------


class TestIsnDefaults:
    def test_ospf_area_defaults(self, repo_topo: Topology) -> None:
        assert repo_topo.isn.ospf_area == "0.0.0.0"

    def test_mtu_defaults(self, repo_topo: Topology) -> None:
        assert repo_topo.isn.mtu == 9150

    def test_repo_topology_yaml_still_validates_unchanged(self) -> None:
        """Backward-compat bar: the real topology.yaml, with no Tier-2
        ISN fields added, must still load + validate cleanly."""
        topo = load_topology(TOPOLOGY_YAML)
        assert len(topo.sites) == 2

    def test_ospfIf_reflects_default_area(self, repo_topo: Topology) -> None:
        site = repo_topo.site_by_name("LAB1")
        store = orchestrator.build_site(repo_topo, site)
        ospf_ifs = store.by_class("ospfIf")
        assert ospf_ifs, "expected at least one ospfIf"
        for mo in ospf_ifs:
            assert mo.attrs["area"] == "0.0.0.0"

    def test_isn_uplink_l1PhysIf_reflects_default_mtu(self, repo_topo: Topology) -> None:
        site = repo_topo.site_by_name("LAB1")
        store = orchestrator.build_site(repo_topo, site)
        spine_ids = {n.id for n in site.spine_nodes()}
        # Every spine's dedicated ISN uplink port (eth1/{49+i}, per
        # interfaces.py's _UPLINK_START convention) must carry isn.mtu.
        spine_isn_dns = {
            f"topology/pod-{site.pod}/node-{sid}/sys/phys-[eth1/{49 + i}]"
            for i, sid in enumerate(sorted(spine_ids))
        }
        found = [mo for mo in store.by_class("l1PhysIf") if mo.dn in spine_isn_dns]
        assert len(found) == len(spine_ids)
        for mo in found:
            assert mo.attrs["mtu"] == "9150"


# ---------------------------------------------------------------------------
# PR-22 — ISN OSPF sub-interface + ISN CSW LLDP neighbor
#
# Real ACI multi-site spines carry the ISN-facing OSPF address on a routed
# VLAN-4 sub-interface (e.g. "eth1/49.4"), not the bare physical port, and
# the ISN switch advertises LLDP on that physical port. autoACI's Topology
# "Fabric (physical)" view stitches these together: ospfIf (port + local IP)
# + ospfAdjEp (remote IP) + lldpAdjEp (CSW-side port/name).
# ---------------------------------------------------------------------------


class TestIsnOspfSubInterfaceAndLldp:
    def test_ospfIf_id_is_vlan4_subinterface(self, repo_topo: Topology) -> None:
        site = repo_topo.site_by_name("LAB1")
        store = orchestrator.build_site(repo_topo, site)
        ospf_ifs = store.by_class("ospfIf")
        assert ospf_ifs, "expected at least one ospfIf"
        spine_ids = sorted(n.id for n in site.spine_nodes())
        for si, sid in enumerate(spine_ids):
            expected_id = f"eth1/{49 + si}.4"
            mo = next(
                m for m in ospf_ifs
                if m.dn == f"topology/pod-{site.pod}/node-{sid}/sys/ospf/inst-default/dom-overlay-1/if-[{expected_id}]"
            )
            assert mo.attrs["id"] == expected_id

    def test_ospfIf_addr_in_site_isn_subnet(self, repo_topo: Topology) -> None:
        site = repo_topo.site_by_name("LAB1")
        store = orchestrator.build_site(repo_topo, site)
        ospf_ifs = store.by_class("ospfIf")
        assert ospf_ifs
        for mo in ospf_ifs:
            assert mo.attrs["addr"].startswith(f"172.16.{site.id}.")
            assert mo.attrs["addr"].endswith("/24")

    def test_ospfAdjEp_dn_nests_under_subinterface_segment(self, repo_topo: Topology) -> None:
        site = repo_topo.site_by_name("LAB1")
        store = orchestrator.build_site(repo_topo, site)
        spine_ids = sorted(n.id for n in site.spine_nodes())
        for si, sid in enumerate(spine_ids):
            expected_segment = f"if-[eth1/{49 + si}.4]"
            adj = next(
                m for m in store.by_class("ospfAdjEp")
                if f"/node-{sid}/" in m.dn and expected_segment in m.dn
            )
            assert adj.attrs["peerIp"] == f"172.16.{site.id}.254"

    def test_lldpAdjEp_exists_for_isn_csw_on_physical_port(self, repo_topo: Topology) -> None:
        site = repo_topo.site_by_name("LAB1")
        store = orchestrator.build_site(repo_topo, site)
        spine_ids = sorted(n.id for n in site.spine_nodes())
        lldp_adjs = list(store.by_class("lldpAdjEp"))
        for si, sid in enumerate(spine_ids):
            local_port = f"eth1/{49 + si}"
            adj = next(
                m for m in lldp_adjs
                if m.dn == f"topology/pod-{site.pod}/node-{sid}/sys/lldp/inst/if-[{local_port}]/adj-1"
            )
            assert adj.attrs["sysName"] == f"ISN-CSW{site.id}"
            assert adj.attrs["portIdV"] == f"Ethernet1/{si + 1}"
            assert adj.attrs["mgmtIp"] == f"172.16.{site.id}.254"

    def test_single_site_fabric_builds_no_isn_ospf_or_lldp(self) -> None:
        """Single-site fabrics never peer OSPF/LLDP with an ISN — no ospfIf/
        ospfAdjEp/ospfDom, and no lldpAdjEp named "ISN-CSW*", should exist."""
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        assert store.by_class("ospfIf") == []
        assert store.by_class("ospfAdjEp") == []
        assert store.by_class("ospfDom") == []
        isn_lldp = [m for m in store.by_class("lldpAdjEp") if "ISN-CSW" in m.attrs.get("sysName", "")]
        assert isn_lldp == []


# ---------------------------------------------------------------------------
# ISN ospf_area / mtu — overrides
# ---------------------------------------------------------------------------


class TestIsnOverrides:
    def test_ospf_area_override(self) -> None:
        isn = ISN(ospf_area="10.0.0.0")
        assert isn.ospf_area == "10.0.0.0"

    def test_ospf_area_decimal_form_accepted(self) -> None:
        isn = ISN(ospf_area="10")
        assert isn.ospf_area == "10"

    def test_mtu_override(self) -> None:
        isn = ISN(mtu=9000)
        assert isn.mtu == 9000

    def test_ospf_area_garbage_rejected(self) -> None:
        topo_dict = generate_topology(sites=2, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["isn"]["ospf_area"] = "not-an-area"
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)

    def test_mtu_too_low_rejected(self) -> None:
        topo_dict = generate_topology(sites=2, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["isn"]["mtu"] = 100
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)

    def test_mtu_too_high_rejected(self) -> None:
        topo_dict = generate_topology(sites=2, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["isn"]["mtu"] = 20000
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)

    def test_ospfIf_reflects_overridden_area(self) -> None:
        topo_dict = generate_topology(sites=2, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["isn"]["ospf_area"] = "0.0.0.1"
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        ospf_ifs = store.by_class("ospfIf")
        assert ospf_ifs
        for mo in ospf_ifs:
            assert mo.attrs["area"] == "0.0.0.1"
        for mo in store.by_class("ospfAdjEp"):
            assert mo.attrs["area"] == "0.0.0.1"

    def test_isn_uplink_l1PhysIf_reflects_overridden_mtu(self) -> None:
        topo_dict = generate_topology(sites=2, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["isn"]["mtu"] = 9000
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        spine_id = site.spine_nodes()[0].id
        isn_dn = f"topology/pod-{site.pod}/node-{spine_id}/sys/phys-[eth1/49]"
        mo = next(m for m in store.by_class("l1PhysIf") if m.dn == isn_dn)
        assert mo.attrs["mtu"] == "9000"

    def test_fabric_uplink_mtu_unaffected_by_isn_mtu_override(self) -> None:
        """Only the dedicated ISN spine uplink port should pick up isn.mtu —
        ordinary intra-fabric ports (leaves, spine fabric-uplinks used for
        the Clos mesh) must keep the 9216 fabric default."""
        topo_dict = generate_topology(sites=2, leaves_per_site=2, spines_per_site=2, border_pairs=0)
        topo_dict["isn"]["mtu"] = 9000
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        leaf_id = site.leaf_nodes()[0].id
        # A leaf's fabric-uplink port is never an ISN port (ISN ports are
        # spine-only) — its mtu must remain the fabric default.
        leaf_ports = [mo for mo in store.by_class("l1PhysIf") if f"node-{leaf_id}/" in mo.dn]
        assert leaf_ports
        for mo in leaf_ports:
            assert mo.attrs["mtu"] == "9216"


# ---------------------------------------------------------------------------
# isn.peer_mesh: partial — documented validate-and-reject (unchanged)
# ---------------------------------------------------------------------------


class TestPeerMeshPartial:
    def test_partial_mesh_rejected_at_build_time(self) -> None:
        """isn.peer_mesh: partial passes schema validation (no schema-level
        enum restriction — see ISN.peer_mesh's `partial` note) but
        build/overlay.py fails fast rather than silently building full-mesh
        or an arbitrary subset."""
        topo_dict = generate_topology(sites=2, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["isn"]["peer_mesh"] = "partial"
        topo = Topology.model_validate(topo_dict)  # schema-level: OK
        site = topo.sites[0]
        with pytest.raises(ValueError, match="peer_mesh"):
            orchestrator.build_site(topo, site)


# ---------------------------------------------------------------------------
# VMM domain — defaults (empty list; backward compat)
# ---------------------------------------------------------------------------


class TestVmmDefaults:
    def test_repo_topology_yaml_has_no_vmm_domains(self, repo_topo: Topology) -> None:
        assert repo_topo.access.vmm_domains == []

    def test_no_vmmDomP_built_when_vmm_domains_empty(self, repo_topo: Topology) -> None:
        site = repo_topo.site_by_name("LAB1")
        store = orchestrator.build_site(repo_topo, site)
        assert store.by_class("vmmDomP") == []
        assert store.by_class("vmmCtrlrP") == []

    def test_bare_vmm_domain_no_vcenter_ip_produces_no_ctrlrp(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["access"]["vmm_domains"] = [{"name": "vmm-vmw-bare"}]
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        dom_mos = store.by_class("vmmDomP")
        assert len(dom_mos) == 1
        assert dom_mos[0].attrs["name"] == "vmm-vmw-bare"
        assert store.by_class("vmmCtrlrP") == []


# ---------------------------------------------------------------------------
# VMM domain — overrides (vCenter attrs populate vmmCtrlrP)
# ---------------------------------------------------------------------------


class TestVmmOverrides:
    def test_vcenter_attrs_populate_vmmCtrlrP(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["access"]["vmm_domains"] = [
            {
                "name": "vmm-vmw-lab1",
                "vcenter_ip": "10.50.1.10",
                "vcenter_dvs": "DVS-LAB1",
                "datacenter": "DC-LAB1",
            }
        ]
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        ctrlrs = store.by_class("vmmCtrlrP")
        assert len(ctrlrs) == 1
        ctrlr = ctrlrs[0]
        assert ctrlr.attrs["hostOrIp"] == "10.50.1.10"
        assert ctrlr.attrs["rootContName"] == "DC-LAB1"
        assert ctrlr.attrs["dvsName"] == "DVS-LAB1"
        assert ctrlr.dn.startswith("uni/vmmp-VMware/dom-vmm-vmw-lab1/ctrlr-")

    def test_vcenter_ip_produces_vmmUsrAccP_child(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["access"]["vmm_domains"] = [
            {"name": "vmm-vmw-lab1", "vcenter_ip": "10.50.1.10"}
        ]
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        usracc = store.by_class("vmmUsrAccP")
        assert len(usracc) == 1

    def test_datacenter_defaults_to_domain_name_when_unset(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["access"]["vmm_domains"] = [
            {"name": "vmm-vmw-lab1", "vcenter_ip": "10.50.1.10"}
        ]
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        ctrlr = store.by_class("vmmCtrlrP")[0]
        assert ctrlr.attrs["rootContName"] == "vmm-vmw-lab1"

    def test_multiple_vmm_domains_each_get_own_ctrlrp(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["access"]["vmm_domains"] = [
            {"name": "vmm-vmw-lab1", "vcenter_ip": "10.50.1.10"},
            {"name": "vmm-vmw-lab2", "vcenter_ip": "10.50.2.10"},
        ]
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        ctrlrs = {mo.attrs["hostOrIp"] for mo in store.by_class("vmmCtrlrP")}
        assert ctrlrs == {"10.50.1.10", "10.50.2.10"}


# ---------------------------------------------------------------------------
# VMM domain — vlan_pool cross-reference validation
# ---------------------------------------------------------------------------


class TestVmmVlanPoolValidation:
    def test_unknown_vlan_pool_rejected(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo_dict["access"]["vmm_domains"] = [
            {"name": "vmm-vmw-lab1", "vlan_pool": "does-not-exist"}
        ]
        with pytest.raises(ValidationError, match="vlan_pool"):
            Topology.model_validate(topo_dict)

    def test_known_vlan_pool_accepted(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        pool_name = topo_dict["access"]["vlan_pools"][0]["name"]
        topo_dict["access"]["vmm_domains"] = [
            {"name": "vmm-vmw-lab1", "vlan_pool": pool_name}
        ]
        topo = Topology.model_validate(topo_dict)
        assert topo.access.vmm_domains[0].vlan_pool == pool_name

    def test_vlan_pool_bind_produces_infraRsVlanNs_child(self) -> None:
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        pool_name = topo_dict["access"]["vlan_pools"][0]["name"]
        topo_dict["access"]["vmm_domains"] = [
            {"name": "vmm-vmw-lab1", "vlan_pool": pool_name}
        ]
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        rsvlan = [
            mo for mo in store.by_class("infraRsVlanNs")
            if mo.dn.startswith("uni/vmmp-VMware/dom-vmm-vmw-lab1")
        ]
        assert len(rsvlan) == 1


# ---------------------------------------------------------------------------
# Leaf/spine count elasticity — `aci-sim new` sugar
# ---------------------------------------------------------------------------


class TestLeafSpineElasticity:
    def test_8_leaves_4_spines_validates_clean(self) -> None:
        topo_dict = generate_topology(sites=2, leaves_per_site=8, spines_per_site=4)
        topo = Topology.model_validate(topo_dict)
        for site in topo.sites:
            assert len(site.leaf_nodes()) == 8
            assert len(site.spine_nodes()) == 4

    def test_8_leaves_4_spines_no_id_collision(self) -> None:
        topo_dict = generate_topology(sites=2, leaves_per_site=8, spines_per_site=4, border_pairs=2)
        topo = Topology.model_validate(topo_dict)
        all_ids: list[int] = []
        for site in topo.sites:
            all_ids.extend(n.id for n in site.all_nodes())
        assert len(all_ids) == len(set(all_ids))

    def test_border_pairs_greater_than_1_works(self) -> None:
        topo_dict = generate_topology(sites=2, leaves_per_site=4, spines_per_site=2, border_pairs=3)
        topo = Topology.model_validate(topo_dict)
        for site in topo.sites:
            assert len(site.border_leaves) == 6  # 3 pairs = 6 border leaves
            # each pair shares one vpc_domain
            domains = {bl.vpc_domain for bl in site.border_leaves}
            assert len(domains) == 3

    @pytest.mark.parametrize(
        "leaves,spines,borders",
        [(101, 2, 0), (2, 101, 0), (150, 4, 3), (2, 2, 50)],
    )
    def test_id_scheme_overrun_rejected_with_clear_error(
        self, leaves: int, spines: int, borders: int
    ) -> None:
        """Previously: these combinations silently produced a Topology dict
        with colliding node ids, only caught deep inside
        Topology.normalize_and_validate with a message that didn't name the
        offending flag. Now: generate_topology itself rejects them."""
        with pytest.raises(ValueError):
            generate_topology(
                sites=2, leaves_per_site=leaves, spines_per_site=spines, border_pairs=borders
            )

    def test_100_leaves_100_spines_boundary_still_works(self) -> None:
        """Exact boundary: 100 is the max safe leaves/spines-per-site count
        before the next block/site is reached."""
        topo_dict = generate_topology(sites=2, leaves_per_site=100, spines_per_site=100, border_pairs=0)
        topo = Topology.model_validate(topo_dict)
        all_ids: list[int] = []
        for site in topo.sites:
            all_ids.extend(n.id for n in site.all_nodes())
        assert len(all_ids) == len(set(all_ids))

    def test_101_leaves_rejected(self) -> None:
        with pytest.raises(ValueError, match="leaves-per-site"):
            generate_topology(sites=2, leaves_per_site=101, spines_per_site=2, border_pairs=0)

    def test_101_spines_rejected(self) -> None:
        with pytest.raises(ValueError, match="spines-per-site"):
            generate_topology(sites=2, leaves_per_site=2, spines_per_site=101, border_pairs=0)

    def test_border_pairs_overrun_rejected(self) -> None:
        with pytest.raises(ValueError, match="border-pairs"):
            generate_topology(sites=2, leaves_per_site=2, spines_per_site=2, border_pairs=50)

    def test_full_build_succeeds_for_8_leaves_4_spines(self) -> None:
        """Confirms the orchestrator can actually build a site (not just
        validate the schema) at the larger count."""
        topo_dict = generate_topology(sites=2, leaves_per_site=8, spines_per_site=4, border_pairs=1)
        topo = Topology.model_validate(topo_dict)
        for site in topo.sites:
            store = orchestrator.build_site(topo, site)
            fabric_nodes = store.by_class("fabricNode")
            switch_ids = {
                int(mo.attrs["id"]) for mo in fabric_nodes if mo.attrs.get("role") != "controller"
            }
            expected_ids = {n.id for n in site.all_nodes()}
            assert switch_ids == expected_ids
            assert len(expected_ids) == 8 + 4 + 2  # leaves + spines + 1 border pair
