<script setup>
/* ================================================================
   上传队列面板：整批进度 + 单文件状态 + 暂停/继续/重试/取消。
   与 lib/fsUploader.js 的 stats() 对齐；样式见 main.css 的 .fs-q-*。
   队列实例是模块级单例，页面切走后上传仍在后台继续，因此卸载时必须解绑回调。
   ================================================================ */
import { computed, onUnmounted, ref } from 'vue'
import { humanSize } from '../../lib/fsPerm'

const props = defineProps({ queue: { type: Object, required: true } });
const emit = defineEmits(['done']);
const tick = ref(0);

const onChange = () => { tick.value++; };
const onFinish = () => { emit('done'); };
props.queue.onChange = onChange;
props.queue.onFinish = onFinish;
const timer = setInterval(() => { tick.value++; }, 500);
onUnmounted(() => {
  clearInterval(timer);
  if(props.queue.onChange === onChange) props.queue.onChange = null;
  if(props.queue.onFinish === onFinish) props.queue.onFinish = null;
});

const st = computed(() => { tick.value; return props.queue.stats(); });
const list = computed(() => { tick.value; return props.queue.items; });
/* 失败原因常驻在列表上方：列表有滚动区，单文件那行错误容易被滚到底部看不见 */
const firstError = computed(() => {
  tick.value;
  return props.queue.items.filter(i => i.status === 'fail')[0] || null;
});

const TXT = { wait: '待上传', hash: '校验中', run: '上传中', done: '已完成', fail: '失败', cancel: '已取消' };
function cls(s){ return TXT[s] || s; }
function pct(it){ return it.size ? Math.min(100, Math.round(it.done * 100 / it.size)) : 0; }
function speed(b){ return humanSize(b) + '/s'; }
function etaText(s){
  if(!s) return '';
  if(s < 60) return s + ' 秒';
  const m = Math.floor(s / 60), r = s % 60;
  return m < 60 ? m + ' 分' + (r ? ' ' + r + ' 秒' : '') : Math.floor(m / 60) + ' 小时';
}
function cancelAll(){
  if(window.confirm('确定取消本队列中所有未完成的上传？')) props.queue.cancelAll();
}
</script>

<template>
  <div class="fs-queue" v-if="list.length">
    <div class="fs-q-h">
      <b>上传队列</b>
      <span class="cnt" v-if="queue.title">{{ queue.title }}</span>
      <span class="sp"></span>
      <button class="btn ghost" v-if="st.busy && !queue.paused" @click="queue.pause()">暂停</button>
      <button class="btn ghost" v-else-if="st.busy" @click="queue.resume()">继续</button>
      <button class="btn ghost" v-if="st.fail && !st.busy" @click="queue.retryFailed()">重试全部失败</button>
      <button class="btn ghost" v-if="st.busy" @click="cancelAll">全部取消</button>
      <button class="btn ghost" v-else @click="queue.clearDone()">清理已完成</button>
    </div>

    <div class="fs-q-sum">
      <span>已完成 <b>{{ st.ok }}/{{ st.files }}</b></span>
      <span>进度 {{ st.pct }}%</span>
      <span>{{ humanSize(st.done) }} / {{ humanSize(st.total) }}</span>
      <span v-if="st.wait">{{ st.wait }} 个待传</span>
      <span class="bad" v-if="st.fail">{{ st.fail }} 个失败</span>
      <span class="warn" v-if="st.busy && st.bps">{{ speed(st.bps) }}</span>
      <span v-if="st.busy && st.eta">剩余 {{ etaText(st.eta) }}</span>
      <span class="warn" v-if="queue.paused && st.busy">已暂停</span>
      <span class="good" v-if="st.finished && !st.fail">全部完成</span>
    </div>

    <div class="fs-q-alert" v-if="firstError">
      <span class="err">上传失败：<b>{{ firstError.rel }}</b> · {{ firstError.error }}
        <template v-if="st.fail > 1">（另有 {{ st.fail - 1 }} 个文件失败）</template>
      </span>
      <button class="btnb btnb-txt" @click="queue.retryFailed()">重试全部失败</button>
    </div>

    <div class="fs-q-bar"><i :class="{ fail: st.fail && !st.busy }" :style="{ width: st.pct + '%' }"></i></div>

    <div class="fs-q-list">
      <div class="fs-q-item" :class="{ fail: it.status === 'fail' }" v-for="it in list" :key="it.key">
        <span class="nm" :title="it.rel">{{ it.rel }}</span>
        <span class="sz">{{ humanSize(it.size) }}</span>
        <span class="bar"><i :style="{ width: pct(it) + '%' }" :class="{ fail: it.status === 'fail' }"></i></span>
        <span class="st" :class="it.status">{{ cls(it.status) }}</span>
        <span class="ops">
          <button class="btnb" v-if="it.status === 'fail'" title="重试该文件" @click="queue.retry(it.key)">↻</button>
          <button class="btnb act-del" v-if="it.status !== 'done' && it.status !== 'cancel'"
                  title="取消该文件" @click="queue.cancel(it.key)">✕</button>
        </span>
        <span class="sub" v-if="it.stage && (it.status === 'run' || it.status === 'hash')">{{ it.stage }}</span>
        <span class="err" v-if="it.error">失败原因：{{ it.error }}</span>
      </div>
    </div>
  </div>
</template>
