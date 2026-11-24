<script setup>
import { onMounted, onUnmounted } from 'vue'
import SystemShell from '@/layouts/SystemShell.vue'
import EditDialog from '@/components/EditDialog.vue'
import DetailDialog from '@/components/DetailDialog.vue'
import ImportDialog from '@/components/ImportDialog.vue'
import BidUploadDialog from '@/components/BidUploadDialog.vue'
import ToastBox from '@/components/ToastBox.vue'
import UploadDock from '@/components/fs/UploadDock.vue'
import LoginView from '@/views/common/LoginView.vue'
import { dlg, imp, boot } from '@/stores/taizhang/bid'
import { auth } from '@/stores/common/auth'
import { um } from '@/stores/common/users'

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
  <SystemShell v-else />
  <EditDialog />
  <DetailDialog />
  <ImportDialog />
  <BidUploadDialog />
  <ToastBox />
  <!-- 全局上传任务面板：文件存储页 / 标段附件上传的所有任务都汇总在这里，
       切页或关窗都不中断，进度与失败常驻可见 -->
  <UploadDock />
</template>
