// Telegram Mini App: то же приложение, открытое внутри Telegram из бота.
// Вход - по данным запуска, которые Telegram подписывает токеном бота (проверяет сервер).
const FLAG = 'modus.tg'

export const tg = { app: null, initData: '' }

function loadScript(src) {
  return new Promise((done) => {
    const s = document.createElement('script')
    s.src = src
    s.onload = s.onerror = done
    document.head.appendChild(s)
    setTimeout(done, 4000) // без связи с telegram.org открываемся как обычный сайт
  })
}

/** Вызывается до первого рендера. Вне Telegram ничего не загружает и не меняет. */
export async function initTelegram() {
  let inside = window.location.hash.includes('tgWebAppData')
  try { inside = inside || sessionStorage.getItem(FLAG) === '1' } catch { /* хранилище недоступно */ }
  if (!inside) return
  await loadScript('https://telegram.org/js/telegram-web-app.js')
  const app = window.Telegram?.WebApp
  if (!app?.initData) return
  try { sessionStorage.setItem(FLAG, '1') } catch { /* хранилище недоступно */ }
  tg.app = app
  tg.initData = app.initData
  document.documentElement.classList.add('tg')
  app.ready()
  app.expand()
  try { // шапка Telegram в цвет фона приложения
    const bg = getComputedStyle(document.body).backgroundColor.match(/\d+/g).slice(0, 3)
    const hex = `#${bg.map((n) => Number(n).toString(16).padStart(2, '0')).join('')}`
    app.setHeaderColor(hex)
    app.setBackgroundColor(hex)
  } catch { /* старый клиент Telegram */ }
}
