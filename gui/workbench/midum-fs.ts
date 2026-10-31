import {
  FileChangeType, FileSystemProviderCapabilities, FileType, FileSystemProviderError, FileSystemProviderErrorCode,
  type IFileChange, type IFileDeleteOptions, type IFileOverwriteOptions, type IFileSystemProviderWithFileReadWriteCapability,
  type IFileWriteOptions, type IStat, type IWatchOptions
} from '@codingame/monaco-vscode-files-service-override'
import { Emitter, Event } from '@codingame/monaco-vscode-api/vscode/vs/base/common/event'
import { Disposable, toDisposable, type IDisposable } from '@codingame/monaco-vscode-api/vscode/vs/base/common/lifecycle'
import type { URI } from '@codingame/monaco-vscode-api/vscode/vs/base/common/uri'
import { ApiError, fsJson, fsRead, fsWrite } from './rpc'
import { host, hostToUri, uriToHost } from './paths'

export interface PathMap { to(uri: URI): string; from(path: string): URI }
export const fileMap: PathMap = { to: uriToHost, from: hostToUri }

const CODES: Record<string, FileSystemProviderErrorCode> = {
  ENOENT: FileSystemProviderErrorCode.FileNotFound,
  EEXIST: FileSystemProviderErrorCode.FileExists,
  ENOTDIR: FileSystemProviderErrorCode.FileNotADirectory,
  EISDIR: FileSystemProviderErrorCode.FileIsADirectory,
  EACCES: FileSystemProviderErrorCode.NoPermissions,
  EPERM: FileSystemProviderErrorCode.NoPermissions,
  ETOOLARGE: FileSystemProviderErrorCode.FileTooLarge
}

function wrap(e: unknown): never {
  if (e instanceof ApiError) throw FileSystemProviderError.create(e.message, CODES[e.code] ?? FileSystemProviderErrorCode.Unknown)
  if (e instanceof FileSystemProviderError) throw e
  throw FileSystemProviderError.create(String((e as any)?.message ?? e), FileSystemProviderErrorCode.Unavailable)
}

interface Watch { id: number; path: string; recursive: boolean; excludes: string[] }

/** `file://` provider backed by the real disk through Midum's /api/fs endpoints. */
export class MidumFileSystemProvider extends Disposable implements IFileSystemProviderWithFileReadWriteCapability {
  readonly capabilities: FileSystemProviderCapabilities
  readonly onDidChangeCapabilities = Event.None
  private readonly _onDidChangeFile = this._register(new Emitter<readonly IFileChange[]>())
  readonly onDidChangeFile = this._onDidChangeFile.event
  private watches = new Map<number, Watch>()
  private nextWatch = 1
  private timer: number | undefined
  private polling = false

  constructor(private readonly map: PathMap = fileMap, private readonly watchable = true) {
    super()
    let caps = FileSystemProviderCapabilities.FileReadWrite | FileSystemProviderCapabilities.FileFolderCopy
    if (host.caseSensitive) caps |= FileSystemProviderCapabilities.PathCaseSensitive
    if (host.trash && map === fileMap) caps |= FileSystemProviderCapabilities.Trash
    this.capabilities = caps
    this._register(toDisposable(() => { if (this.timer) clearInterval(this.timer) }))
  }

  async stat(resource: URI): Promise<IStat> {
    try {
      const r = await fsJson('stat', { path: this.map.to(resource) })
      return { type: r.type, ctime: r.ctime, mtime: r.mtime, size: r.size, permissions: r.readonly ? 1 : undefined } as IStat
    } catch (e) { return wrap(e) }
  }

  async readdir(resource: URI): Promise<[string, FileType][]> {
    try {
      const r = await fsJson('readdir', { path: this.map.to(resource) })
      return r.entries as [string, FileType][]
    } catch (e) { return wrap(e) }
  }

  async readFile(resource: URI): Promise<Uint8Array> {
    try { return (await fsRead(this.map.to(resource))).data } catch (e) { return wrap(e) }
  }

  async writeFile(resource: URI, content: Uint8Array, opts: IFileWriteOptions): Promise<void> {
    try { await fsWrite(this.map.to(resource), content, { create: opts.create, overwrite: opts.overwrite }) } catch (e) { return wrap(e) }
  }

  async mkdir(resource: URI): Promise<void> {
    try { await fsJson('mkdir', { path: this.map.to(resource) }) } catch (e) { return wrap(e) }
  }

  async delete(resource: URI, opts: IFileDeleteOptions): Promise<void> {
    try { await fsJson('delete', { path: this.map.to(resource), recursive: !!opts.recursive, trash: !!opts.useTrash }) } catch (e) { return wrap(e) }
  }

  async rename(from: URI, to: URI, opts: IFileOverwriteOptions): Promise<void> {
    try { await fsJson('rename', { from: this.map.to(from), to: this.map.to(to), overwrite: !!opts.overwrite }) } catch (e) { return wrap(e) }
  }

  async copy(from: URI, to: URI, opts: IFileOverwriteOptions): Promise<void> {
    try { await fsJson('copy', { from: this.map.to(from), to: this.map.to(to), overwrite: !!opts.overwrite }) } catch (e) { return wrap(e) }
  }

  // ── change detection: the server diffs directory snapshots for the watched paths ─────────────
  watch(resource: URI, opts: IWatchOptions): IDisposable {
    if (!this.watchable) return toDisposable(() => {})
    const w: Watch = { id: this.nextWatch++, path: this.map.to(resource), recursive: !!opts.recursive, excludes: [...(opts.excludes ?? [])] }
    if (w.path === '/') return toDisposable(() => {})
    this.watches.set(w.id, w)
    this.ensureTimer()
    return toDisposable(() => { this.watches.delete(w.id) })
  }

  private ensureTimer(): void {
    if (this.timer) return
    this.timer = window.setInterval(() => { void this.poll() }, 2000)
  }

  private async poll(): Promise<void> {
    if (this.polling || this.watches.size === 0 || (window as any).__midumHidden) return
    this.polling = true
    try {
      const r = await fsJson('poll', { watches: [...this.watches.values()].map((w) => ({ id: w.id, path: w.path, recursive: w.recursive })) })
      const changes: IFileChange[] = []
      for (const c of (r.changes || []) as { type: number; path: string }[]) {
        const t = c.type === 1 ? FileChangeType.ADDED : c.type === 2 ? FileChangeType.DELETED : FileChangeType.UPDATED
        changes.push({ type: t, resource: this.map.from(c.path) } as IFileChange)
      }
      if (changes.length) this._onDidChangeFile.fire(changes)
    } catch { /* server busy/offline: try again on the next tick */ }
    finally { this.polling = false }
  }
}
