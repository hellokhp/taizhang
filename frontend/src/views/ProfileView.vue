<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '../api/client'
import { auth } from '../stores/auth'
import { can } from '../stores/perm'
import { toast } from '../stores/bid'

/* ============ 基础 ============ */
const YEAR = new Date().getFullYear();
const MONTH = new Date().getMonth() + 1;
const iso = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
const today = iso(new Date());

const me = computed(() => auth.user || {});
const catalog = ref({ perms: [] });
const stat = reactive({ year: 0, month: 0, loading: false });

/* ============ 资料展示（只读，账号信息由管理员维护） ============ */
const roleText = computed(() => me.value.isAdmin ? '管理员' : (me.value.roleName || me.value.roleKey || '—'));
const scopeText = computed(() => (me.value.dataScope || 'ALL') === 'SELF' ? '仅本人相关' : '全部数据');
const permList = computed(() => {
  const label = {};
  (catalog.value.perms || []).forEach(p => { label[p.key] = p; });
  return Object.keys(me.value.perms || {}).filter(k => me.value.perms[k]).map(k => ({
    key: k, label: (label[k] && label[k].label) || k, group: (label[k] && label[k].group) || ''
  }));
});
const pages = computed(() => permList.value.filter(p => p.group === '页面'));
const ops = computed(() => permList.value.filter(p => p.group !== '页面'));

/* ============ 重置密码 ============ */
const pwd = reactive({ old: '', new: '', again: '', show: false, saving: false });
const err = ref('');
const rules = computed(() => {
  const n = pwd.new;
  return {
    filled: !!pwd.old && !!n && !!pwd.again,
    len: n.length >= 6,
    diff: !!n && n !== pwd.old,
    same: !!pwd.again && n === pwd.again
  };
});
const canSave = computed(() => {
  const r = rules.value;
  return r.filled && r.len && r.diff && r.same && !pwd.saving;
});

async function savePwd(){
  if(!canSave.value) return;
  err.value = '';
  pwd.saving = true;
  try{
    await api('/api/me/password', { method:'POST', body: JSON.stringify({ old: pwd.old, new: pwd.new }) });
    pwd.old = ''; pwd.new = ''; pwd.again = ''; pwd.show = false;
    toast('密码已重置，其它设备需重新登录');
  }catch(e){ err.value = e.message || '密码修改失败'; }
  finally{ pwd.saving = false; }
}

/* ============ 加载 ============ */
async function loadMe(){
  try{
    const d = await api('/api/me');
    if(auth.user) Object.assign(auth.user, d.user);
    else auth.user = d.user;
  }catch(e){ /* 401 由 client 统一处理 */ }
}
async function loadCatalog(){
  try{ catalog.value = await api('/api/perm/catalog'); }catch(e){}
}
async function loadStat(){
  if(!can('view.reports') || !me.value.username) return;
  stat.loading = true;
  try{
    const d = await api('/api/reports?user=' + encodeURIComponent(me.value.username) + '&ym=' + YEAR);
    const mpre = YEAR + '-' + String(MONTH).padStart(2, '0');
    const list = (d.reports || []).filter(r => r.rdate <= today);
    stat.year = list.length;
    stat.month = list.filter(r => r.rdate.indexOf(mpre) === 0).length;
  }catch(e){}
  stat.loading = false;
}

onMounted(async () => { await loadMe(); loadCatalog(); loadStat(); });
</script>

<template>
  <div class="pf-wrap">
    <!-- 左：基本资料 -->
    <div class="card pf-main">
      <div class="card-h">
        <h2>基本资料</h2>
        <span class="hint">账号信息由管理员维护</span>
      </div>
      <div class="card-b">
        <div class="pf-hero">
          <div class="pf-avatar">{{ (me.displayName || me.username || '?').charAt(0) }}</div>
          <div class="pf-hero-t">
            <b>{{ me.displayName || me.username || '—' }}</b>
            <div class="pf-tags">
              <span class="pf-role" :class="{pri: !!me.isAdmin}">{{ roleText }}</span>
            </div>
          </div>
        </div>

        <div class="pf-grid">
          <div class="pf-item"><span class="k">账号</span><span class="v">{{ me.username || '—' }}</span></div>
          <div class="pf-item"><span class="k">姓名</span><span class="v">{{ me.displayName || '—' }}</span></div>
          <div class="pf-item"><span class="k">角色</span><span class="v">{{ roleText }}</span></div>
          <div class="pf-item"><span class="k">数据范围</span><span class="v">{{ scopeText }}</span></div>
          <div class="pf-item"><span class="k">已开通权限</span><span class="v">{{ permList.length }} 项</span></div>
          <div v-if="can('view.reports')" class="pf-item">
            <span class="k">本年日报</span>
            <span class="v">{{ stat.loading ? '…' : stat.year + ' 天' }}</span>
          </div>
          <div v-if="can('view.reports')" class="pf-item">
            <span class="k">{{ MONTH }} 月日报</span>
            <span class="v">{{ stat.loading ? '…' : stat.month + ' 天' }}</span>
          </div>
        </div>

        <div class="pf-sec">
          <div class="pf-sec-h">已开通的页面</div>
          <div class="pf-chips">
            <span v-for="p in pages" :key="p.key" class="pf-chip">{{ p.label }}</span>
            <span v-if="!pages.length" class="muted">无</span>
          </div>
        </div>

        <div class="pf-sec">
          <div class="pf-sec-h">已开通的操作</div>
          <div class="pf-chips">
            <span v-for="p in ops" :key="p.key" class="pf-chip">{{ p.label }}</span>
            <span v-if="!ops.length" class="muted">无</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 右：重置密码 -->
    <div class="card pf-pwd">
      <div class="card-h"><h2>重置密码</h2></div>
      <div class="card-b">
        <div class="fld" style="margin-bottom:12px;">
          <label>原密码</label>
          <div class="pf-pw">
            <input :type="pwd.show ? 'text' : 'password'" v-model="pwd.old"
              autocomplete="current-password" placeholder="请输入当前密码" />
          </div>
        </div>

        <div class="fld" style="margin-bottom:12px;">
          <label>新密码</label>
          <div class="pf-pw">
            <input :type="pwd.show ? 'text' : 'password'" v-model="pwd.new"
              autocomplete="new-password" placeholder="至少 6 位" />
            <button class="pf-eye" type="button" @click="pwd.show = !pwd.show">{{ pwd.show ? '隐藏' : '显示' }}</button>
          </div>
          <div class="fld-tip" :class="pwd.new ? (rules.len && rules.diff ? 'pf-ok' : 'pf-bad') : ''">
            <template v-if="!pwd.new">建议使用字母 + 数字组合，至少 6 位</template>
            <template v-else-if="!rules.len">新密码至少 6 位</template>
            <template v-else-if="pwd.old && !rules.diff">新密码不能与原密码相同</template>
            <template v-else>长度符合要求</template>
          </div>
        </div>

        <div class="fld" style="margin-bottom:10px;">
          <label>确认新密码</label>
          <div class="pf-pw">
            <input :type="pwd.show ? 'text' : 'password'" v-model="pwd.again"
              autocomplete="new-password" placeholder="再输入一次新密码" />
          </div>
          <div v-if="pwd.again" class="fld-tip" :class="rules.same ? 'pf-ok' : 'pf-bad'">
            {{ rules.same ? '两次输入一致' : '两次输入不一致' }}
          </div>
        </div>

        <div v-if="err" class="pf-err">{{ err }}</div>

        <button class="btn pri pf-save" :disabled="!canSave" @click="savePwd">
          {{ pwd.saving ? '提交中…' : '确认重置密码' }}
        </button>
        <div class="pf-note">重置后本账号在其它设备上的登录会立即失效，当前页面保持登录；如忘记原密码，请联系管理员在「用户管理」里重置。</div>
      </div>
    </div>
  </div>
</template>
