import { useState } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle, Clock, Loader2, RefreshCw } from 'lucide-react'
import { api } from '../api.js'
import { ErrorNote, Markdown, Spinner, ago, dayDiff, useApp, useLoad } from '../ui.jsx'

/* Страница читается сверху вниз как четыре вопроса:
 * 1. Что сейчас происходит?  2. Что требует действий?  3. О чем и откуда пишут?  4. Кто отвечает?
 * В каждом блоке один главный факт, остальное - подробности к нему. */

// стадии: те же цвета, что у точки статуса и шкалы этапов в обращении (tokens.css, --stage-*)
const STAGE = {
  new: 'var(--stage-1)',
  review: 'var(--stage-2)',
  in_progress: 'var(--stage-3)',
  clarify: 'var(--stage-wait)',
  done: 'var(--stage-4)',
  rejected: 'var(--danger-solid)',
  closed: 'var(--n-300)',
}
const SHADES = ['var(--a-500)', 'color-mix(in srgb, var(--a-500) 55%, white)', 'var(--a-200)', 'var(--n-300)', 'var(--n-200)']
// календарные дни - та же мера, что у срока в списках и карточке (ui.jsx, dayDiff)
const days = (iso) => Math.max(1, Math.abs(dayDiff(iso)))

function Section({ title, hint, children }) {
  return (
    <section className="sec">
      <div className="sec-head"><h2>{title}</h2>{hint && <p>{hint}</p>}</div>
      {children}
    </section>
  )
}

/** Одна полоса на все обращения: сколько на какой стадии. */
function Pipeline({ rows, labels, total }) {
  const shown = rows.filter((r) => r.count > 0)
  return (
    <div className="stack tight">
      <div className="pipe" role="img" aria-label="Обращения по стадиям">
        {shown.map((r) => <i key={r.key} style={{ flexGrow: r.count, background: STAGE[r.key] }} title={`${labels[r.key]}: ${r.count}`} />)}
      </div>
      <div className="pipe-legend">
        {shown.map((r) => (
          <span key={r.key}><i style={{ background: STAGE[r.key] }} />{labels[r.key]} <b>{r.count}</b><em>{Math.round((r.count / total) * 100)}%</em></span>
        ))}
      </div>
    </div>
  )
}

function Kpi({ v, l, hint }) {
  return <div className="kpi"><div className="v">{v}</div><div className="l">{l}</div>{hint && <div className="small muted">{hint}</div>}</div>
}

/** Строка списка «требуют действий»: причина названа словами и стоит на одном месте у всех строк. */
function Need({ a, why }) {
  return (
    <Link className="need" to={`/appeals/${a.id}`}>
      <div className="grow">
        <b>{a.title}</b>
        <div className="small muted">{a.number} · {a.assignee ? a.assignee.name : 'без ответственного'}{a.priority === 'high' ? ' · высокий приоритет' : ''}</div>
      </div>
      {why === 'overdue'
        ? <span className="status alert"><AlertTriangle size={14} /> просрочено на {days(a.due_at)} дн.</span>
        : <span className="status warn"><Clock size={14} /> без ответа {days(a.created_at)} дн.</span>}
    </Link>
  )
}

/** Горизонтальные полосы: одна мера, один цвет. Просроченная часть - красным. */
function Bars({ rows, label = (r) => r.key }) {
  const max = Math.max(1, ...rows.map((r) => r.total ?? r.count))
  if (!rows.length) return <span className="muted">Нет данных</span>
  return (
    <div>
      {rows.map((r) => {
        const total = r.total ?? r.count
        const over = r.overdue || 0
        return (
          <div className="hbar" key={r.key} title={`${label(r)}: ${total}${over ? `, из них просрочено ${over}` : ''}`}>
            <span>{label(r)}</span>
            <div className="track">
              {total - over > 0 && <div className="fill" style={{ width: `${((total - over) / max) * 100}%`, borderRadius: over ? 0 : undefined }} />}
              {over > 0 && <div className="fill over" style={{ width: `${(over / max) * 100}%` }} />}
            </div>
            <span className="n">{total}</span>
          </div>
        )
      })}
    </div>
  )
}

/** Доли целого: одна составная полоса и подписи с процентами. Для разбивок из 2-4 частей. */
function Share({ title, rows, label = (r) => r.key }) {
  const sum = rows.reduce((s, r) => s + r.count, 0) || 1
  return (
    <div className="share">
      <h3>{title}</h3>
      <div className="bar">{rows.map((r, i) => <i key={r.key} style={{ flexGrow: r.count, background: SHADES[i] || SHADES[4] }} />)}</div>
      {rows.map((r, i) => (
        <div className="li" key={r.key}><i style={{ background: SHADES[i] || SHADES[4] }} /><span className="grow">{label(r)}</span><b>{r.count}</b><em>{Math.round((r.count / sum) * 100)}%</em></div>
      ))}
    </div>
  )
}

/** Поступило и закрыто по неделям: видно, копится ли очередь. */
function Weeks({ weeks }) {
  const max = Math.max(1, ...weeks.map((w) => Math.max(w.created, w.closed)))
  return (
    <div>
      <div className="weeks" role="img" aria-label="Поступило и закрыто по неделям">
        {weeks.map((w) => (
          <div className="w" key={w.week}>
            <div className="tip">Неделя с {w.week}: поступило {w.created}, закрыто {w.closed}</div>
            <div className="pair">
              <div className="b" style={{ height: `${(w.created / max) * 100}%` }}>{w.created > 0 && <span className="v">{w.created}</span>}</div>
              <div className="b c" style={{ height: `${(w.closed / max) * 100}%` }} />
            </div>
          </div>
        ))}
      </div>
      <div className="weeks-x">{weeks.map((w) => <span key={w.week}>{w.week}</span>)}</div>
    </div>
  )
}

export default function Analytics() {
  const { meta, user } = useApp()
  const [d, reloadAll, error] = useLoad(() => api.get('/analytics'), [])
  const canDigest = user.role !== 'manager'
  const [digest, , , setDigest] = useLoad(() => (canDigest ? api.get('/analytics/digest') : Promise.resolve(null)), [])
  const [refreshing, setRefreshing] = useState(false)
  const [err, setErr] = useState('')
  const [allCats, setAllCats] = useState(false)
  const [allPeople, setAllPeople] = useState(false)
  if (error) return <div className="page"><ErrorNote text={error} /></div>
  if (!d) return <Spinner />
  const refresh = () => {
    setRefreshing(true)
    api.get('/analytics/digest?refresh=true').then((x) => { setDigest(x); reloadAll(); setErr('') })
      .catch((e) => setErr(e.message)).finally(() => setRefreshing(false))
  }
  const label = (map) => (r) => map[r.key] || r.key
  const waiting = d.no_response.filter((a) => !d.overdue.some((o) => o.id === a.id))
  const needs = d.overdue.length + waiting.length

  // стадии в порядке движения обращения; «ожидает уточнения» - после «в работе», чтобы синие шли подряд
  const order = ['new', 'review', 'in_progress', 'clarify', 'done', 'rejected', 'closed']
  const stages = order.map((k) => d.by_status.find((r) => r.key === k)).filter(Boolean)

  const cats = [...d.by_category].sort((a, b) => (b.overdue || 0) - (a.overdue || 0) || b.total - a.total)
  const catsShown = allCats ? cats : cats.slice(0, 6)
  const people = [...d.by_handler].sort((a, b) => b.overdue - a.overdue || b.open - a.open)
  const peopleShown = allPeople ? people : people.slice(0, 6)
  const maxOpen = Math.max(1, ...people.map((h) => h.open))
  const lastWeeks = d.weeks.slice(-4)
  const inflow = lastWeeks.reduce((s, w) => s + w.created, 0)
  const outflow = lastWeeks.reduce((s, w) => s + w.closed, 0)

  return (
    <div className="page wide">
      <div className="head"><h1>Аналитика</h1></div>
      <div className="an">

        {/* 1. Что сейчас происходит */}
        <div className="card hero">
          <div className="hero-main">
            <div>
              <div className="hero-say"><b>{d.open}</b><span>из {d.total} обращений сейчас открыто</span></div>
              <div className="flags">
                {d.overdue.length > 0 && <a href="#needs" className="flag bad"><AlertTriangle size={14} />{d.overdue.length} просрочено</a>}
                {waiting.length > 0 && <a href="#needs" className="flag warn"><Clock size={14} />{waiting.length} без ответа 2+ дня</a>}
                {needs === 0 && <span className="flag">Просроченных и забытых нет</span>}
              </div>
            </div>
            <Pipeline rows={stages} labels={meta.statuses} total={d.total} />
          </div>
          <div className="hero-side">
            <Kpi v={d.avg_first_response_h === null ? '-' : `${d.avg_first_response_h} ч`} l="до первого ответа" hint="в среднем" />
            <Kpi v={d.avg_resolution_d == null ? '-' : `${d.avg_resolution_d} дн.`} l="от подачи до решения" hint="в среднем" />
            <Kpi v={`${d.closed_share}%`} l="рассмотрено" hint={`${d.closed} из ${d.total}, реализовано ${d.implemented}`} />
          </div>
        </div>

        {/* 2. Что требует действий */}
        <Section title="Что требует действий" hint={needs ? 'Обращения, где нарушен срок или автор давно ждет первого ответа' : null}>
          <div className={`cols ${canDigest && needs ? 'c32' : ''}`} id="needs">
            {needs > 0 && (
              <div className="card rows">
                <div className="card-head"><h2>Просрочено и без ответа</h2><span className="count">{needs}</span></div>
                {d.overdue.map((a) => <Need key={a.id} a={a} why="overdue" />)}
                {waiting.map((a) => <Need key={a.id} a={a} why="waiting" />)}
              </div>
            )}
            {!needs && !canDigest && <div className="card pad muted">Все обращения идут в срок.</div>}
            {canDigest && (
              <div className="card">
                <div className="card-head">
                  <h2>Сводка ИИ</h2>
                  <div className="row" style={{ gap: 8 }}>
                    {digest && <span className="small muted">{ago(digest.created_at)}</span>}
                    <button className="btn ghost icon sm" onClick={refresh} disabled={refreshing} aria-label="Обновить сводку" title="Обновить">
                      {refreshing ? <Loader2 size={16} className="spin" /> : <RefreshCw size={16} />}
                    </button>
                  </div>
                </div>
                <div className="card-body">{digest ? <Markdown text={digest.text} /> : <Spinner />}<ErrorNote text={err} /></div>
              </div>
            )}
          </div>
        </Section>

        {/* 3. О чем пишут */}
        <Section title="О чем пишут" hint="Темы, которые повторяются в разных обращениях, и категории, куда они попадают">
          <div className="cols c2">
            <div className="card">
              <div className="card-head"><h2>Повторяющиеся темы</h2></div>
              <div className="card-body stack">
                {d.themes.length === 0 && <span className="muted">Повторов пока нет</span>}
                {d.themes.slice(0, 6).map((t, i) => (
                  <div key={i} className="theme">
                    <span className="cnt" title="Сколько раз об этом сказали">{t.count}</span>
                    <div className="grow">
                      <b>{t.title}</b>
                      {t.note && <div className="small muted">{t.note}</div>}
                      {t.departments?.length > 0 && <div className="small ink2" style={{ marginTop: 4 }}>{t.departments.join(', ')}</div>}
                      <div className="row wrap small" style={{ gap: 8, marginTop: 4 }}>
                        {t.appeals.map((a) => <Link key={a.id} to={`/appeals/${a.id}`} title={a.title}>{a.number}</Link>)}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
            <div className="card">
              <div className="card-head"><h2>Категории</h2>
                <div className="legend"><span><i style={{ background: 'var(--series-1)' }} />в срок</span><span><i style={{ background: 'var(--series-alert)' }} />просрочено</span></div></div>
              <div className="card-body">
                <Bars rows={catsShown} />
                {cats.length > 6 && (
                  <button className="btn ghost sm more" onClick={() => setAllCats(!allCats)}>
                    {allCats ? 'Свернуть' : `Еще ${cats.length - 6} - по ${cats[6].total} и меньше`}
                  </button>
                )}
              </div>
            </div>
          </div>
        </Section>

        {/* 4. Поток */}
        <Section title="Откуда и сколько приходит" hint={`За последние 4 недели поступило ${inflow}, закрыто ${outflow}`}>
          <div className="cols c2">
            <div className="card">
              <div className="card-head"><h2>По неделям</h2>
                <div className="legend"><span><i style={{ background: 'var(--series-1)' }} />поступило</span><span><i style={{ background: 'var(--n-300)' }} />закрыто</span></div></div>
              <div className="card-body"><Weeks weeks={d.weeks} /></div>
            </div>
            <div className="card">
              <div className="card-head"><h2>Состав</h2></div>
              <div className="card-body shares">
                <Share title="Тип" rows={d.by_type} label={label(meta.types)} />
                <Share title="Канал" rows={d.by_source} label={label(meta.sources)} />
              </div>
            </div>
          </div>
          <div className="card">
            <div className="card-head"><h2>Отделы авторов</h2></div>
            <div className="card-body deps"><Bars rows={d.by_department} /></div>
          </div>
        </Section>

        {/* 5. Кто отвечает */}
        <Section title="Кто отвечает" hint="Сверху - те, у кого есть просрочка и больше всего открытых обращений">
          <div className="card">
            <div className="table-wrap">
              <table className="tbl num">
                <thead><tr><th>Сотрудник</th><th>Открыто</th><th>Просрочено</th><th>Закрыто</th><th>Первый ответ</th></tr></thead>
                <tbody>{peopleShown.map((h) => (
                  <tr key={h.key}><td><b>{h.key}</b></td>
                    <td><div className="load"><span>{h.open}</span><i style={{ width: `${(h.open / maxOpen) * 100}%` }} /></div></td>
                    <td>{h.overdue ? <b style={{ color: 'var(--danger-text)' }}>{h.overdue}</b> : <span className="muted">-</span>}</td>
                    <td>{h.closed || <span className="muted">-</span>}</td>
                    <td>{h.avg_response_h === null ? <span className="muted">-</span> : `${h.avg_response_h} ч`}</td></tr>
                ))}</tbody>
              </table>
            </div>
            {people.length > 6 && (
              <div className="card-foot">
                <button className="btn ghost sm" onClick={() => setAllPeople(!allPeople)}>{allPeople ? 'Свернуть' : `Показать всех (${people.length})`}</button>
              </div>
            )}
          </div>
        </Section>
      </div>
    </div>
  )
}
