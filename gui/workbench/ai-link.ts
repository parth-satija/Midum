import * as vscode from 'vscode'
import { rpc, postParent } from './rpc'
import { S, persist, persistNow } from './ctx'
import { activeKey, currentWorkspace, hostPathOf, keyOf, listTabs, type TabInfo } from './tabs'
import { baseName, hostToUri, pkey } from './paths'
import { artifactUri } from './artifacts'

/* Chat model <-> Workbench. Same protocol as the classic editor (see editor_link.py): Python sends
   editor_ai_request {id, op, args}; we act on the LIVE documents and answer with editor_ai_reply. */

const toVsUri = (p: string): vscode.Uri => { const u = hostToUri(p); return vscode.Uri.from({ scheme: u.scheme, path: u.path }) }

function state(): any {
  const tabs = listTabs()
  return {
    ok: true, workspace: currentWorkspace() || S.workspace || '', active: activeKey(),
    tabs: tabs.map((t) => ({ key: t.key, kind: t.kind, name: t.name, path: t.path, id: t.id, dirty: t.dirty, readOnly: t.readOnly, active: t.active }))
  }
}

function findTab(path: string): TabInfo | null {
  const q = pkey(path)
  const tabs = listTabs()
  let hit = tabs.find((t) => t.key === 'file:' + q)
  if (hit) return hit
  let m = tabs.filter((t) => t.path && (pkey(t.path) === q || pkey(t.path).slice(-(q.length + 1)) === '/' + q))
  if (m.length === 1) return m[0]
  m = tabs.filter((t) => (t.name || '').toLowerCase() === q.toLowerCase())
  return m.length === 1 ? m[0] : null
}

function docFor(tab: TabInfo): vscode.TextDocument | undefined {
  return vscode.workspace.textDocuments.find((d) => keyOf(d.uri) === tab.key)
}

function snapshot(a: any): any {
  const out = state(); out.file = null
  const tab = a?.path ? findTab(a.path) : listTabs().find((t) => t.active) ?? null
  if (!tab) return out
  const f: any = { key: tab.key, kind: tab.kind, name: tab.name, path: tab.path, id: tab.id, lang: '' }
  const doc = docFor(tab)
  if (!doc) { f.noText = true; f.reason = 'not a text tab'; out.file = f; return out }
  f.lang = doc.languageId
  f.dirty = doc.isDirty; f.readOnly = false; f.deleted = false; f.conflict = false
  f.version = doc.version; f.eol = doc.eol === vscode.EndOfLine.CRLF ? 'CRLF' : 'LF'; f.lineCount = doc.lineCount
  f.content = doc.getText().replace(/\r\n/g, '\n')
  const ed = vscode.window.visibleTextEditors.find((e) => e.document === doc) ?? (vscode.window.activeTextEditor?.document === doc ? vscode.window.activeTextEditor : undefined)
  if (ed) {
    f.cursor = { line: ed.selection.active.line + 1, col: ed.selection.active.character + 1 }
    if (!ed.selection.isEmpty) {
      const s = ed.selection
      f.selection = { startLine: s.start.line + 1, startCol: s.start.character + 1, endLine: s.end.line + 1, endCol: s.end.character + 1, text: doc.getText(s).replace(/\r\n/g, '\n') }
    }
    const vr = ed.visibleRanges
    if (vr.length) f.visible = { start: vr[0].start.line + 1, end: vr[vr.length - 1].end.line + 1 }
  }
  try {
    f.problems = vscode.languages.getDiagnostics(doc.uri).filter((d) => d.severity <= vscode.DiagnosticSeverity.Warning).slice(0, 40)
      .map((d) => ({ line: d.range.start.line + 1, col: d.range.start.character + 1, sev: d.severity === vscode.DiagnosticSeverity.Error ? 'error' : 'warning', msg: String(d.message).slice(0, 200) }))
  } catch { f.problems = [] }
  out.file = f
  return out
}

let flashType: vscode.TextEditorDecorationType | null = null
function flashLines(doc: vscode.TextDocument, ranges: { start: number; end: number }[] | undefined): void {
  const ed = vscode.window.visibleTextEditors.find((e) => e.document === doc)
  if (!ed || !ranges?.length) return
  flashType?.dispose()
  const mine = flashType = vscode.window.createTextEditorDecorationType({ isWholeLine: true, backgroundColor: new vscode.ThemeColor('editor.findMatchHighlightBackground') })
  const n = doc.lineCount
  const rs = ranges.map((r) => { const s = Math.max(1, Math.min(n, r.start)), e = Math.max(s, Math.min(n, r.end)); return new vscode.Range(s - 1, 0, e - 1, 0) })
  ed.setDecorations(mine, rs)
  ed.revealRange(rs[0], vscode.TextEditorRevealType.InCenterIfOutsideViewport)
  setTimeout(() => { mine.dispose(); if (flashType === mine) flashType = null }, 2500)
}

async function edit(a: any): Promise<any> {
  const tab = listTabs().find((t) => t.active)
  const doc = tab ? docFor(tab) : undefined
  if (!tab || !doc) return { ok: false, error: 'no editable text tab is active' }
  if (a.key && a.key !== tab.key) return { ok: false, stale: true, error: 'the active tab changed' }
  if (a.version !== undefined && a.version !== null && doc.version !== a.version) return { ok: false, stale: true, error: 'the buffer changed since it was read' }
  const eol = doc.eol === vscode.EndOfLine.CRLF ? '\r\n' : '\n'
  const we = new vscode.WorkspaceEdit()
  for (const e of a.edits || []) we.replace(doc.uri, new vscode.Range(e.sl - 1, e.sc - 1, e.el - 1, e.ec - 1), String(e.text).replace(/\r\n|\r|\n/g, eol))
  let ok = false
  try { ok = await vscode.workspace.applyEdit(we) } catch (e: any) { return { ok: false, error: 'could not apply the edit: ' + (e?.message || e) } }
  if (!ok) return { ok: false, error: 'could not apply the edit (the document may be read-only)' }
  flashLines(doc, a.flash)
  let saved = false
  if (a.save) { try { saved = await doc.save() } catch { saved = false } }
  return { ok: true, version: doc.version, dirty: doc.isDirty, saved: !!saved, lineCount: doc.lineCount }
}

export async function openFile(a: any): Promise<any> {
  const uri = toVsUri(a.path)
  const sel = a.line ? new vscode.Range(Math.max(0, a.line - 1), Math.max(0, (a.col || 1) - 1), Math.max(0, a.line - 1), Math.max(0, (a.col || 1) - 1)) : undefined
  try {
    const doc = await vscode.workspace.openTextDocument(uri)
    await vscode.window.showTextDocument(doc, { preview: false, selection: sel })
    return { ok: true, key: keyOf(uri), dirty: doc.isDirty, lineCount: doc.lineCount }
  } catch {
    try {                                              // binary / too large for a text editor: let VS Code pick an editor (image preview, ...)
      await vscode.commands.executeCommand('vscode.open', uri, { preview: false })
      return { ok: true, key: keyOf(uri), dirty: false, lineCount: 0, noText: true, reason: 'a binary or very large file' }
    } catch (e: any) { return { ok: false, error: 'it could not be opened: ' + (e?.message || e) } }
  }
}

export async function openFolder(path: string): Promise<any> {
  S.workspace = path
  S.tabs = []; S.active = null
  await persistNow()
  setTimeout(() => location.reload(), 250)             // the workbench binds one workspace per load
  return { ok: true, workspace: path }
}

// ── approval dialog ────────────────────────────────────────────────────────────────────────
const esc = (s: string): string => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c] as string))

function approveDialog(a: any): Promise<{ approved: boolean; always: boolean; note: string; timedOut?: boolean }> {
  return new Promise((resolve) => {
    const bg = document.createElement('div')
    bg.style.cssText = 'position:fixed;inset:0;z-index:2147483000;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center'
    const rows = (a.diff || []).map((l: string) => {
      const c = l.startsWith('+') ? 'color:#4ade80;background:rgba(74,222,128,.10)' : l.startsWith('-') ? 'color:#f87171;background:rgba(248,113,113,.10)' : l.startsWith('@@') ? 'color:#60a5fa' : ''
      return `<div style="${c};white-space:pre">${esc(l || ' ')}</div>`
    }).join('')
    const info = `${a.changes || 0} change(s): +${a.added || 0} / -${a.removed || 0} lines. ` + (a.save ? 'It will also be saved to disk.' : 'It stays unsaved in the editor (Ctrl+Z undoes it).') + (a.path ? '\n' + a.path : '')
    const btn = 'border:1px solid var(--vscode-button-border,#555);border-radius:4px;padding:6px 14px;cursor:pointer;font:inherit;color:var(--vscode-button-secondaryForeground,#ddd);background:var(--vscode-button-secondaryBackground,#3a3d41)'
    bg.innerHTML = `<div role="dialog" style="width:min(860px,92vw);max-height:84vh;display:flex;flex-direction:column;gap:10px;padding:18px;border-radius:8px;background:var(--vscode-editorWidget-background,#252526);color:var(--vscode-foreground,#ccc);border:1px solid var(--vscode-widget-border,#454545);font:13px var(--vscode-font-family,system-ui)">
      <div style="font-size:15px;font-weight:600">Midum wants to edit ${esc(a.name || 'this file')}</div>
      <div style="white-space:pre-wrap;opacity:.8">${esc(info)}</div>
      <div style="overflow:auto;border:1px solid var(--vscode-widget-border,#454545);border-radius:4px;padding:6px;font:12px var(--vscode-editor-font-family,Consolas,monospace);background:var(--vscode-editor-background,#1e1e1e);min-height:60px">${rows || '<div>(no visible difference)</div>'}</div>
      <input id="mz-note" placeholder="Optional note for Midum (sent if you reject)" spellcheck="false" style="padding:6px;border-radius:4px;border:1px solid var(--vscode-input-border,#555);background:var(--vscode-input-background,#3c3c3c);color:var(--vscode-input-foreground,#ccc);font:inherit">
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button data-v="always" style="${btn}">Approve &amp; don't ask again</button>
        <button data-v="no" style="${btn}">Reject</button>
        <button data-v="yes" style="${btn};background:var(--vscode-button-background,#0e639c);color:var(--vscode-button-foreground,#fff)">Approve</button>
      </div></div>`
    document.body.appendChild(bg)
    let finished = false, timer: number | undefined
    const done = (v: string, extra?: object): void => {
      if (finished) return; finished = true; clearTimeout(timer)
      const note = (bg.querySelector('#mz-note') as HTMLInputElement | null)?.value || ''
      document.removeEventListener('keydown', onKey, true); bg.remove()
      resolve({ approved: v === 'yes' || v === 'always', always: v === 'always', note, ...(extra || {}) })
    }
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); done('no') }
      else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); e.stopPropagation(); done('yes') }
      else if (e.key === 'Enter' && (e.target as HTMLElement)?.id === 'mz-note') e.stopPropagation()
    }
    document.addEventListener('keydown', onKey, true)
    bg.addEventListener('click', (e) => { const b = (e.target as HTMLElement).closest('button[data-v]') as HTMLElement | null; if (b) done(b.dataset.v!) })
    if (a.timeoutMs) timer = window.setTimeout(() => done('no', { timedOut: true }), a.timeoutMs)
    ;(bg.querySelector('button[data-v="no"]') as HTMLElement).focus()   // Reject is focused: a stray Enter can never approve
  })
}

async function approve(a: any): Promise<any> {
  const r = await approveDialog(a)
  if (r.always) {
    S.aiApprove = false; persist()
    vscode.window.showInformationMessage("AI edits no longer need approval (F1 > Midum: Toggle Approval for AI Edits turns it back on).")
  }
  return { ok: true, approved: !!r.approved, always: !!r.always, note: r.note || '', timedOut: !!r.timedOut }
}

export async function handleAiRequest(p: { id: string; op: string; args?: any }): Promise<void> {
  let res: any; const a = p.args || {}
  try {
    if (p.op === 'state') res = state()
    else if (p.op === 'snapshot') res = snapshot(a)
    else if (p.op === 'edit') res = await edit(a)
    else if (p.op === 'open_file') res = await openFile(a)
    else if (p.op === 'open_folder') res = await openFolder(a.path)
    else if (p.op === 'approve_edit') res = await approve(a)
    else res = { ok: false, error: 'unknown op: ' + p.op }
  } catch (e: any) { res = { ok: false, error: String(e?.message || e) } }
  try { await rpc('editor_ai_reply', p.id, res) } catch { /* ignore */ }
}

export async function openArtifactById(id: string): Promise<void> {
  try {
    const r = await rpc('editor_open_artifact', id)
    if (!r?.ok) { vscode.window.showWarningMessage(r?.error || 'Could not open artifact'); return }
    const u = artifactUri(id, r.name)
    await vscode.window.showTextDocument(vscode.Uri.from({ scheme: u.scheme, path: u.path }), { preview: false })
  } catch (e: any) { vscode.window.showWarningMessage('Could not open artifact: ' + (e?.message || e)) }
}
void baseName; void hostPathOf; void postParent
