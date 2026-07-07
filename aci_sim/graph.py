"""aci_sim.graph — self-contained SVG/HTML topology diagram renderer.

Renders the BUILT fabric (spines, leaves, border-leaves, controllers, the
spine<->leaf cabling mesh, vPC pairs, and — for multi-site topologies — the
ISN cloud) as a single, dependency-free SVG. No CDN, no server, no npm: the
output is plain hand-generated SVG markup, optionally wrapped in a minimal
`<html><style>...</style><body><svg>...</svg></body></html>` shell.

This is a STANDALONE Python reimplementation of the visual language used by
autoACI's Vue+cytoscape topology view (frontend/src/composables/
useTopologyStyles.js + backend/routers/topology.py) — same role->color
palette and tiered spine/leaf/border-leaf/controller hierarchy with an ISN
cloud between sites — but it imports nothing from autoACI and does not
touch this repo's schema or builders. It only reads the already-validated
`Topology` model (topology/schema.py) and re-derives the same spine<->leaf
mesh build/cabling.py's `cabling_links()` produces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from aci_sim.build.cabling import cabling_links
from aci_sim.topology.schema import Site, Topology

# ---------------------------------------------------------------------------
# Role -> color palette, matched to autoACI's useTopologyStyles.js
# ---------------------------------------------------------------------------

ROLE_COLORS: dict[str, tuple[str, str]] = {
    # role: (fill, border) — matches autoACI's cytoscape stylesheet exactly.
    "spine": ("#2563eb", "#1d4ed8"),
    "leaf": ("#16a34a", "#15803d"),
    "border-leaf": ("#16a34a", "#15803d"),  # same green family as leaf in autoACI (role="leaf")
    "controller": ("#d97706", "#b45309"),
    "isn": ("#475569", "#334155"),
}

LEGEND_ORDER = ["spine", "leaf", "border-leaf", "controller", "isn"]
LEGEND_LABELS = {
    "spine": "Spine",
    "leaf": "Leaf",
    "border-leaf": "Border Leaf",
    "controller": "Controller (APIC)",
    "isn": "ISN Cloud",
}

FONT = "Inter, -apple-system, 'Segoe UI', system-ui, sans-serif"

NODE_W = 96
NODE_H = 40
H_GAP = 130   # horizontal spacing between nodes in the same tier
V_GAP = 130   # vertical spacing between tiers
SITE_GAP = 220  # extra horizontal gap between sites
MARGIN = 60
LEGEND_H = 46
TITLE_H = 56


@dataclass
class GNode:
    id: str
    label: str
    role: str
    site: str | None
    x: float = 0.0
    y: float = 0.0


@dataclass
class GLink:
    source: str
    target: str
    kind: str = "cabling"  # cabling | isn | vpc


@dataclass
class Graph:
    nodes: list[GNode] = field(default_factory=list)
    links: list[GLink] = field(default_factory=list)
    fabric_name: str = ""
    site_count: int = 0

    def node_count(self) -> int:
        return len(self.nodes)


# ---------------------------------------------------------------------------
# Graph construction (pure data — no rendering here)
# ---------------------------------------------------------------------------


def _site_nodes(site: Site) -> tuple[list[GNode], list[GNode], list[GNode], list[GNode]]:
    """Return (spines, leaves, border_leaves, controllers) as GNode lists for a site."""
    spines = [
        GNode(id=f"n{n.id}", label=f"{n.name}\n({n.id})", role="spine", site=site.name)
        for n in site.spine_nodes()
    ]
    leaves = [
        GNode(id=f"n{n.id}", label=f"{n.name}\n({n.id})", role="leaf", site=site.name)
        for n in site.leaf_nodes()
    ]
    border_leaves = [
        GNode(id=f"n{bl.id}", label=f"{bl.name}\n({bl.id})", role="border-leaf", site=site.name)
        for bl in site.border_leaves
    ]
    controllers = [
        GNode(
            id=f"ctrl-{site.name}-{i + 1}",
            label=f"APIC{i + 1}\n{site.name}",
            role="controller",
            site=site.name,
        )
        for i in range(site.controllers)
    ]
    return spines, leaves, border_leaves, controllers


def build_graph(topo: Topology) -> Graph:
    """Derive the node/link graph from a validated Topology.

    Node IDs use the fabric's real node id (`n{id}`) for spines/leaves/
    border-leaves, so cabling links (which are keyed by real node id) join up
    directly; controllers get a synthetic id since they have no fabricLink.
    """
    graph = Graph(fabric_name=topo.fabric.name, site_count=len(topo.sites))

    isn_enabled = topo.isn.enabled and len(topo.sites) > 1
    isn_node = GNode(id="isn-cloud", label="ISN", role="isn", site=None) if isn_enabled else None

    for site in topo.sites:
        spines, leaves, border_leaves, controllers = _site_nodes(site)
        graph.nodes.extend(spines + leaves + border_leaves + controllers)

        # Spine<->leaf/border-leaf cabling mesh — reuse the exact same
        # derivation build/cabling.py's fabricLink builder uses, so the
        # diagram always matches the built fabric's real cabling graph.
        for n1, _s1, _p1, n2, _s2, _p2 in cabling_links(site):
            graph.links.append(GLink(source=f"n{n1}", target=f"n{n2}", kind="cabling"))

        # vPC pairs: border leaves sharing vpc_domain get a dashed peer-link
        # for visual grouping (mirrors autoACI's edge.vpc-link).
        by_domain: dict[str, list[int]] = {}
        for bl in site.border_leaves:
            by_domain.setdefault(bl.vpc_domain, []).append(bl.id)
        for ids in by_domain.values():
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    graph.links.append(GLink(source=f"n{ids[i]}", target=f"n{ids[j]}", kind="vpc"))

        # Spine -> ISN cloud uplinks (autoACI: role="isn" node with spine
        # source links, backend/routers/topology.py isn_connections).
        if isn_node is not None:
            for spine in spines:
                graph.links.append(GLink(source=spine.id, target=isn_node.id, kind="isn"))

    if isn_node is not None:
        graph.nodes.append(isn_node)

    _layout(graph, topo)
    return graph


def _layout(graph: Graph, topo: Topology) -> None:
    """Hand-computed tiered ACI hierarchy layout (mirrors autoACI's preset layout).

    Tiers (top to bottom): ISN cloud (multi-site only) -> spine -> leaf ->
    border-leaf -> controller. Sites are spread side by side; each tier's
    nodes are centered within their site's horizontal band.
    """
    site_names = [s.name for s in topo.sites]
    multi_site = len(site_names) > 1

    by_site: dict[str, dict[str, list[GNode]]] = {
        name: {"spine": [], "leaf": [], "border-leaf": [], "controller": []} for name in site_names
    }
    isn_node = None
    for n in graph.nodes:
        if n.role == "isn":
            isn_node = n
            continue
        by_site[n.site][n.role].append(n)

    def place_row(nodes: list[GNode], site_x: float, y: float) -> None:
        if not nodes:
            return
        width = (len(nodes) - 1) * H_GAP
        start_x = site_x - width / 2
        for i, n in enumerate(nodes):
            n.x = start_x + i * H_GAP
            n.y = y

    y_isn = 0.0
    y_spine = V_GAP * 1 if not multi_site else V_GAP * 1.6
    y_leaf = y_spine + V_GAP
    y_border = y_leaf + V_GAP
    y_ctrl = y_border + V_GAP

    for idx, name in enumerate(site_names):
        site_x = idx * SITE_GAP + idx * H_GAP * 2  # extra spread accounts for wide tiers
        tiers = by_site[name]
        # Recompute site_x based on the widest tier so sites don't overlap.
        place_row(tiers["spine"], site_x, y_spine)
        place_row(tiers["leaf"], site_x, y_leaf)
        place_row(tiers["border-leaf"], site_x, y_border)
        place_row(tiers["controller"], site_x, y_ctrl)

    # Second pass: re-center each site block using the actual widest tier so
    # multi-site layouts don't visually collide when leaf counts differ.
    site_extents: dict[str, tuple[float, float]] = {}
    for name in site_names:
        tiers = by_site[name]
        xs = [n.x for row in tiers.values() for n in row]
        if xs:
            site_extents[name] = (min(xs) - NODE_W, max(xs) + NODE_W)
        else:
            site_extents[name] = (0.0, 0.0)

    cursor = 0.0
    for name in site_names:
        lo, hi = site_extents[name]
        shift = cursor - lo
        for row in by_site[name].values():
            for n in row:
                n.x += shift
        cursor = hi + shift + SITE_GAP

    if isn_node is not None:
        all_x = [n.x for name in site_names for row in by_site[name].values() for n in row]
        isn_node.x = (min(all_x) + max(all_x)) / 2 if all_x else 0.0
        isn_node.y = y_isn


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _wrap_label(label: str) -> list[str]:
    return label.split("\n")


def _node_svg(n: GNode) -> str:
    fill, border = ROLE_COLORS.get(n.role, ("#64748b", "#334155"))
    x = n.x - NODE_W / 2
    y = n.y - NODE_H / 2
    lines = _wrap_label(n.label)
    text_parts = []
    line_h = 13
    start_y = n.y - (len(lines) - 1) * line_h / 2
    for i, line in enumerate(lines):
        text_parts.append(
            f'<text x="{n.x:.1f}" y="{start_y + i * line_h:.1f}" text-anchor="middle" '
            f'dominant-baseline="middle" class="node-label">{escape(line)}</text>'
        )
    return (
        f'<g class="node node-{n.role}" data-id="{escape(n.id)}">'
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{NODE_W}" height="{NODE_H}" rx="8" ry="8" '
        f'fill="{fill}" stroke="{border}" stroke-width="1.5" class="node-shadow"/>'
        + "".join(text_parts)
        + "</g>"
    )


def _link_svg(link: GLink, pos: dict[str, GNode]) -> str:
    a = pos.get(link.source)
    b = pos.get(link.target)
    if a is None or b is None:
        return ""
    cls = {
        "cabling": "link-cabling",
        "isn": "link-isn",
        "vpc": "link-vpc",
    }.get(link.kind, "link-cabling")
    return f'<line x1="{a.x:.1f}" y1="{a.y:.1f}" x2="{b.x:.1f}" y2="{b.y:.1f}" class="{cls}"/>'


def _legend_svg(x: float, y: float, roles_present: list[str]) -> str:
    parts = [f'<g class="legend" transform="translate({x:.1f},{y:.1f})">']
    swatch = 14
    gap = 150
    for i, role in enumerate(roles_present):
        fill, border = ROLE_COLORS[role]
        lx = i * gap
        parts.append(
            f'<rect x="{lx}" y="0" width="{swatch}" height="{swatch}" rx="3" ry="3" '
            f'fill="{fill}" stroke="{border}" stroke-width="1"/>'
            f'<text x="{lx + swatch + 6}" y="{swatch - 2}" class="legend-label">{escape(LEGEND_LABELS[role])}</text>'
        )
    # Link-type samples (mirrors autoACI's topology legend): fabric links,
    # solid ISN uplink, dashed vPC peer-link.
    lx = len(roles_present) * gap + 20
    line_entries = [("link-cabling", "Fabric link"), ("link-isn", "ISN uplink"), ("link-vpc", "vPC peer-link")]
    for cls, label in line_entries:
        parts.append(
            f'<line x1="{lx}" y1="{swatch / 2:.0f}" x2="{lx + 26}" y2="{swatch / 2:.0f}" class="{cls}"/>'
            f'<text x="{lx + 32}" y="{swatch - 2}" class="legend-label">{escape(label)}</text>'
        )
        lx += 130
    parts.append("</g>")
    return "".join(parts)


def _links_table_svg(topo: Topology, x: float, y: float) -> tuple[str, int]:
    """Physical-links table under the diagram (same logic as autoACI's
    Topology "Fabric (physical)" session table): one row per cabling link and
    per spine ISN uplink, with the /31 point-to-point IPs on the ISN rows."""
    from aci_sim.build.cabling import cabling_links

    cols = [("SITE", 0), ("NODE", 110), ("PORT", 260), ("NEIGHBOR", 350), ("NBR PORT", 500), ("LOCAL IP", 610), ("REMOTE IP", 730)]
    row_h = 15
    rows: list[tuple[str, ...]] = []
    for site in topo.sites:
        names = {n.id: n.name for n in site.all_nodes()}
        for n1, _s1, p1, n2, _s2, p2 in cabling_links(site):
            rows.append((site.name, names.get(n1, str(n1)), f"eth1/{p1}", names.get(n2, str(n2)), f"eth1/{p2}", "\u2014", "\u2014"))
    if topo.isn.enabled and len(topo.sites) > 1:
        for site in topo.sites:
            for si, spine in enumerate(site.spine_nodes()):
                rows.append((
                    site.name, spine.name, f"eth1/{49 + si}.{49 + si}",
                    f"ISN-CSW{site.id}", f"Ethernet1/{si + 1}.4",
                    f"172.16.{site.id}.{2 * si}/31", f"172.16.{site.id}.{2 * si + 1}",
                ))
    parts = [f'<g transform="translate({x:.1f},{y:.1f})">',
             '<text x="0" y="0" class="tbl-title">Physical links</text>']
    hy = 18
    for label, cx in cols:
        parts.append(f'<text x="{cx}" y="{hy}" class="tbl-head">{escape(label)}</text>')
    for i, row in enumerate(rows):
        ry = hy + (i + 1) * row_h
        for (label, cx), val in zip(cols, row):
            parts.append(f'<text x="{cx}" y="{ry}" class="tbl-cell">{escape(val)}</text>')
    parts.append("</g>")
    height = hy + (len(rows) + 1) * row_h + 10
    return "".join(parts), height


def _svg_style() -> str:
    return (
        "<style>"
        f".node-label {{ font-family: {FONT}; font-size: 9px; font-weight: 600; fill: #ffffff; }}"
        f".legend-label {{ font-family: {FONT}; font-size: 11px; fill: #334155; }}"
        f".title-text {{ font-family: {FONT}; font-size: 16px; font-weight: 700; fill: #0f172a; }}"
        f".subtitle-text {{ font-family: {FONT}; font-size: 11px; fill: #64748b; }}"
        ".node-shadow { filter: drop-shadow(0 1px 2px rgba(0,0,0,0.25)); }"
        ".link-cabling { stroke: #d1d5db; stroke-width: 1.5; opacity: 0.8; }"
        ".link-isn { stroke: #f59e0b; stroke-width: 2; opacity: 0.9; }"
        ".link-vpc { stroke: #94a3b8; stroke-width: 1.5; stroke-dasharray: 2,3; opacity: 0.7; }"
        ".site-label { font-family: " + FONT + "; font-size: 12px; font-weight: 600; fill: #64748b; }"
        + ".tbl-head { font-family: " + FONT + "; font-size: 10px; font-weight: 700; fill: #475569; }"
        + ".tbl-cell { font-family: " + FONT + "; font-size: 10px; fill: #334155; }"
        + ".tbl-title { font-family: " + FONT + "; font-size: 12px; font-weight: 700; fill: #0f172a; }"
        "</style>"
    )


def _svg_body(graph: Graph, topo: Topology) -> tuple[str, int, int]:
    pos = {n.id: n for n in graph.nodes}
    if graph.nodes:
        min_x = min(n.x for n in graph.nodes) - NODE_W
        max_x = max(n.x for n in graph.nodes) + NODE_W
        min_y = min(n.y for n in graph.nodes) - NODE_H
        max_y = max(n.y for n in graph.nodes) + NODE_H
    else:
        min_x = min_y = 0.0
        max_x = max_y = 0.0

    width = int(max_x - min_x + 2 * MARGIN)
    # A narrow diagram (few nodes) must still fit the single-row legend
    # (role swatches at 150px pitch + three link-type samples at 130px) —
    # otherwise the ISN-uplink/vPC legend entries clip off the right edge.
    _roles_n = len([r for r in LEGEND_ORDER if any(n.role == r for n in graph.nodes)])
    width = max(width, int(2 * MARGIN + _roles_n * 150 + 20 + 3 * 130))
    height = int(max_y - min_y + 2 * MARGIN + TITLE_H + LEGEND_H)

    # Shift everything so the drawing starts at (MARGIN, MARGIN + TITLE_H).
    offset_x = MARGIN - min_x
    offset_y = MARGIN + TITLE_H - min_y
    for n in graph.nodes:
        n.x += offset_x
        n.y += offset_y

    roles_present = [r for r in LEGEND_ORDER if any(n.role == r for n in graph.nodes)]

    links_svg = "".join(_link_svg(link, pos) for link in graph.links)
    nodes_svg = "".join(_node_svg(n) for n in graph.nodes)

    node_count = graph.node_count()
    subtitle = f"{graph.site_count} site(s), {node_count} node(s)"
    title_svg = (
        f'<text x="{MARGIN}" y="28" class="title-text">{escape(graph.fabric_name)} — Topology</text>'
        f'<text x="{MARGIN}" y="46" class="subtitle-text">{escape(subtitle)}</text>'
    )

    legend_y = height - LEGEND_H + 16
    legend_svg = _legend_svg(MARGIN, legend_y, roles_present)

    table_svg, table_h = _links_table_svg(topo, MARGIN, height + 6)
    height += table_h

    body = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" font-family="{FONT}">'
        + _svg_style()
        + f'<rect x="0" y="0" width="{width}" height="{height}" fill="#f8fafc"/>'
        + title_svg
        + links_svg
        + nodes_svg
        + legend_svg
        + table_svg
        + "</svg>"
    )
    return body, width, height


def render_topology(topo: Topology, fmt: str = "html") -> str:
    """Render *topo* as a self-contained SVG or HTML string.

    fmt="svg"  -> raw `<svg>...</svg>` markup.
    fmt="html" -> `<html><body><svg>...</svg></body></html>` with inline CSS.

    No network access, no external assets — the returned string is fully
    self-contained and safe to open directly in any browser.
    """
    graph = build_graph(topo)
    svg, width, _height = _svg_body(graph, topo)

    if fmt == "svg":
        return svg

    if fmt != "html":
        raise ValueError(f"Unsupported format {fmt!r}; expected 'svg' or 'html'")

    title = escape(f"{topo.fabric.name} — Topology")
    return (
        f'<html lang="en"><head><meta charset="utf-8"/><title>{title}</title>'
        "<style>"
        "body { margin: 0; padding: 24px; background: #eef2f7; "
        "font-family: -apple-system, 'Segoe UI', system-ui, sans-serif; }"
        f"svg {{ display: block; margin: 0 auto; max-width: 100%; height: auto; background: #f8fafc; "
        "border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.12); }"
        "</style>"
        f"</head><body>{svg}</body></html>"
    )
