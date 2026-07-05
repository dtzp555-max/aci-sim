# aci-sim — project notes for Claude

A faithful REST simulator of a 2-site Cisco ACI fabric (per-site APIC + one
NDO/MSO plane), driven entirely by `topology.yaml`. Python 3.11+, FastAPI +
uvicorn + Pydantic v2, in-memory MIT object store, no external dependencies.
Full architecture/contract detail: `README.md`, `docs/CONTRACT.md` (the
authoritative ACI/NDO REST behavior contract), `docs/DESIGN.md` (module map +
design decisions + phase-by-phase build history).

Two run modes: **port mode** (`scripts/run.sh`, shared bind + distinct ports
8443/8444/8445) and **sandbox mode** (`scripts/sandbox-up.sh`, one loopback IP
per device on `:443`, needed for NDO auto-discovery clients that hardcode
port 443). Primary public use case: running Ansible `cisco.aci` playbooks
against it directly — see `examples/ansible/`.

Test suite: `.venv/bin/python -m pytest -q` (900 tests, must stay green).
Lint: `ruff check .` (non-blocking in CI — pre-existing baseline, see
`.github/workflows/ci.yml`).

Version lives in `pyproject.toml`; changes are logged in `CHANGELOG.md`
(newest entry at the top, `Keep a Changelog` format). The ACI/NDO object
catalog is documented in `docs/CONTRACT.md` §6; environment variables in
`README.md` §6.
