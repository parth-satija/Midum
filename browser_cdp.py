# --- AUTO-SPLITTER: imports added by automated pass, please review ---
from config import _IS_LINUX
from providers.gemini_web_backend import _JS_GET_TEXT, _browser_element_cache, _clean_gemini_web_text, _get_gemini_web_client, _run_gemini_coro
from tools_registry import _store_index
from ui_automation.linux_navigator import _run
import requests
import subprocess
import time

# --- from main.py, section 1 ---
# BROWSER DOM — Chrome DevTools Protocol (CDP)
# =============================================================================
#
# HOW TO ENABLE (one-time setup):
#
#   Windows: Create a shortcut to chrome.exe with the extra flag:
#     --remote-debugging-port=9222
#   Or launch from terminal:
#     "C:\...\chrome.exe" --remote-debugging-port=9222
#
#   Linux:
#     google-chrome --remote-debugging-port=9222 &
#
#   The port only needs to be open once per Chrome session.
#   After enabling, all CDP tools work instantly with no extra setup.
#
# INSTALLATION:
#   pip install websocket-client requests
#
# =============================================================================

CDP_PORT    = 9222
CDP_HOST    = "localhost"
CDP_BASE    = f"http://{CDP_HOST}:{CDP_PORT}"

_cdp_ws_cache: dict[int, str] = {}   # tab_index → websocket URL

try:
    import websocket as _websocket
    import json as _json_mod
    _requests = requests   # reuse the module-level import (already loaded above)
    _CDP_AVAILABLE = requests is not None
except ImportError:
    _CDP_AVAILABLE = False

def _cdp_get_tabs() -> list[dict]:
    """Return list of open tab descriptors from Chrome's /json endpoint."""
    if not _CDP_AVAILABLE:
        return []
    try:
        resp = _requests.get(f"{CDP_BASE}/json", timeout=3)
        tabs = [t for t in resp.json() if t.get("type") == "page"]
        return tabs
    except Exception:
        return []

def _cdp_call(ws_url: str, method: str, params: dict = None, _retries: int = 2) -> dict:
    """
    Send a single CDP command over a fresh WebSocket connection and return result.
    Retries on connection errors. Raises on JS exceptions so callers can handle them.
    """
    if not _CDP_AVAILABLE:
        raise RuntimeError("websocket-client not installed: pip install websocket-client")
    last_err = None
    for attempt in range(_retries + 1):
        ws = None
        try:
            # Chrome 111+ enforces an Origin allowlist on CDP WebSocket
            # connections and rejects handshakes with no matching Origin
            # header (this is the "origin policy" rejection). Sending an
            # explicit localhost Origin header satisfies the default
            # allowlist so --remote-allow-origins=* is not required.
            ws = _websocket.create_connection(
                ws_url, timeout=12,
                origin=f"http://{CDP_HOST}:{CDP_PORT}"
            )
            msg = _json_mod.dumps({"id": 1, "method": method, "params": params or {}})
            ws.send(msg)
            # Drain until we get our response (id==1); skip CDP events
            deadline = time.time() + 12
            while time.time() < deadline:
                raw  = ws.recv()
                data = _json_mod.loads(raw)
                if data.get("id") == 1:
                    # Propagate runtime exceptions as Python errors
                    exc = (data.get("result") or {}).get("exceptionDetails")
                    if exc:
                        msg_text = exc.get("text") or exc.get("exception", {}).get("description", "JS error")
                        raise RuntimeError(f"CDP JS exception: {msg_text}")
                    return data
            raise TimeoutError("CDP response timed out")
        except (RuntimeError, TimeoutError):
            raise   # don't retry logic errors
        except Exception as e:
            last_err = e
            if attempt < _retries:
                time.sleep(0.3 * (attempt + 1))
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass
    final_err = str(last_err) if last_err else "unknown"
    if "403" in final_err or "handshake" in final_err.lower() or "forbidden" in final_err.lower():
        raise ConnectionError(
            f"CDP WebSocket rejected (likely an origin policy issue): {final_err}\n"
            f"Fix: relaunch Chrome/Brave with BOTH flags: "
            f"--remote-debugging-port={CDP_PORT} --remote-allow-origins=*"
        )
    raise ConnectionError(f"CDP call failed after {_retries + 1} attempts: {last_err}")

def _cdp_ws_for_tab(tab_index: int = 0) -> str | None:
    """Return the WebSocket debugger URL for a tab."""
    tabs = _cdp_get_tabs()
    if not tabs or tab_index >= len(tabs):
        return None
    return tabs[tab_index].get("webSocketDebuggerUrl")

def _cdp_require() -> str | None:
    """Return error string if CDP is unavailable, else None."""
    if not _CDP_AVAILABLE:
        return (
            "CDP not available. Install: pip install websocket-client requests\n"
            "Then launch Chrome with: --remote-debugging-port=9222"
        )
    tabs = _cdp_get_tabs()
    if not tabs:
        return (
            "Chrome is not running with remote debugging enabled.\n"
            "Launch Chrome with: --remote-debugging-port=9222\n"
            "  Windows: Create a shortcut with that flag appended to the Target.\n"
            "  Linux:   google-chrome --remote-debugging-port=9222 &\n"
            "If tabs ARE listed but WebSocket calls still fail with an origin/policy "
            "error, add --remote-allow-origins=* as well (Midum already sends a "
            "matching Origin header, so this should rarely be needed)."
        )
    return None


# =============================================================================

# --- from main.py, section 2 ---
def list_browser_tabs() -> str:
    err = _cdp_require()
    if err:
        return err
    tabs = _cdp_get_tabs()
    if not tabs:
        return "No open tabs found."
    _store_index("browser_tabs", [{"title": t.get("title",""), "url": t.get("url","")} for t in tabs])
    lines = [
        f"Open browser tabs ({len(tabs)})",
        "Use read_browser_page(tab_index=N) to read a tab's content.",
        "",
        f"{'IDX':>4}  {'TITLE':<40}  URL",
        "─" * 90,
    ]
    for i, t in enumerate(tabs):
        title = (t.get("title") or "")[:38]
        url   = (t.get("url")   or "")[:50]
        lines.append(f"{i:>4}  {title:<40}  {url}")
    return "\n".join(lines)


def read_browser_page(tab_index: int = 0) -> str:
    err = _cdp_require()
    if err:
        return err
    ws = _cdp_ws_for_tab(tab_index)
    if not ws:
        return f"Tab {tab_index} not found. Call list_browser_tabs first."
    try:
        # Get URL and title
        nav_result = _cdp_call(ws, "Runtime.evaluate", {
            "expression": "({url: document.URL, title: document.title})",
            "returnByValue": True
        })
        meta = nav_result.get("result", {}).get("result", {}).get("value", {})
        url   = meta.get("url",   "unknown")
        title = meta.get("title", "unknown")

        # Get clean text
        text_result = _cdp_call(ws, "Runtime.evaluate", {
            "expression": _JS_GET_TEXT,
            "returnByValue": True
        })
        text = text_result.get("result", {}).get("result", {}).get("value", "") or ""

        if not text.strip():
            return (
                f"Page: {title}\nURL: {url}\n\n"
                "[Page appears empty or content is dynamically rendered. "
                "Try snapshot(target='browser') or run_js_in_browser to inspect.]"
            )

        header = f"Page: {title}\nURL: {url}\n{'─'*60}\n"
        return header + text
    except Exception as e:
        return f"Error reading browser page: {e}"


def snapshot_browser_elements(tab_index: int = 0, filter_type: str = "") -> str:
    """
    Snapshot every interactable element on the page — not just semantic
    HTML controls. Catches:
      - Standard interactive tags (a, button, input, select, textarea)
      - ARIA roles (button, link, tab, menuitem, checkbox, radio, textbox, etc)
      - contenteditable regions (Gmail compose, Google Docs, chat inputs,
        rich-text editors — these have NO native tag/role but are typed into
        constantly on modern sites)
      - Elements with a raw onclick attribute (legacy/simple sites)
      - label and summary elements (often the actual click target for a
        checkbox/radio or a <details> disclosure)
      - Anything with a non-negative tabindex (explicitly made focusable)
    And traverses into OPEN SHADOW ROOTS recursively, since many modern
    sites (YouTube, GitHub, most component-library-based apps) build their
    real UI inside Web Components — a plain querySelectorAll from the light
    DOM never sees those elements at all.
    This is intended to make run_js_in_browser unnecessary for ordinary
    "what can I click/type into on this page" questions.
    """
    err = _cdp_require()
    if err:
        return err
    ws = _cdp_ws_for_tab(tab_index)
    if not ws:
        return f"Tab {tab_index} not found. Call list_browser_tabs first."

    # Enable Page domain so we can use Runtime.evaluate reliably
    try:
        _cdp_call(ws, "Runtime.enable", {})
    except Exception:
        pass

    try:
        filter_arg = _json_mod.dumps(filter_type.lower())
        js = f"""
(function(filterType) {{
    // Assign stable midum IDs to every element on this snapshot pass
    window.__jarvis_el_map = [];  // fresh snapshot, clear old refs

    var selectors = {{
        'button':   'button, [role="button"], input[type="button"], input[type="submit"], input[type="reset"], [onclick]',
        'link':     'a[href]',
        'input':    'input:not([type="hidden"]), textarea, [contenteditable]:not([contenteditable="false"])',
        'select':   'select',
        'textarea': 'textarea',
        'editable': '[contenteditable]:not([contenteditable="false"]), textarea, input:not([type="hidden"])',
        '': 'a[href], button, input:not([type="hidden"]), select, textarea, ' +
            '[role="button"], [role="link"], [role="tab"], [role="menuitem"], ' +
            '[role="option"], [role="checkbox"], [role="radio"], [role="switch"], ' +
            '[role="slider"], [role="spinbutton"], [role="combobox"], [role="textbox"], ' +
            '[contenteditable]:not([contenteditable="false"]), [onclick], ' +
            'label, summary, ' +
            '[tabindex]:not([tabindex="-1"])'
    }};
    var sel = selectors[filterType] || selectors[''];

    // ── Recursive shadow-DOM-aware query ──────────────────────────────────
    // Plain document.querySelectorAll cannot see into shadow roots at all,
    // which means entire UIs built with Web Components (YouTube, GitHub,
    // most modern component libraries) would otherwise be invisible.
    function deepQuery(root, selector, out, depth) {{
        if (depth > 6) return;   // safety cap on shadow-root nesting
        try {{
            var matches = root.querySelectorAll(selector);
            for (var i = 0; i < matches.length; i++) out.push(matches[i]);
        }} catch (e) {{ /* ignore malformed selector on this root */ }}
        try {{
            var everything = root.querySelectorAll('*');
            for (var j = 0; j < everything.length; j++) {{
                var node = everything[j];
                if (node.shadowRoot) {{
                    deepQuery(node.shadowRoot, selector, out, depth + 1);
                }}
            }}
        }} catch (e) {{ /* ignore */ }}
    }}

    var all = [];
    deepQuery(document, sel, all, 0);

    var results = [];
    var seen = new Set();
    for (var i = 0; i < all.length; i++) {{
        var el = all[i];
        if (seen.has(el)) continue;
        seen.add(el);

        try {{
            var rect = el.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) continue;
            if (rect.bottom < -50 || rect.top > window.innerHeight + 50) continue;
            if (rect.right  < -50 || rect.left > window.innerWidth  + 50) continue;
        }} catch(e) {{ continue; }}

        // Compute role/type
        var role = el.getAttribute('role') || el.tagName.toLowerCase();
        var inputType = el.getAttribute('type');
        if (role === 'input' && inputType) role = 'input[' + inputType + ']';
        var isEditable = el.isContentEditable ||
            (el.getAttribute('contenteditable') && el.getAttribute('contenteditable') !== 'false');
        if (isEditable && role !== 'textarea') role = 'contenteditable';

        // Best label — for contenteditable, innerText is usually empty until
        // focused/typed into, so fall back to common placeholder attributes
        // used by rich-text editors (Gmail, Slack, Notion, etc).
        var label = (
            el.getAttribute('aria-label') ||
            (el.getAttribute('aria-labelledby') && document.getElementById(el.getAttribute('aria-labelledby')) &&
                document.getElementById(el.getAttribute('aria-labelledby')).innerText) ||
            el.getAttribute('title') ||
            el.getAttribute('placeholder') ||
            el.getAttribute('aria-placeholder') ||
            el.getAttribute('data-placeholder') ||
            el.innerText ||
            el.value ||
            el.getAttribute('name') ||
            el.getAttribute('id') ||
            ''
        ).trim().replace(/\\s+/g, ' ').slice(0, 80);

        var jarvisId = window.__jarvis_el_map.length;
        window.__jarvis_el_map.push(el);

        results.push({{
            jarvis_id: jarvisId,
            type: role,
            label: label,
            href: el.href || '',
            x: Math.round(rect.left + rect.width / 2),
            y: Math.round(rect.top  + rect.height / 2),
            disabled: el.disabled || el.getAttribute('aria-disabled') === 'true'
        }});
    }}
    return results;
}})({filter_arg})
"""
        result   = _cdp_call(ws, "Runtime.evaluate", {"expression": js, "returnByValue": True})
        elements = (result.get("result", {}).get("result", {}).get("value") or [])

        if not elements:
            return (
                "No interactive elements found on this page. "
                "The page may still be loading — try wait(2) then retry, "
                "or use read_browser_page() to check what's on the page."
            )

        # Store with jarvis_id as key for O(1) lookup in act_on_browser_element
        cache = {e["jarvis_id"]: e for e in elements}
        _browser_element_cache[tab_index] = cache
        _store_index(f"browser_elements:{tab_index}", elements)

        # Page title for context
        try:
            title_res  = _cdp_call(ws, "Runtime.evaluate",
                                   {"expression": "document.title", "returnByValue": True})
            page_title = title_res.get("result",{}).get("result",{}).get("value","") or ""
        except Exception:
            page_title = ""

        lines = [
            f"Browser elements on '{page_title}' ({len(elements)} visible)",
            "Use act(target='browser', index=N) to interact.",
            "",
            f"{'IDX':>4}  {'TYPE':<18}  {'X':>5}  {'Y':>5}  LABEL",
            "─" * 84,
        ]
        for el in elements:
            disabled = " [disabled]" if el.get("disabled") else ""
            label    = (el.get("label") or "")[:44]
            lines.append(
                f"{el['jarvis_id']:>4}  {el['type']:<18}  {el['x']:>5}  {el['y']:>5}  {label}{disabled}"
            )
        return "\n".join(lines)

    except Exception as e:
        return f"Error snapshotting browser elements: {e}"


def act_on_browser_element(index: int, action: str = "click",
                            text_to_type: str = "", tab_index: int = 0) -> str:
    cache = _browser_element_cache.get(tab_index)
    if not cache:
        return (
            f"No browser element snapshot for tab {tab_index}. "
            "Call snapshot(target='browser') first."
        )

    # Cache is now a dict keyed by jarvis_id for O(1) lookup
    if isinstance(cache, dict):
        el = cache.get(index)
    else:
        # Legacy list format fallback
        el = next((e for e in cache if e.get("jarvis_id") == index
                   or e.get("idx") == index), None)

    if el is None:
        available = sorted(cache.keys()) if isinstance(cache, dict) else list(range(len(cache)))
        return (
            f"Index {index} not found in browser element snapshot. "
            f"Available indices: {available[:20]}{'...' if len(available) > 20 else ''}. "
            f"Call snapshot(target='browser') to refresh."
        )

    err = _cdp_require()
    if err:
        return err
    ws = _cdp_ws_for_tab(tab_index)
    if not ws:
        return f"Tab {tab_index} not found."

    x, y  = el.get("x", 0), el.get("y", 0)
    label = (el.get("label") or el.get("type") or "element")[:40]
    jid   = el.get("jarvis_id", index)

    try:
        if action == "get_text":
            js = f"""
(function() {{
    var el = window.__jarvis_el_map && window.__jarvis_el_map[{jid}];
    if (!el) return 'Element no longer in DOM — call snapshot again.';
    return el.innerText || el.value || el.textContent || '';
}})()"""
            result = _cdp_call(ws, "Runtime.evaluate",
                               {"expression": js, "returnByValue": True})
            text = (result.get("result",{}).get("result",{}).get("value") or "")
            return f"Text of element #{index} ('{label}'): {text}"

        elif action == "set_text":
            safe_text = _json_mod.dumps(text_to_type)
            js = f"""
(function() {{
    var el = window.__jarvis_el_map && window.__jarvis_el_map[{jid}];
    if (!el) return 'stale';
    el.focus();

    var isEditable = el.isContentEditable ||
        (el.getAttribute('contenteditable') && el.getAttribute('contenteditable') !== 'false');

    if (isEditable) {{
        // contenteditable regions (Gmail compose, Slack, Notion, chat inputs, etc)
        // have no .value — set via execCommand/textContent and fire input events
        // that frameworks listening for keystrokes will still pick up.
        document.execCommand('selectAll', false, null);
        document.execCommand('delete', false, null);
        var inserted = false;
        try {{
            inserted = document.execCommand('insertText', false, {safe_text});
        }} catch (e) {{ inserted = false; }}
        if (!inserted) {{
            el.textContent = {safe_text};
        }}
        el.dispatchEvent(new InputEvent('input', {{bubbles: true, data: {safe_text}, inputType: 'insertText'}}));
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
        return 'ok';
    }}

    // Native input/textarea — use the property setter so React-controlled
    // inputs register the change (plain .value = x is invisible to React).
    var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value') ||
        Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value');
    if (nativeInputValueSetter && nativeInputValueSetter.set) {{
        nativeInputValueSetter.set.call(el, {safe_text});
    }} else {{
        el.value = {safe_text};
    }}
    el.dispatchEvent(new Event('input',  {{bubbles: true}}));
    el.dispatchEvent(new Event('change', {{bubbles: true}}));
    return 'ok';
}})()"""
            result = _cdp_call(ws, "Runtime.evaluate",
                               {"expression": js, "returnByValue": True})
            val = (result.get("result",{}).get("result",{}).get("value") or "")
            if val == "ok":
                return f"Success: set text of element #{index} ('{label}') to '{text_to_type[:40]}'"
            if val == "stale":
                return f"Element #{index} is stale — call snapshot(target='browser') to refresh."
            return f"set_text on #{index} returned unexpected: {val}"

        else:   # click
            # 1. Scroll element into view via JS
            _cdp_call(ws, "Runtime.evaluate", {
                "expression": (
                    f"(function() {{"
                    f"  var el = window.__jarvis_el_map && window.__jarvis_el_map[{jid}];"
                    f"  if (el) el.scrollIntoView({{block:'center',inline:'center'}});"
                    f"}})()"
                ),
                "returnByValue": True
            })
            time.sleep(0.1)

            # 2. Re-read coordinates after scroll
            coord_res = _cdp_call(ws, "Runtime.evaluate", {
                "expression": (
                    f"(function() {{"
                    f"  var el = window.__jarvis_el_map && window.__jarvis_el_map[{jid}];"
                    f"  if (!el) return null;"
                    f"  var r = el.getBoundingClientRect();"
                    f"  return {{x: Math.round(r.left+r.width/2), y: Math.round(r.top+r.height/2)}};"
                    f"}})()"
                ),
                "returnByValue": True
            })
            coord = (coord_res.get("result",{}).get("result",{}).get("value") or {})
            if coord:
                x, y = coord.get("x", x), coord.get("y", y)

            # 3. Mouse move + press + release (Chrome requires the move first)
            for evt_type, extras in [
                ("mouseMoved",    {}),
                ("mousePressed",  {"button": "left", "clickCount": 1}),
                ("mouseReleased", {"button": "left", "clickCount": 1}),
            ]:
                _cdp_call(ws, "Input.dispatchMouseEvent", {
                    "type": evt_type, "x": x, "y": y,
                    "modifiers": 0, "timestamp": time.time(),
                    **extras
                })

            # 4. Fallback: also dispatch a JS click event in case the page uses
            #    event listeners that only fire on the element directly
            try:
                _cdp_call(ws, "Runtime.evaluate", {
                    "expression": (
                        f"(function() {{"
                        f"  var el = window.__jarvis_el_map && window.__jarvis_el_map[{jid}];"
                        f"  if (el) el.click();"
                        f"}})()"
                    ),
                    "returnByValue": True
                })
            except Exception:
                pass

            return f"Success: clicked browser element #{index} ('{label}') at ({x},{y})."

    except Exception as e:
        return f"Error acting on browser element #{index} ('{label}'): {e}"


def run_js_in_browser(script: str, tab_index: int = 0) -> str:
    err = _cdp_require()
    if err:
        return err
    ws = _cdp_ws_for_tab(tab_index)
    if not ws:
        return f"Tab {tab_index} not found. Call list_browser_tabs first."
    try:
        # Wrap in an IIFE so both expressions and statements work correctly,
        # and so return values are always captured.
        wrapped = f"(function(){{ try {{ return ({script}); }} catch(e) {{ return String(e); }} }})()"
        result = _cdp_call(ws, "Runtime.evaluate", {
            "expression":   wrapped,
            "returnByValue": True,
            "awaitPromise":  True,
        })
        val   = result.get("result", {}).get("result", {})
        vtype = val.get("type", "")
        value = val.get("value")

        if vtype == "undefined":
            return "(undefined — script ran but returned no value)"
        if vtype == "string":
            return str(value)
        if value is not None:
            return _json_mod.dumps(value, ensure_ascii=False)
        # Handle object/null/etc
        desc = val.get("description") or val.get("className") or vtype
        return f"({desc})"
    except RuntimeError as e:
        return f"JS error: {e}"
    except Exception as e:
        return f"Error executing JS: {e}"


def _cdp_press_enter(ws: str) -> None:
    """Simulate a real Enter keypress via CDP Input events (not a JS-dispatched
    synthetic event) — needed for sites like Gemini that listen for genuine
    keydown/keyup on Enter to submit, rather than a form submit button."""
    for evt_type, extra in [
        ("keyDown", {"text": "\r", "unmodifiedText": "\r"}),
        ("keyUp",   {}),
    ]:
        _cdp_call(ws, "Input.dispatchKeyEvent", {
            "type": evt_type,
            "key":  "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13,
            "nativeVirtualKeyCode":  13,
            **extra,
        })


def _clipboard_read() -> str:
    """
    Read the OS clipboard — pyperclip on Windows, xclip/xsel on Linux.
    Matches the same platform-aware pattern used elsewhere in this file
    (type_text's clipboard paste, the legacy desktop Gemini bridge).
    """
    if _IS_LINUX:
        out, _ = _run(["xclip", "-selection", "clipboard", "-o"])
        if out:
            return out
        out2, _ = _run(["xsel", "--clipboard", "--output"])
        return out2 or ""
    else:
        try:
            import pyperclip
            return pyperclip.paste() or ""
        except Exception:
            return ""


def _clipboard_clear() -> None:
    """
    Clear the OS clipboard before triggering a copy, so a subsequent read
    can distinguish "copy actually happened" from "copy button did nothing
    and we're just reading whatever was already on the clipboard."
    """
    if _IS_LINUX:
        try:
            subprocess.run(["xclip", "-selection", "clipboard"], input="", text=True, timeout=3)
        except Exception:
            pass
    else:
        try:
            import pyperclip
            pyperclip.copy("")
        except Exception:
            pass


def _cdp_verify_page_url(ws: str, expected_prefix: str) -> tuple[bool, str]:
    """
    Confirm the target behind `ws` is genuinely the expected page by asking
    the page itself for `document.URL`, rather than trusting the `url` field
    from Chrome's /json tab listing.

    This matters because Chrome sometimes exposes internal WebUI surfaces
    (tab hover-cards, tab-search previews, split-view prompts, etc) as their
    own inspectable CDP targets, and their /json `url` field can still
    contain the site's URL as embedded text/metadata — which causes a naive
    substring match on the listing to connect to the wrong target entirely
    and return the hover-card's own text ("Create split view", the page
    title, etc) instead of the real page content.

    Calls Runtime.enable first — a brand-new tab's target may not have
    Runtime fully attached yet, and Runtime.evaluate can fail/throw on an
    unattached target even though the tab is otherwise completely valid.
    Retries once on a transient failure before giving up on this candidate.
    """
    for attempt in range(2):
        try:
            _cdp_call(ws, "Runtime.enable", {})
            result = _cdp_call(ws, "Runtime.evaluate", {
                "expression": "document.URL", "returnByValue": True
            })
            actual_url = (result.get("result", {}).get("result", {}).get("value") or "")
            if actual_url:
                return actual_url.startswith(expected_prefix), actual_url
        except Exception:
            pass
        if attempt == 0:
            time.sleep(0.4)
    return False, ""


def query_gemini_app(prompt: str, wait_for_response: int = 90) -> str:
    from orchestration import _extract_legacy_tool_calls
    """
    Send a prompt to Gemini via the community `gemini_webapi` library
    (https://github.com/HanaokaYuzu/Gemini-API), NOT Chrome/CDP DOM
    automation and NOT UIA. This library talks directly to the Gemini web
    app's internal endpoints using your browser's session cookies, so
    there's no page to load, no prompt box to click into, and no "Copy"
    button to find — it's a plain HTTP/async call, which is far more
    reliable than driving a real browser tab.

    Crucially, this still goes through the FREE WEB APP's own usage
    limits (the same ones you'd get chatting manually at
    gemini.google.com) — it is NOT the metered developer API, which has
    much tighter free-tier limits.

    Install:   pip install -U gemini_webapi
    Optional:  pip install -U browser-cookie3   (lets gemini_webapi pull
               cookies automatically from a browser you're already logged
               into, instead of needing them in the secrets file)

    Authentication (one-time), whichever is easier:
      A) Automatic — install browser-cookie3 and be logged into
         https://gemini.google.com in a supported browser. No extra config.
      B) Manual — log into https://gemini.google.com, open DevTools (F12)
         -> Network tab -> refresh -> find the __Secure-1PSID and
         __Secure-1PSIDTS cookies, and add them to the secrets file:
           { "GEMINI_SECURE_1PSID": "...", "GEMINI_SECURE_1PSIDTS": "..." }
    """
    client, err = _get_gemini_web_client()
    if client is None:
        return f"Error: {err}"

    try:
        response = _run_gemini_coro(client.generate_content(prompt), timeout=wait_for_response)
    except Exception as e:
        return f"Error: Gemini web request failed: {e}"

    text = (getattr(response, "text", "") or "").strip()
    if not text:
        return "Gemini did not return any text in its response - try again."
    text = _clean_gemini_web_text(text)

    # Run through the legacy tool-call JSON parser so any embedded
    # tool-call JSON is flagged/stripped instead of being shown raw.
    legacy_calls, cleaned_text = _extract_legacy_tool_calls(text)
    if legacy_calls:
        call_summary = ", ".join(
            f"{c['function']['name']}({c['function']['arguments']})" for c in legacy_calls
        )
        return (
            f"{cleaned_text}\n\n"
            f"[NOTE: detected {len(legacy_calls)} embedded tool call(s) in Gemini's response: "
            f"{call_summary}. query_gemini_app only returns text - dispatch these yourself if needed.]"
        )

    return cleaned_text or text





CHUNK_CHARS = 12000   # max chars per file chunk sent to the model (~3k tokens)


