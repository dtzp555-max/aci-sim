"""Parse APIC filter expressions into a callable predicate ``fn(MO) -> bool``.

Supported grammar (CONTRACT §4):

    eq(<class>.<attr>, "<val>")           equal
    ne(<class>.<attr>, "<val>")           not equal
    wcard(<class>.<attr>, "<substr>")     substring containment
    gt|lt|ge|le(<class>.<attr>, "<val>")  numeric compare (string fallback)
    bw(<class>.<attr>, "<lo>", "<hi>")    inclusive range
    and(<expr>, <expr>, ...)
    or(<expr>, <expr>, ...)

Rules:
- Nesting is allowed: ``and(or(...), eq(...))``
- The ``<class>.`` prefix is advisory; we use the attr name after the last dot.
- ``wcard`` performs a substring containment check (not a glob).
- ``gt/lt/ge/le/bw`` compare numerically when both sides parse as numbers,
  else fall back to string comparison.
- An unknown attr evaluates to False for eq/wcard/compares (attr not present).
- An empty / None expression is treated as "match everything".
- A malformed expression raises :class:`FilterParseError` (the REST layer maps
  it to an APIC 400 error envelope — never a 500).
"""

from __future__ import annotations
from typing import Callable

from aci_sim.mit.mo import MO


Predicate = Callable[[MO], bool]


class FilterParseError(ValueError):
    """Raised when a ``query-target-filter`` expression cannot be parsed.

    The REST layer catches this and returns an APIC error envelope with HTTP
    400, matching real APIC (which rejects a malformed filter with a 400 +
    imdata error) rather than surfacing a 500.
    """

# ---------------------------------------------------------------------------
# Tokenizer (quote- and bracket-aware)
# ---------------------------------------------------------------------------


def _tokenize(s: str) -> list[str]:
    """Produce a flat list of tokens from a filter expression string.

    Token types:
    - Identifiers / dotted names: ``eq``, ``wcard``, ``and``, ``or``,
      ``fabricNode.role``, …
    - Quoted strings (double-quote delimited, returned WITH the quotes).
    - Punctuation: ``(``, ``)`, ``,``.
    Whitespace is skipped.
    """
    tokens: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch in " \t\r\n":
            i += 1
        elif ch == '"':
            j = i + 1
            while j < n and s[j] != '"':
                j += 1
            tokens.append(s[i: j + 1])
            i = j + 1
        elif ch in "(,)":
            tokens.append(ch)
            i += 1
        else:
            # Identifier / dotted name — stop at delimiter chars
            j = i
            while j < n and s[j] not in '(,) \t\r\n"':
                j += 1
            tokens.append(s[i:j])
            i = j
    return tokens


# ---------------------------------------------------------------------------
# Recursive-descent parser
# ---------------------------------------------------------------------------


def _expect(tokens: list[str], pos: int, want: str) -> int:
    """Assert ``tokens[pos] == want`` and return ``pos + 1`` (else raise)."""
    if pos >= len(tokens) or tokens[pos] != want:
        got = tokens[pos] if pos < len(tokens) else "<end-of-expression>"
        raise FilterParseError(f"expected {want!r} but got {got!r}")
    return pos + 1


# Operators taking (class.attr, value): eq/ne substring-agnostic + comparisons.
_BINARY_OPS = ("eq", "ne", "wcard", "gt", "lt", "ge", "le")


def _parse_expr(tokens: list[str], pos: int) -> tuple[Predicate, int]:
    """Parse one expression starting at *tokens[pos]*.

    Returns ``(predicate, new_pos)``. Raises :class:`FilterParseError` on any
    structural problem.
    """
    if pos >= len(tokens):
        raise FilterParseError("unexpected end of filter expression")
    tok = tokens[pos]

    if tok in ("and", "or"):
        pos = _expect(tokens, pos + 1, "(")
        parts: list[Predicate] = []
        while pos < len(tokens) and tokens[pos] != ")":
            if tokens[pos] == ",":
                pos += 1
                continue
            pred, pos = _parse_expr(tokens, pos)
            parts.append(pred)
        pos = _expect(tokens, pos, ")")
        if not parts:
            raise FilterParseError(f"{tok}() requires at least one argument")
        combiner = _and_pred if tok == "and" else _or_pred
        return combiner(parts), pos

    if tok in _BINARY_OPS:
        # <op>(<class>.<attr>, "<val>")
        pos = _expect(tokens, pos + 1, "(")
        dotted, pos = _take(tokens, pos)
        pos = _expect(tokens, pos, ",")
        val_token, pos = _take(tokens, pos)
        pos = _expect(tokens, pos, ")")
        return _make_binary_pred(tok, dotted.split(".")[-1], val_token.strip('"')), pos

    if tok == "bw":
        # bw(<class>.<attr>, "<low>", "<high>")
        pos = _expect(tokens, pos + 1, "(")
        dotted, pos = _take(tokens, pos)
        pos = _expect(tokens, pos, ",")
        low_token, pos = _take(tokens, pos)
        pos = _expect(tokens, pos, ",")
        high_token, pos = _take(tokens, pos)
        pos = _expect(tokens, pos, ")")
        return _bw_pred(dotted.split(".")[-1], low_token.strip('"'), high_token.strip('"')), pos

    raise FilterParseError(f"unknown filter operator {tok!r} at position {pos}")


def _take(tokens: list[str], pos: int) -> tuple[str, int]:
    """Return ``(tokens[pos], pos + 1)`` or raise on end-of-input."""
    if pos >= len(tokens):
        raise FilterParseError("unexpected end of filter expression")
    return tokens[pos], pos + 1


# ---------------------------------------------------------------------------
# Predicate factories
# ---------------------------------------------------------------------------


def _eq_pred(attr: str, val: str) -> Predicate:
    def pred(mo: MO) -> bool:
        return mo.attrs.get(attr) == val
    return pred


def _wcard_pred(attr: str, substr: str) -> Predicate:
    def pred(mo: MO) -> bool:
        return substr in mo.attrs.get(attr, "")
    return pred


def _and_pred(preds: list[Predicate]) -> Predicate:
    def pred(mo: MO) -> bool:
        return all(p(mo) for p in preds)
    return pred


def _or_pred(preds: list[Predicate]) -> Predicate:
    def pred(mo: MO) -> bool:
        return any(p(mo) for p in preds)
    return pred


def _ne_pred(attr: str, val: str) -> Predicate:
    # ne = ¬eq: an object lacking the attr does not equal *val*, so it matches.
    def pred(mo: MO) -> bool:
        return mo.attrs.get(attr) != val
    return pred


def _to_number(s: object) -> float | None:
    try:
        return float(s)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


_CMP_OPS: dict[str, Callable[[object, object], bool]] = {
    "gt": lambda a, b: a > b,
    "lt": lambda a, b: a < b,
    "ge": lambda a, b: a >= b,
    "le": lambda a, b: a <= b,
}


def _cmp_pred(op: str, attr: str, val: str) -> Predicate:
    fn = _CMP_OPS[op]

    def pred(mo: MO) -> bool:
        raw = mo.attrs.get(attr)
        if raw is None:
            return False
        rn, vn = _to_number(raw), _to_number(val)
        if rn is not None and vn is not None:
            return fn(rn, vn)          # numeric compare when both parse
        return fn(str(raw), val)       # else lexical
    return pred


def _bw_pred(attr: str, low: str, high: str) -> Predicate:
    def pred(mo: MO) -> bool:
        raw = mo.attrs.get(attr)
        if raw is None:
            return False
        rn, ln, hn = _to_number(raw), _to_number(low), _to_number(high)
        if None not in (rn, ln, hn):
            return ln <= rn <= hn      # type: ignore[operator]
        return str(low) <= str(raw) <= str(high)
    return pred


def _make_binary_pred(op: str, attr: str, val: str) -> Predicate:
    if op == "eq":
        return _eq_pred(attr, val)
    if op == "ne":
        return _ne_pred(attr, val)
    if op == "wcard":
        return _wcard_pred(attr, val)
    return _cmp_pred(op, attr, val)    # gt/lt/ge/le


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_filter(expr: str | None) -> Predicate:
    """Parse *expr* into a callable predicate.

    Returns a predicate that always returns ``True`` for empty/``None`` input.
    """
    if not expr:
        return lambda mo: True
    try:
        tokens = _tokenize(expr)
        pred, pos = _parse_expr(tokens, 0)
        if pos != len(tokens):
            trailing = tokens[pos]
            raise FilterParseError(
                f"unexpected trailing token {trailing!r} after complete expression "
                f"in filter {expr!r}"
            )
    except FilterParseError:
        raise
    except (IndexError, ValueError) as exc:  # defensive: any tokenizer/parse slip
        raise FilterParseError(f"invalid filter expression {expr!r}: {exc}") from exc
    return pred
