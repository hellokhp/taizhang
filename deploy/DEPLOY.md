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

**唯一事实源**：`/opt/taizhang/server/config.json` 的 `postgres` 段。后端主进程、AI Worker、标讯采集 CLI/Worker 全部经 `server/shared_config.py` 读取它，改地址 / 账号 / 库名只改这一个文件，改完重启相关进程。

```bash
cd /opt/taizhang
cp server/config.example.json server/config.json
vim server/config.json          # 填 host / port / user / password / dbname
python -m server.shared_config  # 打印脱敏摘要并实测连接
```

| 字段 | 说明 |
|---|---|
| `host` / `port` | PG 地址与端口 |
| `user` / `password` | 账号密码，**生产必须改成强密码** |
| `dbname` | 库名，默认 `yadecai` |

> `server/config.json` 含密码，已由根目录 `.gitignore` 忽略，代码库只提交模板 `config.example.json`。
> 兼容保留：显式设置 `PG_*` 环境变量、或用 `TAIZHANG_CONFIG` 指定另一份配置文件可临时覆盖；但项目自带的 `server/ai_worker/.env` 与 supervisor `environment` 已不再设置 `PG_*`，避免多份配置互相打架。
> `STORAGE_ROOT` / `TZ` / Redis / 模型密钥仍由各进程环境注入，属各自专属配置，本轮未动。

### 2.1a 标讯采集：详情正文 AI 抽取

金采网（`cfcpn`）等平台采用**两段式**采集：列表接口拿摘要 → 详情接口拿公告正文 → AI 从正文抽结构化字段 → 拼接详情页 URL 入库。

- **模型配置**：复用 `server/ai_worker/.env` 的 `DASHSCOPE_API_KEY` / `DASHSCOPE_BASE_URL` / `LLM_TEXT_MODEL`（或 `LLM_API_KEY` / `LLM_BASE_URL`）。该 Key 同时供 AI 配方生成与详情解析使用，不需要第二份配置。
- **依赖**：`server/requirements.txt` 需包含 `openai>=1.40`、`python-dotenv>=1.0`、`pypdf>=4.0`（正文为 PDF 的站点需要）。**多套 Python 并存时，每套实际跑后端/worker 的解释器都要装**：`python -c "import pypdf"` 逐个确认。
- **首次建表**：`python -m server.bidcrawl.cli init`（幂等，会加 `bid_item.detail jsonb` 列与 `bid_detail_ai` 缓存表），再 `import-recipes --path server/bidcrawl/recipes/cfcpn.json`。
- **成本控制**：AI 结果按 `sha1(平台 + prompt 版本 + 清洗后正文)` 存进 `bid_detail_ai`。同一公告被多个关键词命中、以及之后每轮跑批都直接读缓存，只有**首次遇到新正文**才调用模型；详情响应本身也走 `bid_fetch_cache`（TTL 7 天）。
- **降级**：无 Key / 模型超时 / 返回非法 JSON 时自动改用配方里的正则兜底，采集不中断；`bid_item.detail->'ai'->>'source'` 记录 `llm` / `cache` / `fallback`，便于审计。
- **关闭 AI**：任务参数加 `with_ai=false`，或 CLI 用 `--no-ai`（只走确定性正则）。
- **调试单条**：`python -m server.bidcrawl.cli detail --platform cfcpn --keyword 门头 --limit 1`。
- **站点频控**：配方 `transport.rate_limit.qps` 同时约束列表与详情请求，生产请保持 `0.1`（约 10 秒一次），不要用 `BIDCRAWL_RATE_SCALE=0`（仅本地联调可临时关闭限速）。

### 2.1b 正文为 PDF 的站点（cg.11185.cn）

中国邮政电子采购平台的详情页只有一个 pdfjs iframe，公告正文在 PDF 文件里。
配方 `server/bidcrawl/recipes/cg_11185.json` 声明 `detail.pdf`：

- `pdf_text.py` 从 iframe 的 `viewer.html?file=` 参数反转义出 PDF 直链（`%26`→`&`、`%3D`→`=`）。
  该直链是 `Content-Disposition: attachment` 的下载接口，必须按**二进制**下载再解析，不能当网页解析。
  解析库按 `pypdf → PyMuPDF → pdfplumber → pdfminer → PyPDF2` 自动降级，任意装一个即可；
  按 PDF URL hash 缓存文本到 `bid_fetch_cache`（同样 7 天），再交给同一套 AI / 正则抽取；
  `pdf_url` 会写进 `bid_item.detail` 便于追溯。
- 列表是 HTML：`searchBulletinList.html?categoryId=88&goSearch={keyword}&page={page}`，
  `ul.newslist > li` 10 条/页，生产 `qps=0.2`、`max_pages=13` 为硬上限。
- 部署：`import-recipes --path server/bidcrawl/recipes/cg_11185.json`；
  验证 `python -m server.bidcrawl.cli verify --platform cg_11185 --keyword 门头`。
- **试跑校验报「PDF 正文为空或过短」时**：现在错误信息会带上具体原因
  （未安装解析库 / 下载 HTTP 状态 / 响应不是 PDF / 扫描件无文本层），并在校验卡显示
  `HTTP 状态 · 字节数 · 附件文件名 · 实际使用的解析库`。
  最常见原因是后端那套 Python 没装任何解析库，执行 `pip install -r server/requirements.txt`
  后**重启后端**即可（旧代码在函数内 import，装完即可生效，无需重启也能恢复）。
- 所有解析库都缺失或下载失败时正文为空，AI 自动走降级，不影响列表入库；
  扫描件 / 图片型 PDF 尚无 OCR，需在页面标注「正文未解析」。

### 2.1c SPA 站点：无头浏览器嗅探（jc.abchina.com.cn）

农银e采等站点是 SPA：入口 HTML 只有空壳，主包是 eval 混淆的 webpack 产物，
接口只出现在异步 chunk 里——**纯 HTTP 抓不到**。系统在静态探测全部失败后，
会自动用**本机 Edge/Chrome + CDP** 无头渲染页面并拦截 XHR，再据此生成配方。

- **运行依赖**：目标机需有浏览器。Windows 自带 Edge 即可；Linux 装
  `chromium` 或 `google-chrome`，也可用 `BIDCRAWL_BROWSER=/path/to/chrome` 指定。
  无需安装 Playwright（内网 pip 常装不到），只用到环境里已有的 `websockets`
  （`uvicorn[standard]` 已带入）。
- **行为**：探测阶段会出现「浏览器嗅探」一步，显示实际使用的浏览器与捕获请求数；
  无浏览器时该步为 `skip` 并说明原因，其余站点完全不受影响。
- **资源保护**：同一时刻只允许一个嗅探进程（进程内互斥锁），单次超时约 40 秒，
  临时 user-data-dir 用完即删。若不想启用，设 `BIDCRAWL_NO_BROWSER=1`。
- **验证**：`python -m server.bidcrawl.browser_probe "<列表页URL>" 门楣`
  可单独测试本机能否嗅探成功。

### 2.1d 嵌套 dto 接口 + HTML 详情（cg.psbc.com）

邮储「邮银易采」的列表接口是 `POST /cms/api/search/searchKeyWord`，请求体嵌套：

```json
{"pageNo":1,"pageSize":50,"dto":{"keyWords":"{keyword}","siteId":"725"}}
```

- 接口参数 `siteId/categoryId` 不在入口页，而在列表页隐藏域
  （`<input type="hidden" name="chunkSiteId" / name="chunkId">`）。探测器会自动
  「入口页 → 导航里的招标信息列表页 → 读隐藏域 → 回填请求体」；`cms=/cms`
  这类全局前缀用于拼接口地址，不会写进请求体。
- 关键词接口无法识别时，会生成 `minimal` 变体（去掉 `categoryId` 等过滤条件，
  只留 `siteId` + 关键词），避免「分类是资格预审、搜不到招标公告」。
- 详情页是 CMS 静态页，正文在 `.content`（HTML 而非 PDF），配方用
  `detail.content_css`，`detail.request.url={url}`；AI 抽取与 PDF 型共用同一套字段。
- 影响字段：`publishDate` 形如 `Tue Apr 01 09:17:32 UTC 2025`，`normalize.parse_datetime`
  已支持该 Java Date 格式。
- 部署：`import-recipes --path server/bidcrawl/recipes/cg_psbc.json`；
  验证 `python -m server.bidcrawl.cli verify --platform cg_psbc --keyword 门头`。
- 响应体积：搜索接口会带 `content` 摘要，50 条约 600KB；配方 `max_response_mb` 已设 20。
  若站点对 `pageSize` 上限更严，`iter_pages` 会自动降档重试。

### 2.1e 生成配方：证据模式（curl + 返回样本）

「平台与配方 → AI 添加平台」现在有三种模式可切换，**默认 `接口 JSON`**：

| 模式 | 需要填 | 说明 |
| --- | --- | --- |
| 接口 JSON（`api_list`） | 列表 curl + 列表 JSON 样本；详情 curl + 详情 JSON/HTML 样本 | 有数据接口的站点，最稳 |
| HTML 列表（`html_list`） | 列表 curl + 列表 HTML 源码；详情 curl + 详情 HTML 源码 | CMS 型站点，正文可为 PDF |
| 自动探测（`auto`） | 入口 URL + 样本关键词 | 旧链路，结构简单的站点兜底 |

- 取证方法：浏览器 F12 → Network → 找到列表/详情请求 → 右键 **Copy as cURL (bash)**；
  返回样本就是该请求的 Response（JSON）或页面源码（Ctrl+U）。
- 系统只做确定性反推：`items_path / total_path / row_selector / 字段 CSS /
  关键词参数 / 分页参数 / 详情请求模板 / 详情页 URL 模板 / PDF 直链`，
  然后三级校验：结构校验 → **样本回测** → 真实请求试跑。
- 生成的配方 `status=paused`、关闭全局采购人白名单，审核后在「配方」里启用。
- **详情页 URL 示例**：列表结果里没有详情页链接时（接口型站点很常见），在这里粘贴
  任意一条公告的**真实地址**即可，系统会用列表行里的 id 字段套成
  `...?messageId={messageId}` 模板；直接写 `{messageId}` 也可以。不填也不会崩，
  但跳转链接只能用兜底地址、可能打不开，时间线会明确告警。
- **HTML 列表模式**还支持写 `https://站点域名{href}`（用行内第一个链接拼详情地址），
  适合列表里混着分类/栏目链接的站点（如 365trade）。
- 详情样本是网页时，配方一律 `GET {url}` 取页面（即使你贴的详情 curl 是接口地址），
  避免拿接口去解析 `.content` 导致正文为空。
- **HTML 列表模式的兼容性**：行元素本身是 `<a>`（`<div><a class="item">…</a>×N`）
  也能识别；类名里的 Tailwind 工具类（`sm:flex`、`w-1/2`）不会拼成非法 CSS 选择器，
  反推出的选择器一律先回测命中才写入配方。解析失败时接口返回可读提示（HTTP 200 +
  `ok:false`），不会再抛 500；若真出现内部异常，错误里带末几行 traceback 便于排查。
- **PDF 正文源按证据挑选**：门户页脚常驻《平台注册规则.pdf》这类文档，而且列表页也有
  同样几个链接。系统会优先用「正文容器内 + 锚文本像附件」的 PDF；文档名像规则/指南/
  注册说明、或同一份 PDF 也出现在列表页，一律排除；正文容器本身有 ≥300 字时直接以
  HTML 为正文。试跑还会校验 PDF 内容是否像公告（命中标题或招标用语），防止"能打开
  但内容是帮助文档"的假成功。
- 列表里的相对链接（`/zgczb/897997.jhtml`）会按 `meta.base_url` 自动补成绝对地址，
  否则详情请求会因「URL missing protocol」静默失败（表现就是：列表能入库、没详情）。
- 页码在路径里（`search.jspx` → `search_2.jspx`）的站点会被自动识别成
  `pagination.page_in_url=true`，第 1 页与后续页分别取 `first_url` / `url_template`。
- **详情页被反爬中间页拦截**（阿里云 `acw_sc__v2`、瑞数 `$_ts`、加速乐
  `__jsl_clearance`，常见 200/403/412 + 一段 JS 挑战脚本，正文 0 字符）时，
  会自动用系统 Edge/Chrome 无头打开该页、跑完挑战脚本（UA 覆盖为普通桌面
  Chrome，避免无头 UA 被直接 400），
  拿回渲染后的 HTML，并把换到的 cookie 复用给后续详情请求（约 1 小时有效）。
  因此**运行机需要装有 Edge 或 Chrome**；路径可用 `BIDCRAWL_BROWSER` 指定，
  想彻底禁用设 `BIDCRAWL_NO_BROWSER=1`（此时遇到挑战会给出明确报错，不会静默空正文）。
  反爬中间页不会被写入详情缓存，历史缓存里的中间页也会被忽略并重新抓取。
- 部署无需额外 Python 依赖（纯 httpx + parsel + websockets，均已在 `requirements.txt`）；
  过盾仅额外依赖本机浏览器。
- 列表页若同页有多份结构相同的 `<ul>`（站点切换器、栏目菜单、公告列表），
  生成器会用「最近带 class 的祖先 + nth-of-type」锚定公告那一份，并对「链接都指向
  别的域名」的行重罚；用户填 `https://host{href}` 时，若样本里 href 本身是绝对
  地址，模板会自动退化为 `{_href}`，不会拼出双域名（曾表现为 `getaddrinfo failed`）。
  接口：`POST /api/bidcrawl/analyze`，`mode` 为 `api_list` / `html_list` 时走证据模式。
- 注意：依赖登录态的站点，curl 里的 Cookie 会过期；过期后重新粘贴一次 curl
  生成新版本即可（旧版本保留可回滚）。
- **排查「改了没生效」**：`POST /api/bidcrawl/analyze` 的响应里带
  `generator_version`（当前 `2026-11-17.4`）；对不上说明进程还是旧代码，重启即可。
- 识别不出列表行时，返回的 `error` / `diag` 会写明原因（候选组数、最大组行数、
  是否反推不出选择器），而不是笼统一句「未识别出公告行」。
- 自检：`python -m server.bidcrawl.selftest`（151 项，含证据模式、反爬降级、
  PDF 正文源选择、列表反推健壮性与诊断信息等离线用例）。

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
# 把 server/config.json 的 postgres.dbname 改为 yadecai_new，确认无误后启动
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
