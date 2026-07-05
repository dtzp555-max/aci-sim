"""Tests for PR-22 — `aci-sim init`, an interactive APIC-setup-style wizard.

Covers:
  - `--defaults` (and non-TTY stdin) accepts every suggested default without
    ever reading stdin, producing a file that passes `load_topology()` +
    validation and is BYTE-IDENTICAL to calling `generate_topology()`
    directly with the same known default parameter set (the wizard is a
    thin front-end — it must not duplicate topology-emission logic or drift
    from it).
  - A multi-site run with `controllers=3` produces `controller_ips`
    sequential from the answered OOB IP (PR-21's `Site.controller_ips`).
  - Custom answers (via `--answers`/`run_wizard(answers=...)` and via
    scripted stdin) override the suggested defaults.
  - The deployment-type branch: single-fabric -> exactly 1 site, no ISN/NDO
    prompts/section; multi-site -> ISN block printed + N sites.
  - Non-TTY stdin auto-accepts defaults with no hang (exercised via
    `subprocess` with stdin explicitly closed/empty, which is what makes
    `sys.stdin.isatty()` false in a test process).

No test ever blocks on real interactive stdin — every wizard drive in this
file uses `io.StringIO` (fully-buffered, returns EOF instead of hanging) or
`--defaults`/`accept_defaults=True` (skips reading stdin entirely).
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import yaml

from aci_sim.cli import generate_topology, run_wizard
from aci_sim.topology.loader import load_topology
from aci_sim.topology.schema import Topology

REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wizard_defaults_topology() -> dict:
    """The topology `aci-sim init --defaults` produces at every wizard default
    (2 sites, multi-site — the wizard's own Step-0 default is "2")."""
    out = io.StringIO()
    topo, _gateway = run_wizard(stream_in=io.StringIO(""), stream_out=out, accept_defaults=True)
    return topo


def _direct_equivalent_topology() -> dict:
    """The SAME topology built by calling `generate_topology()` directly with
    the wizard's own known default values — i.e. what a caller would get if
    they replicated the wizard's Step-1/Step-2 defaults by hand. Asserting
    the wizard's output equals this proves `run_wizard` is a thin front-end
    that does not duplicate/diverge from `generate_topology()`."""
    site_overrides = [
        {
            "name": "Site1-IT-ACI",
            "id": "1",
            "mgmt_ip": "10.192.0.11",
            "fabric_name": "Site1-IT-ACI",
            "asn": 65001,
            "pod": 1,
            "controllers": 1,
            "controller_ips": ["10.192.0.11"],
            # OOB-gateway PR: the wizard's own default-gateway derivation is
            # "first usable IP of the /24 block containing the site's APIC
            # OOB IP" (run_wizard: gw_block - (gw_block % 256) + 1).
            "oob_gateway": "10.192.0.1",
        },
        {
            "name": "Site2-IT-ACI",
            "id": "2",
            "mgmt_ip": "10.192.128.11",
            "fabric_name": "Site2-IT-ACI",
            "asn": 65002,
            "pod": 1,
            "controllers": 1,
            "controller_ips": ["10.192.128.11"],
            "oob_gateway": "10.192.128.1",
        },
    ]
    topo = generate_topology(
        sites=2,
        leaves_per_site=[2, 2],
        spines_per_site=[2, 2],
        border_pairs=[1, 1],
        fabric_name_prefix="",
        ndo_ip="10.192.0.10",
        tep_pool="10.0.0.0/16",
        infra_vlan=3967,
        gipo_pool="225.0.0.0/15",
        isn_ospf_area="0.0.0.0",
        isn_mtu=9150,
        site_overrides=site_overrides,
    )
    topo["fabric"]["name"] = "MULTI-SITE-ACI-IT-ACI"
    topo["fabric"]["oob_subnet"] = "192.168.0.0/16"
    topo["isn"]["enabled"] = True
    # Admin account step (Step 0): the wizard's own defaults (admin/cisco),
    # ndo_same_account=yes so no ndo_username/ndo_password keys are written.
    topo["auth"] = {"username": "admin", "password": "cisco"}
    return topo


# ---------------------------------------------------------------------------
# 1. --defaults consistency: passes validation + equals generate_topology()
#    called directly with the wizard's own default values.
# ---------------------------------------------------------------------------


class TestDefaultsConsistency:
    def test_defaults_topology_passes_validation(self) -> None:
        topo_dict = _wizard_defaults_topology()
        topo = Topology.model_validate(topo_dict)
        assert len(topo.sites) == 2

    def test_defaults_topology_loads_and_builds_from_disk(self, tmp_path: Path) -> None:
        topo_dict = _wizard_defaults_topology()
        out_file = tmp_path / "wizard.topology.yaml"
        out_file.write_text(yaml.dump(topo_dict, sort_keys=False), encoding="utf-8")

        topo = load_topology(out_file)  # same validation `aci-sim validate` runs
        assert len(topo.sites) == 2

        from aci_sim.build import orchestrator

        for site in topo.sites:
            store = orchestrator.build_site(topo, site)
            assert store.by_class("fabricNode")

    def test_defaults_topology_equals_direct_generate_topology_call(self) -> None:
        wizard_topo = _wizard_defaults_topology()
        direct_topo = _direct_equivalent_topology()
        assert wizard_topo == direct_topo


# ---------------------------------------------------------------------------
# 2. Multi-site + controllers=3 -> sequential controller_ips from the
#    answered OOB IP (PR-21's Site.controller_ips).
# ---------------------------------------------------------------------------


class TestControllersThreeSequentialIps:
    def test_answers_file_controllers_3_sequential_from_oob_ip(self) -> None:
        answers = {
            "deployment_type": "2",
            "num_sites": "2",
            "site1_apic1_ip": "10.50.0.20",
            "site1_controllers": "3",
        }
        out = io.StringIO()
        topo, _gw = run_wizard(stream_in=io.StringIO(""), stream_out=out, accept_defaults=False, answers=answers)

        site1 = topo["sites"][0]
        assert site1["controllers"] == 3
        assert site1["controller_ips"] == ["10.50.0.20", "10.50.0.21", "10.50.0.22"]

        # Round-trips through full validation + Site.controller_oob_ips().
        validated = Topology.model_validate(topo)
        assert validated.sites[0].controller_oob_ips() == ["10.50.0.20", "10.50.0.21", "10.50.0.22"]

    def test_scripted_stdin_controllers_3_sequential_suggestion_accepted(self) -> None:
        """Drives the wizard with a real scripted stdin stream (not
        --answers/accept_defaults) accepting the SUGGESTED sequential IP at
        each of the 3 controller prompts by pressing ENTER (blank line)."""
        stdin_script = (
            "\n"           # admin username -> default
            "\n"           # admin password -> default
            "\n"           # ndo_same_account -> default (yes)
            "1\n"          # deployment type -> single fabric
            "\n"           # site name -> default
            "\n"           # site id -> default
            "\n"           # asn -> default
            "\n"           # pod -> default
            "3\n"          # controllers -> 3
            "10.7.7.10\n"  # apic1 ip -> custom
            "\n"           # apic2 ip -> accept suggested 10.7.7.11
            "\n"           # apic3 ip -> accept suggested 10.7.7.12
        )
        out = io.StringIO()
        topo, _gw = run_wizard(stream_in=io.StringIO(stdin_script), stream_out=out, accept_defaults=False)
        site1 = topo["sites"][0]
        assert site1["controller_ips"] == ["10.7.7.10", "10.7.7.11", "10.7.7.12"]

    def test_generated_file_with_controllers_3_validates_and_builds(self, tmp_path: Path) -> None:
        answers = {"deployment_type": "1", "site1_controllers": "3", "site1_apic1_ip": "10.192.0.11"}
        out = io.StringIO()
        topo, _gw = run_wizard(stream_in=io.StringIO(""), stream_out=out, accept_defaults=False, answers=answers)

        out_file = tmp_path / "ctrl3.topology.yaml"
        out_file.write_text(yaml.dump(topo, sort_keys=False), encoding="utf-8")
        loaded = load_topology(out_file)  # raises on validation failure

        from aci_sim.build import orchestrator

        store = orchestrator.build_site(loaded, loaded.sites[0])
        ctrl_top_systems = {
            mo.attrs["id"]: mo.attrs["oobMgmtAddr"]
            for mo in store.by_class("topSystem")
            if mo.attrs.get("role") == "controller"
        }
        assert ctrl_top_systems == {"1": "10.192.0.11", "2": "10.192.0.12", "3": "10.192.0.13"}


# ---------------------------------------------------------------------------
# 3. Custom answers override defaults (both --answers-style dict and
#    scripted stdin).
# ---------------------------------------------------------------------------


class TestCustomAnswersOverrideDefaults:
    def test_answers_dict_overrides_site_name_and_asn(self) -> None:
        answers = {
            "deployment_type": "1",
            "site1_name": "CustomFabric",
            "site1_asn": "70000",
        }
        out = io.StringIO()
        topo, _gw = run_wizard(stream_in=io.StringIO(""), stream_out=out, accept_defaults=False, answers=answers)
        assert topo["sites"][0]["name"] == "CustomFabric"
        assert topo["sites"][0]["asn"] == 70000
        # Untouched fields still take the wizard's own default.
        assert topo["sites"][0]["pod"] == 1

    def test_scripted_stdin_overrides_fabric_name(self) -> None:
        # admin username/password/ndo_same_account -> default (3 blank lines),
        # then deployment=1, site name=MyCustomLab, rest EOF->defaults.
        stdin_script = "\n\n\n1\nMyCustomLab\n"
        out = io.StringIO()
        topo, _gw = run_wizard(stream_in=io.StringIO(stdin_script), stream_out=out, accept_defaults=False)
        assert topo["sites"][0]["name"] == "MyCustomLab"

    def test_custom_topology_still_validates(self) -> None:
        answers = {"deployment_type": "1", "site1_name": "CustomFabric", "site1_leaves": "4", "site1_spines": "3"}
        out = io.StringIO()
        topo, _gw = run_wizard(stream_in=io.StringIO(""), stream_out=out, accept_defaults=False, answers=answers)
        validated = Topology.model_validate(topo)
        assert len(validated.sites[0].leaf_nodes()) == 4
        assert len(validated.sites[0].spine_nodes()) == 3

    def test_invalid_custom_answer_reprompts_then_accepts_valid_value(self) -> None:
        """A bad value typed at a validated prompt re-prompts (per _prompt's
        validator contract) rather than silently accepting garbage."""
        stdin_script = (
            "\n"         # admin username -> default
            "\n"         # admin password -> default
            "\n"         # ndo_same_account -> default (yes)
            "1\n"        # deployment type
            "\n"         # site name
            "\n"         # site id
            "\n"         # asn
            "\n"         # pod
            "not-a-number\n2\n"  # controllers: invalid then valid
        )
        out = io.StringIO()
        topo, _gw = run_wizard(stream_in=io.StringIO(stdin_script), stream_out=out, accept_defaults=False)
        assert topo["sites"][0]["controllers"] == 2
        assert "must be an integer" in out.getvalue()


# ---------------------------------------------------------------------------
# 4. Deployment-type branch: single-fabric vs multi-site.
# ---------------------------------------------------------------------------


class TestDeploymentTypeBranch:
    def test_single_fabric_yields_exactly_one_site_no_isn(self) -> None:
        out = io.StringIO()
        topo, _gw = run_wizard(
            stream_in=io.StringIO(""), stream_out=out, accept_defaults=False, answers={"deployment_type": "1"}
        )
        assert len(topo["sites"]) == 1
        assert topo["isn"]["enabled"] is False
        # "Step 3" is the multi-site-only NDO+ISN step (run_wizard) — must be
        # absent entirely on the single-fabric branch.
        assert "Step 3" not in out.getvalue()
        assert "ISN" not in out.getvalue().split("Step 3", 1)[-1] if "Step 3" in out.getvalue() else True

    def test_single_fabric_never_prompts_for_ndo_or_isn_fields(self) -> None:
        # Deliberately supply an --answers value for an ISN-only field to
        # prove it's simply never consulted on the single-fabric branch
        # (rather than testing an absence of output, which is brittle).
        out = io.StringIO()
        topo, _gw = run_wizard(
            stream_in=io.StringIO(""),
            stream_out=out,
            accept_defaults=False,
            answers={"deployment_type": "1", "isn_mtu": "1234"},
        )
        assert topo["isn"]["mtu"] == 9150  # wizard default, NOT the supplied 1234 (never asked)

    def test_multi_site_default_yields_two_sites_and_isn_enabled(self) -> None:
        out = io.StringIO()
        topo, _gw = run_wizard(
            stream_in=io.StringIO(""), stream_out=out, accept_defaults=False, answers={"deployment_type": "2"}
        )
        assert len(topo["sites"]) == 2
        assert topo["isn"]["enabled"] is True
        assert "Step 3" in out.getvalue()

    def test_multi_site_n_sites_override(self) -> None:
        out = io.StringIO()
        topo, _gw = run_wizard(
            stream_in=io.StringIO(""),
            stream_out=out,
            accept_defaults=False,
            answers={"deployment_type": "2", "num_sites": "3"},
        )
        assert len(topo["sites"]) == 3
        assert topo["isn"]["enabled"] is True
        # Sequential ASN/site-id/name defaults continue past site 2.
        assert topo["sites"][2]["asn"] == 65003
        assert topo["sites"][2]["name"] == "Site3-IT-ACI"

    def test_multi_site_topology_validates_and_builds_all_sites(self) -> None:
        out = io.StringIO()
        topo, _gw = run_wizard(
            stream_in=io.StringIO(""), stream_out=out, accept_defaults=True, answers={"deployment_type": "2"}
        )
        topo_obj = Topology.model_validate(topo)

        from aci_sim.build import orchestrator

        for site in topo_obj.sites:
            store = orchestrator.build_site(topo_obj, site)
            assert store.by_class("fabricNode")


# ---------------------------------------------------------------------------
# 5. Non-TTY stdin auto-accepts defaults with no hang.
# ---------------------------------------------------------------------------


class TestNonTtyAutoAccept:
    def test_accept_defaults_flag_never_reads_stdin(self) -> None:
        """accept_defaults=True must short-circuit _prompt before it ever
        calls stream_in.readline() — assert by passing a stream that raises
        if read from."""

        class _ExplodingStream(io.StringIO):
            def readline(self, *a, **kw):  # noqa: D401 - test double
                raise AssertionError("stdin should never be read when accept_defaults=True")

        out = io.StringIO()
        topo, _gw = run_wizard(stream_in=_ExplodingStream(""), stream_out=out, accept_defaults=True)
        assert len(topo["sites"]) == 2  # completed without ever touching stdin

    def test_cli_subprocess_with_empty_stdin_completes_without_defaults_flag(self, tmp_path: Path) -> None:
        """End-to-end: invoke the real `aci-sim init` subcommand (via -m,
        matching how the CLI is actually launched) with stdin explicitly
        set to DEVNULL (non-TTY, immediate EOF) and NO --defaults flag —
        must still complete (isatty()==False auto-enables accept_defaults)
        rather than hang waiting for input. A timeout means this test caught
        a real hang, not a flaky assertion."""
        out_file = tmp_path / "auto.topology.yaml"
        result = subprocess.run(
            [sys.executable, "-m", "aci_sim.cli", "init", "-o", str(out_file)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert out_file.exists()
        loaded = load_topology(out_file)
        assert len(loaded.sites) == 2


# ---------------------------------------------------------------------------
# Misc: --answers file loading (YAML and JSON), CLI wiring smoke test.
# ---------------------------------------------------------------------------


class TestAnswersFileLoading:
    def test_answers_file_yaml(self, tmp_path: Path) -> None:
        answers_file = tmp_path / "answers.yaml"
        answers_file.write_text("deployment_type: '1'\nsite1_name: FromYaml\n", encoding="utf-8")
        out_file = tmp_path / "out.yaml"

        result = subprocess.run(
            [
                sys.executable, "-m", "aci_sim.cli", "init",
                "-o", str(out_file), "--answers", str(answers_file),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        loaded = load_topology(out_file)
        assert loaded.sites[0].name == "FromYaml"

    def test_answers_file_json(self, tmp_path: Path) -> None:
        answers_file = tmp_path / "answers.json"
        answers_file.write_text('{"deployment_type": "1", "site1_name": "FromJson"}', encoding="utf-8")
        out_file = tmp_path / "out.yaml"

        result = subprocess.run(
            [
                sys.executable, "-m", "aci_sim.cli", "init",
                "-o", str(out_file), "--answers", str(answers_file),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        loaded = load_topology(out_file)
        assert loaded.sites[0].name == "FromJson"

    def test_answers_file_not_found_reports_error_exit_1(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "aci_sim.cli", "init", "--answers", str(tmp_path / "nope.yaml")],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 1
        assert "ERROR" in result.stderr


class TestConfirmWrite:
    def test_confirm_write_false_aborts_without_writing(self, tmp_path: Path) -> None:
        answers_file = tmp_path / "answers.yaml"
        answers_file.write_text("deployment_type: '1'\nconfirm_write: 'no'\n", encoding="utf-8")
        out_file = tmp_path / "should_not_exist.yaml"

        result = subprocess.run(
            [
                sys.executable, "-m", "aci_sim.cli", "init",
                "-o", str(out_file), "--answers", str(answers_file),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 1
        assert not out_file.exists()
        assert "Aborted" in result.stdout


class TestCliSmoke:
    def test_init_appears_in_help(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "aci_sim.cli", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=REPO_ROOT,
        )
        assert "init" in result.stdout

    def test_defaults_flag_and_output_flag_smoke(self, tmp_path: Path) -> None:
        out_file = tmp_path / "smoke.topology.yaml"
        result = subprocess.run(
            [sys.executable, "-m", "aci_sim.cli", "init", "--defaults", "-o", str(out_file)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK: 2 site(s)" in result.stdout
        loaded = load_topology(out_file)
        assert len(loaded.sites) == 2
