# hihaff → Actions 部署 & 运行 ARTEX + frp 反代公网

目标：在 **Janit8r/hihaff** 仓库上用 GitHub Actions 从源码构建并运行
[RuoJi6/ARTEX](https://github.com/RuoJi6/ARTEX)（Go 后端 + Next 静态前端），
再用 **frpc** 把 Web 端口反代到公网 frps **203.195.195.52:7023**。

## 0. 架构

```
GitHub Actions runner (ubuntu-latest, 临时)
├── postgres:16-alpine   (job service, 127.0.0.1:5432)
├── artex  :8787         (go build -tags embedui，内嵌前端)
│         :8788          (MITM 流量记录代理，仅本地)
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
# 或 push 后自动触发；此后每 6 小时 cron 自动续期

# 公网验证（frps 所在网络或任意外网）
curl -sS -o /dev/null -w '%{http_code}\n' http://203.195.195.52:18087/
curl -sS http://203.195.195.52:18087/api/health
```

Job 首页 Summary 会打印公网地址、ARTEX commit、frps 地址与凭据来源（Environment secrets），
以及「跨轮持久化」块的恢复来源 / 数据库恢复状态 / public 表数量；
`Wait for health`、`Start frpc`、`Keep alive` 三步的日志里能看到探活与 frp 握手结果。

## 5. 为什么需要 cron + keep-alive

GitHub 托管 runner **是临时的**，单个 job 最长 6 小时，结束即销毁，进程与隧道随之消失。
现在的设计是「**滚动续期**」，而不是「跑完就等下一班」：

- `timeout-minutes: 350` —— 值守 30 + 300 分钟（≈5h30m），刻意**远大于** cron 的 2 小时间隔；
- `Keep alive` —— 每 30s 检查 artex / nginx / frpc 存活掉线自动拉起，并**每 10 分钟落一次本地快照**；
- `schedule: 17 */2 * * *` —— 每 2 小时第 17 分（UTC）拉新一轮；
- `concurrency: artex-frp` + `cancel-in-progress: true` —— 新轮就绪后顶掉旧轮。

为什么必须这样：旧版 job 只活 103 分钟、间隔 120 分钟，**即使完全准时也有 17 分钟真空**；
再加上整点 cron 被 GitHub 高峰期延迟/丢弃（实测 18:00Z 班延迟 87 分钟、08:00Z 班延迟 71 分钟、
10:00Z 班整班被 drop），空窗会放大到 1~2 小时。现在旧轮能撑 5 个多小时，
**连漏两三班也不断线**；只有新轮构建到 frpc 连上的那 3~5 分钟才有切换空窗。

关于错峰：GitHub 官方文档明确写着

> "The `schedule` event can be delayed during periods of high loads... High load times include
> the start of every hour. If the load is sufficiently high enough, some queued jobs may be dropped.
> To decrease the chance of delay, schedule your workflow to run at a different time of the hour."

所以 `cron` **绝对不要写 `0 ...` 整点**，一律挪到非整点分钟。cron 用的是 UTC，
`17 */2` 对应北京时间偶数点的 17 分。

由于每轮通常是被下一轮 **cancel**（而不是正常结束），数据保存不依赖 `actions/cache` 的
隐式后置步骤，而是三重保险：30 分钟处显式 `Save state cache`、每 10 分钟本地快照、
以及 `if: always()` 的收尾快照/保存。最坏情况丢 ≤10 分钟数据。
需要真正 7×24 零空窗，就把同一份构建产物部署到常驻主机，见第 7 节。

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
| 每 2 小时短暂 404（数分钟） | 新轮构建 / frpc 握手期间的切换空窗 | 属预期；见第 5、7 节 |
| 到了整点却没有新 run / 公网长时间 502 | `schedule` 排在整点被 GitHub 高峰期延迟或整班丢弃；或上一轮已结束、下一班还没来 | 改用非整点 cron（现在是 `17 */2`）；`gh run list --workflow=deploy-artex-frp.yml` 看最近班次的 `created_at` 是否偏离整点；应急可手动 Run workflow |
| 想立刻恢复服务 | 定时班还没到 | Actions → Run workflow，或 `gh workflow run deploy-artex-frp.yml --repo Janit8r/hihaff` |
| 某轮显示 `cancelled` | 被下一轮 concurrency 正常顶掉，滚动续期的一部分 | 属预期；检查 `Snapshot state (final)` / `Save state cache (final)` 是否仍以 `always()` 执行并保存成功 |
| 每轮都是新数据 / Summary 显示 `数据库恢复: false` | 恢复失败或首轮无快照 | 看 `Restore database from snapshot` 日志；确认 `public 表数量` 与上一轮一致 |
| 日志 `unrecognized configuration parameter "transaction_timeout"` | pg_dump 18 导出的 dump 头含 PG18 专有 GUC，PG16 服务端不认 | 工作流已自动过滤并重试；若仍失败，把 service 镜像升到 `postgres:18-alpine` 或看下一行 |
| `::error::数据库快照恢复失败，本轮中止` | 快照损坏 / 版本差异过大，工作流故意中止以免覆盖好数据 | 用 Run workflow 的 `state_cache_key` 指定更早的快照；必要时删掉那份坏缓存 |
| Summary 里 `public 表数量: 0` | 恢复被跳过（无快照）或恢复了空库 | 检查缓存是否命中、dump 是否非空；坏快照用 `state_cache_key` 回滚 |

## 7. 常驻部署（可选，去掉空窗）

同一个二进制，扔到你的常驻主机（例如 203.195.195.52 自己）跑：

```bash
# 本地产物：/tmp/artex-bin（或用 Actions 上传的 artifact）
install -m755 artex-bin /opt/artex/artex
install -d /opt/artex/data /opt/artex/skills
cp -a skills/. /opt/artex/skills/

export ARTEX_PG_DSN='postgres://artex:pw@127.0.0.1:5432/artex?sslmode=disable'
export ARTEX_SKILL_DIR=/opt/artex/skills
cd /opt/artex && nohup ./artex -addr :8787 -proxy 127.0.0.1:8788 -data /opt/artex/data &

# frpc
install -m755 frpc /usr/local/bin/frpc
frpc -c /etc/frp/frpc.toml
```

此时 Actions 只负责「构建 + 发布产物 + 重启远端服务」，可用 SSH 编排：
`webfactory/ssh-agent` + `rsync` + `systemctl restart artex`。
