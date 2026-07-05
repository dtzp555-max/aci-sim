"""Main APIC FastAPI application.

URL routing matches exactly what aci_connector.py builds:
  - GET    /api/class/{cls}.json
  - GET    /api/mo/{dn:path}          (dn may contain slashes + brackets)
  - POST   /api/mo/{dn:path}
  - DELETE /api/mo/{dn:path}          (PR-9 — cisco.aci state=absent)
  - GET/POST/DELETE /api/node/mo/{dn:path}   (PR-10 — real-APIC alias, see below)
  - GET  /api/node/class/{rest:path}
  - Auth: /api/aaaLogin.json, /api/aaaRefresh.json, /api/aaaLogout.json
  - Control: /_sim/reset, /_sim/snapshot/{name}, etc.

PR-10 — /api/node/mo/{dn} alias (live E2E blocker, 11 failures). Real APIC
serves /api/node/mo/{dn}.json as a full equivalent of /api/mo/{dn}.json — it
is the form the APIC GUI's API Inspector emits and what cisco.aci's
aci_rest module (and some aci_connector.py callers) actually issue. The sim
only registered /api/mo/*, so a node-alias request fell through to FastAPI's
default 404 {"detail":"Not Found"} — a shape cisco.aci cannot parse
("APIC Error None: None", status=-1). Each verb below is registered on BOTH
paths, sharing one handler body (stacked route decorators) so behavior can
never drift between the canonical and alias forms. A catch-all for any other
unmatched /api/* path returns an APIC-shaped 400 error envelope instead of
FastAPI's {"detail":...} — see `_unmatched_api_path` below.

PR-9 DELETE semantics: cisco.aci's module_utils/aci.py (`delete_config()`)
only issues an HTTP DELETE after its own `get_existing()` GET has confirmed
the MO is present — `if not self.existing: return` short-circuits before
ever calling DELETE (verified read-only against upstream
plugins/module_utils/aci.py). So the module itself never exercises a
DELETE-on-already-absent-DN round trip. This sim still makes the route
idempotent (200 + empty imdata on a missing DN, same as a present one)
rather than 404, for two reasons: (1) it matches the MIT's natural "delete
what's not there = no-op" semantics used elsewhere in this codebase
(MITStore.upsert's status=deleted branch is unconditional, no existence
check), and (2) it is the safer default for any OTHER client (raw curl,
future modules) that DOES call DELETE without a prior GET — a 404 there
would make state=absent playbooks non-idempotent on second-run against
such a client, which is the one thing real APIC's actual DELETE endpoint
is documented to guarantee. See docs/CONTRACT.md §Ansible-compatibility.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from aci_sim.mit.store import MITStore
from aci_sim.query.engine import (
    params_from_dict,
    run_class_query,
    run_mo_query,
    run_node_scoped,
)
from aci_sim.query.filters import FilterParseError
from aci_sim.rest_aci import subscriptions as subs
from aci_sim.rest_aci.auth import (
    _auth_error_403,
    _lookup_session,
    make_auth_router,
    require_session,
)
from aci_sim.rest_aci.writes import WriteValidationError
from aci_sim.rest_aci.writes import apply as write_apply
from aci_sim.topology.schema import Site, Topology


@dataclass
class ApicSiteState:
    name: str
    site: Site
    topo: Topology
    store: MITStore
    baseline: MITStore  # deepcopy at construction time


def _apic_ok(imdata: list[dict], total: int) -> dict:
    """Wrap imdata in the APIC envelope with string totalCount."""
    return {"imdata": imdata, "totalCount": str(total)}


def _apic_error(text: str, code: str = "103", status_code: int = 400) -> JSONResponse:
    """Return an APIC error envelope as a JSONResponse."""
    body = {
        "imdata": [{"error": {"attributes": {"text": text, "code": code}}}],
        "totalCount": "1",
    }
    return JSONResponse(status_code=status_code, content=body)


def _maybe_subscribe(
    body: dict, request: Request, token: str | None, scope_kind: str, scope_value: str
) -> dict:
    """Opt-in subscription hook shared by all three query routes.

    If the request carries ``?subscription=yes``, registers a subscription
    for *token* watching *scope_kind*/*scope_value* and adds a top-level
    ``subscriptionId`` key to *body* (mirroring real APIC — the field sits
    alongside ``imdata``/``totalCount``, not inside them). Without that query
    param, *body* is returned completely untouched — no key added, no
    registry write — so plain queries stay byte-identical to pre-subscription
    behavior (backward-compat requirement).
    """
    if request.query_params.get("subscription") != "yes":
        return body
    if not token:
        # Should not happen — require_session already gated the route — but
        # never register a subscription with no token to notify later.
        return body
    sid = subs.subscribe(token, scope_kind, scope_value)
    body = dict(body)
    body["subscriptionId"] = sid
    return body


def make_apic_app(state: ApicSiteState) -> FastAPI:
    app = FastAPI(title=f"aci-sim APIC {state.name}")

    # --- Auth routes ---
    app.include_router(make_auth_router())

    # --- Control-plane admin routes ---
    from aci_sim.control.admin import make_admin_router
    app.include_router(make_admin_router(state))

    # --- Class query ---
    @app.get("/api/class/{cls}.json")
    async def get_class(cls: str, request: Request):
        if require_session(request) is None:
            return _auth_error_403()
        try:
            params = params_from_dict(dict(request.query_params))
            imdata, total = run_class_query(state.store, cls, params)
        except FilterParseError as exc:
            return _apic_error(str(exc), code="107", status_code=400)
        body = _apic_ok(imdata, total)
        token = request.cookies.get("APIC-cookie")
        return _maybe_subscribe(body, request, token, "class", cls)

    # --- MO query (GET) ---
    # dn:path captures "uni/tn-Corp.json" including any slashes and brackets.
    # We must strip the trailing ".json".
    # PR-10: /api/node/mo/{dn} is a full alias of /api/mo/{dn} on real APIC
    # (API Inspector / aci_rest form) — same handler, stacked route.
    @app.get("/api/mo/{dn:path}")
    @app.get("/api/node/mo/{dn:path}")
    async def get_mo(dn: str, request: Request):
        if require_session(request) is None:
            return _auth_error_403()
        if dn.endswith(".json"):
            dn = dn[:-5]
        # PR-9 FEATURE 6 (live blocker, verified against upstream
        # module_utils/aci.py's api_call(): status != 200 is UNCONDITIONALLY
        # fatal via fail_json — there is no "404 is fine for GET" special
        # case). cisco.aci's state=present flow always does a GET-before-POST
        # existence check first; on a brand-new object that GET's DN does not
        # exist yet. Real APIC returns 200 + empty imdata for a well-formed
        # DN that simply doesn't exist (not 404) — 404/error codes are for
        # malformed requests, not absent objects. A 404 here made every
        # aci_tenant/aci_* state=present playbook fail on its very first run
        # ("APIC Error 103: Unable to find the object specified"). Superseded
        # the prior 404-on-missing-DN design (originally justified as
        # matching an autoACI ACINotFoundError expectation — that consumer
        # is a different client with different semantics; cisco.aci is this
        # simulator's primary use case per DESIGN.md and takes precedence).
        try:
            params = params_from_dict(dict(request.query_params))
            imdata, total = run_mo_query(state.store, dn, params)
        except FilterParseError as exc:
            return _apic_error(str(exc), code="107", status_code=400)
        body = _apic_ok(imdata, total)
        token = request.cookies.get("APIC-cookie")
        return _maybe_subscribe(body, request, token, "dn", dn)

    # --- MO write (POST) ---
    # PR-10: /api/node/mo/{dn} alias — see get_mo above.
    @app.post("/api/mo/{dn:path}")
    @app.post("/api/node/mo/{dn:path}")
    async def post_mo(dn: str, request: Request):
        if require_session(request) is None:
            return _auth_error_403()
        if dn.endswith(".json"):
            dn = dn[:-5]
        try:
            body = await request.json()
        except Exception:
            return _apic_error("Invalid JSON body", code="103", status_code=400)
        if not body or not isinstance(body, dict):
            return _apic_error("Empty or non-object body", code="103", status_code=400)
        cls = next(iter(body))
        if not isinstance(body[cls], dict):
            return _apic_error("Invalid MO body structure", code="103", status_code=400)
        # Subscriptions need created-vs-modified, which writes.apply's own
        # response shape doesn't distinguish (it always reports "created"
        # for a non-delete write — see writes.py's `apply()`). Determine it
        # here, pre-write, purely for the push event; the HTTP response body
        # below is unchanged from before this feature.
        effective_dn = (body[cls].get("attributes") or {}).get("dn") or dn
        pre_existing = state.store.get(effective_dn) is not None
        try:
            imdata, total = write_apply(state.store, dn, body, topo=state.topo, site=state.site)
        except (WriteValidationError, AttributeError, TypeError, KeyError) as exc:
            return _apic_error(f"Malformed MO body: {exc}", code="103", status_code=400)
        # Push-on-change (subscriptions): notify after the store commit above.
        # imdata is the write result shape [{cls: {"attributes": {"dn":..., "status": "created"|"deleted"}}}]
        # from writes.apply — one entry per top-level POSTed class (children
        # are written but not individually reported here, matching the
        # existing non-subscription response shape).
        for entry in imdata:
            for mo_cls, mo_body in entry.items():
                mo_attrs = mo_body.get("attributes", {})
                mo_dn = mo_attrs.get("dn", dn)
                raw_status = mo_attrs.get("status", "created")
                if raw_status == "deleted":
                    push_status = "deleted"
                elif mo_dn == effective_dn and pre_existing:
                    push_status = "modified"
                else:
                    push_status = "created"
                subs.notify(mo_cls, mo_dn, push_status, mo_attrs)
        return _apic_ok(imdata, total)

    # --- MO delete (DELETE) ---
    # cisco.aci state=absent path: DELETE /api/mo/{dn}.json. Removes the DN's
    # entire subtree from the store. Idempotent: a missing DN also returns
    # 200 + empty imdata rather than 404 (see module docstring above for the
    # real-module-behavior justification).
    # PR-10: /api/node/mo/{dn} alias — see get_mo above.
    @app.delete("/api/mo/{dn:path}")
    @app.delete("/api/node/mo/{dn:path}")
    async def delete_mo(dn: str, request: Request):
        if require_session(request) is None:
            return _auth_error_403()
        if dn.endswith(".json"):
            dn = dn[:-5]
        # Capture the whole subtree BEFORE deleting so push-on-change can
        # notify for every removed MO (a DN-scoped subscription may be
        # watching a descendant, and a class-scoped one may be watching any
        # class present in the subtree, not just the root DN's own class).
        existing = state.store.get(dn)
        removed = ([existing] if existing is not None else []) + state.store.subtree(dn)
        state.store.delete(dn)
        for mo in removed:
            subs.notify(mo.class_name, mo.dn, "deleted", mo.attrs)
        return _apic_ok([], 0)

    # --- Node-scoped class query ---
    # rest captures everything after /api/node/class/
    # e.g. rest = "topology/pod-1/node-101/faultInst.json" (node-scoped)
    #      rest = "topSystem.json"                          (fabric-wide)
    #
    # Real APIC also supports the fabric-wide form of this endpoint — no
    # topology/pod-X/node-Y prefix, just the bare "{cls}.json" — which is
    # equivalent to /api/class/{cls}.json (finding #12). Distinguish the two
    # by whether *rest* (once ".json" is stripped) contains a "/": a
    # node-scoped DN always does (it's a full topology/... path), while the
    # fabric-wide form is a single path segment.
    @app.get("/api/node/class/{rest:path}")
    async def get_node_class(rest: str, request: Request):
        if require_session(request) is None:
            return _auth_error_403()
        token = request.cookies.get("APIC-cookie")
        stripped = rest[:-5] if rest.endswith(".json") else rest
        if "/" not in stripped:
            # Fabric-wide form: {cls}.json with no DN prefix at all.
            cls = stripped
            try:
                params = params_from_dict(dict(request.query_params))
                imdata, total = run_class_query(state.store, cls, params)
            except FilterParseError as exc:
                return _apic_error(str(exc), code="107", status_code=400)
            body = _apic_ok(imdata, total)
            return _maybe_subscribe(body, request, token, "class", cls)

        parts = rest.rsplit("/", 1)
        if len(parts) != 2:
            return _apic_error("Malformed node-scoped URL", code="103", status_code=400)
        node_dn = parts[0]
        cls = parts[1]
        if cls.endswith(".json"):
            cls = cls[:-5]
        try:
            params = params_from_dict(dict(request.query_params))
            imdata, total = run_node_scoped(state.store, node_dn, cls, params)
        except FilterParseError as exc:
            return _apic_error(str(exc), code="107", status_code=400)
        body = _apic_ok(imdata, total)
        return _maybe_subscribe(body, request, token, "dn", node_dn)

    # --- Subscription refresh ---
    # GET /api/subscriptionRefresh.json?id=<id> — keeps a subscription alive
    # past its TTL (real APIC's default subscription lifetime is 90s,
    # refreshed by polling this endpoint). Always 200s for a live session;
    # an unknown/expired id still returns 200 with a small APIC-shaped
    # imdata (real APIC does not error a stale refresh — the caller just
    # re-subscribes on its next query if the id no longer resolves).
    @app.get("/api/subscriptionRefresh.json")
    async def subscription_refresh(request: Request):
        if require_session(request) is None:
            return _auth_error_403()
        subscription_id = request.query_params.get("id", "")
        subs.refresh(subscription_id)
        return {"imdata": [], "totalCount": "0", "subscriptionId": subscription_id}

    # --- Websocket push channel ---
    # GET /socket<token> — real APIC's subscription websocket path, where
    # <token> is the same APIC-cookie token minted by aaaLogin. FastAPI/
    # Starlette route params can't match a bare suffix glued onto a fixed
    # prefix with no separator (real APIC literally concatenates the token
    # onto "/socket" — no slash), so this uses a `{token}` path param on the
    # pattern "/socket{token}", which Starlette resolves correctly (the
    # literal "/socket" prefix consumes up to where the param begins).
    @app.websocket("/socket{token}")
    async def subscription_socket(websocket: WebSocket, token: str):
        session = _lookup_session(token)
        if session is None:
            # Unknown/expired token — reject before accept (real APIC closes
            # the upgrade for an invalid session token).
            await websocket.close(code=4403)
            return
        await websocket.accept()
        queue = subs.register_connection(token)

        async def _pump_events() -> None:
            """Drain the queue and push each event, forever."""
            while True:
                event = await queue.get()
                await websocket.send_json(event)

        async def _watch_for_disconnect() -> None:
            """Block on receive() so a client-initiated close is noticed
            promptly even while _pump_events is idle waiting on an empty
            queue (send_json alone only surfaces a disconnect on the NEXT
            push, which could be arbitrarily far in the future — or never,
            for a subscriber that stops getting matching events). receive()
            does not raise on a disconnect message (only the receive_text/
            receive_json/receive_bytes wrappers do) — it just returns it, so
            this checks the message type itself and raises to match
            _pump_events' failure mode.
            """
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect(code=message.get("code", 1000))

        pump_task = asyncio.ensure_future(_pump_events())
        watch_task = asyncio.ensure_future(_watch_for_disconnect())
        try:
            done, _pending = await asyncio.wait(
                {pump_task, watch_task}, return_when=asyncio.FIRST_EXCEPTION
            )
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    raise exc
        except WebSocketDisconnect:
            pass
        finally:
            pump_task.cancel()
            watch_task.cancel()
            subs.drop_connection(token)

    # --- Unmatched /api/* fallback ---
    # PR-10: real APIC never emits FastAPI's default 404 {"detail":"Not
    # Found"} shape — cisco.aci can't parse it (surfaces as "APIC Error
    # None: None", status=-1). This handler only fires once routing has
    # already failed to match every route above (including the /api/node/mo
    # alias and /api/node/class/*), so it can never shadow a real route —
    # it is Starlette's very last resort for an unmatched path, not a route
    # itself. Scoped to /api/* only; anything outside that prefix (e.g. a
    # typo'd /_sim/* control path) still gets the default FastAPI 404, since
    # this sim's control plane is not part of the APIC REST contract.
    @app.exception_handler(StarletteHTTPException)
    async def _unmatched_api_path(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404 and request.url.path.startswith("/api/"):
            return _apic_error(
                f"Invalid request path: {request.url.path}", code="400", status_code=400
            )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return app
