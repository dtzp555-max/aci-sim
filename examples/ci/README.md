# CI gate example — fail a PR before it reaches real hardware

Pattern: run your network automation (Ansible / Terraform / Python / aci-py)
against a running `aci-sim`, then **assert the MIT reached the expected
end state**. A failed assertion fails the build. No hardware, deterministic,
resettable.

## Files

- **`assert_mit.py`** — the gate. Logs into the sim (or a real APIC) and checks
  that the objects your change was supposed to create/modify are present.
  Exit code = number of failed checks, so CI can gate on it.
- **`github-actions-aci-gate.yml`** — a copy-pasteable GitHub Actions workflow
  wiring the three steps (start sim → run automation → assert).

## `assert_mit.py` check syntax

Each `--expect` (repeatable):

| Check | Meaning |
|---|---|
| `class:<cls>[>=N]` | the class query returns ≥ N objects (default N=1) |
| `mo:<dn>` | `GET /api/mo/<dn>.json` returns the object (exists) |
| `absent:<dn>` | the MO does NOT exist (e.g. after a `state=absent` / delete) |
| `attr:<dn>:<key>=<val>` | the MO at `<dn>` has attribute `<key>` == `<val>` |

## Try it locally

```bash
# 1. start the sim (ships MS-TN1 / SF-TN1-* tenants from topology.yaml)
aci-sim run &

# 2. assert the seeded end state
python examples/ci/assert_mit.py --host 127.0.0.1:8443 \
    --expect "class:fvTenant>=1" \
    --expect "mo:uni/tn-MS-TN1" \
    --expect "attr:uni/tn-MS-TN1:name=MS-TN1" \
    --expect "absent:uni/tn-DoesNotExist"
# → 4/4 checks passed, exit 0
```

Because `assert_mit.py` speaks the plain APIC REST API, you can point it at a
**real APIC** too (`--host <apic-ip> --user ... --password ...`) to diff
sim-vs-real, or to validate the same expectations against staging gear.

## Real-world shape

In practice step 2 is your existing playbook (see `../ansible/smoke.yml` for a
full cisco.aci create→idempotency example), and step 3's `--expect` list is the
objects that playbook is contracted to produce. When someone edits the playbook
in a way that stops creating those objects, the PR goes red — before anything
touches production.
