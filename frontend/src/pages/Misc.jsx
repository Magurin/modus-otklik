import { useState } from 'react'
import { Link } from 'react-router-dom'
import { CheckCircle2, ChevronRight, LayoutList, LogOut, Send, Settings, Users } from 'lucide-react'
import { api } from '../api.js'
import { Avatar, Empty, ErrorNote, Spinner, Status, day, useApp, useLoad } from '../ui.jsx'
import { AgentCard, CalendarCard } from './Connections.jsx'

export function Improvements() {
  const [rows, , error] = useLoad(() => api.get('/improvements'), [])
  return (
    <div className="page">
      <div className="head"><div><h1>Что сделано</h1><p className="sub">По обращениям сотрудников</p></div></div>
      <ErrorNote text={error} />
      {!rows ? <Spinner /> : rows.length === 0 ? <div className="card"><Empty icon={CheckCircle2} title="Скоро здесь появятся результаты" /></div> : (
        <div className="card rows">
          {rows.map((r) => (
            <div key={r.number} className="item" style={{ cursor: 'default' }}>
              <span className="type-ico"><CheckCircle2 size={18} /></span>
              <div className="grow">
                <span className="t">{r.title}</span>
                <div className="pre ink2" style={{ marginTop: 4 }}>{r.resolution}</div>
                <div className="meta" style={{ marginTop: 6 }}>
                  <span>{day(r.closed_at)}</span>
                  {r.category && <span className="sep-dot">{r.category}</span>}
                  {r.supports > 0 && <span className="sep-dot chip"><Users size={13} /> поддержали: {r.supports}</span>}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/** Один мессенджер в профиле: состояние, привязка по коду или ссылке, отключение. */
function Messenger({ m, onChange }) {
  const [link, setLink] = useState(null)
  const [err, setErr] = useState('')
  const getCode = () => api.post(`/me/messengers/${m.key}/code`).then(setLink).catch((e) => setErr(e.message))
  const unlink = () => api.post(`/me/messengers/${m.key}/unlink`).then(() => { setLink(null); onChange() }).catch((e) => setErr(e.message))
  return (
    <div className="stack tight" style={{ padding: 'var(--s-4) 0', borderBottom: '1px solid var(--line)' }}>
      {/* на узком экране состояние и кнопка переносятся под название, а не вылезают за край */}
      <div className="row spread wrap">
        <div className="row" style={{ minWidth: 0 }}><span className="type-ico"><Send size={18} /></span>
          <div style={{ minWidth: 0 }}><b style={{ fontWeight: 600 }}>{m.title}</b><div className="small muted">{m.bot ? `@${m.bot}` : 'бот не подключен'}</div></div>
        </div>
        {m.linked ? <div className="row wrap"><Status dot="ok">подключен</Status><button className="btn sm ghost" onClick={unlink}>Отключить</button></div>
          : m.enabled ? !link && <button className="btn sm" onClick={getCode}>Подключить</button>
            : <Status>недоступен</Status>}
      </div>
      {link && !m.linked && (
        <div className="note"><Send size={16} /><div>
          <a href={link.link} target="_blank" rel="noreferrer"><b style={{ display: 'inline' }}>Открыть бота</b></a> и нажать «Начать». Или отправить ему код
          <div className="num" style={{ fontSize: 24, fontWeight: 650, letterSpacing: 3, marginTop: 4 }}>{link.code}</div>
          <button className="btn sm ghost" style={{ marginTop: 6, marginLeft: -10 }} onClick={onChange}>Я отправил, проверить</button>
        </div></div>
      )}
      <ErrorNote text={err} />
    </div>
  )
}

/** Разделы, которым не хватило места в нижней панели телефона (и в Telegram): иначе до них не добраться. */
function PhoneSections() {
  const { user } = useApp()
  if (user.role === 'employee') return null  // у сотрудника все разделы помещаются в панель
  const links = [
    ['/all', LayoutList, 'Все обращения'],
    ['/done', CheckCircle2, 'Что сделано'],
    ...(user.role === 'admin' ? [['/admin', Settings, 'Настройки']] : []),
  ]
  return (
    <div className="card rows phone-only">
      {links.map(([to, Icon, label]) => (
        <Link key={to} to={to} className="item" style={{ alignItems: 'center', color: 'var(--ink)' }}>
          <span className="type-ico"><Icon size={18} /></span>
          <span className="grow t">{label}</span>
          <ChevronRight size={18} className="muted" />
        </Link>
      ))}
    </div>
  )
}

export function Profile() {
  const { user, meta, logout } = useApp()
  const [list, reload, error] = useLoad(() => api.get('/me/messengers'), [])
  return (
    <div className="page narrow">
      <div className="stack">
        <div className="card pad row" style={{ gap: 16 }}>
          <Avatar name={user.name} size="lg" role={user.role} />
          <div className="grow">
            <h1 className="title">{user.name}</h1>
            <div className="ink2">{user.position}</div>
            <div className="small muted">{user.department} · {meta.roles[user.role]} · {user.email}</div>
          </div>
        </div>
        {(!list || list.length > 0 || error) && (
          <div className="card pad">
            <h2>Мессенджеры</h2>
            <p className="small muted">Писать боту текстом или голосом и получать ответы</p>
            <ErrorNote text={error} />
            {list ? list.map((m) => <Messenger key={m.key} m={m} onChange={reload} />) : !error && <Spinner />}
          </div>
        )}
        <CalendarCard />
        <AgentCard />
        <PhoneSections />
        <div className="no-tg"><button className="btn ghost" onClick={logout}><LogOut size={16} /> Выйти</button></div>
      </div>
    </div>
  )
}
