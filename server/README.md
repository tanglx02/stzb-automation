# 率土自动化 · 后端控制台

采集脚本跑完把结果、报告和截图推到这里；你在这里看结果、改任务配置、排下一次要跑什么。

**技术栈**：Python 3.13 + FastAPI + SQLite（单文件，零运维）+ Jinja2 服务端渲染（无前端构建）。

---

## 先选部署形态

**默认走「内网」**：后端就跑在你自己这台机器（或内网另一台机器）上，
手机 / 别的电脑在同一个局域网里直接开 `http://<内网IP>:8000` 就能用 ——
**不需要域名、不需要 HTTPS 证书、不需要反向代理**。

| 形态 | 适合 | 怎么做 |
|---|---|---|
| **Windows 内网**（推荐，最省事） | 自己家里/办公室，一台机器就够 | 跳 §二，两个 `.bat` 搞定 |
| **Linux 裸机**（内网或公网都行） | 有台常开的 Linux 机器 | 跳 §三（systemd + 裸 uvicorn）；内网就把 bind 改成 `0.0.0.0`、跳过 Caddy |
| **公网 Docker**（可选进阶） | 想在外面（4G/外地）也能看 | 跳 §一，Docker + Caddy 自动 HTTPS |

> ⚠️ **别一上来就上公网**。内网够用就先内网 —— 公网要处理域名、证书、防火墙、
> 暴露面，都是额外的坑。等确实需要「在外地也能看」再迁，采集端接口一个字都不用改。

---

## 一、公网部署（Docker Compose + Caddy，可选进阶）

只有确实要把控制台暴露到公网时才用这条。前置条件比内网多：**需要一台公网 Debian 服务器 +
一个域名 + A 记录 + 放行 80/443**。

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
curl -s http://127.0.0.1:8000/api/agent/ping -H "X-Agent-Token: <你的令牌>"   # {"ok":true,...}
```

浏览器打开 `http://127.0.0.1:8000`（同局域网的其他设备用 `http://<本机内网IP>:8000`），
用上面的账号登录。

> 内网部署**不需要**域名和 HTTPS。文档里凡出现 `https://你的域名` 的地方，
> 内网场景一律替换成 `http://<内网IP>:8000`。

### 6. 把令牌填到采集脚本

在本机 `E:\Project\率土自动化\config.json` 里：

```json
"cloud": {
  "enabled": true,
  "base_url": "http://127.0.0.1:8000",
  "token": "stzb_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

> 后端和采集端跑在**同一台机器**上时用 `127.0.0.1`；
> 分处两台机器时改成后端那台的 `http://<内网IP>:8000`。

然后本机执行一次补传，把历史报告推上去，顺便验证链路：

```bat
cd /d E:\Project\率土自动化
"venv\Scripts\python.exe" run_daily.py --upload-last
```

看到 `✓ 后台控制台：http://127.0.0.1:8000/runs/1` 就成了。

---

## 二、Windows 部署（裸机，**内网默认走这条**）

后端是纯 Python + SQLite，Windows 上直接跑最省事。两个双击脚本搞定：

| 脚本 | 干什么 |
|---|---|
| `setup_server.bat` | 建本地 venv + 装依赖（第一次跑一次就行） |
| `start_server.bat` | 启动服务（读 `.env`，绑 **`0.0.0.0:8000`**） |

```bat
cd server
copy .env.example .env     rem 改成你的初始口令 / 令牌（可选）
setup_server.bat           rem 1) 装环境
start_server.bat           rem 2) 启动
```

启动时它会把你**本机的内网地址**也算出来打印，直接照着开：

```text
  This machine : http://127.0.0.1:8000
  Same LAN     : http://192.168.1.23:8000     <- 手机 / 别的电脑用这个
```

首次启动会把管理员口令和 agent 令牌打印到窗口里（**只一次**）。
`start_server.bat` 会停留在前台，按 Ctrl+C 停止。

> **为什么绑 `0.0.0.0` 而不是 `127.0.0.1`**：内网部署就是要手机/别的电脑能连进来。
> 老文档里写的 `127.0.0.1` 是早期版本，现在默认对内网开放。
> 只想本机访问 → 浏览器开 `http://127.0.0.1:8000`；不想让别人连 → 在 `.env` 里自行限制，
> 或干脆只绑 `127.0.0.1`（改 `start_server.bat` 最后一行的 `--host`）。

### 想让手机/别的电脑连上，但连不上？

1. **放行防火墙**：Windows 默认会弹「是否允许 Python 通过防火墙」，选**专用网络**允许。
   没弹或误点了拒绝 → 手动加一条入站规则放行 TCP `8000`。
2. **确认同一网段**：手机要连的是**同一个路由器/同一个 Wi-Fi**，用打印出来的那个内网 IP。
3. **先在本机验证**：`http://127.0.0.1:8000/healthz` 返回 `{"ok":true,...}` 说明服务本身没问题，
   那问题一定在防火墙或网段。

### Windows 上要 HTTPS（只在有公网域名时才需要）

内网用**不需要**这一节 —— 局域网里跑 HTTP 就够了。

服务本身不碰证书，用反向代理接一层：

- **Caddy（推荐）**：去 caddyserver.com 下 Windows 版，放个 `Caddyfile`（抄 `server/Caddyfile`，
  把 `reverse_proxy app:8000` 改成 `reverse_proxy 127.0.0.1:8000`，`{$STZB_DOMAIN}` 换成域名），
  `caddy run` 即可自动签证书。
- **Nginx / IIS**：自己反代 `http://127.0.0.1:8000` 并配证书。

### Windows 下的数据放哪

默认 `server\data\`（数据库 + 报告 + 截图），备份就备份这个目录。
想改位置：在 `.env` 里写 `STZB_DATA_DIR=D:\stzb-data`。

---

## 三、不用 Docker 的 Linux 部署方式

> 能用 Docker 就用 Docker：SQLite + 证书持久化 + 自动重启，手工做一遍很容易漏。
> 下面这套**内网和公网都能用** —— 内网就把 bind 改成 `0.0.0.0`、跳过 Caddy 那步直接开
> `http://<内网IP>:8000`；公网才需要 Caddy + 域名。

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

> **内网部署**：把 `--host 127.0.0.1` 改成 `--host 0.0.0.0`，
> 然后跳过下面 Caddy 那一步 —— 直接开 `http://<内网IP>:8000` 即可。
> （绑定 `127.0.0.1` 只接受本机连接，局域网里别的设备连不上。）
> 同时记得放行防火墙：`sudo ufw allow 8000/tcp`（或 firewalld 对应命令）。

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

# 忘了管理端口令 —— 打印一个新的（用户不存在会自动建管理员，用于救急）
docker compose exec app python -m app.cli reset-password

# 直接设成指定口令
docker compose exec app python -m app.cli set-password '新的口令'

# ---- 多用户 ----
docker compose exec app python -m app.cli users                  # 列出所有用户
docker compose exec app python -m app.cli add-user zhangsan      # 建普通用户（口令随机）
docker compose exec app python -m app.cli add-user ops --admin    # 建管理员
docker compose exec app python -m app.cli disable-user zhangsan   # 停用（会话立刻失效）
docker compose exec app python -m app.cli enable-user zhangsan
docker compose exec app python -m app.cli promote-user zhangsan   # 提为管理员
docker compose exec app python -m app.cli demote-user ops         # 降为普通用户
docker compose exec app python -m app.cli reset-password -u zhangsan

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
| GET | `/api/agent/ping` | 轻量探活（只带头、无负载；老客户端兼容，仍会登记并刷 `last_seen`） |
| POST | `/api/agent/ping` | **心跳**：上报 uid/主机/版本/设备状态，回指派(`assignment`)+`switch_needed`+待执行指令+心跳间隔 |
| WS | `/api/agent/ws` | **实时长连接**（首选）。需带令牌；连上后后台派任务/下指令/改指派**毫秒级**推下来 |
| POST | `/api/agent/probe` | 回报「探测指令」的执行结果（读屏文字 / 截图 / 设备状态）；回报 `BUSY:` 前缀时指令**放回队列** |
| GET | `/api/agent/config?version=N` | 拉可下发的业务配置（`changed` 告诉你要不要应用） |
| GET | `/api/agent/jobs` | 拉待执行任务（只含「公共任务」+「定向给本机的」） |
| POST | `/api/agent/jobs/{id}/take` | 领取（原子操作，重复领返回 409；定向任务非本人返回 403） |
| POST | `/api/agent/jobs/{id}/ack` | 回执 `{status, run_id}` |
| POST | `/api/agent/run` | 上传运行结果（自动归属到本次心跳的客户端/账号/角色） |
| POST | `/api/agent/run/{id}/report` | 上传报告 HTML（multipart，字段名 `file`） |
| POST | `/api/agent/run/{id}/shot` | 上传一张截图（`file` + `task_key` + `index`） |
| POST | `/api/agent/run/{id}/done` | 收尾，顺带触发旧记录清理 |

> **心跳路径是 `/api/agent/ping`（GET 轻量 / POST 带负载），不是 `/heartbeat`。**
> 早期文档里的 `/api/agent/heartbeat` 从未存在过，客户端 `stzb/cloud.py` 一直走的是 `/ping`。

> **为什么探活要走「客户端主动 POST」而不是服务端主动连客户端？**
> 后端可能在内网/公网、客户端在内网，服务端**连不进去**。所以在线状态一律由客户端主动上报
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
| `/users` | **用户管理**（仅管理员）：建号 / 改名 / 停用 / 调角色 / 重置口令 / 删号 |
| `/password` | 改**自己**的口令（任何登录用户；被重置过口令的人首登会被强制跳来这里） |
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
| **登录暴力破解** | 按 IP 限流（默认 15 分钟内 8 次失败即锁），失败和成功都记事件；用户名错与口令错耗时一致（用固定的假哈希拉平），不泄露哪个字段错，也防用户名枚举。 |
| **多用户数据隔离** | 账号 / 运行 / 待执行都有 `owner_id`；角色的归属顺着 `account_id → game_accounts.owner_id` 查（只存一处，避免「账号给了 A、角色还挂在 B 名下」）。过滤靠显式参数 `own_only` / `owner_id` 传递，**不用全局状态**（并发下会串味）。普通用户手敲 URL 或直接 POST 都被服务端挡掉，界面藏按钮只是顺手。 |
| **权限变更即时生效** | 会话里**只存 uid**（不存整个用户对象），每次请求现查库。管理员停用/降级某人后，那人下一次点任何页面就出不去 —— 塞进 session 的话得等会话过期，等于停用形同虚设。改名不影响会话（认 uid 不认名字）。 |
| **不给自己挖坑** | 不能停用/删除自己；不能改自己的角色（降级会当场丢管理员权限，之后就点不动了）；不能把**最后一个启用的管理员**降级/停用/删除。这三条都是为了不出现「谁也进不去后台、只能命令行救」。 |
| **一次性初始口令** | 管理员建号/重置时生成临时口令，只在跳转 URL 里带一次、页面显示一次，**不入库不出日志**（库里只有哈希）；被重置者首登被强制改成自己的口令，管理员全程不知道对方最终口令。 |
| **删用户不删数据** | 删用户时他的游戏账号**收归管理员**（`owner_id → NULL`）而不是级联删除 —— 账号上挂着历史运行记录和角色任务配置，连带清掉属于「惩罚过重且不可逆」。 |
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
