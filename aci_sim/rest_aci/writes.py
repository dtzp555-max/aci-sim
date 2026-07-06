"""Write handler for POST /api/mo/{dn}.json."""

from __future__ import annotations

import re

from aci_sim.build.fabric import loopback_ip, oob_ip
from aci_sim.build.interfaces import _HOST_PORTS, _UPLINK_START, _add_port
from aci_sim.build.neighbors import add_switch_adjacency
from aci_sim.mit.dn import pod_from_dn
from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.topology.schema import Node

# Class -> RN template for children POSTed without an explicit ``dn``.
#
# Real APIC derives a child's RN from the class's RN *prefix*, not from the
# class name itself (see aci_sim/mit/dn.py's dn_class_hint, which maps
# the same prefixes back to classes for the read path). Templates below are
# taken verbatim from the DNs the builders emit (aci_sim/build/*.py),
# so POSTed objects land on the same canonical DN as builder-created ones:
#
#   fvBD      -> BD-{name}          (build/tenants.py:_build_bd, dn=f"uni/tn-{t}/BD-{bd.name}")
#   fvAp      -> ap-{name}          (build/tenants.py:_build_tenant, dn=f"uni/tn-{t}/ap-{ap.name}")
#   fvAEPg    -> epg-{name}         (build/tenants.py:_build_epg, dn=.../ap-{ap_name}/epg-{epg.name})
#   fvCtx     -> ctx-{name}         (build/tenants.py:_build_tenant, dn=f"uni/tn-{t}/ctx-{vrf.name}")
#   fvSubnet  -> subnet-[{ip}]      (build/tenants.py:_build_bd, dn=.../BD-{bd.name}/subnet-[{cidr}])
#   fvRsCtx   -> rsctx              (build/tenants.py:_build_bd, dn=.../BD-{bd.name}/rsctx, fixed RN)
#   fvRsBd    -> rsbd               (build/tenants.py:_build_epg, dn=.../epg-{epg.name}/rsbd, fixed RN)
#   fvRsBDToOut -> rsBDToOut-{tnL3extOutName}
#                                   (build/tenants.py:_build_bd, dn=.../rsBDToOut-{l3out.name})
#   fvRsDomAtt -> rsdomAtt-[{tDn}]  (build/tenants.py:_build_epg, dn=.../rsdomAtt-[uni/phys-{phys_dom}])
#   fvRsProv  -> rsprov-{tnVzBrCPName}
#                                   (build/tenants.py:_build_epg, dn=.../rsprov-{c})
#   fvRsCons  -> rscons-{tnVzBrCPName}
#                                   (build/tenants.py:_build_epg, dn=.../rscons-{c})
#   vzBrCP    -> brc-{name}         (build/tenants.py:_build_contract, dn=f"uni/tn-{t}/brc-{contract.name}")
#   vzSubj    -> subj-{name}        (build/tenants.py:_build_contract, dn=.../brc-{name}/subj-{name})
#   vzRsSubjFiltAtt -> rssubjFiltAtt-{tnVzFilterName}
#                                   (build/tenants.py:_build_contract, dn=.../rssubjFiltAtt-{flt.name})
#   vzFilter  -> flt-{name}         (build/tenants.py:_build_filter, dn=f"uni/tn-{t}/flt-{flt.name}")
#   vzEntry   -> e-{name}           (build/tenants.py:_build_filter, dn=.../flt-{name}/e-{ename})
#
# Batch-1 additions (RN schemes taken verbatim from the same builders that
# now emit these classes — see build/access.py + build/l3out.py + build/
# tenants.py docstrings for the full DN derivation/attribute-source notes):
#   infraAccPortP    -> accportprof-{name}       (build/access.py:_build_leaf_profile)
#   infraHPortS      -> hports-{name}-typ-range  (build/access.py:_build_hports)
#   infraPortBlk     -> portblk-block1           (build/access.py:_build_hports; fixed —
#                                                  one block per selector in this sim)
#   infraRsAccBaseGrp -> rsaccBaseGrp             (build/access.py:_build_hports, fixed RN)
#   infraAccBndlGrp  -> accbundle-{name}         (build/access.py:_build_bndl_grp)
#   rtctrlProfile    -> prof-{name}              (build/l3out.py:_build_route_control)
#   rtctrlCtxP       -> ctx-{name}               (build/l3out.py:_build_route_control)
#   rtctrlSubjP      -> subj-{name}              (build/l3out.py:_build_tenant_subj)
#   rtctrlMatchRtDest -> dest-[{ip}]              (build/l3out.py:_build_tenant_subj)
#   ipRouteP         -> rt-[{ip}]                (build/l3out.py:_build_node_profile)
#   ipNexthopP       -> nh-[{nhAddr}]            (build/l3out.py:_build_node_profile)
#   l3extMember      -> mem-A / mem-B            (build/l3out.py:_build_node_profile; side
#                                                  picks the RN suffix directly, not a name/attr)
#   ospfExtP         -> ospfExtP                 (build/l3out.py:_build_l3out_tree, fixed RN)
#   fvRsPathAtt      -> rspathAtt-[{tDn}]        (build/tenants.py:_build_epg)
#   vzRsSubjGraphAtt -> rsSubjGraphAtt           (build/tenants.py:_build_contract, fixed RN)
#
# Batch-2 addition — fabricNodeIdentP, the ONE batch-2 class an actual Fabric
# Build playbook POSTs (CONTRACT.md §3 "Node registration reaction":
# initial_fabric_discovery/add_leaf_switch_pair/add_spine_switch all POST
# fabricNodeIdentP; ansible's aci_fabric_node module keys the object on the
# node id, matching both the RN template's "nodeId" attribute AND the real
# ACI RN uni/controller/nodeidentpol/nodep-{id} — see build/fabric.py's
# boot-seeding docstring, which seeds the SAME nodep-{id} scheme so a
# boot-seeded registration and a later POSTed one always land on the same DN
# instead of silently diverging into nodep-{id} vs nodep-{serial}):
#   fabricNodeIdentP -> nodep-{nodeId}   (build/fabric.py:build, dn=f"uni/controller/
#                                          nodeidentpol/nodep-{node.id}")
#
# Each entry is (attribute-to-read-the-value-from, rn-format). A rn-format of
# None means the RN is fixed (no suffix, no attribute lookup).
_RN_TEMPLATES: dict[str, tuple[str | None, str]] = {
    "fvBD": ("name", "BD-{}"),
    "fvAp": ("name", "ap-{}"),
    "fvAEPg": ("name", "epg-{}"),
    "fvCtx": ("name", "ctx-{}"),
    "fvSubnet": ("ip", "subnet-[{}]"),
    "fvRsCtx": (None, "rsctx"),
    "fvRsBd": (None, "rsbd"),
    "fvRsBDToOut": ("tnL3extOutName", "rsBDToOut-{}"),
    "fvRsDomAtt": ("tDn", "rsdomAtt-[{}]"),
    "fvRsProv": ("tnVzBrCPName", "rsprov-{}"),
    "fvRsCons": ("tnVzBrCPName", "rscons-{}"),
    "vzBrCP": ("name", "brc-{}"),
    "vzSubj": ("name", "subj-{}"),
    "vzRsSubjFiltAtt": ("tnVzFilterName", "rssubjFiltAtt-{}"),
    "vzFilter": ("name", "flt-{}"),
    "vzEntry": ("name", "e-{}"),
    "infraAccPortP": ("name", "accportprof-{}"),
    "infraHPortS": ("name", "hports-{}-typ-range"),
    "infraPortBlk": (None, "portblk-block1"),
    "infraRsAccBaseGrp": (None, "rsaccBaseGrp"),
    "infraAccBndlGrp": ("name", "accbundle-{}"),
    "rtctrlProfile": ("name", "prof-{}"),
    "rtctrlCtxP": ("name", "ctx-{}"),
    "rtctrlSubjP": ("name", "subj-{}"),
    "rtctrlMatchRtDest": ("ip", "dest-[{}]"),
    "ipRouteP": ("ip", "rt-[{}]"),
    "ipNexthopP": ("nhAddr", "nh-[{}]"),
    "l3extMember": ("side", "mem-{}"),
    "ospfExtP": (None, "ospfExtP"),
    "fvRsPathAtt": ("tDn", "rspathAtt-[{}]"),
    "vzRsSubjGraphAtt": (None, "rsSubjGraphAtt"),
    "fabricNodeIdentP": ("nodeId", "nodep-{}"),
}

#: Class-keyed default attribute sets, filled ONLY on CREATE (matching real
#: APIC, which commits object defaults at create time; posted attrs always win).
#: Scoped to fvBD — closes the "BD host_route / EP-move show empty" fidelity gap.
_CLASS_DEFAULTS: dict[str, dict[str, str]] = {
    "fvBD": {
        "mac": "00:22:BD:F8:19:FF",
        "type": "regular",
        "arpFlood": "no",
        "unkMacUcastAct": "proxy",
        "unkMcastAct": "flood",
        "v6unkMcastAct": "nd",
        "multiDstPktAct": "bd-flood",
        "epMoveDetectMode": "",          # real create-default: GARP detection OFF
        "hostBasedRouting": "no",
        "limitIpLearnToSubnets": "yes",
        "ipLearning": "yes",
        "unicastRoute": "yes",
        "intersiteBumTrafficAllow": "no",
        "mtu": "9000",
    },
}


class WriteValidationError(ValueError):
    """Raised when a POST body fails shape validation before any store mutation."""


def _child_dn(parent_dn: str, cls: str, attrs: dict, index: int) -> str:
    """Derive a non-empty, correctly-parented DN for a child that omits ``dn``.

    ACI POST bodies may nest children without an explicit dn. Storing them at
    dn="" clobbers (last-writer-wins). Real APIC derives the RN from the
    class's RN *prefix*, not from the class name, so known classes use the
    canonical template harvested from the builders (see ``_RN_TEMPLATES``
    above) — this keeps POSTed objects on the same DN as builder-created ones
    (e.g. fvBD -> "BD-{name}", not "fvBD-{name}").
    """
    dn = attrs.get("dn")
    if dn:
        return dn

    template = _RN_TEMPLATES.get(cls)
    if template is not None:
        attr_name, rn_format = template
        if attr_name is None:
            # Fixed RN (e.g. fvRsCtx -> "rsctx"): no per-instance suffix at all.
            rn = rn_format
        else:
            value = str(attrs.get(attr_name) or index)
            rn = rn_format.format(value)
        return f"{parent_dn}/{rn}"

    # Fallback for classes not covered by _RN_TEMPLATES: best-effort
    # '{cls}-{name}' heuristic. This does NOT match real APIC's RN-prefix
    # scheme, so it may not round-trip with a canonical GET; kept only so
    # unknown/unlisted classes still get a stable, non-clobbering DN.
    name = (
        attrs.get("name")
        or attrs.get("ip")
        or attrs.get("tDn")
        or attrs.get("tnFvBDName")
        or str(index)
    )
    rn = f"[{name}]" if ("/" in name or "[" in name) else name
    return f"{parent_dn}/{cls}-{rn}"


def _validate_node(cls: str, body: dict, path: str) -> None:
    """Validate one {className: {"attributes": dict, "children"?: list}} node.

    Raises WriteValidationError with a descriptive message on any shape
    violation. Does not touch the store — pure validation.
    """
    if not isinstance(body, dict):
        raise WriteValidationError(f"{path}: expected an object body for class {cls!r}, got {type(body).__name__}")

    attributes = body.get("attributes", {})
    if not isinstance(attributes, dict):
        raise WriteValidationError(f"{path}: 'attributes' must be an object")

    children = body.get("children", [])
    if children is None:
        children = []
    if not isinstance(children, list):
        raise WriteValidationError(f"{path}: 'children' must be a list")

    for index, child_entry in enumerate(children):
        child_path = f"{path}/children[{index}]"
        if not isinstance(child_entry, dict):
            raise WriteValidationError(
                f"{child_path}: expected a single-key object mapping class name to body, "
                f"got {type(child_entry).__name__}"
            )
        if len(child_entry) != 1:
            raise WriteValidationError(
                f"{child_path}: expected exactly one class key, got {len(child_entry)}"
            )
        child_cls = next(iter(child_entry))
        child_body = child_entry[child_cls]
        _validate_node(child_cls, child_body, child_path)


def _plan_recursive(cls: str, attrs: dict, children: list, planned: list[tuple[str, dict]]) -> None:
    """Build the ordered list of (class, attrs) MOs to write, without touching the store.

    Appends to *planned* in the same order the old eager code used to upsert,
    so behavior (e.g. parent-before-child) is unchanged — only the timing of
    the actual store mutation moves to after this whole plan succeeds.
    """
    planned.append((cls, attrs))
    # A deleted MO removes its whole subtree; do NOT plan children as orphans.
    if attrs.get("status") == "deleted":
        return
    for index, child_entry in enumerate(children):
        for child_cls, child_body in child_entry.items():
            child_attrs = dict(child_body.get("attributes") or {})
            child_attrs["dn"] = _child_dn(attrs["dn"], child_cls, child_attrs, index)
            child_children = child_body.get("children") or []
            _plan_recursive(child_cls, child_attrs, child_children, planned)


def _upsert_recursive(store: MITStore, cls: str, attrs: dict, children: list) -> list[tuple[str, dict]]:
    """Validate the entire body shape, then upsert the MO and its children.

    Real APIC POST is all-or-nothing: a malformed descendant must not leave
    earlier siblings/ancestors partially written. So this first validates the
    complete subtree (raising WriteValidationError before touching the store
    on any shape violation), builds the full ordered list of MOs to write,
    and only then mutates the store.

    Returns the full ordered ``(class, attrs)`` plan so callers (``apply``)
    can inspect it for reactions (e.g. a nested ``fabricNodeIdentP`` child)
    without re-walking the body themselves.
    """
    # Re-wrap the already-parsed root attrs/children into the same node shape
    # _validate_node expects, so root and descendants share one validation
    # path (root attrs are pre-extracted by apply(), but must still satisfy
    # the same shape rules children do).
    _validate_node(cls, {"attributes": attrs, "children": children}, path=attrs.get("dn", cls))

    planned: list[tuple[str, dict]] = []
    _plan_recursive(cls, attrs, children, planned)

    # Validation passed for the entire subtree — now, and only now, mutate
    # the store (400 on validation failure => zero side effects).
    for mo_cls, mo_attrs in planned:
        # Real APIC commits a class's object defaults at CREATE time only —
        # a later partial-update POST never resets an already-set attribute
        # back to its default. So the overlay applies iff (a) this isn't a
        # delete (no defaults that could resurrect a deleted object's attrs)
        # and (b) the DN doesn't exist yet in the store. Posted attrs are
        # layered on top of the defaults dict, so they always win.
        dn = mo_attrs.get("dn")
        if mo_attrs.get("status") != "deleted" and store.get(dn) is None:
            mo_attrs = {**_CLASS_DEFAULTS.get(mo_cls, {}), **mo_attrs}
        store.upsert(MO(mo_cls, **mo_attrs))

    return planned


def materialize_node_registration(
    store: MITStore,
    *,
    topo,
    site,
    node_id: int,
    name: str,
    role: str = "leaf",
) -> None:
    """Reaction: materialize a full node-registration MO set for *node_id*.

    Mirrors what the real builders (build/fabric.py, build/cabling.py,
    build/interfaces.py, build/health_faults.py) emit for a leaf that was
    present at boot time, so a node registered dynamically via
    fabricNodeIdentP or /_sim/add-leaf renders identically to one seeded from
    topology.yaml:

      - fabricNode        (inventory row)
      - topSystem         (loopback/oob addresses, valid parseable IPv4 —
                            reuses build/fabric.py's loopback_ip/oob_ip so
                            the address scheme never drifts out of sync)
      - healthInst        (node health, matching health_faults.py's shape)
      - fabricLink (+ lldpAdjEp/cdpAdjEp) to EVERY spine in the site, mirroring
        build/cabling.py's leaf-uplink port assignment (eth1/{49+spine_idx})
      - l1PhysIf/ethpmPhysIf inventory (fabric uplinks + host-access ports),
        reusing build/interfaces.py's own `_add_port` helper so port shapes
        never diverge from the boot-time build path

    `pod` is derived from *site* (nodeidentpol DNs carry no pod of their
    own) rather than hardcoded, so multi-pod/multi-site sims register nodes
    under the correct pod.
    """
    pod = site.pod
    node_dn = f"topology/pod-{pod}/node-{node_id}"

    store.upsert(MO(
        "fabricNode",
        dn=node_dn,
        id=str(node_id),
        name=name,
        role=role,
        adSt="on",
        fabricSt="active",
        model="N9K-C9332C",
        serial="",
        version="n9000-14.2(7f)",
    ))

    store.upsert(MO(
        "topSystem",
        dn=f"{node_dn}/sys",
        id=str(node_id),
        name=name,
        role=role,
        version="n9000-14.2(7f)",
        address=loopback_ip(pod, node_id),
        oobMgmtAddr=oob_ip(pod, node_id),
        fabricDomain=site.fabric_name or (topo.fabric.name if topo else ""),
        state="in-service",
        podId=str(pod),
    ))

    store.upsert(MO(
        "healthInst",
        dn=f"{node_dn}/sys/health",
        cur="95",
        min="95",
        max="100",
        prev="95",
    ))

    # Cable this node to every spine in the site (mirrors build/cabling.py's
    # leaf-uplink port assignment: leaf uplink eth1/{49+spine_idx}, spine
    # downlink port picked as the next free slot after its existing leaves).
    spines = list(site.spine_nodes()) if site is not None else []
    leaf_uplinks: list[tuple[int, int]] = []
    for s_idx, spine in enumerate(spines):
        leaf_slot, leaf_port = 1, _UPLINK_START + s_idx
        # Spine-side port: next free downlink slot on that spine (after every
        # existing leaf/border-leaf this site already cabled at boot time).
        existing_downlinks = len(site.leaf_nodes()) + len(site.border_leaf_nodes())
        spine_slot, spine_port = 1, existing_downlinks + 1

        lnk_dn = (
            f"topology/pod-{pod}"
            f"/lnk-{spine.id}-{spine_slot}-{spine_port}-to-{node_id}-{leaf_slot}-{leaf_port}"
        )
        store.upsert(MO(
            "fabricLink",
            dn=lnk_dn,
            n1=str(spine.id),
            n2=str(node_id),
            operSt="up",
            operSpeed="100G",
            linkType="leaf",
        ))

        spine_port_str = f"eth{spine_slot}/{spine_port}"
        leaf_port_str = f"eth{leaf_slot}/{leaf_port}"
        spine_oob = oob_ip(pod, spine.id)
        leaf_oob = oob_ip(pod, node_id)
        # The dynamically-registered node has no Node schema instance of its
        # own (only the id/name/role args this function received) — build a
        # throwaway one so add_switch_adjacency can derive model/version/mac
        # from it exactly like the boot-time builders do. model/version
        # match the hardcoded fabricNode/topSystem values a few lines above.
        new_node = Node(id=node_id, name=name, model="N9K-C9332C", version="n9000-14.2(7f)")

        # Spine sees the new node as neighbor on spine_port_str
        add_switch_adjacency(
            store,
            pod=pod,
            local_node_id=spine.id,
            local_port=spine_port_str,
            remote_port=leaf_port_str,
            neighbor=new_node,
            neighbor_mgmt_ip=leaf_oob,
        )
        # The new node sees the spine as neighbor on leaf_port_str
        add_switch_adjacency(
            store,
            pod=pod,
            local_node_id=node_id,
            local_port=leaf_port_str,
            remote_port=spine_port_str,
            neighbor=spine,
            neighbor_mgmt_ip=spine_oob,
        )
        leaf_uplinks.append((leaf_slot, leaf_port))

    # l1PhysIf inventory: fabric uplinks (one per spine) + host-access ports,
    # reusing build/interfaces.py's own port-building helper so shapes never
    # diverge from the boot-time build path.
    for slot, port in leaf_uplinks:
        _add_port(
            store, pod, node_id, slot, port,
            descr="Fabric uplink to spine",
            mode="trunk",
            with_fcot=True,
        )
    for hp in range(1, _HOST_PORTS + 1):
        _add_port(
            store, pod, node_id, 1, hp,
            descr=f"Host port eth1/{hp}",
            mode="access",
            usage="access",
        )


_BGP_PEERP_RE = re.compile(
    r"uni/tn-(?P<tenant>[^/]+)/out-(?P<l3out>[^/]+)/.*"
    r"paths-(?P<node>\d+)/pathep-\[[^\]]+\]\]/peerP-\[(?P<peer>[^\]]+)\]$"
)


def _materialize_l3out_bgp_session(
    store: MITStore, peer_dn: str, addr: str, remote_asn: str
) -> None:
    """Reaction: a tenant L3Out ``bgpPeerP`` (config) was POSTed — materialize
    the matching operational session MOs (``bgpPeer``/``bgpPeerEntry``/
    ``bgpPeerAfEntry``) so autoACI's ``_compute_session_tables`` (routers/
    topology.py) L3Out BGP table finds it, exactly like the boot-time
    topology-driven path already does for its own L3Outs (build/l3out.py's
    ``_upsert_ebgp_sessions``) — this covers the OTHER path, a tenant config
    pushed live via POST /api/mo (cisco.aci ansible playbook / hidden_push.py),
    which lands generic MOs here in writes.py instead of going through the
    topology builders.

    DN/attribute derivation (verified against routers/topology.py's
    ``_compute_session_tables``, ``_dn_dom`` and ``_dn_node`` helpers):
      - node: parsed from the bgpPeerP's own DN (".../paths-{N}/pathep-...")
        — this IS the L3Out border-leaf, same node the peer is deployed on.
      - dom: MUST be ``dom-{tenant}:{vrf}`` (colon-separated) — topology.py's
        ``_dn_dom`` extracts the dom segment verbatim and then does
        ``if ":" not in dom: continue`` to skip fabric/overlay (``dom-
        overlay-1``) peers, then ``tenant, _, vrf = dom.partition(":")``.
        vrf is read from the L3Out's own ``l3extRsEctx.tnFvCtxName`` (already
        in the store from the earlier ``aci_l3out`` POST) rather than
        hardcoded, so it always matches whatever VRF the tenant push used.
      - remoteAsn: topology.py reads it off the sibling ``bgpPeer.asn``
        attribute (matched via the entry's parent dn) — sourced from the
        bgpPeerP's own nested ``bgpAsP.asn`` child, i.e. the L3Out's actual
        configured peer ASN, never hardcoded.
    """
    m = _BGP_PEERP_RE.match(peer_dn)
    if not m:
        return
    tenant = m.group("tenant")
    l3out_name = m.group("l3out")
    node_id = m.group("node")

    l3out_dn = f"uni/tn-{tenant}/out-{l3out_name}"
    vrf = ""
    for ectx in store.children(l3out_dn, {"l3extRsEctx"}):
        vrf = ectx.attrs.get("tnFvCtxName", "")
        if vrf:
            break
    if not vrf:
        # Can't place this peer under a tenant-VRF dom without a VRF name —
        # nothing sane to derive it from, so skip rather than fudge a dom
        # that would silently misclassify as fabric/overlay (no ":").
        return

    pod = pod_from_dn(peer_dn)
    inst_dn = f"topology/pod-{pod}/node-{node_id}/sys/bgp/inst"
    inst_mo = MO("bgpInst", dn=inst_dn)

    dom_name = f"{tenant}:{vrf}"
    dom_dn = f"{inst_dn}/dom-{dom_name}"
    dom_mo = MO("bgpDom", dn=dom_dn, name=dom_name)

    session_peer_dn = f"{dom_dn}/peer-[{addr}]"
    peer_mo = MO(
        "bgpPeer",
        dn=session_peer_dn,
        addr=addr,
        asn=remote_asn,
        type="ebgp",
        peerRole="ce",
    )
    entry_dn = f"{session_peer_dn}/ent-[{addr}]"
    peer_mo.add_child(MO(
        "bgpPeerEntry",
        dn=entry_dn,
        addr=addr,
        operSt="established",
        connEst="1",
        connDrop="0",
        type="ebgp",
    ))
    af_dn = f"{entry_dn}/af-[ipv4-ucast]"
    peer_mo.add_child(MO(
        "bgpPeerAfEntry",
        dn=af_dn,
        type="ipv4-ucast",
        afId="ipv4-ucast",
        acceptedPaths="3",
        pfxSent="3",
        tblVer="1",
    ))
    dom_mo.add_child(peer_mo)
    inst_mo.add_child(dom_mo)
    store.upsert(inst_mo)


def apply(store: MITStore, dn: str, body: dict, *, topo=None, site=None) -> tuple[list[dict], int]:
    """Apply a write (upsert or delete) from a POST /api/mo/{dn}.json body.

    Body shape: {"<cls>": {"attributes": {...}, "children": [...]}}
    Returns (imdata, total).

    *topo*/*site* are optional context needed by the fabricNodeIdentP
    reaction (pod number, spine list for cabling) — passed through by the
    caller the same way it already threads ``state.store`` here.
    """
    if not body:
        return [], 0

    # Extract class name and body parts
    cls = next(iter(body))
    cls_body = body[cls]
    attrs = dict(cls_body.get("attributes") or {})
    children = cls_body.get("children") or []

    # Honor attrs["dn"] if present, fall back to URL dn
    effective_dn = attrs.get("dn") or dn
    attrs["dn"] = effective_dn

    # Perform the upsert/delete; get back the full ordered (class, attrs)
    # plan so the fabricNodeIdentP reaction fires for a nested child too,
    # not just when it's the top-level POSTed class (finding #19).
    planned = _upsert_recursive(store, cls, attrs, children)

    if site is not None:
        for mo_cls, mo_attrs in planned:
            if mo_cls != "fabricNodeIdentP":
                continue
            if mo_attrs.get("status") == "deleted":
                continue
            node_dn = mo_attrs.get("dn", "")
            m = re.search(r"nodep-(\d+)$", node_dn)
            if not m:
                continue
            node_id = int(m.group(1))
            name = mo_attrs.get("name", f"leaf-{node_id}")
            role = mo_attrs.get("role", mo_attrs.get("nodeType", "leaf"))
            materialize_node_registration(
                store, topo=topo, site=site, node_id=node_id, name=name, role=role,
            )

    # Tenant L3Out eBGP peer (bgpPeerP) reaction: materialize the operational
    # bgpPeer/bgpPeerEntry/bgpPeerAfEntry session MOs so autoACI's L3Out BGP
    # table (routers/topology.py) finds it — see _materialize_l3out_bgp_session
    # docstring. bgpAsP (the peer ASN) is POSTed as bgpPeerP's own nested
    # child in the SAME body (cisco.aci's tenant.yml task), so it is already
    # in this same `planned` list — read it from there instead of a second
    # store lookup.
    for mo_cls, mo_attrs in planned:
        if mo_cls != "bgpPeerP":
            continue
        if mo_attrs.get("status") == "deleted":
            continue
        peer_dn = mo_attrs.get("dn", "")
        addr = mo_attrs.get("addr", "")
        if not peer_dn or not addr:
            continue
        remote_asn = ""
        for as_cls, as_attrs in planned:
            if as_cls == "bgpAsP" and as_attrs.get("dn", "").startswith(f"{peer_dn}/"):
                remote_asn = as_attrs.get("asn", "")
                break
        _materialize_l3out_bgp_session(store, peer_dn, addr, remote_asn)

    status = "deleted" if attrs.get("status") == "deleted" else "created"
    result = [{cls: {"attributes": {"dn": effective_dn, "status": status}}}]
    return result, 1
