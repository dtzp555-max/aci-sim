"""File-backed state persistence for the sim's MITStore and NdoState.

Covers ALL planes — per-site APIC MITStores and the NDO model — so a whole
fabric can be saved to disk and restored across a sim restart (sandbox/port
mode has no other durability: everything else lives in memory only).

Design notes
------------
- ``MO`` instances stored in a :class:`~aci_sim.mit.store.MITStore`
  are always childless (the store keeps the parent/child hierarchy only in
  its own ``_children`` index — see ``MITStore.add``'s docstring). So a
  faithful store round-trip only needs each MO's ``class_name`` + ``attrs``;
  replaying them through ``MITStore.add`` rebuilds the ancestor index and
  therefore all subtree queries.
- ``NdoState`` is a plain ``@dataclass`` of JSON-friendly fields (lists/dicts
  of str/bool/int). ``apply_ndo`` mutates the SAME state object in place via
  ``setattr`` — ``make_ndo_app`` closes over the ``state`` object identity,
  so replacing it with a new instance would leave the running app's routes
  pointed at stale data.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.ndo.model import NdoState

#: NdoState fields that are persisted (mutable, JSON-friendly). Excludes
#: nothing from the dataclass — every field NdoState carries is saved.
FIELDS: list[str] = [
    "tenants",
    "schemas",
    "schema_details",
    "template_summaries",
    "tenant_policy_templates",
    "policy_states",
    "audit_records",
    "local_users",
    "remote_users",
    "extra_schemas",
    "sites",
    "fabric_connectivity",
]


def state_dir() -> Path:
    """Return the base directory for persisted state, creating it if needed.

    Defaults to ``~/.aci-sim/state``; override with ``SIM_STATE_DIR``
    (tests set this to an isolated ``tmp_path`` so nothing touches the
    real home directory).
    """
    base = Path(os.environ.get("SIM_STATE_DIR", os.path.expanduser("~/.aci-sim/state")))
    base.mkdir(parents=True, exist_ok=True)
    return base


# ---------------------------------------------------------------------------
# Snapshot envelope: which sim built this, and against which topology
#
# A snapshot is a full MIT dump, so restoring one taken on an older sim
# reinstates that sim's BOOT-TIME baseline MOs and silently drops whatever the
# current builders would have produced. That is not theoretical: a v0.21 save
# restored onto v0.26 puts back a fabric with no `common`/`infra` tenants and
# overwrites mirror-corrected attributes. The same applies across topology
# edits — the node set the snapshot describes may no longer exist.
#
# So every save carries a `_meta` header and every load compares it. Snapshots
# written before this existed are bare list/dict documents; they still load,
# but report `legacy` so the caller can say the compatibility is unverifiable
# rather than implying it was checked.
# ---------------------------------------------------------------------------

#: Envelope format version — bump only if the wrapper's own shape changes.
ENVELOPE_VERSION = 1


def _read_version() -> str:
    """Read the version — source tree first, then installed metadata.

    An editable install freezes its dist metadata at install time, so
    ``importlib.metadata`` keeps reporting whatever the version was on the day
    ``pip install -e .`` ran (observed: 0.18.1 against a 0.26.1 pyproject).
    Stamping snapshots from that would record a version this code has not been
    for months, which is worse than not stamping at all — so when a pyproject
    sits above the package, it wins. A wheel install has no pyproject and falls
    through to the metadata, which is authoritative there.
    """
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("version") and "=" in stripped:
                return stripped.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    try:
        return _pkg_version("aci-sim")
    except PackageNotFoundError:
        return "unknown"


# Resolved once, at import — i.e. when this process loaded the code it is
# running. Re-reading per call would mean that between a `git pull` and the
# restart that picks it up, the sim stamps snapshots with a version it is not
# yet: 0.28.1 builders producing MOs labelled 0.29.0, which then restore
# cleanly onto a real 0.29.0 because the stamp agrees. The stamp has to
# describe the running code, and the file on disk stops describing it the
# moment someone pulls.
_SIM_VERSION = _read_version()


def sim_version() -> str:
    """This sim's version, as of the moment this process started."""
    return _SIM_VERSION


def topology_fingerprint(path: str | os.PathLike[str] | None = None) -> str:
    """Short content hash of topology.yaml — identifies the fabric shape.

    Returns ``""`` when the file is unreadable, which makes the load-time
    comparison skip the topology check rather than fail a restore for a
    reason the operator cannot act on.
    """
    p = Path(path or os.environ.get("TOPOLOGY_PATH", "topology.yaml"))
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def wrap(payload: Any) -> dict[str, Any]:
    """Wrap *payload* in a stamped envelope."""
    return {
        "_meta": {
            "envelope": ENVELOPE_VERSION,
            "sim_version": sim_version(),
            "topology": topology_fingerprint(),
            "saved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        "data": payload,
    }


def unwrap(doc: Any) -> tuple[Any, dict[str, Any]]:
    """Split a loaded document into ``(payload, meta)``.

    A bare list/dict is a pre-envelope snapshot: returned as-is with
    ``{"legacy": True}`` so callers can flag it.
    """
    if isinstance(doc, dict) and "_meta" in doc and "data" in doc:
        return doc["data"], dict(doc["_meta"])
    return doc, {"legacy": True}


def compatibility(meta: dict[str, Any]) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for restoring a snapshot carrying *meta*.

    An UNVERIFIABLE restore is exactly as dangerous as a known-mismatched one:
    the hazard is reinstating another build's baseline, and not knowing which
    build does not make that safer. So a pre-envelope snapshot is refused too —
    the ones already on disk when stamping landed are precisely the
    several-releases-old dumps this guard exists to stop. ``force=1`` still
    restores them.

    A snapshot whose topology could not be hashed at save time is the one
    genuine exception: that check is skipped rather than failed, since the
    version check still applies and the operator cannot act on a missing hash.
    """
    if meta.get("legacy"):
        return False, (
            "snapshot predates version stamping, so the sim version and topology "
            "it was taken against cannot be verified. Snapshots from before this "
            "check are typically several releases old. Re-push instead, or force it."
        )

    saved_sim = meta.get("sim_version", "unknown")
    now_sim = sim_version()
    if saved_sim != now_sim and "unknown" not in (saved_sim, now_sim):
        return False, (
            f"snapshot was saved on aci-sim {saved_sim}, this is {now_sim}. "
            "Restoring would reinstate the older build's baseline MOs and drop "
            "what the current builders produce. Re-push instead, or force it."
        )

    saved_topo = meta.get("topology", "")
    now_topo = topology_fingerprint()
    if saved_topo and now_topo and saved_topo != now_topo:
        return False, (
            f"snapshot was saved against topology {saved_topo}, this is {now_topo}. "
            "The node set it describes may not exist here. Re-push instead, or force it."
        )

    return True, "ok"


def save_json(path: Path, obj: Any) -> None:
    """Write *obj* to *path* as indented JSON."""
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def load_json(path: Path) -> Any:
    """Read and return the JSON document at *path*."""
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# MITStore (APIC plane) serialization
# ---------------------------------------------------------------------------


def serialize_store(store: MITStore) -> list[dict[str, Any]]:
    """Flatten *store* to a JSON-friendly list of ``{class, attrs}`` dicts.

    Stored MOs are childless (hierarchy lives only in the store's parent
    index — see ``MITStore.add``), so this list alone is sufficient to
    reconstruct the store via :func:`deserialize_store`.
    """
    return [{"class": mo.class_name, "attrs": dict(mo.attrs)} for mo in store.all()]


def deserialize_store(data: list[dict[str, Any]]) -> MITStore:
    """Rebuild a :class:`MITStore` from :func:`serialize_store` output.

    Replays each entry through ``MITStore.add``, which rebuilds the
    ancestor/``_children`` index as it goes — so subtree queries against the
    restored store behave exactly as they did before serialization.
    """
    s = MITStore()
    for d in data:
        s.add(MO(d["class"], **d["attrs"]))
    return s


# ---------------------------------------------------------------------------
# NdoState (NDO plane) serialization
# ---------------------------------------------------------------------------


def serialize_ndo(state: NdoState) -> dict[str, Any]:
    """Return a deep-copied, JSON-friendly dict of *state*'s persisted fields."""
    return {f: copy.deepcopy(getattr(state, f)) for f in FIELDS}


def apply_ndo(state: NdoState, data: dict[str, Any]) -> None:
    """Apply *data* (from :func:`serialize_ndo`) onto *state* IN PLACE.

    Mutates the same object via ``setattr`` rather than returning a new
    ``NdoState`` — ``make_ndo_app`` closes over this exact object, so
    replacing it would leave the running app's routes reading stale state.
    """
    for f, v in data.items():
        setattr(state, f, copy.deepcopy(v))
