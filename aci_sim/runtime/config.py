import os

APIC_A_PORT: int = int(os.environ.get("APIC_A_PORT", "8443"))
APIC_B_PORT: int = int(os.environ.get("APIC_B_PORT", "8444"))
NDO_PORT: int = int(os.environ.get("NDO_PORT", "8445"))
CERT_FILE: str = os.environ.get("CERT_FILE", "certs/sim.crt")
KEY_FILE: str = os.environ.get("KEY_FILE", "certs/sim.key")
TOPOLOGY_PATH: str = os.environ.get("TOPOLOGY_PATH", "topology.yaml")

# Default bind address for the non-sandbox servers (#23). Defaults to loopback
# so the unauthenticated /_sim control plane is never LAN-exposed by accident.
# Set SIM_BIND=0.0.0.0 explicitly for LAN deployments (e.g. the Raspberry Pi
# standing deployment, which is accessed from other hosts on the LAN).
SIM_BIND: str = os.environ.get("SIM_BIND", "127.0.0.1")

# Sandbox mode: bind each controller to its own mgmt_ip on :443 (via lo0 aliases),
# so autoACI connects by IP with no port — like real gear. Set by scripts/sandbox-up.sh.
SANDBOX: bool = os.environ.get("SIM_SANDBOX", "").strip().lower() in ("1", "true", "yes", "on")
SANDBOX_PORT: int = int(os.environ.get("SIM_SANDBOX_PORT", "443"))
