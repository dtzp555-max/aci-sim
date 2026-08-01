"""Server-side value validators for the aci-sim APIC write face (F10).

aci-sim is a faithful ACI management-plane MODEL: it stores/associates MOs
correctly but historically performed no APIC-style server-side attribute
validation. A real APIC 400-rejects out-of-range/malformed values (e.g.
``l3extRsPathL3OutAtt encap=vlan-9999``, ``l3extRsNodeL3OutAtt
rtrId=999.888.777.241``) even when the posting client (``cisco.aci.aci_rest``
raw MO bodies, in this campaign) does zero client-side validation of its own.
This module is the table-driven rule catalog that closes that gap — see the
RULES table below and its section comments for the full rule set, sourcing,
and false-reject-risk analysis. Do not add rules here without a comment
documenting that row's sourcing.

Architecture (design §3.1):
  - Small composable validator *factories* (``rng``, ``one_of``, ``fmt``,
    ``all_of``) each return a ``Validator`` callable with signature
    ``(cls: str, prop: str, value, ctx) -> None`` that raises ``ValueError``
    with a human-readable reason on an invalid value.
  - ``RULES`` maps ``(class, prop) -> Validator``. A ``(class, prop)`` pair
    with NO entry is intentionally unvalidated — see "Fail-safe" below.

Fail-safe / false-reject policy (design §3.6, load-bearing): **default-allow**.
Only pairs present in RULES are checked; every other (class, prop) passes
through untouched. The bias is hard toward "never reject a legal value" —
every rule here is proven against the canonical corpus (design §6) before
being added, and any rule that would reject a canonical value must be
loosened to admit it (never loosened to admit a known-bad value instead).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable

#: A validator inspects one (class, prop, value) triple and either returns
#: None (value is legal) or raises ValueError(reason) (value is illegal).
#: ``ctx`` is reserved for cross-field checks (Priority-3 in the design doc,
#: e.g. fvnsEncapBlk from<=to) — NOT implemented in this batch (P1+P2 only);
#: it is threaded through now so a later batch can add cross-field rules
#: without changing the RULES calling convention.
Validator = Callable[[str, str, object, object], None]


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def rng(lo: int, hi: int | None = None, *, transform: Callable[[object], object] | None = None) -> Validator:
    """Integer range check: ``lo <= int(value) <= hi``.

    *hi=None* means "no upper bound" (used for the few design-doc rules that
    only specify a floor, e.g. infraPortBlk ``fromPort/toPort/fromCard/
    toCard`` >= 1). *transform* is applied to the raw value before the int()
    conversion (e.g. stripping a "vlan-" prefix) — not currently used by any
    RULES entry below (that parsing lives in the ``vlan_encap`` fmt kind
    instead) but kept per the design §3.1 factory signature for forward use.
    """

    def _validator(cls: str, prop: str, value: object, ctx: object = None) -> None:
        raw = value if transform is None else transform(value)
        try:
            n = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError(f"not an integer, got {value!r}") from None
        if n < lo or (hi is not None and n > hi):
            bound = f"[{lo},{hi}]" if hi is not None else f">= {lo}"
            raise ValueError(f"must be {bound}, got {n}")

    return _validator


def one_of(allowed: set[str] | frozenset[str], *, list_sep: str | None = None) -> Validator:
    """Enum check. Plain membership by default; with *list_sep* the MO's
    string value is a token-list (real APIC stores multi-valued enum props
    like ``scope``/``ctrl``/``aggregate``/``peerCtrl`` as a single
    comma-joined string — confirmed against ``cisco.aci`` module source,
    e.g. ``aci_bd_subnet.py``/``aci_l3out_extsubnet.py`` doing
    ``",".join(sorted(scope))``) — split on *list_sep*, and every non-empty
    token must be a member of *allowed*. An empty value (no tokens at all)
    is treated as vacuously valid — nothing to check, and rejecting it would
    risk a false-reject on an unset/omitted enum (design §3.6 bias).
    """
    allowed_set = set(allowed)

    def _validator(cls: str, prop: str, value: object, ctx: object = None) -> None:
        if not isinstance(value, str):
            raise ValueError(f"expected a string, got {type(value).__name__}")
        if list_sep is None:
            if value not in allowed_set:
                raise ValueError(f"must be one of {sorted(allowed_set)}, got {value!r}")
            return
        tokens = [t.strip() for t in value.split(list_sep) if t.strip()]
        for token in tokens:
            if token not in allowed_set:
                raise ValueError(
                    f"token {token!r} not in allowed set {sorted(allowed_set)} (value={value!r})"
                )

    return _validator


_VLAN_ENCAP_RE = re.compile(r"^(vlan|vxlan)-(\d+)$")
_VLAN_MIN, _VLAN_MAX = 1, 4094  # NOT 4096 — see design doc §7 point 2:
# cisco.aci docs say "1 and 4096" but real APIC reserves 4095/4096; 4094 is
# the real server ceiling and comfortably admits every canonical value
# (max observed: 2611).
_VXLAN_MIN, _VXLAN_MAX = 1, 16777215  # F10 fix (independent review): the
# vlan-N 4094 ceiling does NOT apply to vxlan-N — VXLAN VNID is a 24-bit
# field (RFC 7348), max 2**24-1 = 16777215. The original single shared
# ceiling 400-rejected every legal vxlan-N value above 4094, a 100%
# false-reject on this prefix. Fail-safe bias (module docstring / design
# §3.6): take the widest legal range rather than guess a tighter one.

_MAC_COLON_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
_MAC_DOT_RE = re.compile(r"^[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}$")

#: Named L4 ports real APIC/`cisco.aci` accept in place of a numeric port,
#: sourced verbatim from the vendored cisco.aci collection's
#: `FILTER_PORT_MAPPING` (module_utils/constants.py) values, plus the
#: literal "unspecified".
_NAMED_PORTS = {"https", "smtp", "http", "dns", "pop3", "rtsp", "ftpData", "unspecified"}


def _check_vlan_encap(value: object, *, allow_unknown: bool) -> None:
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    if allow_unknown and value == "unknown":
        return
    m = _VLAN_ENCAP_RE.match(value)
    if not m:
        suffix = " or literal 'unknown'" if allow_unknown else ""
        raise ValueError(f"expected 'vlan-N' or 'vxlan-N'{suffix}, got {value!r}")
    prefix, n_str = m.group(1), m.group(2)
    n = int(n_str)
    lo, hi = (_VLAN_MIN, _VLAN_MAX) if prefix == "vlan" else (_VXLAN_MIN, _VXLAN_MAX)
    if not (lo <= n <= hi):
        raise ValueError(f"{prefix} id must be in [{lo},{hi}], got {n}")


def _check_ipv4_iface(value: object) -> None:
    """``a.b.c.d[/prefix]`` OR the IPv6 equivalent — real APIC uses the same
    attribute (``ip``/``addr``) for either family on these props. Uses the
    stdlib ``ipaddress`` module (never a regex) per design §3.1/§7 point 3.
    Reason text is byte-identical to the pre-F10 hardcoded fvSubnet/
    l3extSubnet ip check this replaces (design §3.2: "no behavior change,
    one code path") — do not append the raw ``ipaddress`` exception text
    here, existing regression tests + callers depend on the exact string."""
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    try:
        ipaddress.ip_interface(value)
    except ValueError:
        raise ValueError("not a valid address[/prefix]") from None


def _check_ip_addr(value: object) -> None:
    """A bare IPv4 or IPv6 address, no ``/prefix`` (e.g. bgpPeerP.addr,
    vnsRedirectDest.ip). Reason text is byte-identical to the pre-F10
    hardcoded vnsRedirectDest ip check this replaces — see
    ``_check_ipv4_iface`` docstring."""
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise ValueError("not a valid IP address") from None


def _check_ipv4_addr(value: object) -> None:
    """IPv4-only dotted-quad (design table: l3extRsNodeL3OutAtt.rtrId is
    explicitly "valid IPv4 dotted-quad (octet <=255)", not a dual-family
    field like the iface/addr checks above)."""
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    try:
        ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise ValueError(f"not a valid IPv4 address: {exc}") from None


def _check_mac(value: object) -> None:
    """48-bit MAC, colon form (``HH:HH:HH:HH:HH:HH``) or Cisco dot form
    (``HHHH.HHHH.HHHH``) — design §7 point 4."""
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    if _MAC_COLON_RE.match(value) or _MAC_DOT_RE.match(value):
        return
    raise ValueError(f"not a valid MAC address (colon or dot form), got {value!r}")


def _check_port(value: object) -> None:
    """int in [0,65535], or one of the named ports APIC accepts."""
    if isinstance(value, str) and value in _NAMED_PORTS:
        return
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(
            f"not an integer or a named port {sorted(_NAMED_PORTS)}, got {value!r}"
        ) from None
    if not (0 <= n <= 65535):
        raise ValueError(f"port must be in [0,65535], got {n}")


_FMT_KINDS: dict[str, Callable[..., None]] = {
    "ipv4_iface": _check_ipv4_iface,
    "ip_addr": _check_ip_addr,
    "ipv4_addr": _check_ipv4_addr,
    "mac": _check_mac,
    "port": _check_port,
}


def fmt(kind: str, **opts: object) -> Validator:
    """Format-check factory. *kind* selects the checker:

      - ``vlan_encap``: ``vlan-N`` (N in [1,4094]) or ``vxlan-N`` (N in
        [1,16777215], 24-bit VNID — NOT the vlan-N ceiling, see
        ``_VXLAN_MIN``/``_VXLAN_MAX``); pass ``allow_unknown=True`` for the
        props whose design-table rule text explicitly says "or literal
        unknown" (``fvRsPathAtt.primaryEncap``,
        ``fvRsDomAtt.encap``) — every other encap prop does NOT accept
        "unknown" per its own row text (design §7 point 1: "needs a
        per-rule unknown-ok flag").
      - ``ipv4_iface``: address[/prefix], v4 or v6, via ``ipaddress.ip_interface``.
      - ``ip_addr``: bare address, v4 or v6, via ``ipaddress.ip_address``.
      - ``ipv4_addr``: v4-only dotted-quad, via ``ipaddress.IPv4Address``.
      - ``mac``: 48-bit MAC, colon or dot form.
      - ``port``: int in [0,65535] or a named L4 port.
    """
    if kind == "vlan_encap":
        allow_unknown = bool(opts.get("allow_unknown", False))

        def _validator(cls: str, prop: str, value: object, ctx: object = None) -> None:
            _check_vlan_encap(value, allow_unknown=allow_unknown)

        return _validator

    checker = _FMT_KINDS.get(kind)
    if checker is None:
        raise ValueError(f"unknown fmt kind {kind!r}")

    def _validator(cls: str, prop: str, value: object, ctx: object = None) -> None:
        checker(value)

    return _validator


def mtu() -> Validator:
    """Literal ``inherit`` or int in [576,9216] — l3extRsPathL3OutAtt.mtu /
    fvBD.mtu (design table row 2A "mtu")."""
    _range = rng(576, 9216)

    def _validator(cls: str, prop: str, value: object, ctx: object = None) -> None:
        if isinstance(value, str) and value == "inherit":
            return
        _range(cls, prop, value, ctx)

    return _validator


def all_of(*validators: Validator) -> Validator:
    """Compose several validators on one prop; all must pass."""

    def _validator(cls: str, prop: str, value: object, ctx: object = None) -> None:
        for v in validators:
            v(cls, prop, value, ctx)

    return _validator


# ---------------------------------------------------------------------------
# RULES — (class, prop) -> Validator
#
# Every entry below falls into one of two source categories: §2A
# (Priority 1 — server-only format/range) or §2B (Priority 2 — server-only
# enums), per the rule-source notes in this module's docstring. Cross-field /
# cardinality rules (§2C, e.g. infraPortBlk fromPort<=toPort, fvnsEncapBlk
# from<=to) are OUT OF SCOPE for this batch (P1+P2 only) — deliberately not
# implemented here.
# ---------------------------------------------------------------------------

RULES: dict[tuple[str, str], Validator] = {
    # -- §2A Priority 1: server-only format/range --------------------------
    # encap (vlan-N N in [1,4094] / vxlan-N N in [1,16777215] — see
    # _VXLAN_MIN/_VXLAN_MAX) — "unknown" only where the row text says so.
    ("l3extRsPathL3OutAtt", "encap"): fmt("vlan_encap"),
    ("fvRsPathAtt", "encap"): fmt("vlan_encap"),
    ("fvRsPathAtt", "primaryEncap"): fmt("vlan_encap", allow_unknown=True),
    ("fvRsDomAtt", "encap"): fmt("vlan_encap", allow_unknown=True),
    # fvnsEncapBlk from/to: same vlan-N format+range; from<=to cross-check is
    # §2C (out of scope this batch).
    ("fvnsEncapBlk", "from"): fmt("vlan_encap"),
    ("fvnsEncapBlk", "to"): fmt("vlan_encap"),
    # rtrId: IPv4-only dotted-quad.
    ("l3extRsNodeL3OutAtt", "rtrId"): fmt("ipv4_addr"),
    # l3extRsPathL3OutAtt.addr: iface form (a.b.c.d[/p]), v4 or v6.
    ("l3extRsPathL3OutAtt", "addr"): fmt("ipv4_iface"),
    # bgpPeerP.addr: iface form (a.b.c.d[/p]), v4 or v6 — F10 fix
    # (independent review): real ACI subnet-based BGP peers legally carry a
    # peer addr with an optional /prefix (e.g. "10.100.211.0/30"); the
    # original bare-address-only ip_addr check 400-rejected those. Widened
    # to ipv4_iface (ip_interface, same as l3extRsPathL3OutAtt.addr) which
    # still accepts a bare address (defaults to /32 or /128) so the
    # existing no-prefix canonical values keep passing too.
    ("bgpPeerP", "addr"): fmt("ipv4_iface"),
    ("bgpPeerP", "ttl"): rng(1, 255),
    ("bgpAsP", "asn"): rng(1, 4294967295),
    ("l3extRsPathL3OutAtt", "mtu"): mtu(),
    ("fvBD", "mtu"): mtu(),
    ("fvBD", "mac"): fmt("mac"),
    ("vnsRedirectDest", "mac"): fmt("mac"),
    # infraPortBlk: int >= 1 (no stated upper bound; le-cross-check is §2C).
    ("infraPortBlk", "fromPort"): rng(1, None),
    ("infraPortBlk", "toPort"): rng(1, None),
    ("infraPortBlk", "fromCard"): rng(1, None),
    ("infraPortBlk", "toCard"): rng(1, None),
    # vzEntry ports: int in [0,65535] or a named port.
    ("vzEntry", "dFromPort"): fmt("port"),
    ("vzEntry", "dToPort"): fmt("port"),
    ("vzEntry", "sFromPort"): fmt("port"),
    ("vzEntry", "sToPort"): fmt("port"),

    # -- §2A Priority 1 (continued): ip already-validated (migrated, byte-
    # identical semantics to the pre-F10 hardcoded branches in writes.py) --
    ("fvSubnet", "ip"): fmt("ipv4_iface"),
    ("l3extSubnet", "ip"): fmt("ipv4_iface"),
    ("vnsRedirectDest", "ip"): fmt("ip_addr"),

    # -- §2B Priority 2: server-only enums ----------------------------------
    ("fvSubnet", "scope"): one_of({"private", "public", "shared"}, list_sep=","),
    ("fvSubnet", "ctrl"): one_of(
        {"nd", "no-default-gateway", "querier", "unspecified"}, list_sep=","
    ),
    ("l3extSubnet", "scope"): one_of(
        {
            "import-security",
            "export-rtctrl",
            "import-rtctrl",
            "shared-rtctrl",
            "shared-security",
        },
        list_sep=",",
    ),
    ("l3extSubnet", "aggregate"): one_of(
        {"export-rtctrl", "import-rtctrl", "shared-rtctrl"}, list_sep=","
    ),
    ("fvCtx", "pcEnfPref"): one_of({"enforced", "unenforced"}),
    ("fvCtx", "pcEnfDir"): one_of({"ingress", "egress"}),
    ("fvBD", "unkMacUcastAct"): one_of({"proxy", "flood"}),
    ("fvBD", "multiDstPktAct"): one_of({"bd-flood", "drop", "encap-flood"}),
    ("fvBD", "unkMcastAct"): one_of({"flood", "opt-flood"}),
    ("fvBD", "type"): one_of({"regular", "fc"}),
    ("fvBD", "epMoveDetectMode"): one_of({"", "garp"}),
    ("vzEntry", "etherT"): one_of(
        {
            "arp",
            "fcoe",
            "ip",
            "ipv4",
            "ipv6",
            "mac_security",
            "mpls_ucast",
            "trill",
            "unspecified",
        }
    ),
    ("vzBrCP", "scope"): one_of({"context", "global", "tenant", "application-profile"}),
    ("fvRsDomAtt", "classPref"): one_of({"encap", "useg"}),
    ("fvRsDomAtt", "instrImedcy"): one_of({"immediate", "lazy"}),
    # resImedcy (Resolution Immediacy) — F10 fix (independent review): real
    # ACI's legal set is {immediate, lazy, pre-provision}; the original
    # rule dropped "pre-provision", which is the value 100% of the
    # canonical corpus's EPG-domain bindings actually use (every
    # bind_epg_to_*_domain / migrate_existing_vlan* render), so the bug
    # 400-rejected every one of them. Do NOT add "pre-provision" to
    # instrImedcy (Deployment Immediacy) above — that enum really is just
    # {immediate, lazy} per its own design-table row and 100% of the
    # corpus uses "immediate".
    ("fvRsDomAtt", "resImedcy"): one_of({"immediate", "lazy", "pre-provision"}),
    ("bgpPeerP", "peerCtrl"): one_of(
        {"bfd", "dis-conn-check", "nh-self", "allow-self-as", "send-com", "send-ext-com"},
        list_sep=",",
    ),
    ("fvnsVlanInstP", "allocMode"): one_of({"dynamic", "static", "inherit"}),
    ("fvnsEncapBlk", "allocMode"): one_of({"dynamic", "static", "inherit"}),
}
