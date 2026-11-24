/* ================================================================
   权限判定（前端只做 UI 显隐/禁用；真正的拦截在后端）
   字段四档：writable 可写 / readonly 只读 / masked 脱敏 / hidden 不可见
   ================================================================ */
import { auth } from '@/stores/common/auth'

/** 部门模块准入：能不能进某个系统模块（后端有 403 硬拦截，这里只做 UI 显隐）。
 *  模块清单随 /api/me 返回；老缓存无 modules 时按「全模块」兼容，避免升级瞬间菜单全消失。 */
export function canModule(module){
  const u = auth.user;
  if(!u) return false;
  if(u.isAdmin) return true;
  const mods = u.modules;
  if(!mods) return true;
  return mods.includes(module);
}

/** 模块/操作权限：admin 恒真，其余取角色配置 */
export function can(key){
  const u = auth.user;
  if(!u) return false;
  if(u.isAdmin) return true;
  const p = u.perms || {};
  return !!p[key];
}

/** 字段规则 {level, mask}；兼容旧缓存里的字符串写法 */
export function fieldRule(k){
  const u = auth.user;
  const WRITE = {level:'writable', mask:'full'};
  if(!u || u.isAdmin) return WRITE;
  const raw = (u.fieldPerms || {})[k];
  if(!raw) return WRITE;
  if(typeof raw === 'string'){
    const lv = {'none':'hidden','read':'readonly','write':'writable'}[raw] || raw;
    return {level:lv, mask:'full'};
  }
  return {level: raw.level || 'writable', mask: raw.mask || 'full'};
}

/** 字段档位：'writable' | 'readonly' | 'masked' | 'hidden' */
export function fieldMode(k){ return fieldRule(k).level; }
export function canSee(k){ return fieldMode(k) !== 'hidden'; }
export function canWrite(k){ return fieldMode(k) === 'writable'; }
export function isMasked(k){ return fieldMode(k) === 'masked'; }
export function maskOf(k){ return fieldRule(k).mask; }
/** 能否用该字段参与汇总统计：隐藏或脱敏都会导致汇总失真，故不可 */
export function canAgg(k){ return canSee(k) && !isMasked(k); }

/** 行级数据范围（后端已按此过滤，前端只做提示用） */
export function dataScope(){
  const u = auth.user;
  if(!u || u.isAdmin) return 'ALL';
  return u.dataScope || 'ALL';
}

/** 台账字段四档的显示文案（与后端 LEVELS 对应） */
export const FIELD_MODES = [
  {v:'writable', l:'可编辑'},
  {v:'readonly', l:'只读'},
  {v:'masked', l:'脱敏'},
  {v:'hidden', l:'不可见'}
];
