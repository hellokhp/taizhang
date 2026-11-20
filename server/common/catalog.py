# -*- coding: utf-8 -*-
"""权限模型目录（从 main.py 原样抽出，纯数据，行为零变化）。

字段目录与前端 lib/fields.js 的 F 保持一致，用于权限矩阵与字段屏蔽。
新增功能模块的权限项在 PERM_CATALOG 追加即可，前端自动出现勾选项。
"""
# 台账 48 个字段
FIELD_CATALOG = [
    ('projectName', '项目名称', '项目基础'), ('bidNo', '招标编号', '项目基础'),
    ('pkg', '标段/包号', '项目基础'), ('agency', '代理机构', '项目基础'),
    ('owner', '采购单位（业主）', '项目基础'), ('region', '项目所在地', '项目基础'),
    ('method', '招标方式', '项目基础'), ('bizType', '业务类型', '项目基础'),
    ('projType', '类型', '项目基础'), ('industry', '行业', '项目基础'),
    ('province', '省份', '项目基础'),
    ('deadline', '投标截止时间', '时间与地点'), ('openTime', '开标时间', '时间与地点'),
    ('openAddr', '开标地点', '时间与地点'),
    ('budget', '预算/最高限价（元）', '金额与保证金'), ('ourPrice', '我方报价（元）', '金额与保证金'),
    ('bondAmount', '投标保证金金额（元）', '金额与保证金'), ('bondType', '保证金形式', '金额与保证金'),
    ('bondStatus', '投标保证金状态', '金额与保证金'),
    ('bondBackDate', '投标保证金退回时间', '金额与保证金'),
    ('bondIssuer', '投标保函开具行/收款单位', '金额与保证金'),
    ('bondNo', '投标保函/凭证编号', '金额与保证金'), ('bondPayDate', '投标缴纳/开具日期', '金额与保证金'),
    ('bondDue', '投标保函有效期', '金额与保证金'),
    ('perfAmount', '履约保证金金额（元）', '金额与保证金'), ('perfStatus', '履约保证金状态', '金额与保证金'),
    ('perfBackDate', '履约保证金退回时间', '金额与保证金'),
    ('perfIssuer', '履约保函开具行/收款单位', '金额与保证金'),
    ('perfNo', '履约保函/凭证编号', '金额与保证金'), ('perfPayDate', '履约缴纳/开具日期', '金额与保证金'),
    ('perfDue', '履约保函有效期', '金额与保证金'),
    ('lead', '标书负责人', '人员分工'), ('copilot', '协作人', '人员分工'),
    ('reviewer', '审核人', '人员分工'),
    ('bidStatus', '标书状态', '标书进度'), ('bidWaitAt', '未开始时间', '标书进度'),
    ('bidStartAt', '进行中时间', '标书进度'), ('bidEndAt', '已结束时间', '标书进度'),
    ('bidEffort', '标书投入时间', '标书进度'),
    ('docFee', '购买标书费（元）', '项目开销'), ('travelFee', '差旅费（元）', '项目开销'),
    ('sampleFee', '样品费（元）', '项目开销'),
    ('result', '开标结果', '开标结果'), ('winner', '中标单位', '开标结果'),
    ('winAmount', '中标金额（元）', '开标结果'), ('rank', '我方排名', '开标结果'),
    ('noticeUrl', '中标公告链接', '开标结果'), ('remark', '备注', '开标结果')
]
FIELD_KEYS = [f[0] for f in FIELD_CATALOG]

# 模块/操作权限
PERM_CATALOG = [
    ('view.dashboard', '首页看板', '页面'),
    ('view.manage', '投标管理', '页面'),
    ('view.timeline', '标源总览', '页面'),
    ('view.assign', '项目分配表', '页面'),
    ('view.alerts', '需要处理', '页面'),
    ('view.reports', '工作日报', '页面'),
    ('view.users', '用户管理', '页面'),
    ('view.roles', '角色与权限', '页面'),
    ('view.audit', '审计日志', '页面'),
    ('view.files', '文件存储', '页面'),
    ('view.aiSummary', '开标汇总', '页面'),
    ('view.bidcrawl', '标讯采集', '页面'),
    ('bidcrawl.manage', '标讯采集 · 配置/提交任务', '操作'),
    ('view.schedule', '标讯采集 · 定时任务（查看，需同时开「标讯采集」）', '页面'),
    ('schedule.create', '标讯采集 · 定时任务（新建）', '操作'),
    ('schedule.update', '标讯采集 · 定时任务（修改/立即执行）', '操作'),
    ('schedule.delete', '标讯采集 · 定时任务（删除）', '操作'),
    ('ai.generate', '开标汇总 · 生成/复核/重试', '操作'),
    ('ai.confirm', '开标汇总 · 定稿', '操作'),
    ('ai.layout', '开标汇总 · 布局管理', '操作'),
    ('bid.edit', '台账 · 新增/编辑', '操作'),
    ('bid.delete', '台账 · 删除', '操作'),
    ('bid.import', '台账 · 导入', '操作'),
    ('bid.export', '台账 · 导出/模板', '操作'),
    ('file.admin', '存储 · 全局管理（回收站/备份/日志）', '操作'),
    ('report.viewOthers', '日报 · 查看其他人员数据', '操作'),
]
PERM_KEYS = [p[0] for p in PERM_CATALOG]

# 字段权限四档（对应流程文档：隐藏 / 脱敏 / 只读 / 可写）
LEVELS = [
    ('writable', '可编辑'), ('readonly', '只读'), ('masked', '脱敏'), ('hidden', '不可见')
]
LEVEL_KEYS = [l[0] for l in LEVELS]

# 脱敏规则（仅用于 masked 档）
MASK_CATALOG = [
    ('amount', '金额（保留首位，如 1***）'), ('phone', '手机号（138****1234）'),
    ('name', '姓名/单位（保留首字，如 张**）'), ('text', '文本（保留前2字，如 某某***）'),
    ('full', '全部（***）')
]
MASK_KEYS = [m[0] for m in MASK_CATALOG]

# 行级数据范围（台账无部门字段，DEPT 暂未启用；新增部门字段后在此扩展）
SCOPES = [('ALL', '全部数据'), ('SELF', '仅本人相关（负责人/协作人/审核人）')]

_ALL_FIELDS_READONLY = {k: {'level': 'readonly', 'mask': 'full'} for k in FIELD_KEYS}

ROLE_SEEDS = [
    ('manager', '投标经理', {'view.dashboard': 1, 'view.manage': 1, 'view.timeline': 1, 'view.assign': 1, 'view.alerts': 1, 'view.reports': 1, 'view.users': 0, 'view.roles': 0,
                             'view.audit': 1, 'view.files': 1, 'view.aiSummary': 1, 'ai.generate': 1, 'ai.confirm': 1, 'ai.layout': 1,
                             'bid.edit': 1, 'bid.delete': 1, 'bid.import': 1,
                             'bid.export': 1, 'file.admin': 1, 'report.viewOthers': 1}, {}, 'ALL'),
    ('staff', '投标专员', {'view.dashboard': 1, 'view.manage': 1, 'view.timeline': 1, 'view.assign': 1, 'view.alerts': 1, 'view.reports': 1, 'view.users': 0, 'view.roles': 0,
                           'view.audit': 0, 'view.files': 1, 'view.aiSummary': 1, 'ai.generate': 1, 'ai.confirm': 1, 'ai.layout': 0,
                           'bid.edit': 1, 'bid.delete': 0, 'bid.import': 1,
                           'bid.export': 1, 'file.admin': 0, 'report.viewOthers': 0}, {}, 'ALL'),
    ('viewer', '只读查看', {'view.dashboard': 1, 'view.manage': 1, 'view.timeline': 1, 'view.assign': 1, 'view.alerts': 1, 'view.reports': 1, 'view.users': 0, 'view.roles': 0,
                            'view.audit': 0, 'view.files': 1, 'view.aiSummary': 1, 'ai.generate': 0, 'ai.confirm': 0, 'ai.layout': 0,
                            'bid.edit': 0, 'bid.delete': 0, 'bid.import': 0,
                            'bid.export': 0, 'file.admin': 0, 'report.viewOthers': 0}, _ALL_FIELDS_READONLY, 'ALL'),
]
