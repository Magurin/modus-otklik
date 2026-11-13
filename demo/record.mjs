// Запись демо-ролика: полный путь продукта на копии данных. Курсор и подписи рисуются поверх страницы.
import { chromium } from 'playwright'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const BASE = 'http://127.0.0.1:8011'
const HERE = path.dirname(fileURLToPath(import.meta.url))
const DEMO_FILE = path.join(HERE, fs.readdirSync(HERE).find((f) => f.endsWith('.txt')))
const OUT = path.join(HERE, 'out')
const W = 1440, H = 900

const browser = await chromium.launch({ channel: 'msedge', headless: true })
const ctx = await browser.newContext({ viewport: { width: W, height: H }, recordVideo: { dir: OUT, size: { width: W, height: H } }, locale: 'ru-RU' })
await ctx.addInitScript(() => {
  const boot = () => {
    if (document.getElementById('__cur')) return
    const c = document.createElement('div')
    c.id = '__cur'
    c.style.cssText = 'position:fixed;z-index:99999;width:22px;height:22px;border-radius:50%;background:rgba(51,88,212,.28);border:2px solid #3358d4;pointer-events:none;transform:translate(-50%,-50%);left:-50px;top:-50px;transition:width .12s,height .12s,background .12s'
    const cap = document.createElement('div')
    cap.id = '__cap'
    cap.style.cssText = 'position:fixed;z-index:99998;left:50%;bottom:26px;transform:translateX(-50%);background:rgba(23,23,22,.92);color:#fff;font:600 17px/24px "Inter Variable",system-ui,sans-serif;padding:11px 20px;border-radius:12px;pointer-events:none;max-width:80vw;text-align:center;opacity:0;transition:opacity .25s;box-shadow:0 8px 28px rgba(0,0,0,.25)'
    document.body.append(c, cap)
    addEventListener('mousemove', (e) => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px' }, true)
    addEventListener('mousedown', () => { c.style.width = '14px'; c.style.height = '14px'; c.style.background = 'rgba(51,88,212,.6)' }, true)
    addEventListener('mouseup', () => { c.style.width = '22px'; c.style.height = '22px'; c.style.background = 'rgba(51,88,212,.28)' }, true)
  }
  if (document.body) boot(); else addEventListener('DOMContentLoaded', boot)
})
const page = await ctx.newPage()
page.setDefaultTimeout(30000)
const sleep = (ms) => page.waitForTimeout(ms)

async function cap(text) {
  await page.evaluate((t) => { const el = document.getElementById('__cap'); if (el) { el.textContent = t; el.style.opacity = t ? '1' : '0' } }, text)
  await sleep(900)
}
async function click(loc, pause = 700) {
  const el = loc.first()
  await el.scrollIntoViewIfNeeded()
  const b = await el.boundingBox()
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 28 })
  await sleep(250)
  await el.click()
  await sleep(pause)
}
async function type(loc, text, delay = 16) {
  await click(loc, 200)
  await loc.first().pressSequentially(text, { delay })
  await sleep(500)
}
async function scroll(total, step = 90) {
  const n = Math.ceil(Math.abs(total) / step)
  for (let i = 0; i < n; i += 1) { await page.mouse.wheel(0, Math.sign(total) * step); await sleep(45) }
  await sleep(700)
}
const nav = (label) => click(page.locator('.nav-item', { hasText: label }))
const role = (label) => click(page.locator('.role-row', { hasText: label }), 1500)
async function logout() { await click(page.locator('.nav-item', { hasText: 'Выйти' }), 1200) }
async function loginAs(name) {
  await click(page.locator('.login-card .segs button', { hasText: 'Человек' }))
  await click(page.locator('.role-row', { hasText: name }), 1500)
}
const modalOk = () => click(page.locator('.modal footer .btn').last(), 1400)

process.on('uncaughtException', async (e) => {
  console.log('FAIL', e.message.split('\n').slice(0, 4).join(' | '))
  console.log('URL', page.url(), '| assignee/title vars may help')
  try { await page.screenshot({ path: path.join(OUT, 'fail.png') }) } catch { /* страница закрыта */ }
  await ctx.close(); await browser.close(); process.exit(1)
})
process.on('unhandledRejection', (e) => { throw e })

// ---------- 1. Вход ----------
await page.goto(BASE)
await page.waitForSelector('.role-row')
await page.mouse.move(W / 2, H / 2)
await cap('Модус.Отклик: обратная связь сотрудников в одном месте')
await sleep(1800)
await cap('Вход по роли. Начнем как сотрудник')
await role('Сотрудник')

// ---------- 2. Сотрудник: похожее обращение ----------
await cap('Сотрудник пишет своими словами - без форм и категорий')
const area = page.getByLabel('Текст обращения')
await type(area, 'В спецификациях одно и то же изделие встречается под разными обозначениями, проверка занимает много времени. Нужен способ найти актуальную карточку позиции.')
await page.waitForSelector('.composer .ai')
await cap('Система сама определила тип, категорию, приоритет и ответственного')
await sleep(2200)
if (await page.locator('.similar').count()) {
  await cap('И нашла похожее обращение коллеги - можно присоединиться, а не плодить дубли')
  await sleep(1500)
  await click(page.locator('.similar .btn'), 1800)
  await cap('Голос учтен: ответственный увидит, что проблема не единичная')
  await sleep(1800)
  await click(page.getByRole('button', { name: 'Написать еще' }))
}

// ---------- 3. Сотрудник: новое обращение ----------
await cap('Новое обращение - с предложением решения')
await type(page.getByLabel('Текст обращения'), 'Клиенты часто просят сертификаты на оборудование, а мы каждый раз ищем их по почте и у поставщиков. Ответ клиенту затягивается на день-два.')
await page.waitForSelector('.composer .ai')
await sleep(1200)
await click(page.getByRole('button', { name: 'Есть решение' }))
await type(page.getByLabel('Предлагаемое решение'), 'Собрать сертификаты в одной общей папке по производителям и назначить ответственного за актуальность.')
await sleep(900)
await click(page.locator('.composer .send'), 400)
await page.waitForURL(/\/appeals\/\d+/)
await page.waitForSelector('.steps')
await cap('Обращение зарегистрировано и сразу ушло ответственному. Этапы видны автору')
await sleep(2600)
const title = (await page.locator('h1.title').innerText()).trim()
const assignee = (await page.locator('.fact', { hasText: 'Ответственный' }).locator('.v').innerText()).split('\n')[0].trim()
await scroll(420)
await cap('Вся история обращения - в одном месте')
await sleep(1800)
await scroll(-420)
await nav('Главная')
await page.waitForSelector('.card.rows .item')
await scroll(260)
await cap('Все свои обращения сотрудник видит на главной, с прогрессом по каждому')
await sleep(2400)
await scroll(-260)

// ---------- 3б. Telegram ----------
await click(page.locator('.nav-me'), 1200)
await cap('Писать можно и в Telegram: бот привязывается одним кодом')
if (await page.getByRole('button', { name: 'Подключить' }).count()) {
  await click(page.getByRole('button', { name: 'Подключить' }), 1500)
  await cap('Боту можно написать текстом или надиктовать голосом - обращение уйдет тем же маршрутом, ответы придут в чат')
  await sleep(3200)
} else {
  await sleep(2000)
}

// ---------- 4. Руководитель: уточнение ----------
await logout()
await cap(`Теперь - ответственный: ${assignee}`)
await loginAs(assignee)
await nav('Входящие')
await page.waitForSelector('.segs')
await cap('Входящие: новые, ждущие автора и просроченные - по вкладкам')
await sleep(1800)
await click(page.locator('.item', { hasText: title }), 1200)
await page.waitForSelector('.actions')
await scroll(260)
await cap('Нужно больше деталей - ответственный задает вопрос автору')
await click(page.getByRole('button', { name: 'Уточнить', exact: true }))
await type(page.getByLabel('Вопрос автору'), 'По каким производителям сертификаты запрашивают чаще всего?')
await modalOk()
await cap('Статус сменился на «Ожидает уточнения», автор получил уведомление')
await scroll(-260)
await sleep(2000)

// ---------- 5. Сотрудник отвечает ----------
await logout()
await cap('Автор видит уведомление и отвечает')
await role('Сотрудник')
await click(page.locator('.nav-item', { hasText: 'Уведомления' }), 1500)
await click(page.locator('.n-item').first(), 1200)
await page.waitForSelector('.reply')
await sleep(1500)
await type(page.getByLabel('Сообщение'), 'Чаще всего по камерам и контроллерам доступа, три основных производителя.')
await click(page.locator('.reply .send'), 1500)
await cap('Ответ ушел, обращение вернулось на рассмотрение')
await sleep(1800)

// ---------- 6. Руководитель: работа и результат ----------
await logout()
await cap('Ответственный берет обращение в работу')
await loginAs(assignee)
await nav('Входящие')
await click(page.locator('.item', { hasText: title }), 1200)
await page.waitForSelector('.actions')
await scroll(300)
await click(page.locator('.actions .btn.primary'))
await type(page.locator('.modal textarea'), 'Соберем сертификаты в общую папку до конца недели.')
await modalOk()
await cap('Другие действия: отклонить с причиной, передать коллеге, сменить категорию или срок')
await click(page.getByRole('button', { name: 'Другие действия' }), 1800)
await click(page.getByRole('menuitem', { name: 'Передать другому' }), 900)
await click(page.locator('.modal .sel'), 1800)
await page.keyboard.press('Escape'); await sleep(400)
await page.keyboard.press('Escape'); await sleep(700)
await cap('Работа сделана - результат фиксируется и виден автору')
await click(page.locator('.actions .btn.primary'))
await type(page.locator('.modal textarea'), 'Сертификаты собраны в общей папке по производителям, ответственный за актуальность назначен.')
await modalOk()
await scroll(-300)
await sleep(2200)

// ---------- 7. Все обращения и фильтры ----------
await nav('Все обращения')
await page.waitForSelector('.card.rows .item')
await cap('Все обращения: поиск, вкладки по состоянию и фильтры')
await click(page.locator('.segs button', { hasText: 'Просрочены' }), 1500)
await click(page.getByRole('button', { name: /Фильтры/ }), 900)
await click(page.locator('.filters .sel').nth(2), 1600)
await page.keyboard.press('Escape'); await sleep(500)
await click(page.locator('.segs button', { hasText: 'Открытые' }), 1000)

// ---------- 8. Совещания ----------
await nav('Совещания')
await cap('Главное отличие: замечания с совещаний не теряются')
await sleep(1500)
await click(page.getByRole('button', { name: 'Загрузить запись' }), 900)
await page.locator('.dropzone input[type=file]').setInputFiles(DEMO_FILE)
await sleep(1200)
await cap('Аудио, видео или протокол. Речь распознается на сервере компании')
await sleep(1500)
await modalOk()
await page.waitForURL(/\/meetings\/\d+/)
await cap('Система расшифровывает запись и выделяет главное')
await page.locator('.card.pad .actions').first().waitFor({ timeout: 120000 })
await cap('Проблемы, идеи, решения и поручения - с цитатой и автором')
await sleep(1500)
await scroll(520)
await sleep(1200)
await cap('Пункт становится обращением только после подтверждения человеком')
await click(page.getByRole('button', { name: 'Создать обращение' }).first(), 2200)
await click(page.getByRole('button', { name: 'Создать обращение' }).first(), 2200)
await cap('Пункт можно поправить или пропустить')
await click(page.getByRole('button', { name: 'Изменить пункт' }).first(), 1800)
await click(page.getByRole('button', { name: 'Изменить пункт' }).first(), 600)
await click(page.getByRole('button', { name: 'Пропустить' }).first(), 1800)
await page.evaluate(() => window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }))
await sleep(1600)
await cap('Созданные обращения уже у ответственных, со сроком и историей')
await sleep(2400)
await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'smooth' }))
await sleep(900)

// ---------- 9. Директор ----------
await logout()
await cap('Директор: вся картина без необходимости вспоминать совещания')
await role('Директор')
await page.waitForSelector('.tiles')
await sleep(2200)
await cap('Что просрочено и что осталось без ответа')
await scroll(380)
await sleep(1500)
await cap('Сводка от ИИ и повторяющиеся темы - одна проблема глазами разных отделов')
await scroll(520)
await sleep(2800)
await cap('Динамика, категории, каналы и нагрузка на ответственных')
await scroll(700)
await sleep(1500)
await scroll(900)
await sleep(1500)
await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'smooth' }))
await sleep(900)

// ---------- 10. Администратор ----------
await logout()
await cap('Администратор: маршруты, замещения и каналы')
await role('Администратор')
await nav('Настройки')
await page.waitForSelector('table.tbl')
await cap('У каждой категории - ответственный и срок рассмотрения')
await sleep(2200)
await click(page.locator('.tabs button', { hasText: 'Сотрудники' }), 1200)
await cap('Сотрудник в отпуске - обращения автоматически уйдут заместителю')
await click(page.getByLabel('Отсутствует: Тимур Жумабаев'), 1500)
await click(page.locator('.tabs button', { hasText: 'Кто что разбирает' }), 1200)
const row = page.locator('table.tbl tr', { hasText: 'Каталог и данные' })
await row.scrollIntoViewIfNeeded()
const rb = await row.boundingBox()
await page.mouse.move(rb.x + rb.width * 0.72, rb.y + rb.height / 2, { steps: 25 })
await cap('«Каталог и данные» теперь уходит заместителю - обращение не зависнет')
await sleep(2800)
await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'smooth' }))
await sleep(700)
await click(page.locator('.tabs button', { hasText: 'Каналы' }), 1200)
await cap('Каналы приема: форма, Telegram-бот, почта, совещания')
await sleep(1800)
await type(page.locator('label.field', { hasText: 'Тема' }).locator('input'), 'Акты сверки')
await type(page.locator('label.field', { hasText: 'Текст' }).locator('textarea'), 'Акты сверки приходят в разных форматах, приходится сводить вручную перед закрытием месяца.')
await click(page.getByRole('button', { name: 'Принять письмо' }), 400)
await page.locator('.note.green').waitFor({ timeout: 60000 })
await cap('Письмо стало обращением: автор найден по адресу, категория подобрана')
await sleep(2600)

// ---------- 11. Финал ----------
await logout()
await role('Сотрудник')
await nav('Что сделано')
await page.waitForSelector('.card.rows .item')
await cap('Результаты видят все сотрудники: писать не бесполезно')
await sleep(3000)
await cap('Модус.Отклик: услышать, не потерять, довести до результата')
await sleep(3000)

await ctx.close()
await browser.close()
const f = fs.readdirSync(OUT).filter((x) => x.endsWith('.webm')).map((x) => `${OUT}/${x}`).sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs)[0]
console.log('VIDEO', f, Math.round(fs.statSync(f).size / 1e6), 'MB')
