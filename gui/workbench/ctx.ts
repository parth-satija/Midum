import { rpc } from './rpc'

/** Persisted editor state (same editor_state.json as the classic editor; unknown keys are preserved). */
export const S: Record<string, any> = {}
let timer: number | undefined

export async function loadState(): Promise<void> {
  try {
    const r = await rpc('editor_get_state')
    if (r?.ok) Object.assign(S, r.state || {})
  } catch { /* first run */ }
}

export function persist(): void {
  clearTimeout(timer)
  timer = window.setTimeout(() => { rpc('editor_set_state', S).catch(() => {}) }, 400)
}
export function persistNow(): Promise<any> {
  clearTimeout(timer)
  return rpc('editor_set_state', S).catch(() => {})
}
