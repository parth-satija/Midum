import {
  FileSystemProviderCapabilities, FileType, FileSystemProviderError, FileSystemProviderErrorCode, FileChangeType,
  type IFileChange, type IFileSystemProviderWithFileReadWriteCapability, type IStat, type IWatchOptions, type IFileWriteOptions
} from '@codingame/monaco-vscode-files-service-override'
import { Emitter, Event } from '@codingame/monaco-vscode-api/vscode/vs/base/common/event'
import { Disposable, toDisposable, type IDisposable } from '@codingame/monaco-vscode-api/vscode/vs/base/common/lifecycle'
import { URI } from '@codingame/monaco-vscode-api/vscode/vs/base/common/uri'
import { rpc } from './rpc'

export const ARTIFACT_SCHEME = 'midum-artifact'

interface Art { id: string; name: string; version: number; editable: boolean; content: string }

export function artifactUri(id: string, name: string): URI {
  return URI.from({ scheme: ARTIFACT_SCHEME, path: '/' + id + '/' + name })
}
const idOf = (u: URI): string => u.path.split('/')[1] ?? ''
const enc = new TextEncoder()
const dec = new TextDecoder()

/** Chat artifacts as editable files: midum-artifact:/<id>/<name>. Saving creates a new artifact version (history kept). */
export class ArtifactFileSystemProvider extends Disposable implements IFileSystemProviderWithFileReadWriteCapability {
  readonly capabilities = FileSystemProviderCapabilities.FileReadWrite | FileSystemProviderCapabilities.PathCaseSensitive
  readonly onDidChangeCapabilities = Event.None
  private readonly _onDidChangeFile = this._register(new Emitter<readonly IFileChange[]>())
  readonly onDidChangeFile = this._onDidChangeFile.event
  private cache = new Map<string, Art>()
  private watched = new Map<number, string>()
  private n = 1
  private timer: number | undefined

  private async load(u: URI): Promise<Art> {
    const id = idOf(u)
    const r = await rpc('editor_open_artifact', id)
    if (!r?.ok) throw FileSystemProviderError.create(r?.error || 'artifact not found', FileSystemProviderErrorCode.FileNotFound)
    const a: Art = { id, name: r.name, version: r.version, editable: !!r.editable, content: String(r.content ?? '') }
    this.cache.set(id, a)
    return a
  }

  async stat(u: URI): Promise<IStat> {
    if (u.path === '/' || u.path.split('/').filter(Boolean).length === 1) return { type: FileType.Directory, ctime: 0, mtime: 0, size: 0 }
    const a = await this.load(u)
    return { type: FileType.File, ctime: 0, mtime: a.version, size: enc.encode(a.content).length, permissions: a.editable ? undefined : 1 } as IStat
  }
  async readdir(): Promise<[string, FileType][]> { return [] }
  async readFile(u: URI): Promise<Uint8Array> { return enc.encode((this.cache.get(idOf(u)) ?? (await this.load(u))).content) }
  async writeFile(u: URI, content: Uint8Array, _o: IFileWriteOptions): Promise<void> {
    const id = idOf(u)
    const cached = this.cache.get(id) ?? (await this.load(u))
    if (!cached.editable) throw FileSystemProviderError.create('This artifact is read-only (use Save As)', FileSystemProviderErrorCode.NoPermissions)
    const r = await rpc('editor_save_artifact', id, dec.decode(content), cached.version)
    if (r?.conflict) throw FileSystemProviderError.create('Midum changed this artifact (now v' + r.version + ') while you were editing.', FileSystemProviderErrorCode.FileExists)
    if (!r?.ok) throw FileSystemProviderError.create(r?.error || 'save failed', FileSystemProviderErrorCode.Unavailable)
    cached.version = r.version; cached.content = dec.decode(content)
  }
  async mkdir(): Promise<void> { throw FileSystemProviderError.create('not supported', FileSystemProviderErrorCode.NoPermissions) }
  async delete(): Promise<void> { throw FileSystemProviderError.create('not supported', FileSystemProviderErrorCode.NoPermissions) }
  async rename(): Promise<void> { throw FileSystemProviderError.create('not supported', FileSystemProviderErrorCode.NoPermissions) }

  watch(u: URI, _o: IWatchOptions): IDisposable {
    const w = this.n++
    this.watched.set(w, idOf(u))
    if (!this.timer) this.timer = window.setInterval(() => { void this.refresh() }, 2500)
    return toDisposable(() => { this.watched.delete(w) })
  }

  /** The model edited an artifact (editor_artifact_changed event) or the timer fired: announce changed versions. */
  async refresh(): Promise<void> {
    const ids = [...new Set(this.watched.values())].filter(Boolean)
    if (!ids.length || (window as any).__midumHidden) return
    try {
      const r = await rpc('editor_artifact_stat', ids)
      const changes: IFileChange[] = []
      for (const id of ids) {
        const st = r?.stats?.[id]; const c = this.cache.get(id)
        if (st && c && typeof st.version === 'number' && st.version !== c.version) {
          this.cache.delete(id)                       // force a fresh read; the stat mtime (= version) differs now
          changes.push({ type: FileChangeType.UPDATED, resource: artifactUri(id, c.name) } as IFileChange)
        }
      }
      if (changes.length) this._onDidChangeFile.fire(changes)
    } catch { /* ignore */ }
  }
}
