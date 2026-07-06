# aci-sim — DESIGN

A software simulator that faithfully impersonates the REST APIs of a **2-site Cisco ACI
fabric** (two per-site APICs + one ND/NDO), driven by an editable `topology.yaml`, so that
pointing local **autoACI** at it makes Topology, NDO views, all query plugins, and Fabric
Build render internally-consistent, believable control- + data-plane data.

The contract we emulate is in `docs/CONTRACT.md` (authoritative — trust it, don't re-explore autoACI).

## Locked decisions (owner-approved)

1. **Generic MIT engine** — model each APIC as a real Managed Information Tree (DN-keyed
   objects, parent/child) with a query resolver implementing APIC's class/mo/node-scoped +
   `rsp-subtree`/`target-subtree-class` + `eq/wcard/and/or` + pagination semantics. One YAML →
   full object graph; every current AND future autoACI query works with no per-class code.
2. **Accept-reflect writes + reset/snapshot** — `POST /api/mo` and NDO schema/deploy upsert
   into the live MIT so read-back + Topology show the change (enables push→verify playbook
   testing). In-memory, with `reset` (to baseline) and `snapshot`/restore.
3. **Static deterministic snapshot** — stable, internally-consistent, reproducible data (no
   liveness engine in v1). Deterministic so views are easy to verify.
4. **Configurable topology** — YAML parametrizes spine/leaf counts; leaves can be added/removed
   (statically via YAML+reload, dynamically via the `add_leaf_switch_pair` playbook's
   `fabricNodeIdentP` write reaction). Border-leaf vPC → CSW, 2 sites + ISN, full L3Out subtree.
5. **Scope** — ACI + NDO REST surface (~22 REST playbooks fully testable + the ACI half of
   migration). **NX-OS SSH is OUT of v1** (extension seam left, no auth stub).
6. **Stack** — Python 3.11+, FastAPI + uvicorn + Pydantic v2, PyYAML, httpx (tests). Self-signed
   TLS on each host (autoACI uses `verify=False`). Matches autoACI's stack.

## Process / host layout

One process, three HTTPS apps on three localhost ports (self-signed certs in `certs/`):
`APIC Site-A :8443`, `APIC Site-B :8444`, `NDO :8445`. Each site APIC serves its own MIT;
both sites are built from one global topology so stretched tenants + ISN peering are consistent.
The NDO app derives its schema model from the same topology (same tenant/VRF/BD names).
Point autoACI: Site-A→`127.0.0.1:8443`, Site-B→`127.0.0.1:8444`, NDO→`127.0.0.1:8445`.

## Module map + interfaces (the contracts subagents implement against)

### `mit/` — object store (foundation; no deps)
- `mo.py`: `MO` = `{class_name:str, attrs:dict[str,str], children:list[MO]}`; `attrs["dn"]` is the key.
  Helpers: `MO(cls, **attrs)`, `.dn`, `.add_child(mo)`, `.flat()` (self+descendants).
- `dn.py`: bracket-aware DN utils — `dn_split(dn)->list[str]` (never split inside `[]`),
  `parent_dn(dn)`, `rn_of(dn)`, `dn_class_hint`, `pod_from_dn`, `name_from_dn`.
- `store.py`: `MITStore` — `add(mo)`, `upsert(mo)` (merge attrs + recurse children; honor
  `status=deleted`), `get(dn)->MO|None`, `children(dn, classes=None)`, `subtree(dn, classes=None)`,
  `by_class(cls)->list[MO]`, `all()`; maintains a `dn→MO` map and a `parent→[child dn]` index.
  Deterministic iteration order (insertion order).

### `query/` — ACI query engine (deps: mit)
- `filters.py`: parse `eq/wcard/and/or(...)` strings → predicate `fn(MO)->bool`. Bracket/quote aware.
- `engine.py`: `run_class_query(store, cls, params)`, `run_mo_query(store, dn, params)`,
  `run_node_scoped(store, node_dn, cls, params)`. Params object carries filter, subtree mode
  (`rsp-subtree` children|full or legacy `query-target=subtree`), subtree classes, page, page_size.
  Returns `(imdata:list[dict], total:int)` in the exact `{cls:{attributes,children}}` shape.
  **Two subtree shapes (must match real APIC + autoACI):** (a) legacy `query-target=subtree` (autoACI's
  `query_dn(subtree=True)`) → descendants FLAT as top-level imdata, filtered by `target-subtree-class` (root
  included only if it matches); consumers read the target class at the top level, never recurse into `children`.
  (b) modern `rsp-subtree=children|full` (autoACI's `query_class(subtree=…)`) → matched objects with NESTED
  `children` (children=one level, full=recursive). Pagination slices the result set; `total` = pre-pagination count.

### `topology/` — the YAML contract (deps: none)
- `schema.py`: Pydantic v2 models for the whole topology (see `topology.yaml` + §Topology schema below).
  Validates counts, references (bd→vrf, epg→bd, l3out→vrf), site/ISN config. Fail loudly on bad refs.
  Also validates (PR-8, findings #27/#28):
  - `fabric.loopback_pool`/`fabric.oob_pool` fall within the fixed `10.0.0.0/8` /
    `192.168.0.0/16` ranges that `build/fabric.py`'s `loopback_ip()`/`oob_ip()`
    actually generate (those functions are not parameterized by an arbitrary CIDR
    — 6+ call sites across `build/*.py` call them with just `(pod, node_id)` — so
    the pool fields describe, and are checked against, the builder's real output
    rather than being purely cosmetic).
  - CSW peering params duplicated between `site.border_leaves[].csw` (authoritative
    — carries `local_ip`/`vlan` too, used by `l3out.py` for the physical
    sub-interface/`l3extMember`) and `tenant.l3outs[].csw_peer` (used for the
    L3Out's `bgpPeerP`/static-route nexthop) must agree on `name`/`asn`/`peer_ip`
    for any border-leaf the l3out references, or loading fails with a clear error.
- `loader.py`: `load_topology(path)->Topology`.

### `build/` — generators (deps: topology, mit)
Each sub-builder is `build(topo, site, store)` and only ADDS MOs. `orchestrator.build_site(topo, site)
-> MITStore` runs them in dependency order and returns a populated store; `build_all(topo)->{site:store}`.
Sub-builders (one concern each, <~200 lines):
- `fabric.py` — fabricNode (roles/ids/model/serial/version from YAML), topSystem, fabricHealthTotal, vpcDom/vpcIf (border-leaf pair), infraWiNode.
- `cabling.py` — fabricLink (with `/lnk-…/` DN), lldpAdjEp + cdpAdjEp derived STRICTLY from the cabling graph, isisAdjEp.
- `underlay.py` — bgpInst (local ASN), ospfAdjEp/**ospfIf**✅ (spine↔ISN, on the routed VLAN-4 sub-if `eth1/{49+si}.{49+si}` (port-named) with `addr` — PR-22; multi-site only), IS-IS dom.
- `overlay.py` — intra-site BGP-EVPN (spines as RR, leaves as clients): bgpPeer/bgpPeerEntry(established)/bgpPeerAfEntry;
  **ISN**: on each spine's `sys/bgp/inst`, add inter-site bgpPeer with peer addr /32 + remote (other-site) ASN.
- `endpoints.py` — fvCEp/fvIp distributed across leaves+EPGs (encap/fabricPathDn consistent), epmMacEp/epmIpEp, fvIfConn.
  Runs BEFORE `tenants.py` (PR-3b-batch1 reorder) so `tenants.py` can read back real endpoint paths for `fvRsPathAtt`.
- `tenants.py` — fvTenant/fvCtx/fvBD/fvSubnet/fvAp/fvAEPg + fvRs* rels + vzBrCP/vzSubj/vzFilter/vzEntry from YAML,
  plus **fvRsPathAtt**✅ (static binding, reusing the EPG's own endpoint path) and **vzRsSubjGraphAtt**✅.
- `access.py` — infra AAEP/IPG/port-profiles/selectors/blocks + physDomP/l3extDomP + fvnsVlanInstP/fvnsEncapBlk + rels,
  plus the **leaf-interface-policy cluster**✅ (infraAccPortP/infraHPortS/infraPortBlk/infraRsAccBaseGrp/infraAccBndlGrp)
  and an `infraInfra` root MO at `uni/infra`.
- `l3out.py` — full l3extOut subtree (LNodeP/LIfP/InstP/subnets/RsEctx/RsL3DomAtt/RsPathAtt) + bgpPeerP/bgpAsP to CSW,
  plus the operational bgpPeer/bgpPeerEntry(established) representing the CSW eBGP session; actrlRule for contracts;
  plus the **route-control cluster**✅ (rtctrlProfile/rtctrlCtxP/rtctrlSubjP/rtctrlMatchRtDest/rtctrlSetComm/rtctrlSetPref),
  **static routes**✅ (ipRouteP/ipNexthopP), **l3extMember**✅, and **ospfExtP**✅.
- `routing.py` (new, PR-3b-batch1) — per-(tenant,vrf) **uribv4Route/uribv4Nexthop**✅ RIB entries on every leaf/border-leaf
  (BD subnets as connected routes + a 0.0.0.0/0 default via the L3Out CSW peer on border leaves), and **arpAdjEp**✅ —
  one per locally-learned endpoint, reusing that endpoint's real MAC/IP. Runs after `endpoints`.
- `interfaces.py` — l1PhysIf + ethpmPhysIf (per node, ports incl. fabric uplinks + host ports), ethpmFcot, eqptcapacityPolUsage5min.
- `health_faults.py` — healthInst per node/tenant (`…/sys/health`, `/tn-X/health`), a seeded believable faultInst set
  (node-local DNs so node-scoped queries work), configExportP/configJob, coopPol/coopInst.
- `userprofile.py` — static `uni/userprofile-admin` (aaaUserEp) + `aaaUserDomain` child (name="all"), so the
  CONTRACT §1 login probe (`query-target=subtree&target-subtree-class=aaaUserDomain`) resolves instead of 404ing.

### `rest_aci/` — APIC FastAPI app (deps: query, mit, build)
- `auth.py` — aaaLogin/aaaRefresh/aaaLogout, token+cookie, the topSystem/userprofile login probes.
- `app.py` — `make_apic_app(site_state)`: routes `/api/aaaLogin.json`, `/api/aaaRefresh.json`,
  `/api/aaaLogout.json`, `/api/class/{cls}.json`, `/api/mo/{dn:path}.json` (GET+POST),
  `/api/node/class/{dn:path}/{cls}.json` (node-scoped) and `/api/node/class/{cls}.json`
  (fabric-wide — equivalent to `/api/class/{cls}.json`; PR-6 FIX 3). Parses query params →
  query engine → envelope. Error mapping (§5 of CONTRACT).
- `writes.py` — POST body → upsert into MIT (recurse children, honor status); `fabricNodeIdentP`
  reaction materializes a full node-registration set (fabricNode + topSystem + healthInst +
  fabricLink/lldpAdjEp/cdpAdjEp cabling to every spine + l1PhysIf inventory), fires for a
  `fabricNodeIdentP` anywhere in the planned write (not just top-level), and derives pod from the
  target site instead of hardcoding it (PR-6 FIX 2; shared with `/_sim/add-leaf`). Returns
  created/modified imdata.

### `ndo/` — NDO (deps: topology, mit optional)
- `model.py` — `build_ndo_model(topo)`: sites, tenants, schemas/templates (vrfs/bds/anps/epgs/contracts/filters/externalEpgs
  with `/vrfs/…` `/bds/…` refs), template summaries + tenantPolicy DHCP, fabric-connectivity, policy-states, audit records.
  Names MUST match the APIC MIT (cross-plane consistency).
- `app.py` — `make_ndo_app(ndo_state)`: login/version + all §7 GETs + schema/deploy POST (store + return id/status).

### `runtime/` — supervisor (deps: all)
- `config.py` — ports, cert paths, topology path, host→site map (env-overridable).
- `supervisor.py` — load topology → `build_all` + `build_ndo_model` → start 3 uvicorn servers (asyncio) with TLS.
  Keeps a `baseline` deep-copy of each store for `reset`.

### `control/` — admin API (mounted on each app under `/_sim/…`, deps: runtime state)
- `reset` (restore baseline), `snapshot`/`restore` (named), `reload` (re-read YAML + rebuild against
  the FRESH matching `Site` object from the just-loaded topology — matched by `Site.id`, falling back
  to `Site.name` — not the stale pre-reload one, so edits to a site's nodes/leaves actually take
  effect; returns a 400 APIC error envelope if the site no longer exists in the reloaded topology
  instead of crashing or silently keeping stale state; PR-6 FIX 1), `add-leaf`/`remove-leaf` (runtime
  mutate + rebuild affected derived data — `add-leaf` shares `writes.py`'s enriched node-registration
  reaction, not a bare `fabricNode`). Not part of the APIC/NDO contract — a side control plane for
  testing.

## Internal-consistency rules (the "尽可能真实" core — enforce in build/)

- **Nothing free-floating.** Every operational MO derives from a topology fact.
- Cabling graph is the single source for `fabricLink`, `lldpAdjEp`, `cdpAdjEp` (neighbors ↔ actual links).
- IS-IS/OSPF underlay adjacencies only between physically-cabled spine↔leaf pairs.
- BGP-EVPN overlay: spines = route-reflectors, leaves = clients (sessions to spine loopbacks), operSt=established.
- ISN: for each spine, an inter-site eBGP-EVPN peer to the remote site's spines — addr /32, remote ASN =
  the OTHER site's fabric ASN (this is what makes autoACI draw the ISN cloud). EVPN AF up.
- L3Out: border leaves peer eBGP to CSW (asn from YAML); the l3extOut config subtree AND the operational
  bgpPeer(Entry) to the CSW must both exist and agree (addr/asn).
- Endpoints: each fvCEp lives on a real leaf port, in a real EPG/BD, with a plausible IP in the BD subnet;
  epmMacEp/epmIpEp mirror them on the owning node. Endpoint host ports never overlap the APIC-controller
  port range on the same leaf (see PR-5 FIX 3 below). A stretched-tenant endpoint is local (front-panel port)
  on exactly ONE site and tunnel/vxlan-learned on the other — never front-panel-local on both (PR-5 FIX 4).
- Health/faults: healthInst per node/tenant/controller; a small seeded faultInst set with node-local DNs so
  counts render and node-scoped fault queries return.
- Two sites share stretched tenants (per YAML `stretch:true`) → same tenant/VRF/BD names present in both MITs
  + NDO schema references them; site-local tenants present only in their site.

## PR-5 topology-consistency fixes (findings #13-#18)

Six internal-consistency findings, all fixed in `build/`. Every port an operational MO references
(`l3extRsPathL3OutAtt.tDn`, `ospfAdjEp` interface, `fvCEp.fabricPathDn`) must resolve against a real
`l1PhysIf` built by `interfaces.py` for that exact node — this is now a checked invariant
(`tests/test_pr5_topology_consistency.py`).

**Full port inventory per node role** (see `interfaces.py` module docstring for the authoritative version):
- Spines: `eth1/1..4` fabric uplinks (one per downlink leaf/BL) + `eth1/49[,50,...]` dedicated ISN/IPN
  uplink ports, **multi-site topologies only**, one per spine (`eth1/{49+spine_index}` — matches the port
  `underlay.py`'s `ospfAdjEp` already referenced; previously these ports didn't exist at all → FIX 2).
- Leaves + border-leaves: `eth1/1..8` host-facing access ports, `eth1/49[,50,...]` fabric uplinks (one per
  spine). Border-leaves ONLY also get a dedicated `eth1/48` routed L3Out sub-interface port (previously
  `l3out.py` referenced this port but nothing ever built it → FIX 1).

**FIX 1 (#13) — L3Out port.** Chose to extend the border-leaf port inventory with a dedicated `eth1/48`
routed port (`mode="routed"`, `usage="l3out"`) rather than reassign the L3Out onto an existing host port
(e.g. `eth1/8`), because real ACI border-leaf L3Out sub-interfaces conventionally live on a dedicated
front-panel port, not one shared with regular EPG/host traffic. `l3out.py` was unchanged — it already
referenced `eth1/48`; the fix makes that port real.

**FIX 2 (#14) — spine OSPF/ISN port.** Chose to extend the spine port inventory with a dedicated
`eth1/{49+i}` ISN uplink (one per spine, multi-site only) rather than reuse an existing fabric-uplink port,
because every spine port in `eth1/1-4` is already consumed by the intra-fabric spine↔leaf mesh (full mesh
= 2 leaves + 2 border-leaves = 4 downlinks per spine in the default topology) — there is no free fabric port
to reuse. This also matches real ACI: multi-site spines use a dedicated ISN-facing port, distinct from the
leaf-facing fabric ports. `underlay.py` was unchanged — its `ospfAdjEp` already used `eth1/{49+si}`; the fix
makes that port real via the same index arithmetic.

**FIX 3 (#15) — endpoint/APIC port separation.** `fabric.py` wires each `APIC-{cid}` (cid 1..
`site.controllers`, default 3) to `eth1/{cid}` on the first two leaves (`site.leaf_nodes()[:2]`) — the same
leaves `endpoints.py` places host endpoints on. `endpoints.py` now reserves `eth1/1..{controllers}` on those
leaves and starts endpoint port allocation at `eth1/{controllers+1}`, wrapping within the remaining
`_HOST_PORTS - controllers` budget. Non-APIC-attached leaves are unaffected (still start at `eth1/1`).

**FIX 4 (#16) — multi-site endpoint semantics.** See below.

**FIX 5 (#17) — BD unicastRoute.** `unicastRoute` now reflects whether the BD actually has `fvSubnet`
children (routed) rather than `l2stretch` — a stretched BD can still carry routed subnets, and this
topology's stretched BDs (`bd-BD3_LAB0`, `bd-BD4_LAB0`) do. This agrees with `ndo/model.py`, which
unconditionally reports `unicastRouting: True` for every BD (all BDs in `topology.yaml` have subnets).

**FIX 6 (#18) — controller health.** `health_faults.py` now emits a `healthInst` at
`topology/pod-{pod}/node-{cid}/sys/health` for every controller node id (1..`site.controllers`), matching
the `topSystem` `fabric.py` already builds there. Real APIC exposes controller health the same way as
switch health.

### FIX 4 — multi-site endpoint representation (design)

Real ACI never shows the identical endpoint as locally front-panel-attached at both sites of a stretched
tenant simultaneously. One site has it physically attached; the other only knows about it via the
multi-site overlay (ISN) tunnel. `endpoints.py` now models this:

- **HOME site** (physically attached): `fvCEp.lcC="learned"`, `fabricPathDn` = the real front-panel leaf
  path (`topology/pod-{pod}/paths-{leaf_id}/pathep-[eth1/{port}]`), epm mirrors use `flags="local"`, and a
  `fvIfConn` is emitted (real front-panel port).
- **REMOTE/peer site** (learned via the ISN): same MAC/IP/DN, but `fvCEp.lcC="learned,vxlan"` and
  `fabricPathDn` points at a `tunnelIf` on one of the local spines
  (`topology/pod-{pod}/paths-{spine_id}/pathep-[tunnel{remote_spine_id}]`), epm mirrors use
  `flags="learned,vxlan"`, and no `fvIfConn` is emitted (there is no front-panel port to attach one to).
- A minimal `tunnelIf` MO is created per (local spine, remote spine) pair used
  (`topology/pod-{pod}/node-{spine_id}/sys/tunnel-[tunnel{remote_spine_id}]`), `dest` = the remote spine's
  loopback (representing its spine-proxy anycast TEP), `src` = the local spine's own loopback. `overlay.py`
  does not yet build tunnel/TEP objects, so this is a new minimal object, not a reuse.
- **HOME-site assignment is deterministic and stateless across the two independent per-site `build()`
  calls**: for the i-th endpoint in a stretched tenant's sequence, home site =
  `tenant.sites[i % len(tenant.sites)]`. Since both sites iterate the exact same tenant/AP/EPG/per-EPG
  sequence in the same order, they agree on which site is home for a given MAC without sharing any state —
  each site's independent build() computes the same `i` for the same endpoint and reaches the same
  conclusion.
- Non-stretched (site-local) tenants are unaffected — every endpoint is simply local, exactly as before.

## Topology schema (topology.yaml) — shape

```
fabric: { name, evpn_rr: spine }
sites:
  - name, id, apic_host, asn, pod
    spines: N        # count OR explicit list [{id,name,model,serial}]
    leaves: M        # count OR explicit list
    border_leaves: [{id, name, vpc_domain, csw:{name,asn,peer_ip,local_ip,vlan}}]
    cabling: auto|explicit   # auto = full spine×leaf mesh + border-leaf uplinks
isn: { enabled: true, transit_asn?, peer_mesh: full }   # drives inter-site EVPN
tenants:
  - name, stretch: bool, sites: [..]
    vrfs: [{name}]
    bds:  [{name, vrf, subnets:[cidr], l2stretch?}]
    aps:  [{name, epgs:[{name, bd, contracts:{provide:[],consume:[]}}]}]
    contracts: [{name, scope, filters:[{name, entries:[{proto,from,to,ether}]}]}]
    l3outs: [{name, vrf, border_leaves:[..], csw_peer:{...}, ext_epgs:[{name, subnets:[cidr]}]}]
endpoints: { per_epg: K, ip_pool_from_subnet: true }   # generation knobs
faults:    { seed: [{severity,code,node,descr}] , health_defaults:{node:95,tenant:98} }
access:    { vlan_pools:[{name,from,to}], phys_domains:[..], aaeps:[..] }   # sensible defaults if omitted
```

Default `topology.yaml` ships the spec baseline: 2 sites × (2 spine + 2 leaf + 1 border-leaf vPC pair to CSW),
ISN between them, a couple stretched + site-local tenants, one L3Out per site to its CSW, seeded endpoints/faults.

## Phase plan (each phase: implementer subagent(s) → independent reviewer per 第十律)

- **P1 foundation** — `mit/` (mo, dn, store) + `query/` (filters, engine). Unit-tested in isolation.
- **P2 topology** — `topology/schema.py` + `loader.py` + default `topology.yaml`.
- **P3 fabric builders** — fabric, cabling, underlay, overlay(+ISN), health_faults, interfaces → Topology view renders.
- **P4 tenant builders** — tenants, access, l3out, endpoints → config + data-plane plugins render.
- **P5 ACI REST app** — auth + GET routes + POST writes + reactions + runtime supervisor + TLS + control API.
- **P6 NDO** — model + app.
- **P7 verify** — verification harness that mimics autoACI's queries for every view/plugin + write→readback +
  add_leaf; README + run scripts.

## Verification (第二律 evidence-first)

`tests/verify_autoaci.py` replays the exact query patterns from `docs/CONTRACT.md` §8–9 against the running
sim and asserts non-empty, shape-correct, internally-consistent results for: Topology (2 sites + ISN cloud +
vpc pairs + faults/health), every plugin's class set, L3Out BGP-to-CSW, NDO sites/tenants/schemas, and a
write→read-back cycle (POST a tenant → GET returns it) + an add_leaf reaction. Real command output is the
acceptance evidence.

## PR-3b-batch1 — 21 previously-catalogued-but-unbuilt classes seeded

CONTRACT.md §6 catalogued 21 classes across 6 concern groups that had no builder ever emitting them, so
their class queries returned `totalCount: 0` and the depending autoACI plugins (access_policy.py,
epg_detail.py, contract_detail/contract_map/contract_debug.py, l3out_detail.py, l3out_status.py,
route_control.py, routing_state.py, route_table.py, fabric_ospf.py) rendered nothing. All 21 are now seeded,
derived from the existing topology/tenant objects rather than invented data.

**Access-policy cluster (`build/access.py`)** — one leaf-interface-policy tree per border-leaf vPC domain
(`infraAccBndlGrp` lagT="node", consistent with the vpcDom/vpcIf pair `fabric.py` already builds for the same
border leaves) plus one for the regular-leaf pair (lagT="link", a PC bundle). Each `infraAccPortP` gets one
`infraHPortS` (type="range") selector spanning the real host-port inventory (`eth1/1..eth1/_HOST_PORTS`) via
one `infraPortBlk` + one `infraRsAccBaseGrp` pointing at the bundle group. Also seeds an `infraInfra` MO at
`uni/infra` itself — previously a structural-only DN with no MO, which 404'd `GET /api/mo/uni/infra.json`
even under `rsp-subtree=full` (fixed as part of this batch since the TESTS requirement needed it; the generic
subtree engine already worked correctly once a root MO existed).

**Route-control cluster (`build/l3out.py`)** — one `rtctrlProfile` (export) per L3Out, with a nested
`rtctrlCtxP` (+ `rtctrlSetComm`/`rtctrlSetPref` children using real ACI's fixed short-form RNs `scomm`/`spref`).
One tenant-level `rtctrlSubjP` match rule per L3Out (not nested under the L3Out — autoACI's route_control.py
queries `rtctrlSubjP` with `wcard(dn,"tn-{tenant}")`, tenant-scoped, verified read-only against the plugin
source) with an `rtctrlMatchRtDest` child reusing a real tenant BD subnet. A static default route (`ipRouteP`
ip="0.0.0.0/0") nested under each L3Out's `l3extRsNodeL3OutAtt`, with an `ipNexthopP` child pointing at the
same CSW peer IP the eBGP session builder (`_upsert_ebgp_sessions`) already peers with. `l3extMember`
(side="A") on the existing `l3extRsPathL3OutAtt` — this topology's L3Out path is a single port per
border-leaf (not vPC-bundled), so only one member is emitted, per the batch-1 placement note. `ospfExtP`
added to the SAME L3Out that already carries `bgpExtP` (real ACI supports OSPF+BGP coexistence on one
L3Out) rather than fabricating a second OSPF-only L3Out in topology.yaml — the simpler faithful choice since
this topology has no existing OSPF-only L3Out to attach to.

**EPG/contract classes (`build/tenants.py`)** — `fvRsPathAtt` (static binding) per EPG, pointing at the SAME
front-panel path that EPG's own endpoints already use. This required reordering the orchestrator's builder
list (`endpoints` now runs BEFORE `tenants`, moved up from after `l3out`) so `tenants.py` can read back the
real `fvCEp.fabricPathDn`/`encap` endpoints.py already wrote for that EPG, instead of independently
re-deriving the port-allocation algorithm (which risked silently drifting out of sync). Every other builder
remains pure add-only; `endpoints.py` itself never reads the store, so the reorder is safe. A stretched EPG
whose only endpoints at this site are REMOTE (tunnel-learned) gets no `fvRsPathAtt` — a static binding must
reference a real local port. `vzRsSubjGraphAtt` attached to exactly one contract's subject per tenant (the
tenant's first contract), with a plausible `tnVnsAbsGraphName`; `vnsAbsGraph` itself stays out of scope.

**Operational/routing classes (new `build/routing.py` + `build/underlay.py`)** — `uribv4Route`/`uribv4Nexthop`
seeded per (tenant, vrf) on every leaf/border-leaf: BD subnets as directly-connected routes (nexthop = the
node's own loopback) plus a 0.0.0.0/0 default via the L3Out's CSW peer on border leaves, matching the
`ipRouteP` static route already seeded on the same node. VRF "dom" name reuses the exact `{tenant}:{vrf}`
convention `l3out.py`'s BGP dom already established. `uribv4Nexthop`'s RN uses the 4-bracket
`nh-[proto]-[addr]-[ifname]-[vrf]` shape autoACI's `_RE_NH_PARENT` regex (`route_table.py`/`routing_state.py`,
identical in both files) requires — verified read-only against the plugin source, not re-derived from
guesswork. `arpAdjEp` — one per locally-learned (front-panel) endpoint, reusing that endpoint's real MAC/IP
from the store `endpoints.py` already populated (routing.py runs after `endpoints` in the orchestrator
order); REMOTE/tunnel-learned endpoints get no ARP entry (real ACI resolves those via the overlay, not local
ARP). `ospfIf` nested between the existing `ospfDom` and `ospfAdjEp` in `underlay.py`, on the same
`eth1/{49+i}` ISN uplink port `interfaces.py` already builds a dedicated `l1PhysIf` for — so the pre-existing
`ospfAdjEp` now sits on a real, matching `ospfIf`.

**Writes** — `rest_aci/writes.py` `_RN_TEMPLATES` gained entries for all POST-able batch-1 classes so a
child posted without an explicit `dn` lands on the same canonical DN a GET or builder-created MO would use.

## PR-3b-batch2/3 — remaining previously-catalogued-but-unbuilt classes seeded

Batches 2+3 of the seeding effort closed the rest of CONTRACT.md §6's gap: 9 more classes across
`build/fabric.py`, `build/interfaces.py`, `build/tenants.py`, a new `build/zoning.py`, and `build/overlay.py`,
plus a deliberate decision NOT to build `infraNodeIdentP`. DN/attribute shapes were verified read-only
against the specific autoACI consumer plugins (a targeted grep of autoACI's `backend/plugins/*.py`)
before writing any builder code, per the same discipline batch-1 established.

**Port-channel/vPC (`build/fabric.py`)** — `pcAggrIf` + `pcRsMbrIfs`, one pair per vPC leg (border-leaf),
consistent with the `vpcDom`/`vpcIf` this module already builds for the same pairs. `vpc_status.py` matches
`pcAggrIf` to `vpcIf` by `(node, name)` — so `pcAggrIf.name` intentionally reuses the exact same `"po{bl.id}"`
string `vpcIf.name` already carries (not `access.py`'s differently-named `infraAccBndlGrp`, a different IPG
identity autoACI's plugin does not join on). **Naming correction**: CONTRACT.md catalogued this relation
class as `vpcRsMbrIfs`; a read-only grep of `vpc_status.py` showed it actually queries `pcRsMbrIfs` (the real
ACI class, child of `pcAggrIf`, RN `rsmbrIfs-[{ifDn}]`) — built as `pcRsMbrIfs`, with a CONTRACT.md §6
changelog note recording the correction. Member ports are two real host-facing `l1PhysIf` per leg
(`eth1/1`, `eth1/2` — always present on every leaf/border-leaf) so `pcRsMbrIfs.tDn` always resolves.

**Optics (`build/interfaces.py`)** — `ethpmFcot`, sibling of `ethpmPhysIf` under the same port DN (own
`fcot` RN). Emitted only for fabric-uplink, ISN-uplink, and L3Out routed ports — real optics-bearing
front-panel ports — not plain host-access ports, mirroring real labs where copper/DAC host ports often
report no transceiver inventory while fabric/WAN-facing optical ports do (a deliberate realism choice,
documented in the module docstring, not an omission).

**Cluster health (`build/fabric.py`)** — `infraWiNode`, the APIC appliance-vector: every controller carries
its own view of every cluster member (an N×N matrix for N=`site.controllers`), all seeded `health="fully-fit"`
to match this sim's other all-healthy defaults (`fabricHealthTotal.cur=100`).

**Zoning-rule / pcTag plumbing (`build/tenants.py` + new `build/zoning.py`)** — real ACI zoning rules key off
per-EPG/VRF `pcTag` ("sclass") + `scopeId`, neither of which existed anywhere in this sim before batch-2
(verified: zero prior `pcTag` references). `tenants.py` gained `pctag_for`/`scope_id_for` — small
CRC32-derived helpers (same technique `endpoints.py`'s `_vnid` already uses) offset clear of autoACI's 5
hardcoded system pcTags (1/13/14/15/16384) — assigning a stable `pcTag` to every `fvAEPg` and `fvCtx`
(+ `scope` on `fvCtx`), and seeding one `fvEpP` (`uni/epp/fv-[{epg_dn}]`) per EPG carrying the matching
`pcTag`, the EPG's VRF `scopeId`, and `epgPKey`=the EPG's own dn (`zoning_rules.py` extracts the friendly
EPG name back out of `epgPKey`'s trailing `epg-` segment). New `build/zoning.py` derives `actrlRule` — one
permit rule per (contract subject filter, provider EPG, consumer EPG) triple, emitted on every leaf where
either side has a locally-learned endpoint (read back from `endpoints.py`'s `fvCEp`, same technique
`tenants.py`'s `fvRsPathAtt` already established), with `sPcTag`/`dPcTag`/`scopeId` reusing the exact
`pctag_for`/`scope_id_for` values the owning EPGs/VRF already carry. Also seeds real MOs at the
`.../sys/actrl` and `.../sys/actrl/scope-[...]` structural containers — the same class of gap batch-1's
`infraInfra` fix addressed for `uni/infra`: without a real MO at the exact DN a subtree query targets,
`GET /api/mo/{dn}.json` 404s even though the generic subtree engine itself works fine once a root MO exists.
`zoning` runs right after `tenants` in the orchestrator (both already after `endpoints`).

**EVPN routes (`build/overlay.py`)** — `bgpVpnRoute` + `bgpPath`, the per-spine EVPN VPN route family
`fabric_bgp_evpn.py`'s "vpn_routes" view two-pass parses (pass 1 collects routes by dn; pass 2 recovers
the parent route dn from a path dn via `path_dn.rsplit("/path-", 1)[0]` — so `bgpPath`'s RN must start with
`path-` directly under the route's own dn, verified read-only against the plugin). Seeded per spine
(the EVPN route-reflector role) for each BD subnet deployed at the site, with one `bgpPath` per leaf that
actually hosts a locally-learned endpoint for that BD (read back from `fvRsBd`+`fvCEp`). This needs the
same post-`tenants`/`endpoints` data `zoning.py` needs, but `overlay.build()` itself must stay in its
original EARLY orchestrator slot (BGP peer setup only needs fabric/topology data) — so the new logic lives
in a second function, `overlay.build_vpn_routes()`, called by the orchestrator as an explicit extra step
after the main `_BUILDERS` loop completes, rather than moving/splitting `overlay.build()`'s existing peer
logic.

**Node registration (`build/fabric.py` + `rest_aci/writes.py`)** — `fabricNodeIdentP`, boot-seeded one per
real switch `fabricNode` (spines/leaves/border-leaves — NOT controllers; real ACI's Fabric Membership
registers switches joining the fabric, not the APICs themselves) at `uni/controller/nodeidentpol/nodep-{id}`,
so a GET returns already-registered nodes without requiring a prior POST. Keys on the node **id**, not
serial, to stay consistent with `writes.py`'s pre-existing `_materialize_fabric_node` reaction (which parses
the id back out of the same `nodep-(\d+)$` pattern) — a boot-seeded and a later-POSTed registration must
agree on the identifier scheme, or they'd silently diverge into two different DNs for the same node.
`_RN_TEMPLATES` gained a `fabricNodeIdentP -> nodep-{nodeId}` entry (the one batch-2/3 class an actual
Fabric Build playbook POSTs, per CONTRACT.md §3). Regression-tested: `tests/test_pr3b_batch2.py` re-verifies
`test_rest_aci.py::test_add_leaf_reaction`'s exact scenario (POSTing a NEW `nodep-950` must still
materialize a matching `fabricNode`) to confirm boot-seeding existing nodes doesn't interfere with the
write-path reaction for a genuinely new one.

**`infraNodeIdentP` — deliberately NOT built.** No autoACI plugin references it (verified via a fabric-wide
grep across autoACI's `backend/plugins/*.py`). Real ACI's `infraNodeIdentP` is a node *provisioning-policy*
object, a different concept from `fabricNodeIdentP`'s Fabric Membership registration despite the similar
name — building it with `fabricNodeIdentP`-like semantics would be a wrong object satisfying no real
consumer. Removed from CONTRACT.md's catalog entirely rather than built incorrectly; see that file's
batch-2/3 changelog note for the full reasoning.

## PR-9 — Ansible cisco.aci fidelity

The owner's primary public use case for this simulator is running Ansible `cisco.aci` playbooks against
it directly. PR-9 hardens the write/delete/auth surface to match what `cisco.aci`'s
`plugins/module_utils/aci.py` actually sends, verified read-only against the upstream collection source
(https://github.com/CiscoDevNet/ansible-aci). Full behavioral contract: `docs/CONTRACT.md` §11. Highlights:

- **DELETE `/api/mo/{dn}.json`** added (state=absent), server-side idempotent (200-empty on both
  existing-then-removed and already-missing DNs) — see CONTRACT.md §11 for why idempotent-on-missing
  was chosen over 404 despite the module never actually exercising that exact case itself.
- **GET-on-missing-DN is 200-empty, not 404** (FEATURE 6, a live blocker found running an actual playbook
  against the sim: `fatal: APIC Error 103` on the very first task of a `state=present` create). This
  superseded the pre-PR-9 design (404-on-missing), which had been justified as matching a DIFFERENT
  consumer's (autoACI's `ACINotFoundError`) expectation — cisco.aci is now this simulator's primary
  target per the owner's stated use case, and its `api_call()` treats any non-200 GET as fatal with no
  "not found is fine" exception. `/api/class/*` and node-scoped queries already had no such 404 case
  (they return `200` + empty on a no-match query); this brings `/api/mo/*` in line for "the DN itself
  doesn't exist" too.
- **`status="deleted"` cascade** — already correctly removed the full subtree pre-PR-9
  (`MITStore.upsert`'s `status="deleted"` branch predates this PR); PR-9 added regression coverage
  proving it (not just "skips planning children", which is a separate, narrower behavior the
  write-planning layer also has). `status="created,modified"` (and any status other than `"deleted"`)
  is a plain upsert — no special-casing needed since the store only ever special-cases the literal
  `"deleted"` string.
- **annotation/nameAlias/descr passthrough** — no fix needed; `MO.attrs` is a generic dict and the
  write path never allowlists attribute names. PR-9 added the regression test proving the round-trip
  survives POST → mo GET → class query, since this was previously undocumented/unverified rather than
  actually broken.
- **firmwareCtrlrRunning** — new, small: one per controller in `build/fabric.py`, `version` reusing the
  same `site.apic_version` the controller's `topSystem` already carries.

### Certificate accept-mode trust model (cert-auth, PR-9)

cisco.aci supports password-less certificate-signature auth: instead of calling `aaaLogin`, every request
carries a single `Cookie` header with four fields (`APIC-Certificate-Algorithm`, `APIC-Certificate-DN`,
`APIC-Certificate-Fingerprint`, `APIC-Request-Signature` — an RSA-SHA256 signature over
`METHOD + path + body`, base64-encoded). Real APIC verifies that signature against a public key
previously uploaded for the named user (`aaaUserCert`).

**This simulator does NOT verify the signature.** `require_session()` (`rest_aci/auth.py`) implements
**accept-mode**: if all four cookies are present and `APIC-Certificate-DN` parses to
`uni/userext/user-{user}/usercert-{name}` with `user == SIM_USERNAME`, the request is treated as
authenticated for that user — full stop. The `APIC-Request-Signature` bytes are read (cookie must be
non-empty) but never decoded or checked against any key. This is a deliberate, documented trust
shortcut, not an oversight:

- The simulator has no real user-certificate registry (`aaaUserCert` upload flow is out of scope,
  same tier as NX-OS SSH being out of v1 per the "Locked decisions" above) — implementing real
  verification would require building that entire subsystem for a security property the simulator
  doesn't need (there is no real secret being protected; it's a local test fixture). Trusting the
  claimed identity once the DN's user matches `SIM_USERNAME` is sufficient to unblock cert-auth
  playbooks running unmodified against the sim, but is insufficient as a real security boundary.
- **Do not point this accept-mode design at anything internet-reachable or multi-tenant.** Anyone who
  can reach the sim's port can authenticate as `SIM_USERNAME` by sending four cookies with no
  cryptographic proof of possession.
- `SIM_CERT_STRICT` env var is reserved (parsed but currently a no-op) for a **future** mode that would
  register a real public key per user and verify `APIC-Request-Signature` against it. Not implemented in
  PR-9 — out of scope; noted here so a future PR doesn't have to rediscover the gap.

## PR-18 — Tier-1 fabric configuration parameters

Exposes real-ACI-lab-variable Tier-1 fabric knobs that Ansible/`cisco.aci`/aci-py fidelity depends on,
all optional with defaults matching the pre-PR-18 hardcodes so the existing `topology.yaml` (and any
hand-authored one) validates and builds unchanged.

- **`site.controllers: int` (default 3)** — already existed pre-PR-18 (introduced with the APIC-cluster
  work) and was already fully wired: `build/fabric.py`'s controller `fabricNode`/`topSystem`/
  `firmwareCtrlrRunning` loop, the `infraWiNode` appliance-vector double loop (both viewer and member
  ranges), `build/health_faults.py`, and `build/endpoints.py`'s APIC-reserved-port accounting all already
  read `site.controllers` — no hardcoded `3` remained to replace. PR-18 only added `--controllers` to
  `aci-sim new` and surfaced the value in `aci-sim show`.
- **`fabric.tep_pool: str` (default `"10.0.0.0/16"`)** — the infra TEP address space. Stored + CIDR-
  validated + surfaced (`aci-sim show`/`new`). **No builder derives a per-node address from this field** —
  `build/fabric.py`'s `loopback_ip()`/`oob_ip()` are the actual address source (see the pre-existing
  `loopback_pool`/`oob_pool` note in `topology/schema.py`'s `Fabric` docstring, which documents the same
  "hardcoded family, pool is descriptive metadata" pattern this field follows). Treat `tep_pool` as
  store-only/informational until a future PR threads it into an actual infra-address builder.
- **`fabric.infra_vlan: int` (default 3967, range 1-4094)** — the fabric infrastructure VLAN. Stored +
  range-validated + surfaced. No MIT class in this sim currently carries a distinct infra-VLAN attribute
  to wire it into (real ACI's `infraSetPol.fabricInfraVlan` classes are not part of the current CONTRACT.md
  catalog) — store-only, same caveat as `tep_pool`.
- **`fabric.gipo_pool: str` (default `"225.0.0.0/15"`)** — the multicast GIPo pool. Stored + CIDR-validated
  + surfaced. Store-only, same caveat.
- **Deterministic node serials** — `build/fabric.py`'s `default_serial(site_id, node_id)` replaces the
  previous inline `f"SIM{node.id:08d}"` fallback used whenever an auto-generated `Node.serial` is empty
  (`fabricNode.serial` and `fabricNodeIdentP.serial`). New format: `f"SAL{site_id}{node_id:04d}"` (e.g.
  site "1" node 101 → `"SAL10101"`) — encodes the site id so serials stay visibly distinct per site.
  Explicit `serial:` values in a YAML `Node` entry are unaffected (still override — this is only the
  `serial or default_serial(...)` fallback path). No test asserted the old `"SIM..."` format, so this is
  a safe behavior change; `aci-sim show` now prints the resolved serial per node.
- **`site.pod: int` (already existed, default 1)** — verified honored end-to-end: every builder that
  emits a `topology/pod-{N}/...` DN reads `site.pod` (grepped all of `build/*.py`, `rest_aci/writes.py`,
  `control/admin.py` — zero remaining `pod-1`/`pod=1` literals). A site with `pod: 2` builds all its
  `fabricNode`/`topSystem`/etc. DNs under `pod-2` (verified: `topology/pod-2/node-201`, ...).

### N-site limit (verified, not fixed by PR-18)

`aci-sim new --sites N` (N>2) and the builders that only depend on `site.pod`/`site.controllers`/node-ID
offsets work for arbitrary N with zero node-ID collisions (verified with a 3-site scaffold: sites at
offsets 0/200/400, `infraWiNode` count = `controllers²` per site, all DNs correct).

**However, `Topology.other_site(name)` (topology/schema.py) is hard-coded to 2-site semantics**: it
returns "the first site in `self.sites` that is not `name`", not "every other site". Two builders call it
to resolve the ISN/multi-site remote peer:

- `build/overlay.py`'s ISN inter-site eBGP-EVPN peering (`build()`, "ISN: each local spine → each
  remote-site spine loopback").
- `build/endpoints.py`'s stretched-tenant HOME/REMOTE endpoint placement (`is_stretched` branch).

Verified with a live 3-site topology (`generate_topology(sites=3, ...)`, ISN `peer_mesh: full`): site C
(3rd site, asn 65003) peers ISN eBGP ONLY with site A (asn 65001) — `other_site("LABC")` picks the first
non-C site in list order (`LABA`) and never reaches `LABB`. This means with 3+ sites:

- ISN is NOT a true full mesh across all sites, despite `isn.peer_mesh: full` — each site peers with
  exactly one other (the first site in the YAML's `sites:` list order that isn't itself), not N-1 others.
- A tenant with `stretch: true` and 3+ `sites:` entries passes schema validation (the validator only
  requires `len(tenant.sites) >= 2`, not `== 2`) but `endpoints.py`'s remote-peer logic will treat only
  one of the non-home sites as the ISN-reachable remote for tunnel/REMOTE-endpoint purposes.

**This is a pre-existing limitation, not introduced by PR-18** (confirmed via `git diff main -- 
aci_sim/topology/schema.py`: `other_site()` is byte-identical to what PR-18 branched from).
Fixing it to a real N-site full mesh is a bigger architectural change (both call sites would need to
iterate `[s for s in topo.sites if s.name != site.name]` instead of a single `other_site()` call, and
`endpoints.py`'s single `remote_site`/`tunnel_spine`/`home_spine` variables would need to become
per-remote-site collections) — out of scope for a Tier-1-parameters PR. **Practical limit: ISN/stretch
correctness is verified for exactly 2 sites; 3+ site topologies build without collision but do not get
a correct N-site ISN mesh or N-site stretched-endpoint placement.**

## PR-19 — Tier-2 topology-elasticity parameters

Exposes ISN underlay knobs (`ospf_area`, `mtu`) and a VMM domain vCenter-connection model, plus fixes a
real leaf/spine-count-elasticity bug in `aci-sim new`'s node-ID generator. All new fields optional with
defaults matching pre-PR-19 hardcoded behavior, so the existing `topology.yaml` validates and builds
byte-identical output.

### `isn.ospf_area: str` (default `"0.0.0.0"`) and `isn.mtu: int` (default 9150)

Both were previously hardcoded literals in the builders:

- `build/underlay.py`'s `ospfIf`/`ospfAdjEp` `area` attribute was hardcoded to `"0.0.0.0"` — now reads
  `topo.isn.ospf_area`. Accepts either dotted-decimal (`"0.0.0.0"`) or the decimal-integer shorthand ACI
  also renders (`"0"`) — validated in `Topology.normalize_and_validate` via `IPv4Address` parse or
  `str.isdigit()`.
- `build/interfaces.py`'s dedicated ISN/IPN spine uplink port (`eth1/{49+i}`, multi-site only) was built
  via the same `_add_port()` helper as every other port, which hardcoded `mtu="9216"` (the intra-fabric
  default) for every port including this one. `_add_port()` now takes an `mtu` keyword (default `"9216"`,
  unchanged for all ordinary ports); the ISN uplink call site alone passes `str(topo.isn.mtu)`. Verified:
  a leaf's `eth1/49` fabric-uplink port and a spine's `eth1/49` **ISN** uplink port are different physical
  interfaces on different node roles (interfaces.py's port-numbering docstring), so this change touches
  only the true ISN port — ordinary fabric ports (including any leaf port that happens to share the same
  port *number*) are unaffected. `isn.mtu` range-validated 576-9216 (real ACI inter-pod/inter-site MTU
  range, same ceiling as the fabric's own 9216).
- `isn.peer_mesh: partial` remains a **documented validate-and-reject** (unchanged from PR-17/18's
  decision in `build/overlay.py`) — re-examined for PR-19 and kept: a partial mesh has no sensible default
  partition without additional per-site config this schema doesn't carry (which spine pairs with which
  isn't inferable from spine count alone). Silently building full-mesh (ignoring the setting) or an
  arbitrary subset (guessing a partition no author asked for) would both be worse than failing fast with a
  clear message, so PR-19 keeps the existing rejection rather than inventing a partition scheme.

### VMM domain vCenter connection model — pure addition, not a rewire

`access.vmm_domains: list[VMMDomain]` (default: **empty list**, unlike `phys_domains`/`l3_domains`/
`aaeps`, which each default to one seeded entry). Each `VMMDomain` carries `name` (required) plus
optional `vcenter_ip`, `vcenter_dvs`, `vlan_pool`, `datacenter`.

**Why this is additive, not a wire-up of an existing gap** (verified read-only against a real
aci-py + cisco.mso production chain on a lab test host):

- `~/aci-py/aci_py/playbooks.py`'s `bind_epg_to_vmm_domain` playbook (MSO section) drives
  `mso_schema_site_anp_epg_domain` (shimmed in `~/aci-py/aci_py/shims/mso_mso_epg.py`), which appends a
  JSON-Patch op `add /sites/{site}-{tpl}/anps/{anp}/epgs/{epg}/domainAssociations/-` with
  `value.dn = "uni/vmmp-VMware/dom-{domain_profile}"` — a **DN string**, not a live object reference. The
  sim's NDO layer (`aci_sim/ndo/patch.py`) stores this JSON-Patch document verbatim; nothing
  cross-validates the referenced `dn` against a real `vmmDomP` in any APIC-side MIT store.
  `~/aci-py/environments/lab-multisite-sim/vars/bind_epg_to_vmm_domain_MS-TN1.yml` confirms the domain
  names actually exercised in the live MS-TN1 chain (`vmm-vmw-lab1`, `vmm-vmw-lab2`) are just opaque
  strings from this var file's perspective.
- The single-fabric branch of the same playbook template (`bind_epg_to_vmm_domain.j2.yml`'s `APIC`
  section) DOES drive `cisco.aci.aci_domain` (shimmed in `~/aci-py/aci_py/shims/access.py`'s
  `_domain_dn_class`, which maps `vmmDomain` → `vmmDomP` at `uni/vmmp-{provider}/dom-{name}`) — but this
  branch is gated on `single_fabric|default(false)` and is not what the MS-TN1 multi-site chain
  (`scripts/live_chain.py environments/lab-multisite-sim MS-TN1`) exercises.
- Conclusion: **no consumer in either verified production chain (autoACI sandbox e2e, aci-py MS-TN1)
  reads a `vmmDomP`/`vmmCtrlrP` object from this sim today.** Adding one is genuinely new surface, not a
  fix for an existing silently-broken path — hence the empty-list default (true backward-compat baseline:
  omitting `access.vmm_domains` produces zero `vmmDomP`/`vmmCtrlrP`/`vmmUsrAccP` MOs, byte-identical to
  pre-PR-19) rather than seeding a default domain the way `phys_domains`/`l3_domains`/`aaeps` do.

**MO shape** (`build/access.py::_build_vmm_domain`), verified against the real DN scheme in
`~/aci-py/aci_py/shims/access.py`'s `_domain_dn_class` (`"vmmDomP", "uni/vmmp-{provider}/dom-{name}"`) and
`~/aci-py/aci_py/shims/mso_mso_epg.py`'s `dn_map["vmmDomain"] = "uni/vmmp-VMware/dom-{0}"`:

- `vmmDomP` at `uni/vmmp-VMware/dom-{name}` — always built (only "VMware" is modeled; the sole provider
  either verified call site actually exercises).
- `vmmCtrlrP` at `{vmmDomP dn}/ctrlr-{name}` — built **only when `vcenter_ip` is set**: `hostOrIp` =
  `vcenter_ip`, `rootContName` = `datacenter` (falls back to the domain's own `name` if `datacenter` is
  unset).  `dvsName` is additionally set when `vcenter_dvs` is provided.
- `vmmUsrAccP` at `{vmmCtrlrP dn}/usracc-{name}` — built alongside `vmmCtrlrP` (placeholder
  credential-profile reference; no real credentials in a sim, matching the no-secret pattern phys/l3
  domains already use for their own child objects).
- `infraRsVlanNs` at `{vmmDomP dn}/rsvlanNs` — built only when `vlan_pool` is set, pointing at the first
  `access.vlan_pools[]` entry's DN (same convention `_build_phys_dom`/`_build_l3_dom` already use).
  `vlan_pool` is cross-referenced against `access.vlan_pools[].name` in
  `Topology.normalize_and_validate` — an unknown pool name is a validation error, same discipline as
  `tenant.bds[].vrf`/`epg.bd`.

`_build_epg()` in `build/tenants.py` (the actual EPG-domain binding builder) is **untouched** — EPGs still
bind only to the first `phys_domains[]` entry via `fvRsDomAtt`, exactly as before PR-19. This is
intentional: the real production chain's `bind_epg_to_vmm_domain` never touches this MIT tree (see
above), so there was nothing to rewire.

### Leaf/spine count elasticity — a real generator bug found and fixed

`aci-sim new`'s node-ID scheme (`topology/schema.py`'s docstring: leaves base 101, spines base 201,
`site_offset=200`) has a documented ceiling — **"max ~40 leaves + ~40 spines per site before the ranges
touch"** — but prior to PR-19, `generate_topology()` (`cli.py`) had **no guard enforcing that ceiling**.
Stress-testing the exact boundary (required by this PR's acceptance bar) found three distinct ways a
large `--leaves-per-site`/`--spines-per-site`/`--border-pairs` value silently produced an invalid
`topology.yaml` dict that only failed deep inside `Topology.normalize_and_validate`, with a generic "node
ids collide" message that didn't name the offending flag:

1. **`--leaves-per-site > 100`** — the leaf block itself (`101..100+N`) runs into the spine block
   (`201+`). Reproduced: `--leaves-per-site 150 --spines-per-site 4` → leaf ids run 101-250, directly
   overlapping spine ids 201-204.
2. **`--spines-per-site > 100`** — the spine block itself (`201..200+N`) runs into the NEXT site's leaf
   block (`301+`). Reproduced: `--leaves-per-site 2 --spines-per-site 101` → site 0's spine ids run
   201-301, colliding with site 1's leaf ids starting at 301.
3. **Large `--border-pairs`** — the border-leaf block (already pushed past whichever of leaves/spines
   extends further, per the pre-existing PR-17 border-ID fix) can itself run past the next site's leaf
   block if `border_pairs` is large enough relative to a small leaf/spine count. Reproduced:
   `--leaves-per-site 2 --spines-per-site 2 --border-pairs 50` → border ids run up to 300+, colliding with
   the next site's leaf block at 301.

**Fix:** `generate_topology()` now computes the same leaf/spine/border-top arithmetic the ID-assignment
loop uses and raises a `ValueError` naming the specific offending flag *before* building any node dicts,
for all three cases above. Verified boundaries: `--leaves-per-site 100`/`--spines-per-site 100` (exact
max) still validate and build cleanly; `101` of either is rejected with a clear message;
`--border-pairs 49` at the default leaf/spine count is the max safe value, `50` is rejected. The
acceptance-bar case from this PR's scope (`--leaves-per-site 8 --spines-per-site 4`, well within the
safe range) validates clean and builds a full `MITStore` with the expected node count, verified via
`orchestrator.build_site()` (not just schema validation) in `tests/test_pr19_tier2.py`'s
`TestLeafSpineElasticity.test_full_build_succeeds_for_8_leaves_4_spines`.

This is a genuine bug fix (not a new feature) — the ID scheme's stated ceiling was never actually
enforced anywhere before this PR, meaning any topology author who requested more than ~100
leaves/spines-per-site via the CLI would previously get a confusing Pydantic stack trace instead of an
actionable error.

## PR-20 — Tier-3 fine-tuning parameters (final param tier)

The last planned parameter tier. Exposes commonly-tuned mgmt-subnet/BD-MAC/routing-timer/APIC-version
knobs as optional `topology.yaml` fields, wired into a real MO attribute wherever this sim already builds
one — and honestly marked store-only where it doesn't (BFD timers, `inb_subnet`). All new fields optional
with defaults matching pre-existing hardcoded behavior (or the real ACI factory default where there was
no prior hardcoded value at all), so the existing `topology.yaml` validates and builds byte-identical
output — verified via the full `tests/test_pr20_tier3.py::TestBackwardCompat` class plus the unchanged
672-test suite passing alongside 41 new tests (713 total).

### `bd.mac` / `fabric.default_bd_mac` — the critical backward-compat case

`build/tenants.py`'s `_build_bd` hardcoded `fvBD.mac = "00:22:BD:F8:19:FF"` (the real ACI default gateway
MAC) for **every** BD before this PR. `fabric.default_bd_mac` (fabric-wide default) reproduces that exact
literal as its own schema default, and `bd.mac` (per-BD, optional) overrides it when set — resolution
order in `_build_bd` is `bd.mac or topo.fabric.default_bd_mac`. Because the fabric-wide default equals
the pre-existing hardcoded literal exactly, a topology with neither field set produces **byte-identical**
`fvBD.mac` output to pre-PR-20 — this is the single highest-risk field in this PR (any drift from the
existing literal would visibly change every BD's gateway MAC that a consumer like autoACI might read),
so it got its own dedicated backward-compat test (`test_repo_fvBD_mac_is_real_aci_default`) asserting the
literal value directly against the real repo `topology.yaml`, not just "no schema-level default drift."

### BGP keepalive/hold — `l3out.csw_peer.keepalive`/`.hold` → `bgpPeerP`

Real ACI defaults 60s/180s (the standard 1:3 keepalive:hold ratio). `build/l3out.py`'s
`_build_node_profile` already builds a `bgpPeerP` MO (control-plane BGP peer policy, distinct from the
operational `bgpPeer`/`bgpPeerEntry` session MOs `_upsert_ebgp_sessions` builds) with only `addr`/
`peerCtrl` attrs pre-PR-20; this PR adds `keepAliveIntvl`/`holdIntvl` to the same object. Validated:
`hold` must exceed `keepalive` (a hold at or below the keepalive interval can never survive a single
missed keepalive) — same sanity-check pattern as PR-19's `isn.mtu` range check. The operational
`bgpPeer`/`bgpPeerEntry` MOs are **not** touched — real ACI's operational BGP session table doesn't carry
configured-timer attrs (those live on the policy object), so there's nothing to wire there.

### OSPF hello/dead — `isn.ospf_hello`/`.ospf_dead` → `ospfIf`

Real ACI defaults 10s/40s. `build/underlay.py`'s `ospfIf` MO (already built for `isn.ospf_area`, PR-19)
gains `helloIntvl`/`deadIntvl` attrs. Validated: `ospf_dead` must exceed `ospf_hello`, matching real ACI's
own OSPF sanity check (a dead timer at or below the hello interval can never detect a live neighbor
before declaring it down).

### BFD timers — `isn.bfd_min_rx`/`.bfd_min_tx`/`.bfd_multiplier` — explicitly STORE-ONLY

Real ACI defaults 50ms/50ms/3. Grepped `build/*.py` for "bfd"/"Bfd"/"BFD" — **zero matches**. This sim
builds no `bfdIfP`/`bfdRsIfPol`-style MO anywhere, and adding a faithful one would additionally require a
believable `bfdIfStats`/oper-state child tree with no verified consumer (autoACI plugin or aci-py
playbook) to anchor its shape against — inventing an MO purely to hold a timer, with no real read path
exercising it, would be worse than being explicit that these three fields are schema-level only. Store +
range-validate (`min_rx`/`min_tx` positive; `multiplier` 1-50, real ACI's UI range) + surface in
`aci-sim show`/`new`.

### OOB/INB mgmt subnets — `fabric.oob_subnet` (validate-only) / `fabric.inb_subnet` (store-only)

`fabric.oob_subnet` sits alongside the existing `fabric.oob_pool` (PR-early/#27) — `build/fabric.py`'s
`oob_ip()` derives every node's OOB address from `(pod, node_id)` only and does not consume an arbitrary
subnet/pool CIDR (documented limitation, unchanged by this PR). `oob_subnet` is stored + validated (must
fall within `192.168.0.0/16`, same family as `oob_pool`) + surfaced, letting a topology author record the
*intended* OOB subnet distinctly from the pool the builder actually draws from.

`fabric.inb_subnet` is genuinely new surface, not a wire-up of an existing gap: grepped `build/*.py` for
"mgmtInB"/"inbMgmt" — zero matches. Real ACI's in-band management is a distinct `mgmtInB` bridge-domain
living in the special `mgmt` tenant, requiring its own tenant/BD/EPG-style builder — a materially bigger
addition than "wire an existing attr," and out of scope for a fine-tuning-parameters PR. Stored +
validated (must fall within a private RFC1918 range: `10.0.0.0/8`, `172.16.0.0/12`, or `192.168.0.0/16`)
+ surfaced, explicitly marked store-only in `aci-sim show`'s own output (`(store-only)` label) so a user
never mistakes it for something the sim actually builds.

### `fabric.default_apic_version` — fabric-wide override, on top of already-independent versioning

**Important finding while implementing this PR:** the task ask ("make apic_version drive the APIC
controller topSystem/firmwareCtrlrRunning version consistently, independent of switch-node version") was
**already fully satisfied** before this PR — `site.apic_version` (added in the PR-9/PR-18 lineage) has
driven `build/fabric.py`'s controller `fabricNode.version`/`topSystem.version`/`firmwareCtrlrRunning.version`
since PR-9, completely independently of the per-node switch `Node.version` field (PR-18). Verified: grepped
`build/*.py` for the literal `"4.2(7f)"` and found it only as `Site.apic_version`'s own schema default in
`topology/schema.py` — no builder hardcodes an APIC version literal that bypasses `site.apic_version`.

What was missing: (1) no fabric-wide override existed for topologies with many sites that all want the
same APIC version, and (2) `aci-sim show`/`new` never surfaced `apic_version` at all (verified: zero
matches for "apic_version" in pre-PR-20 `cli.py`). This PR adds `fabric.default_apic_version` (optional,
default `None` = unchanged behavior — each site keeps using its own `site.apic_version`) with resolution
`topo.fabric.default_apic_version or site.apic_version` in `build/fabric.py`, and surfaces the *effective*
resolved value per-site in `aci-sim show`'s new `apic_version=` column — closing the surfacing gap without
touching the wiring that already worked.

### Backward-compat verification performed vs. NOT performed (be precise)

**Performed in this sandboxed development environment:** `aci-sim validate` on the repo `topology.yaml`
(OK), the full unit suite (713 passed: 672 pre-existing + 41 new, zero regressions), and `aci-sim show`/
`new` manual smoke checks confirming every new field surfaces correctly.

**NOT performed — no access from this environment:** the real production-chain regression gate this
project's acceptance bar calls for (autoACI sandbox e2e 11/11, aci-py MS-TN1 live_chain 0-fail) requires
an SSH-reachable hardware test host, which does not exist in
this sandboxed clone. The `fvBD.mac` default was double-checked against the pre-PR-20 hardcoded literal
in `build/tenants.py` specifically because it is the one Tier-3 change with plausible autoACI-regression
risk (autoACI reads `fvBD` attrs) — but this is static-code verification, not a live e2e run. The main
thread's independent re-run of the real Ansible/aci-py chains is the authoritative backward-compat gate
for this PR, per this project's established process (several prior PRs passed unit tests but broke the
real production chain).

## PR-21 — single-APIC default + per-controller OOB IPs

**Design intent (owner directive):** APIC cluster size is irrelevant to Ansible playbook *deployment*
testing — a playbook connects to and pushes config through exactly **one** APIC endpoint, regardless of
how many controllers back it in real ACI. Cluster size only shows up in cluster-health/appliance-vector
tooling, e.g. autoACI's `health_score.py`, which reads `infraWiNode` across every controller. This PR
therefore flips `Site.controllers`'s default from `3` (pre-PR-21) to **`1`** (single-APIC-per-site), while
keeping the field fully configurable (validated range **1-5**) for anyone who does need to exercise
multi-controller/cluster-health semantics.

### `Site.controllers` default 3 → 1, validated range 1-5

This is a **breaking change to generated node counts**, not merely a config default: every site's
`fabricNode`/`topSystem` count drops by (old `controllers` − new `controllers`) = 2 by default (e.g. a
2-spine/2-leaf/2-border-leaf/3-controller site was 9 `fabricNode`s; the same shape at the new
1-controller default is 7). `fabricLink`/`lldpAdjEp`/`cdpAdjEp` counts for the APIC↔leaf attachment drop
proportionally (2 leaves × N controllers, so N=3→6 links becomes N=1→2 links), and `infraWiNode` count
(N²) drops from 9 to 1. Every hardcoded count in the test suite and e2e scripts that assumed the old
default was audited and updated to the new default — see the PR-21 commit message / CHANGELOG for the
full list of touched assertions (the pattern used throughout: prefer a test that derives the expected
count from `site.controllers` dynamically, as `test_pr3b_batch2.py`'s `infraWiNode` test already did
pre-PR-21, over one that hardcodes a literal that will need updating again if the default ever changes).
Range validation (1-5) lives in `Topology.normalize_and_validate` alongside the other per-site checks —
`6` (or `0`, or negative) is rejected with a clear error naming the site.

### `Site.controller_ips` — per-controller OOB mgmt IP (optional)

Pre-PR-21, every controller's `topSystem.oobMgmtAddr` was derived from `(pod, controller_id)` via the
same `oob_ip()` helper switch nodes use — **never** from the site's own `mgmt_ip`, which is the address
the sim's REST server actually binds to (controller-1 / the cluster VIP in sandbox mode). That's fine
for a single controller (there's only one address to assign, and CI/tests never asserted a specific
controller OOB IP), but becomes a believability gap for `controllers > 1`: real multi-controller clusters
assign each APIC its own OOB IP typically adjacent to the cluster's primary address, not derived from an
arbitrary per-node formula.

`controller_ips: list[str]` (optional, default unset) fixes this: when set, `controller_ips[i]` is
controller `i+1`'s OOB IP verbatim (length must equal `controllers`, each entry a valid IP — validated in
`Topology.normalize_and_validate`, same place as the `controllers` range check). When unset, `Site.
controller_oob_ips()` (new helper, `topology/schema.py`) auto-derives sequentially from `mgmt_ip`:
controller 1 = `mgmt_ip`, controller 2 = `mgmt_ip + 1`, controller 3 = `mgmt_ip + 2`, etc. (plain
`ipaddress` integer arithmetic, so it correctly rolls over octets). If `mgmt_ip` is *also* unset (empty
string — some hand-authored topologies never set it), `controller_oob_ips()` returns `[]` and
`build/fabric.py` falls back to the legacy `oob_ip(pod, cid)` scheme, so a topology with neither field
set keeps its pre-PR-21 controller OOB addresses unchanged.

Wired into `build/fabric.py`'s controller loop for both `topSystem.oobMgmtAddr` and the leaf-side
`lldpAdjEp`/`cdpAdjEp.mgmtIp` (the neighbor-reported address must match the controller's own OOB IP, or
autoACI's LLDP-based topology rendering would show a controller "announcing" an IP that doesn't match its
own `topSystem`) — both read the same per-call `controller_oob_ips` list so they can never drift apart.

### Shipped `topology.yaml` — dropped to the new default, not left at 3

Both `LAB1`/`LAB2` already omitted an explicit `controllers:` field pre-PR-21 (it was commented out at
its then-default of 3), so they automatically pick up the new default of 1 with no YAML edit required.
The commented example line was updated to describe the *current* default (1) rather than the old one
(3), and a new commented block was added showing the `controllers: 3` + `controller_ips: [...]` opt-in
shape for anyone who copies this file as a starting point and wants multi-controller semantics.

### Backward-compat verification performed vs. NOT performed (be precise)

**Performed in this sandboxed development environment:** `aci-sim validate` on the repo `topology.yaml`
(OK, now reporting 1 controller/site), the full unit suite (counts below), and manual `aci-sim show`
checks confirming `controller_ips` surfaces correctly for both the auto-derived and explicit cases.

**NOT performed from this sandboxed environment** (no SSH/network access here): the real production-chain
regression gate this project's acceptance bar calls for (autoACI sandbox e2e, aci-py MS-TN1
`live_chain.py`) — this PR explicitly changes generated node counts, which is exactly the kind of change
that gate exists to catch (a hardcoded node-count assertion inside autoACI itself, if one exists, would
only surface there). The main thread's independent re-run of the real Ansible/aci-py/autoACI chains on
ACI hardware is the authoritative backward-compat gate for this PR.

## PR-22 — ISN OSPF routed sub-interface + ISN CSW LLDP neighbor

**Design intent:** autoACI's Topology "Fabric (physical)" view derives per-spine ISN uplink rows from
`ospfIf` (port + local IP), `ospfAdjEp` (remote IP), and `lldpAdjEp` (CSW-side port). Pre-PR-22, the sim
modeled the ISN-facing `ospfIf` as a **plain interface** (`eth1/{49+si}`) with **no address**, and there
was no LLDP neighbor on that port at all — so the demo UI showed `Port=eth1/49`, `Local IP=-`,
`Neighbor Port=-`. Real ACI multi-site spines instead use a **routed sub-interface** (VLAN-4 encap, e.g.
`eth1/49.49`, port-named per real spine CLI) that carries the OSPF address, while the ISN switch advertises LLDP on the underlying
physical port. This PR makes the sim match that reality.

### `build/underlay.py` — `ospfIf` becomes a VLAN-4 routed sub-interface

`ospfIf.id`/dn now read `eth1/{49+si}.{49+si}` (was `eth1/{49+si}`), and the MO gained a new `addr` attribute:
`172.16.{site.id}.{si+1}/24` — the same `/24` as the existing OSPF peer `172.16.{site.id}.254`, so the
adjacency stays subnet-consistent. `area`/`helloIntvl`/`deadIntvl`/`operSt` are unchanged. `ospfAdjEp`'s
dn moves under the new sub-if segment (`if-[eth1/{49+si}.{49+si}]` instead of `if-[eth1/{49+si}]`); its
`peerIp`/`nbrId`/`operSt`/`area` attrs are unchanged. The underlying physical port
(`l1PhysIf`/`ethpmPhysIf`, built by `build/interfaces.py`) is **untouched** — only the OSPF logical
interface becomes a sub-interface; LLDP/physical port state stays on the plain port, matching real
hardware (a routed sub-if never carries its own LLDP/physical-layer identity). Consumers resolve the
underlying port via `ifId.split('.')[0]` — the same fallback autoACI's topology code uses.

### `build/fabric.py` — ISN CSW LLDP/CDP neighbor

Each spine's ISN uplink **physical** port (`eth1/{49+si}`) now gets a simulated LLDP/CDP neighbor via the
shared `add_adjacency()` helper (`build/neighbors.py`) — the third of that module's three call sites,
placed directly after the existing APIC↔leaf adjacency loop in `fabric.py`'s `build()`. Fields:
`sysName=ISN-CSW{site.id}`, `portIdV=Ethernet1/{si+1}` (the CSW's own port numbering), `mgmtIp=
172.16.{site.id}.254` (the same IP the OSPF adjacency already peers with), `model=N9K-C9364C`,
`version=n9000-10.2(5)`. The neighbor's chassis MAC uses a deliberately different OUI-shaped prefix
(`02:1B:0D:...`, locally-administered per the 802 "locally administered" bit) than `node_mac()`'s
`00:1B:0D:...` used for real fabric/controller nodes, keyed by `site.id` (one ISN CSW device per site,
not per spine) — this guarantees no collision with any real node's chassis MAC regardless of ID overlap.

Gate: both changes are multi-site only (`len(topo.sites) > 1`, the same condition `underlay.py`'s
ISN OSPF block already used) — a single-site fabric builds no `ospfDom`/`ospfIf`/`ospfAdjEp` and no
`lldpAdjEp` named `ISN-CSW*`, unchanged from pre-PR-22 behavior.

New/updated tests: `tests/test_pr19_tier2.py` (`TestIsnOspfSubInterfaceAndLldp` — sub-if id/addr shape,
`ospfAdjEp` nesting, `lldpAdjEp` fields, single-site negative case);
`tests/test_pr3b_batch1.py`/`tests/test_pr5_topology_consistency.py` (`ospfIf`/`ospfAdjEp` port-resolution
assertions updated to strip the `.4` suffix before matching against `l1PhysIf`).
