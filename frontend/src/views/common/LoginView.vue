<script setup>
/* ================================================================
   登录页（全屏）：账号密码 → 部门系统选择 → 进入专属视图
   系统列表按账号部门权限动态显示（只显示有权系统），管理部可见全部。
   ================================================================ */
import { reactive, ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { auth, login } from '@/stores/common/auth'
import { bootRemote, toast } from '@/stores/taizhang/bid'
import { api } from '@/api/client'
import { availableSystems, setCurrent, landingSystem } from '@/stores/common/system'
import { SYSTEMS, homeOf } from '@/config/systems'
import logo from '@/assets/images/logo.png'

const router = useRouter();
const fm = reactive({ username: '', password: '' });
const step = ref(1);                 // 1=账号密码 2=选择系统
const picking = ref('');             // 正在进入的系统 key

async function go(){
  if(auth.loading) return;
  if(!fm.username.trim() || !fm.password){ auth.err = '请输入账号和密码'; return; }
  auth.loading = true; auth.err = '';
  try{
    await login(fm.username.trim(), fm.password);
    /* 登录成功但暂不进入：先拿用户信息，算有权系统 */
    const me = await api('/api/me');
    auth.user = me.user;
    const avail = availableSystems();
    if(avail.length === 1){
      await enter(avail[0].key);
    }else if(avail.length > 1){
      picking.value = landingSystem();
      step.value = 2;
    }else{
      throw new Error('该账号未分配任何系统权限，请联系管理员');
    }
  }catch(e){
    auth.err = e.message || '登录失败';
    if(auth.err.indexOf('部门') >= 0 || auth.err.indexOf('无权') >= 0) step.value = 1;
  }finally{ auth.loading = false; }
}

async function enter(key){
  if(picking.value && picking.value !== key && auth.loading) return;
  picking.value = key;
  auth.loading = true; auth.err = '';
  try{
    setCurrent(key);                       // 记住并选择目标系统
    await bootRemote();                    // 加载用户态与（若有）台账数据
    if(!auth.needLogin){
      toast('欢迎进入' + (SYSTEMS[key]?.name || '系统') + '，' + ((auth.user && auth.user.displayName) || fm.username));
      const target = homeOf(key);
      if(router.currentRoute.value.path !== target) router.push(target);
    }
  }catch(e){ auth.err = e.message || '进入系统失败'; step.value = 1; }
  finally{ auth.loading = false; }
}

function back(){ step.value = 1; auth.err = ''; }
const themeClass = computed(() => {
  const key = picking.value || landingSystem();
  return 'theme-' + (SYSTEMS[key]?.theme || 'blue');
});
</script>

<template>
  <div class="login-wrap">
    <div class="login-card" :class="themeClass">
      <aside class="login-brand">
        <div class="lb-top">
          <img class="mark logo-img" :src="logo" alt="亚德彩公司系统平台">
          <div>
            <div class="lb-name">亚德彩公司系统平台</div>
            <div class="lb-sub">多部门协同 · 统一鉴权</div>
          </div>
        </div>
        <div class="lb-body">
          <h2 v-if="step === 1">统一登录，<br>按部门进入系统。</h2>
          <h2 v-else>选择要进入的<br>部门系统。</h2>
          <ul class="lb-list" v-if="step === 1">
            <li>投标台账、财务、设计、管理中心统一入口</li>
            <li>按部门权限显示可进入的系统</li>
            <li>管理部可在多套系统间自由切换</li>
            <li>管理中心跨系统统一管理用户与角色</li>
          </ul>
          <ul class="lb-list" v-else>
            <li>仅显示当前账号有权进入的系统</li>
            <li>各系统数据隔离、专属视图</li>
            <li>如需切换系统，登录后可在顶栏切换</li>
          </ul>
        </div>
        <div class="lb-foot">数据存于部门服务器 · 请使用本人账号登录</div>
      </aside>

      <section class="login-form">
        <template v-if="step === 1">
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
              {{ auth.loading ? '登录中…' : '下一步' }}
            </button>
          </form>
          <div class="lf-note">修改会记录操作人并实时同步；如需开通账号请联系管理员。</div>
        </template>

        <template v-else>
          <h3>选择系统</h3>
          <div class="lf-hint">已验证账号 · 请选择要进入的部门系统</div>
          <div class="sys-pick">
            <button v-for="s in availableSystems()" :key="s.key"
                    :class="['pick-card', 'pc-' + s.theme, {sel: picking === s.key}]"
                    @click="picking = s.key" @dblclick="enter(s.key)">
              <span class="pc-dot"></span>
              <span class="pc-name">{{ s.name }}</span>
              <span class="pc-dept">{{ s.deptName }}</span>
            </button>
          </div>
          <div v-if="auth.err" class="lf-err">{{ auth.err }}</div>
          <button class="btn pri lf-btn" :disabled="auth.loading || !picking" @click="enter(picking)">
            {{ auth.loading ? '进入中…' : '进入系统' }}
          </button>
          <button class="btn ghost lf-back" @click="back">返回重新登录</button>
          <div class="lf-note">登录后可在顶部「系统切换」中随时切换到其它有权系统。</div>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.theme-green .login-brand{background:linear-gradient(165deg,#0C1F1B 0%,#14352E 55%,#1D5142 100%);}
.theme-purple .login-brand{background:linear-gradient(165deg,#17152B 0%,#282449 55%,#3A3568 100%);}
.theme-slate .login-brand{background:linear-gradient(165deg,#1A1D21 0%,#2A2F36 55%,#3A414A 100%);}
.sys-pick{display:flex;flex-direction:column;gap:9px;margin:2px 0 16px;}
.pick-card{
  display:flex;align-items:center;gap:10px;width:100%;padding:11px 12px;cursor:pointer;
  border:1px solid var(--line);border-radius:10px;background:#fff;font-family:inherit;text-align:left;
  transition:.16s var(--ease);
}
.pick-card:hover{border-color:var(--gold);}
.pick-card.sel{border-color:var(--gold);background:rgba(216,182,120,.08);box-shadow:0 0 0 2px rgba(216,182,120,.15);}
.pc-dot{width:10px;height:10px;border-radius:50%;flex:none;}
.pc-blue .pc-dot{background:#378ADD;}
.pc-green .pc-dot{background:#1D9E75;}
.pc-purple .pc-dot{background:#7F77DD;}
.pc-slate .pc-dot{background:#D8B678;}
.pc-name{font-size:13px;font-weight:600;color:var(--pri);}
.pc-dept{font-size:10.5px;color:var(--sub);margin-left:auto;}
.lf-back{width:100%;margin-top:9px;}
</style>
