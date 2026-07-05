# Contributing to aci-sim

Thanks for considering a contribution. This project is a REST simulator, not
a production service, so the bar is: does it faithfully match real APIC/NDO
behavior, and does the test suite stay green.

## Dev setup

```bash
git clone https://github.com/dtzp555-max/aci-sim.git
cd aci-sim
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e '.[dev]'          # installs aci_sim + fastapi/uvicorn/pydantic/pyyaml + httpx/pytest
```

## Running the tests

```bash
.venv/bin/python -m pytest -q
```

The suite currently has **900 tests** and must stay green. If you add a
feature or fix a bug, add or extend tests under `tests/` in the same PR —
most existing tests are organized by PR/feature (`tests/test_pr*.py`,
`tests/test_<feature>.py`); follow that convention for new ones.

`tests/verify_autoaci.py` (run via `scripts/verify.sh`) is a separate
verification harness that replays real ACI/NDO query patterns end-to-end
against a *running* sim instance — it's not part of the plain `pytest`
collection and isn't required for a normal PR, but it's worth running if
your change touches the query engine or the write path.

## Linting (informational only)

There's a `ruff` config in `pyproject.toml` (`[tool.ruff]`), and CI runs
`ruff check .` — but as a **non-blocking** job (see
`.github/workflows/ci.yml`'s `lint` job). There's a pre-existing baseline of
lint findings in the repo today; you don't need to fix unrelated ones in
your PR, but please don't add new ones in the code you touch.

```bash
.venv/bin/ruff check .
```

## `topology.yaml` drives everything

`topology.yaml` is the single source of truth for the whole simulated
fabric — sites, nodes, tenants, VRFs/BDs/EPGs, contracts, L3Outs, ISN,
seeded faults, and access-policy defaults. Every plane (both sites' APIC
stores and the NDO model) is derived from the *same* `Topology` object at
build time (`aci_sim/build/orchestrator.py`, `aci_sim/ndo/model.py`), which
is what keeps tenant/VRF/BD names and IDs consistent between a site's APIC
and NDO's view of it. If you're adding a new object or knob:

1. Add the field to `aci_sim/topology/schema.py` (Pydantic v2) — optional,
   with a default that reproduces the pre-existing hardcoded behavior, so
   existing `topology.yaml` files keep validating and building
   byte-identical output.
2. Wire it into the relevant builder(s) under `aci_sim/build/`.
3. If it should show up in NDO's view too, wire it into `aci_sim/ndo/model.py`.
4. Run `aci-sim validate` against the repo's own `topology.yaml` and against
   `aci-sim new`-generated topologies to confirm nothing regressed.

See `docs/DESIGN.md` for the module map and the design rationale behind
past additions, and `docs/CONTRACT.md` for the exact REST behavior contract
(error envelopes, query semantics, the object catalog) new code needs to
match.

## What PRs should (and shouldn't) do

- Keep `pytest -q` green — this is the actual acceptance bar, not a
  suggestion.
- Prefer additive, backward-compatible changes to `topology.yaml`'s schema
  (new optional fields with safe defaults) over changes that would break an
  existing topology file.
- Match the existing code style — small, single-purpose builder functions,
  docstrings that explain *why* (not just what), and tests that assert on
  the exact REST response shape, not just "it didn't crash."
- **Do not reintroduce any real/customer data** — hostnames, IPs, tenant or
  site names, credentials, or anything else identifying a real deployment.
  This repo only uses generic placeholders (`LAB0`/`LAB1`/`LAB2`,
  `App1`-`App5`, `MS-TN1`/`SF-TN1`, `10.192.x`/`10.10.x`, and similar) —
  keep new examples, tests, and docs consistent with that convention.

## Filing issues

Bug reports are most useful with: the exact request (method + path + body)
that produced the wrong behavior, what you expected (ideally referencing
real APIC/NDO documented behavior), and what the sim actually returned.
