#!/usr/bin/env bash
# Save or restore the WHOLE fabric's state (every APIC site + NDO) in one
# command, by hitting each plane's /_sim/{save,load}/<name> endpoint.
#
#   scripts/sim-state.sh save <name>       # snapshot every plane to disk
#   scripts/sim-state.sh restore <name>     # restore every plane from disk
#
# Files land under SIM_STATE_DIR (default ~/.aci-sim/state) — see
# aci_sim/control/persist.py::state_dir(). Restarting the sim wipes its
# in-memory state (sandbox/port mode has no other durability), so the
# intended workflow is:
#
#   scripts/sim-state.sh save mybaseline
#   <restart the sim: sandbox-down.sh && sandbox-up.sh, or Ctrl-C + aci-sim run>
#   scripts/sim-state.sh restore mybaseline
#
# Addressing — this script must reach ALL planes, so it derives the same
# per-device IPs scripts/sandbox-up.sh uses (straight from topology.yaml, via
# the identical `load_topology` one-liner) and falls back to port mode if
# those IPs aren't reachable:
#
#   sandbox mode (real per-device IPs, :443) — from topology.yaml:
#     NDO       -> https://<fabric.ndo_mgmt_ip>:443
#     APIC site -> https://<site.mgmt_ip>:443           (one per site)
#
#   port mode (fallback, 127.0.0.1 + distinct ports — see runtime/config.py):
#     APIC LAB1 -> https://127.0.0.1:8443   (APIC_A_PORT)
#     APIC LAB2 -> https://127.0.0.1:8444   (APIC_B_PORT)
#     NDO       -> https://127.0.0.1:8445   (NDO_PORT)
#
# `restore` maps to each plane's POST /_sim/load/<name> endpoint (the sim's
# route is named "load"; this script's user-facing verb is "restore" to match
# the existing snapshot/restore vocabulary in /_sim).
set -euo pipefail
cd "$(dirname "$0")/.."
PY="./.venv/bin/python"

usage() {
  echo "usage: $0 {save|restore} <name>" >&2
  exit 1
}

[ $# -eq 2 ] || usage
ACTION="$1"
NAME="$2"

case "$ACTION" in
  save)    SIM_ACTION="save" ;;
  restore) SIM_ACTION="load" ;;
  *) usage ;;
esac

# Derive sandbox IPs the exact same way scripts/sandbox-up.sh does (single
# source of truth: topology.yaml). Prints "<ndo_ip> <site_ip> [<site_ip> ...]"
# or nothing if topology.yaml can't be loaded (e.g. no venv yet).
IPS=$(PYTHONPATH="$PWD" "$PY" -c "from aci_sim.topology.loader import load_topology; t=load_topology('topology.yaml'); print(' '.join([t.fabric.ndo_mgmt_ip]+[s.mgmt_ip for s in t.sites if s.mgmt_ip]))" 2>/dev/null || true)

NDO_IP=""
SITE_IPS=()
if [ -n "$IPS" ]; then
  read -r -a _all <<<"$IPS"
  NDO_IP="${_all[0]}"
  SITE_IPS=("${_all[@]:1}")
fi

# Reachability probe: does curl get ANY response (even 4xx) from this host:443?
# 000 means nothing is listening / unreachable.
reachable() {
  local ip="$1"
  local code
  # curl's -w '%{http_code}' already prints "000" on connect failure/timeout,
  # so don't ALSO append a fallback echo on nonzero exit — that would double
  # up the output (e.g. "000000") and always compare unequal to "000".
  code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 2 "https://${ip}:443/" 2>/dev/null)
  [ "$code" = "200" ] || [ "$code" = "401" ] || [ "$code" = "404" ]
}

USE_SANDBOX=0
if [ -n "$NDO_IP" ] && reachable "$NDO_IP"; then
  USE_SANDBOX=1
fi

hit() {
  local label="$1" host_port="$2"
  local resp
  resp=$(curl -sk -X POST "https://${host_port}/_sim/${SIM_ACTION}/${NAME}" || echo '{"status":"error","detail":"curl failed"}')
  echo "[$label] $resp"
}

if [ "$USE_SANDBOX" -eq 1 ]; then
  echo "[sim-state] sandbox mode — using topology.yaml mgmt IPs" >&2
  hit "NDO" "${NDO_IP}:443"
  i=0
  for ip in "${SITE_IPS[@]}"; do
    i=$((i + 1))
    hit "APIC-site${i}" "${ip}:443"
  done
else
  echo "[sim-state] port mode fallback — 127.0.0.1:8443/8444/8445" >&2
  APIC_A_PORT="${APIC_A_PORT:-8443}"
  APIC_B_PORT="${APIC_B_PORT:-8444}"
  NDO_PORT="${NDO_PORT:-8445}"
  hit "APIC-LAB1" "127.0.0.1:${APIC_A_PORT}"
  hit "APIC-LAB2" "127.0.0.1:${APIC_B_PORT}"
  hit "NDO" "127.0.0.1:${NDO_PORT}"
fi
