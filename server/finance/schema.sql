-- 财务模块建表脚本（幂等）
-- 由 main.py 启动时调用 finance.init_tables(db) 执行，或手动 psql -f
-- 表前缀 fin_ 避免与现有模块撞名；大字段拆详情表；列表查询建复合索引。

CREATE TABLE IF NOT EXISTS fin_item (
  id          BIGSERIAL PRIMARY KEY,
  title       TEXT NOT NULL DEFAULT '',
  amount      DOUBLE PRECISION NOT NULL DEFAULT 0,
  status      TEXT DEFAULT 'draft',
  created_by  TEXT DEFAULT '',
  created_at  DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fin_item_created ON fin_item(created_at DESC, id DESC);

-- 大字段拆表：列表查询绝不碰 content
CREATE TABLE IF NOT EXISTS fin_item_detail (
  item_id   BIGINT PRIMARY KEY REFERENCES fin_item(id),
  content   TEXT DEFAULT '',
  attachments JSON
);

-- 建表入口：从 api.py 或 main.py 调用，保持幂等
-- def init_tables(dbobj):
--     dbobj.executescript(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
