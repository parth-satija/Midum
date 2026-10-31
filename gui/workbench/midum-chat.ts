import * as vscode from 'vscode'
import { registerCustomView, ViewContainerLocation, Parts, setPartVisibility, isPartVisibile } from '@codingame/monaco-vscode-workbench-service-override'
import { postParent } from './rpc'
import { S, persist } from './ctx'

/* Midum chat in the Workbench's secondary (right) sidebar.
   The chat UI itself is the main app's own chat pane (same DOM, same prompt box, tool pills, Markdown + LaTeX, uploads and
   chat storage). It cannot live inside this iframe, so this view is a placeholder "slot": we report its screen rectangle to
   the parent (aux_rect) and the parent docks the real chat pane exactly over it. */

export const CHAT_VIEW_ID = 'midum.chat'
const ICON = 'data:image/svg+xml;utf8,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#000" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg>')

let slot: HTMLElement | null = null
let last = '', raf = 0, dragging = false

function report(): void {
  let rect: { x: number; y: number; w: number; h: number } | null = null
  if (slot && slot.isConnected && slot.getClientRects().length > 0 && !(window as any).__midumHidden) {
    const r = slot.getBoundingClientRect()
    if (r.width > 60 && r.height > 60) rect = { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) }
  }
  const sig = JSON.stringify(rect)
  if (sig === last) return
  last = sig
  postParent({ __ed: 'aux_rect', rect })
}
function schedule(): void { if (!raf) raf = requestAnimationFrame(() => { raf = 0; report() }) }

export function toggleChat(): void {
  if (isPartVisibile(Parts.AUXILIARYBAR_PART)) { setPartVisibility(Parts.AUXILIARYBAR_PART, false); return }
  setPartVisibility(Parts.AUXILIARYBAR_PART, true)
  void Promise.resolve(vscode.commands.executeCommand(CHAT_VIEW_ID + '.focus')).catch(() => {})
  postParent({ __ed: 'chat_focus' })
}

export function registerMidumChat(): void {
  registerCustomView({
    id: CHAT_VIEW_ID, name: 'Midum', location: ViewContainerLocation.AuxiliaryBar, icon: ICON, default: true, canMoveView: false, order: 0,
    renderBody(container: HTMLElement) {
      container.style.height = '100%'; container.style.width = '100%'; container.style.minHeight = '0'
      const el = document.createElement('div')
      el.className = 'midum-chat-slot'
      el.style.cssText = 'flex:1 1 auto;min-width:0;height:100%'
      container.appendChild(el)
      slot = el
      const ro = new ResizeObserver(schedule); ro.observe(el); ro.observe(document.body)
      const iv = window.setInterval(report, 250)                       // catches pure moves (a sidebar resized elsewhere)
      const tick = (): void => { if (!dragging) return; report(); requestAnimationFrame(tick) }
      const down = (): void => { dragging = true; tick() }              // follow sash drags frame by frame
      const up = (): void => { dragging = false; schedule() }
      window.addEventListener('pointerdown', down, true); window.addEventListener('pointerup', up, true)
      window.addEventListener('blur', up); window.addEventListener('resize', schedule)
      schedule()
      return {
        dispose() {
          clearInterval(iv); ro.disconnect()
          window.removeEventListener('pointerdown', down, true); window.removeEventListener('pointerup', up, true)
          window.removeEventListener('blur', up); window.removeEventListener('resize', schedule)
          slot = null; last = ''; postParent({ __ed: 'aux_rect', rect: null })
        }
      }
    }
  })

  vscode.commands.registerCommand('midum.toggleChat', toggleChat)
  vscode.commands.registerCommand('midum.focusChat', () => {
    if (!isPartVisibile(Parts.AUXILIARYBAR_PART)) setPartVisibility(Parts.AUXILIARYBAR_PART, true)
    void Promise.resolve(vscode.commands.executeCommand(CHAT_VIEW_ID + '.focus')).catch(() => {})
    postParent({ __ed: 'chat_focus' })
  })
  const sb = vscode.window.createStatusBarItem('midum.chat', vscode.StatusBarAlignment.Right, 102)
  sb.name = 'Midum chat'; sb.text = '$(comment-discussion) Midum'; sb.tooltip = 'Show / hide the Midum chat sidebar (Ctrl+Alt+M)'; sb.command = 'midum.toggleChat'; sb.show()

  if (!S.chatSidebarShown) {                                            // first run: open the right sidebar once
    S.chatSidebarShown = true; persist()
    setTimeout(() => { try { setPartVisibility(Parts.AUXILIARYBAR_PART, true) } catch { /* layout not ready */ } }, 800)
  }
}
