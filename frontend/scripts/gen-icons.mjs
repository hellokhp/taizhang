/**
 * 由 src/assets/images/logo-source.png（原图，勿删）生成前端用图：
 *   logo.png             高 256px，界面 logo（侧边栏 40px / 登录页 42px 够用 2x 屏）
 *   apple-touch-icon.png 180px 方形，iOS 添加到主屏
 *   favicon.png          96px  方形，浏览器标签页图标
 * 用法：npm run icons
 */
import sharp from 'sharp'
import { statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const dir = resolve(dirname(fileURLToPath(import.meta.url)), '../src/assets/images')
const SRC = resolve(dir, 'logo-source.png')
const LOGO_H = 256
const TILE = { bg1: '#152C44', bg2: '#0D1B2B', border: '#D8B678' }

const kb = f => Math.round(statSync(f).size / 1024) + 'KB'

// 界面 logo：等比缩到指定高度，保留透明通道
const logoPath = resolve(dir, 'logo.png')
await sharp(SRC).resize({ height: LOGO_H }).png({ compressionLevel: 9, palette: true }).toFile(logoPath)
const { width, height } = await sharp(logoPath).metadata()
console.log(`logo.png ${width}x${height} ${kb(logoPath)}`)

const flame = await sharp(SRC).resize({ height: LOGO_H }).png().toBuffer()
const b64 = flame.toString('base64')

// 方形图标：深蓝圆角底 + 金色描边 + 居中火焰
async function icon(size, radius, file) {
  const pad = Math.round(size * 0.17)
  const fh = size - pad * 2
  const fw = Math.round((fh * width) / height)
  const svg = `<svg width="${size}" height="${size}" xmlns="http://www.w3.org/2000/svg">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="${TILE.bg1}"/><stop offset="1" stop-color="${TILE.bg2}"/>
  </linearGradient></defs>
  <rect width="${size}" height="${size}" rx="${radius}" fill="url(#g)"/>
  <rect x="1.5" y="1.5" width="${size - 3}" height="${size - 3}" rx="${radius - 1.5}"
        fill="none" stroke="${TILE.border}" stroke-opacity="0.55" stroke-width="3"/>
  <image href="data:image/png;base64,${b64}" x="${(size - fw) / 2}" y="${pad}" width="${fw}" height="${fh}"/>
</svg>`
  const out = resolve(dir, file)
  await sharp(Buffer.from(svg)).png({ compressionLevel: 9 }).toFile(out)
  console.log(`${file} ${size}x${size} ${kb(out)}`)
}

await icon(96, 22, 'favicon.png')
await icon(180, 40, 'apple-touch-icon.png')
