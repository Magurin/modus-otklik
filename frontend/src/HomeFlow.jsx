import { useEffect, useRef } from 'react'

// Фон главной: тот же мотив, что на входе (FlowLines), но тихий. Линии стекаются к полю ввода,
// все - в цвете роли пользователя. Когда человек печатает, точки бегут быстрее.
// side: с какого края идет пучок, at: высота старта (доля экрана), n: число линий, gap: шаг между ними
const bundlesFor = (role) => [
  { side: -1, at: 0.2, n: 6, gap: 24, rgb: role, bold: 3 },
  { side: -1, at: 0.74, n: 4, gap: 28, rgb: role, bold: -1 },
  { side: 1, at: 0.3, n: 4, gap: 26, rgb: role, bold: -1 },
  { side: 1, at: 0.66, n: 5, gap: 24, rgb: role, bold: 2 },
]

const ANCHOR = 86  // точка схода - на этой глубине от верха поля: середина поля в исходном, пустом виде

const bez = (p, t) => {
  const u = 1 - t
  return [
    u * u * u * p[0] + 3 * u * u * t * p[2] + 3 * u * t * t * p[4] + t * t * t * p[6],
    u * u * u * p[1] + 3 * u * u * t * p[3] + 3 * u * t * t * p[5] + t * t * t * p[7],
  ]
}
const hexToRgb = (hex) => {
  const m = hex.replace('#', '').match(/^([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i)
  return m ? m.slice(1).map((x) => parseInt(x, 16)).join(',') : '51,88,212'
}

export default function HomeFlow({ role = 'employee' }) {
  const ref = useRef(null)
  useEffect(() => {
    const cv = ref.current
    const ctx = cv.getContext('2d')
    const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const color = hexToRgb(getComputedStyle(document.documentElement).getPropertyValue(`--role-${role}-solid`).trim())
    const bundles = bundlesFor(color)
    let w = 0, h = 0, raf = 0, frame = 0, last = 0, flow = 0, energy = 0
    let cx = 0, cy = 260, halfW = 300
    let tx = 0, ty = 260, placed = false  // куда точка схода должна прийти; сама она туда доезжает плавно

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      w = cv.clientWidth; h = cv.clientHeight
      cv.width = w * dpr; cv.height = h * dpr
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      frame = 0
      if (still) draw(0)
    }
    // печатает в поле обращения - поток оживает
    const typing = (e) => { if (e.target?.closest?.('.composer')) energy = Math.min(1, energy + 0.22) }

    function draw(ms) {
      const dt = Math.min(0.05, (ms - last) / 1000 || 0); last = ms
      const t = ms / 1000
      energy *= 0.975
      flow += dt * (1 + energy * 5)
      // точка схода привязана к верху поля ввода (в его положении без прокрутки), а не к центру: поле растет,
      // когда человек печатает, добавляет решение или получает подсказки, - линии при этом остаются на месте.
      // Фон зафиксирован и при скролле не двигается; если поле все же сместилось (изменили окно), линии доезжают плавно
      if (frame++ % 12 === 0) {
        const el = document.querySelector('.composer')
        const box = cv.getBoundingClientRect()
        if (el) {
          const r = el.getBoundingClientRect()
          let scroll = window.scrollY
          for (let p = el.parentElement; p; p = p.parentElement) scroll += p.scrollTop
          tx = r.left - box.left + r.width / 2; ty = r.top + scroll - box.top + ANCHOR; halfW = r.width / 2
          if (!placed || still) { cx = tx; cy = ty; placed = true }
        }
      }
      cx += (tx - cx) * 0.08; cy += (ty - cy) * 0.08
      ctx.clearRect(0, 0, w, h)
      ctx.lineCap = 'round'
      bundles.forEach((b, bi) => {
        const x0 = b.side < 0 ? -40 : w + 40
        for (let i = 0; i < b.n; i++) {
          const o = i - (b.n - 1) / 2
          const ph = bi * 1.7 + i * 0.45
          const y0 = h * b.at + o * b.gap + Math.sin(t * 0.22 + ph) * 14
          const ye = cy + o * 5
          const p = [
            x0, y0,
            x0 - b.side * w * 0.22, y0 + Math.cos(t * 0.16 + ph) * 30 + (cy - y0) * 0.05,
            cx + b.side * (halfW + w * 0.12), ye + Math.sin(t * 0.19 + ph * 1.3) * 16,
            cx, ye,
          ]
          const bold = i === b.bold
          const a = (bold ? 0.68 : 0.2 + 0.12 * (i % 3)) * (1 + energy * 0.4)
          // линия гаснет у края поля, чтобы не просвечивать за карточкой
          const len = Math.abs(cx - x0)
          const f = Math.max(0.3, Math.min(1, (len - halfW + 24) / len))
          const g = ctx.createLinearGradient(x0, 0, cx, 0)
          g.addColorStop(0, `rgba(${b.rgb},${a * 0.1})`)
          g.addColorStop(f * 0.65, `rgba(${b.rgb},${a})`)
          g.addColorStop(Math.max(0, f - 0.03), `rgba(${b.rgb},${a})`)
          g.addColorStop(f, `rgba(${b.rgb},0)`)
          ctx.beginPath(); ctx.moveTo(p[0], p[1]); ctx.bezierCurveTo(p[2], p[3], p[4], p[5], p[6], p[7])
          ctx.strokeStyle = g; ctx.lineWidth = bold ? 2 : 1.1; ctx.stroke()

          if (!still && i % 2 === 0) {
            const u = (flow * (0.04 + (i % 3) * 0.011) + ph * 0.37) % 1
            const [x, y] = bez(p, u)
            ctx.beginPath(); ctx.arc(x, y, bold ? 2.8 : 2, 0, Math.PI * 2)
            ctx.fillStyle = `rgba(${b.rgb},${u < f ? Math.sin(Math.PI * u / f) * (0.55 + energy * 0.4) : 0})`; ctx.fill()
          }
        }
      })
      if (!still) raf = requestAnimationFrame(draw)
    }

    resize()
    const ro = new ResizeObserver(resize)
    ro.observe(cv)
    window.addEventListener('input', typing, true)
    if (!still) raf = requestAnimationFrame(draw)
    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      window.removeEventListener('input', typing, true)
    }
  }, [role])
  return <canvas ref={ref} className="home-flow" aria-hidden="true" />
}
