/* ================================================================
   API 客户端：fetch 封装 + Bearer token + 401 处理
   ================================================================ */
let _token = '';
try{ _token = localStorage.getItem('wb_token') || ''; }catch(e){}

let _on401 = null;

/* ================================================================
   阶段 3：API 路径域重组映射（渐进迁移开关）

   后端已把路由重组为：
     /api/common/*     通用能力（认证/用户/角色/权限/日报）
     /api/taizhang/*   台账业务
   同时保留旧 /api/* 前缀兼容。

   默认 USE_DOMAIN_PATHS = false：前端仍走旧路径，零风险。
   待前端准备就绪后改为 true，所有 api()/apiRaw() 调用会自动重写到新路径，
   业务组件无需逐个修改。映射按「最长前缀优先」匹配，避免 /api/me 误命中 /api/me/password。
   ================================================================ */
const USE_DOMAIN_PATHS = false;

const DOMAIN_PREFIXES = [
  // 台账业务专属
  ['/api/bootstrap', '/api/taizhang'],
  ['/api/rows',      '/api/taizhang'],
  ['/api/changes',   '/api/taizhang'],
  ['/api/merge',     '/api/taizhang'],
  ['/api/clear',     '/api/taizhang'],
  ['/api/audit',     '/api/taizhang'],
  ['/api/bid-dir',   '/api/taizhang'],
  ['/api/bid-files', '/api/taizhang'],
  // 通用能力
  ['/api/login',       '/api/common'],
  ['/api/me',          '/api/common'],
  ['/api/perm/catalog','/api/common'],
  ['/api/users',       '/api/common'],
  ['/api/roles',       '/api/common'],
  ['/api/staff',       '/api/common'],
  ['/api/reports',     '/api/common'],
  // 财务业务
  ['/api/finance',     '/api/finance'],   // 已是新路径，保留以免误映射
  // fs / ai-summary / bidcrawl 保持原路径，不映射
];

export function rewritePath(path){
  if(!USE_DOMAIN_PATHS) return path;
  for(const [oldP, newP] of DOMAIN_PREFIXES){
    if(path === oldP || path.startsWith(oldP + '/') || path.startsWith(oldP + '?')){
      return newP + path.slice(oldP.length);
    }
  }
  return path;
}

export function getToken(){ return _token; }

export function setToken(t){
  _token = t || '';
  try{ _token ? localStorage.setItem('wb_token', _token) : localStorage.removeItem('wb_token'); }catch(e){}
}

export function onUnauthorized(fn){ _on401 = fn; }

export async function api(path, opts){
  opts = opts || {};
  path = rewritePath(path);
  const headers = Object.assign({'Content-Type':'application/json'}, opts.headers || {});
  if(_token) headers['Authorization'] = 'Bearer ' + _token;
  let resp;
  try{ resp = await fetch(path, Object.assign({}, opts, {headers})); }
  catch(e){ throw new Error('无法连接服务器'); }
  if(resp.status === 401 && _on401) _on401();
  if(!resp.ok){
    let msg = '请求失败（' + resp.status + '）';
    try{
      const d = await resp.json();
      if(d && typeof d.detail === 'string') msg = d.detail;
    }catch(e){}
    const err = new Error(msg);
    err.status = resp.status;      // 调用方据此判断是否可自动重试（4xx 不重试）
    throw err;
  }
  return resp.json();
}

/* 原始响应：上传分片（裸流 body）、下载 Blob、导出 CSV 等不走 JSON 的场景 */
export async function apiRaw(path, opts){
  opts = opts || {};
  path = rewritePath(path);
  const headers = Object.assign({}, opts.headers || {});
  if(_token) headers['Authorization'] = 'Bearer ' + _token;
  let resp;
  try{ resp = await fetch(path, Object.assign({}, opts, {headers})); }
  catch(e){ throw new Error('无法连接服务器'); }
  if(resp.status === 401 && _on401) _on401();
  if(!resp.ok){
    let msg = '请求失败（' + resp.status + '）';
    try{
      const d = await resp.json();
      if(d && typeof d.detail === 'string') msg = d.detail;
    }catch(e){}
    const err = new Error(msg);
    err.status = resp.status;
    throw err;
  }
  return resp;
}

/** 下载为 Blob（带 Authorization 头，服务端流式返回） */
export async function apiBlob(path){
  return (await apiRaw(path)).blob();
}
