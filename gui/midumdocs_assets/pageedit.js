"use strict";
/* MidumDocs "edit on the page".
   Click text on a rendered page and it becomes editable right there. When you finish (Enter / click away) the
   word-level difference between the old and new text is mapped back into the LaTeX source, which then recompiles.
   Works for running text (paragraphs, list items, captions). Math, tables and text split by formatting commands
   cannot be mapped safely: the edit is refused with a clear message and an "Ask Midum" shortcut.
   Enter = apply, Shift+Enter = (ignored, one paragraph per edit), Esc = cancel, Alt+click = jump to the source. */
window.MidumPageEdit = (function () {
  let api = null, enabled = true, pages = {}, active = null, pendingRender = false, btn = null, outline = true, calCtx = null;
  const B = String.fromCharCode(92);                       // a single backslash
  const FONT = '"Latin Modern Roman","CMU Serif","Times New Roman",Georgia,serif';

  const st = document.createElement("style");
  st.textContent = `
    .page .pe{position:absolute;z-index:5;box-sizing:border-box;margin:0;padding:0;background:#fff;color:#111;
      outline:2px solid var(--accent,#60a5fa);outline-offset:1px;border-radius:2px;white-space:pre-wrap;
      overflow-wrap:anywhere;cursor:text;caret-color:#111;font-family:${FONT};}
    .page .pe.ghost{outline-color:var(--green,#10b981);}
    #pages.editable .textLayer span:hover{background:color-mix(in srgb,var(--accent,#60a5fa) 16%,transparent);}
    #pages.pe-plain .page .pe{outline:none;}
    #pepop{position:absolute;right:12px;bottom:46px;z-index:12;display:none;flex-direction:column;gap:8px;padding:10px 12px;
      border-radius:10px;background:var(--surface,#161b22);border:1px solid var(--border2,#30363d);font-size:12px;color:var(--text,#e6edf3);
      box-shadow:0 10px 30px -10px rgba(0,0,0,.7);}
    #pepop.show{display:flex;} #pepop label{display:flex;gap:8px;align-items:center;cursor:pointer;white-space:nowrap;}
    #pedit.on{background:var(--accent-faint,#0f1e33);color:var(--accent,#60a5fa);}
  `;
  document.head.appendChild(st);

  // ── text helpers ───────────────────────────────────────────────────────────────────────
  function normWS(t) { return String(t || "").normalize("NFKC").replace(/\s+/g, " ").trim(); }
  function stripPunct(w) { return w.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, ""); }
  function leadP(w) { const m = /^[^\p{L}\p{N}]*/u.exec(w); return m ? m[0] : ""; }
  function trailP(w) { const m = /[^\p{L}\p{N}]*$/u.exec(w); return m ? m[0] : ""; }
  function ctxWord(w) { const c = w ? stripPunct(w) : ""; return c.length >= 3 ? c.slice(0, 24) : ""; }
  function isAlnum(ch) { return !!ch && /[\p{L}\p{N}]/u.test(ch); }

  // A rendered character -> a regex matching the ways it can be written in the source.
  const RX = {
    "\u201C": "(?:``|\u201C)", "\u201D": "(?:''|\u201D)", "\u2018": "(?:`|\u2018)", "\u2019": "(?:'|\u2019)",
    "\u2013": "(?:--|\u2013)", "\u2014": "(?:---|\u2014)"
  };
  RX["&"] = B + B + "?&";
  RX["%"] = B + B + "%";
  RX["$"] = B + B + B + "$";
  RX["#"] = B + B + "#";
  RX["_"] = B + B + "_";
  RX["{"] = B + B + B + "{";
  RX["}"] = B + B + B + "}";
  RX["\u2026"] = "(?:" + B + B + "ldots" + B + "{" + B + "}|" + B + B + "dots" + B + "{" + B + "}|" + B + B + "ldots|\u2026)";
  const META = ".*+?^${}()|[]/";
  function chPat(ch) { if (RX[ch]) return RX[ch]; if (ch === B) return B + B; return META.includes(ch) ? B + ch : ch; }
  function phrasePat(s) { return s.split(" ").map((w) => Array.from(w).map(chPat).join("")).join("\\s+"); }

  // New text -> LaTeX source.
  const TEXMAP = { "&": B + "&", "%": B + "%", "$": B + "$", "#": B + "#", "_": B + "_", "{": B + "{", "}": B + "}",
    "~": B + "textasciitilde{}", "^": B + "textasciicircum{}" };
  TEXMAP[B] = B + "textbackslash{}";
  function texEscape(s) {
    let o = "";
    for (const ch of s) o += TEXMAP[ch] !== undefined ? TEXMAP[ch] : ch;
    return o.replace(/\u201C/g, "``").replace(/\u201D/g, "''").replace(/[\u2018\u2019]/g, "'")
      .replace(/\u2014/g, "---").replace(/\u2013/g, "--").replace(/\u2026/g, B + "ldots{}")
      .replace(/(^|\s)"/g, "$1``").replace(/"/g, "''");
  }

  function findAll(src, text, from) {
    if (!text) return [];
    const ch = Array.from(text);
    const pre = isAlnum(ch[0]) ? "(?<![" + B + B + B + "p{L}" + B + "p{N}])" : "";
    const post = isAlnum(ch[ch.length - 1]) ? "(?![" + B + "p{L}" + B + "p{N}])" : "";
    let re;
    try { re = new RegExp(pre + phrasePat(text) + post, "gu"); } catch (e) { return []; }
    re.lastIndex = from || 0;
    const out = []; let m;
    while ((m = re.exec(src)) && out.length < 300) {
      const ls = src.lastIndexOf("\n", m.index - 1) + 1;
      if (/(^|[^\\])%/.test(src.slice(ls, m.index))) continue;            // inside a % comment
      out.push({ i: m.index, len: m[0].length });
    }
    return out;
  }

  function pickCand(c, src, expect, pw, nw) {
    if (!c.length) return null;
    if (c.length === 1) return c[0];
    const sc = c.map((x) => {
      const bef = src.slice(Math.max(0, x.i - 100), x.i), aft = src.slice(x.i + x.len, x.i + x.len + 100);
      let s = 0; if (pw && bef.includes(pw)) s += 2; if (nw && aft.includes(nw)) s += 2;
      return { x, s, d: Math.abs(x.i - expect) };
    }).sort((p, q) => q.s - p.s || p.d - q.d);
    if (sc[0].s === 0 && sc[1].s === 0 && sc[1].d - sc[0].d < 400) return null;   // ambiguous
    return sc[0].x;
  }

  function diffWords(a, b) {
    const n = a.length, m = b.length, dp = [];
    for (let i = 0; i <= n; i++) dp.push(new Uint16Array(m + 1));
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    const out = []; let i = 0, j = 0, h = null;
    while (i < n || j < m) {
      if (i < n && j < m && a[i] === b[j]) { if (h) { out.push(h); h = null; } i++; j++; }
      else {
        if (!h) h = { i1: i, j1: j, i2: i, j2: j };
        if (j < m && (i >= n || dp[i][j + 1] >= dp[i + 1][j])) { j++; h.j2 = j; } else { i++; h.i2 = i; }
      }
    }
    if (h) out.push(h);
    return out;
  }

  // Map (old rendered text -> new text) to edits on the LaTeX source. Returns {edits} or {error}.
  function mapEdits(oldT, newT, expect) {
    const src = api.cm.getValue(), m0 = /\\begin\{document\}/.exec(src), base = m0 ? m0.index + m0[0].length : 0;
    const a = oldT.split(" ").filter(Boolean), b = newT.split(" ").filter(Boolean), hunks = diffWords(a, b), edits = [];
    const find = (t, pw, nw) => (t ? pickCand(findAll(src, t, base), src, expect, pw, nw) : null);
    const miss = (o) => ({ error: "couldn\u2019t find \u201C" + o.slice(0, 40) + "\u201D in the source (it may be inside math, a table or a formatting command)" });
    const span = (o, n, pw, nw) => {
      const full = find(o, pw, nw);
      if (full) return { edit: { from: full.i, to: full.i + full.len, text: texEscape(n) } };
      const oc = stripPunct(o), nc = stripPunct(n);
      if (oc && leadP(o) === leadP(n) && trailP(o) === trailP(n)) {
        const c = find(oc, pw, nw);
        if (c) return { edit: { from: c.i, to: c.i + c.len, text: texEscape(nc) } };
      }
      return miss(o);
    };
    for (const h of hunks) {
      const oW = a.slice(h.i1, h.i2), nW = b.slice(h.j1, h.j2), pw = ctxWord(a[h.i1 - 1]), nw = ctxWord(a[h.i2]);
      if (oW.length && oW.length === nW.length) {
        for (let k = 0; k < oW.length; k++) {
          if (oW[k] === nW[k]) continue;
          const r = span(oW[k], nW[k], pw, nw); if (r.error) return r; edits.push(r.edit);
        }
      } else if (oW.length && nW.length) {
        const r = span(oW.join(" "), nW.join(" "), pw, nw);
        if (r.error) return { error: r.error + " \u2014 or the change spans formatting" };
        edits.push(r.edit);
      } else if (oW.length) {
        const r = span(oW.join(" "), "", pw, nw); if (r.error) return r;
        const e = r.edit;
        if (src[e.to] === " " || src[e.to] === "\n") e.to++; else if (src[e.from - 1] === " ") e.from--;
        edits.push(e);
      } else {
        const txt = texEscape(nW.join(" "));
        if (h.i1 > 0) {
          const p = a[h.i1 - 1], pc = ctxWord(a[h.i1 - 2]), nc = ctxWord(a[h.i1]);
          let c = find(p, pc, nc), end;
          if (c) end = c.i + c.len;
          else {
            c = find(stripPunct(p), pc, nc);
            if (!c) return miss(p);
            end = c.i + c.len;
            const tp = trailP(p);
            while (src[end] === "}") end++;
            if (tp && src.startsWith(tp, end)) end += tp.length;
            while (src[end] === "}") end++;
          }
          edits.push({ from: end, to: end, text: " " + txt });
        } else if (a.length) {
          const p = a[0], c = find(p, "", ctxWord(a[1])) || find(stripPunct(p), "", ctxWord(a[1]));
          if (!c) return miss(p);
          edits.push({ from: c.i, to: c.i, text: txt + " " });
        } else return { error: "there is no text to anchor the change to" };
      }
    }
    edits.sort((p, q) => p.from - q.from || p.to - q.to);
    for (let i = 1; i < edits.length; i++) if (edits[i].from < edits[i - 1].to) return { error: "two changes overlap" };
    return { edits };
  }

  function applyEdits(edits) {
    const lo = edits[0].from; let hi = 0;
    for (const e of edits) hi = Math.max(hi, e.from + e.text.length);
    const cm = api.cm;
    cm.operation(() => {
      for (let i = edits.length - 1; i >= 0; i--) {
        const e = edits[i];
        cm.replaceRange(e.text, cm.posFromIndex(e.from), cm.posFromIndex(e.to), "+pageedit");
      }
    });
    api.flashLines(lo, hi);
    api.toast("Edited on the page", "ok", { label: "Undo", fn: () => { cm.undo(); api.hideToast(); } }, 6000);
  }

  // ── building the editable block under the click ────────────────────────────────────────
  function segText(parts) {
    let t = "", prev = null;
    for (const s of parts) {
      if (prev && s.x0 - prev.x1 > 0.18 * s.fh && !/\s$/.test(t) && !/^\s/.test(s.s)) t += " ";
      t += s.s; prev = s;
    }
    return t;
  }

  function startEdit(pg, ev) {
    const n = +pg.dataset.n, pt = pages[n];
    if (!pt) return;
    const r = pg.getBoundingClientRect(), px = ev.clientX - r.left, py = ev.clientY - r.top, U = pdfjsLib.Util;
    const its = [];
    for (const it of pt.items) {
      if (typeof it.str !== "string" || !it.str.trim()) continue;
      const t = U.transform(pt.vp.transform, it.transform), fh = Math.hypot(t[2], t[3]);
      if (!(fh > 1)) continue;
      its.push({ s: it.str, x0: t[4], x1: t[4] + it.width * pt.vp.scale, base: t[5], fh });
    }
    let hit = null, bd = 1e9;
    for (const s of its) {
      if (px >= s.x0 - 3 && px <= s.x1 + 3 && py >= s.base - s.fh * 1.05 && py <= s.base + s.fh * 0.4) {
        const d = Math.abs(py - (s.base - s.fh * 0.3));
        if (d < bd) { bd = d; hit = s; }
      }
    }
    if (!hit) return;

    // lines -> segments (runs of text separated by wide gaps, i.e. columns)
    its.sort((p, q) => p.base - q.base || p.x0 - q.x0);
    const lines = [];
    for (const s of its) {
      const L = lines[lines.length - 1];
      if (L && Math.abs(L.base - s.base) <= 0.35 * s.fh) L.items.push(s); else lines.push({ base: s.base, fh: s.fh, items: [s] });
    }
    const segs = [];
    for (const L of lines) {
      L.items.sort((p, q) => p.x0 - q.x0);
      let cur = null;
      for (const s of L.items) {
        if (cur && s.x0 - cur.x1 <= 2.5 * L.fh) { cur.parts.push(s); cur.x1 = Math.max(cur.x1, s.x1); }
        else { cur = { x0: s.x0, x1: s.x1, base: L.base, fh: L.fh, parts: [s] }; segs.push(cur); }
      }
    }
    for (const g of segs) g.text = segText(g.parts);
    const hitSeg = segs.find((g) => g.parts.includes(hit));
    if (!hitSeg) return;
    const ov = (p, q) => (Math.min(p.x1, q.x1) - Math.max(p.x0, q.x0)) / Math.max(1, Math.min(p.x1 - p.x0, q.x1 - q.x0));
    const col = segs.filter((g) => ov(g, hitSeg) > 0.3);
    const colR = Math.max(...col.map((g) => g.x1));
    const same = (g, c) => Math.abs(g.fh - c.fh) < 0.12 * c.fh;

    // a paragraph = neighbouring lines of the same size, evenly spaced, with the usual indent / short-last-line shape
    const block = [hitSeg];
    for (let cur = hitSeg; ;) {
      const cand = col.filter((g) => g.base < cur.base - 0.3 * cur.fh).sort((p, q) => q.base - p.base)[0];
      if (!cand || !same(cand, cur) || cur.base - cand.base > 1.55 * cur.fh) break;
      if (cur.x0 > cand.x0 + 0.8 * cur.fh) break;                 // cur is an indented first line
      if (cand.x1 < colR - 2 * cur.fh) break;                     // cand is the short last line of the previous paragraph
      block.unshift(cand); cur = cand;
    }
    for (let cur = hitSeg; ;) {
      if (cur.x1 < colR - 2 * cur.fh) break;                      // cur is a short last line
      const cand = col.filter((g) => g.base > cur.base + 0.3 * cur.fh).sort((p, q) => p.base - q.base)[0];
      if (!cand || !same(cand, cur) || cand.base - cur.base > 1.55 * cur.fh) break;
      if (cand.x0 > Math.min(...block.map((g) => g.x0)) + 0.8 * cur.fh) break;   // cand starts a new, indented paragraph
      block.push(cand); cur = cand;
    }

    let oldText = "";
    for (const g of block) {
      if (!oldText) oldText = g.text;
      else if (/-$/.test(oldText) && /^[a-z]/.test(g.text)) oldText = oldText.slice(0, -1) + g.text;   // hyphenated line break
      else oldText += " " + g.text;
    }
    oldText = normWS(oldText);
    if (!oldText) return;

    const first = block[0], last = block[block.length - 1];
    const left = Math.min(...block.map((g) => g.x0));
    // The box spans the page's text area (like a word processor), so typing grows the line to the right margin
    // before wrapping. Letter-spacing is calibrated so our font's width matches what TeX actually typeset.
    const colL = Math.min(...col.map((g) => g.x0)), pageW = pg.clientWidth || r.width;
    let right = colR;
    if (colR - colL > 0.5 * pageW) right = Math.max(colR, pageW - colL);       // single column: reach the right margin
    right = Math.min(right, pageW - 3);
    const cal = block.length > 1 ? block[block.length - 1] : block[0];
    calCtx = calCtx || document.createElement("canvas").getContext("2d");
    calCtx.font = block[0].fh + "px " + FONT;
    const spacing = Math.max(-0.06 * block[0].fh, Math.min(0.06 * block[0].fh,
      ((cal.x1 - cal.x0) - calCtx.measureText(cal.text).width) / Math.max(1, cal.text.length)));
    const top = first.base - first.fh * 0.98, bottom = last.base + last.fh * 0.28;
    const el = document.createElement("div");
    el.className = "pe";
    try { el.contentEditable = "plaintext-only"; } catch (e) { /* older engines */ }
    if (el.contentEditable !== "plaintext-only") el.contentEditable = "true";
    el.spellcheck = true;
    el.style.cssText += `left:${left - 2}px;top:${top}px;width:${right - left + 4}px;min-height:${bottom - top}px;` +
      `font-size:${first.fh}px;line-height:${block.length > 1 ? (last.base - first.base) / (block.length - 1) : first.fh * 1.22}px;` +
      `text-indent:${first.x0 - left}px;letter-spacing:${spacing}px;text-align:${block.length > 1 ? "justify" : "left"};text-align-last:left;`;
    el.textContent = oldText;
    pg.appendChild(el);
    active = { el, n, py, pgH: r.height, old: oldText, busy: false };
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); finish(true); }
      else if (e.key === "Enter") e.preventDefault();
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    el.addEventListener("blur", () => finish(true));
    el.addEventListener("paste", (e) => {
      e.preventDefault();
      const t = ((e.clipboardData || window.clipboardData).getData("text") || "").replace(/\s+/g, " ");
      document.execCommand("insertText", false, t);
    });
    el.focus();
    try {
      const rg = document.caretRangeFromPoint(ev.clientX, ev.clientY), sel = window.getSelection();
      if (rg && el.contains(rg.startContainer)) { sel.removeAllRanges(); sel.addRange(rg); }
    } catch (e) { /* caret stays at the start */ }
  }

  function finish(commit) {
    const p = active;
    if (!p || p.busy) return;
    p.busy = true;
    const nt = normWS(p.el.innerText || p.el.textContent);
    let ok = false;
    if (commit && nt && nt !== p.old) {
      const total = Math.max(1, api.pageCount());
      const expect = ((p.n - 1) + p.py / Math.max(1, p.pgH)) / total * api.cm.getValue().length;
      const res = mapEdits(p.old, nt, expect);
      if (res.error) {
        api.toast("Couldn\u2019t place that edit in the source: " + res.error, "err", {
          label: "Ask Midum",
          fn: () => { api.hideToast(); api.askMidum("On the rendered page, change \u201C" + p.old.slice(0, 160) + "\u201D to \u201C" + nt.slice(0, 160) + "\u201D."); }
        }, 10000);
      } else { applyEdits(res.edits); ok = true; }
    }
    active = null;
    if (ok) {                                       // keep the new text visible until the recompiled page replaces it
      p.el.contentEditable = "false"; p.el.classList.add("ghost"); p.el.style.pointerEvents = "none";
    } else p.el.remove();
    if (pendingRender) { pendingRender = false; if (!ok) api.rerender(); }
  }

  // ── public ─────────────────────────────────────────────────────────────────────────────
  function init(a) {
    api = a;
    const pagesEl = api.pagesEl, zoom = document.getElementById("zoom");
    pagesEl.classList.add("editable");
    let cbEdit = null, cbBox = null;
    const savePrefs = () => {
      try { fetch("/prefs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pageEdit: enabled, boxOutline: outline }) }); }
      catch (e) { /* not critical */ }
    };
    const setEnabled = (v, persist) => {
      enabled = !!v;
      if (btn) btn.classList.toggle("on", enabled);
      pagesEl.classList.toggle("editable", enabled);
      if (!enabled) finish(true);
      if (cbEdit) cbEdit.checked = enabled;
      if (persist) savePrefs();
    };
    const setOutline = (v, persist) => {
      outline = !!v;
      pagesEl.classList.toggle("pe-plain", !outline);
      if (cbBox) cbBox.checked = outline;
      if (persist) savePrefs();
    };
    if (zoom) {
      btn = document.createElement("button");
      btn.id = "pedit"; btn.className = "on"; btn.textContent = "\u270E";
      btn.title = "Edit directly on the page: click any text (Alt+click jumps to the source)";
      btn.onclick = () => {
        setEnabled(!enabled, true);
        api.toast(enabled ? "Click any text on the page to edit it" : "Page editing off", "ok", null, 2500);
      };
      const gear = document.createElement("button");
      gear.id = "pesettings"; gear.textContent = "\u2699"; gear.title = "Page editing settings";
      const pop = document.createElement("div");
      pop.id = "pepop";
      pop.innerHTML = '<label><input type="checkbox" id="pe-cb-edit"> Edit directly on the page</label>' +
                      '<label><input type="checkbox" id="pe-cb-box"> Show the editing box outline</label>';
      zoom.parentNode.appendChild(pop);
      cbEdit = pop.querySelector("#pe-cb-edit"); cbBox = pop.querySelector("#pe-cb-box");
      cbEdit.checked = enabled; cbBox.checked = outline;
      cbEdit.onchange = () => setEnabled(cbEdit.checked, true);
      cbBox.onchange = () => setOutline(cbBox.checked, true);
      gear.onclick = (e) => { e.stopPropagation(); pop.classList.toggle("show"); };
      document.addEventListener("click", (e) => { if (!pop.contains(e.target) && e.target !== gear) pop.classList.remove("show"); });
      zoom.insertBefore(gear, zoom.firstChild);
      zoom.insertBefore(btn, zoom.firstChild);
    }
    fetch("/prefs", { cache: "no-store" }).then((r) => r.json()).then((p) => {
      if (!p || typeof p !== "object") return;
      if (p.boxOutline === false) setOutline(false, false);
      if (p.pageEdit === false) setEnabled(false, false);
    }).catch(() => { /* defaults */ });
    pagesEl.addEventListener("click", (ev) => {
      if (!enabled || ev.button !== 0 || ev.altKey || ev.ctrlKey || ev.metaKey || ev.shiftKey) return;
      if (active) return;
      if (api.isReadonly()) return;
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed) return;                        // the user is selecting text, not editing
      const pg = ev.target.closest && ev.target.closest(".page");
      if (pg) startEdit(pg, ev);
    });
  }

  return {
    init,
    setPages(p) { pages = p || {}; },
    isEditing() { return !!active; },
    enabled() { return enabled; },
    defer() { if (active) { pendingRender = true; return true; } return false; }
  };
})();
