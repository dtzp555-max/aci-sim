"""Tests for the Admin account wizard step + `aci-sim run` credential enforcement.

Covers:
  1. `run_wizard`/`cmd_init` write a topology `auth:` section (custom
     username/password, and the `ndo_same_account=no` split-account path),
     round-tripping through `Topology.model_validate`/`load_topology`.
  2. Defaults (no answers / non-tty `--defaults`) still produce a valid
     topology with `auth: {username: admin, password: cisco}`.
  3. `resolve_admin_credentials` — the pure credential-resolution function
     `cmd_run` uses — is unit-tested directly for all three precedence cases:
     topology-only, env-override-wins, neither-present.
  4. `build_run_env_argv` injects SIM_USERNAME/SIM_PASSWORD only when both
     admin_username/admin_password are given, and never touches the
     filesystem (pure function, per its own docstring contract).
  5. End-to-end enforcement: a FastAPI APIC app backed by `auth.py`'s real
     `aaaLogin` route 401s on the wrong password and 200s on the right one,
     for a NON-default (custom) SIM_USERNAME/SIM_PASSWORD pair — proving the
     enforcement is real, not just resolved-and-ignored. Since auth.py binds
     SIM_USERNAME/SIM_PASSWORD as module-level constants at import time, this
     test monkeypatches the module's attributes directly (the aaaLogin route
     closure reads them as module globals at request time, not at def time —
     verified by this file's own tests) rather than fighting import-time
     binding with `importlib.reload`.
  6. Backward compat: an existing topology.yaml with NO `auth:` section still
     loads (`topo.auth is None`) and `resolve_admin_credentials`/
     `build_run_env_argv` inject nothing — `rest_aci/auth.py` keeps its own
     env-or-admin/cisco default, unchanged.

Does NOT weaken any pre-existing auth test — this file only adds new
coverage; `tests/test_pr9_ansible.py`'s cert-auth/aaaLogin tests are
untouched.
"""
from __future__ import annotations

import copy
import io
import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from aci_sim.build.orchestrator import build_site
from aci_sim.cli import build_run_env_argv, resolve_admin_credentials, run_wizard
from aci_sim.rest_aci import auth as auth_module
from aci_sim.rest_aci.app import ApicSiteState, make_apic_app
from aci_sim.topology.loader import load_topology
from aci_sim.topology.schema import Auth, Topology

REPO_ROOT = Path(__file__).parent.parent
TOPOLOGY_YAML = REPO_ROOT / "topology.yaml"


# ---------------------------------------------------------------------------
# 1 + 2. Wizard writes an `auth:` section (defaults + custom + split NDO).
# ---------------------------------------------------------------------------


class TestWizardAuthSection:
    def test_defaults_produce_admin_cisco_auth(self) -> None:
        out = io.StringIO()
        topo, _gw = run_wizard(stream_in=io.StringIO(""), stream_out=out, accept_defaults=True)
        assert topo["auth"] == {"username": "admin", "password": "cisco"}
        validated = Topology.model_validate(topo)
        assert validated.auth == Auth(username="admin", password="cisco")

    def test_custom_admin_credentials_via_answers(self) -> None:
        answers = {
            "admin_username": "netops",
            "admin_password": "s3cr3t!",
            "deployment_type": "1",
        }
        out = io.StringIO()
        topo, _gw = run_wizard(stream_in=io.StringIO(""), stream_out=out, accept_defaults=False, answers=answers)
        assert topo["auth"] == {"username": "netops", "password": "s3cr3t!"}

        validated = Topology.model_validate(topo)
        assert validated.auth.username == "netops"
        assert validated.auth.password == "s3cr3t!"
        assert validated.auth.ndo_username is None
        assert validated.auth.ndo_password is None

    def test_custom_admin_credentials_round_trip_through_yaml(self, tmp_path: Path) -> None:
        answers = {"admin_username": "netops", "admin_password": "s3cr3t!", "deployment_type": "1"}
        out = io.StringIO()
        topo, _gw = run_wizard(stream_in=io.StringIO(""), stream_out=out, accept_defaults=False, answers=answers)

        out_file = tmp_path / "auth.topology.yaml"
        out_file.write_text(yaml.dump(topo, sort_keys=False), encoding="utf-8")
        loaded = load_topology(out_file)
        assert loaded.auth.username == "netops"
        assert loaded.auth.password == "s3cr3t!"

    def test_ndo_same_account_no_stores_split_ndo_credentials(self) -> None:
        answers = {
            "deployment_type": "1",
            "admin_username": "apicadmin",
            "admin_password": "apicpass",
            "ndo_same_account": "no",
            "ndo_username": "ndoadmin",
            "ndo_password": "ndopass",
        }
        out = io.StringIO()
        topo, _gw = run_wizard(stream_in=io.StringIO(""), stream_out=out, accept_defaults=False, answers=answers)
        assert topo["auth"] == {
            "username": "apicadmin",
            "password": "apicpass",
            "ndo_username": "ndoadmin",
            "ndo_password": "ndopass",
        }
        validated = Topology.model_validate(topo)
        assert validated.auth.ndo_username == "ndoadmin"
        assert validated.auth.ndo_password == "ndopass"

    def test_ndo_same_account_no_defaults_ndo_fields_to_admin_account(self) -> None:
        """ndo_same_account=no but ndo_username/ndo_password not separately
        answered -> the prompt defaults (admin_username/admin_password) are used."""
        answers = {"deployment_type": "1", "ndo_same_account": "no"}
        out = io.StringIO()
        topo, _gw = run_wizard(stream_in=io.StringIO(""), stream_out=out, accept_defaults=False, answers=answers)
        assert topo["auth"]["ndo_username"] == "admin"
        assert topo["auth"]["ndo_password"] == "cisco"

    def test_scripted_stdin_admin_account_step(self) -> None:
        stdin_script = (
            "svcaccount\n"  # admin username
            "hunter2\n"     # admin password
            "no\n"          # ndo_same_account -> no
            "ndosvc\n"      # ndo username
            "ndopass\n"     # ndo password
            "1\n"           # deployment type -> single fabric
        )
        out = io.StringIO()
        topo, _gw = run_wizard(stream_in=io.StringIO(stdin_script), stream_out=out, accept_defaults=False)
        assert topo["auth"] == {
            "username": "svcaccount",
            "password": "hunter2",
            "ndo_username": "ndosvc",
            "ndo_password": "ndopass",
        }

    def test_summary_shows_username_but_masks_password(self) -> None:
        answers = {"deployment_type": "1", "admin_username": "netops", "admin_password": "supersecret"}
        out = io.StringIO()
        topo, _gw = run_wizard(stream_in=io.StringIO(""), stream_out=out, accept_defaults=False, answers=answers)

        from aci_sim.cli import _print_summary

        summary_out = io.StringIO()
        _print_summary(topo, _gw, summary_out)
        text = summary_out.getvalue()
        assert "netops" in text
        assert "supersecret" not in text
        assert "Admin account" in text


# ---------------------------------------------------------------------------
# 3. resolve_admin_credentials — the pure precedence function cmd_run uses.
# ---------------------------------------------------------------------------


class TestResolveAdminCredentialsPrecedence:
    def test_topology_auth_present_no_env_override(self) -> None:
        auth = Auth(username="netops", password="s3cr3t!")
        username, password, source = resolve_admin_credentials({}, auth)
        assert (username, password, source) == ("netops", "s3cr3t!", "topology.yaml")

    def test_env_override_wins_even_with_topology_auth_present(self) -> None:
        auth = Auth(username="netops", password="s3cr3t!")
        base_env = {"SIM_USERNAME": "envuser", "SIM_PASSWORD": "envpass"}
        username, password, source = resolve_admin_credentials(base_env, auth)
        assert (username, password, source) == (None, None, "env override")

    def test_neither_present_injects_nothing(self) -> None:
        username, password, source = resolve_admin_credentials({}, None)
        assert (username, password, source) == (None, None, "built-in default")

    def test_env_override_wins_even_without_topology_auth(self) -> None:
        base_env = {"SIM_USERNAME": "envuser"}
        username, password, source = resolve_admin_credentials(base_env, None)
        assert (username, password, source) == (None, None, "env override")

    def test_empty_string_sim_username_in_env_does_not_count_as_override(self) -> None:
        """An empty-string SIM_USERNAME (e.g. `SIM_USERNAME= aci-sim run`) is
        falsy — falls through to topology.yaml/default, matching os.environ's
        own treatment of unset-vs-empty as not meaningfully "set"."""
        auth = Auth(username="netops", password="s3cr3t!")
        username, password, source = resolve_admin_credentials({"SIM_USERNAME": ""}, auth)
        assert (username, password, source) == ("netops", "s3cr3t!", "topology.yaml")

    def test_sim_password_only_in_env_is_an_atomic_override(self) -> None:
        """Regression (reviewer Finding 1): a caller who sets ONLY SIM_PASSWORD
        (rotating just the password, keeping the default username) must NOT have
        it silently discarded in favor of the topology password. Setting EITHER
        credential var counts as an env override, so topology.auth is ignored
        entirely — the override is atomic, the caller owns both vars."""
        auth = Auth(username="topo_user", password="topo_pass")
        username, password, source = resolve_admin_credentials({"SIM_PASSWORD": "caller_pw"}, auth)
        assert (username, password, source) == (None, None, "env override")

    def test_sim_password_only_no_topology_is_env_override(self) -> None:
        username, password, source = resolve_admin_credentials({"SIM_PASSWORD": "caller_pw"}, None)
        assert (username, password, source) == (None, None, "env override")

    def test_caller_sim_password_survives_into_exec_env(self) -> None:
        """End of the Finding-1 chain: with the atomic override, the caller's
        SIM_PASSWORD passes through to the exec'd env untouched (never clobbered
        by the topology password); the unset SIM_USERNAME is left for auth.py to
        fill with its own admin default."""
        base = {"SIM_PASSWORD": "caller_pw"}
        user, pwd, _src = resolve_admin_credentials(base, Auth(username="topo_user", password="topo_pass"))
        env, _argv = build_run_env_argv("topology.yaml", base_env=base, admin_username=user, admin_password=pwd)
        assert env["SIM_PASSWORD"] == "caller_pw"  # survived — not clobbered
        assert "SIM_USERNAME" not in env  # left unset -> auth.py fills with admin default


# ---------------------------------------------------------------------------
# 4. build_run_env_argv — env injection + filesystem purity.
# ---------------------------------------------------------------------------


class TestBuildRunEnvArgvCredentialInjection:
    def test_injects_both_vars_when_both_given(self) -> None:
        env, argv = build_run_env_argv(
            "topology.yaml", base_env={"PATH": "/usr/bin"}, admin_username="netops", admin_password="s3cr3t!"
        )
        assert env["SIM_USERNAME"] == "netops"
        assert env["SIM_PASSWORD"] == "s3cr3t!"
        assert env["TOPOLOGY_PATH"] == "topology.yaml"

    def test_injects_nothing_when_neither_given(self) -> None:
        env, argv = build_run_env_argv("topology.yaml", base_env={"PATH": "/usr/bin"})
        assert "SIM_USERNAME" not in env
        assert "SIM_PASSWORD" not in env

    def test_base_env_untouched_by_reference(self) -> None:
        """build_run_env_argv deepcopies base_env — the caller's dict must
        not be mutated (pure-function contract in its docstring)."""
        base_env = {"PATH": "/usr/bin"}
        build_run_env_argv("topology.yaml", base_env=base_env, admin_username="netops", admin_password="s3cr3t!")
        assert "SIM_USERNAME" not in base_env
        assert "TOPOLOGY_PATH" not in base_env

    def test_does_not_touch_filesystem(self, tmp_path: Path) -> None:
        """Passing a nonexistent topology path must not raise / must not stat
        the file — build_run_env_argv is filesystem-pure per its docstring."""
        nonexistent = tmp_path / "does-not-exist.yaml"
        env, argv = build_run_env_argv(str(nonexistent))
        assert env["TOPOLOGY_PATH"] == str(nonexistent)


# ---------------------------------------------------------------------------
# 5. End-to-end: aaaLogin actually enforces a custom password.
# ---------------------------------------------------------------------------


def _new_client(topo) -> TestClient:
    site = topo.sites[0]
    store = build_site(topo, site)
    state = ApicSiteState(name=site.name, site=site, topo=topo, store=store, baseline=copy.deepcopy(store))
    return TestClient(make_apic_app(state))


class TestEndToEndEnforcement:
    @pytest.fixture(autouse=True)
    def _restore_auth_module_globals(self):
        """auth.py binds SIM_USERNAME/SIM_PASSWORD as module-level constants
        at import time; this fixture monkeypatches the module's OWN attributes
        (which aaaLogin's closure reads as module globals at request time, not
        at def time) and restores the originals after each test, so these
        tests never leak a custom credential into any other test in the
        suite."""
        orig_username = auth_module.SIM_USERNAME
        orig_password = auth_module.SIM_PASSWORD
        yield
        auth_module.SIM_USERNAME = orig_username
        auth_module.SIM_PASSWORD = orig_password

    def test_aaalogin_succeeds_with_custom_password(self) -> None:
        auth_module.SIM_USERNAME = "customadmin"
        auth_module.SIM_PASSWORD = "custompass123"
        topo = load_topology(TOPOLOGY_YAML)
        client = _new_client(topo)

        resp = client.post(
            "/api/aaaLogin.json",
            json={"aaaUser": {"attributes": {"name": "customadmin", "pwd": "custompass123"}}},
        )
        assert resp.status_code == 200

    def test_aaalogin_401s_with_wrong_password(self) -> None:
        auth_module.SIM_USERNAME = "customadmin"
        auth_module.SIM_PASSWORD = "custompass123"
        topo = load_topology(TOPOLOGY_YAML)
        client = _new_client(topo)

        resp = client.post(
            "/api/aaaLogin.json",
            json={"aaaUser": {"attributes": {"name": "customadmin", "pwd": "totally-wrong"}}},
        )
        assert resp.status_code == 401

    def test_aaalogin_401s_with_stale_default_password_after_rotation(self) -> None:
        """The previous default (admin/cisco) must stop working once a
        topology-driven credential rotation has taken effect — proves this
        isn't just "any password works", it's a real replacement."""
        auth_module.SIM_USERNAME = "customadmin"
        auth_module.SIM_PASSWORD = "custompass123"
        topo = load_topology(TOPOLOGY_YAML)
        client = _new_client(topo)

        resp = client.post(
            "/api/aaaLogin.json",
            json={"aaaUser": {"attributes": {"name": "admin", "pwd": "cisco"}}},
        )
        assert resp.status_code == 401

    def test_ndo_remains_lenient_regardless_of_apic_credential_rotation(self) -> None:
        """NDO must keep accepting any credential even while the APIC plane
        enforces a rotated (non-default) password — proves this feature does
        NOT change NDO's deliberate leniency."""
        from aci_sim.ndo.app import make_ndo_app
        from aci_sim.ndo.model import build_ndo_model

        auth_module.SIM_USERNAME = "customadmin"
        auth_module.SIM_PASSWORD = "custompass123"

        topo = load_topology(TOPOLOGY_YAML)
        ndo_state = build_ndo_model(topo)
        ndo_client = TestClient(make_ndo_app(ndo_state))
        resp = ndo_client.post(
            "/api/v1/auth/login",
            json={"userName": "anyone", "userPasswd": "anything-at-all"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 6. Backward compat: no `auth:` section -> no injection, no regression.
# ---------------------------------------------------------------------------


class TestBackwardCompatNoAuthSection:
    def test_repo_topology_has_no_auth_section(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        assert topo.auth is None

    def test_resolve_admin_credentials_injects_nothing_for_repo_topology(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        username, password, source = resolve_admin_credentials({}, topo.auth)
        assert (username, password, source) == (None, None, "built-in default")

    def test_build_run_env_argv_injects_nothing_for_repo_topology(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        username, password, _source = resolve_admin_credentials(dict(os.environ), topo.auth)
        env, _argv = build_run_env_argv(
            str(TOPOLOGY_YAML), base_env={"PATH": "/usr/bin"}, admin_username=username, admin_password=password
        )
        assert "SIM_USERNAME" not in env
        assert "SIM_PASSWORD" not in env

    def test_topology_dict_without_auth_key_still_validates(self) -> None:
        """A hand-authored topology dict that never mentions `auth` at all
        (pre-dating this feature) must still validate — Topology.auth is
        Optional[Auth] = None."""
        from aci_sim.cli import generate_topology

        topo_dict = generate_topology(sites=1, leaves_per_site=1, spines_per_site=1, border_pairs=1)
        assert "auth" not in topo_dict
        validated = Topology.model_validate(topo_dict)
        assert validated.auth is None
