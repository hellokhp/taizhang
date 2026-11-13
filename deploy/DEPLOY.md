# 亚德彩投标台账 · 前后端部署手册

> 技术栈：FastAPI + **PostgreSQL**（后端）/ Vue3 + Vite（前端）/ Nginx（反向代理 + 静态托管）/ Supervisor（进程守护）
> 适用：Linux 生产环境（内网协作工具）
> 配套方案：`docs/平台卡死治理与故障隔离方案.md` v2.0 —— 超时分级、任务中心、进程隔离分阶段实施
>
> **本次修订**：本手册原按 SQLite 编写（`server/data.db`、删文件即重置、`cp data.db` 即备份），
> 与当前后端已切到 PostgreSQL 的事实不符，已全面改为 PG 口径。`data.db` 文件已不存在，
> 代码里只剩一次性 `migrate_from_sqlite()` 迁移入口。

---

## 0. 目录约定与数据分布

代码部署在 `/opt/taizhang`，**业务数据不在代码目录里**：

```
/opt/taizhang/
├── server/
│   ├── main.py            # 后端主进程（Python 3.8 venv）
│   ├── ai_schema.sql      # 全库 DDL 唯一事实源（后端 + AI Worker 共同执行，必须随代码发布）
│   ├── ai_worker/         # 开标汇总 AI Worker（独立 Python >=3.10 venv）
│   └── users.json         # 账号清单（启动时同步进 PG；只读取，不原地更新）
├── frontend/dist/         # 前端构建产物
└── deploy/
    ├── taizhang.conf      # Supervisor 配置 + 末尾 Nginx 参考配置
    └── DEPLOY.md          # 本手册
```

| 数据 | 位置 |
|---|---|
| 台账 / 账号 / 权限 / 会话 / 审计 / AI 队列与结果 | PostgreSQL 库 `yadecai` |
| 文件实体（blob、分片临时目录、旧版本） | `STORAGE_ROOT`（默认 `/var/lib/taizhang/storage`） |
| 运行日志 | `/var/log/taizhang/app.log`、`ai-worker.log` |

> **备份必须 DB（`pg_dump`）与 `STORAGE_ROOT` 目录一起做**，只备一边会导致元数据与实体不一致。

---

## 1. 环境准备

```bash
# --- PostgreSQL（必装）---
sudo apt install -y postgresql            # CentOS/RHEL 用 dnf install postgresql-server
sudo -u postgres psql -c "CREATE USER taizhang WITH PASSWORD '改成强密码';"
sudo -u postgres psql -c "CREATE DATABASE yadecai OWNER taizhang;"
# 首次启动由后端自动建表，不需要手工执行 SQL

# --- 后端主进程 venv ---
python3 -m venv /opt/taizhang/venv
/opt/taizhang/venv/bin/pip install -r /opt/taizhang/server/requirements.txt
# 包含 fastapi / uvicorn / pydantic / psycopg2 等；不要只装前三个，PG 驱动缺失会启动失败

# --- AI Worker 专用 venv（Python >= 3.10，与主进程 3.8 隔离）---
python3.10 -m venv /opt/taizhang/ai-venv
/opt/taizhang/ai-venv/bin/pip install -r /opt/taizhang/server/ai_worker/requirements.txt

# --- Redis（可选，仅作 AI 实时进度信号）---
# 未安装或未就绪时 Worker 自动降级为 3 秒轮询，不会启动失败；
# 生产须设置 requirepass + bind 127.0.0.1 + 独立 db，并在 REDIS_URL 中带密码。

# --- Node（仅构建前端需要；也可本地构建后只上传 dist）---
node -v   # 需 18+

# --- 运行用户与目录 ---
useradd -r -s /sbin/nologin taizhang
chown -R taizhang:taizhang /opt/taizhang/server
mkdir -p /var/log/taizhang /var/lib/taizhang/storage
chown -R taizhang:taizhang /var/log/taizhang /var/lib/taizhang/storage
chmod 750 /var/lib/taizhang/storage
```

---

## 2. 后端部署

### 2.1 PostgreSQL 连接配置

后端读取以下环境变量（未设置时走 `server/main.py` 的默认值 `127.0.0.1:5432 / postgres / yadecai`）：

| 变量 | 说明 |
|---|---|
| `PG_HOST` / `PG_PORT` | PG 地址与端口 |
| `PG_USER` / `PG_PASSWORD` | 账号密码，**生产必须显式配置** |
| `PG_DB` | 库名，默认 `yadecai` |
| `STORAGE_ROOT` | 文件实体根目录 |
| `TZ` | 固定 `Asia/Shanghai` |

> **两个 program 的 `PG_*` 必须逐项一致**：主进程与 AI Worker 连的不是同一个库时，任务与结果会分家。

### 2.2 账号配置

编辑 `/opt/taizhang/server/users.json`（首次启动若不存在会自动生成默认 `admin/123456`）：

```json
{
  "admin":  {"password": "请改成强密码", "display_name": "管理员", "is_admin": true},
  "zhangwei": {"password": "123456", "display_name": "张伟"}
}
```

> 修改 `users.json` 后**重启主进程**即生效（启动时同步进 PG 的 `users` / `roles` 表）。
> 会话存在 PG `sessions` 表，重启不会让在线用户掉登录。

### 2.3 Supervisor 守护

```bash
cp /opt/taizhang/deploy/taizhang.conf /etc/supervisor/conf.d/taizhang.conf
# CentOS/RHEL: cp /opt/taizhang/deploy/taizhang.conf /etc/supervisord.d/taizhang.ini
# 先按文件顶部【必改】清单填好路径、venv、运行用户、PG_*、REDIS_URL 与各类密钥

supervisorctl reread
supervisorctl update
supervisorctl status        # taizhang 与 taizhang-ai-worker 都应显示 RUNNING
```

后端监听 `127.0.0.1:8000`，**当前必须单进程（`--workers 1`）**。

> 原因不是数据库（PG 完全支持并发），而是 `server/main.py` 仍有进程内全局 `RLock`
> （`fs.py`、`ai_summary_api.py` 共用同一把）。多 worker 会让这把锁失效并导致并发写冲突。
> 待《平台卡死治理与故障隔离方案 v2.0》§6.1 移除全局锁后，方可启用多 worker 或按域拆进程（§7）。

### 2.4 开标汇总 AI Worker（独立进程）

- 与后端主进程**不同 venv、不同 Python 版本**，由 `[program:taizhang-ai-worker]` 单独守护，不占用请求线程。
- 启动顺序无依赖：Worker 与后端都会幂等执行 `server/ai_schema.sql`（全库 DDL 唯一事实源），缺该文件会自检失败退出。
- **SQL 占位符两套规则**：后端 `PgDB` 用 `?`（内部转 `%s`），Worker 用 `%s`，DDL 脚本不含占位符且禁裸 `%` —— 跨用会直接报语法错。
- PG 是队列与结果的唯一事实源；Redis 只作可丢的实时信号（SSE 唤醒），可降级轮询，不得当成第二份状态。
- 密钥（PG 密码、`REDIS_URL`、`DASHSCOPE_API_KEY`、`OFFICE_SECRET`）只放 supervisor `environment` 或 KMS，**不要提交进代码库**；模板里一律是 `CHANGE_ME` 占位符。

---

## 3. 前端部署

```bash
cd /opt/taizhang/frontend
npm install
npm run build         # 产物输出到 frontend/dist
```

> 也可在本地 `npm run build` 后只上传 `frontend/dist`，服务器无需装 Node。

---

## 4. Nginx 配置

**唯一权威配置**：Linux 生产请用 `deploy/taizhang.conf` 末尾注释里的 Nginx 参考配置；
Windows 开发用 `server/nginx.conf`。本手册不再内嵌第三份配置，避免分叉。

```bash
# 把参考配置写入 /etc/nginx/conf.d/taizhang.conf 后
nginx -t && systemctl reload nginx
```

四个不能漏的点：

1. **SSE 进度流**：`location ~ ^/api/ai-summary/tasks/[0-9]+/stream$` 单独配置，
   `proxy_read_timeout 1900s` + `proxy_buffering off` + `proxy_cache off`；
   用普通 `/api/` 的超时会掐断开标汇总实时进度。
2. **分片上传/合并**：`proxy_request_buffering off` + `proxy_buffering off`，
   读写超时 600s；否则整包先落盘，分片失效且易超时。
3. **请求体上限**：`client_max_body_size 32m`（分片 16MB 的 2 倍余量，小于分片会 413）。
4. **SPA 兜底**：`location / { try_files $uri $uri/ /index.html; }`（刷新直达不 404）。
5. **健康检查**：`GET /healthz`（免登录，返回进程 / PG 连通 / 任务积压），DB 异常返回 503；
   SSE 与上传不要用普通 API 的 60s 超时。

> 平台卡死治理第 1 步**已落地**：普通 `/api/` 60s；`/api/fs/upload/` 与仍是同步执行的
> `/api/merge`、`/api/clear` 单独 600s；SSE 1900s。导入在第 2 步异步化后应移除其 600s 例外。
> 二期 design/finance 接入后按方案 §7.3 按部门前缀分发（含 `/api/core/`、`/api/fs/`、`/api/jobs/` 等共享前缀与旧路径兜底）。
> **Nginx 反代后客户端 IP 恒为 127.0.0.1，内部接口不要靠 IP 鉴权**，限流按用户维度做。

浏览器访问 `http://taizhang.internal` 即可。

---

## 5. 验证

```bash
# PG 连通性（在部署机上）
psql "host=127.0.0.1 port=5432 user=taizhang dbname=yadecai" -c "SELECT count(*) FROM users;"

# 后端接口
curl http://127.0.0.1:8000/api/perm/catalog   # 返回 {"detail":"未登录"} 即正常
curl -X POST http://127.0.0.1:8000/api/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"你的密码"}'

# AI Worker 是否在跑
supervisorctl status taizhang-ai-worker
tail -f /var/log/taizhang/ai-worker.log

# 健康检查（免登录；DB 正常返回 200，异常返回 503）
curl -s http://127.0.0.1:8000/healthz

# 前端
curl -I http://taizhang.internal              # 200 + index.html
```

> `/healthz` 已随方案 v2.0 第 1 步落地，可直接接入巡检 / 负载均衡探测。
> 慢请求超过 1 秒会在 `app.log` 打 `SLOW` WARN；阈值可用 `SLOW_REQUEST_MS` 调整。
> 重活并发上限默认 2（导入/清空为 1），可用 `HEAVY_CONCURRENCY` 调整，超出返回 429 而不是排队。
> 全局锁等待上限默认 3 秒（`LOCK_WAIT_SECONDS`），超时返回 **503 而不是全站无限排队**；
> 纯读接口已不参与全局互斥。**这三个参数都依赖进程内状态，`--workers 1` 不能放开**：
> 多进程后各进程各持一把锁，会产生重复数据（见方案 §15.6）。

---

## 6. 数据库备份与重置（PostgreSQL）

**⚠️ 重置是不可逆的破坏性操作，务必先完成备份并确认备份可用，再执行。**

### 6.1 备份（DB + 文件实体，缺一不可）

```bash
BK=/var/backups/taizhang && sudo mkdir -p "$BK" && TS=$(date +%Y%m%d_%H%M%S)
sudo -u postgres pg_dump -Fc yadecai > "$BK/db_$TS.dump"
sudo tar czf "$BK/storage_$TS.tar.gz" -C /var/lib/taizhang storage
```

恢复：

```bash
sudo -u postgres pg_restore -d yadecai --clean --if-exists /var/backups/taizhang/db_XXXX.dump
sudo tar xzf /var/backups/taizhang/storage_XXXX.tar.gz -C /var/lib/taizhang
```

### 6.2 重置方式一：新建空库（推荐，旧库可保留回退）

```bash
supervisorctl stop taizhang taizhang-ai-worker
sudo -u postgres psql -c "CREATE DATABASE yadecai_new OWNER taizhang;"
# 把两个 program 的 environment 里 PG_DB 改为 yadecai_new，确认无误后启动
supervisorctl start taizhang taizhang-ai-worker
```

首次启动会由 `main.py` / `fs.py` / `ai_schema.sql` 幂等建表，账号从 `users.json` 重新同步。

### 6.3 重置方式二：原地清空 public schema（危险）

```bash
supervisorctl stop taizhang taizhang-ai-worker
sudo -u postgres pg_dump -Fc yadecai > /var/backups/taizhang/db_before_reset.dump   # 必须先备份
sudo -u postgres psql -d yadecai -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
supervisorctl start taizhang taizhang-ai-worker
```

> 重置后**台账数据、审计日志、删除记录、文件元数据、AI 汇总结果全部清空**；
> `STORAGE_ROOT` 里的实体文件不会自动删除，需另行清理，否则产生孤儿 blob。
> 不再有「删 `data.db` 文件」这种操作 —— 那是 SQLite 时代的做法，对 PG 无效。

---

## 7. 日常运维

| 操作 | 命令 |
|---|---|
| 重启后端 | `supervisorctl restart taizhang` |
| 重启 AI Worker | `supervisorctl restart taizhang-ai-worker` |
| 查看日志 | `tail -f /var/log/taizhang/app.log`、`tail -f /var/log/taizhang/ai-worker.log` |
| 修改账号 | 编辑 `server/users.json` 后 `supervisorctl restart taizhang` |
| 改 PG 连接/密钥 | 改 `taizhang.conf` 两个 program 的 `environment`，`supervisorctl reread && update && restart` |
| 更新前端 | 重新 `npm run build` 替换 `frontend/dist`，Nginx 无需重启 |
| 备份数据 | `pg_dump -Fc yadecai` + `tar czf` 打包 `STORAGE_ROOT`，两者一起 |
| 查看慢接口 | `tail -f /var/log/taizhang/app.log \| grep SLOW`（超过 1 秒自动 WARN） |
| 查看慢 SQL | PG `log_min_duration_statement = 1s`，需 DBA 在 `postgresql.conf` 启用 |
| 健康检查 | `curl -s http://127.0.0.1:8000/healthz` |

---

## 8. ONLYOFFICE 在线预览 / 编辑（可选）

文件存储里的 docx / xlsx / pptx / csv / txt 可以直接在浏览器里预览与编辑（pdf、doc/xls/ppt 只读预览），
保存时自动生成新版本，旧版本照常进入「7 天备份」。由独立的 ONLYOFFICE Document Server（Docker）提供能力。

### 8.1 部署 DS

```bash
cd /opt/taizhang/deploy/onlyoffice
# 先改 docker-compose.yml 里的 JWT_SECRET（与后端 OFFICE_SECRET 完全一致）
docker compose up -d
curl http://127.0.0.1:8888/healthcheck        # 等 1-2 分钟，返回 true 即就绪
```

### 8.2 后端环境变量（三处任选其一，优先级：设置页 > 下面两处）

| 变量 | 值 | 说明 |
|---|---|---|
| `OFFICE_SERVER` | `http://<内网IP>:8888` | **浏览器**访问 DS 的地址；也可在「文件存储→设置」里改 |
| `OFFICE_SECRET` | 与容器 `JWT_SECRET` 一致 | DS 开了 JWT 就必填，不一致编辑器打不开 |
| `OFFICE_BASE_URL` | `http://<宿主机内网IP>:8000` | **DS 容器**回源拉文件/发回调的地址；容器里的 127.0.0.1 是容器自己 |

- Windows 开发机：`start_api.bat` 已写好示例，用 `http://host.docker.internal:8000`。
- Linux 生产：`deploy/taizhang.conf` 中 `[program:taizhang]` 的 `environment` 段，用宿主机内网 IP，DS 容器需 `host-gateway`。
- 设置页（推荐）：「文件存储 → 设置 → office_server / office_secret / office_base_url」，改完立即生效、不必重启。

**端口不能省**：只写 `http://host.docker.internal` 会让 DS 去连容器的 80 端口。
后端有兜底（自动补浏览器访问后端的端口），但显式写端口最稳（前面有 Nginx 反代 80 时可省略）。

### 8.3 验证

登录 → 文件存储 → 上传一个 .docx → 列表行出现「编辑」按钮（只有查看权限的用户是「预览」）→
打开修改 → 点工具栏「保存」→ 列表版本号 +1，「版本」里能看到「在线编辑保存前备份」的旧版本。

一条命令验证容器能否回源（返回 401 即为通，401 只是没登录）：

```bash
docker exec taizhang-onlyoffice curl -s -o /dev/null -w "%{http_code}\n" \
  http://host.docker.internal:8000/api/fs/office/enabled
```

### 8.4 常见问题

> 打开文档报错时，错误面板上有「**一键诊断**」按钮，会把「DS 地址 / DS 可达 / 回源地址 /
> 回源取文件 / JWT 密钥」五项真值摊开并给出结论，先点它。

| 错误码 | 含义 | 处理 |
|---|---|---|
| -4 | 与文档服务连接断开 | DS 重启中或网络抖动，重开即可 |
| -7 | 配置签名校验失败 | `OFFICE_SECRET` ≠ 容器 `JWT_SECRET` |
| -8 | 无法连接文档服务 | DS 未就绪 / 地址端口不对 |
| -21 / -24 | **无法下载文件**（最常见） | `OFFICE_BASE_URL` 指向了容器自己，或漏写端口 |
| -2 / -3 | 打开超时 / 转换失败 | 文件损坏，或后缀与实际格式不符 |
| -29 | 不支持的格式 | 老格式 doc/xls/ppt 只能预览，先转存 docx/xlsx/pptx |

- **报「token is not correctly formed」或 -7**：核对
  `docker inspect taizhang-onlyoffice --format '{{range .Config.Env}}{{println .}}{{end}}' | grep JWT`。
  改完 compose 的 `JWT_SECRET` 必须 `docker compose up -d --force-recreate`（环境变量不能在运行中的容器上改）。
- **保存后列表没变**：刷新列表；仍不变看后端日志搜 `[office] 回调处理失败`。
- **文档打不开但不报错**：浏览器直接访问 config 里的 `document.url`，能下载说明回源正常，问题在 JWT。
