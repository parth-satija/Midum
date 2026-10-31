import * as vscode from 'vscode'
import { S, persist } from './ctx'
import { reportDirty } from './rpc'
import { baseName, pkey } from './paths'
import { ARTIFACT_SCHEME } from './artifacts'

/* Mirror of the open editors in the shape the Python side expects in editor_state.json
   (editor_link.guard_external_write and the chat tools read tabs/active/workspace). */

export interface TabInfo { key: string; kind: 'file' | 'artifact'; name: string; path: string; id: string; dirty: boolean; readOnly: boolean; active: boolean; preview: boolean }

export function hostPathOf(uri: vscode.Uri): string {
  const m = /^\/([a-zA-Z]):(.*)$/.exec(uri.path)
  return m ? m[1].toUpperCase() + ':' + (m[2] || '/') : uri.path
}

export function keyOf(uri: vscode.Uri): string | null {
  if (uri.scheme === 'file') return 'file:' + pkey(hostPathOf(uri))
  if (uri.scheme === ARTIFACT_SCHEME) return 'artifact:' + (uri.path.split('/')[1] ?? '')
  return null
}

export function listTabs(): TabInfo[] {
  const out: TabInfo[] = []
  const active = vscode.window.tabGroups.activeTabGroup.activeTab
  for (const g of vscode.window.tabGroups.all) {
    for (const t of g.tabs) {
      const input: any = t.input
      const uri: vscode.Uri | undefined = input?.uri
      if (!uri) continue
      const key = keyOf(uri); if (!key) continue
      if (out.some((x) => x.key === key)) continue
      const isArt = uri.scheme === ARTIFACT_SCHEME
      out.push({
        key, kind: isArt ? 'artifact' : 'file', name: isArt ? uri.path.split('/').pop() || '' : baseName(uri.path),
        path: isArt ? '' : hostPathOf(uri), id: isArt ? uri.path.split('/')[1] ?? '' : '',
        dirty: t.isDirty, readOnly: false, active: t === active, preview: t.isPreview
      })
    }
  }
  return out
}

export function activeKey(): string | null {
  const t: any = vscode.window.tabGroups.activeTabGroup.activeTab
  return t?.input?.uri ? keyOf(t.input.uri) : null
}

export function currentWorkspace(): string {
  const f = vscode.workspace.workspaceFolders?.[0]
  return f && f.uri.scheme === 'file' ? hostPathOf(f.uri) : ''
}

let lastSig = ''
export function syncTabs(): void {
  const tabs = listTabs()
  const sig = JSON.stringify(tabs.map((t) => [t.key, t.active, t.dirty, t.preview]))
  reportDirty(tabs.filter((t) => t.dirty).length)
  if (sig === lastSig) return
  lastSig = sig
  S.tabs = tabs.map((t) => ({ key: t.key, kind: t.kind, name: t.name, path: t.path, id: t.id, preview: t.preview }))
  S.active = tabs.find((t) => t.active)?.key ?? null
  const ws = currentWorkspace(); if (ws) S.workspace = ws
  persist()
}

export function trackTabs(): void {
  vscode.window.tabGroups.onDidChangeTabs(syncTabs)
  vscode.window.tabGroups.onDidChangeTabGroups(syncTabs)
  vscode.workspace.onDidChangeTextDocument(() => syncTabs())
  vscode.workspace.onDidSaveTextDocument(() => syncTabs())
  syncTabs()
}
