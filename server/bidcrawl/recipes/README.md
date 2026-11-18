# 站点配方（种子）

本目录每个 JSON 是一个招标站点的**采集配方**：请求方式、分页、字段取值规则、
详情正文位置、校验阈值。它们是「插件」而不是数据——运行时真正生效的是数据库
`bid_recipe_version` 里的配方，这里只是**新环境初始化用的种子**和格式样例。

## 导入

```bash
python -m server.bidcrawl.cli init              # 建表 + 种子配置（首次）
python -m server.bidcrawl.cli import-recipes    # 导入本目录全部 JSON 并启用
python -m server.bidcrawl.cli platforms         # 确认结果
```

`import-recipes` 的行为：文件里的 `meta.id` 就是平台 id；每次导入都会**新建一个版本号
并立即启用（status=active）**。`meta.version` 在文件里只是导出时的线上版本，导入时会被
重新编号（新库从 1 开始）。

只导单个文件：`--path server/bidcrawl/recipes/cfcpn.json`。

## 站点清单

| 文件 | 平台 id | 站点 | 形态 | 详情 |
|---|---|---|---|---|
| `365trade.json` | 365trade | 中招联合招标采购平台 | JSON 接口 + 路径式分页 | HTML 正文（需过阿里云 WAF） |
| `bocomgys.json` | bocomgys | 交通银行智采 | JSON 接口 | HTML 正文 |
| `cfcpn.json` | cfcpn | 金采网 | JSON 接口（列表/详情同接口） | `noticeContent` |
| `cg_11185.json` | cg_11185 | 中国邮政电子采购与物资供应平台 | HTML 列表 | PDF（pdfjs iframe） |
| `cg_psbc.json` | cg_psbc | 邮银易采平台 | JSON 接口 | HTML `.content` |
| `cgjy.json` | cgjy | 中烟电子采购平台 | JSON 接口 | HTML 正文优先 |
| `ctpch.json` | ctpch | 中银智采 | JSON 接口 | `ancmCntnt` |
| `hnzb.json` | hnzb | 中通服供应链湖南分公司 | HTML 列表 | HTML 正文（瑞数反爬，走浏览器过盾） |
| `jc.json` | jc | 农银 e 采平台 | JSON 接口 | SPA hash 路由 |

`archive/` 下是历史遗留、**不参与导入**（`import-recipes` 只扫本目录一级 `*.json`）。
`abchina_jc.json` 是旧手写配方，平台 id 为 `abchina_jc`，已被线上 `jc` 取代，
导入它只会多建一个重复平台。

## 注意

1. **导出时已剥离登录会话类的 `Cookie` / `Authorization`**（`JSESSIONID`、`acw_tc`、
   瑞数 `_ts` 等换环境即失效，留着只会让请求带着死会话）。若某站点在新环境返回
   403/412，请在浏览器登录后复制 Cookie，填到配方的 `transport.headers.Cookie`。
   `token: undefined` 这类前端字面量**保留**（部分网关要求原样带上）。
2. **不要把这些文件当线上配置来改**。线上改动走「平台与配方」页面或
   `PUT /api/bidcrawl/platforms/{id}/recipe`，改完如需备份再重新导出。
3. 对**已有平台**的数据库跑 `import-recipes` 会新开版本并覆盖启用，等于回退到文件里的
   状态。只在全新库、或明确要回滚到这些文件时使用。
4. 导入后建议在页面对每个平台点一次「试跑」，确认真实环境仍能取到数据（站点改版、
   Cookie 失效都可能导致失败），再让任务跑批。
