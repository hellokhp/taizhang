<script setup>
/* ================================================================
   模式 D · 下浮率 / 折扣率报价专用结果视图
   数据来自 ai_summary.raw_result.discount（经 lib/aiDiscount.js 适配）
   与独立 CLI 生成的 Excel 对齐：规则 + 排名 + 产品清单 + 规则原文
   ================================================================ */
import { ref, computed } from 'vue'
import { AI } from '../stores/aiSummary'
import {
  parseDiscount, fmtMoney, fmtNum, fmtPct, fmtSigned, fmtScore,
} from '../lib/aiDiscount'

const vm = computed(() => parseDiscount(AI.summary))
const showRule = ref(true)
const showProducts = ref(false)
const showRuleText = ref(false)
const prodQuery = ref('')

const products = computed(() => {
  const q = prodQuery.value.trim().toLowerCase()
  if (!q) return vm.value.products
  return vm.value.products.filter(p =>
    [p.seq, p.name, p.spec, p.unit].some(x => String(x || '').toLowerCase().includes(q)))
})

const kpis = computed(() => {
  const v = vm.value
  if (!v.ok) return []
  const r = v.rule
  return [
    { label: '评标基准价', value: r.benchmark === null ? '—' : fmtNum(r.benchmark, 6), sub: '评审价 = 折扣' },
    { label: '参与基准价', value: (v.totals.benchmarkCount ?? '—') + ' 家', sub: `共 ${v.totals.bidderCount} 家投标` },
    { label: '价格分满分', value: r.fullScore ?? '—', sub: '报价部分 50 分制' },
    { label: '扣分系数', value: `${r.aboveDeduct ?? '—'} / ${r.belowDeduct ?? '—'}`, sub: '高于 / 低于基准价 每 1%' },
    { label: '去高去低', value: `${r.trimHigh ?? 0} 高 ${r.trimLow ?? 0} 低`, sub: `有效报价 > ${r.trimGuard ?? 7} 家时启用` },
    { label: '产品清单', value: v.totals.productCount + ' 项', sub: '来自招标文件项目需求' },
    { label: 'Σ(限价×预估量)', value: fmtMoney(v.totals.capTotal ?? v.totals.capSum), sub: '元 · 与最高限价核对' },
    { label: '预估结算价区间', value: vm.value.bidders.length
        ? `${fmtMoney(Math.min(...vm.value.bidders.map(b => b.estTotal || 0)), 0)} ~ ${fmtMoney(Math.max(...vm.value.bidders.map(b => b.estTotal || 0)), 0)}`
        : '—', sub: '元 · 按折扣估算' },
  ]
})
</script>

<template>
  <div v-if="vm.ok" class="ai-d">
    <!-- 关键指标 -->
    <div class="card ai-d-kpi-card">
      <div class="card-h"><h2>评审关键指标</h2>
        <span class="hint">报价口径：{{ vm.rule.rateLabel }} · 模式 discount</span>
        <div class="spacer"></div>
        <span class="pill" :class="vm.rule.confirmed ? 'ai-pill-live' : 'ai-pill-warn'">
          {{ vm.rule.confirmed ? '规则已交叉校验' : '规则待确认' }}
        </span>
      </div>
      <div class="ai-d-kpis">
        <div v-for="(k, i) in kpis" :key="i" class="ai-d-kpi">
          <span>{{ k.label }}</span><b>{{ k.value }}</b><small>{{ k.sub }}</small>
        </div>
      </div>
    </div>

    <!-- 评审规则 -->
    <div class="card ai-d-rule-card">
      <div class="card-h">
        <h2>评标办法与计分口径</h2>
        <span class="hint">数值均由招标文件原文直取，千问抽取作交叉校验</span>
        <div class="spacer"></div>
        <button class="btn ghost" @click="showRule = !showRule">{{ showRule ? '收起' : '展开' }}</button>
      </div>
      <div v-if="showRule" class="ai-d-rule-body">
        <div class="ai-d-defs">
          <div><span>报价口径</span><b>{{ vm.rule.discountDef || '—' }}</b></div>
          <div><span>评审价定义</span><b>{{ vm.rule.reviewPriceDef || '—' }}</b></div>
          <div><span>中标单价定义</span><b>{{ vm.rule.unitPriceDef || '—' }}</b></div>
          <div v-if="vm.rule.settlementDef"><span>结算定义</span><b>{{ vm.rule.settlementDef }}</b></div>
          <div><span>最高限价</span><b>{{ fmtMoney(vm.rule.statedCap) }} 元<template v-if="vm.rule.capLimitPct !== null"> · 折扣限价 {{ fmtPct(vm.rule.capLimitPct, 0) }}%</template></b></div>
          <div><span>开标一览表</span><b>{{ vm.rule.openingFile || '—' }}</b></div>
        </div>
        <div class="ai-d-block">
          <div class="ai-d-block-h">评标基准价取法</div>
          <p>{{ vm.rule.benchmarkRule || '—' }}</p>
        </div>
        <div class="ai-d-block">
          <div class="ai-d-block-h">价格分计算公式</div>
          <p>{{ vm.rule.formula || '—' }}</p>
        </div>
        <div class="ai-d-meta">
          <span v-if="vm.rule.basis">✓ {{ vm.rule.basis }}</span>
          <span v-if="vm.rule.ruleSourceFile">规则来源：{{ vm.rule.ruleSourceFile }} · P{{ vm.rule.ruleSourcePage }}</span>
          <span v-if="vm.rule.llmPages.length">定位页：P{{ vm.rule.llmPages.join('、P') }}</span>
          <span v-if="vm.totals.tokenIn !== null">Token：{{ fmtMoney(vm.totals.tokenIn, 0) }} 入 / {{ fmtMoney(vm.totals.tokenOut, 0) }} 出</span>
        </div>
      </div>
    </div>

    <!-- 投标人排名 -->
    <div class="card ai-d-rank-card">
      <div class="card-h">
        <h2>开标结果 · 价格评审排名</h2>
        <span class="hint">按价格分降序 · 预估结算价 = 最高限价 × 折扣（元）</span>
        <div class="spacer"></div>
        <span class="cnt">{{ vm.bidders.length }} 家投标人</span>
      </div>
      <div class="ai-d-tbl-wrap">
        <table class="ai-d-tbl">
          <colgroup>
            <col style="width:56px"><col style="width:23%"><col style="width:88px">
            <col style="width:82px"><col style="width:88px"><col style="width:88px">
            <col style="width:82px"><col style="width:140px"><col style="width:130px">
          </colgroup>
          <thead><tr>
            <th>排名</th><th>投标单位</th><th>{{ vm.rule.rateLabel }}(%)</th>
            <th>折扣</th><th>每百报价</th><th>偏差(%)</th>
            <th>价格分</th><th>预估结算价（元）</th><th>标记</th>
          </tr></thead>
          <tbody>
            <tr v-for="b in vm.bidders" :key="b.key" :class="{ 'ai-d-top': b.rank === 1, 'ai-d-trim': !b.inBenchmark }">
              <td class="ai-d-num ai-d-rank">{{ b.rank }}</td>
              <td class="ai-d-name" :title="b.name">{{ b.name }}</td>
              <td class="ai-d-num">{{ fmtPct(b.rate, 2) }}</td>
              <td class="ai-d-num">{{ fmtNum(b.discount, 4) }}</td>
              <td class="ai-d-num">{{ fmtNum(b.per100, 2) }}</td>
              <td class="ai-d-num" :class="(b.deviation || 0) > 0 ? 'ai-d-pos' : 'ai-d-neg'">{{ fmtSigned(b.deviation, 2) }}</td>
              <td class="ai-d-num ai-d-score">{{ fmtScore(b.priceScore) }}</td>
              <td class="ai-d-num ai-d-money">{{ fmtMoney(b.estTotal) }}</td>
              <td>
                <span v-if="!b.inBenchmark" class="pill ai-pill-warn" title="基准价计算时剔除，但仍参与价格评分">基准价剔除</span>
                <span v-else-if="!b.valid" class="pill ai-pill-bad">无效报价</span>
                <span v-else-if="b.overCap" class="pill ai-pill-bad">超限价</span>
                <span v-else class="ai-d-ok">正常</span>
              </td>
            </tr>
          </tbody>
          <tfoot><tr>
            <td colspan="7" class="ai-d-sum-label">最高限价 / 基准价</td>
            <td class="ai-d-num ai-d-money">{{ fmtMoney(vm.totals.capTotal ?? vm.totals.capSum) }}</td>
            <td class="ai-d-num">{{ fmtNum(vm.rule.benchmark, 6) }}</td>
          </tr></tfoot>
        </table>
      </div>
      <div v-if="vm.notes.length || vm.warnings.length" class="ai-d-notes">
        <div class="ai-d-notes-h">待复核 / 提示</div>
        <div v-for="(n, i) in vm.notes" :key="'n' + i" class="ai-d-note">· <b>{{ n.name }}</b>：{{ n.text }}</div>
        <div v-for="(w, i) in vm.warnings" :key="'w' + i" class="ai-d-note">· {{ w }}</div>
      </div>
    </div>

    <!-- 产品清单 -->
    <div class="card ai-d-prod-card">
      <div class="card-h">
        <h2>项目需求 · 产品清单</h2>
        <span class="hint">{{ vm.products.length }} 项 · Σ(限价×预估量) = {{ fmtMoney(vm.totals.capSum) }} 元</span>
        <div class="spacer"></div>
        <input v-if="showProducts" v-model="prodQuery" class="ai-d-search" placeholder="搜索品名 / 规格" style="width:170px">
        <button class="btn ghost" @click="showProducts = !showProducts">
          {{ showProducts ? '收起清单' : `展开 ${vm.products.length} 项` }}
        </button>
      </div>
      <div v-if="showProducts" class="ai-d-tbl-wrap ai-d-prod-wrap">
        <table class="ai-d-tbl ai-d-prod-tbl">
          <colgroup>
            <col style="width:52px"><col style="width:17%"><col style="width:40%">
            <col style="width:56px"><col style="width:86px"><col style="width:110px">
            <col style="width:130px"><col style="width:60px">
          </colgroup>
          <thead><tr>
            <th>序号</th><th>品名</th><th>规格 / 技术要求</th><th>单位</th>
            <th>预估量</th><th>单价限价（元）</th><th>限价小计（元）</th><th>来源页</th>
          </tr></thead>
          <tbody>
            <tr v-for="p in products" :key="p.key">
              <td class="ai-d-num">{{ p.seq }}</td>
              <td class="ai-d-name">{{ p.name }}</td>
              <td class="ai-d-spec" :title="p.spec">{{ p.spec }}</td>
              <td>{{ p.unit || '—' }}</td>
              <td class="ai-d-num">{{ fmtMoney(p.qty, 2) }}</td>
              <td class="ai-d-num">{{ fmtMoney(p.cap) }}</td>
              <td class="ai-d-num ai-d-money">{{ fmtMoney(p.amount) }}</td>
              <td class="ai-d-num">{{ p.sourcePage ? 'P' + p.sourcePage : '—' }}</td>
            </tr>
            <tr v-if="!products.length"><td colspan="8" class="ai-empty">没有匹配的产品</td></tr>
          </tbody>
          <tfoot><tr>
            <td colspan="6" class="ai-d-sum-label">合计（{{ products.length }} 项）</td>
            <td class="ai-d-num ai-d-money">{{ fmtMoney(products.reduce((s, p) => s + (p.amount || 0), 0)) }}</td>
            <td></td>
          </tr></tfoot>
        </table>
      </div>
    </div>

    <!-- 评分规则原文 -->
    <div class="card ai-d-raw-card">
      <div class="card-h">
        <h2>招标文件评分规则原文</h2>
        <span class="hint">用于人工核对 AI 抽取的计分口径</span>
        <div class="spacer"></div>
        <button class="btn ghost" @click="showRuleText = !showRuleText">{{ showRuleText ? '收起原文' : '查看原文' }}</button>
      </div>
      <pre v-if="showRuleText" class="ai-d-raw">{{ vm.rule.ruleText || '—' }}</pre>
    </div>
  </div>
</template>
