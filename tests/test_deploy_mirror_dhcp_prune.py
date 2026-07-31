"""Unbinding a DHCP label in NDO has to reach the site.

`store.upsert` merges and never drops children the new MO omits — on purpose,
so an externally-POSTed epClear survives a redeploy. The cost was that clearing
a BD's dhcpLabels and redeploying left the dhcpLbl and its dhcpRelayP on both
APICs forever.

The relay policy is pruned by what the SITE still references, not by what a
template declares, so a policy another deployed template's BD still labels
survives.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from aci_sim.mit.store import MITStore
from aci_sim.ndo.deploy_mirror import mirror_template_to_sites
from aci_sim.ndo.model import NdoState


@dataclass
class _Apic:
    store: MITStore = field(default_factory=MITStore)


TENANT_ID, TENANT, SCHEMA = "tid", "T", "sch"
POLICIES = [
    {"name": "pol-A", "providers": []},
    {"name": "pol-B", "providers": []},
]


def _state(bds_by_template: dict[str, list[dict]]):
    """One schema, one site, a template per entry — each with its own BDs."""
    templates, sites = [], []
    for tname, bds in bds_by_template.items():
        templates.append({
            "name": tname, "tenantId": TENANT_ID, "vrfs": [], "anps": [],
            "externalEpgs": [], "bds": bds,
        })
        sites.append({"siteId": "1", "templateName": tname, "bds": [], "anps": []})
    return NdoState(
        sites=[], tenants=[{"id": TENANT_ID, "name": TENANT}], schemas=[],
        schema_details={SCHEMA: {"id": SCHEMA, "templates": templates, "sites": sites}},
        template_summaries=[],
        tenant_policy_templates={"t1": {"tenantPolicyTemplate": {"template": {
            "tenantId": TENANT_ID, "dhcpRelayPolicies": POLICIES}}}},
        fabric_connectivity={}, policy_states={}, audit_records=[],
    )


def _bd(name, *labels):
    return {"name": name, "dhcpLabels": [{"ref": "", "name": n} for n in labels]}


def _dns(store, cls):
    return sorted(m.dn for m in store.by_class(cls))


def test_clearing_the_label_removes_it_from_the_site():
    apic = {"1": _Apic()}
    bound = _state({"T1": [_bd("bd-App1", "pol-A")]})
    mirror_template_to_sites(bound, "T1", apic, schema_id=SCHEMA)
    assert _dns(apic["1"].store, "dhcpLbl") == ["uni/tn-T/BD-bd-App1/dhcplbl-pol-A"]

    cleared = _state({"T1": [_bd("bd-App1")]})
    mirror_template_to_sites(cleared, "T1", apic, schema_id=SCHEMA)
    assert _dns(apic["1"].store, "dhcpLbl") == []
    assert _dns(apic["1"].store, "dhcpRelayP") == []


def test_swapping_one_label_for_another_leaves_only_the_new_one():
    apic = {"1": _Apic()}
    mirror_template_to_sites(_state({"T1": [_bd("bd-App1", "pol-A")]}),
                             "T1", apic, schema_id=SCHEMA)
    mirror_template_to_sites(_state({"T1": [_bd("bd-App1", "pol-B")]}),
                             "T1", apic, schema_id=SCHEMA)
    assert _dns(apic["1"].store, "dhcpLbl") == ["uni/tn-T/BD-bd-App1/dhcplbl-pol-B"]
    assert _dns(apic["1"].store, "dhcpRelayP") == ["uni/tn-T/relayp-pol-B"]


def test_a_policy_another_bd_still_labels_survives():
    """The reason pruning keys on site state and not on the template: deleting
    relayp-<name> because THIS template stopped naming it would take out a
    policy a different deployed template's BD still points at."""
    apic = {"1": _Apic()}
    both = _state({"T1": [_bd("bd-App1", "pol-A")], "T2": [_bd("bd-App2", "pol-A")]})
    mirror_template_to_sites(both, "T1", apic, schema_id=SCHEMA)
    mirror_template_to_sites(both, "T2", apic, schema_id=SCHEMA)
    assert _dns(apic["1"].store, "dhcpRelayP") == ["uni/tn-T/relayp-pol-A"]

    # T1 drops its label; T2 still carries one for the same policy
    dropped = _state({"T1": [_bd("bd-App1")], "T2": [_bd("bd-App2", "pol-A")]})
    mirror_template_to_sites(dropped, "T1", apic, schema_id=SCHEMA)

    assert _dns(apic["1"].store, "dhcpLbl") == ["uni/tn-T/BD-bd-App2/dhcplbl-pol-A"]
    assert _dns(apic["1"].store, "dhcpRelayP") == ["uni/tn-T/relayp-pol-A"]


def test_undeploy_takes_the_policy_with_the_bd_that_referenced_it():
    apic = {"1": _Apic()}
    st = _state({"T1": [_bd("bd-App1", "pol-A")]})
    mirror_template_to_sites(st, "T1", apic, schema_id=SCHEMA)
    mirror_template_to_sites(st, "T1", apic, schema_id=SCHEMA, undeploy=True)
    assert _dns(apic["1"].store, "dhcpRelayP") == []
    assert _dns(apic["1"].store, "dhcpLbl") == []


def test_undeploy_leaves_a_policy_another_template_still_uses():
    apic = {"1": _Apic()}
    both = _state({"T1": [_bd("bd-App1", "pol-A")], "T2": [_bd("bd-App2", "pol-A")]})
    mirror_template_to_sites(both, "T1", apic, schema_id=SCHEMA)
    mirror_template_to_sites(both, "T2", apic, schema_id=SCHEMA)

    mirror_template_to_sites(both, "T1", apic, schema_id=SCHEMA, undeploy=True)
    assert _dns(apic["1"].store, "dhcpRelayP") == ["uni/tn-T/relayp-pol-A"]
    assert _dns(apic["1"].store, "dhcpLbl") == ["uni/tn-T/BD-bd-App2/dhcplbl-pol-A"]


def test_a_redeploy_that_changes_nothing_changes_nothing():
    apic = {"1": _Apic()}
    st = _state({"T1": [_bd("bd-App1", "pol-A")]})
    mirror_template_to_sites(st, "T1", apic, schema_id=SCHEMA)
    before = (_dns(apic["1"].store, "dhcpLbl"), _dns(apic["1"].store, "dhcpRelayP"))
    mirror_template_to_sites(st, "T1", apic, schema_id=SCHEMA)
    assert (_dns(apic["1"].store, "dhcpLbl"), _dns(apic["1"].store, "dhcpRelayP")) == before
