import { useMemo, useState } from 'react'
import { Check, Clock, HelpCircle, MoreHorizontal, Paperclip, PenLine, Search, Send, ThumbsUp, Users } from 'lucide-react'
import Select from '../Select.jsx'
import { AppealItem, Avatar, Overdue, Status, StatusPill, Steps, TypeIcon, useApp } from '../ui.jsx'

/** Живой справочник дизайн-системы: все показано настоящими компонентами и читает значения из tokens.css. */

// те же шесть правил, что в шапке tokens.css и в README
const RULES = [
  ['Нейтральная основа', 'Текст и поверхности берутся из одной серой шкалы. Цвет на экране - исключение, а не фон.'],
  ['У цвета три языка', 'Действие - синий акцент: главная кнопка, ссылка, фокус, выбранный пункт. Этап обращения - своя шкала: у статуса один цвет в точке, на шкале этапов, на плитке типа и в аналитике. Роль человека - холодные цвета только на аватаре и значке роли.'],
  ['Цвет не работает один', 'Красный, янтарный и зеленый сообщают состояние: отказ и просрочка, ждем человека, сделано. Рядом всегда стоит слово или значок. Сплошная заливка - только у метки «просрочено».'],
  ['Иерархия шрифтом', 'Важность показывают размер, насыщенность и отступы. Рамки и цвет для этого не используются.'],
  ['Размеры из шкал', 'Размеры шрифта, отступы, скругления и высоты элементов берутся из токенов. Отступления известны (подписи 10-11px, скругление 12px у меню, крупное число в аналитике) и новые не добавляются.'],
  ['Одно состояние - одно название', 'Подписи статусов приходят с сервера; шкала этапов, точки, вкладки и аналитика используют только их. Автору вместо «Ожидает уточнения» показывается «Нужен ваш ответ» - единственное исключение. Кнопки называют действие, а не состояние.'],
]

const NEUTRAL = [
  ['--n-0', 'карточки'], ['--n-25', 'вложенный блок'], ['--n-50', 'фон страницы'], ['--n-100', 'плашки'], ['--n-150', 'разделители'],
  ['--n-200', 'дорожки'], ['--n-300', 'границы полей'], ['--n-400', 'неактивное'], ['--n-500', 'подписи'], ['--n-600', 'вторичный текст'],
  ['--n-700', 'текст статуса'], ['--n-800', 'счетчики'], ['--n-900', 'основной текст'],
]
const ACCENT = [['--a-50', 'наведение'], ['--a-100', 'мягкий фон'], ['--a-200', 'ореол поля в фокусе'], ['--a-500', 'акцент, кольцо фокуса'], ['--a-600', 'наведение на кнопку'], ['--a-700', 'текст на мягком фоне']]
const SEMANTIC = [
  ['--danger-solid', 'точка: отказ; метка: просрочено'], ['--danger-text', 'текст: просрочено'], ['--warning-solid', 'точка и значок: нужно решить'], ['--warning-text', 'текст'],
  ['--success-solid', 'точка: сделано'], ['--success-text', 'текст'],
]
// этапы: токен -> код статуса; подписи берутся из meta.statuses, как и везде
const STAGE = [['--stage-1', 'new'], ['--stage-2', 'review'], ['--stage-3', 'in_progress'], ['--stage-4', 'done'], ['--stage-wait', 'clarify']]
const DEADLINE = [['--overdue-bg', 'метка: просрочено'], ['--waiting-bg', 'метка: без ответа'], ['--waiting-ink', 'текст метки «без ответа»']]
const TYPE = [
  ['Заголовок страницы', 'display', '28 / 34 · 650', 'Что можно улучшить?'],
  ['Заголовок объекта', 'title', '22 / 28 · 650', 'Единое место для итоговых решений'],
  ['Заголовок блока', 'heading', '16 / 24 · 600', 'Мои обращения'],
  ['Основной текст', 'body', '15 / 23 · 400', 'Рабочие обсуждения распределены по нескольким каналам.'],
  ['Подпись', 'small', '13 / 19 · 400', 'A-012 · Коммуникации · 30 окт'],
  ['Надпись', 'caption', '12 / 16 · 500', 'Ответственный'],
]
const SPACE = [['--s-1', 4], ['--s-2', 8], ['--s-3', 12], ['--s-4', 16], ['--s-5', 20], ['--s-6', 24], ['--s-8', 32], ['--s-10', 40], ['--s-12', 48]]
const RADIUS = [['--r-xs', 'метки'], ['--r-sm', 'малые кнопки'], ['--r-md', 'кнопки, поля'], ['--r-lg', 'карточки'], ['--r-xl', 'окна']]

function Swatches({ list }) {
  const css = useMemo(() => getComputedStyle(document.documentElement), [])
  return (
    <div className="ds-swatches">
      {list.map(([token, role]) => (
        <div className="ds-sw" key={token}>
          <i style={{ background: `var(${token})` }} />
          <b>{token.replace('--', '')}</b>
          <span>{css.getPropertyValue(token).trim()}</span>
          <span>{role}</span>
        </div>
      ))}
    </div>
  )
}

function Block({ title, hint, children }) {
  return (
    <section className="card pad ds-section">
      <div><h2>{title}</h2>{hint && <p className="small muted">{hint}</p>}</div>
      {children}
    </section>
  )
}

const now = Date.now()
const iso = (days) => new Date(now + days * 86400000).toISOString()
const sample = (over) => ({
  id: 0, number: 'A-012', type: 'proposal', title: 'Единое место для итоговых решений по задаче', category: 'Коммуникации', supports: 3,
  author: { name: 'Артем Белов' }, assignee: { name: 'Никита Соколов' }, created_at: iso(-11), due_at: iso(-1), closed_at: null, ...over,
})

export default function DesignSystem() {
  const { meta } = useApp()
  const [sel, setSel] = useState('review')
  return (
    <div className="stack">
      <Block title="Принципы">
        <div className="stack tight">
          {RULES.map(([name, text], i) => (
            <div className="ds-rule" key={name}><span className="n">{i + 1}</span><span><b style={{ fontWeight: 600 }}>{name}.</b> <span className="ink2">{text}</span></span></div>
          ))}
        </div>
      </Block>

      <Block title="Цвет" hint="Нейтральная шкала, акцент для действий, цвета состояния, шкала этапов и метки сроков">
        <h3>Нейтральная шкала</h3><Swatches list={NEUTRAL} />
        <h3>Акцент</h3><Swatches list={ACCENT} />
        <h3>Состояния</h3><Swatches list={SEMANTIC} />
        <h3>Этапы обращения</h3><Swatches list={STAGE.map(([token, status]) => [token, meta.statuses[status]])} />
        <h3>Метки сроков</h3><Swatches list={DEADLINE} />
      </Block>

      <Block title="Цвета ролей" hint="Холодная часть спектра, где нет ни одного цвета состояния. Только на аватаре и значке роли">
        <div className="row wrap" style={{ gap: 24 }}>
          {[['employee', 'Сотрудник', 'Асель Тулегенова'], ['manager', 'Руководитель', 'Никита Соколов'], ['director', 'Директор', 'Арман Сеитов'], ['admin', 'Администратор', 'Ольга Ветрова']].map(([r, label, name]) => (
            <div key={r} className="row"><Avatar name={name} role={r} /><div><b style={{ fontWeight: 600 }}>{label}</b>
              <div className="small muted">role-{r}</div></div></div>
          ))}
        </div>
      </Block>

      <Block title="Типографика" hint="Inter. Шесть ступеней, других размеров в интерфейсе нет">
        <div className="ds-type">
          {TYPE.map(([name, key, spec, text]) => (
            <div key={key} style={{ display: 'contents' }}>
              <span className="small muted">{name}<br />{spec}</span>
              <span style={{ fontSize: `var(--fs-${key})`, lineHeight: `var(--lh-${key})`, fontWeight: key === 'display' || key === 'title' ? 650 : key === 'heading' ? 600 : key === 'caption' ? 500 : 400,
                letterSpacing: key === 'display' ? '-.022em' : key === 'title' ? '-.018em' : 0, color: key === 'small' || key === 'caption' ? 'var(--muted)' : 'var(--ink)' }}>{text}</span>
            </div>
          ))}
        </div>
      </Block>

      <Block title="Отступы и скругления" hint="Шаг отступов - 4px">
        <div className="ds-space">{SPACE.map(([t, px]) => <div key={t}><i style={{ width: px }} /><span>{px}</span></div>)}</div>
        <div className="ds-radius">{RADIUS.map(([t, role]) => <div key={t}><i style={{ borderRadius: `var(${t})` }} /><span>{t.replace('--r-', '')} · {role}</span></div>)}</div>
      </Block>

      <Block title="Кнопки" hint="На экране одна главная кнопка. Остальные - обычные или без рамки. Фокус с клавиатуры (Tab) - кольцо акцентного цвета">
        <div className="row wrap">
          <button className="btn primary"><Check size={16} /> Главное действие</button>
          <button className="btn"><HelpCircle size={16} /> Обычная</button>
          <button className="btn ghost">Без рамки</button>
          <button className="btn danger">Отклонить</button>
          <button className="btn icon" aria-label="Еще"><MoreHorizontal size={18} /></button>
          <button className="btn primary" disabled>Недоступна</button>
        </div>
        <div className="row wrap">
          <button className="btn primary lg">Большая</button>
          <button className="btn primary">Средняя</button>
          <button className="btn primary sm">Малая</button>
          <button className="btn sm"><ThumbsUp size={14} /> У меня то же</button>
          <button className="btn ghost sm on"><Paperclip size={14} /> Включено</button>
        </div>
      </Block>

      <Block title="Поля">
        <div className="grid c2">
          <label className="field">Текстовое поле<input type="text" placeholder="Планерка отдела" /></label>
          <label className="field">Выпадающий список
            <Select value={sel} onChange={setSel} ariaLabel="Статус" options={Object.entries(meta.statuses).map(([value, label]) => ({ value, label }))} />
          </label>
        </div>
        <div className="row wrap">
          <div className="search"><Search size={17} /><input type="text" placeholder="Поиск" aria-label="Поиск" /></div>
          <div className="segs"><button className="on">Открытые <span className="n">12</span></button><button>Новые <span className="n">3</span></button><button>Просрочены <span className="n red">2</span></button></div>
        </div>
        <label className="check"><input type="checkbox" defaultChecked /> Флажок</label>
      </Block>

      <Block title="Статусы и метки" hint="У статуса один цвет на всех экранах, рядом с точкой всегда слово. Последний в ряду - тот же статус «Ожидает уточнения» глазами автора">
        <div className="row wrap" style={{ gap: 20 }}>
          {Object.keys(meta.statuses).map((s) => <StatusPill key={s} status={s} />)}
          <StatusPill status="clarify" mine />
        </div>
        <div className="row wrap" style={{ gap: 20 }}>
          <Overdue a={sample({ overdue: true })} />
          <span className="status warn"><Clock size={14} /> без ответа 3 дн.</span>
          <Status dot="warn">Нужно решить: 5</Status>
          <Status dot="ok">подключен</Status>
          <span className="pill">Проблема</span>
          <span className="pill"><Users size={12} /> +3 с тем же</span>
          <span className="chip"><PenLine size={14} /> Асель Тулегенова</span>
        </div>
        <div className="row wrap">
          <TypeIcon type="problem" /><TypeIcon type="proposal" /><TypeIcon type="praise" />
          <TypeIcon type="problem" status="new" /><TypeIcon type="problem" status="review" /><TypeIcon type="problem" status="in_progress" />
          <TypeIcon type="problem" status="clarify" /><TypeIcon type="problem" status="done" /><TypeIcon type="problem" status="rejected" />
          <Avatar name="Артем Белов" /><Avatar name="Никита Соколов" size="sm" />
        </div>
      </Block>

      <Block title="Строка обращения" hint="Списки - одна поверхность с разделителями, а не отдельные карточки">
        <div className="card rows">
          <AppealItem a={sample({ status: 'review', overdue: true })} onClick={() => {}} />
          <AppealItem a={sample({ number: 'A-007', type: 'problem', title: 'Повторный ввод одних и тех же сведений', status: 'new', supports: 1, due_at: iso(8) })} onClick={() => {}} />
          <AppealItem a={sample({ number: 'A-004', title: 'Короткие занятия по новым товарным линейкам', status: 'done', supports: 0, closed_at: iso(-2) })} who="assignee" progress onClick={() => {}} />
        </div>
      </Block>

      <Block title="Этапы обращения">
        <Steps a={{ status: 'in_progress' }} />
        <Steps a={{ status: 'clarify' }} mine />
        <Steps a={{ status: 'done' }} />
        <Steps a={{ status: 'rejected' }} />
      </Block>

      <Block title="Сообщения" hint="Фон нейтральный, смысл несет значок">
        <div className="note"><Send size={16} /><div><b>Информация</b>Обращение перейдет к другому ответственному</div></div>
        <div className="note green"><Check size={16} /><div><b>Готово</b>Обращение отправлено</div></div>
        <div className="note amber"><HelpCircle size={16} /><div><b>Нужен ваш ответ</b>О каких документах речь?</div></div>
        <div className="feed">
          <div className="sys"><span>Назначен ответственный: Никита Соколов · 30 окт</span></div>
          <div className="msg question">
            <Avatar name="Никита Соколов" size="sm" />
            <div className="body"><div><span className="who">Никита Соколов</span><span className="ink2"> просит уточнить</span><span className="small muted"> · 3 ч назад</span></div>
              <div className="bubble">Какие каналы используете чаще всего?</div></div>
          </div>
          <div className="msg">
            <Avatar name="Артем Белов" size="sm" />
            <div className="body"><div><span className="who">Артем Белов</span><span className="ink2"> отвечает</span><span className="small muted"> · 2 ч назад</span></div>
              <div className="bubble">Почту и мессенджер, итоги нигде не фиксируются.</div></div>
          </div>
        </div>
      </Block>
    </div>
  )
}
