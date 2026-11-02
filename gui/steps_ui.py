# gui/steps_ui.py -- front-end for step prompting (button + inline editor + result card).
#
# html_block() returns <style> + markup-free <script> that gui/app.py splices in
# right before </body> of the main _HTML page. Everything is self-mounting:
#   * a "list-checks" button is inserted into #input-box (before the attach button)
#   * an inline editor panel is inserted into #input-wrap, directly above the input
#   * window.StepsUI is exposed for the few hooks in sendMessage()/appendRow()/
#     buildStaticRow() (hasPending, defaultPrompt, onSend, card)
#   * window.__midumEvent / renderHistoryWindow / clearChat are wrapped (not
#     replaced) so live updates and chat/tab switches keep the panel in sync.
# Backend: gui/steps_api.py (Api methods) + step_prompting.py (state + tools).

_CSS = r"""
#steps-btn{position:relative;flex:0 0 auto;}
#steps-btn.has-steps{color:var(--accent);}
#steps-btn.open{background:var(--accent);color:#fff;}
#steps-btn .sp-badge{position:absolute;top:-5px;right:-5px;min-width:16px;height:16px;padding:0 4px;border-radius:999px;
  background:var(--accent);color:#fff;font-size:9px;font-weight:700;line-height:16px;text-align:center;pointer-events:none;
  box-shadow:0 0 0 2px var(--panel);}
#steps-btn.open .sp-badge{background:#fff;color:var(--accent);box-shadow:0 0 0 2px var(--accent);}

#steps-panel{position:absolute;bottom:calc(100% + 8px);left:0;right:0;max-height:min(62vh,500px);overflow-y:auto;
  background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:12px;
  box-shadow:0 8px 24px rgba(0,0,0,.35);display:none;z-index:21;font-size:12px;color:var(--text);}
#steps-panel.open{display:block;}
.sp-head{display:flex;align-items:center;gap:8px;margin-bottom:8px;}
.sp-title{display:flex;align-items:center;gap:7px;font-size:13px;font-weight:700;}
.sp-title .ic{width:16px;height:16px;color:var(--accent);}
.sp-prog{margin-left:auto;font-size:10px;font-weight:700;color:var(--accent);background:var(--accent-faint);
  border:1px solid var(--border2);border-radius:999px;padding:2px 9px;}
.sp-prog:empty{display:none;}
.sp-x{margin-left:0;}
.sp-prog:empty + .sp-x{margin-left:auto;}

.sp-banner{display:none;align-items:center;gap:8px;flex-wrap:wrap;justify-content:space-between;
  font-size:11px;color:var(--subtext);background:var(--surface);border:1px solid var(--border2);
  border-radius:10px;padding:7px 10px;margin-bottom:8px;}
.sp-ban-acts{display:flex;gap:6px;flex-wrap:wrap;}

.sp-list{display:flex;flex-direction:column;gap:2px;margin-bottom:8px;max-height:78px;overflow-y:auto;overflow-x:hidden;
  overscroll-behavior:contain;scrollbar-width:thin;scrollbar-color:var(--border2) transparent;scroll-behavior:smooth;}
.sp-list::-webkit-scrollbar{width:6px;}
.sp-list::-webkit-scrollbar-thumb{background:var(--border2);border-radius:999px;}
.sp-list::-webkit-scrollbar-track{background:transparent;}
.sp-empty{color:var(--subtext);font-size:11px;line-height:1.5;padding:8px 6px;}
.sp-row{display:grid;grid-template-columns:20px 16px 1fr auto;align-items:center;column-gap:8px;padding:4px 6px;border-radius:10px;min-height:36px;box-sizing:border-box;flex:0 0 auto;}
.sp-row:hover{background:var(--surface2);}
.sp-box{width:18px;height:18px;border-radius:5px;border:1.5px solid var(--border2);display:flex;align-items:center;
  justify-content:center;color:#fff;box-sizing:border-box;}
.sp-box svg{width:12px;height:12px;display:none;}
.sp-row.done .sp-box{background:var(--accent);border-color:var(--accent);}
.sp-row.done .sp-box svg{display:block;}
.sp-num{font-size:10px;color:var(--muted);font-variant-numeric:tabular-nums;text-align:right;}
.sp-text{background:transparent;border:1px solid transparent;border-radius:8px;color:var(--text);font:inherit;
  font-size:12px;padding:4px 6px;width:100%;box-sizing:border-box;outline:none;min-width:0;}
.sp-text:focus{border-color:var(--accent);background:var(--surface);}
.sp-text:disabled{opacity:1;cursor:default;-webkit-text-fill-color:currentColor;}
.sp-row.done .sp-text{color:var(--subtext);}
.sp-acts{display:flex;gap:2px;opacity:0;transition:opacity .12s;}
.sp-row:hover .sp-acts,.sp-row:focus-within .sp-acts{opacity:1;}
.sp-acts button{width:22px;height:22px;border-radius:7px;border:none;background:transparent;color:var(--subtext);
  cursor:pointer;font-size:12px;line-height:1;padding:0;}
.sp-acts button:hover:not(:disabled){background:var(--border2);color:var(--text);}
.sp-acts button:disabled{opacity:.25;cursor:default;}
.sp-row.done .sp-acts{display:none;}
.sp-proof{grid-column:3 / -1;font-size:10.5px;line-height:1.45;color:var(--subtext);padding:0 6px 3px;}
.sp-proof b{color:var(--green);font-weight:700;}

.sp-add{display:flex;gap:6px;margin-bottom:10px;}
.sp-add input,.sp-tpl input,.sp-tpl select{flex:1 1 auto;min-width:0;height:28px;box-sizing:border-box;border-radius:10px;
  border:1px solid var(--border2);background:var(--surface);color:var(--text);font:inherit;font-size:12px;padding:0 10px;outline:none;}
.sp-add input:focus,.sp-tpl input:focus,.sp-tpl select:focus{border-color:var(--accent);}
.sp-btn{flex:0 0 auto;height:28px;padding:0 12px;border-radius:999px;border:1px solid var(--border2);background:var(--surface2);
  color:var(--text);font-size:11px;font-weight:600;cursor:pointer;}
.sp-btn:hover:not(:disabled){border-color:var(--accent);}
.sp-btn:disabled{opacity:.5;cursor:default;}
.sp-primary{background:var(--accent);border-color:var(--accent);color:#fff;}
.sp-danger:hover:not(:disabled){border-color:var(--red);color:var(--red);}
.sp-danger.armed{background:var(--red);border-color:var(--red);color:#fff;}

.sp-opts{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;
  padding:8px 2px;border-top:1px solid var(--border);}
.sp-switch{display:flex;align-items:center;gap:8px;cursor:pointer;font-weight:600;font-size:12px;}
.sp-switch input{position:absolute;opacity:0;pointer-events:none;}
.sp-track{width:30px;height:17px;border-radius:999px;background:var(--border2);position:relative;transition:background .15s;flex:0 0 auto;}
.sp-track::after{content:"";position:absolute;top:2px;left:2px;width:13px;height:13px;border-radius:50%;background:#fff;transition:transform .15s;}
.sp-switch input:checked + .sp-track{background:var(--accent);}
.sp-switch input:checked + .sp-track::after{transform:translateX(13px);}
.sp-switch input:focus-visible + .sp-track{box-shadow:0 0 0 2px var(--accent-faint),0 0 0 3px var(--accent);}
.sp-per{display:flex;align-items:center;gap:6px;font-weight:600;font-size:12px;}
.sp-per button{width:24px;height:24px;border-radius:8px;border:1px solid var(--border2);background:var(--surface2);
  color:var(--text);cursor:pointer;font-size:13px;line-height:1;padding:0;}
.sp-per button:hover{border-color:var(--accent);}
#sp-per-val{min-width:28px;text-align:center;font-variant-numeric:tabular-nums;}
.sp-hint{font-size:10.5px;line-height:1.5;color:var(--subtext);padding:0 2px 8px;}
.sp-hint.err{color:var(--red);}
.sp-hint.ok{color:var(--green);}
.sp-tpl{display:flex;gap:6px;flex-wrap:wrap;padding-top:8px;border-top:1px solid var(--border);}
.sp-tpl .sp-tpl-row{display:flex;gap:6px;flex:1 1 100%;}
#steps-panel.sp-busy .sp-list,#steps-panel.sp-busy .sp-add,#steps-panel.sp-busy .sp-tpl{opacity:.55;pointer-events:none;}

/* end-of-turn checklist card */
.row.steps-row{align-items:flex-start;}
.stepcard{width:min(100%,560px);box-sizing:border-box;border:1px solid var(--border2);background:var(--surface);
  border-radius:16px;padding:12px 14px;font-size:var(--cfs-md,13px);}
.stepcard-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;font-weight:700;}
.stepcard-head .ic{width:16px;height:16px;color:var(--accent);}
.sc-chip{font-size:10px;font-weight:700;border-radius:999px;padding:2px 9px;border:1px solid var(--border2);color:var(--subtext);}
.sc-chip.ok{color:var(--green);border-color:var(--green);}
.sc-chip.warn{color:var(--yellow);border-color:var(--yellow);}
.stepcard ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px;}
.stepcard li{display:grid;grid-template-columns:20px 1fr;column-gap:9px;align-items:start;}
.stepcard .sc-box{width:18px;height:18px;border-radius:5px;border:1.5px solid var(--border2);box-sizing:border-box;
  display:flex;align-items:center;justify-content:center;color:#fff;margin-top:1px;}
.stepcard .sc-box svg{width:12px;height:12px;display:none;}
.stepcard li.done .sc-box{background:var(--accent);border-color:var(--accent);}
.stepcard li.done .sc-box svg{display:block;}
.stepcard li.earlier .sc-box{opacity:.55;}
.stepcard li.earlier .sc-text{color:var(--subtext);}
.stepcard li.pending .sc-text{color:var(--text);}
.sc-text{line-height:1.45;word-break:break-word;}
.sc-num{color:var(--muted);font-variant-numeric:tabular-nums;margin-right:5px;}
.sc-proof{grid-column:2;margin-top:2px;font-size:11.5px;line-height:1.45;color:var(--subtext);
  border-left:2px solid var(--green);padding-left:8px;}
.sc-proof b{color:var(--green);font-weight:700;}
.stepcard-foot{margin-top:10px;display:none;}
.stepcard.is-latest .stepcard-foot{display:flex;}
/* "Step N of M done" pill: reuses the interjection bar look (see .row.interjection in app.py), check mark instead of the arrow */
.row.interjection.stepdone .bubble::before{content:"\2713";color:var(--green);font-weight:800;}
.row.interjection.stepdone .bubble{font-weight:600;}
"""

_JS = r"""
(function(){
  "use strict";
  var DEFAULT_PROMPT = "Work through the checklist steps.";
  var CONT_PROMPT = "Continue with the remaining steps.";
  var MAXS = 60;
  var ICON_LIST = '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8M13 12h8M13 18h8"/></svg>';
  var ICON_CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7"/></svg>';

  var S = {steps:[], verify:false, per_turn:0, phase:"none", done:0, total:0, locked:false, source:"user"};
  var TPL = [];
  var panel = null, btn = null, badge = null, listEl = null;
  var _open = false, _chain = Promise.resolve(), _flashT = null, _delArmT = null;

  function $(id){ return document.getElementById(id); }
  function esc(s){
    return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }
  function busy(){ try { return !!state.thinking; } catch(e){ return false; } }
  function pending(){ return Math.max(0, S.total - S.done); }
  function callApi(name){
    var args = Array.prototype.slice.call(arguments, 1);
    return window.pywebview.api[name].apply(null, args);
  }

  // ── backend calls (serialised so rapid edits can't interleave) ────────────
  function call(name){
    var args = Array.prototype.slice.call(arguments, 1);
    _chain = _chain.then(function(){ return callApi.apply(null, [name].concat(args)); })
      .then(handle)
      .catch(function(e){ console.error("[steps]", e); flash("Couldn't reach Midum.", "err"); });
    return _chain;
  }
  function handle(res){
    if (!res) return;
    if (res.ok === false){ flash(res.error || "Couldn't update the steps.", "err"); refresh(); return; }
    if (res.templates) TPL = res.templates;
    if (res.state) applyState(res.state);
    else render();
  }
  function push(rows, verify, per){
    return call("steps_update",
      rows === undefined ? null : rows,
      verify === undefined ? null : verify,
      per === undefined ? null : per);
  }
  function refresh(){
    return Promise.resolve().then(function(){ return callApi("steps_get_state"); }).then(function(res){
      if (res && res.ok){ S = res.state; TPL = res.templates || TPL; render(); syncCards(); }
    });
  }
  function applyState(st){ S = st; render(); syncCards(); }

  // ── panel construction ────────────────────────────────────────────────────
  function mount(){
    var wrap = $("input-wrap"), box = $("input-box"), attach = $("attach-btn");
    if (!wrap || !box || !attach || $("steps-panel")) return !!$("steps-panel");

    btn = document.createElement("button");
    btn.className = "icon-btn"; btn.id = "steps-btn"; btn.title = "Step prompting";
    btn.setAttribute("aria-label", "Step prompting");
    btn.innerHTML = ICON_LIST + '<span class="sp-badge" style="display:none"></span>';
    badge = btn.querySelector(".sp-badge");
    attach.parentNode.insertBefore(btn, attach);

    panel = document.createElement("div");
    panel.id = "steps-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Step prompting");
    panel.innerHTML =
      '<div class="sp-head">' +
        '<div class="sp-title">' + ICON_LIST + '<span>Step prompting</span></div>' +
        '<span class="sp-prog" id="sp-prog"></span>' +
        '<button class="icon-btn sp-x" id="sp-close" title="Close" aria-label="Close">&times;</button>' +
      '</div>' +
      '<div class="sp-banner" id="sp-banner"></div>' +
      '<div class="sp-list" id="sp-list"></div>' +
      '<div class="sp-add">' +
        '<input id="sp-add-input" type="text" placeholder="Add a step\u2026 (Enter to add)" autocomplete="off">' +
        '<button class="sp-btn" id="sp-add-btn">Add</button>' +
      '</div>' +
      '<div class="sp-opts">' +
        '<label class="sp-switch"><input type="checkbox" id="sp-verify"><span class="sp-track"></span><span>Verify each step</span></label>' +
        '<div class="sp-per"><span>Steps per turn</span>' +
          '<button id="sp-per-dec" title="Fewer steps per turn" aria-label="Fewer steps per turn">&minus;</button>' +
          '<span id="sp-per-val">All</span>' +
          '<button id="sp-per-inc" title="More steps per turn" aria-label="More steps per turn">+</button></div>' +
      '</div>' +
      '<div class="sp-hint" id="sp-hint"></div>' +
      '<div class="sp-tpl">' +
        '<div class="sp-tpl-row"><select id="sp-tpl-sel" aria-label="Saved step lists"></select>' +
          '<button class="sp-btn" id="sp-tpl-load">Load</button>' +
          '<button class="sp-btn sp-danger" id="sp-tpl-del" title="Delete the selected saved list">Delete</button></div>' +
        '<div class="sp-tpl-row"><input id="sp-tpl-name" type="text" placeholder="Name this list to save it\u2026" autocomplete="off">' +
          '<button class="sp-btn" id="sp-tpl-save">Save list</button></div>' +
      '</div>';
    wrap.insertBefore(panel, box);
    listEl = $("sp-list");
    wire();
    render();
    return true;
  }

  function wire(){
    btn.addEventListener("click", function(e){ e.stopPropagation(); toggle(); });
    $("sp-close").addEventListener("click", close);
    document.addEventListener("keydown", function(e){ if (e.key === "Escape" && _open) close(); });

    // list: delegated events
    listEl.addEventListener("change", function(e){
      if (e.target.classList.contains("sp-text")) push(collectPending());
    });
    listEl.addEventListener("keydown", function(e){
      if (e.target.classList.contains("sp-text") && e.key === "Enter"){ e.preventDefault(); e.target.blur(); }
    });
    listEl.addEventListener("click", function(e){
      var b = e.target.closest("button[data-act]");
      if (!b || b.disabled) return;
      var row = b.closest(".sp-row"); if (!row) return;
      var id = row.dataset.id, act = b.dataset.act;
      if (act === "del") push(collectPending().filter(function(r){ return r.id !== id; }));
      else if (act === "up") move(id, -1);
      else if (act === "down") move(id, 1);
    });

    // add
    var addIn = $("sp-add-input");
    addIn.addEventListener("keydown", function(e){
      if (e.key === "Enter"){ e.preventDefault(); doAdd(); }
    });
    addIn.addEventListener("paste", function(e){
      var t = (e.clipboardData || window.clipboardData).getData("text") || "";
      if (/[\r\n]/.test(t)){ e.preventDefault(); addSteps(t.split(/\r?\n/)); addIn.value = ""; }
    });
    $("sp-add-btn").addEventListener("click", doAdd);

    // options
    $("sp-verify").addEventListener("change", function(e){
      S.verify = e.target.checked; renderHint(); push(undefined, e.target.checked, undefined);
    });
    $("sp-per-dec").addEventListener("click", function(){ stepPer(-1); });
    $("sp-per-inc").addEventListener("click", function(){ stepPer(1); });

    // templates
    $("sp-tpl-load").addEventListener("click", function(){
      var id = $("sp-tpl-sel").value;
      if (!id){ flash("Pick a saved list first.", "err"); return; }
      call("steps_template_apply", id);
    });
    $("sp-tpl-del").addEventListener("click", function(){
      var id = $("sp-tpl-sel").value, b = $("sp-tpl-del");
      if (!id){ flash("Pick a saved list first.", "err"); return; }
      if (!b.classList.contains("armed")){
        b.classList.add("armed"); b.textContent = "Sure?";
        clearTimeout(_delArmT);
        _delArmT = setTimeout(disarmDelete, 3000);
        return;
      }
      disarmDelete();
      call("steps_template_delete", id).then(function(){ flash("Saved list deleted.", "ok"); });
    });
    $("sp-tpl-save").addEventListener("click", saveTemplate);
    $("sp-tpl-name").addEventListener("keydown", function(e){
      if (e.key === "Enter"){ e.preventDefault(); saveTemplate(); }
    });

    // banner actions (delegated; banner is re-rendered)
    $("sp-banner").addEventListener("click", function(e){
      var b = e.target.closest("button[data-act]"); if (!b || b.disabled) return;
      var act = b.dataset.act;
      if (act === "continue") continueRun();
      else if (act === "reset") call("steps_reset_progress");
      else if (act === "clear") call("steps_clear");
    });
  }

  function disarmDelete(){
    var b = $("sp-tpl-del"); if (!b) return;
    b.classList.remove("armed"); b.textContent = "Delete"; clearTimeout(_delArmT);
  }

  function collectPending(){
    return Array.prototype.map.call(listEl.querySelectorAll('.sp-row[data-done="0"]'), function(r){
      return {id: r.dataset.id, text: r.querySelector(".sp-text").value};
    });
  }
  function move(id, dir){
    var rows = collectPending();
    var i = rows.findIndex(function(r){ return r.id === id; }), j = i + dir;
    if (i < 0 || j < 0 || j >= rows.length) return;
    var t = rows[i]; rows[i] = rows[j]; rows[j] = t;
    push(rows);
  }
  function addSteps(texts){
    var add = (texts || []).map(function(t){
      return String(t).replace(/^\s*(?:[-*\u2022]|\d+[.)])\s*/, "").trim();
    }).filter(Boolean);
    if (!add.length) return;
    if (pending() + add.length > MAXS){ flash("Too many steps (max " + MAXS + ").", "err"); return; }
    push(collectPending().concat(add.map(function(t){ return {text: t}; })));
  }
  function doAdd(){
    var inp = $("sp-add-input"), v = inp.value;
    if (!v.trim()) return;
    inp.value = "";
    addSteps([v]);
    inp.focus();
  }
  function stepPer(d){
    var v = Math.max(0, Math.min(MAXS, (S.per_turn | 0) + d));
    if (v === (S.per_turn | 0)) return;
    S.per_turn = v; renderOptions(); renderHint();
    push(undefined, undefined, v);
  }
  function saveTemplate(){
    var nameEl = $("sp-tpl-name"), name = nameEl.value.trim();
    var rows = Array.prototype.map.call(listEl.querySelectorAll(".sp-text"), function(i){ return {text: i.value}; });
    if (!rows.length){ flash("Add at least one step before saving.", "err"); return; }
    if (!name){ flash("Give the list a name first.", "err"); nameEl.focus(); return; }
    Promise.resolve().then(function(){ return callApi("steps_template_save", name, rows, !!S.verify, S.per_turn | 0); }).then(function(res){
      if (res && res.ok){
        TPL = res.templates || TPL; nameEl.value = ""; renderTemplates(res.id);
        flash(res.overwritten ? "Updated \u201c" + name + "\u201d." : "Saved \u201c" + name + "\u201d.", "ok");
      } else flash((res && res.error) || "Couldn't save.", "err");
    }).catch(function(){ flash("Couldn't reach Midum.", "err"); });
  }

  // ── rendering ─────────────────────────────────────────────────────────────
  function render(){
    if (!panel) return;
    var b = busy(), pend = pending();
    panel.classList.toggle("sp-busy", b);
    $("sp-prog").textContent = S.total ? (S.done + "/" + S.total + " done") : "";

    var ban = "", acts = "";
    if (S.phase === "running" && pend > 0 && b){
      ban = "Midum is working through the checklist\u2026";
    } else if (S.phase === "running" && pend > 0){
      ban = "Paused \u2014 " + pend + " step" + (pend === 1 ? "" : "s") + " left. Continue, or send a message to resume.";
      acts = '<button class="sp-btn sp-primary" data-act="continue">Continue</button>' +
             '<button class="sp-btn" data-act="reset">Reset</button>' +
             '<button class="sp-btn sp-danger" data-act="clear">Clear</button>';
    } else if (S.phase === "complete"){
      ban = "All steps complete.";
      acts = '<button class="sp-btn" data-act="reset">Run again</button>' +
             '<button class="sp-btn sp-danger" data-act="clear">Clear</button>';
    } else if (S.phase === "staged"){
      ban = "These steps go out with your next message.";
      acts = '<button class="sp-btn sp-danger" data-act="clear">Clear</button>';
    }
    var banEl = $("sp-banner");
    banEl.innerHTML = ban ? '<span>' + esc(ban) + '</span><span class="sp-ban-acts">' + acts + '</span>' : "";
    banEl.style.display = ban ? "flex" : "none";

    // list -- keep focus/caret if the user is mid-edit in a row
    var focusId = null, caret = 0, ae = document.activeElement;
    if (ae && ae.classList && ae.classList.contains("sp-text") && listEl.contains(ae)){
      focusId = ae.closest(".sp-row").dataset.id; caret = ae.selectionStart || 0;
    }
    var prevTop = listEl.scrollTop, prevN = listEl.querySelectorAll(".sp-row").length;
    if (!S.steps.length){
      listEl.innerHTML = '<div class="sp-empty">No steps yet. Add the steps you want Midum to follow, in order \u2014 ' +
        'each one becomes a checkbox it has to tick off.</div>';
    } else {
      var firstPend = -1, lastPend = -1;
      S.steps.forEach(function(s, i){ if (!s.done){ if (firstPend < 0) firstPend = i; lastPend = i; } });
      listEl.innerHTML = S.steps.map(function(s, i){
        var proof = (S.verify && s.done && s.proof)
          ? '<div class="sp-proof"><b>Proof:</b> ' + esc(s.proof) + '</div>' : "";
        return '<div class="sp-row' + (s.done ? ' done' : '') + '" data-id="' + esc(s.id) + '" data-done="' + (s.done ? 1 : 0) + '">' +
          '<span class="sp-box">' + ICON_CHECK + '</span>' +
          '<span class="sp-num">' + (i + 1) + '</span>' +
          '<input class="sp-text" type="text" value="' + esc(s.text) + '" maxlength="400"' +
            ((s.done || b) ? ' disabled' : '') + ' aria-label="Step ' + (i + 1) + '">' +
          '<span class="sp-acts">' +
            '<button data-act="up" title="Move up" aria-label="Move up"' + (i <= firstPend ? ' disabled' : '') + '>\u2191</button>' +
            '<button data-act="down" title="Move down" aria-label="Move down"' + (i >= lastPend ? ' disabled' : '') + '>\u2193</button>' +
            '<button data-act="del" title="Remove step" aria-label="Remove step">\u2715</button>' +
          '</span>' + proof + '</div>';
      }).join("");
      if (focusId){
        var el = listEl.querySelector('.sp-row[data-id="' + focusId + '"] .sp-text');
        if (el && !el.disabled){ el.focus(); try { el.setSelectionRange(caret, caret); } catch(e){} }
      }
    }
    // Only ~2 rows are visible: keep the scroll position, jump to the end when a step was added,
    // and while a run is in progress bring the next unchecked step into view.
    if (S.steps.length > prevN && prevN > 0) listEl.scrollTop = listEl.scrollHeight;
    else listEl.scrollTop = prevTop;
    if (!focusId && S.phase === "running"){
      var np = listEl.querySelector('.sp-row[data-done="0"]');
      if (np) listEl.scrollTop += np.getBoundingClientRect().top - listEl.getBoundingClientRect().top;
    }
    renderOptions(); renderHint(); renderTemplates(); updateBtn();
    var dis = b || false;
    $("sp-add-input").disabled = dis; $("sp-add-btn").disabled = dis;
  }
  function renderOptions(){
    $("sp-verify").checked = !!S.verify;
    $("sp-per-val").textContent = S.per_turn ? String(S.per_turn) : "All";
  }
  function renderHint(){
    var h = $("sp-hint");
    if (h.dataset.flash) return;
    h.className = "sp-hint";
    var t = S.per_turn
      ? "Midum does at most " + S.per_turn + " step" + (S.per_turn === 1 ? "" : "s") + " per turn, then pauses. " +
        "Hit Continue after your quota refreshes to run the next batch."
      : "Midum does every step in a single turn.";
    if (S.verify) t += " Each step needs a short proof before it can be checked off.";
    h.textContent = t;
  }
  function renderTemplates(selectId){
    var sel = $("sp-tpl-sel"), cur = selectId || sel.value;
    sel.innerHTML = '<option value="">' + (TPL.length ? "Saved step lists\u2026" : "No saved lists yet") + '</option>' +
      TPL.map(function(t){
        return '<option value="' + esc(t.id) + '">' + esc(t.name) + ' (' + t.steps.length + ')</option>';
      }).join("");
    if (cur && TPL.some(function(t){ return t.id === cur; })) sel.value = cur;
  }
  function updateBtn(){
    if (!btn) return;
    var has = S.total > 0;
    btn.classList.toggle("has-steps", has);
    badge.style.display = has ? "" : "none";
    badge.textContent = has ? (S.phase === "staged" ? String(S.total) : (S.done + "/" + S.total)) : "";
    btn.title = has ? ("Step prompting \u2014 " + S.done + "/" + S.total + " done") : "Step prompting";
  }
  function flash(msg, level){
    var h = $("sp-hint"); if (!h) return;
    clearTimeout(_flashT);
    h.className = "sp-hint" + (level ? " " + level : ""); h.textContent = msg; h.dataset.flash = "1";
    _flashT = setTimeout(function(){ delete h.dataset.flash; renderHint(); }, 3200);
  }

  function open(){
    if (!panel) return;
    _open = true; panel.classList.add("open"); btn.classList.add("open");
    refresh().then(function(){
      if (!S.steps.length && !busy()) $("sp-add-input").focus();
    }).catch(function(){});
  }
  function close(){
    if (!panel) return;
    _open = false; panel.classList.remove("open"); btn.classList.remove("open"); disarmDelete();
  }
  function toggle(){ _open ? close() : open(); }

  function continueRun(){
    if (busy() || pending() === 0) return;
    var input = $("msg-input"); if (!input) return;
    input.value = CONT_PROMPT;
    try { autosizeMsgInput(); } catch(e){}
    close();
    sendMessage();
  }

  // ── end-of-turn checklist card (live + replayed from the saved chat) ──────
  function card(text){
    var d;
    try { d = JSON.parse(text); } catch(e){ d = null; }
    var el = document.createElement("div");
    el.className = "stepcard";
    if (!d || !d.steps){ el.textContent = "Checklist"; return el; }
    var left = d.remaining | 0;
    el.dataset.remaining = String(left);
    var chips = '<span class="sc-chip ' + (left ? '' : 'ok') + '">' + d.done + '/' + d.total + ' done</span>';
    if (d.completed_this_turn) chips += '<span class="sc-chip">+' + d.completed_this_turn + ' this turn</span>';
    if (left) chips += '<span class="sc-chip warn">' + left + ' left</span>';
    else chips += '<span class="sc-chip ok">complete</span>';
    var items = d.steps.map(function(s){
      var cls = s.done ? ("done" + (s.this_turn ? "" : " earlier")) : "pending";
      var proof = (d.verify && s.done && s.proof) ? '<div class="sc-proof"><b>Proof:</b> ' + esc(s.proof) + '</div>' : "";
      return '<li class="' + cls + '"><span class="sc-box">' + ICON_CHECK + '</span>' +
        '<span class="sc-text"><span class="sc-num">' + s.n + '.</span>' + esc(s.text) + '</span>' + proof + '</li>';
    }).join("");
    el.innerHTML = '<div class="stepcard-head">' + ICON_LIST + '<span>Checklist</span>' + chips + '</div>' +
      '<ul>' + items + '</ul>' +
      (left ? '<div class="stepcard-foot"><button class="sp-btn sp-primary stepcard-continue">Continue with remaining steps</button></div>' : '');
    var cb = el.querySelector(".stepcard-continue");
    if (cb) cb.addEventListener("click", continueRun);
    setTimeout(syncCards, 0);
    return el;
  }
  function syncCards(){
    var cards = document.querySelectorAll(".stepcard");
    Array.prototype.forEach.call(cards, function(c){ c.classList.remove("is-latest"); });
    var last = cards[cards.length - 1];
    if (last && !busy() && pending() > 0 && Number(last.dataset.remaining) > 0) last.classList.add("is-latest");
  }

  // ── hooks used by sendMessage() ───────────────────────────────────────────
  window.StepsUI = {
    hasPending: function(){ return pending() > 0 && !busy(); },
    defaultPrompt: function(){ return DEFAULT_PROMPT; },
    onSend: function(){
      close();
      S.phase = pending() > 0 ? "running" : S.phase;
      render();
      setTimeout(function(){ refresh().catch(function(){}); }, 400);
    },
    card: card,
    refresh: function(){ return refresh().catch(function(){}); }
  };

  // ── wrap (never replace) the app's event bridge and chat-switch renderers ──
  var prevEvt = window.__midumEvent;
  window.__midumEvent = function(evt){
    try {
      if (evt && evt.kind === "steps_state"){ applyState(evt.payload); return; }
      if (evt && evt.kind === "steps_summary"){ appendRow("steps", JSON.stringify(evt.payload)); setTimeout(syncCards, 0); return; }
      if (evt && evt.kind === "step_done"){ appendRow("stepdone", (evt.payload && evt.payload.text) || "Step done"); return; }
    } catch(e){ console.error("[steps]", e); }
    var r = prevEvt ? prevEvt.apply(this, arguments) : undefined;
    if (evt && evt.kind === "done") setTimeout(function(){ refresh().catch(function(){}); syncCards(); }, 60);
    return r;
  };
  ["renderHistoryWindow", "clearChat"].forEach(function(n){
    var f = window[n];
    if (typeof f !== "function") return;
    window[n] = function(){
      var r = f.apply(this, arguments);
      setTimeout(function(){ refresh().catch(function(){}); syncCards(); }, 0);
      return r;
    };
  });

  // ── boot ──────────────────────────────────────────────────────────────────
  function boot(){
    if (!mount()) return;
    var tries = 0;
    (function tryInit(){
      refresh().catch(function(){ if (++tries < 20) setTimeout(tryInit, 500); });
    })();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
  window.addEventListener("pywebviewready", function(){ refresh().catch(function(){}); });
})();
"""


def _glass(selectors, body):
    """Wrap rules in the same gate the app's own Glass Surface block uses
    (html.liquid-glass-on AND a transparent backdrop: background image or ambient blobs)."""
    sel = ",\n".join(
        "html.liquid-glass-on.has-bg-image " + s + ",\nhtml.liquid-glass-on:not(.blobs-off) " + s
        for s in selectors
    )
    return sel + "{" + body + "}\n"


_SHEEN = "linear-gradient(150deg, var(--glass-sheen) 0%, var(--glass-sheen-soft) 30%, transparent 55%)"
_EDGE_SHADOW = "inset 0 1px 0 var(--glass-edge), inset 0 -1px 0 var(--glass-edge-dim), var(--glass-glow)"

_GLASS_CSS = (
    # Floating surfaces (editor panel + checklist card): stronger fill so text over chat stays readable.
    # Editor panel: EXACTLY the Settings window recipe (.st-dialog in app.py): panel fill at
    # min(85%, glass% + 15%), diagonal sheen, glass edge + inset highlight, blur(18px) saturate(1.25).
    "html.liquid-glass-on #steps-panel{"
    "background-color:color-mix(in srgb, var(--panel) min(85%, calc(var(--glass-pct) + 15%)), transparent);"
    "background-image:" + _SHEEN + ";border-color:var(--glass-edge);"
    "box-shadow:inset 0 1px 0 var(--glass-edge), 0 10px 30px rgba(0,0,0,.4);"
    "-webkit-backdrop-filter:blur(18px) saturate(1.25);backdrop-filter:blur(18px) saturate(1.25);}\n"
    + _glass([".stepcard"],
           "background-color:var(--glass-fill-strong) !important;background-image:" + _SHEEN + " !important;"
           "border-color:var(--glass-edge) !important;box-shadow:" + _EDGE_SHADOW + " !important;"
           "transition:background-color .18s var(--ease),box-shadow .18s var(--ease),border-color .18s var(--ease);")
    # Controls sitting on those surfaces.
    + _glass(["#steps-panel .sp-banner", "#steps-panel .sp-add input", "#steps-panel .sp-tpl input",
              "#steps-panel .sp-tpl select", "#steps-panel .sp-btn", "#steps-panel .sp-per button",
              "#steps-panel .sp-prog", ".stepcard .sc-chip", ".stepcard .sp-btn"],
             "background-color:var(--glass-fill) !important;background-image:" + _SHEEN + " !important;"
             "border-color:var(--glass-edge-dim) !important;box-shadow:inset 0 1px 0 var(--glass-edge-dim) !important;")
    + _glass(["#steps-panel .sp-btn:hover", "#steps-panel .sp-per button:hover", ".stepcard .sp-btn:hover"],
             "background-color:var(--glass-fill-strong) !important;border-color:var(--glass-edge) !important;")
    + _glass(["#steps-panel .sp-add input:focus", "#steps-panel .sp-tpl input:focus", "#steps-panel .sp-tpl select:focus"],
             "border-color:var(--accent) !important;background-color:var(--glass-fill-strong) !important;")
    # Step text fields stay flat (the generic input glass would box every row).
    + _glass(["#steps-panel .sp-row .sp-text"],
             "background:transparent !important;background-image:none !important;border-color:transparent !important;box-shadow:none !important;")
    + _glass(["#steps-panel .sp-row .sp-text:focus"],
             "background-color:var(--glass-fill-strong) !important;border-color:var(--accent) !important;")
    + _glass(["#steps-panel .sp-row:hover"], "background:var(--glass-sheen-soft);")
    + _glass(["#steps-panel .sp-opts", "#steps-panel .sp-tpl"], "border-top-color:var(--glass-edge-dim);")
    + _glass(["#steps-panel .sp-track"], "background:var(--glass-edge-dim);")
    + _glass(["#steps-panel .sp-switch input:checked + .sp-track"], "background:var(--accent);")
    + _glass(["#steps-panel .sp-box", ".stepcard .sc-box"], "border-color:var(--glass-edge);")
    + _glass(["#steps-panel .sp-row.done .sp-box", ".stepcard li.done .sc-box"],
             "background:var(--accent);border-color:var(--accent);")
    # Accent / destructive buttons keep their colour on glass.
    + _glass(["#steps-panel .sp-btn.sp-primary", ".stepcard .sp-btn.sp-primary"],
             "background-color:color-mix(in srgb,var(--accent) 80%,var(--glass-fill)) !important;background-image:none !important;"
             "border-color:var(--accent) !important;color:#fff !important;")
    + _glass(["#steps-panel .sp-btn.sp-danger.armed"],
             "background-color:var(--red) !important;background-image:none !important;border-color:var(--red) !important;color:#fff !important;")
    + _glass(["#steps-btn.open"],
             "background-color:var(--accent) !important;background-image:none !important;color:#fff !important;")
    + _glass(["#steps-btn .sp-badge"], "box-shadow:0 0 0 2px var(--glass-fill-strong);")
    # Thin scrollbar thumb picks up the glass edge colour.
    + _glass(["#steps-panel .sp-list::-webkit-scrollbar-thumb"], "background:var(--glass-edge);")
)


def html_block() -> str:
    """CSS + JS to splice in right before </body> of gui/app.py's _HTML."""
    return "<style>" + _CSS + _GLASS_CSS + "</style>\n<script>" + _JS + "</script>\n"
