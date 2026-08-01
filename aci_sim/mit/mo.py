"""MO (Managed Object) — building block of the ACI Managed Information Tree."""

from __future__ import annotations

from typing import Any


class MO:
    """A single node in the ACI MIT.

    Attributes
    ----------
    class_name : str          e.g. ``"fvTenant"``
    attrs      : dict[str, str]   must include ``"dn"``
    children   : list[MO]     direct children in this MO's subtree
    """

    def __init__(self, class_name: str, **attrs: str) -> None:
        self.class_name = class_name
        self.attrs: dict[str, str] = dict(attrs)
        self.children: list[MO] = []

    @property
    def dn(self) -> str:
        return self.attrs.get("dn", "")

    def add_child(self, mo: MO) -> None:
        """Append *mo* to this MO's children list."""
        self.children.append(mo)

    def flat(self) -> list[MO]:
        """Return self + all descendants in depth-first order."""
        result: list[MO] = [self]
        for child in self.children:
            result.extend(child.flat())
        return result

    def to_imdata(self) -> dict[str, Any]:
        """Produce the APIC wire shape for this MO.

        ``{"class_name": {"attributes": {...}, "children": [...]}}``

        The ``children`` key is omitted when the MO has no children.
        """
        body: dict[str, Any] = {"attributes": dict(self.attrs)}
        if self.children:
            body["children"] = [c.to_imdata() for c in self.children]
        return {self.class_name: body}

    def __repr__(self) -> str:
        return f"MO({self.class_name!r}, dn={self.dn!r})"
