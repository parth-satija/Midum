# =============================================================================
# SURFACE DELEGATE TOOLS (voice mode)
# =============================================================================
# The Gemini Live voice model is good at conversation but not at writing
# front-end code. So in voice mode it is NOT given the tools that take code
# (write_render_code / edit_render_code / show_ambient_surface /
# edit_ambient_surface -- see VOICE_BLOCKED_TOOLS). Instead it gets two
# request-style tools that hand the coding to the DEFAULT model -- whichever
# provider/model is currently selected in the app (config.MODEL_PROVIDER,
# set by the GUI's model picker) -- and then open the result through the
# normal surface tools:
#
#     get_ambient_surface_made(request, x, y, width, height, ...)
#     get_render_surface_made(request, title?, edit_current?)
#
# Flow: voice model describes what it wants in plain language -> we build a
# focused coding prompt (surface contract + size + any existing code when
# revising) -> ONE tool-free-in-spirit call to the default model through
# orchestration._call_primary_model (same entry point the main chat loop
# uses, so every provider works) -> extract the HTML -> show it via
# show_ambient_surface / write_render_code. The voice model never sees or
# writes the code; it just gets a confirmation to speak.
#
# These schemas are deliberately NOT in tools_schema.py: text-chat models can
# already write code themselves, so only the voice backend appends
# VOICE_SURFACE_TOOL_SCHEMAS (providers/gemini_live_backend.py).
# Descriptions stay under 1000 chars -- the voice backend truncates there.
# =============================================================================
import queue
import re
import threading
import time

import config
import tools.ambient_surface_tools as _ambient
import tools.render_surface_tools as _render

# Tools the voice model must NOT call directly (they take model-written code).
VOICE_BLOCKED_TOOLS = frozenset({
    "write_render_code", "edit_render_code",
    "show_ambient_surface", "edit_ambient_surface",
    "create_dynamic_background", "edit_dynamic_background",
})

_ERR = "[SURFACE DELEGATE ERROR]"
_GEN_TIMEOUT = 240  # seconds for one generation attempt


# ── Which model is "the default"? ────────────────────────────────────────────
def _default_model_label() -> str:
    prov = getattr(config, "MODEL_PROVIDER", "ollama")
    attr = {
        "ollama": "MODEL_NAME", "openrouter": "OPENROUTER_MODEL",
        "gemini_web": "GEMINI_WEB_MODEL", "gemini_api": "GEMINI_API_MODEL",
        "groq": "GROQ_MODEL", "ollama_cloud": "OLLAMA_CLOUD_MODEL",
        "litellm": "LITELLM_MODEL",
    }.get(prov, "MODEL_NAME")
    model = getattr(config, attr, "") or "auto"
    return f"{prov}/{model}"


# ── Prompts for the coding model ─────────────────────────────────────────────
_COMMON_RULES = """You are a senior front-end engineer. You are asked to write ONE self-contained HTML document that will run inside a sandboxed iframe.

OUTPUT FORMAT (strict): reply with ONLY the complete HTML document, starting with <!DOCTYPE html>. No markdown fences, no explanation before or after, and do NOT call any tools.

RULES:
- Everything inline: <style> and <script> in the same file. No external resources of any kind (no CDN scripts, web fonts, remote images) -- they are blocked. Use the system font stack (system-ui, -apple-system, "Segoe UI", sans-serif), inline SVG, CSS shapes and emoji for visuals.
- Make it look polished and modern: good spacing, a clear type hierarchy, restrained colour, subtle radius/shadows. Use real content from the request verbatim; never leave placeholder text.
- Available JS helpers (already injected, do not define them):
    await midum.call(toolName, argsObject)  -> runs a Midum tool and resolves to its result (string). Only use it if the request asks for live data or actions, and only with tool names given in the request.
    parent.postMessage(anyJson, "*")        -> sends user input back to the assistant as a chat message. Use for buttons/forms whose result the assistant must see.
- Keep the script robust: wrap midum.call in try/catch and show a friendly error state.
"""

_AMBIENT_RULES = """
THIS IS AN AMBIENT SURFACE: a small widget floating over the user's desktop, next to whatever they are looking at. It is glanceable, not an app.
- Its box is approximately {dims}. Design for exactly that box: html, body {{ margin:0; width:100%; height:100%; overflow:hidden }} (use overflow:auto on an inner container only if content may exceed the box). Prefer flex/grid, %, rem and clamp() over fixed pixel sizes so it survives small differences.
- The host clips your document to a rounded rectangle with ~48px corner radius and adds its own thin border and a circular close button OUTSIDE the top-right corner. Therefore: do NOT draw a close button, window chrome or an outer rounded frame, and keep at least ~14px padding from the edges (more, ~24px, near the four corners) so text is never cut by the rounding.
- Set an explicit background on <body> (it fills the whole card). Choose light or dark to suit the request; ensure strong text contrast. If the request implies it should sit unobtrusively, a dark translucent look is fine.
- Concise beats comprehensive: short headline, a few lines, small type sizes (13-16px body). It should be readable at a glance.
- Inside the widget, `midum.close()` dismisses it (e.g. for a "Got it" button, or a timer that should disappear when done).
"""

_RENDER_RULES = """
THIS IS A RENDER SURFACE: a panel filling roughly the right half of the Midum app window (tall, about 700x900 CSS px, resizable). It can hold a dashboard, document, chart, gallery, form, game or small tool the user will spend time in.
- Fill the panel: html, body { margin:0; min-height:100% }. Be responsive; it may be narrower than expected. Scroll vertically for long content.
- Charts/visualisations: draw with inline SVG or <canvas> and your own JS (no libraries).
- Interactive things should really work (state, handlers, keyboard where natural). Give it a background colour explicitly.
- The panel has its own header/close; do NOT draw window chrome.
"""


def _extract_html(text: str):
    """Pull an HTML document out of a model reply (tolerates fences/prose)."""
    if not text:
        return None
    t = text.strip()
    fences = re.findall(r"```(?:html|htm|xml)?[ \t]*\r?\n(.*?)```", t, re.S | re.I)
    if fences:
        t = max(fences, key=len).strip()
    else:
        t = re.sub(r"^```[a-zA-Z]*[ \t]*\r?\n", "", t)   # unclosed leading fence
    m = re.search(r"<!doctype\s+html|<html[\s>]", t, re.I)
    if m:
        t = t[m.start():]
        end = t.lower().rfind("</html>")
        return t[:end + 7] if end != -1 else t
    if re.search(r"<(div|section|main|svg|style|body|script|canvas)[\s>]", t, re.I):
        return t
    return None


def _generate_html(system_prompt: str, user_prompt: str):
    """One generation on the default model. Returns (html|None, error_text)."""
    from orchestration import _call_primary_model   # lazy: avoids import cycles
    from state import _abort_event

    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}]
    last_err = "unknown error"
    for _attempt in (1, 2):
        q = queue.Queue()
        threading.Thread(target=_call_primary_model, args=(messages, q),
                         daemon=True).start()
        deadline = time.time() + _GEN_TIMEOUT
        result = None
        while time.time() < deadline:
            try:
                result = q.get(timeout=0.25)
                break
            except queue.Empty:
                if _abort_event.is_set():
                    return None, "aborted by the user"
        if result is None:
            return None, f"timed out after {_GEN_TIMEOUT}s"

        status, payload = result
        if status == "err":
            last_err = str(payload)
            continue                                   # one retry for transient errors
        try:
            content = (payload["message"].get("content") or "")
        except Exception as e:
            last_err = f"unreadable model response ({e})"
            continue
        html = _extract_html(content)
        if html:
            return html, ""
        last_err = "the model didn't return an HTML document"
        messages = messages + [
            {"role": "assistant", "content": content or "(empty)"},
            {"role": "user", "content": (
                "That was not usable. Reply with ONLY the complete HTML document "
                "starting with <!DOCTYPE html> -- no prose, no code fences, no tool calls.")},
        ]
    return None, last_err


def _size_hint(width, height, space) -> str:
    try:
        w, h = float(width), float(height)
        if str(space or "canvas").lower() == "canvas":
            w, h = w * float(config.SCALE_X), h * float(config.SCALE_Y)
        return f"{int(round(w))} x {int(round(h))} screen pixels (CSS px may be somewhat smaller on high-DPI displays)"
    except Exception:
        return "small (a few hundred pixels wide)"


def _user_prompt(request: str, existing: str) -> str:
    if existing:
        return ("REQUEST (a change to the existing document below):\n" + request +
                "\n\nCURRENT DOCUMENT:\n" + existing +
                "\n\nApply the change and return the COMPLETE updated document.")
    return "REQUEST:\n" + request


# ── Tool: ambient ────────────────────────────────────────────────────────────
def get_ambient_surface_made(request, x=None, y=None, width=None, height=None,
                             surface_id="", title="", coordinate_space="canvas",
                             focusable=False, auto_close_seconds=0) -> str:
    request = (request or "").strip()
    if not request:
        return f"{_ERR} 'request' must describe what the widget should show or do."
    for name, v in (("x", x), ("y", y), ("width", width), ("height", height)):
        if v is None or v == "":
            return f"{_ERR} '{name}' is required -- choose where the widget goes and how big it is."
    if _ambient._gui_ambient_hook is None:
        return _ambient._NO_GUI

    sid = (surface_id or "").strip()
    existing = ""
    if sid:  # revising an open widget -> give the coder its current code
        cur = _ambient._call_hook("code", {"surface_id": sid})
        if isinstance(cur, str) and not cur.startswith("[AMBIENT SURFACE ERROR]"):
            existing = cur

    system = _COMMON_RULES + _AMBIENT_RULES.format(dims=_size_hint(width, height, coordinate_space))
    label = _default_model_label()
    html, err = _generate_html(system, _user_prompt(request, existing))
    if not html:
        return (f"{_ERR} The coding model ({label}) couldn't write the widget: {err}. "
                f"Tell the user briefly and offer to try again.")

    result = _ambient.show_ambient_surface(
        html, x, y, width, height, surface_id=sid, title=title,
        coordinate_space=coordinate_space, focusable=focusable,
        auto_close_seconds=auto_close_seconds)
    if result.startswith("[AMBIENT SURFACE ERROR]"):
        return result
    return (f"{result}\n(Code written by the default model, {label}.) "
            f"Just tell the user in one short sentence that it's on screen; "
            f"don't describe any code.")


# ── Tool: render ─────────────────────────────────────────────────────────────
def get_render_surface_made(request, title="", edit_current=False) -> str:
    request = (request or "").strip()
    if not request:
        return f"{_ERR} 'request' must describe what the surface should show or do."
    if _render._gui_render_hook is None:
        return ("[RENDER SURFACE ERROR] No GUI attached -- this tool only "
                "works inside the Midum desktop app.")

    existing = ""
    want_edit = edit_current if isinstance(edit_current, bool) else \
        str(edit_current).strip().lower() in ("1", "true", "yes")
    if want_edit:
        try:
            cur = _render._gui_render_hook("get", {})
            if isinstance(cur, str) and cur and not cur.startswith("[RENDER SURFACE ERROR]"):
                existing = cur
        except Exception:
            existing = ""

    label = _default_model_label()
    html, err = _generate_html(_COMMON_RULES + _RENDER_RULES, _user_prompt(request, existing))
    if not html:
        return (f"{_ERR} The coding model ({label}) couldn't write the surface: {err}. "
                f"Tell the user briefly and offer to try again.")

    result = _render.write_render_code(html, title or "")
    if result.startswith("[RENDER SURFACE ERROR]"):
        return result
    return (f"{result} (Code written by the default model, {label}.) "
            f"Just tell the user in one short sentence that it's open; "
            f"don't describe any code.")


# ── Voice wiring ─────────────────────────────────────────────────────────────
def call_voice_surface_tool(name: str, args: dict) -> str:
    """Blocking; the voice backend runs it on a worker thread."""
    a = dict(args or {})
    if name == "get_ambient_surface_made":
        return get_ambient_surface_made(
            a.get("request", ""), a.get("x"), a.get("y"),
            a.get("width"), a.get("height"),
            surface_id=a.get("surface_id", ""), title=a.get("title", ""),
            coordinate_space=a.get("coordinate_space", "canvas"),
            focusable=a.get("focusable", False),
            auto_close_seconds=a.get("auto_close_seconds", 0))
    if name == "get_render_surface_made":
        return get_render_surface_made(
            a.get("request", ""), a.get("title", ""), a.get("edit_current", False))
    return f"{_ERR} unknown tool '{name}'."


VOICE_SURFACE_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_ambient_surface_made",
            "description": (
                "Have the coding model build and show an on-screen widget (Ambient Surface) "
                "floating over the user's screen. You cannot write HTML yourself: describe in "
                "`request` what it should show/do, including every piece of text or data to "
                "display, word for word -- the coding model can't see the screen or hear the "
                "user. YOU pick the placement: x, y (top-left) and width, height in canvas "
                "coordinates (the 1024px-wide grid of fallback_view_screen); put it BESIDE "
                "the thing it explains, not on top. To revise an open widget, reuse its "
                "surface_id and describe the change. Takes ~10-40s. Say a short line first, "
                "then call it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "request": {"type": "string", "description": "Plain-language spec of the widget, with the exact content to show."},
                    "x": {"type": "integer", "description": "Left edge (canvas coordinates)."},
                    "y": {"type": "integer", "description": "Top edge (canvas coordinates)."},
                    "width": {"type": "integer", "description": "Width (canvas units; 1024 = full screen width). Typical: 200-400."},
                    "height": {"type": "integer", "description": "Height (canvas units). Typical: 100-300."},
                    "surface_id": {"type": "string", "description": "Short name; reuse to revise/replace that widget."},
                    "title": {"type": "string", "description": "Optional internal label."},
                    "coordinate_space": {"type": "string", "enum": ["canvas", "screen"], "description": "Default canvas."},
                    "focusable": {"type": "boolean", "description": "True only if the widget has text inputs to type into."},
                    "auto_close_seconds": {"type": "integer", "description": "If > 0, dismisses itself after this many seconds."},
                },
                "required": ["request", "x", "y", "width", "height"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_render_surface_made",
            "description": (
                "Have the coding model build and open a Render Surface: a panel in the right "
                "half of the Midum window for dashboards, documents, charts, forms, games or "
                "small tools. You cannot write HTML yourself: describe in `request` what it "
                "should show/do, including all the content/data it needs, word for word. Set "
                "edit_current=true to CHANGE the surface that is already open instead of "
                "starting fresh. Takes ~10-60s. Say a short line first, then call it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "request": {"type": "string", "description": "Plain-language spec, with the exact content/data to show."},
                    "title": {"type": "string", "description": "Optional short header label."},
                    "edit_current": {"type": "boolean", "description": "True to modify the currently open surface."},
                },
                "required": ["request"],
            },
        },
    },
]

VOICE_SURFACE_TOOL_NAMES = frozenset(
    t["function"]["name"] for t in VOICE_SURFACE_TOOL_SCHEMAS)

# =============================================================================
