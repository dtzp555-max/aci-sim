#!/usr/bin/env bash
# Equivalent to `aci-sim run` (see README.md §5 CLI) after `pip install -e .`;
# this script remains the dependency-free entrypoint and keeps working
# unchanged.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -f certs/sim.crt ] || [ ! -f certs/sim.key ]; then
  bash scripts/gen_certs.sh
fi
python -m aci_sim.runtime.supervisor
