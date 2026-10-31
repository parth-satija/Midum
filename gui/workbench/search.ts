import { StandaloneServices, ISearchService } from '@codingame/monaco-vscode-api'
import { SearchProviderType, TextSearchMatch, SearchRange, type IFileQuery, type ITextQuery, type ISearchComplete, type ISearchProgressItem } from '@codingame/monaco-vscode-api/vscode/vs/workbench/services/search/common/search'
import type { CancellationToken } from '@codingame/monaco-vscode-api/vscode/vs/base/common/cancellation'
import { fsJson } from './rpc'
import { hostToUri, uriToHost } from './paths'

const complete = (results: ISearchComplete['results'], limitHit = false): ISearchComplete =>
  ({ results, limitHit, messages: [], stats: { fromCache: false, type: 'searchProcess' } as any }) as ISearchComplete

function globList(expr: Record<string, any> | undefined): string[] {
  return Object.entries(expr ?? {}).filter(([, v]) => v === true || (v && typeof v === 'object')).map(([k]) => k)
}

function abortOn(token?: CancellationToken): AbortController {
  const c = new AbortController()
  token?.onCancellationRequested(() => c.abort())
  return c
}

/** Make Ctrl+P (file search) and Ctrl+Shift+F (text search) work over the real workspace on disk. */
export function registerSearchProviders(): void {
  const svc = StandaloneServices.get(ISearchService)

  svc.registerSearchResultProvider('file', SearchProviderType.file, {
    getAIName: async () => undefined,
    clearCache: async () => {},
    textSearch: async () => complete([]),
    fileSearch: async (q: IFileQuery, token?: CancellationToken) => {
      const ctl = abortOn(token)
      try {
        const roots = q.folderQueries.map((f) => ({ path: uriToHost(f.folder), excludes: (f.excludePattern ?? []).flatMap((e) => globList(e.pattern)) }))
        const r = await fsJson('find', {
          roots, pattern: q.filePattern ?? '', exclude: globList(q.excludePattern), include: globList(q.includePattern),
          max: q.maxResults ?? 2000, sortByScore: !!(q as any).sortByScore
        }, ctl.signal)
        return complete((r.files as string[]).map((p) => ({ resource: hostToUri(p) })), !!r.limitHit)
      } catch { return complete([]) }
    }
  })

  svc.registerSearchResultProvider('file', SearchProviderType.text, {
    getAIName: async () => undefined,
    clearCache: async () => {},
    fileSearch: async () => complete([]),
    textSearch: async (q: ITextQuery, onProgress?: (p: ISearchProgressItem) => void, token?: CancellationToken) => {
      const ctl = abortOn(token)
      try {
        const roots = q.folderQueries.map((f) => ({ path: uriToHost(f.folder), excludes: (f.excludePattern ?? []).flatMap((e) => globList(e.pattern)) }))
        const pi = q.contentPattern
        const r = await fsJson('grep', {
          roots, pattern: pi.pattern, regex: !!pi.isRegExp, word: !!pi.isWordMatch, caseSensitive: !!pi.isCaseSensitive,
          exclude: globList(q.excludePattern), include: globList(q.includePattern), max: q.maxResults ?? 5000
        }, ctl.signal)
        const out = (r.files as { path: string; matches: { line: number; start: number; end: number; text: string }[] }[]).map((f) => {
          const results = f.matches.map((m) => new TextSearchMatch(m.text, new SearchRange(m.line - 1, m.start, m.line - 1, m.end) as any, q.previewOptions))
          return { resource: hostToUri(f.path), results }
        })
        out.forEach((m) => onProgress?.(m as any))
        return complete(out as any, !!r.limitHit)
      } catch { return complete([]) }
    }
  })
}
