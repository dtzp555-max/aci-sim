"""
Pydantic v2 topology models for aci-sim.

Node-ID numbering scheme (deterministic, globally unique within a Topology):
  Regular leaves : base=101, site_offset=200  → site-0: 101,102,…  site-1: 301,302,…
  Spines          : base=201, site_offset=200  → site-0: 201,202,…  site-1: 401,402,…
  Border leaves   : always explicitly specified in the YAML (no auto-generation).

A site_offset of 200 guarantees no collision between any node type across the
first four sites (max ~40 leaves + ~40 spines per site before the ranges touch).
Border-leaf IDs must be chosen outside those ranges or they will be caught by
the cross-site collision check in Topology.normalize_and_validate.

All YAML field names match the Python attribute names except:
  - FilterEntry: uses `from_port`/`to_port`/`ether_type` (DESIGN used `from`/`to`/`ether`,
    which are Python reserved keywords or ambiguous abbreviations).
  - VlanPool: uses `from_vlan`/`to_vlan` for the same reason.
"""

from __future__ import annotations

import ipaddress
import re

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

# ---------------------------------------------------------------------------
# Leaf-level value objects
# ---------------------------------------------------------------------------


class Node(BaseModel):
    """A single fabric node (spine, leaf, or border-leaf) with inventory attrs.

    `version` feeds both fabricNode.version and topSystem.version (CONTRACT §6).
    Auto-expanded nodes inherit the default; explicit node dicts may override it.
    """

    id: int
    name: str
    model: str = "N9K-C9332C"
    serial: str = ""
    version: str = "n9000-14.2(7f)"


class CSW(BaseModel):
    """Core-switch peer config attached to a border-leaf interface."""

    name: str
    asn: int
    peer_ip: str   # CSW's BGP peer IP (remote)
    local_ip: str  # border-leaf's own IP on the peering interface
    vlan: int


class BorderLeaf(BaseModel):
    """A border-leaf node; two entries with the same vpc_domain form a vPC pair."""

    id: int
    name: str
    vpc_domain: str  # shared by the two nodes in a vPC pair
    csw: CSW


class Fabric(BaseModel):
    """Fabric-wide settings + address spaces the underlay/overlay builders draw from.

    Address derivation the builders SHOULD use (documented here so it is one place):
      - Node loopback (router-id / topSystem.address / EVPN peer source):
            10.<pod>.<node_id>.1/32   drawn from `loopback_pool` (default 10.0.0.0/16)
      - OOB mgmt (topSystem.oobMgmtAddr):
            192.168.<pod>.<node_id>/24  drawn from `oob_pool` (default 192.168.0.0/16)
      - ISN inter-site EVPN peer (bgpPeer.addr, always a /32):
            the remote spine's loopback /32 (same 10.<pod>.<node_id>.1 rule).
    Pools are strings (CIDR) so a YAML author can move the whole address space at once.
    """

    name: str
    evpn_rr: str = "spine"  # which role acts as BGP-EVPN route-reflectors; see build/overlay.py::_route_reflectors
    # loopback_pool/oob_pool (#27): the address-derivation scheme in build/fabric.py
    # (loopback_ip/oob_ip) is hardcoded to the "10.<pod>.<node_id>.1" / "192.168.
    # <pod>.<node_id>" families documented there — it is NOT parameterized by an
    # arbitrary CIDR (6+ call sites across build/*.py call loopback_ip(pod, node_id)
    # with no pool argument, and multiple tests assert the literal 10.x/192.168.x
    # output). Rather than leave these fields purely cosmetic, normalize_and_validate
    # below enforces that they match the base network the builder actually produces,
    # so a topology author who edits them gets a clear error instead of silent
    # config/behavior divergence.
    loopback_pool: str = "10.0.0.0/16"    # spine/leaf loopbacks + ISN /32 peers
    oob_pool: str = "192.168.0.0/16"      # topSystem.oobMgmtAddr space
    ndo_mgmt_ip: str = "10.192.0.10"      # sandbox: NDO/ND IP (served on :443 in sandbox mode)

    # ---- Tier-1 fabric parameters (PR-18) --------------------------------
    # These describe real ACI infra address-space knobs that Ansible/aci-py
    # fidelity depends on (e.g. `cisco.aci.aci_fabric_node`'s serial, infra
    # VLAN policy, etc.). All optional + defaulted so existing topology.yaml
    # files keep validating and producing byte-identical builder output.
    #
    # tep_pool: the fabric's infra TEP (Tunnel End-Point) address space —
    # real ACI default is 10.0.0.0/16. No builder currently derives a
    # per-node address FROM this pool (loopback_ip()/oob_ip() above are the
    # actual address source, per the loopback_pool/oob_pool note); tep_pool
    # is stored + validated + surfaced (aci-sim show/new) so it is available
    # for a topology author to record and for future infra-address wiring,
    # rather than silently absent from the schema. See normalize_and_validate
    # for the CIDR check.
    tep_pool: str = "10.0.0.0/16"
    # infra_vlan: the fabric infrastructure VLAN (ACI's dedicated VLAN
    # carrying VXLAN/iVXLAN + APIC↔switch discovery traffic). Real ACI
    # default is 3967; valid range is 1-4094. Store + validate + surface;
    # no MIT object in this sim currently carries an infra-VLAN attribute
    # to wire it into (see docs note in normalize_and_validate).
    infra_vlan: int = 3967
    # gipo_pool: the multicast GIPo (Group IP outer) pool ACI uses for
    # BUM traffic replication per bridge-domain. Real ACI default range is
    # 225.0.0.0/15. Store + validate + surface.
    gipo_pool: str = "225.0.0.0/15"

    # ---- Tier-3 fine-tuning parameters (PR-20) ---------------------------
    # oob_subnet: an explicit CIDR describing the OOB mgmt address space, in
    # addition to the existing `oob_pool` (#27, above). build/fabric.py's
    # `oob_ip()` derives every node's OOB address from `(pod, node_id)`
    # only — it does not consume an arbitrary pool/subnet CIDR (same
    # documented limitation as `oob_pool` itself). `oob_subnet` is stored +
    # validated (must fall within `192.168.0.0/16`, mirroring `oob_pool`'s
    # own validation) so a topology author can record the *intended* OOB
    # subnet distinctly from the pool `oob_ip()` actually draws from, and it
    # is surfaced in `aci-sim show`. Default `None` (omitted) is fully
    # backward compatible — when unset, `aci-sim show` falls back to
    # displaying `oob_pool` as before.
    oob_subnet: str | None = None
    # inb_subnet: the fabric's in-band (INB) management address space. This
    # sim has NO `mgmtInB`/`inbMgmtAddr`-style MO anywhere in build/*.py
    # (grepped: only OOB — `topSystem.oobMgmtAddr` — is built); real ACI's
    # in-band EPG/bridge-domain (`mgmtInB`, in the special `mgmt` tenant) is
    # out of scope for this PR (would require a new `mgmt` tenant + `mgmtInB`
    # + `mgmtRsMgmtBD` builder, a materially bigger addition than "wire an
    # existing attr"). Store + validate (must fall within a private RFC1918
    # range) + surface only — explicitly STORE-ONLY, no MO reflects it.
    inb_subnet: str | None = None
    # default_bd_mac: fabric-wide default gateway MAC for every `fvBD` that
    # doesn't set its own `BD.mac` (below). Real ACI's own default gateway
    # MAC is `00:22:BD:F8:19:FF` — build/tenants.py's `_build_bd` already
    # hardcoded exactly this literal for every BD pre-PR-20, so this default
    # reproduces that value exactly (byte-identical `fvBD.mac` for any BD
    # that doesn't override it — true backward compat, not just "a
    # plausible-looking default").
    default_bd_mac: str = "00:22:BD:F8:19:FF"
    # default_apic_version: fabric-wide override for the APIC controller
    # version (`site.apic_version`, PR-18 lineage — used for the controller
    # `fabricNode`/`topSystem`/`firmwareCtrlrRunning` in build/fabric.py).
    # `site.apic_version` already defaults to `"4.2(7f)"` per-site and is
    # already fully wired/independent of switch-node `Node.version`; this
    # field lets a topology author set ONE fabric-wide APIC version instead
    # of repeating `apic_version:` on every site. Default `None` (omitted)
    # means "use each site's own `apic_version`" — unchanged pre-PR-20
    # behavior. When set, it takes precedence over `site.apic_version` for
    # every site (build/fabric.py: `topo.fabric.default_apic_version or
    # site.apic_version`).
    default_apic_version: str | None = None


# ---------------------------------------------------------------------------
# Site
# ---------------------------------------------------------------------------


class Site(BaseModel):
    """Per-site APIC context.

    `spines` and `leaves` accept either:
    - an integer N  → auto-expanded to N Node objects with deterministic IDs
    - a list of node dicts/Node objects  → used as-is

    The expansion is triggered by Topology.model_validator (which knows each
    site's index).  Use `spine_nodes()`, `leaf_nodes()`, `all_nodes()` to
    access the expanded lists.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    id: str
    apic_host: str
    asn: int
    pod: int = 1
    spines: int | list[Node] = 2
    leaves: int | list[Node] = 2
    border_leaves: list[BorderLeaf] = Field(default_factory=list)
    cabling: str = "auto"  # "auto" = full spine×leaf mesh; "explicit" = cabling section
    # controllers: APIC cluster size (role=controller nodes 1..N). Default is
    # 1 — DELIBERATE (PR-21): APIC cluster size is irrelevant to Ansible
    # playbook deployment testing (a playbook connects to and pushes config
    # through exactly ONE APIC endpoint; cluster size only shows up in
    # cluster-health/appliance-vector tooling like autoACI's health_score.py,
    # which reads infraWiNode across the cluster). Single-APIC-per-site is
    # therefore the shipped default; set controllers: 3 (the pre-PR-21
    # default, and real ACI's minimum supported cluster size) on a site that
    # specifically needs to exercise multi-controller/cluster-health
    # semantics. Validated range 1-5 (real ACI supports 3/5/7 for production
    # clusters, but this sim does not enforce odd-only since it never runs
    # real cluster consensus — 1-5 covers "single APIC" through "test a
    # meaningfully large cluster" without inviting nonsense values).
    controllers: int = 1
    # controller_ips: OPTIONAL explicit per-controller OOB mgmt IP list (PR-21).
    # If set, entry i (0-indexed) is controller (i+1)'s topSystem.oobMgmtAddr;
    # len(controller_ips) must equal `controllers` when both are set (see
    # Topology.normalize_and_validate). If NOT set (default, None), OOB IPs
    # are auto-derived SEQUENTIALLY starting from this site's `mgmt_ip`:
    # controller 1 = mgmt_ip, controller 2 = mgmt_ip + 1, controller 3 =
    # mgmt_ip + 2, ... (build/fabric.py `_controller_oob_ip`). The sim's REST
    # server always binds to `mgmt_ip` regardless of `controllers` (it never
    # runs N separate APIC processes) — `mgmt_ip` is controller-1 / the
    # cluster VIP from the REST server's point of view.
    controller_ips: list[str] | None = None
    apic_version: str = "4.2(7f)"   # APIC version (pairs with switch n9000-14.2(7f))
    fabric_name: str = ""           # topSystem.fabricDomain; defaults to topo.fabric.name
    mgmt_ip: str = ""               # sandbox: this APIC's IP (served on :443; NDO advertises it portless)
    # oob_gateway (OOB-gateway PR): the OOB management default gateway for
    # this site. Real ACI's first-boot dialog collects this same value (see
    # `aci-sim init`'s "APIC OOB gateway" prompt, previously discarded — see
    # cli.py history) and models it on the real MO
    # `uni/tn-mgmt/mgmtp-default/oob-default/rsooBStNode-[<node-dn>]`
    # (`mgmtRsOoBStNode.gw`) alongside each node's own OOB address (`.addr`).
    # Per-site (not fabric-wide) because a real gateway is a property of the
    # site's own OOB subnet/VLAN, matching the wizard's existing per-site
    # question. Optional — default `None` is fully backward compatible: a
    # topology that never sets this still builds the mgmt-tenant OOB
    # scaffolding (build/mgmt.py), just with an empty `gw` attribute on every
    # mgmtRsOoBStNode (no gateway is invented/derived — see build/mgmt.py's
    # module docstring for why).
    oob_gateway: str | None = None

    # Populated by Topology.model_validator after expansion
    _spine_nodes: list[Node] = PrivateAttr(default_factory=list)
    _leaf_nodes: list[Node] = PrivateAttr(default_factory=list)

    # ------------------------------------------------------------------ #
    # Normalisation (called by Topology, not by users directly)           #
    # ------------------------------------------------------------------ #

    def _expand_nodes(self, site_index: int) -> None:
        """Expand integer counts into concrete Node lists.

        ID scheme:
          leaf_id   = 101 + site_index * 200 + node_index  (node_index starts at 0)
          spine_id  = 201 + site_index * 200 + node_index
        """
        if isinstance(self.spines, int):
            self._spine_nodes = [
                Node(
                    id=201 + site_index * 200 + i,
                    name=f"spine-{201 + site_index * 200 + i}",
                )
                for i in range(self.spines)
            ]
        else:
            self._spine_nodes = list(self.spines)

        if isinstance(self.leaves, int):
            self._leaf_nodes = [
                Node(
                    id=101 + site_index * 200 + i,
                    name=f"leaf-{101 + site_index * 200 + i}",
                )
                for i in range(self.leaves)
            ]
        else:
            self._leaf_nodes = list(self.leaves)

    # ------------------------------------------------------------------ #
    # Helpers used by builders                                            #
    # ------------------------------------------------------------------ #

    def spine_nodes(self) -> list[Node]:
        """Expanded spine nodes (call after topology validation)."""
        return self._spine_nodes

    def leaf_nodes(self) -> list[Node]:
        """Expanded regular-leaf nodes (call after topology validation)."""
        return self._leaf_nodes

    def border_leaf_nodes(self) -> list[Node]:
        """Border-leaf nodes as Node objects (id/name only)."""
        return [Node(id=bl.id, name=bl.name) for bl in self.border_leaves]

    def all_nodes(self) -> list[Node]:
        """All nodes: spines + regular leaves + border leaves."""
        return self._spine_nodes + self._leaf_nodes + self.border_leaf_nodes()

    def controller_oob_ips(self) -> list[str]:
        """Effective per-controller OOB mgmt IP list, length == self.controllers.

        - If `controller_ips` is explicitly set, return it verbatim (already
          length-validated against `controllers` by
          Topology.normalize_and_validate).
        - Otherwise auto-derive SEQUENTIALLY from `mgmt_ip`: controller 1 =
          mgmt_ip, controller 2 = mgmt_ip + 1, controller 3 = mgmt_ip + 2, ...
          If `mgmt_ip` is unset (""), falls back to the legacy per-node
          `oob_ip(pod, cid)` scheme by returning an empty list — callers
          (build/fabric.py) detect this and keep the pre-PR-21 behavior.
        """
        if self.controller_ips is not None:
            return list(self.controller_ips)
        if not self.mgmt_ip:
            return []
        base = ipaddress.ip_address(self.mgmt_ip)
        return [str(base + i) for i in range(self.controllers)]


# ---------------------------------------------------------------------------
# ISN
# ---------------------------------------------------------------------------


class ISN(BaseModel):
    enabled: bool = True
    transit_asn: int | None = None  # optional transit ASN for ISN underlay
    peer_mesh: str = "full"            # "full" = every spine peers every remote spine; "partial" is
                                        # validated-and-rejected (see build/overlay.py) — not implemented.

    # ---- Tier-2 ISN parameters (PR-19) ------------------------------------
    # ospf_area: the OSPF area spines run toward the IPN/ISN on their
    # dedicated uplink (build/underlay.py's ospfIf/ospfAdjEp). Real ACI
    # default is the backbone area "0.0.0.0" (some deployments write the
    # decimal-integer form "0" — both are accepted; ACI itself renders area
    # "0.0.0.0" or "0" interchangeably in ospfIf.area, and autoACI's
    # fabric_ospf.py reads the field as an opaque string so both round-trip).
    # Threaded into ospfIf.area in build/underlay.py instead of the prior
    # hardcoded "0.0.0.0" literal.
    ospf_area: str = "0.0.0.0"
    # mtu: the ISN/IPN inter-pod/inter-site uplink MTU. Real ACI's default
    # inter-pod-network / inter-site MTU is 9150 (accounts for VXLAN +
    # outer-header overhead vs. the fabric's own 9216 intra-fabric MTU).
    # Threaded into the ISN uplink l1PhysIf.mtu in build/interfaces.py
    # instead of that module's general fabric-port mtu="9216" literal.
    mtu: int = 9150

    # ---- Tier-3 fine-tuning parameters (PR-20) ------------------------
    # ospf_hello / ospf_dead: the spine<->IPN/ISN OSPF adjacency's
    # hello/dead timers (real ACI defaults: hello 10s, dead 40s — the
    # standard OSPF 4x-hello-interval dead-timer relationship). Threaded
    # into `ospfIf`'s `helloIntvl`/`deadIntvl` attrs in build/underlay.py
    # (that MO is already built for `isn.ospf_area`, PR-19 — this PR adds
    # two more attrs to the same object rather than hardcoding them).
    ospf_hello: int = 10
    ospf_dead: int = 40
    # bfd_min_rx / bfd_min_tx / bfd_multiplier: BFD session timers (real ACI
    # defaults: 50ms/50ms, multiplier 3). This sim builds NO `bfdIfP`/
    # `bfdRsIfPol`-style MO anywhere (grepped build/*.py — zero matches for
    # "bfd"/"Bfd"/"BFD"); adding a faithful bfdIfP would additionally require
    # a believable `bfdIfStats`/oper-state tree with no verified consumer to
    # anchor its shape against. Store + validate + surface only — explicitly
    # STORE-ONLY, no MO reflects these three fields.
    bfd_min_rx: int = 50
    bfd_min_tx: int = 50
    bfd_multiplier: int = 3


# ---------------------------------------------------------------------------
# Tenant sub-models
# ---------------------------------------------------------------------------


class VRF(BaseModel):
    name: str


class BD(BaseModel):
    name: str
    vrf: str
    subnets: list[str] = Field(default_factory=list)  # CIDR strings
    l2stretch: bool = False
    # mac (Tier-3, PR-20): per-BD override of the default-gateway MAC
    # (`fvBD.mac`). Optional — `None` means "use `fabric.default_bd_mac`"
    # (build/tenants.py: `bd.mac or topo.fabric.default_bd_mac`), which
    # itself defaults to the real ACI default `00:22:BD:F8:19:FF` (the
    # literal this sim already hardcoded for every BD pre-PR-20).
    mac: str | None = None


class FilterEntry(BaseModel):
    """A single contract filter entry.

    Field names use `from_port`/`to_port`/`ether_type` instead of the DESIGN's
    abbreviated `from`/`to`/`ether` because `from` is a Python reserved keyword.
    """

    proto: str = "tcp"
    from_port: str = "unspecified"
    to_port: str = "unspecified"
    ether_type: str = "ip"


class Filter(BaseModel):
    name: str
    entries: list[FilterEntry] = Field(default_factory=list)


class Contract(BaseModel):
    name: str
    scope: str = "context"
    filters: list[Filter] = Field(default_factory=list)


class ContractRefs(BaseModel):
    provide: list[str] = Field(default_factory=list)
    consume: list[str] = Field(default_factory=list)


class EPG(BaseModel):
    name: str
    bd: str
    contracts: ContractRefs = Field(default_factory=ContractRefs)


class AP(BaseModel):
    name: str
    epgs: list[EPG] = Field(default_factory=list)


class ExtEPG(BaseModel):
    name: str
    subnets: list[str] = Field(default_factory=list)


class CswPeer(BaseModel):
    """Summary CSW peer info attached to an L3Out (name + BGP peering details)."""

    name: str
    asn: int
    peer_ip: str
    # keepalive / hold (Tier-3, PR-20): the eBGP session's keepalive/hold
    # timers (real ACI defaults: 60s/180s, the standard 1:3 keepalive:hold
    # ratio). Threaded into `bgpPeerP` (build/l3out.py's
    # `_build_node_profile`, the control-plane BGP peer policy object) as
    # its `ctrl`-adjacent timer attrs. Optional — default matches the value
    # this sim's `bgpPeerP`/`bgpPeer` MOs implicitly carried pre-PR-20 (real
    # ACI's own out-of-the-box default), so an unset `csw_peer` produces the
    # same session timers as before.
    keepalive: int = 60
    hold: int = 180


class L3Out(BaseModel):
    name: str
    vrf: str
    site: str | None = None  # optional explicit site association (validated if set)
    border_leaves: list[int] = Field(default_factory=list)  # border-leaf node IDs
    csw_peer: CswPeer | None = None
    ext_epgs: list[ExtEPG] = Field(default_factory=list)


class Tenant(BaseModel):
    name: str
    stretch: bool = False
    sites: list[str] = Field(default_factory=list)
    vrfs: list[VRF] = Field(default_factory=list)
    bds: list[BD] = Field(default_factory=list)
    aps: list[AP] = Field(default_factory=list)
    contracts: list[Contract] = Field(default_factory=list)
    l3outs: list[L3Out] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Endpoints / Faults / Access
# ---------------------------------------------------------------------------


class Endpoints(BaseModel):
    per_epg: int = 3
    ip_pool_from_subnet: bool = True


class FaultSeed(BaseModel):
    severity: str   # critical | major | minor | warning
    code: str       # e.g. "F0532"
    node: int       # node ID
    descr: str


class HealthDefaults(BaseModel):
    node: int = 95
    tenant: int = 98


class Faults(BaseModel):
    seed: list[FaultSeed] = Field(default_factory=list)
    health_defaults: HealthDefaults = Field(default_factory=HealthDefaults)


class VlanPool(BaseModel):
    """VLAN pool.

    Uses `from_vlan`/`to_vlan` instead of `from`/`to` (DESIGN notation) because
    `from` is a Python reserved keyword.
    """

    name: str
    from_vlan: int
    to_vlan: int


class PhysDomain(BaseModel):
    name: str


class L3Domain(BaseModel):
    """L3 external domain; the l3out builder binds l3extRsL3DomAtt(tDn) to it."""

    name: str


class AAEP(BaseModel):
    name: str


class VMMDomain(BaseModel):
    """VMM domain (vmmDomP); optional vCenter connection attrs (Tier-2, PR-19).

    All connection attrs are optional + defaulted so a bare `{name: ...}`
    entry (or an omitted `vmm_domains` list entirely) keeps validating —
    the sim seeds no VMM domain by default (unlike phys/l3 domains/AAEPs,
    which have a default single-entry list), since no existing builder or
    production chain (autoACI e2e / aci-py MS-TN1) reads a vmmDomP/vmmCtrlrP
    MIT object today (see docs/DESIGN.md PR-19 note — `bind_epg_to_vmm_domain`
    in the real aci-py chain writes an NDO schema `domainAssociations` entry
    referencing the domain purely by DN string, never touching an APIC-side
    vmmDomP). Populating these fields lets `aci-sim show`/`new` surface a
    vCenter-backed VMM domain and produces a matching vmmCtrlrP/vmmProvP tree
    in build/access.py for a topology author who wants one, without any
    existing flow depending on it.
    """

    name: str
    vcenter_ip: str = ""     # vmmCtrlrP.hostOrIp; empty = no vmmCtrlrP emitted
    vcenter_dvs: str = ""    # DVS name (informational; stored on vmmCtrlrP.name-adjacent attr)
    vlan_pool: str = ""      # name of a topo.access.vlan_pools[] entry to bind (dynamic pool)
    datacenter: str = ""     # vCenter datacenter; vmmCtrlrP.rootContName


class Access(BaseModel):
    vlan_pools: list[VlanPool] = Field(
        default_factory=lambda: [VlanPool(name="vlan-pool-1", from_vlan=100, to_vlan=200)]
    )
    phys_domains: list[PhysDomain] = Field(
        default_factory=lambda: [PhysDomain(name="phys-dom-1")]
    )
    l3_domains: list[L3Domain] = Field(
        default_factory=lambda: [L3Domain(name="l3dom-1")]
    )
    aaeps: list[AAEP] = Field(default_factory=lambda: [AAEP(name="aaep-1")])
    # vmm_domains: Tier-2 (PR-19) — optional, defaults to an EMPTY list (unlike
    # the other access collections above, which default to one seeded entry).
    # No existing builder/topology.yaml/production-chain references a VMM
    # domain by name today, so an empty default is the true backward-compat
    # baseline: omitting `access.vmm_domains` entirely produces byte-identical
    # builder output to pre-PR-19.
    vmm_domains: list[VMMDomain] = Field(default_factory=list)


class Auth(BaseModel):
    """Fabric admin credentials, set by the `aci-sim init` wizard's Admin
    account step.

    The APIC plane ENFORCES these — `rest_aci/auth.py`'s `aaaLogin` 401s on a
    username/password mismatch, resolved into `SIM_USERNAME`/`SIM_PASSWORD`
    by `cli.py`'s `cmd_run` (topology `auth:` loses to an explicit
    `SIM_USERNAME`/`SIM_PASSWORD` already set in the caller's environment —
    see `build_run_env_argv`'s docstring for the exact precedence). The NDO
    plane (`ndo/app.py`) is lenient BY DESIGN — it accepts any credential
    (client-compat for cisco.mso/aci-py) and is not affected by this model at
    all. `ndo_username`/`ndo_password` exist only so an operator can
    deliberately give NDO a different (informational-only, since NDO never
    checks it) account than APIC; `None` (the default) means "same account as
    APIC", which is what the wizard offers by default.
    """

    username: str = "admin"
    password: str = "cisco"
    ndo_username: str | None = None
    ndo_password: str | None = None


# ---------------------------------------------------------------------------
# Top-level Topology
# ---------------------------------------------------------------------------


class Topology(BaseModel):
    """Root topology model.  Load via `topology.loader.load_topology(path)`."""

    fabric: Fabric
    sites: list[Site]
    isn: ISN = Field(default_factory=ISN)
    tenants: list[Tenant] = Field(default_factory=list)
    endpoints: Endpoints = Field(default_factory=Endpoints)
    faults: Faults = Field(default_factory=Faults)
    access: Access = Field(default_factory=Access)
    # auth: optional fabric admin credentials (see Auth docstring above).
    # None (the default) means no `auth:` section was set — every topology.yaml
    # predating this field keeps validating and behaves exactly as before:
    # `cmd_run` injects nothing and auth.py falls back to its own
    # SIM_USERNAME/SIM_PASSWORD env-or-admin/cisco default.
    auth: Auth | None = None

    @model_validator(mode="after")
    def normalize_and_validate(self) -> Topology:
        """Expand int counts → Node lists, then check all cross-references."""
        # Step 1 — expand
        for idx, site in enumerate(self.sites):
            site._expand_nodes(site_index=idx)

        # Step 1b — controllers range + controller_ips (PR-21). APIC cluster
        # size is configurable 1-5 (default 1, single-APIC-per-site by
        # design — see Site.controllers docstring); controller_ips, if set,
        # must be a valid-IP list of exactly `controllers` entries.
        for site in self.sites:
            if not (1 <= site.controllers <= 5):
                raise ValueError(
                    f"Site '{site.name}': controllers={site.controllers} is out of range. "
                    f"Must be 1-5 (default 1 — single APIC per site by design; set 3 for "
                    f"real ACI's minimum multi-controller cluster size if you need to "
                    f"exercise cluster-health/infraWiNode semantics)."
                )
            if site.controller_ips is not None:
                if len(site.controller_ips) != site.controllers:
                    raise ValueError(
                        f"Site '{site.name}': controller_ips has {len(site.controller_ips)} "
                        f"entries but controllers={site.controllers}. When controller_ips is "
                        f"set it must have exactly one IP per controller."
                    )
                for ip in site.controller_ips:
                    try:
                        ipaddress.ip_address(ip)
                    except ValueError as exc:
                        raise ValueError(
                            f"Site '{site.name}': controller_ips entry {ip!r} is not a valid IP "
                            f"address: {exc}"
                        ) from exc

            # oob_gateway (OOB-gateway PR): if set, must be a valid IP address.
            # Optional (None = no gateway configured for this site — see
            # Site.oob_gateway docstring); no further range/subnet check is
            # enforced here (the gateway may legitimately sit outside
            # fabric.oob_pool/oob_subnet, e.g. a shared OOB VLAN gateway that
            # predates this sim's address scheme).
            if site.oob_gateway is not None:
                try:
                    ipaddress.ip_address(site.oob_gateway)
                except ValueError as exc:
                    raise ValueError(
                        f"Site '{site.name}': oob_gateway {site.oob_gateway!r} is not a valid "
                        f"IP address: {exc}"
                    ) from exc

        # Step 2 — collect global node/border identity sets (used by several checks)
        site_names = {s.name for s in self.sites}
        all_node_ids: list[int] = []          # every node id across every site (with dups)
        border_leaf_ids: set[int] = set()     # all border-leaf ids across all sites
        for site in self.sites:
            all_node_ids.extend(n.id for n in site.all_nodes())
            border_leaf_ids.update(bl.id for bl in site.border_leaves)
        node_id_set = set(all_node_ids)

        # Global node-id uniqueness — border-leaf ids must NOT collide with the
        # auto-generated spine/leaf ids, and no two sites may share a node id.
        duplicates = sorted({nid for nid in all_node_ids if all_node_ids.count(nid) > 1})
        if duplicates:
            raise ValueError(
                f"Node ids collide across sites (border-leaf vs auto-generated spine/leaf, "
                f"or two sites overlapping): {duplicates}. "
                f"Adjust border_leaf ids or the site offset."
            )

        # Step 3 — cross-reference validation
        for tenant in self.tenants:
            vrf_names = {v.name for v in tenant.vrfs}
            bd_names = {b.name for b in tenant.bds}
            contract_names = {c.name for c in tenant.contracts}

            # tenant.sites → real site names
            for sname in tenant.sites:
                if sname not in site_names:
                    raise ValueError(
                        f"Tenant '{tenant.name}' references unknown site '{sname}'. "
                        f"Known sites: {sorted(site_names)}"
                    )

            # stretched tenant must name ≥2 sites
            if tenant.stretch and len(tenant.sites) < 2:
                raise ValueError(
                    f"Tenant '{tenant.name}' has stretch=true but lists fewer than 2 sites: "
                    f"{tenant.sites}"
                )

            # BD.vrf → tenant VRFs
            for bd in tenant.bds:
                if bd.vrf not in vrf_names:
                    raise ValueError(
                        f"Tenant '{tenant.name}', BD '{bd.name}' references unknown VRF "
                        f"'{bd.vrf}'. Known VRFs: {sorted(vrf_names)}"
                    )

            # EPG.bd → tenant BDs  +  EPG contract refs → tenant contracts
            for ap in tenant.aps:
                for epg in ap.epgs:
                    if epg.bd not in bd_names:
                        raise ValueError(
                            f"Tenant '{tenant.name}', AP '{ap.name}', EPG '{epg.name}' "
                            f"references unknown BD '{epg.bd}'. Known BDs: {sorted(bd_names)}"
                        )
                    for cname in epg.contracts.provide + epg.contracts.consume:
                        if cname not in contract_names:
                            raise ValueError(
                                f"Tenant '{tenant.name}', AP '{ap.name}', EPG '{epg.name}' "
                                f"references unknown contract '{cname}'. "
                                f"Known contracts: {sorted(contract_names)}"
                            )

            # L3Out cross-refs: vrf, optional site, border-leaf ids
            for l3out in tenant.l3outs:
                if l3out.vrf not in vrf_names:
                    raise ValueError(
                        f"Tenant '{tenant.name}', L3Out '{l3out.name}' references unknown VRF "
                        f"'{l3out.vrf}'. Known VRFs: {sorted(vrf_names)}"
                    )
                if l3out.site is not None and l3out.site not in site_names:
                    raise ValueError(
                        f"Tenant '{tenant.name}', L3Out '{l3out.name}' references unknown site "
                        f"'{l3out.site}'. Known sites: {sorted(site_names)}"
                    )
                for bl_id in l3out.border_leaves:
                    if bl_id not in border_leaf_ids:
                        raise ValueError(
                            f"Tenant '{tenant.name}', L3Out '{l3out.name}' references border-leaf "
                            f"id {bl_id} which is not a border-leaf of any site. "
                            f"Known border-leaf ids: {sorted(border_leaf_ids)}"
                        )

        # Fault seeds must target a real node id
        for fseed in self.faults.seed:
            if fseed.node not in node_id_set:
                raise ValueError(
                    f"Fault seed (code {fseed.code}) targets node {fseed.node} which is not a "
                    f"real node id. Known node ids: {sorted(node_id_set)}"
                )

        # ISN peer_mesh:full requires ≥2 sites
        if self.isn.enabled and self.isn.peer_mesh == "full" and len(self.sites) < 2:
            raise ValueError("ISN peer_mesh:'full' requires at least 2 sites.")

        # fabric.loopback_pool/oob_pool (#27): builders hardcode the "10.x"/
        # "192.168.x" families (see build/fabric.py loopback_ip/oob_ip) — the
        # pool fields are not threaded through as an arbitrary CIDR, so make
        # sure they at least describe the network the builder actually uses,
        # instead of being silently ignorable config drift.
        try:
            loopback_net = ipaddress.ip_network(self.fabric.loopback_pool, strict=False)
        except ValueError as exc:
            raise ValueError(
                f"fabric.loopback_pool {self.fabric.loopback_pool!r} is not a valid CIDR: {exc}"
            ) from exc
        if not loopback_net.overlaps(ipaddress.ip_network("10.0.0.0/8")):
            raise ValueError(
                f"fabric.loopback_pool {self.fabric.loopback_pool!r} must fall within "
                f"10.0.0.0/8 — the builder's loopback_ip() always emits "
                f"10.<pod>.<node_id>.1-style addresses (build/fabric.py) and does not "
                f"honor an arbitrary pool."
            )
        try:
            oob_net = ipaddress.ip_network(self.fabric.oob_pool, strict=False)
        except ValueError as exc:
            raise ValueError(
                f"fabric.oob_pool {self.fabric.oob_pool!r} is not a valid CIDR: {exc}"
            ) from exc
        if not oob_net.overlaps(ipaddress.ip_network("192.168.0.0/16")):
            raise ValueError(
                f"fabric.oob_pool {self.fabric.oob_pool!r} must fall within "
                f"192.168.0.0/16 — the builder's oob_ip() always emits "
                f"192.168.<pod>.<node_id>-style addresses (build/fabric.py) and does "
                f"not honor an arbitrary pool."
            )

        # Tier-1 fabric params (PR-18): tep_pool/gipo_pool must be valid CIDRs;
        # infra_vlan must fall in the real ACI VLAN range (1-4094). None of
        # these are threaded into a builder's address-derivation the way
        # loopback_pool/oob_pool partially are (see Fabric docstring above),
        # so this is a pure well-formedness check — it exists to catch a
        # typo'd CIDR/VLAN before it reaches `aci-sim show`/CI, not to
        # enforce agreement with a builder's hardcoded output.
        try:
            ipaddress.ip_network(self.fabric.tep_pool, strict=False)
        except ValueError as exc:
            raise ValueError(
                f"fabric.tep_pool {self.fabric.tep_pool!r} is not a valid CIDR: {exc}"
            ) from exc
        try:
            ipaddress.ip_network(self.fabric.gipo_pool, strict=False)
        except ValueError as exc:
            raise ValueError(
                f"fabric.gipo_pool {self.fabric.gipo_pool!r} is not a valid CIDR: {exc}"
            ) from exc
        if not (1 <= self.fabric.infra_vlan <= 4094):
            raise ValueError(
                f"fabric.infra_vlan {self.fabric.infra_vlan!r} must be in the range "
                f"1-4094 (real ACI VLAN ID range)."
            )

        # Tier-2 ISN params (PR-19): ospf_area must be a valid OSPF area
        # (dotted-decimal like an IPv4 address, e.g. "0.0.0.0", or the
        # decimal-integer shorthand ACI also accepts, e.g. "0"); mtu must
        # fall within real ACI's inter-pod/inter-site MTU range (576-9216,
        # matching the fabric's own l1PhysIf.mtu ceiling in interfaces.py).
        area = self.isn.ospf_area
        area_ok = False
        if area.isdigit():
            area_ok = True  # decimal-integer area form (e.g. "0")
        else:
            try:
                ipaddress.IPv4Address(area)
                area_ok = True  # dotted-decimal area form (e.g. "0.0.0.0")
            except ValueError:
                area_ok = False
        if not area_ok:
            raise ValueError(
                f"isn.ospf_area {area!r} must be a dotted-decimal OSPF area "
                f"(e.g. '0.0.0.0') or a decimal-integer area (e.g. '0')."
            )
        if not (576 <= self.isn.mtu <= 9216):
            raise ValueError(
                f"isn.mtu {self.isn.mtu!r} must be in the range 576-9216 "
                f"(real ACI inter-pod/inter-site MTU range)."
            )

        # Tier-3 fine-tuning params (PR-20) ------------------------------
        # OSPF hello/dead: both must be positive, and (matching real ACI's
        # own OSPF sanity check) dead must exceed hello — a dead timer
        # shorter than (or equal to) the hello interval can never detect a
        # live neighbor before declaring it down.
        if self.isn.ospf_hello <= 0 or self.isn.ospf_dead <= 0:
            raise ValueError(
                f"isn.ospf_hello ({self.isn.ospf_hello}) and isn.ospf_dead "
                f"({self.isn.ospf_dead}) must both be positive integers (seconds)."
            )
        if self.isn.ospf_dead <= self.isn.ospf_hello:
            raise ValueError(
                f"isn.ospf_dead ({self.isn.ospf_dead}) must be greater than "
                f"isn.ospf_hello ({self.isn.ospf_hello}) — a dead timer at or "
                f"below the hello interval can never detect a live neighbor."
            )
        # BFD timers: store+validate only (no bfdIfP MO built anywhere in
        # this sim — see ISN docstring). min_rx/min_tx must be positive
        # milliseconds; multiplier must be a plausible BFD detect multiplier
        # (real ACI UI range is 1-50).
        if self.isn.bfd_min_rx <= 0 or self.isn.bfd_min_tx <= 0:
            raise ValueError(
                f"isn.bfd_min_rx ({self.isn.bfd_min_rx}) and isn.bfd_min_tx "
                f"({self.isn.bfd_min_tx}) must both be positive integers (milliseconds)."
            )
        if not (1 <= self.isn.bfd_multiplier <= 50):
            raise ValueError(
                f"isn.bfd_multiplier ({self.isn.bfd_multiplier}) must be in the "
                f"range 1-50 (real ACI BFD detect-multiplier range)."
            )

        # BGP keepalive/hold (per l3out.csw_peer): must be positive, and
        # (matching real ACI's own BGP timer sanity check) hold must be at
        # least 2x keepalive's typical relationship — real ACI enforces
        # hold >= 3 or hold==0; we enforce the simpler, always-correct
        # invariant that hold exceeds keepalive (a hold at or below the
        # keepalive interval can never survive a single missed keepalive).
        for tenant in self.tenants:
            for l3out in tenant.l3outs:
                if l3out.csw_peer is None:
                    continue
                cp = l3out.csw_peer
                if cp.keepalive <= 0 or cp.hold <= 0:
                    raise ValueError(
                        f"Tenant '{tenant.name}', L3Out '{l3out.name}': csw_peer "
                        f"keepalive ({cp.keepalive}) and hold ({cp.hold}) must both "
                        f"be positive integers (seconds)."
                    )
                if cp.hold <= cp.keepalive:
                    raise ValueError(
                        f"Tenant '{tenant.name}', L3Out '{l3out.name}': csw_peer "
                        f"hold ({cp.hold}) must be greater than keepalive "
                        f"({cp.keepalive}) — a hold timer at or below the keepalive "
                        f"interval can never survive a single missed keepalive."
                    )

        # BD MAC / fabric.default_bd_mac: must look like a real MAC address
        # (real ACI's fvBD.mac accepts colon-separated hex octets, e.g.
        # "00:22:BD:F8:19:FF"). Reject anything else at load time rather
        # than silently writing a malformed fvBD.mac attribute.
        _mac_re = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
        if not _mac_re.match(self.fabric.default_bd_mac):
            raise ValueError(
                f"fabric.default_bd_mac {self.fabric.default_bd_mac!r} is not a "
                f"valid MAC address (expected colon-separated hex octets, e.g. "
                f"'00:22:BD:F8:19:FF')."
            )
        for tenant in self.tenants:
            for bd in tenant.bds:
                if bd.mac is not None and not _mac_re.match(bd.mac):
                    raise ValueError(
                        f"Tenant '{tenant.name}', BD '{bd.name}': bd.mac {bd.mac!r} "
                        f"is not a valid MAC address (expected colon-separated hex "
                        f"octets, e.g. '00:22:BD:F8:19:FF')."
                    )

        # oob_subnet / inb_subnet: pure well-formedness + range checks (both
        # are store+validate-only — see Fabric docstring above). oob_subnet
        # must fall within 192.168.0.0/16 (same family oob_pool is
        # validated against); inb_subnet must be a private RFC1918 range
        # (ACI's in-band mgmt EPG is conventionally carved out of RFC1918
        # space, same as OOB) and must be a valid CIDR.
        if self.fabric.oob_subnet is not None:
            try:
                oob_subnet_net = ipaddress.ip_network(self.fabric.oob_subnet, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"fabric.oob_subnet {self.fabric.oob_subnet!r} is not a valid "
                    f"CIDR: {exc}"
                ) from exc
            if not oob_subnet_net.overlaps(ipaddress.ip_network("192.168.0.0/16")):
                raise ValueError(
                    f"fabric.oob_subnet {self.fabric.oob_subnet!r} must fall "
                    f"within 192.168.0.0/16, matching fabric.oob_pool's own "
                    f"address family."
                )
        if self.fabric.inb_subnet is not None:
            try:
                inb_net = ipaddress.ip_network(self.fabric.inb_subnet, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"fabric.inb_subnet {self.fabric.inb_subnet!r} is not a valid "
                    f"CIDR: {exc}"
                ) from exc
            _rfc1918 = (
                ipaddress.ip_network("10.0.0.0/8"),
                ipaddress.ip_network("172.16.0.0/12"),
                ipaddress.ip_network("192.168.0.0/16"),
            )
            if not any(inb_net.overlaps(net) for net in _rfc1918):
                raise ValueError(
                    f"fabric.inb_subnet {self.fabric.inb_subnet!r} must fall "
                    f"within a private RFC1918 range (10.0.0.0/8, "
                    f"172.16.0.0/12, or 192.168.0.0/16)."
                )

        # Tier-2 VMM domain params (PR-19): a VMM domain's `vlan_pool` (if
        # set) must reference a real access.vlan_pools[] entry, same
        # cross-reference discipline as tenant.bds[].vrf / epg.bd above.
        vlan_pool_names = {p.name for p in self.access.vlan_pools}
        for vmm in self.access.vmm_domains:
            if vmm.vlan_pool and vmm.vlan_pool not in vlan_pool_names:
                raise ValueError(
                    f"access.vmm_domains '{vmm.name}' references unknown vlan_pool "
                    f"'{vmm.vlan_pool}'. Known vlan pools: {sorted(vlan_pool_names)}"
                )

        # CSW peering params are duplicated between site.border_leaves[].csw and
        # tenant.l3outs[].csw_peer (#28) — the border-leaf's `csw` is authoritative
        # (it carries local_ip/vlan too, which l3out.csw_peer lacks; l3out.py's
        # per-path encap/local_ip/l3extMember addressing comes only from
        # border_leaf.csw). Any l3out.csw_peer covering the same border leaves
        # must agree with the border-leaf's csw on name/asn/peer_ip, or the
        # rendered BGP session (from csw_peer) and the physical sub-interface
        # (from border_leaf.csw) would silently describe two different CSWs.
        bl_csw_by_id = {
            bl.id: bl.csw
            for site in self.sites
            for bl in site.border_leaves
        }
        for tenant in self.tenants:
            for l3out in tenant.l3outs:
                if l3out.csw_peer is None:
                    continue
                for bl_id in l3out.border_leaves:
                    bl_csw = bl_csw_by_id.get(bl_id)
                    if bl_csw is None:
                        continue  # already reported by the border-leaf existence check above
                    mismatch = (
                        bl_csw.name != l3out.csw_peer.name
                        or bl_csw.asn != l3out.csw_peer.asn
                        or bl_csw.peer_ip != l3out.csw_peer.peer_ip
                    )
                    if mismatch:
                        raise ValueError(
                            f"Tenant '{tenant.name}', L3Out '{l3out.name}': csw_peer "
                            f"{{name={l3out.csw_peer.name!r}, asn={l3out.csw_peer.asn}, "
                            f"peer_ip={l3out.csw_peer.peer_ip!r}}} does not match border-leaf "
                            f"{bl_id}'s csw {{name={bl_csw.name!r}, asn={bl_csw.asn}, "
                            f"peer_ip={bl_csw.peer_ip!r}}}. These must agree — the border-leaf's "
                            f"csw is authoritative (see docs/DESIGN.md)."
                        )

        return self

    # ------------------------------------------------------------------ #
    # Topology-level helpers                                              #
    # ------------------------------------------------------------------ #

    def site_by_name(self, name: str) -> Site:
        """Look up a site by name; raises KeyError if not found."""
        for site in self.sites:
            if site.name == name:
                return site
        raise KeyError(f"Site '{name}' not found in topology")

    def other_site(self, name: str) -> Site:
        """Return the first site that is NOT `name`.

        Useful for ISN remote-ASN lookup in a 2-site topology.
        Raises KeyError if no other site exists.
        """
        others = [s for s in self.sites if s.name != name]
        if not others:
            raise KeyError(f"No site other than '{name}' in topology")
        return others[0]
