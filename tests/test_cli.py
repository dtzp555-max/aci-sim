"""Tests for aci_sim/cli.py — the `aci-sim` topology CLI (PR-17).

Covers `validate` (positive + negative), `show` (text + --json, asserting
derived loopback/OOB IPs for a known node), `new` (scaffold generation +
re-validation of the output), and `run`'s env/argv construction (no live
socket binding — build_run_env_argv is a pure helper factored out of
cmd_run for exactly this reason).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from aci_sim.cli import (
    _ensure_certs,
    build_parser,
    build_run_env_argv,
    generate_topology,
    main,
)
from aci_sim.topology.loader import load_topology
from aci_sim.topology.schema import Topology

REPO_ROOT = Path(__file__).parent.parent
TOPOLOGY_YAML = REPO_ROOT / "topology.yaml"


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_validate_default_topology_ok(self, capsys) -> None:
        rc = main(["validate", str(TOPOLOGY_YAML)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "OK:" in out
        assert "2 site(s)" in out
        assert "3 tenant(s)" in out
        assert "LAB1" in out and "LAB2" in out  # per-site breakdown lines
        assert "leaves" in out and "spines" in out and "border leaves" in out

    def test_validate_missing_file(self, capsys) -> None:
        rc = main(["validate", "/nonexistent/path/topology.yaml"])
        err = capsys.readouterr().err
        assert rc != 0
        assert "not found" in err.lower() or "ERROR" in err

    def test_validate_duplicate_node_id(self, tmp_path, capsys) -> None:
        bad = {
            "fabric": {"name": "BAD-ACI"},
            "sites": [
                {
                    "name": "BADSITE",
                    "id": "1",
                    "apic_host": "127.0.0.1:8443",
                    "asn": 65001,
                    "spines": 2,
                    "leaves": [
                        {"id": 101, "name": "leaf-a"},
                        {"id": 101, "name": "leaf-b"},  # duplicate id -> validation error
                    ],
                }
            ],
        }
        bad_path = tmp_path / "bad_topology.yaml"
        bad_path.write_text(yaml.dump(bad), encoding="utf-8")

        rc = main(["validate", str(bad_path)])
        err = capsys.readouterr().err
        assert rc != 0
        assert "collide" in err.lower() or "error" in err.lower()

    def test_validate_bad_ip_pool(self, tmp_path, capsys) -> None:
        bad = {
            "fabric": {"name": "BAD-ACI", "loopback_pool": "not-a-cidr"},
            "isn": {"enabled": False},
            "sites": [
                {
                    "name": "BADSITE",
                    "id": "1",
                    "apic_host": "127.0.0.1:8443",
                    "asn": 65001,
                    "spines": 1,
                    "leaves": 1,
                }
            ],
        }
        bad_path = tmp_path / "bad_ip.yaml"
        bad_path.write_text(yaml.dump(bad), encoding="utf-8")

        rc = main(["validate", str(bad_path)])
        err = capsys.readouterr().err
        assert rc != 0
        assert "loopback_pool" in err or "cidr" in err.lower()

    def test_validate_default_arg_is_topology_yaml(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["validate"])
        assert args.topology == "topology.yaml"


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


class TestShow:
    def test_show_text_contains_known_node_derived_ips(self, capsys) -> None:
        rc = main(["show", str(TOPOLOGY_YAML)])
        out = capsys.readouterr().out
        assert rc == 0
        # Node 101 (LAB1-ACI-LF101, pod 1) -> loopback 10.1.101.1 / oob 192.168.1.101
        assert "10.1.101.1" in out
        assert "192.168.1.101" in out
        assert "LAB1" in out
        assert "LAB2" in out

    def test_show_json_parses_and_has_known_node(self) -> None:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["show", str(TOPOLOGY_YAML), "--json"])
        assert rc == 0
        data = json.loads(buf.getvalue())

        assert data["fabric_name"] == "LAB-IT-ACI"
        site_names = {s["name"] for s in data["sites"]}
        assert {"LAB1", "LAB2"} <= site_names

        lab1 = next(s for s in data["sites"] if s["name"] == "LAB1")
        node_101 = next(n for n in lab1["nodes"] if n["id"] == 101)
        assert node_101["loopback_ip"] == "10.1.101.1"
        assert node_101["oob_ip"] == "192.168.1.101"
        assert node_101["role"] == "leaf"

        # Port bindings: default (non-sandbox) mode -> SIM_BIND host + fixed ports.
        roles = {b["role"] for b in data["ports"]}
        assert "apic" in roles
        assert "ndo" in roles

    def test_show_missing_file(self, capsys) -> None:
        rc = main(["show", "/nonexistent/path/topology.yaml"])
        assert rc != 0
        assert capsys.readouterr().err

    # -- PR-18: Tier-1 fabric params surfaced in `show` --------------------

    def test_show_json_has_tier1_fabric_fields(self) -> None:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["show", str(TOPOLOGY_YAML), "--json"])
        assert rc == 0
        data = json.loads(buf.getvalue())

        assert data["tep_pool"] == "10.0.0.0/16"
        assert data["infra_vlan"] == 3967
        assert data["gipo_pool"] == "225.0.0.0/15"

        lab1 = next(s for s in data["sites"] if s["name"] == "LAB1")
        # PR-21: single-APIC-per-site is now the default (was 3 pre-PR-21).
        assert lab1["controllers"] == 1
        node_101 = next(n for n in lab1["nodes"] if n["id"] == 101)
        assert node_101["serial"] == "SAL10101"

    def test_show_text_contains_tier1_fields(self, capsys) -> None:
        rc = main(["show", str(TOPOLOGY_YAML)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "tep_pool=10.0.0.0/16" in out
        assert "infra_vlan=3967" in out
        assert "gipo_pool=225.0.0.0/15" in out
        assert "controllers=1" in out  # PR-21: single-APIC-per-site default (was 3)
        assert "SAL10101" in out

    # -- PR-19: Tier-2 ISN params + VMM domains surfaced in `show` ----------

    def test_show_json_has_tier2_isn_fields(self) -> None:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["show", str(TOPOLOGY_YAML), "--json"])
        assert rc == 0
        data = json.loads(buf.getvalue())

        assert data["isn_enabled"] is True
        assert data["isn_peer_mesh"] == "full"
        assert data["isn_ospf_area"] == "0.0.0.0"
        assert data["isn_mtu"] == 9150
        assert data["vmm_domains"] == []

    def test_show_text_contains_tier2_isn_fields(self, capsys) -> None:
        rc = main(["show", str(TOPOLOGY_YAML)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ospf_area=0.0.0.0" in out
        assert "mtu=9150" in out


# ---------------------------------------------------------------------------
# new (scaffold)
# ---------------------------------------------------------------------------


class TestNew:
    def test_generate_topology_default_shape(self) -> None:
        topo_dict = generate_topology()
        # Must validate cleanly via the real Pydantic model.
        Topology.model_validate(topo_dict)
        assert len(topo_dict["sites"]) == 2

    def test_new_sites3_leaves4_stdout(self, capsys) -> None:
        rc = main(["new", "--sites", "3", "--leaves-per-site", "4"])
        out = capsys.readouterr().out
        assert rc == 0

        parsed = yaml.safe_load(out)
        assert len(parsed["sites"]) == 3
        for site in parsed["sites"]:
            assert len(site["leaves"]) == 4

        # Re-validate through the full loader-equivalent path.
        topo = Topology.model_validate(parsed)
        assert len(topo.sites) == 3
        for site in topo.sites:
            assert len(site.leaf_nodes()) == 4

    def test_new_node_id_offsets(self) -> None:
        topo_dict = generate_topology(sites=3, leaves_per_site=4, spines_per_site=2, border_pairs=1)
        topo = Topology.model_validate(topo_dict)

        expected_offsets = [0, 200, 400]
        for site, offset in zip(topo.sites, expected_offsets, strict=True):
            leaf_ids = sorted(n.id for n in site.leaf_nodes())
            assert leaf_ids == [101 + offset + i for i in range(4)]
            spine_ids = sorted(n.id for n in site.spine_nodes())
            assert spine_ids == [201 + offset + i for i in range(2)]
            # Border leaves must land outside both the leaf (101-104+offset)
            # and spine (201-202+offset) ranges -- no fixed 103/104 assumption
            # here since that collides once leaves_per_site > 2 (see
            # generate_topology's border_base logic).
            border_ids = sorted(bl.id for bl in site.border_leaves)
            assert len(border_ids) == 2
            assert all(bid not in leaf_ids and bid not in spine_ids for bid in border_ids)
            assert border_ids[1] == border_ids[0] + 1  # a vPC pair, contiguous ids

    def test_new_default_shape_matches_documented_border_ids(self) -> None:
        # The default 2-leaf/2-spine shape should reproduce the exact
        # documented example from topology/schema.py's module docstring
        # (site 0: border 103,104; site 1: border 303,304).
        topo_dict = generate_topology(sites=2, leaves_per_site=2, spines_per_site=2, border_pairs=1)
        topo = Topology.model_validate(topo_dict)
        site0_border = sorted(bl.id for bl in topo.sites[0].border_leaves)
        site1_border = sorted(bl.id for bl in topo.sites[1].border_leaves)
        assert site0_border == [103, 104]
        assert site1_border == [303, 304]

    def test_new_writes_to_file(self, tmp_path) -> None:
        out_path = tmp_path / "scaffold.yaml"
        rc = main(["new", "--sites", "1", "--leaves-per-site", "2", "-o", str(out_path)])
        assert rc == 0
        assert out_path.exists()

        # Must pass the same load_topology() path `aci-sim validate` uses.
        topo = load_topology(out_path)
        assert len(topo.sites) == 1
        assert len(topo.sites[0].leaf_nodes()) == 2

        # And `aci-sim validate` itself must accept it end-to-end.
        rc2 = main(["validate", str(out_path)])
        assert rc2 == 0

    def test_new_custom_asn_count_mismatch_errors(self, capsys) -> None:
        rc = main(["new", "--sites", "2", "--asn", "65001"])  # only 1 value for 2 sites
        err = capsys.readouterr().err
        assert rc != 0
        assert "asn" in err.lower()

    def test_new_single_site_disables_full_isn_mesh(self) -> None:
        # ISN peer_mesh:full requires >=2 sites (schema.py); scaffolding a
        # single-site topology must not trip that validator.
        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=0)
        topo = Topology.model_validate(topo_dict)
        assert len(topo.sites) == 1
        assert topo.isn.enabled is False

    # -- PR-18: Tier-1 fabric params in `new` scaffold ----------------------

    def test_new_controllers_flag_stdout(self, capsys) -> None:
        rc = main(["new", "--sites", "2", "--controllers", "5"])
        out = capsys.readouterr().out
        assert rc == 0
        parsed = yaml.safe_load(out)
        for site in parsed["sites"]:
            assert site["controllers"] == 5
        topo = Topology.model_validate(parsed)
        for site in topo.sites:
            assert site.controllers == 5

    def test_new_tier1_fabric_flags_stdout(self, capsys) -> None:
        rc = main(
            [
                "new",
                "--sites",
                "1",
                "--leaves-per-site",
                "1",
                "--spines-per-site",
                "1",
                "--border-pairs",
                "0",
                "--tep-pool",
                "172.16.0.0/16",
                "--infra-vlan",
                "100",
                "--gipo-pool",
                "226.0.0.0/15",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        parsed = yaml.safe_load(out)
        assert parsed["fabric"]["tep_pool"] == "172.16.0.0/16"
        assert parsed["fabric"]["infra_vlan"] == 100
        assert parsed["fabric"]["gipo_pool"] == "226.0.0.0/15"
        topo = Topology.model_validate(parsed)
        assert topo.fabric.tep_pool == "172.16.0.0/16"
        assert topo.fabric.infra_vlan == 100
        assert topo.fabric.gipo_pool == "226.0.0.0/15"

    def test_new_default_shape_has_default_tier1_values(self) -> None:
        topo_dict = generate_topology()
        assert topo_dict["fabric"]["tep_pool"] == "10.0.0.0/16"
        assert topo_dict["fabric"]["infra_vlan"] == 3967
        assert topo_dict["fabric"]["gipo_pool"] == "225.0.0.0/15"
        for site in topo_dict["sites"]:
            # PR-21: single-APIC-per-site is now the default (was 3 pre-PR-21).
            assert site["controllers"] == 1

    # -- PR-19: Tier-2 ISN flags + leaf/spine count elasticity --------------

    def test_new_isn_flags_stdout(self, capsys) -> None:
        rc = main(
            [
                "new",
                "--sites", "2",
                "--leaves-per-site", "1",
                "--spines-per-site", "1",
                "--border-pairs", "0",
                "--isn-ospf-area", "10.0.0.0",
                "--isn-mtu", "9000",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        parsed = yaml.safe_load(out)
        assert parsed["isn"]["ospf_area"] == "10.0.0.0"
        assert parsed["isn"]["mtu"] == 9000
        topo = Topology.model_validate(parsed)
        assert topo.isn.ospf_area == "10.0.0.0"
        assert topo.isn.mtu == 9000

    def test_new_default_shape_has_default_tier2_isn_values(self) -> None:
        topo_dict = generate_topology()
        assert topo_dict["isn"]["ospf_area"] == "0.0.0.0"
        assert topo_dict["isn"]["mtu"] == 9150

    def test_new_8_leaves_4_spines_via_cli(self, capsys) -> None:
        rc = main(
            ["new", "--sites", "2", "--leaves-per-site", "8", "--spines-per-site", "4"]
        )
        out = capsys.readouterr().out
        assert rc == 0
        parsed = yaml.safe_load(out)
        topo = Topology.model_validate(parsed)
        for site in topo.sites:
            assert len(site.leaf_nodes()) == 8
            assert len(site.spine_nodes()) == 4

    def test_new_leaves_per_site_overrun_errors_cleanly(self, capsys) -> None:
        rc = main(
            ["new", "--sites", "2", "--leaves-per-site", "150", "--spines-per-site", "4"]
        )
        err = capsys.readouterr().err
        assert rc != 0
        assert "leaves-per-site" in err


# ---------------------------------------------------------------------------
# run — env/argv construction only, no live boot
# ---------------------------------------------------------------------------


class TestRunEnvArgv:
    def test_build_run_env_argv_sets_topology_path(self) -> None:
        env, argv = build_run_env_argv("custom/topology.yaml", base_env={"PATH": "/usr/bin"})
        assert env["TOPOLOGY_PATH"] == "custom/topology.yaml"
        assert env["PATH"] == "/usr/bin"  # base env preserved
        assert argv == [sys.executable, "-m", "aci_sim.runtime.supervisor"]

    def test_build_run_env_argv_passthrough_vars_preserved(self) -> None:
        base_env = {"SIM_BIND": "0.0.0.0", "SIM_SANDBOX": "1", "UNRELATED": "x"}
        env, _argv = build_run_env_argv("topology.yaml", base_env=base_env)
        assert env["SIM_BIND"] == "0.0.0.0"
        assert env["SIM_SANDBOX"] == "1"
        assert env["UNRELATED"] == "x"
        assert env["TOPOLOGY_PATH"] == "topology.yaml"

    def test_run_missing_topology_errors_without_exec(self, capsys) -> None:
        rc = main(["run", "/nonexistent/path/topology.yaml"])
        err = capsys.readouterr().err
        assert rc != 0
        assert "not found" in err.lower()

    def test_run_invalid_topology_errors_without_exec(self, tmp_path, capsys) -> None:
        bad_path = tmp_path / "bad.yaml"
        bad_path.write_text(
            yaml.dump(
                {
                    "fabric": {"name": "BAD"},
                    "sites": [
                        {
                            "name": "S",
                            "id": "1",
                            "apic_host": "h",
                            "asn": 1,
                            "leaves": [{"id": 1, "name": "a"}, {"id": 1, "name": "b"}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        rc = main(["run", str(bad_path)])
        err = capsys.readouterr().err
        assert rc != 0
        assert err

    def test_ensure_certs_noop_when_already_present(self, tmp_path) -> None:
        cert = tmp_path / "sim.crt"
        key = tmp_path / "sim.key"
        cert.write_text("existing-cert", encoding="utf-8")
        key.write_text("existing-key", encoding="utf-8")

        # Must not touch (or attempt to regenerate) files that already exist.
        _ensure_certs(str(cert), str(key))

        assert cert.read_text(encoding="utf-8") == "existing-cert"
        assert key.read_text(encoding="utf-8") == "existing-key"
