<script setup>
import { computed } from 'vue'
import { AI, cancelTask } from '../stores/aiSummary'
import { STAGE_LABEL, fmtTime } from '../lib/aiSummaryCols'

const show = computed(() => AI.generating || (AI.task && !['DONE','PARTIAL','FAILED','CANCELED'].includes(AI.task.status)))
const pct = computed(() => Math.max(0, Math.min(100, Number(AI.task?.progress || 0))))
const stage = computed(() => AI.task?.stage_label || STAGE_LABEL[AI.task?.stage] || AI.task?.stage || '准备中')
const terminal = computed(() => ['FAILED','CANCELED'].includes(AI.task?.status))
</script>

<template>
  <div v-if="show" class="card ai-prog">
    <div class="card-h">
      <h2>生成进度</h2>
      <span class="hint">AI 在独立 Worker 中运行，不会阻塞台账</span>
      <span v-if="AI.degraded" class="pill ai-pill-warn" title="Redis 不可用，已自动切换为 3 秒轮询">轮询模式</span>
      <span v-else class="pill ai-pill-live">实时推送</span>
      <div class="spacer"></div>
      <button v-if="AI.generating && !terminal" class="btn ghost" @click="cancelTask()">取消</button>
    </div>
    <div class="ai-prog-line">
      <span class="ai-stage">{{ stage }}</span>
      <span class="ai-pct">{{ pct }}%</span>
    </div>
    <div class="ai-bar"><i :style="{width: pct + '%'}"></i></div>
    <div class="ai-meta">
      <span>任务 #{{ AI.task?.id || '—' }}</span>
      <span>状态 {{ AI.task?.status || 'PENDING' }}</span>
      <span v-if="AI.task?.total_files">文件 {{ AI.task.parsed_files || 0 }} / {{ AI.task.total_files }}</span>
      <span v-if="AI.task?.current_file">当前：{{ AI.task.current_file }}</span>
      <span v-if="AI.task?.started_at">起始 {{ fmtTime(AI.task.started_at) }}</span>
    </div>
    <div v-if="AI.task?.error_msg" class="ai-err">{{ AI.task.error_msg }}</div>
  </div>
</template>
