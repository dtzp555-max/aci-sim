#!/usr/bin/env bash
# verify.sh — run the aci-sim verification harness
# Usage:  scripts/verify.sh [pytest-options]
# Example: scripts/verify.sh -v --tb=short
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m pytest tests/verify_autoaci.py -v "$@"
