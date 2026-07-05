# Deploying aci-sim as a background service

The simulator is a plain long-running process (`python -m
aci_sim.runtime.supervisor`, also reachable via the `aci-sim run`
CLI subcommand — see `README.md` §5 CLI), so it can run under any process
supervisor. This directory ships a systemd **user** unit for Linux (the
standing Raspberry Pi deployment) and a launchd plist sketch for macOS.

Both approaches run the simulator as an unprivileged user process bound to
`SIM_BIND` (default `127.0.0.1`, see `README.md` §Configuration / finding
#23). Sandbox mode (`scripts/sandbox-up.sh`, binding `:443` on real IPs) needs
root for the loopback alias + privileged port and is intentionally NOT
service-managed — run it interactively when you need NDO auto-discovery.

## Linux — systemd `--user` unit

1. Clone the repo and create the venv per `README.md` §2 Quick start:

   ```bash
   git clone <repo-url> ~/aci-sim
   cd ~/aci-sim
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   pip install -e .
   ```

2. Install the unit:

   ```bash
   mkdir -p ~/.config/systemd/user
   cp deploy/aci-sim.service ~/.config/systemd/user/
   ```

3. If the repo lives somewhere other than `~/aci-sim`, edit
   `WorkingDirectory`/`ExecStart` in the copied unit (the shipped unit uses
   `%h` — the systemd specifier for the invoking user's home directory).

4. If this host is a shared/LAN target for other machines (e.g. autoACI
   running elsewhere), uncomment `Environment=SIM_BIND=0.0.0.0` in the unit.
   Otherwise leave it commented — the sim will only be reachable from
   `localhost` on this host.

5. Enable + start:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now aci-sim.service
   ```

6. Allow the user service to run even when the user isn't logged in
   (needed on a headless Pi):

   ```bash
   sudo loginctl enable-linger "$USER"
   ```

7. Verify:

   ```bash
   systemctl --user status aci-sim.service
   journalctl --user -u aci-sim.service -f
   curl -sk https://127.0.0.1:8443/api/class/fabricNode.json   # or the SIM_BIND host
   ```

To pick up an edited unit (e.g. after changing `Environment=`):

```bash
systemctl --user daemon-reload
systemctl --user restart aci-sim.service
```

## macOS — launchd plist sketch

macOS has no systemd; the equivalent is a launchd **LaunchAgent** (per-user,
runs at login — parallel to how OCP-class services on this fleet are
managed). Adjust paths for your clone location, then save as
`~/Library/LaunchAgents/com.aci-sim.supervisor.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.aci-sim.supervisor</string>

    <key>WorkingDirectory</key>
    <string>/Users/YOURUSER/aci-sim</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOURUSER/aci-sim/.venv/bin/python</string>
        <string>-m</string>
        <string>aci_sim.runtime.supervisor</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <!-- Uncomment/add to expose on the LAN (#23); omit for loopback-only -->
        <!-- <key>SIM_BIND</key><string>0.0.0.0</string> -->
    </dict>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>StandardOutPath</key>
    <string>/tmp/aci-sim.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/aci-sim.log</string>
</dict>
</plist>
```

Load/manage it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aci-sim.supervisor.plist
launchctl kickstart -k gui/$(id -u)/com.aci-sim.supervisor   # restart

# after editing the plist's EnvironmentVariables, kickstart -k reuses launchd's
# cached env — you must bootout + bootstrap again for changes to take effect:
launchctl bootout gui/$(id -u)/com.aci-sim.supervisor
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aci-sim.supervisor.plist
```

## Notes

- Both approaches leave `SIM_BIND` at its loopback-only default unless
  explicitly overridden — see `README.md` §Configuration and finding #23.
- Neither unit manages sandbox mode (`scripts/sandbox-up.sh`/`sandbox-down.sh`);
  those remain manual, root-invoked, interactive operations.
- This directory ships unit **files** only — nothing here installs or starts
  a service automatically.
