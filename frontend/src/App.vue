<script setup>
import { onMounted, onUnmounted, computed } from 'vue'
import { useRoute } from 'vue-router'
import NavTabs from './components/NavTabs.vue'
import logo from './assets/images/logo.png'
import EditDialog from './components/EditDialog.vue'
import DetailDialog from './components/DetailDialog.vue'
import ImportDialog from './components/ImportDialog.vue'
import BidUploadDialog from './components/BidUploadDialog.vue'
import ToastBox from './components/ToastBox.vue'
import UploadDock from './components/fs/UploadDock.vue'
import LoginView from './views/LoginView.vue'
import { dlg, imp, boot } from './stores/bid'
import { auth, logout } from './stores/auth'
import { um } from './stores/users'

const route = useRoute();
const pageTitle = computed(() => ({'/':'首页看板','/manage':'投标管理','/timeline':'标源总览','/assign':'项目分配表','/alerts':'需要处理','/files':'文件存储','/users':'用户管理','/roles':'角色权限','/audit':'审计日志','/reports':'工作日报','/profile':'个人中心'}[route.path] || ''));

function onKey(e){
  if(e.key === 'Escape'){
    if(imp.open) imp.open = false;
    else if(um.open) um.open = false;
  }
  if((e.ctrlKey || e.metaKey) && e.key === 'Enter' && dlg.open){
    const btn = document.querySelector('.mask.on .dlg-f .btn.pri');
    if(btn) btn.click();
  }
}
onMounted(() => {
  document.addEventListener('keydown', onKey);
  boot();          // 有 token 则拉取服务器数据并启动轮询，无 token 则弹登录框
});
onUnmounted(() => document.removeEventListener('keydown', onKey));
</script>

<template>
  <LoginView v-if="auth.needLogin" />
  <div v-else class="shell">
    <aside class="sidebar">
      <div class="side-brand">
        <img class="mark logo-img" :src="logo" alt="亚德彩投标台账">
        <div class="side-title">
          <h1>亚德彩投标台账</h1>
          <div class="sub">投标部 · 全流程协同台账</div>
        </div>
      </div>
      <NavTabs />
      <div class="side-foot">数据存于部门服务器<br>每 15 秒自动同步</div>
    </aside>
    <div class="main">
      <header class="topbar">
        <span class="page-title">{{ pageTitle }}</span>
        <div class="spacer"></div>
        <template v-if="auth.user">
          <span class="user-chip" role="button" title="个人中心" @click="$router.push('/profile')">
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
        <div class="foot-note">
          <div><b>口径说明</b>中标率 = 已出结果的中标数 ÷（中标 + 未中标 + 废标 + 流标），不含「待开标」「不投」「放弃」；「不投」（报名后决定不投标）自成一类，既不算投标数也不算废标，仅单独计数展示；「本月」按看板顶部所选统计口径归月。保证金「待退」由系统自动推导（已缴纳且已出结果），超过 30 天未退自动进入预警。</div>
          <div><b>协作方式</b>多人在线共同维护：用各自账号登录后直接新增/编辑/导入，数据保存在部门服务器，页面每 15 秒自动同步同事的修改；「导出 Excel」用于对外报送与备份。</div>
        </div>
      </main>
    </div>
  </div>
  <EditDialog />
  <DetailDialog />
  <ImportDialog />
  <BidUploadDialog />
  <ToastBox />
  <!-- 全局上传任务面板：文件存储页 / 标段附件上传的所有任务都汇总在这里，
       切页或关窗都不中断，进度与失败常驻可见 -->
  <UploadDock />
</template>
