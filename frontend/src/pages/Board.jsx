import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { CalendarDays, ChevronLeft, ChevronRight, Flag, X } from 'lucide-react'
import { api } from '../api.js'
import Select from '../Select.jsx'
import { Avatar, Empty, ErrorNote, Overdue, STATUS_DOT, Spinner, StatusPill, dayDiff, useApp, useLoad } from '../ui.jsx'
import './board.css'

/* Доска сроков: открытые обращения разложены по дням срока.
 * Отвечает на три вопроса руководителя: сколько задач всего, что горит, у кого какие сроки.
 * Данные - тот же список обращений, что во «Всех обращениях»: видно ровно то, что разрешено видеть. */

const WD = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб']
const WD_FULL = ['Воскресенье', 'Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
const SPAN = 7 // сколько дней на доске
const fromToday = (n) => { const d = new Date(); return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n) }
const short = (d) => `${d.getDate()} ${MONTHS[d.getMonth()]}`
const NOBODY = 'none'

/** Колонки доски: «Просрочено», дни окна, «Без срока». Пустые выходные не показываем. */
function columns(tasks, offset, later = false) {
  const cols = []
  if (offset === 0) {
    const late = tasks.filter((t) => t.overdue).sort((a, b) => a.n - b.n)
    if (late.length) cols.push({ id: 'overdue', kind: 'overdue', title: 'Просрочено', items: late })
  }
  for (let i = 0; i < SPAN; i += 1) {
    const n = offset * SPAN + i
    const date = fromToday(n)
    // на текущей неделе просроченное собрано в своей колонке; в прошлых неделях оно стоит в день срока
    const items = tasks.filter((t) => t.n === n && (offset !== 0 || !t.overdue))
    const weekend = date.getDay() === 0 || date.getDay() === 6
    if (weekend && !items.length) continue
    cols.push({
      id: `d${n}`, kind: 'day', date, weekend, today: n === 0, items,
      title: n === 0 ? 'Сегодня' : n === 1 ? 'Завтра' : n === -1 ? 'Вчера' : WD_FULL[date.getDay()],
      sub: n >= -1 && n <= 1 ? `${WD[date.getDay()]}, ${short(date)}` : short(date),
    })
  }
  if (later) {  // в таблице по людям: все, что за пределами окна, чтобы сумма по строке сходилась с «Открыто»
    const rest = tasks.filter((t) => t.n !== null && t.n >= (offset + 1) * SPAN).sort((a, b) => a.n - b.n)
    if (rest.length) cols.push({ id: 'later', kind: 'later', title: 'Позже', items: rest })
  }
  if (offset === 0) {
    const none = tasks.filter((t) => t.n === null)
    if (none.length) cols.push({ id: 'none', kind: 'none', title: 'Без срока', items: none })
  }
  return cols
}

function Stat({ v, l, tone = '', onClick }) {
  const Tag = onClick ? 'button' : 'div'
  return <Tag className={`bd-stat ${v ? tone : 'zero'}`} onClick={onClick}><b>{v}</b><span>{l}</span></Tag>
}

function Card({ t, showWho }) {
  const { a } = t
  return (
    <Link className="bd-card" to={`/appeals/${a.id}`}>
      <span className="t">{a.title}</span>
      <span className="row wrap" style={{ gap: 8 }}>
        <StatusPill status={a.status} />
        {a.priority === 'high' && <span className="chip bd-high" title="Высокий приоритет"><Flag size={13} /> высокий</span>}
      </span>
      {a.overdue && <span><Overdue a={a} /></span>}
      <span className="who">
        {showWho && (a.assignee
          ? <><Avatar name={a.assignee.name} role={a.assignee.role} /><span className="grow">{a.assignee.name}</span></>
          : <span className="grow">без ответственного</span>)}
        <span className="muted">{a.number}</span>
      </span>
    </Link>
  )
}

function ColHead({ c, slim }) {
  if (slim) return <div className="bd-col-head"><b>{WD[c.date.getDay()]}</b><span>{c.date.getDate()}</span></div>
  return (
    <div className="bd-col-head">
      <b>{c.title}</b>{c.sub && <span>{c.sub}</span>}
      <span className="n">{c.items.length}</span>
    </div>
  )
}

function Days({ cols, showWho }) {
  const tpl = cols.map((c) => (c.items.length ? 'minmax(210px, 340px)' : '64px')).join(' ')
  return (
    <div className="bd-board" style={{ '--bd-cols': tpl }}>
      {cols.map((c) => {
        const slim = !c.items.length
        return (
          <section key={c.id} className={`bd-col ${c.kind} ${c.today ? 'today' : ''} ${c.weekend ? 'weekend' : ''} ${slim ? 'slim' : ''}`}
            aria-label={`${c.title}${c.sub ? `, ${c.sub}` : ''}: ${c.items.length}`}>
            <ColHead c={c} slim={slim} />
            {c.items.map((t) => <Card key={t.a.id} t={t} showWho={showWho} />)}
          </section>
        )
      })}
    </div>
  )
}

/** Те же колонки, но строки - люди: у кого сколько задач и на какие дни. */
function People({ cols, people, onPick }) {
  const cell = (items, kind) => items.map((t) => (
    <Link key={t.a.id} className={`bd-chip ${t.overdue ? 'bad' : ''}`} to={`/appeals/${t.a.id}`} title={`${t.a.number} · ${t.a.title}`}>
      <span className={`dot ${STATUS_DOT[t.a.status] || ''}`} />
      <span>{kind === 'later' && <span className="muted">{short(fromToday(t.n))} · </span>}{t.a.title}</span>
    </Link>
  ))
  return (
    <div className="card">
      <div className="table-wrap">
        <table className="tbl bd-table">
          <thead>
            <tr>
              <th>Сотрудник</th><th className="r">Открыто</th>
              {cols.map((c) => (
                <th key={c.id} className={`${c.kind} ${c.today ? 'today' : ''} ${c.items.length ? '' : 'slim'}`}>
                  {c.items.length || c.kind !== 'day' ? <>{c.title}{c.sub && <span> {c.sub}</span>}</> : <>{WD[c.date.getDay()]} {c.date.getDate()}</>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {people.map((p) => (
              <tr key={p.id}>
                <td>
                  {p.user ? (
                    <button className="bd-person" onClick={() => onPick(p.id)} title="Показать только задачи этого сотрудника">
                      <Avatar name={p.user.name} role={p.user.role} />
                      <span className="grow"><b>{p.user.name}</b>
                        <span className="small muted">{p.user.is_absent ? 'отсутствует' : p.user.position}</span></span>
                    </button>
                  ) : <b>Без ответственного</b>}
                </td>
                <td className="r num"><b>{p.items.length}</b></td>
                {cols.map((c) => {
                  const mine = c.items.filter((t) => (t.a.assignee?.id ?? NOBODY) === p.id)
                  return <td key={c.id} className={`${c.today ? 'today' : ''} ${c.items.length ? 'has' : ''}`}><div className="bd-cell">{cell(mine, c.kind)}</div></td>
                })}
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td>Всего</td><td className="r num">{people.reduce((s, p) => s + p.items.length, 0)}</td>
              {cols.map((c) => <td key={c.id} className={`num ${c.today ? 'today' : ''}`}>{c.items.length || ''}</td>)}
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  )
}

export default function Board() {
  const { meta, user } = useApp()
  const lead = user.role !== 'employee'  // сотрудник видит только свои обращения: разбивка по людям ему не нужна
  const [rows, reload, error] = useLoad(() => api.get('/appeals?scope=all'), [])
  const [view, setView] = useState('days')
  const [offset, setOffset] = useState(0)
  const [who, setWho] = useState('')
  useEffect(() => { const t = setInterval(reload, 60000); return () => clearInterval(t) }, [reload])

  // задача = открытое обращение; n - через сколько календарных дней срок (null - срока нет)
  const all = useMemo(() => (rows || []).filter((a) => !meta.final.includes(a.status))
    .map((a) => ({ a, overdue: a.overdue, n: a.due_at ? dayDiff(a.due_at) : null }))
    .sort((x, y) => (y.a.priority === 'high') - (x.a.priority === 'high') || new Date(x.a.due_at || 0) - new Date(y.a.due_at || 0)),
  [rows, meta])
  const people = useMemo(() => {
    const by = new Map()
    all.forEach((t) => {
      const id = t.a.assignee?.id ?? NOBODY
      if (!by.has(id)) by.set(id, { id, user: t.a.assignee, items: [] })
      by.get(id).items.push(t)
    })
    const late = (p) => p.items.filter((t) => t.overdue).length
    return [...by.values()].sort((a, b) => (a.id === NOBODY) - (b.id === NOBODY) || late(b) - late(a) || b.items.length - a.items.length)
  }, [all])

  if (error) return <div className="page"><ErrorNote text={error} /></div>
  if (!rows) return <Spinner />

  const picked = view === 'days' && who ? people.find((p) => String(p.id) === String(who)) : null
  const tasks = picked ? picked.items : all
  const cols = columns(tasks, offset, view === 'people')
  const stat = {
    overdue: tasks.filter((t) => t.overdue).length,
    today: tasks.filter((t) => t.n === 0 && !t.overdue).length,
    week: tasks.filter((t) => t.n !== null && t.n >= 0 && t.n < SPAN && !t.overdue).length,
    later: tasks.filter((t) => t.n !== null && t.n >= SPAN).length,
    none: tasks.filter((t) => t.n === null).length,
  }
  const closed = rows.filter((a) => a.closed_at && dayDiff(a.closed_at) > -SPAN
    && (!picked || a.assignee?.id === picked.id)).length
  const from = fromToday(offset * SPAN)
  const to = fromToday(offset * SPAN + SPAN - 1)
  const range = from.getMonth() === to.getMonth() ? `${from.getDate()} - ${short(to)}` : `${short(from)} - ${short(to)}`
  const shown = cols.reduce((s, c) => s + c.items.length, 0)

  return (
    <div className="page board">
      <div className="head">
        <div>
          <h1>Доска сроков</h1>
          <div className="sub">{picked ? `Задачи сотрудника: ${picked.user?.name || 'без ответственного'}` : lead ? 'Открытые обращения по дням срока' : 'Ваши открытые обращения по дням срока'}</div>
        </div>
      </div>

      <div className="bd-stats">
        <Stat v={tasks.length} l={picked ? 'открыто у сотрудника' : lead ? 'задач открыто' : 'обращений открыто'} tone="main" />
        <Stat v={stat.overdue} l="просрочено" tone="bad" onClick={() => setOffset(0)} />
        <Stat v={stat.today} l="срок сегодня" onClick={() => setOffset(0)} />
        <Stat v={stat.week} l="на ближайшие 7 дней" onClick={() => setOffset(0)} />
        <Stat v={stat.later} l="позже" onClick={() => setOffset(1)} />
        {stat.none > 0 && <Stat v={stat.none} l="без срока" />}
        <Stat v={closed} l="закрыто за 7 дней" tone="ok" />
      </div>

      <div className="toolbar bd-bar">
        {lead && (
          <div className="segs">
            <button className={view === 'days' ? 'on' : ''} onClick={() => setView('days')}>По дням</button>
            <button className={view === 'people' ? 'on' : ''} onClick={() => setView('people')}>По людям</button>
          </div>
        )}
        {lead && view === 'days' && (
          <Select size="sm" value={who} onChange={setWho} ariaLabel="Сотрудник"
            options={[{ value: '', label: 'Все сотрудники' },
              ...people.map((p) => ({ value: p.id, label: p.user?.name || 'Без ответственного', hint: `открыто: ${p.items.length}` }))]} />
        )}
        {picked && <button className="btn ghost sm" onClick={() => setWho('')}><X size={14} /> Сбросить</button>}
        <div className="bd-nav">
          {offset !== 0 && <button className="btn sm" onClick={() => setOffset(0)}>Сегодня</button>}
          <button className="btn ghost icon sm" onClick={() => setOffset((o) => o - 1)} aria-label="Предыдущие 7 дней"><ChevronLeft size={18} /></button>
          <span className="bd-range num">{range}</span>
          <button className="btn ghost icon sm" onClick={() => setOffset((o) => o + 1)} aria-label="Следующие 7 дней"><ChevronRight size={18} /></button>
        </div>
      </div>

      {shown === 0 ? (
        <div className="card"><Empty icon={CalendarDays} title={tasks.length ? 'На эти дни сроков нет' : 'Открытых задач нет'}>
          {tasks.length > 0 && offset !== 0 && <button className="btn sm" onClick={() => setOffset(0)}>Вернуться к сегодня</button>}
        </Empty></div>
      ) : view === 'days'
        ? <Days cols={cols} showWho={!picked} />
        : <People cols={cols} people={people} onPick={(id) => { setWho(id); setView('days') }} />}
    </div>
  )
}
