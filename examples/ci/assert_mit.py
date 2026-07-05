#!/usr/bin/env python3
"""assert_mit.py — a tiny CI gate: assert the simulator's MIT reached an
expected end state after your automation ran.

This is the "gate" half of the CI pattern: run your playbook / Terraform /
Python against a running aci-sim, then run this to fail the build if
the objects your change was supposed to create/modify are not present.

It talks the plain APIC REST API (aaaLogin + class/mo queries), so it works
against a real APIC too — point it at real gear to diff sim-vs-real.

Usage
-----
    python assert_mit.py --host 127.0.0.1:8443 --user admin --password cisco \
        --expect "class:fvTenant>=1" \
        --expect "mo:uni/tn-MS-TN1" \
        --expect "attr:uni/tn-MS-TN1:name=MS-TN1"

Check syntax (each --expect, repeatable):
    class:<cls>[>=N]        the class query returns at least N objects (default N=1)
    mo:<dn>                 GET /api/mo/<dn>.json returns the object (exists)
    absent:<dn>             GET /api/mo/<dn>.json returns empty (does NOT exist)
    attr:<dn>:<key>=<val>   the MO at <dn> has attribute <key> equal to <val>

Exit code = number of failed checks (0 = all passed), so CI can gate on it.
"""
from __future__ import annotations

import argparse
import sys

import httpx


def _login(client: httpx.Client, user: str, password: str) -> None:
    r = client.post(
        "/api/aaaLogin.json",
        json={"aaaUser": {"attributes": {"name": user, "pwd": password}}},
    )
    if r.status_code != 200:
        print(f"  FATAL  aaaLogin failed: HTTP {r.status_code} {r.text[:120]}")
        sys.exit(99)
    tok = r.json()["imdata"][0]["aaaLogin"]["attributes"]["token"]
    client.cookies.set("APIC-cookie", tok)


def _mo(client: httpx.Client, dn: str) -> dict | None:
    r = client.get(f"/api/mo/{dn}.json")
    if r.status_code == 200 and int(r.json().get("totalCount", 0)) > 0:
        return list(r.json()["imdata"][0].values())[0]["attributes"]
    return None


def _check(client: httpx.Client, expect: str) -> bool:
    """Run one --expect check; return True on pass, print the result."""
    if expect.startswith("class:"):
        body = expect[len("class:"):]
        minimum = 1
        cls = body
        if ">=" in body:
            cls, n = body.split(">=", 1)
            minimum = int(n)
        r = client.get(f"/api/class/{cls}.json")
        total = int(r.json().get("totalCount", 0)) if r.status_code == 200 else -1
        ok = total >= minimum
        print(f"  {'PASS' if ok else 'FAIL'}  class {cls} count={total} (need >={minimum})")
        return ok
    if expect.startswith("mo:"):
        dn = expect[len("mo:"):]
        attrs = _mo(client, dn)
        ok = attrs is not None
        print(f"  {'PASS' if ok else 'FAIL'}  mo {dn} {'exists' if ok else 'MISSING'}")
        return ok
    if expect.startswith("absent:"):
        dn = expect[len("absent:"):]
        attrs = _mo(client, dn)
        ok = attrs is None
        print(f"  {'PASS' if ok else 'FAIL'}  absent {dn} {'gone' if ok else 'STILL PRESENT'}")
        return ok
    if expect.startswith("attr:"):
        rest = expect[len("attr:"):]
        dn, kv = rest.rsplit(":", 1)
        key, val = kv.split("=", 1)
        attrs = _mo(client, dn) or {}
        got = attrs.get(key)
        ok = got == val
        print(f"  {'PASS' if ok else 'FAIL'}  attr {dn} {key}={got!r} (want {val!r})")
        return ok
    print(f"  FAIL  unrecognized check: {expect!r}")
    return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Assert the aci-sim MIT end state (CI gate).")
    p.add_argument("--host", required=True, help="APIC host:port, e.g. 127.0.0.1:8443")
    p.add_argument("--user", default="admin")
    p.add_argument("--password", default="cisco")
    p.add_argument("--scheme", default="https", choices=["https", "http"])
    p.add_argument("--expect", action="append", default=[], help="a check (repeatable) — see module docstring")
    a = p.parse_args(argv)

    if not a.expect:
        p.error("at least one --expect is required")

    base = f"{a.scheme}://{a.host}"
    with httpx.Client(base_url=base, verify=False, timeout=30.0) as client:
        _login(client, a.user, a.password)
        failed = sum(0 if _check(client, e) else 1 for e in a.expect)

    print(f"\n=== {len(a.expect) - failed}/{len(a.expect)} checks passed ===")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
