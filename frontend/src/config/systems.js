/* ================================================================
   系统注册中心：三套专属视图的单一事实源
   新增业务系统 = 这里加一项 + 建对应 views/<域>/ 页面 + 后端 MODULES/DEPARTMENTS 登记。
   nav 菜单项 path 为相对系统 base 的完整路径；perm 为模块内细粒度权限（空=只需部门准入）。
   ================================================================ */

export const SYSTEMS = {
  taizhang: {
    key: 'taizhang',
    name: '投标台账',
    dept: '投标部',
    deptName: '投标部',
    tagline: '投标全流程协同台账',
    base: '/taizhang',
    home: '/taizhang/',
    theme: 'blue',
    accent: '#D8B678',
    description: '台账多人实时协同，15 秒自动同步；看板掌握开标、中标与保证金。',
    features: [
      '台账多人实时协同，15 秒自动同步',
      '首页看板掌握开标、中标与保证金',
      '按角色控制页面、字段与数据范围',
      '字段级操作留痕，越权写入自动拦截'
    ],
    nav: [
      { path: '/taizhang/',          title: '首页看板',   perm: 'view.dashboard', icon: 'grid' },
      { path: '/taizhang/alerts',    title: '需要处理',   perm: 'view.alerts',    icon: 'bell', badge: true },
      { path: '/taizhang/manage',    title: '投标管理',   perm: 'view.manage',    icon: 'table', badgeRows: true },
      { path: '/taizhang/timeline',  title: '标源总览',   perm: 'view.timeline',  icon: 'calendar' },
      { path: '/taizhang/assign',    title: '项目分配表', perm: 'view.assign',    icon: 'users' },
      { path: '/taizhang/reports',   title: '工作日报',   perm: 'view.reports',   icon: 'report' },
      { path: '/taizhang/files',     title: '文件存储',   perm: 'view.files',     icon: 'folder' },
      { path: '/taizhang/ai-summary',title: '开标汇总',   perm: 'view.aiSummary', icon: 'doc' },
      { path: '/taizhang/bidcrawl',  title: '标讯采集',   perm: 'view.bidcrawl',  icon: 'globe' },
      { path: '/taizhang/audit',     title: '审计日志',   perm: 'view.audit',     icon: 'list' },
      { path: '/taizhang/users',     title: '用户管理',   perm: 'view.users',     icon: 'user' },
      { path: '/taizhang/roles',     title: '角色权限',   perm: 'view.roles',     icon: 'shield' },
      { path: '/taizhang/profile',   title: '个人中心',   perm: '',               icon: 'user', noPerm: true }
    ]
  },

  finance: {
    key: 'finance',
    name: '财务管理',
    dept: '财务部',
    deptName: '财务部',
    tagline: '收支与项目财务台账',
    base: '/finance',
    home: '/finance/',
    theme: 'green',
    accent: '#5DCAA5',
    description: '项目收支、保证金与财务台账独立管理，与投标数据解耦。',
    features: [
      '项目收支台账独立维护',
      '财务数据按角色控制可见范围',
      '与投标系统数据隔离、互不干扰',
      '管理部可在系统间自由切换'
    ],
    nav: [
      { path: '/finance/',       title: '财务概览',   perm: 'view.finance', icon: 'grid' },
      { path: '/finance/items',  title: '财务台账',   perm: 'view.finance', icon: 'table' },
      { path: '/finance/users',  title: '用户管理',   perm: 'view.users',   icon: 'user' },
      { path: '/finance/roles',  title: '角色权限',   perm: 'view.roles',   icon: 'shield' },
      { path: '/finance/profile',title: '个人中心',   perm: '',             icon: 'user', noPerm: true }
    ]
  },

  design: {
    key: 'design',
    name: '设计管理',
    dept: '设计部',
    deptName: '设计部',
    tagline: '设计任务与成果管理（预留）',
    base: '/design',
    home: '/design/',
    theme: 'purple',
    accent: '#AFA9EC',
    description: '设计任务分派、成果归档的专属系统，框架已就绪，业务开发中。',
    features: [
      '独立的设计部系统视图',
      '与投标、财务系统数据隔离',
      '沿用统一鉴权与角色权限体系',
      '业务模块开发中，框架已预留'
    ],
    nav: [
      { path: '/design/',        title: '设计概览',   perm: '', icon: 'grid', noPerm: true },
      { path: '/design/users',   title: '用户管理',   perm: 'view.users', icon: 'user' },
      { path: '/design/roles',   title: '角色权限',   perm: 'view.roles', icon: 'shield' },
      { path: '/design/profile', title: '个人中心',   perm: '', icon: 'user', noPerm: true }
    ]
  },

  admin: {
    key: 'admin',
    name: '管理中心',
    dept: '管理部',
    deptName: '管理部',
    tagline: '平台全局管理与统一配置',
    base: '/admin',
    home: '/admin/',
    theme: 'slate',
    accent: '#D8B678',
    description: '跨系统统一管理所有部门的用户与角色，按系统/部门分类查看与配置。',
    features: [
      '仅管理部可进入的全局管理视图',
      '用户与角色数据跨所有系统统一可见',
      '按部门/系统分类，支持折叠展开',
      '管理部可在各子系统间自由切换'
    ],
    adminOnly: true,             // 部门层硬约束：只有管理部可进（前端 UI 与后端 require_module 双重）
    nav: [
      { path: '/admin/',        title: '管理看板',   perm: 'view.admin', icon: 'grid' },
      { path: '/admin/users',   title: '用户管理',   perm: 'view.users', icon: 'user' },
      { path: '/admin/roles',   title: '角色权限',   perm: 'view.roles', icon: 'shield' },
      { path: '/admin/profile', title: '个人中心',   perm: '', icon: 'user', noPerm: true }
    ]
  }
};

export const SYSTEM_KEYS = Object.keys(SYSTEMS);
export const SYSTEM_LIST = SYSTEM_KEYS.map(k => SYSTEMS[k]);

/** 根据 URL 路径推断所属系统 key */
export function systemFromPath(pathname){
  for(const k of SYSTEM_KEYS){
    if(pathname === SYSTEMS[k].base || pathname.startsWith(SYSTEMS[k].base + '/')) return k;
  }
  return null;
}

/** 系统切换时的默认落地页 */
export function homeOf(key){
  return (SYSTEMS[key] || SYSTEMS.taizhang).home;
}
