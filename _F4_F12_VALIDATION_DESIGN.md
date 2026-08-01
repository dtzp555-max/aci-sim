# F4 + F12 — NDO Server-Side Fidelity Enforcements for aci-sim (Design Spec)

Status: DESIGN ONLY (no code in this document). Implementation to follow in a
later phase, by a subsequent agent.
Target repo: `/home/tlab/aci-sim` @ main `fe3d132` (v0.23.0), PI230.
Sibling precedent: `_F10_VALIDATION_DESIGN.md` (same repo, same author context) —
F10 closed APIC *value* validation; F4/F12 close NDO *server-side structural*
validation. This doc reuses F10's fail-safe / zero-false-reject discipline.

Investigation method: static read of the sim source + the campaign's own
`custom_mso_schema_service_graph.py` module, `mso-model` role task file, and the
APIC-side `aci-model` device-creation task. The sim was **not** run (all wire
shapes are authoritative from module source), so no `clean-fabric-20260708`
restore was needed.

---

## 0. Problem statement & golden errors

aci-sim faithfully stores NDO schema JSON and APIC MOs but **does not enforce
the two documented NDO 4.x server-side "validation walls"** that a real NDO
applies when a service graph is bound. Both were empirically confirmed as sim
gaps in the 2026-07-08 campaign; the golden error strings below are real-gear
originals (owner, recorded 2026-06-21):

- **F4 — NDO↔APIC device ordering.** When NDO binds a service graph to a site,
  it validates that the referenced L4-L7 device already exists on the *target
  site's APIC*. Sim gap (confirmed): a phase2-only bind (skip phase1) **succeeds**
  in the sim while the APIC `vnsLDevVip` set is empty. Real gear:
  `Service graph device <dev> does not exist in tenant <tenant> in Fabric <site>`
  (HTTP 400).

- **F12 — uniform per-fabric redirect atomicity.** A per-fabric redirect
  (`serviceGraphRelationship` on a site-local contract) must be configured on
  **all** fabrics; NDO validates the **final post-request state**. A single-fabric
  write → 400; one atomic PATCH covering every fabric in one request → 204.
  Real gear: `must have uniform redirect policy configured on all fabrics`
  (HTTP 400).

The contract: the sim must **fail the way real gear fails** for the *illegal*
sequences, while the *legal two-stage flow stays green* (§4 is the load-bearing
part of this design).

---

## 1. Ground truth — the NDO service-graph write path

### 1.1 Where the service-graph writes land (both are `PATCH /mso/api/v1/schemas/{id}`)

Both F4 and F12 arrive as JSON-Patch op lists on the **schema PATCH** route:

- Route: `aci_sim/ndo/app.py::patch_schema` (decorated `@app.patch(
  "/mso/api/v1/schemas/{schema_id}")`, ~L726). It reads `detail =
  state.schema_details[schema_id]`, then `apply_json_patch(detail, ops)`
  (~L751-754) inside a `try/except PatchError → HTTPException(400)`.
- Applier: `aci_sim/ndo/patch.py::apply_json_patch(doc, ops)` (~L752), pure and
  HTTP-free; `PatchError` (~L82) is the 400 signal.

Neither F4 nor F12 belongs at **deploy** time (`POST /mso/api/v1/task` →
`deploy_mirror.mirror_template_to_sites`). Real NDO rejects at **build/save**
time — i.e. exactly when the service-graph relationship PATCH is written. So the
mount is the **schema PATCH path**, not the deploy mirror.

### 1.2 Cross-plane wiring already exists (this is what makes F4 possible)

`make_ndo_app(state, apic_states)` (`ndo/app.py:23`) already receives
`apic_states: dict[str, ApicSiteState]` from `runtime/supervisor.py` (L71-93):

```
apic_states[site.id] = ApicSiteState(name=..., site=..., topo=..., store=store, baseline=...)
configs.append(_cfg(make_ndo_app(ndo_state, apic_states), ndo_host, ndo_port))
```

- Keyed by `site.id`; each value exposes `.store` — a live `MITStore`.
- Today only `POST /mso/api/v1/task` uses it (deploy mirror). **`patch_schema`
  can read the same `apic_states` closure variable** — no signature change, no
  new plumbing. This is the enabling fact for F4's cross-plane device lookup.
- `deploy_mirror` accesses stores with `apic_states.get(str(site_id))` — F4 must
  use the same `str()` coercion when keys were populated from `site.id`.

`MITStore` query surface (`mit/store.py`): `get(dn) -> MO|None` (L101),
`by_class(cls) -> list[MO]` (L158). F4 uses `get(device_dn)` (exact-DN, O(1)).

### 1.3 F4 device-binding request shape (authoritative — from module source)

The "Create service graph" task
(`roles/client_A/mso-model/tasks/template_object.yml:474`) runs
`custom_mso_schema_service_graph` with `device: {name, tenant}` and `site`, gated
`when: deploy_service_graph|default(true)|bool`. That module
(`roles/client_A/mso-model/library/custom_mso_schema_service_graph.py`) builds:

```
device_dn = 'uni/tn-{0}/lDevVip-{1}'.format(tenant, device['name'])   # L252
```

and emits (among others) a **site-local** op (L318-342):

```json
{ "op": "add",
  "path": "/sites/{siteId}-{templateName}/serviceGraphs/-",
  "value": { "serviceGraphRef": {...},
             "serviceNodes": [ { "name": "...",
                                 "device": { "dn": "uni/tn-<tenant>/lDevVip-<dev>" } } ] } }
```

(It also emits template-level `/templates/{tmpl}/serviceGraphs/...` ops carrying
the same `device.dn`, L296 — but the **site-local** op is the one that names the
target fabric via `{siteId}` in the path, so F4 keys on it.) `device.dn` is a
plain field, not a `*Ref`, so `_stringify_refs` (patch.py L570) passes it through
untouched.

The APIC-side device it references is created earlier by the `aci-model` play
(`roles/client_A/aci-model/tasks/tenant.yml:510`) as a raw `aci_rest` POST to
`/api/node/mo/uni/tn-<tenant>/lDevVip-<dev>.json` (`vnsLDevVip`). The sim's APIC
write path (`rest_aci/writes.py::apply`) honors the URL DN
(`effective_dn = attrs.get("dn") or dn`, L571), so the device lands in the site
store at exactly `uni/tn-<tenant>/lDevVip-<dev>` — i.e. `store.get(device_dn)`
resolves it, and `mo.class_name == "vnsLDevVip"`.

> The sim has **no special `vnsLDevVip`/redirect model** today
> (`grep -ri lDevVip aci_sim/` → nothing in the store layer). F4 does not need
> one: it treats the device as an opaque generically-stored MO and only asks
> "does this DN exist in that site's store?".

### 1.4 F12 atomic-redirect request shape (authoritative — from role source)

The "Atomic PATCH — bind service-graph redirect on ALL fabrics in one request"
task (`template_object.yml:665`) sends, via `cisco.mso.mso_rest`,
`PATCH /mso/api/v1/schemas/{id}` with `content = _sg_ops` — a **list with one op
per fabric** (assembled at L632-659), each:

```json
{ "op": "add",
  "path": "/sites/{siteId}-{templateName}/contracts/{contract}/serviceGraphRelationship",
  "value": {
    "serviceGraphRef": { "schemaId":..., "serviceGraphName":..., "templateName":... },
    "serviceNodesRelationship": [ {
      "serviceNodeRef": {...},
      "consumerConnector": { "clusterInterface": {"dn": _lif_c}, "redirectPolicy": {"dn": _rp_c}, "subnets": [] },
      "providerConnector": { "clusterInterface": {"dn": _lif_p}, "redirectPolicy": {"dn": _rp_p} }
    } ] } }
```

Key facts driving the F12 design:

- Each op writes `sites[i].contracts[C].serviceGraphRelationship` for **one**
  fabric. The redirect-policy DNs (`_rp_c`/`_rp_p`) are **per-fabric** (built from
  `item.schema_contract_service_graph_site_consumer_redirect_policy`, a
  site-scoped var) — so "uniform" means **presence on every fabric, NOT identical
  DNs across fabrics**. The values legitimately differ per site.
- The role author's own comment (L593-598) states the real behavior verbatim:
  *"NDO rejects a per-fabric serviceGraphRelationship write until ALL fabrics are
  uniform, so the per-site module (one fabric per call) cannot do it… PATCH every
  fabric's redirect in ONE request (NDO validates the uniform final state)."*
  This is the exact validation F12 emulates and the reason it must be checked on
  **post-request final state**, not per-op.
- The site-local `contracts[]` shadow each op addresses already exists — it is
  mirrored at model-build (`ndo/model.py:351-354`) and on runtime template-contract
  add (`patch.py::_mirror_template_contract_to_sites`, L729). So the op resolves;
  the sim's *current* gap is that it happily accepts a **partial** (single-fabric)
  set.

### 1.5 The phasing that MUST keep passing (regression contract)

| Phase / flag | "Create service graph" (F4 trigger) | atomic redirect PATCH (F12 trigger) | APIC `vnsLDevVip` present? |
|---|---|---|---|
| phase1 `deploy_service_graph=false` | **skipped** (`when` gate) | skipped | created here (APIC `aci-model` play) |
| phase2 `deploy_service_graph=true` + `automate_contract_graph=true` + `automate_site_redirect=true` | runs → device bind PATCH | runs → **all fabrics in one PATCH** | yes (phase1 made it) |
| **phase2-only** (skip phase1) | runs → device bind PATCH | — | **no** → F4 **400** (correct) |
| **single-fabric redirect** (manual/partial) | — | one fabric only | — → F12 **400** (correct) |

The campaign's **MS-TN2 two-stage `create_tenant` is PASS** and must stay PASS:
phase1 builds the device; phase2's atomic PATCH covers both fabrics uniformly.

---

## 2. F4 design — device-existence gate at service-graph bind

### 2.1 Mount point

`aci_sim/ndo/app.py::patch_schema` (~L751), immediately around the existing
`apply_json_patch(detail, ops)` call. New helper (recommended location: a small
new module `aci_sim/ndo/service_graph_validation.py`, or appended to
`ndo/patch.py`):

```
_validate_service_graph_device_refs(ops, apic_states, state) -> None   # raises ServiceGraphValidationError
```

`patch_schema` has `apic_states` and `state` in scope (closure) — no signature
change. Keep the pure `apply_json_patch` **pure**: F4/F12 live in the route
handler wrapper, not inside the shared applier (the template PATCH route,
`_patch_template`, reuses `apply_json_patch` and has no service graphs — do not
burden it).

### 2.2 Timing — pre-scan of ops (all-or-nothing for free)

F4 is a **pure reference check**: device existence in the APIC store does not
depend on the schema mutation. So it runs as a **pre-scan of `ops` BEFORE**
`apply_json_patch` mutates `detail`. Raising before mutation is naturally
all-or-nothing — no copy needed for F4. (This aligns with real NDO validating at
build time.)

### 2.3 Algorithm

For each op with `op ∈ {add, replace}` whose `path` contains `/serviceGraphs`
**and** is site-scoped (starts `/sites/`):

1. Extract the site path segment `seg = path.split("/")[2]` (the
   `{siteId}-{templateName}` composite; `templateName` may contain `-`, so do NOT
   split on `-`).
2. Resolve the site entry from the *current* `detail["sites"]` by the SAME
   composite `_find_by_name` uses (`f"{s['siteId']}-{s['templateName']}" == seg`);
   read `site_id = str(site_entry["siteId"])`. Fabric display name for the message
   = `state.sites[…].name` where `id == site_id` (this is the NDO fabric name,
   e.g. `"LAB1-IT-ACI"`), falling back to `site_id`.
3. From the op `value`, collect every `serviceNodes[*].device.dn` (also accept a
   top-level `value["device"]["dn"]` for forward-safety). For each device dn:
   - Parse with `^uni/tn-(?P<tenant>[^/]+)/lDevVip-(?P<dev>.+)$`. If it does not
     match, **skip** (not a device ref we own — fail-safe, never reject).
   - `apic = apic_states.get(site_id)`; if `apic is None` (site not simulated),
     **skip** (can't prove absence — fail-safe).
   - `mo = apic.store.get(dn)`. If `mo is None` **or**
     `mo.class_name != "vnsLDevVip"` → **raise** with the golden message:
     `Service graph device {dev} does not exist in tenant {tenant} in Fabric {fabric_name}`.

`ServiceGraphValidationError` (new, subclass of `Exception`) is caught in
`patch_schema` and mapped to `HTTPException(status_code=400, detail=str(exc))` —
the same 400 envelope `PatchError` already uses on this route.

### 2.4 Why F4 ⊂ referential-integrity, not the whole of it

F4 is the **narrow, golden-backed subset** of reference-integrity: it fires
**only** on `serviceNodes[].device.dn` → `vnsLDevVip`, for which we have (a) a
real-gear golden string, (b) a confirmed sim gap, and (c) a cross-plane oracle
(the APIC store). Generalized reference integrity (any VRF/BD/contract ref) is
**F11 and out of scope** — real APIC tolerates many dangling refs (materialized as
faults, not 400s), and without golden data + real-gear diffing a broad
"must-exist ⇒ 400" would over-reject legal out-of-order pushes. See §5.

---

## 3. F12 design — uniform per-fabric redirect gate

### 3.1 Mount point & timing — post-request state (needs apply-then-validate)

F12 is a **post-request final-state** check: after all ops in the PATCH apply,
the *set of fabrics that carry a redirect* for a touched contract must equal the
*full set of the template's fabrics*. It **cannot** be per-op (mid-atomic-PATCH,
after op 1 of N, only fabric 1 is set → a per-op check would 400 the legal atomic
case). So F12 must run **after** `apply_json_patch`, and to keep the 400 path
side-effect-free it uses **copy-validate-commit**.

### 3.2 Recommended handler shape (gated copy-validate-commit)

Only engage the copy path when the PATCH actually touches service-graph surface —
otherwise leave today's in-place fast path **byte-identical** (zero behavior
change for the ~95 % of PATCHes that are BD/EPG/subnet):

```
sg_relevant = any(_touches_service_graph(o) for o in ops)   # path has /serviceGraphs
                                                            # OR ends /serviceGraphRelationship
if not sg_relevant:
    apply_json_patch(detail, ops)          # UNCHANGED current path
else:
    _validate_service_graph_device_refs(ops, apic_states, state)   # F4 pre-scan
    working = copy.deepcopy(detail)
    apply_json_patch(working, ops)
    _validate_uniform_site_redirect(working, ops)                  # F12 post-state
    state.schema_details[schema_id] = working                      # commit atomically
    detail = working
# (normalize_* / summary sync continue on the committed doc, unchanged)
```

Bonus: this also fixes a **latent partial-write bug** — today a `PatchError` on op
`k` of a multi-op batch leaves ops `0..k-1` already mutated into the live stored
`detail`. The copy path makes service-graph PATCHes truly all-or-nothing. (Schemas
are one-tenant-sized; a deepcopy per service-graph PATCH is microseconds — see
§6.)

### 3.3 Algorithm (`_validate_uniform_site_redirect(working, ops)`)

1. From `ops`, collect the set of **touched contracts**: for each op whose path
   matches `^/sites/[^/]+/contracts/(?P<c>[^/]+)/serviceGraphRelationship$`
   **and** whose `value` carries at least one
   `serviceNodesRelationship[*].{consumerConnector|providerConnector}.redirectPolicy`,
   record the bare contract name `c`. (Template-level graph binds —
   `/templates/.../contracts/...` with no redirect — do **not** match, so the
   template-level "Bind Service Graph to Contract" task never trips F12.)
2. For each touched contract `c`:
   - Group the schema's site entries by template: `sites_of_tmpl = [s for s in
     working["sites"] if _basename-of any s.contracts[].contractRef == c]`,
     partitioned by `s["templateName"]`.
   - For each template group `G` (all fabrics that carry contract `c`):
     - `configured = { s for s in G if the site's contract-`c` shadow has a
       `serviceGraphRelationship` whose serviceNodesRelationship carries a
       redirectPolicy on a connector }`.
     - If `0 < len(configured) < len(G)` → **not uniform** → **raise**
       `ServiceGraphValidationError("must have uniform redirect policy configured
       on all fabrics")`.
     - `len(configured) == len(G)` (atomic all-fabric) → pass.
       `len(configured) == 0` → nothing was actually written for this template →
       pass (no-op; can't happen for a touched contract but keep it total).
3. Scope note: **only contracts touched by this request** are checked (not the
   whole schema), so F12 never retro-rejects pre-existing state or unrelated
   contracts — it precisely catches "this write left the fabric set non-uniform".

### 3.4 "Uniform" = coverage, not value-equality (load-bearing)

The redirect-policy DNs differ per fabric by design (§1.4). F12 therefore checks
**presence/coverage** of a redirectPolicy across the template's fabrics, never DN
equality. A stricter "all DNs identical" rule would **falsely reject** the very
atomic PATCH we must accept — explicitly rejected here.

Single-fabric **topologies** (the SF campaign tenants) have `len(G) == 1`; a
single-fabric write then satisfies `configured == G` → **pass**. F12 only bites a
**partial write in a genuinely multi-fabric template**. This is the key
regression guarantee for single-fabric tenants.

---

## 4. Regression / false-reject safety (the load-bearing argument)

The design's prime directive (inherited from F10 §3.6): **never reject a legal
value/sequence.** Every branch above is **fail-safe / default-allow** — it only
raises on a *proven* violation with a golden string; every ambiguity (unparsable
DN, unsimulated site, missing key, empty op set) **passes through**.

Why the legal two-stage flow is NOT caught:

- **F4 vs legal phase2.** In phase2 the device DN was materialized on the site's
  APIC in phase1 (`aci-model` play POSTs `vnsLDevVip`). `store.get(device_dn)`
  therefore returns a `vnsLDevVip` MO → F4 passes. F4 only raises when the device
  is genuinely absent (phase2-only / skip-phase1) — the confirmed gap.
- **F12 vs legal atomic redirect.** The atomic PATCH carries one op per fabric in
  **one request**; after apply, `configured == G` for the touched contract →
  uniform → pass. F12 only raises on a *partial* set
  (`0 < configured < G`) — the single-fabric write real gear also 400s.
- **Template-level binds untouched.** F12's trigger requires a **site-local**
  `serviceGraphRelationship` **carrying a redirectPolicy**; the template-level
  "Bind Service Graph to Contract" task (no redirect, `/templates/...`) never
  matches.
- **Non-service-graph PATCHes untouched.** The `sg_relevant` gate leaves every
  BD/EPG/subnet/contract-filter PATCH on the current in-place fast path,
  byte-identical.
- **Single-fabric tenants untouched.** `len(G) == 1` ⇒ any write is uniform.

### 4.1 E2E regression scenarios the implementer must run (evidence gate)

| Scenario | How to drive | Expected | Proves |
|---|---|---|---|
| **MS-TN2 two-stage `create_tenant`** (phase1 `deploy_service_graph=false` → phase2 all three `automate_*=true`) | replay the campaign's MS-TN2 pass | **all green** (F4 pass, F12 pass) | no false-reject of the canonical passing flow |
| **phase2-only** (run only phase2, skip phase1 device) | run "Create service graph" with no prior APIC `vnsLDevVip` | **HTTP 400**, golden F4 string with real dev/tenant/fabric | F4 catches the confirmed gap |
| **single-fabric redirect** | craft a PATCH with the redirect op for **one** of a 2-fabric template's sites only | **HTTP 400**, golden F12 string | F12 catches the partial write |
| **SF single-fabric tenant** (whole `sf` campaign) | replay SF create_tenant | **all green** | F12 does not bite legit 1-fabric topologies |
| any BD/EPG/subnet PATCH | existing `test_pr11_ndo_write` / `test_pr13_ndo_dhcp_svcgraph` | **unchanged** | fast-path untouched |

The existing suite to keep green: `tests/test_ndo.py`, `test_pr11_ndo_write.py`,
`test_pr13_ndo_dhcp_svcgraph.py`, `test_deploy_mirror.py`,
`test_f10_ndo_validation.py`, `test_ndo_delete.py`.

---

## 5. F11 (generalized referential integrity) — explicitly out of scope

F4 is the golden-backed **narrow subset** of "referential integrity": device-ref
→ `vnsLDevVip`, one class, one golden string, one cross-plane oracle, one
confirmed gap. **Do NOT** in this round generalize to arbitrary
VRF/BD/contract/domain ref existence:

- Real APIC/NDO **tolerates** many dangling references — it materializes a
  **fault** (or a `formed`/`missing-target` relation), **not** a 400 — so a naive
  "referenced object must exist ⇒ 400" would **over-reject** legitimate
  out-of-order pushes (a ref pointing at an object created later in the same or a
  later request).
- There is **no golden data** and **no real-gear diff** for the general case, so we
  can't separate "must-400" from "tolerated-dangling".

F11 is a separate design with its own canonical-vs-real fidelity study (mirrors
F10 §4's deferral of referential integrity). This doc enforces **only** F4's
proven slice.

---

## 6. Batching, effort, performance, risk

### 6.1 Batching (IDR / 第十一律 — one reviewable unit per enforcement)

- **Batch A — F4** (device-existence pre-scan): new
  `_validate_service_graph_device_refs` + `ServiceGraphValidationError` + wire into
  `patch_schema` (pre-`apply_json_patch`). Self-contained; no copy path needed.
- **Batch B — F12** (uniform redirect post-state): `_validate_uniform_site_redirect`
  + the gated copy-validate-commit in `patch_schema`. Depends on Batch A only for
  the shared error type / `sg_relevant` gate.
- Ship A first (smaller, no atomicity refactor); B second.

### 6.2 Effort estimate

| Item | Estimate |
|---|---|
| `ndo/service_graph_validation.py` (F4 + F12 helpers + error type) | ~120-170 lines |
| `patch_schema` wiring (gate + pre-scan + copy-commit + except→400) | ~20-30 lines changed |
| Tests (`tests/test_f4_service_graph_device.py`, `tests/test_f12_uniform_redirect.py`) | ~200-300 lines: F4 present/absent/unsimulated-site/unparsable-dn; F12 atomic-pass / single-fabric-400 / single-site-topology-pass / template-level-no-fire; + a 2-store cross-plane fixture | 
| **Total** | **~1 new module + 1 focused handler edit + tests; ~1 implementation session** |

The tests need a **cross-plane fixture**: two `ApicSiteState`s (with stores) +
an `NdoState`, wired exactly as `supervisor.py` does, so `apic_states[site_id].store`
is populated. `test_deploy_mirror.py` already builds this shape — reuse it.

### 6.3 Performance

- F4: one regex + one `dict.get` per device-ref op; a PATCH carries a handful of
  service-graph ops. Negligible.
- F12: `deepcopy` of one tenant's schema (small) **only** on service-graph
  PATCHes (gated); the uniform scan is O(sites × contracts-touched). Microseconds.
  No concern.

### 6.4 Risk register

| Risk | Severity | Mitigation |
|---|---|---|
| **False-reject the legal two-stage flow (break MS-TN2)** | High | Fail-safe default-allow on every ambiguity; §4.1 MS-TN2 replay is a hard gate before merge; F4 only fires on absent `vnsLDevVip`, F12 only on `0<configured<G` |
| **F12 mis-reads "uniform" as value-equality** → rejects atomic PATCH | High | §3.4: coverage-only, never DN equality (redirect DNs are per-fabric by design) |
| **Single-fabric tenants wrongly hit** | Med | `len(G)==1` ⇒ uniform ⇒ pass; SF campaign in §4.1 |
| **siteId/template composite parsing (template names contain `-`)** | Med | Never split on `-`; resolve the site entry by whole-composite match against `sites[].{siteId,templateName}` (same as `_find_by_name`) and read `siteId` from the entry |
| **`apic_states` key type mismatch (int vs str)** | Med | Use `apic_states.get(str(site_id))`, matching `deploy_mirror`'s existing coercion |
| **Copy-validate-commit changes an unrelated PATCH path** | Med | Gate on `sg_relevant`; non-service-graph PATCHes stay byte-identical on the in-place path |
| **Latent partial-write on multi-op PatchError** | Low (pre-existing) | Copy path incidentally fixes it for service-graph PATCHes; broader fix is optional/out-of-scope |
| **Over-reach into F11 general referential integrity** | Med | Hard-scoped to `vnsLDevVip` device refs (§5); everything else default-allows |

---

## 7. Appendix — key file/function references (for the implementer)

- **Mount (both):** `aci_sim/ndo/app.py::patch_schema` (~L726; `apply_json_patch`
  call ~L751-754; `apic_states` + `state` in closure via `make_ndo_app`, L23).
- **Applier (keep pure):** `aci_sim/ndo/patch.py::apply_json_patch` (L752),
  `PatchError` (L82), `_resolve_container`/`_find_by_name` (composite
  `{siteId}-{templateName}` matching, L397/L493), `_stringify_refs` (leaves
  `device.dn` untouched, L570).
- **Cross-plane stores:** `runtime/supervisor.py` L71-93 (`apic_states[site.id] =
  ApicSiteState(store=…)`); `mit/store.py::get` (L101) / `by_class` (L158);
  `rest_aci/writes.py::apply` (device lands at URL DN, L571).
- **NDO fabric names for the F4 message:** `NdoState.sites` = `[{id, name, …}]`
  (`ndo/model.py:122-135`; `name` = fabric_name e.g. `"LAB1-IT-ACI"`).
- **F4 request source of truth:** `roles/client_A/mso-model/library/
  custom_mso_schema_service_graph.py` (`device_dn` L252; site-local op L318-342;
  `PATCH schemas/{id}` L347); task `roles/client_A/mso-model/tasks/
  template_object.yml:474` (`when: deploy_service_graph`).
- **F12 request source of truth:** `roles/client_A/mso-model/tasks/
  template_object.yml:632-676` (`_sg_ops` assembly + atomic `mso_rest` PATCH;
  per-fabric redirect DNs; "uniform final state" comment L593-598).
- **APIC device creation (phase1):** `roles/client_A/aci-model/tasks/
  tenant.yml:510-513` (`vnsLDevVip` at `uni/tn-<t>/lDevVip-<dev>`);
  redirect policy `svcRedirectPol-<name>` L376+.
- **Site-local contract shadow (F12 reads it):** mirrored at `ndo/model.py:351-354`
  and `patch.py::_mirror_template_contract_to_sites` (L729).
- **Golden strings (owner 2026-06-21):**
  F4 `Service graph device <dev> does not exist in tenant <tenant> in Fabric <site>`;
  F12 `must have uniform redirect policy configured on all fabrics`. Both HTTP 400.
- **Tests to keep green:** `tests/test_ndo.py`, `test_pr11_ndo_write.py`,
  `test_pr13_ndo_dhcp_svcgraph.py`, `test_deploy_mirror.py`,
  `test_f10_ndo_validation.py`, `test_ndo_delete.py`.
