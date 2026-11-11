# 亚德彩投标台账 · 前后端部署手册

> 技术栈：FastAPI + SQLite（后端） / Vue3 + Vite（前端） / Nginx（反向代理 + 静态托管） / Supervisor（进程守护）
> 适用：Linux 生产环境（内网协作工具）

---

## 0. 目录约定

假设代码部署到服务器 `/opt/taizhang`，结构如下：

```
/opt/taizhang/
├── server/                 # 后端
│   ├── main.py
│   ├── data.db             # SQLite 数据库（自动生成，勿删）
│   └── users.json          # 账号清单（启动时同步进库）
├── frontend/
│   └── dist/               # 前端构建产物（npm run build 生成）
└── deploy/
    └── taizhang.conf       # Supervisor 配置（已生成）
```

部署前把整个项目（含 `frontend/dist`）上传到 `/opt/taizhang`。

---

## 1. 环境准备

```bash
# Python
python3 -m venv /opt/taizhang/venv
/opt/taizhang/venv/bin/pip install fastapi uvicorn pydantic

# Node（仅构建前端需要，服务器也可本地构建后只上传 dist）
node -v   # 需 18+

# 运行用户（不要用 root 跑业务进程）
useradd -r -s /sbin/nologin taizhang
chown -R taizhang:taizhang /opt/taizhang/server
mkdir -p /var/log/taizhang && chown taizhang:taizhang /var/log/taizhang
```

---

## 2. 后端部署

### 2.1 账号配置

编辑 `/opt/taizhang/server/users.json`（首次启动若不存在会自动生成默认 `admin/123456`）：

```json
{
  "admin":  {"password": "请改成强密码", "display_name": "管理员", "is_admin": true},
  "zhangwei": {"password": "123456", "display_name": "张伟"}
}
```

> 修改 `users.json` 后重启服务即生效（启动时自动同步进库）。

### 2.2 Supervisor 守护

```bash
cp /opt/taizhang/deploy/taizhang.conf /etc/supervisor/conf.d/taizhang.conf
# CentOS/RHEL: cp /opt/taizhang/deploy/taizhang.conf /etc/supervisord.d/taizhang.ini

supervisorctl reread
supervisorctl update
supervisorctl status taizhang        # 应显示 RUNNING
```

后端监听 `127.0.0.1:8000`，**必须单进程**（SQLite + 进程级锁，多 worker 会锁库）。

---

## 3. 前端部署

```bash
cd /opt/taizhang/frontend
npm install
npm run build         # 产物输出到 frontend/dist
```

> 也可在本地 `npm run build` 后只上传 `frontend/dist` 目录，服务器无需装 Node。

---

## 4. Nginx 配置

新建 `/etc/nginx/conf.d/taizhang.conf`：

```nginx
server {
    listen 80;
    server_name taizhang.internal;            # 改成实际域名或内网 IP

    root /opt/taizhang/frontend/dist;         # 前端构建产物
    index index.html;

    client_max_body_size 20m;                 # 允许导入较大 Excel

    location /api/ {                          # API 反代到 uvicorn
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location / {                              # SPA history 路由回退
        try_files $uri $uri/ /index.html;
    }
}
```

```bash
nginx -t && systemctl reload nginx
```

浏览器访问 `http://taizhang.internal` 即可。

---

## 5. 验证

```bash
# 后端健康
curl http://127.0.0.1:8000/api/perm/catalog   # 返回 {"detail":"未登录"} 即正常
# 登录测试
curl -X POST http://127.0.0.1:8000/api/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"你的密码"}'

# 前端
curl -I http://taizhang.internal              # 200 + index.html
```

---

## 6. 数据库重置方法

本系统数据全部存在 `server/data.db`（SQLite）。**重置 = 删除该文件，重启服务后表结构自动重建、账号从 `users.json` 重新同步。**

### 方式一：一条命令重置（保留 users.json）

```bash
# 停服 → 备份 → 删除 → 起服
supervisorctl stop taizhang
cp /opt/taizhang/server/data.db /opt/taizhang/server/data.db.bak.$(date +%Y%m%d_%H%M%S)
rm -f /opt/taizhang/server/data.db
supervisorctl start taizhang
```

### 方式二：连账号一起重置

```bash
supervisorctl stop taizhang
rm -f /opt/taizhang/server/data.db /opt/taizhang/server/users.json
supervisorctl start taizhang
# 启动后自动生成默认管理员 admin / 123456，登录后立即改密码
```

> 重置后所有台账数据、审计日志、删除记录都会清空，操作前务必确认已备份。

---

## 7. 日常运维

| 操作 | 命令 |
|---|---|
| 重启后端 | `supervisorctl restart taizhang` |
| 查看日志 | `tail -f /var/log/taizhang/app.log` |
| 修改账号 | 编辑 `server/users.json` 后 `supervisorctl restart taizhang` |
| 更新前端 | 重新 `npm run build` 替换 `frontend/dist`，Nginx 无需重启 |
| 备份数据 | `cp server/data.db server/data.db.bak`（复制该文件即完成备份） |

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
- Linux 生产：`taizhang.conf` 的 `environment` 段，用宿主机内网 IP，DS 容器需 `host-gateway`。
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
