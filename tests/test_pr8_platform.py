"""Tests for PR-8 cross-platform + deployment engineering (findings #23-#28).

Covers:
  - SIM_BIND default/override parsing (runtime/config.py, #23)
  - port-selection function (A/B/fallback) (runtime/supervisor.py, #25)
  - CSW peer/border-leaf mismatch validation raises (topology/schema.py, #28)
  - `bash -n` syntax check for both sandbox scripts (#24)
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from aci_sim.topology.schema import Topology

REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# SIM_BIND (#23)
# ---------------------------------------------------------------------------


class TestSimBind:
    def test_default_is_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SIM_BIND", raising=False)
        from aci_sim.runtime import config

        importlib.reload(config)
        assert config.SIM_BIND == "127.0.0.1"

    def test_override_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIM_BIND", "0.0.0.0")
        from aci_sim.runtime import config

        importlib.reload(config)
        try:
            assert config.SIM_BIND == "0.0.0.0"
        finally:
            monkeypatch.delenv("SIM_BIND", raising=False)
            importlib.reload(config)  # restore default for any later importers


# ---------------------------------------------------------------------------
# Port selection (#25)
# ---------------------------------------------------------------------------


class TestApicPortForSite:
    def test_site_zero_uses_apic_a_port(self) -> None:
        from aci_sim.runtime.config import APIC_A_PORT
        from aci_sim.runtime.supervisor import apic_port_for_site

        assert apic_port_for_site(0) == APIC_A_PORT

    def test_site_one_uses_apic_b_port(self) -> None:
        """APIC_B_PORT (config.py) must actually be honored for the second
        site, not silently shadowed by an APIC_A_PORT+i formula."""
        from aci_sim.runtime.config import APIC_B_PORT
        from aci_sim.runtime.supervisor import apic_port_for_site

        assert apic_port_for_site(1) == APIC_B_PORT

    def test_site_two_falls_back_to_apic_a_port_plus_index(self) -> None:
        from aci_sim.runtime.config import APIC_A_PORT
        from aci_sim.runtime.supervisor import apic_port_for_site

        assert apic_port_for_site(2) == APIC_A_PORT + 2

    def test_apic_b_port_env_override_takes_effect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APIC_B_PORT", "9999")
        from aci_sim.runtime import config

        importlib.reload(config)
        from aci_sim.runtime import supervisor

        importlib.reload(supervisor)
        try:
            assert supervisor.apic_port_for_site(1) == 9999
        finally:
            monkeypatch.delenv("APIC_B_PORT", raising=False)
            importlib.reload(config)
            importlib.reload(supervisor)


# ---------------------------------------------------------------------------
# CSW mismatch validation (#28)
# ---------------------------------------------------------------------------


_BASE_TOPOLOGY = {
    "fabric": {"name": "test-fabric"},
    "sites": [
        {
            "name": "SiteA",
            "id": "1",
            "apic_host": "127.0.0.1:8443",
            "asn": 65001,
            "pod": 1,
            "spines": 1,
            "leaves": 1,
            "cabling": "auto",
            "border_leaves": [
                {
                    "id": 103,
                    "name": "SiteA-BL103",
                    "vpc_domain": "vpcdom-A",
                    "csw": {
                        "name": "CSW01",
                        "asn": 65100,
                        "peer_ip": "10.100.0.1",
                        "local_ip": "10.100.0.2",
                        "vlan": 100,
                    },
                }
            ],
        }
    ],
    "isn": {"enabled": False},
    "tenants": [
        {
            "name": "TN1",
            "stretch": False,
            "sites": ["SiteA"],
            "vrfs": [{"name": "vrf1"}],
            "bds": [],
            "aps": [],
            "contracts": [],
            "l3outs": [
                {
                    "name": "l3o-test",
                    "vrf": "vrf1",
                    "site": "SiteA",
                    "border_leaves": [103],
                    "csw_peer": {
                        "name": "CSW01",
                        "asn": 65100,
                        "peer_ip": "10.100.0.1",
                    },
                }
            ],
        }
    ],
}


def _deep_copy(d):
    return yaml.safe_load(yaml.safe_dump(d))


class TestCswMismatchValidation:
    def test_matching_csw_peer_loads_fine(self) -> None:
        Topology.model_validate(_deep_copy(_BASE_TOPOLOGY))

    def test_mismatched_asn_raises(self) -> None:
        data = _deep_copy(_BASE_TOPOLOGY)
        data["tenants"][0]["l3outs"][0]["csw_peer"]["asn"] = 65999
        with pytest.raises(ValidationError, match="does not match border-leaf"):
            Topology.model_validate(data)

    def test_mismatched_peer_ip_raises(self) -> None:
        data = _deep_copy(_BASE_TOPOLOGY)
        data["tenants"][0]["l3outs"][0]["csw_peer"]["peer_ip"] = "10.100.0.99"
        with pytest.raises(ValidationError, match="does not match border-leaf"):
            Topology.model_validate(data)

    def test_mismatched_name_raises(self) -> None:
        data = _deep_copy(_BASE_TOPOLOGY)
        data["tenants"][0]["l3outs"][0]["csw_peer"]["name"] = "CSW-WRONG"
        with pytest.raises(ValidationError, match="does not match border-leaf"):
            Topology.model_validate(data)


# ---------------------------------------------------------------------------
# Sandbox scripts syntax (#24)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    ["scripts/sandbox-up.sh", "scripts/sandbox-down.sh"],
)
def test_sandbox_script_bash_syntax_ok(script: str) -> None:
    result = subprocess.run(
        ["bash", "-n", str(REPO_ROOT / script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "script",
    ["scripts/sandbox-up.sh", "scripts/sandbox-down.sh"],
)
def test_sandbox_script_has_linux_branch(script: str) -> None:
    text = (REPO_ROOT / script).read_text()
    assert "Linux" in text
    assert "ip addr" in text
