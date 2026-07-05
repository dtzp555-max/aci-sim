"""Bracket-aware DN utilities.

ACI Distinguished Names are slash-separated, but path components inside
square brackets must never be split on the slash.  Examples:
  uni/tn-T/BD-b/subnet-[10.0.0.1/24]
  uni/tn-T/pathep-[eth1/9]
  uni/phys-X/rsdomAtt-[uni/phys-X]
  uni/infra/.../rslldpIfAtt-[pathep-[eth1/9]]   (nested brackets)
"""

from __future__ import annotations
import re


def dn_split(dn: str) -> list[str]:
    """Split a DN into RN components, never splitting inside [...].

    Returns a list of RN strings; returns [] for an empty DN.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in dn:
        if ch == "[":
            depth += 1
            current.append(ch)
        elif ch == "]":
            depth -= 1
            current.append(ch)
        elif ch == "/" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def parent_dn(dn: str) -> str:
    """Return the parent DN (everything except the last RN).

    Returns "" for top-level DNs (no parent).
    """
    parts = dn_split(dn)
    if len(parts) <= 1:
        return ""
    return "/".join(parts[:-1])


def rn_of(dn: str) -> str:
    """Return the last RN of a DN."""
    parts = dn_split(dn)
    return parts[-1] if parts else ""


def name_from_dn(dn: str) -> str:
    """Extract the name from the last RN (part after the first '-').

    For bracket RNs like `subnet-[10.0.0.1/24]`, returns `10.0.0.1/24`.
    For plain RNs like `tn-T`, returns `T`.
    For RNs with no dash (e.g. `uni`), returns the whole RN.
    """
    rn = rn_of(dn)
    # Bracket form: foo-[...] → extract content of innermost [...] span
    m = re.match(r"[^-]+-\[(.+)\]$", rn)
    if m:
        return m.group(1)
    # Plain dash: foo-bar → bar
    idx = rn.find("-")
    if idx >= 0:
        return rn[idx + 1:]
    return rn


def pod_from_dn(dn: str) -> str:
    """Extract the pod number from a DN.

    Searches for `/pod-N` in the DN; returns "1" as the default when absent.
    """
    m = re.search(r"/pod-(\d+)(?:/|$)", dn)
    if m:
        return m.group(1)
    # Also handle DN that starts with "pod-N"
    m2 = re.match(r"pod-(\d+)(?:/|$)", dn)
    if m2:
        return m2.group(1)
    return "1"


def dn_class_hint(rn: str) -> str | None:
    """Guess the ACI class from an RN prefix (best-effort).

    Used by the REST layer to build default class names from path components.
    Returns None if the prefix is not recognised.
    """
    _PREFIX_MAP: dict[str, str] = {
        "uni": "polUni",
        "tn-": "fvTenant",
        "ctx-": "fvCtx",
        "BD-": "fvBD",
        "subnet-": "fvSubnet",
        "ap-": "fvAp",
        "epg-": "fvAEPg",
        "cep-": "fvCEp",
        "node-": "fabricNode",
        "sys": "topSystem",
        "health": "healthInst",
        "fault-": "faultInst",
        "lnk-": "fabricLink",
    }
    for prefix, cls in _PREFIX_MAP.items():
        if rn == prefix or (prefix.endswith("-") and rn.startswith(prefix)):
            return cls
    return None
