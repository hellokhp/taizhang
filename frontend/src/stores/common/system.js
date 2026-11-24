/* ================================================================
   当前系统状态：记住用户在哪个子系统，提供其有权访问的系统列表。
   部门模块清单随 /api/me 返回（auth.user.modules）；后端准入是硬门槛，
   这里只做 UI 显隐与跳转，防止用户选到无权系统后撞 403。
   ================================================================ */
import { reactive } from 'vue'
import { auth } from '@/stores/common/auth'
import { SYSTEMS, SYSTEM_LIST, SYSTEM_KEYS, homeOf, systemFromPath } from '@/config/systems'

const LS_KEY = 'yadecai.currentSystem';
let remembered = '';
try{ remembered = localStorage.getItem(LS_KEY) || ''; }catch(e){}

export const sys = reactive({
  current: SYSTEM_KEYS.includes(remembered) ? remembered : 'taizhang'
});

/** 是否管理部（超管或 dept=management）——管理中心只对管理部开放 */
export function isManagement(authUser){
  const u = authUser || auth.user;
  return !!(u && (u.isAdmin || u.dept === 'management'));
}

/** 当前用户有权访问的系统列表（管理部 is_admin/全模块 → 全部） */
export function availableSystems(){
  const u = auth.user;
  if(!u) return [];
  const mgmt = isManagement(u);
  if(u.isAdmin) return SYSTEM_LIST.slice();
  const mods = u.modules;
  const list = mods ? SYSTEM_LIST.filter(s => mods.includes(s.key)) : SYSTEM_LIST.slice();  // 老缓存兼容
  // 双保险：adminOnly 系统（管理中心）即便 modules 误配也不向非管理部暴露
  return list.filter(s => !s.adminOnly || mgmt);
}

/** 当前用户是否有权进入某系统 */
export function canEnter(key){
  return availableSystems().some(s => s.key === key);
}

/** 记住当前系统（同时写 localStorage，刷新后保持） */
export function setCurrent(key){
  if(!SYSTEMS[key]) return;
  sys.current = key;
  try{ localStorage.setItem(LS_KEY, key); }catch(e){}
}

/** 从 URL 推断并同步当前系统（路由守卫调用，支持收藏夹直达） */
export function syncFromPath(pathname){
  const k = systemFromPath(pathname);
  if(k && SYSTEMS[k]) setCurrent(k);
}

/** 切换到目标系统：返回首页路径（由调用方 router.push） */
export function switchTo(key){
  if(!canEnter(key)) return null;
  setCurrent(key);
  return homeOf(key);
}

/** 登录后落哪个系统：优先记住的上次系统（若仍有权），否则第一个有权系统 */
export function landingSystem(){
  const avail = availableSystems();
  if(!avail.length) return 'taizhang';
  const hit = avail.find(s => s.key === sys.current);
  return (hit || avail[0]).key;
}
