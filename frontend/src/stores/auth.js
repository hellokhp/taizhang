/* ================================================================
   登录态：auth 响应式状态 + login / logout
   ================================================================ */
import { reactive } from 'vue'
import { api, setToken, getToken, onUnauthorized } from '../api/client'

export const auth = reactive({ token: getToken(), user: null, needLogin: !getToken(), err: '', loading: false });

/* 任意请求 401 → 弹出登录框 */
onUnauthorized(() => { auth.needLogin = true; });

export async function login(username, password){
  const d = await api('/api/login', {method:'POST', body: JSON.stringify({username, password})});
  setToken(d.token);
  auth.token = d.token;
}

export function logout(){
  setToken('');
  location.reload();
}
