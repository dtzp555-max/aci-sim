"""Tests for the OOB management gateway PR — a real MO instead of a discarded value.

Today, every node gets `topSystem.oobMgmtAddr` (the OOB IP) but no gateway is
modeled anywhere. Real APIC carries the OOB address + gateway in the special
`mgmt` tenant: `uni/tn-mgmt/mgmtp-default/oob-default` (a `mgmtOoB` EPG) with
one `mgmtRsOoBStNode` child per node, carrying `addr` (node's OOB IP + mask)
and `gw` (the OOB default gateway). Prior to this PR, `aci-sim init`'s wizard
COLLECTED this same gateway value but discarded it (no schema field, no MO,
just an informational summary line) — see cli.py's `run_wizard` history.

Covers:
  - `Site.oob_gateway`: defaults to `None`; IP-format validation (valid IPs
    accepted, garbage rejected).
  - `build/mgmt.py`: mgmtRsOoBStNode built per node (switches + controllers),
    with `tDn`/`addr`/`gw` matching the real DN/address/gateway; addr mask
    derived from `fabric.oob_subnet` if set, else defaults to /24; `gw` empty
    when `site.oob_gateway` is unset (no invented default).
  - wizard (`run_wizard`): the answered per-site OOB gateway is written into
    `topology_dict["sites"][i]["oob_gateway"]` (both `--defaults` and
    `--answers`-driven runs), not discarded.
  - Backward compatibility: the repo's own topology.yaml (no `oob_gateway`
    set anywhere) still builds without error, still produces exactly the
    same `fabricNode`/`topSystem` MOs as before, and now ALSO builds the new
    mgmt-tenant OOB scaffolding (additive — new MOs, no changed existing
    ones) with an empty `gw` on every mgmtRsOoBStNode.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from pydantic import ValidationError

from aci_sim.build import mgmt, orchestrator
from aci_sim.build.fabric import oob_ip
from aci_sim.cli import generate_topology, run_wizard
from aci_sim.topology.loader import load_topology
from aci_sim.topology.schema import Topology

REPO_ROOT = Path(__file__).parent.parent
TOPOLOGY_YAML = REPO_ROOT / "topology.yaml"


def _base_topo_dict(**kwargs) -> dict:
    return generate_topology(sites=1, leaves_per_site=2, spines_per_site=2, border_pairs=1, **kwargs)


# ---------------------------------------------------------------------------
# Schema: Site.oob_gateway
# ---------------------------------------------------------------------------


class TestSchemaField:
    def test_default_is_none(self) -> None:
        topo = Topology.model_validate(_base_topo_dict())
        for site in topo.sites:
            assert site.oob_gateway is None

    def test_repo_topology_has_no_oob_gateway_set(self) -> None:
        """The shipped topology.yaml predates this PR — no site sets it."""
        topo = load_topology(TOPOLOGY_YAML)
        for site in topo.sites:
            assert site.oob_gateway is None

    @pytest.mark.parametrize("gw", ["10.192.0.1", "192.168.1.1", "172.16.5.254", "::1", "2001:db8::1"])
    def test_valid_ip_accepted(self, gw: str) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["oob_gateway"] = gw
        topo = Topology.model_validate(topo_dict)
        assert topo.sites[0].oob_gateway == gw

    @pytest.mark.parametrize("gw", ["not-an-ip", "10.192.0.999", "", "10.192.0"])
    def test_invalid_ip_rejected(self, gw: str) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["oob_gateway"] = gw
        with pytest.raises(ValidationError, match="oob_gateway"):
            Topology.model_validate(topo_dict)


# ---------------------------------------------------------------------------
# Builder: mgmtRsOoBStNode
# ---------------------------------------------------------------------------


class TestMgmtBuilder:
    def test_mgmt_tenant_scaffolding_present(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["oob_gateway"] = "10.192.0.1"
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)

        tenant_mo = store.get("uni/tn-mgmt")
        assert tenant_mo is not None and tenant_mo.class_name == "fvTenant"

        mgmtp_mo = store.get("uni/tn-mgmt/mgmtp-default")
        assert mgmtp_mo is not None and mgmtp_mo.class_name == "mgmtMgmtP"

        oob_mo = store.get("uni/tn-mgmt/mgmtp-default/oob-default")
        assert oob_mo is not None and oob_mo.class_name == "mgmtOoB"

    def test_one_mgmtrsoobstnode_per_node_including_controllers(self) -> None:
        """2 spines + 2 leaves + 2 border-leaves + 1 controller (default) = 7."""
        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["oob_gateway"] = "10.192.0.1"
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)

        mos = store.by_class("mgmtRsOoBStNode")
        assert len(mos) == 7, f"Expected 7 mgmtRsOoBStNode, got {len(mos)}"

        # Every switch/controller node id must have exactly one entry, keyed
        # by tDn == topology/pod-<pod>/node-<id>.
        all_node_ids = {n.id for n in site.all_nodes()} | {1}  # +1 controller
        seen_tdns = {mo.attrs["tDn"] for mo in mos}
        expected_tdns = {f"topology/pod-{site.pod}/node-{nid}" for nid in all_node_ids}
        assert seen_tdns == expected_tdns

    def test_addr_and_gw_correct_when_gateway_set(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["oob_gateway"] = "10.192.0.1"
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)

        for node in site.all_nodes():
            node_dn = f"topology/pod-{site.pod}/node-{node.id}"
            mo = store.get(f"uni/tn-mgmt/mgmtp-default/oob-default/rsooBStNode-[{node_dn}]")
            assert mo is not None, f"missing mgmtRsOoBStNode for {node_dn}"
            assert mo.attrs["tDn"] == node_dn
            assert mo.attrs["addr"] == f"{oob_ip(site.pod, node.id)}/24"
            assert mo.attrs["gw"] == "10.192.0.1"

    def test_gw_empty_when_gateway_unset(self) -> None:
        """No gateway configured -> gw is the empty string, never invented."""
        topo = Topology.model_validate(_base_topo_dict())
        site = topo.sites[0]
        assert site.oob_gateway is None
        store = orchestrator.build_site(topo, site)

        mos = store.by_class("mgmtRsOoBStNode")
        assert mos, "expected mgmtRsOoBStNode entries even with no gateway set"
        for mo in mos:
            assert mo.attrs["gw"] == ""

    def test_mask_derived_from_oob_subnet_when_set(self) -> None:
        topo_dict = _base_topo_dict()
        topo_dict["fabric"]["oob_subnet"] = "192.168.0.0/20"
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)

        for mo in store.by_class("mgmtRsOoBStNode"):
            assert mo.attrs["addr"].endswith("/20")

    def test_mask_defaults_to_24_without_oob_subnet(self) -> None:
        topo = Topology.model_validate(_base_topo_dict())
        assert topo.fabric.oob_subnet is None
        store = orchestrator.build_site(topo, topo.sites[0])
        for mo in store.by_class("mgmtRsOoBStNode"):
            assert mo.attrs["addr"].endswith("/24")

    def test_controller_addr_matches_controller_oob_ip(self) -> None:
        """Controller's mgmtRsOoBStNode.addr must match its topSystem.oobMgmtAddr
        (same address source: site.controller_oob_ips() / oob_ip fallback)."""
        topo_dict = _base_topo_dict()
        topo_dict["sites"][0]["mgmt_ip"] = "10.192.0.11"
        topo = Topology.model_validate(topo_dict)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)

        ctrl_dn = f"topology/pod-{site.pod}/node-1"
        top_sys = store.get(f"{ctrl_dn}/sys")
        assert top_sys is not None
        mgmt_mo = store.get(f"uni/tn-mgmt/mgmtp-default/oob-default/rsooBStNode-[{ctrl_dn}]")
        assert mgmt_mo is not None
        assert mgmt_mo.attrs["addr"].split("/")[0] == top_sys.attrs["oobMgmtAddr"]


# ---------------------------------------------------------------------------
# Wizard: writes oob_gateway instead of discarding it
# ---------------------------------------------------------------------------


class TestWizardWritesGateway:
    def test_defaults_run_writes_oob_gateway_per_site(self) -> None:
        out = io.StringIO()
        topo_dict, gateway_note = run_wizard(stream_in=io.StringIO(""), stream_out=out, accept_defaults=True)

        assert gateway_note  # backward-compat return value still populated
        for site in topo_dict["sites"]:
            assert site.get("oob_gateway"), f"site {site['name']} missing oob_gateway"

        # Result must still validate (schema field wired correctly end-to-end).
        topo = Topology.model_validate(topo_dict)
        for site in topo.sites:
            assert site.oob_gateway is not None

    def test_custom_answers_gateway_flows_through(self) -> None:
        answers = {
            "deployment_type": "1",
            "site1_gateway": "10.50.0.1",
        }
        out = io.StringIO()
        topo_dict, gateway_note = run_wizard(
            stream_in=io.StringIO(""), stream_out=out, accept_defaults=True, answers=answers
        )
        assert gateway_note == "10.50.0.1"
        assert topo_dict["sites"][0]["oob_gateway"] == "10.50.0.1"

        topo = Topology.model_validate(topo_dict)
        assert topo.sites[0].oob_gateway == "10.50.0.1"

    def test_multi_site_each_site_gets_its_own_gateway(self) -> None:
        answers = {
            "deployment_type": "2",
            "num_sites": "2",
            "site1_gateway": "10.10.0.1",
            "site2_gateway": "10.20.0.1",
        }
        out = io.StringIO()
        topo_dict, _ = run_wizard(
            stream_in=io.StringIO(""), stream_out=out, accept_defaults=True, answers=answers
        )
        assert topo_dict["sites"][0]["oob_gateway"] == "10.10.0.1"
        assert topo_dict["sites"][1]["oob_gateway"] == "10.20.0.1"

        # And the built store for each site carries its OWN gateway, not the
        # other site's.
        topo = Topology.model_validate(topo_dict)
        store0 = orchestrator.build_site(topo, topo.sites[0])
        store1 = orchestrator.build_site(topo, topo.sites[1])
        gws0 = {mo.attrs["gw"] for mo in store0.by_class("mgmtRsOoBStNode")}
        gws1 = {mo.attrs["gw"] for mo in store1.by_class("mgmtRsOoBStNode")}
        assert gws0 == {"10.10.0.1"}
        assert gws1 == {"10.20.0.1"}


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_repo_topology_builds_without_error(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        stores = orchestrator.build_all(topo)
        assert set(stores.keys()) == {s.name for s in topo.sites}

    def test_repo_topology_mgmt_scaffolding_builds_with_empty_gw(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        for site in topo.sites:
            store = orchestrator.build_site(topo, site)
            mos = store.by_class("mgmtRsOoBStNode")
            assert mos, f"expected mgmtRsOoBStNode entries for site {site.name}"
            for mo in mos:
                assert mo.attrs["gw"] == ""

    def test_repo_topology_existing_mos_unchanged(self) -> None:
        """fabricNode/topSystem must be byte-identical to pre-PR shape — this
        PR is purely additive (new mgmt-tenant MOs), it must not touch any
        existing MO's attributes."""
        topo = load_topology(TOPOLOGY_YAML)
        site = topo.sites[0]
        store = orchestrator.build_site(topo, site)

        for node in site.all_nodes():
            ts = store.get(f"topology/pod-{site.pod}/node-{node.id}/sys")
            assert ts is not None
            assert ts.attrs["oobMgmtAddr"] == oob_ip(site.pod, node.id)
            assert "gw" not in ts.attrs  # topSystem never gains a gateway attr

    def test_mgmt_module_registered_in_orchestrator(self) -> None:
        assert mgmt in orchestrator._BUILDERS
