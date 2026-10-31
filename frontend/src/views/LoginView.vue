<script setup>
/* ================================================================
   登录页（全屏）：左品牌区 + 右登录表单
   ================================================================ */
import { reactive } from 'vue'
import { auth, login } from '../stores/auth'
import { bootRemote, toast } from '../stores/bid'

const fm = reactive({ username: '', password: '' });

async function go(){
  if(auth.loading) return;
  if(!fm.username.trim() || !fm.password){ auth.err = '请输入账号和密码'; return; }
  auth.loading = true; auth.err = '';
  try{
    await login(fm.username.trim(), fm.password);
    await bootRemote();
    if(!auth.needLogin) toast('欢迎，' + ((auth.user && auth.user.displayName) || fm.username));
  }catch(e){ auth.err = e.message || '登录失败'; }
  finally { auth.loading = false; }
}
</script>

<template>
  <div class="login-wrap">
    <div class="login-card">
      <aside class="login-brand">
        <div class="lb-top">
          <div class="mark">亚</div>
          <div>
            <div class="lb-name">亚德彩投标台账</div>
            <div class="lb-sub">投标部 · 全流程协同台账</div>
          </div>
        </div>
        <div class="lb-body">
          <h2>投标全流程，<br>一处协同。</h2>
          <ul class="lb-list">
            <li>台账多人实时协同，15 秒自动同步</li>
            <li>首页看板掌握开标、中标与保证金</li>
            <li>按角色控制页面、字段与数据范围</li>
            <li>字段级操作留痕，越权写入自动拦截</li>
          </ul>
        </div>
        <div class="lb-foot">数据存于部门服务器 · 请使用本人账号登录</div>
      </aside>

      <section class="login-form">
        <h3>登录</h3>
        <div class="lf-hint">使用部门分配的账号登录系统</div>
        <form @submit.prevent="go">
          <div class="fld">
            <label for="lg-user">账号</label>
            <input id="lg-user" v-model="fm.username" type="text" autocomplete="username"
                   placeholder="请输入账号" autofocus>
          </div>
          <div class="fld">
            <label for="lg-pw">密码</label>
            <input id="lg-pw" v-model="fm.password" type="password" autocomplete="current-password"
                   placeholder="请输入密码">
          </div>
          <div v-if="auth.err" class="lf-err">{{ auth.err }}</div>
          <button class="btn pri lf-btn" type="submit" :disabled="auth.loading">
            {{ auth.loading ? '登录中…' : '登 录' }}
          </button>
        </form>
        <div class="lf-note">修改会记录操作人并实时同步给同事；如需开通账号请联系管理员。</div>
      </section>
    </div>
  </div>
</template>
