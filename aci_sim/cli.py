"""aci-sim — CLI for authoring, validating, previewing, and running topology.yaml.

`topology.yaml` (loaded via `aci_sim.topology.loader.load_topology`,
validated by the Pydantic v2 models in `aci_sim.topology.schema`)
remains the single source of truth for the whole simulated fabric — this CLI
is purely a convenience layer around it. It does NOT change the config
format, the schema, the builders, or the NDO/REST code; it only:

  validate  — run the same load_topology()/Pydantic validation CI already
              depends on, with a friendlier pass/fail summary.
  show      — print the DERIVED inventory (node IDs, loopback/OOB IPs, port
              bindings) a user needs before pointing a client at the sim.
  run       — a thin wrapper around `python -m aci_sim.runtime.supervisor`
              that points TOPOLOGY_PATH at the given file.
  new       — scaffold a minimal, valid topology.yaml from a handful of flags,
              using the same node-ID scheme documented in topology/schema.py.
  graph     — render a self-contained SVG/HTML topology diagram (aci_sim.graph),
              openable directly in a browser: no server, no CDN, no npm.

No sockets are bound directly by this module (that stays supervisor.py's
job); `run` only builds an environment + argv and execs/spawns the
supervisor, which keeps the CLI itself trivially testable.
"""

from __future__ import annotations

import argparse
import copy
import ipaddress
import json
import os
import re
import ssl
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

import yaml
from pydantic import ValidationError

from aci_sim.build.fabric import default_serial, loopback_ip, oob_ip
from aci_sim.topology.loader import load_topology
from aci_sim.topology.schema import Auth, Topology

DEFAULT_TOPOLOGY_PATH = "topology.yaml"


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def _summarize_topology(topo: Topology) -> str:
    """Build the human-readable OK summary printed by `aci-sim validate`."""
    lines = [f"OK: {len(topo.sites)} site(s), {len(topo.tenants)} tenant(s)"]
    for site in topo.sites:
        lines.append(
            f"  - {site.name}: "
            f"{len(site.leaf_nodes())} leaves, "
            f"{len(site.spine_nodes())} spines, "
            f"{len(site.border_leaves)} border leaves"
        )
    return "\n".join(lines)


def cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.topology)
    try:
        topo = load_topology(path)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        # Covers both YAML parse errors (loader) and pydantic ValidationError
        # (a subclass of ValueError) with a readable rendering of each error.
        print(f"ERROR: topology validation failed for {path}:\n", file=sys.stderr)
        print(_render_validation_error(exc), file=sys.stderr)
        return 1

    print(_summarize_topology(topo))
    return 0


def _render_validation_error(exc: Exception) -> str:
    """Render a pydantic ValidationError (or plain ValueError) readably."""
    if isinstance(exc, ValidationError):
        parts = []
        for i, err in enumerate(exc.errors(), start=1):
            loc = " -> ".join(str(p) for p in err.get("loc", ()))
            msg = err.get("msg", "")
            parts.append(f"  {i}. [{loc}] {msg}" if loc else f"  {i}. {msg}")
        return "\n".join(parts)
    return f"  {exc}"


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _port_bindings(topo: Topology) -> list[dict[str, Any]]:
    """Per-site controller port/address bindings (mirrors supervisor.py's logic).

    Returns one dict per site (APIC) plus one for NDO, each describing the
    host/port a client would actually connect to, in whichever mode
    (port vs sandbox) the current environment/config selects.
    """
    # Imported lazily so `show`'s output reflects the environment at call
    # time (config.py reads os.environ at import time).
    from aci_sim.runtime import config as rt_config
    from aci_sim.runtime.supervisor import apic_port_for_site

    bindings = []
    for i, site in enumerate(topo.sites):
        if rt_config.SANDBOX:
            host, port = (site.mgmt_ip or "127.0.0.1"), rt_config.SANDBOX_PORT
        else:
            host, port = rt_config.SIM_BIND, apic_port_for_site(i)
        bindings.append(
            {
                "site": site.name,
                "role": "apic",
                "host": host,
                "port": port,
                "url": f"https://{host}:{port}",
            }
        )

    if rt_config.SANDBOX:
        ndo_host, ndo_port = (topo.fabric.ndo_mgmt_ip or "127.0.0.1"), rt_config.SANDBOX_PORT
    else:
        ndo_host = rt_config.SIM_BIND
        last_apic = apic_port_for_site(len(topo.sites) - 1) if topo.sites else rt_config.APIC_A_PORT
        ndo_port = rt_config.NDO_PORT if rt_config.NDO_PORT > last_apic else last_apic + 1
    bindings.append(
        {
            "site": None,
            "role": "ndo",
            "host": ndo_host,
            "port": ndo_port,
            "url": f"https://{ndo_host}:{ndo_port}",
        }
    )
    return bindings


def _node_role(site, node_id: int) -> str:
    spine_ids = {n.id for n in site.spine_nodes()}
    border_ids = {n.id for n in site.border_leaf_nodes()}
    if node_id in spine_ids:
        return "spine"
    if node_id in border_ids:
        return "border-leaf"
    return "leaf"


def build_inventory(topo: Topology) -> dict[str, Any]:
    """Build the derived-inventory data structure shared by text and --json `show`."""
    sites_out = []
    for site in topo.sites:
        nodes_out = []
        for node in site.all_nodes():
            nodes_out.append(
                {
                    "id": node.id,
                    "name": node.name,
                    "role": _node_role(site, node.id),
                    "model": node.model,
                    "version": node.version,
                    "serial": node.serial or default_serial(site.id, node.id),
                    "loopback_ip": loopback_ip(site.pod, node.id),
                    "oob_ip": oob_ip(site.pod, node.id),
                }
            )
        nodes_out.sort(key=lambda n: n["id"])
        sites_out.append(
            {
                "name": site.name,
                "apic_host": site.apic_host,
                "mgmt_ip": site.mgmt_ip,
                "asn": site.asn,
                "pod": site.pod,
                "controllers": site.controllers,
                # PR-21: effective per-controller OOB mgmt IPs — explicit
                # controller_ips if set, else sequentially derived from
                # mgmt_ip (empty list if mgmt_ip is also unset, matching the
                # legacy per-node oob_ip(pod, cid) fallback in build/fabric.py).
                "controller_ips": site.controller_oob_ips(),
                # Tier-3 (PR-20): the EFFECTIVE APIC controller version this
                # site's controller topSystem/firmwareCtrlrRunning actually
                # carry — fabric.default_apic_version wins if set, else this
                # site's own apic_version (build/fabric.py mirrors this
                # exact resolution order).
                "apic_version": topo.fabric.default_apic_version or site.apic_version,
                "fabric_name": site.fabric_name or topo.fabric.name,
                # OOB-gateway PR: this site's OOB mgmt default gateway (None
                # if unset) — lands on the real mgmtRsOoBStNode.gw MO
                # (build/mgmt.py), not just stored/informational.
                "oob_gateway": site.oob_gateway,
                "nodes": nodes_out,
            }
        )

    vmm_domains_out = [
        {
            "name": v.name,
            "vcenter_ip": v.vcenter_ip,
            "vcenter_dvs": v.vcenter_dvs,
            "vlan_pool": v.vlan_pool,
            "datacenter": v.datacenter,
        }
        for v in topo.access.vmm_domains
    ]

    return {
        "fabric_name": topo.fabric.name,
        "ndo_mgmt_ip": topo.fabric.ndo_mgmt_ip,
        "tep_pool": topo.fabric.tep_pool,
        "infra_vlan": topo.fabric.infra_vlan,
        "gipo_pool": topo.fabric.gipo_pool,
        # Tier-3 (PR-20) fabric-wide fine-tuning params:
        "oob_subnet": topo.fabric.oob_subnet,
        "inb_subnet": topo.fabric.inb_subnet,
        "default_bd_mac": topo.fabric.default_bd_mac,
        "default_apic_version": topo.fabric.default_apic_version,
        "isn_enabled": topo.isn.enabled,
        "isn_peer_mesh": topo.isn.peer_mesh,
        "isn_ospf_area": topo.isn.ospf_area,
        "isn_mtu": topo.isn.mtu,
        # Tier-3 (PR-20) ISN fine-tuning params:
        "isn_ospf_hello": topo.isn.ospf_hello,
        "isn_ospf_dead": topo.isn.ospf_dead,
        "isn_bfd_min_rx": topo.isn.bfd_min_rx,
        "isn_bfd_min_tx": topo.isn.bfd_min_tx,
        "isn_bfd_multiplier": topo.isn.bfd_multiplier,
        "vmm_domains": vmm_domains_out,
        "sites": sites_out,
        "ports": _port_bindings(topo),
    }


def _format_show_text(inv: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"Fabric: {inv['fabric_name']}  (NDO mgmt IP: {inv['ndo_mgmt_ip']})")
    lines.append(
        f"  tep_pool={inv['tep_pool']} infra_vlan={inv['infra_vlan']} gipo_pool={inv['gipo_pool']}"
    )
    lines.append(
        f"  oob_subnet={inv['oob_subnet'] or '-'} inb_subnet={inv['inb_subnet'] or '-'} "
        f"(store-only) default_bd_mac={inv['default_bd_mac']}"
    )
    if inv["default_apic_version"]:
        lines.append(f"  default_apic_version={inv['default_apic_version']} (overrides all sites)")
    lines.append(
        f"  isn: enabled={inv['isn_enabled']} peer_mesh={inv['isn_peer_mesh']} "
        f"ospf_area={inv['isn_ospf_area']} mtu={inv['isn_mtu']} "
        f"ospf_hello={inv['isn_ospf_hello']} ospf_dead={inv['isn_ospf_dead']}"
    )
    lines.append(
        f"  isn bfd (store-only): min_rx={inv['isn_bfd_min_rx']} "
        f"min_tx={inv['isn_bfd_min_tx']} multiplier={inv['isn_bfd_multiplier']}"
    )
    if inv["vmm_domains"]:
        lines.append("  vmm_domains:")
        for v in inv["vmm_domains"]:
            lines.append(
                f"    {v['name']:<16} vcenter_ip={v['vcenter_ip'] or '-':<15} "
                f"dvs={v['vcenter_dvs'] or '-':<12} datacenter={v['datacenter'] or '-'}"
            )
    lines.append("")

    for site in inv["sites"]:
        lines.append(
            f"Site {site['name']}: APIC host={site['apic_host']} mgmt_ip={site['mgmt_ip'] or '-'} "
            f"asn={site['asn']} pod={site['pod']} controllers={site['controllers']} "
            f"apic_version={site['apic_version']} fabric_name={site['fabric_name']} "
            f"controller_ips={','.join(site['controller_ips']) or '-'} "
            f"oob_gateway={site['oob_gateway'] or '-'}"
        )
        lines.append(
            f"  {'ID':<6} {'NAME':<20} {'ROLE':<12} {'MODEL':<14} {'VERSION':<16} "
            f"{'SERIAL':<14} {'LOOPBACK':<14} {'OOB IP'}"
        )
        for n in site["nodes"]:
            lines.append(
                f"  {n['id']:<6} {n['name']:<20} {n['role']:<12} {n['model']:<14} "
                f"{n['version']:<16} {n['serial']:<14} {n['loopback_ip']:<14} {n['oob_ip']}"
            )
        lines.append("")

    lines.append("Port bindings:")
    for b in inv["ports"]:
        label = b["site"] if b["site"] else "NDO"
        lines.append(f"  {label:<8} ({b['role']:<4}) -> {b['url']}")

    return "\n".join(lines)


def cmd_show(args: argparse.Namespace) -> int:
    path = Path(args.topology)
    try:
        topo = load_topology(path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    inv = build_inventory(topo)
    if args.json:
        print(json.dumps(inv, indent=2))
    else:
        print(_format_show_text(inv))
    return 0


# ---------------------------------------------------------------------------
# lldp — show lldp/cdp neighbors (materialized from a fresh build_site(), the
# same way `run`/the REST server derive their store — this CLI never starts
# a server, it just builds in-process and reads the resulting MIT store).
# ---------------------------------------------------------------------------


_ADJ_DN_RE = re.compile(r"^topology/pod-\d+/node-(\d+)/sys/(?:lldp|cdp)/inst/if-\[([^\]]+)\]/adj-1$")


def _parse_adj_dn(dn: str) -> tuple[int, str] | None:
    """Extract (local_node_id, local_port) from an lldpAdjEp/cdpAdjEp DN.

    Returns None for a DN that doesn't match the expected shape (defensive —
    should not happen for MOs this sim itself produces).
    """
    m = _ADJ_DN_RE.match(dn)
    if not m:
        return None
    return int(m.group(1)), m.group(2)


def _platform_from_lldp_sysdesc(sys_desc: str) -> str:
    """Best-effort model/platform token out of lldpAdjEp.sysDesc.

    sysDesc is either "Cisco APIC" (controller neighbor) or
    "{model} running {version}" (switch neighbor, see build/neighbors.py) —
    the model token is everything before " running ".
    """
    if not sys_desc:
        return ""
    if " running " in sys_desc:
        return sys_desc.split(" running ", 1)[0]
    return sys_desc


def _collect_neighbors(topo: Topology, site, *, cdp: bool, node_filter: int | None) -> list[dict[str, Any]]:
    """Build site's store and return one row per in-scope lldpAdjEp/cdpAdjEp."""
    from aci_sim.build.orchestrator import build_site

    store = build_site(topo, site)
    node_names = {n.id: n.name for n in site.all_nodes()}
    # Controllers aren't in site.all_nodes() (switches only) but a leaf can
    # legitimately have no local-node match if node_filter is a controller
    # id — real APIC controllers don't run their own lldpAdjEp query target
    # in this sim, so this is only used to label the LOCAL node (always a
    # switch: the adjacency root is always .../node-{N}/sys/lldp|cdp/...).

    cls = "cdpAdjEp" if cdp else "lldpAdjEp"
    rows: list[dict[str, Any]] = []
    for mo in store.by_class(cls):
        parsed = _parse_adj_dn(mo.dn)
        if parsed is None:
            continue
        local_node_id, local_port = parsed
        if node_filter is not None and local_node_id != node_filter:
            continue
        attrs = mo.attrs
        neighbor_port = (attrs.get("portId") if cdp else attrs.get("portIdV")) or "-"
        platform = attrs.get("platId") or _platform_from_lldp_sysdesc(attrs.get("sysDesc", ""))
        rows.append(
            {
                "local_node_id": local_node_id,
                "local_node_name": node_names.get(local_node_id, f"node-{local_node_id}"),
                "local_port": local_port,
                "neighbor": attrs.get("sysName", ""),
                "neighbor_port": neighbor_port,
                "mgmt_ip": attrs.get("mgmtIp", ""),
                "platform": platform,
            }
        )
    rows.sort(key=lambda r: (r["local_node_id"], r["local_port"]))
    return rows


def _format_lldp_text(site_name: str, rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    by_node: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for r in rows:
        key = (r["local_node_id"], r["local_node_name"])
        by_node.setdefault(key, []).append(r)

    for (node_id, node_name) in sorted(by_node.keys()):
        lines.append(f"Site {site_name} — node-{node_id} ({node_name})")
        lines.append(f"  {'Local Port':<11} {'Neighbor':<17} {'Neighbor Port':<14} {'Mgmt IP':<14} {'Platform'}")
        for r in by_node[(node_id, node_name)]:
            lines.append(
                f"  {r['local_port']:<11} {r['neighbor']:<17} {r['neighbor_port']:<14} "
                f"{r['mgmt_ip']:<14} {r['platform']}"
            )
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def cmd_lldp(args: argparse.Namespace) -> int:
    path = Path(args.topology)
    try:
        topo = load_topology(path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    sites = [s for s in topo.sites if not args.site or s.name == args.site]
    node_filter = args.node

    all_rows: list[dict[str, Any]] = []
    for site in sites:
        rows = _collect_neighbors(topo, site, cdp=args.cdp, node_filter=node_filter)
        for r in rows:
            r["site"] = site.name
        all_rows.extend(rows)

    if args.json:
        print(json.dumps(all_rows, indent=2))
        return 0

    if not all_rows:
        print("No neighbors found.")
        return 0

    for site in sites:
        site_rows = [r for r in all_rows if r["site"] == site.name]
        if not site_rows:
            continue
        text = _format_lldp_text(site.name, site_rows)
        if text:
            print(text)
    return 0


# ---------------------------------------------------------------------------
# query — zero-friction authenticated MIT query (aaaLogin -> cookie -> GET),
# so newcomers never have to hand-roll the login/cookie dance themselves.
# stdlib-only (urllib.request + ssl + http.cookies) — httpx is dev-only (see
# pyproject.toml's [project.optional-dependencies] dev extra), and `aci-sim
# query` must work from a bare `pip install aci-sim` with no dev extras.
# ---------------------------------------------------------------------------

DEFAULT_QUERY_HOST = "127.0.0.1:8443"
DEFAULT_QUERY_USER = "admin"
DEFAULT_QUERY_PASSWORD = "cisco"


class QueryConnectionError(Exception):
    """Raised when the sim cannot be reached at all (connection refused/timeout)."""


def _unverified_ssl_context() -> ssl.SSLContext:
    """A permissive SSL context that accepts the sim's self-signed cert.

    Mirrors `curl -k` / `httpx.Client(verify=False)` used elsewhere in this
    repo (examples/ci/assert_mit.py, scripts/e2e_*.py) — the sim's cert is
    self-signed by scripts/gen_certs.sh, so hostname/CA verification is
    deliberately skipped for this local dev tool.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _aaa_login(host: str, user: str, password: str, timeout: float = 5.0) -> str:
    """POST /api/aaaLogin.json; return the APIC-cookie token string.

    Raises QueryConnectionError on a connection-level failure (sim not
    running), or ValueError if the sim answered but login failed (bad
    credentials / malformed envelope).
    """
    import json as _json
    import urllib.error
    import urllib.request

    url = f"https://{host}/api/aaaLogin.json"
    body = _json.dumps({"aaaUser": {"attributes": {"name": user, "pwd": password}}}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_unverified_ssl_context()) as resp:
            resp_body = resp.read().decode("utf-8")
            set_cookie = resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as exc:
        resp_body = exc.read().decode("utf-8")
        text = _error_text_from_envelope(resp_body) or f"HTTP {exc.code}"
        raise ValueError(f"aaaLogin failed: {text}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise QueryConnectionError(str(exc)) from exc

    token = _extract_cookie(set_cookie, "APIC-cookie")
    if token is not None:
        return token

    # Fall back to the token embedded in the aaaLogin response body itself
    # (docs/CONTRACT.md: imdata[0].aaaLogin.attributes.token) in case
    # Set-Cookie parsing ever misses it.
    try:
        data = _json.loads(resp_body)
        return data["imdata"][0]["aaaLogin"]["attributes"]["token"]
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"aaaLogin succeeded but no token found in response: {resp_body[:200]}") from exc


def _extract_cookie(set_cookie_headers: list[str], name: str) -> str | None:
    """Pull cookie *name*'s value out of one or more raw Set-Cookie header strings."""
    from http.cookies import SimpleCookie

    for header in set_cookie_headers:
        jar: SimpleCookie = SimpleCookie()
        jar.load(header)
        if name in jar:
            return jar[name].value
    return None


def _error_text_from_envelope_dict(data: Any) -> str | None:
    """If *data* (already-parsed) is an APIC error envelope, return its human-readable text."""
    imdata = data.get("imdata") if isinstance(data, dict) else None
    if not imdata or not isinstance(imdata, list):
        return None
    first = imdata[0]
    if isinstance(first, dict) and "error" in first:
        attrs = first["error"].get("attributes", {})
        code = attrs.get("code", "")
        text = attrs.get("text", "")
        return f"{text} (code {code})" if code else text
    return None


def _error_text_from_envelope(raw_body: str) -> str | None:
    """If *raw_body* is an APIC error envelope, return its human-readable text."""
    import json as _json

    try:
        data = _json.loads(raw_body)
    except ValueError:
        return None
    return _error_text_from_envelope_dict(data)


def _query_mit(
    host: str, token: str, *, cls: str | None, dn: str | None, timeout: float = 5.0
) -> dict[str, Any]:
    """GET /api/class/{cls}.json or /api/mo/{dn}.json with the APIC-cookie; return parsed JSON."""
    import json as _json
    import urllib.error
    import urllib.request

    if dn is not None:
        url = f"https://{host}/api/mo/{dn}.json"
    else:
        url = f"https://{host}/api/class/{cls}.json"

    req = urllib.request.Request(url, method="GET", headers={"Cookie": f"APIC-cookie={token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_unverified_ssl_context()) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise QueryConnectionError(str(exc)) from exc

    return _json.loads(raw)


# A handful of well-known attributes worth surfacing as extra table columns,
# tried in this order — the first one present on the MO wins (keeps the
# table useful across very different classes without a per-class mapping).
_QUERY_KEY_ATTR_CANDIDATES = ("name", "dn", "id")
_QUERY_EXTRA_ATTR_CANDIDATES = ("role", "state", "status", "adSt", "operSt", "descr")


def _format_query_text(cls_label: str, imdata: list[dict[str, Any]]) -> str:
    """Render a compact one-row-per-MO table, in `aci-sim lldp`'s tabular style."""
    if not imdata:
        return f"No {cls_label} objects found."

    rows: list[dict[str, str]] = []
    for entry in imdata:
        if not isinstance(entry, dict) or not entry:
            continue
        class_name, body = next(iter(entry.items()))
        attrs = body.get("attributes", {}) if isinstance(body, dict) else {}
        key = next((attrs[a] for a in _QUERY_KEY_ATTR_CANDIDATES if attrs.get(a)), "-")
        extra = next((attrs[a] for a in _QUERY_EXTRA_ATTR_CANDIDATES if attrs.get(a)), "-")
        rows.append({"class": class_name, "key": key, "extra": extra, "dn": attrs.get("dn", "-")})

    lines = [f"{'CLASS':<16} {'NAME/KEY':<28} {'EXTRA':<12} DN"]
    for r in rows:
        lines.append(f"{r['class']:<16} {r['key']:<28} {r['extra']:<12} {r['dn']}")
    return "\n".join(lines)


def cmd_query(args: argparse.Namespace) -> int:
    host = args.host
    user = args.user
    password = args.password

    if not args.dn and not args.cls:
        print("ERROR: provide a CLASS (e.g. `aci-sim query fabricNode`) or --dn DN", file=sys.stderr)
        return 1

    try:
        token = _aaa_login(host, user, password)
    except QueryConnectionError:
        print(
            f"ERROR: could not connect to {host} — is the sim running? "
            f"start it with `aci-sim run`",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        data = _query_mit(host, token, cls=args.cls if not args.dn else None, dn=args.dn)
    except QueryConnectionError:
        print(
            f"ERROR: could not connect to {host} — is the sim running? "
            f"start it with `aci-sim run`",
            file=sys.stderr,
        )
        return 1

    imdata = data.get("imdata", []) if isinstance(data, dict) else []
    error_text = _error_text_from_envelope_dict(data)
    if error_text is not None:
        print(f"ERROR: {error_text}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    label = args.dn if args.dn else args.cls
    print(_format_query_text(label, imdata))
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

# Environment variables passed through unmodified from the caller's
# environment into the supervisor subprocess/exec, if present.
_PASSTHROUGH_ENV_VARS = (
    "SIM_BIND",
    "SIM_SANDBOX",
    "SIM_SANDBOX_PORT",
    "SIM_USERNAME",
    "SIM_PASSWORD",
    "APIC_A_PORT",
    "APIC_B_PORT",
    "NDO_PORT",
    "CERT_FILE",
    "KEY_FILE",
    "SIM_CERT_STRICT",
)


def resolve_admin_credentials(
    base_env: dict[str, str],
    auth: Auth | None,
) -> tuple[str | None, str | None, str]:
    """Resolve which SIM_USERNAME/SIM_PASSWORD `aci-sim run` should inject, and why.

    Precedence (highest first):
      1. `base_env` already has EITHER `SIM_USERNAME` or `SIM_PASSWORD` set —
         an explicit env override wins outright (preserves CI/env-override
         behavior); `auth` is ignored entirely. Returns `(None, None,
         "env override")` — i.e. inject nothing, so the caller's env is
         authoritative and `rest_aci/auth.py` fills any var the caller left
         unset with its own admin/cisco default. The override is atomic ON
         PURPOSE: checking only `SIM_USERNAME` would let a caller who sets
         only `SIM_PASSWORD` (e.g. rotating just the password from a secret,
         keeping the default username) have their password SILENTLY discarded
         in favor of the plaintext topology password — a partial-config trap.
         So if the caller touched either credential var, they own both.
      2. `auth` is not None (the topology has an `auth:` section, written by
         the wizard's Admin account step) — returns `(auth.username,
         auth.password, "topology.yaml")` so the caller injects both vars.
      3. Neither — returns `(None, None, "built-in default")`; injecting
         nothing lets `rest_aci/auth.py` fall back to its own
         SIM_USERNAME/SIM_PASSWORD-env-or-admin/cisco default, unchanged.

    Only APIC is affected — the NDO plane is deliberately lenient (accepts
    any credential) and has no env var this function would set.
    """
    if base_env.get("SIM_USERNAME") or base_env.get("SIM_PASSWORD"):
        return None, None, "env override"
    if auth is not None:
        return auth.username, auth.password, "topology.yaml"
    return None, None, "built-in default"


def build_run_env_argv(
    topology: str,
    base_env: dict[str, str] | None = None,
    executable: str | None = None,
    admin_username: str | None = None,
    admin_password: str | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Build the (env, argv) pair `aci-sim run` execs — factored out for testing.

    Does not touch the filesystem or spawn anything; pure function of its
    inputs so tests can assert on the env/argv without booting a real
    supervisor (a live boot binds real sockets, which we deliberately do not
    exercise in the test suite).

    `admin_username`/`admin_password` (optional): when both are given, they
    are injected as `SIM_USERNAME`/`SIM_PASSWORD` in the returned env. Callers
    (`cmd_run`) are expected to have already resolved these via
    `resolve_admin_credentials` — this function stays filesystem-pure and
    topology-agnostic (it takes credentials as plain params, it never loads
    a topology file itself).
    """
    env = copy.deepcopy(base_env) if base_env is not None else dict(os.environ)
    env["TOPOLOGY_PATH"] = str(topology)
    if admin_username is not None and admin_password is not None:
        env["SIM_USERNAME"] = admin_username
        env["SIM_PASSWORD"] = admin_password
    exe = executable or sys.executable
    argv = [exe, "-m", "aci_sim.runtime.supervisor"]
    return env, argv


def _ensure_certs(cert_file: str, key_file: str) -> None:
    """Generate a self-signed cert/key pair if missing (mirrors scripts/gen_certs.sh).

    Keeps `aci-sim run` a drop-in replacement for `scripts/run.sh` (which
    generates certs on first run) without requiring the caller to invoke a
    separate script first.
    """
    if Path(cert_file).exists() and Path(key_file).exists():
        return
    import subprocess

    Path(cert_file).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:4096",
            "-keyout", key_file, "-out", cert_file,
            "-days", "3650", "-nodes",
            "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
    )
    print(f"[aci-sim] generated self-signed certs: {cert_file}, {key_file}")


def cmd_graph(args: argparse.Namespace) -> int:
    path = Path(args.topology)
    try:
        topo = load_topology(path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    out_path = Path(args.output)
    suffix = out_path.suffix.lower()
    if suffix == ".svg":
        fmt = "svg"
    elif suffix in (".html", ".htm"):
        fmt = "html"
    else:
        print(
            f"ERROR: Unrecognized output extension {suffix!r} for {out_path}; "
            f"use .html or .svg",
            file=sys.stderr,
        )
        return 1

    from aci_sim.graph import render_topology

    content = render_topology(topo, fmt=fmt)
    out_path.write_text(content, encoding="utf-8")
    print(f"[aci-sim] wrote topology diagram: {out_path}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    path = Path(args.topology)
    if not path.exists():
        print(f"ERROR: Topology file not found: {path}", file=sys.stderr)
        return 1

    # Fail fast with a friendly message before handing off to the supervisor,
    # which would otherwise raise a raw ValidationError traceback. Also gives
    # us the parsed Topology (specifically its optional `auth:` section) so
    # we can resolve which admin credentials the supervisor should enforce —
    # see resolve_admin_credentials's precedence docstring.
    try:
        topo = load_topology(path)
    except ValueError as exc:
        print(f"ERROR: topology validation failed for {path}:\n", file=sys.stderr)
        print(_render_validation_error(exc), file=sys.stderr)
        return 1

    from aci_sim.runtime.config import CERT_FILE, KEY_FILE

    _ensure_certs(CERT_FILE, KEY_FILE)

    admin_username, admin_password, source = resolve_admin_credentials(dict(os.environ), topo.auth)
    env, argv = build_run_env_argv(str(path), admin_username=admin_username, admin_password=admin_password)
    # The operator should never be surprised about which credential is live —
    # print the username + where it came from, but NEVER the password.
    live_username = admin_username or env.get("SIM_USERNAME") or "admin"
    if source == "env override":
        print(f"[aci-sim] Auth: APIC admin {live_username!r} (from SIM_USERNAME/SIM_PASSWORD env override)")
    elif source == "topology.yaml":
        print(f"[aci-sim] Auth: APIC admin {live_username!r} (from {path}'s auth: section)")
    else:
        print(f"[aci-sim] Auth: APIC admin {live_username!r} (built-in default)")
    print(f"[aci-sim] running supervisor with TOPOLOGY_PATH={path}")
    os.execvpe(argv[0], argv, env)
    return 0  # pragma: no cover — os.execvpe never returns on success


# ---------------------------------------------------------------------------
# new (scaffold)
# ---------------------------------------------------------------------------


def _default_asns(n_sites: int) -> list[int]:
    return [65001 + i for i in range(n_sites)]


def _parse_asn_list(raw: str | None, n_sites: int) -> list[int]:
    if not raw:
        return _default_asns(n_sites)
    values = [int(x.strip()) for x in raw.split(",") if x.strip()]
    if len(values) != n_sites:
        raise ValueError(
            f"--asn must supply exactly {n_sites} value(s) (one per site), got {len(values)}: {raw!r}"
        )
    return values


def _site_letter(index: int) -> str:
    """A, B, C, ... Z, AA, AB, ... for site naming beyond 26 sites."""
    letters = ""
    n = index
    while True:
        n, rem = divmod(n, 26)
        letters = chr(ord("A") + rem) + letters
        if n == 0:
            break
        n -= 1
    return letters


def generate_topology(
    sites: int = 2,
    leaves_per_site: int | list[int] = 2,
    spines_per_site: int | list[int] = 2,
    border_pairs: int | list[int] = 1,
    asn: str | None = None,
    fabric_name_prefix: str = "LAB",
    ndo_ip: str = "10.192.0.10",
    apic_ip_base: str = "10.192",
    controllers: int = 1,
    tep_pool: str = "10.0.0.0/16",
    infra_vlan: int = 3967,
    gipo_pool: str = "225.0.0.0/15",
    isn_ospf_area: str = "0.0.0.0",
    isn_mtu: int = 9150,
    isn_ospf_hello: int = 10,
    isn_ospf_dead: int = 40,
    isn_bfd_min_rx: int = 50,
    isn_bfd_min_tx: int = 50,
    isn_bfd_multiplier: int = 3,
    default_bd_mac: str = "00:22:BD:F8:19:FF",
    site_overrides: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a fresh, minimal, valid topology dict (same shape as topology.yaml).

    Node-ID scheme matches topology/schema.py's documented convention:
      leaf base=101, site_offset=200   -> site i: 101+200*i, 102+200*i, ...
      spine base=201, site_offset=200  -> site i: 201+200*i, 202+200*i, ...
      border leaves: explicit ids outside those ranges. The default shape
                     (2 leaves, 2 spines) reproduces the documented example
                     (site 0: 103,104; site 1: 303,304); for larger
                     --leaves-per-site/--spines-per-site counts, the border-
                     leaf base is pushed past whichever auto-generated range
                     (leaves or spines) extends further, so it can never
                     collide with an auto-generated leaf/spine id — this is
                     required by Topology.normalize_and_validate's global
                     node-id collision check.

    Border leaves are generated in vPC pairs (`border_pairs` pairs = 2 * N
    border leaves), each pair sharing a vpc_domain and CSW.

    `controllers` (PR-18 Tier-1 param) sets APIC cluster size per site
    (schema default 1, single-APIC by design as of PR-21 — see
    Site.controllers docstring; range 1-5); `tep_pool`/`infra_vlan`/`gipo_pool`
    are the Tier-1
    fabric-wide params (schema defaults 10.0.0.0/16, 3967, 225.0.0.0/15) —
    all four are simply forwarded into the scaffolded YAML, using the same
    schema defaults if the caller doesn't override them.

    `isn_ospf_area`/`isn_mtu` (Tier-2, PR-19) are the ISN underlay params
    (schema defaults "0.0.0.0", 9150) forwarded into the scaffolded `isn:`
    block the same way.

    `isn_ospf_hello`/`isn_ospf_dead`/`isn_bfd_min_rx`/`isn_bfd_min_tx`/
    `isn_bfd_multiplier` (Tier-3, PR-20) are further ISN timer params
    (schema defaults 10, 40, 50, 50, 3) forwarded the same way; OSPF
    hello/dead are wired into `ospfIf` (build/underlay.py), BFD timers are
    store+validate+surface only (no `bfdIfP` MO built anywhere in this
    sim). `default_bd_mac` (Tier-3, PR-20; schema default the real ACI
    default `"00:22:BD:F8:19:FF"`) is the fabric-wide default gateway MAC
    forwarded into `fabric.default_bd_mac`.

    `site_overrides` (PR-22, optional): a list of length `sites`, each entry a
    dict of per-site overrides applied ON TOP of the auto-derived site_def
    below (name/id/apic_host/mgmt_ip/fabric_name/asn/pod/controllers/
    controller_ips) — e.g. `[{"mgmt_ip": "10.192.0.11", "controller_ips":
    ["10.192.0.11", "10.192.0.12"]}, {...}]`. Added so `aci-sim init` (the
    interactive wizard) can collect per-site answers (including PR-21's
    `controller_ips`, which the flat `--controllers`/`apic_ip_base` flags
    above cannot express per-site) and feed them through this SAME generator
    instead of duplicating topology-emission logic. `None` (default) leaves
    every site's auto-derived fields untouched — fully backward compatible
    with every pre-PR-22 caller of this function.

    `leaves_per_site`/`spines_per_site`/`border_pairs` (PR-22, widened): each
    now also accepts a `list[int]` of length `sites` — a PER-SITE count —
    instead of only a single `int` applied uniformly to every site (the
    pre-PR-22 behavior, still the default and still what every existing
    caller/flag passes). Added so `aci-sim init` can express sites with
    different node counts (asked per-site in the wizard) without a second
    topology-emission code path.
    """
    if sites < 1:
        raise ValueError("--sites must be >= 1")
    if site_overrides is not None and len(site_overrides) != sites:
        raise ValueError(
            f"site_overrides must have exactly {sites} entries (one per site), "
            f"got {len(site_overrides)}"
        )

    def _per_site(value: int | list[int], flag: str) -> list[int]:
        if isinstance(value, list):
            if len(value) != sites:
                raise ValueError(f"{flag} list must have exactly {sites} entries (one per site), got {len(value)}")
            return list(value)
        return [value] * sites

    leaves_list = _per_site(leaves_per_site, "leaves_per_site")
    spines_list = _per_site(spines_per_site, "spines_per_site")
    border_list = _per_site(border_pairs, "border_pairs")

    if any(n < 0 for n in leaves_list) or any(n < 0 for n in spines_list) or any(n < 0 for n in border_list):
        raise ValueError("--leaves-per-site/--spines-per-site/--border-pairs must be >= 0")

    # Tier-2 (PR-19) leaf/spine-count-sugar guard: the node-ID scheme
    # (topology/schema.py docstring) gives each site a 200-wide ID budget —
    # leaves start at 101+offset, spines at 201+offset, and the NEXT site's
    # leaf block starts at 301+offset. Large --leaves-per-site/
    # --spines-per-site/--border-pairs values can overrun that budget in
    # three distinct ways that Topology.normalize_and_validate would only
    # catch AFTER the fact with a confusing "node ids collide" error that
    # doesn't say which flag caused it:
    #   1. leaf block itself reaches into the spine block  (leaves_per_site > 100)
    #   2. spine block itself reaches into the NEXT site's leaf block
    #      (spines_per_site > 100)
    #   3. the border-leaf block (pushed past whichever of leaves/spines
    #      extends further) runs past the next site's leaf block
    #      (base_after_leaves_and_spines + 2*border_pairs - 1 >= 300)
    # Fail fast here, at the CLI layer, with a message that names the
    # specific flag(s) to reduce — same spirit as PR-17's border-ID fix,
    # which pushed the border base to avoid case 3 for modest counts but
    # did not guard against leaves/spines themselves overrunning the budget.
    # (PR-22: checked per-site now that these can vary per site.)
    for site_idx, (lps, sps, bp) in enumerate(zip(leaves_list, spines_list, border_list, strict=True)):
        if lps > 100:
            raise ValueError(
                f"--leaves-per-site={lps} (site index {site_idx}) exceeds the per-site node-ID "
                f"budget (max 100 — leaf ids run 101..200+site_offset before "
                f"colliding with the auto-generated spine block at 201+site_offset)."
            )
        if sps > 100:
            raise ValueError(
                f"--spines-per-site={sps} (site index {site_idx}) exceeds the per-site node-ID "
                f"budget (max 100 — spine ids run 201..300+site_offset before "
                f"colliding with the NEXT site's auto-generated leaf block at "
                f"301+site_offset)."
            )
        if bp > 0:
            leaf_top = 100 + lps if lps else 100
            spine_top = 200 + sps if sps else 200
            border_base = max(leaf_top, spine_top) + 1
            border_top = border_base + 2 * bp - 1
            if border_top > 300:
                raise ValueError(
                    f"--border-pairs={bp} (site index {site_idx}, combined with "
                    f"--leaves-per-site={lps}/--spines-per-site="
                    f"{sps}) would push border-leaf ids up to "
                    f"{border_top}+site_offset, past the per-site node-ID budget "
                    f"of 300+site_offset (the NEXT site's auto-generated leaf "
                    f"block starts at 301+site_offset). Reduce --border-pairs or "
                    f"--leaves-per-site/--spines-per-site."
                )

    asns = _parse_asn_list(asn, sites)

    site_defs = []
    tenant_site_names = []
    fault_seeds = []

    for i in range(sites):
        letter = _site_letter(i)
        site_name = f"{fabric_name_prefix}{letter}" if fabric_name_prefix else f"SITE{letter}"
        site_offset = i * 200
        pod = 1
        n_leaves_i = leaves_list[i]
        n_spines_i = spines_list[i]
        n_border_i = border_list[i]

        leaves = [
            {"id": 101 + site_offset + j, "name": f"{site_name}-LF{101 + site_offset + j}"}
            for j in range(n_leaves_i)
        ]
        spines = [
            {"id": 201 + site_offset + j, "name": f"{site_name}-SP{201 + site_offset + j}"}
            for j in range(n_spines_i)
        ]

        # Prefer the gap right after the leaf block and before the spine
        # block (matches the documented example: leaves 101-102, border
        # 103-104, spines 201-202). If leaves_per_site is large enough that
        # the leaf block would run into the spine block, fall back to
        # starting right after whichever range extends further, so border
        # ids can never collide with an auto-generated leaf/spine id
        # regardless of how many were requested.
        leaf_top = 101 + site_offset + n_leaves_i - 1 if n_leaves_i else 101 + site_offset - 1
        spine_base = 201 + site_offset
        spine_top = spine_base + n_spines_i - 1 if n_spines_i else spine_base - 1
        gap_start = leaf_top + 1
        gap_needed = 2 * n_border_i
        if n_spines_i == 0 or gap_start + gap_needed - 1 < spine_base:
            border_base = gap_start
        else:
            border_base = max(leaf_top, spine_top) + 1

        border_leaves = []
        for p in range(n_border_i):
            vpc_domain = f"vpcdom-{site_name}-{p + 1}"
            bl_base = border_base + (p * 2)
            csw_asn = 65100
            peer_ip = f"10.{100 + i}.{p}.1"
            for leg, bl_id in enumerate((bl_base, bl_base + 1)):
                border_leaves.append(
                    {
                        "id": bl_id,
                        "name": f"{site_name}-LF{bl_id}",
                        "vpc_domain": vpc_domain,
                        "csw": {
                            "name": f"{site_name}-CSW{p + 1:02d}",
                            "asn": csw_asn,
                            "peer_ip": peer_ip,
                            "local_ip": f"10.{100 + i}.{p}.{2 + leg}",
                            "vlan": 100 + i * 100 + p,
                        },
                    }
                )

        apic_ip = f"{apic_ip_base}.{i}.11" if apic_ip_base else ""
        site_def: dict[str, Any] = {
            "name": site_name,
            "id": str(i + 1),
            "apic_host": f"127.0.0.1:{8443 + i}",
            "mgmt_ip": apic_ip,
            "fabric_name": f"{site_name}-IT-ACI",
            "asn": asns[i],
            "pod": pod,
            "controllers": controllers,
            "spines": spines,
            "leaves": leaves,
            "border_leaves": border_leaves,
            "cabling": "auto",
        }
        if site_overrides is not None:
            # Shallow-merge the caller's per-site overrides on top of the
            # auto-derived fields above (spines/leaves/border_leaves are
            # deliberately NOT overridable this way — node topology stays
            # driven by leaves_per_site/spines_per_site/border_pairs).
            site_def.update(site_overrides[i])
        site_name = site_def["name"]  # re-read in case an override renamed the site
        site_defs.append(site_def)
        tenant_site_names.append(site_name)
        if leaves:
            fault_seeds.append(
                {
                    "severity": "warning",
                    "code": "F0532",
                    "node": leaves[0]["id"],
                    "descr": f"Link flap detected on {leaves[0]['name']} eth1/1 (scaffolded seed)",
                }
            )

    # One trivial, single-site tenant per site so the file is runnable
    # end-to-end (endpoints/health/query builders all expect >=0 tenants,
    # but a demo file should have at least something to look at).
    tenants = []
    for site_name in tenant_site_names:
        tenants.append(
            {
                "name": f"SF-DEMO-{site_name}",
                "stretch": False,
                "sites": [site_name],
                "vrfs": [{"name": f"vrf-L3_{site_name}"}],
                "bds": [
                    {
                        "name": f"bd-DEMO_{site_name}",
                        "vrf": f"vrf-L3_{site_name}",
                        "subnets": ["192.168.100.0/24"],
                    }
                ],
                "aps": [
                    {
                        "name": f"app-Demo_{site_name}",
                        "epgs": [
                            {
                                "name": f"epg-Demo_{site_name}",
                                "bd": f"bd-DEMO_{site_name}",
                                "contracts": {"provide": [], "consume": []},
                            }
                        ],
                    }
                ],
                "contracts": [],
                "l3outs": [],
            }
        )

    topology: dict[str, Any] = {
        "fabric": {
            "name": f"{fabric_name_prefix}-IT-ACI" if fabric_name_prefix else "SIM-IT-ACI",
            "evpn_rr": "spine",
            "loopback_pool": "10.0.0.0/16",
            "oob_pool": "192.168.0.0/16",
            "ndo_mgmt_ip": ndo_ip,
            "tep_pool": tep_pool,
            "infra_vlan": infra_vlan,
            "gipo_pool": gipo_pool,
            "default_bd_mac": default_bd_mac,
        },
        "sites": site_defs,
        "isn": {
            "enabled": sites >= 2,
            "peer_mesh": "full",
            "ospf_area": isn_ospf_area,
            "mtu": isn_mtu,
            "ospf_hello": isn_ospf_hello,
            "ospf_dead": isn_ospf_dead,
            "bfd_min_rx": isn_bfd_min_rx,
            "bfd_min_tx": isn_bfd_min_tx,
            "bfd_multiplier": isn_bfd_multiplier,
        },
        "tenants": tenants,
        "endpoints": {"per_epg": 3, "ip_pool_from_subnet": True},
        "faults": {
            "seed": fault_seeds,
            "health_defaults": {"node": 95, "tenant": 98},
        },
        "access": {
            "vlan_pools": [{"name": "vlp-general", "from_vlan": 100, "to_vlan": 200}],
            "phys_domains": [{"name": "phy-general"}],
            "l3_domains": [{"name": "l3d-l3out"}],
            "aaeps": [{"name": "aep-general"}],
        },
    }
    return topology


_SCAFFOLD_HEADER = """\
# Generated by `aci-sim new` — a minimal, valid topology.yaml.
# topology.yaml (whichever file you point the sim at) remains the single
# source of truth; edit freely, then re-validate with `aci-sim validate`.
"""


def cmd_new(args: argparse.Namespace) -> int:
    try:
        topology = generate_topology(
            sites=args.sites,
            leaves_per_site=args.leaves_per_site,
            spines_per_site=args.spines_per_site,
            border_pairs=args.border_pairs,
            asn=args.asn,
            fabric_name_prefix=args.fabric_name_prefix,
            ndo_ip=args.ndo_ip,
            apic_ip_base=args.apic_ip_base,
            controllers=args.controllers,
            tep_pool=args.tep_pool,
            infra_vlan=args.infra_vlan,
            gipo_pool=args.gipo_pool,
            isn_ospf_area=args.isn_ospf_area,
            isn_mtu=args.isn_mtu,
            isn_ospf_hello=args.isn_ospf_hello,
            isn_ospf_dead=args.isn_ospf_dead,
            isn_bfd_min_rx=args.isn_bfd_min_rx,
            isn_bfd_min_tx=args.isn_bfd_min_tx,
            isn_bfd_multiplier=args.isn_bfd_multiplier,
            default_bd_mac=args.default_bd_mac,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    yaml_text = _SCAFFOLD_HEADER + yaml.dump(topology, sort_keys=False, default_flow_style=False)

    # Self-check: the generated file must pass the same validation `aci-sim
    # validate` runs, so `new` never hands the user a broken starting point.
    try:
        Topology.model_validate(topology)
    except ValidationError as exc:
        print(
            "ERROR: internal error — generated topology failed validation:\n"
            + _render_validation_error(exc),
            file=sys.stderr,
        )
        return 1

    if args.output:
        Path(args.output).write_text(yaml_text, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(yaml_text, end="")
    return 0


# ---------------------------------------------------------------------------
# init (interactive APIC-setup-style wizard)
# ---------------------------------------------------------------------------
#
# Modeled on the real APIC first-boot "setup dialog" (the console Q&A a
# fresh APIC controller runs on first power-on): each field is prompted with
# a suggested default shown in [brackets] — press ENTER to accept it, or
# type a custom value. This wizard asks for the SAME fields `aci-sim new`
# takes as flags (fabric name, ASN, pod, TEP pool, infra VLAN, GIPo pool,
# OOB mgmt IP/gateway, controller count) plus this sim's multi-site extras
# (ISN OSPF area/MTU, NDO mgmt IP), then calls `generate_topology()` — the
# SAME generator `aci-sim new` uses — so it never duplicates topology-
# emission logic. It is purely a thin interactive front-end.
#
# Real APIC's dialog also asks about "multi-pod" — this sim doesn't build
# true multi-pod fabrics (no per-pod spine/leaf partitioning or IPN), so
# that choice is deliberately NOT offered; a single fabric can still set a
# non-default `pod` id per PR-19's pod-elasticity field, mentioned in the
# per-site prompt.


class WizardAbort(Exception):
    """Raised internally when the user declines to write the file at the end."""


def _prompt(
    label: str,
    default: str,
    stream_in: TextIO,
    stream_out: TextIO,
    validator: Callable[[str], str | None] | None = None,
    accept_defaults: bool = False,
) -> str:
    """Print `label [default]: `, read one line, return default on empty input.

    Re-prompts (by recursing) if `validator` is given and returns a non-None
    error message for the candidate value (default included — a bad default
    should never happen, but we don't special-case it).

    `accept_defaults=True` (non-interactive: `--defaults` or a non-TTY stdin)
    skips reading entirely and returns `default` immediately, so `aci-sim
    init` can run unattended in CI/tests without ever blocking on stdin.
    """
    if accept_defaults:
        return default

    stream_out.write(f"{label} [{default}]: ")
    stream_out.flush()
    raw = stream_in.readline()
    if raw == "":
        # EOF on stdin (e.g. a scripted/short answer stream ran out) — treat
        # exactly like accept_defaults for THIS field rather than hanging or
        # raising, so scripted stdin that supplies answers for only a prefix
        # of the prompts still completes deterministically.
        return default
    value = raw.strip()
    if value == "":
        value = default

    if validator is not None:
        error = validator(value)
        if error is not None:
            stream_out.write(f"  ! {error}\n")
            return _prompt(label, default, stream_in, stream_out, validator, accept_defaults)
    return value


def _prompt_yes_no(label: str, default_yes: bool, stream_in: TextIO, stream_out: TextIO, accept_defaults: bool) -> bool:
    default = "yes" if default_yes else "no"
    while True:
        value = _prompt(label, default, stream_in, stream_out, accept_defaults=accept_defaults)
        lowered = value.strip().lower()
        if lowered in ("y", "yes"):
            return True
        if lowered in ("n", "no"):
            return False
        if accept_defaults:
            return default_yes
        stream_out.write("  ! please answer yes or no\n")


def _validate_int_range(lo: int, hi: int) -> Callable[[str], str | None]:
    def _v(value: str) -> str | None:
        try:
            n = int(value)
        except ValueError:
            return f"must be an integer, got {value!r}"
        if not (lo <= n <= hi):
            return f"must be between {lo} and {hi}, got {n}"
        return None

    return _v


def _validate_ip(value: str) -> str | None:
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        return f"not a valid IP address: {exc}"
    return None


def _validate_cidr(value: str) -> str | None:
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        return f"not a valid CIDR: {exc}"
    return None


def _next_ip(ip_str: str) -> str:
    """Return `ip_str` + 1, e.g. '10.192.0.11' -> '10.192.0.12' (PR-21 scheme)."""
    return str(ipaddress.ip_address(ip_str) + 1)


class _AnswerSource:
    """Wraps an optional pre-filled answers dict (from `--answers FILE`).

    `get(key, default)` returns the answers-file value for `key` if present
    (coerced to str, matching what an interactive prompt would return), else
    `default`. This lets `--answers` partially override just a few fields
    while everything else still falls back to the wizard's own defaults —
    scriptable without having to enumerate every field.
    """

    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = data or {}

    def get(self, key: str, default: str) -> str:
        if key in self._data:
            return str(self._data[key])
        return default

    def has(self, key: str) -> bool:
        return key in self._data


def _load_answers_file(path: str) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text)  # yaml.safe_load also parses plain JSON
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"--answers file {path!r} must contain a mapping/object, got {type(data).__name__}")
    return data


def _wizard_prompt_field(
    key: str,
    label: str,
    default: str,
    answers: _AnswerSource,
    stream_in: TextIO,
    stream_out: TextIO,
    accept_defaults: bool,
    validator: Callable[[str], str | None] | None = None,
) -> str:
    """One wizard field: --answers value (if present) wins outright (no re-prompt,
    matching a scriptable non-interactive contract); otherwise prompt/accept-default
    with `default` as the suggested value."""
    if answers.has(key):
        return answers.get(key, default)
    return _prompt(label, default, stream_in, stream_out, validator, accept_defaults)


def run_wizard(
    *,
    stream_in: TextIO,
    stream_out: TextIO,
    accept_defaults: bool,
    answers: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Drive the full Q&A flow; return `(topology_dict, oob_gateway_note)`.

    Pure-ish: takes explicit I/O streams (never touches real sys.stdin/stdout
    directly) so tests can pass io.StringIO. Does not write any file — the
    caller (`cmd_init`) handles the confirm-and-write + auto-validate steps.

    OOB-gateway PR: every site's answered OOB gateway is now written into
    that site's own dict in `topology_dict["sites"][i]["oob_gateway"]`
    (Site.oob_gateway — a real schema field with a real MO to land on,
    build/mgmt.py's mgmtRsOoBStNode.gw), no longer discarded. The second
    return value is kept for backward compatibility with existing callers
    of this function (`cmd_init`'s summary print) — it is site-1's answered
    gateway specifically, now simply a convenience echo of
    `topology_dict["sites"][0]["oob_gateway"]` rather than the only place
    the value lives.
    """
    ans = _AnswerSource(answers)
    out = stream_out

    out.write("This wizard collects the same setup information as a real APIC's\n")
    out.write("first-boot dialog, then generates a topology.yaml for aci-sim.\n")
    out.write("Press ENTER to accept a suggested default shown in [brackets].\n\n")

    # ---- Step 0: admin account ---------------------------------------------
    # The account the APIC plane will ENFORCE (rest_aci/auth.py's aaaLogin
    # 401s on a mismatch) once `aci-sim run` starts — see cmd_run's
    # credential-resolution precedence. NDO shares this account by default
    # (ndo_same_account=yes) but stays lenient regardless (accepts any
    # credential, by design — see Auth's docstring in topology/schema.py); a
    # "no" answer here only changes what NDO is TOLD, never what it checks.
    out.write("Step 0: Admin account\n")
    admin_username = _wizard_prompt_field(
        "admin_username", "  Admin username", "admin", ans, stream_in, out, accept_defaults
    )
    admin_password = _wizard_prompt_field(
        "admin_password", "  Admin password", "cisco", ans, stream_in, out, accept_defaults
    )
    if ans.has("ndo_same_account"):
        ndo_same_account = ans.get("ndo_same_account", "yes").strip().lower() in ("y", "yes", "true", "1")
    else:
        ndo_same_account = _prompt_yes_no(
            "  NDO uses the same admin account as APIC?", True, stream_in, out, accept_defaults
        )
    if not ndo_same_account:
        ndo_username = _wizard_prompt_field(
            "ndo_username", "  NDO admin username", admin_username, ans, stream_in, out, accept_defaults
        )
        ndo_password = _wizard_prompt_field(
            "ndo_password", "  NDO admin password", admin_password, ans, stream_in, out, accept_defaults
        )
    else:
        ndo_username = admin_username
        ndo_password = admin_password
    out.write("\n")

    # ---- Step 1: deployment type -----------------------------------------
    out.write("Step 1: Deployment type\n")
    deploy_choice = _wizard_prompt_field(
        "deployment_type",
        "Deployment type: 1) Single fabric  2) Multi-site (2+ fabrics via ISN+NDO)",
        "2",
        ans,
        stream_in,
        out,
        accept_defaults,
        _validate_int_range(1, 2),
    )
    multi_site = deploy_choice.strip() == "2"

    if multi_site:
        n_sites_str = _wizard_prompt_field(
            "num_sites", "Number of sites", "2", ans, stream_in, out, accept_defaults, _validate_int_range(2, 26)
        )
        n_sites = int(n_sites_str)
    else:
        n_sites = 1
    out.write("\n")

    # ---- Step 2: per-site fields -------------------------------------------
    site_overrides: list[dict[str, Any]] = []
    per_site_leaves: list[int] = []
    per_site_spines: list[int] = []
    per_site_border_pairs: list[int] = []
    first_site_tep_pool = "10.0.0.0/16"
    first_site_infra_vlan = 3967
    first_site_gipo_pool = "225.0.0.0/15"
    first_site_oob_subnet = "192.168.0.0/16"
    first_site_gateway = ""

    for i in range(n_sites):
        idx = i + 1
        out.write(f"Step 2: Site {idx} of {n_sites}\n")

        default_name = f"Site{idx}-IT-ACI"
        name = _wizard_prompt_field(
            f"site{idx}_name", "  Fabric/site name", default_name, ans, stream_in, out, accept_defaults
        )

        site_id = _wizard_prompt_field(
            f"site{idx}_id", "  Site ID", str(idx), ans, stream_in, out, accept_defaults, _validate_int_range(1, 9999)
        )

        default_asn = str(65000 + idx)
        asn = _wizard_prompt_field(
            f"site{idx}_asn",
            "  BGP AS (fabric)",
            default_asn,
            ans,
            stream_in,
            out,
            accept_defaults,
            _validate_int_range(1, 4294967295),
        )

        pod = _wizard_prompt_field(
            f"site{idx}_pod", "  Pod ID", "1", ans, stream_in, out, accept_defaults, _validate_int_range(1, 9999)
        )

        n_controllers_str = _wizard_prompt_field(
            f"site{idx}_controllers",
            "  Number of APIC controllers",
            "1",
            ans,
            stream_in,
            out,
            accept_defaults,
            _validate_int_range(1, 5),
        )
        n_controllers = int(n_controllers_str)

        # Sequential default OOB IP block per site: site1 10.192.0.11, site2
        # 10.192.128.11, ... — matches the existing sandbox convention
        # (topology.yaml's LAB1/LAB2 10.192.0.0/25 vs 10.192.128.0/25 split).
        default_apic_ip = f"10.192.{(i * 128) % 256}.11"
        apic1_ip = _wizard_prompt_field(
            f"site{idx}_apic1_ip",
            "  APIC OOB mgmt IP",
            default_apic_ip,
            ans,
            stream_in,
            out,
            accept_defaults,
            _validate_ip,
        )

        controller_ips = [apic1_ip]
        for c in range(2, n_controllers + 1):
            suggested = _next_ip(controller_ips[-1])
            ctrl_ip = _wizard_prompt_field(
                f"site{idx}_apic{c}_ip",
                f"  APIC-{c} OOB IP",
                suggested,
                ans,
                stream_in,
                out,
                accept_defaults,
                _validate_ip,
            )
            controller_ips.append(ctrl_ip)

        gw_block = ipaddress.ip_address(apic1_ip)
        default_gateway = str(ipaddress.ip_address(int(gw_block) - (int(gw_block) % 256) + 1))
        gateway = _wizard_prompt_field(
            f"site{idx}_gateway",
            "  APIC OOB gateway (shared by all controllers at this site)",
            default_gateway,
            ans,
            stream_in,
            out,
            accept_defaults,
            _validate_ip,
        )

        tep_pool = _wizard_prompt_field(
            f"site{idx}_tep_pool", "  TEP pool", "10.0.0.0/16", ans, stream_in, out, accept_defaults, _validate_cidr
        )
        infra_vlan = _wizard_prompt_field(
            f"site{idx}_infra_vlan",
            "  Infra VLAN (1-4094)",
            "3967",
            ans,
            stream_in,
            out,
            accept_defaults,
            _validate_int_range(1, 4094),
        )
        gipo_pool = _wizard_prompt_field(
            f"site{idx}_gipo_pool",
            "  Multicast/GIPo pool",
            "225.0.0.0/15",
            ans,
            stream_in,
            out,
            accept_defaults,
            _validate_cidr,
        )
        oob_subnet = _wizard_prompt_field(
            f"site{idx}_oob_subnet",
            "  OOB mgmt subnet",
            "192.168.0.0/16",
            ans,
            stream_in,
            out,
            accept_defaults,
            _validate_cidr,
        )
        n_spines = _wizard_prompt_field(
            f"site{idx}_spines", "  Number of spines", "2", ans, stream_in, out, accept_defaults, _validate_int_range(0, 100)
        )
        n_leaves = _wizard_prompt_field(
            f"site{idx}_leaves", "  Number of leaves", "2", ans, stream_in, out, accept_defaults, _validate_int_range(0, 100)
        )
        n_border_pairs = _wizard_prompt_field(
            f"site{idx}_border_pairs",
            "  Number of border-leaf pairs",
            "1",
            ans,
            stream_in,
            out,
            accept_defaults,
            _validate_int_range(0, 20),
        )
        out.write("\n")

        site_overrides.append(
            {
                "name": name,
                "id": site_id,
                "mgmt_ip": apic1_ip,
                "fabric_name": f"{name}-IT-ACI" if not name.endswith("-IT-ACI") else name,
                "asn": int(asn),
                "pod": int(pod),
                "controllers": n_controllers,
                "controller_ips": controller_ips,
                # OOB-gateway PR: this site's own answered gateway now has a
                # real per-site schema field (Site.oob_gateway) and a real MO
                # to land on (build/mgmt.py's mgmtRsOoBStNode.gw) — unlike
                # oob_subnet/tep_pool/infra_vlan/gipo_pool below, which stay
                # fabric-wide (only site 1's answer applies) because their
                # schema fields (Fabric.oob_subnet/tep_pool/infra_vlan/
                # gipo_pool) are fabric-wide by design, matching real APIC
                # where these values are set once and inherited by every
                # later controller joining the fabric.
                "oob_gateway": gateway,
            }
        )
        per_site_leaves.append(int(n_leaves))
        per_site_spines.append(int(n_spines))
        per_site_border_pairs.append(int(n_border_pairs))
        # oob_subnet has no PER-SITE schema field (Fabric.oob_subnet is
        # fabric-wide) — only site 1's answer is applied fabric-wide;
        # tep_pool/infra_vlan/gipo_pool are likewise fabric-wide
        # (Fabric.tep_pool/infra_vlan/gipo_pool), matching real APIC where
        # these ARE fabric-wide values that only the first controller sets
        # (later controllers join the existing fabric and inherit them).
        if i == 0:
            first_site_tep_pool = tep_pool
            first_site_infra_vlan = int(infra_vlan)
            first_site_gipo_pool = gipo_pool
            first_site_oob_subnet = oob_subnet
            first_site_gateway = gateway
    # ---- Step 3: multi-site only (NDO + ISN) -------------------------------
    if multi_site:
        out.write("Step 3: Multi-site (NDO + ISN)\n")
        ndo_ip = _wizard_prompt_field(
            "ndo_mgmt_ip", "  NDO/ND mgmt IP", "10.192.0.10", ans, stream_in, out, accept_defaults, _validate_ip
        )
        isn_ospf_area = _wizard_prompt_field(
            "isn_ospf_area", "  ISN OSPF area", "0.0.0.0", ans, stream_in, out, accept_defaults
        )
        isn_mtu = int(
            _wizard_prompt_field(
                "isn_mtu", "  ISN MTU", "9150", ans, stream_in, out, accept_defaults, _validate_int_range(576, 9216)
            )
        )
        out.write(
            "  note: inter-site EVPN uses each fabric's own BGP AS above — "
            "there is no separate ISN AS.\n\n"
        )
    else:
        ndo_ip = "10.192.0.10"
        isn_ospf_area = "0.0.0.0"
        isn_mtu = 9150

    # ---- Assemble the generate_topology() call -----------------------------
    # A SINGLE call, reusing exactly the same generator `aci-sim new` calls —
    # per-site heterogeneity (name/id/asn/pod/controllers/controller_ips/
    # mgmt_ip, and now leaves/spines/border_pairs counts too, PR-22) is
    # expressed via `site_overrides` + the newly-list-capable
    # leaves_per_site/spines_per_site/border_pairs params, so nothing here
    # re-implements node-ID derivation, tenant scaffolding, or YAML shape.
    fabric_name = "MULTI-SITE-ACI" if multi_site else site_overrides[0]["name"]
    topology = generate_topology(
        sites=n_sites,
        leaves_per_site=per_site_leaves,
        spines_per_site=per_site_spines,
        border_pairs=per_site_border_pairs,
        fabric_name_prefix="",
        ndo_ip=ndo_ip,
        tep_pool=first_site_tep_pool,
        infra_vlan=first_site_infra_vlan,
        gipo_pool=first_site_gipo_pool,
        isn_ospf_area=isn_ospf_area,
        isn_mtu=isn_mtu,
        site_overrides=site_overrides,
    )
    topology["fabric"]["name"] = f"{fabric_name}-IT-ACI" if not fabric_name.endswith("-IT-ACI") else fabric_name
    topology["fabric"]["oob_subnet"] = first_site_oob_subnet
    topology["isn"]["enabled"] = multi_site

    # Admin account (Step 0 above) — always written, even when every field is
    # the wizard's own default (admin/cisco), so a generated topology.yaml is
    # always explicit about which account `aci-sim run` will enforce on the
    # APIC plane rather than relying on cmd_run's silent admin/cisco fallback.
    auth: dict[str, Any] = {"username": admin_username, "password": admin_password}
    if not ndo_same_account:
        auth["ndo_username"] = ndo_username
        auth["ndo_password"] = ndo_password
    topology["auth"] = auth

    return topology, first_site_gateway


def _mask_password(password: str) -> str:
    """First char + '***' (or just '***' if empty) — never echo it in full."""
    return f"{password[0]}***" if password else "***"


def _print_summary(topology: dict[str, Any], oob_gateway: str, stream_out: TextIO) -> None:
    stream_out.write("Summary:\n")
    auth = topology.get("auth") or {}
    if auth:
        admin_line = f"  Admin account: username={auth['username']} password={_mask_password(auth['password'])}"
        if "ndo_username" in auth:
            admin_line += (
                f"  (NDO: username={auth['ndo_username']} "
                f"password={_mask_password(auth.get('ndo_password', ''))})"
            )
        else:
            admin_line += "  (NDO: same account — lenient by design, accepts any credential)"
        stream_out.write(admin_line + "\n")
    stream_out.write(f"  Fabric: {topology['fabric']['name']}\n")
    stream_out.write(f"  ISN/multi-site enabled: {topology['isn']['enabled']}\n")
    if topology["isn"]["enabled"]:
        stream_out.write(
            f"  NDO mgmt IP: {topology['fabric']['ndo_mgmt_ip']}  "
            f"ISN ospf_area: {topology['isn']['ospf_area']}  ISN mtu: {topology['isn']['mtu']}\n"
        )
    if oob_gateway:
        stream_out.write(f"  APIC OOB gateway (site 1): {oob_gateway}\n")
    for site in topology["sites"]:
        n_leaves = len(site["leaves"]) if isinstance(site["leaves"], list) else site["leaves"]
        n_spines = len(site["spines"]) if isinstance(site["spines"], list) else site["spines"]
        n_border = len(site.get("border_leaves", []))
        stream_out.write(
            f"  Site {site['name']} (id={site['id']}, asn={site['asn']}, pod={site['pod']}): "
            f"{site['controllers']} controller(s) {site.get('controller_ips')}, "
            f"oob_gateway={site.get('oob_gateway') or '-'}, "
            f"{n_spines} spines, {n_leaves} leaves, {n_border} border leaves ({n_border // 2} pair(s))\n"
        )
    stream_out.write("\n")


def cmd_init(args: argparse.Namespace) -> int:
    accept_defaults = bool(args.defaults) or not sys.stdin.isatty()
    stream_in: TextIO = sys.stdin
    stream_out: TextIO = sys.stdout

    answers: dict[str, Any] = {}
    if args.answers:
        try:
            answers = _load_answers_file(args.answers)
        except (OSError, ValueError) as exc:
            print(f"ERROR: could not read --answers file: {exc}", file=sys.stderr)
            return 1

    topology, oob_gateway = run_wizard(
        stream_in=stream_in,
        stream_out=stream_out,
        accept_defaults=accept_defaults,
        answers=answers,
    )

    _print_summary(topology, oob_gateway, stream_out)

    ans = _AnswerSource(answers)
    if ans.has("confirm_write"):
        confirmed = ans.get("confirm_write", "yes").strip().lower() in ("y", "yes", "true", "1")
    else:
        confirmed = _prompt_yes_no("Write to topology.yaml?", True, stream_in, stream_out, accept_defaults)

    if not confirmed:
        stream_out.write("Aborted — nothing written.\n")
        return 1

    # Self-check (mirrors `aci-sim new`): the generated file must pass the
    # same validation `aci-sim validate` runs before we ever write it.
    try:
        Topology.model_validate(topology)
    except ValidationError as exc:
        print(
            "ERROR: internal error — generated topology failed validation:\n"
            + _render_validation_error(exc),
            file=sys.stderr,
        )
        return 1

    yaml_text = _SCAFFOLD_HEADER + yaml.dump(topology, sort_keys=False, default_flow_style=False)
    output_path = args.output or DEFAULT_TOPOLOGY_PATH
    Path(output_path).write_text(yaml_text, encoding="utf-8")
    stream_out.write(f"Wrote {output_path}\n")

    # Auto-run validate on the result and print OK (or the errors), per spec.
    try:
        topo = load_topology(Path(output_path))
    except (FileNotFoundError, ValueError) as exc:
        stream_out.write(f"ERROR: {exc}\n")
        return 1
    stream_out.write(_summarize_topology(topo) + "\n")
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aci-sim",
        description="Author, validate, preview, and run aci-sim topology.yaml files.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="Validate a topology.yaml (CI gate).")
    p_validate.add_argument(
        "topology", nargs="?", default=DEFAULT_TOPOLOGY_PATH, help="Path to topology.yaml (default: ./topology.yaml)"
    )
    p_validate.set_defaults(func=cmd_validate)

    p_show = sub.add_parser("show", help="Print the derived inventory (IDs, IPs, ports).")
    p_show.add_argument(
        "topology", nargs="?", default=DEFAULT_TOPOLOGY_PATH, help="Path to topology.yaml (default: ./topology.yaml)"
    )
    p_show.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_show.set_defaults(func=cmd_show)

    p_lldp = sub.add_parser(
        "lldp", help="Show LLDP (or --cdp) neighbors, show-lldp-neighbors-style."
    )
    p_lldp.add_argument(
        "topology", nargs="?", default=DEFAULT_TOPOLOGY_PATH, help="Path to topology.yaml (default: ./topology.yaml)"
    )
    p_lldp.add_argument("--site", default=None, help="Filter to one site by name (default: all sites)")
    p_lldp.add_argument("--node", type=int, default=None, help="Filter to one node id (default: all nodes)")
    p_lldp.add_argument("--cdp", action="store_true", help="Show CDP neighbors instead of LLDP.")
    p_lldp.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_lldp.set_defaults(func=cmd_lldp)

    p_query = sub.add_parser(
        "query",
        help="Authenticated MIT query (aaaLogin -> cookie -> GET) against a running sim — no manual cookie handling.",
    )
    p_query.add_argument("cls", metavar="CLASS", nargs="?", default=None, help="MO class to query, e.g. fabricNode, fvTenant, topSystem")
    p_query.add_argument("--dn", default=None, help="Query a single MO by DN instead of a class query")
    p_query.add_argument("--host", default=DEFAULT_QUERY_HOST, help=f"APIC host:port (default: {DEFAULT_QUERY_HOST}, i.e. port-mode LAB1)")
    p_query.add_argument("--user", default=DEFAULT_QUERY_USER, help=f"Admin username (default: {DEFAULT_QUERY_USER})")
    p_query.add_argument("--password", default=DEFAULT_QUERY_PASSWORD, help=f"Admin password (default: {DEFAULT_QUERY_PASSWORD})")
    p_query.add_argument("--json", action="store_true", help="Emit the raw APIC JSON envelope instead of a compact table.")
    p_query.set_defaults(func=cmd_query)

    p_graph = sub.add_parser(
        "graph", help="Render a self-contained SVG/HTML topology diagram (no server, no CDN)."
    )
    p_graph.add_argument(
        "topology", nargs="?", default=DEFAULT_TOPOLOGY_PATH, help="Path to topology.yaml (default: ./topology.yaml)"
    )
    p_graph.add_argument(
        "-o", "--output", default="topology.html",
        help="Output file path; format is inferred from the extension (.html or .svg). Default: topology.html"
    )
    p_graph.set_defaults(func=cmd_graph)

    p_run = sub.add_parser("run", help="Run the simulator supervisor against a topology.yaml.")
    p_run.add_argument(
        "topology", nargs="?", default=DEFAULT_TOPOLOGY_PATH, help="Path to topology.yaml (default: ./topology.yaml)"
    )
    p_run.set_defaults(func=cmd_run)

    p_new = sub.add_parser("new", help="Scaffold a fresh topology.yaml.")
    p_new.add_argument("--sites", type=int, default=2, help="Number of sites (default: 2)")
    p_new.add_argument("--leaves-per-site", type=int, default=2, help="Regular leaves per site (default: 2)")
    p_new.add_argument("--spines-per-site", type=int, default=2, help="Spines per site (default: 2)")
    p_new.add_argument(
        "--border-pairs", type=int, default=1, help="Border-leaf vPC pairs per site (default: 1 = 2 border leaves)"
    )
    p_new.add_argument("--asn", default=None, help="Comma-separated per-site fabric ASNs (default: 65001,65002,...)")
    p_new.add_argument("--fabric-name-prefix", default="LAB", help="Fabric/site name prefix (default: LAB)")
    p_new.add_argument("--ndo-ip", default="10.192.0.10", help="NDO/ND mgmt IP (default: 10.192.0.10)")
    p_new.add_argument("--apic-ip-base", default="10.192", help="First two octets for per-site APIC mgmt IPs (default: 10.192)")
    p_new.add_argument(
        "--controllers", type=int, default=1, help="APIC cluster size per site (default: 1, range 1-5; single APIC by design — see README)"
    )
    p_new.add_argument(
        "--tep-pool", default="10.0.0.0/16", help="Fabric infra TEP pool CIDR (default: 10.0.0.0/16)"
    )
    p_new.add_argument(
        "--infra-vlan", type=int, default=3967, help="Fabric infrastructure VLAN, 1-4094 (default: 3967)"
    )
    p_new.add_argument(
        "--gipo-pool", default="225.0.0.0/15", help="Multicast GIPo pool CIDR (default: 225.0.0.0/15)"
    )
    p_new.add_argument(
        "--isn-ospf-area", default="0.0.0.0", help="ISN OSPF area, dotted-decimal or decimal (default: 0.0.0.0)"
    )
    p_new.add_argument(
        "--isn-mtu", type=int, default=9150, help="ISN/IPN inter-site uplink MTU, 576-9216 (default: 9150)"
    )
    p_new.add_argument(
        "--isn-ospf-hello", type=int, default=10, help="ISN OSPF hello timer, seconds (default: 10; Tier-3, PR-20)"
    )
    p_new.add_argument(
        "--isn-ospf-dead", type=int, default=40, help="ISN OSPF dead timer, seconds (default: 40; Tier-3, PR-20)"
    )
    p_new.add_argument(
        "--isn-bfd-min-rx", type=int, default=50,
        help="ISN BFD min-rx timer, ms; store-only, no bfdIfP MO built (default: 50; Tier-3, PR-20)"
    )
    p_new.add_argument(
        "--isn-bfd-min-tx", type=int, default=50,
        help="ISN BFD min-tx timer, ms; store-only, no bfdIfP MO built (default: 50; Tier-3, PR-20)"
    )
    p_new.add_argument(
        "--isn-bfd-multiplier", type=int, default=3,
        help="ISN BFD detect multiplier, 1-50; store-only, no bfdIfP MO built (default: 3; Tier-3, PR-20)"
    )
    p_new.add_argument(
        "--default-bd-mac", default="00:22:BD:F8:19:FF",
        help="Fabric-wide default BD gateway MAC (default: 00:22:BD:F8:19:FF, the real ACI default; Tier-3, PR-20)"
    )
    p_new.add_argument("-o", "--output", default=None, help="Write to this file instead of stdout")
    p_new.set_defaults(func=cmd_new)

    p_init = sub.add_parser(
        "init", help="Interactive APIC-setup-style wizard: Q&A, then write + validate a topology.yaml."
    )
    p_init.add_argument("-o", "--output", default=None, help="Write to this path (default: ./topology.yaml)")
    p_init.add_argument(
        "--defaults", action="store_true",
        help="Accept every suggested default without prompting (also auto-enabled when stdin is not a TTY)"
    )
    p_init.add_argument(
        "--answers", default=None,
        help="Path to a YAML/JSON file of pre-filled answers (field name -> value); "
             "unlisted fields fall back to prompting/defaults as usual"
    )
    p_init.set_defaults(func=cmd_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
