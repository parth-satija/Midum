import * as vscode from 'vscode'
import * as monaco from 'monaco-editor'
import { postParent, rpc } from './rpc'
import { S, persistNow } from './ctx'
import { activeKey, currentWorkspace, keyOf, listTabs, syncTabs } from './tabs'
import { hostToUri, pkey } from './paths'
import { artifactUri } from './artifacts'

/* Simple editor <-> full IDE. The two pages never edit the same buffer at once: whichever is on screen is "active",
   and every switch hands the session over. Both read and write the same files on disk and the same editor_state.json;
   the handoff carries what lives only in memory: unsaved buffers, which tab is active, cursor, and shared settings. */

export interface HandTab {
  key: string; kind: 'file' | 'artifact'; path: string; id: string; name: string
  dirty: boolean; readOnly: boolean; preview: boolean; content?: string; line?: number; col?: number
}
export interface HandSettings { fontSize?: number; wordWrap?: boolean; minimap?: boolean; autoSave?: boolean; inlineAI?: boolean; aiApprove?: boolean; termShell?: string }
export interface HandData { workspace: string; active: string | null; tabs: HandTab[]; settings: HandSettings }

const lf = (t: string): string => t.replace(/\r\n/g, '\n')
const vsUri = (u: { scheme: string; path: string }): vscode.Uri => vscode.Uri.from({ scheme: u.scheme, path: u.path })

function collectSettings(): HandSettings {
  const e = vscode.workspace.getConfiguration('editor'), f = vscode.workspace.getConfiguration('files')
  return {
    fontSize: e.get<number>('fontSize'), wordWrap: e.get<string>('wordWrap') === 'on', minimap: e.get<boolean>('minimap.enabled') !== false,
    autoSave: (f.get<string>('autoSave') ?? 'off') !== 'off', inlineAI: S.inlineAI !== false, aiApprove: S.aiApprove !== false,
    termShell: S.termShell === 'powershell' ? 'powershell' : 'wsl'
  }
}

async function applySettings(s?: HandSettings): Promise<void> {
  if (!s) return
  const G = vscode.ConfigurationTarget.Global
  const e = vscode.workspace.getConfiguration('editor'), f = vscode.workspace.getConfiguration('files')
  const cur = collectSettings()
  if (typeof s.fontSize === 'number' && s.fontSize !== cur.fontSize) await e.update('fontSize', s.fontSize, G)
  if (typeof s.wordWrap === 'boolean' && s.wordWrap !== cur.wordWrap) await e.update('wordWrap', s.wordWrap ? 'on' : 'off', G)
  if (typeof s.minimap === 'boolean' && s.minimap !== cur.minimap) await e.update('minimap.enabled', s.minimap, G)
  if (typeof s.autoSave === 'boolean' && s.autoSave !== cur.autoSave) await f.update('autoSave', s.autoSave ? 'afterDelay' : 'off', G)
  if (typeof s.inlineAI === 'boolean') { S.inlineAI = s.inlineAI; await e.update('inlineSuggest.enabled', s.inlineAI, G) }
  if (typeof s.aiApprove === 'boolean') S.aiApprove = s.aiApprove
  if (s.termShell) S.termShell = s.termShell
}

export async function handoffOut(): Promise<HandData> {
  const tabs: HandTab[] = []
  for (const t of listTabs()) {
    const doc = vscode.workspace.textDocuments.find((d) => keyOf(d.uri) === t.key)
    const o: HandTab = { key: t.key, kind: t.kind, path: t.path, id: t.id, name: t.name, dirty: t.dirty, readOnly: t.readOnly, preview: t.preview }
    if (doc) {
      if (t.dirty) o.content = lf(doc.getText())
      const ed = vscode.window.visibleTextEditors.find((e) => e.document === doc)
      if (ed) { o.line = ed.selection.active.line + 1; o.col = ed.selection.active.character + 1 }
    }
    tabs.push(o)
  }
  syncTabs()
  await persistNow()                                   // the other editor boots/reads editor_state.json
  return { workspace: currentWorkspace() || S.workspace || '', active: activeKey(), tabs, settings: collectSettings() }
}

function tabUri(t: HandTab): vscode.Uri {
  return t.kind === 'artifact' ? vsUri(artifactUri(t.id, t.name)) : vsUri(hostToUri(t.path))
}

export async function handoffIn(d: HandData, reqId?: string): Promise<boolean> {
  // The workbench binds one workspace per load: a different folder means reloading, then applying after boot.
  if (d.workspace && pkey(d.workspace) !== pkey(currentWorkspace())) {
    try { sessionStorage.setItem('midum-handoff', JSON.stringify({ id: reqId ?? '', data: d })) } catch { /* ignore */ }
    S.workspace = d.workspace
    await persistNow()
    location.reload()
    return false
  }
  await applySettings(d.settings)
  const keep = new Set<string>()
  for (const t of d.tabs) {
    keep.add(t.key)
    const uri = tabUri(t)
    try {
      const doc = await vscode.workspace.openTextDocument(uri)
      await vscode.window.showTextDocument(doc, { preview: t.preview && !t.dirty, preserveFocus: true })
      if (t.dirty && typeof t.content === 'string') {
        if (lf(doc.getText()) !== t.content) {
          const we = new vscode.WorkspaceEdit()
          const eol = doc.eol === vscode.EndOfLine.CRLF ? '\r\n' : '\n'
          we.replace(uri, new vscode.Range(0, 0, doc.lineCount, 0), t.content.replace(/\n/g, eol))
          await vscode.workspace.applyEdit(we)
        }
      } else {
        await vscode.commands.executeCommand('workbench.action.files.revert')   // clean in the other editor: re-read the file
      }
    } catch {
      try { await vscode.commands.executeCommand('vscode.open', uri, { preview: false, preserveFocus: true }) } catch { /* not openable */ }
    }
  }
  // tabs that no longer exist in the other editor are closed (their unsaved content, if any, was discarded there)
  for (const g of vscode.window.tabGroups.all) {
    for (const tab of [...g.tabs]) {
      const u: vscode.Uri | undefined = (tab.input as any)?.uri
      const k = u ? keyOf(u) : null
      if (!k || keep.has(k)) continue
      try {
        if (tab.isDirty) { await vscode.window.showTextDocument(u!, { preserveFocus: false }); await vscode.commands.executeCommand('workbench.action.revertAndCloseActiveEditor') }
        else await vscode.window.tabGroups.close(tab, true)
      } catch { /* ignore */ }
    }
  }
  const act = d.tabs.find((t) => t.key === d.active)
  if (act) {
    try {
      const pos = act.line ? new vscode.Position(Math.max(0, act.line - 1), Math.max(0, (act.col || 1) - 1)) : undefined
      await vscode.window.showTextDocument(tabUri(act), { preview: false, selection: pos ? new vscode.Range(pos, pos) : undefined })
    } catch { /* ignore */ }
  }
  syncTabs()
  rpc('editor_ai_hello').catch(() => {})        // this editor is now the one the chat model talks to
  return true
}

/** Monaco measures glyph widths from the DOM. If that happens while the iframe is hidden (zero size / display:none), every
 *  width reads as 0 and the caret stays pinned to column 0 no matter where you click or type. Re-measure the fonts and force
 *  a re-layout whenever the IDE becomes visible (and once after boot), over a couple of frames so the iframe has its real size. */
export function remeasureEditors(): void {
  const run = (): void => {
    try { monaco.editor.remeasureFonts() } catch { /* not ready */ }
    try { monaco.editor.getEditors().forEach((e) => e.layout()) } catch { /* not ready */ }
    window.dispatchEvent(new Event('resize'))
  }
  requestAnimationFrame(() => { run(); requestAnimationFrame(run); setTimeout(run, 250) })
}

let busy: Promise<void> = Promise.resolve()
/** Resolves when no incoming handoff is being applied (main keeps the boot splash up until then). */
export function whenIdle(): Promise<void> { return busy }

/** Parent control messages. Replies go back as handoff / handoff_done. */
export function onControl(d: any): void {
  if (d.__ed === 'visibility') { (window as any).__midumHidden = !d.visible; if (d.visible) remeasureEditors(); return }
  if (d.__ed === 'handoff_out') {
    handoffOut().then((data) => postParent({ __ed: 'handoff', id: d.id, data })).catch(() => postParent({ __ed: 'handoff', id: d.id, data: null }))
  } else if (d.__ed === 'handoff_in') {
    const run = handoffIn(d.data, d.id).then((done) => { if (done) postParent({ __ed: 'handoff_done', id: d.id }) }).catch(() => postParent({ __ed: 'handoff_done', id: d.id }))
    busy = busy.then(() => run)
  }
}

/** After a workspace-changing reload: apply the handoff that triggered it. */
export async function applyPendingHandoff(): Promise<void> {
  let raw: string | null = null
  try { raw = sessionStorage.getItem('midum-handoff'); sessionStorage.removeItem('midum-handoff') } catch { /* ignore */ }
  if (!raw) return
  try {
    const { id, data } = JSON.parse(raw)
    await handoffIn(data, id)
    postParent({ __ed: 'handoff_done', id })
  } catch { /* ignore */ }
}
