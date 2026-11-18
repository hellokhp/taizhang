import { reactive, watch, onBeforeUnmount } from 'vue'

/* ================================================================
   表格列宽 / 行高伸缩
   · 整体：beginResize('allcol' | 'allrow')
   · 单列：beginResize('col', key)
   · 单行：beginResize('row', key)
   · 配置本地记忆，支持重置；必须在 setup 中调用
   ================================================================ */

const clamp = (v, min, max) => Math.min(max, Math.max(min, Math.round(Number(v) || 0)))

export function useTableLayout(options = {}) {
  const {
    storageKey = '',
    colDefault = 160,
    rowDefault = 40,
    colMin = 80,
    colMax = 520,
    rowMin = 28,
    rowMax = 260,
    saveDelay = 200
  } = options

  const cfg = reactive({ colW: colDefault, rowH: rowDefault, widths: {}, heights: {} })

  if (storageKey) {
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || 'null')
      if (saved) {
        cfg.colW = clamp(saved.colW ?? colDefault, colMin, colMax)
        cfg.rowH = clamp(saved.rowH ?? rowDefault, rowMin, rowMax)
        if (saved.widths && typeof saved.widths === 'object') cfg.widths = saved.widths
        if (saved.heights && typeof saved.heights === 'object') cfg.heights = saved.heights
      }
    } catch (e) { /* 本地配置损坏时使用默认值 */ }
  }

  let saveTimer = null
  if (storageKey) {
    watch(cfg, () => {
      clearTimeout(saveTimer)
      saveTimer = setTimeout(() => {
        try {
          localStorage.setItem(storageKey, JSON.stringify({
            colW: cfg.colW, rowH: cfg.rowH, widths: cfg.widths, heights: cfg.heights
          }))
        } catch (e) { /* 存储不可用时静默降级 */ }
      }, saveDelay)
    }, { deep: true })
  }

  const colWOf = key => clamp(cfg.widths[key] ?? cfg.colW, colMin, colMax)
  const rowHOf = key => clamp(cfg.heights[key] ?? cfg.rowH, rowMin, rowMax)
  /* 行高变矮时先收内边距，避免文字被裁切 */
  const rowPadOf = key => Math.max(2, Math.min(11, Math.round((rowHOf(key) - 18) / 2)))

  let drag = null
  let resized = false

  function onMove(e) {
    if (!drag) return
    const isRow = drag.kind === 'row' || drag.kind === 'allrow'
    const delta = isRow ? e.clientY - drag.y : e.clientX - drag.x
    if (Math.abs(delta) < 1) return

    if (drag.kind === 'row') {
      cfg.heights[drag.key] = clamp(drag.h + delta, rowMin, rowMax)
    } else if (drag.kind === 'allrow') {
      if (!drag.cleared) { cfg.heights = {}; drag.cleared = true }
      cfg.rowH = clamp(drag.h + delta, rowMin, rowMax)
    } else if (drag.kind === 'col') {
      cfg.widths[drag.key] = clamp(drag.w + delta, colMin, colMax)
    } else if (drag.kind === 'allcol') {
      if (!drag.cleared) { cfg.widths = {}; drag.cleared = true }
      cfg.colW = clamp(drag.w + delta, colMin, colMax)
    }
  }

  function endDrag() {
    if (!drag) return
    drag = null
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointercancel', endDrag)
    /* 延后一帧复位，避免刚拖完就被表头点击当成排序 */
    setTimeout(() => { resized = false }, 0)
  }

  function beginResize(kind, key, e) {
    if (e.button !== undefined && e.button !== 0) return
    e.preventDefault()
    e.stopPropagation()
    const isRow = kind === 'row' || kind === 'allrow'
    drag = {
      kind,
      key,
      x: e.clientX,
      y: e.clientY,
      w: colWOf(key),
      h: rowHOf(key),
      cleared: false
    }
    resized = true
    document.body.style.cursor = isRow ? 'row-resize' : 'col-resize'
    document.body.style.userSelect = 'none'
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', endDrag, { once: true })
    window.addEventListener('pointercancel', endDrag)
  }

  function reset() {
    cfg.colW = colDefault
    cfg.rowH = rowDefault
    cfg.widths = {}
    cfg.heights = {}
  }

  onBeforeUnmount(() => { endDrag() })

  return {
    cfg,
    colWOf,
    rowHOf,
    rowPadOf,
    beginResize,
    reset,
    justResized: () => resized
  }
}
