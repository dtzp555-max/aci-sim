"""
NDO state model builder — Phase 6.

`build_ndo_model(topo)` derives a complete NdoState from the same Topology
object used for the APIC MIT builders, so every NDO tenant/VRF/BD/EPG/contract
name *exactly* matches the APIC MIT (cross-plane consistency, DESIGN.md rule).

The state is a plain dataclass — no FastAPI dependency here.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

from aci_sim.topology.schema import Topology

from .patch import _new_site_bd, _new_site_epg, normalize_site, normalize_template

# ---------------------------------------------------------------------------
# Fixed deterministic artefacts (stable across test runs — no datetime.now())
# ---------------------------------------------------------------------------

#: UUID used as the dhcpRelayPolicy reference in every BD's dhcpLabels.
#: The tenantPolicy template exposes this UUID so ndo_bd_view can resolve it.
DHCP_RELAY_UUID: str = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

#: UUID for the DHCP option policy exposed in the tenantPolicy template.
DHCP_OPTION_UUID: str = "f9e8d7c6-b5a4-3210-fedc-ba9876543210"

#: Stable template-id for the single tenantPolicy template.
TENANT_POLICY_TEMPLATE_ID: str = "tenantpolicy000000000001"  # 24 chars


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hex_id(seed: str) -> str:
    """Return a 24-char lower-hex deterministic id (Mongo ObjectId style)."""
    return hashlib.sha256(seed.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# State container
# ---------------------------------------------------------------------------


@dataclass
class NdoState:
    """All NDO-plane state derived from the topology, plus a mutable POST bucket."""

    #: [{id, name, platform, urls}]  — from topo.sites
    sites: list

    #: [{id, name, displayName, siteAssociations}]  — one per topo.tenants
    tenants: list

    #: Summary shape [{id, displayName, name, templates:[{name}]}]
    schemas: list

    #: schema_id → full schema detail dict (returned by GET /schemas/{id})
    schema_details: dict

    #: [{templateId, templateType}]  — includes one "tenantPolicy" entry
    template_summaries: list

    #: templateId → full template doc  (returned by GET /templates/{id})
    tenant_policy_templates: dict

    #: {"sites": [{id, siteId, status, connectivityStatus}]}
    fabric_connectivity: dict

    #: schema_id → [{status, drift}]
    policy_states: dict

    #: [{id, timestamp, user, action, description, details}]  — fixed set
    audit_records: list

    #: ND-platform local users, ND-shaped `{"items":[{"spec":{...}}]}` — PR-11.
    #: `cisco.mso.mso_tenant`'s `lookup_users()`/`lookup_remote_users()` query
    #: this (and the sibling `remote_users`) as part of every tenant create.
    local_users: dict = field(default_factory=dict)

    #: ND-platform remote (AAA/TACACS/LDAP) users, same ND aggregate shape.
    #: Seeded with the sandbox's own `admin` login so `mso_tenant`'s implicit
    #: `users: [ansible_user]` (defaulted to admin if unset) resolves.
    remote_users: dict = field(default_factory=dict)

    #: Accumulates schemas POSTed at runtime (mutable — shared with app closures)
    extra_schemas: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_ndo_model(topo: Topology, sandbox: bool = False) -> NdoState:  # noqa: C901
    """Derive an NdoState from *topo* with names matching the APIC MIT.

    In sandbox mode each APIC is served on its own ``mgmt_ip`` at :443, so NDO
    advertises the portless ``https://<mgmt_ip>`` URL — exactly how real NDO
    reports controller URLs, and what autoACI's discovery expects. Otherwise it
    advertises ``https://<apic_host>`` (the 127.0.0.1:<port> loopback form).
    """

    # ------------------------------------------------------------------
    # Sites
    # ------------------------------------------------------------------
    # PR-10: real NDO registers sites under their fabric name (e.g.
    # "LAB1-IT-ACI"), not the short topology-internal site name ("LAB1") —
    # verified against a real-gear cisco.mso.mso_tenant run (see
    # docs/CONTRACT.md §7 / CHANGELOG 0.2.1): the tenant's `sites` list only
    # accepted the fabric-name form, and `mso_tenant` failed with
    # "Site 'LAB1-IT-ACI' is not a valid site name" when only the short name
    # was exposed here. `Site.fabric_name` (topSystem.fabricDomain) already
    # carries this value; fall back to the short `site.name` only if a
    # topology omits fabric_name (keeps this builder tolerant of minimal
    # topologies used by unit tests that don't set it).
    sites: list = []
    site_id_map: dict[str, str] = {}  # site.name (short) → site.id — internal join key
    for site in topo.sites:
        apic_url = f"https://{site.mgmt_ip}" if (sandbox and site.mgmt_ip) else f"https://{site.apic_host}"
        ndo_site_name = site.fabric_name or site.name
        sites.append(
            {
                "id": site.id,
                "name": ndo_site_name,
                "platform": "APIC",
                "urls": [apic_url],
            }
        )
        site_id_map[site.name] = site.id

    # ------------------------------------------------------------------
    # Tenants
    # ------------------------------------------------------------------
    tenants: list = []
    for tenant in topo.tenants:
        t_id = _hex_id(f"tenant-{tenant.name}")
        site_assocs = [
            {"siteId": site_id_map[s]}
            for s in tenant.sites
            if s in site_id_map
        ]
        tenants.append(
            {
                "id": t_id,
                "name": tenant.name,
                "displayName": tenant.name,
                "siteAssociations": site_assocs,
            }
        )

    # ------------------------------------------------------------------
    # Schemas — one per tenant
    # ------------------------------------------------------------------
    schemas: list = []
    schema_details: dict = {}
    policy_states: dict = {}

    for tenant in topo.tenants:
        schema_id = _hex_id(f"schema-{tenant.name}")
        tmpl_type = "stretched" if tenant.stretch else "application"
        tmpl_name = f"{tenant.name}-template"

        # VRFs — name/displayName/vrfRef match APIC fvCtx names
        vrfs = [
            {
                "name": vrf.name,
                "displayName": vrf.name,
                "vrfRef": f"/vrfs/{vrf.name}",
                "vzAnyEnabled": False,
                "ipDataPlaneLearning": "enabled",
            }
            for vrf in tenant.vrfs
        ]

        # BDs — name/vrfRef match APIC fvBD names; every BD carries a DHCP
        # relay label so ndo_bd_view can exercise uuid→name resolution.
        bds = []
        for bd in tenant.bds:
            bds.append(
                {
                    "name": bd.name,
                    "vrfRef": f"/vrfs/{bd.vrf}",
                    "subnets": [{"ip": s} for s in bd.subnets],
                    "l2Stretch": bd.l2stretch,
                    "intersiteBumTraffic": bd.l2stretch,
                    "l2UnknownUnicast": "flood",
                    "arpFlood": True,
                    "unicastRouting": True,
                    "epMoveDetectMode": "garp",
                    "dhcpLabels": [{"ref": DHCP_RELAY_UUID}],
                }
            )

        # ANPs / EPGs — names match APIC fvAp / fvAEPg names
        anps = []
        for ap in tenant.aps:
            epgs = []
            for epg in ap.epgs:
                contract_rels = [
                    {
                        "relationshipType": "provider",
                        "contractRef": f"/contracts/{c}",
                    }
                    for c in epg.contracts.provide
                ] + [
                    {
                        "relationshipType": "consumer",
                        "contractRef": f"/contracts/{c}",
                    }
                    for c in epg.contracts.consume
                ]
                epgs.append(
                    {
                        "name": epg.name,
                        "bdRef": f"/bds/{epg.bd}",
                        "preferredGroup": False,
                        "proxyArp": False,
                        "uSegEpg": False,
                        "intraEpg": "unenforced",
                        "contractRelationships": contract_rels,
                    }
                )
            anps.append({"name": ap.name, "epgs": epgs})

        # Contracts — names match APIC vzBrCP names
        contracts = []
        for contract in tenant.contracts:
            filter_rels = [
                {"filterRef": f"/filters/{flt.name}"}
                for flt in contract.filters
            ]
            contracts.append(
                {
                    "name": contract.name,
                    "scope": contract.scope,
                    "filterRelationships": filter_rels,
                }
            )

        # Filters — deduplicated; entries use CONTRACT §7 field names
        # (ipProtocol / dFromPort / dToPort / etherType)
        seen_filters: set[str] = set()
        filters: list = []
        for contract in tenant.contracts:
            for flt in contract.filters:
                if flt.name in seen_filters:
                    continue
                seen_filters.add(flt.name)
                entries = [
                    {
                        "name": f"{e.proto}-{e.from_port}",
                        "ipProtocol": e.proto,
                        "dFromPort": e.from_port,
                        "dToPort": e.to_port,
                        "etherType": e.ether_type,
                    }
                    for e in flt.entries
                ]
                filters.append({"name": flt.name, "entries": entries})

        # External EPGs from L3Out ext_epgs — vrfRef / l3outRef match APIC names
        external_epgs: list = []
        for l3out in tenant.l3outs:
            for ext_epg in l3out.ext_epgs:
                external_epgs.append(
                    {
                        "name": ext_epg.name,
                        "vrfRef": f"/vrfs/{l3out.vrf}",
                        "l3outRef": f"/l3outs/{l3out.name}",
                        "type": "on-premise",
                        "subnets": [{"ip": s} for s in ext_epg.subnets],
                    }
                )

        # Schema-level site associations (template ↔ site mappings)
        schema_sites = [
            {"templateName": tmpl_name, "siteId": site_id_map[s]}
            for s in tenant.sites
            if s in site_id_map
        ]

        template = {
            "name": tmpl_name,
            "templateType": tmpl_type,
            # PR-13: `custom_mso_schema_service_graph.py`'s "service graph
            # already exists, add a node to it" branch (invoked the SECOND
            # time the module PATCHes the same service-graph name — e.g.
            # once per site in a multi-site service-graph bind) reads
            # `schema_obj.get('templates')[template_idx]['tenantId']` with
            # a bare subscript to resolve the L4-L7 device's owning tenant.
            # Same deterministic id `build_ndo_model`'s tenants[] list
            # already assigns this tenant (`_hex_id(f"tenant-{name}")`) —
            # recomputed here rather than threaded through because it's
            # derived purely from the tenant name.
            "tenantId": _hex_id(f"tenant-{tenant.name}"),
            "vrfs": vrfs,
            "bds": bds,
            "anps": anps,
            "contracts": contracts,
            "filters": filters,
            "externalEpgs": external_epgs,
        }

        # PR-12: backfill self-referencing anpRef/epgRef on every template
        # anp/epg (real NDO carries these — see aci_sim/ndo/patch.py
        # normalize_template's docstring), then mirror each template ANP/EPG
        # into every site already associated with this template, matching
        # real NDO 4.x's auto-population of sites[].anps[].epgs[] the moment
        # a template with sites attached gains an ANP/EPG — otherwise a
        # topology.yaml tenant that ships with ANPs/EPGs already configured
        # would boot into the same `set_site_anp()`-returns-None crash this
        # PR fixes for the PATCH-built case.
        normalize_template(template, schema_id)
        for site in schema_sites:
            if site.get("templateName") != tmpl_name:
                continue
            # PR-15: mirror each template BD into every site already
            # associated with this template too — same rationale as the
            # ANP/EPG mirroring below (and `_mirror_template_bd_to_sites` in
            # patch.py, which does the equivalent for a runtime PATCH `add`):
            # real NDO 4.x auto-creates the site BDDelta shadow the moment a
            # template BD is added to a template with sites attached, and
            # `mso_schema_site_bd.py` always PATCHes that shadow with `op:
            # replace` (never `add`) — a topology.yaml tenant that boots with
            # BDs already configured must start with the shadow already
            # present, or the FIRST site-BD PATCH 400s with "'bd-X' not found
            # at '/sites/{siteId}-{t}/bds/bd-X'" (confirmed on a real hardware
            # aci-py create_tenant/create_bd run against a freshly-booted sim).
            site.setdefault("bds", [])
            for bd in template["bds"]:
                bd_ref = "/schemas/{}/templates/{}/bds/{}".format(schema_id, tmpl_name, bd["name"])
                site["bds"].append(_new_site_bd(bd_ref))
            site.setdefault("anps", [])
            for anp in template["anps"]:
                site["anps"].append(
                    {"anpRef": anp["anpRef"], "epgs": [_new_site_epg(epg["epgRef"]) for epg in anp["epgs"]]}
                )
            # PR-13: mirror each template contract into every site already
            # associated with this template too — same rationale as the
            # ANP/EPG mirroring above, for the site-local `contracts[]`
            # array a raw mso_rest-driven PATCH (e.g. the mso-model role's
            # per-fabric service-graph redirect bind) addresses by bare
            # contract name (`/sites/{siteId}-{t}/contracts/{c}/
            # serviceGraphRelationship`).
            site.setdefault("contracts", [])
            for contract in template["contracts"]:
                contract_ref = "/schemas/{}/templates/{}/contracts/{}".format(schema_id, tmpl_name, contract["name"])
                site["contracts"].append({"contractRef": contract_ref})
            normalize_site(site)

        schema_name = f"{tenant.name}-schema"
        schemas.append(
            {
                "id": schema_id,
                "displayName": schema_name,
                "name": schema_name,
                # Summary shape: only template names (no inner lists)
                "templates": [{"name": tmpl_name}],
            }
        )
        schema_details[schema_id] = {
            "id": schema_id,
            "displayName": schema_name,
            "name": schema_name,
            "templates": [template],
            "sites": schema_sites,
        }
        policy_states[schema_id] = [{"status": "synced", "drift": False}]

    # ------------------------------------------------------------------
    # Template summaries
    # ------------------------------------------------------------------
    # One tenantPolicy template (carries DHCP policies), plus one summary
    # entry per schema template so ndo_deployment_status / stretch-analysis
    # can enumerate them.
    #
    # PR-13: `MSOTemplate.__init__`'s name-based lookup
    # (`ansible-mso`'s `plugins/module_utils/template.py`) filters
    # `templates/summaries` entries by `templateName`+`templateType` (and
    # tolerates absent `schemaName`/`schemaId` kwargs — `query_objs()` skips
    # `None`-valued filter kwargs). Every summary entry must therefore carry
    # a `templateName` field, not just `templateId`/`templateType`, or a
    # by-name lookup (`ndo_template`/`ndo_dhcp_relay_policy`'s `template:
    # "TenantPol-MS-TN1"` param) never matches anything.
    template_summaries: list = [
        {
            "templateId": TENANT_POLICY_TEMPLATE_ID,
            "templateName": "tenantpolicy-default",
            "templateType": "tenantPolicy",
        }
    ]
    for tenant in topo.tenants:
        s_id = _hex_id(f"schema-{tenant.name}")
        tmpl_type = "stretched" if tenant.stretch else "application"
        template_summaries.append(
            {
                "templateId": f"{s_id}-{tenant.name}",
                "templateName": f"{tenant.name}-template",
                "templateType": tmpl_type,
            }
        )

    # ------------------------------------------------------------------
    # Tenant policy template detail (DHCP relay + option policies)
    # ------------------------------------------------------------------
    # PR-13: keyed by templateId, one entry per `GET /templates/{id}` a
    # caller might ask for. Every entry mirrors real NDO's generic template
    # envelope shape: `{templateId, displayName, templateType,
    # tenantPolicyTemplate:{template:{...}, sites:[{siteId}]}}` — the
    # `sites` array (not just the inner `template` dict) is required because
    # `ndo_template`'s "template already exists" PATCH branch
    # (`append_site_config_to_ops`) reads
    # `config.get(template_type_container, {}).get("sites", [])` directly.
    tenant_policy_templates: dict = {
        TENANT_POLICY_TEMPLATE_ID: {
            "templateId": TENANT_POLICY_TEMPLATE_ID,
            "displayName": "tenantpolicy-default",
            "templateType": "tenantPolicy",
            "tenantPolicyTemplate": {
                "template": {
                    "dhcpRelayPolicies": [
                        {"uuid": DHCP_RELAY_UUID, "name": "dhcp-relay-1"},
                    ],
                    "dhcpOptionPolicies": [
                        {"uuid": DHCP_OPTION_UUID, "name": "dhcp-option-1"},
                    ],
                },
                "sites": [],
            },
        }
    }

    # ------------------------------------------------------------------
    # Fabric connectivity
    # ------------------------------------------------------------------
    fabric_connectivity: dict = {
        "sites": [
            {
                "id": site.id,
                "siteId": site.id,
                "status": "up",
                "connectivityStatus": "up",
            }
            for site in topo.sites
        ]
    }

    # ------------------------------------------------------------------
    # Audit records (fixed timestamps — no Date.now() / datetime.now())
    # ------------------------------------------------------------------
    audit_records: list = [
        {
            "id": "audit-001",
            "timestamp": "2026-06-01T10:00:00Z",
            "user": "admin",
            "action": "create",
            "description": "Schema Corp-schema created",
            "details": "Initial schema creation via NDO",
        },
        {
            "id": "audit-002",
            "timestamp": "2026-06-01T10:05:00Z",
            "user": "admin",
            "action": "deploy",
            "description": "Template Corp-template deployed to SiteA, SiteB",
            "details": "Full deployment: 1 VRF, 2 BDs, 2 EPGs, 1 contract",
        },
        {
            "id": "audit-003",
            "timestamp": "2026-06-02T09:00:00Z",
            "user": "operator",
            "action": "update",
            "description": "BD app-bd updated",
            "details": "Changed ARP flood setting to enabled",
        },
    ]

    # ------------------------------------------------------------------
    # ND local/remote users — PR-11
    # ------------------------------------------------------------------
    # `cisco.mso.mso_tenant`'s state=present flow always resolves the tenant's
    # `users` list (defaulted to ["admin"] when unset) via `lookup_users()`,
    # which on ND platforms queries `/nexus/infra/api/aaa/v4/localusers` +
    # `/remoteusers` first, falling back to the legacy `/api/config/class/*`
    # form when both come back empty (see ansible-mso module_utils/mso.py).
    # The sim only serves the legacy `/api/config/class/remoteusers` route
    # (see ndo/app.py), so seed a single "admin" local user in ND's
    # `{"items":[{"spec":{...}}]}` aggregate shape — `get_user_from_list_of_
    # users()` reads `item["spec"]["loginID"]` and needs `id`/`userID` on
    # that same spec dict to build the `userAssociations` id list.
    local_users: dict = {
        "items": [
            {
                "spec": {
                    "loginID": "admin",
                    "loginDomain": None,
                    "id": "admin-local-0000000000000001",
                    "userID": "admin-local-0000000000000001",
                }
            }
        ]
    }
    # Remote (AAA/TACACS/LDAP) users start empty: `nd_request()` returns the
    # raw JSON body untouched (see module_utils/mso.py `nd_request()`), and
    # `get_user_from_list_of_users()` only iterates `body["items"]` when body
    # is a dict — so any 200 response with an `"items"` key (even empty)
    # short-circuits the "ND Error: Unknown error" 404 failure the real
    # playbook run hit. No remote users are configured in the tenants under
    # test, only the local "admin" ships above.
    remote_users: dict = {"items": []}

    return NdoState(
        sites=sites,
        tenants=tenants,
        schemas=schemas,
        schema_details=schema_details,
        template_summaries=template_summaries,
        tenant_policy_templates=tenant_policy_templates,
        fabric_connectivity=fabric_connectivity,
        policy_states=policy_states,
        audit_records=audit_records,
        local_users=local_users,
        remote_users=remote_users,
    )
