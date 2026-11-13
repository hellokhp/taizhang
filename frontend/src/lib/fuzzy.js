/* ================================================================
   轻量模糊匹配（纯函数，无第三方依赖）—— 口径对照 / 本地兜底

   【权威实现在后端】server/ai_project_index.py：项目选择器改为服务端
   模糊搜索 + 分页后，命中判定/排序/高亮区间全由后端完成（nameParts/ridParts
   直接下发），前端不再下载全量项目。本文件保留为：
     1) 后端算法的 JS 镜像，便于回归对照（两边打分/区间口径必须一致）；
     2) 需要在前端本地过滤少量数据的兜底工具。
   ⚠ 改这里任何匹配/打分/区间逻辑，必须同步改 server/ai_project_index.py，
     否则后端高亮与前端展示口径会漂移。

   - 面向中文项目名 + 招标编号 + 英文/数字混合
   - 多关键词空格分隔，全部命中才算命中（AND）
   - 每个关键词：连续子串优先（越靠前/越贴词边界越相关），
     否则退化为「按顺序出现」的子序列匹配（低分兜底，容错首字母缩写）
   - 返回命中区间，供下拉列表高亮
   ================================================================ */

const RE_SPACE = /\s+/
const RE_BOUNDARY = /[\s（(\/、，,·_\-—:：]/

export function tokenize(query) {
  return String(query == null ? '' : query).trim().toLowerCase().split(RE_SPACE).filter(Boolean)
}

function lower(s) {
  return String(s == null ? '' : s).toLowerCase()
}

/** 连续子串命中；返回 null 表示未命中 */
function substrHit(hay, needle) {
  const i = hay.indexOf(needle)
  if (i < 0) return null
  let score
  if (i === 0 && hay.length === needle.length) score = 1000        // 完全相等
  else if (i === 0) score = 640                                     // 前缀
  else {
    score = 420 - Math.min(i, 240)                                  // 越靠前越相关
    if (RE_BOUNDARY.test(hay[i - 1])) score += 60                   // 词边界
  }
  return { score, ranges: [[i, i + needle.length]], kind: i === 0 ? 'prefix' : 'substr' }
}

/** 子序列命中：字符按顺序出现即可（允许跳字），分低于任何子串命中 */
function subseqHit(hay, needle) {
  if (!hay.length || !needle.length) return null
  let ti = 0, start = -1, prev = -1, gaps = 0
  const ranges = []
  for (let i = 0; i < hay.length && ti < needle.length; i++) {
    if (hay[i] === needle[ti]) {
      if (start < 0) start = i
      if (prev >= 0 && i - prev > 1) gaps += i - prev - 1
      const last = ranges[ranges.length - 1]
      if (last && last[1] === i) last[1] = i + 1
      else ranges.push([i, i + 1])
      prev = i
      ti++
    }
  }
  if (ti < needle.length) return null
  const span = prev - start + 1
  // 子序列必须显著低于子串（子串最低约 420-240=180），避免短名称靠 span 惩罚小反而排到前面。
  // 基准压到 120，再用间隔与跨度做小幅区分。
  return { score: 120 - gaps * 2 - span * 0.3, ranges, kind: 'subseq' }
}

/** 单关键词对单字段的最佳匹配 */
export function matchOne(text, token) {
  const hay = lower(text)
  const needle = lower(token)
  if (!needle) return null
  return substrHit(hay, needle) || subseqHit(hay, needle)
}

/** 合并重叠/相邻区间，供高亮渲染 */
export function mergeRanges(ranges) {
  if (!ranges || !ranges.length) return []
  const sorted = ranges.slice().sort((a, b) => a[0] - b[0])
  const out = [sorted[0].slice()]
  for (let i = 1; i < sorted.length; i++) {
    const last = out[out.length - 1]
    if (sorted[i][0] <= last[1]) last[1] = Math.max(last[1], sorted[i][1])
    else out.push(sorted[i].slice())
  }
  return out
}

/** 把字符串按命中区间切成 [{ text, hit }] 片段 */
export function splitByRanges(text, ranges) {
  const s = String(text == null ? '' : text)
  if (!ranges || !ranges.length) return [{ text: s, hit: false }]
  const parts = []
  let pos = 0
  for (let i = 0; i < ranges.length; i++) {
    const a = ranges[i][0], b = ranges[i][1]
    if (a > pos) parts.push({ text: s.slice(pos, a), hit: false })
    parts.push({ text: s.slice(a, b), hit: true })
    if (b > pos) pos = b
  }
  if (pos < s.length) parts.push({ text: s.slice(pos), hit: false })
  return parts
}

/** 单字段多关键词匹配；全部命中才返回，否则 null */
export function fuzzyMatch(text, query) {
  const tokens = tokenize(query)
  if (!tokens.length) return { score: 0, ranges: [], tokens: [] }
  let score = 0
  const all = []
  for (let i = 0; i < tokens.length; i++) {
    const hit = matchOne(text, tokens[i])
    if (!hit) return null
    score += hit.score
    all.push(...hit.ranges)
  }
  return { score, ranges: mergeRanges(all), tokens }
}

/**
 * 项目列表过滤 + 排序。
 * 每条项目同时对 name 与 rid 匹配；某关键词在任一字段命中即可，
 * 两个字段的命中区间都保留用于高亮。空查询保持后端原顺序。
 */
export function filterProjects(projects, query) {
  const list = Array.isArray(projects) ? projects : []
  const tokens = tokenize(query)
  if (!tokens.length) return list.slice()

  const out = []
  for (let i = 0; i < list.length; i++) {
    const p = list[i]
    const name = lower(p.name)
    const rid = lower(p.rid)
    let total = 0
    const nameRanges = []
    const ridRanges = []
    let ok = true
    for (let t = 0; t < tokens.length; t++) {
      const a = matchOne(name, tokens[t])
      const b = matchOne(rid, tokens[t])
      if (!a && !b) { ok = false; break }
      total += Math.max(a ? a.score : -Infinity, b ? b.score : -Infinity)
      if (a) nameRanges.push(...a.ranges)
      if (b) ridRanges.push(...b.ranges)
    }
    if (!ok) continue
    out.push({
      ...p,
      _score: total + (p.hasTenderDir ? 5 : 0),      // 轻微优先有招标文件目录的项目
      _nameRanges: mergeRanges(nameRanges),
      _ridRanges: mergeRanges(ridRanges),
      _idx: i,
    })
  }
  out.sort((a, b) => (b._score - a._score)
    || (String(a.name || '').length - String(b.name || '').length)
    || (a._idx - b._idx))
  return out
}
