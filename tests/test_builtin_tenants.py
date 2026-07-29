"""Every APIC ships three built-in tenants; the sim used to build only mgmt.

`uni/tn-common` in particular is where shared contracts/filters/L3Outs are
conventionally defined, and var files reference it by DN, so its absence was
visible to anything that queried fvTenant or resolved that DN.
"""

from aci_sim.build.orchestrator import build_all
from aci_sim.topology.loader import load_topology

TOPOLOGY = "topology.yaml"


def _stores():
    return build_all(load_topology(TOPOLOGY))


def test_common_and_infra_exist_on_every_site():
    for site, store in _stores().items():
        for name in ("common", "infra"):
            mo = store.get(f"uni/tn-{name}")
            assert mo is not None, f"{site} is missing uni/tn-{name}"
            assert mo.class_name == "fvTenant"
            assert mo.attrs["name"] == name


def test_mgmt_still_built_by_its_own_builder():
    # build/mgmt.py stays the single owner of tn-mgmt and its OOB tree — the
    # new builder must not shadow it or drop its children.
    for site, store in _stores().items():
        mgmt = store.get("uni/tn-mgmt")
        assert mgmt is not None, f"{site} lost uni/tn-mgmt"
        assert store.get("uni/tn-mgmt/mgmtp-default/oob-default") is not None


def test_all_three_builtins_present_together():
    for site, store in _stores().items():
        names = {
            mo.attrs.get("name")
            for mo in store.by_class("fvTenant")
        }
        assert {"common", "infra", "mgmt"} <= names, f"{site} has {sorted(names)}"
