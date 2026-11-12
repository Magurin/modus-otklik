import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { AlertCircle, AlertTriangle, Check, Heart, Lightbulb, Loader2, MessageSquareWarning, Users, X } from 'lucide-react'

export const Ctx = createContext(null)
export const useApp = () => useContext(Ctx)

/** Загрузка данных с перезапросом: const [data, reload, error, setData] = useLoad(() => api.get(...), [deps]) */
export function useLoad(fn, deps = []) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const load = useCallback(() => {
    fn().then((d) => { setData(d); setError('') }).catch((e) => setError(e.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  useEffect(() => { load() }, [load])
  return [data, load, error, setData]
}

/* ---------- даты по-человечески ---------- */
const pad = (n) => String(n).padStart(2, '0')
const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
const dayStart = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate())
/** Разница в календарных днях от сегодня: 1 - завтра, -1 - вчера. Одна мера для дат и сроков,
 *  чтобы «завтра» и «осталось N дн.» не могли разойтись. */
export function dayDiff(iso) {
  return Math.round((dayStart(new Date(iso)) - dayStart(new Date())) / 86400000)
}
export function day(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  const now = new Date()
  const diff = dayDiff(iso)
  if (diff === 0) return 'сегодня'
  if (diff === -1) return 'вчера'
  if (diff === 1) return 'завтра'
  return `${d.getDate()} ${MONTHS[d.getMonth()]}${d.getFullYear() !== now.getFullYear() ? ` ${d.getFullYear()}` : ''}`
}
export function dayTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return `${day(iso)}, ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
export function ago(iso) {
  if (!iso) return ''
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
  if (m < 1) return 'только что'
  if (m < 60) return `${m} мин назад`
  if (m < 60 * 24) return `${Math.round(m / 60)} ч назад`
  const d = Math.round(m / 60 / 24)
  return d < 8 ? `${d} дн. назад` : day(iso)
}
/** Сколько календарных дней до срока: 0 - срок сегодня, отрицательное - просрочено. */
export function daysLeft(a) {
  if (!a.due_at || a.closed_at) return null
  return dayDiff(a.due_at)
}
export function dueText(a) {
  const n = daysLeft(a)
  if (n === null) return ''
  if (n < 0) return `просрочено на ${-n} дн.`
  if (a.overdue) return 'просрочено'   // срок был сегодня, но час уже прошел
  if (n === 0) return 'срок сегодня'
  if (n === 1) return 'срок завтра'
  return `осталось ${n} дн.`
}

/* ---------- статусы и типы ---------- */
// Цвет несет только точка, и он тот же, что у этапа на шкале: синее кольцо - новое, оранжевый - рассмотрение,
// желтый - в работе, малиновый - ждем автора, зеленый - сделано, красный - отказ, серый - закрыто.
export const STATUS_DOT = { new: 'st-new', review: 'st-review', clarify: 'st-wait', in_progress: 'st-work', done: 'ok', rejected: 'bad', closed: '' }
// тип обращения - это знак на плитке; цвет плитки - статус обращения (тот же, что у точки и шкалы этапов)
const TYPE_ICON = { problem: MessageSquareWarning, proposal: Lightbulb, praise: Heart }

// Названия состояний - только из meta.statuses (сервер, models.STATUSES): одно состояние - одно название на всех экранах.
// Единственное исключение - обращение автору: вместо «Ожидает уточнения» он видит, что ход за ним.
export const YOUR_TURN = 'Нужен ваш ответ'

/** mine - статус читает автор обращения. */
export function StatusPill({ status, label, mine = false }) {
  const { meta } = useApp()
  const text = label || (mine && status === 'clarify' ? YOUR_TURN : meta.statuses[status]) || status
  return <span className="status"><span className={`dot ${STATUS_DOT[status] || ''}`} />{text}</span>
}
/** Статус чего угодно: <Status dot="ok">Разобрано</Status> */
export function Status({ dot = '', children }) {
  return <span className="status"><span className={`dot ${dot}`} />{children}</span>
}
export function TypeIcon({ type, status, size = 18 }) {
  const Icon = TYPE_ICON[type] || AlertCircle
  const { meta } = useApp()
  const label = meta?.types?.[type]
  return <span className={`type-ico ${status ? `s-${status}` : ''}`} title={label} aria-hidden={!label}><Icon size={size} strokeWidth={2.2} /></span>
}
export function Overdue({ a }) {
  if (!a.overdue) return null
  return <span className="status alert"><AlertTriangle size={14} /> {dueText(a) || 'просрочено'}</span>
}

/* ---------- мелочи ---------- */
export function Spinner({ text }) {
  return <div className="empty"><Loader2 size={22} className="spin" />{text && <span>{text}</span>}</div>
}
export function ErrorNote({ text }) {
  return text ? <div className="note red"><AlertTriangle size={16} /> <span>{text}</span></div> : null
}
export function Empty({ icon: Icon, title, children }) {
  return (
    <div className="empty">
      {Icon && <span className="type-ico lg"><Icon size={22} /></span>}
      {title && <b>{title}</b>}
      {children && <div>{children}</div>}
    </div>
  )
}
export function initials(name = '') {
  return name.split(' ').filter(Boolean).map((p) => p[0]).slice(0, 2).join('').toUpperCase()
}
/** role - роль человека: аватар окрашивается в ее цвет. Без роли (система, внешний автор) аватар серый. */
export function Avatar({ name, size = '', role = '' }) {
  return <span className={`avatar ${size} ${role ? `role r-${role}` : ''}`} aria-hidden>{initials(name) || '?'}</span>
}

export function Modal({ title, children, footer, onClose }) {
  useEffect(() => {
    const h = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <header><h2>{title}</h2><button className="btn ghost icon sm" onClick={onClose} aria-label="Закрыть"><X size={18} /></button></header>
        <div className="body stack">{children}</div>
        <footer>{footer}</footer>
      </div>
    </div>
  )
}

/** Меню действий: <Menu trigger={<button/>} items={[{ label, icon, onClick, danger } | 'sep']} /> */
export function Menu({ trigger, items, up = false, left = false }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  useEffect(() => {
    if (!open) return undefined
    const out = (e) => ref.current && !ref.current.contains(e.target) && setOpen(false)
    const esc = (e) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', out)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('mousedown', out); document.removeEventListener('keydown', esc) }
  }, [open])
  return (
    <span className="menu-wrap" ref={ref}>
      <span onClick={() => setOpen((o) => !o)}>{trigger}</span>
      {open && (
        <div className={`menu ${up ? 'up' : ''} ${left ? 'left' : ''}`} role="menu">
          {items.filter(Boolean).map((it, i) => (it === 'sep' ? <hr key={i} /> : (
            <button key={i} role="menuitem" className={it.danger ? 'danger' : ''} onClick={() => { setOpen(false); it.onClick() }}>
              {it.icon && <it.icon size={16} />}{it.label}
            </button>
          )))}
        </div>
      )}
    </span>
  )
}

/* ---------- этапы обращения ---------- */
const ORDER = { new: 0, review: 1, clarify: 1, in_progress: 2, done: 3 }
// этапы - это статусы по порядку; подписи те же, что у точки статуса
const STAGES = ['new', 'review', 'in_progress', 'done']

export function Steps({ a, mine }) {
  const { meta } = useApp()
  if (a.status === 'rejected' || a.status === 'closed') {
    const bad = a.status === 'rejected'
    return (
      <div className="steps">
        <div className="step done"><span className="c"><Check size={14} /></span>{meta.statuses.new}</div>
        <div className="step done"><span className="c"><Check size={14} /></span>{meta.statuses.review}</div>
        <div className={`step now ${bad ? 'bad' : 'ok'}`}><span className="c">{bad ? <X size={14} /> : <Check size={14} />}</span>{meta.statuses[a.status]}</div>
      </div>
    )
  }
  const cur = ORDER[a.status] ?? 0
  return (
    <div className="steps">
      {STAGES.map((key, i) => {
        const done = i < cur
        const now = i === cur
        const wait = now && a.status === 'clarify'
        const ok = now && a.status === 'done'
        return (
          <div key={key} className={`step ${done ? 'done' : ''} ${now ? 'now' : ''} ${wait ? 'wait' : ''} ${ok ? 'ok' : ''}`}>
            <span className="c">{wait ? '?' : (done || ok) && <Check size={14} />}</span>
            {wait ? (mine ? YOUR_TURN : meta.statuses.clarify) : meta.statuses[key]}
          </div>
        )
      })}
    </div>
  )
}
export function MiniSteps({ a }) {
  const final = a.status === 'rejected' || a.status === 'closed'
  const cur = final ? 3 : ORDER[a.status] ?? 0
  return (
    <div className={`mini-steps ${final ? 'off' : ''}`} aria-hidden>
      {[0, 1, 2, 3].map((i) => <i key={i} className={`${i <= cur ? 'on' : ''} ${i === cur && a.status === 'clarify' ? 'cur' : ''}`} />)}
    </div>
  )
}

/** Строка обращения в любом списке. who: 'author' - показывать автора (для тех, кто рассматривает), 'assignee' - ответственного (для автора). */
export function AppealItem({ a, who = 'author', onClick, progress = false }) {
  const person = who === 'author' ? a.author?.name : a.assignee?.name
  const left = daysLeft(a)
  return (
    <button className="item" onClick={onClick}>
      <TypeIcon type={a.type} status={a.status} />
      <span className="grow">
        <span className="t">{a.title}</span>
        <span className="meta">
          <span>{a.number}</span>
          {a.category && who === 'author' && <span className="sep-dot">{a.category}</span>}
          {person && <span className="sep-dot">{person}</span>}
          <span className="sep-dot">{day(a.created_at)}</span>
          {a.supports > 0 && <span className="sep-dot chip"><Users size={13} /> +{a.supports}</span>}
        </span>
        {progress && <MiniSteps a={a} />}
      </span>
      <span className="side">
        <StatusPill status={a.status} mine={who === 'assignee'} />
        {a.overdue ? <Overdue a={a} />
          : left !== null && left <= 2 && who === 'author' ? <span className="small muted nowrap">{dueText(a)}</span> : null}
      </span>
    </button>
  )
}

/** Мини-рендер Markdown для сводки: абзацы, списки, **жирный**. Разметка экранируется React. */
export function Markdown({ text }) {
  const inline = (s) => s.split(/(\*\*[^*]+\*\*)/g).map((p, i) => (p.startsWith('**') ? <b key={i}>{p.slice(2, -2)}</b> : p))
  const blocks = []
  let list = null
  text.split('\n').forEach((raw) => {
    const line = raw.trim()
    const li = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/)
    if (li) {
      if (!list) { list = []; blocks.push({ list }) }
      list.push(li[1])
    } else {
      list = null
      if (line) blocks.push({ p: line.replace(/^#+\s*/, ''), head: /^#+\s/.test(line) })
    }
  })
  return (
    <div className="md">
      {blocks.map((b, i) => (b.list
        ? <ul key={i}>{b.list.map((x, j) => <li key={j}>{inline(x)}</li>)}</ul>
        : <p key={i}>{b.head ? <b>{b.p}</b> : inline(b.p)}</p>))}
    </div>
  )
}
