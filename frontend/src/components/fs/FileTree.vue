<script setup>
/* 左侧目录树：懒加载展开，扁平结构 + 缩进，避免递归组件 */
import { onMounted, ref } from 'vue'
import { api } from '../../api/client'

const props = defineProps({ current: { type: String, default: '' } });
const emit = defineEmits(['open']);
const rows = ref([]);

async function load(pid){
  const r = await api('/api/fs/tree?parent_id=' + pid);
  return (r.items || []).map(x => ({ id: x.id, name: x.name, hasChild: x.hasChild,
                                     depth: 0, open: false, loaded: false }));
}

onMounted(async () => { rows.value = await load(''); });

async function toggle(row, i){
  const open = !row.open;
  if(open && !row.loaded){
    const kids = await load(row.id);
    kids.forEach(k => { k.depth = row.depth + 1; });
    rows.value.splice(i + 1, 0, ...kids);
    row.loaded = true;
  }
  row.open = open;
  if(!open){
    // 收起：移除本节点之下所有更深层的行
    let j = i + 1;
    while(j < rows.value.length && rows.value[j].depth > row.depth) j++;
    rows.value.splice(i + 1, j - i - 1);
    row.loaded = false;
  }
  emit('open', row.id);
}

function refresh(){
  rows.value = [];
  load('').then(list => { rows.value = list; });
}
defineExpose({ refresh });
</script>

<template>
  <div class="fs-tree">
    <div class="fs-tnode root" :class="{ on: current === '' }" @click="emit('open', '')">
      <span class="ico">▤</span>存储根目录
    </div>
    <template v-for="(r, i) in rows" :key="r.id">
      <div class="fs-tnode" :class="{ on: current === r.id }"
           :style="{ paddingLeft: (10 + r.depth * 14) + 'px' }" @click="emit('open', r.id)">
        <span class="caret" :class="{ open: r.open }" v-if="r.hasChild"
              @click.stop="toggle(r, i)">▸</span>
        <span class="caret ph" v-else></span>
        <span class="ico">{{ r.open ? '▨' : '▤' }}</span>
        <span class="nm">{{ r.name }}</span>
      </div>
    </template>
    <div class="fs-tempty" v-if="!rows.length">暂无子目录</div>
  </div>
</template>
