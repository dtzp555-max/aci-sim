"""Snapshot envelope: stamp the sim version + topology, check it on restore.

A snapshot is a full MIT dump, so restoring one taken on a different sim
reinstates that build's boot-time baseline and drops whatever the current
builders produce. Observed for real: a v0.21 save restored onto v0.26 puts back
a fabric with no `common`/`infra` tenants. These tests pin the guard and, just
as importantly, pin that pre-envelope snapshots still load.
"""

import json
from pathlib import Path

import pytest

from aci_sim.control.persist import (
    _read_version,
    compatibility,
    sim_version,
    topology_fingerprint,
    unwrap,
    wrap,
)

# ── envelope round trip ─────────────────────────────────────────────────────

def test_wrap_carries_version_and_topology():
    meta = wrap([1, 2, 3])["_meta"]
    assert meta["sim_version"] == sim_version()
    assert meta["topology"] == topology_fingerprint()
    assert meta["envelope"] == 1
    assert meta["saved_at"]


def test_unwrap_returns_payload_unchanged():
    payload = [{"class": "fvTenant", "attrs": {"dn": "uni/tn-x"}}]
    got, meta = unwrap(wrap(payload))
    assert got == payload
    assert not meta.get("legacy")


def test_wrapped_document_is_json_serializable():
    json.dumps(wrap({"tenants": [{"name": "t"}]}))


# ── pre-envelope snapshots: readable, but not restorable unforced ───────────

def test_bare_list_is_treated_as_legacy():
    payload = [{"class": "fvTenant", "attrs": {"dn": "uni/tn-x"}}]
    got, meta = unwrap(payload)
    assert got == payload
    assert meta == {"legacy": True}


def test_bare_dict_is_treated_as_legacy():
    payload = {"tenants": [], "schemas": []}
    got, meta = unwrap(payload)
    assert got == payload
    assert meta == {"legacy": True}


def test_legacy_snapshot_is_refused_because_it_cannot_be_verified():
    # the snapshots already on disk when stamping landed are exactly the
    # old-version dumps this guard exists to stop — "unknown" is not "safe"
    ok, reason = compatibility({"legacy": True})
    assert ok is False
    assert "cannot be verified" in reason


# ── the guard itself ────────────────────────────────────────────────────────

def test_same_version_and_topology_is_ok():
    ok, reason = compatibility(
        {"sim_version": sim_version(), "topology": topology_fingerprint()}
    )
    assert ok is True
    assert reason == "ok"


def test_older_sim_version_is_refused():
    ok, reason = compatibility(
        {"sim_version": "0.21.0", "topology": topology_fingerprint()}
    )
    assert ok is False
    assert "0.21.0" in reason and sim_version() in reason


def test_different_topology_is_refused():
    ok, reason = compatibility(
        {"sim_version": sim_version(), "topology": "0000deadbeef"}
    )
    assert ok is False
    assert "topology" in reason


def test_unknown_version_does_not_refuse():
    # outside an installed tree the version is unknowable; refusing every
    # restore there would be worse than not checking
    ok, _ = compatibility({"sim_version": "unknown", "topology": topology_fingerprint()})
    assert ok is True


def test_missing_topology_skips_only_that_check():
    # a snapshot saved where topology.yaml was unreadable still checks version
    ok, _ = compatibility({"sim_version": sim_version(), "topology": ""})
    assert ok is True
    ok, reason = compatibility({"sim_version": "0.1.0", "topology": ""})
    assert ok is False
    assert "0.1.0" in reason


# ── version source ──────────────────────────────────────────────────────────

def test_version_comes_from_the_source_tree_not_stale_metadata():
    """An editable install freezes dist metadata; pyproject is the truth."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject.is_file():
        pytest.skip("not running from a source tree")
    declared = ""
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("version") and "=" in s:
            declared = s.split("=", 1)[1].strip().strip("\"'")
            break
    assert declared, "pyproject has no version"
    # _read_version(), not sim_version(): the latter is pinned at import,
    # so it would not notice a pyproject edit — which is the point of it.
    assert _read_version() == declared
    assert sim_version() == _read_version()
