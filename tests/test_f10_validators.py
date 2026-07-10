"""F10 — server-side value validation, unit level (aci_sim/rest_aci/validators.py).

Table-driven per this suite's own zero-false-reject proof methodology: every
canonical value that appears in the
real-hardware-passing corpus (``~/e2e-20260708/vars/{ms,sf,common}/`` +
``logs/ansible/*create_tenant*.yml``) MUST pass; every F10-probe bad value
(``logs/probe/*probe_L2b.yml``) and one or two hand-built out-of-range/format
errors per rule MUST 400 (here: raise ValueError, since these are unit-level
RULES lookups — the FastAPI-level 400 plumbing is exercised separately in
tests/test_f10_apic_writeface.py and tests/test_f10_ndo_validation.py).

This file also locks the fail-safe default-allow contract (design §3.6): an
unregistered (class, prop) pair must never be checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aci_sim.rest_aci.validators import RULES, fmt, mtu, one_of, rng

# ---------------------------------------------------------------------------
# Golden set: canonical values that must ACCEPT. (cls, prop, value) — sourced
# from the design doc's own corpus sample (§6 table) and, where noted, the
# live canonical corpus at ~/e2e-20260708/vars/{ms,sf,common}/ on PI230.
# ---------------------------------------------------------------------------
ACCEPT_CASES: list[tuple[str, str, str]] = [
    # encap (vlan-N) — real l3out_vlan/bind_epg_to_static_port/migrate_
    # existing_vlan values from ms/sf vars.
    ("l3extRsPathL3OutAtt", "encap", "vlan-51"),
    ("l3extRsPathL3OutAtt", "encap", "vlan-100"),
    ("l3extRsPathL3OutAtt", "encap", "vlan-2501"),
    ("l3extRsPathL3OutAtt", "encap", "vlan-2611"),
    ("l3extRsPathL3OutAtt", "encap", "vlan-1"),
    ("l3extRsPathL3OutAtt", "encap", "vlan-4094"),
    ("fvRsPathAtt", "encap", "vlan-2502"),
    ("fvRsPathAtt", "encap", "vlan-2503"),
    ("fvRsPathAtt", "encap", "vlan-2602"),
    ("fvRsPathAtt", "encap", "vlan-2603"),
    ("fvRsPathAtt", "primaryEncap", "vlan-100"),
    ("fvRsPathAtt", "primaryEncap", "unknown"),  # edge: literal unknown allowed here
    ("fvRsDomAtt", "encap", "vlan-51"),
    ("fvRsDomAtt", "encap", "unknown"),  # edge: literal unknown allowed here
    ("fvnsEncapBlk", "from", "vlan-1"),
    ("fvnsEncapBlk", "to", "vlan-4094"),
    ("fvnsEncapBlk", "from", "vlan-2501"),
    # vxlan-N — F10 F2 fix: NOT capped at the vlan-N 4094 ceiling (24-bit
    # VNID space instead). 5000 is a legal VNID that the pre-fix shared
    # ceiling would have 400-rejected.
    ("l3extRsPathL3OutAtt", "encap", "vxlan-5000"),
    # rtrId — IPv4-only dotted quad (l3out node profile router-id).
    ("l3extRsNodeL3OutAtt", "rtrId", "10.100.201.241"),
    ("l3extRsNodeL3OutAtt", "rtrId", "10.100.202.242"),
    ("l3extRsNodeL3OutAtt", "rtrId", "10.100.211.243"),
    ("l3extRsNodeL3OutAtt", "rtrId", "10.100.212.241"),
    # l3extRsPathL3OutAtt.addr — interface form with /prefix, v4 and v6.
    ("l3extRsPathL3OutAtt", "addr", "10.100.201.17/30"),
    ("l3extRsPathL3OutAtt", "addr", "10.100.211.49/30"),
    ("l3extRsPathL3OutAtt", "addr", "2001:db8::1/64"),  # edge: IPv6
    # bgpPeerP.addr — bare address, v4 and v6.
    ("bgpPeerP", "addr", "10.100.0.1"),
    ("bgpPeerP", "addr", "2001:db8::1"),  # edge: IPv6
    # bgpPeerP.addr with /prefix — F10 F3 fix: subnet-based BGP peers
    # legally carry a /prefix; the old ip_addr (bare-only) check rejected it.
    ("bgpPeerP", "addr", "10.100.211.0/30"),
    ("bgpPeerP", "ttl", "1"),
    ("bgpPeerP", "ttl", "255"),
    ("bgpPeerP", "ttl", "2"),
    ("bgpAsP", "asn", "65100"),
    ("bgpAsP", "asn", "1"),
    ("bgpAsP", "asn", "4294967295"),
    # mtu — literal inherit or [576,9216].
    ("l3extRsPathL3OutAtt", "mtu", "9000"),
    ("l3extRsPathL3OutAtt", "mtu", "9216"),
    ("l3extRsPathL3OutAtt", "mtu", "1492"),
    ("l3extRsPathL3OutAtt", "mtu", "576"),
    ("l3extRsPathL3OutAtt", "mtu", "inherit"),  # edge: literal inherit
    ("fvBD", "mtu", "9000"),
    ("fvBD", "mtu", "inherit"),  # edge: literal inherit
    # mac — colon and Cisco dot form.
    ("fvBD", "mac", "00:22:BD:F8:19:FF"),  # the sim's own BD create-default
    ("fvBD", "mac", "00:11:22:33:44:55"),
    ("fvBD", "mac", "0011.2233.4455"),  # edge: dot form
    ("vnsRedirectDest", "mac", "00:00:0c:9f:f7:01"),
    ("vnsRedirectDest", "mac", "0000.0c9f.f701"),  # edge: dot form
    # infraPortBlk — int >= 1.
    ("infraPortBlk", "fromPort", "1"),
    ("infraPortBlk", "toPort", "48"),
    ("infraPortBlk", "fromCard", "1"),
    ("infraPortBlk", "toCard", "1"),
    # vzEntry ports — int in [0,65535] or a named port.
    ("vzEntry", "dFromPort", "443"),
    ("vzEntry", "dToPort", "https"),  # edge: named
    ("vzEntry", "sFromPort", "0"),
    ("vzEntry", "sToPort", "unspecified"),  # edge: named
    ("vzEntry", "dFromPort", "20"),
    # ip (migrated, byte-identical semantics) — including the 0.0.0.0/0 and
    # 192.168.x/24 forms from create_bd/create_tenant vars.
    ("fvSubnet", "ip", "192.168.41.1/24"),
    ("fvSubnet", "ip", "192.168.31.1/24"),
    ("fvSubnet", "ip", "192.168.250.1/24"),
    ("l3extSubnet", "ip", "0.0.0.0/0"),
    ("l3extSubnet", "ip", "10.100.201.17/30"),
    ("vnsRedirectDest", "ip", "10.100.201.241"),
    ("vnsRedirectDest", "ip", "2001:db8::1"),  # edge: IPv6
    # scope / ctrl / aggregate — single token and multi-token (comma) forms.
    ("fvSubnet", "scope", "public"),
    ("fvSubnet", "scope", "private,shared"),  # edge: token-list
    ("fvSubnet", "ctrl", "unspecified"),
    ("fvSubnet", "ctrl", "nd,querier"),  # edge: token-list
    ("l3extSubnet", "scope", "export-rtctrl"),
    ("l3extSubnet", "scope", "import-security,shared-rtctrl"),  # edge: token-list
    ("l3extSubnet", "aggregate", "export-rtctrl"),
    ("fvCtx", "pcEnfPref", "enforced"),
    ("fvCtx", "pcEnfPref", "unenforced"),
    ("fvCtx", "pcEnfDir", "ingress"),
    ("fvCtx", "pcEnfDir", "egress"),
    ("fvBD", "unkMacUcastAct", "proxy"),
    ("fvBD", "unkMacUcastAct", "flood"),
    ("fvBD", "multiDstPktAct", "bd-flood"),
    ("fvBD", "multiDstPktAct", "drop"),
    ("fvBD", "unkMcastAct", "flood"),
    ("fvBD", "unkMcastAct", "opt-flood"),
    ("fvBD", "type", "regular"),
    ("fvBD", "epMoveDetectMode", ""),  # edge: empty-string literal member
    ("fvBD", "epMoveDetectMode", "garp"),
    ("vzEntry", "etherT", "ip"),
    ("vzEntry", "etherT", "unspecified"),
    ("vzBrCP", "scope", "context"),
    ("vzBrCP", "scope", "application-profile"),
    ("fvRsDomAtt", "classPref", "encap"),
    ("fvRsDomAtt", "instrImedcy", "immediate"),
    ("fvRsDomAtt", "resImedcy", "lazy"),
    # resImedcy "pre-provision" — F10 F1 fix (BLOCKER): the canonical
    # corpus's ACTUAL resolution_immediacy value (34/34 in the campaign
    # that motivated this batch); the pre-fix enum {immediate, lazy} 400-
    # rejected every single EPG-domain binding.
    ("fvRsDomAtt", "resImedcy", "pre-provision"),
    ("bgpPeerP", "peerCtrl", "bfd"),
    ("bgpPeerP", "peerCtrl", "bfd,nh-self"),  # edge: token-list
    ("fvnsVlanInstP", "allocMode", "dynamic"),
    ("fvnsEncapBlk", "allocMode", "static"),
]


@pytest.mark.parametrize("cls,prop,value", ACCEPT_CASES, ids=[f"{c}.{p}={v!r}" for c, p, v in ACCEPT_CASES])
def test_canonical_value_accepted(cls, prop, value):
    validator = RULES[(cls, prop)]
    validator(cls, prop, value, {})  # must NOT raise


def test_canonical_corpus_sample_size():
    """Design doc §6 samples ~40 distinct canonical values; this suite
    covers at least that many (cls, prop, value) accept-cases."""
    assert len(ACCEPT_CASES) >= 40


# ---------------------------------------------------------------------------
# Bad values that must REJECT — the F10 probe values plus 1-2 hand-built
# out-of-range/format errors per rule family.
# ---------------------------------------------------------------------------
REJECT_CASES: list[tuple[str, str, str]] = [
    # F10 probe values (the actual empirically-confirmed gap).
    ("l3extRsPathL3OutAtt", "encap", "vlan-9999"),
    ("l3extRsNodeL3OutAtt", "rtrId", "999.888.777.241"),
    ("l3extRsPathL3OutAtt", "addr", "999.888.777.17/30"),
    ("l3extRsPathL3OutAtt", "addr", "999.888.777.25/30"),
    # encap: out of range / malformed.
    ("l3extRsPathL3OutAtt", "encap", "vlan-0"),
    ("l3extRsPathL3OutAtt", "encap", "vlan-4095"),
    ("l3extRsPathL3OutAtt", "encap", "garbage"),
    ("fvRsPathAtt", "encap", "unknown"),  # NOT allowed for this specific prop
    ("fvRsDomAtt", "encap", "vlan-9999"),
    ("fvnsEncapBlk", "from", "vlan-4095"),
    ("fvnsEncapBlk", "to", "vlan-0"),
    # vxlan-N: still must reject beyond the 24-bit VNID ceiling (F10 F2
    # regression guard — widening vlan-N's cap must not become "no cap").
    ("l3extRsPathL3OutAtt", "encap", "vxlan-99999999"),
    ("l3extRsPathL3OutAtt", "encap", "vxlan-0"),
    # rtrId: IPv6 not allowed (row says IPv4-only dotted-quad).
    ("l3extRsNodeL3OutAtt", "rtrId", "2001:db8::1"),
    ("l3extRsNodeL3OutAtt", "rtrId", "not-an-ip"),
    # bgpPeerP.
    ("bgpPeerP", "addr", "999.888.777.1"),
    ("bgpPeerP", "addr", "999.888.777.5"),  # F10 F3 regression guard: /prefix now
    # tolerated but a garbage octet must still 400 (ip_interface catches it too).
    ("bgpPeerP", "addr", "999.888.777.5/30"),  # same, with a /prefix
    ("bgpPeerP", "ttl", "0"),
    ("bgpPeerP", "ttl", "256"),
    ("bgpPeerP", "peerCtrl", "not-a-flag"),
    # bgpAsP.
    ("bgpAsP", "asn", "0"),
    ("bgpAsP", "asn", "4294967296"),
    # mtu.
    ("l3extRsPathL3OutAtt", "mtu", "99999"),
    ("l3extRsPathL3OutAtt", "mtu", "500"),
    ("fvBD", "mtu", "not-a-number"),
    # mac.
    ("fvBD", "mac", "zz:11:22:33:44:55"),
    ("fvBD", "mac", "00:11:22:33:44"),
    ("vnsRedirectDest", "mac", "not-a-mac"),
    # infraPortBlk.
    ("infraPortBlk", "fromPort", "0"),
    ("infraPortBlk", "toCard", "-1"),
    # vzEntry ports.
    ("vzEntry", "dFromPort", "70000"),
    ("vzEntry", "sToPort", "httpz"),
    # enums.
    ("fvSubnet", "scope", "publicc"),
    ("fvSubnet", "scope", "private,publicc"),
    ("fvSubnet", "ctrl", "bogus"),
    ("l3extSubnet", "scope", "bogus-scope"),
    ("l3extSubnet", "aggregate", "bogus-agg"),
    ("fvCtx", "pcEnfPref", "bogus"),
    ("fvCtx", "pcEnfDir", "bogus"),
    ("fvBD", "unkMacUcastAct", "bogus"),
    ("fvBD", "multiDstPktAct", "bogus"),
    ("fvBD", "type", "weird"),
    ("fvBD", "epMoveDetectMode", "bogus"),
    ("vzEntry", "etherT", "ipv7"),
    ("vzBrCP", "scope", "bogus"),
    ("fvRsDomAtt", "classPref", "bogus"),
    ("fvRsDomAtt", "instrImedcy", "bogus"),
    ("fvnsVlanInstP", "allocMode", "weird"),
    # ip (migrated) regression — the exact garbage from the 2026-07-07 J2
    # incident that motivated the pre-F10 hardcoded checks.
    ("fvSubnet", "ip", ".1/24"),
    ("l3extSubnet", "ip", ".1/24"),
    ("vnsRedirectDest", "ip", "."),
]


@pytest.mark.parametrize("cls,prop,value", REJECT_CASES, ids=[f"{c}.{p}={v!r}" for c, p, v in REJECT_CASES])
def test_bad_value_rejected(cls, prop, value):
    validator = RULES[(cls, prop)]
    with pytest.raises(ValueError):
        validator(cls, prop, value, {})


# ---------------------------------------------------------------------------
# Corpus enumeration lock (independent-review test-blind-spot fix, F10 F1
# post-mortem): ACCEPT_CASES above hand-picks ONE value per enum prop (e.g.
# resImedcy="lazy"), which is exactly how F1 slipped past review — "lazy" is
# legal but is NOT what the real campaign ever sends (100% "pre-provision").
# A hand-picked accept-case proves the validator accepts *a* legal value, not
# that it accepts the corpus's *actual* values. This test closes that gap
# mechanically: it scans every resolution_immediacy/deployment_immediacy
# value that the real campaign's vars (~/e2e-20260708/vars/{ms,sf,common}/)
# actually rendered into (the rendered artifacts live at
# ~/e2e-20260708/logs/ansible/*.yml — same corpus this module's docstring
# already cites) and asserts EVERY distinct value found is ACCEPTed, with no
# human hand-picking a subset. Skipped (not failed) off PI230 where this
# machine-local e2e corpus doesn't exist.
# ---------------------------------------------------------------------------

_E2E_ANSIBLE_LOGS = Path.home() / "e2e-20260708" / "logs" / "ansible"
_IMMEDIACY_RE = re.compile(r'(resolution_immediacy|deployment_immediacy):\s*"([^"]+)"')


def _enumerate_corpus_immediacy_values() -> dict[str, set[str]]:
    """Mechanically extract every distinct resolution_immediacy /
    deployment_immediacy string literal from the rendered aci-model-*.yml /
    mso-model-*.yml artifacts under ~/e2e-20260708/logs/ansible/ — these are
    exactly what results from rendering ~/e2e-20260708/vars/{ms,sf,common}/
    through the campaign's pipeline. Values are read off disk, not typed in
    by hand, so a future enum with a real-corpus value this suite doesn't
    already know about will surface here instead of staying invisible."""
    found: dict[str, set[str]] = {"resolution_immediacy": set(), "deployment_immediacy": set()}
    for path in sorted(_E2E_ANSIBLE_LOGS.glob("*.yml")):
        text = path.read_text()
        for key, value in _IMMEDIACY_RE.findall(text):
            found[key].add(value)
    return found


@pytest.mark.skipif(
    not _E2E_ANSIBLE_LOGS.is_dir(),
    reason="~/e2e-20260708/logs/ansible corpus not present on this machine",
)
def test_corpus_resolution_and_deployment_immediacy_values_all_accepted():
    found = _enumerate_corpus_immediacy_values()
    assert found["resolution_immediacy"], (
        "corpus scan found zero resolution_immediacy values — the glob/regex "
        "is broken, this is not a real pass"
    )
    res_imedcy = RULES[("fvRsDomAtt", "resImedcy")]
    for value in sorted(found["resolution_immediacy"]):
        res_imedcy("fvRsDomAtt", "resImedcy", value, {})  # must NOT raise

    assert found["deployment_immediacy"], (
        "corpus scan found zero deployment_immediacy values — the glob/regex "
        "is broken, this is not a real pass"
    )
    instr_imedcy = RULES[("fvRsDomAtt", "instrImedcy")]
    for value in sorted(found["deployment_immediacy"]):
        instr_imedcy("fvRsDomAtt", "instrImedcy", value, {})  # must NOT raise


def test_corpus_resolution_immediacy_is_actually_pre_provision():
    """Pins the specific fact that motivated the F1 fix (not just "some
    value accepts") — if this ever flips, F1's own justification is stale
    and needs re-checking, not silently trusted."""
    found = _enumerate_corpus_immediacy_values()
    if not found["resolution_immediacy"]:
        pytest.skip("~/e2e-20260708/logs/ansible corpus not present on this machine")
    assert found["resolution_immediacy"] == {"pre-provision"}


# ---------------------------------------------------------------------------
# Fail-safe / default-allow contract (design §3.6).
# ---------------------------------------------------------------------------


def test_unregistered_class_prop_pair_has_no_validator():
    assert ("fvTenant", "name") not in RULES
    assert ("fvAp", "name") not in RULES
    assert ("bogusClass", "bogusProp") not in RULES


def test_rules_table_size_matches_design_scope():
    """Sanity lock on the registry shape: P1+P2 (+3 migrated ip rules) should
    land in the low-to-mid 40s of (class, prop) entries (16+17 source-table
    rows; several rows cover 2+ props, e.g. infraPortBlk's 4 port/card
    fields, so the (class, prop) key count is naturally higher than the 31
    "core rules" row-count figure)."""
    assert 35 <= len(RULES) <= 55


# ---------------------------------------------------------------------------
# Direct factory-level tests (not routed through RULES) — pin the exact
# behavior of the composable helpers themselves.
# ---------------------------------------------------------------------------


def test_rng_no_upper_bound():
    v = rng(1, None)
    v("infraPortBlk", "fromPort", "1", {})
    v("infraPortBlk", "fromPort", "999999", {})  # no ceiling specified
    with pytest.raises(ValueError):
        v("infraPortBlk", "fromPort", "0", {})


def test_rng_rejects_non_integer():
    v = rng(1, 10)
    with pytest.raises(ValueError):
        v("x", "y", "not-a-number", {})


def test_one_of_plain_membership():
    v = one_of({"a", "b"})
    v("x", "y", "a", {})
    with pytest.raises(ValueError):
        v("x", "y", "c", {})


def test_one_of_token_list_empty_value_is_vacuously_allowed():
    """An empty string with list_sep set has no tokens to check — the
    fail-safe bias (design §3.6) treats 'nothing to validate' as pass rather
    than risk a false-reject on an omitted/unset multi-valued enum."""
    v = one_of({"a", "b"}, list_sep=",")
    v("x", "y", "", {})  # must not raise


def test_fmt_unknown_kind_raises_at_construction():
    with pytest.raises(ValueError):
        fmt("not-a-real-kind")


def test_mtu_helper_directly():
    m = mtu()
    m("fvBD", "mtu", "inherit", {})
    m("fvBD", "mtu", "9216", {})
    with pytest.raises(ValueError):
        m("fvBD", "mtu", "9217", {})
    with pytest.raises(ValueError):
        m("fvBD", "mtu", "not-inherit-and-not-a-number", {})


def test_vlan_encap_unknown_only_where_flagged():
    strict = fmt("vlan_encap")
    lenient = fmt("vlan_encap", allow_unknown=True)
    with pytest.raises(ValueError):
        strict("x", "y", "unknown", {})
    lenient("x", "y", "unknown", {})  # must not raise
