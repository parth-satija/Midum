import { SimpleTerminalBackend, SimpleTerminalProcess, type ITerminalChildProcess } from '@codingame/monaco-vscode-terminal-service-override'
import { Emitter } from '@codingame/monaco-vscode-api/vscode/vs/base/common/event'
import { rpc } from './rpc'

/* VS Code terminals that are views onto Midum's two persistent shells (the same ones as Midum's Shell tab and the
   model's execute_wsl_command / execute_terminal_command). Every line either side runs streams in as an
   "editor_shell_line" event; history is replayed when a terminal opens. Input is line-edited here and sent with
   editor_term_run, whose own 'cmd' event is the canonical echo. */

export type ShellKind = 'wsl' | 'powershell'
interface Line { shell: ShellKind; stream: string; text: string; cwd?: string; origin?: string; seq?: number }

const CAP = 1000
const hub = {
  lines: { wsl: [] as Line[], powershell: [] as Line[] },
  seen: { wsl: 0, powershell: 0 } as Record<ShellKind, number>,
  cwd: { wsl: '~', powershell: '~' } as Record<ShellKind, string>,
  ready: { wsl: false, powershell: false } as Record<ShellKind, boolean>,
  buf: { wsl: [] as Line[], powershell: [] as Line[] },
  subs: new Set<(l: Line) => void>(),
  info: [] as { kind: ShellKind; label: string; available: boolean }[],
  defaultShell: 'wsl' as ShellKind
}

function add(l: Line): void {
  const k: ShellKind = l.shell === 'powershell' ? 'powershell' : 'wsl'
  if (typeof l.seq === 'number') { if (l.seq <= hub.seen[k]) return; hub.seen[k] = l.seq }
  const arr = hub.lines[k]; arr.push(l); if (arr.length > CAP) arr.shift()
  if (l.stream === 'meta' && l.cwd) hub.cwd[k] = l.cwd
  hub.subs.forEach((s) => s({ ...l, shell: k }))
}

export function termOnLine(p: any): void {
  const k: ShellKind = p?.shell === 'powershell' ? 'powershell' : 'wsl'
  if (!hub.ready[k]) { hub.buf[k].push(p); return }
  add(p)
}

export async function termBoot(defaultShell: ShellKind): Promise<void> {
  hub.defaultShell = defaultShell
  try {
    const i = await rpc('editor_term_info')
    if (i?.ok) { hub.info = i.shells; i.shells.forEach((s: any) => { if (s.cwd && s.cwd !== '~') hub.cwd[s.kind as ShellKind] = s.cwd }) }
  } catch { /* shells unavailable: terminals still open, just empty */ }
  for (const k of ['wsl', 'powershell'] as ShellKind[]) {
    try { const r = await rpc('editor_term_history', k, 0); if (r?.ok) r.lines.forEach(add) } catch { /* ignore */ }
    hub.ready[k] = true
    hub.buf[k].splice(0).forEach(add)
  }
}

const R = '\x1b[0m'
const dim = (s: string): string => '\x1b[90m' + s + R

function render(l: Line): string {
  const ps = l.shell === 'powershell'
  const text = String(l.text ?? '').replace(/\r?\n/g, '\r\n')
  if (l.stream === 'cmd') {
    const who = l.origin === 'model' ? '\x1b[35m[model]\x1b[0m ' : l.origin === 'editor' ? '\x1b[36m[editor]\x1b[0m ' : ''
    return who + '\x1b[34m' + (ps ? 'PS ' : '') + (l.cwd ?? '') + R + ' ' + (ps ? '>' : '$') + ' ' + text
  }
  if (l.stream === 'meta') return dim(text)
  return text
}

class MidumTerminalProcess extends SimpleTerminalProcess {
  private buf = ''
  private cur = 0
  private hist: string[] = []
  private hidx = -1
  private unsub: (() => void) | null = null

  constructor(id: number, private shell: ShellKind, private emitter: Emitter<string>) {
    super(id, id, hub.cwd[shell], emitter.event)
  }

  private prompt(): string {
    const ps = this.shell === 'powershell'
    return '\x1b[32m' + (ps ? 'PS ' : '') + hub.cwd[this.shell] + R + ' ' + (ps ? '>' : '$') + ' '
  }
  private redraw(): void {
    const tail = this.buf.length - this.cur
    this.emitter.fire('\r\x1b[2K' + this.prompt() + this.buf + (tail > 0 ? `\x1b[${tail}D` : ''))
  }

  async start(): Promise<undefined> {
    // replay history, then follow live lines
    const out = hub.lines[this.shell].map(render).join('\r\n')
    if (out) this.emitter.fire(out + '\r\n')
    const sub = (l: Line): void => {
      if (l.shell !== this.shell) return
      this.emitter.fire('\r\x1b[2K' + render(l) + '\r\n' + this.prompt() + this.buf + (this.buf.length - this.cur > 0 ? `\x1b[${this.buf.length - this.cur}D` : ''))
    }
    hub.subs.add(sub)
    this.unsub = () => hub.subs.delete(sub)
    this.emitter.fire(dim(`Midum ${this.shell === 'powershell' ? 'PowerShell' : 'WSL'} session - shared with the Shell tab and the model`) + '\r\n' + this.prompt())
    return undefined
  }

  override shutdown(): void { this.unsub?.(); this.unsub = null }
  resize(): void { /* line-based shells: nothing to resize */ }
  override clearBuffer(): void { this.emitter.fire('\x1b[2J\x1b[3J\x1b[H' + this.prompt() + this.buf) }
  override sendSignal(): void { /* no process to signal */ }

  override input(data: string): void {
    // paste or fast typing arrives as one chunk: handle escape sequences first, then printable runs
    for (let i = 0; i < data.length; i++) {
      const c = data[i]
      if (c === '\x1b') {
        const seq = data.slice(i, i + 4)
        if (seq.startsWith('\x1b[A')) { this.history(-1); i += 2 }
        else if (seq.startsWith('\x1b[B')) { this.history(1); i += 2 }
        else if (seq.startsWith('\x1b[C')) { if (this.cur < this.buf.length) { this.cur++; this.redraw() } i += 2 }
        else if (seq.startsWith('\x1b[D')) { if (this.cur > 0) { this.cur--; this.redraw() } i += 2 }
        else if (seq.startsWith('\x1b[H') || seq.startsWith('\x1bOH')) { this.cur = 0; this.redraw(); i += 2 }
        else if (seq.startsWith('\x1b[F') || seq.startsWith('\x1bOF')) { this.cur = this.buf.length; this.redraw(); i += 2 }
        else if (seq.startsWith('\x1b[3~')) { if (this.cur < this.buf.length) { this.buf = this.buf.slice(0, this.cur) + this.buf.slice(this.cur + 1); this.redraw() } i += 3 }
        else i = data.length
      } else if (c === '\r' || c === '\n') {
        this.submit()
      } else if (c === '\x7f' || c === '\b') {
        if (this.cur > 0) { this.buf = this.buf.slice(0, this.cur - 1) + this.buf.slice(this.cur); this.cur--; this.redraw() }
      } else if (c === '\x03') {                      // Ctrl+C: drop the line being typed
        this.buf = ''; this.cur = 0; this.emitter.fire('^C\r\n' + this.prompt())
      } else if (c === '\x0c') {                      // Ctrl+L
        this.clearBuffer()
      } else if (c === '\x15') {                      // Ctrl+U
        this.buf = this.buf.slice(this.cur); this.cur = 0; this.redraw()
      } else if (c >= ' ') {
        this.buf = this.buf.slice(0, this.cur) + c + this.buf.slice(this.cur); this.cur++
        if (this.cur === this.buf.length) this.emitter.fire(c); else this.redraw()
      }
    }
  }

  private history(dir: number): void {
    if (!this.hist.length) return
    this.hidx = dir < 0 ? (this.hidx < 0 ? this.hist.length - 1 : Math.max(0, this.hidx - 1)) : (this.hidx < 0 ? -1 : this.hidx + 1)
    if (this.hidx >= this.hist.length || this.hidx < 0) { this.hidx = -1; this.buf = '' } else this.buf = this.hist[this.hidx]
    this.cur = this.buf.length; this.redraw()
  }

  private submit(): void {
    const cmd = this.buf
    this.buf = ''; this.cur = 0; this.hidx = -1
    this.emitter.fire('\r\x1b[2K')                    // the shell's own 'cmd' event echoes the command
    if (!cmd.trim()) { this.emitter.fire(this.prompt()); return }
    this.hist.push(cmd); if (this.hist.length > 200) this.hist.shift()
    rpc('editor_term_run', this.shell, cmd).catch((e) => this.emitter.fire(dim(String(e?.message ?? e)) + '\r\n' + this.prompt()))
  }
}

let pid = 0
export class MidumTerminalBackend extends SimpleTerminalBackend {
  override getDefaultSystemShell = async (): Promise<string> => hub.defaultShell
  override getProfiles = async (): Promise<any[]> => {
    const avail = (k: ShellKind): boolean => hub.info.length === 0 || !!hub.info.find((s) => s.kind === k && s.available)
    return ([
      { profileName: 'WSL (Midum)', path: 'wsl', isDefault: hub.defaultShell === 'wsl', icon: 'terminal-linux' },
      { profileName: 'PowerShell (Midum)', path: 'powershell', isDefault: hub.defaultShell === 'powershell', icon: 'terminal-powershell' }
    ] as any[]).filter((p) => avail(p.path))
  }
  override createProcess = async (config: any): Promise<ITerminalChildProcess> => {
    const exe = String(config?.executable ?? '').toLowerCase()
    const kind: ShellKind = exe.includes('powershell') || exe.includes('pwsh') ? 'powershell' : exe.includes('wsl') || exe.includes('bash') ? 'wsl' : hub.defaultShell
    return new MidumTerminalProcess(++pid, kind, new Emitter<string>())
  }
}
