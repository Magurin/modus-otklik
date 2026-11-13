import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { Bot, CalendarDays, Check, Copy, Download } from 'lucide-react'
import { api } from '../api.js'
import { tg } from '../tg.js'
import { ErrorNote, Spinner, Status, useLoad } from '../ui.jsx'

const CAL_NOTE = {
  ok: ['green', 'Календарь подключен', 'Открытые обращения уже раскладываются по календарю - каждое полосой на весь срок.'],
  denied: ['amber', 'Календарь не подключен', 'Доступ не был выдан. Можно попробовать еще раз.'],
  error: ['red', 'Не получилось подключить', 'Google не подтвердил доступ. Попробуйте еще раз.'],
}

/** Google Календарь в профиле: подключение, состояние, отключение. */
export function CalendarCard() {
  const [c, reload, error, setC] = useLoad(() => api.get('/me/calendar'), [])
  const [err, setErr] = useState('')
  const [waiting, setWaiting] = useState(false)
  const loc = useLocation()
  const nav = useNavigate()
  const back = new URLSearchParams(loc.search).get('calendar')  // вернулись со страницы согласия Google
  const [note] = useState(CAL_NOTE[back])
  useEffect(() => { if (back) nav('/profile', { replace: true }) }, [back, nav])

  const connect = () => api.post('/me/calendar/connect').then(({ url }) => {
    // Google не пускает на страницу согласия из встроенного окна Telegram - открываем обычный браузер
    if (tg.app) { tg.app.openLink(url); setWaiting(true) } else window.location.href = url
  }).catch((e) => setErr(e.message))
  const disconnect = () => api.post('/me/calendar/disconnect').then(setC).catch((e) => setErr(e.message))

  // пока Google на сервере не настроен, подключать нечего - карточку не показываем
  if (!c || (!c.configured && !c.connected)) return null

  return (
    <div className="card pad stack tight">
      <div className="row spread wrap">
        <div className="row" style={{ minWidth: 0 }}><span className="type-ico"><CalendarDays size={18} /></span>
          <div style={{ minWidth: 0 }}><b style={{ fontWeight: 600 }}>Google Календарь</b>
            <div className="small muted">{c?.connected ? c.email || 'подключен' : 'Обращение - полосой на весь срок, от регистрации до дедлайна'}</div></div>
        </div>
        {c.connected ? <div className="row wrap"><Status dot="ok">подключен</Status><button className="btn sm ghost" onClick={disconnect}>Отключить</button></div>
          : <button className="btn sm" onClick={connect}>Подключить</button>}
      </div>
      {!c.connected && (
        <div className="small muted">После подключения ваши обращения и те, за которые вы отвечаете, появятся в календаре сами. Перенесут срок - полоса сдвинется.</div>
      )}
      {waiting && !c.connected && (
        <div className="note"><CalendarDays size={16} /><div>Разрешите доступ в открывшемся браузере и вернитесь сюда
          <div><button className="btn sm ghost" style={{ marginTop: 6, marginLeft: -10 }} onClick={reload}>Готово, проверить</button></div>
        </div></div>
      )}
      {note && <div className={`note ${note[0]}`}><CalendarDays size={16} /><div><b>{note[1]}</b>{note[2]}</div></div>}
      <ErrorNote text={err || error} />
    </div>
  )
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
  } catch { // страница открыта не по https или буфер недоступен - старый способ через скрытое поле
    const t = document.createElement('textarea')
    t.value = text
    t.style.position = 'fixed'
    t.style.opacity = '0'
    document.body.appendChild(t)
    t.select()
    const ok = document.execCommand('copy')
    t.remove()
    if (!ok) throw new Error('Не удалось скопировать - выделите текст вручную')
  }
}

/** Доступ для агента: файл навыка (без ключа) и промпт подключения, который сотрудник отправляет своему ИИ-агенту. */
export function AgentCard() {
  const [a, , error, setA] = useLoad(() => api.get('/me/agent'), [])
  const [err, setErr] = useState('')
  const [copied, setCopied] = useState('')
  const flash = (what) => { setCopied(what); setErr(''); setTimeout(() => setCopied(''), 1800) }
  const fail = (e) => setErr(e.message)

  const copySkill = () => copyText(a.skill).then(() => flash('skill')).catch(fail)
  const download = () => {
    const url = URL.createObjectURL(new Blob([a.skill], { type: 'text/markdown;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = 'SKILL.md'
    link.click()
    URL.revokeObjectURL(url)
  }
  // без ключа промпт бесполезен: первый раз выпускаем ключ сами, он попадает в промпт и больше нигде не показывается
  const copyPrompt = async () => {
    try {
      const cur = a.enabled ? a : await api.post('/me/agent/key')
      if (cur !== a) setA(cur)
      await copyText(cur.prompt)
      flash('prompt')
    } catch (e) { fail(e) }
  }
  const reissue = () => api.post('/me/agent/key').then((d) => { setA(d); setErr('') }).catch(fail)
  const revoke = () => api.post('/me/agent/revoke').then((d) => { setA(d); setErr('') }).catch(fail)

  return (
    <div className="card pad stack">
      <div>
        <h2>Доступ для агента</h2>
        <p className="small muted" style={{ margin: '4px 0 0' }}>
          Отправьте этот промпт своему ИИ-агенту - Claude, Cursor, ChatGPT или другому. Он обсудит с вами задачу,
          сверится с похожими обращениями и после вашего согласия отправит ее в общий список от вашего имени.
        </p>
      </div>
      {a && !a.skill ? <ErrorNote text="Сервер запущен со старой версией - перезапустите бэкенд, чтобы появился файл навыка" />
        : !a ? (error ? null : <Spinner />) : (
        <>
          <div className="row wrap">
            <button className="btn" onClick={copySkill}>{copied === 'skill' ? <Check size={16} /> : <Copy size={16} />} {copied === 'skill' ? 'Скопировано' : 'Копировать навык'}</button>
            <button className="btn" onClick={download}><Download size={16} /> Скачать SKILL.md</button>
          </div>
          <details className="skill">
            <summary>Показать файл навыка</summary>
            <pre className="codebox">{a.skill}</pre>
          </details>
          <div className="row wrap" style={{ gap: '8px 12px' }}>
            <button className="btn primary" onClick={copyPrompt}>
              {copied === 'prompt' ? <Check size={16} /> : <Bot size={16} />} {copied === 'prompt' ? 'Промпт скопирован' : 'Копировать промпт для агента'}
            </button>
            {a.enabled && <Status dot="ok">ключ выдан</Status>}
            {a.enabled && <button className="btn sm ghost" onClick={reissue}>Новый ключ</button>}
            {a.enabled && <button className="btn sm ghost" onClick={revoke}>Отозвать</button>}
          </div>
        </>
      )}
      <ErrorNote text={err || error} />
    </div>
  )
}
