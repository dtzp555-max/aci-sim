"""The DHCP relay half of the NDO -> APIC deploy mirror.

Confirmed SIM GAP, measured on a live fabric: `bind_dhcp_relay_to_bd` sets
`dhcpLabels` on a schema BD and deploys the template — a deploy the mirror
already handled — yet both sites' APICs held zero `dhcpLbl`, `dhcpRelayP` and
`dhcpRsProv`. The playbook returned rc=0 with nothing to verify on the site.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from aci_sim.mit.store import MITStore
from aci_sim.ndo.deploy_mirror import mirror_template_to_sites
from aci_sim.ndo.model import NdoState


@dataclass
class _FakeApicState:
    store: MITStore = field(default_factory=MITStore)


TENANT_ID = "tenant-dhcp-id"
TENANT = "ANS-MS_TN1"
SCHEMA_ID = "schema-dhcp"
TEMPLATE = "LAB1-LAB2"
POLICY_TEMPLATE_ID = "tpt-1"


def _state(*, labels=None, policies=None, policy_tenant_id=TENANT_ID) -> NdoState:
    """One schema/template carrying a BD (optionally labelled), an EPG to act
    as a DHCP provider, and an L3Out + external EPG to act as the other kind.
    """
    schema_detail = {
        "id": SCHEMA_ID,
        "displayName": "dhcp-schema",
        "templates": [
            {
                "name": TEMPLATE,
                "tenantId": TENANT_ID,
                "vrfs": [{"name": "VRF1"}],
                "bds": [
                    {
                        "name": "bd-App1_LAB0",
                        "vrfRef": f"/schemas/{SCHEMA_ID}/templates/{TEMPLATE}/vrfs/VRF1",
                        "dhcpLabels": labels if labels is not None else [],
                    }
                ],
                "anps": [{"name": "app-App_LAB0", "epgs": [{"name": "epg-web"}]}],
                "externalEpgs": [
                    {
                        "name": "xepg-All_LAB0",
                        "l3outRef": f"/schemas/{SCHEMA_ID}/templates/{TEMPLATE}"
                                    "/l3outs/l3o-bgp-Core_LAB0",
                    }
                ],
            }
        ],
        "sites": [{"siteId": "1", "templateName": TEMPLATE, "bds": [], "anps": []}],
    }
    tpt = {
        POLICY_TEMPLATE_ID: {
            "templateId": POLICY_TEMPLATE_ID,
            "templateType": "tenantPolicy",
            "tenantPolicyTemplate": {
                "template": {
                    "tenantId": policy_tenant_id,
                    "dhcpRelayPolicies": policies or [],
                }
            },
        }
    }
    return NdoState(
        sites=[],
        tenants=[{"id": TENANT_ID, "name": TENANT}],
        schemas=[],
        schema_details={SCHEMA_ID: schema_detail},
        template_summaries=[],
        tenant_policy_templates=tpt,
        fabric_connectivity={},
        policy_states={},
        audit_records=[],
    )


EXTEPG_POLICY = {
    "name": "pol-dhcp_relay-Core",
    "description": "via the L3Out",
    "providers": [{"ip": "10.101.250.10", "externalEpgName": "xepg-All_LAB0"}],
}
EPG_POLICY = {
    "name": "pol-dhcp_relay-Relay1",
    "providers": [{"ip": "10.101.1.10", "epgName": "epg-web"}],
}
LABEL_CORE = [{"ref": "", "name": "pol-dhcp_relay-Core"}]


def _mirror(state):
    apic = {"1": _FakeApicState()}
    mirror_template_to_sites(state, TEMPLATE, apic, schema_id=SCHEMA_ID)
    return apic["1"].store


# ── the label itself ────────────────────────────────────────────────────────

def test_bd_label_reaches_apic():
    store = _mirror(_state(labels=LABEL_CORE, policies=[EXTEPG_POLICY]))
    lbl = store.get(
        f"uni/tn-{TENANT}/BD-bd-App1_LAB0/dhcplbl-pol-dhcp_relay-Core"
    )
    assert lbl is not None
    assert lbl.attrs["name"] == "pol-dhcp_relay-Core"
    assert lbl.attrs["owner"] == "tenant"


def test_bd_without_labels_gets_none():
    store = _mirror(_state(labels=[], policies=[EXTEPG_POLICY]))
    assert store.by_class("dhcpLbl") == []


# ── the policy the label names ──────────────────────────────────────────────

def test_referenced_policy_is_materialized_with_l3out_provider():
    store = _mirror(_state(labels=LABEL_CORE, policies=[EXTEPG_POLICY]))
    relay = store.get(f"uni/tn-{TENANT}/relayp-pol-dhcp_relay-Core")
    assert relay is not None
    assert relay.attrs["descr"] == "via the L3Out"

    provs = store.by_class("dhcpRsProv")
    assert len(provs) == 1
    # the provider names only the external EPG; the L3Out has to be resolved
    # from the schema for the dn to be usable
    assert provs[0].attrs["tDn"] == (
        f"uni/tn-{TENANT}/out-l3o-bgp-Core_LAB0/instP-xepg-All_LAB0"
    )
    assert provs[0].attrs["addr"] == "10.101.250.10"


def test_epg_provider_resolves_its_application_profile():
    labels = [{"ref": "", "name": "pol-dhcp_relay-Relay1"}]
    store = _mirror(_state(labels=labels, policies=[EPG_POLICY]))
    provs = store.by_class("dhcpRsProv")
    assert len(provs) == 1
    assert provs[0].attrs["tDn"] == (
        f"uni/tn-{TENANT}/ap-app-App_LAB0/epg-epg-web"
    )


def test_unreferenced_policy_stays_off_the_site():
    # nothing deployed the tenantPolicy template, so a policy no BD names has
    # no business on a site — only the dangling-reference case is closed
    store = _mirror(_state(labels=LABEL_CORE, policies=[EXTEPG_POLICY, EPG_POLICY]))
    names = {r.attrs["name"] for r in store.by_class("dhcpRelayP")}
    assert names == {"pol-dhcp_relay-Core"}


def test_policy_from_another_tenant_is_not_pulled_in():
    state = _state(
        labels=LABEL_CORE, policies=[EXTEPG_POLICY], policy_tenant_id="some-other-tenant"
    )
    store = _mirror(state)
    # the label still lands (it is part of the BD), but nothing resolves it
    assert store.get(
        f"uni/tn-{TENANT}/BD-bd-App1_LAB0/dhcplbl-pol-dhcp_relay-Core"
    ) is not None
    assert store.by_class("dhcpRelayP") == []


def test_provider_with_an_unresolvable_target_is_skipped_not_guessed():
    policy = {
        "name": "pol-dhcp_relay-Core",
        "providers": [{"ip": "10.0.0.1", "externalEpgName": "xepg-does-not-exist"}],
    }
    store = _mirror(_state(labels=LABEL_CORE, policies=[policy]))
    assert store.get(f"uni/tn-{TENANT}/relayp-pol-dhcp_relay-Core") is not None
    assert store.by_class("dhcpRsProv") == []


# ── lifecycle ───────────────────────────────────────────────────────────────

def test_mirror_is_idempotent():
    state = _state(labels=LABEL_CORE, policies=[EXTEPG_POLICY])
    apic = {"1": _FakeApicState()}
    mirror_template_to_sites(state, TEMPLATE, apic, schema_id=SCHEMA_ID)
    mirror_template_to_sites(state, TEMPLATE, apic, schema_id=SCHEMA_ID)
    assert len(apic["1"].store.by_class("dhcpRelayP")) == 1
    assert len(apic["1"].store.by_class("dhcpRsProv")) == 1
    assert len(apic["1"].store.by_class("dhcpLbl")) == 1


def test_undeploy_removes_the_relay_policy_it_pulled_in():
    state = _state(labels=LABEL_CORE, policies=[EXTEPG_POLICY])
    apic = {"1": _FakeApicState()}
    mirror_template_to_sites(state, TEMPLATE, apic, schema_id=SCHEMA_ID)
    assert apic["1"].store.get(f"uni/tn-{TENANT}/relayp-pol-dhcp_relay-Core")

    mirror_template_to_sites(
        state, TEMPLATE, apic, schema_id=SCHEMA_ID, undeploy=True
    )
    # deleting the BD alone would strand the policy on the site
    assert apic["1"].store.get(f"uni/tn-{TENANT}/relayp-pol-dhcp_relay-Core") is None
