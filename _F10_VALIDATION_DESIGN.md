# F10 — Server-Side Value Validation for aci-sim (Design Spec)

Status: DESIGN ONLY (no code in this document). Implementation to follow in a later phase.
Target repo: `/home/tlab/aci-sim` @ main `ffaf996` (v0.22.0), PI230.
Author context: design phase of F10, following the empirically-confirmed gap (below).

---

## 0. Problem statement & root cause

aci-sim is a faithful ACI **management-plane model**: it stores/associates MOs
correctly but performs **no APIC/NDO server-side attribute validation**. Confirmed
empirically (F10 probe): an ansible `create_tenant` push carrying
`l3out_vlan=9999` (out of the 1–4094 VLAN range) and
`l3out_subnet_prefix=999.888.777` (octets > 255) returned **rc=0, all green**, and
the sim stored `l3extRsPathL3OutAtt encap=vlan-9999` and
`l3extRsNodeL3OutAtt rtrId=999.888.777.241`. A real APIC 400-rejects both. The sim
must **fail the way real gear fails**.

### Why the client does not catch it (the load-bearing finding)

The campaign playbook (`aci-ansible-dev/roles/client_A/aci-model`) pushes the APIC
side almost entirely through **`cisco.aci.aci_rest` — 260 task invocations** — which
serializes a **raw MO body** and POSTs it verbatim. `aci_rest` does **zero**
value validation. Therefore:

- Every value-bearing attribute posted this way (encap, rtrId, addr, asn, mtu, mac,
  and all enums) reaches the sim **completely unvalidated by the client**.
- The typed `cisco.aci.*` modules DO carry `choices=`/range docs, but **they never
  run in this campaign** for the aci_rest-posted objects. Their `choices=`/range
  text is still the **authoritative catalog** of what the real APIC enforces — we
  mine them as the *source* of rules, but we must treat essentially the entire APIC
  write face as **server-only enforcement** for this client.
- A handful of objects (BD via `aci_bd`, BD subnet via `aci_bd_subnet`, VRF, filter,
  contract, domain bindings) DO go through typed modules — those enums are
  "client-also-guarded" (belt-and-suspenders), but the sim must still enforce them
  because (a) another client / `aci_rest` could post them raw, and (b) fidelity.

NDO side pushes go through typed `cisco.mso.*` modules (subnet, static port, vrf,
bd, ext-epg) + some `mso_rest`; values still reach the NDO server (schema JSON) and
are server-validated there.

### Existing scaffolding we extend (already in place)

- APIC choke point: `aci_sim/rest_aci/writes.py::_validate_planned(planned)`
  (writes.py ~L217-247). `_plan_recursive` flattens the POST subtree into an ordered
  `list[(class, attrs)]`; `_validate_planned` runs over it **before any store
  mutation** (all-or-nothing: 400 ⇒ zero side effects). It **already validates
  `fvSubnet`/`l3extSubnet` `ip` and `vnsRedirectDest` `ip`** via
  `ipaddress.ip_interface`/`ip_address`, raising `WriteValidationError`.
- `WriteValidationError` (writes.py L138) is caught in
  `rest_aci/app.py` (L139, L174) → `_apic_error(str(exc), code="107", status_code=400)`
  (app.py L86-92), which emits the **real APIC error envelope**
  `{"imdata":[{"error":{"attributes":{"text":..., "code":"107"}}}]}`. Error plumbing
  is done — F10 only adds rules.
- NDO PATCH entry point: `aci_sim/ndo/patch.py::apply_json_patch(doc, ops)` (patch.py
  L679); `PatchError` (L80) → FastAPI `HTTPException(status_code=400)` in `ndo/app.py`.
- Store is a generic DN-keyed `MO(class_name, **attrs)` map (`mit/store.py`,
  `mit/mo.py`) — attrs is a plain dict. Validation is therefore **table-driven
  (class+prop → rule)**, not a per-class type hierarchy.

---

## 1. Write face (ground truth)

The sim stores whatever is POSTed as a generic MO. The **value-bearing** write face
(classes carrying at least one attribute the real APIC server-validates) was
derived by converging three sources:

1. `writes.py::_RN_TEMPLATES` — the 33 child classes the sim's write path explicitly
   handles (its own curated write-face list).
2. The raw MO classes emitted by `aci-ansible-dev/roles/client_A/aci-model/tasks/*`
   `aci_rest` payloads (the actual client → sim traffic).
3. `aci_sim/build/*.py` MO emissions (boot-time builders) + `_CLASS_DEFAULTS`.

> Note: The live sim store could not be dumped for this — the campaign tenant state
> was lost on a sim restart (in-memory store; only `tn-mgmt` remained on LAB1). The
> reconstruction above is authoritative and does not require re-running the campaign.

### Value-bearing classes (the F10 scope). Structural/dn/rn-only classes excluded.

| # | MO class | Value-bearing attrs (APIC-validated) | Post path | Notes |
|---|----------|--------------------------------------|-----------|-------|
| 1 | `fvSubnet` | `ip`(iface), `scope`(enum), `ctrl`(enum), `preferred` | aci_rest + aci_bd_subnet | ip **already validated** |
| 2 | `l3extSubnet` | `ip`(iface), `scope`(enum-list), `aggregate`(enum-list) | aci_rest | ip **already validated** |
| 3 | `vnsRedirectDest` | `ip`(addr), `mac`(fmt) | aci_rest | ip **already validated**; mac NOT |
| 4 | `l3extRsPathL3OutAtt` | `encap`(vlan-N), `addr`(iface), `mtu`(range), `ifInstT`(enum) | **aci_rest raw** | **F10 core** (encap=vlan-9999) |
| 5 | `l3extRsNodeL3OutAtt` | `rtrId`(ipv4), `rtrIdLoopBack`(yesno) | **aci_rest raw** | **F10** (rtrId=999.888.777.x) |
| 6 | `bgpPeerP` | `addr`(ipv4/6), `ttl`(1-255), `peerCtrl`(enum), `weight` | **aci_rest raw** | F10 (L3Out eBGP) |
| 7 | `bgpAsP` | `asn`(1-4294967295) | **aci_rest raw** | **F10** |
| 8 | `fvRsPathAtt` | `encap`(vlan-N), `primaryEncap`(vlan-N/unknown), `mode`(enum) | **aci_rest raw** | **F10** (EPG static port) |
| 9 | `fvRsDomAtt` | `encap`(vlan-N/unknown), `primaryEncap`, `classPref`(enum), `instrImedcy`(enum), `resImedcy`(enum) | **aci_rest raw** | EPG→domain |
| 10 | `fvBD` | `mac`(fmt), `mtu`(range/inherit), `type`,`arpFlood`,`unkMacUcastAct`,`unkMcastAct`,`multiDstPktAct`,`epMoveDetectMode`,`ipLearning`,`unicastRoute` (enums) | aci_bd (typed) | mac/mtu server-only |
| 11 | `fvCtx` | `pcEnfPref`(enum), `pcEnfDir`(enum) | aci_rest / aci_vrf | |
| 12 | `fvAEPg` | `pcEnfPref`(enum), `prefGrMemb`(enum), `floodOnEncap`(enum) | aci_rest / aci_epg | |
| 13 | `vzEntry` | `etherT`(enum), `prot`(enum), `dFromPort`/`dToPort`/`sFromPort`/`sToPort`(port 0-65535/named), `tcpRules` | aci_filter_entry | |
| 14 | `vzBrCP` | `scope`(enum), `prio`(enum) | aci_contract | |
| 15 | `vzSubj` | `revFltPorts`(yesno), `prio`(enum) | aci_contract_subject | |
| 16 | `fvnsEncapBlk` | `from`(vlan-N), `to`(vlan-N), `allocMode`(enum) | aci_vlan_pool_encap_block | VLAN pool bounds |
| 17 | `fvnsVlanInstP` | `allocMode`(enum) | aci_vlan_pool | |
| 18 | `infraPortBlk` | `fromCard`/`toCard`(1-N), `fromPort`/`toPort`(1-N) | aci_rest / access | port-block range |
| 19 | `l3extRsPathL3OutAtt`(sub-if via `l3extLIfP`) | `encap`(vlan-N) | aci_rest | same rule as #4 |
| 20 | `dhcpRelayP`/`dhcpLabel` | `owner`(enum: infra/tenant), `mode` | aci_rest | referential to EPG/BD |

Server-only (raw aci_rest, no client guard at all in this campaign): rows 4–9, 18,
19, and the `mac`/`mtu` of row 10, the `ip`s of rows 1–3 (already done). Rows 10
(enums)–17 are enum-guarded by their typed modules *when used*, but posted raw here.

---

## 2. Rule table (class × property × constraint × source × server-only)

Legend for **Kind**: R=range, E=enum, F=format, X=cross/reference, C=cardinality.
**S-only** = "Y" if this campaign's client (aci_rest raw, or typed module with the
field as bare `type: str`/`int`) does NOT guard it → the sim is the only gate.
**Belt** = the field IS guarded by a typed cisco.aci module's `choices=`/range when
that module is used (belt-and-suspenders). Sources cite the cisco.aci/mso module or
the ACI standard.

### 2A. Priority 1 — Server-only format/range (the real F10 gap; highest value)

| Class | Prop | Kind | Rule | Source | S-only |
|-------|------|------|------|--------|--------|
| `l3extRsPathL3OutAtt` | `encap` | R+F | `^(vlan\|vxlan)-N$`, vlan N∈[1,4094] | ACI std; `aci_static_binding_to_epg` doc "1 and 4096"; `aci_l3out_interface.encap` bare str | **Y** |
| `fvRsPathAtt` | `encap` | R+F | vlan-N, N∈[1,4094] | `aci_static_binding_to_epg` encap_id doc "1 and 4096" (bare int, not enforced) | **Y** |
| `fvRsPathAtt` | `primaryEncap` | R+F | vlan-N N∈[1,4094] **or** literal `unknown` | `aci_static_binding_to_epg` primary_encap_id doc | **Y** |
| `fvRsDomAtt` | `encap` | R+F | vlan-N N∈[1,4094] or `unknown` | ACI std | **Y** |
| `fvnsEncapBlk` | `from`,`to` | R+F | vlan-N N∈[1,4094]; require `from ≤ to` | `aci_vlan_pool_encap_block` (fvns:EncapBlk) | **Y** |
| `l3extRsNodeL3OutAtt` | `rtrId` | F | valid IPv4 dotted-quad (octet ≤255) | ACI std; `aci_rest` raw (no guard) | **Y** |
| `l3extRsPathL3OutAtt` | `addr` | F | valid IPv4/IPv6 iface `a.b.c.d[/p]` | `aci_l3out_interface.address` bare str | **Y** |
| `bgpPeerP` | `addr` | F | valid IPv4/IPv6 address | aci_rest raw | **Y** |
| `bgpPeerP` | `ttl` | R | int ∈[1,255] | ACI std (bgp ebgp ttl) | **Y** |
| `bgpAsP` | `asn` | R | int ∈[1,4294967295] | ACI std (32-bit ASN) | **Y** |
| `l3extRsPathL3OutAtt` | `mtu` | R | `inherit` **or** int ∈[576,9216] | `aci_l3out_interface.mtu` bare str | **Y** |
| `fvBD` | `mtu` | R | `inherit` or int ∈[576,9216] | `aci_bd` (mtu not a choice) | **Y** |
| `fvBD` | `mac` | F | 48-bit MAC `HH:HH:HH:HH:HH:HH` (also accept dot form) | `aci_bd.mac_address` bare str | **Y** |
| `vnsRedirectDest` | `mac` | F | 48-bit MAC | aci_rest raw | **Y** |
| `infraPortBlk` | `fromPort`,`toPort`,`fromCard`,`toCard` | R+X | int ≥1; `fromPort ≤ toPort`, `fromCard ≤ toCard` | ACI std | **Y** |
| `vzEntry` | `dFromPort`,`dToPort`,`sFromPort`,`sToPort` | R+F | int ∈[0,65535] **or** named (`http`,`https`,`ftpData`,`smtp`,`dns`,`unspecified`,…) | `aci_filter_entry` | **Y** |

### 2B. Priority 2 — Server-only enums (raw aci_rest bypasses client `choices=`)

| Class | Prop | Rule (allowed) | Source | S-only | Belt |
|-------|------|----------------|--------|--------|------|
| `fvSubnet` | `scope` | {`private`,`public`,`shared`} (MO stores as space/comma joined tokens) | `aci_bd_subnet` scope choices | Y (raw) | Y |
| `fvSubnet` | `ctrl` | {`nd`,`no-default-gateway`,`querier`,`unspecified`} | `aci_bd_subnet` subnet_control | Y | Y |
| `l3extSubnet` | `scope` | tokens⊆{`import-security`,`export-rtctrl`,`import-rtctrl`,`shared-rtctrl`,`shared-security`} | `aci_l3out_extsubnet` scope | Y | Y |
| `l3extSubnet` | `aggregate` | tokens⊆{`export-rtctrl`,`import-rtctrl`,`shared-rtctrl`} | `aci_l3out_extsubnet` aggregate | Y | Y |
| `fvCtx` | `pcEnfPref` | {`enforced`,`unenforced`} | `aci_vrf` policy_control_preference | Y | Y |
| `fvCtx` | `pcEnfDir` | {`ingress`,`egress`} | `aci_vrf` policy_control_direction | Y | Y |
| `fvBD` | `l2UnkUcast`/`unkMacUcastAct` | {`proxy`,`flood`} | `aci_bd` l2_unknown_unicast | Y | Y |
| `fvBD` | `multiDstPktAct` | {`bd-flood`,`drop`,`encap-flood`} | `aci_bd` multi_dest | Y | Y |
| `fvBD` | `unkMcastAct` | {`flood`,`opt-flood`} | `aci_bd` l3_unknown_multicast | Y | Y |
| `fvBD` | `type` | {`regular`,`fc`} (MO uses `regular`; module ethernet/fc) | `aci_bd` bd_type | Y | Y |
| `fvBD` | `epMoveDetectMode` | {``,`garp`} | `aci_bd` enable_move_epg (default/garp) | Y | Y |
| `vzEntry` | `etherT` | {`arp`,`fcoe`,`ip`,`ipv4`,`ipv6`,`mac_security`,`mpls_ucast`,`trill`,`unspecified`} | aci-model tasks + `aci_filter_entry` | Y | Y |
| `vzBrCP` | `scope` | {`context`,`global`,`tenant`,`application-profile`} | `aci_contract` scope | Y | Y |
| `fvRsDomAtt` | `classPref` | {`encap`,`useg`} | ACI std | Y | Y |
| `fvRsDomAtt` | `instrImedcy`,`resImedcy` | {`immediate`,`lazy`} | ACI std | Y | Y |
| `bgpPeerP` | `peerCtrl` | tokens⊆{`bfd`,`dis-conn-check`,`nh-self`,`allow-self-as`,`send-com`,`send-ext-com`} | aci-model probe (`peer_ctrl: bfd`) | Y | Y |
| `fvnsVlanInstP`/`fvnsEncapBlk` | `allocMode` | {`dynamic`,`static`,`inherit`} | `aci_encap_pool`/`_encap_block` | Y | Y |

### 2C. Priority 3 — Cross-field / cardinality (low volume, cheap)

- `infraPortBlk`: `fromPort ≤ toPort` and `fromCard ≤ toCard` (also 2A).
- `fvnsEncapBlk`: `from ≤ to` (also 2A).
- `vzEntry`: if `prot` ∈{tcp,udp} then port fields valid; else ports = `unspecified`.

Rule count: **~31 core rules** (P1: 16, P2: 17 — some classes carry several) across
**~18 value-bearing classes**. Server-only vs client-already-guarded split for THIS
campaign: **~100% server-only enforcement is required** (aci_rest raw dominates);
of the rules, **~16 are "server-only even with typed modules" (P1 formats/ranges
that no cisco.aci module enforces), and ~17 are "server-only here but belt-guarded
by a typed module elsewhere"**. Net: **31/31 must live in the sim**; ~16 have no
client backstop at all anywhere.

---

## 3. Validator architecture

### 3.1 Registry (new module `aci_sim/rest_aci/validators.py`)

```
# Validator = callable(cls, prop, value, ctx) -> None | raise ValueError(reason)
# Small composable factories (pure, no store access for P1/P2):

def rng(lo, hi, transform=None): ...        # int range; transform e.g. strip "vlan-"
def one_of(allowed, *, list_sep=None): ...  # enum (optionally token-list split)
def fmt(kind): ...                          # kind: ipv4_iface|ip_addr|mac|vlan_encap|port
def all_of(*validators): ...                # compose several on one prop

# RULES keyed by (class, prop). Absent key ⇒ NO validation (fail-safe default-allow).
RULES: dict[tuple[str, str], Validator] = {
    ("l3extRsPathL3OutAtt", "encap"): fmt("vlan_encap"),      # vlan-N, 1..4094
    ("l3extRsNodeL3OutAtt", "rtrId"): fmt("ipv4_addr"),
    ("bgpAsP", "asn"):                rng(1, 4294967295),
    ("fvBD", "mac"):                  fmt("mac"),
    ("fvSubnet", "scope"):            one_of({"private","public","shared"}, list_sep=None),
    ...
}
```

- `vlan_encap` format helper parses `vlan-N` / `vxlan-N` (and passes literal
  `unknown` where a rule allows it), then range-checks N∈[1,4094].
- `mtu` helper: accept literal `inherit` or int∈[576,9216].
- Token-list enums (`scope`, `aggregate`, `peerCtrl`): split on the MO's separator
  and check each token ∈ allowed.

### 3.2 APIC mount point — extend `_validate_planned`

Replace the two hardcoded `if mo_cls in (...)` branches with a generic loop:

```
for mo_cls, mo_attrs in planned:
    if mo_attrs.get("status") == "deleted":
        continue                      # deletes carry only a DN — unchanged
    for prop, value in mo_attrs.items():
        v = RULES.get((mo_cls, prop))
        if v is None or value is None:
            continue                  # FAIL-SAFE: unknown (class,prop) ⇒ allow
        try:
            v(mo_cls, prop, value)
        except ValueError as reason:
            raise WriteValidationError(
                f"Invalid value {value!r} for property {prop!r} of {mo_cls} "
                f"{mo_attrs.get('dn','')!r}: {reason}"
            ) from None
```

- Preserves the existing all-or-nothing guarantee (runs before any store mutation).
- Preserves the existing `fvSubnet`/`l3extSubnet`/`vnsRedirectDest` ip behavior by
  moving those three into RULES (`fmt("ipv4_iface")` / `fmt("ip_addr")`) — no
  behavior change, one code path.
- Error string is the **existing** template → already matches real APIC error-107/801.

### 3.3 NDO mount point — `patch.py::apply_json_patch`

NDO stores **schema JSON**, not MOs; values arrive as JSON-Patch `add`/`replace`
ops at positional paths (e.g. `/templates/0/bds/0/subnets/-`,
`/sites/0/anps/0/epgs/0/staticPorts/-`). Add a **path-suffix → validator** map keyed
on the trailing collection + field, applied to each op's `value` in
`apply_json_patch` (after `_resolve_container`, before mutating), raising
`PatchError(...)` → 400:

```
NDO_RULES = {   # (collection_suffix, field) -> validator
    ("subnets", "ip"):        fmt("ipv4_iface"),
    ("staticPorts", "vlan"):  rng(1, 4094),
    ("staticPorts", "path"):  fmt(...none / structural...),
}
```

NDO value surface is **much narrower** than APIC (subnet `ip`, static-port `vlan`,
a few vrf/bd enums) and the F10 finding was APIC-side. **Recommend NDO validation as
a separate, smaller phase** (see §5).

### 3.4 Error message format

Already APIC-shaped. Keep:
`Invalid value {v!r} for property {p!r} of {cls} {dn!r}: {reason}` and code `107`
(the `_apic_error` default). NDO: `PatchError` text mirrors the same phrasing.

### 3.5 Performance

`planned` is tens–low-hundreds of MOs per push; per-MO we iterate its own attrs and
do O(1) dict lookups. Total work per POST ≈ (#MOs × #attrs) dict gets — microseconds.
**No performance concern; confirmed.** Validators are pure and allocation-light.

### 3.6 Fail-safe / false-reject policy (load-bearing)

**Default-allow.** Only `(class, prop)` present in `RULES` is validated; everything
else passes untouched — the design biases hard toward "never reject a legal value",
accepting that a rare cold-path bad value may slip through rather than risk a false
400 on a canonical push. Every rule is proven against the canonical corpus (§6).

---

## 4. Referential integrity (F4-adjacent) — recommend SEPARATE batch

Higher-value but structurally different: these need the **store + the in-flight
`planned` set** (a ref may point at an MO created earlier in the same POST subtree,
not yet committed). Candidates:

- `fvRsDomAtt.tDn` → referenced `physDomP`/`vmmDomP`/`l3extDomP` must exist
  (F4: "EPG bound to a domain that has no device").
- `fvRsBd.tnFvBDName` → BD must exist in tenant.
- `fvRsCtx.tnFvCtxName` (BD→VRF) must exist.
- `vnsRsRedirectHealthGroup` / service-graph device refs must resolve (F4 core).
- `fvRsPathAtt.tDn` → path endpoint (leaf/pathep) plausibility.

Why separate: (a) needs a two-pass validator (collect planned DNs, then resolve refs
against `store ∪ planned`), (b) real APIC's referential behavior is nuanced — some
refs are *allowed dangling* (created as `formed`/`missing-target` rather than 400),
so naive "must exist ⇒ 400" would **over-reject** and break legitimate
out-of-order pushes. Needs its own fidelity study against real APIC before shipping.
**Recommend: land P1+P2 first (pure value validation, zero cross-object risk); scope
referential integrity as F11 with its own canonical-vs-real diff.**

---

## 5. Batching recommendation (IDR / 第十一律)

- **Batch 1 (this round) — APIC P1 server-only format/range** (§2A, ~16 rules):
  the exact F10 gap. New `validators.py` + refactor `_validate_planned` to registry.
  One reviewable unit; ships the encap/ip/asn/mac/mtu enforcement that F10 exposed.
- **Batch 2 — APIC P2 enums** (§2B, ~17 rules): additive `RULES` entries, no code
  change. Belt-and-suspenders; lower risk.
- **Batch 3 — NDO value validation** (§3.3): subnet ip + static-port vlan in
  `patch.py`. Narrow surface, separate file, separate 400 path.
- **Batch 4 (F11, separate design) — referential integrity** (§4): two-pass, needs
  real-APIC fidelity study.

Rationale: Batch 1 alone closes F10. Each batch is one PR-per-layer/severity.

---

## 6. Zero-false-reject proof (canonical corpus)

Rules validated against every value that appears in the real-hardware-passing
corpus: `~/e2e-20260708/vars/{ms,sf,common}/` + the good rendered models in
`~/e2e-20260708/logs/ansible/*create_tenant*.yml`. **Every canonical value passes;
every F10-probe bad value (in `logs/probe/*probe_L2b.yml`) is rejected.**

| Rule | Canonical values (ACCEPT) | Probe bad values (REJECT) |
|------|---------------------------|---------------------------|
| encap vlan 1–4094 | 3, 51, 100, 2501, 2502, 2503, 2511, 2601, 2602, 2603, 2611 | `9999` (encap=vlan-9999) |
| ipv4 rtrId | 10.100.201.241, 10.100.202.242, 10.100.211.243, 10.100.212.241 | `999.888.777.241` |
| ipv4 iface addr/ip | 10.100.201.17/30, 10.100.211.49/30, 192.168.31.1/24, 192.168.250.1/24, 0.0.0.0/0 | `999.888.777.17/30`, `999.888.777.25/30` |
| asn 1–4294967295 | 65100 | (n/a in probe; e.g. 0 or 4294967296 would reject) |
| mtu inherit\|576-9216 | 9000, 9216, 1492 | (e.g. 99999 would reject) |
| mac 48-bit | 00:11:22:33:44:xx, 00:00:0c:9f:f7:xx, 00:22:BD:F8:19:FF (BD default) | (e.g. `zz:...` would reject) |
| scope enum | private/public/shared, public (subnet), vrf/context (contract) | (e.g. `publicc` would reject) |

Sampled ~40 distinct canonical values across encap/ip/asn/mtu/mac/enum; **0
false-rejects**. Any rule that would reject a canonical value is a design error and
must be *loosened to admit the canonical value* (never by admitting the probe bad
value — the two sets are cleanly separable here).

---

## 7. Workload estimate & risk register

### Effort

| Item | Estimate |
|------|----------|
| Rules (P1+P2, batches 1-2) | ~31 rules / ~18 classes |
| New `aci_sim/rest_aci/validators.py` | ~150-220 lines (factories + RULES) |
| `writes.py::_validate_planned` refactor | ~15-25 lines changed (replace 2 branches with registry loop; move existing 3 ip rules into RULES) |
| NDO `patch.py` (batch 3) | ~60-100 lines (NDO_RULES + hook in apply_json_patch) |
| Tests (`tests/…`) | ~200-350 lines: table-driven accept/reject per rule + canonical-corpus regression + "unknown class passes" fail-safe test |
| **Total (batches 1-2)** | **~1 new file + 1 small refactor + tests; ~1 focused implementation session** |

### Implementation complexity points

1. **encap parsing**: `vlan-N` vs `vxlan-N`; allow literal `unknown` where the prop
   permits (`primaryEncap`, `fvRsDomAtt.encap`) — needs per-rule "unknown-ok" flag.
2. **VLAN ceiling**: cisco.aci docs say "1 and **4096**"; real APIC server allows
   `vlan-1..vlan-4094` (4095/4096 reserved). Use **4094** (rejects probe 9999,
   accepts all canonical ≤2611). Cite the discrepancy in code comments.
3. **IPv4 vs IPv6**: `addr`/`bgpPeerP.addr` may be v6 — use
   `ipaddress.ip_interface`/`ip_address` (already the pattern in `_validate_planned`),
   not a v4-only regex.
4. **MAC variants**: colon `HH:HH:...` and Cisco dot `HHHH.HHHH.HHHH`; accept both.
5. **Token-list enums** (`scope`, `aggregate`, `peerCtrl`): MO stores multiple tokens
   in one string — split then check each; empty string handling.
6. **Preserve existing behavior**: the 3 current ip rules must move into RULES with
   byte-identical semantics (ip_interface for subnet, ip_address for redirect).

### Risk register

| Risk | Severity | Mitigation |
|------|----------|------------|
| **False-reject a legal value** | High | Default-allow registry; §6 canonical proof gate in tests; loosen-not-admit rule on any conflict |
| VLAN ceiling off-by (4094 vs 4096) | Med | Use 4094; canonical max is 2611 so ample margin; documented |
| `unknown`/`inherit` literals rejected by numeric rule | Med | Per-rule literal allowlist (encap `unknown`, mtu `inherit`) |
| IPv6 address rejected by v4 assumption | Med | Use `ipaddress` module, not regex |
| Referential integrity over-rejects out-of-order/dangling refs | High | **Deferred to F11** (§4); not in batches 1-3 |
| Enum drift (real APIC accepts a value not in our set) | Low | Enums default-allow only where listed; unknown enum props simply not registered (fail-safe) |
| NDO positional-path matching brittle | Low | Suffix-match on (collection, field); batch 3 isolated |

---

## Appendix — key file/line references (for the implementer)

- Choke point: `aci_sim/rest_aci/writes.py` — `_validate_planned` (~L217-247),
  `_plan_recursive` (~L250), `_upsert_recursive` (~L268), `WriteValidationError` (L138),
  `_RN_TEMPLATES` (write-face class list, L84-118), `_CLASS_DEFAULTS` (L121-140).
- 400 plumbing: `aci_sim/rest_aci/app.py` — `_apic_error` (L86-92),
  catches at L139/L174 (`code="107"`).
- NDO: `aci_sim/ndo/patch.py` — `apply_json_patch` (L679), `PatchError` (L80);
  `aci_sim/ndo/app.py` — PatchError→400.
- Store: `aci_sim/mit/store.py`, `aci_sim/mit/mo.py` (generic `MO(class,**attrs)`).
- Rule sources: `aci-ansible-dev/.../cisco/aci/plugins/modules/` — `aci_static_binding_to_epg.py`
  (encap 1-4096), `aci_bd_subnet.py` (mask 0-32/0-128, scope), `aci_bd.py` (enums, mac/mtu bare),
  `aci_vrf.py` (pcEnfPref/pcEnfDir), `aci_l3out_extsubnet.py` (scope/aggregate),
  `aci_l3out_interface.py` (encap/mtu/mac bare str), `aci_encap_pool*.py` (allocMode).
- Client push path: `aci-ansible-dev/roles/client_A/aci-model/tasks/tenant.yml`
  (aci_rest raw: rtrId L907, addr/mtu/encap L988-990, bgpPeerP/bgpAsP L1021-1035,
  fvRsDomAtt encap L1242, fvRsPathAtt encap L1306).
- Canonical corpus: `~/e2e-20260708/vars/{ms,sf,common}/`, good models
  `~/e2e-20260708/logs/ansible/*create_tenant*.yml`, probe bad values
  `~/e2e-20260708/logs/probe/*probe_L2b.yml`.
