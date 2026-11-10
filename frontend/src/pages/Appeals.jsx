import { useEffect, useMemo, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import {
  ArrowLeft, ArrowRightLeft, ArrowUp, Check, CheckCircle2, EyeOff, HelpCircle, Inbox, Lock, MoreHorizontal, Paperclip, Pencil, RotateCcw, Search,
  SlidersHorizontal, ThumbsUp, Users, X, XCircle,
} from 'lucide-react'
import { api, qs } from '../api.js'
import Select, { fromList, fromMap } from '../Select.jsx'
import {
  AppealItem, Avatar, Empty, ErrorNote, Menu, Modal, Overdue, Spinner, StatusPill, Steps, TypeIcon, ago, day, dueText, useApp, useLoad,
} from '../ui.jsx'

/* ---------- список: входящие и все обращения ---------- */

const EMPTY_FILTERS = { type: '', category_id: '', department_id: '', source: '', status: '' }

export function AppealList({ scope }) {
  const { meta } = useApp()
  const nav = useNavigate()
  const [tab, setTab] = useState('open')
  const [f, setF] = useState(EMPTY_FILTERS)
  const [showFilters, setShowFilters] = useState(false)
  const [q, setQ] = useState('')
  const [query, setQuery] = useState('')
  useEffect(() => { const t = setTimeout(() => setQuery(q), 300); return () => clearTimeout(t) }, [q])
  const { status, ...server } = f
  const [rows, , error] = useLoad(() => api.get(`/appeals${qs({ scope, ...server, q: query })}`), [scope, JSON.stringify(server), query])

  const isFinal = (a) => meta.final.includes(a.status)
  const groups = useMemo(() => ({
    open: (a) => !isFinal(a),
    new: (a) => a.status === 'new',
    clarify: (a) => a.status === 'clarify',
    overdue: (a) => a.overdue,
    closed: isFinal,
  }), [meta]) // eslint-disable-line react-hooks/exhaustive-deps
  const count = (k) => (rows ? rows.filter(groups[k]).length : 0)
  const shown = useMemo(() => {
    if (!rows) return null
    const list = rows.filter(status ? (a) => a.status === status : groups[tab])
    const rank = (a) => (a.overdue ? 0 : a.status === 'new' ? 1 : 2)
    return [...list].sort((a, b) => rank(a) - rank(b) || b.id - a.id)
  }, [rows, tab, status, groups])
  const active = Object.values(f).filter(Boolean).length
  const setV = (k) => (v) => setF({ ...f, [k]: v })
  const TABS = [['open', 'Открытые'], ['new', 'Новые'], ['clarify', 'Ждут автора'], ['overdue', 'Просрочены'], ['closed', 'Завершены']]

  return (
    <div className="page">
      <div className="head"><h1>{scope === 'inbox' ? 'Входящие' : 'Все обращения'}</h1></div>
      <div className="toolbar">
        <div className="segs">
          {TABS.map(([k, label]) => (
            <button key={k} className={!status && tab === k ? 'on' : ''} onClick={() => { setTab(k); setF({ ...f, status: '' }) }}>
              {label} {count(k) > 0 && <span className={`n ${k === 'overdue' ? 'red' : ''}`}>{count(k)}</span>}
            </button>
          ))}
        </div>
      </div>
      <div className="toolbar">
        <div className="search"><Search size={17} /><input type="text" placeholder="Поиск" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Поиск по обращениям" /></div>
        <button className={`btn ${showFilters ? 'on' : ''}`} onClick={() => setShowFilters(!showFilters)} aria-expanded={showFilters}>
          <SlidersHorizontal size={16} /> Фильтры {active > 0 && <span className="nav-count" style={{ margin: 0 }}>{active}</span>}
        </button>
      </div>
      {showFilters && (
        <div className="filters">
          <Select size="sm" value={f.status} onChange={setV('status')} ariaLabel="Статус" options={[{ value: '', label: 'Любой статус' }, ...fromMap(meta.statuses)]} />
          <Select size="sm" value={f.type} onChange={setV('type')} ariaLabel="Тип" options={[{ value: '', label: 'Любой тип' }, ...fromMap(meta.types)]} />
          <Select size="sm" value={f.category_id} onChange={setV('category_id')} ariaLabel="Категория" options={[{ value: '', label: 'Все категории' }, ...fromList(meta.categories)]} />
          <Select size="sm" value={f.department_id} onChange={setV('department_id')} ariaLabel="Отдел автора" options={[{ value: '', label: 'Все отделы' }, ...fromList(meta.departments)]} />
          <Select size="sm" value={f.source} onChange={setV('source')} ariaLabel="Канал" options={[{ value: '', label: 'Все каналы' }, ...fromMap(meta.sources)]} />
          {active > 0 && <button className="btn ghost sm" onClick={() => setF(EMPTY_FILTERS)}><X size={14} /> Сбросить</button>}
        </div>
      )}
      <ErrorNote text={error} />
      {!shown ? <Spinner /> : shown.length === 0 ? (
        <div className="card"><Empty icon={Inbox} title={query || active ? 'Ничего не найдено' : tab === 'open' && scope === 'inbox' ? 'Все разобрано' : 'Здесь пусто'} /></div>
      ) : (
        <div className="card rows">
          {shown.map((a) => <AppealItem key={a.id} a={a} who="author" onClick={() => nav(`/appeals/${a.id}`)} />)}
        </div>
      )}
    </div>
  )
}

/* ---------- карточка обращения ---------- */

const PRIMARY = { new: 'review', review: 'in_progress', clarify: 'in_progress', in_progress: 'done', done: 'in_progress', rejected: 'review', closed: 'review' }
function actionLabel(from, to) {
  if (to === 'review') return from === 'new' ? 'Принять' : from === 'clarify' ? 'Вернуть на рассмотрение' : 'Пересмотреть'
  if (to === 'in_progress') return from === 'done' ? 'Вернуть в работу' : 'Взять в работу'
  if (to === 'done') return 'Готово'
  if (to === 'rejected') return 'Отклонить'
  if (to === 'closed') return 'Закрыть без действий'
  return to
}
const ACTION_ICON = { review: RotateCcw, in_progress: Check, done: CheckCircle2, rejected: XCircle, closed: X }

function Fact({ k, children }) {
  return <div className="fact"><div className="k">{k}</div><div className="v">{children}</div></div>
}

function Event({ e, a, full }) {
  const { meta } = useApp()
  const time = <span className="small muted"> · {ago(e.created_at)}</span>
  if (e.kind === 'created') {
    const how = { meeting: 'записано с совещания', telegram: 'отправлено через Telegram', email: 'отправлено письмом' }[a.source] || 'отправлено'
    return <div className="sys"><span>{e.actor} · {how}{time}</span></div>
  }
  if (e.kind === 'routed') return <div className="sys"><span>{full ? e.text : e.text.replace(/\s*\(.*\)$/, '')}{time}</span></div>
  if (e.kind === 'escalated') return <div className="sys red"><span>{e.text}{time}</span></div>
  if (e.kind === 'assigned' || e.kind === 'edited' || e.kind === 'supported') return <div className="sys"><span>{e.text}{time}</span></div>
  if (e.kind === 'status' && !e.text) return <div className="sys"><span>{e.actor}: «{meta.statuses[e.data.to]}»{time}</span></div>
  if (e.kind === 'rated' && !e.text) return <div className="sys"><span>{e.actor} подтверждает: вопрос решен{time}</span></div>
  const head = e.kind === 'status' ? <> · «{meta.statuses[e.data.to]}»</>
    : e.kind === 'question' ? ' просит уточнить' : e.kind === 'answer' ? ' отвечает'
      : e.kind === 'rated' ? (e.data.ok ? ' подтверждает: вопрос решен' : ' сообщает: вопрос не решен') : ''
  return (
    <div className={`msg ${e.kind} ${e.internal ? 'internal' : ''}`}>
      <Avatar name={e.actor} size="sm" role={e.actor_role || ''} />
      <div className="body">
        <div><span className="who">{e.actor}</span><span className="ink2">{head}</span>{time}
          {e.internal && <span className="pill" style={{ marginLeft: 8 }}><Lock size={11} /> только для своих</span>}
        </div>
        <div className="bubble pre">{e.text}</div>
      </div>
    </div>
  )
}

export function AppealDetail() {
  const { id } = useParams()
  const { meta, user } = useApp()
  const nav = useNavigate()
  const loc = useLocation()
  const [a, reload, error, setA] = useLoad(() => api.get(`/appeals/${id}`), [id])
  const [modal, setModal] = useState(null)
  const [msg, setMsg] = useState('')
  const [internal, setInternal] = useState(false)
  const [err, setErr] = useState('')

  if (error) return <div className="page"><Link className="back" to="/"><ArrowLeft size={17} /> На главную</Link><ErrorNote text={error} /></div>
  if (!a) return <Spinner />
  const act = (p) => p.then((d) => { setA(d); setModal(null); setErr(''); setMsg('') }).catch((e) => setErr(e.message))
  const answering = a.can.answer && !a.can.handle
  const send = () => msg.trim() && act(api.post(`/appeals/${a.id}/${answering ? 'answer' : 'comment'}`, { text: msg, internal }))
  const open = (kind, extra = {}) => { setErr(''); setModal({ kind, ...extra }) }
  const isFinal = meta.final.includes(a.status)
  const primary = a.can.handle ? PRIMARY[a.status] : null
  const others = a.can.transitions.filter((s) => s !== 'clarify' && s !== primary)
  const PrimaryIcon = ACTION_ICON[primary] || Check

  return (
    <div className="page">
      <a className="back" href="#" onClick={(e) => { e.preventDefault(); if (window.history.length > 1) nav(-1); else nav('/') }}><ArrowLeft size={17} /> Назад</a>
      <div className="stack">
        {loc.state?.created && (
          <div className="note green"><CheckCircle2 size={18} /><div>
            <b>Обращение отправлено</b>{a.assignee ? `Его рассмотрит ${a.assignee.name}. Об ответе придет уведомление.` : 'Об ответе придет уведомление.'}
          </div></div>
        )}
        <div className="card pad stack">
          <div className="row spread wrap">
            <div className="row" style={{ gap: 8 }}>
              <TypeIcon type={a.type} size={18} />
              <span><b>{meta.types[a.type]}</b> <span className="muted">· {a.number}</span></span>
            </div>
            <div className="row wrap" style={{ gap: '6px 14px' }}>
              {a.author.anonymous && <span className="pill"><EyeOff size={12} /> анонимно</span>}
              {a.supports > 0 && <span className="pill"><Users size={12} /> +{a.supports} с тем же</span>}
              <Overdue a={a} /><StatusPill status={a.status} />
            </div>
          </div>
          <h1 className="title">{a.title}</h1>
          <Steps a={a} mine={a.mine} />
          <div className="pre">{a.text}</div>
          {a.solution && <div><h3>Предлагаемое решение</h3><div className="pre" style={{ marginTop: 6 }}>{a.solution}</div></div>}
          {a.attachments.length > 0 && (
            <div className="row wrap">
              {a.attachments.map((f) => (
                <button key={f.id} className="btn sm" onClick={() => api.download(f.id, f.filename)}><Paperclip size={14} /> {f.filename}</button>
              ))}
            </div>
          )}
          {a.resolution && (
            <div className={`note ${a.status === 'rejected' ? 'red' : 'green'}`}>
              {a.status === 'rejected' ? <XCircle size={18} /> : <CheckCircle2 size={18} />}
              <div><b>{a.status === 'rejected' ? 'Отклонено' : a.status === 'done' ? 'Что сделано' : 'Итог'}</b><span className="pre">{a.resolution}</span></div>
            </div>
          )}
          {a.can.rate && (
            <div className="note"><HelpCircle size={18} /><div>
              <b>Вопрос решен?</b>Если нет - обращение вернется в работу.
              <div className="row" style={{ marginTop: 10 }}>
                <button className="btn sm primary" onClick={() => act(api.post(`/appeals/${a.id}/rate`, { ok: true }))}><Check size={15} /> Да, спасибо</button>
                <button className="btn sm" onClick={() => open('reopen')}>Нет, не решен</button>
              </div>
            </div></div>
          )}
          {a.open_question && a.can.answer && (
            <div className="note amber"><HelpCircle size={18} /><div><b>Нужен ваш ответ</b>{a.open_question.text}</div></div>
          )}
          <hr className="divider" />
          <div className="facts">
            <Fact k="Автор">{a.author.name}{a.author.position && <div className="small muted">{a.author.position}</div>}</Fact>
            <Fact k="Ответственный">{a.assignee ? <>{a.assignee.name}<div className="small muted">{a.assignee.position}</div></> : <span className="muted">не назначен</span>}</Fact>
            <Fact k="Категория">{a.category || <span className="muted">нет</span>}</Fact>
            {a.closed_at ? <Fact k="Закрыто">{day(a.closed_at)}</Fact>
              : <Fact k="Срок">{day(a.due_at)} <span className="small muted">· {dueText(a)}</span></Fact>}
            {a.can.handle && <Fact k="Приоритет">{meta.priorities[a.priority]}</Fact>}
            {a.can.handle && <Fact k="Канал">{meta.sources[a.source]}{a.meeting_id && <div className="small"><Link to={`/meetings/${a.meeting_id}`}>открыть совещание</Link></div>}</Fact>}
          </div>
          {a.can.handle && (
            <>
              <hr className="divider" />
              <div className="actions">
                {primary && (
                  <button className="btn primary" onClick={() => open('status', { status: primary })}>
                    <PrimaryIcon size={17} /> {actionLabel(a.status, primary)}
                  </button>
                )}
                {!isFinal && a.author.id && <button className="btn" onClick={() => open('clarify')}><HelpCircle size={17} /> Уточнить</button>}
                <Menu left trigger={<button className="btn icon" aria-label="Другие действия"><MoreHorizontal size={18} /></button>} items={[
                  ...others.map((s) => ({ label: actionLabel(a.status, s), icon: ACTION_ICON[s], danger: s === 'rejected', onClick: () => open('status', { status: s }) })),
                  others.length ? 'sep' : null,
                  { label: 'Передать другому', icon: ArrowRightLeft, onClick: () => open('assign') },
                  { label: 'Изменить категорию или срок', icon: Pencil, onClick: () => open('edit') },
                ]} />
              </div>
            </>
          )}
          {!a.can.handle && a.can.support && (
            <div className="actions">
              {a.supported ? <span className="chip"><ThumbsUp size={15} /> Вы присоединились</span>
                : <button className="btn" onClick={() => api.post(`/appeals/${a.id}/support`).then(reload).catch((e) => setErr(e.message))}><ThumbsUp size={16} /> У меня то же</button>}
            </div>
          )}
        </div>

        <div className="card pad stack">
          <h2>Обсуждение</h2>
          <div className="feed">{a.events.map((e) => <Event key={e.id} e={e} a={a} full={a.can.handle} />)}</div>
          {a.can.comment && (
            <>
              <div className="reply">
                <textarea rows={1} value={msg} onChange={(e) => { setMsg(e.target.value); e.target.style.height = 'auto'; e.target.style.height = `${Math.min(e.target.scrollHeight, 200)}px` }}
                  placeholder={answering ? 'Ваш ответ' : internal ? 'Заметка для своих - автор не увидит' : a.can.handle ? 'Написать автору' : 'Написать комментарий'}
                  onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send() } }} aria-label="Сообщение" />
                {a.can.handle && (
                  <button className={`btn ghost icon sm ${internal ? 'on' : ''}`} onClick={() => setInternal(!internal)} aria-pressed={internal}
                    title="Заметка для своих: автор не увидит"><Lock size={16} /></button>
                )}
                <button className="send" disabled={!msg.trim()} onClick={send} aria-label="Отправить" title="Отправить - Enter"><ArrowUp size={19} /></button>
              </div>
              {!modal && <ErrorNote text={err} />}
            </>
          )}
        </div>

        {a.similar.length > 0 && user.role !== 'employee' && (
          <div className="card pad stack tight">
            <h2>Похожие</h2>
            <div className="list">
              {a.similar.map((s) => (
                <div key={s.id} className="row spread" style={{ padding: '8px 0' }}>
                  <span className="grow">{s.can_open ? <Link to={`/appeals/${s.id}`}><b>{s.number}</b> {s.title}</Link> : <span><b>{s.number}</b> {s.title}</span>}</span>
                  <StatusPill status={s.status} />
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {modal?.kind === 'status' && <StatusModal a={a} status={modal.status} err={err} onClose={() => setModal(null)}
        onOk={(text) => act(api.post(`/appeals/${a.id}/status`, { status: modal.status, text }))} />}
      {modal?.kind === 'clarify' && <TextModal title="Вопрос автору" placeholder="Что нужно уточнить?" ok="Спросить" err={err}
        onClose={() => setModal(null)} onOk={(text) => act(api.post(`/appeals/${a.id}/clarify`, { text }))} />}
      {modal?.kind === 'reopen' && <TextModal title="Что осталось нерешенным?" placeholder="Ответственный увидит этот текст" ok="Вернуть в работу" err={err}
        onClose={() => setModal(null)} onOk={(text) => act(api.post(`/appeals/${a.id}/rate`, { ok: false, text }))} />}
      {modal?.kind === 'assign' && <AssignModal a={a} err={err} onClose={() => setModal(null)}
        onOk={(uid) => act(api.post(`/appeals/${a.id}/assign`, { user_id: Number(uid) }))} />}
      {modal?.kind === 'edit' && <EditModal a={a} err={err} onClose={() => setModal(null)}
        onOk={(body) => act(api.patch(`/appeals/${a.id}`, body))} />}
    </div>
  )
}

const STATUS_PROMPT = {
  done: ['Что сделано?', 'Автор увидит этот текст', true],
  rejected: ['Почему отклонено?', 'Автор увидит причину', true],
  closed: ['Комментарий', 'Необязательно', false],
  in_progress: ['Что планируете сделать?', 'Необязательно', false],
  review: ['Комментарий', 'Необязательно', false],
}

function StatusModal({ a, status, err, onOk, onClose }) {
  const [title, placeholder, required] = STATUS_PROMPT[status] || ['Комментарий', '', false]
  const [text, setText] = useState('')
  return (
    <Modal title={`${actionLabel(a.status, status)} · ${a.number}`} onClose={onClose}
      footer={<><button className="btn ghost" onClick={onClose}>Отмена</button>
        <button className={`btn ${status === 'rejected' ? 'danger' : 'primary'}`} disabled={required && !text.trim()} onClick={() => onOk(text)}>{actionLabel(a.status, status)}</button></>}>
      <label className="field">{title}<textarea rows={4} value={text} onChange={(e) => setText(e.target.value)} placeholder={placeholder} autoFocus /></label>
      <ErrorNote text={err} />
    </Modal>
  )
}

function TextModal({ title, placeholder, ok, err, onOk, onClose }) {
  const [text, setText] = useState('')
  return (
    <Modal title={title} onClose={onClose}
      footer={<><button className="btn ghost" onClick={onClose}>Отмена</button>
        <button className="btn primary" disabled={!text.trim()} onClick={() => onOk(text)}>{ok}</button></>}>
      <textarea rows={4} value={text} onChange={(e) => setText(e.target.value)} placeholder={placeholder} aria-label={title} autoFocus />
      <ErrorNote text={err} />
    </Modal>
  )
}

function AssignModal({ a, err, onOk, onClose }) {
  const { meta } = useApp()
  const [uid, setUid] = useState('')
  return (
    <Modal title="Передать другому" onClose={onClose}
      footer={<><button className="btn ghost" onClick={onClose}>Отмена</button>
        <button className="btn primary" disabled={!uid} onClick={() => onOk(uid)}>Передать</button></>}>
      <Select value={uid} onChange={setUid} ariaLabel="Кому передать" placeholder="Выберите сотрудника"
        options={meta.handlers.filter((h) => h.id !== a.assignee?.id).map((h) => ({
          value: h.id, label: h.name, hint: `${h.position}${h.is_absent ? ' · сейчас отсутствует' : ''}`,
        }))} />
      <ErrorNote text={err} />
    </Modal>
  )
}

function EditModal({ a, err, onOk, onClose }) {
  const { meta } = useApp()
  const [f, setF] = useState({ type: a.type, category_id: a.category_id || '', priority: a.priority, due_days: '' })
  const setV = (k) => (v) => setF({ ...f, [k]: v })
  const body = () => {
    const b = {}
    if (f.type !== a.type) b.type = f.type
    if (String(f.category_id) !== String(a.category_id || '')) b.category_id = Number(f.category_id)
    if (f.priority !== a.priority) b.priority = f.priority
    if (f.due_days) b.due_days = Number(f.due_days)
    return b
  }
  const moved = String(f.category_id) !== String(a.category_id || '')
  const cat = meta.categories.find((c) => String(c.id) === String(f.category_id))
  return (
    <Modal title={`Изменить · ${a.number}`} onClose={onClose}
      footer={<><button className="btn ghost" onClick={onClose}>Отмена</button>
        <button className="btn primary" disabled={Object.keys(body()).length === 0} onClick={() => onOk(body())}>Сохранить</button></>}>
      <label className="field">Категория
        <Select value={f.category_id} onChange={setV('category_id')} ariaLabel="Категория" placeholder="Не выбрана"
          options={fromList(meta.categories, (c) => c.name, (c) => c.responsible || 'ответственный не назначен')} />
      </label>
      {moved && cat && <div className="note small"><ArrowRightLeft size={15} /><span>Обращение перейдет к: {cat.responsible || 'администратору'}</span></div>}
      <div className="grid c2">
        <label className="field">Тип
          <Select value={f.type} onChange={setV('type')} ariaLabel="Тип" options={fromMap(meta.types)} />
        </label>
        <label className="field">Приоритет
          <Select value={f.priority} onChange={setV('priority')} ariaLabel="Приоритет" options={fromMap(meta.priorities)} />
        </label>
      </div>
      <label className="field">Новый срок, дней от сегодня <span className="opt">необязательно</span>
        <input type="number" min="1" max="365" value={f.due_days} onChange={(e) => setF({ ...f, due_days: e.target.value })} />
      </label>
      <ErrorNote text={err} />
    </Modal>
  )
}
