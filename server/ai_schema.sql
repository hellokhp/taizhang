-- ============================================================================
-- 开标汇总 AI · 统一 DDL（唯一事实源）
--
-- 由两处共同执行，都必须幂等：
--   1) 后端 3.8：server/ai_summary_api.py::init_tables() -> db.executescript()
--   2) Worker 3.10：server/ai_worker/schema.py::ensure_schema()
--
-- 两个进程 Python 版本与依赖不同、不能共享 Python 模块，但 DDL 是纯 SQL，
-- 共用一份文件即可避免「Worker 先于后端启动时 ai_* 表不存在」。
--
-- 约束：
--   - 只能用 PG 语法；全部 CREATE ... IF NOT EXISTS / ON CONFLICT DO NOTHING。
--   - 禁止出现 `?` 占位符（后端 executescript 直接整段执行，不做参数替换）。
--   - 禁止出现裸 `%` 字符（psycopg2 可能当作占位符前缀）。
--   - 老库补列不在这里做：PG 无 ADD COLUMN IF NOT EXISTS，由两侧 Python 逐条 try。
-- ============================================================================

CREATE TABLE IF NOT EXISTS ai_summary_task (
  id             BIGSERIAL PRIMARY KEY,
  rid            TEXT NOT NULL,
  source_nodes   JSONB NOT NULL DEFAULT '[]',
  source_kind    VARCHAR(16) DEFAULT 'tender',
  fingerprint    CHAR(32) NOT NULL DEFAULT '',
  force_refresh  SMALLINT DEFAULT 0,
  status         VARCHAR(16) NOT NULL DEFAULT 'PENDING',
  stage          VARCHAR(24), stage_label TEXT, progress SMALLINT DEFAULT 0,
  total_files INT DEFAULT 0, parsed_files INT DEFAULT 0,
  failed_files INT DEFAULT 0, skipped_files INT DEFAULT 0,
  current_file TEXT, mode VARCHAR(16),
  prompt_version TEXT DEFAULT '',
  summary_id BIGINT,
  cancel_requested SMALLINT DEFAULT 0, attempts SMALLINT DEFAULT 0,
  claimed_by TEXT, error_msg TEXT,
  tokens_in INT DEFAULT 0, tokens_out INT DEFAULT 0,
  started_at DOUBLE PRECISION, finished_at DOUBLE PRECISION,
  updated_at DOUBLE PRECISION NOT NULL,
  created_by TEXT, created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_task_status ON ai_summary_task(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_ai_task_rid ON ai_summary_task(rid, id DESC);
CREATE INDEX IF NOT EXISTS idx_ai_task_fp ON ai_summary_task(rid, fingerprint, status);

CREATE TABLE IF NOT EXISTS ai_summary (
  id BIGSERIAL PRIMARY KEY,
  task_id BIGINT NOT NULL REFERENCES ai_summary_task(id) ON DELETE CASCADE,
  rid TEXT NOT NULL, mode VARCHAR(16) NOT NULL,
  lot_no TEXT, lot_name TEXT,
  status VARCHAR(12) DEFAULT 'DRAFT',
  project_name TEXT, project_code TEXT, tenderer TEXT, agency TEXT,
  open_time_raw TEXT, open_time DATE, open_place TEXT,
  budget NUMERIC(18,2), eval_method TEXT, bidder_count INT,
  warnings JSONB DEFAULT '[]', confirmed_snapshot JSONB,
  raw_result JSONB DEFAULT '{}', prompt_version TEXT DEFAULT '',
  confirmed_by TEXT, confirmed_at DOUBLE PRECISION,
  created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_summary_rid ON ai_summary(rid, id DESC);

CREATE TABLE IF NOT EXISTS ai_bidder (
  id BIGSERIAL PRIMARY KEY,
  summary_id BIGINT NOT NULL REFERENCES ai_summary(id) ON DELETE CASCADE,
  lot_id BIGINT, seq INT,
  bidder_name TEXT NOT NULL, bidder_name_norm TEXT, is_ours SMALLINT DEFAULT 0,
  is_consortium SMALLINT DEFAULT 0, consortium_members TEXT,
  bid_price NUMERIC(18,2), incl_total NUMERIC(18,2), total_net NUMERIC(18,2),
  tax_rate NUMERIC(6,4),
  duration TEXT, deposit NUMERIC(18,2),
  legal_person TEXT, authorized_rep TEXT, qualification VARCHAR(8),
  reject_reason TEXT, remark TEXT,
  price_score NUMERIC(8,3), rank INT, deviation NUMERIC(8,4),
  row_status VARCHAR(12) DEFAULT '正常', flags JSONB DEFAULT '[]',
  source_node_id TEXT, source_sheet TEXT, source_row INT,
  created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_bidder_summary ON ai_bidder(summary_id, seq);

CREATE TABLE IF NOT EXISTS ai_field_evidence (
  id BIGSERIAL PRIMARY KEY,
  summary_id BIGINT NOT NULL REFERENCES ai_summary(id) ON DELETE CASCADE,
  bidder_id BIGINT REFERENCES ai_bidder(id) ON DELETE CASCADE,
  scope VARCHAR(8) NOT NULL,
  field_key VARCHAR(48) NOT NULL, field_label TEXT,
  value_text TEXT, value_num NUMERIC(18,2), value_date TEXT,
  confidence NUMERIC(4,3), level VARCHAR(6),
  source_node_id TEXT, source_file TEXT, source_page TEXT,
  source_cell TEXT, source_quote TEXT,
  candidates JSONB DEFAULT '[]', conflict SMALLINT DEFAULT 0,
  manual SMALLINT DEFAULT 0, note TEXT,
  created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_ev_sum ON ai_field_evidence(summary_id, scope, field_key);
CREATE INDEX IF NOT EXISTS idx_ai_ev_bidder ON ai_field_evidence(bidder_id, field_key);

CREATE TABLE IF NOT EXISTS ai_file (
  id BIGSERIAL PRIMARY KEY,
  task_id BIGINT NOT NULL REFERENCES ai_summary_task(id) ON DELETE CASCADE,
  node_id TEXT NOT NULL, rel_path TEXT, name TEXT, ext TEXT,
  size BIGINT, sha256 CHAR(64),
  kind VARCHAR(20), bidder_guess TEXT, page_count INT, is_scanned SMALLINT DEFAULT 0,
  status VARCHAR(12) DEFAULT 'NEW', error_msg TEXT,
  normalized_md TEXT,
  created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_file_task ON ai_file(task_id);

CREATE TABLE IF NOT EXISTS ai_task_event (
  id BIGSERIAL PRIMARY KEY,
  task_id BIGINT NOT NULL REFERENCES ai_summary_task(id) ON DELETE CASCADE,
  seq INT NOT NULL,
  stage VARCHAR(24), progress SMALLINT,
  payload JSONB NOT NULL DEFAULT '{}',
  created_at DOUBLE PRECISION NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_event_task_seq ON ai_task_event(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_ai_event_task ON ai_task_event(task_id, seq);

CREATE TABLE IF NOT EXISTS ai_change_log (
  id BIGSERIAL PRIMARY KEY,
  summary_id BIGINT NOT NULL, entity TEXT, entity_id BIGINT,
  field_key TEXT, old_value TEXT, new_value TEXT,
  actor TEXT, reason TEXT, created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_change_sum ON ai_change_log(summary_id, id);

CREATE TABLE IF NOT EXISTS ai_field_template (
  id BIGSERIAL PRIMARY KEY,
  scope VARCHAR(8) NOT NULL, fields JSONB NOT NULL DEFAULT '[]',
  is_default SMALLINT DEFAULT 0, updated_by TEXT,
  updated_at DOUBLE PRECISION NOT NULL,
  UNIQUE(scope)
);

CREATE TABLE IF NOT EXISTS ai_prompt_version (
  id BIGSERIAL PRIMARY KEY,
  scene VARCHAR(24) NOT NULL,
  version VARCHAR(32) NOT NULL,
  system_prompt TEXT NOT NULL,
  user_tmpl TEXT,
  status VARCHAR(12) NOT NULL DEFAULT 'draft',
  metrics JSONB DEFAULT '{}', note TEXT,
  created_by TEXT, created_at DOUBLE PRECISION NOT NULL,
  activated_at DOUBLE PRECISION,
  UNIQUE(scene, version)
);
CREATE INDEX IF NOT EXISTS idx_ai_prompt_active ON ai_prompt_version(scene, status);

CREATE TABLE IF NOT EXISTS ai_prompt_fewshot (
  id BIGSERIAL PRIMARY KEY,
  scene VARCHAR(24) NOT NULL,
  format_sig CHAR(16) NOT NULL,
  file_kinds JSONB DEFAULT '[]',
  input_excerpt TEXT NOT NULL,
  expected_json JSONB NOT NULL,
  source_task_id BIGINT, source_evidence_id BIGINT,
  hits INT DEFAULT 0, enabled SMALLINT DEFAULT 1,
  is_regression SMALLINT DEFAULT 0,
  created_by TEXT, created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_fewshot_sig ON ai_prompt_fewshot(scene, format_sig, enabled);

CREATE TABLE IF NOT EXISTS ai_field_alias (
  id BIGSERIAL PRIMARY KEY,
  scope VARCHAR(8) NOT NULL, field_key VARCHAR(48) NOT NULL,
  alias VARCHAR(64) NOT NULL, source VARCHAR(8) DEFAULT 'seed',
  hit_count INT DEFAULT 0, enabled SMALLINT DEFAULT 1,
  created_at DOUBLE PRECISION NOT NULL,
  UNIQUE(scope, field_key, alias)
);
CREATE INDEX IF NOT EXISTS idx_ai_alias_key ON ai_field_alias(scope, field_key, enabled);

-- ---------------------------------------------------------------- 默认字段字典
-- 与 ai_summary_api.py 的 DEFAULT_TEMPLATES 保持一致；金额单位一律「元」。
-- ON CONFLICT DO NOTHING：管理员在页面改过的模板不会被启动覆盖。
INSERT INTO ai_field_template(scope, fields, is_default, updated_by, updated_at)
VALUES ('PROJECT',
  '[{"key":"project_name","label":"项目名称","type":"text","required":true},{"key":"project_code","label":"项目编号","type":"text","required":true},{"key":"tenderer","label":"招标人（采购人）","type":"text"},{"key":"agency","label":"招标代理机构","type":"text"},{"key":"open_time","label":"开标时间","type":"text"},{"key":"open_place","label":"开标地点","type":"text"},{"key":"budget","label":"预算/最高限价（元）","type":"money","ledgerField":"budget"},{"key":"eval_method","label":"评标办法","type":"text"}]'::jsonb,
  1, 'system', extract(epoch from now()))
ON CONFLICT(scope) DO NOTHING;

INSERT INTO ai_field_template(scope, fields, is_default, updated_by, updated_at)
VALUES ('BIDDER',
  '[{"key":"bidder_name","label":"投标人名称","type":"text","required":true},{"key":"bid_price","label":"投标报价（元）","type":"money","required":true,"ledgerField":"ourPrice"},{"key":"price_in_words","label":"报价大写","type":"text"},{"key":"duration","label":"工期/服务期","type":"text"},{"key":"quality","label":"质量标准","type":"text"},{"key":"deposit","label":"投标保证金（元）","type":"money","ledgerField":"bondAmount"},{"key":"legal_person","label":"法定代表人","type":"text"},{"key":"authorized_rep","label":"授权代表/联系人","type":"text"},{"key":"contact","label":"联系方式","type":"text"},{"key":"qualification","label":"资格响应","type":"text"},{"key":"reject_reason","label":"废标/否决项","type":"text"},{"key":"remark","label":"备注","type":"text"}]'::jsonb,
  1, 'system', extract(epoch from now()))
ON CONFLICT(scope) DO NOTHING;

-- ---------------------------------------------------------------- 字段体系 v2（自定义列 / 排名主视图）
-- 设计约束（docs/开标价格汇总字段体系_优化方案.md v2）：
--   · 布局是视图层配置，不是事实；raw_result 仍是唯一事实源。
--   · 用户/项目偏好只存 diff，绝不整份拷 spec，否则系统升版后新列被旧副本吃掉。
--   · 全部幂等；禁止问号占位符与裸百分号字符（沿用本文件顶部约定）。
CREATE TABLE IF NOT EXISTS ai_field_contract (
  id            BIGSERIAL PRIMARY KEY,
  canonical_key VARCHAR(64) NOT NULL,
  label         TEXT NOT NULL,
  dtype         VARCHAR(16) NOT NULL,
  unit          VARCHAR(16) DEFAULT '',
  money_unit    VARCHAR(8) DEFAULT '元',
  value_scope   VARCHAR(16) NOT NULL DEFAULT 'BIDDER',
  perm_group    VARCHAR(32) DEFAULT '',
  is_anchor     SMALLINT DEFAULT 0,
  applies_to    JSONB DEFAULT '[]',
  source_paths  JSONB DEFAULT '{}',
  caliber_key   VARCHAR(64) DEFAULT '',
  aliases       JSONB DEFAULT '[]',
  formatter     VARCHAR(32) DEFAULT '',
  required      SMALLINT DEFAULT 0,
  sort_hint     INT DEFAULT 0,
  note          TEXT,
  version       VARCHAR(16) DEFAULT 'v1',
  enabled       SMALLINT DEFAULT 1,
  created_at    DOUBLE PRECISION NOT NULL,
  UNIQUE(canonical_key, version)
);
CREATE INDEX IF NOT EXISTS idx_ai_contract_scope ON ai_field_contract(value_scope, enabled);
CREATE INDEX IF NOT EXISTS idx_ai_contract_anchor ON ai_field_contract(is_anchor, sort_hint);

CREATE TABLE IF NOT EXISTS ai_view_layout (
  id           BIGSERIAL PRIMARY KEY,
  layout_id    VARCHAR(64) NOT NULL,
  version      VARCHAR(16) NOT NULL DEFAULT 'v1',
  mode         VARCHAR(24) NOT NULL DEFAULT '',
  variant      VARCHAR(24) NOT NULL DEFAULT '',
  format_sig   CHAR(16) NOT NULL DEFAULT '',
  is_baseline  SMALLINT DEFAULT 0,
  title        TEXT DEFAULT '',
  spec         JSONB NOT NULL DEFAULT '{}',
  status       VARCHAR(12) NOT NULL DEFAULT 'draft',
  owner_scope  VARCHAR(16) NOT NULL DEFAULT 'system',
  metrics      JSONB DEFAULT '{}',
  note         TEXT,
  created_by   TEXT, created_at DOUBLE PRECISION NOT NULL,
  activated_at DOUBLE PRECISION,
  UNIQUE(layout_id, version, owner_scope)
);
CREATE INDEX IF NOT EXISTS idx_ai_layout_lookup ON ai_view_layout(mode, variant, format_sig, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_layout_active
  ON ai_view_layout(mode, variant, format_sig, owner_scope) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS ai_view_pref (
  id             BIGSERIAL PRIMARY KEY,
  scope          VARCHAR(12) NOT NULL,
  scope_id       TEXT NOT NULL DEFAULT '',
  layout_id      VARCHAR(64) NOT NULL DEFAULT '',
  base_version   VARCHAR(16) NOT NULL DEFAULT '',
  overrides      JSONB NOT NULL DEFAULT '{}',
  custom_columns JSONB NOT NULL DEFAULT '[]',
  updated_by     TEXT, updated_at DOUBLE PRECISION NOT NULL,
  UNIQUE(scope, scope_id, layout_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_pref_scope ON ai_view_pref(scope, scope_id);

-- 字段契约 seed：6 个锚点列 + 常见价格口径列（幂等，不覆盖管理员修改）
INSERT INTO ai_field_contract(canonical_key,label,dtype,unit,value_scope,perm_group,is_anchor,applies_to,aliases,formatter,sort_hint,note,created_at)
SELECT v.*, extract(epoch from now()) FROM (VALUES
  ('rank','排名','int','','BIDDER','result',1,'[]'::jsonb,'["名次"]'::jsonb,'int',10,'跨模式锚点，locked 不可隐藏'),
  ('bidder_name','投标单位','text','','BIDDER','',1,'[]'::jsonb,'["投标人名称","投标人"]'::jsonb,'text',20,'跨模式锚点，locked 不可隐藏'),
  ('quote_primary','报价 / 下浮率 / 加权报价','text','','BIDDER','ourPrice',1,'[]'::jsonb,'["评标价","评审价"]'::jsonb,'text',30,'跨模式锚点，显示名随口径切换'),
  ('deviation','偏差','pct','','BIDDER','result',1,'[]'::jsonb,'["偏差率","综合偏差率"]'::jsonb,'signed',40,'统一以百分点展示'),
  ('total_score','价格分','score','分','BIDDER','result',1,'[]'::jsonb,'["投标报价得分","价格得分"]'::jsonb,'score',50,'口径由 caliber_key 区分，禁止跨口径比较'),
  ('status','状态 / 标记','text','','BIDDER','',1,'[]'::jsonb,'["报价状态","备注"]'::jsonb,'text',60,'跨模式锚点'),
  ('rate','下浮率 / 折扣率','pct','','BIDDER','ourPrice',0,'["discount"]'::jsonb,'["优惠率","报价折扣率"]'::jsonb,'pct2',100,'原文数字，换算方向由 rate_kind 决定'),
  ('discount','折扣（评审价）','num','','BIDDER','ourPrice',0,'["discount"]'::jsonb,'["评审价"]'::jsonb,'num4',110,'下浮率口径为 1−rate/100；折扣率口径为 rate/100'),
  ('deviation_pct','偏差（百分点）','pct','','BIDDER','result',0,'[]'::jsonb,'[]'::jsonb,'signed',120,''),
  ('unit_sum','折算单价合计','money','元','BIDDER','ourPrice',0,'["discount"]'::jsonb,'[]'::jsonb,'money',130,'Σ(限价 × 折扣)，不含量'),
  ('est_total','预估结算现价','money','元','BIDDER','ourPrice',0,'["discount"]'::jsonb,'["预估结算价"]'::jsonb,'money',140,'Σ(限价 × 预估量 × 折扣)'),
  ('discount_project','结算现价 ÷ 项目最高限价','pct','','BIDDER','result',0,'["discount"]'::jsonb,'["结算价占最高限价"]'::jsonb,'pct4',150,'分子分母同为含税·全项目·含预估量'),
  ('discount_unit','折算单价合计 ÷ 单价限价合计','pct','','BIDDER','result',0,'["discount"]'::jsonb,'[]'::jsonb,'pct4',160,'分子分母同为含税·全项目·单价合计'),
  ('quoted','报价产品数','int','项','BIDDER','',0,'["shortlist.per_item","shortlist.aggregate"]'::jsonb,'[]'::jsonb,'int',200,''),
  ('missing','缺报产品数','int','项','BIDDER','',0,'["shortlist.per_item","shortlist.aggregate"]'::jsonb,'[]'::jsonb,'int',210,''),
  ('over_cap_n','超限价产品数','int','项','BIDDER','',0,'["shortlist.per_item","shortlist.aggregate"]'::jsonb,'[]'::jsonb,'int',220,''),
  ('eval_price','加权评标价','money','元','BIDDER','ourPrice',0,'["shortlist.aggregate","shortlist.per_item"]'::jsonb,'["加权报价水平"]'::jsonb,'money',230,'Σ(单价 × 权重)'),
  ('raw_sum','未加权单价合计','money','元','BIDDER','ourPrice',0,'["shortlist.aggregate","shortlist.per_item"]'::jsonb,'[]'::jsonb,'money',240,''),
  ('cat_score','分类价格得分','score','分','BIDDER','result',0,'["shortlist.aggregate","shortlist.per_item"]'::jsonb,'[]'::jsonb,'score',250,'各类满分不同，口径随 caliber_key'),
  ('penalty_low','偏离惩罚·偏低项','int','项','BIDDER','',0,'["shortlist.per_item"]'::jsonb,'[]'::jsonb,'int',260,'该项不得分'),
  ('penalty_high','偏离惩罚·偏高项','int','项','BIDDER','',0,'["shortlist.per_item"]'::jsonb,'[]'::jsonb,'int',270,'该项不得分'),
  ('cat_net_quote','分类不含税报价','money','元','BIDDER','ourPrice',0,'["opening.itemized"]'::jsonb,'[]'::jsonb,'money',300,'动态列 key 用 cat:{i}:cat_net_quote'),
  ('cat_incl_quote','分类含税报价','money','元','BIDDER','ourPrice',0,'["opening.itemized"]'::jsonb,'[]'::jsonb,'money',310,'动态列 key 用 cat:{i}:cat_incl_quote'),
  ('part_score','分项得分','score','分','BIDDER','result',0,'["opening.itemized"]'::jsonb,'[]'::jsonb,'score',320,'各分项满分不同'),
  ('formula','评分公式','text','','RULE','',0,'[]'::jsonb,'[]'::jsonb,'text',400,''),
  ('benchmark','评标基准价','num','','RULE','result',0,'[]'::jsonb,'[]'::jsonb,'num6',410,'基准价不等于最高限价'),
  ('full_score','价格分满分','score','分','RULE','',0,'[]'::jsonb,'[]'::jsonb,'score',420,''),
  -- adapter 实际使用的其余列也必须登记，否则 validate_overrides 会把整份 order/hidden 判为非法丢弃
  ('per_100','每 100 元市场价结算','money','元','BIDDER','ourPrice',0,'["discount"]'::jsonb,'[]'::jsonb,'money',170,''),
  ('in_benchmark','参与基准价','bool','','BIDDER','result',0,'["discount"]'::jsonb,'[]'::jsonb,'bool',180,'剔除者仍参与评分'),
  ('bench_ratio','评标价 ÷ 评标基准价','pct','','BIDDER','result',0,'["opening"]'::jsonb,'[]'::jsonb,'pct4',190,'对照比例，非折扣率；基准价不等于最高限价'),
  ('incl_total','含税总价（对照）','money','元','BIDDER','ourPrice',0,'["opening"]'::jsonb,'[]'::jsonb,'money',330,''),
  ('tax_rate','税率','pct','','BIDDER','',0,'["opening"]'::jsonb,'[]'::jsonb,'pct2',340,''),
  ('duration','工期 / 服务期','text','','BIDDER','',0,'[]'::jsonb,'[]'::jsonb,'text',350,''),
  ('deposit','投标保证金','money','元','BIDDER','bondAmount',0,'[]'::jsonb,'[]'::jsonb,'money',360,''),
  ('qualification','资格响应','text','','BIDDER','',0,'[]'::jsonb,'[]'::jsonb,'text',370,''),
  ('lot_name','标段','text','','BIDDER','',0,'["opening"]'::jsonb,'[]'::jsonb,'text',380,'多标段时展示')
) AS v(canonical_key,label,dtype,unit,value_scope,perm_group,is_anchor,applies_to,aliases,formatter,sort_hint,note)
WHERE NOT EXISTS (SELECT 1 FROM ai_field_contract f WHERE f.canonical_key = v.canonical_key AND f.version = 'v1');

-- 系统布局 seed：基线 + 五套口径；spec 只描述列，不含任何数据。
INSERT INTO ai_view_layout(layout_id,version,mode,variant,format_sig,is_baseline,title,spec,status,owner_scope,created_by,created_at,activated_at)
SELECT * FROM (VALUES
 ('result.ranking.v1','v1','','','',1,'开标结果 · 价格评审排名',
  '{"layout_id":"result.ranking.v1","title":"开标结果 · 价格评审排名","column_groups":[{"id":"anchor","label":"开标结果","columns":[{"key":"rank","contract":"rank","locked":true},{"key":"bidder_name","contract":"bidder_name","locked":true},{"key":"quote_primary","contract":"quote_primary"},{"key":"deviation","contract":"deviation"},{"key":"total_score","contract":"total_score"},{"key":"status","contract":"status"}]}],"sort":{"by":"rank","order":"asc"},"footnotes":["价格分由确定性代码计算，前端仅投影","折扣率仅在同维度时计算"]}'::jsonb, 'active', 'system','system',extract(epoch from now()),extract(epoch from now())),
 ('discount.v1','v1','discount','','',0,'开标结果 · 下浮率/折扣报价',
  '{"layout_id":"discount.v1","title":"开标结果 · 下浮率/折扣报价价格评审排名","column_groups":[{"id":"anchor","label":"开标结果","columns":[{"key":"rank","contract":"rank","locked":true},{"key":"bidder_name","contract":"bidder_name","locked":true},{"key":"quote_primary","contract":"quote_primary"},{"key":"deviation","contract":"deviation"},{"key":"total_score","contract":"total_score"},{"key":"status","contract":"status"}]},{"id":"discount","label":"折扣率（同维度）","columns":[{"key":"discount_project","contract":"discount_project"},{"key":"discount_unit","contract":"discount_unit"}]}],"rules_panel":["报价口径","价格分公式","评标基准价","最高限价（Σ限价×预估量）","规则原文"],"sort":{"by":"rank","order":"asc"}}'::jsonb, 'active', 'system','system',extract(epoch from now()),extract(epoch from now())),
 ('shortlist.aggregate.v1','v1','shortlist','aggregate','',0,'开标结果 · 分类加权评标价',
  '{"layout_id":"shortlist.aggregate.v1","title":"开标结果 · 分类加权评标价价格评审排名","column_groups":[{"id":"anchor","label":"开标结果","columns":[{"key":"rank","contract":"rank","locked":true},{"key":"bidder_name","contract":"bidder_name","locked":true},{"key":"quote_primary","contract":"quote_primary"},{"key":"deviation","contract":"deviation"},{"key":"total_score","contract":"total_score"},{"key":"status","contract":"status"}]},{"id":"dynamic","label":"分类报价与得分","repeat":{"axis":"category","from":"$.shortlist.categories[*]","key_template":"cat:{i}:{metric}","columns":["eval_price","cap_ratio","discount","raw_sum","score"]}}],"sort":{"by":"rank","order":"asc"}}'::jsonb, 'active', 'system','system',extract(epoch from now()),extract(epoch from now())),
 ('shortlist.per_item.v1','v1','shortlist','per_item','',0,'开标结果 · 逐项加权最低价',
  '{"layout_id":"shortlist.per_item.v1","title":"开标结果 · 逐项加权最低价价格评审排名","column_groups":[{"id":"anchor","label":"开标结果","columns":[{"key":"rank","contract":"rank","locked":true},{"key":"bidder_name","contract":"bidder_name","locked":true},{"key":"quote_primary","contract":"quote_primary"},{"key":"deviation","contract":"deviation"},{"key":"total_score","contract":"total_score"},{"key":"status","contract":"status"}]},{"id":"dynamic","label":"分类报价与得分","repeat":{"axis":"category","from":"$.shortlist.categories[*]","key_template":"cat:{i}:{metric}","columns":["eval_price","cap_ratio","discount","raw_sum","score"]}},{"id":"penalty","label":"异常与惩罚","columns":[{"key":"penalty_low","contract":"penalty_low"},{"key":"penalty_high","contract":"penalty_high"}]}],"sort":{"by":"rank","order":"asc"}}'::jsonb, 'active', 'system','system',extract(epoch from now()),extract(epoch from now())),
 ('opening.table.v1','v1','opening','','',0,'开标结果 · 开标记录表',
  '{"layout_id":"opening.table.v1","title":"开标结果 · 开标记录表价格评审排名","column_groups":[{"id":"anchor","label":"开标结果","columns":[{"key":"rank","contract":"rank","locked":true},{"key":"bidder_name","contract":"bidder_name","locked":true},{"key":"quote_primary","contract":"quote_primary"},{"key":"deviation","contract":"deviation"},{"key":"total_score","contract":"total_score"},{"key":"status","contract":"status"}]},{"id":"bench","label":"基准价对照","columns":[{"key":"bench_ratio","contract":"benchmark"}]}],"sort":{"by":"rank","order":"asc"},"footnotes":["基准价不等于最高限价，对照比例不是折扣率"]}'::jsonb, 'active', 'system','system',extract(epoch from now()),extract(epoch from now())),
 ('opening.itemized.v1','v1','opening','itemized','',0,'开标结果 · 分类分项计分',
  '{"layout_id":"opening.itemized.v1","title":"开标结果 · 分类分项计分价格评审排名","column_groups":[{"id":"anchor","label":"开标结果","columns":[{"key":"rank","contract":"rank","locked":true},{"key":"bidder_name","contract":"bidder_name","locked":true},{"key":"quote_primary","contract":"quote_primary"},{"key":"deviation","contract":"deviation"},{"key":"total_score","contract":"total_score"},{"key":"status","contract":"status"}]},{"id":"dynamic","label":"分类报价与得分","repeat":{"axis":"part","from":"$.lots[*].parts[*]","key_template":"part:{i}:{metric}","columns":["net","incl","score"]}}],"sort":{"by":"rank","order":"asc"}}'::jsonb, 'active', 'system','system',extract(epoch from now()),extract(epoch from now()))
) AS v(layout_id,version,mode,variant,format_sig,is_baseline,title,spec,status,owner_scope,created_by,created_at,activated_at)
WHERE NOT EXISTS (SELECT 1 FROM ai_view_layout l WHERE l.layout_id = v.layout_id AND l.version = v.version AND l.owner_scope = 'system');
