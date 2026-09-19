# 率土自动化 · 后端控制台

采集脚本跑完把结果、报告和截图推到这里；你在这里看结果、改任务配置、排下一次要跑什么。

**技术栈**：Python 3.13 + FastAPI + SQLite（单文件，零运维）+ Jinja2 服务端渲染（无前端构建）。
**部署形态**：Docker Compose（应用 + Caddy 自动 HTTPS）。

---

## 一、Debian 服务器部署（推荐路径）

### 0. 前置条件

- 一台公网 Debian 12/13 服务器
- 一个域名，**A 记录指向这台服务器**（Caddy 签证书要用）
- 防火墙放行 **80 / 443**（80 是 Let's Encrypt 的 HTTP-01 校验口）

### 1. 装 Docker（Debian 官方源）

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

> 国内服务器如果拉不动镜像，给 Docker 配一下镜像加速再继续。

### 2. 把 server/ 传上去

```bash
# 在你本机执行
scp -r server root@你的服务器:/opt/stzb-server
```

（或者走 git：把项目推上去再 `git clone`，记得别把 `.env` 提交进去。）

### 3. 配置并启动

```bash
cd /opt/stzb-server
cp .env.example .env
nano .env          # 至少改 STZB_DOMAIN 和 ACME_EMAIL
docker compose up -d
```

### 4. 拿凭据

```bash
docker compose logs app | head -40
```

如果 `.env` 里没填 `STZB_ADMIN_PASSWORD` / `STZB_AGENT_TOKEN`，
这里会打印一次随机生成的，形如：

```
==============================================================
  首次启动 —— 下面的凭据只显示这一次，请立刻保存
==============================================================
  管理端账号 : admin
  管理端口令 : xxxxxxxxxxxx
  采集端令牌 : stzb_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
==============================================================
```

**这两串东西只打印这一次**（库里存的是 scrypt 哈希，明文找不回来）。

### 5. 验证

```bash
curl -s https://你的域名/healthz          # {"ok":true,...}
```

浏览器打开 `https://你的域名`，用上面的账号登录。

### 6. 把令牌填到采集脚本

在本机 `E:\Project\率土自动化\config.json` 里：

```json
"cloud": {
  "enabled": true,
  "base_url": "https://你的域名",
  "token": "stzb_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

然后本机执行一次补传，把历史报告推上去，顺便验证链路：

```bat
cd /d E:\Project\率土自动化
"C:\Users\tangl\.workbuddy\binaries\python\envs\default\Scripts\python.exe" run_daily.py --upload-last
```

看到 `✓ 后台控制台：https://你的域名/runs/1` 就成了。

---

## 二、Windows 部署（裸机，适合自己家里/办公室的机器）

后端是纯 Python + SQLite，Windows 上也能直接跑。三个双击脚本搞定：

| 脚本 | 干什么 |
|---|---|
| `setup_server.bat` | 建本地 venv + 装依赖（第一次跑一次就行） |
| `start_server.bat` | 启动服务（读 `.env`，监听 `127.0.0.1:8000`） |

```bat
cd server
copy .env.example .env     rem 改成你的初始口令 / 令牌（可选）
setup_server.bat           rem 1) 装环境
start_server.bat           rem 2) 启动
```

首次启动会把管理员口令和 agent 令牌打印到窗口里（只一次），
`start_server.bat` 会停留在前台，按 Ctrl+C 停止。

### Windows 上要 HTTPS（有域名）

服务本身不碰证书，用反向代理接一层：

- **Caddy（推荐）**：去 caddyserver.com 下 Windows 版，放个 `Caddyfile`（抄 `server/Caddyfile`，
  把 `reverse_proxy app:8000` 改成 `reverse_proxy 127.0.0.1:8000`，`{$STZB_DOMAIN}` 换成域名），
  `caddy run` 即可自动签证书。
- **Nginx / IIS**：自己反代 `http://127.0.0.1:8000` 并配证书。

> 只想本机访问、不需要公网，就**不用**反代 —— 直接浏览器开 `http://127.0.0.1:8000`。

### Windows 下的数据放哪

默认 `server\data\`（数据库 + 报告 + 截图），备份就备份这个目录。
想改位置：在 `.env` 里写 `STZB_DATA_DIR=D:\stzb-data`。

---

## 三、不用 Docker 的 Linux 部署方式

> 能用 Docker 就用 Docker：SQLite + 证书持久化 + 自动重启，手工做一遍很容易漏。

```bash
# 1) 系统依赖 + 独立用户
sudo apt install -y python3-venv python3-pip caddy
sudo useradd -r -s /usr/sbin/nologin -d /opt/stzb stzb
sudo mkdir -p /opt/stzb /var/lib/stzb && sudo chown -R stzb:stzb /opt/stzb /var/lib/stzb

# 2) 代码 + 虚拟环境
sudo -u stzb cp -r server/. /opt/stzb/
sudo -u stzb python3 -m venv /opt/stzb/.venv
sudo -u stzb /opt/stzb/.venv/bin/pip install -r /opt/stzb/requirements.txt

# 3) 环境变量
sudo tee /etc/stzb.env >/dev/null <<'EOF'
STZB_DATA_DIR=/var/lib/stzb
STZB_SECRET_KEY=换成一串40位以上的随机字符
STZB_ADMIN_USER=admin
STZB_ADMIN_PASSWORD=先在这里写一个强口令，启动后可以删掉
STZB_AGENT_TOKEN=先在这里写一个令牌，启动后可以删掉
TZ=Asia/Shanghai
EOF
sudo chmod 600 /etc/stzb.env
```

`/etc/systemd/system/stzb.service`：

```ini
[Unit]
Description=STZB console
After=network-online.target
Wants=network-online.target

[Service]
User=stzb
WorkingDirectory=/opt/stzb
EnvironmentFile=/etc/stzb.env
ExecStart=/opt/stzb/.venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips '*'
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Caddy：`/etc/caddy/Caddyfile` 直接抄 `server/Caddyfile`，
把 `{$STZB_DOMAIN}` 换成你的域名（或设环境变量），
再把 `reverse_proxy app:8000` 改成 `reverse_proxy 127.0.0.1:8000`。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now stzb caddy
sudo journalctl -u stzb -n 40       # 看首次启动打印的凭据
```

> 拿到凭据后，把 `/etc/stzb.env` 里的 `STZB_ADMIN_PASSWORD` / `STZB_AGENT_TOKEN`
> 两行删掉再重启 —— 它们只在库里没记录时才生效，删掉不影响已初始化的账号。

---

## 四、日常运维

```bash
docker compose ps                     # 状态
docker compose logs -f app            # 实时日志
docker compose restart app            # 重启
docker compose pull && docker compose up -d --build    # 升级

# 忘了管理端口令 —— 打印一个新的
docker compose exec app python -m app.cli reset-password

# 直接设成指定口令
docker compose exec app python -m app.cli set-password '新的口令'

# 轮换采集端令牌（轮换后必须同步改本机 config.json，否则上不了报）
docker compose exec app python -m app.cli new-token

# 看统计
docker compose exec app python -m app.cli stats

# 手动清理历史（默认按 STZB_KEEP_RUNS 自动清理）
docker compose exec app python -m app.cli prune --keep 50
```

### 备份

只有一个目录要备份：**`server/data/`**（数据库 + 报告 + 截图）。

```bash
# 冷备（停服务最稳）
docker compose stop app
tar czf stzb-backup-$(date +%F).tar.gz data/
docker compose start app
```

想热备也行，先 `sqlite3 data/stzb.db ".backup /tmp/stzb.db"` 再打包，
但报告文件可能正在写，一致性不如冷备。

### 磁盘占用

| 项 | 大小 |
|---|---|
| 每轮报告 HTML | 约 3 MB |
| 每轮截图（每任务最多 4 张、缩到 1000px JPEG） | 约 2 MB |
| 每轮合计 | **约 5 MB** |

默认保留 120 轮 ≈ **600 MB**。每天跑 2 次的话是 2 个月的量。
想改就调 `.env` 里的 `STZB_KEEP_RUNS`。

### 管多台机器 / 多个账号（客户端页 + 账号角色页）

**第一步：让机器出现在「客户端」页。**
采集端每次心跳都会自动登记（不需要预先添加）。想让机器全天在线，在采集机上加跑常驻进程：

```bat
venv\Scripts\python.exe agent.py
```

**第二步：登记账号与角色**（`账号角色` 页）。

- 账号只需要填**脱敏串**（如 `159****4508`，登录页上显示成什么样就填什么样）——
  它只用来比对「切没切成功」，**不存密码**。
- 每个账号下面可以加多个角色（角色名 / 区服 / 赛季都按游戏里显示的填）。
- ⚠️ 要自动切换的账号，必须**先在这台采集机的模拟器里手动登录过一次**，
  否则网易登录页的「常用」里没有它，切不过去（系统会如实报错，不会瞎点）。

**第三步：给客户端指派**（`客户端` 页 → 选中某台 → 指派表单）。
选账号 → 角色下拉会自动过滤成该账号的角色 → 保存。客户端**下次心跳**（约 30 秒内）
就会拿到指派并自动切换。

**「探测」按钮**：点一下，服务端挂一条一次性指令；客户端下次心跳执行（读当前屏幕文字 +
截图 + 设备状态）并回传。用来远程确认「这台机器现在到底在哪个界面」。
不想让后台读屏的话，采集端用 `agent.py --no-probe` 起。

**「定向任务」**：`待执行任务` 页新建时选一个客户端，这条任务就只有它能领。
不选就是公共任务，谁先来谁领。

**在线判定**：默认 30 秒心跳 / 90 秒判掉线。想改就调 `.env`：

```bash
STZB_HEARTBEAT_INTERVAL=30     # 服务端告诉客户端多久心跳一次
STZB_CLIENT_OFFLINE_AFTER=90   # 超过多少秒没心跳算掉线
```

---

## 五、接口一览

### 采集端（需要 `X-Agent-Token` 头）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/agent/ping` | 轻量探活（只回 `pong` + 版本，不写状态） |
| POST | `/api/agent/heartbeat` | **心跳**：上报 uid/主机/版本/设备状态，回指派(`assignment`)+`switch_needed`+待执行指令+心跳间隔 |
| POST | `/api/agent/probe` | 回报「探测指令」的执行结果（读屏文字 / 截图 / 设备状态） |
| GET | `/api/agent/config?version=N` | 拉可下发的业务配置（`changed` 告诉你要不要应用） |
| GET | `/api/agent/jobs` | 拉待执行任务（只含「公共任务」+「定向给本机的」） |
| POST | `/api/agent/jobs/{id}/take` | 领取（原子操作，重复领返回 409；定向任务非本人返回 403） |
| POST | `/api/agent/jobs/{id}/ack` | 回执 `{status, run_id}` |
| POST | `/api/agent/run` | 上传运行结果（自动归属到本次心跳的客户端/账号/角色） |
| POST | `/api/agent/run/{id}/report` | 上传报告 HTML（multipart，字段名 `file`） |
| POST | `/api/agent/run/{id}/shot` | 上传一张截图（`file` + `task_key` + `index`） |
| POST | `/api/agent/run/{id}/done` | 收尾，顺带触发旧记录清理 |

> **为什么探活要走 `POST /heartbeat` 而不是服务端主动连客户端？**
> 后端在公网、客户端在内网，服务端**连不进去**。所以在线状态一律由客户端主动上报
> （每 30 秒一次），服务端只记 `last_seen`；超过 90 秒没来即判掉线。
> 「探测客户端当前界面」同理 —— 服务端把指令**挂在客户端记录上**，客户端下次心跳取走执行。

> **心跳的身份**：`X-Agent-Uid`（稳定客户端 id，落盘在客户端 `state/client.json`）。
> 没带 uid 的调用视为「未注册」，`GET /jobs` 只会返回**公共任务**，
> 拿不到也领不走任何定向任务（这条是联调测试抓出来的漏洞，务必别退回去）。

### 管理端（浏览器会话）

| 路径 | 作用 |
|---|---|
| `/` | 总览：最近 14 天统计 + 最近运行 + 客户端在线卡片 + 待执行 |
| `/clients` | **客户端管理**：在线/掉线、设备状态、指派账号角色、一键探测、改名/停用/删除 |
| `/accounts` | **账号与角色管理**：登记账号、为账号加角色、看指派总览 |
| `/runs` | 运行历史（可按客户端 / 账号筛选，可分页） |
| `/runs/{id}` | 运行详情：任务明细 + 环境 + 客户端/账号 + 日志 + 截图 + 内嵌完整报告 |
| `/runs/{id}/report` | 原报告 HTML（严格 CSP 沙箱，脚本一律不执行） |
| `/artifacts/{id}` | 单张截图原图 |
| `/config` | 任务配置编辑 + 版本历史 + 回滚 |
| `/jobs` | 待执行任务：新建 / 取消，可选「定向给某客户端」 |
| `/settings` | 令牌轮换、改口令、服务端信息 |
| `/events` | 事件日志（上传、配置变更、登录、轮换） |
| `/api/summary` | 给外部用的 JSON 摘要 |
| `/api/clients` | 客户端在线状态 JSON |
| `/healthz` | 公开健康检查（不需要登录） |

---

## 六、安全设计（为什么这么写）

| 关注点 | 做法 |
|---|---|
| **配置下发边界** | 服务端**只能**下发业务段（`tasks` / 各任务参数 / `safety`）。`device`（adb 路径与端口）、`emulator`（MuMuManager 路径、虚拟机索引）、`cloud`（后端地址与令牌）、`logging` **永远以本机为准**。脚本侧还有一份同样的白名单做二次过滤 —— 两边各拦一遍，任何一边写错都不会把本机配置搞坏。 |
| **口令/令牌存储** | 只存 **scrypt 哈希**（n=2^15, r=8, p=1），常数量时间比较；界面只显示令牌头尾。数据库泄露也拿不到明文。 |
| **上传报告 XSS** | 报告是「上传来的文件」。服务端用严格 CSP（`default-src 'none'`、禁 script、只允许 `data:` 图片和内联样式）提供，并放在 `<iframe sandbox>` 里渲染。就算有人往报告里塞 `<script>` 也执行不了。 |
| **登录暴力破解** | 按 IP 限流（默认 15 分钟内 8 次失败即锁），失败和成功都记事件；用户名错与口令错耗时一致，不泄露哪个字段错。 |
| **路径穿越** | 上传文件名只保留 `[A-Za-z0-9._-]`，并强制落在 `artifacts/<run_id>/` 下。 |
| **磁盘被塞满** | 单文件上限 24MB、每轮截图上限 60 张，超出直接 413/429；历史按 `STZB_KEEP_RUNS` 自动清理。 |
| **定向任务越权** | 定向任务（`run_requests.client_id` 非空）只有指定客户端能看见和领取；**未注册/无 uid** 的调用一律只拿公共任务。领取时还会**再查一次归属**，防止抢别人已排的任务。这条是端到端联调测出来的真实漏洞，已修并有回归测试。 |
| **账号密码** | 服务端**不存任何游戏账号密码**。账号只登记「用于比对的脱敏串」（如 `159****4508`）。切换靠客户端在网易登录页点「常用」列表完成 —— 前提是该账号在这台机器登录过一次。 |
| **传输安全** | Caddy 自动 HTTPS + HSTS；脚本侧做**强制证书校验**（优先 certifi 的根证书包），失败就报错，**绝不静默降级成不校验**。 |

> 一句话：这台服务器上放的是你游戏账号的自动化凭据，所以宁可多设几道闸。

---

## 七、目录结构

```
server/
├─ app/
│   ├─ main.py           FastAPI 装配 + 安全响应头 + 异常处理
│   ├─ settings.py       环境变量（数据目录、上限、保留轮数、心跳间隔/掉线阈值）
│   ├─ db.py             SQLite 表结构与查询（客户端 / 账号 / 角色 / 运行 / 待执行）
│   ├─ security.py       scrypt 哈希 / 会话密钥 / 首启引导 / 登录限流
│   ├─ managed.py        可下发配置白名单（安全边界在这里）
│   ├─ schemas.py        请求体模型（含心跳 / 探测回报）
│   ├─ routes_agent.py   采集端 API（整组挂令牌校验；心跳 / 指派 / 探测 / 定向任务）
│   ├─ routes_ui.py      管理端页面与表单动作（含客户端页 / 账号角色页）
│   ├─ cli.py            运维命令（重置口令 / 换令牌 / 统计 / 清理）
│   ├─ templates/        Jinja2 页面
│   │   ├─ clients.html  客户端管理（在线卡片 / 指派 / 探测）
│   │   ├─ accounts.html 账号与角色管理
│   │   └─ ...
│   └─ static/style.css
├─ requirements.txt      已锁版本
├─ Dockerfile
├─ docker-compose.yml    应用 + Caddy
├─ Caddyfile             自动 HTTPS 反代
└─ .env.example
```

数据（不在代码目录里）：`./data/stzb.db`、`./data/artifacts/<run_id>/...`
