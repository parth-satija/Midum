import 'vscode/localExtensionHost'
import { initialize } from '@codingame/monaco-vscode-api'
import { ExtensionHostKind, registerExtension } from '@codingame/monaco-vscode-api/extensions'
import { StandaloneServices, IFileService } from '@codingame/monaco-vscode-api'
import { URI } from '@codingame/monaco-vscode-api/vscode/vs/base/common/uri'
import type { IWorkbenchConstructionOptions } from '@codingame/monaco-vscode-api/vscode/vs/workbench/browser/web.api'

// Built-in language grammars, language configurations and the default VS Code themes (pure data extensions)
import '@codingame/monaco-vscode-theme-defaults-default-extension'
import '@codingame/monaco-vscode-theme-seti-default-extension'
import '@codingame/monaco-vscode-python-default-extension'
import '@codingame/monaco-vscode-javascript-default-extension'
import '@codingame/monaco-vscode-typescript-basics-default-extension'
import '@codingame/monaco-vscode-json-default-extension'
import '@codingame/monaco-vscode-html-default-extension'
import '@codingame/monaco-vscode-css-default-extension'
import '@codingame/monaco-vscode-scss-default-extension'
import '@codingame/monaco-vscode-less-default-extension'
import '@codingame/monaco-vscode-markdown-basics-default-extension'
import '@codingame/monaco-vscode-markdown-math-default-extension'
import '@codingame/monaco-vscode-java-default-extension'
import '@codingame/monaco-vscode-cpp-default-extension'
import '@codingame/monaco-vscode-csharp-default-extension'
import '@codingame/monaco-vscode-go-default-extension'
import '@codingame/monaco-vscode-rust-default-extension'
import '@codingame/monaco-vscode-php-default-extension'
import '@codingame/monaco-vscode-ruby-default-extension'
import '@codingame/monaco-vscode-lua-default-extension'
import '@codingame/monaco-vscode-swift-default-extension'
import '@codingame/monaco-vscode-shellscript-default-extension'
import '@codingame/monaco-vscode-powershell-default-extension'
import '@codingame/monaco-vscode-bat-default-extension'
import '@codingame/monaco-vscode-yaml-default-extension'
import '@codingame/monaco-vscode-xml-default-extension'
import '@codingame/monaco-vscode-sql-default-extension'
import '@codingame/monaco-vscode-latex-default-extension'
import '@codingame/monaco-vscode-docker-default-extension'
import '@codingame/monaco-vscode-ini-default-extension'
import '@codingame/monaco-vscode-make-default-extension'
import '@codingame/monaco-vscode-log-default-extension'
import '@codingame/monaco-vscode-diff-default-extension'
import '@codingame/monaco-vscode-emmet-default-extension'
import '@codingame/monaco-vscode-media-preview-default-extension'
import '@codingame/monaco-vscode-configuration-editing-default-extension'
import '@codingame/monaco-vscode-references-view-default-extension'
import '@codingame/monaco-vscode-search-result-default-extension'
import '@codingame/monaco-vscode-merge-conflict-default-extension'
import '@codingame/monaco-vscode-git-base-default-extension'
import '@codingame/monaco-vscode-github-authentication-default-extension'

import { handshake, rpc, setEventHandler, setThemeHandler, setControlHandler, takeControlQueue, fsJson, postParent } from './rpc'
import * as vscode from 'vscode'
import { onControl, applyPendingHandoff, whenIdle, remeasureEditors } from './handoff'
import { currentWorkspace } from './tabs'
import { loadState, S, persist } from './ctx'
import { installWorkers, buildServices } from './setup'
import { setHost, hostToUri } from './paths'
import { registerSearchProviders } from './search'
import { ArtifactFileSystemProvider, ARTIFACT_SCHEME } from './artifacts'
import { termBoot, termOnLine } from './terminal'
import { trackTabs } from './tabs'
import { handleAiRequest, openArtifactById, openFile, openFolder } from './ai-link'
import { applyTheme, isDark } from './theme'
import { registerInlineAi, aiProbe } from './inline-ai'
import { registerMidumCommands, RUN_WHEN } from './commands'
import { installExtAssetProxy } from './ext-assets'
import { registerMidumChat } from './midum-chat'

// Extension icon themes load their SVGs from open-vsx.org, which the embedded browser will not render: route them via Midum's server
installExtAssetProxy()

// Inside Midum the workbench lives in an iframe, where Keyboard Lock is not allowed and would throw an uncaught error.
try { Object.defineProperty((navigator as any).keyboard, 'lock', { value: () => Promise.resolve(), configurable: true }) } catch { /* not supported: nothing to patch */ }

const bootS = document.getElementById('boot-s')!
const bootEl = document.getElementById('boot')!

// ── startup diagnostics + watchdog ───────────────────────────────────────────────────────────
// If startup hangs (for example the browser engine cannot load the {uuid}.localhost iframe that hosts extensions), the page
// first retries once in "safe mode" (no worker extension host), and if that also fails it shows exactly where it is stuck.
const SAFE = (() => { try { return localStorage.getItem('midum-ide-safe') === '1' } catch { return false } })()
const t0 = Date.now()
let stage = 'starting', failed = false
const status = (t: string): void => { stage = t; bootS.textContent = t }
const diag: string[] = []
const note = (s: string): void => { diag.push(('+' + Math.round((Date.now() - t0) / 1000) + 's ' + s).slice(0, 500)); if (diag.length > 14) diag.shift() }
window.addEventListener('error', (e) => note('error: ' + (e.message || e)))
window.addEventListener('unhandledrejection', (e) => note('rejection: ' + ((e.reason && (e.reason.message || e.reason)) || e.reason)))
const realError = console.error.bind(console)
console.error = (...a: any[]): void => { note('console.error: ' + a.map((x) => String((x && x.message) || x)).join(' ')); realError(...a) }
const inflight = new Map<number, { url: string; t: number }>()
let fetchId = 0
const realFetch = window.fetch.bind(window)
window.fetch = ((input: any, init?: any) => {
  const id = ++fetchId
  inflight.set(id, { url: String(typeof input === 'string' ? input : input?.url ?? input), t: Date.now() })
  return realFetch(input, init).finally(() => { inflight.delete(id) })
}) as typeof window.fetch

function probe<T>(fn: (done: (v: string) => void) => void, ms = 4000): Promise<string> {
  return new Promise((resolve) => {
    const to = setTimeout(() => resolve('timed out after ' + ms / 1000 + 's'), ms)
    try { fn((v) => { clearTimeout(to); resolve(v) }) } catch (e: any) { clearTimeout(to); resolve('exception: ' + (e?.message || e)) }
  })
}
async function buildReport(): Promise<string> {
  const [idb, worker, sub] = await Promise.all([
    probe((done) => { const r = indexedDB.open('midum-probe'); r.onsuccess = () => { r.result.close(); done('ok') }; r.onerror = () => done('error: ' + r.error); r.onblocked = () => done('blocked') }),
    probe((done) => { const w = new Worker(URL.createObjectURL(new Blob(['postMessage(1)'], { type: 'text/javascript' }))); w.onmessage = () => { w.terminate(); done('ok') }; w.onerror = (e) => done('error: ' + (e.message || 'blocked')) }),
    probe((done) => { fetch(`${location.protocol}//probe.localhost:${location.port}/wb/stub-empty.js`, { mode: 'no-cors' }).then(() => done('reachable'), (e) => done('FAILED: ' + e)) })
  ])
  const now = Date.now()
  const flight = [...inflight.values()].map((f) => `  ${Math.round((now - f.t) / 1000)}s  ${f.url.slice(0, 140)}`)
  return [
    'Midum IDE startup report', `stage: ${stage} (+${Math.round((now - t0) / 1000)}s)   safe mode: ${SAFE}`,
    `page: ${location.href}`, `framed: ${parent !== window}   secure context: ${isSecureContext}   cross-origin isolated: ${crossOriginIsolated}`,
    `agent: ${navigator.userAgent}`, `IndexedDB: ${idb}`, `Web Worker (blob): ${worker}`, `*.localhost subdomain: ${sub}`,
    'requests still in flight: ' + (flight.length ? '\n' + flight.join('\n') : 'none'),
    'recent errors: ' + (diag.length ? '\n  ' + diag.join('\n  ') : 'none')
  ].join('\n')
}

function fatal(e: unknown): void {
  failed = true
  console.error(e)
  const pre = document.getElementById('boot-e')!, btn = document.getElementById('boot-b') as HTMLButtonElement, cp = document.getElementById('boot-c') as HTMLButtonElement
  pre.style.display = 'block'; pre.textContent = String((e as any)?.stack || (e as any)?.message || e)
  btn.style.display = 'inline-block'; cp.style.display = 'inline-block'
  btn.onclick = () => { postParent({ __ed: 'ide_close' }) }
  cp.onclick = () => { navigator.clipboard?.writeText(pre.textContent || '').then(() => { cp.textContent = 'Copied' }, () => { const r = document.createRange(); r.selectNodeContents(pre); getSelection()?.removeAllRanges(); getSelection()?.addRange(r); cp.textContent = 'Selected: press Ctrl+C' }) }
  document.querySelector('#boot .spin')?.remove()
  ;(document.querySelector('#boot .t') as HTMLElement).textContent = 'The VS Code workbench could not start'
}

setTimeout(async () => {
  if (bootEl.classList.contains('hide') || failed) return
  const report = await buildReport()
  if (!SAFE) {
    let flagged = false
    try { localStorage.setItem('midum-ide-safe', '1'); localStorage.setItem('midum-ide-safe-why', report); flagged = localStorage.getItem('midum-ide-safe') === '1' } catch { /* storage blocked */ }
    if (flagged) {
      const m = takeControlQueue().find((d) => d.__ed === 'handoff_in')          // keep the session the simple editor handed over
      if (m) { try { sessionStorage.setItem('midum-handoff', JSON.stringify({ id: m.id, data: m.data })) } catch { /* ignore */ } }
      location.reload()
      return
    }
  }
  fatal(new Error(report))
}, SAFE ? 60000 : 30000)

async function main(): Promise<void> {
  installWorkers()
  status('Connecting to Midum')
  const themeVars = await handshake()
  for (const [k, v] of Object.entries(themeVars)) document.documentElement.style.setProperty(k, v)
  await loadState()

  status('Reading the file system')
  const info = await fsJson('info', {})
  setHost({ win: !!info.win, sep: info.sep, caseSensitive: !!info.caseSensitive, trash: !!info.trash })

  // the workspace is a real folder on disk; without a (valid) one the workbench shows its Welcome / Open Folder page
  let ws: string = S.workspace || info.defaultWorkspace || ''
  try { const st = await fsJson('stat', { path: ws }); if (st.type !== 2) ws = '' } catch { ws = '' }
  const folderUri = ws ? hostToUri(ws) : undefined

  // webview iframes (Markdown preview, extension pages, settings editor) need a distinct origin: {uuid}.localhost
  const webviewDomain = `${location.protocol}//{{uuid}}.localhost:${location.port}/`
  const userRoot: string = info.userData
  const userMap = {
    to: (u: URI): string => userRoot.replace(/\\/g, '/').replace(/\/$/, '') + (u.path === '/' ? '' : u.path),
    from: (p: string): URI => URI.from({ scheme: 'vscode-userdata', path: p.replace(/\\/g, '/').slice(userRoot.replace(/\\/g, '/').replace(/\/$/, '').length) || '/' })
  }

  status('Starting VS Code services')
  const dark = isDark(themeVars)
  const constructOptions: IWorkbenchConstructionOptions = {
    enableWorkspaceTrust: false,
    windowIndicator: { label: '$(zap)', tooltip: 'Midum Editor, powered by monaco-vscode-api', command: '' },
    configurationDefaults: {
      'window.title': '${dirty}${activeEditorShort}${separator}${rootName}',
      'workbench.colorTheme': dark ? 'Default Dark Modern' : 'Default Light Modern',
      'workbench.iconTheme': 'vs-seti',
      'workbench.startupEditor': 'none',
      'workbench.activity.showAccounts': false,
      'window.menuBarVisibility': 'compact',
      'editor.inlineSuggest.enabled': S.inlineAI !== false,
      'files.autoSave': S.autoSave ? 'afterDelay' : 'off',
      'editor.fontSize': S.fontSize || 13,
      'terminal.integrated.defaultProfile.linux': 'WSL (Midum)',
      'terminal.integrated.gpuAcceleration': 'off',      // the DOM renderer honours a transparent terminal background (Glass Surfaces)
      'terminal.integrated.minimumContrastRatio': 1,
      'editor.minimap.renderCharacters': false,
      'terminal.integrated.defaultProfile.windows': S.termShell === 'powershell' ? 'PowerShell (Midum)' : 'WSL (Midum)',
      'extensions.autoCheckUpdates': false,
      'extensions.ignoreRecommendations': true,
      'telemetry.telemetryLevel': 'off',
      'update.mode': 'none',
      'git.enabled': false
    },
    productConfiguration: {
      nameShort: 'Midum', nameLong: 'Midum Editor',
      extensionsGallery: {
        serviceUrl: 'https://open-vsx.org/vscode/gallery',
        resourceUrlTemplate: 'https://open-vsx.org/vscode/unpkg/{publisher}/{name}/{version}/{path}',
        extensionUrlTemplate: 'https://open-vsx.org/vscode/gallery/{publisher}/{name}/latest',
        controlUrl: '', nlsBaseUrl: ''
      }
    } as any,
    workspaceProvider: {
      trusted: true,
      workspace: folderUri ? { folderUri } : undefined,
      async open(workspace: any): Promise<boolean> {
        const u = workspace?.folderUri
        if (u && u.scheme === 'file') { const m = /^\/([a-zA-Z]):(.*)$/.exec(u.path); void openFolder(m ? m[1].toUpperCase() + ':' + (m[2] || '/') : u.path); return true }
        return false
      }
    }
  }

  const services = buildServices({ userRoot, userMap, webviewDomain, safe: SAFE, onOpenWorkspace: (p) => { void openFolder(p) } })

  await initialize(services, document.getElementById('workbench')!, constructOptions, { userHome: URI.file(info.home || '/') as any })

  status('Registering Midum features')
  const ext = registerExtension({
    name: 'midum', publisher: 'midum', version: '1.0.0', engines: { vscode: '*' },
    contributes: {
      commands: [
        { command: 'midum.exitIde', title: 'Midum: Back to Simple Editor' },
        { command: 'midum.retryFullMode', title: 'Midum: Retry Full IDE Mode (extension marketplace)' },
        { command: 'midum.toggleAiEditApproval', title: 'Midum: Toggle Approval for AI Edits' },
        { command: 'midum.toggleAiInline', title: 'Midum: Toggle AI Inline Suggestions' },
        { command: 'midum.toggleThemeSync', title: 'Midum: Toggle Midum Theme Sync' },
        { command: 'midum.openArtifact', title: 'Midum: Open Chat Artifact…' },
        { command: 'midum.openFolder', title: 'Midum: Open Folder…' },
        { command: 'midum.openFile', title: 'Midum: Open File…' },
        { command: 'midum.revealInExplorer', title: 'Midum: Reveal in File Explorer' },
        { command: 'midum.runFile', title: 'Midum: Run Current File', icon: '$(play)' },
        { command: 'midum.restartShell', title: 'Midum: Restart Shell Session' },
        { command: 'midum.toggleChat', title: 'Midum: Toggle Chat Sidebar' },
        { command: 'midum.focusChat', title: 'Midum: Focus Chat' }
      ],
      menus: {
        'explorer/context': [{ command: 'midum.revealInExplorer', group: '2_workspace' }],
        'editor/title/run': [{ command: 'midum.runFile', group: 'navigation', when: RUN_WHEN }]
      },
      keybindings: [
        { command: 'midum.openFolder', key: 'ctrl+k ctrl+o' },
        { command: 'midum.runFile', key: 'f5', when: 'editorTextFocus && ' + RUN_WHEN },
        { command: 'midum.runFile', key: 'ctrl+alt+n', when: RUN_WHEN },
        { command: 'midum.toggleChat', key: 'ctrl+alt+m' }
      ]
    }
  } as any, ExtensionHostKind.LocalProcess)
  await ext.setAsDefaultApi()
  void ext

  const artifacts = new ArtifactFileSystemProvider()
  StandaloneServices.get(IFileService).registerProvider(ARTIFACT_SCHEME, artifacts as any)
  registerSearchProviders()
  registerInlineAi()
  await registerMidumCommands()
  try { registerMidumChat() } catch (e) { note('midum chat sidebar: ' + ((e as any)?.message || e)) }   // never block the IDE if the view cannot register
  trackTabs()
  applyTheme(themeVars)
  setThemeHandler((v) => applyTheme(v))
  void termBoot(S.termShell === 'powershell' ? 'powershell' : 'wsl')
  void aiProbe()

  setEventHandler((kind, p) => {
    if (kind === 'editor_open_file' && p?.path) void openFile(p).catch(() => {})
    else if (kind === 'editor_open_artifact' && p?.id) void openArtifactById(p.id)
    else if (kind === 'editor_open_folder' && p?.path) void openFolder(p.path)
    else if (kind === 'editor_ai_request' && p?.id) void handleAiRequest(p)
    else if (kind === 'editor_shell_line' && p?.text !== undefined) termOnLine(p)
    else if (kind === 'editor_shell_lines' && p?.lines) p.lines.forEach(termOnLine)
    else if (kind === 'editor_artifact_changed') void artifacts.refresh()
  })

  setControlHandler(onControl)
  await applyPendingHandoff()
  await whenIdle()                                   // keep the splash until an incoming handoff has opened the tabs
  rpc('editor_ai_hello').catch(() => {})
  persist()
  bootEl.classList.add('hide')
  remeasureEditors()                                 // the editor may have been created while the iframe was hidden: re-measure glyph widths so the caret tracks the text
  postParent({ __ed: 'booted', workspace: currentWorkspace() || ws })
  if (SAFE) {
    let why = ''
    try { why = localStorage.getItem('midum-ide-safe-why') || '' } catch { /* ignore */ }
    void vscode.window.showWarningMessage('The IDE started in safe mode: normal startup did not finish, so marketplace extensions and extension webviews are off. Everything else works.', 'Copy details', 'Retry full mode').then((pick) => {
      if (pick === 'Copy details') void vscode.env.clipboard.writeText(why || 'no details were saved')
      else if (pick === 'Retry full mode') void vscode.commands.executeCommand('midum.retryFullMode')
    })
  }
}

main().catch(fatal)
