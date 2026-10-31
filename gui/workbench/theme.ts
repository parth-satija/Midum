import { getService, IConfigurationService } from '@codingame/monaco-vscode-api'
import { ConfigurationTarget } from '@codingame/monaco-vscode-api/vscode/vs/platform/configuration/common/configuration'
import { S } from './ctx'

/* Midum app theme -> VS Code. Midum posts its CSS variables; we derive workbench.colorCustomizations and apply them
   to the in-memory configuration layer, so the user's settings.json is never touched. A theme picked from the
   VS Code theme list still works; "Midum: Toggle Theme Sync" turns the mapping off. */

let probe: HTMLElement | null = null
export function toHex(v: string | undefined): string | undefined {
  if (!v) return undefined
  if (!probe) { probe = document.createElement('div'); probe.style.display = 'none'; document.body.appendChild(probe) }
  probe.style.color = ''; probe.style.color = v
  const c = getComputedStyle(probe).color
  const m = /rgba?\(([^)]+)\)/.exec(c); if (!m) return undefined
  const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number)
  const h = (n: number): string => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, '0')
  return '#' + h(p[0]) + h(p[1]) + h(p[2]) + (p.length > 3 && p[3] < 1 ? h(p[3] * 255) : '')
}

export function isDark(vars: Record<string, string>): boolean {
  const hex = toHex(vars['--bg']) ?? '#1e1e1e'
  const n = parseInt(hex.slice(1, 7), 16)
  const lum = (0.2126 * ((n >> 16) & 255) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255)) / 255
  return lum < 0.5
}

export interface Glass { on: boolean; pct: number }
export function glassOf(vars: Record<string, string>): Glass {
  const p = parseFloat(vars['--mz-glass-pct'])
  return { on: vars['--mz-glass-on'] === '1', pct: Math.max(0, Math.min(100, isNaN(p) ? 55 : p)) }
}
const byte = (pct: number): string => Math.round(Math.max(0, Math.min(100, pct)) / 100 * 255).toString(16).padStart(2, '0')
const alpha = (hex: string | undefined, pct: number): string | undefined => hex ? hex.slice(0, 7) + byte(pct) : undefined

/** Midum CSS variables -> VS Code workbench colors. With Glass Surfaces on, panels are translucent fills (same recipe as
 *  Midum's own panes), the editor surface is fully clear so the blobs / background image show through, and popups keep a
 *  denser fill so their text stays readable. */
export function colorsFrom(vars: Record<string, string>): Record<string, string> {
  const g = (k: string): string | undefined => toHex(vars[k])
  const gl = glassOf(vars)
  const bg = g('--bg'), panelRaw = g('--panel') ?? bg, surfRaw = g('--surface') ?? panelRaw, border = g('--border2') ?? g('--border')
  const accent = g('--accent'), text = g('--text'), sub = g('--subtext'), muted = g('--muted')
  const fill = (h: string | undefined, extra = 0): string | undefined => gl.on ? alpha(h, Math.min(100, gl.pct + extra)) : h
  const panel = fill(panelRaw), surf = fill(surfRaw, 10), pop = gl.on ? alpha(panelRaw, Math.min(85, gl.pct + 15)) : panelRaw
  const clear = '#00000000'
  const ed = gl.on ? clear : bg
  const edge = gl.on ? '#ffffff' + byte(gl.pct * 0.22) : border          // Midum's --glass-edge-dim
  const c: Record<string, string | undefined> = {
    'editor.background': ed, 'editorGutter.background': ed, 'minimap.background': ed, 'breadcrumb.background': ed,
    'editor.foreground': text, 'foreground': text, 'descriptionForeground': sub,
    'editor.lineHighlightBackground': alpha(surfRaw, 70), 'editor.lineHighlightBorder': clear,
    'editorLineNumber.foreground': muted, 'editorLineNumber.activeForeground': sub, 'editorCursor.foreground': text ?? accent,
    'editor.selectionBackground': accent ? accent + '44' : undefined, 'editor.inactiveSelectionBackground': accent ? accent + '22' : undefined,
    'sideBar.background': panel, 'sideBar.foreground': text, 'sideBar.border': edge, 'sideBarSectionHeader.background': surf,
    'activityBar.background': panel, 'activityBar.foreground': text, 'activityBar.inactiveForeground': muted, 'activityBar.border': edge,
    'activityBarBadge.background': accent, 'activityBar.activeBorder': accent,
    'titleBar.activeBackground': panel, 'titleBar.inactiveBackground': panel, 'titleBar.activeForeground': text, 'titleBar.border': edge,
    'editorGroupHeader.tabsBackground': panel, 'tab.activeBackground': gl.on ? alpha(panelRaw, Math.min(100, gl.pct + 10)) : bg,
    'tab.inactiveBackground': clear, 'tab.activeForeground': text, 'tab.inactiveForeground': sub, 'tab.border': edge, 'tab.activeBorderTop': accent,
    'panel.background': panel, 'panel.border': edge, 'panelTitle.activeBorder': accent,
    'terminal.background': gl.on ? clear : bg, 'terminal.foreground': text,
    'statusBar.background': panel, 'statusBar.foreground': sub, 'statusBar.border': edge, 'statusBar.noFolderBackground': panel,
    // the Midum window indicator (a bolt icon): no default blue slab, just an accent-coloured glyph with a soft hover
    'statusBarItem.remoteBackground': clear, 'statusBarItem.remoteForeground': accent, 'statusBarItem.remoteHoverBackground': surf,
    'statusBarItem.hoverBackground': surf,
    'extensionButton.prominentBackground': accent,
    'focusBorder': accent ? accent + '88' : undefined, 'button.background': accent, 'progressBar.background': accent, 'textLink.foreground': accent,
    'list.hoverBackground': surf, 'list.inactiveSelectionBackground': surf, 'list.activeSelectionBackground': surf, 'list.focusBackground': surf,
    'input.background': surf, 'input.border': edge, 'dropdown.background': pop, 'dropdown.border': edge,
    'editorWidget.background': pop, 'editorWidget.border': border, 'editorSuggestWidget.background': pop, 'editorSuggestWidget.border': border,
    'editorHoverWidget.background': pop, 'editorHoverWidget.border': border, 'quickInput.background': pop, 'menu.background': pop,
    'notifications.background': pop, 'peekViewEditor.background': pop, 'peekViewResult.background': pop, 'editorStickyScroll.background': pop,
    'editorGroup.border': edge, 'widget.border': border, 'scrollbarSlider.background': muted ? muted + '55' : undefined,
    'scrollbarSlider.hoverBackground': muted ? muted + '88' : undefined, 'editorOverviewRuler.border': clear
  }
  if (gl.on) Object.assign(c, {
    // everything that would otherwise paint an opaque slab over the blobs / background image
    'editorGroup.emptyBackground': clear, 'editorGroupHeader.noTabsBackground': clear, 'editorOverviewRuler.background': clear,
    'welcomePage.background': clear, 'walkThrough.embeddedEditorBackground': clear, 'settings.headerBackground': clear,
    // both sidebars (primary + secondary) are completely clear: no fill, no sheen; only a hairline edge remains
    'sideBar.background': clear, 'sideBarSectionHeader.background': clear, 'auxiliaryBar.background': clear,
    'sideBarTitle.background': clear, 'panelSectionHeader.background': surf, 'panelStickyScroll.background': pop,
    'sideBarStickyScroll.background': clear, 'tab.unfocusedActiveBackground': clear,
    // the two top bars (title / menu bar and the editor tab bar) are completely clear too; the active tab keeps its accent line
    'titleBar.activeBackground': clear, 'titleBar.inactiveBackground': clear, 'editorGroupHeader.tabsBackground': clear,
    'tab.activeBackground': clear, 'tab.activeModifiedBorder': accent, 'tab.border': clear,
    'tab.unfocusedInactiveBackground': clear, 'tab.hoverBackground': surf, 'tab.unfocusedHoverBackground': surf,
    'editorGroup.dropBackground': accent ? accent + '22' : undefined, 'scrollbar.shadow': clear, 'widget.shadow': '#00000040',
    'banner.background': panel, 'debugToolBar.background': pop, 'notificationCenterHeader.background': pop,
    'notificationToast.border': edge, 'quickInputTitle.background': surf, 'keybindingTable.headerBackground': surf,
    'keybindingTable.rowsBackground': clear, 'settings.textInputBackground': surf, 'settings.numberInputBackground': surf,
    'settings.dropdownBackground': surf, 'settings.checkboxBackground': surf, 'checkbox.background': surf,
    'terminalStickyScroll.background': pop, 'editorStickyScrollHover.background': surf, 'inlineChat.background': pop,
    'editorInlayHint.background': clear, 'editor.wordHighlightBackground': accent ? accent + '22' : undefined,
    'statusBar.debuggingBackground': panel, 'titleBar.border': edge, 'menubar.selectionBackground': surf,
    'toolbar.hoverBackground': surf, 'activityBar.activeBackground': clear, 'sideBar.dropBackground': accent ? accent + '22' : undefined
  })
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(c)) if (v) out[k] = v
  return out
}

let last: Record<string, string> | null = null
export function applyTheme(vars?: Record<string, string>): void {
  if (vars) last = vars
  if (!last) return
  const cfg = getService(IConfigurationService) as any
  void cfg.then?.(() => {})
  const gl = glassOf(last), root = document.documentElement
  Object.entries(last).forEach(([k, v]) => { if (!k.startsWith('--mz-')) root.style.setProperty(k, v) })
  root.style.setProperty('--glass-pct', gl.pct + '%')
  root.classList.toggle('glass-on', gl.on)
  root.classList.toggle('glass-no-sheen', last['--mz-glass-sheen'] === '0')
  const set = (v: any): void => { try { Promise.resolve(cfg).then((s: any) => s.updateValue('workbench.colorCustomizations', v, ConfigurationTarget.MEMORY)) } catch { /* ignore */ } }
  set(S.syncTheme === false ? undefined : colorsFrom(last))
}
