import { useEffect, useRef } from 'react'

// Фон страницы входа: голоса сотрудников (линии) стекаются с краев экрана в одну точку - под форму входа.
// side: с какого края идет пучок, at: высота старта (доля экрана), n: число линий, gap: шаг между ними
const BUNDLES = [
  { side: -1, at: 0.2, n: 9, gap: 26, rgb: '51,88,212', bold: 5 },
  { side: -1, at: 0.88, n: 6, gap: 30, rgb: '201,163,92', bold: -1 },
  { side: 1, at: 0.1, n: 6, gap: 28, rgb: '228,143,169', bold: -1 },
  { side: 1, at: 0.76, n: 8, gap: 24, rgb: '47,158,148', bold: 3 },
]
// замкнутые контуры вокруг формы
const RINGS = [
  { rx: 430, ry: 300, rgb: '228,143,169', a: 0.5, w: 1.6, fill: 0.05 },
  { rx: 480, ry: 345, rgb: '228,143,169', a: 0.28, w: 1 },
  { rx: 545, ry: 400, rgb: '47,158,148', a: 0.55, w: 2 },
  { rx: 600, ry: 450, rgb: '47,158,148', a: 0.22, w: 1 },
  { rx: 665, ry: 505, rgb: '51,88,212', a: 0.16, w: 1 },
]

const bez = (p, t) => {
  const u = 1 - t
  return [
    u * u * u * p[0] + 3 * u * u * t * p[2] + 3 * u * t * t * p[4] + t * t * t * p[6],
    u * u * u * p[1] + 3 * u * u * t * p[3] + 3 * u * t * t * p[5] + t * t * t * p[7],
  ]
}

export default function FlowLines() {
  const ref = useRef(null)
  useEffect(() => {
    const cv = ref.current
    const ctx = cv.getContext('2d')
    const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    let w = 0, h = 0, raf = 0, frame = 0, focusY = 300, halfW = 200
    const ptr = { x: 0, y: 0, tx: 0, ty: 0 }

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      w = window.innerWidth; h = window.innerHeight
      cv.width = w * dpr; cv.height = h * dpr
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      if (still) draw(0)
    }
    const move = (e) => { ptr.tx = (e.clientX / w) * 2 - 1; ptr.ty = (e.clientY / h) * 2 - 1 }

    function draw(ms) {
      const t = ms / 1000
      ptr.x += (ptr.tx - ptr.x) * 0.04; ptr.y += (ptr.ty - ptr.y) * 0.04
      const cx = w / 2 + ptr.x * 14
      // точка схода - центр того, что сейчас на экране: плитки ролей или форма
      if (frame++ % 30 === 0) {
        const el = document.querySelector('.login .roles, .login form')
        if (el) { const r = el.getBoundingClientRect(); focusY = r.top + r.height / 2; halfW = r.width / 2 }
      }
      const cy = focusY + ptr.y * 10
      ctx.clearRect(0, 0, w, h)
      ctx.lineCap = 'round'

      // контуры
      const k = Math.max(1, Math.min(1.25, w / 1500))
      RINGS.forEach((r, ri) => {
        ctx.beginPath()
        for (let s = 0; s <= 140; s++) {
          const a = (s / 140) * Math.PI * 2
          const c = Math.cos(a), sn = Math.sin(a)
          const wob = 1 + 0.07 * Math.sin(2 * a + t * 0.11 + ri * 0.6) + 0.045 * Math.sin(3 * a - t * 0.08 + ri * 1.3)
          const x = cx + ptr.x * ri * 4 + Math.sign(c) * Math.abs(c) ** 0.8 * r.rx * k * wob
          const y = cy + 30 + ptr.y * ri * 3 + Math.sign(sn) * Math.abs(sn) ** 0.8 * r.ry * k * wob
          s ? ctx.lineTo(x, y) : ctx.moveTo(x, y)
        }
        ctx.closePath()
        if (r.fill) { ctx.fillStyle = `rgba(${r.rgb},${r.fill})`; ctx.fill() }
        ctx.strokeStyle = `rgba(${r.rgb},${r.a})`; ctx.lineWidth = r.w; ctx.stroke()
      })

      // пучки линий и бегущие по ним точки
      BUNDLES.forEach((b, bi) => {
        const x0 = b.side < 0 ? -40 : w + 40
        for (let i = 0; i < b.n; i++) {
          const o = i - (b.n - 1) / 2
          const ph = bi * 1.7 + i * 0.45
          const y0 = h * b.at + o * b.gap + Math.sin(t * 0.25 + ph) * 18 - ptr.y * 20
          const ye = cy + o * 6
          const p = [
            x0, y0,
            x0 - b.side * w * 0.24, y0 + Math.cos(t * 0.18 + ph) * 40 + (cy - y0) * 0.05,
            cx + b.side * (halfW + w * 0.16), ye + Math.sin(t * 0.21 + ph * 1.3) * 22,
            cx, ye,
          ]
          const bold = i === b.bold
          const a = bold ? 0.9 : 0.25 + 0.18 * (i % 3)
          // линия гаснет, уйдя под край формы, чтобы не просвечивать в зазорах между плитками
          const len = Math.abs(cx - x0)
          const f = Math.max(0.3, Math.min(1, (len - halfW + 40) / len))
          const g = ctx.createLinearGradient(x0, 0, cx, 0)
          g.addColorStop(0, `rgba(${b.rgb},${a * 0.15})`)
          g.addColorStop(f * 0.6, `rgba(${b.rgb},${a})`)
          g.addColorStop(f - 0.03, `rgba(${b.rgb},${a})`)
          g.addColorStop(f, `rgba(${b.rgb},0)`)
          ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.bezierCurveTo(p[2], p[3], p[4], p[5], p[6], p[7])
          ctx.strokeStyle = g; ctx.lineWidth = bold ? 2.8 : 1.3; ctx.stroke()

          if (!still && i % 2 === 0) {
            const u = (t * (0.045 + (i % 3) * 0.012) + ph * 0.37) % 1
            const [x, y] = bez(p, u)
            ctx.beginPath(); ctx.arc(x, y, bold ? 3.4 : 2.4, 0, Math.PI * 2)
            ctx.fillStyle = `rgba(${b.rgb},${u < f ? Math.sin(Math.PI * u / f) * 0.9 : 0})`; ctx.fill()
          }
        }
      })
      if (!still) raf = requestAnimationFrame(draw)
    }

    resize()
    window.addEventListener('resize', resize)
    if (!still) { window.addEventListener('pointermove', move); raf = requestAnimationFrame(draw) }
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', resize)
      window.removeEventListener('pointermove', move)
    }
  }, [])
  return <canvas ref={ref} className="flow-lines" aria-hidden="true" />
}
