/* ================================================================
   分片上传引擎：并发 3 · 分片串行 · 失败退避重试 · 暂停/继续 · 断点续传
   内存占用 ≈ 并发 × 分片大小，与文件大小无关（每片只持有一个 Blob 引用）
   ================================================================ */
import { api, apiRaw } from '../api/client'

const HASH_LIMIT = 100 * 1024 * 1024;   // 超过 100MB 不预计算哈希（太慢），由服务端合并后校验
const SKIP = ['__macosx', '.git'];      // 纯系统/版本库噪音目录，整文件夹上传时跳过
const SKIP_FILE = ['.ds_store', 'thumbs.db', 'desktop.ini'];

function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }

/* ---------------- 全局上传任务登记 ----------------
   每个 UploadQueue 实例自注册，供右下角「上传任务」悬浮条（UploadDock.vue）展示。
   不依赖 Vue 响应式：消费方用定时器轮询 LIVE 即可，避免队列状态与组件生命周期耦合。 */
export const LIVE = [];
let QSEQ = 0;
export function liveTasks(){ return LIVE.filter(q => q.items.length); }

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

/* 与服务端 fs.rel_bad 对齐：非法片段返回片段本身，合法返回 ''。
   注意开头的点合法（.github / .gitignore / .env），只有「尾部」点为非法。 */
const BAD_CH = /[<>:"\\|?*\u0000-\u001f]/;
export function relBad(rel){
  const segs = String(rel || '').split('/');
  for(let i = 0; i < segs.length; i++){
    const s = segs[i];
    if(!s || s === '.' || s === '..') return s || rel;
    if(BAD_CH.test(s) || /[. ]$/.test(s)) return s;
  }
  return '';
}

function keep(rel){
  const segs = String(rel || '').split('/').filter(Boolean);
  if(segs.some(s => SKIP.indexOf(s.toLowerCase()) >= 0)) return false;
  const fn = (segs[segs.length - 1] || '').toLowerCase();
  return SKIP_FILE.indexOf(fn) < 0;      // 隐藏文件（.gitignore 等）保留，只跳过系统垃圾文件
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
  constructor(title){
    this.id = ++QSEQ;
    this.title = title || '上传文件';   // 悬浮进度条上的任务名（调用方可改）
    this.sub = '';                       // 副标题（项目名等）
    this.items = [];
    this.conc = 2;               // 文件级并发：单 worker 后端下 2 个足够，再多只会互相抢磁盘
    this.paused = false;
    this.active = 0;
    this.onChange = null;
    this.onFinish = null;
    this._resumers = [];
    this._seq = 0;
    this._samples = [];          // 速度采样（最近 5 秒），用于算瞬时速度与剩余时间
    this._finAt = 0;             // 整批完成的时间戳（悬浮条据此自动收起）
  }

  emit(){
    if(this.onChange) this.onChange();
    this._sample();
    /* 空闲通知集中在这里：无论「传完 / 失败 / 取消」都会恰好触发一次，
       避免取消或断网时 onFinish 不触发、界面停在「上传中」 */
    const busy = this.active > 0 || this.items.some(it => it.status === 'wait');
    if(busy) this._wasBusy = true;
    else if(this._wasBusy){ this._wasBusy = false; if(this.onFinish) this.onFinish(); }
  }

  /* 采样 + 完成时刻标记：用字节增量算瞬时速度，比「总进度/总耗时」对卡顿更敏感 */
  _sample(){
    let done = 0;
    this.items.forEach(it => { done += Math.min(it.done || 0, it.size || 0); });
    const now = Date.now(), s = this._samples, last = s[s.length - 1];
    if(!last || last.bytes !== done || now - last.t > 900) s.push({ t: now, bytes: done });
    while(s.length > 2 && now - s[0].t > 5000) s.shift();
    if(!this.items.length){ this._finAt = 0; this._samples = []; return; }
    const finished = this.items.every(it => it.status === 'done' || it.status === 'fail' || it.status === 'cancel');
    if(finished && !this._finAt) this._finAt = now;
    if(!finished) this._finAt = 0;
    if(finished) this._samples = [];       // 停下来就不该再显示速度/剩余时间
  }

  add(files, parentId, opts){
    (files || []).forEach(f => {
      const rel = f.rel || f.file.name;
      this.items.push(Object.assign({
        key: ++this._seq, file: f.file, rel: rel, name: f.file.name,
        parentId: parentId, size: f.file.size, done: 0, status: 'wait',
        uploadId: '', total: 0, chunkSize: 0, nodeId: '', ver: 0, error: ''
      }, opts || {}));
    });
    if(LIVE.indexOf(this) < 0) LIVE.push(this);   // 有任务才登记，空队列不进悬浮条
    this.emit();
    this._pump();
    return this.items.length;
  }

  stats(){
    let total = 0, done = 0, ok = 0, fail = 0, run = 0, wait = 0, cancel = 0;
    this.items.forEach(it => {
      total += it.size;
      done += Math.min(it.done || 0, it.size || 0);
      if(it.status === 'done') ok++;
      else if(it.status === 'fail') fail++;
      else if(it.status === 'cancel') cancel++;
      else if(it.status === 'wait') wait++;
      else run++;                       // run / hash（校验中）都算进行中
    });
    if(done > total) done = total;
    const pct = total ? Math.min(100, Math.round(done * 1000 / total) / 10) : 0;
    let bps = 0;
    const s = this._samples;
    if(s.length > 1){
      const a = s[0], b = s[s.length - 1], dt = (b.t - a.t) / 1000;
      if(dt >= 0.4) bps = Math.max(0, (b.bytes - a.bytes) / dt);
    }
    const left = Math.max(0, total - done);
    return {
      total, done, ok, fail, run, wait, cancel, pct, bps,
      eta: bps > 0 && left > 0 ? Math.ceil(left / bps) : 0,
      files: this.items.length,
      busy: run > 0 || wait > 0,
      finished: this.items.length > 0 && ok + fail + cancel === this.items.length
    };
  }

  /** 重试全部失败项（含批量失败时一键重来） */
  retryFailed(){
    let n = 0;
    this.items.forEach(it => {
      if(it.status === 'fail'){ it.status = 'wait'; it.error = ''; it.done = 0; n++; }
    });
    if(n){ this.paused = false; this.emit(); this._pump(); }
    return n;
  }
  /** 取消全部未完成项（保留已完成的记录） */
  cancelAll(){
    this.items.forEach(it => { if(it.status === 'wait' || it.status === 'run' || it.status === 'hash') this.cancel(it.key); });
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
    if(it.status === 'run' || it.status === 'wait' || it.status === 'hash'){
      it.status = 'cancel';
      if(it.uploadId) api('/api/fs/upload/abort', { method: 'POST', body: JSON.stringify({ uploadId: it.uploadId }) }).catch(() => {});
    }
    this.emit();
  }
  retry(key){
    const it = this.items.filter(x => x.key === key)[0];
    if(!it) return;
    it.status = 'wait'; it.error = ''; it.done = 0;
    this.paused = false;
    this.emit();
    this._pump();
  }
  clearDone(){
    this.items = this.items.filter(x => x.status !== 'done' && x.status !== 'cancel');
    this.emit();
  }
  /** 清空整条队列（会中断进行中的上传，调用前应确认） */
  clearAll(){
    this.cancelAll();
    this.items = [];
    this._samples = [];
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
        this.emit();          // 空闲时会在这里触发 onFinish（见 emit）
        this._pump();
      });
    }
  }

  async _run(it){
    try{
      /* 大文件先算 sha256 可能耗时数秒，期间给出「校验中」，避免用户以为卡住 */
      it.status = 'hash'; it.stage = '校验文件指纹…';
      this.emit();
      const sha = await sha256Of(it.file);
      if(it.status === 'cancel') return;
      it.status = 'run'; it.stage = '';
      this.emit();
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

      /* 合并阶段可能被判「分片与磁盘不一致 / 还缺 N 片」（GC、取消、服务端清理都会造成），
         这类 409 不是终态：重新拉一次进度、补齐缺片再合并，最多补 3 轮 */
      for(let round = 1; round <= 3; round++){
        if(!await this._sendParts(it)) return;        // 被取消
        try{
          const r = await api('/api/fs/upload/complete', {
            method: 'POST', body: JSON.stringify({ uploadId: it.uploadId })
          });
          it.nodeId = r.nodeId; it.ver = r.ver || 1; it.done = it.size;
          it.status = 'done'; it.stage = '';
          return;
        }catch(e){
          const missPart = e && e.status === 409 && /分片/.test(e.message || '');
          if(!missPart || round === 3) throw e;
          it.stage = '分片不齐，正在补传（第 ' + (round + 1) + ' 轮）';
          this.emit();
        }
      }
    }catch(e){
      it.status = 'fail';
      it.stage = '';
      it.error = (e && e.message) || '上传失败';
    }
  }

  /* 拉一次服务端已收分片 + 串行补齐缺失分片；返回 false 表示任务被取消 */
  async _sendParts(it){
    let got = [];
    try{
      const st = await api('/api/fs/upload/status?upload_id=' + it.uploadId);
      got = st.got || [];
      if(st.total) it.total = Number(st.total) || it.total;
      it.done = Math.min(it.size, got.length * it.chunkSize);
      this.emit();
    }catch(e){ /* 拉不到进度就从头补，服务端会按已有分片去重 */ }
    for(let i = 0; i < it.total; i++){
      await this._wait();
      if(it.status === 'cancel') return false;
      if(got.indexOf(i) >= 0){
        it.done = Math.min(it.size, (i + 1) * it.chunkSize);
        continue;
      }
      await this._chunk(it, i);
    }
    return it.status !== 'cancel';
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
        it.stage = '';
        this.emit();
        return;
      }catch(e){
        last = e;
        it.stage = '连接中断，第 ' + (t + 1) + ' 次重试';
        this.emit();
        await sleep(400 * Math.pow(2, t));
      }
    }
    throw last || new Error('分片上传失败');
  }
}

/* ---------------- 文件存储页共享队列 ----------------
   模块级单例：从「文件存储」切到别的页面再回来，队列里的进度与失败记录仍在，
   且后台上传不会因为路由卸载而中断。只有 add() 后才会登记进 LIVE 悬浮条。 */
export const filesQueue = new UploadQueue('文件存储');
