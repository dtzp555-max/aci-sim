#!/usr/bin/env bash
# Sandbox virtual network — give each ACI controller a real IP on :443 via a
# loopback alias (macOS: lo0 alias; Linux: ip addr add ... dev lo), so autoACI
# connects to it by IP with NO port, exactly like real gear. autoACI is NOT
# modified in any way. Requires root (loopback alias + privileged :443).
#
#   sudo bash scripts/sandbox-up.sh
#
# Safe to re-run: it stops any previous sim (verifying the recorded pid is
# actually a supervisor process before killing it — #24), waits for :443 to
# actually free up on each sandbox IP (avoids the bind race that silently
# killed old restarts), then verifies the servers really came up — failing
# loudly with the log if not.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="./.venv/bin/python"

OS="$(uname -s)"

# Sandbox IPs come straight from the topology (single source of truth).
# TOPOLOGY_PATH, same variable the runtime reads — the two must agree, or
# the sim serves one fabric while these scripts bind another's addresses.
IPS=$(PYTHONPATH="$PWD" "$PY" -c "import os; from aci_sim.topology.loader import load_topology; t=load_topology(os.environ.get('TOPOLOGY_PATH', 'topology.yaml')); print(' '.join([t.fabric.ndo_mgmt_ip]+[s.mgmt_ip for s in t.sites if s.mgmt_ip]))")

if [ "$(id -u)" -ne 0 ]; then
  echo "Needs root (loopback alias + bind :443). Re-run:  sudo bash scripts/sandbox-up.sh" >&2
  exit 1
fi

# Return 0 if (ip, 443) is bindable RIGHT NOW — same options uvicorn will use
# (SO_REUSEADDR), so this gate matches exactly what the supervisor is about to do.
port_free() {
  "$PY" -c 'import socket,sys
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
try:
    s.bind((sys.argv[1],443)); s.close()
except OSError:
    sys.exit(1)' "$1"
}

# Verify a recorded pid really is our supervisor process before we kill it —
# a stale /tmp pid file could otherwise reference an unrelated (possibly
# security-sensitive) process that has since reused that pid (#24).
pid_is_supervisor() {
  local pid="$1"
  if [ "$OS" = "Linux" ]; then
    [ -r "/proc/$pid/cmdline" ] || return 1
    tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q "aci_sim.runtime.supervisor"
  else
    ps -p "$pid" -o command= 2>/dev/null | grep -q "aci_sim.runtime.supervisor"
  fi
}

echo "[sandbox] loopback aliases ($OS): $IPS"
for ip in $IPS; do
  if [ "$OS" = "Linux" ]; then
    ip addr add "${ip}/32" dev lo 2>/dev/null || true
  else
    ifconfig lo0 alias "$ip" 255.255.255.255 up 2>/dev/null || true
  fi
done

[ -f certs/sim.crt ] || bash scripts/gen_certs.sh

# Stop any previous sim, then WAIT for it to die and for :443 to actually free up
# on every sandbox IP before starting — starting while the old listener is still
# holding the socket is what made uvicorn sys.exit(1) silently.
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
for i in $(seq 1 40); do          # up to ~20s
  pgrep -f "aci_sim.runtime.supervisor" >/dev/null 2>&1 && { sleep 0.5; continue; }
  free=1
  for ip in $IPS; do port_free "$ip" || { free=0; break; }; done
  [ "$free" = 1 ] && break
  sleep 0.5
done

LOG=/tmp/aci-sim-sandbox.log
export SIM_SANDBOX=1 PYTHONPATH="$PWD"
nohup "$PY" -m aci_sim.runtime.supervisor >"$LOG" 2>&1 &
PID=$!
echo "$PID" >/tmp/aci-sim-sandbox.pid
ndo_ip=${IPS%% *}

# Verify the servers actually came up on :443 (loud, not silent). Any HTTP code
# (200/401/404) means the server answered; 000 means nothing is listening yet.
up=0
for i in $(seq 1 24); do          # up to ~12s
  kill -0 "$PID" 2>/dev/null || break   # process already died → stop, dump log below
  all=1
  for ip in $IPS; do
    code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 2 "https://${ip}:443/" 2>/dev/null || echo 000)
    [ "$code" = 000 ] && { all=0; break; }
  done
  [ "$all" = 1 ] && { up=1; break; }
  sleep 0.5
done

if [ "$up" -ne 1 ]; then
  echo "[sandbox] ERROR: sim did not come up on :443 for all of: $IPS" >&2
  echo "----- last 25 log lines ($LOG) -----------------------------------" >&2
  tail -n 25 "$LOG" >&2 || true
  echo "------------------------------------------------------------------" >&2
  exit 1
fi

echo "[sandbox] up — aliases: $IPS"
echo "[sandbox] sim PID $PID serving :443  (log: $LOG)"
echo "[sandbox] autoACI → log in to NDO at  $ndo_ip  → Discover → Connect All Sites"
echo "[sandbox] stop with:  sudo bash scripts/sandbox-down.sh"
