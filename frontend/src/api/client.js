/* ================================================================
   API 客户端：fetch 封装 + Bearer token + 401 处理
   ================================================================ */
let _token = '';
try{ _token = localStorage.getItem('wb_token') || ''; }catch(e){}

let _on401 = null;

export function getToken(){ return _token; }

export function setToken(t){
  _token = t || '';
  try{ _token ? localStorage.setItem('wb_token', _token) : localStorage.removeItem('wb_token'); }catch(e){}
}

export function onUnauthorized(fn){ _on401 = fn; }

export async function api(path, opts){
  opts = opts || {};
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
    throw new Error(msg);
  }
  return resp.json();
}

/* 原始响应：上传分片（裸流 body）、下载 Blob、导出 CSV 等不走 JSON 的场景 */
export async function apiRaw(path, opts){
  opts = opts || {};
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
    throw new Error(msg);
  }
  return resp;
}

/** 下载为 Blob（带 Authorization 头，服务端流式返回） */
export async function apiBlob(path){
  return (await apiRaw(path)).blob();
}
