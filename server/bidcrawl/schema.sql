-- 标讯采集 · 数据模型（P0）
-- 与《标讯采集_平台自解析与配方复用方案》第 8、10 章一致；所有表统一 bid_ 前缀，
-- 与 ai_* 表完全隔离。DDL 幂等，后端启动与 worker 启动都可重复执行。

-- ---------------------------------------------------------------- 平台与配方
CREATE TABLE IF NOT EXISTS bid_platform (
  id            text PRIMARY KEY,               -- 平台短 id，例如 cfcpn
  name          text NOT NULL,
  host          text NOT NULL,
  entry_url     text,                           -- 新增平台时人工填写的入口 URL（解析用）
  status        text NOT NULL DEFAULT 'draft',  -- active|degraded|paused|draft（disabled 为历史同义词）|error
  active_ver    int,
  last_ok_at    timestamptz,
  last_err      text,
  created_at    timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bid_recipe_version (
  platform_id   text NOT NULL REFERENCES bid_platform(id) ON DELETE CASCADE,
  version       int  NOT NULL,
  recipe        jsonb NOT NULL,                 -- 完整 Recipe
  verify        jsonb,                          -- 自校验结果
  created_by    text,                           -- ai|human|cli
  created_at    timestamptz DEFAULT now(),
  PRIMARY KEY (platform_id, version)
);

CREATE TABLE IF NOT EXISTS bid_platform_credential (
  platform_id   text PRIMARY KEY REFERENCES bid_platform(id) ON DELETE CASCADE,
  cookie        text,
  token         text,
  updated_at    timestamptz DEFAULT now()
);

-- ---------------------------------------------------------------- 运行记录
CREATE TABLE IF NOT EXISTS bid_crawl_run (
  id            bigserial PRIMARY KEY,
  platform_id   text NOT NULL,
  recipe_ver    int,
  config_ver    bigint,
  keyword       text,
  mode          text,                           -- full|incremental|repair
  status        text,                           -- running|ok|partial|failed
  requests      int DEFAULT 0,
  page_size     int,
  avg_ms        int,
  fetched       int DEFAULT 0,
  inserted      int DEFAULT 0,
  updated       int DEFAULT 0,
  dropped       int DEFAULT 0,
  error         text,
  started_at    timestamptz DEFAULT now(),
  finished_at   timestamptz
);
CREATE INDEX IF NOT EXISTS idx_bid_run_platform
  ON bid_crawl_run(platform_id, started_at DESC);

-- ---------------------------------------------------------------- 标讯条目
CREATE TABLE IF NOT EXISTS bid_item (
  id             bigserial PRIMARY KEY,
  platform_id    text NOT NULL,
  external_id    text,
  url            text NOT NULL,
  url_hash       char(40) NOT NULL,
  title          text NOT NULL,
  buyer          text,
  budget_yuan    numeric,
  fee_note       text,
  scope          text,
  doc_get_start  timestamptz,
  doc_get_end    timestamptz,
  open_at        timestamptz,
  publish_at     timestamptz,
  region         text,
  source_name    text,
  status         text,                          -- open|soon|expired|unknown
  raw            jsonb,
  first_seen_at  timestamptz DEFAULT now(),
  last_seen_at   timestamptz DEFAULT now(),
  UNIQUE (platform_id, url_hash)
);
CREATE INDEX IF NOT EXISTS idx_bid_item_status ON bid_item(status, open_at);
CREATE INDEX IF NOT EXISTS idx_bid_item_deadline ON bid_item(doc_get_end);
CREATE INDEX IF NOT EXISTS idx_bid_item_first_seen ON bid_item(first_seen_at DESC);

-- P1 详情 AI 解析结果（幂等加列，旧库无需重建）
ALTER TABLE bid_item ADD COLUMN IF NOT EXISTS detail jsonb;
COMMENT ON COLUMN bid_item.detail IS
  '详情解析扩展字段：招标编号/代理机构/联系方式/资质要求/工期/中标人数量 + ai 审计信息';

-- 详情正文 AI 抽取结果缓存：同一份正文只调用一次模型
CREATE TABLE IF NOT EXISTS bid_detail_ai (
  cache_key     char(40) PRIMARY KEY,       -- sha1(platform_id + prompt_ver + 清洗后正文)
  platform_id   text,
  prompt_ver    text,
  model         text,
  result        jsonb,
  source        text,                       -- llm | cache | fallback
  created_at    timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bid_detail_ai_platform
  ON bid_detail_ai(platform_id, created_at DESC);

CREATE TABLE IF NOT EXISTS bid_item_change (
  id          bigserial PRIMARY KEY,
  item_id     bigint NOT NULL REFERENCES bid_item(id) ON DELETE CASCADE,
  change_type text NOT NULL,                    -- new|field_changed|expired|reopened
  diff        jsonb,
  created_at  timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bid_change_item ON bid_item_change(item_id, created_at DESC);

CREATE TABLE IF NOT EXISTS bid_fetch_cache (
  url_hash     char(40) PRIMARY KEY,
  url          text NOT NULL,
  body         text,
  content_type text,
  fetched_at   timestamptz DEFAULT now()
);

-- ---------------------------------------------------------------- 采集配置（页面 CRUD 的对象）
CREATE TABLE IF NOT EXISTS bid_keyword (
  id           bigserial PRIMARY KEY,
  word         text NOT NULL,
  scope        text NOT NULL DEFAULT 'global',  -- global | <platform_id>
  enabled      boolean NOT NULL DEFAULT true,
  is_sample    boolean NOT NULL DEFAULT false,
  sort_no      int DEFAULT 0,
  note         text,
  hits_last    int DEFAULT 0,
  created_at   timestamptz DEFAULT now(),
  updated_at   timestamptz DEFAULT now(),
  deleted_at   timestamptz,
  UNIQUE (word, scope)
);

CREATE TABLE IF NOT EXISTS bid_buyer_pattern (
  id           bigserial PRIMARY KEY,
  pattern      text NOT NULL,
  match_type   text NOT NULL DEFAULT 'contains', -- contains|regex
  scope        text NOT NULL DEFAULT 'global',
  enabled      boolean NOT NULL DEFAULT true,
  sort_no      int DEFAULT 0,
  note         text,
  created_at   timestamptz DEFAULT now(),
  updated_at   timestamptz DEFAULT now(),
  deleted_at   timestamptz
);

CREATE TABLE IF NOT EXISTS bid_exclude_rule (
  id           bigserial PRIMARY KEY,
  name         text NOT NULL,
  rule_type    text NOT NULL,                   -- title_regex|notice_type|buyer_regex|age_days|url_regex
  value        jsonb NOT NULL,                  -- {"regex":"..."} / {"days":35} / {"types":[...]}
  scope        text NOT NULL DEFAULT 'global',
  enabled      boolean NOT NULL DEFAULT true,
  sort_no      int DEFAULT 0,
  note         text,
  created_at   timestamptz DEFAULT now(),
  updated_at   timestamptz DEFAULT now(),
  deleted_at   timestamptz
);

CREATE TABLE IF NOT EXISTS bid_config_audit (
  id          bigserial PRIMARY KEY,
  entity      text NOT NULL,                    -- keyword|buyer|rule|recipe|platform
  entity_id   bigint,
  action      text NOT NULL,                    -- create|update|delete|enable|disable
  before      jsonb,
  after       jsonb,
  operator    text,
  created_at  timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bid_audit_time ON bid_config_audit(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bid_kw_scope ON bid_keyword(scope, enabled);
CREATE INDEX IF NOT EXISTS idx_bid_buyer_scope ON bid_buyer_pattern(scope, enabled);
CREATE INDEX IF NOT EXISTS idx_bid_rule_scope ON bid_exclude_rule(scope, enabled);

-- ---------------------------------------------------------------- 字段字典与投影
CREATE TABLE IF NOT EXISTS bid_field_def (
  key        text PRIMARY KEY,                  -- title / buyer / budget ...
  label      text NOT NULL,
  data_type  text NOT NULL,                     -- text|amount|datetime|url|enum
  required   boolean NOT NULL DEFAULT false,
  aliases    jsonb NOT NULL DEFAULT '[]',
  sort_no    int DEFAULT 0,
  enabled    boolean NOT NULL DEFAULT true
);

-- ---------------------------------------------------------------- 字段正则配置
-- 普通员工维护：一个字段一行「匹配词」，系统拼成完整正则（{alts} 占位），
-- 优先级 配置中心 > 配方 detail.ai.fallback_regex > 内置 DEFAULT_FALLBACK_REGEX。
-- 不用 bid_field_def.aliases：那份是给开标汇总的模型做定位用的，语义不同。
CREATE TABLE IF NOT EXISTS bid_regex_field (
  field_key   text PRIMARY KEY,                 -- budget / open_at / bid_no ...
  label       text NOT NULL DEFAULT '',
  pattern_tpl text NOT NULL,                    -- 含 {alts} 占位符的完整正则
  value_hint  text NOT NULL DEFAULT '',         -- 给普通员工看的「匹配到什么」
  enabled     boolean NOT NULL DEFAULT true,
  sort_no     int DEFAULT 0,
  updated_at  timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bid_regex_alias (
  id         bigserial PRIMARY KEY,
  field_key  text NOT NULL,
  alias      text NOT NULL,                     -- 原文里的写法：预算、项目规模…
  scope      text NOT NULL DEFAULT 'global',    -- global | <platform_id>
  enabled    boolean NOT NULL DEFAULT true,
  sort_no    int DEFAULT 0,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),
  deleted_at timestamptz,
  UNIQUE (field_key, alias, scope)
);

CREATE INDEX IF NOT EXISTS idx_bid_regex_alias
  ON bid_regex_alias (field_key, scope) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS bid_recipe_field (
  platform_id   text NOT NULL,
  version       int  NOT NULL,
  field_key     text NOT NULL,
  source        text NOT NULL,                  -- list|detail
  selector_kind text NOT NULL,                  -- path|css|xpath|regex|template|const
  selector      text,
  type_hint     text,
  has_fallback  boolean DEFAULT false,
  PRIMARY KEY (platform_id, version, field_key, source)
);

-- ---------------------------------------------------------------- 异步任务
CREATE TABLE IF NOT EXISTS bid_crawl_job (
  id               bigserial PRIMARY KEY,
  job_type         text NOT NULL,               -- run|rules_preview|analyze
  platform_id      text,
  dedupe_key       text NOT NULL,
  params           jsonb NOT NULL DEFAULT '{}', -- 任务参数（worker 据此执行）
  client_token     text,
  status           text NOT NULL DEFAULT 'queued',
  stage            text,
  progress         int  NOT NULL DEFAULT 0,
  done_steps       int DEFAULT 0,
  total_steps      int DEFAULT 0,
  message          text,
  result           jsonb,
  error            text,
  attempts         int DEFAULT 0,
  claimed_by       text,
  cancel_requested boolean DEFAULT false,
  created_by       text,
  created_at       timestamptz DEFAULT now(),
  started_at       timestamptz,
  heartbeat_at     timestamptz,
  finished_at      timestamptz
);

-- 同一平台同一类任务，全局只允许一个在排队或运行（防堆叠的关键）
CREATE UNIQUE INDEX IF NOT EXISTS uq_job_active
  ON bid_crawl_job (job_type, coalesce(platform_id, '-'))
  WHERE status IN ('queued', 'running');

-- 同一个提交令牌只落一条，网络重发/连点都合并
CREATE UNIQUE INDEX IF NOT EXISTS uq_job_token
  ON bid_crawl_job (client_token) WHERE client_token IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_bid_job_status ON bid_crawl_job(status, id);

-- ---------------------------------------------------------------- 定时任务
-- 每个任务每天在 run_at（服务器本地时间 HH:MM）提交一次采集 job。
-- platforms / keywords 存 id 数组：空数组 = 全部启用平台 / 各平台全部关键词。
CREATE TABLE IF NOT EXISTS bid_crawl_schedule (
  id              bigserial PRIMARY KEY,
  name            text NOT NULL,
  run_at          text NOT NULL,                      -- 'HH:MM'
  platforms       jsonb NOT NULL DEFAULT '[]'::jsonb,
  keywords        jsonb NOT NULL DEFAULT '[]'::jsonb,
  enabled         boolean NOT NULL DEFAULT true,
  created_by      text DEFAULT '',
  updated_by      text DEFAULT '',
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  last_run_at     timestamptz,
  last_run_date   date,                               -- 同日只触发一次（多 worker 原子抢占）
  last_job_id     bigint,
  last_status     text DEFAULT '',
  last_error      text DEFAULT '',
  last_attempt_at timestamptz                         -- 提交失败后的重试节流
);

CREATE INDEX IF NOT EXISTS idx_bid_schedule_due
  ON bid_crawl_schedule (enabled, run_at);

CREATE TABLE IF NOT EXISTS bid_job_event (
  id          bigserial PRIMARY KEY,
  job_id      bigint NOT NULL REFERENCES bid_crawl_job(id) ON DELETE CASCADE,
  seq         int NOT NULL,
  stage       text,
  progress    int,
  message     text,
  created_at  timestamptz DEFAULT now(),
  UNIQUE (job_id, seq)
);

-- ---------------------------------------------------------------- 种子数据
INSERT INTO bid_field_def(key, label, data_type, required, aliases, sort_no) VALUES
  ('title',         '项目名称',   'text',     true,  '["标题","公告标题","标的名称"]', 10),
  ('url',           '原文链接',   'url',      true,  '["链接","详情地址"]', 20),
  ('buyer',         '采购人',     'text',     false, '["招标人","采购单位","招标单位"]', 30),
  ('budget',        '预算/规模',  'amount',   false, '["预算金额","项目规模","采购预算"]', 40),
  ('fee_note',      '报名费',     'text',     false, '["标书费","文件费"]', 50),
  ('scope',         '采购范围',   'text',     false, '["标的","采购内容"]', 60),
  ('doc_get_start', '获取开始',   'datetime', false, '["文件获取开始时间"]', 70),
  ('doc_get_end',   '获取截止',   'datetime', false, '["报名截止","获取截止时间"]', 80),
  ('open_at',       '开标时间',   'datetime', false, '["递交截止","投标截止"]', 90),
  ('publish_at',    '发布时间',   'datetime', false, '["发布日期","公告时间"]', 100),
  ('region',        '地区',       'text',     false, '["所属地区","省份"]', 110),
  ('source_name',   '来源',       'text',     false, '["平台名称"]', 120),
  ('status',        '状态',       'enum',     false, '["公告状态"]', 130),
  ('bid_no',        '招标编号',   'text',     false, '["项目编号","招标编号"]', 140),
  ('agency',        '代理机构',   'text',     false, '["招标代理机构"]', 150),
  ('contact',       '联系方式',   'text',     false, '["联系人","联系电话"]', 160),
  ('qualification', '资质要求',   'text',     false, '["资格要求","业绩门槛"]', 170),
  ('project_period','工期/服务期','text',     false, '["工期","服务期"]', 180),
  ('bidder_limit',  '中标人数量', 'text',     false, '["中标人数量"]', 190),
  ('project_name',  '项目全称',   'text',     false, '["项目名称"]', 200),
  ('funding',       '资金来源',   'text',     false, '["资金来源"]', 210),
  ('bid_place',     '开标地点',   'text',     false, '["递交地点"]', 220),
  ('notice_type',   '采购方式',   'text',     false, '["招标方式"]', 230)
ON CONFLICT (key) DO NOTHING;

-- 关键词种子（21 个，各自独立检索，不组合）
INSERT INTO bid_keyword(word, scope, enabled, is_sample, sort_no)
SELECT v.word, 'global', true, v.word = '门头', v.sort_no
FROM (VALUES
  ('门头',10),('门楣',20),('门匾',30),('灯箱',40),('VI',50),('CI',60),
  ('物料宣传',70),('宣传物料',80),('印刷物料',90),('印刷服务',100),
  ('业务宣传',110),('营销用品',120),('标识标牌',130),('视觉形象',140),
  ('零售终端',150),('吸烟环境',160),('企宣',170),('终端建设',180),
  ('LOGO',190),('6S',200),('5S',210),('标识',220)
) AS v(word, sort_no)
WHERE NOT EXISTS (
  SELECT 1 FROM bid_keyword k WHERE k.word = v.word AND k.scope = 'global'
);

-- 采购人白名单种子
INSERT INTO bid_buyer_pattern(pattern, match_type, scope, enabled, sort_no)
SELECT v.pattern, 'contains', 'global', true, v.sort_no
FROM (VALUES
  ('烟草',10),('中烟',20),('卷烟厂',30),('烟叶',40),('银行',50),
  ('农商行',60),('农信社',70),('村镇银行',80),('邮储',90),('邮政',100),
  ('中邮',110),('石油',120),('石化',130)
) AS v(pattern, sort_no)
WHERE NOT EXISTS (
  SELECT 1 FROM bid_buyer_pattern b WHERE b.pattern = v.pattern AND b.scope = 'global'
);

-- 剔除规则种子（结果类 / 公告类型 / 时间窗 / 媒介投放）
INSERT INTO bid_exclude_rule(name, rule_type, value, scope, enabled, sort_no)
SELECT v.name, v.rule_type, v.value::jsonb, 'global', true, v.sort_no
FROM (VALUES
  ('结果类公告', 'title_regex', '{"regex":"中标(结果|公告|候选人)|结果公告|成交公告|中选结果"}', 10),
  ('变更/流标类', 'title_regex', '{"regex":"变更公告|更正公告|澄清|流标|废标|终止公告|失败公告"}', 20),
  ('报名已截止', 'title_regex', '{"regex":"报名已截止|已截止报名"}', 30),
  ('发布超35天', 'age_days', '{"days":35}', 40),
  ('纯媒介投放', 'title_regex', '{"regex":"地铁|高铁|站台|报纸|大屏|广告位|代运营|媒体投放"}', 50)
) AS v(name, rule_type, value, sort_no)
WHERE NOT EXISTS (
  SELECT 1 FROM bid_exclude_rule r WHERE r.name = v.name AND r.scope = 'global'
);

-- 金采网单独口径：单一来源 / 采前公示 / 直接采购（已定供应商）不纳入。
-- 平台级规则，不污染其他平台；同名规则优先于全局（config._dedupe 按 name 覆盖）。
INSERT INTO bid_exclude_rule(name, rule_type, value, scope, enabled, sort_no)
SELECT '单一来源/采前公示', 'title_regex',
       '{"regex":"单一来源|采前公示|直接采购|拟单一来源"}'::jsonb,
       'cfcpn', true, 60
WHERE NOT EXISTS (
  SELECT 1 FROM bid_exclude_rule r
  WHERE r.name = '单一来源/采前公示' AND r.scope = 'cfcpn'
);

-- ---------------------------------------------------------------- 字段正则种子
-- 模板与别名等价于 detail_ai.DEFAULT_FALLBACK_REGEX（上线不改变现有抽取结果），
-- 之后所有调整都在「采集配置 → 字段正则」里做，不再改配方。
INSERT INTO bid_regex_field(field_key, label, pattern_tpl, value_hint, sort_no)
SELECT v.field_key, v.label, v.pattern_tpl, v.value_hint, v.sort_no
FROM (VALUES
  ('budget', '预算/最高限价', '(?:{alts})[：:\s]*([^\n，。；]{1,40})',
   '匹配词之后到逗号/句号为止，最多 40 字', 10),
  ('fee_note', '报名费/标书费', '(?:{alts})[：:]\s*([^\n]{1,60})',
   '匹配词之后整行，最多 60 字', 20),
  ('doc_get_start', '文件获取开始', '(?:{alts})[：:]\s*从\s*([^\n到]{4,40}?)\s*到',
   '取「获取时间：从 X 到 Y」里的 X', 30),
  ('doc_get_end', '文件获取截止', '(?:{alts})[：:]\s*从\s*[^\n到]{4,40}?\s*到\s*([^\n]{4,40})',
   '取「获取时间：从 X 到 Y」里的 Y', 40),
  ('open_at', '开标时间', '(?:{alts})[：:]\s*([^\n，。（]{4,45})',
   '匹配词之后到逗号/句号/左括号为止', 50),
  ('deadline', '投标截止时间', '(?:{alts})[：:]\s*([^\n，。（]{4,45})',
   '与开标时间同格式，但只在「递交/投标截止」处取值', 60),
  ('bid_no', '招标编号', '(?:{alts})[：:]\s*([A-Za-z0-9\-_（）()]{4,40})',
   '字母、数字、括号组成的编号', 70),
  ('agency', '代理机构', '([\u4e00-\u9fa5]{4,30}(?:有限公司|有限责任公司))[（(](?:{alts})',
   '公司全称 + 紧跟的括号说明（匹配词在括号里）', 80),
  ('contact', '联系人', '(?:{alts})[：:]\s*([^\n，。]{1,40})',
   '匹配词之后到逗号/句号为止', 90),
  ('project_period', '工期/服务期', '(?:{alts})[：:]\s*([^\n，。]{1,20})',
   '匹配词之后到逗号/句号为止', 100),
  ('bidder_limit', '中标人数量', '(?:{alts})[：:]\s*([^\n，。名]{1,10})',
   '匹配词之后的数量（几 名/家）', 110)
) AS v(field_key, label, pattern_tpl, value_hint, sort_no)
ON CONFLICT (field_key) DO NOTHING;

INSERT INTO bid_regex_alias(field_key, alias, scope, sort_no)
SELECT v.field_key, v.alias, 'global', v.sort_no
FROM (VALUES
  ('budget', '合同估算价', 10), ('budget', '项目预算', 20), ('budget', '采购预算', 30),
  ('budget', '预算金额', 40), ('budget', '最高限价', 50), ('budget', '预算', 60),
  ('fee_note', '招标文件费', 10), ('fee_note', '标书费', 20),
  ('fee_note', '文件费', 30), ('fee_note', '费用', 40),
  ('doc_get_start', '获取时间', 10), ('doc_get_end', '获取时间', 10),
  ('open_at', '递交截止时间', 10), ('open_at', '开标时间', 20),
  ('deadline', '投标文件递交截止时间', 10), ('deadline', '响应文件递交截止时间', 20),
  ('deadline', '递交截止时间', 30), ('deadline', '投标截止时间', 40),
  ('bid_no', '招标编号', 10), ('bid_no', '项目编号', 20), ('bid_no', '采购编号', 30),
  ('agency', '招标代理机构', 10), ('agency', '采购代理机构', 20),
  ('contact', '联系人', 10),
  ('project_period', '工期', 10), ('project_period', '服务期', 20),
  ('bidder_limit', '中标人数量', 10)
) AS v(field_key, alias, sort_no)
ON CONFLICT (field_key, alias, scope) DO NOTHING;
