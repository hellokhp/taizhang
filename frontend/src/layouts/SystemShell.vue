<script setup>
/* ================================================================
   系统专属外壳：按当前系统渲染侧栏配色、品牌、菜单与顶栏。
   每套系统的视觉（theme）+ 菜单（SYSTEMS[key].nav）+ 顶栏系统切换器
   完全数据驱动，见 config/systems.js。页面内容由 router-view 承载。
   ================================================================ */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import logo from '@/assets/images/logo.png'
import NavIcon from '@/components/NavIcon.vue'
import SystemSwitcher from '@/components/SystemSwitcher.vue'
import { SYSTEMS } from '@/config/systems'
import { sys } from '@/stores/common/system'
import { auth, logout } from '@/stores/common/auth'
import { can, canAgg } from '@/stores/common/perm'
import { ST, bidFiles, filesState } from '@/stores/taizhang/bid'
import { buildAlerts } from '@/lib/alerts'

const route = useRoute();
const router = useRouter();

const cfg = computed(() => SYSTEMS[sys.current] || SYSTEMS.taizhang);

/* 菜单：按模块内细粒度权限过滤（noPerm 项只需能进系统即可） */
const menu = computed(() => (cfg.value.nav || []).filter(n => n.noPerm || !n.perm || can(n.perm)));

const pageTitle = computed(() => {
  const hit = (cfg.value.nav || []).find(n => n.path === route.path);
  return hit ? hit.title : '';
});

/* 台账侧栏角标：待处理数（仅台账系统 + 有看板聚合权限时计算） */
const pendingCnt = computed(() => {
  if(sys.current !== 'taizhang') return 0;
  if(!canAgg('bidStatus') || !canAgg('bondStatus') || !canAgg('perfStatus')) return 0;
  return buildAlerts(ST.rows, {
    bid: canAgg('bidStatus'), bond: canAgg('bondStatus'), perf: canAgg('perfStatus'), minor: false,
    bidFile: canAgg('bidStatus') && can('view.files') && filesState.loaded, files: bidFiles
  }).length;
});
function badgeOf(item){
  if(item.badge) return pendingCnt.value || 0;
  if(item.badgeRows) return sys.current === 'taizhang' ? ST.rows.length : 0;
  return 0;
}

/* 页脚说明：台账保留原有口径/协作说明，其它系统给通用说明 */
const footLines = computed(() => {
  if(sys.current === 'taizhang'){
    return {
      scope: '中标率 = 已出结果的中标数 ÷（中标 + 未中标 + 废标 + 流标），不含「待开标」「不投」「放弃」。',
      coop: '多人在线共同维护，数据保存在部门服务器，每 15 秒自动同步。'
    };
  }
  return {
    scope: cfg.value.description,
    coop: '本系统数据独立存储，按部门与角色控制访问；如需开通账号请联系管理员。'
  };
});
</script>

<template>
  <div class="shell" :class="'sys-' + cfg.theme">
    <aside class="sidebar">
      <div class="side-brand">
        <img class="mark logo-img" :src="logo" :alt="cfg.name">
        <div class="side-title">
          <h1>{{ cfg.name }}</h1>
          <div class="sub">{{ cfg.deptName }} · 公司系统平台</div>
        </div>
      </div>
      <nav class="nav">
        <button v-for="n in menu" :key="n.path" :class="{on: route.path === n.path}"
                @click="router.push(n.path)">
          <NavIcon :name="n.icon" />
          <span>{{ n.title }}</span>
          <span v-if="badgeOf(n)" class="badge">{{ badgeOf(n) }}</span>
        </button>
      </nav>
      <div class="side-foot">{{ cfg.tagline }}<br>数据存于部门服务器</div>
    </aside>

    <div class="main">
      <header class="topbar">
        <span class="page-title">{{ pageTitle }}</span>
        <div class="spacer"></div>
        <SystemSwitcher />
        <template v-if="auth.user">
          <span class="user-chip" role="button" title="个人中心" @click="router.push(cfg.base + '/profile')">
            <span class="avatar">{{ (auth.user.displayName || '?').charAt(0) }}</span>
            {{ auth.user.displayName }}
            <em v-if="auth.user.isAdmin">管理员</em>
            <em v-else-if="auth.user.roleName">{{ auth.user.roleName }}</em>
          </span>
          <button class="btn ghost" @click="logout()">退出登录</button>
        </template>
      </header>
      <main class="content">
        <router-view />
        <div class="foot-note" v-if="sys.current === 'taizhang'">
          <div><b>口径说明</b>{{ footLines.scope }}</div>
          <div><b>协作方式</b>{{ footLines.coop }}</div>
        </div>
      </main>
    </div>
  </div>
</template>

<style scoped>
/* 三套系统主题：覆盖侧栏渐变与选中强调色，页面区样式沿用全局 */
.sys-blue .sidebar{
  background:radial-gradient(520px 300px at 92% -8%, rgba(176,138,68,.20), rgba(176,138,68,0) 62%),
             linear-gradient(166deg,#0D1B2B 0%,#152C44 48%,#1D3A57 100%);
}
.sys-green .sidebar{
  background:radial-gradient(520px 300px at 92% -8%, rgba(93,202,165,.18), rgba(93,202,165,0) 62%),
             linear-gradient(166deg,#0C1F1B 0%,#14352E 48%,#1D5142 100%);
}
.sys-purple .sidebar{
  background:radial-gradient(520px 300px at 92% -8%, rgba(175,169,236,.18), rgba(175,169,236,0) 62%),
             linear-gradient(166deg,#17152B 0%,#282449 48%,#3A3568 100%);
}
.sys-slate .sidebar{
  background:radial-gradient(520px 300px at 92% -8%, rgba(216,182,120,.20), rgba(216,182,120,0) 62%),
             linear-gradient(166deg,#1A1D21 0%,#2A2F36 48%,#3A414A 100%);
}
.sys-green.shell :deep(.nav button.on){box-shadow:inset 2px 0 0 #5DCAA5;}
.sys-purple.shell :deep(.nav button.on){box-shadow:inset 2px 0 0 #AFA9EC;}
.sys-slate.shell :deep(.nav button.on){box-shadow:inset 2px 0 0 #D8B678;}
</style>
