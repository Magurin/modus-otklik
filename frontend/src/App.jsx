import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, NavLink, Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { BarChart3, Bell, CheckCircle2, Home as HomeIcon, Inbox, LayoutList, LogOut, Mic, Settings } from 'lucide-react'
import { api, token } from './api.js'
import { Avatar, Ctx, Empty, Spinner, ago, useApp } from './ui.jsx'
import Login from './Login.jsx'
import Home from './pages/Home.jsx'
import { AppealList, AppealDetail } from './pages/Appeals.jsx'
import { Meetings, MeetingDetail } from './pages/Meetings.jsx'
import Analytics from './pages/Analytics.jsx'
import Admin from './pages/Admin.jsx'
import { Improvements, Profile } from './pages/Misc.jsx'

export default function App() {
  const [user, setUser] = useState(null)
  const [meta, setMeta] = useState(null)
  const [ready, setReady] = useState(false)
  const navigate = useNavigate()

  const boot = useCallback(async () => {
    if (!token.get()) { setUser(null); setReady(true); return }
    try {
      const [u, m] = await Promise.all([api.get('/me'), api.get('/meta')])
      setUser(u); setMeta(m)
    } catch {
      setUser(null)
    }
    setReady(true)
  }, [])

  useEffect(() => { boot() }, [boot])
  useEffect(() => {
    const out = () => setUser(null)
    window.addEventListener('modus:logout', out)
    return () => window.removeEventListener('modus:logout', out)
  }, [])

  if (!ready) return <Spinner />
  if (!user || !meta) return <Login onDone={(t) => { token.set(t); setReady(false); boot() }} />

  // после выхода следующий пользователь должен попасть на свою стартовую страницу, а не на адрес предыдущего
  const logout = () => { token.clear(); setUser(null); navigate('/', { replace: true }) }
  const isHandler = user.role !== 'employee'
  const start = user.role === 'director' ? '/analytics' : isHandler ? '/inbox' : '/home'
  return (
    <Ctx.Provider value={{ user, meta, logout, reloadMeta: boot }}>
      <div className="app">
        <Nav />
        <main className="content">
          <Routes>
            <Route path="/" element={<Navigate to={start} replace />} />
            <Route path="/home" element={<Home />} />
            <Route path="/appeals/:id" element={<AppealDetail />} />
            <Route path="/done" element={<Improvements />} />
            <Route path="/profile" element={<Profile />} />
            {isHandler && <>
              <Route path="/inbox" element={<AppealList scope="inbox" />} />
              <Route path="/all" element={<AppealList scope="all" />} />
              <Route path="/meetings" element={<Meetings />} />
              <Route path="/meetings/:id" element={<MeetingDetail />} />
              <Route path="/analytics" element={<Analytics />} />
            </>}
            {user.role === 'admin' && <Route path="/admin" element={<Admin />} />}
            <Route path="*" element={<Navigate to={start} replace />} />
          </Routes>
        </main>
      </div>
    </Ctx.Provider>
  )
}

function Nav() {
  const { user, logout } = useApp()
  const isHandler = user.role !== 'employee'
  const [inbox, setInbox] = useState(0)
  useEffect(() => {
    if (!isHandler) return undefined
    const load = () => api.get('/appeals?scope=inbox&status=open').then((r) => setInbox(r.length)).catch(() => {})
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [isHandler])
  const item = (to, Icon, label, count, cls = '') => (
    <NavLink to={to} title={label} className={({ isActive }) => `nav-item ${cls} ${isActive ? 'active' : ''}`}>
      <Icon size={18} /><span className="lbl">{label}</span>{count > 0 && <span className="nav-count">{count}</span>}
    </NavLink>
  )
  return (
    <nav className="nav" aria-label="Основная навигация">
      <Link to="/" className="nav-logo"><img src="/favicon.svg" alt="" /><span>Модус.Отклик</span></Link>
      {item('/home', HomeIcon, 'Главная')}
      {isHandler && item('/inbox', Inbox, 'Входящие', inbox)}
      {isHandler && item('/all', LayoutList, 'Все обращения', 0, 'hide-phone')}
      {isHandler && item('/meetings', Mic, 'Совещания')}
      {isHandler && item('/analytics', BarChart3, 'Аналитика')}
      {item('/done', CheckCircle2, 'Что сделано', 0, isHandler ? 'hide-phone' : '')}
      {user.role === 'admin' && item('/admin', Settings, 'Настройки', 0, 'hide-phone')}
      <div className="nav-bottom">
        <Notifications />
        <button className="nav-item hide-phone" onClick={logout} title="Выйти"><LogOut size={18} /><span className="lbl">Выйти</span></button>
        <NavLink to="/profile" className="nav-me" title={user.name}>
          <Avatar name={user.name} role={user.role} />
          <span className="who"><b>{user.name}</b><span>{user.position}</span></span>
        </NavLink>
      </div>
    </nav>
  )
}

function Notifications() {
  const [data, setData] = useState({ unread: 0, items: [] })
  const [open, setOpen] = useState(false)
  const nav = useNavigate()
  const ref = useRef(null)
  const load = useCallback(() => api.get('/notifications').then(setData).catch(() => {}), [])
  useEffect(() => {
    load()
    const t = setInterval(load, 20000)
    return () => clearInterval(t)
  }, [load])
  useEffect(() => {
    if (!open) return undefined
    const h = (e) => ref.current && !ref.current.contains(e.target) && setOpen(false)
    const esc = (e) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', h)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('mousedown', h); document.removeEventListener('keydown', esc) }
  }, [open])
  const toggle = () => {
    if (!open && data.unread) api.post('/notifications/read').then(() => setTimeout(load, 4000)).catch(() => {})
    setOpen((o) => !o)
  }
  return (
    <div ref={ref} style={{ display: 'contents' }}>
      <button className={`nav-item ${open ? 'active' : ''}`} onClick={toggle} title="Уведомления" aria-expanded={open}>
        <Bell size={18} /><span className="lbl">Уведомления</span>
        {data.unread > 0 && <span className="nav-count accent">{data.unread}</span>}
      </button>
      {open && (
        <div className="notif" role="dialog" aria-label="Уведомления">
          <div className="card-head"><h2>Уведомления</h2></div>
          <div className="scroll">
            {data.items.length === 0 && <Empty icon={Bell} title="Пока тихо" />}
            {data.items.map((n) => (
              <a key={n.id} className={`n-item ${n.read ? '' : 'unread'}`}
                onClick={() => { setOpen(false); if (n.appeal_id) nav(`/appeals/${n.appeal_id}`) }}>
                <span className="d" />
                <span className="grow"><span>{n.text}</span><br /><span className="small muted">{ago(n.created_at)}</span></span>
              </a>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
