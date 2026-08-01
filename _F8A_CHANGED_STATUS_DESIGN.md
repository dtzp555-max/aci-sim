# F8a — Core Write-Path Fidelity: `created` / `modified` / no-change status

**Status:** DESIGN ONLY (no code changed). Implementation to follow in a later agent.
**Repo:** `~/aci-sim` @ `694454d` (v0.24.0), main, clean (only untracked design docs).
**Author:** design agent, 2026-07-10. Untracked — do NOT commit.

---

## 0. TL;DR

The sim's APIC POST handler **unconditionally** stamps `status:"created"` on the
one top-level MO it echoes, regardless of whether the POST actually changed
anything in the store. `cisco.aci.aci_rest` derives `changed` **solely** from
that response (deep-scan for any `status ∈ {created,modified,deleted}`), so every
`aci_rest`-driven write reports `changed=true` forever — breaking idempotency
across the 7 `aci-model` role task files (`tenant`, `mgmt_tenant`, `infra_tenant`,
`access`, `fabric`, `system_settings`, `admin`).

**Fix:** at the write choke point (`writes._upsert_recursive`, which already calls
`store.get(dn)` per MO for the defaults overlay), diff each planned MO's
**user-posted attributes** against the pre-existing stored MO and label it
`created` (dn absent) / `modified` (posted attr differs) / *unchanged* (all posted
attrs equal). Build the response `imdata` from only the changed MOs; emit **empty
imdata** when nothing changed. `changed()`'s deep recursive scan then returns
`false` on a no-op re-push — real-APIC idempotency.

**Blast radius is narrower than it first appears:** the higher-level cisco.aci
modules (`aci_tenant`, `aci_bd`, `aci_ap`, `aci_epg`, …) do **not** depend on
response status — they diff client-side via `get_existing()` + `get_diff()` and
skip the POST entirely when unchanged (`post_config` early-returns on empty
`self.config`). F8a only moves the needle for **raw `aci_rest` pushes** and raw
curl.

---

## 1. Current state — how the sim emits `status`

### 1.1 `writes.apply()` — the status is a hard-coded constant

`aci_sim/rest_aci/writes.py :: apply()` (tail):

```python
status = "deleted" if attrs.get("status") == "deleted" else "created"
result = [{cls: {"attributes": {"dn": effective_dn, "status": status}}}]
return result, 1
```

- The response is **always exactly one flat entry** — the top-level POSTed class only.
  Nested children that were written to the store are **not** echoed.
- Only `dn` + `status` are echoed (no other attributes).
- `status` is `created` for every non-delete write. There is **no comparison**
  against the pre-existing store state anywhere in this function.

### 1.2 The write pipeline (choke point already exists)

`apply()` → `_upsert_recursive(store, cls, attrs, children)`:

1. `_validate_node(...)` — shape validation (400 on malformed structure).
2. `_plan_recursive(...)` → flat ordered `planned: list[(cls, attrs)]`
   (parent-first; child DNs derived via `_child_dn` / `_RN_TEMPLATES`).
3. `_validate_planned(planned)` — **F10** value validation (400 on bad values),
   runs **before any store mutation** → zero side effects on reject.
4. **Mutation loop** — the natural F8a choke point, because it *already* reads the
   pre-existing MO for the create-only defaults overlay:

```python
for mo_cls, mo_attrs in planned:
    dn = mo_attrs.get("dn")
    if mo_attrs.get("status") != "deleted" and store.get(dn) is None:
        mo_attrs = {**_CLASS_DEFAULTS.get(mo_cls, {}), **mo_attrs}   # create-only defaults
    store.upsert(MO(mo_cls, **mo_attrs))
```

`store.get(dn)` here is the exact pre-existing lookup F8a needs. F8a slots the
diff in **right here**, before `store.upsert`, using the **raw posted** `mo_attrs`
(not the defaults-merged copy).

Reactions (`fabricNodeIdentP` node materialization, `bgpPeerP` session
materialization) run in `apply()` *after* `_upsert_recursive` returns and upsert
**sim-internal** MOs that were never in the response — correctly excluded from the
changed set (they are not in `planned`). This matches real APIC, where a
`fabricNodeIdentP` POST returns only the ident policy; node discovery is async and
not echoed.

### 1.3 `post_mo()` — the `pre_existing` flag is for **subscriptions only**

`aci_sim/rest_aci/app.py :: post_mo()`:

```python
effective_dn = (body[cls].get("attributes") or {}).get("dn") or dn
pre_existing = state.store.get(effective_dn) is not None   # <-- pre-write existence check
...
imdata, total = write_apply(state.store, dn, body, topo=..., site=...)
# push-on-change loop: created vs modified derived from pre_existing, PER PUSH EVENT
for entry in imdata:
    ...
    elif mo_dn == effective_dn and pre_existing:
        push_status = "modified"
    else:
        push_status = "created"
    subs.notify(mo_cls, mo_dn, push_status, mo_attrs)
return _apic_ok(imdata, total)
```

**Clarification of "the agent mentioned a `pre_existing`":** `pre_existing` is a
top-level-DN-only existence check computed **purely for the websocket push event**
(created-vs-modified in the subscription notification). It is **not** used in the
HTTP response body — `_apic_ok(imdata, total)` returns `write_apply`'s unmodified
result. So the push path *already* distinguishes created/modified for the root DN,
but the HTTP response does not, and neither path detects "no change".

### 1.4 Store semantics relevant to the diff

`aci_sim/mit/store.py` / `mo.py`:

- `store.get(dn) -> MO | None`. `MO.attrs` is a plain `dict[str,str]`; **no `__eq__`**
  on `MO` — equality must be done on `.attrs`.
- `store.upsert(MO)` merges via `existing.attrs.update(mo.attrs)` (last-writer-wins,
  recurses children). Merge is a superset update — it never removes keys, so
  stored attrs accumulate posted keys + create-time defaults.
- The store keeps values **verbatim** as posted — the sim performs **no
  normalization** of attribute values. Consequence: a byte-identical re-push always
  produces stored == posted for every posted key (this is what makes an exact-bytes
  diff reliable).

### 1.5 Live evidence (in-process TestClient, zero impact on the running shared sim)

POST `uni/tn-F8AProbe` (fvTenant + child fvBD) three times with
`?rsp-subtree=modified` (the query string `aci_rest` always appends):

```
POST#1 (create)    imdata=[{"fvTenant":{"attributes":{"dn":"uni/tn-F8AProbe","status":"created"}}}]  changed()=True
POST#2 (identical) imdata=[{"fvTenant":{"attributes":{"dn":"uni/tn-F8AProbe","status":"created"}}}]  changed()=True   <-- BUG
POST#3 (descr chg) imdata=[{"fvTenant":{"attributes":{"dn":"uni/tn-F8AProbe","status":"created"}}}]  changed()=True   <-- right verdict, WRONG label (should be modified)
```

Confirms: (a) identical re-push must be empty/unchanged but is `created`;
(b) genuine modify must be labelled `modified` but is `created`; (c) the child
`fvBD` is absent from the response entirely (flat, single-entry, dn+status only).

---

## 2. `cisco.aci.aci_rest` — request shape and `changed()` scan (GROUND TRUTH)

Source read: `.../cisco/aci/plugins/modules/aci_rest.py` +
`.../module_utils/aci.py`.

### 2.1 Does `aci_rest` always send `?rsp-subtree=modified`? — **YES (for POST, by default)**

```python
if aci.params.get("method") != "get" and not rsp_subtree_preserve:
    aci.path = "{0}?rsp-subtree=modified".format(aci.path)
    aci.url  = update_qsl(aci.url, {"rsp-subtree": "modified"})
```

- For **any non-GET** method (POST *and* DELETE), `rsp-subtree=modified` is
  appended **unconditionally**, unless `rsp_subtree_preserve=true` (module option,
  **default `false`**). All 7 `aci-model` role tasks use the default → **every POST
  carries `?rsp-subtree=modified`.**
- `rsp_subtree_preserve=true` is the only way to suppress it (advanced/rare; none of
  the affected roles use it).

### 2.2 What does `changed()` scan? — **recursive DEEP scan for any `status` value**

```python
def changed(self, d):
    if isinstance(d, dict):
        for k, v in d.items():
            if k == "status" and v in ("created", "modified", "deleted"):
                return True
            elif self.changed(v) is True:
                return True
    elif isinstance(d, list):
        for i in d:
            if self.changed(i) is True:
                return True
    return False
```

Called as `self.result["changed"] = self.changed(self.imdata)` — **only on HTTP
200** (any non-200 is `fail_json`, never reaches here).

**Implications for the response shape:**

- The scan is **shape-agnostic** for the boolean verdict: it walks the whole
  `imdata` tree (lists, dicts, nested `children` lists) and returns `True` the
  moment it finds **any** dict key `"status"` whose value is in
  `{created,modified,deleted}` at **any depth**.
- Therefore, to make `changed=false`, the response must contain **no**
  `status ∈ {created,modified,deleted}` anywhere → **empty `imdata`** is the
  simplest guaranteed-false shape.
- To make `changed=true`, it is sufficient to place **one** changed MO carrying
  `status:created|modified` anywhere in `imdata` — flat OR nested both work. This
  is why the exact nesting is **not** load-bearing for the idempotency goal (only
  for fidelity to real APIC's wire shape).

### 2.3 Higher-level modules do NOT use this path (scoping)

`module_utils/aci.py :: post_config()` only issues the POST when `self.config`
(the client-computed proposed-vs-existing diff from `get_diff` after
`get_existing()`'s GET) is non-empty; otherwise it early-returns and `changed`
stays `false`. So `aci_tenant`/`aci_bd`/`aci_ap`/`aci_epg`/… idempotency is
**independent of the sim's response status** and already correct against the sim
(their GET reads the store; equal proposed ⇒ no POST, no change). **F8a's bug and
fix are confined to `aci_rest` (and raw curl).**

---

## 3. Real-APIC response semantics (evidence grading)

Legend: **[E]** real-hardware evidence / upstream-source evidence · **[A]** reasoned
approximation (no real-APIC capture available in this environment).

- **[E]** `aci_rest` docstring: *"Thanks to the idempotent nature of the APIC, this
  module is idempotent and reports changes."* + note: *"using `status='created'`
  will cause idempotency issues, use `status='modified'` instead."* → confirms real
  APIC distinguishes created vs modified and that the module's idempotency rides
  entirely on the response.
- **[E]** POST **with real change** + `?rsp-subtree=modified` → `imdata` contains the
  changed MO(s) each carrying `status ∈ {created,modified}`; `changed()` ⇒ true.
- **[A, strong]** POST with **no change** + `?rsp-subtree=modified` → `imdata` is
  **empty** (`totalCount:"0"`); `changed()` ⇒ false. This is the documented
  "idempotent nature" and the direct corollary of `changed()` scanning for status —
  the module could not be idempotent otherwise. Corroborated by prior curl evidence
  (re-push identical `l3extLNodeP` with `?rsp-subtree=modified` on **real** gear
  reports no change).
- **[A]** POST **without** any `rsp-subtree` param → real APIC returns **empty
  `imdata`** (it does not echo the subtree unless asked). Not exercised by the
  affected roles (they always send `modified`); see §5 for how the sim treats the
  no-param case to avoid regressing existing create-tests.
- **[A]** Nested-subtree shape of a `rsp-subtree=modified` response (parent MO with
  changed descendants nested under `children`, `status` only on changed MOs): the
  precise set of echoed attributes is version-dependent and **not captured here** —
  treated as an approximation (see §4.3).

---

## 4. Design — changed-status computation

### 4.1 Per-MO status rule (at the choke point, before `store.upsert`)

For each `(mo_cls, mo_attrs)` in `planned`, compute `status` from the **raw posted**
`mo_attrs` vs the pre-existing stored MO **before** the defaults overlay / upsert:

```
existing = store.get(dn)                      # dn = mo_attrs["dn"] (always set by plan)
if mo_attrs.get("status") == "deleted":
    st = "deleted"
elif existing is None:
    st = "created"
else:
    posted = {k: v for k, v in mo_attrs.items() if k not in ("dn", "status")}
    st = "modified" if any(existing.attrs.get(k) != v for k, v in posted.items()) else "unchanged"
```

**Correctness invariants (this is the part the task flags as make-or-break):**

- **Diff only user-posted keys**, never the full stored attr set. The store
  accumulates create-time defaults (`_CLASS_DEFAULTS`, e.g. `fvBD` mac/mtu/…) and
  reaction-materialized attrs. Comparing the full stored dict against a partial
  posted body would spuriously report `modified`. Diffing only posted keys makes a
  partial re-push that repeats the original values correctly `unchanged`.
- **Exclude `dn`** (identity — always equal by construction; we looked the MO up by
  it) **and `status`** (control attr — e.g. a posted `status:"created,modified"` is
  stored verbatim by the current sim but is not real config; excluding it prevents a
  false `modified`).
- **Compute before the defaults merge.** The defaults overlay only alters what is
  *stored on create*; it must not enter the diff. On create `existing is None`, so
  the diff branch is never reached anyway; on a re-push the defaults were already
  committed at create time and the re-push's posted keys are compared against them
  as stored values — consistent.
- **Verbatim-store ⇒ exact-bytes idempotency.** Because the sim stores posted values
  without normalization, a byte-identical re-push yields `existing.attrs[k] == v`
  for every posted `k` ⇒ `unchanged`. (Residual gap, out of scope — see §7.)

### 4.2 Response assembly (recommended: flat list of changed MOs)

`_upsert_recursive` returns per-MO status alongside the plan (e.g.
`list[(cls, attrs, status)]`, or a parallel `statuses` list). `apply()` builds:

```
changed = [(cls, attrs, st) for (cls, attrs, st) in results
           if st in ("created", "modified", "deleted")]
imdata  = [{cls: {"attributes": {"dn": attrs["dn"], "status": st}}} for (cls, attrs, st) in changed]
total   = len(imdata)
return imdata, total
```

- **Nothing changed ⇒ `imdata == []`, `total == 0`** ⇒ `changed()` false. ✅ the fix.
- **Anything changed ⇒ that MO present with its true `created`/`modified`** ⇒
  `changed()` true + correct label. ✅
- Shape is a **superset-compatible extension** of today's response: same per-entry
  form `{cls:{attributes:{dn,status}}}`, still flat, just potentially 0 or >1
  entries. Delete path unchanged (single `deleted` entry).

**Why flat (not nested) for F8a core:** (1) `changed()` is deep-recursive and
shape-agnostic, so flat trips it identically; (2) the current response is *already*
a non-faithful minimal echo (dn+status only, no children), so no consumer in this
codebase relies on a rich POST-response tree — `apply()`/`write_apply` has exactly
**one** caller (`post_mo`); (3) flat avoids the unvalidated nesting guesswork.
Real-APIC parity of the nested shape is deferred (§4.3).

### 4.3 Nested subtree — the hard, unvalidated part

Real APIC's `rsp-subtree=modified` returns a **nested** tree (parent MO with the
changed descendants under `children`, `status` on changed MOs only), e.g.:

```json
{"imdata":[{"fvTenant":{"attributes":{"dn":"uni/tn-X","status":"modified"},
  "children":[{"fvBD":{"attributes":{"rn":"BD-b","status":"created"}}}]}}]}
```

**Design decision:** F8a ships the **flat** list (§4.2) as the documented
approximation and treats nested reconstruction as an **optional future fidelity
enhancement**, because:

- The exact attribute set real APIC echoes per MO (full attrs? just `dn`/`rn`+
  `status`? annotation?) is **not captured here** — an unvalidated guess would be a
  new fidelity liability, not a fix.
- Multi-MO subtree scenarios ARE handled correctly by the flat list: a POST where
  the parent is unchanged but a child changed yields `imdata=[<child modified>]` →
  `changed()` true. (One ordering nuance: on such a mixed change the first `imdata`
  element is the **child**, not the root — see §6.2 for why no existing test breaks.)

**Uncertainty / open questions for the implementer (nested, if ever pursued):**
1. Does real APIC include unchanged **structural ancestors** (as childless
   containers with no `status`) to preserve the path to a changed leaf? (Likely yes,
   but unconfirmed.)
2. Full attribute echo vs `dn`+`status`-only per MO?
3. `rn` vs `dn` on nested children in the real wire form?

None of these block F8a's idempotency goal.

### 4.4 `?rsp-subtree` handling in `post_mo` / `apply`

The **status computation runs always** (independent of the param) — the param only
governs *what is included* in the response:

| `rsp-subtree` value | Behavior | Notes |
|---|---|---|
| `modified` (the `aci_rest` default) | return changed-MO list; `[]` if none | **primary path** — the fix |
| *absent* | same as `modified` | keeps existing create-tests green (create ⇒ non-empty ⇒ `imdata[0].status=created`); the only behavior change vs today is idempotency on re-push, which no test asserts against |
| `full` | **[A]** return changed-MO list (approximation) | real `full` echoes whole subtree with status only on changed MOs; flat-changed-only is a safe non-regressing subset — no test uses `full` on POST. Future: echo full subtree. |
| `no` | **[A]** return `[]` | real APIC echoes nothing; not exercised by any client/test. Caveat: `changed()` cannot detect change under `no` (matches real APIC). |

**Recommendation:** do **not** gate the fix on the presence of `?rsp-subtree=modified`.
Compute status unconditionally; map `{modified, absent, full}` → changed-MO list and
`no` → empty. `post_mo` should read `request.query_params.get("rsp-subtree")` and
thread it into `write_apply` (new optional kwarg), OR `apply` can accept the whole
query dict — implementer's call; keep the signature change minimal (one optional
param, default preserving today's `modified`-equivalent behavior).

### 4.5 Subscriptions / push-on-change — keep minimal (recommended)

**Recommended (lowest blast radius): change ONLY the HTTP response `imdata`; leave
`post_mo`'s push loop untouched.** The push loop already derives created/modified
from `pre_existing` and all three subscription tests (create/modify/delete) stay
green. On an identical re-push the response becomes empty (goal met) while a
`modified` push still fires — a minor subscription-fidelity gap that does **not**
affect the `aci_rest` idempotency goal and breaks no test.

**Optional consistency enhancement (flag, do not require):** have `apply()` return
the per-MO statuses and let `post_mo` push from the same set (skip pushes for
`unchanged`). This makes subscriptions faithful too, but note it changes push
**count** for multi-child creates (today: 1 push for the top-level; then: one per
changed MO) — verify no subscription test asserts an exact push count for a
multi-child body before adopting. The three existing subscription tests use
childless bodies, so they are unaffected either way.

### 4.6 Ordering vs F10 / F4 / F12 / deploy_mirror

- **F10 (`_validate_planned`)** still runs first, before the mutation loop → a bad
  value is still a 400 with zero side effects. F8a adds the diff **inside** the
  mutation loop (after validation) → strictly after F10. No interaction.
- **F4 / F12 (NDO golden 400s)** live in the NDO app, not this route — untouched.
- **`deploy_mirror`** calls `store.upsert` **directly** (it only imports
  `_CLASS_DEFAULTS`), bypassing `apply()` entirely → its status/response is not on
  the F8a path. Untouched.

---

## 5. Regression plan (blast radius is large — be thorough)

### 5.1 Unit tests (pytest, 1131 collected)

Grep of all POST-response `status` assertions → every one is a **first-create**,
a **genuine delete**, or a **genuine modify** — none locks the "identical re-push
still created" bug. Predicted outcome: **F8a breaks zero existing assertions.**

| Test | Line | Scenario | Under F8a | Verdict |
|---|---|---|---|---|
| `test_pr10_ansible_gaps.py::test_node_mo_alias_get_post_delete_round_trip` | 119 | FIRST create via `/api/node/mo` alias, asserts `imdata[0].status=="created"` | `existing is None` ⇒ `created`; root is first in plan ⇒ `imdata[0]` | ✅ passes unchanged |
| `test_pr9_ansible.py::test_status_created_modified_is_treated_as_upsert` | 195 | FIRST create with posted `status:"created,modified"`, asserts `created` | fresh ⇒ `created`; `status` excluded from diff | ✅ passes unchanged |
| `test_pr9_ansible.py::test_status_deleted_via_post_removes_subtree` | 171 | POST `status:"deleted"`, asserts `deleted` | delete branch unchanged | ✅ passes unchanged |
| `test_subscriptions.py::test_websocket_push_created_modified_deleted` | 215/224/233 | **push events** (not HTTP body): create⇒created, re-post w/ descr change⇒modified, delete⇒deleted | push loop untouched (recommended §4.5); all three are genuine changes | ✅ passes unchanged |

**"Correctly should change" (none found, but the rule):** any test that POSTs the
**same object twice** and asserts the **second** response is non-empty / `created`
would be **locking the F8a bug** and should be updated to expect empty/unchanged.
Grep found none. The implementer must re-grep after writing new tests.

**New tests F8a must add (TDD):**
- re-push byte-identical single MO ⇒ `imdata == []`, `totalCount == "0"`, `changed()==False`.
- re-push with one attr changed ⇒ `imdata` has that MO with `status=="modified"`, `changed()==True`.
- first create ⇒ `status=="created"` (guard against over-eager unchanged).
- partial re-push (subset of original attrs, same values) ⇒ unchanged (defaults not misread).
- create-then-repush of a MO **with `_CLASS_DEFAULTS`** (`fvBD`) ⇒ unchanged (defaults-overlay guard).
- parent unchanged + child attr changed ⇒ `imdata` contains the child `modified`.
- delete ⇒ `deleted` (unchanged behavior).
- `?rsp-subtree` absent still returns created on first create (no-param path guard).

### 5.2 E2E (pass1 / pass2, all legs)

**pass1 (fresh fabric, everything created):** every `aci_rest` write hits
`existing is None` ⇒ `created` ⇒ `changed=true`. **pass1 stays all-green.**

**pass2 (re-run same config against already-built fabric):** the `aci_rest` blocks
in these role task files flip from `changed>0` to `changed=0` (idempotent):

- `roles/{client}/aci-model/tasks/tenant.yml`
- `roles/{client}/aci-model/tasks/mgmt_tenant.yml`
- `roles/{client}/aci-model/tasks/infra_tenant.yml`
- `roles/{client}/aci-model/tasks/access.yml`
- `roles/{client}/aci-model/tasks/fabric.yml`
- `roles/{client}/aci-model/tasks/system_settings.yml`
- `roles/{client}/aci-model/tasks/admin.yml`
- `playbooks/{client}/provision_aci_ansible_user.yml`

**Expected fail→pass flips:** every pass2 cell whose assertion is "aci_rest tasks
report changed=0 / playbook idempotent" and currently fails because those tasks
report `changed=true`. (Exact E2E cell IDs live in the autoACI E2E harness / vault
report `Sim-E2E/`, not in this repo — the implementer/reviewer should capture the
precise cell names from a pass2 run diff.)

**Not affected on pass2 (already idempotent, must stay idempotent):** all
higher-level-module tasks (`aci_tenant`, `aci_bd`, `aci_ap`, `aci_epg`,
`aci_l3out`, …) — they were already `changed=0` on pass2 via client-side get-diff,
independent of F8a. Confirm they don't regress (they shouldn't — F8a doesn't touch
their GET path).

### 5.3 "Must NOT break" checklist

- **F10:** bad-value POST still 400 (validation runs before the diff).
- **F4 / F12:** NDO golden bad-payloads still 400 (different app/route).
- **pass1:** first create still `created` / `changed=true`.
- **`deploy_mirror`:** unaffected (bypasses `apply()`).
- **DELETE:** still `deleted` + idempotent 200-empty on missing DN.
- **GET-on-missing-DN:** still 200 + empty imdata (untouched).
- **Subscriptions:** create/modify/delete push events still fire (push loop
  untouched under recommended §4.5).
- **`fabricNodeIdentP` / `bgpPeerP` reactions:** still materialize sim-internal MOs;
  still excluded from the response.

---

## 6. Implementation notes & subtleties

### 6.1 Locus of change (smallest surface)
1. `_upsert_recursive` — compute per-MO `status` in the existing mutation loop
   (reuse the `store.get(dn)` it already calls); return `(cls, attrs, status)` triples
   (or a parallel status list) instead of/alongside `planned`.
2. `apply()` — build `imdata` from changed MOs only; keep reactions reading the
   `(cls, attrs)` projection of `planned` (their logic is unchanged).
3. `post_mo` — read `?rsp-subtree`, thread it to `write_apply` (one optional kwarg);
   push loop left as-is (recommended).

Keep `_upsert_recursive`'s return backward-usable for the two reaction loops in
`apply()` that iterate `for mo_cls, mo_attrs in planned` (adjust unpacking).

### 6.2 Ordering nuance (documented, non-breaking)
On a mixed change (root unchanged, child changed) the flat `imdata`'s first element
is the **child**. No existing test asserts `imdata[0]==<root>` on a *re-push* — all
`imdata[0]` root assertions are on **first creates**, where every MO is `created`
and the root is first in parent-first plan order. Safe.

### 6.3 `status` attribute pollution (pre-existing quirk, handled)
The sim stores a posted `status` (e.g. `"created,modified"`) as a real attribute
(existing behavior). F8a excludes `status` from the diff, so this neither triggers a
false `modified` nor is affected. (Cleaning up stored `status` is a separate,
out-of-scope fidelity item.)

---

## 7. Known limitations / residual gaps (out of F8a scope)

- **Normalization-equivalent inputs:** the sim stores verbatim and does not
  normalize; a client that sends *different bytes that real APIC would normalize to
  the same value* (e.g. bool `"true"`→`"yes"`, whitespace-trimmed `descr`) will be
  reported `modified` while real APIC says unchanged. F8a fixes the identical-bytes
  case (the actual blocker); normalization parity is a distinct future fidelity task.
- **Nested `rsp-subtree=modified` wire shape** not reproduced (flat approximation) —
  §4.3.
- **`rsp-subtree=full`** approximated as changed-only — §4.4.
- **Subscription push** not made no-change-aware under the recommended minimal path —
  §4.5.

---

## 8. Effort & risk

**Effort:** ~S–M. Core change is ~30–50 lines across `writes.py` (diff + return
shape) and `app.py` (thread `rsp-subtree`, adjust response), plus ~8 new tests. No
new modules, no store schema change. One caller of `apply()`.

**Risk:**
- **Low** for the core diff/response change — the choke point already reads
  pre-existing state; grep shows zero conflicting assertions; `apply()` has a single
  caller.
- **Medium — the "diff only posted keys, before defaults" invariant** (§4.1) is the
  one place a wrong implementation reintroduces false `changed` (the exact failure
  mode the task warns about). Must be unit-tested against a `_CLASS_DEFAULTS` class
  (`fvBD`) and a partial re-push.
- **Medium — E2E blast radius:** F8a changes behavior for every `aci_rest` write
  across 7 role files; a subtle diff bug could make a genuinely-changed pass1 write
  report `unchanged` (false negative → real config silently "not applied" from the
  playbook's view). Mitigate: assert pass1 stays all-`created`/all-changed, and add
  the "first create still created" + "modify still modified" guards.
- **Low — nested approximation:** deferred; flat shape is `changed()`-correct.
- **Low — subscriptions:** untouched under the recommended path.

**Verification gate before merge:** full `pytest` (expect 1131 → 1131+new, all
green) + E2E pass1 (all `created`) + E2E pass2 (aci_rest legs flip to `changed=0`,
higher-level-module legs stay `changed=0`, F10/F4/F12 golden 400s intact).

---

## 9. Return-summary anchors (for the dispatching agent)

- **aci_rest always sends `?rsp-subtree=modified` on POST** unless
  `rsp_subtree_preserve=true` (default false) → affected roles always send it.
- **`changed()` = recursive deep scan** for any `status ∈ {created,modified,deleted}`
  at any depth ⇒ empty imdata = the reliable `changed=false` shape; shape-agnostic
  otherwise.
- **Real-APIC semantics:** change+modified ⇒ MO(s) with status **[E]**; no-change
  ⇒ empty imdata **[A-strong]**; no-param ⇒ empty **[A]**; nested shape **[A]**.
- **Status calc:** created (dn absent) / modified (posted attr ≠ stored) / unchanged
  (all posted attrs == stored), **diffing only posted keys excluding dn+status,
  before the defaults overlay**.
- **Nested-subtree uncertainty:** exact echoed attrs + structural-ancestor inclusion
  unknown → ship flat changed-MO list (approximation), nested deferred.
- **Affected tests:** 0 predicted regressions (all status assertions are
  first-create/delete/genuine-modify); ~8 new tests to add.
- **Effort S–M; risk medium on the "posted-keys-only, pre-defaults" diff invariant
  and E2E blast radius.**
