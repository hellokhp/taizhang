<script setup>
import { imp, impStat, confirmImport } from '../stores/bid'
</script>

<template>
  <div class="mask" :class="{on: imp.open}" @click.self="imp.open = false">
    <div class="dlg" style="width:660px">
      <div class="dlg-h"><h3>导入预览</h3><span class="act" style="color:var(--mut)" @click="imp.open = false">关闭 ✕</span></div>
      <div class="dlg-b">
        <div style="font-size:12.5px;color:var(--mut);margin-bottom:10px;">文件：<b style="color:var(--txt)">{{ imp.name }}</b> · 共识别 <b style="color:var(--txt)">{{ impStat.total }}</b> 行</div>
        <div class="imp-grid">
          <div class="imp-stat"><b>{{ impStat.total - impStat.noLead }}</b> 行已分配负责人</div>
          <div class="imp-stat warn"><b>{{ impStat.noLead }}</b> 行无负责人</div>
          <div class="imp-stat" :class="{warn: impStat.noBid > 0}"><b>{{ impStat.noBid }}</b> 行标注不投（不计废标）</div>
          <div class="imp-stat ok"><b>{{ impStat.sel }}</b> 行将处理</div>
        </div>
        <div style="margin-top:12px">
          <label class="imp-chk"><input type="checkbox" v-model="imp.opt.needLead"><span>只导入已分配标书负责人的项目 —— 没写负责人的先挂起，等他填上再导一次即可自动补进来</span></label>
          <label class="imp-chk"><input type="checkbox" v-model="imp.opt.skipNo"><span>跳过「不投」的项目 —— 不投不等于废标，跳过后完全不进表；不勾则按「不投」状态入表留痕，但同样不计入投标数与废标率</span></label>
          <label class="imp-chk"><input type="checkbox" v-model="imp.opt.overwrite"><span>同编号项目用新数据覆盖更新（不勾则已存在的直接跳过）</span></label>
        </div>
        <div class="imp-sum">本次将 <b class="red">新增 {{ impStat.add }}</b> 条，<b style="color:var(--pri)">更新 {{ impStat.upd }}</b> 条<span v-if="!impStat.sel" style="color:var(--warn)"> · 当前没有可导入的行</span></div>
      </div>
      <div class="dlg-f">
        <button class="btn" @click="imp.open = false">取消</button>
        <button class="btn pri" @click="confirmImport()">确认导入</button>
      </div>
    </div>
  </div>
</template>
