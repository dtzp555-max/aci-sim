# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: minor bumps may include breaking changes to the sim's behavior).

## [0.29.2] - 2026-08-01

> Includes everything listed under 0.29.1 below. That section was written but
> its `pyproject` bump silently did nothing — the script anchored on the
> previous version string, which had already moved — and no `v0.29.1` tag was
> ever cut, so nothing shipped under that number.

### Fixed

- **The sandbox scripts honour `TOPOLOGY_PATH`.** The runtime has read it all
  along; `sandbox-up.sh`, `sandbox-down.sh` and `sim-state.sh` hardcoded
  `topology.yaml` while the first called it "the single source of truth". Set
  the variable and the sim served one topology while the scripts bound another's
  addresses — so the variable could not actually be used, and a deployment was
  forced to edit the *tracked* file in place. That is how a demo topology twice
  reached a commit; the second time CI caught it, with every test asserting the
  stock topology has no `auth:` block failing across the matrix.

### Removed

- Three F-numbered design drafts (1410 lines, marked "DESIGN ONLY") that
  `git add -A` swept in. Now ignored, along with the local topology backups and
  the rendered SVG.

## [0.29.1] — folded into 0.29.2, never tagged

### Changed

- **The CI lint job is a real gate.** It had been `ruff check . || true` since it
  was added, written around a documented baseline of ~60 findings. The baseline
  is cleared and the `|| true` is gone: on a clean tree it only guaranteed the
  next finding would go unnoticed.

  44 findings were mechanical and ruff fixed them. The rest got a decision each
  rather than a blanket ignore — `zip(strict=True)` where a row is built to
  match its header, `pytest.raises(Exception)` narrowed to the exception the
  client actually raises, unpacked-but-unused names underscored, three dead
  assignments removed, and a per-file `E402` waiver for the one file whose
  imports genuinely must follow its `sys.path.insert`.

  No behaviour change; suite unchanged at 1192 passed.

## [0.29.0] - 2026-08-01

### Fixed

- **`sim_version()` is resolved once, at import.** It re-read `pyproject.toml`
  on every call, and the sim is normally served straight out of a git working
  tree — so between a `git pull` and the restart that picks it up, the file said
  the new version while the process was still the old code. Snapshots saved in
  that window were stamped with a version they were not, and then restored
  cleanly after the restart *because the stamp matched*: precisely the
  cross-build restore the guard exists to refuse, waved through by a stamp that
  was wrong when it was written. Binding it to process start ties the stamp to
  the code that produced the data.

- **Unbinding a DHCP label in NDO now reaches the site.** `store.upsert` merges
  and never drops children the new MO omits — deliberately, so an
  externally-POSTed `epClear` survives a redeploy — so clearing a BD's
  `dhcpLabels` and redeploying left the `dhcpLbl` and its `dhcpRelayP` on both
  APICs forever. Labels a BD no longer declares are dropped, and a relay policy
  is removed once nothing on that site labels it.

  Keyed on **site state, not on the template**: deleting `relayp-<name>` because
  one template stopped naming it would take out a policy another deployed
  template's BD still points at. The undeploy path did exactly that, and is
  corrected the same way.

### Added

- Endpoint-level tests for the snapshot guard. The 0.27.0 tests covered
  `wrap`/`unwrap`/`compatibility` as pure functions, so deleting either
  `if not ok and not force: 409` left the suite green and the guard's observable
  behaviour rested on a manual check. Both planes are now pinned, including the
  two different refusal envelopes (`detail` for NDO, `imdata/error` for APIC)
  that `scripts/sim-state.sh` parses, and that 404 stays 404 rather than
  becoming 409 — the wrapper exits 3 vs 2 on that distinction.

All three found by independent review.

## [0.28.1] - 2026-08-01

### Fixed

- **`scripts/sim-state.sh` only failed on the guard.** Every other error — a
  snapshot name that was never saved (404), a 500, a plane that did not answer
  at all — printed its response body and exited **0**, so a scripted restore of
  a typo reported success having restored nothing. A curl failure was worse
  after 0.27.0 than before it: the empty body printed as a bare `[NDO] ` blank
  line, where the pre-guard version at least said `curl failed`.

  Non-2xx now fails: exit **2** for the version/topology refusal (unchanged, it
  has its own next step) and exit **3** for anything else, with the reason and a
  note that the fabric is partially restored at best. `mktemp` replaces the
  predictable `/tmp/.sim-state.$$`, which this script wrote to as root.

  Found by independent review.

## [0.28.0] - 2026-07-31

### Added

- **The deploy mirror now carries the BD's DHCP label and the relay policy it
  names into the site APICs.** `bind_dhcp_relay_to_bd` writes `dhcpLabels` onto
  a schema BD and then deploys the template — a deploy the mirror already
  handled — but the label never reached a site. Measured on a live two-site
  fabric: NDO held the relay policy, its providers and the BD label; both APICs
  held **zero** `dhcpLbl`, `dhcpRelayP` and `dhcpRsProv`. The playbook returned
  rc=0 with nothing on the site to verify against.

  A deployed BD now gets its `dhcpLbl` children, and the `dhcpRelayP` those
  labels name is materialized with a `dhcpRsProv` per provider. Both provider
  shapes resolve: an application EPG (`epgName`, the DHCP2 pattern) becomes
  `uni/tn-T/ap-A/epg-E`, and an L3Out external EPG (`externalEpgName`, DHCP1)
  becomes `uni/tn-T/out-L/instP-E` — NDO names only the external EPG, so the
  owning L3Out is resolved from the schema.

  Only the policies a deployed BD label actually references are mirrored, not
  the whole tenantPolicy template. Nothing deployed that template, so its other
  policies have no business on a site; what this closes is the dangling
  reference — a `dhcpLbl` pointing at an absent `dhcpRelayP` — which is the same
  thing `_mirror_contracts` already exists to prevent for `fvRsProv`/`fvRsCons`.
  An undeploy removes the policies it pulled in, rather than stranding them.

  A provider whose target cannot be resolved is skipped rather than guessed:
  the `dhcpRelayP` still appears, without that `dhcpRsProv`.

## [0.27.0] - 2026-07-31

### Added

- **Snapshots now record which sim and which topology produced them, and a
  restore checks both.** A snapshot is a full MIT dump, so restoring one taken
  on a different build silently reinstates *that* build's boot-time baseline
  and drops whatever the current builders produce — a v0.21 save restored onto
  v0.26 puts back a fabric with no `common`/`infra` tenants and overwrites
  mirror-corrected attributes. Every save now carries a `_meta` header
  (`sim_version`, a short `topology` content hash, `saved_at`) and
  `POST /_sim/load/{name}` returns **409** on a mismatch. `?force=1` still
  restores.

  Snapshots written before this are bare list/dict documents: they still parse,
  but are **also refused** by default. Unverifiable is not safe — the ones
  already on disk are precisely the several-releases-old dumps the check exists
  to stop.

  `scripts/sim-state.sh` gained `--force`, prints a one-line reason instead of
  a JSON blob when a plane refuses (parsing both the NDO `detail` and APIC
  `imdata/error` envelopes), and exits `2` so a scripted restore fails loudly.

### Fixed

- **`sim_version()` reads the source tree before installed metadata.** An
  editable install freezes its dist metadata at install time, so
  `importlib.metadata` still reported **0.18.1** against a 0.26.1 `pyproject`.
  Stamping snapshots from that would have recorded a version this code had not
  been for months. A wheel install has no `pyproject` and still uses the
  metadata, which is authoritative there.

## [0.26.1] - 2026-07-28

### Fixed

- **NDO deploy mirror dropped two site-local bind attributes.** Running the
  `bind_epg_to_static_port` / `bind_epg_to_physical_domain` playbooks against a
  multi-site tenant left the NDO schema correct but the mirrored APIC MOs
  incomplete:
  - `fvRsPathAtt.encap` was always empty. NDO writes the static-port VLAN as
    `portEncapVlan` (a bare int, `110`); the mirror only read an `encap` key, so
    every mirrored static port landed with no encap — invalid on real gear, and
    blank in any tool reading it back. Now converted to APIC form (`vlan-110`),
    with an explicit APIC-form `encap` still winning so hand-written or
    directly-POSTed entries are unaffected.
  - `fvRsDomAtt` hardcoded `instrImedcy="lazy"` and never wrote `resImedcy` at
    all, discarding the `deploymentImmediacy` / `resolutionImmediacy` NDO carries
    per association. Both are now mirrored, falling back to `lazy`.

  Found by pushing the full post-EPG playbook set (domain / static-port / AAEP /
  DHCP bindings) at a two-site tenant and diffing NDO state against both APICs.

- **Only the `mgmt` built-in tenant existed.** A real APIC boots with `common`,
  `infra` and `mgmt`; this sim built only `mgmt` (as the parent for the OOB
  scaffolding), so `GET /api/class/fvTenant.json` returned two fewer objects than
  any real fabric and `uni/tn-common` — where shared contracts/filters/L3Outs are
  conventionally defined, and which var files reference by DN — did not resolve at
  all. New `build/builtin_tenants.py` emits `common` and `infra`; `build/mgmt.py`
  stays the sole owner of `mgmt` and its OOB tree. Scope is deliberately minimal
  (the tenant MOs, not the policy trees a real APIC pre-populates), matching the
  existing mgmt builder's stance.

### Known gap

- Tenant **policy** templates are still not mirrored. `create_dhcp_relay`
  correctly creates the NDO tenant-policy template, but no `dhcpRelayP` /
  `dhcpLbl` appears on the APICs — same family as the un-mirrored
  `vzBrCP`/`vzSubj` gap.

## [0.26.0] - 2026-07-11

### Fixed
- **NDO tenant-delete guard now covers tenant policy templates (F5b)** — the
  in-use guard added in 0.22.0 only scanned SCHEMA templates' `tenantId`, so a
  tenant referenced only by a tenant policy template
  (`tenant_policy_templates[*].tenantPolicyTemplate.template.tenantId`) could
  be deleted, leaving a dangling reference (fails-open). `DELETE
  /mso/api/v1/tenants/{id}` now scans both: a tenant referenced by a policy
  template returns `400 "Tenant '<id>' is referenced by tenant policy
  template(s): <names> — delete those templates first"` (wording is a
  format-mirrored approximation of the hardware-grounded schema-template
  message — no real-NDO capture of this specific rejection text exists yet,
  and the code comment says so). The schema-template message is byte-for-byte
  unchanged; deleting the referencing template still releases the guard;
  malformed policy-template entries default-allow (fail-safe). +4 tests.

## [0.25.0] - 2026-07-10

### Changed
- **APIC write-face changed-status fidelity (F8a)** — a POST now reports the
  real per-MO status (`created` / `modified` / *unchanged*) instead of
  unconditionally stamping `created`. `aci_sim/rest_aci/writes.py` diffs each
  planned MO's **raw posted attributes** (excluding `dn`/`status`, before the
  create-time `_CLASS_DEFAULTS` overlay) against the pre-existing stored MO at
  the write choke point, and `apply()` builds `imdata` from **only the changed
  MOs** — returning empty `imdata` (+ `totalCount "0"`) when nothing changed.
  `post_mo` reads `?rsp-subtree` and threads it through (`modified`/absent/`full`
  → changed-MO list; `no` → empty). This restores real-APIC idempotency for
  every raw `aci_rest`-driven write: an unchanged re-push now yields
  `changed=false` (previously `changed=true` forever), fixing pass2 idempotency
  across the L3Out topology (`l3extLNodeP`/`l3extLIfP`/`l3extRsNodeL3OutAtt`/
  `l3extRsPathL3OutAtt`/`bgpPeerP`/`bfdIfP`), static-path, and PBR-redirect
  blocks. Higher-level cisco.aci modules were already idempotent (client-side
  get-diff) and are unaffected; the flat changed-MO response is `changed()`-scan
  faithful (real APIC's nested `rsp-subtree` wire shape is a documented deferred
  enhancement — no real-gear capture available to reproduce it accurately).
- **Idempotent delete-of-missing (F1)** — POSTing `status:"deleted"` to an
  already-absent DN now reports *unchanged* (empty `imdata`) instead of
  `deleted`, matching real APIC: `store.upsert` pops a missing DN as a silent
  no-op, so nothing changed. A delete that actually removes an existing MO still
  reports `deleted`.
  +12 tests (re-push idempotency, defaults-overlay guard, partial re-push,
  parent-unchanged/child-changed, status-pollution re-push, delete-of-missing,
  `rsp-subtree=no`).

## [0.24.0] - 2026-07-10

### Added
- **NDO service-graph server-side enforcement (F4 + F12)** — the sim's NDO now
  faithfully rejects, at `PATCH /mso/api/v1/schemas/{id}` time, the two
  documented real-NDO 4.x validation walls that drove the two-phase deploy
  workflow (new `aci_sim/ndo/service_graph_validation.py`):
  - **F4 — device-existence (NDO<->APIC ordering)**: binding a site-local
    service graph whose `serviceNodes[].device.dn` (`uni/tn-<t>/lDevVip-<d>`)
    does not exist on the target site's APIC returns
    `400 "Service graph device <dev> does not exist in tenant <t> in Fabric
    <site>"` — matching real NDO. Legal two-phase flows (phase1 creates the
    APIC device, phase2 binds) are unaffected; a phase2-only push is now
    rejected instead of silently binding a dangling graph.
  - **F12 — uniform site redirect**: per-fabric `serviceGraphRelationship`
    redirect writes are validated against the POST-request state (deepcopy ->
    apply -> validate -> commit): partial coverage (some but not all of a
    template's fabrics configured) returns
    `400 "must have uniform redirect policy configured on all fabrics"`,
    while a single atomic all-fabric PATCH passes — matching real NDO's
    post-state validation. Coverage-based (not DN-equality; redirect DNs are
    legitimately per-fabric). Uniformity is scoped per (template, contract),
    so same-named contracts in sibling templates cannot cross-reject.
  Non-service-graph PATCHes keep the original in-place fast path unchanged.
  Fail-safe default-allow throughout; all-or-nothing (rejected writes leave
  zero residue). +33 tests.

## [0.23.0] - 2026-07-09

### Added
- **Server-side value validation (F10)**: the sim now rejects out-of-range /
  malformed scalar MO property values that real APIC/NDO reject server-side,
  instead of silently storing them. New `aci_sim/rest_aci/validators.py` holds a
  table-driven `RULES: {(class, prop) -> validator}` registry (range / enum /
  IP·MAC-format / vlan-encap checks); `writes.py::_validate_planned` (APIC) and
  `ndo/patch.py::apply_json_patch` (NDO) enforce it before any store mutation.
  **Fail-safe default-allow**: only registered `(class, prop)` pairs are checked,
  so unknown properties pass untouched (no false-rejects). Bad values now return
  `APIC Error 103: Malformed MO body: Invalid value ...` (real-APIC-style) —
  e.g. VLAN encap outside [1,4094], VXLAN outside [1,16777215], invalid
  IPv4/IPv6 address, ASN out of range, malformed MAC, out-of-range MTU/port.
  Constraints sourced from the vendored cisco.aci module arg-specs and standard
  ACI ranges, cross-checked against the real-machine var corpus for zero
  false-rejects. ~45 rules across ~18 MO classes; +176 tests. Closes the F10
  fidelity gap (previously a var with `vlan-9999` / `999.888.777.x` pushed
  rc=0 and was stored; now 400 + not stored).

### Notes
- Referential integrity (service-graph device / BD-VRF must-exist) is
  deliberately NOT included here — tracked separately (F11), as it needs a
  real-APIC fidelity study to avoid over-rejecting the dangling refs that real
  APIC tolerates.

## [0.22.0] - 2026-07-08

### Added
- **NDO DELETE endpoints — schemas and tenants (F5)**:
  `DELETE /mso/api/v1/schemas/{id}` removes a schema (detail, summary —
  seeded or runtime-POSTed — and policy-states record; unknown id → 404)
  and `DELETE /mso/api/v1/tenants/{id}` (+ bare `/api/v1/tenants/{id}`
  alias, same dual-shape pair as GET /tenants) removes a tenant.
  `cisco.mso.mso_schema` / `mso_tenant` with `state=absent` send exactly
  these requests and previously got 405, blocking E2E baseline cleanup.
  Real-NDO guard: a tenant still referenced by any schema template's
  `tenantId` is refused with a 400 ("referenced by schema(s) …") until
  the referencing schemas are deleted. Schema deletion deliberately does
  NOT cascade into the APIC deploy mirror — real NDO does not undeploy on
  schema delete, so already-deployed objects stay orphaned on the sites'
  APICs unless the caller undeploys first (`POST /mso/api/v1/task` with
  `undeploy`).

## [0.21.0] - 2026-07-07

### Added
- **Write validation — fail like real gear**: `POST /api/mo` now rejects
  malformed property values with an APIC-style 400 instead of absorbing
  them into the MIT: `fvSubnet`/`l3extSubnet` `ip` must be a valid
  address[/prefix], `vnsRedirectDest` `ip` a valid IP. (Surfaced by a TN2
  push whose var file was missing the firewall block: the unguarded J2
  template rendered `ip=".1/24"` and `ip="."` and the sim accepted both.)
  Deletes are exempt — cleanup only needs the DN.
- **`query-target=children`** on MO/class queries (real-APIC semantics:
  the DIRECT children of each matched root as flat imdata, root excluded,
  `target-subtree-class` honored). Previously unsupported and silently
  answered with empty imdata — a verification false-negative.
- **Deploy mirror: contract/filter/service-graph shadows** — an NDO deploy
  now also materializes `vzFilter`/`vzEntry`, `vzBrCP`/`vzSubj` (+
  `vzRsSubjFiltAtt`, and `vzRsSubjGraphAtt` when the NDO contract carries a
  serviceGraphRelationship) and `vnsAbsGraph` on every target site's APIC.
  Previously the mirrored EPGs' `fvRsProv`/`fvRsCons` were dangling
  references (`GET /api/class/vzBrCP` returned 0 fabric-wide) — same family
  as the F1 fvTenant-root gap. Undeploy tears the shadows down.

## [0.20.5] - 2026-07-07

### Docs
- README: macOS note — the bundled `python3` is often 3.9 (below the 3.11
  floor); install 3.11+ from python.org or Homebrew first. Verified fresh
  installs from the public repo on macOS 13 / Intel / Python 3.14 (908 tests
  green) in addition to Apple Silicon / Python 3.13; classifiers now list
  Python 3.13/3.14.

## [0.20.4] - 2026-07-07

### Docs
- README: added a Platforms section — Linux (x86-64/ARM incl. Raspberry Pi OS,
  Debian, Ubuntu) and macOS (Apple Silicon/Intel), Python 3.11+; both verified
  with the full test suite (macOS re-verified at v0.20.3: fresh clone from the
  public repo, 908 tests green). Sandbox mode is Linux/macOS-only (root);
  Windows untested.

## [0.20.3] - 2026-07-07

### Fixed
- `aci-sim graph`: border leaves now share the LEAF row (placed to its
  right), matching autoACI's topology view — the previous separate
  border-leaf tier under the leaves read as a bogus three-tier
  "leaf under leaf" hierarchy. README diagram regenerated.

## [0.20.2] - 2026-07-07

### Fixed
- `aci-sim graph`: the single-row legend (role swatches + link-type samples)
  clipped off the right edge on narrow diagrams (few nodes) — the SVG width
  now accounts for the legend's minimum width.

### Docs
- README: regenerated the embedded topology diagram with the v0.20 renderer
  (solid ISN uplinks, link-type legend, physical-links table with /31
  point-to-point ISN addressing) + caption updated.

## [0.20.1] - 2026-07-07

### Docs
- README: added a "What it simulates (and what it doesn't)" positioning note —
  aci-sim is a management-plane simulator; protocol operational state is
  materialized from configuration, not computed by running protocols.

## [0.20.0] - 2026-07-07

### Changed
- **Spine-to-ISN underlay is now /31 point-to-point** (per the Cisco ACI
  Multi-Site design guide) — each spine's ISN-facing VLAN-4 sub-interface
  gets its own /31 pair (`172.16.{site}.{2*si}/31`, IPN peer `.{2*si+1}`)
  instead of the previous shared `/24` LAN segment with a single `.254`
  IPN router. `ospfIf.addr` and each `ospfAdjEp` peer reflect the per-link
  pair; the ISN CSW's LLDP mgmt address is unchanged (a device-level
  management IP, not an interface address).
- **`aci-sim graph` now mirrors the autoACI topology view logic**: ISN
  uplinks are drawn solid (they are physical backbone links, dashes are for
  overlays), the legend gains link-type entries (fabric link / ISN uplink /
  vPC peer-link), and a **Physical links table** is rendered under the
  diagram — one row per fabric link and per spine ISN uplink, the ISN rows
  carrying the /31 local/remote IPs.

## [0.19.1] - 2026-07-07

### Fixed
- **ISN sub-interface naming conventions flipped to match real gear** (owner
  review of the v0.19.0 demo, verified against Cisco docs/real spine CLI):
  the ACI spine names its VLAN-4 sub-if after the PORT (`eth1/49.49`,
  cf. `Eth1/29.29`-style output in `show ip ospf neighbors vrf overlay-1`),
  while the ISN Nexus names its dot1q-4 sub-if after the VLAN
  (`Ethernet1/1.4`). v0.19.0 had it backwards (spine `.4`, CSW bare
  physical): `ospfIf`/`ospfAdjEp` ids/dns now use the port-named form and
  the ISN-CSW `lldpAdjEp.portIdV` gained the `.4` suffix.

## [0.19.0] - 2026-07-07

### Changed
- **Spine ISN-facing OSPF interface is now a routed VLAN-4 sub-interface**
  (`build/underlay.py`) — real ACI multi-site spines carry the OSPF address
  toward the IPN/ISN on a routed sub-interface (e.g. `eth1/49.4`), not the
  bare physical port. `ospfIf.id`/dn now end in `.4` and carry a new `addr`
  attribute (`172.16.{site.id}.{si+1}/24`, same `/24` as the existing
  `172.16.{site.id}.254` peer); `ospfAdjEp` nests under the new sub-if
  segment. The underlying physical port (`eth1/{49+si}`) and its
  `l1PhysIf`/`ethpmPhysIf` are unchanged — LLDP/physical state stays there,
  matching real hardware.
- **ISN CSW LLDP neighbor** (`build/fabric.py`) — each spine's ISN uplink
  physical port now gets a simulated LLDP neighbor (`lldpAdjEp`/`cdpAdjEp`,
  via the shared `add_adjacency` helper) representing the inter-site
  network switch: `sysName=ISN-CSW{site.id}`, `portIdV=Ethernet1/{si+1}`,
  model `N9K-C9364C`. Together with the sub-interface change above, this
  closes the gap where autoACI's Topology "Fabric (physical)" view showed
  `Local IP=-`/`Neighbor Port=-` for spine ISN uplinks — it now derives a
  complete row from `ospfIf` (port + local IP), `ospfAdjEp` (remote IP),
  and this `lldpAdjEp` (CSW-side port). Multi-site fabrics only; single-site
  fabrics build none of this (unchanged: no OSPF/LLDP toward an ISN that
  doesn't exist). New/updated assertions in `tests/test_pr19_tier2.py`,
  `tests/test_pr3b_batch1.py`, `tests/test_pr5_topology_consistency.py`.

## [0.18.1] - 2026-07-07

### Changed
- **License: MIT -> PolyForm Noncommercial 1.0.0** — aci-sim stays free for
  personal, lab, research, and other noncommercial use; commercial use now
  requires the author's permission. (`LICENSE`, `pyproject.toml` SPDX field,
  README badge + License section.)
- README: added a Buy-Me-a-Coffee badge/link.

## [0.18.0] - 2026-07-06

### Added
- **Runtime tenant-L3Out eBGP session materialization** — a tenant L3Out
  pushed live via `POST /api/mo` (cisco.aci playbooks / aci-py) lands
  `bgpPeerP` (config) through the generic write path, which never produced
  the operational session MOs. `writes.py` now reacts to every runtime
  `bgpPeerP` by materializing `bgpPeer`/`bgpPeerEntry`(`operSt=established`)/
  `bgpPeerAfEntry` under a `dom-{tenant}:{vrf}` BGP domain on the L3Out's own
  node — the peer ASN derived from the nested `bgpAsP`, the VRF from the
  L3Out's `l3extRsEctx.tnFvCtxName` (skips cleanly when no VRF is bound, or
  on `status=deleted`). This mirrors what `build/l3out.py` already does for
  topology-defined L3Outs, and makes per-tenant L3Out BGP session views in
  API clients (e.g. autoACI's "L3Out BGP" topology table) populate for
  runtime-pushed tenants. New regression tests in
  `tests/test_l3out_bgp_session.py` (903 tests total).

## [0.17.0] - 2026-07-05

### Added
- **`aci-sim query CLASS`** — a zero-friction, stdlib-only authenticated MIT
  query helper (`aci_sim/cli.py`'s `cmd_query`), closing a clean-room
  onboarding gap: the README's smoke test (`curl .../class/fabricNode.json`)
  returned an APIC 403 error envelope, not the fabric, because — like real
  APIC — the sim requires `aaaLogin` before any query. `aci-sim query`
  does the `aaaLogin` -> `APIC-cookie` -> `GET /api/class|mo/...` dance for
  you (via `urllib.request`/`ssl`/`http.cookies`, no new dependency — httpx
  stays dev-only) and prints a compact table (`--json` for the raw
  envelope). Supports `--dn DN` for a single-MO query, `--host`/`--user`/
  `--password` overrides, and a friendly hint ("is the sim running? start
  it with `aci-sim run`") on a connection error instead of a traceback.

### Fixed
- **`scripts/run.sh` failed with `ModuleNotFoundError: uvicorn` outside an
  activated venv.** It invoked bare `python` (whatever that resolves to on
  `$PATH`), unlike `scripts/sandbox-up.sh` which already resolves
  `./.venv/bin/python`. Now `scripts/run.sh` uses the same
  `PY="./.venv/bin/python"` (falling back to `python3`) pattern, so
  `bash scripts/run.sh` works from a fresh, non-activated shell right after
  `pip install -e '.[dev]'` — no `source .venv/bin/activate` required.
- **README quickstart + smoke test now copy-paste-correct.** Added a
  60-second quickstart (`pip install` -> `aci-sim run` -> `aci-sim query
  fabricNode`) verified end-to-end in a clean-room test; the old smoke test
  (`curl` with no login) is replaced with `aci-sim query fabricNode` (or the
  full authenticated curl, shown as an alternative). Also embeds a rendered
  `aci-sim graph` topology screenshot (`docs/images/topology.png`) near the
  top and in the `aci-sim graph` CLI section, and documents `aci-sim query`
  in §5 with a live sample transcript.

## [0.16.0] - 2026-07-05

### Changed
- **Project renamed `aci-fabric-sim` -> `aci-sim`.** Import package
  `aci_fabric_sim` -> `aci_sim`, PyPI distribution name, GitHub repo, and the
  `aci-sim` CLI entry point are now all unified under one name (previously
  the package and CLI were `aci-sim`/`aci_sim` while the repo/distribution
  metadata still said `aci-fabric-sim` in places). No behavior change —
  every module path under `aci_sim/` and every documented command are
  unchanged; this is a naming-consistency cleanup ahead of the repo going
  public.

## [0.15.0] - 2026-07-05

### Added
- **NDO -> APIC deploy mirror.** `POST /mso/api/v1/task` (the deploy/undeploy
  request `cisco.mso.ndo_schema_template_deploy` sends) was a pure
  acknowledgment no-op — it never materialized the deployed schema
  template's VRFs/BDs/ANPs/EPGs/binds into the target sites' APIC stores, so
  a multi-site tenant created purely through NDO never appeared on the APIC
  side. `mirror_template_to_sites()` (`aci_sim/ndo/deploy_mirror.py`) closes
  this gap: given the NDO state, a template name, and a site_id ->
  ApicSiteState map, it walks the owning schema's template plus each
  associated SITE entry (host-based-routing overlay, domain associations,
  static ports) and upserts the equivalent `fvCtx`/`fvBD`/`fvAp`/`fvAEPg` (+
  children) MOs into every target site's `MITStore`, reusing the same
  DN/attribute conventions the boot-time builders and the direct POST-write
  path already use. Also creates a per-site `fvTenant` shadow so the tenant
  itself — not just its child objects — is visible on each target site's
  APIC after an NDO-driven deploy (previously only the site passed via
  `--apic` in ad hoc testing got the tenant; the other site's `fvTenant` was
  missing entirely).
- **`fvBD` create-time default attributes.** A POSTed BD now carries the
  same default attribute set a real APIC assigns at creation
  (`epMoveDetectMode="garp"`, `hostBasedRouting="no"`, `mtu="9000"`, …)
  instead of only the attributes the caller explicitly set — so
  host-based-routing and EP-move-detection now show up correctly in the
  APIC BD view for a BD created via a plain POST, matching real-APIC
  behavior.

## [0.14.0] - 2026-07-05

### Added
- **File-backed state persistence.** New `POST /_sim/save/{name}` and
  `POST /_sim/load/{name}` routes on both the APIC plane
  (`aci_sim/control/admin.py`) and the NDO plane (`aci_sim/ndo/app.py`)
  serialize/restore each plane's store to/from disk under `SIM_STATE_DIR`
  (default `~/.aci-sim/state`) — unlike the existing in-memory
  `snapshot`/`restore`, this state survives a full sim restart.
  `scripts/sim-state.sh {save|restore} <name>` is the whole-fabric wrapper:
  it derives each plane's address the same way `scripts/sandbox-up.sh` does
  and hits every plane's endpoint (both APIC sites + NDO) in one command.
  New module `aci_sim/control/persist.py` implements the serialization: a
  `MITStore` flattens to a JSON list of `{class, attrs}` dicts (MOs stored
  in the MIT are always childless — the parent/child hierarchy lives only
  in the store's own index — so replaying them through `MITStore.add`
  faithfully rebuilds it); `NdoState`'s persisted fields are deep-copied to
  a JSON-friendly dict and reapplied onto the same live object via
  `setattr` (the running NDO app closes over that object's identity, so a
  reload must mutate in place rather than swap in a new instance).

## [0.13.2] - 2026-07-04

### Fixed
- **NDO `serviceNodeRef` resolved to a string ref (was a dict).** The
  service-graph -> contract bind path stored `serviceNodeRef` as a dict-form
  ref while every other mirror/lookup path expects the canonical NDO string
  ref shape (`/schemas/{id}/templates/{tmpl}/{category}/{name}`); re-running
  the bind against an already-bound contract was not idempotent because the
  two representations never compared equal. Fixed to resolve/store the
  string form, matching the rest of the NDO ref conventions.

## [0.13.1] - 2026-07-04

### Fixed
- **`pip install` was completely broken — now fixed (critical, packaging).** A
  from-scratch clean-venv E2E revealed that `pip install aci-sim` / `pip
  install .` produced an unusable package: (1) **zero runtime dependencies** were
  declared to pip — `pyproject.toml` had `dynamic = ["dependencies"]` with no
  `[tool.setuptools.dynamic]` source, so fastapi/uvicorn/pydantic/pyyaml were
  never installed (the CLI crashed on the first third-party import, `import
  yaml`); and (2) **every subpackage was omitted** — `packages =
  ["aci_sim"]` shipped only the top-level module, dropping
  `topology`/`build`/`rest_aci`/`ndo`/`mit`/`runtime`/`query`/`control`. The dev
  workflow (`pip install -e .` + `pip install -r requirements.txt`) masked both.
  Fix: static `[project.dependencies]` (fastapi/uvicorn[standard]/pydantic/pyyaml),
  a `[project.optional-dependencies].dev` extra (httpx/pytest/pytest-asyncio), and
  `[tool.setuptools.packages.find] include = ["aci_sim*"]` to ship every
  subpackage. Verified end-to-end: a fresh venv `pip install .` now yields a
  working `aci-sim` CLI + importable package.

## [0.13.0] - 2026-07-04

### Added
- **Admin account in the setup wizard + `aci-sim run` credential enforcement.**
  `aci-sim init`'s new **Step 0: Admin account** prompts for the fabric admin
  username/password (default `admin`/`cisco`) and whether NDO shares the APIC
  account (default yes), writing them into a new topology.yaml `auth:` section
  (`schema.Auth`: `username`/`password` + optional `ndo_username`/`ndo_password`).
  `aci-sim run` resolves these into `SIM_USERNAME`/`SIM_PASSWORD` for the
  supervisor via the new `cli.resolve_admin_credentials()`, so the APIC plane
  then ENFORCES them (`aaaLogin` returns 401 on a username/password mismatch).
  **Precedence** (highest first): an explicit `SIM_USERNAME`/`SIM_PASSWORD`
  already in the environment wins (the CI/override escape hatch), else the
  topology `auth:` section, else `auth.py`'s own `admin`/`cisco` default.
  `aci-sim run` prints a one-line auth-source banner at startup (the username +
  where it came from, never the password).
- The NDO plane stays **lenient by design** — `/login` and `/api/v1/auth/login`
  accept any credential and never validate the bearer token (cisco.mso/aci-py
  client compatibility). The shared account is recorded/reported but not
  enforced there; a split `ndo_username`/`ndo_password` is informational.

### Notes
- Purely additive / backward-compatible: a topology.yaml with **no** `auth:`
  section still validates (`Topology.auth: Auth | None = None`) and `aci-sim
  run` injects nothing — behavior is identical to 0.12.0.
- 24 new tests (`tests/test_admin_account.py`) cover the wizard's `auth:`
  output (defaults / custom / split-NDO), `resolve_admin_credentials`
  precedence, `build_run_env_argv` injection + filesystem purity, end-to-end
  `aaaLogin` enforcement of a rotated password (incl. the stale default now
  failing) with NDO still lenient, and no-`auth:` backward-compat.

## [0.12.0] - 2026-07-04

### Added
- **LLDP/CDP neighbor fidelity — enriched adjacencies + materialized container MOs + CLI.**
  `lldpAdjEp`/`cdpAdjEp` previously carried only `sysName`/`mgmtIp`; both are now enriched with
  real-APIC-shaped fields — `lldpAdjEp` gains `portIdV` (the NEIGHBOR's own port), `portIdT`,
  `chassisIdT`, `chassisIdV` (a deterministic per-node MAC via new `build/neighbors.py:node_mac`),
  `sysDesc` (`"{model} running {version}"` for a switch neighbor, `"Cisco APIC"` for a controller),
  `capability`, `id`, `ttl`; `cdpAdjEp` gains `devId` (== `sysName`), `portId`, `platId`, `ver`,
  `cap`. Purely additive — `sysName`/`mgmtIp` are unchanged, so every pre-existing consumer
  (autoACI, this repo's own tests) keeps working untouched.
  New container classes `lldpInst`/`lldpIf`/`cdpInst`/`cdpIf` are now materialized (one `*Inst`
  per switch node with any adjacency, one `*If` per adjacency-bearing local port), fixing a bug
  where a deep-root subtree query (`GET /api/mo/topology/pod-1/node-101/sys/lldp/inst.json?
  query-target=subtree&target-subtree-class=lldpAdjEp`) returned `totalCount=0` — the query
  engine's `run_mo_query` short-circuits to `([], 0)` when the root DN has no MO of its own, and
  `.../sys/lldp/inst` previously had none. `/api/class/lldpInst.json`/`lldpIf.json`/`cdpInst.json`/
  `cdpIf.json` now also return real results.
  All three adjacency-construction sites (`build/cabling.py` spine<->leaf links, `build/fabric.py`
  APIC<->leaf attachment, `rest_aci/writes.py`'s dynamic node-registration reaction) now share one
  helper module, `build/neighbors.py` (`add_adjacency`/`add_switch_adjacency` +
  `ensure_lldp_containers`/`ensure_cdp_containers`), so a dynamically-registered node (via POST
  `fabricNodeIdentP` or `/_sim/add-leaf`) gets the identical enriched/materialized treatment a
  boot-time-seeded node gets — verified by `tests/test_lldp_cdp_fidelity.py`'s dynamic-path tests.
  New CLI subcommand `aci-sim lldp [topology] [--site NAME] [--node ID] [--cdp] [--json]` prints a
  `show lldp neighbors`-style grouped table (or CDP with `--cdp`, or structured JSON with `--json`).
  `tests/test_lldp_cdp_fidelity.py` (20 tests): container materialization, the subtree-query
  regression fix, bidirectional `portIdV` consistency across a spine<->leaf link, `cdpAdjEp.devId
  == sysName`, backward-compat field presence, the dynamic-registration path, and the CLI (text/
  `--json`/`--cdp`/a graceful non-existent node id).

## [0.11.0] - 2026-07-04

### Added
- **APIC query subscriptions + websocket push-on-change.** Real APIC's subscription mechanism,
  the piece the "Monitoring/observability integration" use case (README §1) needed to move past
  polling-only. `GET /api/class/{cls}.json`, `GET /api/mo/{dn}.json` (+ `/api/node/mo/{dn}` alias),
  and `GET /api/node/class/{...}` all now accept `?subscription=yes`, returning the *unchanged*
  query result plus a top-level `subscriptionId` string (opt-in only — a plain query with no
  `?subscription=yes` is byte-identical to before this feature, no key added, no behavior change).
  `GET /socket<token>` (real APIC's literal path shape, `<token>` = the `APIC-cookie` session
  token) opens a websocket; when a subscribed class or DN/subtree changes via `POST`/`DELETE`,
  a `{"subscriptionId":[...],"imdata":[{<class>:{"attributes":{...,"status":"created|modified|
  deleted","dn":...}}}]}` event pushes over that connection. `GET /api/subscriptionRefresh.json?
  id=<id>` keeps a subscription alive past its 90s TTL (matching real APIC), always 200 even for
  an unknown id. Disconnecting the websocket drops that connection's subscriptions. New module
  `aci_sim/rest_aci/subscriptions.py` (in-memory registry + notify — module-level state,
  same pattern as `auth.py`'s session store); write handlers stay fully synchronous and simply
  call `notify()`, which enqueues onto each live connection's own `asyncio.Queue` — the websocket
  route drains that queue independently, so a write request never awaits network I/O on some
  other client's socket. `tests/test_subscriptions.py` (12 tests): subscribe returns
  `subscriptionId`; a plain query has zero shape change; websocket created/modified/deleted push
  for a subscribed class; an unsubscribed class does not push; DN-scoped subscriptions match
  subtree writes; refresh returns 200 (known and unknown id); an unknown-token websocket is
  rejected; disconnect cleans up subscriptions. See README §10a and `docs/CONTRACT.md` §2a for
  the full contract.

## [0.10.0] - 2026-07-04

### Added
- **OOB management gateway — a real MO instead of a discarded value.** Every node already got
  `topSystem.oobMgmtAddr` (the OOB IP), but no gateway was modeled anywhere; `aci-sim init`'s wizard
  even asked for one per site, but discarded the answer (no schema field, no MO — just an
  informational summary line). Real APIC models the OOB address + gateway in the special `mgmt`
  tenant (`uni/tn-mgmt/mgmtp-default/oob-default`, a `mgmtOoB` EPG) with one `mgmtRsOoBStNode` child
  per node. New `Site.oob_gateway: Optional[str]` schema field (optional, default `None`, validated
  as a real IP address) plus a new builder `aci_sim/build/mgmt.py` (registered in
  `build/orchestrator.py`) that emits the minimal faithful scaffolding for every site:
  `fvTenant`(mgmt) → `mgmtMgmtP`(default) → `mgmtOoB`(default) → one `mgmtRsOoBStNode`(tDn,addr,gw)
  per node (switches AND controllers), `addr` = the node's OOB IP + mask (mask from
  `fabric.oob_subnet` if set, else `/24` by default — deliberately not `fabric.oob_pool`'s wider
  `/16`, which spans multiple pods/sites), `gw` = `site.oob_gateway` (empty string, never an invented
  default, when unset).
- `aci-sim init`'s wizard now WRITES each site's answered OOB gateway into
  `topology_dict["sites"][i]["oob_gateway"]` instead of discarding it; `aci-sim show` surfaces
  `oob_gateway=<value|->` per site.
- `tests/test_oob_gateway.py` (25 tests): schema default/validation, builder MO shape (mgmtRsOoBStNode
  per node, addr/gw correctness, mask derivation, empty-gw-when-unset), wizard write-through
  (defaults run, custom answers, multi-site per-site gateways), and backward compatibility (repo
  `topology.yaml` builds unchanged, existing `topSystem`/`fabricNode` attrs untouched, new
  mgmt-tenant scaffolding is purely additive).

### Changed
- `docs/CONTRACT.md`: new "Mgmt tenant — OOB management" entry cataloging `fvTenant`(mgmt)/
  `mgmtMgmtP`/`mgmtOoB`/`mgmtRsOoBStNode`.
- `README.md` §6c (Tier-3 config reference) documents `site.oob_gateway`; §5 `aci-sim show`/
  `aci-sim init` sample output updated to include `oob_gateway=...`.

## [0.9.0] - 2026-07-04

### Added
- **`aci-sim graph` — self-contained SVG/HTML topology diagram.** New `aci_sim/graph.py`
  renders the *built* fabric (spines, leaves, border leaves with vPC-pair grouping, per-site
  controllers, and — for multi-site topologies with `isn.enabled: true` — a single ISN cloud node
  with spine→ISN uplinks) as hand-generated, dependency-free SVG: no CDN, no local server, no npm
  build step. `-o FILE.html` wraps the SVG in a minimal inline-CSS HTML shell (double-click to open
  in any browser); `-o FILE.svg` writes raw SVG. Layout is a hand-computed tiered ACI hierarchy
  (spine row → leaf row → border-leaf row → controller row per site, sites placed side by side, ISN
  cloud centered above/between them for multi-site) plus a role→color legend and a title bar
  (fabric name, site count, node count). Purely additive: no change to `topology/schema.py` or any
  `build/*.py` builder — `graph.py` only reads the validated `Topology` model and re-derives the
  same spine×leaf mesh `build/cabling.py::cabling_links()` produces, so the diagram always matches
  the fabric that would actually be built.
- **Visual language matched to autoACI's topology view** (studied read-only on autoACI's
  `frontend/src/composables/useTopologyStyles.js` + `backend/routers/topology.py`; NOT imported —
  autoACI's implementation is Vue+cytoscape, this is a standalone Python reimplementation of the
  same colors/hierarchy): spine = `#2563eb` blue, leaf/border-leaf = `#16a34a` green, controller =
  `#d97706` amber, ISN cloud = `#475569` slate; rounded-rect nodes with a subtle drop shadow;
  cabling links solid gray, ISN links dashed orange, vPC-peer links dotted slate.
- Complements `aci-sim show` (text inventory) with a visual counterpart — `show` for machine/human
  text detail, `graph` for at-a-glance topology shape.
- `tests/test_graph.py` (18 tests): node/link counts against the repo's default 2-site topology,
  ISN-cloud presence for multi-site and absence for a generated 1-site topology, a generated 3-site
  topology rendering all 3 sites, role-color presence, well-formed-XML checks via `xml.etree`, and
  `aci-sim graph` CLI coverage (HTML/SVG output, default paths, missing-file and bad-extension
  errors) — no browser required anywhere in the suite.

## [0.8.1] - 2026-07-04

### Added
- **CI gate example + Use Cases docs.** New `examples/ci/`: `assert_mit.py` (a tiny MIT-end-state
  assertion helper — `class:`/`mo:`/`absent:`/`attr:` checks, exit code = failed-check count, works
  against the sim OR a real APIC) and `github-actions-aci-gate.yml` (copy-pasteable start-sim →
  run-automation → assert workflow). README §1 gains a "More applications" table (SDK/provider
  testing, change pre-flight, REST-contract testing, multi-tool interop, monitoring, scale) with
  honest ready/partial/extension status, plus an explicit out-of-scope note.

## [0.8.0] - 2026-07-04

### Added
- **`aci-sim init` — interactive APIC-setup-style topology wizard (PR-22).** Modeled on the real APIC
  first-boot setup dialog: prompts each field with a suggested default shown in `[brackets]` (ENTER
  accepts it, or type a custom value), then writes a `topology.yaml` and auto-runs `aci-sim validate` on
  the result. Flow: Step 0 deployment type (`1) Single fabric` / `2) Multi-site` — real APIC's "multi-pod"
  choice is deliberately NOT offered, since this sim builds no true multi-pod fabric); Step 1 per-site
  fields (name, site ID, BGP AS, pod ID, controller count 1-5, APIC OOB mgmt IP, then — if
  controllers > 1 — one prompt per additional controller each suggesting the previous IP + 1 via
  `ipaddress.ip_address(prev) + 1`, mapping to PR-21's `Site.controller_ips`; OOB gateway asked once per
  site; TEP pool/infra VLAN/GIPo pool/OOB subnet/spine-leaf-border counts); Step 2 multi-site-only
  (NDO mgmt IP, ISN OSPF area/MTU); Step 3 summary + confirm + write + auto-validate. Reuses
  `generate_topology()` — the SAME generator `aci-sim new` calls — as a thin interactive front-end; no
  topology-emission logic is duplicated.
- **Non-interactive wizard modes**: `--defaults` accepts every suggested default without prompting, and is
  also auto-enabled whenever stdin is not a TTY (so `aci-sim init` never hangs in CI/scripts even without
  the flag). `--answers FILE` accepts a YAML or JSON file of pre-filled field-name -> value answers;
  listed fields are used verbatim, unlisted fields still prompt/default as usual — enables fully scripted,
  reproducible topology generation (e.g. `site1_controllers: "3"` to script a specific cluster size).
- **`generate_topology()` widened (PR-22, additive, backward compatible)**: new optional `site_overrides`
  parameter — a `list[dict]` (one entry per site) shallow-merged onto each auto-derived site definition
  (name/id/apic_host/mgmt_ip/fabric_name/asn/pod/controllers/controller_ips) — lets a caller (the wizard)
  express per-site heterogeneity, including PR-21's `controller_ips`, which the existing flat
  `--controllers`/`--apic-ip-base` flags cannot express per-site. `leaves_per_site`/`spines_per_site`/
  `border_pairs` now also accept a `list[int]` (one count per site) in addition to the pre-existing
  single `int` applied uniformly to every site — needed because the wizard asks spine/leaf/border-pair
  counts per site. Both widenings are fully backward compatible: every pre-PR-22 caller (including
  `aci-sim new` and every existing test) passes a plain `int`/omits `site_overrides`, and gets
  byte-identical output to before.

### Changed
- CLI help/argparse: new `init` subcommand registered alongside `validate`/`show`/`run`/`new`.

## [0.7.0] - 2026-07-04

### Changed
- **BREAKING (node counts): `Site.controllers` default 3 → 1 — single-APIC-per-site by design (PR-21).**
  APIC cluster size is irrelevant to Ansible playbook *deployment* testing (a playbook targets exactly one
  APIC endpoint); cluster size only matters for cluster-health/appliance-vector tooling such as autoACI's
  `health_score.py`. `controllers` remains fully configurable, now range-validated **1-5**
  (`Topology.normalize_and_validate`) — set `controllers: 3` on a site to restore the old default /
  exercise multi-controller `infraWiNode`/cluster-health semantics. This changes every site's generated
  `fabricNode`/`topSystem` count (−2 per site at the new default vs. the old one), `fabricLink`/
  `lldpAdjEp`/`cdpAdjEp` counts for the APIC↔leaf attachment, and `infraWiNode` count (`controllers²`).
  All existing test assertions and e2e scripts encoding the old counts were recomputed and updated — see
  `docs/DESIGN.md`'s "PR-21" section for the full before/after accounting.
- `aci-sim new`'s `--controllers` flag and `generate_topology()`'s `controllers` parameter both default to
  `1` now (was `3`), matching the new `Site.controllers` schema default.
- The shipped root `topology.yaml`'s commented `# controllers: N` example line now shows the current
  default (`1`) instead of the pre-PR-21 default (`3`); a new commented block shows the
  `controllers: 3` + `controller_ips: [...]` opt-in shape for multi-controller topologies.

### Added
- **`Site.controller_ips: Optional[list[str]]` — explicit per-controller OOB mgmt IPs (PR-21).** When
  set, `controller_ips[i]` is controller `i+1`'s `topSystem.oobMgmtAddr` verbatim (length must equal
  `controllers`, each entry a valid IP — validated). When unset (default), OOB IPs are auto-derived
  **sequentially from `site.mgmt_ip`**: controller 1 = `mgmt_ip`, controller 2 = `mgmt_ip`+1, controller 3
  = `mgmt_ip`+2, etc., via the new `Site.controller_oob_ips()` helper (`topology/schema.py`). Falls back
  to the legacy per-node `oob_ip(pod, cid)` scheme when `mgmt_ip` is also unset, so a topology setting
  neither field keeps byte-identical controller OOB addresses to pre-PR-21. Wired into
  `build/fabric.py`'s controller `topSystem.oobMgmtAddr` and the leaf-side `lldpAdjEp`/`cdpAdjEp.mgmtIp`
  (kept consistent — both read the same per-call derived list). Surfaced in `aci-sim show`/`show --json`
  as `controller_ips`. The sim's REST server still binds to exactly one address per site (`mgmt_ip`,
  controller-1 / the cluster VIP) regardless of `controllers` — it never runs N separate APIC processes;
  `controllers`/`controller_ips` only affect the simulated fabricNode/topSystem/infraWiNode data.
- README "Configuration" section (§6a) now documents the single-APIC design intent explicitly, and a new
  `docs/DESIGN.md` "PR-21" section covers the full before/after node-count accounting and verification.

## [0.6.0] - 2026-07-04

### Added
- **Tier-3 fine-tuning parameters (PR-20) — the last planned param tier.**
  New optional `topology.yaml` fields — all defaulted to pre-existing
  hardcoded behavior (or the real ACI factory default where no prior
  hardcoded value existed), so the existing repo `topology.yaml` validates
  and builds unchanged:
  - `bd.mac` (per-BD, optional) and `fabric.default_bd_mac` (fabric-wide,
    default `"00:22:BD:F8:19:FF"` — the exact literal `build/tenants.py`
    already hardcoded for every BD pre-PR-20) — now threaded into
    `fvBD.mac`, replacing that hardcode. Resolution order: `bd.mac or
    fabric.default_bd_mac`. Byte-identical output when both are unset.
  - `l3out.csw_peer.keepalive`/`.hold` (defaults `60`/`180`, real ACI
    defaults) — threaded into `build/l3out.py`'s `bgpPeerP.keepAliveIntvl`/
    `holdIntvl` (previously not carried on that MO at all). `hold` must
    exceed `keepalive`.
  - `isn.ospf_hello`/`.ospf_dead` (defaults `10`/`40`, real ACI defaults) —
    threaded into `build/underlay.py`'s `ospfIf.helloIntvl`/`deadIntvl`
    (previously not carried on that MO at all). `ospf_dead` must exceed
    `ospf_hello`.
  - `isn.bfd_min_rx`/`.bfd_min_tx`/`.bfd_multiplier` (defaults `50`/`50`/
    `3`, real ACI defaults) — store + range-validate + surface only. This
    sim builds no `bfdIfP`-style MO anywhere (grepped `build/*.py`: zero
    matches for "bfd"), so these are explicitly **store-only**, documented
    as such in `aci-sim show`'s own output.
  - `fabric.oob_subnet` (validate-only; must fall within
    `192.168.0.0/16`) and `fabric.inb_subnet` (store-only; must fall
    within a private RFC1918 range) — `oob_ip()` still derives every
    node's OOB address from `(pod, node_id)` only (documented pre-existing
    limitation, unchanged); this sim has no in-band mgmt MO at all, so
    `inb_subnet` is explicitly store-only.
  - `fabric.default_apic_version` (fabric-wide override; default `None` =
    unchanged behavior) — takes precedence over each site's own
    `site.apic_version` (PR-9/PR-18 lineage, already fully wired and
    already independent of switch-node `Node.version`) in
    `build/fabric.py`'s controller `fabricNode`/`topSystem`/
    `firmwareCtrlrRunning`. **Finding while implementing this PR:**
    `site.apic_version` already drove those MOs independently of switch
    version before this PR — the actual gap closed here is (1) a
    fabric-wide override knob and (2) `aci-sim show` never surfaced
    `apic_version` at all pre-PR-20 (now shown per-site as the resolved
    effective value).
  - `aci-sim new` gained `--isn-ospf-hello`, `--isn-ospf-dead`,
    `--isn-bfd-min-rx`, `--isn-bfd-min-tx`, `--isn-bfd-multiplier`,
    `--default-bd-mac` flags; `aci-sim show` now prints
    `oob_subnet`/`inb_subnet` (labeled store-only)/`default_bd_mac`,
    `default_apic_version` (when set), ISN `ospf_hello`/`ospf_dead`, ISN
    BFD timers (labeled store-only), and each site's effective
    `apic_version`.
  - New tests: `tests/test_pr20_tier3.py` (41 tests). Full suite: 713
    passed (672 pre-existing + 41 new), zero regressions.
  - See `docs/DESIGN.md`'s "PR-20 — Tier-3 fine-tuning parameters" section
    for the full design rationale and an explicit statement of which
    backward-compat verification was performed in this sandboxed
    development environment vs. deferred to the main thread's independent
    production-chain re-run (no SSH access to the hardware test host from
    this environment).

## [0.5.0] - 2026-07-04

### Added
- **Tier-2 topology-elasticity parameters (PR-19).** New optional
  `topology.yaml` fields — all defaulted to pre-existing hardcoded behavior,
  so the existing repo `topology.yaml` validates and builds unchanged:
  - `isn.ospf_area` (default `"0.0.0.0"`, accepts dotted-decimal or
    decimal-integer notation) and `isn.mtu` (default `9150`, range
    576-9216) — now threaded into `build/underlay.py`'s `ospfIf`/
    `ospfAdjEp` `area` and `build/interfaces.py`'s dedicated ISN/IPN spine
    uplink `l1PhysIf.mtu`, replacing hardcoded `"0.0.0.0"`/`9216` literals
    at those specific call sites. Ordinary fabric ports keep their existing
    9216 MTU.
  - `access.vmm_domains[]` (default: empty list) — a VMM domain model
    (`vcenter_ip`, `vcenter_dvs`, `vlan_pool`, `datacenter`) that builds a
    `vmmDomP` (+ `vmmCtrlrP`/`vmmUsrAccP` when `vcenter_ip` is set, +
    `infraRsVlanNs` when `vlan_pool` is set) in `build/access.py`. Verified
    (read-only, hardware test host) that this is a **pure addition**: the real
    aci-py `bind_epg_to_vmm_domain` MS-TN1 chain writes an NDO schema
    `domainAssociations` entry referencing the domain by DN string only —
    no existing builder or production chain reads a `vmmDomP`/`vmmCtrlrP`
    object today, hence the empty-list default (unlike `phys_domains`/
    `l3_domains`/`aaeps`, which each seed one entry). See `docs/DESIGN.md`
    PR-19 section for the full verification trail.
  - **Leaf/spine count elasticity bug fix**: `aci-sim new`'s node-ID
    generator (`generate_topology()`) had no guard against
    `--leaves-per-site`/`--spines-per-site` exceeding the node-ID scheme's
    documented ~100-per-site ceiling, or `--border-pairs` pushing the
    border block past the next site's leaf range — all three previously
    produced a `topology.yaml` dict that failed deep inside
    `Topology.normalize_and_validate` with a generic "node ids collide"
    error. Now rejected at generation time with a clear message naming the
    offending flag. Verified: `--leaves-per-site 8 --spines-per-site 4`
    (and `--border-pairs` > 1) validate and build clean; the exact boundary
    (100 leaves/spines, 49 border-pairs at default counts) still works;
    101/50 respectively are cleanly rejected.
  - `isn.peer_mesh: partial` re-examined and kept as a documented
    validate-and-reject (unchanged decision from PR-17/18) — no sensible
    default partition exists without additional per-site config the schema
    doesn't carry.
  - `aci-sim new` gained `--isn-ospf-area`, `--isn-mtu` flags; `aci-sim
    show` now prints ISN enabled/peer_mesh/ospf_area/mtu and any configured
    VMM domains.
  - New tests: `tests/test_pr19_tier2.py` (37 tests) + 6 additions to
    `tests/test_cli.py`.

## [0.4.0] - 2026-07-04

### Added
- **Tier-1 fabric configuration parameters (PR-18).** New optional
  `topology.yaml` fields — all defaulted to the pre-existing hardcoded
  behavior, so the existing repo `topology.yaml` validates and builds
  unchanged:
  - `fabric.tep_pool` (default `"10.0.0.0/16"`), `fabric.infra_vlan`
    (default `3967`, range 1-4094), `fabric.gipo_pool` (default
    `"225.0.0.0/15"`) — stored, CIDR/range-validated, and surfaced via
    `aci-sim show`/`aci-sim new`. No builder currently derives a per-node
    address or VLAN attribute from these (see `docs/DESIGN.md` PR-18
    section) — store-only until a future PR wires them into a builder.
  - `site.controllers` (APIC cluster size, default 3) and `site.pod`
    (default 1) already existed and were already fully wired through every
    relevant builder (`fabricNode`/`topSystem`/`infraWiNode`/
    `firmwareCtrlrRunning`/health/endpoints); PR-18 verified this end-to-end
    and added `aci-sim new --controllers N`.
  - Deterministic non-empty node serials: `build/fabric.py`'s new
    `default_serial(site_id, node_id)` (`f"SAL{site_id}{node_id:04d}"`)
    replaces the previous inline `SIM{node.id:08d}` fallback used whenever
    an auto-generated `Node.serial` is empty, so `fabricNode.serial` /
    `fabricNodeIdentP.serial` always carry a real, deterministic serial for
    `cisco.aci.aci_fabric_node` to register against. Explicit YAML
    `serial:` values still override.
  - `aci-sim new` gained `--controllers`, `--tep-pool`, `--infra-vlan`,
    `--gipo-pool` flags; `aci-sim show` now prints each node's resolved
    serial and each site's controller count, plus the fabric-wide
    tep_pool/infra_vlan/gipo_pool.
  - Verified N-site behavior: a 3-site scaffold (`aci-sim new --sites 3`)
    builds with zero node-ID collisions and per-site `infraWiNode` counts
    matching the configured controller count. **Documented limit**: ISN
    inter-site peering and stretched-tenant HOME/REMOTE endpoint placement
    still rely on `Topology.other_site()`, which is 2-site-only semantics
    (pre-existing, not introduced by this PR) — with 3+ sites, ISN is not a
    true full mesh. See README §6a / §8 and `docs/DESIGN.md` PR-18 section.
  - New tests: `tests/test_pr18_tier1.py`.

## [0.3.0] - 2026-07-04

### Added
- **`aci-sim` topology CLI (PR-17).** A new console script
  (`aci_sim/cli.py`, `[project.scripts] aci-sim`) that makes
  `topology.yaml` easier to author, validate, preview, and run — purely
  additive: it does not change the topology schema, builders, or NDO/REST
  code, and `topology.yaml` remains the single source of truth. Four
  subcommands:
  - `aci-sim validate [TOPOLOGY]` — runs the same `load_topology()`/Pydantic
    validation the sim already performs at startup, printing a concise
    pass/fail summary (sites, leaves/spines/border-leaves per site, tenant
    count) instead of a raw traceback. This is the CI gate.
  - `aci-sim show [TOPOLOGY]` — prints the **derived** inventory (not just
    an echo of the YAML): per-site APIC host/mgmt IP/ASN/pod/fabric name, a
    table of every node with its derived loopback/OOB IP (via
    `build/fabric.py`'s `loopback_ip`/`oob_ip`), the NDO mgmt IP, and the
    actual host:port each controller will listen on (port mode or sandbox
    mode, read from `runtime/config.py`). `--json` for machine-readable
    output.
  - `aci-sim run [TOPOLOGY]` — a thin wrapper around `python -m
    aci_sim.runtime.supervisor`: validates the topology up front,
    generates self-signed certs on first run (mirrors
    `scripts/gen_certs.sh`), sets `TOPOLOGY_PATH`, passes through
    `SIM_BIND`/`SIM_SANDBOX`/etc. from the environment, and execs the
    supervisor — `scripts/run.sh` is unchanged and keeps working.
  - `aci-sim new` — scaffolds a fresh, minimal, valid `topology.yaml` (to
    stdout or `-o FILE`) from flags (`--sites`, `--leaves-per-site`,
    `--spines-per-site`, `--border-pairs`, `--asn`, `--fabric-name-prefix`,
    `--ndo-ip`, `--apic-ip-base`), using the same node-ID scheme documented
    in `topology/schema.py` (leaves base 101 +200/site, spines base 201
    +200/site, border leaves placed outside both ranges in vPC pairs). The
    generated file always passes `aci-sim validate` and boots.
  - New `tests/test_cli.py` (20 tests): validate positive/negative
    (duplicate node id, bad CIDR), show text + `--json` asserting derived
    IPs for a known node, `new --sites 3 --leaves-per-site 4` round-tripped
    through the real Pydantic model, and `run`'s env/argv construction via
    a pure `build_run_env_argv` helper (no live socket binding in tests).
  - README gets a new "## 5. CLI" section (subsequent sections renumbered
    6-11); `deploy/README.md`/Quickstart note `aci-sim run` as the
    preferred entrypoint alongside `scripts/run.sh`.

## [0.2.7] - 2026-07-04

### Fixed
- **NDO site-local BD duplication — mirror vs site-module dedup (PR-16).**
  `aci_sim/ndo/patch.py::_mirror_template_bd_to_sites` (PR-15)
  auto-creates a site-local BD shadow with a full-path STRING `bdRef` the
  moment a template BD is added. Separately, `mso_schema_site_bd.py`'s
  "BD does not exist yet" branch PATCHes `add /sites/{siteId}-{template}/
  bds/-` (the RFC-6902 *append* form) with a DICT-shaped `bdRef`. The
  append branch of `apply_json_patch` never consulted `_find_by_name`/ref
  resolution before appending, and `_find_by_name`'s own ref-lookup only
  recognized the string ref shape — so the two writers never converged on
  one entry, and `sites[].bds[]` ended up with two rows for the same
  template BD (one empty mirror, one carrying the real subnet) —
  confirmed against a reference NDO schema (`App1-Net-LAB0`, Acme
  App1-Net tenant) after `create_tenant`+`create_bd`.
  Fixed by deduping a **nameless site-local shadow** strictly by its
  single IDENTITY ref (`bds`→`bdRef`, `anps`→`anpRef`, `epgs`→`epgRef`,
  `contracts`→`contractRef`; `_IDENTITY_REF_BY_COLLECTION`), resolved in
  both string and dict shapes via a new `_bare_ref_name()` helper. A new
  `_find_shadow_by_identity()` is the append-path dedup; `_find_by_name`'s
  ref-lookup now fires only for nameless items and only over identity
  refs. A NAMED object (a template-level BD add) is matched only by
  name/displayName and always appends.
  IMPORTANT — the first cut of this fix iterated **all** `*Ref` keys
  (including `vrfRef`), which collapsed every template's BDs into a single
  entry because distinct BDs share one `vrfRef` (`vrf-L3_LAB0`); an
  independent full-chain Acme run caught it (`create_bd` failed
  `"Provided BD 'bd-App2-Net-...' does not exist"`). The shipped fix
  restricts ref-matching to identity refs of nameless shadows only —
  guarded by `test_distinct_template_bds_sharing_vrfref_do_not_collapse`.
  Verified end-to-end on real ACI hardware: Acme App1-Net
  `create_tenant.yml`/`create_bd.yml` both `failed=0`; live NDO schema
  `App1-Net-LAB0` shows template BD counts `LAB1-LAB2=6`, `LAB1=14`,
  `LAB2=10` (30, no collapse) and exactly one site-local BD entry per
  template BD (no duplicate). Covers the analogous ANP/contract
  site-mirror paths too — no regression against the PR-12/PR-13 mirror
  suites. 9 tests in `tests/test_pr16_bd_mirror_dedup.py`.

## [0.2.6] - 2026-07-04

### Added
- **Domain-qualified `aaaLogin` name + template-BD site mirroring for
  aci-py (PR-15).** Two confirmed sim gaps from a real `aci-py` (pure-Python
  Ansible-to-ACI compiler) run against the hardware sandbox:
  1. **APIC login rejects a domain-qualified login name.** `aci-py` posts
     `POST /api/aaaLogin.json` with `aaaUser.attributes.name =
     "apic:local\admin"` — a valid real-APIC login format
     (`apic:<loginDomain>\<username>`). The sim's `aaaLogin` handler
     (`aci_sim/rest_aci/auth.py`) exact-matched the raw `name`
     attribute against `SIM_USERNAME`, so every domain-qualified login
     401'd even with the correct password — confirmed live against the
     sandbox (`name="apic:local\admin"` -> 401, `name="admin"` -> 200
     before this fix). Added `_bare_username()`, which strips an optional
     `apic:<domain>\` / `<domain>\` prefix before the credential compare,
     so both the plain and domain-qualified forms authenticate identically;
     a wrong username in either form still 401s.
  2. **NDO schema PATCH 400 mid `create_tenant`/`create_bd`.** `aci-py`'s
     `mso_schema_site_bd` shim always PATCHes a site-local BD shadow with
     `op: replace` (never `add`), because real NDO 4.x auto-creates that
     shadow ("BDDelta") the moment a template-level BD is added to a
     template that already has sites attached — a fresh `add` there 409s
     on real hardware, so the shim does a GET-then-replace instead (its own
     docstring documents this real-hardware behavior). The sim never
     mirrored a template BD `add` into `sites[].bds[]` (unlike ANPs/EPGs
     since PR-12 and contracts since PR-13 — the same "auto-mirror on
     template add" pattern, just missing the BD collection), so the
     site-BD `replace` 400'd with `"replace: 'bd-FW_LAB0' not found at
     '/sites/1-LAB1-LAB2/bds/bd-FW_LAB0'"` — confirmed verbatim against a
     real hardware aci-py MS-TN2 run before this fix. Added
     `_mirror_template_bd_to_sites` (`aci_sim/ndo/patch.py`,
     PATCH-time, parallel to `_mirror_template_anp_to_sites` /
     `_mirror_template_contract_to_sites`) and the equivalent boot-time
     mirror in `build_ndo_model` (`aci_sim/ndo/model.py`), so a
     topology.yaml tenant that ships with BDs already configured also
     boots with the site shadow pre-mirrored.
  - VERDICT: both gaps are genuine sim fidelity gaps, not aci-py bugs —
    aci-py's domain-qualified login name and its GET-then-replace
    site-BD shim both mirror documented real-APIC/real-NDO behavior.
  - Confirmed end-to-end on real ACI hardware: aci-py's `live_chain.py`
    driver against `environments/lab-multisite-sim` now reaches
    **MS-TN2: 13/13 steps rc=0** (was 9 ok / 4 fail before this PR — the 4
    failures were exactly the login 401s and the `replace: 'bd-*' not
    found` 400) and **MS-TN1: 11/11 rc=0** (1 skip, no regression).
- 13 new tests in `tests/test_pr15_acipy_gaps.py` covering both plain and
  domain-qualified (`apic:local\admin` and `local\admin`) aaaLogin forms,
  a domain-qualified wrong-user/wrong-password still-401 check, the
  template-BD-add-mirrors-into-site PATCH behavior (single site, multiple
  sites, idempotency, non-associated-site isolation), the full
  add-then-replace sequence aci-py actually emits, and the boot-time
  mirror for topology.yaml tenants that ship with BDs already configured.

## [0.2.5] - 2026-07-04

### Added
- **Classic ND bare `/login` + `/logout` endpoints (aci-py, cisco.nd)
  (PR-14).** aci-py's NDO connector and the `cisco.nd` httpapi plugin POST
  to the classic Nexus Dashboard bare `/login` (body `{userName,
  userPasswd, domain}`, JWT in `token`/`jwttoken`) alongside the newer
  `/api/v1/auth/login`. The sim only had the latter, so aci-py 404'd on
  every NDO login. Mirrored the existing auth handler at bare `/login` +
  `/logout`.

## [0.2.4] - 2026-07-04

### Added
- **NDO tenant-policy-template DHCP surface + schema serviceGraphs for
  cisco.mso (PR-13).** Two confirmed sim gaps from a real aci-ansible run
  against ACI hardware, both the last NDO write surfaces blocking the multi-site
  (MS) suite:
  1. **Tenant-policy-template CREATE flow.** `prereq_tenantpol.yml`'s
     `cisco.mso.ndo_template` task (`delegate_to: localhost`) 404'd on
     `GET /api/v1/templates/summaries` — a BARE path with no `/mso` prefix.
     Root cause: `delegate_to: localhost` forces `ansible-mso`'s
     `MSOModule` down its direct-HTTP branch instead of the persistent
     `ansible.netcommon.httpapi` connection every other task on the same
     playbook host uses, and that branch never adds the `/mso` prefix.
     Added a full generalized template store (`POST`/`GET`/`PATCH`/`DELETE
     /templates{,/summaries,/objects,/{id}}`, both bare and `/mso`-prefixed)
     so `ndo_template` can create a tenant-policy template that persists,
     `cisco.mso.ndo_dhcp_relay_policy` (`create_dhcp_relay`) can add a DHCP
     relay policy against it (schema-template EPGs now carry a `uuid` field
     for provider refs — previously absent), and
     `cisco.mso.ndo_schema_template_bd_dhcp_policy` (`bind_dhcp_relay_to_bd`)
     can resolve/bind it to a BD via the new `templates/objects?type=
     dhcpRelay&{uuid|name}=...` cross-template lookup route. Confirmed
     end-to-end on a real hardware run: MS-TN1/MS-TN2 `create_dhcp_relay` +
     `bind_dhcp_relay_to_bd` now reach `failed=0`.
  2. **Schema serviceGraphs surface.** The mso-model role's "Create service
     graph" task (`custom_mso_schema_service_graph.py`, a legacy
     pre-`MSOTemplate`/`MSOSchema` module) bare-subscripts
     `schema_obj.get('sites')[site_idx]['serviceGraphs']` — the
     TEMPLATE-level `serviceGraphs` bare-subscript was already covered by
     PR-11, but the SITE-level sibling was not, hitting `KeyError:
     'serviceGraphs'` on a real hardware MS-TN2 `create_tenant` pass-2 run
     (`-e automate_contract_graph=true`). Added `SITE_TOP_LEVEL_DEFAULTS`
     (`aci_sim/ndo/patch.py`) to backfill `serviceGraphs` (and
     `contracts`, see below) on every schema `sites[]` entry; `GET
     /mso/api/v1/schemas` now returns full nested schema detail (matching
     real NDO) instead of a trimmed `{name}`-only summary, since this same
     legacy module bare-subscripts straight into that response; added
     `GET schemas/service-node-types` and a `tenantId` field on every
     schema template (both required by the same module). Chasing this to
     green on real ACI hardware surfaced a DEEPER gap: once the
     `serviceGraphs` KeyError was fixed, the mso-model role's raw
     `cisco.mso.mso_rest`-driven "Atomic PATCH — bind service-graph
     redirect on ALL fabrics" task addresses a SITE-LOCAL contract by bare
     name (`/sites/{siteId}-{t}/contracts/{c}/serviceGraphRelationship`) —
     the sim never mirrored template-level contracts into
     `sites[].contracts[]` at all (unlike ANPs/EPGs, mirrored since PR-12),
     hitting `PatchError: path segment 'con-Firewall_LAB0' not found in
     list`. Added `_mirror_template_contract_to_sites` (PATCH-time) and the
     equivalent boot-time mirror in `build_ndo_model`. Confirmed end-to-end
     on a real hardware run: MS-TN2 `create_tenant` pass-2 (01b) now reaches
     `failed=0`.
- 26 new tests in `tests/test_pr13_ndo_dhcp_svcgraph.py` covering the
  bare/mso-prefixed template routes, the full `ndo_template` create→lookup
  round trip, DHCP relay policy add/read via `templates/objects`,
  `bind_dhcp_relay_to_bd`'s full PATCH sequence, site-level `serviceGraphs`
  normalization, the full schema-detail `GET /schemas` shape, and the
  site-local contracts mirroring + atomic redirect PATCH.

## [0.2.3] - 2026-07-03

### Added
- **NDO site-local ANP/EPG + static-port schema surface.** PR-11 closed the
  schema write round-trip for template-level objects but left the last
  MS-TN1 gap: `bind_epg_to_static_port`
  (`cisco.mso.mso_schema_site_anp_epg_staticport`) crashed client-side with
  `'NoneType' object has no attribute 'details'`. Two compounding root
  causes, both fixed:
  1. Real NDO stores a self-referencing `anpRef` string on every
     template-level ANP object itself (and `epgRef` on every EPG) —
     confirmed because `ansible-mso`'s `mso_schema_template_anp.py`/
     `_anp_epg.py` explicitly strip it client-side before an idempotency
     comparison (`if "anpRef" in mso.previous: del mso.previous["anpRef"]`
     — dead code unless the server sends one back). `MSOSchema.
     set_site_anp()`/`set_site_anp_epg()` (`ansible-mso`'s
     `module_utils/schema.py`) key their site-local lookup off exactly
     this field; without it the lookup always compares against `None`.
     `normalize_template()` (`aci_sim/ndo/patch.py`) now backfills
     `anpRef`/`epgRef` on every `anps[]`/`epgs[]` entry (takes an optional
     `schema_id` param to build them); `build_ndo_model()`
     (`aci_sim/ndo/model.py`) calls it at boot too.
  2. Real NDO 4.x auto-populates a schema's site-local
     `sites[].anps[].epgs[]` the moment a template-level ANP/EPG is added
     to a template that already has a site attached — the sim never did
     this mirroring, so `set_site_anp()`/`set_site_anp_epg()` always
     returned `None` and the module's own dead-on-4.x fallback path (its
     own source comment: *"Coverage misses this two conditionals when
     testing on 4.x and above"*) crashed instead. `apply_json_patch` now
     auto-mirrors: `add /templates/{t}/anps/-` creates a matching
     `{anpRef, epgs:[]}` site-anp in every associated site; `add
     /templates/{t}/anps/{a}/epgs/-` creates a matching **full-shaped**
     site-EPG `{epgRef, subnets:[], staticPorts:[], staticLeafs:[],
     domainAssociations:[]}` under it. The full child-collection shape is
     mandatory — every sibling `mso_schema_site_anp_epg_*` module
     bare-subscripts its own array (a missing key is a hard `KeyError`,
     not a 4xx): `_domain` reads `domainAssociations`, `_staticleaf` reads
     `staticLeafs`, `_staticport` reads `staticPorts`, `_subnet` reads
     `subnets`. (An earlier iteration that omitted `domainAssociations`
     regressed `bind_epg_to_physical_domain`/`_vmm_domain` on ACI hardware with
     `KeyError: 'domainAssociations'`.) Single source of truth for the
     shape is `SITE_OBJECT_DEFAULTS["epgs"]`, consumed by `_new_site_epg()`
     (mirror path) and `normalize_site()` (backfill path).
     `build_ndo_model()` performs the same mirroring at boot for
     topology-seeded schemas. Both idempotent.

  `epgRef`'s canonical string form uniquely nests two name segments
  (`/schemas/{id}/templates/{t}/anps/{a}/epgs/{e}`, confirmed against
  `ansible-mso`'s `module_utils/mso.py` `epg_ref()`) — handled by a
  dedicated `_stringify_refs` branch since the existing single-category
  `_REF_NAME_FIELDS` table only fits one name field. `_REF_LOOKUP_KEYS`
  gained `epgRef` so a site-epg (ref-only, no `name` of its own) resolves
  by its bare EPG name the same way site-local bds/anps already did
  (PR-11). Verified end-to-end against a real-hardware multi-site E2E run:
  the full MS-TN1 chain (`create_tenant` → `create_bd` →
  `create_application` → `bind_epg_to_static_port`) now reaches `failed=0`
  on every host, closing the CONTRACT.md §7a "known gap" note. New
  `tests/test_pr12_site_local.py`.

## [0.2.2] - 2026-07-03

### Added
- **NDO schema write round-trip (POST/PATCH).** PR-10's schema surface was
  read-only (a static per-tenant seed plus a POST that stored a schema no
  PATCH could reach). PR-11 makes the schema store fully writable, matching
  the exact request sequence every `cisco.mso` schema-object module
  performs (verified against `ansible-mso`'s `plugins/module_utils/mso.py` +
  `schema.py` and confirmed on a real multi-site aci-ansible E2E run against
  ACI hardware): `GET schemas/list-identity` (displayName→id, already existed) →
  `GET schemas/{id}` (already existed) → `PATCH schemas/{id}` with a
  JSON-Patch-shaped op list (`{"op":"add"/"replace"/"remove","path":...,
  "value":...}`). New `aci_sim/ndo/patch.py` implements a general
  applier (`apply_json_patch`) supporting three path-addressing conventions
  cisco.mso modules use: numeric index / `"-"` append (RFC 6902), a *named*
  segment resolved against a `name`/`displayName` field
  (`/templates/LAB1/bds/bd-App1_LAB1`), a *synthetic composite* key
  `"{siteId}-{templateName}"` unique to the top-level `sites[]` array
  (`/sites/1-LAB1/bds/-` — every `mso_schema_site_*` module builds this
  literally), and a *`*Ref`-name* lookup for site-local child objects that
  carry no `name` of their own (`/sites/1-LAB1/bds/bd-App1_LAB1/subnets/-`
  resolves `bd-App1_LAB1` against the site-local BD's `bdRef` string).
  `normalize_template()`/`normalize_site()` backfill every collection-default
  key (`vrfs`/`bds`/`anps.epgs`/`contracts`/`filters`/`externalEpgs`/
  `intersiteL3outs`/`serviceGraphs`/`subnets`/...) real NDO defaults
  server-side but several `cisco.mso` create/add payloads omit — a missing
  key there is a hard crash client-side (`'NoneType' object is not
  iterable`, bare `KeyError`, or `TypeError: sequence item 0: expected str
  instance, dict found` when a sibling module `", ".join()`s a `*Ref` field
  the sim had stored as a dict instead of NDO's canonical string form
  `/schemas/{id}/templates/{tmpl}/{category}/{name}` — all three confirmed
  on real hardware create_tenant/create_bd runs before the fix).
- **`GET /api/config/class/remoteusers` + `/localusers`.** `cisco.mso.mso_
  tenant`'s tenant-create flow always resolves the tenant's `users` list via
  `lookup_users()`, which on ND platforms falls back to these legacy
  `/api/config/class/*` routes when the modern `/nexus/infra/api/aaa/v4/*`
  endpoints aren't available. The sim only served the modern route's absence
  as a 404, so `nd_request()` had no `"code"`/`"messages"` in the error body
  and aborted with `"ND Error: Unknown error no error code in decoded
  payload"` — verified on a real hardware create_tenant run. Now seeded with a
  local `admin` user in ND's `{"items":[{"spec":{...}}]}` aggregate shape.
- **Bare `GET /api/v1/sites`.** `cisco.mso.ndo_template`'s TenantPol prereq
  lookup hits this un-prefixed path (no `/mso`); the sim only served the
  `/mso/api/v1/sites` form. Same data, added alongside PR-10's bare
  `/api/v1/tenants`.
- **`PUT`/`POST /mso/api/v1/tenants{,/{id}}`.** `cisco.mso.mso_tenant`
  updates a tenant in place (`PUT .../tenants/{id}`) whenever the tenant
  already exists in `GET /mso/api/v1/tenants` — true for every tenant this
  sim seeds from the topology. Previously unimplemented (404).
- **`GET /mso/api/v1/schemas/{id}/validate`, `execute|status/schema/{id}/
  template/{name}`, `POST /mso/api/v1/task`.** The full schema-template
  deploy surface: `cisco.mso.mso_schema_template_deploy` (legacy) validates
  then calls `GET execute|status/schema/.../template/...`; the module
  aci-ansible's `mso-model` role actually invokes,
  `cisco.mso.ndo_schema_template_deploy`, instead validates then `POST
  /mso/api/v1/task` with `{schemaId, templateName, isRedeploy}` — confirmed
  against the collection installed on real ACI hardware (differs from the upstream
  `master`-branch module source). All three were 404 before this PR.

### Fixed
- `GET /mso/api/v1/schemas` / `list-identity` no longer double-lists (or
  silently drops) a schema after it's mutated by `PATCH` — the summary
  projection is now kept in sync in place rather than appended as a
  duplicate "extra" entry.

### Verified against a real-hardware E2E run
MS-TN1 (multi-site tenant) now reaches `failed=0` for `create_tenant`,
`create_bd`, `create_application`, `bind_epg_to_physical_domain`,
`bind_epg_to_vmm_domain`, and `bind_epg_to_secure_application`. Remaining
known gap: `bind_epg_to_static_port` needs site-local ANP/EPG object
creation (`mso_schema_site_anp`/`_anp_epg`), not implemented in this PR —
tracked for PR-12 (see docs/CONTRACT.md §7 for the precise failure
signature).

### Tests
- New `tests/test_pr11_ndo_write.py` (31 tests): `apply_json_patch` op
  semantics (add/replace/remove, index/name/composite-key/ref-name
  resolution, auto-vivification safety), `normalize_template`/
  `normalize_site` collection-default backfilling, `*Ref` dict→string
  normalization, and full schema create→PATCH→GET round-trips through the
  FastAPI routes (including the exact site-local BD + BD-subnet sequence a
  real E2E run exercises).

## [0.2.1] - 2026-07-03

### Fixed
- **`/api/node/mo/{dn}` alias (11 E2E failures).** Real APIC serves
  `/api/node/mo/{dn}.json` as a full equivalent of `/api/mo/{dn}.json` (the
  form the APIC GUI's API Inspector and some `cisco.aci` call paths emit).
  The sim only registered `/api/mo/*`, so an alias request fell through to
  FastAPI's default 404 `{"detail":"Not Found"}` — a shape `cisco.aci`
  cannot parse (surfaces as `"APIC Error None: None"`, `status=-1`). GET,
  POST, and DELETE are now all registered on both paths via stacked route
  decorators sharing one handler, so behavior can never drift between the
  canonical and alias forms.
- **APIC-shaped 400 for unmatched `/api/*` paths.** Any request under
  `/api/*` that doesn't match a real route now returns an APIC error
  envelope (`imdata`/`error`/`code`) instead of FastAPI's `{"detail":...}` —
  real APIC never emits that shape. Implemented as a Starlette 404 exception
  handler scoped to the `/api/*` prefix, so it only fires once routing has
  already failed and can never shadow a real route.
- **NDO site names now use the fabric name, not the short topology name.**
  `GET /mso/api/v1/sites` previously reported the topology's internal short
  site name (`"LAB1"`/`"LAB2"`); real NDO registers sites under their
  fabric name (`"LAB1-IT-ACI"`/`"LAB2-IT-ACI"`), and `cisco.mso.mso_tenant`
  failed with `"Site 'LAB1-IT-ACI' is not a valid site name"` against the
  short form. Verified against a real-gear `cisco.mso.mso_tenant` run.
  `aci_sim/ndo/model.py`'s site builder now exposes
  `Site.fabric_name` (falling back to the short name if unset); internal
  site-id joins (`tenant.sites`, schema site associations) are unaffected
  since they key off the topology's short name, never the NDO-facing
  display name.
- **NDO schema `list-identity` + bare `/api/v1/tenants`.** Added
  `GET /mso/api/v1/schemas/list-identity` — the lightweight
  schema-enumeration endpoint every `cisco.mso` schema module calls first to
  resolve a schema `displayName` to its `id` (verified against
  `ansible-mso`'s `plugins/module_utils/mso.py`:
  `query_objs("schemas/list-identity", key="schemas", displayName=schema)`,
  which reads `json["schemas"][]` entries with at least `id` + `displayName`).
  Declared *before* the parameterized `GET /mso/api/v1/schemas/{schema_id}`
  route so `"list-identity"` is never matched as a schema id (previously
  would 404 with `{"detail":"Schema 'list-identity' not found"}`). Also
  added `GET /api/v1/tenants`, backing `cisco.mso.ndo_template`'s tenant
  prereq lookup, returning the same tenant list as the existing
  `/mso/api/v1/tenants`. Both are read-only additions — the schema/template
  *write* surface (`PATCH` deploy) is an intentional follow-up.
- New regression suite `tests/test_pr10_ansible_gaps.py` (9 tests) covers
  all of the above; `tests/test_ndo.py`'s site-name assertions updated to
  join by fabric name.

## [0.2.0] - 2026-07-03

### Added
- Public-release preparation: rewritten README (purpose, architecture, both
  run modes, configuration reference, error-handling contract, known
  limitations), MIT `LICENSE`, this `CHANGELOG.md`, GitHub Actions CI
  (`ci.yml`), a validated `examples/ansible/` playbook set (smoke +
  teardown), and a project-root `CLAUDE.md` release-kit overlay.

### Changed
- Hygiene pass: removed a real internal hostname/username pair from
  `docs/DESIGN.md`'s batch-2/3 verification note and genericized an example
  path in `deploy/aci-sim.service`; no functional/behavioral change.

## [0.1.12] - 2026-07-03

### Added
- Ansible `cisco.aci` fidelity: `DELETE /api/mo/{dn}.json` (`state=absent`,
  server-side idempotent), `GET` on a nonexistent-but-well-formed DN now
  returns 200-empty instead of 404 (unblocks `state=present` GET-before-write
  playbooks), `status="deleted"` cascade regression coverage,
  `annotation`/`ownerKey`/`ownerTag`/`nameAlias` passthrough regression
  coverage, and certificate-signature auth in accept-mode (claimed-identity
  trust, no signature verification — documented trust-model caveat).

## [0.1.11] - 2026-07-03

### Added
- Default loopback bind (`SIM_BIND`, defaults to `127.0.0.1`) so the
  unauthenticated `/_sim` control plane is never LAN-exposed by accident.
- Linux sandbox-mode support (`ip addr add/del … dev lo`) alongside the
  existing macOS `lo0 alias` path in `sandbox-up.sh`/`sandbox-down.sh`.
- Effective-port environment overrides and topology config validation.
- systemd `--user` unit and launchd plist deployment docs (`deploy/`).

## [0.1.10] - 2026-07-03

### Added
- Mutation-killing numeric-comparison test coverage and minimum-row
  assertions on HTTP fault-path tests, closing gaps a mutation-testing pass
  identified in the query engine's comparison operators.

## [0.1.9] - 2026-07-03

### Fixed
- `/_sim/reload` now rebuilds against the freshly-loaded `Site` object
  (matched by id, falling back to name) instead of a stale pre-reload
  reference, so topology edits actually take effect.
- The `fabricNodeIdentP` node-registration reaction now materializes an
  enriched MO set (fabricNode + topSystem + healthInst + fabricLink/
  lldpAdjEp/cdpAdjEp cabling + port inventory), not just a bare fabricNode.
- `GET /api/node/class/{class}.json` (no DN prefix) now correctly resolves
  as the fabric-wide equivalent of `/api/class/{class}.json`.

## [0.1.8] - 2026-07-03

### Added
- Seeded batch-2/3 contract classes: port-channel/vPC member relations
  (`pcAggrIf`, `pcRsMbrIfs`), transceiver/optics inventory (`ethpmFcot`),
  APIC cluster health (`infraWiNode`), zoning-rule/pcTag plumbing
  (`actrlRule`, `fvEpP`), EVPN VPN routes (`bgpVpnRoute`, `bgpPath`), and
  boot-seeded node registration (`fabricNodeIdentP`). Deliberately did NOT
  build `infraNodeIdentP` (no real consumer; a different concept from
  `fabricNodeIdentP` despite the similar name).

## [0.1.7] - 2026-07-03

### Added
- Seeded batch-1 previously-catalogued-but-unbuilt contract classes:
  access-policy cluster (`infraAccPortP`/`infraHPortS`/`infraPortBlk`/
  `infraRsAccBaseGrp`/`infraAccBndlGrp`), route-control cluster
  (`rtctrlProfile`/`rtctrlCtxP`/`rtctrlSubjP`/`rtctrlMatchRtDest`/
  `rtctrlSetComm`/`rtctrlSetPref`), static routes (`ipRouteP`/`ipNexthopP`),
  RIB/ARP (`uribv4Route`/`uribv4Nexthop`/`arpAdjEp`), `ospfIf`, and static
  bindings (`fvRsPathAtt`, `vzRsSubjGraphAtt`).

## [0.1.6] - 2026-07-03

### Fixed
- Topology-consistency findings: real ports built for L3Out sub-interfaces
  and spine OSPF/ISN uplinks (previously referenced but never built),
  endpoint/APIC port separation on shared leaves, correct multi-site
  endpoint semantics (home-site front-panel attachment vs. remote tunnel-
  learned), BD `unicastRoute` now reflects actual subnet presence, and
  controller health (`healthInst`) seeded for every controller node.

## [0.1.5] - 2026-07-03

### Fixed
- Auth is now enforced end-to-end: token validation, credential checking
  against `SIM_USERNAME`/`SIM_PASSWORD`, session expiry
  (`refreshTimeoutSeconds`), and APIC error envelopes (401/403) on every
  query/write route instead of accepting any request.

## [0.1.4] - 2026-07-03

### Fixed
- Valid IPv4 derivation for node IDs greater than 255 (previous scheme
  overflowed the last octet). Seeded the `userprofile`/`aaaUserDomain`
  login-probe MO so the CONTRACT §1 subtree query resolves.

## [0.1.3] - 2026-07-03

### Fixed
- Write path is now atomic (validate-then-commit): a malformed node
  anywhere in a POST body's tree rejects the whole write with a 400
  envelope and leaves the store untouched. Canonical RN synthesis for
  dn-less children (e.g. `fvBD` → `BD-{name}`) instead of a generic
  `{class}-{name}` guess, avoiding orphan duplicate DNs.

## [0.1.2] - 2026-07-03

### Fixed
- Query engine: subtree-scope filtering now applies `query-target-filter`
  to the whole scoped set (root + descendants) rather than pre-filtering
  the root out of scope; `page`/`page-size` parameter validation (400 on
  non-numeric/out-of-range values instead of silent truncation); stricter
  filter-expression parsing (malformed/truncated expressions → 400 instead
  of a 500 or silent no-op).

## [0.1.1] - 2026-07-02

### Fixed
- `rsp-subtree=full` nesting (recursive children, not one level).
- Comparison filter operators (`gt`/`lt`/`ge`/`le`/`bw`) with numeric-vs-
  lexical comparison semantics.

## [0.1.0] - 2026-07-01

### Added
- Initial project scaffold: MIT store + query engine (P1/P2), topology
  schema + default `topology.yaml`, fabric/tenant builders (P3/P4), the APIC
  REST app + runtime supervisor + `/_sim` control API (P5), the NDO plane
  (P6), and an end-to-end verification harness (P7) — see `docs/CONTRACT.md`
  and `docs/DESIGN.md` for the full design record.
