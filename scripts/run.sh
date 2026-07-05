#!/usr/bin/env bash
# Equivalent to `aci-sim run` (see README.md §5 CLI) after `pip install -e .`;
# this script remains the dependency-free entrypoint and keeps working
# unchanged.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="./.venv/bin/python"
[ -x "$PY" ] || PY="python3"
if [ ! -f certs/sim.crt ] || [ ! -f certs/sim.key ]; then
  bash scripts/gen_certs.sh
fi
"$PY" -m aci_sim.runtime.supervisor
