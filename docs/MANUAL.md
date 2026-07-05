# aci-sim — Operation Manual / 操作手册

A stateful REST simulator of a Cisco ACI/NDO **management plane** (per-site APIC + Nexus
Dashboard/NDO). It answers `moquery`/class/DN queries computed from an in-memory MIT, applies
`POST`/`DELETE` mutations atomically, and models `aaaLogin` + NDO orchestration — enough fidelity
to test Ansible playbooks, aci-py, autoACI, or any tool that speaks the APIC/NDO REST API, with
**no hardware, no data plane, fully deterministic and resettable.**

> 一个有状态的 Cisco ACI/NDO **管理平面** REST 模拟器(每站点一个 APIC + Nexus Dashboard/NDO)。
> 它从内存中的 MIT 计算 `moquery`/class/DN 查询的响应、原子地应用 `POST`/`DELETE` 变更、模拟
> `aaaLogin` 与 NDO 编排 —— 保真度足以测试 Ansible playbook、aci-py、autoACI,或任何讲 APIC/NDO
> REST API 的工具,**无需硬件、无数据平面、完全确定且可重置。**

---

## 1. Overview / 概述

- **What it does**: serves the APIC REST plane (GET class/mo queries, POST/DELETE mutations,
  aaaLogin) and the NDO/ND REST plane (schema/template CRUD + deploy). Both are backed by one
  in-memory Management Information Tree (MIT) built from `topology.yaml`.
- **What it does NOT do**: no data plane, no real NX-OS/APIC software stack, no live telemetry.
  It models the object model + REST behavior, not packet forwarding.

> **能做什么**:提供 APIC REST 平面(GET class/mo 查询、POST/DELETE 变更、aaaLogin)和 NDO/ND
> REST 平面(schema/template 增删改 + deploy)。两者都由一棵从 `topology.yaml` 构建的内存 MIT 支撑。
> **不做什么**:没有数据平面、没有真实 NX-OS/APIC 软件栈、没有实时遥测。它模拟对象模型 + REST
> 行为,不做报文转发。

**Primary use cases / 主要场景**: Ansible playbook testing · CI gate before real hardware ·
SDK/provider testing · AI-agent sandbox · contract/regression tests · teaching/demo.

---

## 2. Installation / 安装

Requires Python >= 3.11.

```bash
# from a checkout (or  pip install "aci-sim @ git+<repo-url>")
python3 -m venv .venv
.venv/bin/pip install -e .          # runtime deps: fastapi, uvicorn[standard], pydantic, pyyaml
.venv/bin/pip install -e '.[dev]'   # + test deps: pytest, pytest-asyncio, httpx
aci-sim --help
```

> 需要 Python >= 3.11。`pip install -e .` 安装运行依赖(fastapi/uvicorn/pydantic/pyyaml);
> `.[dev]` 额外装测试依赖。装完 `aci-sim --help` 应列出子命令,无 traceback。

---

## 3. Topology & the init wizard / 拓扑与 init 向导

Everything is driven by `topology.yaml` (Pydantic-validated). Author it three ways:

```bash
aci-sim init                 # interactive wizard (APIC setup-dialog style) — RECOMMENDED
aci-sim new --sites 2        # non-interactive scaffold with flags
# or hand-edit topology.yaml
aci-sim validate topology.yaml   # validate before running (CI gate)
aci-sim show topology.yaml       # print the derived inventory (IDs/IPs/ports)
```

The **`aci-sim init` wizard** walks: Step 0 Admin account -> Step 1 Deployment type (single
fabric / multi-site) -> Step 2 per-site (name/id/asn/pod/controllers/IPs) -> Step 3 multi-site
(NDO IP + ISN). Press ENTER to accept the `[bracketed]` default.

> 一切由 `topology.yaml` 驱动(Pydantic 校验)。三种方式创建:`aci-sim init`(交互式向导,
> 仿 APIC setup dialog,**推荐**)、`aci-sim new`(带 flag 非交互脚手架)、手改。运行前用
> `aci-sim validate` 校验。向导顺序:Step 0 管理员账号 -> 1 部署类型 -> 2 每站点参数 -> 3 多站点
> (NDO IP + ISN)。回车接受 `[方括号]` 里的默认值。

---

## 4. Admin account / 管理员账号

The wizard's **Step 0** sets the fabric admin credentials, written to a `topology.yaml` `auth:`
section:

```yaml
auth:
  username: admin
  password: cisco
  # ndo_username / ndo_password only if you gave NDO a separate account
```

- **APIC plane ENFORCES it** — `aaaLogin` returns 401 on a username/password mismatch.
- **NDO plane is lenient by design** (accepts any credential, for cisco.mso/aci-py compat);
  the same account drives it by default.
- **Credential precedence** at `aci-sim run`: an explicit `SIM_USERNAME`/`SIM_PASSWORD` in the
  environment wins (atomic — set either and you own both) -> else the topology `auth:` section ->
  else the built-in `admin`/`cisco` default. `aci-sim run` prints which source is live (never the
  password).

> 向导 Step 0 设置管理员凭据,写进 `topology.yaml` 的 `auth:` 段。**APIC 平面强制校验**(密码错
> 返回 401);**NDO 平面故意宽松**(接受任意凭据,为兼容 cisco.mso/aci-py),默认用同一账号。
> `aci-sim run` 的凭据优先级:环境里显式的 `SIM_USERNAME`/`SIM_PASSWORD` 最高(原子:设了任一就
> 两个都归你)-> 其次 topology 的 `auth:` -> 再次内建默认 `admin`/`cisco`。run 会打印当前凭据来源
> (不打印密码)。

---

## 5. Running — two modes / 运行的两种模式

### 5a. Port mode (default) / 端口模式(默认)

Binds `127.0.0.1` on distinct ports. Simplest; good for local dev + CI.

```bash
aci-sim run topology.yaml
# APIC LAB1 -> https://127.0.0.1:8443   APIC LAB2 -> :8444   NDO -> :8445
```

### 5b. Sandbox mode (real per-device IPs on :443) / sandbox 模式(真实 per-device IP,:443)

Each APIC/NDO gets its **own loopback-alias IP on :443** (like real gear). Needed for tools that
assume one IP per controller (e.g. autoACI multi-site discovery). Requires root (adds `lo`
aliases + binds :443).

```bash
sudo bash scripts/sandbox-up.sh     # adds lo aliases from topology mgmt IPs, binds :443, detaches
#   APIC LAB1 -> https://10.192.0.11:443   APIC LAB2 -> https://10.192.128.11:443
#   NDO       -> https://10.192.0.10:443
sudo bash scripts/sandbox-down.sh   # stop + remove aliases
```

The sandbox IPs come from `topology.yaml`'s `fabric.ndo_mgmt_ip` + each `site.mgmt_ip`. On Linux
they are `lo` aliases -> reachable **only on that host** (run clients on the same box). PID in
`/tmp/aci-sim-sandbox.pid`, log in `/tmp/aci-sim-sandbox.log`.

> **端口模式**(默认):绑 `127.0.0.1` 不同端口,最简单,适合本地开发/CI。
> **sandbox 模式**:每个 APIC/NDO 拿到自己的 loopback 别名 IP、都在 :443(像真机),适合假设"每个
> 控制器一个 IP"的工具(如 autoACI 多站点发现)。需 root(加 `lo` 别名 + 绑 :443)。sandbox IP 来自
> `topology.yaml` 的 `ndo_mgmt_ip` 和各 `site.mgmt_ip`;Linux 上是 `lo` 别名,**只在本机可达**(客户端
> 要在同一台跑)。PID/日志见 `/tmp/aci-sim-sandbox.{pid,log}`。

---

## 6. Connecting clients / 连接客户端

Credentials = your admin account (default `admin`/`cisco`). TLS is self-signed -> disable cert
validation.

**Ansible — cisco.aci (single fabric) / cisco.mso (multi-site):**
```yaml
# inventory: point apic_host / MSO ansible_host at the sim's IP:port
apic_host: "10.192.0.11:443"      # sandbox, or 127.0.0.1:8443 in port mode
apic_username: admin
apic_password: cisco
apic_validate_certs: no
```

**aci-py** (Python pusher): point `--apic` at the APIC IP; for multi-site pass an NDO connector
(`Ndo(host, username, password)`) so tenants become NDO-managed (visible in autoACI).

**autoACI**: log in to the **NDO** IP (`10.192.0.10`) -> Discover -> Connect All Sites. It reads
NDO, so multi-site tenants (created via cisco.mso or aci-py's NDO path) appear there.

> 凭据 = 你的管理员账号(默认 `admin`/`cisco`),TLS 自签 -> 关掉证书校验。Ansible 用 cisco.aci
> (单站点)/cisco.mso(多站点),inventory 里把 `apic_host`/MSO `ansible_host` 指向 sim 的 IP:端口。
> aci-py 把 `--apic` 指向 APIC;多站点要传 NDO 连接器,tenant 才会被 NDO 管理(autoACI 才看得到)。
> autoACI 登录 **NDO** IP(`10.192.0.10`)-> Discover -> Connect All Sites。

> **Multi-site invariant / 多站点不变量**: a tenant is "multi-site" only if it exists in **NDO**.
> An APIC-only push (e.g. aci-py without an NDO connector) creates an APIC-local tenant that
> autoACI's NDO view will NOT show. 名字含多站点语义的 tenant 必须出现在 NDO 上。

---

## 7. Querying the MIT / 查询 MIT

Speaks the plain APIC REST API — works with `curl`, httpx, cisco.aci, or a real APIC.

```bash
# login -> cookie
curl -sk -c j.txt -X POST https://<apic>/api/aaaLogin.json \
  -d '{"aaaUser":{"attributes":{"name":"admin","pwd":"cisco"}}}'
# class query  (== moquery -c fvTenant)
curl -sk -b j.txt https://<apic>/api/class/fvTenant.json
# mo query + subtree
curl -sk -b j.txt "https://<apic>/api/mo/uni/tn-MS-TN1.json?query-target=subtree&target-subtree-class=fvBD"
# LLDP / CDP neighbors
curl -sk -b j.txt https://<apic>/api/class/lldpAdjEp.json
```

`aci-sim lldp topology.yaml [--site N --node N --cdp --json]` prints a `show lldp neighbors`-style
table without hand-writing URLs. Query subscriptions + websocket push-on-change are supported
(`?subscription=yes` + `/socket<token>`).

> 讲标准 APIC REST API,`curl`/httpx/cisco.aci/真机 APIC 都能用。class 查询等价 `moquery -c`;
> mo 查询支持 `query-target=subtree`。`aci-sim lldp` 直接打印邻居表。支持查询订阅 + websocket
> 变更推送(`?subscription=yes`)。

---

## 8. CLI reference / CLI 参考

| Command | Purpose / 用途 |
|---|---|
| `aci-sim init` | interactive wizard -> topology.yaml / 交互向导 |
| `aci-sim new --sites N ...` | non-interactive scaffold / 非交互脚手架 |
| `aci-sim validate FILE` | validate topology (CI gate) / 校验拓扑 |
| `aci-sim show FILE [--json]` | print derived inventory / 打印推导出的清单 |
| `aci-sim lldp FILE [--site --node --cdp --json]` | LLDP/CDP neighbor table / 邻居表 |
| `aci-sim graph FILE -o out.svg\|.html` | render topology diagram / 渲染拓扑图 |
| `aci-sim run FILE` | run the supervisor (port mode) / 起 supervisor(端口模式) |
| `scripts/sandbox-up.sh` / `sandbox-down.sh` | sandbox mode (real IPs :443) / sandbox 模式 |
| `scripts/sim-state.sh {save\|restore} NAME` | save/restore whole-fabric state (§10) / 保存恢复整个 fabric 状态(见 §10) |

---

## 9. `/_sim` control API / 控制 API

For AI-agent sandboxes and test isolation: snapshot / restore / reset the MIT, and seed faults —
so each test starts from a known state.

> 面向 AI agent 沙箱和测试隔离:对 MIT 做 snapshot / restore / reset、注入 fault —— 让每个测试从
> 已知状态开始。

---

## 10. State persistence / 状态保存与恢复

In sandbox/port mode the sim's state is **in-memory only** — restarting the process wipes every
tenant/schema/template on every plane. To survive a restart, save state to disk first and restore
it after.

**Per-plane endpoints** (mirrors the `/_sim` control API in §9):

| Plane | Save | Load |
|---|---|---|
| APIC (each site) | `POST /_sim/save/{name}` | `POST /_sim/load/{name}` |
| NDO | `POST /_sim/save/{name}` | `POST /_sim/load/{name}` |

- APIC's save writes `{name}.{site.id}.apic.json` (keyed by site id, so multiple sites in the same
  `SIM_STATE_DIR` never collide); NDO's save writes `{name}.ndo.json`.
- A missing `load` name returns 404 — an APIC-envelope error (`imdata[0].error`) on the APIC plane,
  a plain `{"detail": ...}` on the NDO plane (matching each plane's existing error shape).
- Files are plain JSON under `SIM_STATE_DIR` (default `~/.aci-sim/state`) — human-readable,
  diffable, safe to check into a fixtures directory for a known-good baseline.

**Wrapper script — save/restore the WHOLE fabric (all APIC sites + NDO) in one command:**

```bash
scripts/sim-state.sh save mybaseline      # snapshot every plane
scripts/sim-state.sh restore mybaseline   # restore every plane (maps to /_sim/load)
```

It derives each plane's address the same way `scripts/sandbox-up.sh` does (straight from
`topology.yaml`'s `fabric.ndo_mgmt_ip` / each `site.mgmt_ip`, sandbox mode's real per-device
`:443` IPs), and falls back to port mode (`127.0.0.1:8443`/`:8444`/`:8445`) if those IPs aren't
reachable — so it works unmodified in either running mode.

**Restart workflow:**

```bash
scripts/sim-state.sh save mybaseline
sudo bash scripts/sandbox-down.sh && sudo bash scripts/sandbox-up.sh   # or Ctrl-C + aci-sim run
scripts/sim-state.sh restore mybaseline
```

**`SIM_STATE_DIR`** — override the base directory for all saved state (default
`~/.aci-sim/state`; created automatically). Tests set this to an isolated `tmp_path` so
nothing is ever written under the real home directory.

> sandbox/端口模式下 sim 状态**只在内存里**——重启进程会清空所有平面上的 tenant/schema/template。
> 要跨重启保留状态,重启前先保存、重启后再恢复。
>
> **各平面端点**(与 §9 的 `/_sim` 控制 API 呼应):APIC(每个站点)和 NDO 都提供
> `POST /_sim/save/{name}` / `POST /_sim/load/{name}`。APIC 的保存文件名为
> `{name}.{site.id}.apic.json`(按 site id 区分,同一个 `SIM_STATE_DIR` 里多个站点不会冲突);
> NDO 的保存文件名为 `{name}.ndo.json`。`load` 遇到不存在的 name 返回 404 ——APIC 平面是
> APIC 信封错误(`imdata[0].error`),NDO 平面是普通 `{"detail": ...}`(各自匹配平面已有的
> 错误格式)。文件是 `SIM_STATE_DIR`(默认 `~/.aci-sim/state`)下的纯 JSON——可读、可
> diff,也可以存进 fixtures 目录当已知良好基线用。
>
> **一键保存/恢复整个 fabric**(所有 APIC 站点 + NDO):`scripts/sim-state.sh save <name>` /
> `scripts/sim-state.sh restore <name>`(`restore` 对应 `/_sim/load`)。地址推导方式与
> `scripts/sandbox-up.sh` 一致(从 `topology.yaml` 读 `ndo_mgmt_ip`/各 `site.mgmt_ip`),这些 IP
> 不可达时自动退回端口模式(`127.0.0.1:8443`/`:8444`/`:8445`),两种运行模式下都能直接用。
>
> **重启工作流**:先 `sim-state.sh save` → 重启 sim(`sandbox-down.sh && sandbox-up.sh`,或
> Ctrl-C 后 `aci-sim run`)→ 再 `sim-state.sh restore`。
>
> **`SIM_STATE_DIR`**:覆盖所有保存状态的根目录(默认 `~/.aci-sim/state`,自动创建)。
> 测试会把它设成隔离的 `tmp_path`,绝不会写到真实 home 目录下。

---

## 11. Multi-site vs single-fabric / 多站点 vs 单站点

- **Single-fabric**: one APIC. Tenants pushed directly via cisco.aci — APIC-local, no NDO.
- **Multi-site**: NDO + per-site APICs. Tenants are created as NDO schemas/templates and
  **deployed** to sites; only then do they appear on the site APICs (and in autoACI).
- **multi-pod ~= single-fabric** for Ansible tenant testing — the IPN lives on external Nexus (NX-OS),
  not cisco.aci; the only per-pod nuance is the pod id in static binding paths, covered by `site.pod`.

> 单站点:一个 APIC,tenant 经 cisco.aci 直推,APIC 本地,无 NDO。多站点:NDO + 各站点 APIC,tenant
> 建成 NDO schema/template 再 **deploy** 到站点,之后才落到站点 APIC(和 autoACI)。对 Ansible
> tenant 测试,**multi-pod ~= single-fabric** —— IPN 在外部 Nexus 上(NX-OS),不经 cisco.aci。

---

## 12. Troubleshooting / 排错

| Symptom / 现象 | Cause & fix / 原因与解决 |
|---|---|
| `aaaLogin` 401 | password != the enforced account; check `auth:` / `SIM_USERNAME`+`SIM_PASSWORD` / the `run` banner. |
| sandbox: `Needs root` | `sudo bash scripts/sandbox-up.sh` (needs `lo` alias + :443 bind). |
| sandbox: nothing on :443 after start | old listener still holds the socket; the script waits for it to free — check `/tmp/aci-sim-sandbox.log`. |
| deep-root subtree query returns 0 | query a DN that has a materialized MO (e.g. `.../sys`, not an un-materialized container). |
| client can't reach sandbox IP from another host | sandbox IPs are `lo` aliases -> same-host only; use port mode + the host LAN IP for remote clients, or run the client on the sim host. |
| `pip install` CLI crashes on import | ensure v0.13.1+ (earlier packaging didn't declare deps / omitted subpackages). |

---

*Generated as part of the sim playbook-E2E effort. For the REST contract details see
`docs/CONTRACT.md`; for design rationale see `docs/DESIGN.md`; for the changelog see `CHANGELOG.md`.*
