// Bridge to Midum: postMessage RPC to the parent (name-whitelisted editor_* Api methods, same protocol as the
// classic editor) plus token-protected HTTP calls to Midum's localhost server.
export type EventHandler = (kind: string, payload: any) => void

let seq = 0
const pending = new Map<number, { res: (v: any) => void; rej: (e: Error) => void }>()
let eventHandler: EventHandler | null = null
const eventQueue: { kind: string; payload: any }[] = []
let themeHandler: ((vars: Record<string, string>) => void) | null = null
let lastTheme: Record<string, string> | null = null
const themeWaiters: ((v: Record<string, string>) => void)[] = []
let controlHandler: ((d: any) => void) | null = null
const controlQueue: any[] = []

window.addEventListener('message', (e) => {
  if (e.source !== parent) return
  const d = e.data
  if (!d || !d.__ed) return
  if (d.__ed === 'rpc_result') {
    const p = pending.get(d.id)
    if (!p) return
    pending.delete(d.id)
    if (d.error) p.rej(new Error(d.error))
    else p.res(d.result)
  } else if (d.__ed === 'theme') {
    lastTheme = d.vars || {}
    themeWaiters.splice(0).forEach((w) => w(lastTheme!))
    themeHandler?.(lastTheme!)
  } else if (d.__ed === 'handoff_out' || d.__ed === 'handoff_in' || d.__ed === 'visibility') {
    if (controlHandler) controlHandler(d)
    else controlQueue.push(d)
  } else if (d.__ed === 'event') {
    if (eventHandler) eventHandler(d.kind, d.payload)
    else eventQueue.push({ kind: d.kind, payload: d.payload })
  }
})

export function postParent(msg: any): void {
  try { parent.postMessage(msg, '*') } catch { /* ignore */ }
}

export function rpc<T = any>(method: string, ...args: any[]): Promise<T> {
  return new Promise<T>((res, rej) => {
    const id = ++seq
    pending.set(id, { res, rej })
    parent.postMessage({ __ed: 'rpc', id, method, args }, '*')
    if (!/^editor_pick/.test(method)) {
      setTimeout(() => { if (pending.delete(id)) rej(new Error('timed out: ' + method)) }, 60000)
    }
  })
}

/** Tell the parent we can receive theme + events; resolves with the first theme (or {} after a timeout). */
export function handshake(timeoutMs = 1500): Promise<Record<string, string>> {
  return new Promise((resolve) => {
    const t = setTimeout(() => resolve(lastTheme || {}), timeoutMs)
    themeWaiters.push((v) => { clearTimeout(t); resolve(v) })
    postParent({ __ed: 'ready' })
  })
}
export function setEventHandler(h: EventHandler): void {
  eventHandler = h
  eventQueue.splice(0).forEach((q) => h(q.kind, q.payload))
}
export function setThemeHandler(h: (vars: Record<string, string>) => void): void {
  themeHandler = h
  if (lastTheme) h(lastTheme)
}
export function reportDirty(count: number): void { postParent({ __ed: 'dirty', count }) }

// ── token-protected HTTP (same origin as this page) ──────────────────────────────────────────
export const TOKEN: string = (document.querySelector('meta[name="midum-token"]') as HTMLMetaElement | null)?.content ?? ''

export class ApiError extends Error {
  constructor(public code: string, message: string) { super(message) }
}

export async function fsJson<T = any>(op: string, body: any, signal?: AbortSignal): Promise<T> {
  const r = await fetch('/api/fs/' + op, {
    method: 'POST', cache: 'no-store', signal,
    headers: { 'Content-Type': 'application/json', 'X-Midum-Token': TOKEN },
    body: JSON.stringify(body ?? {})
  })
  if (r.status === 403) throw new ApiError('EACCES', 'Midum refused the request (bad token)')
  const j = await r.json()
  if (j && j.ok === false) throw new ApiError(j.code || 'EUNKNOWN', j.error || 'request failed')
  return j as T
}

export async function fsRead(path: string): Promise<{ data: Uint8Array; mtime: number; size: number }> {
  const r = await fetch('/api/fs/read', {
    method: 'POST', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', 'X-Midum-Token': TOKEN },
    body: JSON.stringify({ path })
  })
  // errors come back as JSON (HTTP 200); file contents come back as application/octet-stream
  if (!r.ok || (r.headers.get('Content-Type') || '').includes('application/json')) {
    let j: any = {}
    try { j = await r.json() } catch { /* ignore */ }
    throw new ApiError(j.code || 'EUNKNOWN', j.error || 'read failed (' + r.status + ')')
  }
  const buf = new Uint8Array(await r.arrayBuffer())
  return { data: buf, mtime: Number(r.headers.get('X-Midum-Mtime') || 0), size: Number(r.headers.get('X-Midum-Size') || buf.length) }
}

export async function fsWrite(path: string, data: Uint8Array, opts: { create: boolean; overwrite: boolean }): Promise<{ mtime: number; size: number }> {
  const r = await fetch('/api/fs/write', {
    method: 'POST', cache: 'no-store',
    headers: {
      'Content-Type': 'application/octet-stream', 'X-Midum-Token': TOKEN,
      'X-Midum-Path': encodeURIComponent(path),
      'X-Midum-Create': opts.create ? '1' : '0', 'X-Midum-Overwrite': opts.overwrite ? '1' : '0'
    },
    body: data as any
  })
  const j = await r.json().catch(() => ({}))
  if (!r.ok || j.ok === false) throw new ApiError(j.code || 'EUNKNOWN', j.error || 'write failed (' + r.status + ')')
  return j
}

/** Take (and clear) control messages that arrived before the handler was installed (used to carry a handoff across a safe-mode reload). */
export function takeControlQueue(): any[] { return controlQueue.splice(0) }

/** Parent -> IDE control messages: handoff_out / handoff_in / visibility. */
export function setControlHandler(h: (d: any) => void): void {
  controlHandler = h
  controlQueue.splice(0).forEach(h)
}
