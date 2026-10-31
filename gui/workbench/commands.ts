import * as vscode from 'vscode'
import { rpc, postParent } from './rpc'
import { S, persist } from './ctx'
import { hostPathOf } from './tabs'
import { openFile, openFolder, openArtifactById } from './ai-link'
import { aiEnabled, aiStatus, aiToggle, onAiState } from './inline-ai'
import { applyTheme } from './theme'

const reg = (id: string, fn: (...a: any[]) => any): vscode.Disposable => vscode.commands.registerCommand(id, fn)

/** File types the host can run (keep in sync with _RUN_KINDS in gui/editor_backend.py). */
export const RUN_EXTS = ['py', 'pyw', 'java', 'html', 'htm', 'js', 'mjs', 'cjs', 'ts', 'go', 'php', 'rb', 'lua', 'c', 'cpp', 'cc', 'cxx', 'rs', 'sh', 'ps1', 'bat', 'cmd']
export const RUN_WHEN = 'resourceScheme == file && resourceExtname =~ /^\\.(' + RUN_EXTS.join('|') + ')$/'
const isRunnable = (d?: vscode.TextDocument): boolean => !!d && d.uri.scheme === 'file' && RUN_EXTS.includes(d.uri.path.split('.').pop()!.toLowerCase())

async function pickFolder(): Promise<void> {
  const r = await rpc('editor_pick_folder')
  if (r?.ok && r.path) await openFolder(r.path)
}
async function pickFiles(): Promise<void> {
  const r = await rpc('editor_pick_file')
  if (r?.ok) for (const p of r.paths || []) await openFile({ path: p })
}

/** Back to the simple editor: the parent runs the handoff (unsaved buffers, tabs, workspace) and hides this screen. */
export function exitIde(): void { postParent({ __ed: 'ide_close' }) }

export async function registerMidumCommands(): Promise<void> {
  reg('midum.exitIde', () => exitIde())
  reg('midum.retryFullMode', () => { try { localStorage.removeItem('midum-ide-safe') } catch { /* ignore */ } location.reload() })
  reg('midum.toggleAiEditApproval', () => {
    S.aiApprove = S.aiApprove === false ? true : false; persist()
    vscode.window.showInformationMessage('AI edits ' + (S.aiApprove === false ? 'no longer need approval' : 'now need your approval'))
  })
  reg('midum.toggleAiInline', () => { aiToggle(); vscode.window.showInformationMessage('AI inline suggestions ' + (aiEnabled() ? 'on' : 'off')) })
  reg('midum.toggleThemeSync', () => {
    S.syncTheme = S.syncTheme === false; persist(); applyTheme()
    vscode.window.showInformationMessage('Midum theme sync ' + (S.syncTheme === false ? 'off (pure VS Code theme)' : 'on'))
  })
  reg('midum.openArtifact', async () => {
    const r = await rpc('editor_list_artifacts')
    const list: any[] = r?.ok ? r.artifacts : []
    if (!list.length) { vscode.window.showInformationMessage('No chat artifacts yet.'); return }
    const pick = await vscode.window.showQuickPick(list.map((a) => ({ label: a.name, description: 'v' + a.version, id: a.id })), { placeHolder: 'Open a chat artifact' })
    if (pick) await openArtifactById((pick as any).id)
  })
  // Native pickers instead of VS Code's in-browser "simple" dialogs (these ids are what File > Open ... runs)
  for (const id of ['midum.openFolder', 'workbench.action.files.openFolder', 'workbench.action.files.openFolderViaWorkspace', 'workbench.action.files.openFileFolder']) reg(id, pickFolder)
  for (const id of ['midum.openFile', 'workbench.action.files.openFile']) reg(id, pickFiles)
  reg('midum.revealInExplorer', async (uri?: vscode.Uri) => {
    const u = uri instanceof Object && (uri as any).scheme ? uri : vscode.window.activeTextEditor?.document.uri
    if (u && u.scheme === 'file') await rpc('editor_reveal', hostPathOf(u))
  })
  reg('midum.runFile', async (uri?: vscode.Uri) => {
    const doc = uri && (uri as any).scheme === 'file'
      ? vscode.workspace.textDocuments.find((d) => d.uri.toString() === uri.toString()) ?? await vscode.workspace.openTextDocument(uri)
      : vscode.window.activeTextEditor?.document
    if (!isRunnable(doc)) { vscode.window.showInformationMessage('Open a runnable file to run it (' + RUN_EXTS.join(', ') + ').'); return }
    if (doc!.isDirty) await doc!.save()
    const r = await rpc('editor_run_file', hostPathOf(doc!.uri), S.termShell === 'powershell' ? 'powershell' : 'wsl')
    if (!r?.ok) vscode.window.showErrorMessage(r?.error || 'Could not run the file')
    else if (r.kind !== 'html') {
      await vscode.commands.executeCommand('workbench.action.terminal.focus')      // opens the panel if it is closed
    }
  })
  reg('midum.restartShell', async () => {
    const r = await rpc('editor_term_restart', S.termShell === 'powershell' ? 'powershell' : 'wsl')
    if (!r?.ok) vscode.window.showErrorMessage(r?.error || 'Could not restart the shell')
  })

  // ── status bar: AI state, back to the simple editor ─────────────────────────────────────────
  const ai = vscode.window.createStatusBarItem('midum.ai', vscode.StatusBarAlignment.Right, 100)
  ai.name = 'Midum AI suggestions'; ai.command = 'midum.toggleAiInline'
  const renderAi = (): void => {
    const st = aiStatus()
    if (!aiEnabled()) { ai.text = '$(sparkle) AI: off'; ai.tooltip = 'AI inline suggestions are off (click to turn on)' }
    else {
      ai.text = st.state === 'busy' ? '$(loading~spin) AI' : st.state === 'cooling' ? '$(clock) AI: cooling' : st.state === 'nokeys' ? '$(key) AI: no keys' : st.state === 'error' ? '$(error) AI: error' : '$(sparkle) AI: on'
      ai.tooltip = 'AI inline suggestions (Gemini). Tab accepts, Alt+\\ triggers. Click to turn off.' + (st.info ? ' - ' + st.info : '')
    }
    ai.show()
  }
  onAiState(renderAi); renderAi()
  const cl = vscode.window.createStatusBarItem('midum.simple', vscode.StatusBarAlignment.Left, 1000)
  cl.name = 'Simple editor'; cl.text = '$(arrow-left) Simple Editor'; cl.tooltip = 'Back to the simple editor (your open files, unsaved changes and workspace come with you)'; cl.command = 'midum.exitIde'; cl.show()

  // Run button in the status bar (visible while a runnable file is active); F5 / Ctrl+Alt+N also run it
  const run = vscode.window.createStatusBarItem('midum.run', vscode.StatusBarAlignment.Left, 999)
  run.name = 'Run current file'; run.text = '$(play) Run'; run.tooltip = 'Run the current file in the Midum shell (F5 or Ctrl+Alt+N)'; run.command = 'midum.runFile'
  const syncRun = (): void => { if (isRunnable(vscode.window.activeTextEditor?.document)) run.show(); else run.hide() }
  vscode.window.onDidChangeActiveTextEditor(syncRun); syncRun()
}
