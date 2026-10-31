import * as monaco from 'monaco-editor'
import * as vscode from 'vscode'
import { TOKEN } from './rpc'
import { S, persist } from './ctx'
import { baseName } from './paths'
import { ARTIFACT_SCHEME } from './artifacts'

/* Gemini inline suggestions (same /api/inline service and protocol as the classic editor), shown with Monaco's native
   ghost text: Tab accepts, Alt+\ triggers, Esc dismisses. Also handles "instruction comments" (write a comment, press
   Enter, get the code). */

const AI_DELAY = 200, AI_PREFIX = 5000, AI_SUFFIX = 1500
const AI_SKIP = /^(\.env(\..*)?|.*\.(pem|key|pfx|p12|kdbx|ppk)|id_(rsa|dsa|ecdsa|ed25519)|.*secrets?.*)$/i   // never sent anywhere

type AiState = 'idle' | 'busy' | 'cooling' | 'nokeys' | 'error'
const AI = { state: 'idle' as AiState, seq: 0, pausedUntil: 0, info: '', warnedNoKeys: false, onState: (): void => {}, calls: 0, skip: '' }
;(window as any).__midum = { ai: AI }   // diagnostics: how often the provider ran and why it skipped (DevTools / tests)
export const aiStatus = (): { state: AiState; info: string } => ({ state: AI.state, info: AI.info })
export function onAiState(fn: () => void): void { AI.onState = fn }
const setState = (s: AiState, info?: string): void => { AI.state = s; if (info !== undefined) AI.info = info; AI.onState() }

function post(body: any, signal?: AbortSignal): Promise<any> {
  return fetch(location.origin + '/api/inline', {
    method: 'POST', cache: 'no-store', signal,
    headers: { 'Content-Type': 'application/json', 'X-Midum-Token': TOKEN }, body: JSON.stringify(body)
  }).then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json() })
}

function isComment(s: string, lang: string): boolean {
  const t = s.trim()
  if (!t || lang === 'markdown' || lang === 'plaintext') return false
  if (t.startsWith('//') || t.startsWith('/*') || t.startsWith('--') || t.startsWith('<!--')) return true
  const c = t.charAt(0)
  if (c === '*' || c === '%' || c === ';') return true
  return c === '#' && !/^#(include|define|if|ifdef|ifndef|elif|else|endif|pragma|import|region|endregion|!)/.test(t)
}
const indentOf = (l: string): number => { let i = 0; while (i < l.length && (l[i] === ' ' || l[i] === '\t')) i++; return i }
const isCloser = (l: string): boolean => { const c = l.trim().charAt(0); return c === ')' || c === ']' || c === '}' }

function targetBlock(model: monaco.editor.ITextModel, start: number): { start: number; end: number } | null {
  const n = model.getLineCount()
  if (start > n || !model.getLineContent(start).trim()) return null
  const base = indentOf(model.getLineContent(start))
  let end = start
  for (let i = start + 1; i <= n && i - start < 80; i++) {
    const l = model.getLineContent(i)
    if (!l.trim()) {
      let j = i + 1; while (j <= n && !model.getLineContent(j).trim()) j++
      if (j <= n && j - start < 80 && indentOf(model.getLineContent(j)) > base) { i = j - 1; continue }
      break
    }
    const ind = indentOf(l)
    if (ind > base) { end = i; continue }
    if (ind === base && isCloser(l)) {
      end = i; const t = l.trim()
      if ('{:('.includes(t.charAt(t.length - 1))) continue
    }
    break
  }
  return { start, end }
}

function instrContext(model: monaco.editor.ITextModel, pos: monaco.Position): any {
  const lang = model.getLanguageId(), ln = pos.lineNumber, cur = model.getLineContent(ln)
  let cEnd = 0, onComment = false
  if (isComment(cur, lang)) {
    if (cur.slice(pos.column - 1).trim() !== '') return null
    cEnd = ln; onComment = true
  } else if (!cur.trim()) {
    let k = ln - 1; while (k >= 1 && k >= ln - 3 && !model.getLineContent(k).trim()) k--
    if (k >= 1 && isComment(model.getLineContent(k), lang)) cEnd = k; else return null
  } else return null
  let cStart = cEnd
  while (cStart > 1 && cEnd - cStart < 12 && isComment(model.getLineContent(cStart - 1), lang)) cStart--
  const lines: string[] = []; for (let i = cStart; i <= cEnd; i++) lines.push(model.getLineContent(i).trim())
  const tb = targetBlock(model, ln + 1)
  const tl: string[] = []; if (tb) for (let i = tb.start; i <= tb.end; i++) tl.push(model.getLineContent(i))
  return { cStart, cEnd, comment: lines.join('\n'), target: tl.join('\n'), tStart: tb ? tb.start : 0, tEnd: tb ? tb.end : 0, onComment }
}

function editWindow(model: monaco.editor.ITextModel, pos: monaco.Position, ictx: any): { start: number; lines: string[] } | null {
  let s: number, e: number
  if (ictx) { if (!ictx.target) return null; s = ictx.tStart; e = ictx.tEnd }
  else { s = Math.max(1, pos.lineNumber - 2); e = Math.min(model.getLineCount(), pos.lineNumber + 4) }
  const lines: string[] = []
  for (let i = s; i <= e; i++) { const l = model.getLineContent(i); if (l.length > 400) return null; lines.push(l) }
  return { start: s, lines }
}

function context(model: monaco.editor.ITextModel, pos: monaco.Position): { prefix: string; suffix: string } {
  const off = model.getOffsetAt(pos), a = Math.max(0, off - AI_PREFIX), p0 = model.getPositionAt(a)
  let prefix = model.getValueInRange(new monaco.Range(p0.lineNumber, p0.column, pos.lineNumber, pos.column))
  const p1 = model.getPositionAt(off + AI_SUFFIX)
  const suffix = model.getValueInRange(new monaco.Range(pos.lineNumber, pos.column, p1.lineNumber, p1.column))
  if (a > 0) { const nl = prefix.indexOf('\n'); if (nl >= 0) prefix = prefix.slice(nl + 1) }
  return { prefix, suffix }
}

export function aiEnabled(): boolean { return S.inlineAI !== false }
export function aiToggle(force?: boolean): void {
  S.inlineAI = typeof force === 'boolean' ? force : !aiEnabled()
  if (!S.inlineAI) AI.seq++; else { AI.pausedUntil = 0; setState('idle') }
  vscode.workspace.getConfiguration('editor').update('inlineSuggest.enabled', !!S.inlineAI, vscode.ConfigurationTarget.Global)
  persist(); AI.onState()
}

export function registerInlineAi(): void {
  monaco.languages.registerInlineCompletionsProvider('*', {
    async provideInlineCompletions(model, position, ctx, token) {
      const empty = { items: [] as monaco.languages.InlineCompletion[] }
      AI.calls++; AI.skip = ''
      if (!aiEnabled() || model.uri.scheme === 'output' || model.uri.scheme.startsWith('vscode')) { AI.skip = 'disabled-or-scheme'; return empty }
      const name = model.uri.path.split('/').pop() || ''
      if (AI_SKIP.test(name) || AI_SKIP.test(baseName(model.uri.path))) { AI.skip = 'secret-file'; return empty }
      if (ctx.selectedSuggestionInfo || Date.now() < AI.pausedUntil) { AI.skip = 'selected-or-paused'; return empty }
      const explicit = ctx.triggerKind === monaco.languages.InlineCompletionTriggerKind.Explicit
      const ictx = instrContext(model, position)
      if (ictx && ictx.onComment && !explicit) { AI.skip = 'typing-comment'; return empty }                  // still typing the comment: wait for Enter (or Alt+\)
      if (!ictx && /^[A-Za-z0-9_$]/.test(model.getLineContent(position.lineNumber).slice(position.column - 1))) { AI.skip = 'mid-identifier'; return empty }   // mid-identifier
      const my = ++AI.seq, ver = model.getVersionId()
      if (AI.state === 'busy') setState('idle')
      await new Promise((r) => setTimeout(r, ictx || explicit ? 0 : AI_DELAY))
      if (token.isCancellationRequested || my !== AI.seq) { AI.skip = 'superseded'; return empty }
      const c = context(model, position), ctrl = new AbortController()
      token.onCancellationRequested(() => ctrl.abort())
      setState('busy')
      const body: any = { op: 'suggest', prefix: c.prefix, suffix: c.suffix, lang: model.getLanguageId(), name }
      if (ictx) body.instr = { comment: ictx.comment, cursor: ictx.onComment ? 'end_of_comment_line' : 'blank_line_below_comment' }
      body.cursor = { line: position.lineNumber, col: position.column }
      const ew = editWindow(model, position, ictx); if (ew) body.edit = ew
      try {
        const r = await post(body, ctrl.signal)
        if (my !== AI.seq) return empty
        if (r.ok) {
          setState('idle', r.key ? `last request: key ${r.key} of ${r.keys}` + (r.cached ? ' (cached)' : `, ${r.ms} ms`) : AI.info)
          const action = r.action || 'insert'
          if (action !== 'insert') {
            // replace / delete proposals: whole lines [start..end]; shown by Monaco as an inline edit when it can
            const s = r.start, e = r.end, n = model.getLineCount()
            if (!(s >= 1 && e >= s && e <= n) || model.getVersionId() !== ver) return empty
            const del = action === 'delete'
            const range = del
              ? (e < n ? new monaco.Range(s, 1, e + 1, 1) : s > 1 ? new monaco.Range(s - 1, model.getLineMaxColumn(s - 1), e, model.getLineMaxColumn(e)) : new monaco.Range(s, 1, e, model.getLineMaxColumn(e)))
              : new monaco.Range(s, 1, e, model.getLineMaxColumn(e))
            return { items: [{ insertText: del ? '' : String(r.text || ''), range }] }
          }
          if (!r.text) return empty
          return { items: [{ insertText: r.text, range: new monaco.Range(position.lineNumber, position.column, position.lineNumber, position.column) }] }
        }
        if (r.error === 'no_keys') {
          setState('nokeys', 'No Gemini API keys found in the secrets file.'); AI.pausedUntil = Date.now() + 30000
          if (!AI.warnedNoKeys) { AI.warnedNoKeys = true; vscode.window.showWarningMessage('AI suggestions: add GEMINI_API_KEY (and _2 to _5) to your secrets file.') }
        } else if (r.cooling) {
          const wait = Math.min(Math.max(r.retry_after || 20, 5), 60)
          AI.pausedUntil = Date.now() + wait * 1000
          setState('cooling', `All ${r.total} keys are rate-limited; retrying in about ${Math.round(wait)} s.`)
        } else if (r.busy) setState('idle')
        else setState('error', r.error || 'request failed')
      } catch (e: any) {
        if (my === AI.seq) { if (e?.name === 'AbortError') setState('idle'); else setState('error', String(e?.message || e)) }
      }
      return empty
    },
    disposeInlineCompletions() {}
  } as any)
}

export async function aiProbe(): Promise<void> {
  try {
    const r = await post({ op: 'status' })
    if (!r?.ok) return
    if (!r.total) setState('nokeys', 'No Gemini API keys found in the secrets file.')
    else setState(AI.state === 'nokeys' ? 'idle' : AI.state, `${r.total} Gemini key${r.total > 1 ? 's' : ''} loaded (${r.model}), used round-robin with automatic hopping.`)
  } catch { setState('error', 'The suggestion service is not reachable.') }
}
void ARTIFACT_SCHEME
