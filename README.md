# hihaff → Actions 部署 & 运行 ARTEX + frp 反代公网

目标：在 **Janit8r/hihaff** 仓库上用 GitHub Actions 从源码构建并运行
[200210xsb/artex-full](https://github.com/200210xsb/artex-full)（Go 后端 + Next 静态前端），
再用 **frpc** 把 Web 端口反代到公网 frps **203.195.195.52:7023**。

> **源码仓库**：`https://github.com/200210xsb/artex-full.git`（默认分支 `main`，
> 已从旧上游仓库切换过来）。要换源，改 `.github/workflows/deploy-artex-frp.yml` 里两处：
> 顶层 `env.ARTEX_REPO` 和 `Checkout ARTEX` 步骤的 `repository: 200210xsb/artex-full`，
> 两处必须指向同一个仓库。

## 0. 架构

```
GitHub Actions runner (ubuntu-latest, 临时)
├── postgres:16-alpine   (job service, 127.0.0.1:5432)
├── artex  :8787         (go build -tags embedui，内嵌前端)
│         :8788          (MITM 流量记录代理，仅本地)
├── .artex-mcp/bin       (5 个 stdio MCP 脚本 + subfinder，注册进 mcp_servers)
├── .artex-state/        (db 快照 / data / payloads/webshell / MCP 模板索引)
├── /opt/artex -> .artex-nuclei (nuclei 二进制 + 模板库，Go 常量路径)
├── nginx  :8080         (边缘层：托管静态资源 + gzip_static + 长缓存，/api 反代 8787)
└── frpc ── TCP ──> 203.195.195.52:7023 (frps)
                     └── remotePort 18087 ──> 公网
公网访问： http://203.195.195.52:18087
```

### 性能调优（针对隧道带宽瓶颈）

实测该链路（客户端 → 203.195.195.52:7023 → GitHub runner）：单流吞吐 **≈14KB/s**，
6 路并发总吞吐基本不变（说明是 frps 侧带宽上限，不是并发限制），并伴随传输截断（丢包）。
首屏 17 个资源共 **1.29MB**，原样传输需 **80s+**。故加 nginx 边缘层，按收益排序：

| 措施 | 效果 |
|---|---|
| 预压缩（`gzip -9` 生成 `.gz`）+ `gzip_static on` | 实测 JS 压到 **39.9%**，1.29MB → ≈515KB |
| `/_next/static/*` 一年 `immutable` 缓存 | 二次访问**零下载**（文件名带 hash） |
| 页面 60s 短缓存 | 减少重复往返 |
| nginx 直接托管静态文件 | 省掉 Go 转发开销 |
| `transport.poolCount = 4` + 心跳收紧 | 丢包后更快恢复 |
| upstream `keepalive` + `proxy_http_version 1.1` | API 不必每次重建连接 |

预期：首屏 ≈80s → **≈30s**；同一浏览器二次访问**接近秒开**。
再快只能提高链路带宽（frps 规格/线路）或改国内常驻部署。

### 跨轮数据持久化（不用每次重跑）

runner 是临时的，`postgres` 作为 service 容器随 job 销毁，所以默认每轮都是空库。
解决办法：**把数据库转储 + ARTEX 数据目录放进 Actions 缓存**，下一轮先恢复再启动。

```
本轮结束前                      下一轮开始
pg_dump → .artex-state/db.sql.gz   actions/cache/restore
tar data → .artex-state/data.tgz    →  psql 恢复（失败即中止，不用空库覆盖）
      ↓                                  ↓
actions/cache/save                 artex -data .artex-state/data
```

| 关键点 | 说明 |
|---|---|
| `STATE_DIR` | `$GITHUB_WORKSPACE/.artex-state`，缓存路径 |
| 缓存 key | `artex-state-<run_id>-mid` / `-final`，唯一不覆盖 |
| 恢复方式 | `restore-keys: artex-state-` 前缀匹配，取**最近一次** |
| `-data` 指向 | `${STATE_DIR}/data`，ARTEX 自身数据也随缓存走；`data/` 缺失时从 `data.tgz` 兜底还原 |
| 周期快照 | keepalive 每 10 分钟调一次 `snapshot.sh`，保证 `STATE_DIR` 始终接近最新 |
| 显式检查点 | 第 30 分钟 `Snapshot + Save state cache`，即使随后整轮被 cancel 也已进缓存 |
| 取消时抢救 | 收尾快照与保存是 `if: always()`，被下一轮顶掉时再写一次缓存；最坏丢 ≤10 分钟 |
| 整轮时长 | ≈5h33m（两段 30 + 300 分钟），**远大于** cron 间隔 → 通常被下一轮 cancel，见第 5 节 |
| 缓存保留 | 最近 12 份 + 本轮实际恢复的那一份（永不淘汰，作为可回滚的已知好快照） |

#### 为什么之前「每轮都是新数据」—— 已修复

实测日志（run `43150501982`）：缓存其实**每次都命中了**，但数据库恢复失败，
而旧代码把失败降级成「继续用空库启动，不影响上线」，于是看起来像没做持久化；
更糟的是 50 分钟后的段间快照会把这个空库写回缓存，**把上一轮数据永久覆盖掉**。

```
ERROR:  unrecognized configuration parameter "transaction_timeout"
恢复失败（继续用空库启动，不影响上线）：
```

根因是 **pg_dump / 服务端版本错配**：runner 现在跑 ubuntu-26.04，自带 `pg_dump 18.6`，
dump 头会写 `SET transaction_timeout = 0;`（PG18 新增的 GUC），而 service 是
`postgres:16-alpine` —— 服务端不认识，`psql -v ON_ERROR_STOP=1` 在第 4 行就中断。

现在的处理：

1. 导出时直接过滤 PG18 专有的 `transaction_timeout`，写进缓存的快照在 PG16 上可直接导入；
2. 恢复时若仍遇到「未知 GUC」，自动解析报错参数、加入过滤表、重置 `public` schema 后重试（最多 6 轮）；
3. 恢复后校验 `public` 表数量不少于 dump 里的 `CREATE TABLE` 数，确认真的恢复了；
4. **任何恢复失败都直接中止 job**，绝不用空库继续跑、绝不覆盖上一份好快照；
5. 快照脚本加 `pipefail` + `gzip -t` 校验 + 原子 `mv`，导出失败一律保留旧快照
   （旧版没有 `pipefail`，`pg_dump` 挂了也可能被当成成功而写入截断的 dump）。

#### 验证与回滚

job Summary 的「跨轮持久化」块会显示 `恢复来源`、`数据库恢复: true` 和 `public 表数量`；
连跑两轮，第二轮应显示 `恢复来源: artex-state-<上一轮run_id>-final`，且表数量与上一轮一致。

某一轮产生了坏快照时，Actions → **Run workflow** → 填 `state_cache_key`
（例如 `artex-state-43150501982-final`）即可强制回到任意历史快照；
留空则自动取最近一次。也可以在 Summary 里看缓存 key 列表。

ARTEX 关键事实（已核对源码）：

| 项 | 值 | 来源 |
|---|---|---|
| Web 端口 | `:8787` | `cmd/artex/main.go` 的 `-addr` 默认值 |
| 代理端口 | `127.0.0.1:8788` | `-proxy` 默认值 |
| 数据目录 | `-data`，默认可执行文件旁 `data/` | `main.go` |
| 数据库 | 必须 PostgreSQL，`ARTEX_PG_DSN` 或 `config.json` | `main.go` / `config.example.json` |
| skill 目录 | `ARTEX_SKILL_DIR` > config `skill_dir` > 可执行文件旁 `skills/` | 同上 |
| 前端构建 | `cd web && npm ci && npm run build:static` → `web/out` | `web/package.json` |
| 内嵌前端 | `cp -a web/out/. server/webui/dist/` 后 `go build -tags embedui` | `server/webui_embed.go`、`build.sh` |
| Go 版本 | 由 `go.mod` 决定（`go 1.26.10`） | `go.mod` |

## 1. frps 服务端准备（203.195.195.52）

1. 安装 frps（[releases](https://github.com/fatedier/frp/releases)）：
   ```bash
   curl -fsSL -o frp.tgz https://github.com/fatedier/frp/releases/download/v0.61.0/frp_0.61.0_linux_amd64.tar.gz
   tar -xzf frp.tgz && install -m755 frp_0.61.0_linux_amd64/frps /usr/local/bin/frps
   ```
2. 写配置 `/etc/frp/frps.toml`（见 `deploy/frps.toml`）。`bindPort` 与 `auth.token`
   用 frp 原生的环境变量模板语法读取，真实值只放在服务器本地的
   `/etc/frp/frps.env`，**必须与 GitHub Environment `production` 下的两个 secrets 一致**：
   - `FRP_BIND_PORT = 7023` → `bindPort`
   - `FRP_TOKEN = <强口令>` → `auth.token`（不要写进仓库文件）
   - `allowPorts` 必须包含 `18087`（或你自定义的 `FRP_REMOTE_PORT`）

   ```bash
   sudo tee /etc/frp/frps.env >/dev/null <<'EOF'
   FRP_BIND_PORT=7023
   FRP_TOKEN=<与 GitHub Environment secret FRP_TOKEN 相同的值>
   EOF
   sudo chmod 600 /etc/frp/frps.env
   # systemd unit 里加 EnvironmentFile=/etc/frp/frps.env，然后 restart frps
   ```

3. 放行端口：
   ```bash
   # 云厂商安全组放行 7023 与 18087；主机防火墙：
   ufw allow 7023/tcp && ufw allow 18087/tcp
   ```
4. 启动并设为开机自启：
   ```bash
   frps -c /etc/frp/frps.toml        # 前台试跑
   # 正式：写 systemd unit 或 nohup frps -c /etc/frp/frps.toml &
   ss -lntp | grep -E '7023|18087'
   ```

> 如果 **7023 其实是你要访问的 Web 端口**（即 frps 的 `vhostHTTPPort`），
> 把 `deploy/frpc.toml` 里的 proxy 改成 `type = "http"` + `customDomains`，
> 并让 frps 配 `vhostHTTPPort = 7023`（此时 secret `FRP_BIND_PORT` 换成别的端口，例如 7000）。

## 2. GitHub Environment secrets（必填）

frpc 的 `auth.token` 与 `serverPort`（= frps `bindPort`）都从 **Environment secrets** 注入，
仓库里不再有明文。工作流在 job 上声明了 `environment: production`，所以 secret 必须建在
**Settings → Environments → production → Environment secrets**，不是仓库级 Secrets。
（同名仓库级 secret 会被 Environment 级覆盖，Environment 级优先。）

**Environment `production`（必建）**

| Secret 名称 | 值 | 用途 |
|---|---|---|
| `FRP_TOKEN` | 与 frps `auth.token` 完全一致的强口令 | 渲染 frpc 的 `auth.token` |
| `FRP_BIND_PORT` | `7023` | 渲染 frpc 的 `serverPort`（= frps `bindPort`） |

> 两个值都不落仓库：服务端放 `/etc/frp/frps.env`（权限 600），客户端放 GitHub Environment secrets。
> 缺任一个，`Render frpc.toml` 会直接失败，不会静默连错端口或空 token。
> 口令含 `*` `!` 等字符时网页直接填入即可，工作流会做 TOML 转义，不要自己加引号；
> `FRP_BIND_PORT` 只填纯数字。日志里端口显示 `***` 是 GitHub 自动脱敏，属正常现象。
> **不要**给 `production` 加 required reviewers / wait timer，否则 cron 轮次会卡在等待审批。

用 gh CLI 创建（口令交互式输入，不落命令行历史）：

```bash
gh api -X PUT repos/Janit8r/hihaff/environments/production --input - <<<'{}' >/dev/null
gh secret set FRP_BIND_PORT --env production --repo Janit8r/hihaff --body '7023'
gh secret set FRP_TOKEN --env production --repo Janit8r/hihaff   # 回车后粘贴口令
gh secret list --env production --repo Janit8r/hihaff
```

**仓库级 Secrets（按需）**

| 名称 | 说明 |
|---|---|
| `ANTHROPIC_API_KEY` | LLM 调用 |
| `OPENAI_API_KEY` | LLM 调用 |

**Variables（可选，也可以建在 Environment 上）**

| 名称 | 默认 | 说明 |
|---|---|---|
| `FRP_REMOTE_PORT` | `18087` | 公网访问端口，须在 frps `allowPorts` 内 |
| `ARTEX_LLM_PROVIDER` / `ARTEX_LLM_MODEL` / `ARTEX_LLM_BASE_URL` | 空 | LLM 接入点 |
| `ARTEX_NUCLEI_ASSETS` | `1` | 设成 `0` 跳过 nuclei / 模板库下载（只留空目录占位） |
| `ARTEX_MCP_REGISTER` | `1` | 设成 `0` 跳过 MCP 注册（只铺脚本与资产，不动数据库） |
| `ARTEX_MCP_OOB` | `0` | 设成 `1` 才注册 OOB MCP；**仅让页面可见，真实回连仍不可用**，见下 |
| `ARTEX_ENGLISH_DEFAULTS` | `1` | 设成 `0` 完全关掉英文补丁，回到上游中文内置模板/提示词 |
| `ARTEX_ENGLISH_DEFAULTS_FORCE` | `0` | 设成 `1` 强制重跑一次数据库英文化迁移（会覆盖用户手改的提示词） |

### Nuclei POC 模板库（切到 artex-full 后新增的依赖）

`200210xsb/artex-full` 的 `server/nuclei.go` 与 `server/assets_extra.go` 把

```
/opt/artex/bin/nuclei
/opt/artex/nuclei-templates
```

写成 **Go 常量**（没有环境变量开关，原本是给常驻部署 `/opt/artex` 用的）。
裸 runner 上没有这两个路径，打开 POC / 模板库页面就会返回
`模板目录不可用: /opt/artex/nuclei-templates`。

工作流在 `Start artex` 之前加了三个步骤解决：

| 步骤 | 作用 |
|---|---|
| `Restore nuclei assets cache` | 固定 key `artex-nuclei-v1-3.11.1`，跨轮复用，只在首次 miss 时下载 |
| `Prepare nuclei POC assets` | 下载 nuclei v3.11.1 release + `nuclei-templates` 的 codeload tarball（不带 `.git`）；把 `/opt/artex` **整层软链**到 `.artex-nuclei` |
| `Save nuclei assets cache` | 仅 cache miss 且至少一个资产下载成功时写缓存 |

两个容易踩的坑：

- **不能只给 `nuclei-templates` 做软链**：Go 的 `filepath.WalkDir` 对根节点用 `Lstat`，
  根节点是符号链接时只访问链接本身，会扫到 **0 个 YAML**。软链放在中间层
  (`/opt/artex` → 资产目录) 时，最终组件是真实目录，才能正常递归。
- 下载失败只降级、不阻断部署：目录始终存在，页面显示空模板列表而不是抛错；
  全失败时不写空缓存，下一轮会重新尝试。

首次运行会多花 1~3 分钟下载并打包约百 MB 的模板库，之后每轮命中缓存只需几秒。
升级 nuclei 版本时，同步改 `env.NUCLEI_VERSION` 和两个缓存步骤的
`key: artex-nuclei-v1-<版本>`（版本进 key 才能触发重新下载）。
不想付这个代价就把 Variable `ARTEX_NUCLEI_ASSETS` 设为 `0`。

### MCP 工具链（Subfinder / WebPath / Webshell / Nuclei）

`artex-full` 的扫描、子域枚举、路径枚举、拿 shell 能力**不在二进制里**，由 5 个外挂
stdio MCP（纯标准库 Python）子进程提供。`db/schema.sql` 只 seed 一条 `ScopeSentry`
（`enabled=false`）；此前工作流完全没有安装/注册步骤，所以「系统 → MCP」是空的、
agent 手里没有工具。

补齐后的步骤：

| 步骤 | 作用 |
|---|---|
| `Checkout deployment assets` | 把本仓库签出到 `.ci-assets`，取 `deploy/payloads/webshell/` 种子；与 ARTEX 的 checkout 路径不同，互不覆盖 |
| `Restore / Save MCP toolchain cache` | 固定 key `artex-mcp-tools-v1-2.16.0`，跨轮复用 subfinder 二进制 |
| `Prepare MCP toolchain assets` | 从 ARTEX checkout 铺 5 个 `*_mcp.py` + `subs.txt`；下载 subfinder v2.16.0；生成 provider-config；建 payload 目录 |
| `Register MCP servers` | 跑官方 `*/register-mcp.sh`，写 `mcp_servers` + `agent_visibility` |

**注册必须放在 `Wait for health` 之后**：`mcp_servers` / `agents` / `agent_visibility`
是 artex 启动时执行 `db/schema.sql` 才建的表。注册结果存在 PostgreSQL 里，随
`db.sql.gz` 快照跨轮保留；脚本用 `ON CONFLICT (name) DO UPDATE`，每轮重跑幂等
（注意：这会重新置 `enabled=true`，页面里手动禁用的服务下一轮会被再次启用）。

> 踩过的坑：上游 `nuclei/register-mcp.sh` 的 `build_env_sql()` 在带 `--env` 时会拼出
> `jsonb_build_object('KEY',,'VALUE')` 这种**双逗号非法 SQL**（数组元素里自带逗号，
> 又被 `IFS=,` 拼了一次）。本地模拟抓到后，工作流不给它传 `--env`；Nuclei 的模板索引 /
> 日志路径与 WebPath 的日志 / 代理地址统一用一条
> `UPDATE mcp_servers SET env = env || jsonb_build_object(...)` 覆写，绕开这个 bug。

| MCP | 状态 | 说明 |
|---|---|---|
| Nuclei | ✅ | 指向 `/opt/artex/bin/nuclei` + `/opt/artex/nuclei-templates`；14k 模板的索引放 `STATE_DIR/mcp/nuclei-mcp`，跨轮复用。`poc_update` 因模板库是 codeload tarball（无 `.git`）不可用 |
| Subfinder | ✅ | `subfinder` v2.16.0 + `subs.txt`；`subfinder_enum` / `bruteforce_enum` / `dns_resolve` / `http_probe` 无需 key，`hunter_enum` 等要 provider-config 里的 key |
| WebPath | ✅ | 纯标准库，走已有的 `127.0.0.1:8788` 录制代理；缺 CA 证书时自动直连 |
| Webshell | ✅ | 会话库与页面共用 `STATE_DIR/data/webshells.json`；payload 目录见下一节 |
| OOB | ❌ 默认不注册 | runner 在 NAT 后、frp 只映射 `18087`，没有公网常驻监听器与 `8889/18666/18667`（甚至 53/UDP）；`ARTEX_MCP_OOB=1` 只让页面可见，真实回连仍收不到 |

工具列表不会在注册瞬间出现：MCP 是「随 agent 意图 spawn」的。想立刻看到，去页面
「系统 → MCP」点某个服务的**刷新**（`pgRefreshMCP` 会重新发现并写 `mcp_tools_cache`），
或让 agent 领一条意图。

#### Webshell payload 库 500 已修

`server/webshell.go` 的 `webshellPayloadRoot()` 默认取「可执行文件旁的
`payloads/webshell`」，runner 上二进制在 `/tmp` 且没有这个目录，于是
`GET /api/webshell/payloads` 直接 500 `payload 目录不存在: ...`。
现在用 `ARTEX_WEBSHELL_PAYLOAD_DIR` 指到 `${STATE_DIR}/payloads/webshell`，
`Prepare MCP toolchain assets` 建出 `php/ jsp/ aspx/` 三个子目录并同步
`deploy/payloads/webshell/` 的种子 → 接口返回 200 + 3 条探针。

- 上游**不分发**真实 payload（"可直接落地的马不入库"）。本仓库只放 3 个
  **无命令执行能力的连通性探针**（回显一个标记），用来验证挂载、检索、读取与
  元信息解析链路；需要真实 payload 请按 `<类型>/<文件名>.<类型>` 自己放进
  `deploy/payloads/webshell/`，push 会触发新一轮部署。格式见该目录的 README。
- 同名种子每轮覆盖，其它文件一律保留；整个目录随 `.artex-state` 缓存跨轮保留，
  不会每轮清空。
- Webshell 页面侧（会话 CRUD / exec / upload / download）本来就是纯 Go 实现，
  会话库也随状态缓存保留，不依赖 MCP 即可使用。

### 英文默认：ARTEX 内部 agent ↔ LLM 的消息全量英文化

> 范围只针对 **ARTEX 内部 agent 与 LLM 之间的消息**（系统提示词、内置任务模板、
> 运行时注入的规约、收尾词、报告模板）。我们之间在 WorkBuddy 里的对话不受影响。
> 起因是部分模型不支持中文，中文 system prompt 会让这些模型输出异常。

只改数据库**不够**，中文来自三层，工作流分别处理：

| 层 | 中文来源 | 处理方式 |
|---|---|---|
| ① 内置提示词 | `agent/*.go` 的 11 个常量：`goalsDefaultTmpl`、`plannerDefaultTmpl`、`mainAgentDefaultTmpl`、`workerDefaultTmpl`、`autoDefaultTmpl`、`pentestDefaultTmpl`、`redteamDefaultTmpl`、`goalsScopeTail`、`assetCollectionRule`、`DefaultAssistantPrompt`、`ReporterDefaultPrompt` | 构建前按**常量名**整体替换成完整英文版，并在 9 个完整 system prompt 末尾追加 `LANGUAGE_POLICY`（最高优先级：所有推理、TodoWrite、工具参数、结论、报告一律英文；上下文里的中文只当证据数据） |
| ② 代码固定注入片段 | `worker.go` 的流量/出口/上报门槛、`planner.go` 的态势与唤醒块、`wrapup.go` 的收尾词、`constraints.go` 的操作约束、`finding_workflow.go` 的编号与证据规约等 ~100 个中文字符串 | 构建前按**内容哈希**替换整个 Go 字符串字面量；这些片段是代码拼在 DB 正文之后的**段 [B]/[C]**，后台页面里根本改不掉 |
| ③ 内置任务模板 | `db/task_templates_seed.json` 的 7 个模板（名称/描述/目标） | 构建前改写嵌入的 JSON；`nkey` **原样保留**，含 `web 渗透`、`攻防演练` 这两个中文内部键 |

配套的两个工作流步骤：

- `Apply English defaults to ARTEX source`（`Show source revision` 之后、`go build` 之前）
  执行 `deploy/i18n/en/apply_en.py`，同时产出 `${I18N_DIR}/en_defaults.sql`。
- `Apply English defaults to persisted DB`（`Wait for health` 之后）
  执行该 SQL。`agent.PromptOverride` 让 **DB 正文优先于代码默认**，所以老库里
  已经播种的中文 `agent_prompts` / `task_templates` 必须显式改写，否则源码补丁无效。

幂等与安全性：

- 数据库迁移是**一次性**的：`settings.artex_i18n_en_v1` 置位后，所有 `UPDATE`
  都会跳过。续期轮每 ~5.5 小时跑一次，但不会反复覆盖用户后来在后台手改的提示词。
  要重置就把 `ARTEX_ENGLISH_DEFAULTS_FORCE` 设为 `1` 跑一轮（会覆盖手改内容）。
- 自定义 agent 不是内置模板：保留其正文，仅在末尾追加 `LANGUAGE_POLICY`。
- `agents.wrapup_prompt` / `task_timeout_wrapup_prompt` 为空时不动，直接用
  已经英文化过的代码默认值；非空且仍是中文才改写。
- 补丁失败只告警、不阻断上线；此时删掉半成品 SQL，避免把半写好的迁移灌进数据库。
- 校验：`apply_en.py` 会扫描打完补丁的 `agent/*.go`，要求**中文字符串字面量为 0**，
  并检查 Go 词法状态（防替换出未闭合的字符串导致 `go build` 失败）。
  `db/*.go`、`server/*.go` 里剩下的中文是界面标签 / 日志文案，不会进入 LLM 消息。

资源文件：`deploy/i18n/en/` 下
`prompts_en.py`（11 个完整英文提示词 + 语言策略）、
`templates_en.py`（7 个英文任务模板）、
`strings_en.py`（代码固定片段英译 + 英文收尾词）、
`apply_en.py`（补丁器 + SQL 生成器）。上游改了文案时，哈希不匹配会打
`::warning::` 并在 Summary 里列出，不会静默把中文放回提示词。

## 3. 落地工作流

把 `.github/workflows/deploy-artex-frp.yml` 放到 hihaff 仓库，二选一：

### 方式 A：网页上传（无需本地 git）

1. 打开 `https://github.com/Janit8r/hihaff` → **Add file → Create new file**
2. 文件名填 `.github/workflows/deploy-artex-frp.yml`
3. 粘贴本文件全部内容 → **Commit changes**（提交到 main）

### 方式 B：命令行

```powershell
# 若本机没有 git：
winget install --id Git.Git -e --source winget

cd C:\Users\Administrator\Desktop\ha\deploy-hihaff
git init
git remote add origin git@github.com:Janit8r/hihaff.git
# 复用已生成的部署密钥
$env:GIT_SSH_COMMAND='ssh -i C:\Users\Administrator\Desktop\ha\keys\id_ed25519_ghdeploy -o StrictHostKeyChecking=accept-new'
git add .github deploy
git commit -m "ci: deploy ARTEX via GitHub Actions + frp tunnel"
git push -u origin main
```

> 已确认该部署密钥对 `Janit8r/hihaff` 认证成功（`Hi Janit8r/hihaff!`），且带写权限即可 push。

## 4. 触发与验证

```bash
# 触发：Actions → artex-deploy-frp → Run workflow
# 或 push 后自动触发；此后靠「Queue successor run」自我续期（cron 只作兜底，见第 5 节）

# 公网验证（frps 所在网络或任意外网）
curl -sS -o /dev/null -w '%{http_code}\n' http://203.195.195.52:18087/
curl -sS http://203.195.195.52:18087/api/health
```

Job 首页 Summary 会打印公网地址、ARTEX commit、frps 地址与凭据来源（Environment secrets）、
「跨轮持久化」块的恢复来源 / 数据库恢复状态 / public 表数量，以及「MCP 工具链」块
（已注册服务、OOB 开关、Webshell payload 目录）。
`Wait for health`、`Start frpc`、`Keep alive` 三步的日志里能看到探活与 frp 握手结果；
`Prepare MCP toolchain assets` 会打印 `MCP 脚本: 5/5` 和 `payload 库 ... 个条目`，
`Register MCP servers` 会打印 `mcp_servers` 清单。登录后到「功能 → WebShell → Payload 库」
应看到 3 条探针，到「系统 → MCP」应看到 4 个已启用服务。

## 5. 在线保障：自我续期（不依赖 GitHub 定时调度）

GitHub 托管 runner **是临时的**，单个 job 最长 6 小时，结束即销毁，进程与隧道随之消失。

### 结论先行：不能再靠 schedule

实测这个仓库的 GitHub 定时调度**连续丢班**（UTC）：

| 阶段 | 配置 | 结果 |
|---|---|---|
| 最初 | `0 */2`（整点） | 18:00 班延迟 87 分钟、08:00 班延迟 71 分钟、10:00 班整班被 drop |
| 错峰 + 拉长 job | `17 */2` + 5h33m | 02:38 那轮跑满后正常结束，但 **04:17 / 06:17 / 08:17 三班全都没创建 run**，公网从 08:11 起 502 |
| 新建轻量看门狗 | `3,13,23,33,43,53 * * * *` | **08:33 / 08:43 两班同样没触发** —— 连秒级 workflow 的 schedule 都丢 |

所以主力改成 **自我续期**：每一轮服务就绪后，立刻用 `GITHUB_TOKEN` 触发下一轮
`workflow_dispatch`，把它排进 `concurrency` 队列。之后本轮无论正常结束、失败、被 cancel
还是被 timeout 回收，排队的下一棒都会立即接上 —— **完全不需要等任何 cron**。

GitHub 文档明确说 `workflow_dispatch` / `repository_dispatch` 是「GITHUB_TOKEN 触发的事件
不会再启动新 run」这条规则的**例外**，因此无需 PAT 或外部定时服务。

### 三层保障

| 机制 | 角色 |
|---|---|
| 主 workflow 的 `Queue successor run` 步骤 | **主力**：服务就绪即排下一棒 |
| `.github/workflows/keep-online.yml` 看门狗 | 兜底：发现没有活跃/排队 run 就补一轮；但它的 schedule 同样可能被丢，不能当主力 |
| 主 workflow 的 `schedule: 17 */2` | 最后的兜底 |

`concurrency` 用 `cancel-in-progress: ${{ inputs.successor != 'true' }}` 区分两种触发：

- 自我续期传 `successor=true` → 求值为 `false` → **排队**，等当前轮完整跑完（含数据快照
  收尾）再接棒，不打断服务；
- `push` / `schedule` / 手动触发 → 求值为 `true` → 立即顶掉旧轮，保证改代码能马上生效。

GitHub 保证同一组最多 1 running + 1 pending，不会堆积；稳态就是「1 个在跑 + 1 个排队」。

关于错峰（虽已不是主力，仍是兜底），官方文档原文：

> "The `schedule` event can be delayed during periods of high loads... High load times include
> the start of every hour. If the load is sufficiently high enough, some queued jobs may be dropped.
> To decrease the chance of delay, schedule your workflow to run at a different time of the hour."

所以 `cron` **绝对不要写 `0 ...` 整点**，cron 用 UTC。

### 空窗与数据

- 接棒轮已排队，当前轮一结束就立刻开始构建，空窗只有 **3~5 分钟**（构建 + frpc 握手），
  不再有「等下一班 cron」的 1~2 小时真空。
- 风险点：`Queue successor run` 本身失败（API 故障）会断链。它带 3 次重试；真断了用
  `gh workflow run deploy-artex-frp.yml --repo Janit8r/hihaff` 手工接上。
- 想让兜底也可靠：用外部定时服务（cron-job.org / Cloudflare Workers Cron）每 10 分钟调用
  `POST /repos/Janit8r/hihaff/actions/workflows/keep-online.yml/dispatches`（需 PAT）。
- 数据保存三重保险：30 分钟处显式 `Save state cache`、每 10 分钟本地快照、
  `if: always()` 收尾抢救；自然结束、被顶掉、超时都能保住数据，最坏丢 ≤10 分钟。

需要真正 7×24 零空窗，就把同一份构建产物部署到常驻主机，见第 7 节 ——
用 Actions 硬撑常驻服务终究受 6 小时上限限制。

## 6. 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `Build artex` 报 `go: downloading go1.26.x` 失败 | `go.mod` 要求较新工具链 | 工作流已用 `go-version-file` + `check-latest`，自动下载；公司网络代理需放通 `proxy.golang.org` |
| `静态导出缺少 out/index.html` | 前端构建失败 | 看 `Build frontend` 日志；`npm ci` 需要 `web/package-lock.json`（仓库已有） |
| `artex 进程已退出` | 缺 PostgreSQL 或 DSN 错 | 检查 `services.postgres` 健康、`ARTEX_PG_DSN` 密码一致 |
| `Render frpc.toml` 报 `缺少 Environment secret ...` | 环境名不匹配，或 secret 建在了仓库级而不是 Environment 级 | 核对 job 的 `environment: production` 与 Settings → Environments → production 下的 `FRP_TOKEN` / `FRP_BIND_PORT` |
| `FRP_BIND_PORT 必须是纯数字端口` | secret 里混入了引号、空格或说明文字 | 只填 `7023` |
| frpc 日志 `login to server failed` | `FRP_TOKEN` 与 frps 的 `auth.token` 不一致 / 端口不通 | 核对 Environment secret 与 `/etc/frp/frps.env`；`telnet 203.195.195.52 7023` |
| 日志里 `serverPort = ***` | GitHub 自动脱敏短数字 secret | 正常现象，写进 `/tmp/frpc.toml` 的是真实值 |
| workflow 一直停在 "Waiting for approval" | Environment 加了 required reviewers / wait timer | 在 Environment 保护规则里去掉，否则定时轮次无法自动续期 |
| frpc 日志 `port already used` | `remotePort` 被占或被 `allowPorts` 拒 | 换 `FRP_REMOTE_PORT` 或扩大 `allowPorts` |
| 公网 502/超时但 job 正常 | 安全组未放行 `18087` | 云控制台放行 + 主机防火墙 |
| 前端白屏 | 未用 `embedui` 构建 | 确认 `Sync frontend` 与 `-tags embedui` 都执行了 |
| 每轮切换时短暂 404（数分钟） | 新轮构建 / frpc 握手期间的切换空窗 | 属预期；见第 5、7 节 |
| 公网长时间 502、主 workflow 没有活跃 run | 自我续期链条断了（`Queue successor run` 失败），且 GitHub scheduler 丢班没能兜住 | 先看上一轮 `Queue successor run` 日志；手动 `gh workflow run deploy-artex-frp.yml --repo Janit8r/hihaff` 重新起链，新轮会再次排下一棒 |
| `Queue successor run` 报 `无法排入下一轮` | `GITHUB_TOKEN` 缺 `actions: write`，或 dispatch API 临时故障 | 确认 `permissions: actions: write` 与 Settings → Actions → Workflow permissions；步骤有 3 次重试，仍失败就手动起链 |
| Actions 里同时看到 1 个 `in_progress` + 1 个 `queued` | 自我续期排好的接棒轮在排队 | **属预期**；`concurrency` 保证最多 1+1，当前轮结束后排队轮自动接上 |
| 想立刻恢复服务 / 冷启动起链 | 链条断了或刚改完代码 | Actions → Run workflow，或 `gh workflow run deploy-artex-frp.yml --repo Janit8r/hihaff` |
| 某轮显示 `cancelled` | 被新触发的一轮 concurrency 顶掉 | 属预期；检查 `Snapshot state (final)` / `Save state cache (final)` 是否仍以 `always()` 执行并保存成功 |
| 每轮都是新数据 / Summary 显示 `数据库恢复: false` | 恢复失败或首轮无快照 | 看 `Restore database from snapshot` 日志；确认 `public 表数量` 与上一轮一致 |
| 日志 `unrecognized configuration parameter "transaction_timeout"` | pg_dump 18 导出的 dump 头含 PG18 专有 GUC，PG16 服务端不认 | 工作流已自动过滤并重试；若仍失败，把 service 镜像升到 `postgres:18-alpine` 或看下一行 |
| 页面报 `模板目录不可用: /opt/artex/nuclei-templates` | artex-full 把 nuclei / 模板库路径写成 Go 常量，runner 上默认没有；或 `Prepare nuclei POC assets` 的下载失败 | 看该步骤日志里的 `模板文件数`；正常首次下载后应有数千个 yaml。仍为 0 就检查 codeload / release 是否被网络拦，或手动触发新一轮重试 |
| POC 模板列表为空但不报错 | 模板库下载被降级（目录存在、内容为空），或 `ARTEX_NUCLEI_ASSETS=0` | 确认 Variable 不是 `0`；删除缓存 `artex-nuclei-v1-3.11.1` 后重跑一轮强制重新下载 |
| `payload 目录不存在` / WebShell 页 Payload 库 500 | 旧版没设 `ARTEX_WEBSHELL_PAYLOAD_DIR`，`webshellPayloadRoot()` 指向不存在的目录 | 看 `Prepare MCP toolchain assets` 日志里的 `payload 库 ... 个条目`；正常应有 `php/ jsp/ aspx/` 三个子目录 |
| Payload 库返回 200 但条目为 0 | 种子未签出（`.ci-assets` 缺失）或文件后缀与目录名不匹配 | 必须是 `php/*.php`、`jsp/*.jsp`、`aspx/*.aspx`；检查 `deploy/payloads/webshell/` 是否已 push |
| 「系统 → MCP」里看不到 Nuclei/Subfinder/WebPath/Webshell | `Register MCP servers` 被跳过或失败；或 Variable `ARTEX_MCP_REGISTER=0` | 看该步骤日志的 `mcp_servers` 清单；注册依赖 `Wait for health` 之后才有的表，且 `psql` 由该步骤自动安装 |
| MCP 服务已启用但工具数是 0 | 工具列表是「随 agent 意图 spawn」后才缓存的 | 页面「系统 → MCP」点「刷新」，或让 agent 领一条意图 |
| `subfinder 下载失败` 告警 | release 下载被网络拦 | 只影响 `subfinder_enum`；爆破/解析/探活仍可用。删缓存 `artex-mcp-tools-v1-2.16.0` 后重跑重试 |
| agent 调 `hunter_enum` 报没有 key | provider-config 里的 `hunter:` 为空 | 该文件在 `STATE_DIR/mcp/subfinder-provider-config.yaml`，runner 上无法直接编辑；要固定 key 建议常驻部署（第 7 节） |
| `poc_update` 报不是 git 仓库 | 模板库用 codeload tarball，没有 `.git` | 属预期；只为省 ~50MB 缓存。要可 pull 就改成 `git clone --depth 1`，代价是缓存翻倍 |
| OOB 页面 `available:false` / 收不到回连 | runner 在 NAT 后、frp 只映射 `18087`，没有公网常驻监听器 | **补不齐**：需要公网 IP/域名 + 8889/18666/18667（甚至 53/UDP）；见第 7 节常驻部署 |
| 后台「Agent」提示词 / 任务模板仍是中文 | 数据库是缓存里恢复的老库，`SeedPromptIfEmpty` / `ON CONFLICT (nkey) DO NOTHING` 不会覆盖旧行；或 `ARTEX_ENGLISH_DEFAULTS=0` | 看 `Apply English defaults to persisted DB` 步骤日志；正常应有 `✓ 持久化数据库已应用英文默认` 与 `builtin_agent_prompts_english=7/7`。若迁移门已置位但仍是中文（例如手动改回），把 `ARTEX_ENGLISH_DEFAULTS_FORCE` 设 `1` 重跑一轮 |
| agent 仍用中文回复 | ① LLM 不遵循 `LANGUAGE_POLICY`；② 用了自定义 agent / 自定义模板；③ 上游新增了没翻译的中文片段 | 先看 `Apply English defaults to ARTEX source` Summary 里 `Chinese literals in agent/ ... 0` 与 `unmatched known hashes ... 0`；自定义模板正文需自己改英文，或确认已追加 `LANGUAGE POLICY (highest priority` |
| `::warning::...did not match upstream` / `Chinese literals in agent/ > 0` | 上游 `200210xsb/artex-full` 改了提示词文案，内容哈希对不上 | 不会静默放回中文：按 Summary 列出的片段补进 `deploy/i18n/en/strings_en.py` 后重新 push；补丁失败只告警，服务仍会用上游默认值上线 |
| `::error::数据库快照恢复失败，本轮中止` | 快照损坏 / 版本差异过大，工作流故意中止以免覆盖好数据 | 用 Run workflow 的 `state_cache_key` 指定更早的快照；必要时删掉那份坏缓存 |
| Summary 里 `public 表数量: 0` | 恢复被跳过（无快照）或恢复了空库 | 检查缓存是否命中、dump 是否非空；坏快照用 `state_cache_key` 回滚 |

## 7. 常驻部署（可选，去掉空窗）

同一个二进制，扔到你的常驻主机（例如 203.195.195.52 自己）跑：

```bash
# 本地产物：/tmp/artex-bin（或用 Actions 上传的 artifact）
install -m755 artex-bin /opt/artex/artex
install -d /opt/artex/data /opt/artex/skills /opt/artex/bin
cp -a skills/. /opt/artex/skills/

# artex-full 的 POC / 模板库页面硬编码 /opt/artex/bin/nuclei 与
# /opt/artex/nuclei-templates，常驻部署也要铺上（约 100 MB 模板库）：
cd /path/to/artex-full && ./mcp/install-mcp.sh --only nuclei --no-register
# 等价于：下载 nuclei v3.11.1 到 /opt/artex/bin/nuclei，
#        git clone --depth 1 projectdiscovery/nuclei-templates → /opt/artex/nuclei-templates

# 常驻主机上可以一次装齐 5 个 MCP 并注册（OOB 还要先按 oob/README.md 起监听器）：
cd /path/to/artex-full && ./mcp/install-mcp.sh
# payload 库放 /opt/artex/payloads/webshell/<php|jsp|aspx>/，无需 ARTEX_WEBSHELL_PAYLOAD_DIR

# 英文默认：常驻部署不走 Actions，需要自己先打源码补丁再编译，
# 并在服务启动后对已有的库执行一次生成的 SQL：
python3 deploy/i18n/en/apply_en.py --src /path/to/artex-full \
  --sql-out /tmp/en_defaults.sql --report-out /tmp/en_report.json
# 然后 go build -tags embedui ...；服务就绪后：
psql "$ARTEX_PG_DSN" -v ON_ERROR_STOP=1 -q -f /tmp/en_defaults.sql

export ARTEX_PG_DSN='postgres://artex:pw@127.0.0.1:5432/artex?sslmode=disable'
export ARTEX_SKILL_DIR=/opt/artex/skills
cd /opt/artex && nohup ./artex -addr :8787 -proxy 127.0.0.1:8788 -data /opt/artex/data &

# frpc
install -m755 frpc /usr/local/bin/frpc
frpc -c /etc/frp/frpc.toml
```

此时 Actions 只负责「构建 + 发布产物 + 重启远端服务」，可用 SSH 编排：
`webfactory/ssh-agent` + `rsync` + `systemctl restart artex`。
