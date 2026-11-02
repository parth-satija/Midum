# --- Midum first-launch onboarding wizard -------------------------------------
# Self-contained: backend methods (install()) + the front-end block (html_block()).
# Hooked into gui/app.py in three places (import, _HTML injection, install(Api) in main()).
#
# Flow: Welcome -> Gemini API keys (strongly recommended, up to 5 free-tier keys)
#       -> First chat (sends a real message through the normal chat UI).
# Auto-opens on a true first launch (no completion flag AND no keys configured), and can be
# re-opened any time from Settings (button injected next to "API keys") or via
# window.midumOpenOnboarding() in the page.
import json
import os
import urllib.request
import urllib.error
import webbrowser

import config

_GEMINI_FIELDS = ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3",
                  "GEMINI_API_KEY_4", "GEMINI_API_KEY_5")
_KEYS_URL = "https://aistudio.google.com/app/apikey"
_FLAG_FILE = os.path.join(config.STORAGE_DIR, "onboarding.json")


# ── helpers ───────────────────────────────────────────────────────────────────
def _read_secrets() -> dict:
    try:
        with open(os.path.abspath(config.SECRETS_FILE), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _saved_gemini_keys(data: dict = None) -> list:
    data = _read_secrets() if data is None else data
    out = []
    for field in _GEMINI_FIELDS:
        v = data.get(field, "")
        out.append(v.strip() if isinstance(v, str) else "")
    return out  # always 5 entries, "" for empty slots


def _any_key_configured(data: dict) -> bool:
    for _g, key, _l, _u in config.SECRET_KEY_FIELDS:
        v = data.get(key, "")
        if isinstance(v, str) and v.strip():
            return True
    lp = data.get("LITELLM_PROVIDERS")
    if isinstance(lp, dict):
        for cfg in lp.values():
            if isinstance(cfg, dict) and str(cfg.get("api_key", "")).strip():
                return True
    return False


def _read_flag() -> dict:
    try:
        with open(_FLAG_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_flag(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_FLAG_FILE), exist_ok=True)
        with open(_FLAG_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception as e:
        print(f"⚠️ [Onboarding] Could not write {_FLAG_FILE}: {e}")


def _mask(v: str) -> str:
    return config._mask_secret(v) if v else ""


# ── Api methods (attached to the Api class by install()) ─────────────────────
def onboarding_get_state(self):
    """Status for the wizard. auto_open is True only on a genuine first launch."""
    try:
        flag = _read_flag()
        data = _read_secrets()
        saved = _saved_gemini_keys(data)
        completed = bool(flag.get("completed"))
        has_any = _any_key_configured(data)
        # Existing user upgrading into this feature: they already have keys, so don't nag.
        if not completed and has_any:
            _write_flag({"completed": True, "auto_marked": True})
            completed = True
        return {
            "ok": True,
            "completed": completed,
            "auto_open": (not completed) and (not has_any),
            "gemini_count": sum(1 for k in saved if k),
            "gemini_masked": [_mask(k) for k in saved if k],
            "max_keys": len(_GEMINI_FIELDS),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def onboarding_test_gemini_key(self, key):
    """Cheap validity check: list one model. The key is sent as a header, never logged."""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "Paste a key first.", "network": False}
    if any(c in key for c in "\r\n\t "):
        return {"ok": False, "error": "That looks like more than just the key.", "network": False}
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
        headers={"x-goog-api-key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return {"ok": 200 <= resp.status < 300, "network": False}
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return {"ok": True, "note": "Key is valid (currently rate-limited).", "network": False}
        if e.code in (400, 401, 403):
            return {"ok": False, "error": "Google rejected this key. Check it was copied fully.", "network": False}
        return {"ok": False, "error": f"Unexpected response from Google (HTTP {e.code}).", "network": True}
    except Exception:
        return {"ok": False, "error": "Couldn't reach Google to verify the key.", "network": True}


def onboarding_save_gemini_keys(self, keys):
    """Add new keys into the first free Gemini slots (max 5 total), skipping duplicates,
    then hot-reload the Gemini backend so the key works without a restart."""
    try:
        if not isinstance(keys, list):
            return {"ok": False, "error": "Nothing to save."}
        new = []
        for k in keys:
            k = (k or "").strip() if isinstance(k, str) else ""
            if k and k not in new:
                new.append(k)
        if not new:
            return {"ok": False, "error": "Nothing to save."}
        saved = _saved_gemini_keys()
        updates, added, dupes = {}, 0, 0
        for k in new:
            if k in saved:
                dupes += 1
                continue
            try:
                slot = saved.index("")
            except ValueError:
                return {"ok": False, "error": "All 5 Gemini key slots are already full. Clear one under Settings > Models > API keys first."}
            saved[slot] = k
            updates[_GEMINI_FIELDS[slot]] = k
            added += 1
        if updates:
            res = config.save_secret_values(updates)
            if not res.get("ok"):
                return res
        reloaded = False
        try:
            import providers.gemini_api_backend as _gab
            reloaded = bool(_gab._load_gemini_api()[0])
        except Exception:
            pass
        return {"ok": True, "added": added, "duplicates": dupes,
                "total": sum(1 for k in saved if k), "reloaded": reloaded}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def onboarding_use_gemini(self):
    """Best-effort: make Gemini (API) the active chat provider."""
    try:
        fn = getattr(self, "select_provider", None)
        if fn is None:
            return {"ok": False, "error": "select_provider not found"}
        for arg in ("gemini_api", "Gemini (API)"):
            try:
                fn(arg)
                return {"ok": True}
            except TypeError:
                continue
        return {"ok": False, "error": "Could not switch provider automatically."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def onboarding_open_keys_page(self):
    try:
        webbrowser.open(_KEYS_URL)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def onboarding_mark_done(self, skipped_gemini=False):
    _write_flag({"completed": True, "skipped_gemini": bool(skipped_gemini)})
    return {"ok": True}


def install(api_cls):
    """Attach the wizard's backend methods to the Api class (call before Api() is created)."""
    for fn in (onboarding_get_state, onboarding_test_gemini_key, onboarding_save_gemini_keys,
               onboarding_use_gemini, onboarding_open_keys_page, onboarding_mark_done):
        setattr(api_cls, fn.__name__, fn)


# ── Front end ────────────────────────────────────────────────────────────────
_CSS = r"""
<style id="mob-style">
#mob-overlay{position:fixed;inset:0;z-index:99990;display:none;align-items:center;justify-content:center;
  background:rgba(4,6,12,.62);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);font-family:inherit}
#mob-overlay.open{display:flex}
#mob-card{width:min(600px,92vw);max-height:90vh;overflow:auto;border-radius:18px;padding:26px 28px 22px;
  background:var(--panel,#0f1420);color:var(--text,#e8ecf5);border:1px solid rgba(255,255,255,.10);
  box-shadow:0 24px 80px rgba(0,0,0,.55)}
#mob-card h2{margin:0 0 6px;font-size:21px;font-weight:650}
#mob-card p{margin:6px 0;line-height:1.5;font-size:14px;opacity:.9}
#mob-dots{display:flex;gap:6px;margin-bottom:16px}
#mob-dots i{height:4px;flex:1;border-radius:3px;background:rgba(255,255,255,.14)}
#mob-dots i.on{background:var(--accent,#6aa8ff)}
.mob-rec{margin:12px 0;padding:12px 14px;border-radius:12px;background:rgba(106,168,255,.10);
  border:1px solid rgba(106,168,255,.35);font-size:13.5px;line-height:1.5}
.mob-rec b{color:var(--accent,#8fbcff)}
.mob-rec ul{margin:6px 0 0 18px;padding:0}
.mob-row{display:flex;gap:8px;align-items:center;margin:8px 0}
.mob-row input[type=password],.mob-row input[type=text],#mob-chat-text{flex:1;padding:9px 11px;border-radius:9px;
  border:1px solid rgba(255,255,255,.16);background:rgba(255,255,255,.05);color:inherit;font:inherit;font-size:13.5px}
#mob-chat-text{width:100%;box-sizing:border-box;min-height:84px;resize:vertical}
.mob-st{font-size:12px;min-width:110px}
.mob-ok{color:#5fd38d}.mob-bad{color:#ff7b7b}.mob-dim{opacity:.65}
.mob-btn{padding:8px 15px;border-radius:9px;border:1px solid rgba(255,255,255,.18);background:rgba(255,255,255,.07);
  color:inherit;font:inherit;font-size:13.5px;cursor:pointer}
.mob-btn:hover{background:rgba(255,255,255,.13)}
.mob-btn.pri{background:var(--accent,#3d7bff);border-color:transparent;color:#fff}
.mob-btn:disabled{opacity:.5;cursor:default}
.mob-foot{display:flex;justify-content:space-between;align-items:center;margin-top:18px;gap:10px}
.mob-chips{display:flex;flex-wrap:wrap;gap:7px;margin:8px 0}
.mob-chip{padding:5px 11px;border-radius:999px;font-size:12.5px;cursor:pointer;border:1px solid rgba(255,255,255,.16);
  background:rgba(255,255,255,.05)}
.mob-chip:hover{background:rgba(255,255,255,.12)}
.mob-warn{margin:10px 0;padding:10px 12px;border-radius:10px;background:rgba(255,160,70,.12);
  border:1px solid rgba(255,160,70,.45);font-size:13px}
</style>
"""

_HTML_BODY = r"""
<div id="mob-overlay" role="dialog" aria-modal="true" aria-label="Welcome to Midum"><div id="mob-card"></div></div>
"""

_JS = r"""
<script id="mob-script">
(function(){
  const MAX = 5;
  const CHIPS = ["Introduce yourself and tell me what you can do.",
                 "Help me plan my day. Ask me what's on my list.",
                 "Explain what Midum can do on my computer, with examples."];
  let st = null;
  let opened = false;

  function call(name, ...a){ try { return api(name, ...a); } catch(e){ return Promise.resolve(null); } }
  const $ = (s)=>document.querySelector(s);
  const esc = (s)=>String(s).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

  function dots(n){ return '<div id="mob-dots">' + [0,1,2].map(i=>'<i class="'+(i<=n?'on':'')+'"></i>').join("") + '</div>'; }
  function card(html){ $("#mob-card").innerHTML = html; }

  async function openWizard(){
    const s = await call("onboarding_get_state");
    st = {step:0, vals:[""], slots:1, tested:{}, saved:(s&&s.gemini_count)||0,
          masked:(s&&s.gemini_masked)||[], warnSkip:false, useGemini:true, busy:false};
    $("#mob-overlay").classList.add("open");
    render();
  }
  function closeWizard(){ $("#mob-overlay").classList.remove("open"); }

  function render(){
    if (st.step === 0) return renderWelcome();
    if (st.step === 1) return renderKeys();
    return renderChat();
  }

  function renderWelcome(){
    card(dots(0) + '<h2>Welcome to Midum</h2>' +
      '<p>Let\'s get you set up in about a minute: connect a Gemini API key, then have your first chat.</p>' +
      '<div class="mob-foot"><button class="mob-btn mob-dim" id="mob-skipall">Skip setup</button>' +
      '<button class="mob-btn pri" id="mob-next">Get started</button></div>');
    $("#mob-next").onclick = ()=>{ st.step = 1; render(); };
    $("#mob-skipall").onclick = async ()=>{ await call("onboarding_mark_done", true); closeWizard(); };
  }

  function keyRows(){
    let h = "";
    for (let i = 0; i < st.slots; i++){
      const t = st.tested[i];
      const msg = t ? (t.ok ? '<span class="mob-ok">✓ ' + esc(t.note || "Key works") + '</span>'
                           : '<span class="mob-bad">' + esc(t.error) + '</span>') : "";
      h += '<div class="mob-row"><input type="password" data-i="'+i+'" placeholder="Gemini API key '+(i+1)+
           '" value="'+esc(st.vals[i]||"")+'" autocomplete="off" spellcheck="false">' +
           '<button class="mob-btn" data-test="'+i+'">Test</button></div>' +
           '<div class="mob-st" data-msg="'+i+'" style="margin:-4px 0 4px">'+msg+'</div>';
    }
    return h;
  }

  function renderKeys(){
    const free = MAX - st.saved;
    card(dots(1) + '<h2>Add your Gemini API key</h2>' +
      '<div class="mob-rec"><b>Strongly recommended:</b> set up at least one Gemini API key. It enables:' +
      '<ul><li><b>Voice mode</b></li><li><b>AI inline code suggestions</b></li><li><b>File uploads</b></li></ul>' +
      '<div style="margin-top:8px">You can add up to <b>5 free-tier API keys</b>. When one runs out of quota, Midum switches to the next automatically.</div></div>' +
      '<div class="mob-row"><button class="mob-btn" id="mob-getkey">Get a free key from Google AI Studio ↗</button></div>' +
      (st.saved ? '<p class="mob-ok" style="font-size:13px">✓ ' + st.saved + ' key' + (st.saved>1?'s':'') +
                  ' already saved (' + esc(st.masked.join(", ")) + '). New keys are added to the free slots.</p>' : '') +
      '<div id="mob-keyrows">' + keyRows() + '</div>' +
      (st.slots < free ? '<button class="mob-btn" id="mob-add">+ Add another key (' + st.slots + '/' + free + ')</button>' : '') +
      (st.warnSkip ? '<div class="mob-warn">Without a Gemini key, <b>Voice mode, AI inline code suggestions and File uploads won\'t work</b>. ' +
                     'You can add one later from Settings. <button class="mob-btn" id="mob-skipkey" style="margin-left:6px">Skip anyway</button></div>' : '') +
      '<div class="mob-foot"><button class="mob-btn" id="mob-back">Back</button>' +
      '<button class="mob-btn pri" id="mob-cont">' + (st.busy ? "Checking…" : "Save & continue") + '</button></div>');

    document.querySelectorAll('#mob-keyrows input').forEach(inp=>{
      inp.oninput = ()=>{ st.vals[+inp.dataset.i] = inp.value; delete st.tested[+inp.dataset.i]; st.warnSkip = false;
                          const m = document.querySelector('[data-msg="'+inp.dataset.i+'"]'); if (m) m.innerHTML = ""; };
    });
    document.querySelectorAll('[data-test]').forEach(b=>{ b.onclick = async ()=>{
      const i = +b.dataset.test; st.tested[i] = await call("onboarding_test_gemini_key", st.vals[i]||"") || {ok:false,error:"No response."};
      render(); }; });
    $("#mob-getkey").onclick = ()=>call("onboarding_open_keys_page");
    const add = $("#mob-add"); if (add) add.onclick = ()=>{ st.slots++; render(); };
    $("#mob-back").onclick = ()=>{ st.step = 0; render(); };
    const sk = $("#mob-skipkey"); if (sk) sk.onclick = ()=>{ st.step = 2; st.skippedKey = true; render(); };
    $("#mob-cont").onclick = continueFromKeys;
  }

  async function continueFromKeys(){
    if (st.busy) return;
    const entered = st.vals.map((v,i)=>({v:(v||"").trim(), i})).filter(x=>x.v);
    if (!entered.length){
      if (st.saved){ st.step = 2; return render(); }
      st.warnSkip = true; return render();
    }
    st.busy = true; render();
    let hardFail = false;
    for (const {v,i} of entered){
      let t = st.tested[i];
      if (!t) t = st.tested[i] = await call("onboarding_test_gemini_key", v) || {ok:false,error:"No response.",network:true};
      if (!t.ok && !t.network) hardFail = true;
    }
    st.busy = false;
    if (hardFail){ render(); return; }
    const res = await call("onboarding_save_gemini_keys", entered.map(x=>x.v));
    if (!res || !res.ok){
      st.tested[entered[0].i] = {ok:false, error:(res && res.error) || "Could not save keys."}; return render();
    }
    st.saved = res.total; st.reloaded = res.reloaded; st.step = 2; render();
  }

  function renderChat(){
    const hasKey = st.saved > 0;
    card(dots(2) + '<h2>' + (hasKey ? "You're all set. Say hello!" : "Almost there") + '</h2>' +
      (hasKey ? '<p class="mob-ok">✓ ' + st.saved + ' Gemini key' + (st.saved>1?'s':'') + ' saved.' +
                (st.reloaded ? '' : ' If Voice mode or inline suggestions don\'t appear right away, restart Midum once.') + '</p>'
              : '<div class="mob-warn">No Gemini key yet. You can still chat if another provider is configured in the Model tab, ' +
                'and you can add a key any time from Settings.</div>') +
      '<p>Start your first chat:</p><div class="mob-chips">' +
      CHIPS.map((c,i)=>'<span class="mob-chip" data-chip="'+i+'">'+esc(c)+'</span>').join("") + '</div>' +
      '<textarea id="mob-chat-text" placeholder="Type your first message…">' + esc(CHIPS[0]) + '</textarea>' +
      (hasKey ? '<label style="display:flex;gap:8px;align-items:center;margin-top:10px;font-size:13px">' +
                '<input type="checkbox" id="mob-usegem" ' + (st.useGemini?'checked':'') + '> Use Gemini (API) as my chat model</label>' : '') +
      '<div class="mob-foot"><button class="mob-btn mob-dim" id="mob-finish">Finish without chatting</button>' +
      '<button class="mob-btn pri" id="mob-send">Send first message</button></div>');
    document.querySelectorAll('[data-chip]').forEach(c=>{ c.onclick = ()=>{ $("#mob-chat-text").value = CHIPS[+c.dataset.chip]; }; });
    $("#mob-finish").onclick = async ()=>{ await call("onboarding_mark_done", !!st.skippedKey || !hasKey); closeWizard(); };
    $("#mob-send").onclick = async ()=>{
      const text = ($("#mob-chat-text").value || "").trim();
      const cb = $("#mob-usegem");
      if (hasKey && cb && cb.checked) await call("onboarding_use_gemini");
      await call("onboarding_mark_done", !!st.skippedKey || !hasKey);
      closeWizard();
      const input = document.getElementById("msg-input");
      if (input && text){
        input.value = text; input.dispatchEvent(new Event("input", {bubbles:true}));
        try { if (typeof sendMessage === "function") sendMessage(); } catch(e){}
      }
    };
  }

  window.midumOpenOnboarding = openWizard;

  async function maybeAutoOpen(){
    if (opened) return; opened = true;
    const s = await call("onboarding_get_state");
    if (s && s.ok && s.auto_open) openWizard();
  }
  window.addEventListener("pywebviewready", async ()=>{
    try { if (window.__midumSplashGate) await window.__midumSplashGate; } catch(e){}
    setTimeout(maybeAutoOpen, 2500);   // after the app's own startup() has finished
  });

  // Settings > General has a "Setup guide" card (in app.py) whose button calls this.
  window.midumOpenOnboardingFromSettings = function(){
    const back = document.getElementById("settings-back-btn");
    if (back) back.click();            // close the Settings dialog first
    setTimeout(openWizard, 120);
  };
})();
</script>
"""


def html_block() -> str:
    """CSS + overlay markup + JS, appended before </body> (same pattern as steps_ui.html_block)."""
    return _CSS + _HTML_BODY + _JS
