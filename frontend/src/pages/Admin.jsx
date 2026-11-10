import { useState } from 'react'
import { Link } from 'react-router-dom'
import { CheckCircle2, Mail, Mic, Plus, Send, Sparkles } from 'lucide-react'
import { api } from '../api.js'
import { Avatar, ErrorNote, Spinner, Status, useApp, useLoad } from '../ui.jsx'
import Select, { fromList, fromMap } from '../Select.jsx'
import DesignSystem from './DesignSystem.jsx'
import Integrations from './Integrations.jsx'

const SYNCED = { '1c': '1С', b24: 'Битрикс24', file: 'файл' }

const ROLE_HINT = {
  employee: 'пишет обращения', manager: 'разбирает свои категории', director: 'видит все', admin: 'настраивает систему',
}

export default function Admin() {
  const [tab, setTab] = useState('routes')
  return (
    <div className="page wide">
      <div className="head"><h1>Настройки</h1></div>
      <div className="tabs">
        {[['routes', 'Кто что разбирает'], ['users', 'Сотрудники'], ['org', '1С и Битрикс24'], ['channels', 'Каналы'], ['design', 'Дизайн-система']].map(([k, l]) => (
          <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{l}</button>
        ))}
      </div>
      {tab === 'routes' && <Routes />}
      {tab === 'users' && <Users />}
      {tab === 'org' && <Integrations />}
      {tab === 'channels' && <Channels />}
      {tab === 'design' && <DesignSystem />}
    </div>
  )
}

function Routes() {
  const { meta, reloadMeta } = useApp()
  const [rows, reload, error] = useLoad(() => api.get('/admin/categories'), [])
  const [err, setErr] = useState('')
  const [name, setName] = useState('')
  const save = (id, body) => api.patch(`/admin/categories/${id}`, body).then(() => { reload(); reloadMeta(); setErr('') }).catch((e) => setErr(e.message))
  const add = (e) => {
    e.preventDefault()
    api.post('/admin/categories', { name }).then(() => { setName(''); reload(); reloadMeta(); setErr('') }).catch((er) => setErr(er.message))
  }
  if (!rows) return error ? <ErrorNote text={error} /> : <Spinner />
  return (
    <div className="stack">
      <ErrorNote text={err} />
      <div className="card table-wrap">
        <table className="tbl tbl-cards">
          <thead><tr><th>Категория</th><th>Ответственный</th><th>Срок, дней</th><th>Сейчас уйдет к</th><th /></tr></thead>
          <tbody>{rows.map((c) => (
            <tr key={c.id} style={{ opacity: c.active ? 1 : 0.5 }}>
              <td><b>{c.name}</b><div className="small muted">{c.description}</div></td>
              <td data-label="Ответственный"><Select value={c.responsible_id || ''} ariaLabel={`Ответственный: ${c.name}`}
                onChange={(v) => save(c.id, { responsible_id: v ? Number(v) : null })}
                options={[{ value: '', label: 'Не назначен' },
                  ...fromList(meta.handlers, (h) => h.name, (h) => `${h.position}${h.is_absent ? ' · отсутствует' : ''}`)]} /></td>
              <td data-label="Срок, дней" style={{ width: 96 }}><input type="number" min="1" max="365" defaultValue={c.sla_days} aria-label={`Срок: ${c.name}`}
                onBlur={(e) => Number(e.target.value) !== c.sla_days && save(c.id, { sla_days: Number(e.target.value) })} /></td>
              <td data-label="Сейчас уйдет к"><b>{c.route || '-'}</b>{c.route_reason && !c.route_reason.startsWith('по категории') && <div className="small muted">{c.route_reason}</div>}</td>
              <td><button className="btn sm ghost" onClick={() => save(c.id, { active: !c.active })}>{c.active ? 'Скрыть' : 'Вернуть'}</button></td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      <form className="row" onSubmit={add} style={{ maxWidth: 460 }}>
        <input type="text" placeholder="Новая категория" value={name} onChange={(e) => setName(e.target.value)} aria-label="Название новой категории" />
        <button className="btn" disabled={!name.trim()}><Plus size={16} /> Добавить</button>
      </form>
    </div>
  )
}

function Users() {
  const { meta } = useApp()
  const [rows, reload, error] = useLoad(() => api.get('/admin/users'), [])
  const [err, setErr] = useState('')
  const save = (id, body) => api.patch(`/admin/users/${id}`, body).then(() => { reload(); setErr('') }).catch((e) => setErr(e.message))
  if (!rows) return error ? <ErrorNote text={error} /> : <Spinner />
  return (
    <div className="stack">
      <ErrorNote text={err} />
      <div className="card table-wrap">
        <table className="tbl tbl-cards">
          <thead><tr><th>Сотрудник</th><th>Роль</th><th>Отсутствует</th><th>Кто замещает</th><th>Мессенджеры</th></tr></thead>
          <tbody>{rows.map((u) => (
            <tr key={u.id}>
              <td><div className="row"><Avatar name={u.name} role={u.role} />
                <div><b>{u.name}</b><div className="small muted">{u.position} · {u.department}
                  {u.synced?.length > 0 && ` · из ${u.synced.map((s) => SYNCED[s] || s).join(', ')}`}
                  {!u.active && ' · отключен'}</div></div></div></td>
              <td data-label="Роль"><Select value={u.role} ariaLabel={`Роль: ${u.name}`} onChange={(v) => save(u.id, { role: v })}
                options={fromMap(meta.roles).map((o) => ({ ...o, hint: ROLE_HINT[o.value] }))} /></td>
              <td data-label="Отсутствует"><input type="checkbox" checked={u.is_absent} onChange={(e) => save(u.id, { is_absent: e.target.checked })} aria-label={`Отсутствует: ${u.name}`} /></td>
              <td data-label={u.role !== 'employee' ? 'Кто замещает' : undefined}>{u.role !== 'employee' && (
                <Select value={u.deputy_id || ''} ariaLabel={`Заместитель: ${u.name}`}
                  onChange={(v) => save(u.id, { deputy_id: v ? Number(v) : null })}
                  options={[{ value: '', label: 'Никто' },
                    ...fromList(meta.handlers.filter((h) => h.id !== u.id), (h) => h.name, (h) => h.position)]} />
              )}</td>
              <td data-label="Мессенджеры">{u.telegram || u.max ? <Status dot="ok">{[u.telegram && 'Telegram', u.max && 'MAX'].filter(Boolean).join(', ')}</Status> : <span className="muted">-</span>}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  )
}

function State({ icon: Icon, on, title, children }) {
  return (
    <div className="row top" style={{ gap: 14 }}>
      <span className="type-ico"><Icon size={18} /></span>
      <div className="grow"><b style={{ fontWeight: 600 }}>{title}</b><div className="small muted">{children}</div></div>
      <Status dot={on ? 'ok' : ''}>{on ? 'работает' : 'выключено'}</Status>
    </div>
  )
}

function Channels() {
  const [c, , error] = useLoad(() => api.get('/admin/channels'), [])
  const [mail, setMail] = useState({ sender: 'acc@modus.demo', subject: '', body: '' })
  const [made, setMade] = useState(null)
  const [err, setErr] = useState('')
  if (!c) return error ? <ErrorNote text={error} /> : <Spinner />
  const send = (e) => {
    e.preventDefault()
    api.post('/admin/intake/email', mail).then((a) => { setMade(a); setErr(''); setMail({ ...mail, subject: '', body: '' }) }).catch((er) => setErr(er.message))
  }
  return (
    <div className="cols c2">
      <div className="card pad stack">
        <State icon={Sparkles} on={c.ai.enabled} title="ИИ-разбор">{c.ai.enabled ? c.ai.engine : 'Ключ не задан, работают правила'}</State>
        <State icon={Send} on={c.telegram.enabled} title="Telegram-бот">
          {c.telegram.enabled ? `@${c.telegram.username}` : c.telegram.configured ? `Не запущен: ${c.telegram.error}` : 'Токен не задан'}
        </State>
        <State icon={Send} on={c.max.enabled} title="MAX-бот">
          {c.max.enabled ? `@${c.max.username}` : c.max.configured ? `Не запущен: ${c.max.error}` : 'Готов к подключению: нужен токен (MAX_BOT_TOKEN)'}
        </State>
        <State icon={Mail} on={c.email.enabled && !c.email.error} title="Почта">
          {c.email.enabled ? (c.email.error ? `Ошибка: ${c.email.error}` : c.email.address) : 'Ящик не подключен'}
        </State>
        <State icon={Mic} on title="Распознавание речи">Whisper {c.whisper.model}, на этом сервере</State>
      </div>
      <form className="card pad stack" onSubmit={send}>
        <h2>Проверить прием письма</h2>
        <label className="field">От кого<input type="text" value={mail.sender} onChange={(e) => setMail({ ...mail, sender: e.target.value })} /></label>
        <label className="field">Тема<input type="text" value={mail.subject} onChange={(e) => setMail({ ...mail, subject: e.target.value })} /></label>
        <label className="field">Текст<textarea rows={4} value={mail.body} onChange={(e) => setMail({ ...mail, body: e.target.value })} /></label>
        <ErrorNote text={err} />
        {made && <div className="note green"><CheckCircle2 size={18} /><span>Создано <Link to={`/appeals/${made.id}`}><b>{made.number}</b></Link> · {made.category} · {made.assignee?.name}</span></div>}
        <div><button className="btn primary" disabled={mail.body.trim().length < 20}>Принять письмо</button></div>
      </form>
    </div>
  )
}
