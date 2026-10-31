import { URI } from '@codingame/monaco-vscode-api/vscode/vs/base/common/uri'

// Host path <-> file:// URI. We never use URI.fsPath: it depends on browser platform sniffing.
export const host = { win: false, sep: '/', caseSensitive: true, trash: false }

export function setHost(h: { win: boolean; sep: string; caseSensitive: boolean; trash?: boolean }): void {
  host.win = h.win; host.sep = h.sep; host.caseSensitive = h.caseSensitive; host.trash = !!h.trash
}

/** file URI -> path string understood by the Python side ("D:/dir/x", "/home/x", or "/" for the drive list). */
export function uriToHost(uri: URI): string {
  let p = uri.path || '/'
  if (host.win) {
    const m = /^\/([a-zA-Z]):(.*)$/.exec(p)
    if (m) return m[1].toUpperCase() + ':' + (m[2] || '/')
    return '/'                                   // virtual root: the list of drives
  }
  return p
}

export function hostToUri(p: string): URI {
  let q = p.replace(/\\/g, '/')
  const m = /^([a-zA-Z]):(.*)$/.exec(q)
  if (m) q = '/' + m[1].toLowerCase() + ':' + (m[2] || '/')
  else if (!q.startsWith('/')) q = '/' + q
  return URI.from({ scheme: 'file', path: q })
}

export function baseName(p: string): string {
  const q = p.replace(/[\\/]+$/, '')
  return q.split(/[\\/]/).pop() || p
}
export function pkey(p: string): string {
  const q = p.replace(/\\/g, '/')
  return /^[a-zA-Z]:/.test(q) ? q.toLowerCase() : q
}
