const KEY = 'modus.token'

export const token = {
  get: () => localStorage.getItem(KEY),
  set: (t) => localStorage.setItem(KEY, t),
  clear: () => localStorage.removeItem(KEY),
}

async function request(method, path, body) {
  const headers = {}
  const t = token.get()
  if (t) headers.Authorization = `Bearer ${t}`
  let payload
  if (body instanceof FormData) payload = body
  else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  let res
  try {
    res = await fetch(`/api${path}`, { method, headers, body: payload })
  } catch {
    throw new Error('Нет связи с сервером')
  }
  if (res.status === 401 && t) {
    token.clear()
    window.dispatchEvent(new Event('modus:logout'))
  }
  if (!res.ok) {
    let msg = `Ошибка ${res.status}`
    try {
      const d = await res.json()
      if (typeof d.detail === 'string') msg = d.detail
      else if (Array.isArray(d.detail)) msg = 'Проверьте заполнение полей'
    } catch { /* не JSON */ }
    throw Object.assign(new Error(msg), { status: res.status })
  }
  return res
}

export const api = {
  get: (p) => request('GET', p).then((r) => r.json()),
  post: (p, b) => request('POST', p, b ?? {}).then((r) => r.json()),
  patch: (p, b) => request('PATCH', p, b).then((r) => r.json()),
  form: (p, fd) => request('POST', p, fd).then((r) => r.json()),
  async download(id, filename) {
    const r = await request('GET', `/attachments/${id}`)
    const url = URL.createObjectURL(await r.blob())
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  },
}

export function qs(params) {
  const u = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => { if (v !== '' && v !== null && v !== undefined && v !== false) u.set(k, v) })
  const s = u.toString()
  return s ? `?${s}` : ''
}
