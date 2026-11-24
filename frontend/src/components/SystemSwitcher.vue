<script setup>
/* 顶栏系统切换器：仅当前用户有权访问多个系统时显示。
   页面标题旁下拉，选中即跳到目标系统首页，无需重新登录。 */
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { availableSystems, switchTo } from '@/stores/common/system'
import { sys } from '@/stores/common/system'
import { SYSTEMS } from '@/config/systems'

const router = useRouter();
const open = ref(false);
const root = ref(null);
const lists = computed(() => availableSystems());
const current = computed(() => SYSTEMS[sys.current]);

function choose(key){
  open.value = false;
  if(key === sys.current) return;
  const path = switchTo(key);
  if(path) router.push(path);
}
function onDoc(e){
  if(root.value && !root.value.contains(e.target)) open.value = false;
}
onMounted(() => document.addEventListener('click', onDoc));
onUnmounted(() => document.removeEventListener('click', onDoc));
</script>

<template>
  <div class="sys-switch" ref="root" v-if="lists.length > 1">
    <button class="sys-switch-btn" @click="open = !open" :title="'当前系统：' + (current && current.name)">
      <span class="sys-dot" :class="'theme-' + (current && current.theme)"></span>
      {{ (current && current.name) || '系统' }}
      <span class="caret">▾</span>
    </button>
    <div v-if="open" class="sys-menu">
      <div class="sys-menu-h">切换系统</div>
      <button v-for="s in lists" :key="s.key" :class="{on: s.key === sys.current}" @click="choose(s.key)">
        <span class="sys-dot" :class="'theme-' + s.theme"></span>
        <span class="sys-menu-txt">
          <b>{{ s.name }}</b>
          <em>{{ s.deptName }}</em>
        </span>
        <span v-if="s.key === sys.current" class="sys-cur">当前</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.sys-switch{position:relative;}
.sys-switch-btn{
  display:flex;align-items:center;gap:6px;padding:5px 10px;border:1px solid var(--line);
  background:#fff;border-radius:8px;cursor:pointer;font-size:12px;font-weight:500;
  color:var(--pri);font-family:inherit;transition:.16s var(--ease);
}
.sys-switch-btn:hover{border-color:var(--gold);}
.caret{font-size:10px;color:var(--sub);}
.sys-dot{width:8px;height:8px;border-radius:50%;flex:none;display:inline-block;}
.sys-dot.theme-blue{background:#378ADD;}
.sys-dot.theme-green{background:#1D9E75;}
.sys-dot.theme-purple{background:#7F77DD;}
.sys-dot.theme-slate{background:#D8B678;}
.sys-menu{
  position:absolute;right:0;top:calc(100% + 6px);z-index:40;min-width:210px;
  background:#fff;border:1px solid var(--line);border-radius:12px;box-shadow:var(--sh2);
  padding:6px;
}
.sys-menu-h{font-size:10.5px;color:var(--sub);padding:6px 10px 5px;letter-spacing:.5px;}
.sys-menu button{
  display:flex;align-items:center;gap:9px;width:100%;border:0;background:transparent;
  padding:8px 10px;border-radius:8px;cursor:pointer;font-family:inherit;text-align:left;color:var(--pri);
}
.sys-menu button:hover{background:var(--bg-2, #F5F3EE);}
.sys-menu button.on{background:rgba(216,182,120,.13);}
.sys-menu-txt{display:flex;flex-direction:column;gap:1px;flex:1;}
.sys-menu-txt b{font-size:12.5px;font-weight:600;}
.sys-menu-txt em{font-size:10px;color:var(--sub);font-style:normal;}
.sys-cur{font-size:10px;color:#A87F32;background:rgba(216,182,120,.18);border-radius:6px;padding:1px 6px;}
</style>
