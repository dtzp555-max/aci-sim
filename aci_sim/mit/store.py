"""MITStore — DN-keyed in-memory object store for ACI MOs."""

from __future__ import annotations

import copy

from aci_sim.mit.dn import parent_dn
from aci_sim.mit.mo import MO


class MITStore:
    """Maintains a ``dn -> MO`` map and a ``parent_dn -> [child dn]`` index.

    Iteration order is deterministic (insertion order of DNs).
    The store is deep-copyable for baseline/snapshot workflows.
    """

    def __init__(self) -> None:
        self._mos: dict[str, MO] = {}               # dn → MO
        self._children: dict[str, list[str]] = {}   # parent_dn → [child dn]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _register(self, dn: str) -> None:
        """Register *dn* AND ensure its full ancestor chain is linked (idempotent).

        Walking the whole chain (not just the immediate parent) lets :meth:`subtree`
        traverse through structural intermediates that were never added as MOs — e.g.
        a deep ``epmMacEp`` at ``…/sys/ctx-[…]/bd-[…]/db-ep/mac-…`` whose ctx/bd/db-ep
        containers have no MO of their own. Without this, ``subtree(node/sys)`` could
        not reach it.
        """
        self._children.setdefault(dn, [])
        cur = dn
        while True:
            pdn = parent_dn(cur)
            if not pdn or pdn == cur:
                break
            siblings = self._children.setdefault(pdn, [])
            if cur not in siblings:
                siblings.append(cur)
            cur = pdn

    def _remove_subtree(self, dn: str) -> None:
        """Remove *dn* and all its descendants from both maps."""
        for child_dn in list(self._children.get(dn, [])):
            self._remove_subtree(child_dn)
        self._mos.pop(dn, None)
        self._children.pop(dn, None)
        pdn = parent_dn(dn)
        siblings = self._children.get(pdn)
        if siblings is not None:
            try:
                siblings.remove(dn)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, mo: MO) -> None:
        """Insert *mo* (and recursively its ``.children``) without merging.

        Stores a *childless* copy — hierarchy lives only in the parent index
        (same invariant as :meth:`upsert`). Otherwise a stored tree MO would
        carry a second copy of its subtree via ``MO.to_imdata()``, leaking
        ``children`` onto non-subtree queries and duplicating descendants on
        subtree queries.
        """
        dn = mo.dn
        self._mos[dn] = MO(mo.class_name, **mo.attrs)
        self._register(dn)
        for child in mo.children:
            self.add(child)

    def upsert(self, mo: MO) -> None:
        """Merge *mo*'s attrs into an existing entry or insert it.

        Recurses into ``mo.children``.
        If ``mo.attrs["status"] == "deleted"``, removes the DN and its entire
        subtree instead of merging.
        """
        dn = mo.dn
        if mo.attrs.get("status") == "deleted":
            self._remove_subtree(dn)
            return

        existing = self._mos.get(dn)
        if existing is None:
            new_mo = MO(mo.class_name, **mo.attrs)
            self._mos[dn] = new_mo
            self._register(dn)
        else:
            existing.attrs.update(mo.attrs)

        for child in mo.children:
            self.upsert(child)

    def get(self, dn: str) -> MO | None:
        """Return the MO at *dn*, or ``None`` if not present."""
        return self._mos.get(dn)

    def delete(self, dn: str) -> None:
        """Remove *dn* and its entire subtree from the store.

        Idempotent: deleting a *dn* that is not present (or was already
        removed) is a silent no-op, matching ``upsert``'s ``status="deleted"``
        branch (also unconditional — no existence check) and real APIC's
        DELETE /api/mo/{dn}.json used by cisco.aci's ``state=absent`` path
        (PR-9). Public wrapper around the same ``_remove_subtree`` helper
        ``upsert`` already uses, so callers outside this module (the DELETE
        HTTP route) don't need to reach into a private method.
        """
        self._remove_subtree(dn)

    def children(self, dn: str, classes: set[str] | None = None) -> list[MO]:
        """Return direct children of *dn*, optionally filtered by class names."""
        result: list[MO] = []
        for cdn in self._children.get(dn, []):
            mo = self._mos.get(cdn)
            if mo is None:
                continue
            if classes is None or mo.class_name in classes:
                result.append(mo)
        return result

    def child_dns(self, dn: str) -> list[str]:
        """Return the raw child-DN list of *dn* in insertion order.

        Includes structural intermediates that have no MO of their own (see
        :meth:`_register`). Used by the query engine to reconstruct the nested
        subtree for ``rsp-subtree=full`` without flattening the hierarchy.
        """
        return list(self._children.get(dn, []))

    def subtree(self, dn: str, classes: set[str] | None = None) -> list[MO]:
        """Return all descendants of *dn* (not *dn* itself), depth-first.

        If *classes* is given, only include MOs whose class_name is in the set;
        but always recurse into ALL children regardless (mirrors APIC behaviour
        where a class filter on rsp-subtree-class traverses the full tree).
        """
        acc: list[MO] = []
        self._collect_subtree(dn, classes, acc)
        return acc

    def _collect_subtree(self, dn: str, classes: set[str] | None, acc: list[MO]) -> None:
        for cdn in self._children.get(dn, []):
            mo = self._mos.get(cdn)
            if mo is not None and (classes is None or mo.class_name in classes):
                acc.append(mo)
            # Recurse even through MO-less structural intermediates so deeply
            # nested descendants (e.g. epm* under uninstantiated containers) are reached.
            self._collect_subtree(cdn, classes, acc)

    def by_class(self, cls: str) -> list[MO]:
        """Return all MOs whose ``class_name == cls``, in insertion order."""
        return [mo for mo in self._mos.values() if mo.class_name == cls]

    def all(self) -> list[MO]:
        """Return all MOs in insertion order."""
        return list(self._mos.values())

    # ------------------------------------------------------------------
    # Deep-copy support (for baseline / snapshot)
    # ------------------------------------------------------------------

    def __deepcopy__(self, memo: dict) -> MITStore:
        new = MITStore.__new__(MITStore)
        new._mos = copy.deepcopy(self._mos, memo)
        new._children = copy.deepcopy(self._children, memo)
        return new
