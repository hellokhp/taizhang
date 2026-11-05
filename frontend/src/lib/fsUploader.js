/* ================================================================
   分片上传引擎：并发 3 · 分片串行 · 失败退避重试 · 暂停/继续 · 断点续传
   内存占用 ≈ 并发 × 分片大小，与文件大小无关（每片只持有一个 Blob 引用）
   ================================================================ */
import { api, apiRaw } from '../api/client'

const HASH_LIMIT = 100 * 1024 * 1024;   // 超过 100MB 不预计算哈希（太慢），由服务端合并后校验
const SKIP = ['__MACOSX'];
const SKIP_FILE = ['.ds_store', 'thumbs.db', 'desktop.ini'];

function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }

/** 小文件才算 sha256 做秒传 + 完整性校验；非 https/localhost 下 crypto.subtle 不可用则跳过 */
export async function sha256Of(file){
  try{
    if(file.size > HASH_LIMIT) return '';
    if(!window.crypto || !crypto.subtle) return '';
    const buf = await file.arrayBuffer();
    const d = await crypto.subtle.digest('SHA-256', buf);
    return Array.prototype.map.call(new Uint8Array(d), b => ('0' + b.toString(16)).slice(-2)).join('');
  }catch(e){ return ''; }
}

function keep(rel){
  const segs = String(rel || '').split('/');
  if(segs.some(s => SKIP.indexOf(s) >= 0)) return false;
  const fn = segs[segs.length - 1].toLowerCase();
  return SKIP_FILE.indexOf(fn) < 0 && fn.charAt(0) !== '.';
}

/** <input webkitdirectory> 选中的文件（含相对路径） */
export function fromInput(fileList){
  const out = [];
  Array.prototype.forEach.call(fileList || [], f => {
    const rel = f.webkitRelativePath || f.name;
    if(keep(rel)) out.push({ file: f, rel: rel });
  });
  return out;
}

/** 拖拽文件夹：webkitGetAsEntry 递归遍历 */
export function fromDataTransfer(dt){
  const entries = [];
  Array.prototype.forEach.call((dt && dt.items) || [], it => {
    const e = it.webkitGetAsEntry && it.webkitGetAsEntry();
    if(e) entries.push(e);
  });
  const files = [], dirs = [];
  function walk(entry, prefix){
    return new Promise(res => {
      if(!entry) return res();
      const path = prefix ? prefix + '/' + entry.name : entry.name;
      if(entry.isFile){
        entry.file(f => { if(keep(path)) files.push({ file: f, rel: path }); res(); }, () => res());
      }else if(entry.isDirectory){
        if(keep(path)) dirs.push(path);
        const reader = entry.createReader();
        const all = [];
        (function read(){
          reader.readEntries(list => {
            if(!list.length){
              Promise.all(all.map(e => walk(e, path))).then(() => res());
              return;
            }
            all.push.apply(all, list);
            read();
          }, () => res());
        })();
      }else res();
    });
  }
  return Promise.all(entries.map(e => walk(e, ''))).then(() => ({ files, dirs }));
}

export class UploadQueue{
  constructor(){
    this.items = [];
    this.conc = 3;
    this.paused = false;
    this.active = 0;
    this.onChange = null;
    this.onFinish = null;
    this._resumers = [];
    this._seq = 0;
  }

  emit(){ if(this.onChange) this.onChange(); }

  add(files, parentId, opts){
    (files || []).forEach(f => {
      const rel = f.rel || f.file.name;
      this.items.push(Object.assign({
        key: ++this._seq, file: f.file, rel: rel, name: f.file.name,
        parentId: parentId, size: f.file.size, done: 0, status: 'wait',
        uploadId: '', total: 0, chunkSize: 0, nodeId: '', ver: 0, error: ''
      }, opts || {}));
    });
    this.emit();
    this._pump();
    return this.items.length;
  }

  stats(){
    let total = 0, done = 0, ok = 0, fail = 0, run = 0;
    this.items.forEach(it => {
      total += it.size; done += it.done;
      if(it.status === 'done') ok++;
      else if(it.status === 'fail') fail++;
      else if(it.status === 'run') run++;
    });
    return { total, done, ok, fail, run, pct: total ? Math.round(done * 1000 / total) / 10 : 0 };
  }

  pause(){ this.paused = true; this.emit(); }
  resume(){
    this.paused = false;
    (this._resumers || []).forEach(f => f());
    this._resumers = [];
    this._pump();
  }
  cancel(key){
    const it = this.items.filter(x => x.key === key)[0];
    if(!it) return;
    if(it.status === 'run' || it.status === 'wait'){
      it.status = 'cancel';
      if(it.uploadId) api('/api/fs/upload/abort', { method: 'POST', body: JSON.stringify({ uploadId: it.uploadId }) }).catch(() => {});
    }
    this.emit();
  }
  retry(key){
    const it = this.items.filter(x => x.key === key)[0];
    if(!it) return;
    it.status = 'wait'; it.error = ''; it.done = 0;
    this.emit();
    this._pump();
  }
  clearDone(){
    this.items = this.items.filter(x => x.status !== 'done' && x.status !== 'cancel');
    this.emit();
  }
  clearAll(){
    this.items.forEach(it => { if(it.status === 'run') this.cancel(it.key); });
    this.items = [];
    this.emit();
  }

  _wait(){
    if(!this.paused) return Promise.resolve();
    return new Promise(r => this._resumers.push(r));
  }

  _pump(){
    while(this.active < this.conc && !this.paused){
      const it = this.items.filter(x => x.status === 'wait')[0];
      if(!it) break;
      it.status = 'run';
      this.active++;
      this.emit();
      this._run(it).then(() => {
        this.active--;
        this.emit();
        this._pump();
        if(!this.items.some(x => x.status === 'run' || x.status === 'wait') && this.onFinish) this.onFinish();
      });
    }
  }

  async _run(it){
    try{
      const sha = await sha256Of(it.file);
      const init = await api('/api/fs/upload/init', {
        method: 'POST',
        body: JSON.stringify({ parentId: it.parentId, name: it.name, size: it.file.size,
                               sha256: sha, conflict: it.conflict || 'reject' })
      });
      if(init.instant){                       // 秒传命中
        it.done = it.size; it.status = 'done'; it.nodeId = init.nodeId; it.ver = init.ver || 1;
        return;
      }
      it.uploadId = init.uploadId;
      it.total = init.total;
      it.chunkSize = init.chunkSize;
      it.name = init.name || it.name;
      let got = [];
      try{
        const st = await api('/api/fs/upload/status?upload_id=' + it.uploadId);
        got = st.got || [];
        it.done = got.length * it.chunkSize;
      }catch(e){}
      for(let i = 0; i < it.total; i++){
        await this._wait();
        if(it.status === 'cancel') return;
        if(got.indexOf(i) >= 0){
          it.done = Math.min(it.size, (i + 1) * it.chunkSize);
          continue;
        }
        await this._chunk(it, i);
      }
      if(it.status === 'cancel') return;
      const r = await api('/api/fs/upload/complete', {
        method: 'POST', body: JSON.stringify({ uploadId: it.uploadId })
      });
      it.nodeId = r.nodeId; it.ver = r.ver; it.done = it.size; it.status = 'done';
    }catch(e){
      it.status = 'fail';
      it.error = (e && e.message) || '上传失败';
    }
  }

  async _chunk(it, i){
    const start = i * it.chunkSize;
    const end = Math.min(start + it.chunkSize, it.file.size);
    const blob = it.file.slice(start, end);
    let last = null;
    for(let t = 0; t < 3; t++){
      await this._wait();
      if(it.status === 'cancel') return;
      try{
        await apiRaw('/api/fs/upload/chunk?upload_id=' + it.uploadId + '&index=' + i,
                     { method: 'POST', body: blob, headers: { 'Content-Type': 'application/octet-stream' } });
        it.done = end;
        this.emit();
        return;
      }catch(e){
        last = e;
        await sleep(400 * Math.pow(2, t));
      }
    }
    throw last || new Error('分片上传失败');
  }
}
