<script setup>
/* 设计系统占位页：框架已就绪，业务开发中。 */
import { SYSTEMS } from '@/config/systems'
const cfg = SYSTEMS.design;
</script>

<template>
  <div class="dv-wrap">
    <div class="dv-card">
      <div class="dv-icon">
        <svg viewBox="0 0 48 48" width="44" height="44" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M8 34 20 12l10 16 4-7 6 13Z"/><circle cx="34" cy="13" r="4"/>
        </svg>
      </div>
      <h2>{{ cfg.name }}</h2>
      <div class="dv-tag">{{ cfg.tagline }}</div>
      <p>{{ cfg.description }}</p>
      <ul>
        <li v-for="f in cfg.features" :key="f">{{ f }}</li>
      </ul>
      <div class="dv-note">设计部专属系统视图已接入统一鉴权与部门准入；业务模块开发中。</div>
    </div>
  </div>
</template>

<style scoped>
.dv-wrap{padding:40px 22px;display:flex;justify-content:center;}
.dv-card{max-width:560px;width:100%;background:#fff;border:1px solid var(--line);border-radius:16px;
  box-shadow:var(--sh1);padding:38px 34px;text-align:center;}
.dv-icon{width:76px;height:76px;margin:0 auto 18px;border-radius:18px;display:flex;align-items:center;justify-content:center;
  color:#7F77DD;background:#EEEDFE;border:1px solid #CECBF6;}
.dv-card h2{margin:0;font-size:22px;color:var(--pri);letter-spacing:1px;}
.dv-tag{font-size:12px;color:#7F77DD;margin-top:5px;}
.dv-card p{font-size:13px;color:var(--sub);line-height:1.8;margin:18px 0;}
.dv-card ul{list-style:none;margin:0 0 22px;padding:0;text-align:left;display:flex;flex-direction:column;gap:9px;}
.dv-card li{font-size:12.5px;color:var(--txt);padding-left:20px;position:relative;}
.dv-card li::before{content:"";position:absolute;left:2px;top:7px;width:6px;height:6px;border-radius:50%;background:#AFA9EC;}
.dv-note{font-size:11.5px;color:var(--sub);border-top:1px dashed var(--line);padding-top:16px;}
</style>
