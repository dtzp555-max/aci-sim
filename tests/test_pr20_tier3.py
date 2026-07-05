"""Tests for PR-20 — Tier-3 fine-tuning parameters (the last param tier).

Covers:
  - fabric.oob_subnet / fabric.inb_subnet: schema defaults (None/omitted),
    validation (CIDR well-formedness + address-family range), and that
    `aci-sim show` surfaces both — inb_subnet explicitly STORE-ONLY (no MO
    reflects it; this sim has no mgmtInB/inbMgmtAddr-style object anywhere).
  - fvBD.mac: `bd.mac` (per-BD) falling back to `fabric.default_bd_mac`
    (fabric-wide), which itself defaults to the real ACI default gateway
    MAC "00:22:BD:F8:19:FF" — the exact literal build/tenants.py already
    hardcoded pre-PR-20, so an unset bd.mac/default_bd_mac reproduces
    byte-identical fvBD.mac output.
  - BGP keepalive/hold (l3out.csw_peer): schema defaults (60/180, real ACI
    defaults) reflected on the built bgpPeerP MO; overrides honored;
    hold<=keepalive rejected.
  - OSPF hello/dead (isn.ospf_hello/ospf_dead): schema defaults (10/40,
    real ACI defaults) reflected on the built ospfIf MO; overrides
    honored; dead<=hello rejected.
  - BFD timers (isn.bfd_min_rx/min_tx/multiplier): schema defaults (50/50/3,
    real ACI defaults), validated, surfaced in `aci-sim show` — explicitly
    STORE-ONLY, no bfdIfP MO built anywhere in this sim.
  - fabric.default_apic_version: fabric-wide override that takes
    precedence over site.apic_version (already independently settable from
    switch-node Node.version since PR-18/PR-9 lineage) on the controller's
    fabricNode/topSystem/firmwareCtrlrRunning.

See docs/DESIGN.md's "PR-20 — Tier-3 fine-tuning parameters" section for
the full design rationale, including which timers are builder-wired vs
store-only.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from aci_sim.build import orchestrator
from aci_sim.cli import generate_topology
from aci_sim.topology.loader import load_topology
from aci_sim.topology.schema import BD, CswPeer, Fabric, ISN, Topology

REPO_ROOT = Path(__file__).parent.parent
TOPOLOGY_YAML = REPO_ROOT / "topology.yaml"


@pytest.fixture(scope="module")
def repo_topo() -> Topology:
    return load_topology(TOPOLOGY_YAML)


def _base_topo_dict(**kwargs) -> dict:
    return generate_topology(sites=2, leaves_per_site=1, spines_per_site=1, border_pairs=1, **kwargs)


def _topo_dict_with_l3out(**kwargs) -> dict:
    """`_base_topo_dict()` plus one l3out+csw_peer on site 0, agreeing with
    that site's own border-leaf `csw` (name/asn/peer_ip), matching
    `topology.yaml`'s real l3o-bgp-Core_LAB1 shape — needed because
    `generate_topology()`'s scaffold never emits an l3out (verified: no
    "l3outs"/"csw_peer" call sites in cli.py's generator; l3outs is always
    an empty list `[]`)."""
    topo_dict = _base_topo_dict(**kwargs)
    site0 = topo_dict["sites"][0]
    bl = site0["border_leaves"][0]
    tenant = topo_dict["tenants"][0]
    tenant["l3outs"] = [
        {
            "name": "l3o-bgp-test",
            "vrf": tenant["vrfs"][0]["name"],
            "site": site0["name"],
            "border_leaves": [bl["id"]],
            "csw_peer": {
                "name": bl["csw"]["name"],
                "asn": bl["csw"]["asn"],
                "peer_ip": bl["csw"]["peer_ip"],
            },
            "ext_epgs": [],
        }
    ]
    return topo_dict


# ---------------------------------------------------------------------------
# Backward compat — repo topology.yaml unchanged
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_repo_topology_yaml_still_validates_unchanged(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        assert len(topo.sites) == 2

    def test_all_new_fabric_fields_default(self, repo_topo: Topology) -> None:
        assert repo_topo.fabric.oob_subnet is None
        assert repo_topo.fabric.inb_subnet is None
        assert repo_topo.fabric.default_bd_mac == "00:22:BD:F8:19:FF"
        assert repo_topo.fabric.default_apic_version is None

    def test_all_new_isn_fields_default(self, repo_topo: Topology) -> None:
        assert repo_topo.isn.ospf_hello == 10
        assert repo_topo.isn.ospf_dead == 40
        assert repo_topo.isn.bfd_min_rx == 50
        assert repo_topo.isn.bfd_min_tx == 50
        assert repo_topo.isn.bfd_multiplier == 3

    def test_repo_fvBD_mac_is_real_aci_default(self, repo_topo: Topology) -> None:
        """Critical backward-compat assertion: every existing BD's fvBD.mac
        stays exactly "00:22:BD:F8:19:FF" (the literal build/tenants.py
        already hardcoded pre-PR-20) with no topology.yaml changes."""
        site = repo_topo.site_by_name("LAB1")
        store = orchestrator.build_site(repo_topo, site)
        bds = store.by_class("fvBD")
        assert bds, "expected at least one fvBD"
        for mo in bds:
            assert mo.attrs["mac"] == "00:22:BD:F8:19:FF"

    def test_repo_csw_peer_keepalive_hold_default(self, repo_topo: Topology) -> None:
        for tenant in repo_topo.tenants:
            for l3out in tenant.l3outs:
                if l3out.csw_peer is not None:
                    assert l3out.csw_peer.keepalive == 60
                    assert l3out.csw_peer.hold == 180

    def test_repo_bgpPeerP_reflects_default_timers(self, repo_topo: Topology) -> None:
        site = repo_topo.site_by_name("LAB1")
        store = orchestrator.build_site(repo_topo, site)
        peers = store.by_class("bgpPeerP")
        assert peers, "expected at least one bgpPeerP"
        for mo in peers:
            assert mo.attrs["keepAliveIntvl"] == "60"
            assert mo.attrs["holdIntvl"] == "180"

    def test_repo_ospfIf_reflects_default_hello_dead(self, repo_topo: Topology) -> None:
        site = repo_topo.site_by_name("LAB1")
        store = orchestrator.build_site(repo_topo, site)
        ospf_ifs = store.by_class("ospfIf")
        assert ospf_ifs, "expected at least one ospfIf"
        for mo in ospf_ifs:
            assert mo.attrs["helloIntvl"] == "10"
            assert mo.attrs["deadIntvl"] == "40"

    def test_site_apic_version_unaffected_when_default_apic_version_unset(
        self, repo_topo: Topology
    ) -> None:
        site = repo_topo.site_by_name("LAB1")
        store = orchestrator.build_site(repo_topo, site)
        ctrl_top_system = next(
            m for m in store.by_class("topSystem") if m.dn == f"topology/pod-{site.pod}/node-1/sys"
        )
        assert ctrl_top_system.attrs["version"] == site.apic_version == "4.2(7f)"


# ---------------------------------------------------------------------------
# fvBD.mac — per-BD override + fabric-wide default override
# ---------------------------------------------------------------------------


class TestBdMac:
    def test_bd_mac_schema_default_is_none(self) -> None:
        bd = BD(name="bd-x", vrf="vrf-x")
        assert bd.mac is None

    def test_fabric_default_bd_mac_schema_default(self) -> None:
        fab = Fabric(name="fab")
        assert fab.default_bd_mac == "00:22:BD:F8:19:FF"

    def test_per_bd_mac_override_wins(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["tenants"][0]["bds"][0]["mac"] = "AA:BB:CC:DD:EE:FF"
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        bd_name = topo_dict["tenants"][0]["bds"][0]["name"]
        mo = next(m for m in store.by_class("fvBD") if m.attrs["name"] == bd_name)
        assert mo.attrs["mac"] == "AA:BB:CC:DD:EE:FF"

    def test_fabric_wide_default_bd_mac_applies_when_bd_mac_unset(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["fabric"]["default_bd_mac"] = "02:00:00:00:00:01"
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        bds = store.by_class("fvBD")
        assert bds
        for mo in bds:
            assert mo.attrs["mac"] == "02:00:00:00:00:01"

    def test_per_bd_mac_wins_over_fabric_wide_default(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["fabric"]["default_bd_mac"] = "02:00:00:00:00:01"
        topo_dict["tenants"][0]["bds"][0]["mac"] = "AA:BB:CC:DD:EE:FF"
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        bd_name = topo_dict["tenants"][0]["bds"][0]["name"]
        mo = next(m for m in store.by_class("fvBD") if m.attrs["name"] == bd_name)
        assert mo.attrs["mac"] == "AA:BB:CC:DD:EE:FF"

    def test_invalid_bd_mac_rejected(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["tenants"][0]["bds"][0]["mac"] = "not-a-mac"
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)

    def test_invalid_fabric_default_bd_mac_rejected(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["fabric"]["default_bd_mac"] = "nope"
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)


# ---------------------------------------------------------------------------
# BGP keepalive/hold — l3out.csw_peer -> bgpPeerP
# ---------------------------------------------------------------------------


class TestBgpTimers:
    def test_schema_defaults(self) -> None:
        cp = CswPeer(name="csw", asn=65100, peer_ip="10.0.0.1")
        assert cp.keepalive == 60
        assert cp.hold == 180

    def test_override_reflected_on_bgpPeerP(self) -> None:
        topo_dict = _topo_dict_with_l3out()
        l3out = topo_dict["tenants"][0]["l3outs"][0]
        l3out["csw_peer"]["keepalive"] = 10
        l3out["csw_peer"]["hold"] = 30
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        peers = store.by_class("bgpPeerP")
        assert peers
        for mo in peers:
            assert mo.attrs["keepAliveIntvl"] == "10"
            assert mo.attrs["holdIntvl"] == "30"

    def test_hold_at_or_below_keepalive_rejected(self) -> None:
        topo_dict = _topo_dict_with_l3out()
        l3out = topo_dict["tenants"][0]["l3outs"][0]
        l3out["csw_peer"]["keepalive"] = 60
        l3out["csw_peer"]["hold"] = 60
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)

    def test_negative_timers_rejected(self) -> None:
        topo_dict = _topo_dict_with_l3out()
        l3out = topo_dict["tenants"][0]["l3outs"][0]
        l3out["csw_peer"]["keepalive"] = -1
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)


# ---------------------------------------------------------------------------
# OSPF hello/dead — isn.ospf_hello/ospf_dead -> ospfIf
# ---------------------------------------------------------------------------


class TestOspfTimers:
    def test_schema_defaults(self) -> None:
        isn = ISN()
        assert isn.ospf_hello == 10
        assert isn.ospf_dead == 40

    def test_override_reflected_on_ospfIf(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["isn"]["ospf_hello"] = 5
        topo_dict["isn"]["ospf_dead"] = 20
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        ospf_ifs = store.by_class("ospfIf")
        assert ospf_ifs
        for mo in ospf_ifs:
            assert mo.attrs["helloIntvl"] == "5"
            assert mo.attrs["deadIntvl"] == "20"

    def test_dead_at_or_below_hello_rejected(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["isn"]["ospf_hello"] = 10
        topo_dict["isn"]["ospf_dead"] = 10
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)

    def test_non_positive_hello_rejected(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["isn"]["ospf_hello"] = 0
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)


# ---------------------------------------------------------------------------
# BFD timers — store+validate+surface only (no bfdIfP MO anywhere)
# ---------------------------------------------------------------------------


class TestBfdTimersStoreOnly:
    def test_schema_defaults(self) -> None:
        isn = ISN()
        assert isn.bfd_min_rx == 50
        assert isn.bfd_min_tx == 50
        assert isn.bfd_multiplier == 3

    def test_override_accepted_and_stored(self) -> None:
        isn = ISN(bfd_min_rx=100, bfd_min_tx=100, bfd_multiplier=5)
        assert isn.bfd_min_rx == 100
        assert isn.bfd_min_tx == 100
        assert isn.bfd_multiplier == 5

    def test_no_bfd_mo_built_anywhere(self) -> None:
        """Confirms the store-only claim: building a full site produces no
        MO with 'bfd' anywhere in its class name."""
        topo_dict = _base_topo_dict()
        topo_dict["isn"]["bfd_min_rx"] = 999
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        bfd_classes = {mo.class_name for mo in store.all() if "bfd" in mo.class_name.lower()}
        assert bfd_classes == set()

    def test_multiplier_out_of_range_rejected(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["isn"]["bfd_multiplier"] = 51
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)

    def test_non_positive_min_rx_rejected(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["isn"]["bfd_min_rx"] = 0
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)


# ---------------------------------------------------------------------------
# oob_subnet / inb_subnet — store+validate+surface
# ---------------------------------------------------------------------------


class TestMgmtSubnets:
    def test_defaults_are_none(self) -> None:
        fab = Fabric(name="fab")
        assert fab.oob_subnet is None
        assert fab.inb_subnet is None

    def test_oob_subnet_accepted_within_family(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["fabric"]["oob_subnet"] = "192.168.50.0/24"
        topo = Topology.model_validate(topo_dict)
        assert topo.fabric.oob_subnet == "192.168.50.0/24"

    def test_oob_subnet_outside_family_rejected(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["fabric"]["oob_subnet"] = "172.20.0.0/24"
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)

    def test_oob_subnet_malformed_rejected(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["fabric"]["oob_subnet"] = "not-a-cidr"
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)

    def test_inb_subnet_accepted_within_rfc1918(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["fabric"]["inb_subnet"] = "10.50.0.0/16"
        topo = Topology.model_validate(topo_dict)
        assert topo.fabric.inb_subnet == "10.50.0.0/16"

    def test_inb_subnet_outside_rfc1918_rejected(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["fabric"]["inb_subnet"] = "8.8.8.0/24"
        with pytest.raises(ValidationError):
            Topology.model_validate(topo_dict)

    def test_inb_subnet_no_mo_built(self) -> None:
        """Confirms the store-only claim: no mgmtInB/inbMgmtAddr-style MO
        exists anywhere in the built store."""
        topo_dict = _base_topo_dict()
        topo_dict["fabric"]["inb_subnet"] = "10.50.0.0/16"
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        inb_classes = {
            mo.class_name for mo in store.all()
            if "inb" in mo.class_name.lower() or "mgmtinb" in mo.class_name.lower()
        }
        assert inb_classes == set()

    def test_show_surfaces_both_subnets(self) -> None:
        from aci_sim.cli import build_inventory

        topo_dict = _base_topo_dict()
        topo_dict["fabric"]["oob_subnet"] = "192.168.50.0/24"
        topo_dict["fabric"]["inb_subnet"] = "10.50.0.0/16"
        topo = Topology.model_validate(topo_dict)
        inv = build_inventory(topo)
        assert inv["oob_subnet"] == "192.168.50.0/24"
        assert inv["inb_subnet"] == "10.50.0.0/16"


# ---------------------------------------------------------------------------
# fabric.default_apic_version — fabric-wide override, independent of
# switch-node Node.version
# ---------------------------------------------------------------------------


class TestApicVersion:
    def test_schema_default_is_none(self) -> None:
        fab = Fabric(name="fab")
        assert fab.default_apic_version is None

    def test_unset_uses_site_apic_version(self) -> None:
        topo_dict = _base_topo_dict()
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        ctrl_top_system = next(
            m for m in store.by_class("topSystem") if m.dn == f"topology/pod-{site.pod}/node-1/sys"
        )
        assert ctrl_top_system.attrs["version"] == site.apic_version

    def test_override_drives_controller_topSystem_independent_of_switch_version(self) -> None:
        """apic_version override must show up on the controller's topSystem/
        firmwareCtrlrRunning while switch-node fabricNode/topSystem.version
        (Node.version, PR-18) stays completely unaffected."""
        topo_dict = _base_topo_dict()
        topo_dict["fabric"]["default_apic_version"] = "5.2(7g)"
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)

        ctrl_top_system = next(
            m for m in store.by_class("topSystem") if m.dn == f"topology/pod-{site.pod}/node-1/sys"
        )
        assert ctrl_top_system.attrs["version"] == "5.2(7g)"

        ctrl_node = next(m for m in store.by_class("fabricNode") if m.dn == f"topology/pod-{site.pod}/node-1")
        assert ctrl_node.attrs["version"] == "5.2(7g)"

        fw = next(m for m in store.by_class("firmwareCtrlrRunning"))
        assert fw.attrs["version"] == "5.2(7g)"

        # Switch-node version is untouched — independence confirmed.
        leaf_id = site.leaf_nodes()[0].id
        leaf_top_system = next(
            m for m in store.by_class("topSystem") if m.dn == f"topology/pod-{site.pod}/node-{leaf_id}/sys"
        )
        assert leaf_top_system.attrs["version"] == "n9000-14.2(7f)"

    def test_override_applies_to_all_controllers(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["fabric"]["default_apic_version"] = "5.2(7g)"
        topo_dict["sites"][0]["controllers"] = 3
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)
        fw_versions = {m.attrs["version"] for m in store.by_class("firmwareCtrlrRunning")}
        assert fw_versions == {"5.2(7g)"}

    def test_show_surfaces_effective_apic_version(self) -> None:
        from aci_sim.cli import build_inventory

        topo_dict = _base_topo_dict()
        topo_dict["fabric"]["default_apic_version"] = "5.2(7g)"
        topo = Topology.model_validate(topo_dict)
        inv = build_inventory(topo)
        assert all(s["apic_version"] == "5.2(7g)" for s in inv["sites"])
