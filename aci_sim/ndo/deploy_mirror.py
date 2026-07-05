"""NDO -> APIC deploy mirror.

Confirmed SIM GAP: `POST /mso/api/v1/task` (the deploy/undeploy request
`cisco.mso.ndo_schema_template_deploy` sends — see ndo/app.py's docstring on
that route) has always been a pure ack no-op: it never materializes the
deployed schema template's VRFs/BDs/ANPs/EPGs/binds into the TARGET SITES'
APIC MITStores. A real E2E Ansible run never direct-POSTs those MOs to the
APIC for a multi-site tenant either — it relies entirely on NDO's deploy to
push them down. So today, multi-site EPGs exist in the NDO schema doc but
never appear on the APIC side of this sim, which breaks any playbook/test
that reads EPGs back from the APIC after an NDO-driven multi-site deploy.

`mirror_template_to_sites()` closes that gap: given the NDO state, a
template name, and a site_id -> ApicSiteState map, it walks the owning
schema's template (vrfs/bds/anps/epgs) plus each associated SITE entry
(hostBasedRouting overlay, domainAssociations, staticPorts) and upserts the
equivalent fvCtx/fvBD/fvAp/fvAEPg (+ children) MOs into every target site's
MITStore — reusing the exact DN/attribute conventions build/tenants.py's
boot-time builders and rest_aci/writes.py's POST-write path already
establish, so a mirrored MO is indistinguishable from a boot-built or
directly-POSTed one.

Pure and HTTP-free: no FastAPI/Request dependency, so it's unit-testable
without spinning up either app.
"""
from __future__ import annotations

from typing import Any

from aci_sim.build.tenants import pctag_for
from aci_sim.mit.mo import MO
from aci_sim.mit.store import MITStore
from aci_sim.rest_aci.writes import _CLASS_DEFAULTS

from .model import NdoState


#: Leaf object-name keys inside a DICT-form NDO ref, in priority order. An
#: epgRef carries BOTH anpName and epgName, so epgName is checked first (the
#: EPG's own name wins); every other ref dict carries only its own *Name key.
#: schemaId/templateName are deliberately excluded.
_REF_NAME_KEYS = ("epgName", "bdName", "vrfName", "contractName", "anpName", "name")


def _basename(ref: Any) -> str:
    """Return the object name for an NDO `*Ref`, handling BOTH forms it uses.

    - STRING ref `/schemas/<id>/templates/<tmpl>/bds/<bdName>` -> last segment.
    - DICT ref `{"vrfName": ..., "schemaId": ..., "templateName": ...}` -> the
      leaf object-name key (see `_REF_NAME_KEYS`). aci-ansible's cisco.mso
      modules store the dict shape; aci-py stringifies its refs — so the mirror
      must accept either or it AttributeErrors (`'dict'.rsplit`) on a redeploy
      of an aci-ansible multi-site tenant.

    The last segment / leaf name already matches the APIC MO name (see
    ndo/model.py's docstring: NDO names are derived verbatim from the APIC
    fvBD/fvAp/fvAEPg names at build time).
    """
    if isinstance(ref, dict):
        for key in _REF_NAME_KEYS:
            value = ref.get(key)
            if isinstance(value, str) and value:
                return value
        return ""
    if not ref:
        return ""
    return ref.rsplit("/", 1)[-1]


def _tenant_name(state: NdoState, tenant_id: str) -> str:
    """Resolve a template's `tenantId` to the tenant's APIC-facing name."""
    for tenant in state.tenants:
        if tenant.get("id") == tenant_id:
            return tenant.get("name", "")
    return ""


def _find_template(
    state: NdoState, template_name: str, schema_id: str | None = None
) -> tuple[dict | None, dict | None]:
    """Locate (schema_detail, template_doc) for a template named *template_name*.

    If *schema_id* is given, resolve ONLY within that schema — template names
    are NOT unique across schemas (every multi-site tenant's schema commonly
    has templates literally named e.g. 'LAB1-LAB2'/'LAB1'/'LAB2'), so a
    cross-schema scan risks mirroring the WRONG tenant. When *schema_id* is
    None, fall back to the legacy scan-every-schema behavior (kept for
    existing callers/tests that don't have a schema id handy).
    """
    if schema_id is not None:
        sd = state.schema_details.get(schema_id)
        if sd is None:
            return None, None
        for tmpl in sd.get("templates", []):
            if tmpl.get("name") == template_name:
                return sd, tmpl
        return sd, None

    for schema_detail in state.schema_details.values():
        for tmpl in schema_detail.get("templates", []):
            if tmpl.get("name") == template_name:
                return schema_detail, tmpl
    return None, None


def _site_entries_for_template(schema_detail: dict, template_name: str) -> list[dict]:
    """Return every top-level `sites[]` entry belonging to *template_name*."""
    return [
        site
        for site in schema_detail.get("sites", [])
        if isinstance(site, dict) and site.get("templateName") == template_name
    ]


def _find_site_bd(site_entry: dict, bd_name: str) -> dict | None:
    for bd in site_entry.get("bds", []) or []:
        if isinstance(bd, dict) and _basename(bd.get("bdRef")) == bd_name:
            return bd
    return None


def _find_site_epg(site_entry: dict, anp_name: str, epg_name: str) -> dict | None:
    for anp in site_entry.get("anps", []) or []:
        if not isinstance(anp, dict) or _basename(anp.get("anpRef")) != anp_name:
            continue
        for epg in anp.get("epgs", []) or []:
            if isinstance(epg, dict) and _basename(epg.get("epgRef")) == epg_name:
                return epg
    return None


def _mirror_bd(store: MITStore, tenant: str, bd: dict, site_bd: dict | None) -> None:
    """Upsert fvCtx-referencing fvBD (+ fvRsCtx/fvSubnet children) for one
    template BD, overlaid with the SITE bd's hostBasedRouting."""
    bd_name = bd.get("name", "")
    vrf_name = _basename(bd.get("vrfRef"))
    bd_dn = f"uni/tn-{tenant}/BD-{bd_name}"

    l2_unknown_ucast = bd.get("l2UnknownUnicast", "flood")
    unicast_routing = bool(bd.get("unicastRouting", True))
    host_based_routing = bool(site_bd.get("hostBasedRouting")) if site_bd else False

    attrs: dict[str, Any] = dict(_CLASS_DEFAULTS.get("fvBD", {}))
    attrs.update(
        {
            "dn": bd_dn,
            "name": bd_name,
            "epMoveDetectMode": bd.get("epMoveDetectMode", ""),
            "arpFlood": "yes" if bd.get("arpFlood") else "no",
            "unkMacUcastAct": l2_unknown_ucast,
            "unicastRoute": "yes" if unicast_routing else "no",
            "hostBasedRouting": "yes" if host_based_routing else "no",
            "intersiteBumTrafficAllow": "yes" if bd.get("intersiteBumTrafficAllow") else "no",
        }
    )
    bd_mo = MO("fvBD", **attrs)
    if vrf_name:
        bd_mo.add_child(MO(
            "fvRsCtx",
            dn=f"{bd_dn}/rsctx",
            tnFvCtxName=vrf_name,
        ))
    for subnet in bd.get("subnets", []) or []:
        if not isinstance(subnet, dict):
            continue
        ip = subnet.get("ip")
        if not ip:
            continue
        scope = subnet.get("scope") or "public,shared"
        bd_mo.add_child(MO(
            "fvSubnet",
            dn=f"{bd_dn}/subnet-[{ip}]",
            ip=ip,
            scope=scope,
            preferred="yes" if subnet.get("primary") else "no",
        ))
    # Merge-upsert (NOT delete-then-recreate) on purpose: the BD may carry an
    # externally-POSTed `epClear` attr (the clear_remote_mac stub a playbook
    # posts directly to the APIC) that a delete-then-recreate would drop.
    # store.upsert() merges attrs/children onto any existing MO at this dn,
    # so that attr survives a redeploy. Contrast with _mirror_epg below,
    # which deletes-then-recreates because EPGs have no such externally-set
    # state to preserve.
    store.upsert(bd_mo)


def _mirror_epg(store: MITStore, tenant: str, anp_name: str, epg: dict, site_epg: dict | None) -> None:
    """Upsert fvAEPg (+ fvRsBd/fvRsProv/fvRsCons/fvSubnet/fvRsDomAtt/
    fvRsPathAtt children) for one template EPG, overlaid with the SITE epg's
    domainAssociations/staticPorts (bind data — usually empty until a bind
    playbook runs)."""
    epg_name = epg.get("name", "")
    epg_dn = f"uni/tn-{tenant}/ap-{anp_name}/epg-{epg_name}"

    epg_mo = MO(
        "fvAEPg",
        dn=epg_dn,
        name=epg_name,
        prefGrMemb="include" if epg.get("preferredGroup") else "exclude",
        pcTag=pctag_for(epg_dn),
        pcEnfPref="unenforced",
        isAttrBasedEPg="no",
        floodOnEncap="disabled",
    )
    bd_name = _basename(epg.get("bdRef"))
    if bd_name:
        epg_mo.add_child(MO(
            "fvRsBd",
            dn=f"{epg_dn}/rsbd",
            tnFvBDName=bd_name,
        ))

    for rel in epg.get("contractRelationships", []) or []:
        if not isinstance(rel, dict):
            continue
        contract = _basename(rel.get("contractRef"))
        if not contract:
            continue
        if rel.get("relationshipType") == "provider":
            epg_mo.add_child(MO(
                "fvRsProv",
                dn=f"{epg_dn}/rsprov-{contract}",
                tnVzBrCPName=contract,
            ))
        elif rel.get("relationshipType") == "consumer":
            epg_mo.add_child(MO(
                "fvRsCons",
                dn=f"{epg_dn}/rscons-{contract}",
                tnVzBrCPName=contract,
            ))

    for subnet in epg.get("subnets", []) or []:
        if not isinstance(subnet, dict):
            continue
        ip = subnet.get("ip")
        if not ip:
            continue
        epg_mo.add_child(MO(
            "fvSubnet",
            dn=f"{epg_dn}/subnet-[{ip}]",
            ip=ip,
            scope=subnet.get("scope") or "public,shared",
            preferred="yes" if subnet.get("primary") else "no",
        ))

    if site_epg is not None:
        for dom in site_epg.get("domainAssociations", []) or []:
            if not isinstance(dom, dict):
                continue
            t_dn = dom.get("dn") or dom.get("domainRef") or dom.get("tDn")
            if not t_dn:
                continue
            epg_mo.add_child(MO(
                "fvRsDomAtt",
                dn=f"{epg_dn}/rsdomAtt-[{t_dn}]",
                tDn=t_dn,
                instrImedcy="lazy",
            ))
        for path in site_epg.get("staticPorts", []) or []:
            if not isinstance(path, dict):
                continue
            path_dn = path.get("path") or path.get("dn") or path.get("tDn")
            if not path_dn:
                continue
            epg_mo.add_child(MO(
                "fvRsPathAtt",
                dn=f"{epg_dn}/rspathAtt-[{path_dn}]",
                tDn=path_dn,
                encap=path.get("encap", ""),
                mode=path.get("mode", "regular"),
                instrImedcy=path.get("deploymentImmediacy", "lazy"),
            ))

    # Redeploy idempotency: delete-then-recreate (NOT a merge-upsert like
    # _mirror_bd). store.upsert() merges attrs/children onto an existing MO,
    # so a contract/bind removed in NDO since the last deploy would otherwise
    # leave a stale fvRsProv/fvRsDomAtt/etc. child behind forever. This is
    # safe because multi-site EPGs have NO externally-posted children of
    # their own — every child under this dn comes from this mirror, so
    # wiping the subtree first and rebuilding it clean can't drop anything
    # an outside POST relied on (contrast with the BD's epClear, above).
    store.upsert(MO("fvAEPg", dn=epg_dn, status="deleted"))
    store.upsert(epg_mo)


def _template_object_dns(tenant: str, template: dict) -> list[tuple[str, str]]:
    """Every tenant-scoped (dn, class_name) pair this template owns (its
    VRFs/BDs/ANPs/EPGs) — used to scope an undeploy's delete to just this
    template's objects, never the whole `uni/tn-<T>` tenant (a schema may
    hold more than one template against the same tenant).

    Deletion in the store is DN-based (status="deleted" removes by dn
    regardless of the class passed), so the class here only matters for
    fidelity — the emitted delete MO now carries the object's real class
    instead of a hardcoded 'fvBD' for everything.
    """
    dns: list[tuple[str, str]] = []
    for vrf in template.get("vrfs", []) or []:
        name = vrf.get("name") if isinstance(vrf, dict) else None
        if name:
            dns.append((f"uni/tn-{tenant}/ctx-{name}", "fvCtx"))
    for bd in template.get("bds", []) or []:
        name = bd.get("name") if isinstance(bd, dict) else None
        if name:
            dns.append((f"uni/tn-{tenant}/BD-{name}", "fvBD"))
    for anp in template.get("anps", []) or []:
        if not isinstance(anp, dict):
            continue
        anp_name = anp.get("name")
        if not anp_name:
            continue
        dns.append((f"uni/tn-{tenant}/ap-{anp_name}", "fvAp"))
        for epg in anp.get("epgs", []) or []:
            epg_name = epg.get("name") if isinstance(epg, dict) else None
            if epg_name:
                dns.append((f"uni/tn-{tenant}/ap-{anp_name}/epg-{epg_name}", "fvAEPg"))
    return dns


def mirror_template_to_sites(
    state: NdoState,
    template_name: str,
    apic_states: dict,
    *,
    schema_id: str | None = None,
    undeploy: bool = False,
) -> int:
    """Materialize (or, if *undeploy*, tear down) *template_name*'s
    VRFs/BDs/ANPs/EPGs into every associated site's APIC MITStore.

    *schema_id*, when known (the real `POST /mso/api/v1/task` body always
    carries one), scopes template resolution to that ONE schema — template
    names collide across schemas (e.g. every multi-site tenant's schema has
    templates literally named 'LAB1-LAB2'/'LAB1'/'LAB2'), so resolving by
    name alone across ALL schemas risks mirroring the WRONG tenant. Pass
    None only for legacy callers/tests that don't have a schema id handy.

    *apic_states* maps site_id -> an object exposing a `.store` (MITStore) —
    typically `aci_sim.rest_aci.app.ApicSiteState`. Only sites that
    are BOTH associated with this template (schema `sites[].templateName ==
    template_name`) AND present in *apic_states* are touched; every other
    site's store is left completely untouched.

    Returns the number of MOs upserted/deleted (0 if the template or none of
    its sites could be resolved — a safe no-op).
    """
    schema_detail, template = _find_template(state, template_name, schema_id)
    if template is None or schema_detail is None:
        return 0

    tenant = _tenant_name(state, template.get("tenantId", ""))
    if not tenant:
        return 0

    site_entries = _site_entries_for_template(schema_detail, template_name)
    if not site_entries:
        return 0

    written = 0

    if undeploy:
        object_dns = _template_object_dns(tenant, template)
        for site_entry in site_entries:
            site_id = site_entry.get("siteId")
            apic_state = apic_states.get(str(site_id)) if apic_states else None
            if apic_state is None:
                continue
            store = apic_state.store
            for dn, cls in object_dns:
                store.upsert(MO(cls, dn=dn, status="deleted"))
                written += 1
        return written

    for site_entry in site_entries:
        site_id = site_entry.get("siteId")
        apic_state = apic_states.get(str(site_id)) if apic_states else None
        if apic_state is None:
            continue
        store = apic_state.store

        # Materialize the tenant shadow on this site FIRST. An NDO deploy
        # creates the tenant on each target site's APIC; without the fvTenant
        # root MO, a subtree/mo query on `uni/tn-<T>` returns empty even though
        # the mirrored children exist (real F1 root cause: the direct-POST path
        # only creates fvTenant on the --apic site, so the OTHER site had the
        # mirrored VRF/BD/EPG children but no tenant root to query them under).
        store.upsert(MO("fvTenant", dn=f"uni/tn-{tenant}", name=tenant))
        written += 1

        for vrf in template.get("vrfs", []) or []:
            if not isinstance(vrf, dict):
                continue
            vrf_name = vrf.get("name", "")
            if not vrf_name:
                continue
            store.upsert(MO(
                "fvCtx",
                dn=f"uni/tn-{tenant}/ctx-{vrf_name}",
                name=vrf_name,
            ))
            written += 1

        for bd in template.get("bds", []) or []:
            if not isinstance(bd, dict):
                continue
            bd_name = bd.get("name", "")
            if not bd_name:
                continue
            site_bd = _find_site_bd(site_entry, bd_name)
            _mirror_bd(store, tenant, bd, site_bd)
            written += 1

        for anp in template.get("anps", []) or []:
            if not isinstance(anp, dict):
                continue
            anp_name = anp.get("name", "")
            if not anp_name:
                continue
            store.upsert(MO(
                "fvAp",
                dn=f"uni/tn-{tenant}/ap-{anp_name}",
                name=anp_name,
            ))
            written += 1
            for epg in anp.get("epgs", []) or []:
                if not isinstance(epg, dict):
                    continue
                epg_name = epg.get("name", "")
                if not epg_name:
                    continue
                site_epg = _find_site_epg(site_entry, anp_name, epg_name)
                _mirror_epg(store, tenant, anp_name, epg, site_epg)
                written += 1

    return written
