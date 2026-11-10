import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  ArrowUp, EyeOff, FileText, HelpCircle, Lightbulb, Loader2, Paperclip, PenLine, Sparkles, ThumbsUp, UserRoundCheck, X,
} from 'lucide-react'
import { api } from '../api.js'
import HomeFlow from '../HomeFlow.jsx'
import Select, { fromList, fromMap } from '../Select.jsx'
import { AppealItem, ErrorNote, Spinner, StatusPill, useApp, useLoad } from '../ui.jsx'

export default function Home() {
  const nav = useNavigate()
  const [rows, , error] = useLoad(() => api.get('/appeals?scope=mine'), [])
  const [tab, setTab] = useState('open')
  const { meta, user } = useApp()
  const open = rows ? rows.filter((a) => !meta.final.includes(a.status)) : []
  const closed = rows ? rows.filter((a) => meta.final.includes(a.status)) : []
  const shown = tab === 'open' ? open : closed
  return (
    <div className="page home">
      <HomeFlow role={user.role} />
      <div className={`home-hero ${rows && rows.length === 0 ? 'first' : ''}`}><h1>Что можно улучшить?</h1></div>
      <Composer />
      <div style={{ height: 40 }} />
      <ErrorNote text={error} />
      {!rows ? <Spinner /> : rows.length === 0 ? null : (
        <>
          <div className="section-title">
            <h2>Мои обращения</h2>
            <div className="segs">
              <button className={tab === 'open' ? 'on' : ''} onClick={() => setTab('open')}>Открытые <span className="n">{open.length}</span></button>
              <button className={tab === 'closed' ? 'on' : ''} onClick={() => setTab('closed')}>Завершены <span className="n">{closed.length}</span></button>
            </div>
          </div>
          <div className="card rows">
            {shown.length === 0 && <div className="empty">{tab === 'open' ? 'Все обращения рассмотрены' : 'Пока нет завершенных'}</div>}
            {shown.map((a) => <AppealItem key={a.id} a={a} who="assignee" progress onClick={() => nav(`/appeals/${a.id}`)} />)}
          </div>
        </>
      )}
    </div>
  )
}

/** Одно поле вместо формы: человек пишет текст, тип, категорию и ответственного подбирает система. */
export function Composer() {
  const { meta } = useApp()
  const nav = useNavigate()
  const [text, setText] = useState('')
  const [solution, setSolution] = useState('')
  const [withSolution, setWithSolution] = useState(false)
  const [anonymous, setAnonymous] = useState(false)
  const [files, setFiles] = useState([])
  const [form, setForm] = useState({ type: '', category_id: '', priority: '' })
  const [touched, setTouched] = useState({})
  const [hint, setHint] = useState(null)
  const [thinking, setThinking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [supported, setSupported] = useState(null)
  const lastAsked = useRef('')
  const area = useRef(null)

  const grow = (el) => { if (el) { el.style.height = 'auto'; el.style.height = `${Math.min(el.scrollHeight, 320)}px` } }
  useEffect(() => { grow(area.current) }, [text])

  // подсказка: через паузу после ввода подбираем тип и категорию и ищем похожие обращения
  useEffect(() => {
    const t = text.trim()
    if (t.length < 30) { if (!t) { setHint(null); lastAsked.current = '' } return undefined }
    if (t === lastAsked.current) return undefined
    const timer = setTimeout(() => {
      lastAsked.current = t
      setThinking(true)
      api.post('/appeals/assist', { text: t }).then((h) => {
        setHint(h)
        setForm((f) => ({
          type: touched.type ? f.type : h.type,
          category_id: touched.category_id ? f.category_id : (h.category_id || ''),
          priority: touched.priority ? f.priority : h.priority,
        }))
      }).catch(() => {}).finally(() => setThinking(false))
    }, 1000)
    return () => clearTimeout(timer)
  }, [text, touched])

  const set = (k) => (v) => { setForm({ ...form, [k]: v }); setTouched({ ...touched, [k]: true }) }
  const cat = meta.categories.find((c) => String(c.id) === String(form.category_id))
  const ready = text.trim().length >= 20

  const submit = () => {
    if (!ready || busy) return
    setBusy(true); setError('')
    const fd = new FormData()
    fd.set('text', text)
    if (withSolution) fd.set('solution', solution)
    fd.set('anonymous', anonymous)
    Object.entries(form).forEach(([k, v]) => v && fd.set(k, v))
    if (hint?.title && lastAsked.current === text.trim()) fd.set('title', hint.title)
    files.forEach((f) => fd.append('files', f))
    api.form('/appeals', fd).then((a) => nav(`/appeals/${a.id}`, { state: { created: true } }))
      .catch((e) => { setError(e.message); setBusy(false) })
  }
  const support = (s) => api.post(`/appeals/${s.id}/support`).then((r) => setSupported({ ...r, id: s.id })).catch((e) => setError(e.message))

  if (supported) {
    return (
      <div className="card pad stack">
        <div className="note green"><ThumbsUp size={18} /><div>
          <b>Вы присоединились к {supported.number}</b>
          Об этом сказали уже {supported.supports + 1}. О решении вам сообщат.
        </div></div>
        <div className="row">
          <button className="btn" onClick={() => { setSupported(null); setText(''); setHint(null); setFiles([]) }}>Написать еще</button>
        </div>
      </div>
    )
  }
  const similar = (hint?.similar || []).filter((s) => !s.mine).slice(0, 2)
  // Enter отправляет, Shift+Enter - новая строка (во время набора через IME Enter не перехватываем)
  const enterSends = (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit() }
  }

  return (
    <div className="stack tight">
      <div className="composer">
        <textarea ref={area} value={text} onChange={(e) => setText(e.target.value)} rows={3} aria-label="Текст обращения"
          placeholder="Опишите проблему или идею своими словами"
          onKeyDown={enterSends} />
        {withSolution && (
          <textarea className="second" value={solution} onChange={(e) => setSolution(e.target.value)} rows={2}
            placeholder="Как это можно решить?" aria-label="Предлагаемое решение" autoFocus onKeyDown={enterSends} />
        )}
        {(hint || thinking || files.length > 0) && (
          <div className="extras">
            {files.length > 0 && (
              <div className="row wrap" style={{ gap: 6 }}>
                {files.map((f, i) => (
                  <span key={i} className="pill"><FileText size={12} /> {f.name}
                    <button className="btn ghost icon sm" style={{ width: 18, height: 18 }} onClick={() => setFiles(files.filter((_, j) => j !== i))} aria-label={`Убрать ${f.name}`}><X size={12} /></button>
                  </span>
                ))}
              </div>
            )}
            {thinking && !hint && <span className="chip"><Loader2 size={14} className="spin" /> Подбираю категорию</span>}
            {hint && (
              <div className="ai">
                {thinking ? <Loader2 size={16} className="spin muted" /> : <Sparkles size={16} className="muted" />}
                <Select size="pill" value={form.type} onChange={set('type')} ariaLabel="Тип" options={fromMap(meta.types)} />
                <Select size="pill" value={form.category_id} onChange={set('category_id')} ariaLabel="Категория" placeholder="Категория"
                  options={fromList(meta.categories, (c) => c.name, (c) => c.responsible || '')} />
                <Select size="pill" value={form.priority} onChange={set('priority')} ariaLabel="Приоритет"
                  options={fromMap(meta.priorities).map((o) => ({ ...o, label: `Приоритет: ${o.label.toLowerCase()}` }))} />
                {cat?.responsible && <span className="chip"><UserRoundCheck size={15} /> Рассмотрит {cat.responsible}</span>}
              </div>
            )}
            {hint?.question && <div className="note amber small"><HelpCircle size={15} /><span>Стоит уточнить: {hint.question}</span></div>}
          </div>
        )}
        <div className="bar">
          <label className="btn ghost sm" title="Приложить файл"><Paperclip size={16} /> Файл
            <input type="file" multiple hidden onChange={(e) => { setFiles([...files, ...e.target.files]); e.target.value = '' }} />
          </label>
          <button type="button" className={`btn ghost sm ${withSolution ? 'on' : ''}`} onClick={() => setWithSolution(!withSolution)}><Lightbulb size={16} /> Есть решение</button>
          <button type="button" className={`btn ghost sm ${anonymous ? 'on' : ''}`} onClick={() => setAnonymous(!anonymous)} aria-pressed={anonymous}
            title="Имя и отдел не увидит никто, включая администратора"><EyeOff size={16} /> Анонимно</button>
          <span className="grow" />
          <button className="send" disabled={!ready || busy} onClick={submit} aria-label="Отправить" title="Отправить - Enter">
            {busy ? <Loader2 size={18} className="spin" /> : <ArrowUp size={19} />}
          </button>
        </div>
      </div>
      {similar.map((s) => (
        <div key={s.id} className="similar">
          <span className="grow"><span className="small muted">Похожее уже есть</span><br /><b>{s.number}</b> {s.title}</span>
          <StatusPill status={s.status} />
          <button className="btn sm" onClick={() => support(s)}><ThumbsUp size={14} /> У меня то же</button>
        </div>
      ))}
      <ErrorNote text={error} />
    </div>
  )
}

export function ComposeLink() {
  return <Link className="btn primary" to="/home"><PenLine size={16} /> Написать</Link>
}
