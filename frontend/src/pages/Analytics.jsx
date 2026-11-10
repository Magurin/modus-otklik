import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Loader2, RefreshCw } from 'lucide-react'
import { api } from '../api.js'
import { AppealItem, ErrorNote, Markdown, Spinner, ago, useApp, useLoad } from '../ui.jsx'

/** Плитка-показатель: подпись сверху, число снизу. Красным число становится, только если оно требует действий. */
function Tile({ v, l, alert }) {
  return <div className={`tile ${alert ? 'alert' : ''}`}><div className="l">{l}</div><div className="v">{v}</div></div>
}

/** Горизонтальные полосы: одна мера, один цвет. Просроченная часть - красным, с подписью в легенде. */
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

/** Сколько обращений поступало по неделям: одна серия, значение подписано над столбцом, «закрыто» - в подсказке. */
function Weeks({ weeks }) {
  const max = Math.max(1, ...weeks.map((w) => w.created))
  return (
    <div>
      <div className="weeks" role="img" aria-label="Поступило обращений по неделям">
        {weeks.map((w) => (
          <div className="w" key={w.week}>
            <div className="tip">Неделя с {w.week}: поступило {w.created}, закрыто {w.closed}</div>
            {w.created > 0 && <span className="v">{w.created}</span>}
            <div className="b" style={{ height: `${(w.created / max) * 78}%` }} />
          </div>
        ))}
      </div>
      <div className="weeks-x">{weeks.map((w) => <span key={w.week}>{w.week}</span>)}</div>
    </div>
  )
}

export default function Analytics() {
  const { meta, user } = useApp()
  const nav = useNavigate()
  const [d, reloadAll, error] = useLoad(() => api.get('/analytics'), [])
  const canDigest = user.role !== 'manager'
  const [digest, , , setDigest] = useLoad(() => (canDigest ? api.get('/analytics/digest') : Promise.resolve(null)), [])
  const [refreshing, setRefreshing] = useState(false)
  const [err, setErr] = useState('')
  if (error) return <div className="page"><ErrorNote text={error} /></div>
  if (!d) return <Spinner />
  const refresh = () => {
    setRefreshing(true)
    api.get('/analytics/digest?refresh=true').then((x) => { setDigest(x); reloadAll(); setErr('') })
      .catch((e) => setErr(e.message)).finally(() => setRefreshing(false))
  }
  const label = (map) => (r) => map[r.key] || r.key
  const attention = [...d.overdue, ...d.no_response.filter((a) => !d.overdue.some((o) => o.id === a.id))]
  return (
    <div className="page wide">
      <div className="head"><h1>Аналитика</h1></div>
      <div className="stack">
        <div className="tiles">
          <Tile v={d.open} l={`Открыто из ${d.total}`} />
          <Tile v={d.overdue.length} l="Просрочено" alert={d.overdue.length > 0} />
          <Tile v={d.no_response.length} l="Без ответа 2+ дня" alert={d.no_response.length > 0} />
          <Tile v={d.avg_first_response_h === null ? '-' : `${d.avg_first_response_h} ч`} l="До первого ответа" />
          <Tile v={`${d.closed_share}%`} l="Рассмотрено" />
          <Tile v={d.implemented} l="Реализовано" />
        </div>

        {attention.length > 0 && (
          <div className="card rows">
            <div className="card-head"><h2>Требуют внимания</h2></div>
            {attention.map((a) => <AppealItem key={a.id} a={a} who="assignee" onClick={() => nav(`/appeals/${a.id}`)} />)}
          </div>
        )}

        <div className={`cols ${canDigest ? 'c2' : ''}`}>
          {canDigest && (
            <div className="card">
              <div className="card-head">
                <h2>Сводка</h2>
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
                    <div className="row wrap small" style={{ gap: 8, marginTop: 4 }}>
                      {t.appeals.map((a) => <Link key={a.id} to={`/appeals/${a.id}`} title={a.title}>{a.number}</Link>)}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="cols c2">
          <div className="card">
            <div className="card-head"><h2>Поступило по неделям</h2></div>
            <div className="card-body"><Weeks weeks={d.weeks} /></div>
          </div>
          <div className="card">
            <div className="card-head"><h2>По категориям</h2>
              <div className="legend"><span><i style={{ background: 'var(--series-1)' }} />в срок</span><span><i style={{ background: 'var(--series-alert)' }} />просрочено</span></div></div>
            <div className="card-body"><Bars rows={d.by_category} /></div>
          </div>
        </div>

        <div className="cols c3">
          <div className="card"><div className="card-head"><h2>Типы</h2></div><div className="card-body"><Bars rows={d.by_type} label={label(meta.types)} /></div></div>
          <div className="card"><div className="card-head"><h2>Каналы</h2></div><div className="card-body"><Bars rows={d.by_source} label={label(meta.sources)} /></div></div>
          <div className="card"><div className="card-head"><h2>Отделы авторов</h2></div><div className="card-body"><Bars rows={d.by_department} /></div></div>
        </div>

        <div className="card">
          <div className="card-head"><h2>Ответственные</h2></div>
          <div className="table-wrap">
            <table className="tbl num">
              <thead><tr><th>Сотрудник</th><th>Открыто</th><th>Просрочено</th><th>Закрыто</th><th>Первый ответ</th></tr></thead>
              <tbody>{d.by_handler.map((h) => (
                <tr key={h.key}><td><b>{h.key}</b></td><td>{h.open}</td>
                  <td>{h.overdue ? <b style={{ color: 'var(--danger-text)' }}>{h.overdue}</b> : <span className="muted">0</span>}</td><td>{h.closed}</td>
                  <td>{h.avg_response_h === null ? <span className="muted">-</span> : `${h.avg_response_h} ч`}</td></tr>
              ))}</tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  )
}
