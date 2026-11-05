<script setup>
/* 上传队列：整体进度 + 单文件进度 + 暂停/继续/重试/取消 */
import { computed, onUnmounted, ref } from 'vue'
import { humanSize } from '../../lib/fsPerm'

const props = defineProps({ queue: { type: Object, required: true } });
const emit = defineEmits(['done']);
const tick = ref(0);

props.queue.onChange = () => { tick.value++; };
props.queue.onFinish = () => { emit('done'); };
const timer = setInterval(() => { tick.value++; }, 600);
onUnmounted(() => clearInterval(timer));

const st = computed(() => { tick.value; return props.queue.stats(); });
const list = computed(() => { tick.value; return props.queue.items; });

function cls(s){ return { wait: '待传', run: '进行中', done: '已完成', fail: '失败', cancel: '已取消' }[s] || s; }
function pct(it){ return it.size ? Math.round(it.done * 100 / it.size) : 0; }
</script>

<template>
  <div class="fs-queue" v-if="list.length">
    <div class="fs-q-h">
      <b>上传队列</b>
      <span class="cnt">{{ st.ok }}/{{ list.length }} 完成 · {{ st.pct }}% ·
        {{ humanSize(st.done) }} / {{ humanSize(st.total) }}</span>
      <span class="sp"></span>
      <button class="btn ghost" v-if="!queue.paused" @click="queue.pause()">暂停</button>
      <button class="btn ghost" v-else @click="queue.resume()">继续</button>
      <button class="btn ghost" @click="queue.clearDone()">清理已完成</button>
    </div>
    <div class="fs-q-bar"><i :style="{ width: st.pct + '%' }"></i></div>
    <div class="fs-q-list">
      <div class="fs-q-item" v-for="it in list" :key="it.key">
        <span class="nm" :title="it.rel">{{ it.rel }}</span>
        <span class="sz">{{ humanSize(it.size) }}</span>
        <span class="bar"><i :style="{ width: pct(it) + '%' }" :class="{ fail: it.status === 'fail' }"></i></span>
        <span class="st" :class="it.status">{{ cls(it.status) }}</span>
        <span class="ops">
          <button class="btnb" v-if="it.status === 'fail'" @click="queue.retry(it.key)" title="重试">↻</button>
          <button class="btnb act-del" v-if="it.status !== 'done'" @click="queue.cancel(it.key)" title="取消">✕</button>
        </span>
        <span class="err" v-if="it.error">{{ it.error }}</span>
      </div>
    </div>
  </div>
</template>
