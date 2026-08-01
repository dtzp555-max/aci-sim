"""Control-plane admin router mounted under /_sim on each APIC app."""

from __future__ import annotations

import copy

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from aci_sim.control.persist import (
    compatibility,
    deserialize_store,
    load_json,
    save_json,
    serialize_store,
    state_dir,
    unwrap,
    wrap,
)
from aci_sim.mit.mo import MO


def _apic_error(text: str, code: str = "103", status_code: int = 400) -> JSONResponse:
    """Return an APIC error envelope as a JSONResponse.

    Duplicated (rather than imported) from rest_aci.app to avoid a circular
    import: app.py imports make_admin_router from this module.
    """
    body = {
        "imdata": [{"error": {"attributes": {"text": text, "code": code}}}],
        "totalCount": "1",
    }
    return JSONResponse(status_code=status_code, content=body)


def make_admin_router(state) -> APIRouter:
    """Build the /_sim admin router.

    state is an ApicSiteState dataclass; we mutate state.store directly.
    """
    router = APIRouter(prefix="/_sim")
    _snapshots: dict[str, object] = {}  # name → MITStore deepcopy

    @router.post("/reset")
    async def reset():
        state.store = copy.deepcopy(state.baseline)
        return {"status": "ok", "action": "reset"}

    @router.post("/snapshot/{name}")
    async def snapshot(name: str):
        _snapshots[name] = copy.deepcopy(state.store)
        return {"status": "ok", "snapshot": name}

    @router.post("/restore/{name}")
    async def restore(name: str):
        if name not in _snapshots:
            return JSONResponse(
                status_code=404,
                content={"status": "error", "detail": f"snapshot '{name}' not found"},
            )
        state.store = copy.deepcopy(_snapshots[name])
        return {"status": "ok", "restored": name}

    def _find_site(topo):
        """Look up the site matching state.site in a freshly-loaded topology.

        Matches by the stable Site.id first (the schema's documented stable
        key), falling back to Site.name for topologies that only vary node
        content but keep the same id. Returns None if the site no longer
        exists in the new topology at all.
        """
        for candidate in topo.sites:
            if candidate.id == state.site.id:
                return candidate
        for candidate in topo.sites:
            if candidate.name == state.site.name:
                return candidate
        return None

    @router.post("/reload")
    async def reload(request: Request):
        from aci_sim.build.orchestrator import build_site
        from aci_sim.runtime.config import TOPOLOGY_PATH
        from aci_sim.topology.loader import load_topology

        topo = load_topology(TOPOLOGY_PATH)
        fresh_site = _find_site(topo)
        if fresh_site is None:
            return _apic_error(
                text=(
                    f"Site '{state.site.name}' (id={state.site.id!r}) no longer "
                    "exists in the reloaded topology"
                ),
                code="103",
                status_code=400,
            )

        # Use the FRESH site object from the just-loaded topology, not the
        # stale pre-reload state.site — otherwise edits to this site's nodes/
        # leaves in topology.yaml are silently ignored (finding #11).
        state.topo = topo
        state.site = fresh_site
        new_store = build_site(topo, fresh_site)
        state.store = new_store
        state.baseline = copy.deepcopy(new_store)
        return {"status": "ok", "action": "reload"}

    @router.post("/save/{name}")
    async def save(name: str):
        """Persist this site's MITStore to disk under *name*.

        File is keyed by both *name* and ``state.site.id`` so a single
        SIM_STATE_DIR can hold saves for multiple sites without collision
        (mirrors the wrapper script's per-plane file naming).
        """
        path = state_dir() / f"{name}.{state.site.id}.apic.json"
        data = serialize_store(state.store)
        save_json(path, wrap(data))
        return {"status": "ok", "file": str(path), "count": len(data)}

    @router.post("/load/{name}")
    async def load(name: str, force: bool = False):
        """Restore this site's MITStore from a prior :func:`save`.

        Refuses a snapshot stamped with a different sim version or topology
        unless ``force=1`` — see ``persist.compatibility``.
        """
        path = state_dir() / f"{name}.{state.site.id}.apic.json"
        if not path.exists():
            return _apic_error(
                f"state '{name}' not found for site {state.site.id}",
                status_code=404,
            )
        data, meta = unwrap(load_json(path))
        ok, reason = compatibility(meta)
        if not ok and not force:
            # A full-MIT restore across sim versions silently reinstates the
            # older build's baseline, so refuse by default and say why.
            return _apic_error(reason, status_code=409)
        state.store = deserialize_store(data)
        return {"status": "ok", "count": len(data), "compatibility": reason}

    @router.post("/add-leaf")
    async def add_leaf(request: Request):
        from aci_sim.rest_aci.writes import materialize_node_registration

        body = await request.json()
        node_id = body.get("id")
        if node_id is None:
            existing = [int(mo.attrs.get("id", 0)) for mo in state.store.by_class("fabricNode")]
            node_id = max(existing, default=100) + 1
        name = body.get("name", f"leaf-{node_id}")
        # Shares the fabricNodeIdentP write-reaction's enrichment (finding #19):
        # fabricNode + topSystem + healthInst + fabricLink cabling to the
        # spines + l1PhysIf inventory, not just a bare fabricNode.
        materialize_node_registration(
            state.store,
            topo=state.topo,
            site=state.site,
            node_id=int(node_id),
            name=name,
            role=body.get("role", "leaf"),
        )
        return {"status": "ok", "node_id": node_id, "name": name}

    @router.post("/remove-leaf")
    async def remove_leaf(request: Request):
        body = await request.json()
        node_id = body.get("id")
        if node_id is None:
            return JSONResponse(status_code=400, content={"status": "error", "detail": "id required"})
        pod = state.site.pod
        dn = f"topology/pod-{pod}/node-{node_id}"
        mo = MO("fabricNode", dn=dn, status="deleted")
        state.store.upsert(mo)
        return {"status": "ok", "removed": node_id}

    return router
