import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Check, ChevronDown, ChevronRight, FileAudio, Loader2, Mic, Pencil, RefreshCw, Upload, UserRound, X } from 'lucide-react'
import { api } from '../api.js'
import Select, { fromList, fromMap } from '../Select.jsx'
import { Empty, ErrorNote, Modal, Spinner, Status, StatusPill, day, useApp, useLoad } from '../ui.jsx'

const KIND = { problem: 'Проблема', proposal: 'Идея', praise: 'Благодарность', decision: 'Решение', task: 'Поручение' }
const TO_APPEAL = ['problem', 'proposal', 'praise']
const busy = (s) => s === 'queued' || s === 'transcribing' || s === 'extracting'

function State({ m }) {
  if (busy(m.status)) return <span className="status"><Loader2 size={14} className="spin" /> Обрабатывается</span>
  if (m.status === 'error') return <Status dot="bad">Ошибка</Status>
  if (m.items_draft > 0) return <Status dot="warn">Нужно решить: {m.items_draft}</Status>
  return <Status dot="ok">Разобрано</Status>
}

export function Meetings() {
  const nav = useNavigate()
  const [rows, reload, error] = useLoad(() => api.get('/meetings'), [])
  const [upload, setUpload] = useState(false)
  useEffect(() => {
    if (!rows || !rows.some((m) => busy(m.status))) return undefined
    const t = setInterval(reload, 3000)
    return () => clearInterval(t)
  }, [rows, reload])
  return (
    <div className="page">
      <div className="head">
        <h1>Совещания</h1>
        <button className="btn primary" onClick={() => setUpload(true)}><Upload size={17} /> Загрузить запись</button>
      </div>
      <ErrorNote text={error} />
      {!rows ? <Spinner /> : rows.length === 0 ? (
        <div className="card"><Empty icon={Mic} title="Загрузите запись совещания">Система выпишет из нее проблемы, идеи и решения</Empty></div>
      ) : (
        <div className="card rows">
          {rows.map((m) => (
            <button key={m.id} className="item" onClick={() => nav(`/meetings/${m.id}`)}>
              <span className="type-ico"><Mic size={18} /></span>
              <span className="grow">
                <span className="t">{m.title}</span>
                <span className="meta"><span>{day(m.held_at)}</span>{m.status === 'ready' && <span className="sep-dot">пунктов: {m.items_total}</span>}</span>
              </span>
              <span className="side"><State m={m} /></span>
            </button>
          ))}
        </div>
      )}
      {upload && <UploadModal onClose={() => setUpload(false)} onDone={(m) => nav(`/meetings/${m.id}`)} />}
    </div>
  )
}

function UploadModal({ onClose, onDone }) {
  const [title, setTitle] = useState('')
  const [date, setDate] = useState(new Date().toISOString().slice(0, 10))
  const [file, setFile] = useState(null)
  const [text, setText] = useState('')
  const [mode, setMode] = useState('file')
  const [over, setOver] = useState(false)
  const [sending, setSending] = useState(false)
  const [err, setErr] = useState('')
  const input = useRef(null)
  const { meta } = useApp()
  const speech = meta.speech !== false
  const pick = (f) => { if (f) { setFile(f); if (!title.trim()) setTitle(f.name.replace(/\.[^.]+$/, '')) } }
  const ok = title.trim() && (mode === 'file' ? file : text.trim().length >= 50)
  const submit = () => {
    setSending(true); setErr('')
    const fd = new FormData()
    fd.set('title', title)
    fd.set('held_at', date)
    if (mode === 'file') fd.set('file', file)
    else fd.set('transcript', text)
    api.form('/meetings', fd).then(onDone).catch((e) => { setErr(e.message); setSending(false) })
  }
  return (
    <Modal title="Новое совещание" onClose={onClose}
      footer={<><button className="btn ghost" onClick={onClose}>Отмена</button>
        <button className="btn primary" disabled={!ok || sending} onClick={submit}>{sending && <Loader2 size={16} className="spin" />} Разобрать</button></>}>
      {mode === 'file' ? (
        <div className={`dropzone ${over ? 'over' : ''} ${file ? 'has' : ''}`} onClick={() => input.current.click()}
          onDragOver={(e) => { e.preventDefault(); setOver(true) }} onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); pick(e.dataTransfer.files[0]) }} role="button" tabIndex={0}
          onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && input.current.click()}>
          {file ? <FileAudio size={28} /> : <Upload size={28} />}
          <b>{file ? file.name : speech ? 'Перетащите запись сюда или нажмите' : 'Перетащите протокол сюда или нажмите'}</b>
          {!file && <span className="small">{speech ? 'Аудио, видео, .txt или .docx' : '.txt или .docx · на демо-стенде записи не распознаются'}</span>}
          <input ref={input} type="file" hidden accept={speech ? 'audio/*,video/*,.txt,.md,.docx,.vtt,.srt,.oga,.opus' : '.txt,.md,.docx,.vtt,.srt'} onChange={(e) => pick(e.target.files[0])} />
        </div>
      ) : (
        <textarea rows={6} value={text} onChange={(e) => setText(e.target.value)} placeholder="Вставьте протокол или расшифровку" aria-label="Текст совещания" autoFocus />
      )}
      <div className="center"><button className="btn ghost sm" onClick={() => setMode(mode === 'file' ? 'text' : 'file')}>{mode === 'file' ? 'Или вставить текст' : 'Или загрузить файл'}</button></div>
      <div className="grid c2">
        <label className="field">Название<input type="text" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Планерка отдела" /></label>
        <label className="field">Дата<input type="date" value={date} onChange={(e) => setDate(e.target.value)} /></label>
      </div>
      <ErrorNote text={err} />
    </Modal>
  )
}

function Progress({ status }) {
  const steps = [['transcribing', 'Распознаю речь'], ['extracting', 'Выделяю главное'], ['ready', 'Готово']]
  const cur = status === 'queued' ? 0 : steps.findIndex(([k]) => k === status)
  return (
    <div className="progress">
      {steps.map(([k, label], i) => (
        <span key={k} className="row" style={{ gap: 8 }}>
          {i > 0 && <span className="line" />}
          <span className={`p ${i < cur ? 'done' : i === cur ? 'now' : ''}`}>
            <span className="c">{i < cur ? <Check size={13} /> : i === cur ? <Loader2 size={13} className="spin" /> : null}</span>{label}
          </span>
        </span>
      ))}
    </div>
  )
}

export function MeetingDetail() {
  const { id } = useParams()
  const [m, reload, error] = useLoad(() => api.get(`/meetings/${id}`), [id])
  const [showText, setShowText] = useState(false)
  const [err, setErr] = useState('')
  useEffect(() => {
    if (!m || !busy(m.status)) return undefined
    const t = setInterval(reload, 2500)
    return () => clearInterval(t)
  }, [m, reload])
  if (error) return <div className="page"><Link to="/meetings" className="back"><ArrowLeft size={17} /> Совещания</Link><ErrorNote text={error} /></div>
  if (!m) return <Spinner />
  const drafts = m.items.filter((i) => i.state === 'draft')
  const done = m.items.filter((i) => i.state !== 'draft')
  const retry = () => api.post(`/meetings/${m.id}/retry`).then(reload).catch((e) => setErr(e.message))
  return (
    <div className="page">
      <Link to="/meetings" className="back"><ArrowLeft size={17} /> Совещания</Link>
      <div className="stack">
        <div className="card pad stack">
          <div className="row spread wrap"><span className="muted">{day(m.held_at)}{m.uploaded_by ? ` · ${m.uploaded_by}` : ''}</span><State m={m} /></div>
          <h1 className="title">{m.title}</h1>
          {busy(m.status) && <><Progress status={m.status} /><span className="small muted">Страницу можно закрыть - результат сохранится</span></>}
          {m.status === 'error' && (
            <div className="note red"><X size={18} /><div><b>Не удалось обработать</b>{m.error}
              <div style={{ marginTop: 8 }}><button className="btn sm" onClick={retry}><RefreshCw size={14} /> Повторить</button></div></div></div>
          )}
          {m.summary && m.status === 'ready' && <p className="ink2">{m.summary}</p>}
          <ErrorNote text={err} />
        </div>

        {m.status === 'ready' && m.items.length === 0 && <div className="card"><Empty icon={Mic} title="Ничего важного не нашлось" /></div>}
        {drafts.length > 0 && <div className="section-title"><h2>Нужно решить <span className="muted">{drafts.length}</span></h2></div>}
        {drafts.map((i) => <ItemCard key={i.id} item={i} onChange={reload} />)}
        {done.length > 0 && (
          <div className="card pad stack tight">
            <h2>Разобрано</h2>
            <div className="list">{done.map((i) => <DoneRow key={i.id} item={i} />)}</div>
          </div>
        )}

        {m.transcript && (
          <div className="card">
            <button className="btn ghost block" style={{ justifyContent: 'flex-start', height: 50, padding: '0 20px' }} onClick={() => setShowText(!showText)} aria-expanded={showText}>
              {showText ? <ChevronDown size={17} /> : <ChevronRight size={17} />} Расшифровка
            </button>
            {showText && (
              <div className="card-body stack" style={{ borderTop: '1px solid var(--line)' }}>
                <div className="pre small ink2">{m.transcript}</div>
                {m.status === 'ready' && <div><button className="btn sm" onClick={retry}><RefreshCw size={14} /> Разобрать заново</button></div>}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function DoneRow({ item }) {
  return (
    <div className="row spread" style={{ padding: '10px 0', borderBottom: '1px solid var(--line)', opacity: item.state === 'rejected' ? 0.55 : 1 }}>
      <span className="pill">{KIND[item.kind] || item.kind}</span>
      <span className="grow">{item.title || item.text.slice(0, 90)}</span>
      {item.state === 'rejected' ? <span className="small muted nowrap">пропущено</span>
        : item.appeal ? <Link to={`/appeals/${item.appeal.id}`} className="row nowrap" style={{ gap: 8 }}><b>{item.appeal.number}</b><StatusPill status={item.appeal.status} /></Link>
          : <span className="small muted nowrap">в протоколе</span>}
    </div>
  )
}

function ItemCard({ item, onChange }) {
  const { meta } = useApp()
  // имя из расшифровки однозначно совпало с сотрудником - подставляем его автором
  const guess = item.speaker ? (meta.employees || []).filter((u) => u.name.toLowerCase().split(' ').includes(item.speaker.toLowerCase().split(' ')[0])) : []
  const [f, setF] = useState({ title: item.title, text: item.text, kind: item.kind, category_id: item.category_id || '', priority: item.priority, author_id: guess.length === 1 ? guess[0].id : '' })
  const [edit, setEdit] = useState(false)
  const [err, setErr] = useState('')
  const [wait, setWait] = useState(false)
  const setV = (k) => (v) => setF({ ...f, [k]: v })
  const toAppeal = TO_APPEAL.includes(f.kind)
  const cat = meta.categories.find((c) => String(c.id) === String(f.category_id))
  const author = (meta.employees || []).find((u) => String(u.id) === String(f.author_id))

  const run = (p) => { setWait(true); p.then(onChange).catch((e) => { setErr(e.message); setWait(false) }) }
  const accept = () => run(api.post(`/meetings/items/${item.id}/accept`, {
    title: f.title, text: f.text, kind: f.kind, priority: f.priority,
    category_id: f.category_id ? Number(f.category_id) : null, author_id: f.author_id ? Number(f.author_id) : null,
  }))
  return (
    <div className="card pad stack">
      <div className="row spread">
        <div className="row wrap" style={{ gap: 8 }}>
          <span className="pill">{KIND[f.kind] || f.kind}</span>
          {(author || item.speaker) && <span className="chip"><UserRound size={14} /> {author ? author.name : item.speaker}</span>}
          {toAppeal && cat && <span className="chip muted sep-dot">{cat.name}</span>}
        </div>
        <button className={`btn ghost icon sm ${edit ? 'on' : ''}`} onClick={() => setEdit(!edit)} aria-label="Изменить пункт" aria-pressed={edit} title="Изменить"><Pencil size={15} /></button>
      </div>
      {edit ? (
        <div className="stack tight">
          <input type="text" value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} aria-label="Заголовок" style={{ fontWeight: 600 }} />
          <textarea rows={3} value={f.text} onChange={(e) => setF({ ...f, text: e.target.value })} aria-label="Текст" />
          <div className="grid c2">
            <label className="field">Вид
              <Select value={f.kind} onChange={setV('kind')} ariaLabel="Вид пункта" options={Object.entries(KIND).map(([value, label]) => ({ value, label }))} />
            </label>
            {toAppeal && <label className="field">Кто поднял вопрос
              <Select value={f.author_id} onChange={setV('author_id')} ariaLabel="Кто поднял вопрос"
                options={[{ value: '', label: 'Не указывать' }, ...fromList(meta.employees || [], (u) => u.name, (u) => u.department || '')]} />
            </label>}
            {toAppeal && <label className="field">Категория
              <Select value={f.category_id} onChange={setV('category_id')} ariaLabel="Категория"
                options={[{ value: '', label: 'Подобрать автоматически' }, ...fromList(meta.categories, (c) => c.name, (c) => c.responsible || '')]} />
            </label>}
            {toAppeal && <label className="field">Приоритет
              <Select value={f.priority} onChange={setV('priority')} ariaLabel="Приоритет" options={fromMap(meta.priorities)} />
            </label>}
          </div>
        </div>
      ) : (
        <>
          <h2>{f.title || f.text.slice(0, 90)}</h2>
          <div className="ink2">{f.text}</div>
        </>
      )}
      {item.quote && item.quote !== item.text && <div className="quote">«{item.quote}»</div>}
      <ErrorNote text={err} />
      <div className="actions">
        <button className="btn primary" disabled={wait} onClick={accept}><Check size={17} /> {toAppeal ? 'Создать обращение' : 'В протокол'}</button>
        <button className="btn ghost" disabled={wait} onClick={() => run(api.post(`/meetings/items/${item.id}/reject`))}><X size={17} /> Пропустить</button>
      </div>
    </div>
  )
}
