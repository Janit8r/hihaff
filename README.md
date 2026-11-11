# hihaff → Actions 部署 & 运行 ARTEX + frp 反代公网

目标：在 **Janit8r/hihaff** 仓库上用 GitHub Actions 从源码构建并运行
[RuoJi6/ARTEX](https://github.com/RuoJi6/ARTEX)（Go 后端 + Next 静态前端），
再用 **frpc** 把 Web 端口反代到公网 frps **203**。

## 0. 架构

```
GitHub Actions runner (ubuntu-latest, 临时)
├── postgres:16-alpine   (job service, 127.0.0.1:5432)
├── artex  :8787         (go build -tags embedui，内嵌前端)
│         :8788          (MITM 流量记录代理，仅本地)
└── frpc ── TCP ──> 203.195.195.52:7023 (frps)
                     └── remotePort 18087 ──> 公网
公网访问： http://203.195.195.52:18087
```

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
2. 写配置 `/etc/frp/frps.toml`（见 `deploy/frps.toml`），要点：
   - `bindPort = 7023`
   - `auth.token = "<强口令>"`（与 GitHub Secret `FRP_TOKEN` 一致）
   - `allowPorts` 必须包含 `18087`（或你自定义的 `FRP_REMOTE_PORT`）
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
> 并让 frps 配 `vhostHTTPPort = 7023`（此时 `bindPort` 用别的端口，例如 7000）。

## 2. GitHub Secrets / Variables

仓库 → Settings → Secrets and variables → Actions

**Secrets（必填）**

| 名称 | 说明 |
|---|---|
| `FRP_TOKEN` | frps 的 `auth.token`；frps 未设 token 则留空不建 |

**Secrets（按需）**

| 名称 | 说明 |
|---|---|
| `ANTHROPIC_API_KEY` | LLM 调用 |
| `OPENAI_API_KEY` | LLM 调用 |

**Variables（可选）**

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

Job 首页 Summary 会打印公网地址、ARTEX commit、frps 地址；
`Wait for health`、`Start frpc`、`Keep alive` 三步的日志里能看到探活与 frp 握手结果。

## 5. 为什么需要 cron + keep-alive

GitHub 托管 runner **是临时的**，单个 job 最长 6 小时，结束即销毁，进程与隧道随之消失。
所以工作流这样设计：

- `timeout-minutes: 355` —— 跑到 5h55m 由平台回收；
- `Keep alive` —— 每 30s 检查 artex / frpc 存活，掉线自动拉起，窗口 ~5h40m 后主动退出；
- `schedule: 0 */6 * * *` —— 每 6 小时拉新一轮；
- `concurrency: artex-frp` + `cancel-in-progress: true` —— 新一轮顶掉旧一轮，避免占用重复。

效果：**近乎连续在线**，每 6 小时有数十秒的切换空窗。
需要 7×24 零空窗，就把同一份构建产物部署到常驻主机，见第 7 节。

## 6. 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `Build artex` 报 `go: downloading go1.26.x` 失败 | `go.mod` 要求较新工具链 | 工作流已用 `go-version-file` + `check-latest`，自动下载；公司网络代理需放通 `proxy.golang.org` |
| `静态导出缺少 out/index.html` | 前端构建失败 | 看 `Build frontend` 日志；`npm ci` 需要 `web/package-lock.json`（仓库已有） |
| `artex 进程已退出` | 缺 PostgreSQL 或 DSN 错 | 检查 `services.postgres` 健康、`ARTEX_PG_DSN` 密码一致 |
| frpc 日志 `login to server failed` | token 不匹配 / 端口不通 | 核对 `FRP_TOKEN`；`telnet 203.195.195.52 7023` |
| frpc 日志 `port already used` | `remotePort` 被占或被 `allowPorts` 拒 | 换 `FRP_REMOTE_PORT` 或扩大 `allowPorts` |
| 公网 502/超时但 job 正常 | 安全组未放行 `18087` | 云控制台放行 + 主机防火墙 |
| 前端白屏 | 未用 `embedui` 构建 | 确认 `Sync frontend` 与 `-tags embedui` 都执行了 |
| 每 6 小时短暂 404 | runner 切换空窗 | 属预期；见第 5、7 节 |

## 7. 常驻部署（可选，去掉空窗）

同一个二进制，扔到你的常驻主机（例如 202 自己）跑：

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
