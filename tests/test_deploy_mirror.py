"""Tests for the NDO -> APIC deploy mirror (aci_sim/ndo/deploy_mirror.py).

Confirmed SIM GAP: `POST /mso/api/v1/task` used to be a pure ack no-op —
multi-site EPGs/BDs/VRFs deployed via NDO never appeared on the target
sites' APIC MITStores. These tests cover the pure `mirror_template_to_sites`
function directly (unit + idempotency + undeploy) and the app-level wiring
in `POST /mso/api/v1/task` (with and without `apic_states`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from aci_sim.mit.store import MITStore
from aci_sim.ndo.app import make_ndo_app
from aci_sim.ndo.deploy_mirror import _basename, mirror_template_to_sites
from aci_sim.ndo.model import NdoState


@dataclass
class _FakeApicState:
    """Minimal stand-in for rest_aci.app.ApicSiteState — the mirror only
    ever touches `.store`, so a full ApicSiteState (name/site/topo/baseline)
    is unnecessary scaffolding for these unit tests."""

    store: MITStore = field(default_factory=MITStore)


TENANT_ID = "tenant-ms-tn1-id"
TENANT_NAME = "ANS-MS_TN1"
SCHEMA_ID = "schema-1"
TEMPLATE_NAME = "MS-TN1-template"


def _build_state(*, site1_host_based_routing: bool = True) -> NdoState:
    """A minimal NdoState: one schema, one template (1 vrf, 1 bd with
    subnets, 1 anp with 1 epg providing a contract), and one site entry
    (siteId '1') associated with the template."""
    template = {
        "name": TEMPLATE_NAME,
        "tenantId": TENANT_ID,
        "vrfs": [{"name": "VRF1"}],
        "bds": [
            {
                "name": "App1",
                "vrfRef": f"/schemas/{SCHEMA_ID}/templates/{TEMPLATE_NAME}/vrfs/VRF1",
                "epMoveDetectMode": "garp",
                "arpFlood": True,
                "l2UnknownUnicast": "flood",
                "unicastRouting": True,
                "l2Stretch": True,
                "intersiteBumTrafficAllow": True,
                "subnets": [{"ip": "10.10.10.1/24", "scope": "public,shared", "primary": True}],
            }
        ],
        "anps": [
            {
                "name": "AP1",
                "epgs": [
                    {
                        "name": "Web",
                        "bdRef": f"/schemas/{SCHEMA_ID}/templates/{TEMPLATE_NAME}/bds/App1",
                        "preferredGroup": False,
                        "subnets": [],
                        "contractRelationships": [
                            {
                                "relationshipType": "provider",
                                "contractRef": f"/schemas/{SCHEMA_ID}/templates/{TEMPLATE_NAME}/contracts/Web-Con",
                            }
                        ],
                    }
                ],
            }
        ],
        "contracts": [{"name": "Web-Con"}],
    }

    schema_detail = {
        "id": SCHEMA_ID,
        "templates": [template],
        "sites": [
            {
                "siteId": "1",
                "templateName": TEMPLATE_NAME,
                "bds": [
                    {
                        "bdRef": f"/schemas/{SCHEMA_ID}/templates/{TEMPLATE_NAME}/bds/App1",
                        "hostBasedRouting": site1_host_based_routing,
                    }
                ],
                "anps": [
                    {
                        "anpRef": f"/schemas/{SCHEMA_ID}/templates/{TEMPLATE_NAME}/anps/AP1",
                        "epgs": [
                            {
                                "epgRef": f"/schemas/{SCHEMA_ID}/templates/{TEMPLATE_NAME}/anps/AP1/epgs/Web",
                                "domainAssociations": [],
                                "staticPorts": [],
                                "staticLeafs": [],
                                "subnets": [],
                            }
                        ],
                    }
                ],
                # site '2' deliberately omitted — no association at all.
            }
        ],
    }

    return NdoState(
        sites=[],
        tenants=[{"id": TENANT_ID, "name": TENANT_NAME}],
        schemas=[],
        schema_details={SCHEMA_ID: schema_detail},
        template_summaries=[],
        tenant_policy_templates={},
        fabric_connectivity={},
        policy_states={},
        audit_records=[],
    )


# ---------------------------------------------------------------------------
# Unit tests — pure mirror_template_to_sites()
# ---------------------------------------------------------------------------


def test_mirror_materializes_vrf_bd_anp_epg():
    state = _build_state(site1_host_based_routing=True)
    apic_states = {"1": _FakeApicState(), "2": _FakeApicState()}

    written = mirror_template_to_sites(state, TEMPLATE_NAME, apic_states)
    assert written > 0

    store1 = apic_states["1"].store

    ctx = store1.get(f"uni/tn-{TENANT_NAME}/ctx-VRF1")
    assert ctx is not None
    assert ctx.class_name == "fvCtx"

    bd = store1.get(f"uni/tn-{TENANT_NAME}/BD-App1")
    assert bd is not None
    assert bd.class_name == "fvBD"
    assert bd.attrs["hostBasedRouting"] == "yes"
    assert bd.attrs["epMoveDetectMode"] == "garp"
    assert bd.attrs["unkMacUcastAct"] == "flood"
    assert bd.attrs["unicastRoute"] == "yes"
    # _CLASS_DEFAULTS-filled attrs present (not overridden by the template)
    assert bd.attrs["mac"] == "00:22:BD:F8:19:FF"
    assert bd.attrs["type"] == "regular"
    assert bd.attrs["limitIpLearnToSubnets"] == "yes"

    rsctx = store1.get(f"uni/tn-{TENANT_NAME}/BD-App1/rsctx")
    assert rsctx is not None
    assert rsctx.attrs["tnFvCtxName"] == "VRF1"

    subnet = store1.get(f"uni/tn-{TENANT_NAME}/BD-App1/subnet-[10.10.10.1/24]")
    assert subnet is not None

    ap = store1.get(f"uni/tn-{TENANT_NAME}/ap-AP1")
    assert ap is not None
    assert ap.class_name == "fvAp"

    epg_dn = f"uni/tn-{TENANT_NAME}/ap-AP1/epg-Web"
    epg = store1.get(epg_dn)
    assert epg is not None
    assert epg.class_name == "fvAEPg"

    rsbd = store1.get(f"{epg_dn}/rsbd")
    assert rsbd is not None
    assert rsbd.attrs["tnFvBDName"] == "App1"

    rsprov = store1.get(f"{epg_dn}/rsprov-Web-Con")
    assert rsprov is not None
    assert rsprov.attrs["tnVzBrCPName"] == "Web-Con"

    # class query surface works too
    epgs_by_class = store1.by_class("fvAEPg")
    assert any(e.dn == epg_dn for e in epgs_by_class)

    # The tenant SHADOW MO must exist on the site so a subtree/mo query on
    # uni/tn-<T> resolves the mirrored children (F1 root cause: without the
    # fvTenant root MO, the subtree root is empty and the query returns 0 even
    # though the mirrored VRF/BD/EPG children are present).
    tenant_mo = store1.get(f"uni/tn-{TENANT_NAME}")
    assert tenant_mo is not None and tenant_mo.class_name == "fvTenant"

    # Site '2' was never associated with this template's schema-sites entry,
    # and is absent from the schema — its store must stay completely empty.
    store2 = apic_states["2"].store
    assert store2.all() == []


def test_mirror_skips_sites_not_in_apic_states():
    state = _build_state()
    # Only site '1' has a backing APIC store; a real deployment might target
    # other sites this sim process doesn't happen to be running.
    apic_states = {"1": _FakeApicState()}
    written = mirror_template_to_sites(state, TEMPLATE_NAME, apic_states)
    assert written > 0
    assert apic_states["1"].store.get(f"uni/tn-{TENANT_NAME}/BD-App1") is not None


def test_mirror_unknown_template_is_safe_noop():
    state = _build_state()
    apic_states = {"1": _FakeApicState()}
    written = mirror_template_to_sites(state, "does-not-exist", apic_states)
    assert written == 0
    assert apic_states["1"].store.all() == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_mirror_twice_is_idempotent():
    state = _build_state()
    apic_states = {"1": _FakeApicState()}

    mirror_template_to_sites(state, TEMPLATE_NAME, apic_states)
    count_after_first = len(apic_states["1"].store.all())

    mirror_template_to_sites(state, TEMPLATE_NAME, apic_states)
    count_after_second = len(apic_states["1"].store.all())

    assert count_after_first == count_after_second

    epg = apic_states["1"].store.get(f"uni/tn-{TENANT_NAME}/ap-AP1/epg-Web")
    assert epg is not None
    assert epg.attrs["name"] == "Web"


# ---------------------------------------------------------------------------
# Undeploy
# ---------------------------------------------------------------------------


def test_undeploy_removes_template_objects():
    state = _build_state()
    apic_states = {"1": _FakeApicState()}

    mirror_template_to_sites(state, TEMPLATE_NAME, apic_states)
    assert apic_states["1"].store.get(f"uni/tn-{TENANT_NAME}/ap-AP1/epg-Web") is not None

    mirror_template_to_sites(state, TEMPLATE_NAME, apic_states, undeploy=True)

    store1 = apic_states["1"].store
    assert store1.get(f"uni/tn-{TENANT_NAME}/BD-App1") is None
    assert store1.get(f"uni/tn-{TENANT_NAME}/ap-AP1") is None
    assert store1.get(f"uni/tn-{TENANT_NAME}/ap-AP1/epg-Web") is None
    assert store1.get(f"uni/tn-{TENANT_NAME}/ctx-VRF1") is None


# ---------------------------------------------------------------------------
# Schema-collision regression (schema_id threading)
# ---------------------------------------------------------------------------


def test_schema_id_scopes_template_resolution_to_the_right_tenant():
    """Two DIFFERENT schemas each have a template named 'LAB1-LAB2' (a real
    collision: every multi-site tenant's schema commonly reuses these
    generic template names), but belong to different tenants. Passing
    schema_id must resolve ONLY the targeted schema's tenant — the other
    tenant's DNs must never appear in any site's store."""
    collide_name = "LAB1-LAB2"
    schema_a_id = "schema-a"
    schema_b_id = "schema-b"
    tenant_a_id, tenant_a_name = "tenant-a-id", "Tenant-A"
    tenant_b_id, tenant_b_name = "tenant-b-id", "Tenant-B"

    def _make_schema(schema_id: str, tenant_id: str) -> dict:
        template = {
            "name": collide_name,
            "tenantId": tenant_id,
            "vrfs": [{"name": "VRF1"}],
            "bds": [],
            "anps": [],
        }
        return {
            "id": schema_id,
            "templates": [template],
            "sites": [
                {
                    "siteId": "1",
                    "templateName": collide_name,
                    "bds": [],
                    "anps": [],
                }
            ],
        }

    state = NdoState(
        sites=[],
        tenants=[
            {"id": tenant_a_id, "name": tenant_a_name},
            {"id": tenant_b_id, "name": tenant_b_name},
        ],
        schemas=[],
        schema_details={
            schema_a_id: _make_schema(schema_a_id, tenant_a_id),
            schema_b_id: _make_schema(schema_b_id, tenant_b_id),
        },
        template_summaries=[],
        tenant_policy_templates={},
        fabric_connectivity={},
        policy_states={},
        audit_records=[],
    )
    apic_states = {"1": _FakeApicState()}

    written = mirror_template_to_sites(
        state, collide_name, apic_states, schema_id=schema_b_id
    )
    assert written > 0

    store1 = apic_states["1"].store
    assert store1.get(f"uni/tn-{tenant_b_name}/ctx-VRF1") is not None
    # Tenant A's DNs must be completely absent — no cross-schema bleed.
    assert store1.get(f"uni/tn-{tenant_a_name}/ctx-VRF1") is None


# ---------------------------------------------------------------------------
# fvRsPathAtt encap key regression
# ---------------------------------------------------------------------------


def test_static_port_encap_uses_encap_key_not_deployment_immediacy():
    state = _build_state()
    schema_detail = state.schema_details[SCHEMA_ID]
    site_epg = schema_detail["sites"][0]["anps"][0]["epgs"][0]
    site_epg["staticPorts"] = [
        {
            "path": "topology/pod-1/paths-101/pathep-[eth1/1]",
            "encap": "vlan-101",
            "deploymentImmediacy": "immediate",
        }
    ]

    apic_states = {"1": _FakeApicState()}
    mirror_template_to_sites(state, TEMPLATE_NAME, apic_states)

    epg_dn = f"uni/tn-{TENANT_NAME}/ap-AP1/epg-Web"
    path_dn = "topology/pod-1/paths-101/pathep-[eth1/1]"
    rspath = apic_states["1"].store.get(f"{epg_dn}/rspathAtt-[{path_dn}]")
    assert rspath is not None
    assert rspath.attrs["encap"] == "vlan-101"
    assert rspath.attrs["instrImedcy"] == "immediate"


# ---------------------------------------------------------------------------
# Redeploy prune — stale EPG children must not survive a merge-upsert
# ---------------------------------------------------------------------------


def test_redeploy_prunes_removed_contract_binding():
    state = _build_state()
    template = state.schema_details[SCHEMA_ID]["templates"][0]
    epg = template["anps"][0]["epgs"][0]
    # Seed a SECOND provider contract relationship (C1) alongside the
    # existing Web-Con one, deploy, then remove C1 from the template (as if
    # NDO-side config removed it) and redeploy — C1's fvRsProv must be gone.
    epg["contractRelationships"].append(
        {
            "relationshipType": "provider",
            "contractRef": f"/schemas/{SCHEMA_ID}/templates/{TEMPLATE_NAME}/contracts/C1",
        }
    )

    apic_states = {"1": _FakeApicState()}
    mirror_template_to_sites(state, TEMPLATE_NAME, apic_states)

    epg_dn = f"uni/tn-{TENANT_NAME}/ap-AP1/epg-Web"
    store1 = apic_states["1"].store
    assert store1.get(f"{epg_dn}/rsprov-C1") is not None

    # Remove C1 from the template (mutate in place) and redeploy.
    epg["contractRelationships"] = [
        rel
        for rel in epg["contractRelationships"]
        if _basename_test_helper(rel["contractRef"]) != "C1"
    ]
    mirror_template_to_sites(state, TEMPLATE_NAME, apic_states)

    assert store1.get(f"{epg_dn}/rsprov-C1") is None
    # The surviving provider relationship must still be present.
    assert store1.get(f"{epg_dn}/rsprov-Web-Con") is not None


def _basename_test_helper(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def test_basename_resolves_string_and_dict_refs():
    # STRING ref -> last path segment.
    assert _basename("/schemas/x/templates/LAB1-LAB2/bds/bd-App1_LAB0") == "bd-App1_LAB0"
    # DICT refs -> the leaf object-name key (aci-ansible's cisco.mso store shape).
    # Regression: the mirror used to AttributeError ('dict'.rsplit) on a redeploy
    # of an aci-ansible multi-site tenant whose vrfRef/bdRef are dicts, 500-ing
    # the NDO deploy.
    assert _basename({"vrfName": "vrf-L3_LAB0", "schemaId": "s", "templateName": "t"}) == "vrf-L3_LAB0"
    assert _basename({"bdName": "bd-App1_LAB0", "schemaId": "s", "templateName": "t"}) == "bd-App1_LAB0"
    assert _basename({"contractName": "con-Firewall", "schemaId": "s", "templateName": "t"}) == "con-Firewall"
    # epgRef carries BOTH anpName and epgName; the EPG's own name wins.
    assert _basename({"epgName": "epg-web", "anpName": "app-App", "schemaId": "s"}) == "epg-web"
    assert _basename(None) == ""
    assert _basename({}) == ""


# ---------------------------------------------------------------------------
# Empty vrfRef — no fvRsCtx child
# ---------------------------------------------------------------------------


def test_bd_with_no_vrf_ref_emits_no_fvrsctx_child():
    state = _build_state()
    template = state.schema_details[SCHEMA_ID]["templates"][0]
    bd = template["bds"][0]
    bd["vrfRef"] = None

    apic_states = {"1": _FakeApicState()}
    mirror_template_to_sites(state, TEMPLATE_NAME, apic_states)

    bd_dn = f"uni/tn-{TENANT_NAME}/BD-App1"
    assert apic_states["1"].store.get(bd_dn) is not None
    assert apic_states["1"].store.get(f"{bd_dn}/rsctx") is None


# ---------------------------------------------------------------------------
# App-level wiring — POST /mso/api/v1/task
# ---------------------------------------------------------------------------


def test_app_level_deploy_mirrors_into_apic_store():
    state = _build_state()
    apic_states = {"1": _FakeApicState()}
    app = make_ndo_app(state, apic_states)

    with TestClient(app) as client:
        resp = client.post(
            "/mso/api/v1/task",
            json={"schemaId": SCHEMA_ID, "templateName": TEMPLATE_NAME},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["templateName"] == TEMPLATE_NAME
        assert body["status"] == "success"

    epg = apic_states["1"].store.get(f"uni/tn-{TENANT_NAME}/ap-AP1/epg-Web")
    assert epg is not None
    assert epg.class_name == "fvAEPg"


def test_app_level_without_apic_states_is_still_a_noop():
    """make_ndo_app(state) with NO apic_states (every pre-existing test /
    call site) must keep behaving exactly as before — ack only, no mirror
    attempted (and no crash from a missing apic_states arg)."""
    state = _build_state()
    app = make_ndo_app(state)

    with TestClient(app) as client:
        resp = client.post(
            "/mso/api/v1/task",
            json={"schemaId": SCHEMA_ID, "templateName": TEMPLATE_NAME},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["templateName"] == TEMPLATE_NAME
        assert body["status"] == "success"


def test_app_level_undeploy_via_task_body():
    state = _build_state()
    apic_states = {"1": _FakeApicState()}
    app = make_ndo_app(state, apic_states)

    with TestClient(app) as client:
        client.post(
            "/mso/api/v1/task",
            json={"schemaId": SCHEMA_ID, "templateName": TEMPLATE_NAME},
        )
        assert apic_states["1"].store.get(f"uni/tn-{TENANT_NAME}/ap-AP1/epg-Web") is not None

        resp = client.post(
            "/mso/api/v1/task",
            json={
                "schemaId": SCHEMA_ID,
                "templateName": TEMPLATE_NAME,
                "undeploy": ["1"],
            },
        )
        assert resp.status_code == 200

    assert apic_states["1"].store.get(f"uni/tn-{TENANT_NAME}/ap-AP1/epg-Web") is None
