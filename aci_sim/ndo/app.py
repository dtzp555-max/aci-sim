"""
NDO FastAPI application — Phase 6.

`make_ndo_app(state)` returns a FastAPI app that implements every §7 endpoint
from CONTRACT.md.  Bearer auth is lenient: tokens are issued on login but
never validated on subsequent requests (accept present-or-absent).
"""
from __future__ import annotations

import copy
import hashlib
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from aci_sim.control.persist import (
    apply_ndo,
    compatibility,
    load_json,
    save_json,
    serialize_ndo,
    state_dir,
    unwrap,
    wrap,
)

from .deploy_mirror import mirror_template_to_sites
from .model import NdoState
from .patch import PatchError, apply_json_patch, normalize_site, normalize_template
from .service_graph_validation import (
    ServiceGraphValidationError,
    _validate_service_graph_device_refs,
    _validate_uniform_site_redirect,
    service_graph_relevant,
)


def make_ndo_app(state: NdoState, apic_states: dict[str, Any] | None = None) -> FastAPI:
    """Build and return the NDO FastAPI application backed by *state*.

    *apic_states* (optional — defaults to ``None``, keeping every existing
    single-arg call site valid) maps site_id -> ``ApicSiteState``. When
    provided, ``POST /mso/api/v1/task`` (deploy/undeploy) mirrors the
    deployed template's VRFs/BDs/ANPs/EPGs into the target sites' APIC
    MITStores via ``deploy_mirror.mirror_template_to_sites`` — see that
    module's docstring for the SIM GAP this closes. Typed as a plain
    ``dict[str, Any]`` (not ``dict[str, ApicSiteState]``) to avoid importing
    ``aci_sim.rest_aci.app`` here, which would risk a circular import
    (that module doesn't currently import ``ndo.app``, but the two live in
    sibling packages wired together only by ``runtime/supervisor.py`` — this
    keeps ``ndo/app.py`` decoupled from the APIC app module's import graph).
    """

    app = FastAPI(title="aci-sim NDO", version="4.2.2")

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @app.post("/api/v1/auth/login")
    async def login(request: Request):
        """Accept any credentials and return a bearer token."""
        body = await request.json()
        token = f"sim-ndo-{uuid.uuid4().hex[:16]}"
        return {
            "token": token,
            "userName": body.get("userName", "admin"),
            "domain": body.get("domain", "local"),
        }

    @app.post("/login")
    async def nd_bare_login(request: Request):
        """Classic Nexus Dashboard bare-login endpoint.

        Real ND serves ``POST /login`` (body ``{userName, userPasswd, domain}``)
        alongside the newer ``/api/v1/auth/login``. Clients that use the ND
        platform login directly (aci-py's NDO connector, cisco.nd httpapi's
        session bookkeeping) POST here and read the JWT from ``token`` /
        ``jwttoken``.
        """
        body = await request.json()
        token = f"sim-ndo-{uuid.uuid4().hex[:16]}"
        return {
            "token": token,
            "jwttoken": token,
            "userName": body.get("userName", "admin"),
            "domain": body.get("domain", "local"),
        }

    @app.post("/logout")
    async def nd_bare_logout():
        """Classic ND bare-logout — mirrors POST /login."""
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Platform version
    # ------------------------------------------------------------------

    @app.get("/api/v1/platform/version")
    async def platform_version():
        # Dotted form per CONTRACT.md §7 — matches real Nexus Dashboard;
        # autoACI's ndo_connector.py treats this as an opaque passthrough string.
        return {"version": "4.2.2"}

    # ------------------------------------------------------------------
    # Sites
    # ------------------------------------------------------------------

    @app.get("/mso/api/v1/sites/fabric-connectivity")
    async def get_fabric_connectivity():
        """Per-site connectivity status.

        Must be declared BEFORE the generic /{site_id} route (if any)
        so FastAPI's literal-first match picks it up correctly.
        """
        return state.fabric_connectivity

    @app.get("/mso/api/v1/sites")
    async def get_sites():
        return {"sites": state.sites}

    # ------------------------------------------------------------------
    # Tenants
    # ------------------------------------------------------------------

    @app.get("/mso/api/v1/tenants")
    async def get_tenants():
        return {"tenants": state.tenants}

    # PR-10: cisco.mso.ndo_template's `lookup_tenant()` prereq lookup (used
    # by TenantPol tenant-policy templates) resolves through this bare
    # /api/v1/tenants path in addition to the /mso-prefixed form above —
    # backs both with the same tenant list so a caller using either shape
    # sees identical data (see docs/CONTRACT.md §7).
    @app.get("/api/v1/tenants")
    async def get_tenants_bare():
        return {"tenants": state.tenants}

    def _find_tenant(tenant_id: str) -> dict | None:
        for t in state.tenants:
            if t["id"] == tenant_id:
                return t
        return None

    @app.put("/mso/api/v1/tenants/{tenant_id}")
    async def update_tenant(tenant_id: str, request: Request):
        """Update an existing tenant in place — cisco.mso.mso_tenant's
        "tenant already exists" branch (PR-11).

        Every tenant this sim seeds comes straight from the topology (see
        build_ndo_model), so a real E2E run's `mso_tenant` task always finds
        the tenant already present via `GET /mso/api/v1/tenants` and takes
        this PUT-to-update path rather than POST-to-create — confirmed on a
        real hardware run: `PUT https://.../mso/api/v1/tenants/{id}` → sim 404
        (route didn't exist) → "MSO Error:". Accept the module's full
        payload (description/displayName/siteAssociations/userAssociations)
        and merge it into the stored tenant dict, then echo it back per
        MSOModule.request()'s 200/201/202 "parse and return the body" path.
        """
        tenant = _find_tenant(tenant_id)
        if tenant is None:
            raise HTTPException(
                status_code=404, detail=f"Tenant '{tenant_id}' not found"
            )
        body = await request.json()
        tenant.update(body)
        tenant["id"] = tenant_id
        return tenant

    @app.post("/mso/api/v1/tenants")
    async def create_tenant(request: Request):
        """Create a brand-new tenant not already in the seeded topology."""
        body = await request.json()
        new_id = body.get("id") or f"tenant-{uuid.uuid4().hex[:16]}"
        tenant = dict(body)
        tenant["id"] = new_id
        tenant.setdefault("siteAssociations", [])
        state.tenants.append(tenant)
        return tenant

    # F5: `cisco.mso.mso_tenant` (state=absent) tears a tenant down with
    # `DELETE tenants/{id}` — previously unrouted here (observed 405),
    # which blocked E2E baseline cleanup. Real NDO refuses to delete a
    # tenant while any schema template still references it (tenants must
    # be dissociated/schema-free first), so mirror that guard: any
    # schema_details template whose `tenantId` matches → 400. Registered
    # on both the /mso-prefixed and bare paths, following the
    # GET /mso/api/v1/tenants ↔ GET /api/v1/tenants dual-shape pair above
    # (`delegate_to: localhost` cisco.mso tasks skip the /mso prefix —
    # see the PR-13 templates-store comment below).
    #
    # F5b: the F5 guard above only ever scanned SCHEMA templates — a
    # tenant referenced ONLY by a tenant policy template
    # (`tenant_policy_templates[*].tenantPolicyTemplate.template.
    # tenantId` — same field path `_get_template_objects`/
    # `_backfill_policy_uuids` already traverse above) deleted cleanly,
    # fails-open vs. real NDO which refuses regardless of which template
    # type holds the reference. Extend the scan to cover both. The
    # policy-template branch's exact wording is an approximation mirrored
    # from the F5 schema-template message's format/shape — no real-NDO
    # hardware capture of THIS specific rejection text exists yet, unlike
    # the schema-template wording above (see F5's commit evidence). A
    # malformed policy-template entry (non-dict `tenantPolicyTemplate`/
    # `template`) default-allows rather than crashing the guard, matching
    # this sim's fail-safe philosophy elsewhere.
    @app.delete("/mso/api/v1/tenants/{tenant_id}")
    @app.delete("/api/v1/tenants/{tenant_id}")
    async def delete_tenant(tenant_id: str):
        tenant = _find_tenant(tenant_id)
        if tenant is None:
            raise HTTPException(
                status_code=404, detail=f"Tenant '{tenant_id}' not found"
            )
        referencing_schemas = sorted(
            {
                detail.get("displayName") or detail.get("name") or detail["id"]
                for detail in state.schema_details.values()
                for tmpl in detail.get("templates", []) or []
                if isinstance(tmpl, dict) and tmpl.get("tenantId") == tenant_id
            }
        )
        referencing_policy_templates = sorted(
            {
                doc.get("displayName") or key
                for key, doc in state.tenant_policy_templates.items()
                if isinstance(doc, dict)
                and isinstance(doc.get("tenantPolicyTemplate"), dict)
                and isinstance(
                    doc["tenantPolicyTemplate"].get("template"), dict
                )
                and doc["tenantPolicyTemplate"]["template"].get("tenantId")
                == tenant_id
            }
        )
        if referencing_schemas and referencing_policy_templates:
            detail_msg = (
                f"Tenant '{tenant_id}' is referenced by schema(s): "
                f"{', '.join(referencing_schemas)} — delete those schemas "
                f"first; and tenant policy template(s): "
                f"{', '.join(referencing_policy_templates)} — delete those "
                f"templates first"
            )
        elif referencing_schemas:
            detail_msg = (
                f"Tenant '{tenant_id}' is referenced by schema(s): "
                f"{', '.join(referencing_schemas)} — delete those schemas first"
            )
        elif referencing_policy_templates:
            detail_msg = (
                f"Tenant '{tenant_id}' is referenced by tenant policy "
                f"template(s): {', '.join(referencing_policy_templates)} — "
                f"delete those templates first"
            )
        if referencing_schemas or referencing_policy_templates:
            raise HTTPException(status_code=400, detail=detail_msg)
        state.tenants.remove(tenant)
        return {}

    # PR-11: `cisco.mso.ndo_template`'s prereq lookup for TenantPol templates
    # (`prereq_tenantpol.yml`'s `ndo_template` task) resolves sites through
    # this BARE path too — confirmed against a real hardware run that hit
    # `GET https://.../api/v1/sites` (no `/mso` prefix) → 404. Same data as
    # the canonical `/mso/api/v1/sites` route above.
    @app.get("/api/v1/sites")
    async def get_sites_bare():
        return {"sites": state.sites}

    # ------------------------------------------------------------------
    # ND-platform user class queries — PR-11
    # ------------------------------------------------------------------
    # cisco.mso's MSOModule.lookup_users()/lookup_remote_users() query the
    # modern `/nexus/infra/api/aaa/v4/{local,remote}users` endpoints first
    # (ignore_not_found_error=True) and, only when BOTH come back as an empty
    # dict, fall back to these legacy `/api/config/class/*` routes — see
    # ansible-mso plugins/module_utils/mso.py. The sim doesn't implement the
    # v4 aaa routes, so httpapi's connection plugin treats their absence as
    # "not found" → empty dict → the fallback below is exactly what's hit.
    # A real ACI-hardware E2E run confirmed the failure signature: GET
    # /api/config/class/remoteusers → sim 404 {"detail":"Not Found"} → nd_request()
    # has no "code"/"messages" in that body → "ND Error: Unknown error no
    # error code in decoded payload", aborting mso_tenant's Create a tenant task.
    @app.get("/api/config/class/remoteusers")
    async def get_remote_users_class():
        return state.remote_users

    @app.get("/api/config/class/localusers")
    async def get_local_users_class():
        return state.local_users

    # ------------------------------------------------------------------
    # Schemas — list and detail
    # ------------------------------------------------------------------

    @app.get("/mso/api/v1/schemas")
    async def get_schemas():
        """Return the FULL schema detail list (not the `list-identity`
        lightweight summary below).

        PR-13: real NDO's plain `GET /schemas` returns every schema's
        complete nested doc (`templates[].{bds,anps,serviceGraphs,...}`,
        top-level `sites[]`) — this is precisely why `schemas/list-identity`
        exists as a separate, lighter-weight enumeration endpoint (per its
        own PR-10 docstring below: callers that only need id/displayName
        use that one instead of paying for the full payload here). Several
        OLDER `cisco.mso` community modules that predate the `MSOTemplate`/
        `MSOSchema` module_utils classes — e.g. `mso-model`'s
        `custom_mso_schema_service_graph.py` (`mso.get_obj('schemas',
        displayName=schema)`) — bare-subscript straight into this response
        (`schema_obj.get('templates')[idx]['serviceGraphs']`,
        `schema_obj.get('sites')[idx]['serviceGraphs']`), so a trimmed
        `{name}`-only projection here would KeyError/IndexError on those
        exact modules even with every other collection-default fix in
        place; a real hardware MS-TN2 `create_tenant` pass-2 run confirmed
        this class of failure (`KeyError: 'serviceGraphs'`).
        """
        all_ids = [s["id"] for s in state.schemas] + [s["id"] for s in state.extra_schemas]
        full = [state.schema_details[sid] for sid in all_ids if sid in state.schema_details]
        return {"schemas": full}

    # PR-10: cisco.mso's MSOModule.lookup_schema() calls this lightweight
    # enumeration endpoint FIRST to resolve a schema displayName -> id
    # (verified against ansible-mso plugins/module_utils/mso.py:
    # `self.query_objs("schemas/list-identity", key="schemas", displayName=schema)`,
    # which reads json["schemas"][] entries with at least id + displayName).
    # CRITICAL ORDERING: this MUST be declared before the parameterized
    # GET /mso/api/v1/schemas/{schema_id} route below, or FastAPI/Starlette
    # matches "list-identity" as a schema_id path param first (that bug was
    # observed as a 404 body {"detail":"Schema 'list-identity' not found"}).
    # Same store as GET /mso/api/v1/schemas — every schema this sim knows
    # about (seeded + runtime-POSTed) is enumerable here too.
    @app.get("/mso/api/v1/schemas/list-identity")
    async def get_schemas_list_identity():
        all_schemas = list(state.schemas) + list(state.extra_schemas)
        return {
            "schemas": [
                {"id": s["id"], "displayName": s.get("displayName", s.get("name", ""))}
                for s in all_schemas
            ]
        }

    # PR-13: `custom_mso_schema_service_graph.py` (mso-model role's "Create
    # service graph" task) resolves a service node's display type
    # (Firewall/Load Balancer/Other) to a stable id via
    # `mso.get_obj('schemas/service-node-types', key='serviceNodeTypes',
    # displayName=node_type)` before adding the service-graph node. Same
    # routing-order requirement as `list-identity` above: this literal path
    # segment MUST be declared before the parameterized `/schemas/{schema_id}`
    # route below or FastAPI/Starlette matches "service-node-types" as a
    # schema id instead (observed as a 404 body {"detail":"Schema
    # 'service-node-types' not found"}).
    @app.get("/mso/api/v1/schemas/service-node-types")
    @app.get("/api/v1/schemas/service-node-types")
    async def get_service_node_types():
        return {
            "serviceNodeTypes": [
                {"id": "svc-node-type-firewall", "displayName": "Firewall"},
                {"id": "svc-node-type-adc", "displayName": "Load Balancer"},
                {"id": "svc-node-type-other", "displayName": "Other"},
            ]
        }

    @app.get("/mso/api/v1/schemas/{schema_id}/policy-states")
    async def get_policy_states(schema_id: str):
        """Per-schema deployment / drift state.

        Declared before the bare `/{schema_id}` route so FastAPI routes
        `/schemas/X/policy-states` here rather than treating `policy-states`
        as part of the schema id.
        """
        ps = state.policy_states.get(schema_id)
        if ps is None:
            # Caller may have POSTed a schema whose id we stored in policy_states;
            # fall back to a healthy default rather than 404.
            ps = [{"status": "synced", "drift": False}]
        return {"policyStates": ps}

    # PR-11: both `cisco.mso.mso_schema_template_deploy` (legacy) and
    # `cisco.mso.ndo_schema_template_deploy` (the one aci-ansible's mso-model
    # role actually calls, confirmed against the collection installed on
    # real ACI hardware) call `mso.validate_schema(schema_id)` — `GET
    # schemas/{id}/validate` — before every deploy/redeploy. The return
    # value is discarded by the caller, only the status code matters; a real
    # hardware run 404'd here because the route didn't exist at all. Declared
    # before the bare `/{schema_id}` route for the same routing-order reason
    # as policy-states above.
    @app.get("/mso/api/v1/schemas/{schema_id}/validate")
    async def validate_schema(schema_id: str):
        if schema_id not in state.schema_details:
            raise HTTPException(
                status_code=404, detail=f"Schema '{schema_id}' not found"
            )
        return {"errors": [], "warnings": []}

    @app.get("/mso/api/v1/schemas/{schema_id}")
    async def get_schema(schema_id: str):
        """Full schema detail — templates with VRFs/BDs/EPGs/contracts/filters."""
        detail = state.schema_details.get(schema_id)
        if detail is None:
            raise HTTPException(
                status_code=404, detail=f"Schema '{schema_id}' not found"
            )
        return detail

    # PR-11: legacy `mso_schema_template_deploy`'s deploy/undeploy request —
    # `GET execute/schema/{id}/template/{name}` (deploy/undeploy) or
    # `GET status/schema/{id}/template/{name}` (state=status). The response
    # is splatted straight into `mso.exit_json(**status)`, so it must be a
    # JSON object (dict), not a list/scalar. Kept alongside the POST /task
    # route below since both modules ship in the same collection and either
    # could be used by a given playbook.
    @app.get("/mso/api/v1/execute/schema/{schema_id}/template/{template_name}")
    async def execute_schema_template(schema_id: str, template_name: str):
        if schema_id not in state.schema_details:
            raise HTTPException(
                status_code=404, detail=f"Schema '{schema_id}' not found"
            )
        return {"status": "success", "schemaId": schema_id, "templateName": template_name}

    @app.get("/mso/api/v1/status/schema/{schema_id}/template/{template_name}")
    async def status_schema_template(schema_id: str, template_name: str):
        if schema_id not in state.schema_details:
            raise HTTPException(
                status_code=404, detail=f"Schema '{schema_id}' not found"
            )
        return {"status": "success", "schemaId": schema_id, "templateName": template_name}

    # PR-11: `cisco.mso.ndo_schema_template_deploy` — the module aci-ansible's
    # mso-model/tasks/template_deploy.yml role actually invokes (confirmed
    # against the collection installed on real ACI hardware, which differs slightly from
    # the legacy mso_schema_template_deploy.py) — sends deploy/redeploy/
    # undeploy as `POST /mso/api/v1/task` with body
    # `{"schemaId":...,"templateName":...,"isRedeploy":bool}` (or `undeploy:
    # [siteId,...]`), and `state=query` as `GET status/schema/{id}/template/
    # {name}` (same route as the legacy module's status query above). A real
    # hardware run 404'd on `POST .../mso/api/v1/task` — the route didn't exist.
    @app.post("/mso/api/v1/task")
    async def post_task(request: Request):
        body = await request.json()
        schema_id = body.get("schemaId")
        if schema_id is not None and schema_id not in state.schema_details:
            raise HTTPException(
                status_code=404, detail=f"Schema '{schema_id}' not found"
            )
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        template_name = body.get("templateName")

        # Deploy mirror (confirmed SIM GAP): `ndo_schema_template_deploy`
        # sends `undeploy: [siteId, ...]` (a non-empty list) to tear a
        # template down from those sites; every other shape (deploy /
        # `isRedeploy: true` redeploy) is a create/refresh. `apic_states`
        # is None in every pre-existing test/call site (default arg), so
        # this stays a strict no-op there — unchanged behavior.
        if apic_states is not None and template_name:
            undeploy = bool(body.get("undeploy"))
            mirror_template_to_sites(
                state, template_name, apic_states, schema_id=schema_id, undeploy=undeploy,
            )

        return {
            "id": task_id,
            "schemaId": schema_id,
            "templateName": template_name,
            "status": "success",
        }

    # ------------------------------------------------------------------
    # Template summaries and detail — PR-13 generalized template store
    # ------------------------------------------------------------------
    # `cisco.mso.ndo_template`'s CREATE flow (`prereq_tenantpol.yml`'s
    # `Create TenantPol-<tenant> tenant policy template` task) needs a real
    # POST + PATCH round trip against a persistent template store, not just
    # the single fixed tenantPolicy template PR-10/PR-11 seeded. And because
    # that task carries `delegate_to: localhost` while every OTHER
    # `cisco.mso`/`ndo_*` task in the same playbook run does not, the two
    # code paths hit DIFFERENT URL shapes for the identical logical
    # endpoint:
    #   - `delegate_to: localhost` tasks build `MSOModule` with no
    #     persistent httpapi connection (`module._socket_path is None`), so
    #     `MSOModule.request()` takes its direct-HTTP branch and never adds
    #     the `/mso` prefix at all: `GET/POST https://host/api/v1/templates*`
    #     (confirmed on a real hardware run: `prereq_tenantpol.yml`'s
    #     `ndo_template` task 404'd on exactly this bare path).
    #   - every other task on this same playbook host runs over the
    #     persistent `ansible.netcommon.httpapi` connection
    #     (`ansible_network_os: cisco.nd.nd`), whose platform tag routes
    #     through `NDO_API_VERSION_PATH_FORMAT = "/mso/api/{v}/{path}"` —
    #     e.g. `create_dhcp_relay`'s `ndo_dhcp_relay_policy` task.
    # Both shapes must therefore back the SAME mutable store so a template
    # created via the bare path is visible to a later mso-prefixed lookup
    # (and vice versa) — registered via a shared handler, matching the
    # existing `/api/v1/tenants` ↔ `/mso/api/v1/tenants` / `/api/v1/sites`
    # ↔ `/mso/api/v1/sites` pattern PR-10/PR-11 already established.

    def _template_summary_view(doc: dict) -> dict:
        return {
            "templateId": doc.get("templateId", ""),
            "templateName": doc.get("displayName", ""),
            "templateType": doc.get("templateType", ""),
        }

    def _sync_template_summary(doc: dict) -> None:
        """Keep `state.template_summaries` in sync with a stored template
        doc — mutate the existing summary entry in place if present, else
        append. Mirrors `_schema_summary`/`_find_summary`'s schema-side
        pattern below."""
        view = _template_summary_view(doc)
        for existing in state.template_summaries:
            if existing.get("templateId") == view["templateId"]:
                existing.update(view)
                return
        state.template_summaries.append(view)

    async def _get_template_summaries():
        """Return a list of {templateId, templateName, templateType}.

        ndo_connector.get_dhcp_policy_map() handles both a plain list and
        {"templates": [...]} — we return the list directly. Query params
        (templateName/templateType/schemaName/schemaId) are accepted but
        filtering happens client-side in `MSOTemplate`/`query_objs()`, so
        this route always returns the full list — same as every other
        `query_objs()`-backed enumeration endpoint in this file.
        """
        return state.template_summaries

    async def _get_template(template_id: str):
        """Return a specific template detail (e.g. the tenantPolicy DHCP template)."""
        doc = state.tenant_policy_templates.get(template_id)
        if doc is None:
            raise HTTPException(
                status_code=404, detail=f"Template '{template_id}' not found"
            )
        return doc

    def _backfill_policy_uuids(doc: dict, template_id: str) -> None:
        """Assign a stable `uuid` to every dhcpRelayPolicies/
        dhcpOptionPolicies entry that lacks one.

        `ndo_dhcp_relay_policy`'s add payload is only `{name, providers,
        description?}` (no `uuid` — real NDO assigns that server-side).
        `ndo_schema_template_bd_dhcp_policy`'s `get_dhcp_relay_policy_uuid()`/
        `get_dhcp_option_policy_uuid()` (the `bind_dhcp_relay_to_bd`
        playbook step) then requires a non-empty `uuid` on the matched
        entry — `mso.fail_json()`s otherwise — so a query-back immediately
        after the add must already see one.
        """
        container = doc.get("tenantPolicyTemplate", {}).get("template", {})
        tenant_id = container.get("tenantId", "")
        for list_key in ("dhcpRelayPolicies", "dhcpOptionPolicies"):
            for item in container.get(list_key, []) or []:
                if isinstance(item, dict) and not item.get("uuid"):
                    seed = "{}-uuid-{}-{}-{}".format(list_key, template_id, tenant_id, item.get("name", ""))
                    item["uuid"] = hashlib.sha256(seed.encode()).hexdigest()[:32]

    async def _create_template(request: Request):
        """Create a new template (`ndo_template`'s "does not exist yet"
        branch — POST payload shape: `{displayName, templateType,
        <typeContainer>: {template:{...}, sites:[{siteId}]}}`, e.g.
        `tenantPolicyTemplate: {template:{tenantId}, sites:[{siteId}]}}`).

        Assigns a stable-looking id, stores the full doc (so a later
        `ndo_dhcp_relay_policy` PATCH against `templates/{id}` finds it),
        and mirrors a summary entry into `template_summaries` so the next
        `templates/summaries?templateName=...` lookup (by any caller,
        bare-path or mso-prefixed) resolves it.
        """
        body = await request.json()
        new_id = f"template-{uuid.uuid4().hex[:20]}"
        doc = dict(body)
        doc["templateId"] = new_id
        doc.setdefault("displayName", body.get("displayName", ""))
        doc.setdefault("templateType", body.get("templateType", ""))
        _backfill_policy_uuids(doc, new_id)
        state.tenant_policy_templates[new_id] = doc
        _sync_template_summary(doc)
        return doc

    async def _patch_template(template_id: str, request: Request):
        """Apply a JSON-Patch op list to a stored template (e.g.
        `ndo_dhcp_relay_policy`'s `add /tenantPolicyTemplate/template/
        dhcpRelayPolicies/-`, or `ndo_template`'s site add/remove ops)."""
        doc = state.tenant_policy_templates.get(template_id)
        if doc is None:
            raise HTTPException(
                status_code=404, detail=f"Template '{template_id}' not found"
            )
        body = await request.json()
        ops = body if isinstance(body, list) else body.get("ops", [])
        if not ops:
            return doc
        try:
            apply_json_patch(doc, ops)
        except PatchError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        doc["templateId"] = template_id
        _backfill_policy_uuids(doc, template_id)
        _sync_template_summary(doc)
        return doc

    async def _delete_template(template_id: str):
        state.tenant_policy_templates.pop(template_id, None)
        state.template_summaries[:] = [
            s for s in state.template_summaries if s.get("templateId") != template_id
        ]
        return {}

    #: `type`→(store-path-segment, name-key) for the generic cross-template
    #: object lookup below.
    _TEMPLATE_OBJECT_TYPES: dict[str, tuple[str, str]] = {
        "dhcpRelay": ("dhcpRelayPolicies", "name"),
        "dhcpOption": ("dhcpOptionPolicies", "name"),
    }

    async def _get_template_objects(request: Request):
        """Cross-template object lookup — `GET templates/objects?type=
        {dhcpRelay|dhcpOption|epg|externalEpg}&{uuid=...|name=...}`.

        Real NDO exposes this as a flat cross-cutting search over every
        template's policy objects; `ansible-mso`'s `MSOTemplate.
        get_template_object_by_uuid()` (`plugins/module_utils/template.py`)
        and `ndo_schema_template_bd_dhcp_policy.py`'s
        `get_dhcp_relay_policy_uuid()`/`get_dhcp_relay_label_name()` both
        call this exact route — the former to resolve a DHCP relay/option
        policy's UUID by name (`create_dhcp_relay`'s NDO 4.x path), the
        latter to resolve a stored `dhcpLabels[].ref` UUID back to a name
        (`bind_dhcp_relay_to_bd`'s query-back-after-PATCH step). Confirmed
        against a real hardware run: both hit `GET /mso/api/v1/templates/
        objects?type=dhcpRelay&name=...` mid-playbook.

        `type=epg`/`type=externalEpg` additionally search every SCHEMA
        template's `anps[].epgs[]` / `externalEpgs[]` (not just the
        tenantPolicy templates) — `ndo_dhcp_relay_policy`'s
        `insert_dhcp_relay_policy_relation_name()` resolves a stored
        `epgRef`/`externalEpgRef` UUID back to a display name this way on
        every query/present round trip.
        """
        obj_type = request.query_params.get("type", "")
        uuid_q = request.query_params.get("uuid")
        name_q = request.query_params.get("name")

        results: list[dict] = []

        if obj_type in _TEMPLATE_OBJECT_TYPES:
            list_key, name_key = _TEMPLATE_OBJECT_TYPES[obj_type]
            for tmpl_doc in state.tenant_policy_templates.values():
                tenant_id = (
                    tmpl_doc.get("tenantPolicyTemplate", {}).get("template", {}).get("tenantId")
                )
                container = tmpl_doc.get("tenantPolicyTemplate", {}).get("template", {})
                for item in container.get(list_key, []) or []:
                    if not isinstance(item, dict):
                        continue
                    entry = dict(item)
                    entry.setdefault("tenantId", tenant_id)
                    results.append(entry)
        elif obj_type in ("epg", "externalEpg"):
            array_key = "externalEpgs" if obj_type == "externalEpg" else None
            for detail in list(state.schema_details.values()):
                for tmpl in detail.get("templates", []):
                    if array_key:
                        for item in tmpl.get("externalEpgs", []) or []:
                            if isinstance(item, dict):
                                results.append(item)
                    else:
                        for anp in tmpl.get("anps", []) or []:
                            for epg in anp.get("epgs", []) or []:
                                if isinstance(epg, dict):
                                    results.append(epg)

        if uuid_q is not None:
            match = next((r for r in results if r.get("uuid") == uuid_q), None)
            return match or {}
        if name_q is not None:
            matches = [r for r in results if r.get(_TEMPLATE_OBJECT_TYPES.get(obj_type, ("", "name"))[1]) == name_q]
            return matches
        return results

    for prefix in ("/mso/api/v1", "/api/v1"):
        app.add_api_route(f"{prefix}/templates/summaries", _get_template_summaries, methods=["GET"])
        app.add_api_route(f"{prefix}/templates/objects", _get_template_objects, methods=["GET"])
        app.add_api_route(f"{prefix}/templates", _create_template, methods=["POST"])
        app.add_api_route(f"{prefix}/templates/{{template_id}}", _get_template, methods=["GET"])
        app.add_api_route(f"{prefix}/templates/{{template_id}}", _patch_template, methods=["PATCH"])
        app.add_api_route(f"{prefix}/templates/{{template_id}}", _delete_template, methods=["DELETE"])

    # ------------------------------------------------------------------
    # Audit records (both canonical and fallback paths)
    # ------------------------------------------------------------------

    async def _audit_records(count: int = 50):
        return {"auditRecords": state.audit_records[:count]}

    app.add_api_route(
        "/api/v1/audit-records",
        _audit_records,
        methods=["GET"],
    )
    app.add_api_route(
        "/mso/api/v1/audit-records",
        _audit_records,
        methods=["GET"],
    )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def _schema_summary(detail: dict) -> dict:
        """Project a full schema detail dict down to the list/GET-many shape.

        Kept in sync on every write (POST create, PATCH mutate) so GET
        /mso/api/v1/schemas and GET /mso/api/v1/schemas/list-identity always
        reflect the current templates[].name set — mso_schema_template.py's
        `get_obj("schemas", displayName=schema)` inspects exactly this.
        """
        return {
            "id": detail["id"],
            "displayName": detail.get("displayName", detail.get("name", "")),
            "name": detail.get("name", detail.get("displayName", "")),
            "templates": [
                {"name": t.get("name", "")} for t in detail.get("templates", [])
            ],
        }

    def _find_summary(schema_id: str) -> dict | None:
        """Locate a schema's summary dict in whichever list holds it."""
        for s in state.schemas:
            if s["id"] == schema_id:
                return s
        for s in state.extra_schemas:
            if s["id"] == schema_id:
                return s
        return None

    @app.post("/mso/api/v1/schemas")
    async def create_schema(request: Request):
        """Store a new schema and return its assigned id + status.

        Two shapes reach this route:
          - `mso_schema.py` (state=present, no templates): POST
            `{"displayName":..., "description":...}` — an empty schema, no
            `id`/`templates` in the body.
          - `mso_schema_template.py`'s "schema does not exist yet" branch:
            POST `{"displayName": schema, "templates": [{name, displayName,
            tenantId}], "sites": []}` — the schema is born already carrying
            its first template. This is the path a real hardware run takes
            for the per-tenant-region `<tenant>-<region>` schemas (e.g.
            "MS-TN1-LAB0") — the schema must round-trip through GET
            /schemas/list-identity (by displayName) and GET /schemas/{id}
            for the subsequent mso_schema_template_bd/_anp/... PATCH calls
            to find it (PR-11).
        """
        body = await request.json()
        new_id = f"schema-{uuid.uuid4().hex[:16]}"
        display_name = body.get("displayName", body.get("name", "unnamed"))
        schema_name = body.get("name", display_name)

        # Full detail (returned by GET /schemas/{id}) — store the POSTed
        # body verbatim (templates/sites and all) plus the assigned id, so
        # every field a later PATCH op path might reference already exists.
        detail = dict(body)
        detail["id"] = new_id
        detail["displayName"] = display_name
        detail["name"] = schema_name
        detail.setdefault("templates", [])
        detail.setdefault("sites", [])
        for tmpl in detail["templates"]:
            normalize_template(tmpl, new_id)
        for site in detail["sites"]:
            if isinstance(site, dict):
                site.setdefault("bds", [])
                site.setdefault("anps", [])
                normalize_site(site)

        summary = _schema_summary(detail)

        state.extra_schemas.append(summary)
        state.schema_details[new_id] = detail
        state.policy_states[new_id] = [{"status": "synced", "drift": False}]

        return {"id": new_id, "displayName": display_name, "status": "active"}

    @app.patch("/mso/api/v1/schemas/{schema_id}")
    async def patch_schema(schema_id: str, request: Request):
        """Apply a JSON-Patch op list to the stored schema and return the doc.

        Every cisco.mso schema-object module (mso_schema_template,
        mso_schema_template_bd/_anp/_anp_epg, mso_schema_site, ...) mutates a
        schema this way: look it up (list-identity → id, then GET by id),
        then PATCH with a small list of `{op, path, value}` entries such as
        `{"op":"add","path":"/templates/LAB1/bds/-","value":{...}}`. See
        aci_sim/ndo/patch.py for the applier and docs/CONTRACT.md §7
        for the full request-sequence writeup (PR-11).
        """
        detail = state.schema_details.get(schema_id)
        if detail is None:
            raise HTTPException(
                status_code=404, detail=f"Schema '{schema_id}' not found"
            )

        body = await request.json()
        ops = body if isinstance(body, list) else body.get("ops", [])
        if not ops:
            # cisco.mso's own MSOModule.request() short-circuits an empty-ops
            # PATCH client-side and never sends it, but stay tolerant.
            return detail

        # F4 + F12 (see aci_sim/ndo/service_graph_validation.py docstring): only PATCHes
        # that actually touch service-graph surface (a site-local
        # `serviceGraphs` add/replace, or a site-local contract's
        # `serviceGraphRelationship`) take the gated copy-validate-commit
        # path below. Every other PATCH (the ~95% that are BD/EPG/subnet/
        # contract-filter) stays on the original in-place fast path,
        # byte-identical to before this change.
        try:
            if not service_graph_relevant(ops):
                apply_json_patch(detail, ops)
            else:
                # F4 is a pure reference check — device existence doesn't
                # depend on the mutation — so it pre-scans `ops` against the
                # CURRENT (pre-mutation) `detail` before anything is applied.
                # Raising here is naturally all-or-nothing (design §2.2).
                _validate_service_graph_device_refs(ops, apic_states, state, detail)

                # F12 is a post-request-final-state check (design §3.1): it
                # cannot be evaluated per-op without false-rejecting the
                # legal atomic multi-fabric PATCH mid-batch. Apply the ops to
                # a deepcopy, validate the RESULT, and only commit the copy
                # back into state.schema_details on success — a 400 here
                # leaves the live stored schema completely untouched (and,
                # incidentally, fixes the pre-existing partial-write-on-
                # PatchError bug for service-graph PATCHes — design §3.2).
                working = copy.deepcopy(detail)
                apply_json_patch(working, ops)
                _validate_uniform_site_redirect(working, ops)
                state.schema_details[schema_id] = working
                detail = working
        except (PatchError, ServiceGraphValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        detail["id"] = schema_id  # ops must never be able to clobber the id

        # An "add" op can introduce a brand-new template dict (mso_schema_
        # template.py's "template does not exist" branch PATCHes
        # {"op":"add","path":"/templates/-","value":{name,displayName,
        # tenantId}} — no vrfs/bds/... keys). Re-normalize every template
        # after each PATCH so downstream MSOSchema.set_template_vrf() etc.
        # never meets a missing collection key (same rule as create_schema).
        for tmpl in detail.get("templates", []):
            normalize_template(tmpl, schema_id)

        # Same rule for the top-level `sites[]` array — mso_schema_site_bd.py
        # et al PATCH child objects into `/sites/{siteId-templateName}/bds/-`
        # etc. without every collection default (PR-11).
        for site in detail.get("sites", []):
            if isinstance(site, dict):
                site.setdefault("bds", [])
                site.setdefault("anps", [])
                normalize_site(site)

        # Keep the summary (GET /schemas, /schemas/list-identity) in sync —
        # template adds/removes and displayName/name replace ops all need to
        # be visible to the next lookup_schema() call in the same playbook.
        # Mutate whichever summary list already holds this id in place, so
        # GET /schemas never double-lists or drops the entry.
        updated_summary = _schema_summary(detail)
        existing_summary = _find_summary(schema_id)
        if existing_summary is not None:
            existing_summary.update(updated_summary)
        else:
            # Shouldn't normally happen (every schema_details entry is
            # created alongside a summary), but degrade gracefully.
            state.extra_schemas.append(updated_summary)

        state.policy_states.setdefault(schema_id, [{"status": "synced", "drift": False}])

        return detail

    # F5: `cisco.mso.mso_schema` (state=absent) deletes a whole schema via
    # `DELETE schemas/{id}` — previously unrouted (observed 405). Removes
    # the full detail, its summary entry (whichever list holds it — seeded
    # `state.schemas` or runtime `state.extra_schemas`) and its
    # policy-states record, so GET /schemas, /schemas/list-identity and
    # GET /schemas/{id} all stop reflecting it immediately. /mso prefix
    # only — the schema CRUD surface (GET/POST/PATCH above) has no bare
    # /api/v1 alias convention, unlike tenants/sites/templates.
    #
    # Deliberately NO cascade into the APIC deploy mirror: real NDO does
    # not undeploy when a schema is deleted — objects already deployed to
    # the sites' APICs are left orphaned there. Callers that want a clean
    # teardown must undeploy first (`POST /mso/api/v1/task` with
    # `undeploy: [siteId, ...]`), exactly as on real gear.
    @app.delete("/mso/api/v1/schemas/{schema_id}")
    async def delete_schema(schema_id: str):
        if schema_id not in state.schema_details:
            raise HTTPException(
                status_code=404, detail=f"Schema '{schema_id}' not found"
            )
        del state.schema_details[schema_id]
        state.schemas[:] = [s for s in state.schemas if s["id"] != schema_id]
        state.extra_schemas[:] = [
            s for s in state.extra_schemas if s["id"] != schema_id
        ]
        state.policy_states.pop(schema_id, None)
        return {}

    # ------------------------------------------------------------------
    # State persistence (/_sim) — mirrors the APIC plane's admin router
    # (aci_sim/control/admin.py), which the NDO app doesn't otherwise
    # mount, so it's registered directly here.
    # ------------------------------------------------------------------

    @app.post("/_sim/save/{name}")
    async def sim_save(name: str):
        """Persist the NDO state to disk under *name*."""
        path = state_dir() / f"{name}.ndo.json"
        save_json(path, wrap(serialize_ndo(state)))
        return {"status": "ok", "file": str(path)}

    @app.post("/_sim/load/{name}")
    async def sim_load(name: str, force: bool = False):
        """Restore the NDO state from a prior :func:`sim_save`.

        Refuses a snapshot stamped with a different sim version or topology
        unless ``force=1`` — see ``persist.compatibility``.
        """
        path = state_dir() / f"{name}.ndo.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"state '{name}' not found")
        data, meta = unwrap(load_json(path))
        ok, reason = compatibility(meta)
        if not ok and not force:
            raise HTTPException(status_code=409, detail=reason)
        apply_ndo(state, data)
        return {"status": "ok", "compatibility": reason}

    @app.post("/mso/api/v1/deploy")
    async def deploy(request: Request):
        """Acknowledge a deploy request and return an in-progress record."""
        deploy_id = f"deploy-{uuid.uuid4().hex[:12]}"
        return {"id": deploy_id, "status": "in-progress"}

    return app
