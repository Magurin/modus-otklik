// Распознавание записи совещания там, где на сервере нет Whisper (демо-стенд на Vercel).
// Браузер достает звук из аудио или видео, режет его на куски до 2 минут по паузам и отправляет по одному:
// кусок WAV 16 кГц моно весит до 3,8 МБ и проходит лимит тела запроса Vercel (4,5 МБ) при записи любой длины.
import { api } from './api.js'

const RATE = 16000
const PART = 120          // с: максимальная длина куска
const SEARCH = 20         // с: в последних секундах куска ищем самую тихую точку, чтобы не резать слово
const FRAME = RATE / 20   // 50 мс
const SILENT = 0.005      // кусок тише этого пика - тишина, в API не отправляем
export const MAX_SIZE = 600 * 1024 * 1024  // файл целиком читается в память браузера

export const MEDIA_EXT = ['mp3', 'wav', 'm4a', 'ogg', 'oga', 'opus', 'aac', 'flac', 'mp4', 'mov', 'mkv', 'webm', 'avi']
export const isMedia = (file) => MEDIA_EXT.includes(file.name.split('.').pop().toLowerCase())
  || /^(audio|video)\//.test(file.type)

async function decode(file) {
  const Ctx = window.OfflineAudioContext || window.webkitOfflineAudioContext
  if (!Ctx) throw new Error('Этот браузер не умеет читать звук из файлов - откройте страницу в Chrome, Edge или Safari')
  const buf = await file.arrayBuffer()
  let audio
  try {
    audio = await new Ctx(1, 1, RATE).decodeAudioData(buf)  // браузер сам пересчитывает звук в 16 кГц
  } catch {
    throw new Error('Браузер не смог прочитать звук из этого файла. Сохраните запись в mp3, m4a или mp4 и загрузите снова')
  }
  const n = audio.length
  const k = audio.numberOfChannels
  if (k === 1) return audio.getChannelData(0)
  const mono = new Float32Array(n)
  for (let c = 0; c < k; c++) {
    const d = audio.getChannelData(c)
    for (let i = 0; i < n; i++) mono[i] += d[i] / k
  }
  return mono
}

function cut(s) {
  const parts = []
  let start = 0
  while (start < s.length) {
    let end = Math.min(start + PART * RATE, s.length)
    if (end < s.length) {
      let best = end
      let low = Infinity
      for (let f = end - SEARCH * RATE; f + FRAME <= end; f += FRAME) {
        let e = 0
        for (let i = f; i < f + FRAME; i++) e += s[i] * s[i]
        if (e < low) { low = e; best = f + FRAME / 2 }
      }
      end = best
    }
    parts.push({ start: start / RATE, data: s.subarray(start, end) })
    start = end
  }
  return parts
}

function peak(d) {
  let m = 0
  for (let i = 0; i < d.length; i++) { const a = Math.abs(d[i]); if (a > m) m = a }
  return m
}

function wav(d) {
  const v = new DataView(new ArrayBuffer(44 + d.length * 2))
  const str = (o, t) => { for (let i = 0; i < t.length; i++) v.setUint8(o + i, t.charCodeAt(i)) }
  str(0, 'RIFF'); v.setUint32(4, 36 + d.length * 2, true); str(8, 'WAVE')
  str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true)
  v.setUint32(24, RATE, true); v.setUint32(28, RATE * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true)
  str(36, 'data'); v.setUint32(40, d.length * 2, true)
  for (let i = 0; i < d.length; i++) {
    const x = Math.max(-1, Math.min(1, d[i]))
    v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true)
  }
  return new Blob([v.buffer], { type: 'audio/wav' })
}

const stamp = (t) => `[${String(Math.floor(t / 60)).padStart(2, '0')}:${String(Math.floor(t % 60)).padStart(2, '0')}]`

async function send(blob, i, prev) {
  for (let attempt = 0; ; attempt++) {
    const fd = new FormData()
    fd.set('audio', blob, `part${i + 1}.wav`)
    fd.set('prev', prev)
    try {
      return (await api.form('/meetings/transcribe', fd)).text || ''
    } catch (e) {
      // сбой связи или временная ошибка - повторяем один раз; отказ ключа, лимит и т.п. - сразу наверх
      if (attempt > 0 || (e.status && e.status < 500)) throw e
    }
  }
}

// Расшифровка в том же виде, что у Whisper на сервере: строки «[мм:сс] текст».
// onStep({ step: 'decode' }) и onStep({ step: 'part', done, total }); alive() === false - прекратить.
export async function transcribe(file, onStep, alive = () => true) {
  if (file.size > MAX_SIZE) throw new Error('Файл больше 600 МБ. Сохраните из видео только звук (mp3 или m4a) и загрузите его')
  onStep({ step: 'decode' })
  const parts = cut(await decode(file)).filter((p) => peak(p.data) >= SILENT)
  if (!parts.length) throw new Error('В записи не найдено речи')
  const lines = []
  let prev = ''
  for (let i = 0; i < parts.length; i++) {
    if (!alive()) return null
    onStep({ step: 'part', done: i, total: parts.length })
    const text = (await send(wav(parts[i].data), i, prev.slice(-300))).trim()
    if (text) { lines.push(`${stamp(parts[i].start)} ${text}`); prev = text }
  }
  if (!lines.length) throw new Error('В записи не найдено речи')
  return lines.join('\n')
}
