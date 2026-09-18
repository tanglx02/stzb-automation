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

---

## 五、接口一览

### 采集端（需要 `X-Agent-Token` 头）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/agent/ping` | 探活 + 拿远端配置版本号 |
| GET | `/api/agent/config?version=N` | 拉可下发的业务配置（`changed` 告诉你要不要应用） |
| GET | `/api/agent/jobs` | 拉待执行任务 |
| POST | `/api/agent/jobs/{id}/take` | 领取（原子操作，重复领返回 409） |
| POST | `/api/agent/jobs/{id}/ack` | 回执 `{status, run_id}` |
| POST | `/api/agent/run` | 上传运行结果，返回 `run_id` |
| POST | `/api/agent/run/{id}/report` | 上传报告 HTML（multipart，字段名 `file`） |
| POST | `/api/agent/run/{id}/shot` | 上传一张截图（`file` + `task_key` + `index`） |
| POST | `/api/agent/run/{id}/done` | 收尾，顺带触发旧记录清理 |

### 管理端（浏览器会话）

| 路径 | 作用 |
|---|---|
| `/` | 总览：最近 14 天统计 + 最近运行 + 待执行 |
| `/runs` | 运行历史（可分页、可只看有未完成项的） |
| `/runs/{id}` | 运行详情：任务明细 + 环境 + 日志 + 截图 + 内嵌完整报告 |
| `/runs/{id}/report` | 原报告 HTML（严格 CSP 沙箱，脚本一律不执行） |
| `/artifacts/{id}` | 单张截图原图 |
| `/config` | 任务配置编辑 + 版本历史 + 回滚 |
| `/jobs` | 待执行任务：新建 / 取消 |
| `/settings` | 令牌轮换、改口令、服务端信息 |
| `/events` | 事件日志（上传、配置变更、登录、轮换） |
| `/api/summary` | 给外部用的 JSON 摘要 |
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
| **传输安全** | Caddy 自动 HTTPS + HSTS；脚本侧做**强制证书校验**（优先 certifi 的根证书包），失败就报错，**绝不静默降级成不校验**。 |

> 一句话：这台服务器上放的是你游戏账号的自动化凭据，所以宁可多设几道闸。

---

## 七、目录结构

```
server/
├─ app/
│   ├─ main.py           FastAPI 装配 + 安全响应头 + 异常处理
│   ├─ settings.py       环境变量（数据目录、上限、保留轮数）
│   ├─ db.py             SQLite 表结构与查询
│   ├─ security.py       scrypt 哈希 / 会话密钥 / 首启引导 / 登录限流
│   ├─ managed.py        可下发配置白名单（安全边界在这里）
│   ├─ schemas.py        请求体模型
│   ├─ routes_agent.py   采集端 API（整组挂令牌校验）
│   ├─ routes_ui.py      管理端页面与表单动作
│   ├─ cli.py            运维命令（重置口令 / 换令牌 / 统计 / 清理）
│   ├─ templates/        Jinja2 页面
│   └─ static/style.css
├─ requirements.txt      已锁版本
├─ Dockerfile
├─ docker-compose.yml    应用 + Caddy
├─ Caddyfile             自动 HTTPS 反代
└─ .env.example
```

数据（不在代码目录里）：`./data/stzb.db`、`./data/artifacts/<run_id>/...`
