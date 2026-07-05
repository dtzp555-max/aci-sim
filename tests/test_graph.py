"""Tests for aci_sim/graph.py — self-contained SVG/HTML topology diagram.

Covers `render_topology()` directly (node counts, ISN cloud presence/absence,
role colors, well-formed XML) plus the `aci-sim graph` CLI subcommand. No
browser is required anywhere — every assertion works on the raw string
output.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from aci_sim.cli import generate_topology, main
from aci_sim.graph import ROLE_COLORS, build_graph, render_topology
from aci_sim.topology.loader import load_topology
from aci_sim.topology.schema import Topology


def _new_topology(sites: int) -> Topology:
    """Build a fresh N-site Topology via cli.generate_topology() (returns a dict)."""
    raw = generate_topology(sites=sites)
    return Topology.model_validate(raw)


REPO_ROOT = Path(__file__).parent.parent
TOPOLOGY_YAML = REPO_ROOT / "topology.yaml"


def _total_node_count(topo) -> int:
    total = 0
    for site in topo.sites:
        total += len(site.spine_nodes()) + len(site.leaf_nodes()) + len(site.border_leaves)
        total += site.controllers
    return total


def _svg_fragment(html: str) -> str:
    m = re.search(r"<svg.*</svg>", html, re.S)
    assert m, "no <svg>...</svg> fragment found"
    return m.group(0)


class TestRenderTopologyRepoDefault:
    """The repo's default topology.yaml is a 2-site, ISN-enabled fabric."""

    def test_html_contains_svg(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        html = render_topology(topo, fmt="html")
        assert "<svg" in html
        assert html.startswith("<html")

    def test_svg_format_starts_with_svg_tag(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        svg = render_topology(topo, fmt="svg")
        assert svg.startswith("<svg")
        assert "</svg>" in svg

    def test_node_rect_count_matches_topology(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        svg = render_topology(topo, fmt="svg")
        node_groups = re.findall(r'<g class="node node-[a-z-]+"', svg)
        expected = _total_node_count(topo)  # ISN cloud is the +1 (checked separately)
        assert len(node_groups) == expected + 1  # +1 for the ISN cloud node

    def test_isn_cloud_present_for_multisite(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        assert topo.isn.enabled and len(topo.sites) > 1
        svg = render_topology(topo, fmt="svg")
        assert 'class="node node-isn"' in svg
        assert "ISN" in svg

    def test_role_colors_present(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        svg = render_topology(topo, fmt="svg")
        for role in ("spine", "leaf", "border-leaf", "controller", "isn"):
            fill, _border = ROLE_COLORS[role]
            assert fill in svg, f"expected {role} fill color {fill} in output"

    def test_output_is_well_formed_xml(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        svg = render_topology(topo, fmt="svg")
        ET.fromstring(svg)  # raises if malformed

    def test_html_output_is_well_formed_xml_fragment(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        html = render_topology(topo, fmt="html")
        ET.fromstring(_svg_fragment(html))

    def test_legend_present(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        svg = render_topology(topo, fmt="svg")
        assert 'class="legend"' in svg
        assert "Spine" in svg and "Leaf" in svg and "Border Leaf" in svg

    def test_title_contains_fabric_name_and_site_count(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        svg = render_topology(topo, fmt="svg")
        assert topo.fabric.name in svg
        assert f"{len(topo.sites)} site(s)" in svg


class TestSingleFabricNoIsn:
    def test_single_site_omits_isn_cloud(self) -> None:
        single = _new_topology(sites=1)
        assert len(single.sites) == 1  # sanity: no ISN possible with 1 site
        svg = render_topology(single, fmt="svg")
        assert 'class="node node-isn"' not in svg
        ET.fromstring(svg)


class TestThreeSiteTopology:
    def test_three_sites_all_render(self) -> None:
        topo = _new_topology(sites=3)
        assert len(topo.sites) == 3
        svg = render_topology(topo, fmt="svg")
        ET.fromstring(svg)
        for site in topo.sites:
            assert site.name in svg
        node_groups = re.findall(r'<g class="node node-[a-z-]+"', svg)
        assert len(node_groups) == _total_node_count(topo) + 1  # +1 ISN cloud
        assert 'class="node node-isn"' in svg


class TestBuildGraph:
    def test_build_graph_node_and_link_counts(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        graph = build_graph(topo)
        assert graph.node_count() == _total_node_count(topo) + 1  # + ISN cloud
        assert graph.site_count == len(topo.sites)
        # every link references a node that exists in the graph
        node_ids = {n.id for n in graph.nodes}
        for link in graph.links:
            assert link.source in node_ids
            assert link.target in node_ids

    def test_vpc_pair_links_present(self) -> None:
        topo = load_topology(TOPOLOGY_YAML)
        graph = build_graph(topo)
        vpc_links = [link for link in graph.links if link.kind == "vpc"]
        assert len(vpc_links) >= 1  # LAB1 + LAB2 each have one vPC pair


class TestGraphCLI:
    def test_cli_graph_writes_html_file(self, tmp_path, capsys) -> None:
        out_file = tmp_path / "g.html"
        rc = main(["graph", str(TOPOLOGY_YAML), "-o", str(out_file)])
        out = capsys.readouterr().out
        assert rc == 0
        assert out_file.exists()
        content = out_file.read_text(encoding="utf-8")
        assert content.startswith("<html")
        assert "<svg" in content
        assert str(out_file) in out

    def test_cli_graph_writes_svg_file(self, tmp_path) -> None:
        out_file = tmp_path / "g.svg"
        rc = main(["graph", str(TOPOLOGY_YAML), "-o", str(out_file)])
        assert rc == 0
        content = out_file.read_text(encoding="utf-8")
        assert content.startswith("<svg")

    def test_cli_graph_default_topology_and_output(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(REPO_ROOT)
        out_file = REPO_ROOT / "topology.html"
        try:
            rc = main(["graph", "-o", str(tmp_path / "default_out.html")])
            assert rc == 0
        finally:
            if out_file.exists():
                out_file.unlink()

    def test_cli_graph_unknown_extension_errors(self, tmp_path, capsys) -> None:
        out_file = tmp_path / "g.png"
        rc = main(["graph", str(TOPOLOGY_YAML), "-o", str(out_file)])
        err = capsys.readouterr().err
        assert rc != 0
        assert "ERROR" in err

    def test_cli_graph_missing_topology_errors(self, tmp_path, capsys) -> None:
        rc = main(["graph", "/nonexistent/topology.yaml", "-o", str(tmp_path / "g.html")])
        err = capsys.readouterr().err
        assert rc != 0
        assert "ERROR" in err
