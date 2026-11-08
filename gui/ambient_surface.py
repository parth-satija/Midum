# =============================================================================
# AMBIENT SURFACE MANAGER
# =============================================================================
# GUI half of the Ambient Surface feature (the model-facing tools live in
# tools/ambient_surface_tools.py; gui/app.py wires the two together).
#
# An Ambient Surface is a Render Surface's floating cousin: the model writes
# HTML+CSS+JS, it runs in a sandboxed iframe with the same window.midum.call
# bridge -- but instead of a pane inside the Midum window it is its OWN small
# borderless, transparent, always-on-top OS window drawn over whatever the
# user is looking at, at a position and size the model picks in the tool call.
#
# Each surface is one pywebview window (same Qt/Chromium backend the main GUI
# uses) whose top-level page is a small trusted "shell" -- built by
# _build_shell_html below -- that contains:
#
#   * #card   -- a well-rounded rectangle filling the window; clips its
#                content (a sandboxed <iframe srcdoc=...> holding the model's
#                HTML) to the rounded corners.
#   * #close  -- a circular ✕ button placed just OUTSIDE the card's top-right
#                rounded corner: inside the window's bounding box (i.e. where
#                the corner would be if the rectangle had square corners) but
#                in the empty sliver the rounding cuts away, tangent to the
#                card's edge. Its size and offset are computed in JS from the
#                actual corner radius/window size so it never overlaps the
#                card (see layout()).
#
# The iframe is deliberately NOT allow-same-origin, so model code can't reach
# pywebview.api; it can only postMessage to the shell, which forwards tool
# calls / free-form input to this manager's per-window _AmbientBridge (the
# window's own js_api). This is the exact trust model of the Render Surface.
#
# Windows extras (applied once the native window exists, see _finish_window):
# exact physical-pixel placement via SetWindowPos (Qt6 may interpret
# create_window's x/y/width/height in DPI-scaled units), WS_EX_TOOLWINDOW (no
# taskbar/Alt-Tab entry), WS_EX_NOACTIVATE (never steals focus, unless the
# surface asked to be focusable) and WDA_EXCLUDEFROMCAPTURE (hidden from
# screenshots/OCR/screen-share unless capture_visible). On other platforms the
# window is placed with pywebview's own move()/resize().
# =============================================================================
import json
import re
import sys
import threading
import time
import uuid
import os
import datetime

import config
import tools.ambient_surface_tools as _ambient_tools

try:
    import webview  # pywebview -- same package/backend as gui/app.py
except Exception:  # pragma: no cover - GUI package always present in the app
    webview = None


MIN_W, MIN_H = 80, 60          # smallest widget, in real screen pixels
MAX_SURFACES = 8               # hard cap on simultaneously open widgets
DEFAULT_RADIUS = 48            # CSS px; "well rounded"
_INITIAL_WINDOW_MIN = (40, 40)  # pywebview/Qt min_size -- must not exceed MIN_*

DEFAULT_MOVE_MS = 450          # glide duration for move_ambient_surface
MAX_MOVE_MS = 3000
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")
_SAFE_COLOR_RE = re.compile(r"^[#A-Za-z0-9\s(),.%/+-]{1,80}$")


# ── Qt profile guard ────────────────────────────────────────────────────────
def _use_private_profile_for_child_windows() -> None:
    """
    Prevent the freeze/crash on the first show_ambient_surface call.

    The main window is started with webview.start(private_mode=False,
    storage_path=...). In pywebview's Qt backend EVERY BrowserView then builds
    its own QWebEngineProfile('pywebview') with setPersistentStoragePath() on
    that same directory. The main window already owns it, so a second profile
    on the same locked Chromium profile dir (cookies / LevelDB / cache) hangs
    the GUI thread and then kills the process the moment an ambient window is
    created.

    Ambient surfaces never need persistent storage (their HTML only lives in
    memory), so make child windows use an off-the-record profile instead. This
    is deliberately set once and left on, NOT toggled back: the BrowserView is
    built asynchronously on the GUI thread after create_window() returns, so
    resetting the flag would race with it. The main window already exists by
    the time any surface is shown, so it keeps its persistent profile.
    """
    try:
        state = getattr(webview, "_state", None)
        if state is not None and not state["private_mode"]:
            state["private_mode"] = True
    except Exception:
        pass


# ── Small helpers ───────────────────────────────────────────────────────────
def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(v)


def _num(v, name: str):
    """Coerce a model-supplied number (int/float/'123'/'123px') to float."""
    if isinstance(v, bool):
        raise ValueError(f"'{name}' must be a number, got {v!r}.")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().lower().rstrip("px").strip()
        try:
            return float(s)
        except ValueError:
            pass
    raise ValueError(f"'{name}' must be a number, got {v!r}.")


def _clean_id(raw) -> str:
    return _SAFE_ID_RE.sub("_", str(raw or "").strip()).strip("_")[:40]


def _json_for_html(obj) -> str:
    """json.dumps that is safe to embed inside an inline <script> block."""
    return (json.dumps(obj)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026"))


def _bridge_html(sid: str) -> str:
    """
    Prepended to the model's code inside the iframe (never stored with it, so
    edit_ambient_surface's exact-substring matching only ever sees what the
    model wrote). Gives the surface window.midum.call(name, args) -- identical
    to the Render Surface bridge -- plus window.midum.close() to dismiss
    itself and window.midum.ambient.id.
    """
    return (
        "<script>\n(function(){\n"
        "  var n = 0, pending = {};\n"
        "  window.addEventListener('message', function(e){\n"
        "    var d = e.data;\n"
        "    if (d && d.__midum_call_result === true){\n"
        "      var p = pending[d.id];\n"
        "      if (p){ delete pending[d.id];\n"
        "        if (d.error) p.reject(new Error(d.error)); else p.resolve(d.result); }\n"
        "    }\n"
        "  });\n"
        "  window.midum = window.midum || {};\n"
        "  window.midum.call = function(tool, args){\n"
        "    return new Promise(function(resolve, reject){\n"
        "      var id = 'a' + (++n) + '_' + Date.now();\n"
        "      pending[id] = {resolve: resolve, reject: reject};\n"
        "      parent.postMessage({__midum_call: true, id: id, tool: tool, args: args || {}}, '*');\n"
        "    });\n"
        "  };\n"
        "  window.midum.ambient = {id: " + json.dumps(sid) + ",\n"
        "    close: function(){ parent.postMessage({__ambient_close: true}, '*'); }};\n"
        "  window.midum.close = window.midum.ambient.close;\n"
        "})();\n</script>"
    )


# ── The trusted host page for every surface ─────────────────────────────────
# NOTE: placeholders (__X__) are substituted with str.replace, not str.format,
# because the CSS/JS below is full of braces.
_SHELL_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__WINDOW_TITLE__</title>
<style>
:root{ --R:48px; --BTN:15px; --OFF:0px; --CARD-BG:__CARD_BG__; }
*{ box-sizing:border-box; }
html,body{
  margin:0; padding:0; width:100%; height:100%; overflow:hidden;
  background:transparent;                 /* window itself is transparent */
  user-select:none; -webkit-user-select:none;
}
/* The rounded rectangle. clip-path + overflow both clip the iframe to the
   rounded corners (clip-path is the one Chromium honours reliably for
   iframes). Everything outside this shape is fully transparent. */
#card{
  position:absolute; left:0; top:0; right:0; bottom:0;
  border-radius:var(--R); overflow:hidden;
  clip-path:inset(0 round var(--R));
  background:var(--CARD-BG);
}
#frame{
  position:absolute; left:0; top:0; width:100%; height:100%;
  border:0; display:block; background:transparent;
}
#ring{                                     /* hairline edge, above the iframe */
  position:absolute; left:0; top:0; right:0; bottom:0; z-index:2;
  border-radius:var(--R); pointer-events:none;
  box-shadow:inset 0 0 0 1px rgba(127,127,127,.38);
}
/* Circular close button, in the corner the rounding cuts away. --BTN and
   --OFF are set by layout() so it is tangent to (never overlapping) the
   card's rounded corner and always inside the window's bounding box. */
#close{
  position:absolute; top:var(--OFF); right:var(--OFF);
  width:var(--BTN); height:var(--BTN); z-index:5;
  margin:0; padding:0; border:0; border-radius:50%; cursor:pointer;
  display:flex; align-items:center; justify-content:center;
  background:#1c1f27; color:#fff;
  box-shadow:0 0 0 1px rgba(255,255,255,.6), 0 1px 3px rgba(0,0,0,.5);
  transition:background .12s ease;
}
#close::before{                            /* forgiving invisible hit area */
  content:""; position:absolute; left:-7px; top:-7px; right:-7px; bottom:-7px;
  border-radius:50%;
}
#close:hover{ background:#e5484d; }
#close:active{ background:#c93b40; }
#close svg{
  width:56%; height:56%; pointer-events:none;
  stroke:currentColor; stroke-width:2.8; stroke-linecap:round; fill:none;
}
</style>
</head>
<body>
<div id="card">
  <iframe id="frame"
          sandbox="allow-scripts allow-forms allow-popups allow-modals"
          referrerpolicy="strict-origin-when-cross-origin"></iframe>
  <div id="ring"></div>
</div>
<button id="close" type="button" aria-label="Close" title="Close">
  <svg viewBox="0 0 10 10" aria-hidden="true"><path d="M2 2 L8 8 M8 2 L2 8"/></svg>
</button>

<script id="ambient-init" type="application/json">__INIT_JSON__</script>
<script>
(function(){
  "use strict";
  var INIT  = JSON.parse(document.getElementById("ambient-init").textContent);
  var frame = document.getElementById("frame");
  var root  = document.documentElement;

  // ── Geometry: rounded corner + close button that fits the cut-off corner ──
  // Card corner arc centre A = (W-R, R). A button of radius r placed on the
  // diagonal from A towards the sharp corner, tangent to the arc with a 1.5px
  // gap, has its centre at distance (R+r+gap) from A, i.e. at
  //     d = R - (R+r+gap)/sqrt(2)
  // from both the top and right edges of the window. Largest r that still
  // keeps the whole button inside the bounding box is where d == r.
  function layout(){
    var m   = Math.min(window.innerWidth, window.innerHeight);
    var R   = INIT.radius > 0 ? INIT.radius : 48;
    R = Math.max(Math.min(40, m / 2), Math.min(R, m / 2));
    var S = Math.SQRT1_2, gap = 1.5;
    var rMax = (R * (1 - S) - gap * S) / (1 + S);
    var r    = Math.max(3, Math.min(rMax, 10));
    var d    = R - (R + r + gap) * S;
    root.style.setProperty("--R",   R + "px");
    root.style.setProperty("--BTN", (2 * r) + "px");
    root.style.setProperty("--OFF", Math.max(0, d - r) + "px");
  }
  window.addEventListener("resize", layout);
  layout();

  // ── pywebview bridge (the window's own js_api = _AmbientBridge) ──────────
  var queue = [];
  function api(){ return window.pywebview && window.pywebview.api; }
  function whenReady(fn){ if (api()) fn(); else queue.push(fn); }
  function flush(){ if (!api()) return false; queue.splice(0).forEach(function(f){ f(); }); return true; }
  window.addEventListener("pywebviewready", flush);
  var poll = setInterval(function(){ if (flush() && queue.length === 0) clearInterval(poll); }, 100);
  setTimeout(function(){ clearInterval(poll); }, 15000);

  function reply(msg){ try { frame.contentWindow.postMessage(msg, "*"); } catch(_){} }
  function closeMe(){ whenReady(function(){ api().ambient_close(); }); }

  document.getElementById("close").addEventListener("click", closeMe);
  document.addEventListener("contextmenu", function(e){ e.preventDefault(); });

  // Messages from the sandboxed iframe (the model's surface). Only the live
  // iframe is trusted as a source.
  window.addEventListener("message", function(e){
    if (!frame.contentWindow || e.source !== frame.contentWindow) return;
    var d = e.data;
    if (d && d.__midum_call === true){
      whenReady(function(){
        api().ambient_call(String(d.tool || ""), d.args || {})
          .then(function(result){ reply({__midum_call_result: true, id: d.id, result: result}); })
          .catch(function(err){ reply({__midum_call_result: true, id: d.id, error: String((err && err.message) || err)}); });
      });
      return;
    }
    if (d && d.__ambient_close === true){ closeMe(); return; }
    whenReady(function(){ api().ambient_submit(d); });   // free-form input -> chat turn
  });

  // Called by Python (evaluate_js) on show/edit.
  window.__ambientSetCode = function(code){
    frame.srcdoc = INIT.bridge + (code || "");
  };
  window.__ambientSetCode(INIT.code);
})();
</script>
</body>
</html>
"""


def _build_shell_html(sid: str, window_title: str, code: str,
                      radius: float, background: str) -> str:
    init = {"id": sid, "code": code, "bridge": _bridge_html(sid),
            "radius": float(radius or 0)}
    title = (window_title.replace("&", "&amp;")
                         .replace("<", "&lt;").replace(">", "&gt;"))
    return (_SHELL_TEMPLATE
            .replace("__WINDOW_TITLE__", title)
            .replace("__CARD_BG__", background)
            .replace("__INIT_JSON__", _json_for_html(init)))


# ── Windows-only native helpers ─────────────────────────────────────────────
def _find_hwnd(title: str):
    """This process's top-level window whose title is exactly `title` (no
    visibility requirement -- the window may still be showing up)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        kernel32 = ctypes.WinDLL("kernel32")
        pid = kernel32.GetCurrentProcessId()
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            wpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid:
                n = user32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    if buf.value == title:
                        found.append(hwnd)
            return True

        user32.EnumWindows(_cb, 0)
        return found[0] if found else None
    except Exception:
        return None


_place_fn = None


def _win_place(hwnd, rect) -> bool:
    """Cheap per-frame reposition/resize (no style or affinity changes) used
    by the move animation. Keeps z-order and never activates the window."""
    global _place_fn
    try:
        if _place_fn is None:
            import ctypes
            from ctypes import wintypes
            fn = ctypes.WinDLL("user32").SetWindowPos
            fn.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, wintypes.UINT]
            fn.restype = wintypes.BOOL
            _place_fn = fn
        SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
        x, y, w, h = rect
        return bool(_place_fn(hwnd, None, int(x), int(y), int(w), int(h),
                              SWP_NOZORDER | SWP_NOACTIVATE))
    except Exception:
        return False


def _ease_in_out_cubic(t: float) -> float:
    return 4 * t * t * t if t < 0.5 else 1 - ((-2 * t + 2) ** 3) / 2


def _win_set_capture(hwnd, capture_visible: bool) -> bool:
    """Toggle only the screenshot exclusion on an existing window."""
    try:
        import ctypes
        from ctypes import wintypes
        WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11
        fn = ctypes.WinDLL("user32").SetWindowDisplayAffinity
        fn.argtypes = [wintypes.HWND, wintypes.DWORD]
        fn.restype = wintypes.BOOL
        return bool(fn(hwnd, WDA_NONE if capture_visible else WDA_EXCLUDEFROMCAPTURE))
    except Exception:
        return False  # Windows < 10 2004: widget simply stays capturable


def _win_apply(hwnd, rect, focusable: bool, capture_visible: bool) -> bool:
    """Place the window in exact physical pixels, always on top, and apply
    the tool-window / no-activate / capture-exclusion styles."""
    try:
        import ctypes
        from ctypes import wintypes
        # A private WinDLL (not ctypes.windll.user32) so setting argtypes here
        # can't change how the rest of the process calls these same functions.
        user32 = ctypes.WinDLL("user32")

        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00000080, 0x00040000
        WS_EX_NOACTIVATE = 0x08000000
        SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x0010, 0x0020, 0x0040
        HWND_TOPMOST = -1
        WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11

        get_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        set_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        get_long.restype = ctypes.c_ssize_t
        set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_long.restype = ctypes.c_ssize_t

        ex = get_long(hwnd, GWL_EXSTYLE)
        ex = (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW   # no taskbar/Alt-Tab
        ex = (ex & ~WS_EX_NOACTIVATE) if focusable else (ex | WS_EX_NOACTIVATE)
        set_long(hwnd, GWL_EXSTYLE, ex)

        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                        ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SetWindowPos.restype = wintypes.BOOL
        x, y, w, h = rect
        ok = bool(user32.SetWindowPos(
            hwnd, HWND_TOPMOST, int(x), int(y), int(w), int(h),
            SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW))

        try:
            user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
            user32.SetWindowDisplayAffinity(
                hwnd, WDA_NONE if capture_visible else WDA_EXCLUDEFROMCAPTURE)
        except Exception:
            pass  # Windows < 10 2004: widget simply stays capturable
        return ok
    except Exception:
        return False


# ── Per-surface records ─────────────────────────────────────────────────────
class _Surface:
    """Everything the manager tracks about one open widget."""

    def __init__(self, sid, title, code, rect, radius, background,
                 focusable, capture_visible):
        self.sid = sid
        self.title = title
        self.code = code
        self.rect = rect                      # (x, y, w, h) in real screen px
        self.radius = radius
        self.background = background
        self.focusable = focusable
        self.capture_visible = capture_visible
        self.window = None                    # pywebview Window
        self.window_title = f"MidumAmbient-{sid}-{uuid.uuid4().hex[:8]}"
        self.closed = False
        self.timer = None
        self.opened_at = time.time()
        self.hwnd = None            # native handle once found (Windows)
        self.cur_rect = rect        # where the window actually is right now
        self.anim_id = 0            # bumped per move animation; older ones bail out

    @property
    def style_key(self):
        return (self.radius, self.background, self.focusable, self.capture_visible)


class _AmbientBridge:
    """
    The js_api object handed to ONE surface's window (pywebview exposes its
    public methods as window.pywebview.api.*). Every attribute is underscore-
    prefixed on purpose: pywebview walks a js_api's public attributes to
    build the JS proxy, and walking into the manager/window objects would be
    slow at best and recursive at worst.
    """

    def __init__(self, manager, sid):
        self._m = manager
        self._sid = sid

    def ambient_call(self, tool, args=None):
        return self._m._on_call(self._sid, tool, args)

    def ambient_submit(self, data=None):
        return self._m._on_submit(self._sid, data)

    def ambient_close(self):
        # Off-thread: destroying a window from inside its own js_api call
        # would otherwise deadlock waiting on that very call to return.
        threading.Thread(target=self._m._user_close, args=(self._sid,),
                         daemon=True).start()
        return {"ok": True}


# ── The manager ─────────────────────────────────────────────────────────────
class AmbientSurfaceManager:
    """
    Owns every open Ambient Surface window.

    Callbacks supplied by gui/app.py's Api (all optional):
      call_tool(surface_id, tool_name, args) -> result   # midum.call(...)
      on_submit(surface_id, data)            -> result   # parent.postMessage
      on_event(text)                                     # log line for the GUI
    """

    def __init__(self, call_tool=None, on_submit=None, on_event=None):
        self._call_tool_cb = call_tool
        self._on_submit_cb = on_submit
        self._on_event_cb = on_event
        self._lock = threading.RLock()
        self._surfaces = {}        # sid -> _Surface, in opening order
        self._auto_id = 0
        self._dismissed = []       # [(sid, title, when)], newest last, max 10
        # Global SETTINGS toggle ("Hide ambient surfaces from screenshots").
        # On (default): surfaces are excluded from the model's screenshots /
        # OCR / screen-share unless one asked for capture_visible=True.
        # Off: every surface is capturable, whatever the model requested.
        self._hide_from_capture = True

    # ── Screenshot-exclusion setting ─────────────────────────────────────────
    def _capture_visible_for(self, rec) -> bool:
        """Effective 'visible to screenshots' for one surface: the surface's
        own capture_visible request, or everything when the global hide
        toggle is off."""
        return bool(rec.capture_visible or not self._hide_from_capture)

    def set_hide_from_capture(self, enabled) -> None:
        """Apply the SETTINGS toggle. Takes effect immediately on every open
        surface (display affinity only -- no move, restyle or flicker)."""
        self._hide_from_capture = _truthy(enabled)
        with self._lock:
            recs = list(self._surfaces.values())
        for rec in recs:
            if rec.closed:
                continue
            hwnd = rec.hwnd or (_find_hwnd(rec.window_title)
                                if sys.platform == "win32" else None)
            if hwnd:
                rec.hwnd = hwnd
                _win_set_capture(hwnd, self._capture_visible_for(rec))

    # ── Entry point used by tools/ambient_surface_tools.py ───────────────────
    def handle(self, kind: str, payload: dict) -> str:
        handlers = {
            "show": self._show, "edit": self._edit, "move": self._move,
            "close": self._close, "list": self._list, "code": self._code,
            "save": self._save, "load": self._load,
        }
        fn = handlers.get(kind)
        if fn is None:
            return f"[AMBIENT SURFACE ERROR] unknown action '{kind}'."
        if webview is None:
            return "[AMBIENT SURFACE ERROR] pywebview is not available."
        try:
            return fn(**(payload or {}))
        except ValueError as e:
            return f"[AMBIENT SURFACE ERROR] {e}"

    # ── Geometry ─────────────────────────────────────────────────────────────
    @staticmethod
    def _canvas_of(rect):
        x, y, w, h = rect
        sx, sy = float(config.SCALE_X), float(config.SCALE_Y)
        return (int(round(x / sx)), int(round(y / sy)),
                int(round(w / sx)), int(round(h / sy)))

    def _to_screen_rect(self, x, y, w, h, space, current=None):
        """
        Convert model-supplied x/y/width/height (any may be None when
        `current` -- a real-pixel rect -- is given to fall back to) into a
        clamped real-pixel rect. Returns (rect, adjusted: bool).
        """
        space = (space or "canvas").strip().lower()
        if space not in ("canvas", "screen"):
            raise ValueError("coordinate_space must be 'canvas' or 'screen'.")
        kx = float(config.SCALE_X) if space == "canvas" else 1.0
        ky = float(config.SCALE_Y) if space == "canvas" else 1.0

        # x / y may be a named anchor instead of a number, resolved against the
        # REAL screen size below so the model never has to know it:
        #   x: "left" | "center" | "right"      y: "top" | "center" | "bottom"
        x_anchor = y_anchor = None
        if isinstance(x, str) and x.strip().lower() in ("left", "center", "centre", "right"):
            x_anchor, x = x.strip().lower(), None
        if isinstance(y, str) and y.strip().lower() in ("top", "center", "centre", "bottom"):
            y_anchor, y = y.strip().lower(), None

        cur = current or (None, None, None, None)
        vals = []
        for v, name, k, c, anch in ((x, "x", kx, cur[0], x_anchor),
                                    (y, "y", ky, cur[1], y_anchor),
                                    (w, "width", kx, cur[2], None),
                                    (h, "height", ky, cur[3], None)):
            if v is None:
                if anch is not None:
                    vals.append(0.0)          # filled in once the size is known
                elif c is None:
                    raise ValueError(f"'{name}' is required.")
                else:
                    vals.append(float(c))
            else:
                vals.append(_num(v, name) * k)
        nx, ny, nw, nh = vals

        sw, sh = int(config.SCREEN_W), int(config.SCREEN_H)
        cw = int(round(max(MIN_W, min(sw, nw))))
        ch = int(round(max(MIN_H, min(sh, nh))))
        if x_anchor is not None:
            nx = 0 if x_anchor == "left" else (sw - cw if x_anchor == "right" else (sw - cw) / 2)
        if y_anchor is not None:
            ny = 0 if y_anchor == "top" else (sh - ch if y_anchor == "bottom" else (sh - ch) / 2)
        cx, cy = int(round(nx)), int(round(ny))
        # keep it fully on the primary screen (both coordinate spaces)
        cx = max(0, min(sw - cw, cx))
        cy = max(0, min(sh - ch, cy))
        adjusted = (abs(cw - nw) > 1 or abs(ch - nh) > 1
                    or abs(cx - nx) > 1 or abs(cy - ny) > 1)
        return (cx, cy, cw, ch), adjusted

    def _describe(self, rec, adjusted=False):
        cx, cy, cw, ch = self._canvas_of(rec.rect)
        x, y, w, h = rec.rect
        s = (f"canvas ({cx}, {cy}) size {cw}x{ch} "
             f"[screen px ({x}, {y}) size {w}x{h}] -- screen is "
             f"{int(config.SCREEN_W)}x{int(config.SCREEN_H)} px, canvas is "
             f"{int(config.MODEL_CANVAS_W)}x{int(config.MODEL_CANVAS_H)}")
        if adjusted:
            s += " -- adjusted to fit the screen / minimum size"
        return s

    # ── Lookup helpers ───────────────────────────────────────────────────────
    def _get(self, surface_id):
        sid = _clean_id(surface_id)
        with self._lock:
            if sid:
                rec = self._surfaces.get(sid)
                if rec is None:
                    open_ids = ", ".join(self._surfaces) or "none"
                    raise ValueError(
                        f"No open Ambient Surface with id '{sid}' (the user may "
                        f"have dismissed it). Open: {open_ids}.")
                return rec
            if len(self._surfaces) == 1:
                return next(iter(self._surfaces.values()))
            if not self._surfaces:
                raise ValueError("No Ambient Surface is open.")
            raise ValueError("Several Ambient Surfaces are open -- pass "
                             "surface_id. Open: " + ", ".join(self._surfaces))

    def _log(self, text):
        if self._on_event_cb:
            try:
                self._on_event_cb(text)
            except Exception:
                pass

    # ── show ─────────────────────────────────────────────────────────────────
    def _show(self, code, x, y, width, height, surface_id="", title="",
              corner_radius=0, background="", coordinate_space="canvas",
              focusable=False, capture_visible=False, auto_close_seconds=0):
        code = code or ""
        if not code.strip():
            raise ValueError("'code' must be a non-empty HTML document.")

        rect, adjusted = self._to_screen_rect(x, y, width, height, coordinate_space)

        radius = 0.0
        if corner_radius not in (None, "", 0):
            radius = max(0.0, _num(corner_radius, "corner_radius"))
        bg = (background or "").strip()
        if bg and not _SAFE_COLOR_RE.match(bg):
            raise ValueError(f"'background' isn't a plain CSS colour: {bg!r}")
        bg = bg or "#ffffff"
        focusable = _truthy(focusable)
        capture_visible = _truthy(capture_visible)
        try:
            auto_close = max(0.0, float(auto_close_seconds or 0))
        except (TypeError, ValueError):
            raise ValueError("'auto_close_seconds' must be a number.")
        title = (title or "").strip()[:80]

        sid = _clean_id(surface_id)
        with self._lock:
            if not sid:
                while True:
                    self._auto_id += 1
                    sid = f"ambient{self._auto_id}"
                    if sid not in self._surfaces:
                        break
            existing = self._surfaces.get(sid)
            if existing is None and len(self._surfaces) >= MAX_SURFACES:
                raise ValueError(
                    f"Already {MAX_SURFACES} Ambient Surfaces open -- close one "
                    f"first. Open: {', '.join(self._surfaces)}.")

        new_style = (radius, bg, focusable, capture_visible)

        # Same id, same look -> update in place (no flicker).
        if existing is not None and existing.style_key == new_style and not existing.closed:
            existing.code = code
            existing.title = title or existing.title
            existing.rect = rect
            self._cancel_timer(existing)
            self._eval(existing, f"window.__ambientSetCode({json.dumps(code)})")
            self._apply_rect(existing)
            self._arm_timer(existing, auto_close)
            return (f"Ambient surface '{sid}' updated in place at "
                    f"{self._describe(existing, adjusted)}.")

        # Same id, different look (or gone) -> replace.
        if existing is not None:
            self._discard(existing)

        rec = _Surface(sid, title, code, rect, radius, bg, focusable, capture_visible)
        shell = _build_shell_html(sid, rec.window_title, code, radius, bg)
        self._create_window(rec, shell)
        with self._lock:
            self._surfaces[sid] = rec
        self._arm_timer(rec, auto_close)
        threading.Thread(target=self._finish_window, args=(rec,), daemon=True).start()
        note = f" It will dismiss itself after {auto_close:g}s." if auto_close > 0 else ""
        return (f"Ambient surface '{sid}' shown at "
                f"{self._describe(rec, adjusted)}.{note} The user can dismiss it "
                f"with its ✕ button.")

    def _create_window(self, rec, shell_html):
        _use_private_profile_for_child_windows()
        x, y, w, h = rec.rect
        kwargs = dict(
            html=shell_html,
            js_api=_AmbientBridge(self, rec.sid),
            x=x, y=y, width=w, height=h,
            frameless=True, transparent=True, on_top=True,
            easy_drag=False, resizable=True,
            min_size=_INITIAL_WINDOW_MIN,
            focus=rec.focusable,
        )
        try:
            window = webview.create_window(rec.window_title, **kwargs)
        except TypeError:
            kwargs.pop("focus", None)      # pywebview < 4.x has no `focus`
            window = webview.create_window(rec.window_title, **kwargs)
        rec.window = window
        try:
            window.events.closed += lambda: self._on_window_closed(rec)
        except Exception:
            pass

    def _finish_window(self, rec):
        """Once the native window exists, place it in exact physical pixels
        and apply the overlay styles. Runs off-thread; applied twice because
        Qt occasionally re-applies its own geometry right after first show."""
        applied = False
        if sys.platform == "win32":
            deadline = time.time() + 8.0
            hwnd = None
            while time.time() < deadline and not rec.closed:
                hwnd = _find_hwnd(rec.window_title)
                if hwnd:
                    break
                time.sleep(0.05)
            if hwnd and not rec.closed:
                rec.hwnd = hwnd
                applied = _win_apply(hwnd, rec.rect, rec.focusable,
                                     self._capture_visible_for(rec))
                if applied:
                    rec.cur_rect = rec.rect
                time.sleep(0.4)
                # Skip the re-assert if a move animation already started.
                if not rec.closed and rec.anim_id == 0:
                    _win_apply(hwnd, rec.rect, rec.focusable,
                               self._capture_visible_for(rec))
        if not applied and not rec.closed:
            self._pywebview_place(rec)

    def _pywebview_place(self, rec):
        try:
            x, y, w, h = rec.rect
            rec.window.resize(w, h)
            rec.window.move(x, y)
        except Exception:
            pass

    def _apply_rect(self, rec):
        """Instant placement."""
        hwnd = rec.hwnd or (_find_hwnd(rec.window_title) if sys.platform == "win32" else None)
        if hwnd and _win_apply(hwnd, rec.rect, rec.focusable,
                               self._capture_visible_for(rec)):
            rec.hwnd = hwnd
            rec.cur_rect = rec.rect
            return
        self._pywebview_place(rec)
        rec.cur_rect = rec.rect

    def _animate(self, rec, a, b, ms):
        """Glide the window from rect `a` to rect `b` (real px) over `ms` ms,
        time-based ease-in-out so it stays smooth even if frames are slow.
        Blocks the calling tool thread until done. A newer move on the same
        surface bumps rec.anim_id, which makes this loop exit and lets the
        new animation continue from wherever the window currently is."""
        rec.anim_id += 1
        my_id = rec.anim_id
        hwnd = rec.hwnd or (_find_hwnd(rec.window_title) if sys.platform == "win32" else None)
        if hwnd:
            rec.hwnd = hwnd
        dur = max(0.05, ms / 1000.0)
        t0 = time.perf_counter()
        while True:
            if rec.closed or rec.anim_id != my_id:
                return
            t = min(1.0, (time.perf_counter() - t0) / dur)
            e = _ease_in_out_cubic(t)
            cur = tuple(int(round(a[i] + (b[i] - a[i]) * e)) for i in range(4))
            if t >= 1.0:
                cur = tuple(int(v) for v in b)
            if hwnd:
                _win_place(hwnd, cur)
            else:
                try:
                    rec.window.resize(cur[2], cur[3])
                    rec.window.move(cur[0], cur[1])
                except Exception:
                    pass
            rec.cur_rect = cur
            if t >= 1.0:
                return
            time.sleep(0.006)

    def _eval(self, rec, js):
        try:
            rec.window.evaluate_js(js)
        except Exception:
            pass

    # ── edit ─────────────────────────────────────────────────────────────────
    def _edit(self, old_code, new_code, surface_id=""):
        rec = self._get(surface_id)
        old = old_code or ""
        new = new_code or ""
        if not old:
            raise ValueError("'old_code' must be a non-empty exact snippet.")
        count = rec.code.count(old)
        if count == 0:
            raise ValueError(
                f"old_code was not found in ambient surface '{rec.sid}' -- copy "
                f"it exactly, character for character, from what you last wrote.")
        if count > 1:
            raise ValueError(
                f"old_code matches {count} locations in '{rec.sid}' -- include a "
                f"little more surrounding context so it's unique.")
        rec.code = rec.code.replace(old, new, 1)
        self._eval(rec, f"window.__ambientSetCode({json.dumps(rec.code)})")
        return f"Ambient surface '{rec.sid}' updated."

    # ── move / resize ────────────────────────────────────────────────────────
    def _move(self, surface_id="", x=None, y=None, width=None, height=None,
              coordinate_space="canvas", duration_ms=None):
        if x is None and y is None and width is None and height is None:
            raise ValueError("Pass at least one of x, y, width, height.")
        rec = self._get(surface_id)
        if duration_ms in (None, ""):
            ms = DEFAULT_MOVE_MS
        else:
            ms = int(max(0.0, min(float(MAX_MOVE_MS), _num(duration_ms, "duration_ms"))))
        rect, adjusted = self._to_screen_rect(x, y, width, height,
                                              coordinate_space, current=rec.rect)
        start = rec.cur_rect or rec.rect
        rec.rect = rect                 # target; list/further moves use this
        if ms <= 0 or tuple(start) == tuple(rect):
            self._apply_rect(rec)       # instant (the shell re-runs layout() itself)
            how = "instantly"
        else:
            self._animate(rec, tuple(start), tuple(rect), ms)
            how = f"with a {ms} ms glide"
        return f"Ambient surface '{rec.sid}' moved {how} to {self._describe(rec, adjusted)}."

    # ── close / list ─────────────────────────────────────────────────────────
    def _close(self, surface_id=""):
        raw = str(surface_id or "").strip()
        if raw.lower() == "all":
            with self._lock:
                recs = list(self._surfaces.values())
            for r in recs:
                self._discard(r)
            return (f"Closed {len(recs)} Ambient Surface(s)." if recs
                    else "No Ambient Surfaces were open.")
        with self._lock:
            if not raw:
                if not self._surfaces:
                    return "No Ambient Surface is open."
                rec = list(self._surfaces.values())[-1]
            else:
                rec = self._surfaces.get(_clean_id(raw))
                if rec is None:
                    return (f"No open Ambient Surface '{raw}' -- it may already have "
                            f"been dismissed. Open: {', '.join(self._surfaces) or 'none'}.")
        self._discard(rec)
        return f"Ambient surface '{rec.sid}' closed."

    def _code(self, surface_id=""):
        """Read-only: a widget's current source (voice-mode delegation uses
        this to hand the coding model the code it should revise)."""
        return self._get(surface_id).code

    def _save(self, name, surface_id=""):
        """Snapshot an open widget (code + look + canvas position/size) to
        storage/ambient_surfaces/<name>.json -- the Ambient twin of the
        Render Surface 'save' action."""
        name = (name or "").strip()
        if not name:
            raise ValueError("'name' is required to save.")
        rec = self._get(surface_id)
        cx, cy, cw, ch = self._canvas_of(rec.rect)
        data = {
            "name": name,
            "title": rec.title or name,
            "code": rec.code,
            "x": cx, "y": cy, "width": cw, "height": ch,
            "coordinate_space": "canvas",
            "corner_radius": rec.radius,
            "background": rec.background,
            "focusable": rec.focusable,
            "capture_visible": rec.capture_visible,
            "saved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        try:
            os.makedirs(_ambient_tools.AMBIENT_SURFACES_DIR, exist_ok=True)
            with open(_ambient_tools._saved_ambient_path(name), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            raise ValueError(f"Could not save '{name}': {e}")
        self._log(f"\U0001F4BE Ambient surface saved as '{name}'.\n")
        return f"Ambient surface '{rec.sid}' saved as '{name}'."

    def _load(self, name, surface_id="", x=None, y=None, width=None,
              height=None, coordinate_space="canvas"):
        """Reopen a saved widget. Saved geometry is canvas-space; any of
        x/y/width/height passed in override just those fields."""
        name = (name or "").strip()
        if not name:
            raise ValueError("'name' is required to load.")
        path = _ambient_tools._saved_ambient_path(name)
        if not os.path.exists(path):
            raise ValueError(f"No saved Ambient Surface named '{name}'.")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise ValueError(f"Could not read saved surface '{name}': {e}")
        code = data.get("code") or ""
        if not code.strip():
            raise ValueError(f"Saved surface '{name}' has no code.")

        space = (coordinate_space or "canvas").strip().lower()
        if space not in ("canvas", "screen"):
            raise ValueError("coordinate_space must be 'canvas' or 'screen'.")
        # Saved values are canvas units; convert to the requested space for
        # any field the caller didn't override.
        kx = 1.0 if space == "canvas" else float(config.SCALE_X)
        ky = 1.0 if space == "canvas" else float(config.SCALE_Y)

        def pick(override, key, k):
            if override not in (None, ""):
                return override
            if data.get(key) is None:
                raise ValueError(f"Saved surface '{name}' has no '{key}'; pass it explicitly.")
            return data[key] * k

        result = self._show(
            code=code,
            x=pick(x, "x", kx), y=pick(y, "y", ky),
            width=pick(width, "width", kx), height=pick(height, "height", ky),
            surface_id=surface_id or name,
            title=data.get("title") or name,
            corner_radius=data.get("corner_radius") or 0,
            background=data.get("background") or "",
            coordinate_space=space,
            focusable=data.get("focusable", False),
            capture_visible=data.get("capture_visible", False),
            auto_close_seconds=0,
        )
        return f"Loaded saved Ambient Surface '{name}'. {result}"

    def _list(self):
        with self._lock:
            recs = list(self._surfaces.values())
            dismissed = list(self._dismissed)
        lines = []
        if not recs:
            lines.append("No Ambient Surfaces are open.")
        else:
            lines.append(f"Open Ambient Surfaces ({len(recs)}):")
            for r in recs:
                label = f' "{r.title}"' if r.title else ""
                lines.append(f"- {r.sid}{label}: {self._describe(r)}, "
                             f"{len(r.code)} chars of code")
        if dismissed:
            recent = ", ".join(sid for sid, _t, _w in dismissed[-5:])
            lines.append(f"Recently dismissed by the user: {recent}.")
        return "\n".join(lines)

    # ── Teardown ─────────────────────────────────────────────────────────────
    def _cancel_timer(self, rec):
        if rec.timer is not None:
            try:
                rec.timer.cancel()
            except Exception:
                pass
            rec.timer = None

    def _arm_timer(self, rec, seconds):
        self._cancel_timer(rec)
        if seconds and seconds > 0:
            t = threading.Timer(seconds, self._auto_close, args=(rec,))
            t.daemon = True
            rec.timer = t
            t.start()

    def _auto_close(self, rec):
        if not rec.closed:
            self._discard(rec)

    def _discard(self, rec):
        """Remove from the registry and destroy the window. Safe to repeat."""
        with self._lock:
            if self._surfaces.get(rec.sid) is rec:
                del self._surfaces[rec.sid]
        rec.closed = True
        self._cancel_timer(rec)
        if rec.window is not None:
            try:
                rec.window.destroy()
            except Exception:
                pass

    def _user_close(self, sid):
        """The user clicked the ✕ button."""
        with self._lock:
            rec = self._surfaces.get(sid)
        if rec is None:
            return
        self._discard(rec)
        with self._lock:
            self._dismissed.append((rec.sid, rec.title, time.time()))
            del self._dismissed[:-10]
        self._log(f"\U0001FA9F Ambient surface '{rec.sid}' dismissed by the user.\n")

    def _on_window_closed(self, rec):
        """pywebview 'closed' event -- window went away by some other route."""
        rec.closed = True
        self._cancel_timer(rec)
        with self._lock:
            if self._surfaces.get(rec.sid) is rec:
                del self._surfaces[rec.sid]

    def close_all(self):
        """Called on app shutdown -- pywebview keeps the process alive while
        ANY window is open, so leftover overlays must not outlive the app."""
        with self._lock:
            recs = list(self._surfaces.values())
        for r in recs:
            try:
                self._discard(r)
            except Exception:
                pass

    # ── Callbacks from a surface's window ────────────────────────────────────
    def _on_call(self, sid, tool, args):
        if self._call_tool_cb is None:
            return "Error: tool calls from Ambient Surfaces are not wired up."
        return self._call_tool_cb(sid, tool, args)

    def _on_submit(self, sid, data):
        if self._on_submit_cb is None:
            return {"ok": False, "error": "no_handler"}
        return self._on_submit_cb(sid, data)


# =============================================================================
