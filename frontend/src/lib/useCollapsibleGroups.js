/* ================================================================
   通用折叠分组状态：避免「大组全展开挤掉其它组」的问题。

   策略：
   - 智能默认：条目数 > autoCollapseAt 的组默认折叠，小组默认展开；
     这样大组不会一进来就占满容器，其他组一眼可见。
   - 用户手动切换过的组状态会被记住（数据刷新不覆盖用户选择）。
   - expandAll / collapseAll 提供批量操作。

   用法：
     const gs = computed(...);            // 每项含唯一 key + items
     const c = useCollapsibleGroups(gs, { keyOf: g => g.key, autoCollapseAt: 6 });
     模板里 c.isCollapsed(g.key) / c.toggle(g.key) / c.expandAll() / c.collapseAll()
   ================================================================ */
import { ref, watch, computed } from 'vue'

export function useCollapsibleGroups(groupsRef, options = {}) {
  const keyOf = options.keyOf || (g => g.key ?? g.name);
  const countOf = options.countOf || (g => (g.items ? g.items.length : 0));
  const autoCollapseAt = options.autoCollapseAt ?? 6;   // 超过此数量默认折叠

  const collapsed = ref({});
  const userTouched = ref({});       // 记录用户是否手动切换过某组

  watch(groupsRef, gs => {
    const next = {};
    (gs || []).forEach(g => {
      const k = keyOf(g);
      // 用户手动切换过 → 尊重其选择；否则按条目数智能默认
      next[k] = userTouched.value[k]
        ? !!collapsed.value[k]
        : countOf(g) > autoCollapseAt;
    });
    collapsed.value = next;
  }, { immediate: true, deep: false });

  function toggle(k){
    userTouched.value[k] = true;
    collapsed.value[k] = !collapsed.value[k];
  }
  function isCollapsed(k){ return !!collapsed.value[k]; }
  function expandAll(){
    (groupsRef.value || []).forEach(g => {
      const k = keyOf(g);
      userTouched.value[k] = true;
      collapsed.value[k] = false;
    });
  }
  function collapseAll(){
    (groupsRef.value || []).forEach(g => {
      const k = keyOf(g);
      userTouched.value[k] = true;
      collapsed.value[k] = true;
    });
  }
  const allCollapsed = computed(() => {
    const gs = groupsRef.value || [];
    return gs.length > 0 && gs.every(g => collapsed.value[keyOf(g)]);
  });

  return { collapsed, toggle, isCollapsed, expandAll, collapseAll, allCollapsed };
}
