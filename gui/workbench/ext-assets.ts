/* Marketplace extension assets (icon themes, product icon fonts) are referenced from CSS as
   https://open-vsx.org/vscode/unpkg/<publisher>/<name>/<version>/<path>. The embedded browser does not render those (the CDN's
   headers / hotlink rules), so e.g. Material Icon Theme shows no file icons. Midum's local server exposes the same files under
   /ext/open-vsx.org/... with correct content types and a disk cache; this rewrites the urls in every <style> in <head>
   (the workbench writes its generated icon-theme CSS there, now and whenever the theme changes). */

const RX = /https:\/\/open-vsx\.org\/(vscode\/unpkg\/[^\s)'"\\]+)/g
const HAS = /https:\/\/open-vsx\.org\/vscode\/unpkg\//

function fix(el: HTMLStyleElement): void {
  const t = el.textContent
  if (!t || !HAS.test(t)) return
  el.textContent = t.replace(RX, (_m, p: string) => location.origin + '/ext/open-vsx.org/' + p)
}

function scan(n: Node): void {
  if (n instanceof HTMLStyleElement) { fix(n); return }
  if (n.nodeType === 3 && n.parentElement instanceof HTMLStyleElement) { fix(n.parentElement); return }
  if (n instanceof Element) n.querySelectorAll('style').forEach((s) => fix(s as HTMLStyleElement))
}

export function installExtAssetProxy(): void {
  const start = (): void => {
    document.head.querySelectorAll('style').forEach((s) => fix(s as HTMLStyleElement))
    new MutationObserver((muts) => {
      for (const m of muts) {
        if (m.type === 'characterData') scan(m.target)
        else m.addedNodes.forEach(scan)
      }
    }).observe(document.head, { childList: true, subtree: true, characterData: true })
  }
  if (document.head) start(); else document.addEventListener('DOMContentLoaded', start, { once: true })
}
