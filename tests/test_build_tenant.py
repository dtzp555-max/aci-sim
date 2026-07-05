"""Tests for Phase 4 tenant-side builders.

Load topology.yaml, build SiteA into a fresh MITStore, then assert:
  1. Tenant / VRF / BD / EPG presence; SiteB-Local absent; SiteA-Local present.
  2. Every fvRsBd.tnFvBDName references a real fvBD.
  3. Endpoints: parent EPG real; fvIp inside BD subnet; fabricPathDn leaf in SiteA.
  4. L3Out SiteA present, SiteB absent; bgpPeerP and bgpPeerEntry established.
  5. Contracts/filters cross-refs valid; each vzFilter has ≥1 vzEntry.
"""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import pytest

from aci_sim.build import access, endpoints, l3out, tenants
from aci_sim.mit.dn import parent_dn
from aci_sim.mit.store import MITStore
from aci_sim.topology.loader import load_topology

TOPOLOGY_YAML = Path(__file__).parent.parent / "topology.yaml"


@pytest.fixture(scope="module")
def built():
    topo = load_topology(TOPOLOGY_YAML)
    site_a = topo.site_by_name("LAB1")
    store = MITStore()
    tenants.build(topo, site_a, store)
    access.build(topo, site_a, store)
    l3out.build(topo, site_a, store)
    endpoints.build(topo, site_a, store)
    return store, topo, site_a


# ---------------------------------------------------------------------------
# 1. Tenant / VRF / BD / EPG presence
# ---------------------------------------------------------------------------

class TestTenantPresence:
    def test_corp_tenant_exists(self, built) -> None:
        store, _, _ = built
        assert store.get("uni/tn-MS-TN1") is not None

    def test_corp_vrf_exists(self, built) -> None:
        store, _, _ = built
        assert store.get("uni/tn-MS-TN1/ctx-vrf-L3_LAB0") is not None

    def test_corp_bd_app_bd_exists(self, built) -> None:
        store, _, _ = built
        assert store.get("uni/tn-MS-TN1/BD-bd-BD3_LAB0") is not None

    def test_corp_bd_db_bd_exists(self, built) -> None:
        store, _, _ = built
        assert store.get("uni/tn-MS-TN1/BD-bd-BD4_LAB0") is not None

    def test_corp_epg_web_exists(self, built) -> None:
        store, _, _ = built
        assert store.get("uni/tn-MS-TN1/ap-app-Stretched_Apps_LAB0/epg-epg-APP3_Stretched_BD") is not None

    def test_corp_epg_db_exists(self, built) -> None:
        store, _, _ = built
        assert store.get("uni/tn-MS-TN1/ap-app-Stretched_Apps_LAB0/epg-epg-APP4_Stretched_BD") is not None

    def test_siteb_local_absent(self, built) -> None:
        store, _, _ = built
        assert store.get("uni/tn-SF-TN1-LAB2") is None

    def test_sitea_local_present(self, built) -> None:
        store, _, _ = built
        assert store.get("uni/tn-SF-TN1-LAB1") is not None


# ---------------------------------------------------------------------------
# 2. fvRsBd cross-references
# ---------------------------------------------------------------------------

class TestFvRsBdRefs:
    def test_all_rsbds_reference_real_bds(self, built) -> None:
        store, _, _ = built
        bd_names = {mo.attrs["name"] for mo in store.by_class("fvBD")}
        for rsbd in store.by_class("fvRsBd"):
            assert rsbd.attrs["tnFvBDName"] in bd_names, (
                f"fvRsBd {rsbd.dn!r} references BD "
                f"{rsbd.attrs['tnFvBDName']!r} not in store"
            )


# ---------------------------------------------------------------------------
# 3. Endpoints
# ---------------------------------------------------------------------------

class TestEndpoints:
    def test_at_least_one_fvcep(self, built) -> None:
        store, _, _ = built
        assert len(store.by_class("fvCEp")) >= 1

    def test_cep_parent_is_real_epg(self, built) -> None:
        store, _, _ = built
        for cep in store.by_class("fvCEp"):
            p_dn = parent_dn(cep.dn)
            assert store.get(p_dn) is not None, (
                f"fvCEp {cep.dn!r} parent {p_dn!r} not in store"
            )

    def test_cep_has_fvip_inside_bd_subnet(self, built) -> None:
        store, _, _ = built
        for cep in store.by_class("fvCEp"):
            fvips = store.children(cep.dn, {"fvIp"})
            assert fvips, f"fvCEp {cep.dn!r} has no fvIp child"

            epg_dn = parent_dn(cep.dn)
            rsbds = store.children(epg_dn, {"fvRsBd"})
            assert rsbds, f"EPG {epg_dn!r} has no fvRsBd"
            bd_name = rsbds[0].attrs.get("tnFvBDName", "")

            bd_mo = next(
                (m for m in store.by_class("fvBD") if m.attrs.get("name") == bd_name),
                None,
            )
            assert bd_mo is not None, f"BD {bd_name!r} not found for {cep.dn!r}"

            subnet_mos = store.children(bd_mo.dn, {"fvSubnet"})
            assert subnet_mos, f"BD {bd_mo.dn!r} has no fvSubnet"

            for fvip in fvips:
                ip_str = fvip.attrs.get("addr", "")
                in_net = any(
                    ipaddress.ip_address(ip_str)
                    in ipaddress.ip_network(sm.attrs.get("ip", "0.0.0.0/0"), strict=False)
                    for sm in subnet_mos
                )
                assert in_net, (
                    f"fvIp {ip_str!r} not inside any BD subnet for {cep.dn!r}"
                )

    def test_cep_fabricpathdn_uses_sitea_leaf(self, built) -> None:
        """Every fvCEp's fabricPathDn must reference a node that really exists
        in SiteA: either a real leaf (HOME endpoint, front-panel port) or a
        real spine (REMOTE/stretched endpoint, learned via the multi-site
        overlay tunnel that terminates on a local spine — see FIX 4 /
        docs/DESIGN.md). It must never reference a node id from the OTHER
        site (that was the finding #16 bug: identical local-looking fvCEp on
        both sites)."""
        store, _, site_a = built
        leaf_ids = {n.id for n in site_a.leaf_nodes()}
        spine_ids = {n.id for n in site_a.spine_nodes()}
        for cep in store.by_class("fvCEp"):
            path = cep.attrs.get("fabricPathDn", "")
            m = re.search(r"paths-(\d+)", path)
            assert m is not None, f"Cannot parse leafId from {path!r}"
            nid = int(m.group(1))
            assert nid in leaf_ids or nid in spine_ids, (
                f"fvCEp {cep.dn!r} uses node {nid} not in SiteA "
                f"leaves={leaf_ids} spines={spine_ids}"
            )


# ---------------------------------------------------------------------------
# 4. L3Out
# ---------------------------------------------------------------------------

class TestL3Out:
    def test_l3out_sitea_exists(self, built) -> None:
        store, _, _ = built
        assert store.get("uni/tn-MS-TN1/out-l3o-bgp-Core_LAB1") is not None

    def test_l3out_siteb_absent(self, built) -> None:
        store, _, _ = built
        assert store.get("uni/tn-MS-TN1/out-l3o-bgp-Core_LAB2") is None

    def test_bgp_peer_p_with_csw_a_ip(self, built) -> None:
        store, _, _ = built
        peers = store.by_class("bgpPeerP")
        assert any(p.attrs.get("addr") == "10.100.0.1" for p in peers), (
            "No bgpPeerP with addr=10.100.0.1 found"
        )

    def test_bgp_peer_entry_established_on_border_leaf(self, built) -> None:
        store, _, _ = built
        entries = store.by_class("bgpPeerEntry")
        matching = [
            e for e in entries
            if e.attrs.get("operSt") == "established"
            and e.attrs.get("addr") == "10.100.0.1"
            and ("node-103" in e.dn or "node-104" in e.dn)
        ]
        assert matching, (
            "No bgpPeerEntry with operSt=established, addr=10.100.0.1 on node-103/104"
        )


# ---------------------------------------------------------------------------
# 5. Contracts / Filters
# ---------------------------------------------------------------------------

class TestContractsFilters:
    def test_vzbrcp_web_to_db_exists(self, built) -> None:
        store, _, _ = built
        assert store.get("uni/tn-MS-TN1/brc-con-Web_to_DB_LAB0") is not None

    def test_rssubjfiltatt_refs_real_filter(self, built) -> None:
        store, _, _ = built
        flt_names = {mo.attrs["name"] for mo in store.by_class("vzFilter")}
        for rs in store.by_class("vzRsSubjFiltAtt"):
            assert rs.attrs["tnVzFilterName"] in flt_names, (
                f"vzRsSubjFiltAtt {rs.dn!r} refs filter "
                f"{rs.attrs['tnVzFilterName']!r} not in store"
            )

    def test_each_vzfilter_has_vzentry(self, built) -> None:
        store, _, _ = built
        filters = store.by_class("vzFilter")
        assert filters, "No vzFilter MOs in store"
        for flt in filters:
            entries = store.children(flt.dn, {"vzEntry"})
            assert entries, f"vzFilter {flt.dn!r} has no vzEntry children"
