/* ================================================================
   存储权限位（前端只做显隐/禁用，真正拦截在后端）
   五个原子动作：查看 / 上传 / 修改 / 覆盖 / 删除（+ 授权）
   ================================================================ */
export const FS_OPS = [
  { k: 'view', l: '查看' },
  { k: 'upload', l: '上传' },
  { k: 'modify', l: '修改' },
  { k: 'overwrite', l: '覆盖' },
  { k: 'delete', l: '删除' }
];

export function canFs(perm, op){
  return !!(perm && perm[op]);
}

/** 权限位 -> 文字，用于日志与提示 */
export function fsPermText(perm){
  if(!perm) return '无';
  const on = FS_OPS.filter(o => perm[o.k]).map(o => o.l);
  return on.length ? on.join('·') : '无';
}

/** 大小格式化 */
export function humanSize(n){
  n = Number(n || 0);
  if(n < 1024) return n + ' B';
  if(n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  if(n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
  return (n / 1073741824).toFixed(2) + ' GB';
}

export function tsText(t){
  if(!t) return '—';
  return new Date(t * 1000).toLocaleString('zh-CN', { hour12: false });
}

/** 图标类型（按扩展名给个简短分类，纯展示用） */
export function kindOf(name){
  const ext = String(name || '').split('.').pop().toLowerCase();
  const map = {
    pdf: 'PDF', doc: 'DOC', docx: 'DOC', xls: 'XLS', xlsx: 'XLS', csv: 'XLS',
    ppt: 'PPT', pptx: 'PPT', png: 'IMG', jpg: 'IMG', jpeg: 'IMG', gif: 'IMG',
    webp: 'IMG', bmp: 'IMG', zip: 'ZIP', rar: 'ZIP', '7z': 'ZIP',
    txt: 'TXT', mp4: 'VID', avi: 'VID', mov: 'VID'
  };
  return map[ext] || (ext ? ext.slice(0, 3).toUpperCase() : 'FILE');
}
