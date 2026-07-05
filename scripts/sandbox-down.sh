#!/usr/bin/env bash
# Tear down the sandbox virtual network: stop the sim and remove the loopback
# aliases (macOS: lo0 alias; Linux: ip addr del ... dev lo).
#
#   sudo bash scripts/sandbox-down.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."
PY="./.venv/bin/python"
OS="$(uname -s)"
IPS=$(PYTHONPATH="$PWD" "$PY" -c "from aci_sim.topology.loader import load_topology; t=load_topology('topology.yaml'); print(' '.join([t.fabric.ndo_mgmt_ip]+[s.mgmt_ip for s in t.sites if s.mgmt_ip]))")

if [ "$(id -u)" -ne 0 ]; then
  echo "Needs root: sudo bash scripts/sandbox-down.sh" >&2
  exit 1
fi

# Verify a recorded pid really is our supervisor process before killing it —
# a stale /tmp pid file could otherwise reference an unrelated process that
# has since reused that pid (#24).
pid_is_supervisor() {
  local pid="$1"
  if [ "$OS" = "Linux" ]; then
    [ -r "/proc/$pid/cmdline" ] || return 1
    tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q "aci_sim.runtime.supervisor"
  else
    ps -p "$pid" -o command= 2>/dev/null | grep -q "aci_sim.runtime.supervisor"
  fi
}

if [ -f /tmp/aci-sim-sandbox.pid ]; then
  old_pid="$(cat /tmp/aci-sim-sandbox.pid)"
  if pid_is_supervisor "$old_pid"; then
    kill "$old_pid" 2>/dev/null || true
  else
    echo "[sandbox] WARNING: /tmp/aci-sim-sandbox.pid ($old_pid) is not an" >&2
    echo "[sandbox]          aci_sim.runtime.supervisor process — refusing to kill it." >&2
  fi
fi
pkill -f "aci_sim.runtime.supervisor" 2>/dev/null || true
# Wait for the supervisor to actually exit before dropping aliases, so an
# immediately-following `up` sees a clean (process-gone, :443-free) state.
for i in $(seq 1 16); do          # up to ~8s
  pgrep -f "aci_sim.runtime.supervisor" >/dev/null 2>&1 || break
  sleep 0.5
done
rm -f /tmp/aci-sim-sandbox.pid
for ip in $IPS; do
  if [ "$OS" = "Linux" ]; then
    ip addr del "${ip}/32" dev lo 2>/dev/null || true
  else
    ifconfig lo0 -alias "$ip" 2>/dev/null || true
  fi
  echo "[sandbox] removed alias $ip"
done
echo "[sandbox] down."
