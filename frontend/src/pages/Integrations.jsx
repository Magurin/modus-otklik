import { useRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, Database, Download, FileSpreadsheet, Network, Upload } from 'lucide-react'
import { api } from '../api.js'
import { ErrorNote, Spinner, Status, ago, useApp, useLoad } from '../ui.jsx'

const SYSTEMS = [
  { key: '1c', icon: Database, title: '1С:ЗУП', env: 'ONEC_ODATA_URL', gives: 'Сотрудники, подразделения, руководители, отпуска и командировки' },
  { key: 'b24', icon: Network, title: 'Битрикс24', env: 'BITRIX_WEBHOOK_URL', gives: 'Сотрудники, подразделения и руководители. Отпуска портал не передает' },
]

const STATS = [
  ['created', 'новых'], ['updated', 'изменено'], ['deactivated', 'отключено'], ['absent_on', 'отсутствуют'],
  ['absent_off', 'вернулись'], ['deps_created', 'новых подразделений'], ['deps_updated', 'изменений в подразделениях'], ['skipped', 'пропущено'],
]

function statLine(stats) {
  const parts = STATS.filter(([k]) => stats?.[k]).map(([k, l]) => `${stats[k]} ${l}`)
  return parts.length ? parts.join(' · ') : 'без изменений'
}

export default function Integrations() {
  const { reloadMeta } = useApp()
  const [state, reload, error] = useLoad(() => api.get('/admin/org'), [])
  const [busy, setBusy] = useState('')
  const [report, setReport] = useState(null)
  const [err, setErr] = useState('')
  const [file, setFile] = useState(null)
  const fileRef = useRef()

  const run = (source, apply) => {
    setBusy(`${source}:${apply}`)
    setErr('')
    let req
    if (source === 'file') {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('apply', apply ? 'true' : 'false')
      req = api.form('/admin/org/import', fd)
    } else req = api.post(`/admin/org/sync/${source}${apply ? '?apply=true' : ''}`)
    req.then((r) => {
      setReport(r)
      if (apply) { reload(); reloadMeta() }
    }).catch((e) => { setErr(e.message); setReport(null); if (apply) reload() }).finally(() => setBusy(''))
  }

  const template = () => {
    const url = URL.createObjectURL(new Blob(['﻿' + state.template], { type: 'text/csv;charset=utf-8' }))
    const a = document.createElement('a')
    a.href = url
    a.download = 'сотрудники-шаблон.csv'
    a.click()
    URL.revokeObjectURL(url)
  }

  if (!state) return error ? <ErrorNote text={error} /> : <Spinner />
  const Buttons = ({ source, disabled }) => (
    <div className="row" style={{ gap: 8 }}>
      <button className="btn sm" disabled={disabled || !!busy} onClick={() => run(source, false)}>
        {busy === `${source}:false` ? 'Проверяем…' : 'Проверить'}
      </button>
      <button className="btn sm ghost" disabled={disabled || !!busy} onClick={() => run(source, true)}>
        {busy === `${source}:true` ? 'Применяем…' : 'Применить'}
      </button>
    </div>
  )

  return (
    <div className="stack">
      <p className="muted small" style={{ margin: 0, maxWidth: 720 }}>
        Сотрудники, подразделения и отсутствия подтягиваются из учетных систем: новые сотрудники появляются сами,
        у ушедших в отпуск обращения уходят заместителю, уволенные теряют доступ. «Проверить» показывает изменения, ничего не меняя.
      </p>
      <div className="cols c3">
        {SYSTEMS.map(({ key, icon: Icon, title, env, gives }) => {
          const s = state[key]
          return (
            <div key={key} className="card pad stack">
              <div className="row top" style={{ gap: 12 }}>
                <span className="type-ico"><Icon size={18} /></span>
                <div className="grow"><b style={{ fontWeight: 600 }}>{title}</b>
                  <div className="small muted">{s.configured ? s.url : `Эмулятор. Для своей базы задайте ${env}`}</div></div>
                <Status dot={s.configured ? 'ok' : ''}>{s.configured ? 'подключено' : 'эмулятор'}</Status>
              </div>
              <div className="small">{gives}</div>
              <div className="small muted">Связано сотрудников: {state.linked[key] || 0}
                {state.auto.source === key && state.auto.hours > 0 && ` · автоматически раз в ${state.auto.hours} ч`}</div>
              <Buttons source={key} />
            </div>
          )
        })}
        <div className="card pad stack">
          <div className="row top" style={{ gap: 12 }}>
            <span className="type-ico"><FileSpreadsheet size={18} /></span>
            <div className="grow"><b style={{ fontWeight: 600 }}>Файл выгрузки</b>
              <div className="small muted">CSV или XLSX из 1С, Excel или кадровой программы</div></div>
          </div>
          <div className="small">ФИО и почта обязательны. Можно передать только отпуска: отсутствующих в файле не трогаем</div>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            <input ref={fileRef} type="file" accept=".csv,.xlsx,.txt" hidden onChange={(e) => { setFile(e.target.files[0] || null); setReport(null) }} />
            <button className="btn sm ghost" onClick={() => fileRef.current.click()}><Upload size={14} /> {file ? file.name : 'Выбрать файл'}</button>
            <button className="btn sm ghost" onClick={template}><Download size={14} /> Шаблон</button>
          </div>
          <Buttons source="file" disabled={!file} />
        </div>
      </div>

      <ErrorNote text={err} />
      {report && <Report r={report} busy={busy} onApply={() => run(report.source, true)} />}

      {state.runs.length > 0 && (
        <div className="card table-wrap">
          <table className="tbl">
            <thead><tr><th>Когда</th><th>Источник</th><th>Кто запустил</th><th>Результат</th></tr></thead>
            <tbody>{state.runs.map((r) => (
              <tr key={r.id}>
                <td className="small">{ago(r.created_at)}</td>
                <td>{r.label}</td>
                <td className="small">{r.actor}</td>
                <td className="small">{r.ok ? statLine(r.stats) : <span style={{ color: 'var(--danger-solid)' }}>{r.error}</span>}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function Report({ r, busy, onApply }) {
  const empty = !r.changes.length
  return (
    <div className="card pad stack">
      <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h2 style={{ margin: 0 }}>{r.label}{r.emulator && <span className="muted small"> · эмулятор</span>}</h2>
          <div className="small muted">
            {r.dry_run ? 'Предпросмотр, в базе ничего не изменено' : 'Применено'} · в источнике {r.people} сотрудников, {r.departments} подразделений
          </div>
        </div>
        {r.dry_run && !empty && <button className="btn primary" disabled={!!busy} onClick={onApply}>Применить эти изменения</button>}
      </div>
      <div className="small"><b>{statLine(r.stats)}</b></div>
      {empty ? (
        <div className="note green"><CheckCircle2 size={18} /><span>Данные совпадают с источником</span></div>
      ) : (
        <ul className="small" style={{ margin: 0, paddingLeft: 18, display: 'grid', gap: 4 }}>
          {r.changes.map((c, i) => <li key={i}>{c}</li>)}
        </ul>
      )}
      {r.warnings.length > 0 && (
        <div className="note amber"><AlertTriangle size={18} />
          <div className="small" style={{ display: 'grid', gap: 2 }}>{r.warnings.map((w, i) => <span key={i}>{w}</span>)}</div>
        </div>
      )}
    </div>
  )
}
