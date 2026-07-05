"""APIC query-subscription registry + websocket push-on-change (PR — subscriptions).

Real APIC's subscription mechanism (verified against the documented behavior
cisco.aci / ACI SDK clients and monitoring tools rely on):

  1. **Subscribe**: any of the three query shapes (class/mo/node-class) accepts
     `?subscription=yes`. The response is the NORMAL query result (imdata +
     totalCount, unchanged) PLUS a top-level `"subscriptionId"` string. The
     query itself also establishes the "current state" the subscription is
     watching relative to.
  2. **Websocket**: the client opens `GET /socket<token>` where `<token>` is
     the same APIC-cookie token from aaaLogin. All of that session's live
     subscriptions are pushed over this one connection.
  3. **Push on change**: when a write (POST create/update or DELETE) commits,
     APIC evaluates every live subscription's scope against the changed
     MO(s). Any match gets a push:
     `{"subscriptionId":[<id>,...],"imdata":[{<class>:{"attributes":{...,
     "status":"created|modified|deleted","dn":...}}}]}`.
  4. **Refresh**: `GET /api/subscriptionRefresh.json?id=<id>` keeps a
     subscription alive past its TTL; real APIC's default subscription
     lifetime is 90s, refreshed by polling this endpoint.

This module is the in-memory registry + notify/bridge glue. It has ZERO
FastAPI route decorators of its own — `app.py` wires the HTTP/websocket
routes and calls into this module's functions, matching how `auth.py` keeps
its session store separate from route registration.

Sync-write -> async-websocket bridge
-------------------------------------
`app.py`'s query/write handlers are `async def` but call straight into sync
store code — there's no actual concurrency boundary within a single request.
The awkward part is different: `notify()` must be callable from those
handlers (regular function-call context, not itself a coroutine) but the
actual send happens on a websocket that's being driven by its own `asyncio`
task (`websocket.receive()` loop). Two connections could be subscribed to
the same class, and a slow/stuck client must never block or break the write
request that triggered the notification.

The fix: give every websocket connection its own `asyncio.Queue`. `notify()`
is a plain sync function — it just does `queue.put_nowait(event)` (never
blocks, drops nothing since the queue is unbounded) for every connection
whose subscriptions match. The websocket route runs a small async task that
does `event = await queue.get(); await websocket.send_json(event)` in a loop.
This decouples "a write happened" (sync call stack) from "bytes went out on
a socket" (async task on that connection's own schedule) with no shared
event-loop reentrancy concerns and no risk of a write request awaiting
network I/O on some other client's socket.

`app.py`'s route actually runs two tasks per connection, raced via
``asyncio.wait(..., return_when=FIRST_EXCEPTION)``: one draining this
queue and pushing events, one blocked on ``websocket.receive()`` purely to
notice a client-initiated disconnect promptly (a lone queue-drain loop
would only discover a dead connection the next time it tried to push —
arbitrarily late, or never, for a subscriber sitting on a quiet class).
Either task finishing tears down both and calls `drop_connection`.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field

# Real APIC's default subscription lifetime is 90s; refreshed via
# GET /api/subscriptionRefresh.json?id=<id>. Kept generous here since this
# sim has no eviction sweep beyond the refresh no-op / disconnect cleanup.
SUBSCRIPTION_LIFETIME_SECONDS: float = 90.0

_id_counter = itertools.count(1)


@dataclass
class _Connection:
    """One live `/socket<token>` websocket connection for a session token."""

    token: str
    queue: "asyncio.Queue[dict]" = field(default_factory=asyncio.Queue)


@dataclass
class Subscription:
    """A single registered subscription (one per `?subscription=yes` query).

    scope is either:
      - ("class", "<className>")   — class query / node-class fabric-wide form
      - ("dn", "<dn>")              — mo query (dn-scoped; also matches the
                                       DN's subtree so a child MO's change
                                       still notifies a parent-DN subscriber)
    """

    id: str
    token: str
    scope_kind: str  # "class" | "dn"
    scope_value: str
    expires_at: float


# subscriptionId -> Subscription
_subscriptions: dict[str, Subscription] = {}
# token -> _Connection (at most one live websocket per session token)
_connections: dict[str, _Connection] = {}


def _now() -> float:
    return time.monotonic()


def reset() -> None:
    """Test/isolation helper: drop all subscriptions + connections."""
    _subscriptions.clear()
    _connections.clear()


def _prune_expired() -> None:
    now = _now()
    expired = [sid for sid, sub in _subscriptions.items() if sub.expires_at <= now]
    for sid in expired:
        del _subscriptions[sid]


def subscribe(token: str, scope_kind: str, scope_value: str) -> str:
    """Register a new subscription for *token* watching *scope_kind*/*scope_value*.

    Returns the new subscriptionId (a string, matching real APIC's shape).
    """
    _prune_expired()
    sid = str(next(_id_counter))
    _subscriptions[sid] = Subscription(
        id=sid,
        token=token,
        scope_kind=scope_kind,
        scope_value=scope_value,
        expires_at=_now() + SUBSCRIPTION_LIFETIME_SECONDS,
    )
    return sid


def refresh(subscription_id: str) -> bool:
    """Renew a subscription's TTL. Returns False if the id is unknown/expired."""
    _prune_expired()
    sub = _subscriptions.get(subscription_id)
    if sub is None:
        return False
    sub.expires_at = _now() + SUBSCRIPTION_LIFETIME_SECONDS
    return True


def register_connection(token: str) -> "asyncio.Queue[dict]":
    """Associate a websocket connection with *token*; returns its event queue.

    Only one live connection per token is tracked (matches real APIC — a
    session has one websocket). A reconnect replaces the prior queue.
    """
    conn = _Connection(token=token)
    _connections[token] = conn
    return conn.queue


def drop_connection(token: str) -> None:
    """Remove the connection + any subscriptions owned by *token* (disconnect cleanup)."""
    _connections.pop(token, None)
    _prune_expired()
    dead = [sid for sid, sub in _subscriptions.items() if sub.token == token]
    for sid in dead:
        del _subscriptions[sid]


def is_known_token(token: str) -> bool:
    """True if *token* has ever registered a connection (used only for tests/debug)."""
    return token in _connections


def _dn_matches(scope_dn: str, changed_dn: str) -> bool:
    """True if *changed_dn* is *scope_dn* itself or lives in its subtree."""
    return changed_dn == scope_dn or changed_dn.startswith(scope_dn + "/")


def notify(cls: str, dn: str, status: str, attributes: dict) -> None:
    """Push an event to every live subscription whose scope matches this change.

    Called synchronously from the write path (POST/DELETE handlers in
    app.py) right after the store commit. Never awaits, never raises for a
    slow/absent client — `asyncio.Queue.put_nowait` on an unbounded queue
    cannot block, and a token with no live connection (subscribed but the
    websocket hasn't connected yet, or already disconnected) is skipped.

    *attributes* should be the MO's attribute dict (will be shallow-copied)
    with ``dn``/``status`` already set or overridable via the explicit args.
    """
    _prune_expired()
    matched: dict[str, list[str]] = {}  # token -> [subscriptionId, ...]
    for sid, sub in _subscriptions.items():
        if sub.scope_kind == "class" and sub.scope_value == cls:
            matched.setdefault(sub.token, []).append(sid)
        elif sub.scope_kind == "dn" and _dn_matches(sub.scope_value, dn):
            matched.setdefault(sub.token, []).append(sid)

    if not matched:
        return

    attrs = dict(attributes)
    attrs["dn"] = dn
    attrs["status"] = status
    event_body = {cls: {"attributes": attrs}}

    for token, sids in matched.items():
        conn = _connections.get(token)
        if conn is None:
            continue
        event = {"subscriptionId": sids, "imdata": [event_body]}
        conn.queue.put_nowait(event)
