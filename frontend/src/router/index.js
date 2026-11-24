/* ================================================================
   路由：按系统加路径前缀 /taizhang/*  /finance/*  /design/*
   - 每套系统独立路由空间，配专属外壳（config/systems.js + layouts/SystemShell.vue）
   - 旧路径（/、/manage 等）重定向到 /taizhang/* 兼容收藏夹与外部链接
   - 守卫：未登录放行（登录页由 App 渲染）；部门无权的目标系统回落到有权首页
   ================================================================ */
import { createRouter, createWebHistory } from 'vue-router'
import DashboardView from '@/views/taizhang/DashboardView.vue'
import ManageView from '@/views/taizhang/ManageView.vue'
import TimelineView from '@/views/taizhang/TimelineView.vue'
import AssignView from '@/views/taizhang/AssignView.vue'
import HandleView from '@/views/taizhang/HandleView.vue'
import ReportsView from '@/views/taizhang/ReportsView.vue'
import FilesView from '@/views/fs/FilesView.vue'
import AiSummaryView from '@/views/ai/AiSummaryView.vue'
import BidCrawlView from '@/views/bidcrawl/BidCrawlView.vue'
import UserManageView from '@/views/common/UserManageView.vue'
import RoleManageView from '@/views/common/RoleManageView.vue'
import AuditView from '@/views/taizhang/AuditView.vue'
import ProfileView from '@/views/taizhang/ProfileView.vue'
import FinanceView from '@/views/caiwu/FinanceView.vue'
import DesignView from '@/views/design/DesignView.vue'
import AdminDashboard from '@/views/admin/AdminDashboard.vue'
import { auth } from '@/stores/common/auth'
import { boot } from '@/stores/taizhang/bid'
import { can } from '@/stores/common/perm'
import { canEnter, landingSystem, syncFromPath } from '@/stores/common/system'
import { systemFromPath, SYSTEMS, homeOf } from '@/config/systems'

/* 系统内页面：suffix -> {component, perm}。perm 空=只需部门准入。 */
const TAIZHANG_PAGES = {
  '':            { c: DashboardView, perm: 'view.dashboard' },
  'manage':      { c: ManageView,    perm: 'view.manage' },
  'timeline':    { c: TimelineView,  perm: 'view.timeline' },
  'assign':      { c: AssignView,    perm: 'view.assign' },
  'alerts':      { c: HandleView,    perm: 'view.alerts' },
  'reports':     { c: ReportsView,   perm: 'view.reports' },
  'files':       { c: FilesView,     perm: 'view.files' },
  'ai-summary':  { c: AiSummaryView, perm: 'view.aiSummary' },
  'bidcrawl':    { c: BidCrawlView,  perm: 'view.bidcrawl' },
  'audit':       { c: AuditView,     perm: 'view.audit' },
  'users':       { c: UserManageView,perm: 'view.users' },
  'roles':       { c: RoleManageView,perm: 'view.roles' },
  'profile':     { c: ProfileView,   perm: '' }
};
const FINANCE_PAGES = {
  '':            { c: FinanceView,    perm: 'view.finance' },
  'items':       { c: FinanceView,    perm: 'view.finance' },
  'users':       { c: UserManageView, perm: 'view.users' },
  'roles':       { c: RoleManageView, perm: 'view.roles' },
  'profile':     { c: ProfileView,    perm: '' }
};
const DESIGN_PAGES = {
  '':            { c: DesignView,     perm: '' },
  'users':       { c: UserManageView, perm: 'view.users' },
  'roles':       { c: RoleManageView, perm: 'view.roles' },
  'profile':     { c: ProfileView,    perm: '' }
};
/* 管理中心：看板 + 全量用户/角色（复用同一组件，靠 sys.current=admin 切换全量模式） */
const ADMIN_PAGES = {
  '':            { c: AdminDashboard, perm: 'view.admin' },
  'users':       { c: UserManageView, perm: 'view.users' },
  'roles':       { c: RoleManageView, perm: 'view.roles' },
  'profile':     { c: ProfileView,    perm: '' }
};

function buildRoutes(base, pages){
  return Object.entries(pages).map(([suffix, def]) => {
    const path = suffix ? base + '/' + suffix : base + '/';
    const meta = { system: systemFromPath(base), perm: def.perm, title: '' };
    const hit = (SYSTEMS[meta.system]?.nav || []).find(n => n.path === path);
    if(hit) meta.title = hit.title;
    return { path, name: base.slice(1) + '-' + (suffix || 'home'), component: def.c, meta };
  });
}

const routes = [
  ...buildRoutes('/taizhang', TAIZHANG_PAGES),
  ...buildRoutes('/finance', FINANCE_PAGES),
  ...buildRoutes('/design', DESIGN_PAGES),
  ...buildRoutes('/admin', ADMIN_PAGES),

  /* 旧路径兼容：重定向到台账系统对应路径 */
  { path: '/',           redirect: '/taizhang/' },
  { path: '/manage',     redirect: '/taizhang/manage' },
  { path: '/timeline',   redirect: '/taizhang/timeline' },
  { path: '/assign',     redirect: '/taizhang/assign' },
  { path: '/alerts',     redirect: '/taizhang/alerts' },
  { path: '/reports',    redirect: '/taizhang/reports' },
  { path: '/files',      redirect: '/taizhang/files' },
  { path: '/ai-summary', redirect: '/taizhang/ai-summary' },
  { path: '/bidcrawl',   redirect: '/taizhang/bidcrawl' },
  { path: '/audit',      redirect: '/taizhang/audit' },
  { path: '/users',      redirect: '/taizhang/users' },
  { path: '/roles',      redirect: '/taizhang/roles' },
  { path: '/profile',    redirect: '/taizhang/profile' },
  { path: '/finance',    redirect: '/finance/' },
  { path: '/design',     redirect: '/design/' },
  { path: '/admin',      redirect: '/admin/' },
  { path: '/:pathMatch(.*)*', redirect: '/taizhang/' }
];

const router = createRouter({
  history: createWebHistory(),
  routes
});

/* 守卫：
   1) 从 URL 同步当前系统（收藏夹直达 /finance/items 时自动切到财务外壳）
   2) 未登录放行（登录页由 App 渲染）；登录态未就绪时等 boot
   3) 部门无权进入该系统 → 回落到有权系统的首页（后端仍有 403 硬门槛） */
router.beforeEach(async (to) => {
  const sysKey = systemFromPath(to.path);
  if(sysKey) syncFromPath(to.path);
  if(!auth.token) return true;
  if(!auth.user){ try{ await boot(); }catch(e){} }
  if(!auth.user) return true;
  if(sysKey && !canEnter(sysKey)){
    const land = landingSystem();
    return { path: homeOf(land) };
  }
  if(to.meta.perm && !can(to.meta.perm)){
    const base = sysKey ? SYSTEMS[sysKey].base : '/taizhang';
    return { path: base + '/' };
  }
  return true;
});

export default router;
