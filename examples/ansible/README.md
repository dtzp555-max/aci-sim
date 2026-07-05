# Ansible `cisco.aci` examples

This directory contains a small, validated `cisco.aci` playbook set that
exercises aci-sim the same way you'd exercise a real APIC: create a
tenant/VRF/BD/AP/EPG, confirm idempotency (re-apply reports no change), then
tear it all down.

## Prerequisites

```bash
pip install ansible
ansible-galaxy collection install cisco.aci
```

The sim must be running first — see the main `README.md` §4 Usage/Quickstart.

## Files

| File | Purpose |
| --- | --- |
| `inventory.ini` | Example inventory — one host (`sim-apic-a`) pointed at the sim, with connection vars set as group vars. |
| `smoke.yml` | Creates `SMOKE-TN1` (tenant) → `SMOKE-VRF1` (VRF) → `SMOKE-BD1` (BD) → `SMOKE-AP1` (AP) → `SMOKE-EPG1` (EPG), then re-applies every task and asserts `changed=false` on the second pass. |
| `teardown.yml` | Removes everything `smoke.yml` created, in reverse dependency order, then asserts that deleting an already-absent tenant is idempotent (`changed=false`). |

## Running against port mode (default)

Port mode is what `bash scripts/run.sh` starts: one shared bind address
(`127.0.0.1` by default), APIC site A on `:8443`.

```bash
# from the repo root, in one terminal:
bash scripts/run.sh

# in another terminal:
cd examples/ansible
ansible-playbook -i inventory.ini smoke.yml
ansible-playbook -i inventory.ini teardown.yml
```

`inventory.ini` already points `sim-apic-a` at `127.0.0.1:8443` with
`admin`/`cisco` and `validate_certs=false` (the sim's TLS cert is
self-signed — see main `README.md` §5 Configuration). If you changed
`APIC_A_PORT` or `SIM_USERNAME`/`SIM_PASSWORD`, override the matching
`aci_port`/`aci_username`/`aci_password` host/group vars.

## Running against sandbox mode

Sandbox mode (`sudo bash scripts/sandbox-up.sh`) gives each APIC a real IP
on the standard port `443` — no port number, like real gear. Point the
inventory at that IP instead and drop the port override (or set it to 443
explicitly):

```bash
sudo bash scripts/sandbox-up.sh   # from the repo root
```

Edit `inventory.ini` (or pass `-e` overrides on the command line) so
`ansible_host`/`aci_hostname` is the site's `mgmt_ip` from `topology.yaml`
(e.g. `10.192.0.11` for LAB1's default topology) and `aci_port` is `443`:

```bash
cd examples/ansible
ansible-playbook -i inventory.ini smoke.yml \
  -e aci_hostname=10.192.0.11 -e aci_port=443
ansible-playbook -i inventory.ini teardown.yml \
  -e aci_hostname=10.192.0.11 -e aci_port=443
```

Tear the sandbox down when done:

```bash
sudo bash scripts/sandbox-down.sh
```

## What "idempotent" means here

`cisco.aci` modules are idempotent at the **client** level: they GET the
existing object, diff it against the desired state, and only issue a
POST/DELETE if something actually differs. `smoke.yml`'s second pass and
`teardown.yml`'s re-delete both assert `changed: false`, proving the round
trip (create → GET-existing → diff → no-op) behaves the same against
aci-sim as it would against a real APIC — this is exactly the
GET-before-write contract documented in `docs/CONTRACT.md` §11
(`api_call()` treats any non-200 GET as fatal, so a not-yet-created
object's GET must be `200 {"imdata":[],"totalCount":"0"}`, never 404).

## Troubleshooting

- **Connection refused** — the sim isn't running, or you're pointed at the
  wrong host/port for the run mode you started (port mode vs. sandbox mode).
- **`fatal: APIC Error 103: Unable to find the object specified`** on the
  very first task — this was a real bug class this sim's PR-9 fixed (GET on
  a nonexistent DN used to 404); if you see it, you're likely running an
  older checkout — update to the latest `main`.
- **SSL errors** — make sure `validate_certs: false` (the sim's certificate
  is self-signed; see `scripts/gen_certs.sh`).
