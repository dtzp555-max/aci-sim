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
import json
import os
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
