import { useEffect, useMemo, useState } from 'react'
import { BarChart3, ChevronRight, Inbox, PenLine, Search, Send, Settings } from 'lucide-react'
import { api } from './api.js'
import FlowLines from './FlowLines.jsx'
import { Avatar, ErrorNote, Spinner } from './ui.jsx'

// роль -> подпись, значок и человек, под которым входим по умолчанию
const GROUPS = [
  ['employee', 'Сотрудник', 'Пишет обращения', PenLine, 'sales@modus.demo'],
  ['manager', 'Руководитель', 'Разбирает обращения', Inbox, 'it@modus.demo'],
  ['director', 'Директор', 'Видит всю картину', BarChart3, 'director@modus.demo'],
  ['admin', 'Администратор', 'Настраивает систему', Settings, 'admin@modus.demo'],
]

/** Вход: одна карточка с тремя способами - по роли (демо), под конкретным человеком (демо) и по почте. */
export default function Login({ onDone, telegram }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [demo, setDemo] = useState(null)
  const [mode, setMode] = useState('role')
  const [q, setQ] = useState('')
  useEffect(() => { api.get('/auth/demo-users').then(setDemo).catch(() => setDemo([])) }, [])
  const submit = (e) => {
    e.preventDefault()
    setError('')
    api.post('/auth/login', { email, password }).then((r) => onDone(r.token)).catch((er) => setError(er.message))
  }
  const quick = (em) => api.post('/auth/demo-login', { email: em }).then((r) => onDone(r.token)).catch((er) => setError(er.message))
  const found = useMemo(() => {
    const s = q.trim().toLowerCase()
    return (demo || []).filter((u) => !s || `${u.name} ${u.position}`.toLowerCase().includes(s))
  }, [demo, q])

  if (demo === null) return <Spinner />
  const hasDemo = demo.length > 0
  const view = hasDemo ? mode : 'mail'
  return (
    <div className="login">
      <FlowLines />
      <div className="box stack loose">
        <div className="center stack tight">
          <div className="brand"><img src="/favicon.svg" alt="" /><h1>Модус.Отклик</h1></div>
          <p className="ink2">Проблемы и идеи сотрудников - в одном месте</p>
        </div>
        {telegram && (
          <div className="note"><Send size={18} /><div>
            <b>Войдите один раз</b>Этот Telegram привяжется к вашей учетной записи: дальше приложение будет открываться без пароля, а бот - писать вам о ваших обращениях.
          </div></div>
        )}
        {/* класс roles - точка, в которую сходятся линии фона (FlowLines) */}
        <div className="roles login-card">
          {hasDemo && (
            <div className="segs" role="tablist" aria-label="Способ входа">
              {[['role', 'Роль'], ['person', 'Человек'], ['mail', 'Почта']].map(([k, label]) => (
                <button key={k} role="tab" aria-selected={view === k} className={view === k ? 'on' : ''} onClick={() => { setMode(k); setError('') }}>{label}</button>
              ))}
            </div>
          )}

          {view === 'role' && (
            <div className="login-rows">
              {GROUPS.map(([role, label, hint, Icon, mail]) => {
                const who = demo.find((u) => u.email === mail) || demo.find((u) => u.role === role)
                return who && (
                  <button key={role} className="role-row" onClick={() => quick(who.email)}>
                    <span className={`role-mark r-${role}`}><Icon size={18} /></span>
                    <span className="grow"><b>{label}</b><span>{hint}</span></span>
                    <ChevronRight size={18} className="arr" />
                  </button>
                )
              })}
            </div>
          )}

          {view === 'person' && (
            <>
              <div className="search"><Search size={17} /><input type="text" placeholder="Имя или должность" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Поиск сотрудника" autoFocus /></div>
              <div className="login-rows scroll">
                {GROUPS.map(([role, label]) => {
                  const list = found.filter((u) => u.role === role)
                  return list.length > 0 && (
                    <div key={role}>
                      <h3><span className={`role-dot r-${role}`} />{label}</h3>
                      {list.map((u) => (
                        <button key={u.email} className="role-row" onClick={() => quick(u.email)}>
                          <Avatar name={u.name} role={u.role} />
                          <span className="grow"><b>{u.name}</b><span>{u.position}</span></span>
                        </button>
                      ))}
                    </div>
                  )
                })}
                {found.length === 0 && <div className="empty">Никого не нашлось</div>}
              </div>
            </>
          )}

          {view === 'mail' && (
            <form className="stack" onSubmit={submit}>
              <label className="field">Почта
                <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@modustrade.kz" autoComplete="username" required autoFocus />
              </label>
              <label className="field">Пароль
                <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
              </label>
              <button className="btn primary block" disabled={!email || !password}>Войти</button>
            </form>
          )}
        </div>
        <ErrorNote text={error} />
      </div>
    </div>
  )
}
