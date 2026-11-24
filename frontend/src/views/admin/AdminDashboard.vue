<script setup>
/* 管理中心看板：全局用户/角色统计与分布。数据来自 /api/admin/overview（仅管理部）。*/
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '@/api/client'
import { SYSTEMS } from '@/config/systems'

const router = useRouter();
const data = ref(null);
const err = ref('');

const deptLabel = {bid:'投标部', finance:'财务部', design:'设计部', management:'管理部'};
const sysLabel = {taizhang:'投标台账', finance:'财务管理', design:'设计管理', admin:'管理中心', '':'未归属'};

const maxDept = computed(() => Math.max(1, ...(data.value?.usersByDept || []).map(x => x.count)));
const maxSys = computed(() => Math.max(1, ...(data.value?.rolesBySystem || []).map(x => x.count)));

onMounted(async () => {
  try{ data.value = await api('/api/admin/overview'); }
  catch(e){ err.value = e.message || '加载失败'; }
});
</script>

<template>
  <div>
    <div class="card" style="margin-bottom:14px;">
      <div class="card-h" style="padding-bottom:12px;">
        <h2>管理看板</h2>
        <span class="hint">全平台视图 · 数据跨所有子系统汇总</span>
      </div>
      <div class="card-b">
        <div v-if="err" style="color:var(--win)">{{ err }}</div>
        <div v-else-if="!data" style="color:var(--sub)">加载中…</div>
        <template v-else>
          <div class="kpi-row">
            <div class="kpi">
              <div class="kpi-l">用户总数</div>
              <div class="kpi-v">{{ data.totalUsers }}</div>
              <div class="kpi-s">跨全部部门</div>
            </div>
            <div class="kpi">
              <div class="kpi-l">角色总数</div>
              <div class="kpi-v">{{ data.totalRoles }}</div>
              <div class="kpi-s">跨全部系统</div>
            </div>
            <div class="kpi">
              <div class="kpi-l">管理员</div>
              <div class="kpi-v">{{ data.adminCount }}</div>
              <div class="kpi-s">超级管理员账号</div>
            </div>
            <div class="kpi kpi-act" @click="router.push('/admin/users')">
              <div class="kpi-l">用户管理 →</div>
              <div class="kpi-v" style="font-size:20px">管理全部</div>
              <div class="kpi-s">按部门分类查看</div>
            </div>
            <div class="kpi kpi-act" @click="router.push('/admin/roles')">
              <div class="kpi-l">角色权限 →</div>
              <div class="kpi-v" style="font-size:20px">配置全部</div>
              <div class="kpi-s">按系统分类配置</div>
            </div>
          </div>

          <div class="dist-grid">
            <div class="dist">
              <div class="dist-h">用户按部门分布</div>
              <div v-for="x in (data.usersByDept || [])" :key="x.key" class="dist-row">
                <span class="dist-l">{{ deptLabel[x.key] || x.key }}</span>
                <span class="dist-bar"><i :style="{width: (x.count / maxDept * 100) + '%'}"></i></span>
                <span class="dist-n">{{ x.count }}</span>
              </div>
              <div v-if="!(data.usersByDept || []).length" class="dist-empty">暂无数据</div>
            </div>
            <div class="dist">
              <div class="dist-h">角色按系统分布</div>
              <div v-for="x in (data.rolesBySystem || [])" :key="x.key" class="dist-row">
                <span class="dist-l">{{ sysLabel[x.key] || x.key }}</span>
                <span class="dist-bar"><i :style="{width: (x.count / maxSys * 100) + '%'}"></i></span>
                <span class="dist-n">{{ x.count }}</span>
              </div>
              <div v-if="!(data.rolesBySystem || []).length" class="dist-empty">暂无数据</div>
            </div>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.kpi-row{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;}
.kpi{background:var(--bg-2,#F7F6F2);border:1px solid var(--line);border-radius:12px;padding:16px;}
.kpi-l{font-size:12px;color:var(--sub);}
.kpi-v{font-size:32px;font-weight:700;color:var(--pri);line-height:1.3;}
.kpi-s{font-size:11px;color:var(--mut);}
.kpi-act{cursor:pointer;transition:.16s var(--ease);}
.kpi-act:hover{border-color:var(--gold);background:rgba(216,182,120,.08);}
.dist-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:18px;}
.dist{background:var(--bg-2,#F7F6F2);border:1px solid var(--line);border-radius:12px;padding:16px;}
.dist-h{font-size:13px;font-weight:600;margin-bottom:12px;color:var(--pri);}
.dist-row{display:flex;align-items:center;gap:10px;margin-bottom:9px;}
.dist-l{width:64px;font-size:12px;color:var(--sub);flex:none;}
.dist-bar{flex:1;height:8px;background:var(--line);border-radius:6px;overflow:hidden;}
.dist-bar i{display:block;height:100%;background:linear-gradient(90deg,#B08A44,#D8B678);border-radius:6px;}
.dist-n{width:24px;text-align:right;font-size:12px;font-weight:600;color:var(--pri);}
.dist-empty{font-size:12px;color:var(--sub);}
@media(max-width:900px){.kpi-row{grid-template-columns:repeat(2,1fr);}.dist-grid{grid-template-columns:1fr;}}
</style>
