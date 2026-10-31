/* ================================================================
   路由：/ 首页看板 · /manage 投标管理 · /users 用户管理 · /roles 角色权限
   各页面对应后端「模块权限」，无权限自动回首页
   ================================================================ */
import { createRouter, createWebHistory } from 'vue-router'
import DashboardView from '../views/DashboardView.vue'
import ManageView from '../views/ManageView.vue'
import UserManageView from '../views/UserManageView.vue'
import RoleManageView from '../views/RoleManageView.vue'
import AuditView from '../views/AuditView.vue'
import { auth } from '../stores/auth'
import { boot } from '../stores/bid'
import { can } from '../stores/perm'

const routes = [
  { path: '/', name: 'dash', component: DashboardView, meta: { perm: 'view.dashboard' } },
  { path: '/manage', name: 'manage', component: ManageView, meta: { perm: 'view.manage' } },
  { path: '/users', name: 'users', component: UserManageView, meta: { perm: 'view.users' } },
  { path: '/roles', name: 'roles', component: RoleManageView, meta: { perm: 'view.roles' } },
  { path: '/audit', name: 'audit', component: AuditView, meta: { perm: 'view.audit' } },
  { path: '/:pathMatch(.*)*', redirect: '/' }
];

const router = createRouter({
  history: createWebHistory(),
  routes
});

/* 模块权限守卫：未登录交给登录页（登录页由 App 渲染，无需重定向）；
   登录态未就绪（如服务器不可达）时放行，避免重定向死循环；无该模块权限回首页 */
router.beforeEach(async (to) => {
  if(!to.meta.perm) return true;
  if(!auth.token) return true;
  if(!auth.user){ try{ await boot(); }catch(e){} }   // 刷新直达时等登录态加载完
  if(!auth.user) return true;
  if(can(to.meta.perm)) return true;
  return to.path === '/' ? true : { path: '/' };
});

export default router;
