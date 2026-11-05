<script setup>
import { computed, reactive, watch } from 'vue'
import { ST, cfgDlg, saveTableCfg, applyTableCfg, deleteTableCfg, toast } from '../stores/bid'
import { TABLE_COLS, DEFAULT_WIDTHS, defaultTableCfg, clampRowH, MIN_ROW_H, MAX_ROW_H } from '../lib/tableCols'
import { canSee } from '../stores/perm'

const TIME_L = computed(() => ST.basis === 'deadline' ? '交标时间' : '开标时间');

const draft = reactive({activeId:'', name:'', cols:[], widths:{}, rowH:40});

function canSeeCol(c){
  if(c.k === 'project') return canSee('projectName');
  if(c.k === 'time') return canSee(ST.basis === 'deadline' ? 'deadline' : 'openTime');
  if(c.k === 'amount') return canSee('ourPrice') || canSee('winAmount');
  if(c.k === 'bondStatus') return canSee('bondStatus');
  return canSee(c.k);
}
/* 可选列按字段分组；无权限的字段不出现 */
const groups = computed(() => {
  const out = [], seen = {};
  TABLE_COLS.forEach(c => {
    if(!canSeeCol(c)) return;
    const g = c.g || '常用列';
    if(!seen[g]){ seen[g] = []; out.push({g, items:seen[g]}); }
    seen[g].push(c);
  });
  return out;
});
function labelOf(c){ return c.k === 'time' ? TIME_L.value : c.l; }

function loadDraft(){
  draft.activeId = ST.tableCfg.activeId;
  const c = ST.tableCfg.list.find(x => x.id === draft.activeId);
  draft.name = c ? c.name : '';
  draft.cols = ST.tableCfg.cols.slice();
  draft.widths = Object.assign({}, DEFAULT_WIDTHS, ST.tableCfg.widths);
  draft.rowH = ST.tableCfg.rowH;
}
watch(() => cfgDlg.open, v => { if(v) loadDraft(); });

function has(k){ return draft.cols.indexOf(k) >= 0; }
function idx(k){ return draft.cols.indexOf(k); }
function toggle(k){
  const i = idx(k);
  if(i >= 0){
    if(draft.cols.length <= 1){ toast('至少保留一列'); return; }
    draft.cols.splice(i, 1);
  }else{
    draft.cols.push(k);
    if(!draft.widths[k]) draft.widths[k] = DEFAULT_WIDTHS[k] || 120;
  }
}
function move(k, dir){
  const i = idx(k), j = i + dir;
  if(i < 0 || j < 0 || j >= draft.cols.length) return;
  draft.cols.splice(i, 1);
  draft.cols.splice(j, 0, k);
}
function pick(id){
  draft.activeId = id;
  if(!id){
    const d = defaultTableCfg();
    draft.name = '';
    draft.cols = d.cols;
    draft.widths = Object.assign({}, d.widths);
    draft.rowH = d.rowH;
    return;
  }
  const c = ST.tableCfg.list.find(x => x.id === id);
  if(!c) return;
  draft.name = c.name;
  draft.cols = c.cols.slice();
  draft.widths = Object.assign({}, DEFAULT_WIDTHS, c.widths);
  draft.rowH = c.rowH;
}
function resetDraft(){
  const d = defaultTableCfg();
  draft.activeId = '';
  draft.name = '';
  draft.cols = d.cols;
  draft.widths = Object.assign({}, d.widths);
  draft.rowH = d.rowH;
}
function commit(){
  draft.rowH = clampRowH(draft.rowH);
  const n = draft.name.trim();
  if(!n){
    applyTableCfg('');
    cfgDlg.open = false;
    toast('已使用默认视图');
    return;
  }
  const c = saveTableCfg(n, draft);
  if(!c) return;
  applyTableCfg(c.id);
  cfgDlg.open = false;
  toast('已应用配置「' + c.name + '」');
}
function del(){
  const c = ST.tableCfg.list.find(x => x.id === draft.activeId);
  if(!c || !confirm('确认删除配置「' + c.name + '」？')) return;
  deleteTableCfg(c.id);
  resetDraft();
  toast('已删除配置');
}
</script>

<template>
  <div class="mask" :class="{on: cfgDlg.open}" @click.self="cfgDlg.open = false">
    <div class="dlg" style="width:min(900px, calc(100vw - 40px))">
      <div class="dlg-h">
        <h3>表格字段与列配置</h3>
        <span class="act" style="color:var(--mut)" @click="cfgDlg.open = false">关闭 ✕</span>
      </div>
      <div class="dlg-b">
        <div class="cfg-top">
          <label>已保存配置</label>
          <select :value="draft.activeId" @change="pick($event.target.value)" style="width:200px">
            <option value="">默认视图</option>
            <option v-for="c in ST.tableCfg.list" :key="c.id" :value="c.id">{{ c.name }}</option>
          </select>
          <label>配置名称</label>
          <input type="text" maxlength="30" placeholder="例如：招投标周会视图" v-model="draft.name" style="width:190px">
          <label>行高</label>
          <input type="number" :min="MIN_ROW_H" :max="MAX_ROW_H" v-model.number="draft.rowH" style="width:72px"> px
        </div>

        <div class="cfg-hint">勾选要显示的列并调整顺序；表头分隔线可拖动改列宽，工具栏「行高」手柄可上下拖动。</div>

        <div class="cfg-groups">
          <div v-for="g in groups" :key="g.g" class="cfg-group">
            <div class="cfg-sec-title">{{ g.g }}</div>
            <div class="cfg-grid">
              <div v-for="c in g.items" :key="c.k" class="cfg-col-item" :class="{on: has(c.k)}">
                <input type="checkbox" :checked="has(c.k)" @change="toggle(c.k)">
                <span class="nm">{{ labelOf(c) }}</span>
                <span v-if="has(c.k)" class="cfg-ord">
                  <em>{{ idx(c.k) + 1 }}</em>
                  <span class="cfg-mini" :class="{dis: idx(c.k) <= 0}" title="上移" @click="move(c.k, -1)">↑</span>
                  <span class="cfg-mini" :class="{dis: idx(c.k) >= draft.cols.length - 1}" title="下移" @click="move(c.k, 1)">↓</span>
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div class="dlg-f">
        <button v-if="draft.activeId" class="btn danger" style="margin-right:auto" @click="del()">删除此配置</button>
        <span style="margin-right:auto" v-else></span>
        <button class="btn ghost" @click="resetDraft()">恢复默认</button>
        <button class="btn" @click="cfgDlg.open = false">取消</button>
        <button class="btn pri" @click="commit()">保存并应用</button>
      </div>
    </div>
  </div>
</template>
