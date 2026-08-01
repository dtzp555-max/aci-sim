"""Topology YAML loader.

Usage::

    from aci_sim.topology.loader import load_topology
    topo = load_topology("topology.yaml")
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .schema import Topology


def load_topology(path: str | Path) -> Topology:
    """Read *path*, parse YAML, validate, and return a :class:`~schema.Topology`.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    ValueError
        If the YAML is syntactically invalid.
    pydantic.ValidationError
        If the topology fails schema or cross-reference validation; the error
        message lists every violation found.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Topology file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse YAML from {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(
            f"Topology YAML must be a mapping at the top level, got {type(raw).__name__}"
        )

    # Let ValidationError propagate naturally — callers inspect it directly.
    return Topology.model_validate(raw)
