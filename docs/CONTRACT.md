# autoACI ⇄ APIC/NDO REST CONTRACT (what the simulator must satisfy)

> Distilled from a read-only audit of `~/autoACI/backend` (services/aci_connector.py,
> ndo_connector.py, routers/topology.py, routers/ndo.py, routers/fabric_build.py,
> services/acipy_exec.py, plugins/*). This is the authoritative contract the sim
> emulates. Do NOT re-explore autoACI; trust this file. If something here is
> ambiguous, prefer the behavior that makes autoACI render.

autoACI connects to **three host identities**, each `https://{host}` with `verify=False`:
- **Site-A APIC**, **Site-B APIC** (each logged in separately; autoACI tracks `sites[].{host,asn}` and prefixes node IDs by site when >1 site), and
- **ND / NDO** (separate auth + endpoints).

---

## 1. APIC — authentication

- `POST /api/aaaLogin.json` body `{"aaaUser":{"attributes":{"name":<user>,"pwd":<pwd>}}}`.
  `name` may be a bare username (`admin`) or **domain-qualified**
  (`apic:<domain>\<user>` or `<domain>\<user>`, e.g. `apic:local\admin`) — PR-15:
  aci-py's APIC connector sends the domain-qualified form by default, matching
  real APIC's local-login-domain behavior. `auth.py::_bare_username()` strips an
  optional `apic:<domain>\`/`<domain>\` prefix before the credential compare, so
  both forms authenticate identically; the domain segment itself is not
  validated (this sim has no concept of configured login domains — any domain
  string is accepted). Credentials (post-strip) are checked against
  `SIM_USERNAME`/`SIM_PASSWORD` (default `admin`/`cisco`; see README §Configuration) —
  a mismatch (either form) returns a **401 APIC error envelope**, never a bare token.
  **Admin-account wizard (topology `auth:` section):** `aci-sim init`'s Step 0
  writes an `auth: {username, password}` section into `topology.yaml`
  (`topology/schema.py`'s `Auth` model); `aci-sim run` resolves
  `SIM_USERNAME`/`SIM_PASSWORD` with this precedence (highest first): (1) the
  caller's shell already has `SIM_USERNAME` set — wins outright, `auth:` is
  ignored; (2) `topology.yaml` has an `auth:` section — its `username`/
  `password` are injected; (3) neither — nothing injected, this sim falls back
  to the hardcoded `admin`/`cisco` default documented above. `cli.py`'s
  `resolve_admin_credentials()` implements this precedence and is unit-tested
  directly (`tests/test_admin_account.py`). **NDO is NOT affected** —
  `ndo/app.py`'s `POST /api/v1/auth/login`/`POST /login` (§7 below) accept
  any credential by design and never validate anything against
  `auth.ndo_username`/`ndo_password`; those two fields (set only when the
  wizard's `ndo_same_account` answer is "no") are stored for informational
  parity only. Response on
  success: `{"imdata":[{"aaaLogin":{"attributes":{"token":"<t>","refreshTimeoutSeconds":"600"}}}]}`.
  Token also returned as `APIC-cookie` cookie; subsequent requests send that cookie.
  Sessions expire after `refreshTimeoutSeconds` (600s); every query/write route
  requires a live, unexpired `APIC-cookie` or returns a **403 APIC error envelope**
  (`text="Token was invalid (Error: Token timeout)"`, `code="403"`). A malformed
  login body (wrong shape / non-JSON) returns a **400 APIC error envelope**, never
  FastAPI's raw `{"detail": [...]}` validation-error shape.
- `GET /api/aaaRefresh.json` → requires a valid existing `APIC-cookie`; renews its
  expiry to a full `refreshTimeoutSeconds` and returns the same aaaLogin envelope
  (autoACI refreshes every ~240s). Without/with an invalid cookie → 403 envelope.
- `POST /api/aaaLogout.json` body `{"aaaUser":{"attributes":{"name":<user>}}}` —
  invalidates the server-side session in addition to clearing the cookie.
- During/after login autoACI probes: `GET /api/class/topSystem.json?query-target-filter=eq(topSystem.role,"controller")&page-size=1`
  and `GET /api/mo/uni/userprofile-<user>.json?query-target=subtree&target-subtree-class=aaaUserDomain`.

## 2. APIC — the three query shapes + full param grammar

1. **Class query** — `GET /api/class/{class}.json` with any of:
   - `page-size` (always; default 500, clamped to `[1, 5000]`), `page` (always; 0-indexed, must be `>= 0`).
     A non-numeric or out-of-range `page`/`page-size` yields a **400 APIC error envelope**
     (`code="107"`), never a bare 500 or silent truncation.
   - `query-target-filter={expr}` (filter grammar §4)
   - `rsp-subtree=children|full` + `rsp-subtree-class={c1,c2,...}` (modern subtree)
   - `query-target=subtree` + `target-subtree-class={c1,c2,...}` (legacy subtree)
2. **MO/DN query** — `GET /api/mo/{dn}.json` with optional `query-target=subtree` + `target-subtree-class={...}`.
   DNs contain slashes inside brackets: `subnet-[10.0.0.1/24]`, `pathep-[eth1/9]`, `rsdomAtt-[uni/phys-X]` — bracket-aware DN parsing required.
   `GET/POST/DELETE /api/node/mo/{dn}.json` is a full alias of `/api/mo/{dn}.json`
   (PR-10) — real APIC serves both; the sim's API Inspector / `aci_rest`-module
   callers may use either interchangeably with identical behavior.
3. **Node-scoped class query** — `GET /api/node/class/{dn}/{class}.json` with optional `query-target-filter`.
   Used for node-local `faultInst` (e.g. `.../topology/pod-1/node-101/faultInst.json`).
   **Fabric-wide form:** `GET /api/node/class/{class}.json` (no `topology/pod-X/node-Y`
   DN prefix at all — just the bare class name) is also supported and is equivalent to
   `GET /api/class/{class}.json` (same result set, real APIC behavior). The sim
   distinguishes the two by whether the path segment after `/api/node/class/` contains
   a `/`: a node-scoped DN always does, the fabric-wide form never does.

**Response envelope (always):**
```json
{"imdata":[ {"<class>":{"attributes":{...}, "children":[ {"<child>":{"attributes":{...}}} ]}} ], "totalCount":"<N>"}
```
- `children` present only when a subtree mode was requested (children/full nest children;
  `rsp-subtree-class`/`target-subtree-class` filter which descendant classes appear).
- `totalCount` is a STRING; used for pagination (client loops `page` until a short page).
- Subtree responses may return children before parents → consumers do **two-pass parsing**; the
  sim may emit any order but MUST include all requested descendants.

## 2a. APIC — query subscriptions + websocket push

All three query shapes in §2 accept `?subscription=yes`. The response is
the normal §2 envelope PLUS a top-level `subscriptionId` string sitting
alongside `imdata`/`totalCount` (NOT nested inside them):

```json
{"imdata":[...], "totalCount":"<N>", "subscriptionId":"<id>"}
```

Without `?subscription=yes`, the response has no `subscriptionId` key —
byte-identical to a pre-subscription-feature query. This is a strict
backward-compat requirement, not a default choice.

**Scope**: a class-query subscription (`/api/class/{cls}.json`) watches
every MO of that class fabric-wide. A DN-scoped subscription
(`/api/mo/{dn}.json`, `/api/node/class/{dn}/{cls}.json`) watches that DN
and its entire subtree.

**Websocket**: `GET /socket<token>` — `<token>` is the same `APIC-cookie`
token from `aaaLogin` (no separator, literal path concatenation, matching
real APIC). Unknown/expired token → connection rejected before accept. One
live connection per token.

**Push event** (sent on the matching token's websocket after a write commits):

```json
{"subscriptionId":["<id>",...], "imdata":[{"<class>":{"attributes":{...,"status":"created|modified|deleted","dn":"<dn>"}}}]}
```

- `subscriptionId` is a list — every subscription on that token's session
  that matched the change (a client can hold multiple overlapping subs).
- `status` is `created` (DN didn't previously exist), `modified` (DN
  already existed), or `deleted` (DELETE, or POST body `status="deleted"`).
- A DELETE on a DN with children emits one event per removed MO in the
  subtree (root + all descendants), each carrying its own class/dn/status,
  so both class-scoped and DN-scoped subscribers see everything actually
  removed.

**Refresh**: `GET /api/subscriptionRefresh.json?id=<id>` — always 200
(`{"imdata":[],"totalCount":"0","subscriptionId":"<id>"}`), including for
an unknown/expired id (real APIC does not error a stale refresh). Default
subscription TTL is 90s, matching real APIC, renewed by this call.

**Lifecycle**: disconnecting the websocket immediately drops every
subscription owned by that connection's token — a subsequent matching
change is silently skipped (no error), same as a DN/class nobody
subscribed to.

Implementation: `aci_sim/rest_aci/subscriptions.py` (in-memory
registry + notify) and `aci_sim/rest_aci/app.py` (route wiring). See
README §10a for the user-facing walkthrough.

## 3. APIC — write path (Fabric Build apply)

- `POST /api/mo/{dn}.json` body `{"<class>":{"attributes":{"name":...,"dn":...,"status":"created|modified|deleted"?}, "children":[...]}}`.
  Response `{"imdata":[{"<class>":{"attributes":{"dn":...,"status":"created"}}}],"totalCount":"1"}` (HTTP 200).
  Nested `children` in the body must be upserted too (recursively). `status:"deleted"` removes.
- Writes must **upsert into the live MIT** so a later GET reflects them (read-back verification).
- **Atomic (all-or-nothing):** the entire body (root + every nested child, recursively) is shape-validated
  *before* any store mutation happens. A malformed node anywhere in the tree (e.g. a `children` entry that
  isn't a single-key `{className: {...}}` object) rejects the whole POST with a 400 APIC error envelope and
  leaves the store completely untouched — including MOs earlier in traversal order that would otherwise
  have been written first. Mirrors real APIC POST semantics.
- **Canonical RN synthesis:** a child posted without an explicit `dn` gets its RN derived from the class's
  real RN prefix (e.g. `fvBD` → `BD-{name}`, `fvAEPg` → `epg-{name}`, `fvSubnet` → `subnet-[{ip}]`), not a
  generic `{class}-{name}` guess — so it lands on the same DN a canonical GET or a later explicit-`dn` POST
  would use (no orphan duplicates). Classes without a known template fall back to `{class}-{name}`.
- **Node registration reaction:** `initial_fabric_discovery`/`add_leaf_switch_pair`/`add_spine_switch`
  POST `fabricNodeIdentP` at `uni/controller/nodeidentpol/nodep-{id}`. The sim reacts by
  materializing a full node-registration MO set, not just a bare `fabricNode`: `fabricNode`,
  `topSystem` (loopback + OOB addresses via the same scheme every boot-seeded node gets),
  `healthInst`, `fabricLink` (+enriched `lldpAdjEp`/`cdpAdjEp` and their `lldpInst`/`lldpIf`/
  `cdpInst`/`cdpIf` containers, v0.12.0 — same `build/neighbors.py` helper the boot-time
  builders use, so a dynamically-registered node's neighbors are queryable the identical way)
  cabling the new node to every spine in its site, and `l1PhysIf`/`ethpmPhysIf` port inventory
  (fabric uplinks + host-access ports) — so a dynamically-registered node renders identically
  to one seeded from `topology.yaml`. The
  reaction fires for a `fabricNodeIdentP` found **anywhere** in the POST body's validated write
  plan, not only when it is the top-level class (a `fabricNodeIdentP` nested under a parent, e.g.
  `uni/controller`, also triggers it). Pod is derived from the target site (`site.pod`), never
  hardcoded — `fabricNodeIdentP`/nodeidentpol DNs carry no pod of their own. `POST /_sim/add-leaf`
  shares this same enrichment.
- Multi-site playbooks push NDO schemas/templates/deploy (see §7) then per-site APIC MOs.

## 4. APIC — filter grammar (query-target-filter values)

- `eq(<class>.<attr>,"<val>")` exact match (numbers unquoted).
- `ne(<class>.<attr>,"<val>")` not-equal (a missing attr is treated as ≠, i.e. matches).
- `wcard(<class>.<attr>,"<substr>")` substring/wildcard (commonly on `.dn`).
- `gt|lt|ge|le(<class>.<attr>,"<val>")` compare — numeric when both sides parse as
  numbers (e.g. `gt(fabricNode.id,"200")`), else lexical.
- `bw(<class>.<attr>,"<lo>","<hi>")` inclusive range.
- `and(<c1>,<c2>,...)`, `or(<c1>,<c2>,...)` compose. Nesting allowed. Zero-argument
  `and()`/`or()` are rejected (not "match everything" / "match nothing").
- `query-target=subtree` scopes the match set to root+descendants FIRST, then applies
  `query-target-filter` to every scoped MO (root and descendants alike) — a filter on
  an attribute only descendants carry (e.g. `faultInst.severity` under a `fabricNode`
  root) still reaches matching descendants; it does not pre-filter the root out of the
  scope before expansion.
- A malformed expression (unknown operator, truncated call, trailing tokens after a
  complete expression, e.g. two comma-joined filters or an unbalanced trailing `)`)
  yields a **400 APIC error envelope** (`code="107"`), never a 500.
- Examples seen verbatim:
  `eq(fabricNode.role,"spine")`, `wcard(fvCtx.dn,"tn-Tenant1/")`,
  `and(eq(fvBD.name,"X"),wcard(fvBD.dn,"tn-T/"))`,
  `or(eq(faultInst.severity,"critical"),eq(faultInst.severity,"major"))`,
  `ne(fvTenant.name,"common")`, `gt(fabricNode.id,"200")`, `bw(fabricNode.id,"100","200")`.

## 5. APIC — error shapes / HTTP codes

- 200 success. 400 → ACIQueryError (also: malformed aaaLogin body, bad page params,
  strict-filter-parse errors). 401 → bad aaaLogin credentials
  ("Authentication failed: invalid username or password"). 403 → ACIAuthError —
  missing/unknown/expired `APIC-cookie` on any query/write route or on
  aaaRefresh ("Token was invalid (Error: Token timeout)").
  Error body: `{"imdata":[{"error":{"attributes":{"text":"<msg>","code":"<c>"}}}]}`.
  The `/_sim/*` control-plane router and `/api/aaaLogin.json`/`aaaLogout.json`
  are intentionally exempt from the 403 auth gate (test tooling + the login
  flow itself depend on them being reachable pre-auth).
- **`GET /api/mo/{dn}.json` on a nonexistent DN → 200 + empty envelope
  (`{"imdata":[],"totalCount":"0"}`), NOT 404** (PR-9 FEATURE 6, corrects a
  pre-PR-9 design choice that returned 404/ACINotFoundError here). Verified
  against upstream cisco.aci `plugins/module_utils/aci.py`'s `api_call()`:
  any GET response with `status != 200` is unconditionally fatal
  (`fail_json`) — there is no "404 means not-found, that's fine" special
  case. Every `state=present` module does a `get_existing()` GET *before*
  building its diff, so on a brand-new object that GET's DN does not exist
  yet; a 404 there fails the very first task of any create playbook
  (`fatal: APIC Error 103: Unable to find the object specified`). 404 is
  reserved for genuinely malformed requests, not absent-but-well-formed
  objects — matching real APIC. `/api/class/*` and `/api/node/class/*`
  already returned 200-empty for a query matching nothing; this brings
  `/api/mo/*` in line for the "DN itself absent" case too.
- **Any unmatched `/api/*` path → 400 APIC error envelope, never FastAPI's
  `{"detail":...}` shape** (PR-10). Real APIC never emits FastAPI's default
  404 body; `cisco.aci` cannot parse it and surfaces it as
  `"APIC Error None: None"`, `status=-1`. Body:
  `{"imdata":[{"error":{"attributes":{"text":"Invalid request path: <path>","code":"400"}}}],"totalCount":"1"}`.
  Implemented as a 404 exception-handler fallback scoped to `/api/*` — it
  only fires once every real route (including the `/api/node/mo` alias)
  has already failed to match, so it can never shadow one.

## 6. APIC — class catalog (102) + attributes actually read

Group by concern; every listed attribute is read by some plugin/router — include them.
(Header count corrected from a stale "63" to the actual number of distinct
classes catalogued below, verified by direct count — see PR-3b-batch1.
✅ marks classes seeded by PR-3b-batch1 (previously catalogued here but never
built, so their class queries returned empty and the depending autoACI
plugins rendered nothing); 🆕 marks classes seeded by PR-3b-batch2/3 (same
gap, closed in this batch); everything else was already built pre-batch-1.

**Batch-2/3 changelog note (this revision):**
- `vpcRsMbrIfs` → **corrected to `pcRsMbrIfs`**. The catalogued name was
  wrong: autoACI's `vpc_status.py` actually queries `pcRsMbrIfs` (the real
  ACI class for a port-channel member-port relation, child of `pcAggrIf`,
  RN `rsmbrIfs-[{ifDn}]`), never `vpcRsMbrIfs` — verified read-only against
  the plugin source before building. Built as `pcRsMbrIfs` in
  `build/fabric.py`; see docs/DESIGN.md "Batch-2" section.
- `infraNodeIdentP` → **removed from this catalog, NOT built.** No autoACI
  plugin/router references it at all (verified: a fabric-wide grep across
  `~/autoACI/backend/plugins/*.py` for `infraNodeIdentP` returns zero
  matches — unlike `fabricNodeIdentP`, which the Fabric Build write-path
  reaction genuinely needs). Real ACI's `infraNodeIdentP` is a *node
  provisioning-policy* object (`uni/infra/nodep-{id}`, part of interface
  selector/AEP scoping for a specific node), a materially different concept
  from `fabricNodeIdentP`'s Fabric Membership node registration
  (`uni/controller/nodeidentpol/nodep-{id}`) — the two are easy to confuse
  by name alone. Building an `infraNodeIdentP` that mirrors
  `fabricNodeIdentP`'s semantics (as an earlier draft of this catalog
  implied) would be a faithfulness regression: a wrong object with a
  plausible-looking name, satisfying no real consumer. Decision: leave it
  out of scope entirely rather than build a wrong object.

**Fabric/topology:** `fabricNode`(id,name,role∈{spine,leaf,controller},model,serial,version,adSt,fabricSt,dn) ·
`fabricLink`(dn contains `/lnk-{n1port}-to-{n2port}/`, n1,n2,operSt,operSpeed,linkType) ·
`topSystem`(dn,fabricDomain,version,role,state,address/oobMgmtAddr) ·
`fabricHealthTotal`(cur) · `healthInst`(cur,min,max,prev) ·
`vpcDom`(id,dn,peerSt) · `vpcIf`(dn,name,operSt) · `pcAggrIf`🆕(dn,name,id,operSt) · `pcRsMbrIfs`🆕(tDn,parentSKey) · `infraWiNode`🆕(health,state) ·
`firmwareCtrlrRunning`🆕(version,node) — PR-9, one per controller at `{ctrl_dn}/ctrlrfwstatuscont/ctrlrrunning`.

**Mgmt tenant — OOB management (OOB-gateway PR):** real APIC models each
node's OOB management address + default gateway in the special `mgmt`
tenant, never on `topSystem` itself (`topSystem.oobMgmtAddr` carries only the
address — see above). Prior to this PR, `aci-sim init`'s wizard collected an
OOB gateway per site but discarded it entirely (no schema field, no MO).
`build/mgmt.py` now builds the minimal faithful scaffolding, one tree per
site: `fvTenant`(name="mgmt", dn=`uni/tn-mgmt`) →
`mgmtMgmtP`(name="default", dn=`uni/tn-mgmt/mgmtp-default`) →
`mgmtOoB`(name="default", dn=`uni/tn-mgmt/mgmtp-default/oob-default`) → one
`mgmtRsOoBStNode`(tDn,addr,gw) per node — switches AND controllers — at
`{oob-default dn}/rsooBStNode-[topology/pod-{p}/node-{id}]`, where
`tDn=topology/pod-{p}/node-{id}`, `addr=<node's OOB IP>/<mask>` (same
`oob_ip(pod,node_id)` address `topSystem.oobMgmtAddr` uses; mask from
`fabric.oob_subnet` if set, else `/24` — see build/mgmt.py docstring for why
not `fabric.oob_pool`'s `/16`), and `gw=site.oob_gateway` (empty string if
unset — never invented/derived). No other mgmt-tenant children (`mgmtInB`,
`mgmtRsOoBCtx`, `mgmtInstP`, ...) are built — no autoACI/aci-py chain
audited for this sim reads them; same scope judgment as
`fabric.inb_subnet`'s store-only status (README §6c).

**Faults:** `faultInst`(dn,severity∈{critical,major,minor,warning},code,lc,type,subject,descr,created,lastTransition).
Fault DNs live under node-local shards; node-scoped query via `/api/node/class/{nodeDn}/faultInst.json`.

**Interfaces:** `l1PhysIf`(id,adminSt,descr,mtu,mode) · `ethpmPhysIf`(dn has `phys-[eth{s}/{p}]`,operSt,operSpeed,usage,lastLinkStChg,resetCtr) · `ethpmFcot`🆕(typeName,guiName,vendorName,vendorSn,actualType).
PR-19: the ISN/IPN spine uplink `l1PhysIf.mtu` is now `isn.mtu`-driven (default 9150) instead of the general fabric `9216` hardcode every other port still uses.

**Neighbors/underlay:** `lldpAdjEp`(dn,sysName,mgmtIp,portIdV,portIdT,chassisIdT,chassisIdV,sysDesc,capability,id,ttl) ·
`cdpAdjEp`(dn,sysName,mgmtIp,devId,portId,platId,ver,cap) ·
`lldpInst`🆕(dn,adminSt,name,holdTime,initDelayTime,txFreq,ctrl,optTlvSel) · `lldpIf`🆕(dn has `if-[eth{s}/{p}]`,id,adminRxSt,adminTxSt,operRxSt,operTxSt,mac,portDesc,portVlan,sysDesc,wiring) ·
`cdpInst`🆕(dn,adminSt,name,holdIntvl,txFreq,ver,ctrl) · `cdpIf`🆕(dn has `if-[eth{s}/{p}]`,id,adminSt,operSt) ·
`isisAdjEp`(dn,operSt,sysId,lastTrans,numAdjTrans) · `isisDom` · `ospfAdjEp`(dn,operSt,id,peerIp/addr,nbrId,area) · `ospfIf`✅(name,area,state,helloIntvl,deadIntvl,addr) · `arpAdjEp`✅(ip,mac,ifId,physIfId,operSt).
LLDP/CDP fidelity (v0.12.0): `lldpAdjEp`/`cdpAdjEp` are now enriched with real-APIC-shaped fields
(`portIdV`/`portId` carry the NEIGHBOR's own port; `chassisIdV` is a deterministic per-node MAC,
see `build/neighbors.py:node_mac`; `sysDesc`/`platId`/`ver` describe the neighbor's model+version, or
"Cisco APIC"/`APIC-SERVER-M3` for a controller neighbor) — additive only, pre-existing `sysName`/
`mgmtIp` are unchanged. `lldpInst`/`cdpInst`/`lldpIf`/`cdpIf` are newly materialized containers (one
inst per switch node with any adjacency, one `*If` per adjacency-bearing local port) — this also fixes
a bug where a deep-root subtree query (`GET .../sys/lldp/inst.json?query-target=subtree&
target-subtree-class=lldpAdjEp`) returned `totalCount=0` because the root DN had no MO of its own.
`aci-sim lldp` (CLI) prints a `show lldp neighbors`-style table from these fields (`--cdp` for CDP,
`--json` for machine-readable output).
PR-19: `ospfIf.area`/`ospfAdjEp.area` are now `isn.ospf_area`-driven (default `"0.0.0.0"`) instead of a hardcoded literal.
PR-20: `ospfIf.helloIntvl`/`deadIntvl` are new, driven by `isn.ospf_hello`/`isn.ospf_dead` (defaults 10/40, real ACI defaults) — previously not carried on this MO at all.
PR-22: the ISN-facing `ospfIf`/`ospfAdjEp` on each multi-site spine now sit on a routed VLAN-4 sub-interface (`id`/dn `eth1/{49+si}.{49+si}` — port-named per real ACI spine CLI (Eth1/29.29-style); was the bare physical port `eth1/{49+si}`) and `ospfIf` gained an `addr` attribute (`172.16.{site.id}.{si+1}/24`) — matching real ACI multi-site spines, which carry the ISN OSPF address on a sub-if rather than the physical port. The underlying physical port's `l1PhysIf`/`ethpmPhysIf` (interfaces.py) are unchanged. Each spine's ISN uplink physical port also gained a simulated `lldpAdjEp`/`cdpAdjEp` (`sysName=ISN-CSW{site.id}`, `portIdV=Ethernet1/{si+1}.4` (the ISN Nexus names its dot1q-4 sub-if after the VLAN), model `N9K-C9364C`) via `build/fabric.py`, the third `add_adjacency` call site — see docs/DESIGN.md's "PR-22" section.

**Overlay/BGP:** `bgpInst`(dn,asn) · `bgpPeer`(dn,asn,addr,type,peerRole) ·
`bgpPeerEntry`(dn,addr,operSt∈{established,...},rtrId,lastFlapTs,connEst,connDrop,type) ·
`bgpPeerAfEntry`(dn,type/afId,acceptedPaths,pfxSent,tblVer) · `bgpVpnRoute`🆕(pfx,rd) · `bgpPath`🆕(nh,asPath,localPref,metric,origin,type).
For ISN: each spine `/sys/bgp/inst` subtree must contain `bgpPeer` entries whose addr is a **/32** and whose ASN ≠ the local fabric ASN (autoACI infers the inter-site EVPN cloud from this).

**COOP/EPM:** `coopPol`,`coopInst`(operSt) · `epmMacEp`(addr,ifId,flags,createTs,encap) · `epmIpEp`(addr,ifId,flags,createTs).

**Routing tables/control:** `uribv4Route`✅,`uribv4Nexthop`✅ · `rtctrlProfile`✅,`rtctrlCtxP`✅,`rtctrlSubjP`✅,`rtctrlMatchRtDest`✅,`rtctrlSetComm`✅,`rtctrlSetPref`✅.

**Tenant (control):** `fvTenant`(name,descr,dn) · `fvCtx`(name,pcEnfPref,bdEnforcedEnable,ipDataPlaneLearning,pcEnfDir,pcTag,scope) ·
`fvBD`(name,arpFlood,unicastRoute,ipLearning,unkMacUcastAct,unkMcastAct,multiDstPktAct,epMoveDetectMode,limitIpLearnToSubnets,mtu,mac,intersiteBumTrafficAllow,type) ·
PR-20: `fvBD.mac` now reflects `bd.mac` (per-BD) falling back to `fabric.default_bd_mac` (fabric-wide, default `"00:22:BD:F8:19:FF"` — the exact literal already hardcoded here pre-PR-20, so unset topology.yaml fields produce byte-identical output).
`fvSubnet`(ip,scope) · `fvAp`(name) · `fvAEPg`(name,prefGrMemb,floodOnEncap,pcEnfPref,isAttrBasedEPg,pcTag) ·
`fvCEp`(mac,ip,encap,fabricPathDn) · `fvIp`(addr) · `fvIfConn`(dn has tn-/ap-/epg-, encap) · `fvEpP`🆕(epgPKey,pcTag,scopeId).
**Rels:** `fvRsBd`(tnFvBDName) · `fvRsCtx`(tnFvCtxName) · `fvRsBDToOut`(tnL3extOutName) ·
`fvRsDomAtt`(tDn,instrImedcy) · `fvRsPathAtt`✅(tDn,encap,mode,instrImedcy) · `fvRsCons`(tnVzBrCPName) · `fvRsProv`(tnVzBrCPName).

**Contracts:** `vzBrCP`(name,scope,prio,targetDscp) · `vzSubj`(name,revFltPorts) · `vzFilter`(name) ·
`vzEntry`(name,etherT,prot,dFromPort,dToPort,sFromPort,sToPort,stateful) · `vzRsSubjFiltAtt`(tnVzFilterName) · `vzRsSubjGraphAtt`✅ · `actrlRule`🆕(scopeId,sPcTag,dPcTag,fltId,action,direction,prio,ctrctName,operSt).

**L3Out (control):** `l3extOut`(name,enforceRtctrl) · `l3extLNodeP`(name) · `l3extRsNodeL3OutAtt`(tDn,rtrId) ·
`l3extLIfP`(name) · `l3extRsPathL3OutAtt`(tDn,encap,addr,ifInstT) · `l3extInstP`(name,prefGrMemb) ·
`l3extSubnet`(ip,scope) · `l3extRsL3DomAtt`(tDn) · `l3extRsEctx`(tnFvCtxName,tDn) · `l3extMember`✅(side,addr) ·
`bgpPeerP`(addr,peerCtrl,keepAliveIntvl,holdIntvl) · `bgpAsP`(asn) · `bgpExtP` · `ospfExtP`✅ · `ipRouteP`✅(ip,pref) · `ipNexthopP`✅(nhAddr).
PR-20: `bgpPeerP.keepAliveIntvl`/`holdIntvl` are new, driven by `l3out.csw_peer.keepalive`/`.hold` (defaults 60/180, real ACI defaults) — previously not carried on this MO at all.

**Access policy:** `infraAttEntityP`(AAEP,name) · `infraAccPortGrp`,`infraAccBndlGrp`✅(name,lagT) · `infraAccPortP`✅(name) ·
`infraHPortS`✅(name,type) · `infraPortBlk`✅(fromPort,toPort,fromCard) · `infraRsAttEntP`(tDn) · `infraRsAccBaseGrp`✅(tDn) ·
`infraRsDomP`(tDn) · `infraRsVlanNs`(tDn) · `physDomP`(name) · `l3extDomP`(name) · `fvnsVlanInstP`(name,allocMode) · `fvnsEncapBlk`(from,to) ·
`vmmDomP`🆕(name) · `vmmCtrlrP`🆕(name,hostOrIp,rootContName,dvsName,dvsVersion) · `vmmUsrAccP`🆕(name) — PR-19, Tier-2
`access.vmm_domains[]`; `vmmCtrlrP`/`vmmUsrAccP` only emitted when `vcenter_ip` is set. Pure addition — no
existing plugin/production-chain reads these (see docs/DESIGN.md's PR-19 section for the verified
`bind_epg_to_vmm_domain` DN-string-only finding).

**Infra write targets (Fabric Build):** `fabricNodeIdentP`🆕(nodep-{id},serial,nodeId,name,role) · `infraInfra`,`infraAttEntityP`, VLAN pools/AAEPs.
(`infraNodeIdentP` intentionally removed from this catalog — see the batch-2/3 changelog note above.)

**Capacity/backup:** `eqptcapacityPolUsage5min`(polUsage,polUsageCap,polUsageCum,polUsageCapCum) · `configExportP`(name) · `configJob`(operSt,executeTime).

## 7. NDO / MSO contract

- `POST /api/v1/auth/login` `{"userName","userPasswd","domain":"local"}` → `{"token":...}` (Bearer header after).
  **Deliberately lenient**: accepts ANY credential and never validates the returned
  token on subsequent requests (client-compat for `cisco.mso`/aci-py, which expect a
  login to just work). This is unchanged by the admin-account wizard (§1) — NDO
  shares the APIC admin account by default but is never actually checked against it.
- `GET /api/v1/platform/version` → `{"version":"4.2.2"}`.
- `GET /mso/api/v1/sites` → `{"sites":[{id,name,platform,urls[]}]}`. **`name` is
  the topology's `fabric_name`** (e.g. `"LAB1-IT-ACI"`), NOT the short
  internal site name (`"LAB1"`) — PR-10, verified against a real-gear
  `cisco.mso.mso_tenant` run that rejected the short form
  (`"Site 'LAB1-IT-ACI' is not a valid site name"` when only `"LAB1"` was
  exposed). Internal site-id joins (tenant/schema site associations) key off
  the topology's short name regardless of what `name` reports here.
- `GET /mso/api/v1/tenants` (also `GET /api/v1/tenants` — PR-10, backs
  `cisco.mso.ndo_template`'s `lookup_tenant()` prereq lookup, same data) →
  `{"tenants":[{id,name,displayName,siteAssociations:[{siteId}]}]}`.
- `GET /mso/api/v1/schemas` → `{"schemas":[{id,displayName,name,templates:[{name}]}]}`.
- `GET /mso/api/v1/schemas/list-identity` (PR-10) → `{"schemas":[{id,displayName}]}`
  — lightweight enumeration every `cisco.mso` schema module calls first to
  resolve a schema `displayName` → `id` (verified against `ansible-mso`'s
  `plugins/module_utils/mso.py` `lookup_schema()`, which calls
  `query_objs("schemas/list-identity", key="schemas", displayName=schema)`).
  **Must be declared before** the parameterized `/schemas/{id}` route below,
  or `"list-identity"` gets matched as a schema id instead.
- `GET /mso/api/v1/schemas/{id}` → full schema; template shape:
  `templates[].{name,templateType,vrfs[],bds[],anps[].epgs[],contracts[],filters[],externalEpgs[]}` and top-level `sites[].{templateName,siteId}`.
  - vrf: `{name,displayName,vrfRef:"/vrfs/NAME",vzAnyEnabled,ipDataPlaneLearning}`
  - bd: `{name,vrfRef:"/vrfs/NAME",subnets:[{ip}],l2Stretch,intersiteBumTraffic,l2UnknownUnicast,arpFlood,unicastRouting,epMoveDetectMode,dhcpLabels:[{ref:UUID}]}`
  - epg: `{name,bdRef:"/bds/NAME",preferredGroup,proxyArp,uSegEpg,intraEpg,contractRelationships:[{relationshipType,contractRef:"/contracts/NAME"}]}`
  - contract: `{name,scope,filterRelationships:[{filterRef:"/filters/NAME"}]}`  filter: `{name,entries:[{name,ipProtocol,dFromPort,dToPort,etherType}]}`
  - externalEpg: `{name,vrfRef,l3outRef:"/l3outs/NAME",type,subnets:[{ip}]}`
- `GET /mso/api/v1/sites/fabric-connectivity` → `{"sites":[{id/siteId,status,connectivityStatus}]}` (or list).
- `GET /mso/api/v1/schemas/{id}/policy-states` → `[{status,drift}]` or `{policyStates:[...]}`.
- `GET /mso/api/v1/templates/summaries` → `[{templateId,templateName,templateType}]` (tenantPolicy carries DHCP;
  `templateName` required — PR-13 — since `ansible-mso`'s `MSOTemplate` resolves templates by name+type).
- `GET /mso/api/v1/templates/{id}` → `{tenantPolicyTemplate:{template:{dhcpRelayPolicies:[{uuid,name}],dhcpOptionPolicies:[{uuid,name}]}}}`.
- **Template writes (PR-13):** `POST /templates` (create — payload
  `{displayName,templateType,<typeContainer>:{template:{...},sites:[{siteId}]}}`,
  e.g. `tenantPolicyTemplate`), `PATCH /templates/{id}` (JSON-Patch ops,
  e.g. `add /tenantPolicyTemplate/template/dhcpRelayPolicies/-`), `DELETE
  /templates/{id}`. **Both bare (`/api/v1/...`) and `/mso`-prefixed
  (`/mso/api/v1/...`) forms are backed by the same store** — see §7c.
- `GET /templates/objects?type={dhcpRelay|dhcpOption|epg|externalEpg}&{uuid=...|name=...}`
  (both prefixes) — cross-template object lookup; `dhcpRelay`/`dhcpOption`
  search every tenant-policy template's policy lists, `epg`/`externalEpg`
  search every schema template's `anps[].epgs[]`/`externalEpgs[]`. Returns
  a list when only `type` (or `type`+`name`) is given, a single object (or
  `{}`) when `uuid` is given.
- `GET /mso/api/v1/schemas/service-node-types` (also bare) →
  `{"serviceNodeTypes":[{id,displayName}]}` (Firewall/Load Balancer/Other).
- `GET /api/v1/audit-records?count=N` (fallback `/mso/api/v1/audit-records`) → `{auditRecords:[{timestamp,user,action,description,details}]}` (or records/imdata/list).
- **Writes:** `POST /mso/api/v1/schemas`, `POST /mso/api/v1/deploy` → accept + store; return an id/status. Cross-plane rule: NDO tenants/VRFs/BDs must match APIC MIT names.
- Errors: 401 → NDOAuthError; else propagate. Router returns `{"detail":...}`.

### 7a. NDO schema write round-trip (PR-11)

The multi-site (`MS`) aci-ansible playbooks create a per-tenant-region schema (`<tenant>-<region>`, e.g. `"MS-TN1-LAB0"`) during `create_tenant`'s MSO play, then PATCH it from every subsequent playbook (`create_bd`, `create_application`, ...). Verified against `ansible-mso`'s `plugins/module_utils/{mso,schema}.py` and a real multi-site E2E run against ACI hardware.

- **Sequence:** `GET schemas/list-identity` (displayName→id) → `GET schemas/{id}` (full doc) → `PATCH schemas/{id}` with a JSON-Patch-shaped op list `[{"op":"add"/"replace"/"remove","path":...,"value":...}]`. `POST /mso/api/v1/schemas` creates a schema (optionally already carrying its first template — `mso_schema_template.py`'s "schema doesn't exist yet" branch); `PATCH` applies ops against the stored doc and returns the updated doc. Implemented in `aci_sim/ndo/patch.py` (`apply_json_patch`), wired into `aci_sim/ndo/app.py`'s `create_schema`/`patch_schema` routes.
- **Path addressing — three conventions cisco.mso modules use, all supported:**
  1. Standard RFC 6902: numeric index, or `"-"` to append.
  2. Named-segment lookup against a `name`/`displayName` field — most template-level objects (`bds`/`anps`/`epgs`/`contracts`/`filters`/`externalEpgs`/`vrfs`), e.g. `/templates/LAB1/bds/bd-App1_LAB1`.
  3. A synthetic **composite key** `"{siteId}-{templateName}"` unique to the top-level `sites[]` array — every `mso_schema_site_*` module builds this literally (`site_template = "{0}-{1}".format(site_id, template)`), e.g. `/sites/1-LAB1/bds/-`.
  4. A **`*Ref`-name** lookup for site-local child objects, which carry no `name` field of their own — only a `bdRef`/`vrfRef`/etc string pointing back at the template object — yet are still addressed by that object's bare name: `/sites/1-LAB1/bds/bd-App1_LAB1/subnets/-`.
- **Collection-default normalization:** real NDO always serves back every collection key (`vrfs`/`bds`/`anps`/`contracts`/`filters`/`externalEpgs`/`intersiteL3outs`/`serviceGraphs`, and nested `subnets`/`epgs`/`contractRelationships`/...) as `[]` even when a client's create/add payload omitted them. `normalize_template()`/`normalize_site()` backfill these on every create and every PATCH — a missing key is a hard client-side crash (`'NoneType' object is not iterable`, bare `KeyError`), not a 4xx, so this has to be done proactively rather than reactively.
- **`*Ref` string normalization:** several write payloads (e.g. `mso_schema_site_bd.py`'s `bdRef=dict(schemaId=...,templateName=...,bdName=...)`) send a *dict* form of a `*Ref` field, but real NDO stores/serves it as the canonical **string** `/schemas/{schemaId}/templates/{templateName}/{category}/{name}` — confirmed because a sibling module (`custom_mso_schema_site_bd_subnet.py`) does `bd_ref_string in [v.get('bdRef') for v in ...]` and `', '.join(...)` on the stored values; a dict there crashes with `TypeError: sequence item 0: expected str instance, dict found`. `apply_json_patch` stringifies `bdRef`/`vrfRef`/`l3outRef`/`filterRef`/`contractRef`/`anpRef`/`serviceGraphRef` dicts on every `add`/`replace` op.
- **ND user-class fallback:** `GET /api/config/class/remoteusers` / `/localusers` → `{"items":[{"spec":{"loginID",...,"id"|"userID"}}]}`. `cisco.mso.mso_tenant`'s `lookup_users()`/`lookup_remote_users()` query the modern `/nexus/infra/api/aaa/v4/*` routes first and fall back to these legacy routes when both come back empty; a 404 here (no route at all) aborts with `"ND Error: Unknown error no error code in decoded payload"` since the error body has neither `code` nor `messages`.
- **Bare `GET /api/v1/sites`** (PR-11) — `cisco.mso.ndo_template`'s TenantPol prereq lookup hits this un-prefixed path; same data as `/mso/api/v1/sites`.
- **`PUT`/`POST /mso/api/v1/tenants{,/{id}}`** — `mso_tenant`'s create-vs-update branch: every tenant this sim seeds already exists in `GET /mso/api/v1/tenants`, so a real run always takes the `PUT .../tenants/{id}` (update) path.
- **Schema-template deploy:** `GET schemas/{id}/validate` (legacy + `ndo_schema_template_deploy`, discarded return value, only status matters), `GET execute|status/schema/{id}/template/{name}` (legacy `mso_schema_template_deploy`), `POST /mso/api/v1/task` `{schemaId,templateName,isRedeploy}` (`ndo_schema_template_deploy` — the module aci-ansible's `mso-model` role actually invokes; confirmed against the collection installed on real ACI hardware, which differs from `ansible-mso`'s `master`-branch module source).
- **Site-local ANP/EPG auto-mirroring + self-referencing anpRef/epgRef (PR-12).** Two compounding gaps, both required to close `bind_epg_to_static_port`:
  1. **Template-level self-refs.** Real NDO stores a self-referencing `anpRef` string on every template-level ANP object itself (and `epgRef` on every EPG) — confirmed because `ansible-mso`'s `mso_schema_template_anp.py`/`_anp_epg.py` explicitly strip it client-side before an idempotency comparison (`if "anpRef" in mso.previous: del mso.previous["anpRef"]`) — dead code unless the server actually sends one back. `MSOSchema.set_site_anp()`/`set_site_anp_epg()` (`ansible-mso`'s `module_utils/schema.py`) key their site-local lookup off exactly this field (`template_anp.details.get("anpRef")`); without it, the lookup compares every site-anp's `anpRef` against `None` and never matches. `normalize_template()` (`aci_sim/ndo/patch.py`) now backfills `anpRef`/`epgRef` on every `anps[]`/`epgs[]` entry it normalizes (`/schemas/{id}/templates/{t}/anps/{a}` and `/schemas/{id}/templates/{t}/anps/{a}/epgs/{e}` respectively), and takes an optional `schema_id` param to build them; `build_ndo_model()` (`aci_sim/ndo/model.py`) calls it at boot too, so a topology tenant that ships with ANPs/EPGs already configured doesn't skip this.
  2. **Site-local mirroring.** Real NDO 4.x auto-populates a schema's site-local `sites[].anps[].epgs[]` the moment a template-level ANP/EPG is added to a template that already has a site attached (`mso_schema_site.py` already ran in `create_tenant`'s flow, per PR-11). `apply_json_patch` replicates this: an `add /templates/{t}/anps/-` op auto-creates a matching `{anpRef, epgs:[]}` entry in every `sites[]` row whose `templateName == t`; an `add /templates/{t}/anps/{a}/epgs/-` op auto-creates a matching **full-shaped** site-EPG `{epgRef, subnets:[], staticPorts:[], staticLeafs:[], domainAssociations:[]}` entry under that mirrored site-anp. `build_ndo_model()` performs the same mirroring at boot for topology-seeded schemas. Both are idempotent (safe on playbook retries). The full child-collection shape is mandatory: every sibling `mso_schema_site_anp_epg_*` module reads its own array with a **bare subscript** (not `.get()`), so any missing key is a hard `KeyError` client-side — `mso_schema_site_anp_epg_domain.py` bare-subscripts `domainAssociations` (`domains = [dom.get("dn") for dom in ...["epgs"][epg_idx]["domainAssociations"]]`), `_staticleaf` reads `staticLeafs`, `_staticport` reads `staticPorts`, `_subnet` reads `subnets`. A real hardware MS-TN1 run regressed on `bind_epg_to_physical_domain`/`_vmm_domain` with `KeyError: 'domainAssociations'` when the mirrored site-EPG shipped without it (pre-PR-12 those binds passed only because no site-EPG existed at all, so the module took a non-crashing branch). Single source of truth for the shape is `SITE_OBJECT_DEFAULTS["epgs"]`, consumed by both `_new_site_epg()` (mirror path) and `normalize_site()` (backfill path).

  Both `*Ref` strings use NDO's canonical form — `anpRef`: `/schemas/{id}/templates/{t}/anps/{a}` (`_REF_NAME_FIELDS`); `epgRef` uniquely nests **two** name segments, `/schemas/{id}/templates/{t}/anps/{a}/epgs/{e}` (confirmed against `ansible-mso`'s `module_utils/mso.py` `epg_ref()` — handled by a dedicated `_stringify_refs` branch since the generic single-category table only fits one name field). `_REF_LOOKUP_KEYS` also gained `epgRef` so a site-epg (ref-only, no `name` of its own) resolves by its bare EPG name the same way site-local bds/anps already did (PR-11).

  Without either fix, `mso_schema_site_anp_epg_staticport.py`'s own "create site anp/epg if missing" fallback fires instead (its own code comment: *"Coverage misses this two conditionals when testing on 4.x and above"* — i.e. it's dead code on real hardware) and ultimately crashes with `'NoneType' object has no attribute 'details'` — confirmed on a real MS-TN1 `bind_epg_to_static_port` run on ACI hardware before this fix (schema=MS-TN1-LAB0, anp=app-Web_LAB1, epg=epg-web, site=LAB1-IT-ACI, static vpc port `ipg-vpc-LegacySW01` vlan-100), now resolved end-to-end.

### 7c. Tenant-policy-template DHCP surface + schema serviceGraphs (PR-13)

Closes the last two NDO write-surface gaps blocking the MS suite's DHCP and service-graph tasks. Verified against `ansible-mso`'s `plugins/modules/{ndo_template,ndo_dhcp_relay_policy,ndo_schema_template_bd_dhcp_policy}.py`, `plugins/module_utils/{mso,template}.py`, the `mso-model` role's `library/custom_mso_schema_service_graph.py`, and real multi-site E2E runs against ACI hardware.

- **Bare vs `/mso`-prefixed split, and why it matters for templates.** `ansible-mso`'s `MSOModule.request()` builds its URL differently depending on connection mode: tasks using the persistent `ansible.netcommon.httpapi` connection (`ansible_network_os: cisco.nd.nd`) route through `NDO_API_VERSION_PATH_FORMAT = "/mso/api/{v}/{path}"`; tasks carrying `delegate_to: localhost` (no persistent httpapi socket) fall through `MSOModule`'s direct-HTTP branch, which never adds the `/mso` prefix at all — `self.url = "{0}api/{1}/{2}".format(base_uri, api_version, path)`. `prereq_tenantpol.yml`'s `cisco.mso.ndo_template` task uses `delegate_to: localhost`, so it hits `GET/POST https://host/api/v1/templates*` (BARE) while `create_dhcp_relay`'s `cisco.mso.ndo_dhcp_relay_policy` and `bind_dhcp_relay_to_bd`'s `cisco.mso.ndo_schema_template_bd_dhcp_policy` (no `delegate_to`) hit the `/mso`-prefixed form. Both forms are registered against the SAME mutable template store (`aci_sim/ndo/app.py`'s `_get_template_summaries`/`_create_template`/`_patch_template`/`_get_template_objects`, looped over `("/mso/api/v1", "/api/v1")`), matching the existing `/api/v1/tenants` ↔ `/mso/api/v1/tenants` / `/api/v1/sites` ↔ `/mso/api/v1/sites` pattern PR-10/PR-11 established for the identical `delegate_to` split.
- **Template create/lookup sequence.** `ndo_template`'s `MSOTemplate.__init__` first queries `templates/summaries?templateName=...&templateType=...` (name+type filter; `schemaName`/`schemaId` kwargs are `None` and skipped by `query_objs()`) — every summary entry therefore needs a `templateName` field, not just `templateId`/`templateType` (the boot-seeded tenantPolicy template summary was retrofitted with one). If nothing matches, it `POST`s `{displayName, templateType, tenantPolicyTemplate:{template:{tenantId}, sites:[{siteId}]}}` to `templates` (no `/summaries` suffix) — the sim assigns a `templateId`, stores the full doc, and mirrors a summary entry so the very next `templates/summaries` lookup (by any caller, either path prefix) resolves it.
- **DHCP relay policy add + uuid backfill.** `ndo_dhcp_relay_policy`'s add PATCHes `add /tenantPolicyTemplate/template/dhcpRelayPolicies/-` with `{name, providers, description?}` — no `uuid` (real NDO assigns one server-side). A provider's `epgRef` is the target schema-template EPG's `uuid` (`MSOSchema.set_template_anp_epg().details.get("uuid")`) — schema-template EPGs/externalEpgs never carried a `uuid` field before this PR; `normalize_template()` now backfills a stable one (`_normalize_object`'s `epgs`/`externalEpgs` branches). `ndo_schema_template_bd_dhcp_policy` (`bind_dhcp_relay_to_bd`) resolves a relay/option policy's uuid via `GET templates/objects?type={dhcpRelay|dhcpOption}&name=...`, requiring both a `tenantId` (matched client-side against the policy-owning template) and a non-empty `uuid` on the returned entry, or it `fail_json`s — the sim's `_patch_template` backfills a stable uuid on every dhcpRelayPolicies/dhcpOptionPolicies entry that lacks one. The same `templates/objects` route also serves `type=epg`/`type=externalEpg` queries (searching every schema template's `anps[].epgs[]`/`externalEpgs[]`), used by `ndo_dhcp_relay_policy`'s own uuid→name resolution on query/present.
- **Site-level `serviceGraphs` + full-detail `GET /schemas`.** The mso-model role's "Create service graph" task uses `custom_mso_schema_service_graph.py`, a pre-`MSOTemplate`/`MSOSchema` community module (`version_added: '2.8'`) that bare-subscripts straight into the RAW `GET /schemas` list response: `mso.get_obj('schemas', displayName=schema)` then `schema_obj.get('templates')[idx]['serviceGraphs']` and, once a site-local device binding step runs, `schema_obj.get('sites')[site_idx]['serviceGraphs']`. The template-level bare-subscript was already covered (PR-11's `TEMPLATE_COLLECTION_KEYS`), but (a) the sim's `GET /mso/api/v1/schemas` previously returned only a trimmed `{id,displayName,name,templates:[{name}]}` summary — insufficient for this module's bare-subscript pattern, which needs the full nested doc (`bds`/`anps`/`serviceGraphs`/`sites`) — and (b) the SITE-level `serviceGraphs` key was never backfilled at all. Both fixed: `GET /schemas` now returns full schema detail per entry (real NDO's plain list endpoint already does this — it's precisely why the lighter `schemas/list-identity` endpoint exists as a separate optimization); `SITE_TOP_LEVEL_DEFAULTS` (`aci_sim/ndo/patch.py`) backfills `serviceGraphs` (and `contracts`, see below) as top-level keys on every schema `sites[]` entry via `normalize_site()`. The module also needs `GET schemas/service-node-types` (Firewall/Load Balancer/Other → stable id) and a `tenantId` field on the schema template itself (used on its "service graph already exists, add a node" branch) — both added.
- **Site-local contracts mirroring (deeper layer, found chasing GAP 2 to green on real ACI hardware).** Once the `serviceGraphs` KeyError was fixed, a real hardware MS-TN2 `create_tenant` pass-2 run (`-e automate_contract_graph=true -e automate_site_redirect=true`) progressed to the mso-model role's raw `cisco.mso.mso_rest`-driven "Atomic PATCH — bind service-graph redirect on ALL fabrics in one request" task, which PATCHes `add /sites/{siteId}-{template}/contracts/{contract}/serviceGraphRelationship` — addressing a SITE-LOCAL contract by bare name, mirroring the same addressing convention site-local bds/anps already use. The sim never mirrored template-level contract adds into `sites[].contracts[]` at all (unlike anps/epgs, mirrored since PR-12), so `apply_json_patch`'s `_find_by_name` raised `PatchError: path segment 'con-Firewall_LAB0' not found in list` — confirmed verbatim on the real run. `_mirror_template_contract_to_sites()` (PATCH-time, parallel to `_mirror_template_anp_to_sites`) and an equivalent boot-time mirror in `build_ndo_model()` close this: an `add /templates/{t}/contracts/-` op (or a topology-seeded template that already has contracts) now auto-creates a matching `{contractRef}` entry in every `sites[]` row whose `templateName == t`.
- **Verified on real ACI hardware end-to-end:** MS-TN1 `create_dhcp_relay` (08) and `bind_dhcp_relay_to_bd` (09) both reach `failed=0`; MS-TN2's identical DHCP chain (08/09) also `failed=0`; MS-TN2 `create_tenant` pass-2 (01b, `-e automate_contract_graph=true -e automate_site_redirect=true`) reaches `failed=0` — including the atomic per-fabric service-graph redirect PATCH. No regression observed on MS-TN1/MS-TN2's already-green tasks (01-07, 10, 01a) or a SF-suite spot-check (create_tenant, create_dhcp_relay) re-run on the same sandbox.

### 7d. Site-local BD auto-mirroring for aci-py (PR-15)

Closes a schema-PATCH 400 mid `create_tenant`/`create_bd`, found running `aci-py` (a pure-Python Ansible-to-ACI compiler, distinct from the `ansible-mso`/`cisco.mso` collection §7a-7c document) against the sandbox on real ACI hardware. Same family of gap as PR-12's ANP/EPG mirroring and PR-13's contract mirroring — this closes the missing BD sibling.

- **Why `mso_schema_site_bd` always sends `replace`, never `add`.** aci-py's `mso_schema_site_bd` shim (mirroring `ansible-mso`'s `mso_schema_site_bd.py`) PATCHes a site-local BD shadow with `op: replace` unconditionally — its own docstring/comment documents why: real NDO 4.x auto-creates the site BDDelta shadow the instant a template-level BD is added to a template that already has a site attached, so by the time the site-BD module runs the shadow already exists and a fresh `add` would 409 ("Multiple BDDelta entries") on real hardware; the module does a GET-then-replace instead.
- **The gap.** The sim mirrored template-level ANP/EPG adds into `sites[].anps[].epgs[]` (PR-12) and contract adds into `sites[].contracts[]` (PR-13) but never mirrored template-level BD adds into `sites[].bds[]` at all. So the very first `replace /sites/{siteId}-{t}/bds/{bd}` against a schema that had only ever seen the template-level `add /templates/{t}/bds/-` hit `_find_by_name`'s "not found" branch: `PatchError: replace: 'bd-FW_LAB0' not found at '/sites/1-LAB1-LAB2/bds/bd-FW_LAB0'` — confirmed verbatim against a real hardware aci-py `create_tenant`/`create_bd` run (MS-TN2) before this fix.
- **Fix.** `_mirror_template_bd_to_sites()` (`aci_sim/ndo/patch.py`, PATCH-time, parallel to `_mirror_template_anp_to_sites`/`_mirror_template_contract_to_sites`): an `add /templates/{t}/bds/-` op now auto-creates a matching site-local BD shadow (`_new_site_bd()`: `{bdRef, hostBasedRouting: False, subnets: []}` — the full child-collection shape `mso_schema_site_bd_subnet.py` bare-subscripts) in every `sites[]` row whose `templateName == t`. `build_ndo_model()` (`aci_sim/ndo/model.py`) performs the equivalent mirroring at boot, so a topology.yaml tenant that ships with BDs already configured also starts with the site shadow pre-mirrored (matching the same boot-time treatment PR-12/PR-13 gave ANPs/EPGs/contracts). Idempotent (safe on playbook/driver retries) — `_find_by_name` skips a site that already carries the bdRef.
- **Verified on real ACI hardware end-to-end:** aci-py's `scripts/live_chain.py` driver against `environments/lab-multisite-sim` — MS-TN2 now reaches **13/13 steps rc=0** (was 9 ok / 4 fail before this fix: `create_tenant` pass1, `create_bd`, `create_tenant` pass2, and `migrate_existing_vlan_gateway` all 400'd on exactly this `replace: 'bd-*' not found` error); MS-TN1 stays **11/11 rc=0** (1 skip — no `set_primary_fw_state` var file for TN1's combo — no regression).

### 7e. Site-local BD mirror-vs-site-module dedup (PR-16)

Closes a site-local BD *duplication* left behind by PR-15's mirror, found running the Acme App1-Net tenant's `create_tenant`/`create_bd` playbooks against a real NDO schema on ACI hardware (schema `App1-Net-LAB0`).

- **The symptom.** After `create_tenant` (whose template-BD `add` fires `_mirror_template_bd_to_sites`, PR-15) and `create_bd` (whose `cisco.mso.mso_schema_site_bd`/`custom_mso_schema_site_bd_subnet` tasks PATCH the site-local shadow), `sites[0].bds` on the live schema carried **two** entries for the same template BD `App1-Net_VLAN10`: one empty mirror (`bdRef=".../templates/LAB1/bds/App1-Net_VLAN10"`, `subnets=[]`) and one carrying the real subnet (`10.10.10.1/24`). Real NDO has exactly one site-BD row per template BD.
- **Root cause #1 — the append path skipped dedup entirely.** `apply_json_patch`'s `op: add` handling has three sub-branches keyed on the last path token: `"-"` (append), a numeric index (`insert`), or a name (resolved via `_find_by_name`, which already deduped named-segment adds). Only the named-segment branch deduped. `mso_schema_site_bd.py`'s "BD does not exist yet" branch (confirmed against the pre-existing `test_patch_add_site_bd_by_composite_key`, PR-11) PATCHes `add /sites/{siteId}-{template}/bds/-` — the **append** form — so it always appended unconditionally, regardless of whether the template-add mirror already created a site-local shadow for the same BD.
- **Root cause #2 — `_find_by_name`'s own ref-lookup only recognized the STRING ref form.** Even where dedup *was* attempted (the mirror's own idempotency check, `_find_by_name(site["bds"], bd_name)`), it iterated each item's `bdRef` and matched only `isinstance(ref_val, str)`. Several real cisco.mso write payloads send `bdRef` as a **dict** (`{schemaId, templateName, bdName}`, or a bare `{bdName: ...}` with no schema/template context) — an existing site-local entry left in that shape was invisible to any subsequent name-based lookup.
- **Fix — dedup a NAMELESS shadow strictly by its single IDENTITY ref.** A site-local shadow (`sites[].bds`/`anps`/`epgs`/`contracts` entry) carries no `name`/`displayName` of its own; its identity is exactly one `*Ref` (`bds`→`bdRef`, `anps`→`anpRef`, `epgs`→`epgRef`, `contracts`→`contractRef`, in `_IDENTITY_REF_BY_COLLECTION`). `_bare_ref_name(ref_val)` extracts the bare object name from that identity ref regardless of shape (string trailing segment, or the dict form's own name field via `_REF_NAME_FIELDS`), so a dict-form `bdRef` (a site-module payload's shape, possibly a bare `{bdName: ...}`) and a string-form `bdRef` (the mirror's canonical shape) resolve to the same bare name. `_find_by_name`'s ref-lookup now fires **only for nameless items** and **only over the identity-ref keys** (never a property ref like `vrfRef`). `_find_shadow_by_identity(items, value, collection)` is the append-path counterpart: it dedups **only** a nameless value, **only** by the identity ref for that collection (`tokens[-2]`), and `apply_json_patch`'s `add`+`"-"` branch calls it before appending — a match **replaces** in place (cisco.mso's GET-then-write idiom, real NDO's one-row-per-object invariant), a genuinely new shadow appends. Because the machinery is keyed by collection→identity-ref, it covers site-local ANPs/contracts too with no per-type change.
- **Why identity scoping is load-bearing (regression the first cut of this PR shipped and an independent full-chain run caught).** An earlier cut resolved a value's identity by iterating **all** `_REF_LOOKUP_KEYS` — including `vrfRef`/`l3outRef`. But every template BD under one L3-attached VRF shares the same `vrfRef` (e.g. `vrf-L3_LAB0`), and a template-level BD `add` (a NAMED object, not a shadow) was also run through the same dedup. Result: two distinct template BDs both "matched" via their shared `vrfRef`, so each `add /templates/{T}/bds/-` overwrote the previous one and every template's BDs collapsed to a **single** entry — the real Acme `create_bd` failed with `"Provided BD 'bd-App2-Net_VLAN11_LAB0' does not exist. Existing BDs: bd-App3-Hst_VLAN12_LAB0"` (only the last-added BD survived per template). The invariant the corrected code enforces: **a named object is matched ONLY by name/displayName, never by any `*Ref`; ref-matching is ONLY for nameless shadows and ONLY via the ONE identity ref for that list** — a property ref is a property, not an identity. Guarded by `test_distinct_template_bds_sharing_vrfref_do_not_collapse` and three sibling tests.
- **Verified on real ACI hardware end-to-end:** Acme App1-Net tenant's `create_tenant.yml` + `create_bd.yml` both `failed=0` against the sim; the live NDO schema `App1-Net-LAB0` shows template BD counts `LAB1-LAB2=6`, `LAB1=14`, `LAB2=10` (30 total, no template collapse) and exactly one site-local BD entry per template BD in each site's `bds[]` (no duplicate).

## 8. Topology view (`GET /api/topology`) — exact needs

Per site autoACI issues: `fabricNode`, `fabricLink`, `vpcDom`, `fabricHealthTotal` (paged);
per-node `query_dn("topology/pod-{pod}/node-{id}/sys/health")` → healthInst.cur;
fabric-wide `faultInst` (severity filter) — counts per node parsed from `/node-{id}/` in DN;
session tables `ospfAdjEp`, `bgpPeer`, `bgpPeerEntry`, `bgpPeerAfEntry`;
multi-site ISN: per spine `query_dn("topology/pod-1/node-{spineId}/sys/bgp/inst", subtree=True, subtree_class="bgpPeer")`
→ detects /32 peers with asn≠local → synthesizes an ISN cloud node + spine→ISN links.
Node detail `GET /api/topology/node/{id}`: `ethpmPhysIf`,`l1PhysIf` (via `/sys` subtree), node-scoped `faultInst`,
`fvIfConn` (EPGs on ports), health. `pod` is parsed from `fabricNode.dn` (`pod-{N}`) — do NOT assume pod-1 everywhere.
Link adjacency from `fabricLink.n1/n2` + DN `/lnk-{p1}-to-{p2}/`. Roles from `fabricNode.role`.

## 9. Plugins — 46 files, all must get believable data

Every class in §6 is read by some plugin. Control-plane plugins (tenant/bd/epg/vrf/contract/l3out/access/
capacity) read config MOs. Data-plane plugins (bgp_health, routing_state, fabric_bgp_evpn, adjacency_health,
interface_status/flaps, ep_tracker, fault_summary, health_score) read operational MOs. Many do two-pass
parent→child parsing and use `eq/wcard` filters + subtree. The generic query engine + a consistent MIT
covers all of them without per-plugin code.

## 10. Fabric Build (mostly local; write path is dry-run-first)

Catalog/prereq-gate/common-vars/generate-vars are LOCAL to autoACI (no APIC). Real apply goes through a
hidden `/api/_internal/run-playbook` that compiles templates via aci-py → `POST /api/mo/{dn}.json` (single-fabric)
and/or NDO `schemas`+`deploy` (multi-site), `--check` (dry-run) by default. To test a playbook end-to-end:
run in apply mode → sim upserts the MOs → verify via read-back. ~22 playbooks are pure ACI/NDO REST
(create_*/bind_*/initial_fabric_*/add_leaf/add_spine/provision_*); the N5K/N7K SSH migration family is OUT OF SCOPE.

## 11. Ansible-compatibility (PR-9 — cisco.aci fidelity)

The owner's primary public use case is running **Ansible `cisco.aci` playbooks**
against this simulator directly (not just autoACI). Everything below was
verified read-only against upstream `plugins/module_utils/aci.py`
(https://github.com/CiscoDevNet/ansible-aci) — the shared HTTPAPI/module
base every `cisco.aci.aci_*` module calls into.

**GET-before-write existence check (FEATURE 6, live blocker).** Every
`state=present`/`state=absent` module calls `get_existing()` (a GET) before
building its diff or delete. `api_call()`'s response handling treats ANY
`status != 200` as fatal via `fail_json` — GET has no "404 = not found,
that's fine" carve-out. So `GET /api/mo/{dn}.json` on a nonexistent-but-
well-formed DN **must** be `200 {"imdata":[],"totalCount":"0"}`, never 404
(§5 above) — otherwise the first task of any `state=present` playbook
against a not-yet-created object fails immediately.

**`DELETE /api/mo/{dn}.json` (`state=absent`).** `delete_config()`:
```python
if not self.existing and not self.suppress_previous:
    return
elif not self.module.check_mode:
    self.api_call("DELETE", self.url, None, return_response=False)
```
The module itself is idempotent at the CLIENT level — it only ever issues
DELETE after its own GET confirmed the object exists, so it never actually
sends a DELETE for an already-absent DN. This sim implements the route as
server-side idempotent too (200 + empty envelope whether the DN existed or
not) rather than 404-on-missing: (a) it matches this codebase's existing
MIT delete semantics (`MITStore.upsert`'s `status="deleted"` branch is
unconditional, no existence check) and (b) it's the safer default for any
OTHER client that DOES call DELETE without a prior GET. An existing DN's
entire subtree is removed (`MITStore.delete(dn)`, public wrapper added in
PR-9 around the pre-existing `_remove_subtree` helper `upsert` already used).

**`status` attribute.** cisco.aci's `payload()` never sets a `status` key —
delete goes through the dedicated DELETE verb above, not a POST body with
`status:"deleted"`. That POST-body form is a Fabric Build / aci-py
convention this sim already supported (§3) and continues to: `status:
"deleted"` on any planned `(class, attrs)` node removes that DN's entire
subtree (verified: `MITStore.upsert` already special-cased this pre-PR-9;
PR-9 added regression coverage). Any other `status` value (including the
literal string `"created,modified"`, which real APIC/aci-py tooling can
emit) is ignored and the write proceeds as a normal upsert — no special
handling, no error.

**`annotation`/`ownerKey`/`ownerTag`/`nameAlias` passthrough.** cisco.aci's
`aci_annotation_spec()` defaults `annotation` to the literal string
`"orchestrator:ansible"` and `payload()` injects it (plus `ownerKey`/
`ownerTag` when set) into `proposed["attributes"]` for every managed
object, unconditionally. `name_alias` is a separate per-module spec, also
folded into the same attributes dict when set. None of these are special
sim attributes — they round-trip because `MO.attrs` is a generic
`dict[str,str]` and the write path never allowlists known attribute names;
this section just documents and regression-tests that this passthrough
survives POST → `mo` GET → class query unchanged.

**Certificate signature auth (accept-mode).** cisco.aci supports
password-less auth via `private_key`; `cert_auth()` sets a single `Cookie`
header per request (never touches `APIC-cookie` / never calls aaaLogin):
```python
sig_dn = "uni/userext/user-{username}/usercert-{certificate_name}"
self.headers["Cookie"] = (
    "APIC-Certificate-Algorithm=v1.0; "
    f"APIC-Certificate-DN={sig_dn}; "
    "APIC-Certificate-Fingerprint=fingerprint; "
    f"APIC-Request-Signature={base64(RSA-SHA256(method+path+body))}"
)
```
This sim implements **accept-mode**: `require_session()` treats a request
carrying all four `APIC-Certificate-*`/`APIC-Request-Signature` cookies as
authenticated, for the user named in `APIC-Certificate-DN`, PROVIDED that
user matches `SIM_USERNAME` — WITHOUT verifying the RSA-SHA256 signature
bytes at all. A missing cookie, an unparsable `APIC-Certificate-DN`, or a
DN naming a different user all fall through to the normal cookie-session
check (which also fails for a request that never called aaaLogin),
yielding the standard 403 envelope. This is a deliberate simulator trust
model — see docs/DESIGN.md's "Certificate accept-mode trust model" note.
`SIM_CERT_STRICT` env var is a documented placeholder for a future
signature-verifying mode; it currently has no effect.

**firmwareCtrlrRunning.** Seeded per controller (`build/fabric.py`,
alongside the existing controller `topSystem`) at
`{ctrl_dn}/ctrlrfwstatuscont/ctrlrrunning`, `version` = `site.apic_version`
— the same value `topSystem.version` already carried, so the two can never
drift apart. Small, trivially-derived addition; not previously built.
