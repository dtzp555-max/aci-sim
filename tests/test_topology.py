"""Tests for topology/schema.py and topology/loader.py (Phase 2).

All tests load the default topology.yaml at the repo root.  One negative test
builds a minimal bad topology inline to verify cross-reference validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from aci_sim.topology.loader import load_topology
from aci_sim.topology.schema import Node, Topology

TOPOLOGY_YAML = Path(__file__).parent.parent / "topology.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_topo() -> Topology:
    return load_topology(TOPOLOGY_YAML)


# ---------------------------------------------------------------------------
# Positive tests against default topology.yaml
# ---------------------------------------------------------------------------


class TestDefaultTopologyLoad:
    def test_has_two_sites(self) -> None:
        topo = get_topo()
        assert len(topo.sites) == 2

    def test_site_names(self) -> None:
        topo = get_topo()
        names = [s.name for s in topo.sites]
        assert "LAB1" in names
        assert "LAB2" in names

    def test_site_a_spine_count(self) -> None:
        topo = get_topo()
        site_a = topo.site_by_name("LAB1")
        assert len(site_a.spine_nodes()) == 2

    def test_site_a_leaf_count(self) -> None:
        topo = get_topo()
        site_a = topo.site_by_name("LAB1")
        assert len(site_a.leaf_nodes()) == 2

    def test_site_a_border_leaf_count(self) -> None:
        topo = get_topo()
        site_a = topo.site_by_name("LAB1")
        assert len(site_a.border_leaves) == 2

    def test_site_b_spine_count(self) -> None:
        topo = get_topo()
        site_b = topo.site_by_name("LAB2")
        assert len(site_b.spine_nodes()) == 2

    def test_site_b_leaf_count(self) -> None:
        topo = get_topo()
        site_b = topo.site_by_name("LAB2")
        assert len(site_b.leaf_nodes()) == 2

    def test_site_b_border_leaf_count(self) -> None:
        topo = get_topo()
        site_b = topo.site_by_name("LAB2")
        assert len(site_b.border_leaves) == 2


class TestNodeIdExpansion:
    """Int-count → Node expansion produces deterministic, collision-free IDs."""

    def test_site_a_spine_ids(self) -> None:
        # site_index=0: spine_id = 201 + 0*200 + i → 201, 202
        topo = get_topo()
        ids = sorted(n.id for n in topo.site_by_name("LAB1").spine_nodes())
        assert ids == [201, 202]

    def test_site_a_leaf_ids(self) -> None:
        # site_index=0: leaf_id = 101 + 0*200 + i → 101, 102
        topo = get_topo()
        ids = sorted(n.id for n in topo.site_by_name("LAB1").leaf_nodes())
        assert ids == [101, 102]

    def test_site_b_spine_ids(self) -> None:
        # site_index=1: spine_id = 201 + 1*200 + i → 401, 402
        topo = get_topo()
        ids = sorted(n.id for n in topo.site_by_name("LAB2").spine_nodes())
        assert ids == [401, 402]

    def test_site_b_leaf_ids(self) -> None:
        # site_index=1: leaf_id = 101 + 1*200 + i → 301, 302
        topo = get_topo()
        ids = sorted(n.id for n in topo.site_by_name("LAB2").leaf_nodes())
        assert ids == [301, 302]

    def test_no_collisions_across_sites(self) -> None:
        topo = get_topo()
        all_ids: list[int] = []
        for site in topo.sites:
            all_ids.extend(n.id for n in site.all_nodes())

        duplicates = [nid for nid in set(all_ids) if all_ids.count(nid) > 1]
        assert duplicates == [], f"Duplicate node IDs across sites: {duplicates}"

    def test_spine_names_deterministic(self) -> None:
        topo = get_topo()
        site_a = topo.site_by_name("LAB1")
        names = sorted(n.name for n in site_a.spine_nodes())
        assert names == ["LAB1-ACI-SP01", "LAB1-ACI-SP02"]

    def test_leaf_names_deterministic(self) -> None:
        topo = get_topo()
        site_b = topo.site_by_name("LAB2")
        names = sorted(n.name for n in site_b.leaf_nodes())
        assert names == ["LAB2-ACI-LF301", "LAB2-ACI-LF302"]


class TestStretchedTenant:
    def test_stretched_tenant_exists(self) -> None:
        topo = get_topo()
        stretched = [t for t in topo.tenants if t.stretch]
        assert len(stretched) >= 1, "No stretched tenant found in topology"

    def test_stretched_tenant_in_both_sites(self) -> None:
        topo = get_topo()
        for tenant in topo.tenants:
            if tenant.stretch:
                assert "LAB1" in tenant.sites, (
                    f"Stretched tenant '{tenant.name}' missing LAB1"
                )
                assert "LAB2" in tenant.sites, (
                    f"Stretched tenant '{tenant.name}' missing LAB2"
                )

    def test_corp_tenant_vrfs_bds_aps(self) -> None:
        topo = get_topo()
        corp = next(t for t in topo.tenants if t.name == "MS-TN1")
        assert len(corp.vrfs) >= 1
        assert len(corp.bds) >= 1
        assert len(corp.aps) >= 1

    def test_corp_contracts_non_empty(self) -> None:
        topo = get_topo()
        corp = next(t for t in topo.tenants if t.name == "MS-TN1")
        assert len(corp.contracts) >= 1

    def test_corp_l3outs_per_site(self) -> None:
        topo = get_topo()
        corp = next(t for t in topo.tenants if t.name == "MS-TN1")
        assert len(corp.l3outs) >= 2, "MS-TN1 tenant should have ≥1 L3Out per site"


class TestL3OutVrfResolves:
    def test_l3out_vrfs_valid(self) -> None:
        topo = get_topo()
        for tenant in topo.tenants:
            vrf_names = {v.name for v in tenant.vrfs}
            for l3out in tenant.l3outs:
                assert l3out.vrf in vrf_names, (
                    f"Tenant '{tenant.name}', L3Out '{l3out.name}' references "
                    f"VRF '{l3out.vrf}' not in {vrf_names}"
                )


class TestHelpers:
    def test_site_by_name_found(self) -> None:
        topo = get_topo()
        site = topo.site_by_name("LAB1")
        assert site.name == "LAB1"

    def test_site_by_name_missing_raises(self) -> None:
        topo = get_topo()
        with pytest.raises(KeyError):
            topo.site_by_name("Nonexistent")

    def test_other_site_from_a_returns_b(self) -> None:
        topo = get_topo()
        other = topo.other_site("LAB1")
        assert other.name == "LAB2"

    def test_other_site_from_b_returns_a(self) -> None:
        topo = get_topo()
        other = topo.other_site("LAB2")
        assert other.name == "LAB1"

    def test_other_site_gives_remote_asn(self) -> None:
        """other_site() should let builders look up the remote fabric ASN for ISN."""
        topo = get_topo()
        # LAB1's ISN peer is LAB2's ASN
        remote_asn = topo.other_site("LAB1").asn
        assert remote_asn == topo.site_by_name("LAB2").asn

    def test_all_nodes_count(self) -> None:
        topo = get_topo()
        site_a = topo.site_by_name("LAB1")
        # 2 spines + 2 leaves + 2 border leaves = 6
        assert len(site_a.all_nodes()) == 6


class TestISN:
    def test_isn_enabled(self) -> None:
        topo = get_topo()
        assert topo.isn.enabled is True

    def test_isn_peer_mesh_full(self) -> None:
        topo = get_topo()
        assert topo.isn.peer_mesh == "full"


class TestFaultsAndEndpoints:
    def test_fault_seeds_present(self) -> None:
        topo = get_topo()
        assert len(topo.faults.seed) >= 1

    def test_health_defaults(self) -> None:
        topo = get_topo()
        assert topo.faults.health_defaults.node > 0
        assert topo.faults.health_defaults.tenant > 0

    def test_endpoints_per_epg(self) -> None:
        topo = get_topo()
        assert topo.endpoints.per_epg >= 1


class TestNewBuilderFields:
    """Fields added for the Phase 3/4 builders (version, l3 domains, addr pools)."""

    def test_node_has_version_default(self) -> None:
        topo = get_topo()
        for node in topo.site_by_name("LAB1").all_nodes():
            assert node.version, f"Node {node.id} missing version"

    def test_explicit_node_version_override(self) -> None:
        n = Node(id=201, name="spine-201", version="n9000-16.0(1)")
        assert n.version == "n9000-16.0(1)"

    def test_access_has_l3_domain(self) -> None:
        topo = get_topo()
        assert len(topo.access.l3_domains) >= 1
        assert topo.access.l3_domains[0].name

    def test_fabric_address_pools(self) -> None:
        topo = get_topo()
        assert topo.fabric.loopback_pool
        assert topo.fabric.oob_pool

    def test_l3out_site_association(self) -> None:
        topo = get_topo()
        corp = next(t for t in topo.tenants if t.name == "MS-TN1")
        by_name = {lo.name: lo for lo in corp.l3outs}
        assert by_name["l3o-bgp-Core_LAB1"].site == "LAB1"
        assert by_name["l3o-bgp-Core_LAB2"].site == "LAB2"

    def test_l3out_border_leaves_are_real(self) -> None:
        topo = get_topo()
        all_border_ids = {
            bl.id for site in topo.sites for bl in site.border_leaves
        }
        for tenant in topo.tenants:
            for l3out in tenant.l3outs:
                for bl_id in l3out.border_leaves:
                    assert bl_id in all_border_ids

    def test_fault_seed_nodes_are_real(self) -> None:
        topo = get_topo()
        real_ids = {n.id for site in topo.sites for n in site.all_nodes()}
        for fseed in topo.faults.seed:
            assert fseed.node in real_ids


# ---------------------------------------------------------------------------
# Negative test — bad cross-reference raises ValidationError
# ---------------------------------------------------------------------------


class TestValidationFailures:
    def _load_bad(self, yaml_text: str) -> None:
        data = yaml.safe_load(yaml_text)
        Topology.model_validate(data)

    def test_epg_references_missing_bd_raises(self) -> None:
        """EPG.bd that does not exist in tenant.bds must raise ValidationError."""
        bad_yaml = """
fabric:
  name: bad-topo
sites:
  - name: SiteA
    id: "1"
    apic_host: "127.0.0.1:8443"
    asn: 65001
    pod: 1
    spines: 1
    leaves: 1
    cabling: auto
isn:
  enabled: false
tenants:
  - name: BadTenant
    stretch: false
    sites: [SiteA]
    vrfs:
      - name: vrf1
    bds:
      - name: real-bd
        vrf: vrf1
        subnets: []
    aps:
      - name: App
        epgs:
          - name: bad-epg
            bd: nonexistent-bd
            contracts: {}
    contracts: []
    l3outs: []
"""
        with pytest.raises(ValidationError):
            self._load_bad(bad_yaml)

    def test_bd_references_missing_vrf_raises(self) -> None:
        """BD.vrf that does not exist in tenant.vrfs must raise ValidationError."""
        bad_yaml = """
fabric:
  name: bad-topo
sites:
  - name: SiteA
    id: "1"
    apic_host: "127.0.0.1:8443"
    asn: 65001
    pod: 1
    spines: 1
    leaves: 1
    cabling: auto
isn:
  enabled: false
tenants:
  - name: BadTenant
    stretch: false
    sites: [SiteA]
    vrfs:
      - name: vrf1
    bds:
      - name: bd1
        vrf: ghost-vrf
        subnets: []
    aps: []
    contracts: []
    l3outs: []
"""
        with pytest.raises(ValidationError):
            self._load_bad(bad_yaml)

    def test_stretched_tenant_with_one_site_raises(self) -> None:
        """stretch:true but only one site listed must raise ValidationError."""
        bad_yaml = """
fabric:
  name: bad-topo
sites:
  - name: SiteA
    id: "1"
    apic_host: "127.0.0.1:8443"
    asn: 65001
    pod: 1
    spines: 1
    leaves: 1
    cabling: auto
  - name: SiteB
    id: "2"
    apic_host: "127.0.0.1:8444"
    asn: 65002
    pod: 1
    spines: 1
    leaves: 1
    cabling: auto
isn:
  enabled: true
  peer_mesh: full
tenants:
  - name: BadStretch
    stretch: true
    sites: [SiteA]
    vrfs:
      - name: vrf1
    bds: []
    aps: []
    contracts: []
    l3outs: []
"""
        with pytest.raises(ValidationError):
            self._load_bad(bad_yaml)

    def test_tenant_references_unknown_site_raises(self) -> None:
        """tenant.sites that names a non-existent site must raise ValidationError."""
        bad_yaml = """
fabric:
  name: bad-topo
sites:
  - name: SiteA
    id: "1"
    apic_host: "127.0.0.1:8443"
    asn: 65001
    pod: 1
    spines: 1
    leaves: 1
    cabling: auto
isn:
  enabled: false
tenants:
  - name: BadTenant
    stretch: false
    sites: [SiteX]
    vrfs: []
    bds: []
    aps: []
    contracts: []
    l3outs: []
"""
        with pytest.raises(ValidationError):
            self._load_bad(bad_yaml)

    def test_l3out_references_missing_vrf_raises(self) -> None:
        """L3Out.vrf that does not exist in tenant.vrfs must raise ValidationError."""
        bad_yaml = """
fabric:
  name: bad-topo
sites:
  - name: SiteA
    id: "1"
    apic_host: "127.0.0.1:8443"
    asn: 65001
    pod: 1
    spines: 1
    leaves: 1
    border_leaves:
      - id: 103
        name: leaf-103
        vpc_domain: vpcdom-siteA
        csw: {name: CSW-A, asn: 65100, peer_ip: 10.100.0.1, local_ip: 10.100.0.2, vlan: 100}
    cabling: auto
isn:
  enabled: false
tenants:
  - name: BadTenant
    stretch: false
    sites: [SiteA]
    vrfs:
      - name: vrf1
    bds: []
    aps: []
    contracts: []
    l3outs:
      - name: bad-l3out
        vrf: ghost
        border_leaves: [103]
        ext_epgs: []
"""
        with pytest.raises(ValidationError):
            self._load_bad(bad_yaml)

    def test_isn_full_mesh_single_site_raises(self) -> None:
        """isn.peer_mesh='full' with fewer than 2 sites must raise ValidationError."""
        bad_yaml = """
fabric:
  name: bad-topo
sites:
  - name: SiteA
    id: "1"
    apic_host: "127.0.0.1:8443"
    asn: 65001
    pod: 1
    spines: 1
    leaves: 1
    cabling: auto
isn:
  enabled: true
  peer_mesh: full
tenants: []
"""
        with pytest.raises(ValidationError):
            self._load_bad(bad_yaml)

    def test_l3out_references_missing_border_leaf_raises(self) -> None:
        """L3Out.border_leaves id not present in any site's border_leaves must raise."""
        bad_yaml = """
fabric:
  name: bad-topo
sites:
  - name: SiteA
    id: "1"
    apic_host: "127.0.0.1:8443"
    asn: 65001
    pod: 1
    spines: 1
    leaves: 1
    border_leaves:
      - id: 103
        name: leaf-103
        vpc_domain: vpcdom-siteA
        csw: {name: CSW-A, asn: 65100, peer_ip: 10.100.0.1, local_ip: 10.100.0.2, vlan: 100}
    cabling: auto
isn:
  enabled: false
tenants:
  - name: T
    stretch: false
    sites: [SiteA]
    vrfs:
      - name: vrf1
    bds: []
    aps: []
    contracts: []
    l3outs:
      - name: l3o
        vrf: vrf1
        border_leaves: [999]
        ext_epgs: []
"""
        with pytest.raises(ValidationError):
            self._load_bad(bad_yaml)

    def test_fault_seed_unknown_node_raises(self) -> None:
        """faults.seed[i].node that is not a real node id must raise ValidationError."""
        bad_yaml = """
fabric:
  name: bad-topo
sites:
  - name: SiteA
    id: "1"
    apic_host: "127.0.0.1:8443"
    asn: 65001
    pod: 1
    spines: 1
    leaves: 1
    cabling: auto
isn:
  enabled: false
tenants: []
faults:
  seed:
    - severity: minor
      code: "F0532"
      node: 9999
      descr: "fault on a non-existent node"
"""
        with pytest.raises(ValidationError):
            self._load_bad(bad_yaml)

    def test_epg_references_missing_contract_raises(self) -> None:
        """EPG contract ref not present in tenant.contracts must raise ValidationError."""
        bad_yaml = """
fabric:
  name: bad-topo
sites:
  - name: SiteA
    id: "1"
    apic_host: "127.0.0.1:8443"
    asn: 65001
    pod: 1
    spines: 1
    leaves: 1
    cabling: auto
isn:
  enabled: false
tenants:
  - name: T
    stretch: false
    sites: [SiteA]
    vrfs:
      - name: vrf1
    bds:
      - name: bd1
        vrf: vrf1
        subnets: []
    aps:
      - name: App
        epgs:
          - name: epg1
            bd: bd1
            contracts:
              provide: [ghost-contract]
              consume: []
    contracts: []
    l3outs: []
"""
        with pytest.raises(ValidationError):
            self._load_bad(bad_yaml)

    def test_border_leaf_id_collision_raises(self) -> None:
        """A border-leaf id colliding with an auto-generated leaf id must raise."""
        # SiteA auto leaves are 101,102; giving a border leaf id=101 collides.
        bad_yaml = """
fabric:
  name: bad-topo
sites:
  - name: SiteA
    id: "1"
    apic_host: "127.0.0.1:8443"
    asn: 65001
    pod: 1
    spines: 2
    leaves: 2
    border_leaves:
      - id: 101
        name: leaf-101-dup
        vpc_domain: vpcdom-siteA
        csw: {name: CSW-A, asn: 65100, peer_ip: 10.100.0.1, local_ip: 10.100.0.2, vlan: 100}
    cabling: auto
isn:
  enabled: false
tenants: []
"""
        with pytest.raises(ValidationError):
            self._load_bad(bad_yaml)
