# --- Midum GUI v2 — Chromium (pywebview/WebView2) UI, animated rounded panes ---
import os
import sys

# Declare DPI awareness BEFORE any window/webview/Qt object is created.
# Rationale: when run as `python gui/app.py`, python.exe ships a manifest
# that already marks the process DPI-aware, so Windows lets it render at
# native resolution on scaled displays. A PyInstaller-frozen .exe has no
# such manifest, so Windows falls back to treating it as DPI-unaware and
# bitmap-stretches the whole rendered window to fit the (smaller) virtual
# resolution it assumes -- which is exactly what produces a GUI that's
# shrunk/deformed and shoved into a corner on any monitor running above
# 100% scaling. Setting this explicitly at startup opts the frozen exe
# into the same per-monitor-DPI-aware behavior python.exe gets for free.
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # Windows 7/8 fallback
        except Exception:
            pass

# gui/app.py -> parent: midum_pkg (package root)
_GUI_DIR  = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_GUI_DIR)
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)
if _GUI_DIR not in sys.path:
    sys.path.insert(0, _GUI_DIR)

# Window/titlebar icon -- drop an icon.ico (preferred on Windows, supports
# multiple embedded resolutions) or icon.png here and it's picked up
# automatically on next launch, no code changes needed.
_ASSETS_DIR = os.path.join(_GUI_DIR, "assets")
os.makedirs(_ASSETS_DIR, exist_ok=True)
_ICON_CANDIDATES = [os.path.join(_ASSETS_DIR, name) for name in ("icon.ico", "icon.png")]

import threading
import datetime
import queue
import json
import time
import base64
import traceback
import subprocess
import re
import uuid

import webview  # pywebview — renders through the OS Chromium engine (WebView2 on
                 # Windows, WebKitGTK on Linux, WKWebView on macOS). Replaces the
                 # previous customtkinter/Tkinter shell entirely.

# Pygments powers server-side syntax highlighting for ```-fenced code blocks
# in the chat (see Api.highlight_code / Api.get_pygments_css below). Guarded
# import so a missing/broken install degrades to the plain unhighlighted
# code blocks the GUI already rendered before this feature existed, instead
# of crashing the whole app on startup.
try:
    from pygments import highlight as _pygments_highlight
    from pygments.lexers import get_lexer_by_name as _pygments_get_lexer_by_name, guess_lexer as _pygments_guess_lexer
    from pygments.formatters import HtmlFormatter as _PygmentsHtmlFormatter
    from pygments.util import ClassNotFound as _PygmentsClassNotFound
    _PYGMENTS_OK = True
except Exception:
    _PYGMENTS_OK = False

from gui.chat_store import ChatStore, MidumSession
from gui.dispatch import _dispatch_midum_tool
from flows import classify_tool_kind
import knowledge_base as _knowledge_base
import concurrent.futures
import tools.user_prompt_tools as _user_prompt_tools
import tools.render_surface_tools as _render_surface_tools
import tools.ambient_surface_tools as _ambient_surface_tools
from gui.ambient_surface import AmbientSurfaceManager

import config
import main as midum
import permissions
import artifacts
import midumdocs
from gui.editor_backend import EditorMixin
import midum_mcp.filesystem_policy as fs_policy

CHATS_DIR = os.path.join(midum.STORAGE_DIR, "chats")

# Snapshot of which conversation tabs were open (and which was active) the
# last time Midum closed -- see Api._persist_open_tabs / _restore_tabs_or_start_fresh.
# Stores each open tab's PERSISTED chat id (in tab order), not its in-RAM
# conversation_tabs.py tab id, which is just a per-process counter and
# means nothing after a restart.
OPEN_TABS_FILE = os.path.join(midum.STORAGE_DIR, "open_tabs.json")

_SAY_TAG = "\x02MIDUM_SAY\x02"

# Only lines that represent an actual tool invocation should light up the
# pulsing dot. "-> Executing: '<tool_name>'" is printed exactly once per
# real tool call (see orchestration.py's process_chat_turn); the various
# emoji-prefixed status/log lines used throughout startup and elsewhere
# are NOT tool calls and must not trigger the dot, even though several of
# them happen to share emoji with tool-related output.
_TOOL_LINE_KEYWORDS = (
    "-> executing:",
)


def _is_tool_line(raw_line: str) -> bool:
    line = raw_line.strip()
    if not line:
        return False
    low = line.lower()
    return any(low.startswith(k) for k in _TOOL_LINE_KEYWORDS)


class _StdoutRedirector:
    def __init__(self, callback):
        self._cb = callback
        self._old = sys.stdout

    def write(self, text):
        if text.strip():
            self._cb(text)

    def flush(self):
        pass

    def restore(self):
        sys.stdout = self._old


def _default_model_for_provider(provider_key: str) -> str:
    return {
        "ollama":        midum.config.MODEL_NAME,
        "openrouter":    midum.config.OPENROUTER_MODEL,
        "gemini_web":    midum.config.GEMINI_WEB_MODEL or "(auto)",
        "gemini_api":    midum.config.GEMINI_API_MODEL,
        "groq":          midum.config.GROQ_MODEL,
        "ollama_cloud":  midum.config.OLLAMA_CLOUD_MODEL,
        "litellm":       midum.config.LITELLM_MODEL,
        "openai_web":    "(default)",
    }.get(provider_key, "")


def _known_models_for_provider(provider_key: str) -> list:
    if provider_key == "openrouter":
        return list(dict.fromkeys(midum.config.OPENROUTER_FALLBACK_MODELS))
    if provider_key == "groq":
        return list(dict.fromkeys(midum.config.GROQ_FALLBACK_MODELS))
    if provider_key == "gemini_api":
        return list(dict.fromkeys([midum.config.GEMINI_API_MODEL, "gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]))
    if provider_key == "gemini_web":
        return _list_gemini_web_model_options()
    if provider_key == "ollama_cloud":
        return list(dict.fromkeys(midum.config.OLLAMA_CLOUD_FALLBACK_MODELS))
    if provider_key == "litellm":
        # The GUI shows a dedicated service/model/key picker for LiteLLM
        # (see attachLitellmPicker in the frontend JS) instead of a flat model
        # list -- the choices depend on which service is picked.
        return []
    if provider_key == "openai_web":
        # llm_cookie_bridge talks to the ChatGPT web app's default model for
        # the logged-in account -- the library doesn't expose a model= param
        # to pick a different one, so there's nothing real to list. A single
        # "(default)" entry keeps the Model tab's dropdown looking consistent
        # with every other provider instead of showing an empty field.
        return ["(default)"]
    return [midum.config.MODEL_NAME]


def _list_gemini_web_model_options() -> list:
    """
    Real, current model lineup for the logged-in Gemini web account (via
    gemini_webapi's list_models()), with "(auto)" always offered first so
    the user can go back to auto-selection. Falls back to a small
    hardcoded guess if the account/session can't be reached yet (not
    logged in, library missing, network hiccup, etc) instead of leaving
    the dropdown looking broken.

    This hits the network (lazily initialises the gemini_webapi client/
    session on first call, then calls list_models()) and can take a couple
    of seconds — callers on the GUI thread should use
    _gemini_web_model_options_fast() for an instant response and fetch
    this in the background instead (see Api.select_provider).
    """
    try:
        from providers.gemini_web_backend import list_gemini_web_models
        models = list_gemini_web_models()
    except Exception:
        models = []
    if not models:
        options = _gemini_web_fallback_options()
    else:
        options = list(dict.fromkeys(["(auto)"] + models))
    global _gemini_web_model_options_cache
    _gemini_web_model_options_cache = options
    return options


# Cache of the last real gemini_webapi model lineup fetched (see
# _list_gemini_web_model_options), so a provider switch after the first
# successful fetch can serve instantly from cache instead of paying the
# network round trip again every time.
_gemini_web_model_options_cache: list | None = None


def _gemini_web_fallback_options() -> list:
    """Small hardcoded guess, no network — used before the first real
    fetch has ever completed."""
    fallback = [midum.config.GEMINI_WEB_MODEL, "gemini-3-flash"]
    return list(dict.fromkeys(["(auto)"] + [m for m in fallback if m]))


def _gemini_web_model_options_fast() -> list:
    """
    Instant, non-network model list for Gemini-web: the cached real lineup
    if we've already fetched it once this session, otherwise the small
    hardcoded fallback. Used so switching TO the Gemini-web provider in
    the GUI updates the model field immediately, same as every other
    provider, instead of blocking on a gemini_webapi client init/RPC.
    """
    return _gemini_web_model_options_cache or _gemini_web_fallback_options()


def _list_ollama_cloud_models() -> list:
    try:
        from providers.ollama_cloud_backend import _ollama_cloud_client, _OLLAMA_CLOUD_AVAILABLE
        if not _OLLAMA_CLOUD_AVAILABLE or not _ollama_cloud_client:
            return []
        resp = _ollama_cloud_client.list()
        models = resp.get("models", []) if isinstance(resp, dict) else getattr(resp, "models", [])
        names = []
        for m in models:
            name = m.get("model") or m.get("name") if isinstance(m, dict) else getattr(m, "model", None)
            if name:
                names.append(name)
        return names
    except Exception:
        return []


def _list_ollama_models() -> list:
    try:
        resp = midum.ollama.list()
        models = resp.get("models", []) if isinstance(resp, dict) else getattr(resp, "models", [])
        names = []
        for m in models:
            name = m.get("model") or m.get("name") if isinstance(m, dict) else getattr(m, "model", None)
            if name:
                names.append(name)
        return names
    except Exception:
        return []


PROVIDER_OPTIONS = [
    ("Local (Ollama)", "ollama"),
    ("Ollama Cloud",    "ollama_cloud"),
    ("OpenRouter",      "openrouter"),
    ("Gemini (Web)",    "gemini_web"),
    ("Gemini (API)",    "gemini_api"),
    ("ChatGPT (Web)",   "openai_web"),
    ("Groq",            "groq"),
    ("LiteLLM (any provider)", "litellm"),
]
_PROVIDER_LABEL_TO_KEY = {label: key for label, key in PROVIDER_OPTIONS}
_PROVIDER_KEY_TO_LABEL = {key: label for label, key in PROVIDER_OPTIONS}
DEFAULT_PROVIDER_KEY = "ollama"

# Tabs — "Chat" is the permanent, always-visible pane. Every other entry is
# an auxiliary tool pane that slides in beside it when selected.
# NOTE: "Log" and "Parameters" are intentionally absent here -- they're
# surfaced as togglable buttons inside the Settings panel (frontend JS)
# rather than as tabs, so they don't take up horizontal tab-bar space.
TAB_DEFS = [
    ("Chat",         "💬"),
    ("Model",        "🧬"),
    ("System Core",  "🧠"),
    ("Knowledge",    "📚"),
    ("Skills",       "🛠"),
    ("Tools",        "🔧"),
    ("Flows",        "🔗"),
    ("MCP",          "🔌"),
    ("Permissions",  "🔐"),
]


# =============================================================================
# Custom titlebar — native Windows Snap support
#
# Frameless windows (see webview.create_window(frameless=True, ...) at the
# bottom of this file) lose the OS titlebar and, with it, every OS-level
# behavior that hangs off it: drag-to-move, drag-to-edge/Aero-Snap,
# Win+Left/Right/Up/Down, double-click-to-maximize, and (Windows 11) the
# Snap Layouts flyout you get from hovering the maximize button. None of
# that can be recreated in pure JS/CSS or by any JS library -- dragging a
# DOM element around does not move an OS window, and no browser API can
# ask DWM to snap a window or draw the Snap Layouts flyout. The only way
# to get genuine OS snapping back on a frameless window is to tell Windows
# that parts of the client area ARE the caption/min/max/close buttons,
# via a WM_NCHITTEST subclass -- the same technique Windows Terminal,
# VS Code, and Chromium's own custom-titlebar mode use.
# =============================================================================

_TITLEBAR_H = 36     # px, must match #app-titlebar's height in the HTML/CSS below
_CAPTION_BTN_W = 46  # px, must match .titlebar-btn's width in the CSS below
_RESIZE_BORDER = 6   # px, invisible resize grab area outside a maximized window
_snap_wndproc_refs = {}  # hwnd -> ctypes callback, kept alive to avoid GC


def _find_own_hwnd(title_hint: str = "Midum Control Center"):
    """Locates this process's own top-level window by walking all windows
    owned by our PID, matched by title. More robust than trying to pull a
    native handle out of pywebview's Qt wrapper directly, since that
    internal shape can vary across pywebview/PySide/PyQt versions."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        pid = kernel32.GetCurrentProcessId()
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum_cb(hwnd, _lparam):
            wpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid and user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    if title_hint in buf.value:
                        found.append(hwnd)
            return True

        user32.EnumWindows(_enum_cb, 0)
        return found[0] if found else None
    except Exception:
        return None


def _win_show_window(window, sw_cmd: int) -> bool:
    """ShowWindow(hwnd, sw_cmd) on our own top-level window. Returns False
    (never raises) if the hwnd can't be found, so callers can fall back to
    pywebview's own minimize()/toggle_fullscreen()."""
    try:
        import ctypes
        hwnd = _find_own_hwnd()
        if not hwnd:
            return False
        ctypes.windll.user32.ShowWindow(hwnd, sw_cmd)
        return True
    except Exception:
        return False


def _install_win_snap_hook(window):
    """Subclasses our window's WNDPROC to answer WM_NCHITTEST as if the
    top _TITLEBAR_H px were a real caption bar and the rightmost three
    _CAPTION_BTN_W-wide slots were real min/max/close buttons. Reporting
    HTCAPTION/HTMINBUTTON/HTMAXBUTTON/HTCLOSE hands drag-to-move,
    Aero-Snap, Win+arrow snapping, and (on Windows 11) the Snap Layouts
    hover-flyout on the "maximize" zone straight back to the OS -- exactly
    as if this were a normal titlebar. Every other message is passed
    through untouched to the original WNDPROC. Best-effort: any failure
    just leaves the window without native snap (drag still works via the
    JS/-webkit-app-region fallback, just without OS snapping).

    IMPORTANT ctypes note: GetWindowLongPtrW/SetWindowLongPtrW/
    CallWindowProcW all move pointer-sized (LONG_PTR) values. ctypes
    defaults every foreign function to restype=c_int (32-bit) unless told
    otherwise -- left at that default, the 64-bit original-WNDPROC
    pointer gets silently truncated/corrupted, and CallWindowProcW is then
    called against a garbage address for every single message the window
    receives (not just WM_NCHITTEST), which looks like "the whole GUI
    stopped responding" rather than just the titlebar. All three are
    given explicit c_void_p/argtypes below specifically to avoid that.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = _find_own_hwnd()
        if not hwnd:
            return

        user32 = ctypes.windll.user32
        GWLP_WNDPROC = -4
        WM_NCHITTEST = 0x0084
        WM_NCCALCSIZE = 0x0083
        HTCLIENT, HTCAPTION = 1, 2
        HTLEFT, HTRIGHT, HTTOP, HTBOTTOM = 10, 11, 12, 15
        HTTOPLEFT, HTTOPRIGHT, HTBOTTOMLEFT, HTBOTTOMRIGHT = 13, 14, 16, 17

        is_64bit = ctypes.sizeof(ctypes.c_void_p) == 8
        LRESULT = ctypes.c_longlong if is_64bit else ctypes.c_long
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)

        # -- Explicit signatures (see docstring above for why this matters) --
        if is_64bit:
            get_wndproc, set_wndproc = user32.GetWindowLongPtrW, user32.SetWindowLongPtrW
        else:
            get_wndproc, set_wndproc = user32.GetWindowLongW, user32.SetWindowLongW
        get_wndproc.restype = ctypes.c_void_p
        get_wndproc.argtypes = [wintypes.HWND, ctypes.c_int]
        set_wndproc.restype = ctypes.c_void_p
        set_wndproc.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.CallWindowProcW.restype = LRESULT
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        call_wndproc = user32.CallWindowProcW

        # WM_NCHITTEST/GetWindowRect coordinates are PHYSICAL pixels, but
        # _TITLEBAR_H/_CAPTION_BTN_W/_RESIZE_BORDER are logical/CSS pixels
        # (they have to match the HTML's #app-titlebar/.titlebar-btn sizes,
        # which Chromium lays out in logical pixels). The physical-per-
        # logical ratio is read from `window._snap_dpi_scale` -- set by
        # the JS side from its OWN window.devicePixelRatio (see
        # initTitlebar()/set_dpi_scale below) -- rather than queried here
        # via GetDpiForWindow. Chromium (inside QtWebEngine) can end up
        # rendering at a different effective scale than the raw Windows
        # monitor DPI depending on Qt's own HighDPI handling, so asking
        # Windows for "the" DPI and asking Chromium what it actually used
        # are not guaranteed to agree -- devicePixelRatio is the one value
        # that's always exactly right for translating a physical WM_
        # NCHITTEST coordinate back into the CSS pixels the titlebar was
        # laid out in. Read fresh on every hit-test so a monitor change
        # (different scaling) is picked up automatically once JS reports it.
        def _current_scale():
            try:
                s = float(getattr(window, "_snap_dpi_scale", 1.0) or 1.0)
                return s if s > 0 else 1.0
            except Exception:
                return 1.0

        def _wnd_proc(h, msg, wparam, lparam):
            try:
                if msg == WM_NCCALCSIZE and wparam:
                    # Returning 0 here (instead of forwarding to the
                    # original WNDPROC/DefWindowProc) tells Windows the
                    # client rect is the ENTIRE proposed window rect --
                    # i.e. reserve no space for the caption/borders the
                    # WS_CAPTION|WS_THICKFRAME style bits above would
                    # otherwise cause it to paint. That's what makes the
                    # window still look fully custom/frameless despite
                    # now carrying "real window" styles for snap purposes.
                    return 0
                if msg == WM_NCHITTEST:
                    x = ctypes.c_short(lparam & 0xFFFF).value
                    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                    rc = wintypes.RECT()
                    user32.GetWindowRect(h, ctypes.byref(rc))
                    lx, ly = x - rc.left, y - rc.top
                    w, hgt = rc.right - rc.left, rc.bottom - rc.top
                    scale = _current_scale()

                    b = _RESIZE_BORDER * scale
                    on_left, on_right = lx < b, lx > w - b
                    on_top, on_bottom = ly < b, ly > hgt - b
                    if on_top and on_left: return HTTOPLEFT
                    if on_top and on_right: return HTTOPRIGHT
                    if on_bottom and on_left: return HTBOTTOMLEFT
                    if on_bottom and on_right: return HTBOTTOMRIGHT
                    if on_left: return HTLEFT
                    if on_right: return HTRIGHT
                    if on_top: return HTTOP
                    if on_bottom: return HTBOTTOM

                    titlebar_h_px = _TITLEBAR_H * scale
                    btn_w_px = _CAPTION_BTN_W * scale
                    if ly < titlebar_h_px:
                        btn_start = w - btn_w_px * 3
                        # The button strip must report plain HTCLIENT, not
                        # HTMINBUTTON/HTMAXBUTTON/HTCLOSE: those hit-test
                        # codes mean "this is a native, OS-drawn caption
                        # button" and Windows then treats every mouse
                        # message over them as non-client -- Chromium's
                        # DOM never receives a hover or click there at
                        # all. Our buttons are plain HTML, so they need
                        # real client-area mouse messages; JS onclick
                        # handlers already call titlebar_minimize/
                        # titlebar_toggle_maximize/titlebar_close (see
                        # initTitlebar() below) to do the actual native
                        # action. Trade-off: this means hovering the
                        # maximize button won't trigger Windows 11's Snap
                        # Layouts flyout, since that specifically requires
                        # a genuine OS-owned caption button -- edge-drag
                        # and Win+arrow snapping (from HTCAPTION below)
                        # still work fine.
                        if lx >= btn_start:
                            return HTCLIENT
                        # Everything else in the titlebar row defaults to
                        # HTCAPTION (real OS drag) -- but that's a BLANKET
                        # rule that knows nothing about the titlebar's own
                        # DOM layout, so any clickable HTML placed in
                        # #titlebar-center (the conversation-tab strip,
                        # "+" new-tab button, tab-group dropdown -- see
                        # #conv-tab-controls in the HTML/JS below) would
                        # never receive a real click on Windows no matter
                        # what -webkit-app-region CSS says, since HTCAPTION
                        # hit-test results are resolved entirely at the OS
                        # level before Chromium ever sees the message.
                        # reportTitlebarNodragZones() (JS) keeps
                        # window._nodrag_zones (logical/CSS px, set via
                        # Api.set_titlebar_nodrag_zones) up to date with
                        # that DOM's actual bounding rect; anything inside
                        # one of those rects gets real client-area mouse
                        # messages instead of being swallowed as caption.
                        for z in (getattr(window, "_nodrag_zones", None) or []):
                            try:
                                zl, zt = z["left"] * scale, z["top"] * scale
                                zr, zb = z["right"] * scale, z["bottom"] * scale
                            except Exception:
                                continue
                            if zl <= lx <= zr and zt <= ly <= zb:
                                return HTCLIENT
                        return HTCAPTION
                    return HTCLIENT
            except Exception:
                # Never let a bug in this hook eat the message -- fall
                # through to the original WNDPROC below just like any
                # other message would.
                pass
            return call_wndproc(_snap_wndproc_refs[hwnd], h, msg, wparam, lparam)

        cb = WNDPROC(_wnd_proc)
        old_proc = get_wndproc(hwnd, GWLP_WNDPROC)
        if not old_proc:
            print("[titlebar] Windows Snap hook not installed: could not read the original WNDPROC.")
            return
        _snap_wndproc_refs[hwnd] = old_proc
        set_wndproc(hwnd, GWLP_WNDPROC, cb)
        # Keep the ctypes callback object referenced for the process's
        # lifetime -- if it gets garbage-collected the window's WNDPROC
        # would point at freed memory the moment Windows calls it.
        _snap_wndproc_refs[f"{hwnd}_cb"] = cb

        # Only NOW -- with our WM_NCCALCSIZE-suppressing WNDPROC already
        # active -- put the real window styles back. pywebview's
        # frameless=True gives the HWND a bare WS_POPUP style on Windows,
        # missing WS_CAPTION/WS_THICKFRAME/WS_MAXIMIZEBOX/WS_MINIMIZEBOX/
        # WS_SYSMENU. Those bits are what the window manager checks for
        # snap-eligibility -- WM_NCHITTEST alone (already handled above)
        # only controls hit-testing, not whether Windows considers the
        # window snap/maximize-capable at all, which is exactly why
        # clicks/hover worked but Aero Snap and the Snap Layouts flyout
        # didn't. Restoring the bits AFTER the subclass is installed (not
        # before) matters too: SWP_FRAMECHANGED forces an immediate
        # WM_NCCALCSIZE recalculation, and if that fires against the
        # original (Qt) WNDPROC instead of ours, DefWindowProc would
        # briefly compute a real caption+border client-rect shrink --
        # i.e. a one-frame flash of a native titlebar -- before our
        # handler ever gets a chance to suppress it. This is the same
        # "real styles + hidden non-client area" technique apps like
        # Windows Terminal/VS Code use for a custom titlebar that still
        # gets genuine OS snapping.
        GWL_STYLE = -16
        WS_CAPTION     = 0x00C00000
        WS_THICKFRAME  = 0x00040000
        WS_MINIMIZEBOX = 0x00020000
        WS_MAXIMIZEBOX = 0x00010000
        WS_SYSMENU     = 0x00080000
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x2, 0x1, 0x4, 0x10, 0x20
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        style |= WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU
        user32.SetWindowLongW(hwnd, GWL_STYLE, style)
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
    except Exception as e:
        print(f"[titlebar] Windows Snap hook not installed: {e}")


# =============================================================================
# JS <-> Python bridge — every method here is callable from the frontend as
# `pywebview.api.<method>(...)` and returns JSON-serialisable data.
# =============================================================================
class Api(EditorMixin):
    def __init__(self):
        self.window = None
        self._closing       = False   # set once real teardown has started -- see _push_event / _on_closing

        self._session      = MidumSession()
        self._thinking     = False
        self._log_queue    = queue.Queue()

        # Guards self._thinking (read AND write) together with
        # self._pending_agent_notes below. _on_agent_done fires on a
        # sub-agent's OWN worker thread -- concurrently with the GUI/turn
        # thread -- so "if not self._thinking: self._thinking = True" is a
        # classic check-then-act race: two agents finishing back-to-back
        # could both see _thinking==False and both spawn a _run_turn()
        # thread, running two process_chat_turn() calls against the same
        # session concurrently. This lock makes that check-and-set atomic.
        self._agent_turn_lock   = threading.Lock()
        # Agent-finished notes that arrive while a turn is ALREADY running
        # (_thinking is True) can't be spliced into that turn's own
        # in-flight history_snapshot -- process_chat_turn owns that list
        # exclusively until the turn ends. Queuing them here and merging
        # them into self._session.history in _run_turn's own finally block
        # (see below) means a note is never lost (previously: appended
        # straight to self._session.history, then silently discarded when
        # the in-flight turn's `self._session.history = history_snapshot`
        # overwrote it) and never spliced into the MIDDLE of a live turn's
        # message list, which is what could leave an assistant tool_calls
        # turn without its matching function-response turn immediately
        # after it -- exactly the shape of request Gemini's API rejects
        # with "function call turn comes immediately after a user turn or
        # after a function response turn".
        self._pending_agent_notes = []

        # Guards _display_log mutations + _persist_current_chat() itself.
        # Without this, a close-triggered persist (from _on_closing, on the
        # GUI/close thread) could interleave with the turn-execution
        # thread's own append-then-persist sequence in _run_turn -- e.g.
        # reading self._display_log via list(...) after the reply had been
        # appended but before self._session.history was updated (or vice
        # versa), producing a torn snapshot that's missing the just-finished
        # reply. That torn, incomplete save could then be the LAST thing
        # written to disk if the window finished closing right after --
        # silently dropping the final reply from the saved chat even though
        # it was fully rendered on screen.
        self._persist_lock     = threading.Lock()
        self._chat_store       = ChatStore(CHATS_DIR)

        # ── Conversation tabs (see conversation_tabs.py) ────────────────────
        # Everything below this comment (_current_chat_id, _chat_title,
        # _display_log, _kb_state, _explain_progress/_explain_page_progress,
        # _render_surface_*) is GUI-visible state that belongs to whichever
        # conversation tab is currently active -- conversation_tabs.py only
        # tracks each tab's raw LLM history list, not this GUI-only stuff.
        # tab_id -> a snapshot dict of all of it, for every tab that ISN'T
        # the active one right now (see _snapshot_gui_state/_load_tab_gui_state
        # below). The active tab's copy of this state simply lives in the
        # plain self._current_chat_id/_chat_title/etc attributes below, same
        # as before tabs existed -- switching tabs snapshots the outgoing
        # tab's state in here, then loads the incoming tab's state (or
        # blanks, for a brand-new tab) into those attributes.
        self._tab_gui_state    = {}

        # GUI-only "preview" pointer: set to an agent tab's id while the
        # person is looking at that sub-agent's (read-only) conversation
        # tab in the titlebar strip, else None. Deliberately NOT the same
        # thing as conversation_tabs' own active-tab pointer (which
        # self._session.history always tracks) -- an agent tab must never
        # actually become "the" active conversation, since _on_agent_done/
        # _on_agent_inform below append straight to self._session.history
        # assuming it's always the Supervisor's own. See
        # switch_conversation_tab / list_conversation_tabs / _agent_tab_display.
        self._viewing_agent_tab_id = None
        self._current_chat_id  = uuid.uuid4().hex
        self._chat_title       = None
        self._display_log      = []

        # ── Edit Prompt / Regenerate Response (most-recent-turn only) ──────
        # Marks the slice of self._session.history / self._display_log that
        # the CURRENT tab's most recent user-initiated turn occupies, so it
        # can be cleanly popped back off for "Edit Prompt" (put the raw text
        # back in the input box, remove the turn) or "Regenerate Response"
        # (keep the user message, drop only what came after it, re-run).
        # None/None means "no editable turn right now" (fresh tab, or the
        # last turn was already consumed by an edit). Tab-scoped exactly
        # like _display_log itself -- see _snapshot_gui_state/
        # _load_tab_gui_state/_blank_gui_state and load_chat().
        self._turn_seq                 = 0
        self._last_turn_history_start  = None
        self._last_turn_display_start  = None
        self._last_turn_display_end    = None
        # Serializes edit_last_prompt()/regenerate_last_response() against
        # each other (and against themselves being double-clicked) -- both
        # mutate _session.history/_display_log and must never run
        # concurrently.
        self._gui_edit_lock    = threading.Lock()
        # Explain Mode's per-source progress: source name -> current part
        # index (0-based, into knowledge_base.build_pdf_source_parts).
        # Reset whenever the chat/session resets -- a walkthrough is scoped
        # to one conversation, not persisted across chats. Used by the
        # Part-by-Part mode only.
        self._explain_progress = {}
        # Page-by-Page mode's per-source progress: source name -> current
        # page number (1-based). Separate from _explain_progress above
        # since the two modes track position completely differently (part
        # index vs raw page number) and a source could in principle be
        # walked in either mode across a session.
        self._explain_page_progress = {}
        # Current KB Only / Explain Mode toggle state, kept in sync with the
        # frontend's own `state.kbOnly` / `state.kbSources` / `state.explainMode`
        # / `state.explainModeType` on every send_message() call (see below).
        # Persisted alongside the chat (_persist_current_chat) and restored on
        # load_chat() so reopening a chat mid-explanation resumes with KB Only
        # (and Explain Mode, and the correct source selection) already active
        # instead of the user having to re-enable everything by hand.
        self._kb_state = {
            "kb_only": False, "kb_sources": [],
            "explain_mode": False, "explain_mode_type": "part",
        }
        # Tracks the display-log/session-history entry currently being
        # streamed into by voice transcripts, so consecutive fragments from
        # the same speaker fold into one saved message instead of one saved
        # message per fragment -- see _voice_record_transcript().
        self._voice_stream_tag = None
        self._voice_stream_idx = None
        # Set when the window is closed while a reply is still being
        # generated -- see _on_closing() / _run_turn()'s finally block.
        self._close_requested   = False

        self._selected_provider = DEFAULT_PROVIDER_KEY
        self._selected_model    = _default_model_for_provider(DEFAULT_PROVIDER_KEY)

        # ── Render Surface ────────────────────────────────────────────────
        # Model-controlled HTML/CSS/JS panel (see write_render_code /
        # edit_render_code / close_render_surface in
        # tools/render_surface_tools.py and _handle_render below). The
        # current markup is kept ONLY in this Python attribute and, once
        # pushed, the sandboxed iframe's srcdoc on the JS side -- it is
        # never written to a temp file or anywhere else on disk. Reset to
        # None/False whenever the surface is closed, whether that's the
        # model calling close_render_surface or the user clicking the ✕.
        self._render_surface_open = False
        self._render_surface_code = None
        self._render_surface_title = None

        self._base_work_dir = r"D:\\"
        if not os.path.exists(self._base_work_dir):
            self._base_work_dir = os.path.expanduser("~/Documents")

        self._stdout_redir = _StdoutRedirector(self._on_log_line)
        sys.stdout = self._stdout_redir

        def _gui_say_intercept(label, text):
            if not text or re.match(r'^[{}\[\]",:\s]*$', text.strip()):
                return
            self._push_event("say", {"text": text})
            # Persist say() the same way a normal assistant reply is persisted
            # (see _run_turn's cleaned_reply append below) -- until now this
            # intercept only pushed the LIVE event; nothing here ever wrote to
            # self._display_log, so a say() call rendered fine during the live
            # session but reopening the chat showed only the generic tool-call
            # card for it (see _handle_tool_call's new "toolcall" persistence
            # below), not the actual reply text. Mirrors _on_voice_event's
            # "voice_say" handling, which already does this for voice turns.
            with self._persist_lock:
                self._display_log.append(("midum", text))
        midum._print_reply = _gui_say_intercept
        # IMPORTANT: this must be set on tools.user_prompt_tools itself, not
        # on `midum` (main.py). main.py does `from tools.user_prompt_tools
        # import _gui_ask_hook, ...`, which copies the value ONCE at import
        # time into main's own namespace -- rebinding `midum._gui_ask_hook`
        # afterwards only changes that copy, and every ask_user_* function
        # in tools/user_prompt_tools.py checks ITS OWN module-level global,
        # which would stay None forever. That silently sent every approval/
        # question/input request through the raw Tkinter popup fallback
        # instead of this app's inline chat card, no matter what.
        _user_prompt_tools._gui_ask_hook = self._handle_gui_ask
        # Same rebinding caveat as _gui_ask_hook just above applies here --
        # this must be set on tools.render_surface_tools itself, not on
        # `midum`, or write_render_code/edit_render_code/close_render_surface
        # would silently keep calling a stale None hook forever.
        _render_surface_tools._gui_render_hook = self._handle_render
        # MidumDocs (LaTeX editor pane): artifacts of type 'latex' open/refresh through this hook.
        midumdocs._gui_hook = self._handle_midumdocs
        # Editor tab (gui/editor_backend.py): lets the model open artifacts in it and lets artifact
        # commits (model edits) refresh an open editor tab live. Set on `artifacts` itself for the same
        # rebinding reason as the hooks above.
        artifacts._gui_editor_hook = self._handle_editor

        # ── Ambient Surfaces ──────────────────────────────────────────────
        # Floating over-the-screen widgets (see tools/ambient_surface_tools.py
        # and gui/ambient_surface.py). Same rebinding caveat as above: the
        # hook must be set on the tools module itself.
        self._ambient = AmbientSurfaceManager(
            call_tool=self._ambient_call_tool,
            on_submit=self._ambient_submit,
            on_event=lambda t: self._push_event("log", {"text": t}),
        )
        _ambient_surface_tools._gui_ambient_hook = self._ambient.handle
        # Sync the persisted "hide ambient surfaces from screenshots" toggle.
        try:
            self._ambient.set_hide_from_capture(
                self.get_settings().get("ambient_hide_from_capture", True))
        except Exception:
            pass

        # Structured tool-call detail (name + args + result) for every tool
        # executed during a turn -- powers the expandable tool-call cards in
        # the chat pane. Separate from the plain-text log line the console
        # already gets from _on_log_line's "-> Executing: ..." interception.
        midum.set_tool_call_hook(self._handle_tool_call)
        midum.set_tool_start_hook(self._handle_tool_start)

        # Wires the persistent WSL2 shell (shell_session.py) to the Shell
        # tab -- every command run there, whether issued by the model via
        # execute_wsl_command or typed by the user directly into the tab,
        # streams here as a "shell_line" event so both sides watch (and
        # can drive) the exact same live terminal.
        from shell_session import set_shell_event_hook, set_workspace_resolver
        set_shell_event_hook(self._push_event)
        # Both shells start in the editor's active workspace folder.
        from gui.editor_backend import active_workspace
        set_workspace_resolver(active_workspace)

        # Notified the moment a sub-agent's Action Loop stops (task done,
        # crashed, or cut off) -- see multi_agent.set_agent_done_hook and
        # Api._on_agent_done below. This is what lets the Supervisor learn
        # about a finished agent automatically instead of polling for it.
        midum.set_agent_done_hook(self._on_agent_done)

        # Fired the moment a sub-agent calls tell_supervisor_to_inform_user()
        # mid-task -- see multi_agent.set_agent_inform_hook and
        # Api._on_agent_inform below. Same delivery mechanism as
        # _on_agent_done, just for an interim message instead of the final
        # report.
        midum.set_agent_inform_hook(self._on_agent_inform)

        # Fired the instant a sub-agent's conversation tab actually OPENS
        # (its worker thread picked up a queued task) -- see
        # multi_agent.set_agent_started_hook and Api._on_agent_started
        # below. Without this, the tab strip only repaints on other agent
        # events (created/stopped/finished/mid-task message), which can
        # leave a working agent's tab invisible for up to the worker
        # loop's 0.5s poll interval, or longer if nothing else happens to
        # trigger a rebuild in the meantime.
        midum.set_agent_started_hook(self._on_agent_started)

        self._pending_ask = {}  # ask_id -> threading.Event / result box

        # ── Voice control (Gemini Live) ──────────────────────────────────
        from providers.gemini_live_backend import get_voice_session
        self._voice_session = get_voice_session(self._on_voice_event)

        # ── Push-to-talk global hotkeys (Voice tab) ───────────────────────
        # Drives self._voice_session directly (start-on-first-press,
        # mute/unmute on press/release) -- see hotkeys.py. Passed a getter
        # rather than the session object so hotkeys.py never has to import
        # the voice-deps stack (google-genai/sounddevice) itself.
        import hotkeys as _hotkeys_mod
        self._ptt_manager = _hotkeys_mod.get_manager(
            lambda: self._voice_session,
            on_state_change=self._on_ptt_state_change,
        )
        _ptt_start_msg = self._ptt_manager.start()
        if not _hotkeys_mod._PYNPUT_AVAILABLE:
            self._push_event("log", {"text": f"⚠️ {_ptt_start_msg}\n"})

    # ── Low-level plumbing ──────────────────────────────────────────────
    def _push_event(self, kind: str, payload: dict):
        """Push an async event to the frontend via window.evaluate_js.

        Runs the actual evaluate_js call on a short-lived daemon thread and
        waits on it with a timeout, instead of calling it directly on the
        caller's own thread. This matters because _push_event is called from
        background threads (the turn-execution thread, the scheduler tick
        thread) as well as the GUI thread itself -- if evaluate_js ever
        blocks (e.g. because the webview is mid-teardown while the titlebar
        X is being clicked), a direct call would hang that thread forever.
        For the turn thread specifically, that meant self._thinking never
        got reset to False, which made _on_closing wait forever and the
        whole app looked like it had frozen on close -- this bounds that.
        """
        if not self.window or self._closing:
            return
        try:
            data = json.dumps({"kind": kind, "payload": payload})
        except Exception:
            return

        def _do():
            try:
                self.window.evaluate_js(f"window.__midumEvent && window.__midumEvent({data})")
            except Exception:
                pass

        t = threading.Thread(target=_do, daemon=True)
        t.start()
        t.join(timeout=2.0)

    def _on_log_line(self, line: str):
        if line.startswith(_SAY_TAG):
            self._push_event("say", {"text": line[len(_SAY_TAG):]})
            return
        self._push_event("log", {"text": line})
        # Suppress the normal "tool_line" chat-pane row for prints that
        # originate on a sub-agent's own worker thread (this callback runs
        # synchronously on WHICHEVER thread called print(), so this check
        # is accurate even though sub-agents print concurrently on their
        # own threads). Their tool activity still reaches the chat pane --
        # see _handle_tool_call below, which pushes a single grey
        # "agent_tool_call" row (call + result together) once the call
        # actually finishes, instead of the two-stage placeholder-then-
        # detail flow normal Supervisor tool calls use.
        if midum.get_current_agent_name() is not None:
            return
        if _is_tool_line(line):
            self._push_event("tool_line", {"text": line.strip()})

    def _on_schedule_run(self, sched: dict, result: str):
        """Called by scheduler.py (off the tick thread) whenever a
        scheduled flow finishes running. Logs it to the Log pane and
        tells the Schedule pane (if open) to refresh so next_run_at/
        last_result reflect the fresh state."""
        flow_name = sched.get("flow_name")
        self._push_event("log", {"text": f"⏰ [Schedule '{sched.get('id')}'] ran flow '{flow_name}' -> {str(result)[:200]}\n"})
        self._push_event("schedule_ran", {"schedule_id": sched.get("id"), "flow_name": flow_name, "result": str(result)[:500]})

    def _handle_tool_start(self, name: str, args: dict, agent_name: str = None):
        """Called by orchestration.py right BEFORE a tool executes, with the
        exact arguments it is about to run with. Pushes a 'tool_start' event
        so the chat pane's tool pill can show the call immediately instead of
        waiting for the result (see _handle_tool_call for the finished
        call + result). Sub-agent calls and say() get no pill, so they're
        skipped here."""
        if agent_name or name == "say":
            return
        try:
            safe_args = json.loads(json.dumps(args, default=str))
        except Exception:
            safe_args = {k: str(v) for k, v in (args or {}).items()} if isinstance(args, dict) else {}
        if isinstance(safe_args, dict):
            for k, v in list(safe_args.items()):
                if isinstance(v, str) and len(v) > 4000:
                    safe_args[k] = v[:4000] + f"… [{len(v)} chars total]"
        self._push_event("tool_start", {"name": name, "args": safe_args})

    def _handle_tool_call(self, name: str, args: dict, result: str, agent_name: str = None):
        """Called by orchestration.py right after every tool call executes,
        with the exact arguments it ran and the raw (pre-HTML-escaped)
        result. agent_name is set when the call ran on a sub-agent's own
        worker thread (see multi_agent.get_current_agent_name) instead of
        the Supervisor's -- in that case this pushes a single self-
        contained 'agent_tool_call' event (call + result together) that
        the chat pane renders as a dim/grey row, instead of the normal
        'tool_call' event, which only attaches detail to a placeholder row
        _on_log_line already pushed for the Supervisor's own tool calls --
        a placeholder that was intentionally NOT pushed for agent calls."""
        try:
            safe_args = json.loads(json.dumps(args, default=str))
        except Exception:
            safe_args = {k: str(v) for k, v in (args or {}).items()}
        # Trim any individual argument value that's absurdly long (e.g. a
        # full base64 image or file blob) so the event stays light -- the
        # full untruncated value already went to conversation history.
        for k, v in list(safe_args.items()):
            if isinstance(v, str) and len(v) > 4000:
                safe_args[k] = v[:4000] + f"… [{len(v)} chars total]"

        if agent_name:
            self._push_event("agent_tool_call", {
                "agent": agent_name,
                "name": name,
                "args": safe_args,
                "result": str(result)[:8000],
            })
            # Persist this call into the agent's OWN transcript too, not
            # just the live event above -- otherwise a call that already
            # ran is gone the moment nobody's actively watching the live
            # stream (e.g. clicking into that agent's conversation tab
            # after some calls already happened) has nothing to replay.
            # 'say' is excluded exactly like the Supervisor's own path
            # below, since it's already recorded as a transcript entry by
            # multi_agent.py's own _agent_say_hook the instant it's called.
            if name != "say":
                midum.record_agent_tool_call(agent_name, name, safe_args, result)
            return

        self._push_event("tool_call", {
            "name": name,
            "args": safe_args,
            "result": str(result)[:8000],
        })

        # 'say' is intentionally excluded from the persisted toolcall record
        # below -- its actual content is already persisted as a normal
        # "midum" text bubble by _gui_say_intercept (midum._print_reply)
        # above, the same instant it's called. Persisting it again here too
        # would show every say() twice on reload: once as the real reply
        # bubble, once as a redundant bare tool-call card next to it.
        if name == "say":
            return

        # Persist a self-contained record (call + result together) into the
        # replayable display log -- until now this hook only pushed a LIVE
        # event for the current session's chat pane; nothing here ever
        # reached self._display_log, so tool-call cards rendered fine during
        # a live turn but silently vanished the moment the chat was closed
        # and reopened (load_chat only replays what's in _display_log/the
        # persisted "display" list -- see gui/chat_store.py). Stored as its
        # own "toolcall" tag (JSON payload) rather than reusing the live
        # "tool" tag, since "tool" renders a placeholder row that expects a
        # separate attachToolCallDetail() event to fill in later -- on
        # replay there is no such follow-up event, so the row needs to
        # arrive fully formed. See appendRow()'s "toolcall" branch.
        try:
            toolcall_payload = json.dumps({
                "name": name,
                "args": safe_args,
                "result": str(result)[:8000],
            }, default=str)
        except Exception:
            toolcall_payload = json.dumps({"name": name, "args": {}, "result": str(result)[:8000]})
        with self._persist_lock:
            self._display_log.append(("toolcall", toolcall_payload))

    # ── Shell tab (persistent WSL2 terminal, shared with the model) ────────
    def shell_run_user_command(self, command: str, shell: str = "wsl"):
        """Runs a command the USER typed directly into the Shell tab, in the
        exact same persistent WSL2 session the model's execute_wsl_command
        tool drives -- so cwd/env/background jobs stay consistent no matter
        which side is typing. Output streams back asynchronously as
        "shell_line" events (see shell_session.ShellSession._emit / the
        hook wired up in __init__ above); this call just kicks it off on a
        worker thread so the GUI never blocks on a slow/long-running command.
        """
        command = (command or "").strip()
        if not command:
            return {"ok": False, "error": "empty command"}
        from shell_session import get_shell_session
        sess = get_shell_session(shell)
        def _run():
            # `edit <path> [line]` opens the file/folder in the Editor tab
            # instead of running in the shell (see _shell_try_edit).
            if self._shell_try_edit(sess, command):
                return
            sess.run(command, origin="user", timeout=120)
        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    def _shell_try_edit(self, sess, command):
        """Handles `edit <path> [line]` typed by the user into either shell:
        the path is resolved against the shell's own cwd (WSL paths are
        mapped to Windows paths) and opened in the Editor tab."""
        import re as _re, shlex
        m = _re.match(r"^\s*edit\s+(\S.*?)\s*$", command)
        if not m:
            return False
        ps = sess.kind == "powershell"
        try:
            parts = shlex.split(m.group(1), posix=not ps)
        except ValueError:
            return False
        if ps:
            parts = [p.strip("'\"") for p in parts]
        if not parts:
            return False
        line = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        with sess._lock:
            sess._emit("user", "cmd", command, cwd=sess.get_cwd())
            host = sess.to_host_path(parts[0])
            if not host:
                msg = "edit: could not resolve that path"
            elif os.path.isdir(host):
                self._push_event("editor_open_folder", {"path": host})
                msg = f"opened folder in Editor: {host}"
            elif os.path.isfile(host):
                self._handle_editor("open_file", {"path": host, "line": line})
                msg = f"opened in Editor: {host}"
            else:
                msg = f"edit: no such file: {host}"
            sess._emit("system", "meta", msg, cwd=sess.get_cwd())
        return True

    def shell_restart(self, shell: str = "wsl"):
        """Kills the current WSL2 shell process so the next command (from
        either the model or the user) starts a fresh session -- exposed as
        a "Restart" button in the Shell tab for when a command hangs or the
        environment needs a clean slate."""
        from shell_session import get_shell_session
        get_shell_session(shell).restart()
        return {"ok": True}

    def get_shell_info(self):
        """Which shells exist on this machine (for the Shell tab switcher)."""
        from shell_session import available_shells
        return available_shells()

    def get_shell_history(self, shell: str = "wsl", since: int = 0):
        """Replay of a shell's recent lines (used by the Editor terminal panel)."""
        from shell_session import get_shell_session
        return get_shell_session(shell).get_history(int(since or 0))

    def get_shell_cwd(self, shell: str = "wsl"):
        """Current working directory of the persistent shell, for the
        Shell tab's terminal-style prompt label ("<cwd> $"). Cheap
        in-process lookup -- shell_session.py keeps this in sync with the
        shell's real $PWD after every command (see ShellSession.run)."""
        from shell_session import get_shell_session
        return get_shell_session(shell).get_cwd()

    # ── Bootstrap ─────────────────────────────────────────────────────────
    def startup(self):
        threading.Thread(target=self._startup_worker, daemon=True).start()
        return {"ok": True}

    def _startup_worker(self):
        try:
            # Restore persisted default provider/model before anything else
            # touches config.MODEL_PROVIDER / config.MODEL_NAME.
            saved = self.get_settings()
            self.apply_model(saved["provider"], saved["model"])

            midum.memory._bootstrap_all_files()

            # Every launch starts a genuinely new session GOAL/NOTES file --
            # the same reset the "New Chat" button performs. This is
            # separate from which chat TABS are open (see
            # _restore_tabs_or_start_fresh below, which DOES restore those
            # across a restart) -- there's just no need to silently carry
            # the previous session's goal/notes forward into whatever tabs
            # come back.
            self._reset_session_memory_file()

            try:
                sys_prompt = midum.get_system_prompt()
            except AttributeError:
                sys_prompt = "You are Midum. Rules:\n- Proceed safely."

            memories = []
            master_ctx = midum.memory.load_memory_into_context(midum.MASTER_MEMORY, "master")
            if master_ctx:
                memories.append(master_ctx)
            try:
                with open(midum.INSTRUCTIONS_FILE, "r", encoding="utf-8") as f:
                    _instr = f.read().strip()
                if _instr:
                    memories.append("[MIDUM INSTRUCTIONS — always active]\n" + _instr)
            except Exception:
                pass

            self._session.initialise(sys_prompt, memories)

            # Reopen every tab that was open the last time Midum closed
            # (or start a single fresh tab if there's no saved state / it
            # all failed to restore) -- see _restore_tabs_or_start_fresh's
            # own docstring. Must happen AFTER initialise() above, not in
            # __init__ -- initialise() reassigns self._session.history to
            # a brand-new list object, and the fresh-tab fallback path
            # needs to capture that final object by reference, not
            # whatever empty list existed before initialise() ran.
            import conversation_tabs
            self._restore_tabs_or_start_fresh(sys_prompt, memories)

            # Everything above this point is exactly what's needed to know
            # which tabs were open last time and show them -- cheap, local,
            # no network/subprocess involved. Push it to the frontend RIGHT
            # NOW rather than waiting on the slower subsystems below (MCP
            # server reconnects, the scheduler, the Discord bot), which used
            # to run first and gate the tab strip's very first paint behind
            # themselves for no real reason -- none of them affect which
            # tabs exist or what's in them. JS listens for this event and
            # calls buildConversationTabs() immediately; "status":"Ready"
            # further down still marks the whole app as fully booted.
            #
            # Also hand over the ACTIVE tab's own display log + KB/Explain
            # state right here, same shape switch_conversation_tab returns
            # -- otherwise the chat pane sits empty (showing none of the
            # restored conversation) until the user manually switches away
            # from and back to this tab, since nothing else ever pushes
            # this tab's content to the frontend on a plain launch.
            self._push_event("tabs_restored", {"display": self._display_log, "kb_state": self._kb_state})

            configs = midum._load_mcp_config()
            if configs:
                self._push_event("log", {"text": f"🔌 Reconnecting {len(configs)} saved MCP server(s)...\n"})
                midum.init_mcp_servers_from_config()
                self._push_event("mcp_changed", {})

            # Scheduler: only fires while this app is open (see scheduler.py's
            # module docstring) -- start the background tick thread now, once
            # per app launch. Safe to call again; start_scheduler() is a no-op
            # if a tick thread is already alive.
            try:
                midum.start_scheduler(on_run=self._on_schedule_run)
                self._push_event("log", {"text": "⏰ [Scheduler started -- scheduled Flows will run while this app is open]\n"})
            except Exception as e:
                self._push_event("log", {"text": f"⚠️ Scheduler failed to start: {e}\n"})

            # Discord bot: auto-start on launch if DISCORD_BOT_TOKEN is
            # configured, instead of requiring the user to explicitly ask
            # Midum to start_discord_bot() every session. Mirrors the
            # scheduler's pattern above -- best-effort, logged either way,
            # never blocks the rest of startup. providers.discord_voice
            # already no-ops safely if discord.py isn't installed or the
            # token is missing/blank, so this is safe to always attempt.
            try:
                from providers.discord_voice import start_discord_bot as _start_discord_bot, _CREDS_AVAILABLE as _discord_creds_available
                if _discord_creds_available:
                    discord_result = _start_discord_bot()
                    self._push_event("log", {"text": f"\U0001f4ac [Discord] {discord_result}\n"})
                # If not configured, stay silent -- most users never set up
                # Discord at all, and the setup docstring in discord_voice.py
                # already covers what's needed for anyone who does.
            except Exception as e:
                self._push_event("log", {"text": f"⚠️ Discord bot failed to auto-start: {e}\n"})

            self._push_event("status", {"text": "Ready", "level": "ok"})
        except Exception as e:
            self._push_event("status", {"text": f"Startup error: {e}", "level": "err"})

        self._scan_workspace_directory()

        # NOTE: warming the PDF line-extraction cache (build_pdf_source_parts
        # for every registered source) used to run automatically here on a
        # background thread. For accounts with many/large PDF sources that
        # full-PDF text extraction pass was heavy enough to noticeably lag
        # the whole machine on every single launch, even though nothing in
        # the Knowledge tab was open yet. It's now opt-in: the user triggers
        # it explicitly via the "Load Sources" button in the Knowledge tab
        # (see warm_pdf_sources() below), so startup stays light.

    def _warm_pdf_source_cache(self):
        """Extracts every registered PDF source's full line-by-line text
        (the heavy step -- PyMuPDF's page.get_text("dict") plus building
        large nested Python dict/list structures for every line of every
        page) in separate worker PROCESSES rather than looping through it
        on this background thread.

        Running it on a plain background thread (the old approach) still
        made the whole GUI stutter/freeze on click: this is genuinely
        CPU-bound pure-Python work, and CPython's GIL means a thread doing
        that work and the GUI's own thread (handling webview/JS calls)
        trade off the interpreter every ~5ms rather than truly running
        alongside each other -- for a large/many-page PDF that adds up to
        real, visible lag on every click, exactly the "extreme lag" this
        fixes. A ProcessPoolExecutor runs each extraction in its own
        interpreter/GIL, so this CPU work never competes with the GUI
        thread at all; only the small, already-parsed result list is
        pickled back across the process boundary.

        Results are written into knowledge_base._PDF_LINES_CACHE here (the
        same cache _cached_extract_pdf_lines reads from), so the
        build_pdf_source_parts() calls that follow are cheap in-process
        lookups instead of re-parsing anything.
        """
        try:
            names = midum.list_pdf_sources()
        except Exception:
            return

        # Resolve each source's PDF path up front, deduped (several
        # sources could in principle point at the same file) and limited
        # to files that still exist on disk.
        paths = set()
        for name in names:
            try:
                record = midum.read_pdf_source(name)
            except Exception:
                continue
            path = (record or {}).get("source_path")
            if path and os.path.exists(path):
                paths.add(path)

        if paths:
            try:
                max_workers = min(4, max(1, os.cpu_count() or 1))
                with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as ex:
                    futures = {ex.submit(_knowledge_base.extract_pdf_lines, p): p for p in paths}
                    for fut in concurrent.futures.as_completed(futures):
                        path = futures[fut]
                        try:
                            lines = fut.result()
                        except Exception:
                            continue
                        try:
                            mtime = os.path.getmtime(path)
                        except OSError:
                            mtime = None
                        _knowledge_base._PDF_LINES_CACHE[path] = (mtime, lines)
            except Exception:
                # Process pool unavailable for some reason (sandboxed
                # environment, frozen-exe spawn issue, etc) -- fall back
                # to the old sequential in-thread extraction rather than
                # warming nothing at all. Uses the cached helper so the
                # result still lands in _PDF_LINES_CACHE for reuse.
                for path in paths:
                    try:
                        _knowledge_base._cached_extract_pdf_lines(path)
                    except Exception:
                        pass

        for name in names:
            try:
                midum.build_pdf_source_parts(name)
            except Exception:
                pass

    def warm_pdf_sources(self):
        """User-triggered (Knowledge tab "Load Sources" button) equivalent
        of the old automatic startup warm-up. Runs the full-PDF text
        extraction for every registered PDF source on a background thread
        -- this is the heavy pass that used to run unconditionally on every
        launch and could noticeably lag the machine, so it's now opt-in and
        only runs when the user explicitly asks for it. Pushes a
        'pdf_cache_warmed' event when done so the button can reset itself.
        """
        if getattr(self, "_warming_pdf_cache", False):
            return {"ok": True, "already_running": True}
        self._warming_pdf_cache = True

        def _run():
            try:
                self._warm_pdf_source_cache()
            finally:
                self._warming_pdf_cache = False
                self._push_event("pdf_cache_warmed", {})

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    # ── Status / dashboard ───────────────────────────────────────────────
    def get_status(self):
        proj = midum.memory._active_project_memory_path
        return {
            "provider": _PROVIDER_KEY_TO_LABEL.get(self._selected_provider, self._selected_provider),
            "model": self._selected_model or "(auto)",
            "goal": midum.memory._current_goal or "None active",
            "workspace": os.path.dirname(proj) if proj else "No project selected",
            "gemini": bool(midum.providers_gemini_reasoning._GEMINI_AVAILABLE),
            "ocr": bool(midum._TESSERACT_AVAILABLE),
            "uia": bool(midum._UIA_AVAILABLE),
            "turns": self._session.turn_counter,
            "thinking": self._thinking,
        }

    def get_providers(self):
        models = (
            _gemini_web_model_options_fast()
            if self._selected_provider == "gemini_web"
            else _known_models_for_provider(self._selected_provider)
        )
        return {
            "options": [label for label, _ in PROVIDER_OPTIONS],
            "current": _PROVIDER_KEY_TO_LABEL[self._selected_provider],
            "models": models,
            "current_model": self._selected_model,
        }

    # Syntax highlighting (Pygments) --------------------------------------
    # ```-fenced code blocks in chat are highlighted by running the block's
    # text through Pygments here and handing back ready-to-inject HTML (see
    # renderPendingCodeHighlight() in the frontend JS). Pure string
    # transform, safe to call for every code block rendered, including ones
    # re-rendered from a loaded chat history.
    _PYGMENTS_STYLE = "monokai"

    def highlight_code(self, code: str, lang: str = ""):
        """Returns Pygments-highlighted HTML (span soup, no wrapping
        <pre>/<div> -- the frontend already supplies those) for one fenced
        code block, or None if Pygments isn't available/no lexer could be
        resolved, in which case the frontend keeps its plain-escaped-text
        rendering."""
        if not _PYGMENTS_OK or not code:
            return None
        try:
            lang = (lang or "").strip()
            try:
                lexer = _pygments_get_lexer_by_name(lang) if lang else _pygments_guess_lexer(code)
            except _PygmentsClassNotFound:
                lexer = _pygments_guess_lexer(code)
            formatter = _PygmentsHtmlFormatter(nowrap=True, style=self._PYGMENTS_STYLE)
            return _pygments_highlight(code, lexer, formatter)
        except Exception:
            return None

    def get_pygments_css(self):
        """CSS for the Pygments token classes (.k, .s, .nf, etc), scoped
        under .code-block so it can only ever affect highlighted code.
        Fetched once on startup and injected as a <style> tag (see the
        pywebviewready handler below)."""
        if not _PYGMENTS_OK:
            return ""
        try:
            css = _PygmentsHtmlFormatter(style=self._PYGMENTS_STYLE).get_style_defs(".code-block")
            # get_style_defs() also emits a couple of rules that aren't
            # scoped to our prefix (a bare "pre { line-height: ... }" and a
            # base ".code-block { background/color }" that would fight our
            # own pre.code-block background). Drop both -- the base look of
            # the block already comes from our own CSS; only the per-token
            # (.code-block .k, .code-block .s, ...) rules are wanted here.
            kept = [
                line for line in css.splitlines()
                if line.strip().startswith(".code-block .")
            ]
            return "\n".join(kept)
        except Exception:
            return ""

    # ── Voice control (Gemini Live) ──────────────────────────────────────
    def _on_voice_event(self, kind: str, payload: dict):
        """Callback handed to VoiceSession -- relays every voice-session
        event straight to the frontend over the same async event bridge
        used for text-chat events (say/log/tool_call/etc), and also folds
        spoken turns into the same persisted chat history/display log that
        text chat uses (self._session.history / self._display_log), so a
        voice conversation survives app restarts and replays in the chat
        pane exactly like a normal typed conversation."""
        if kind == "voice_transcript":
            self._voice_record_transcript(payload.get("role"), payload.get("text") or "")
        elif kind == "voice_say":
            # say() narration during a voice turn -- breaks whichever
            # transcript bubble was streaming (same as a real tool call)
            # and folds in as its own assistant message, so it survives
            # app restarts and replays in the chat pane like any other
            # message instead of vanishing once the turn ends.
            self._voice_stream_tag = None
            self._voice_stream_idx = None
            say_text = payload.get("text") or ""
            if say_text:
                self._display_log.append(("midum", say_text))
                with self._session._lock:
                    self._session.history.append({"role": "assistant", "content": say_text})
        elif kind == "voice_tool_call":
            # A tool call breaks whichever spoken bubble was streaming, same
            # as the frontend does -- the next transcript fragment starts a
            # fresh saved message instead of merging into the old one.
            self._voice_stream_tag = None
            self._voice_stream_idx = None
        elif kind == "voice_tool_result":
            self._voice_stream_tag = None
            self._voice_stream_idx = None
        elif kind in ("voice_turn_complete", "voice_interrupted", "voice_error"):
            self._voice_stream_tag = None
            self._voice_stream_idx = None
            self._persist_current_chat()
        elif kind == "voice_status" and payload.get("status") == "stopped":
            self._voice_stream_tag = None
            self._voice_stream_idx = None
            self._persist_current_chat()
        self._push_event(kind, payload)

    def _voice_record_transcript(self, role: str, text: str):
        """Folds one voice-transcript fragment into self._display_log (the
        replayable chat bubbles) and self._session.history (the LLM-facing
        context), merging consecutive fragments from the same speaker into
        a single message the same way the chat pane merges them into a
        single bubble -- Gemini Live streams transcription in small pieces,
        not whole utterances, so without merging every fragment would save
        as its own separate chat message."""
        if not text:
            return
        tag = "user" if role == "user" else "midum"
        session_role = "user" if role == "user" else "assistant"

        if (self._voice_stream_tag == tag and self._voice_stream_idx is not None
                and 0 <= self._voice_stream_idx < len(self._display_log)):
            old_tag, old_text = self._display_log[self._voice_stream_idx]
            self._display_log[self._voice_stream_idx] = (old_tag, old_text + text)
            with self._session._lock:
                if self._session.history and self._session.history[-1].get("role") == session_role:
                    self._session.history[-1]["content"] += text
        else:
            self._display_log.append((tag, text))
            self._voice_stream_idx = len(self._display_log) - 1
            self._voice_stream_tag = tag
            if not self._chat_title and tag == "user":
                self._set_chat_title_from_first_message(text)
            with self._session._lock:
                self._session.history.append({"role": session_role, "content": text})

    def get_voice_status(self):
        from providers.gemini_live_backend import voice_dependencies_status
        return {
            "running": self._voice_session.is_running(),
            "dependencies": voice_dependencies_status(),
            "model": config.GEMINI_LIVE_MODEL,
            "voice": config.GEMINI_LIVE_VOICE,
        }

    def start_voice_session(self, model="", voice=""):
        return self._voice_session.start(model or "", voice or "")

    def stop_voice_session(self):
        return self._voice_session.stop()

    def set_voice_muted(self, muted: bool):
        return self._voice_session.set_muted(bool(muted))

    # ── Push-to-talk global hotkeys (Voice tab) ───────────────────────────
    def _on_ptt_state_change(self, pressed_ids):
        """Relayed to the frontend so the Voice tab can show a live
        'currently talking' indicator while a PTT key/button is held."""
        self._push_event("voice_ptt_state", {"pressed": list(pressed_ids)})

    def get_ptt_hotkeys(self):
        return self._ptt_manager.status()

    def set_ptt_hotkey(self, slot_id: str, kind: str, value: str, label: str = ""):
        ok = self._ptt_manager.set_hotkey(slot_id, kind, value, label)
        return {"ok": ok, "hotkeys": self._ptt_manager.get_hotkeys()}

    def reset_ptt_hotkeys(self):
        return {"ok": True, "hotkeys": self._ptt_manager.reset_defaults()}

    def start_ptt_capture(self, slot_id: str):
        """Arms capture mode: the next global key press or mouse click
        anywhere is bound to `slot_id` and pushed back as a 'ptt_captured'
        event so the Voice tab's rebind button can update itself."""
        def _cb(kind, value, label):
            self._push_event("ptt_captured", {"slot_id": slot_id, "kind": kind, "value": value, "label": label})
        self._ptt_manager.begin_capture(slot_id, _cb)
        return {"ok": True}

    def cancel_ptt_capture(self):
        self._ptt_manager.cancel_capture()
        return {"ok": True}

    def get_context_token_limit(self):
        """Current context-token setting for the Model tab field. Returns
        the saved override if the user has one, otherwise the effective
        value the summarizer would use right now for the active model
        (falls back to 32000 if that model isn't in the built-in table)."""
        saved = midum.get_user_context_tokens()
        return {
            "saved": saved,
            "effective": midum.get_context_window(),
            "is_override": saved is not None,
        }

    def set_context_token_limit(self, max_tokens):
        try:
            n = int(max_tokens)
            if n < 1000:
                return {"ok": False, "error": "Must be at least 1000 tokens."}
            new_val = midum.set_user_context_tokens(n)
            self._push_event("log", {"text": f"🧠 [Context window set to {new_val:,} tokens — used by the summarizer to decide when to compact history]\n"})
            return {"ok": True, "saved": new_val}
        except (TypeError, ValueError):
            return {"ok": False, "error": "Enter a whole number of tokens."}

    def refresh_ollama_models(self):
        if self._selected_provider == "ollama_cloud":
            return _list_ollama_cloud_models()
        if self._selected_provider == "ollama":
            return _list_ollama_models()
        return _known_models_for_provider(self._selected_provider)

    def select_provider(self, label: str):
        provider_key = _PROVIDER_LABEL_TO_KEY.get(label, DEFAULT_PROVIDER_KEY)
        if provider_key == "ollama_cloud":
            return {
                "models": _list_ollama_cloud_models(),
                "default_model": _default_model_for_provider(provider_key),
            }
        if provider_key == "gemini_web":
            # list_gemini_web_models() lazily initialises the gemini_webapi
            # client/session over the network and can take a couple of
            # seconds — calling it here would block this synchronous
            # pywebview API call, making the Model field visibly lag behind
            # every other provider (which are all static config lookups).
            # Return the cached/fallback list instantly instead, and kick
            # the real fetch off on a background thread; once it lands,
            # push an event so the frontend can refresh the dropdown if
            # the user is still on this provider.
            def _refresh():
                fresh = _list_gemini_web_model_options()
                if self._selected_provider == "gemini_web":
                    self._push_event("gemini_web_models", {"models": fresh})
            threading.Thread(target=_refresh, daemon=True).start()
            return {
                "models": _gemini_web_model_options_fast(),
                "default_model": _default_model_for_provider(provider_key),
            }
        return {
            "models": _known_models_for_provider(provider_key),
            "default_model": _default_model_for_provider(provider_key),
        }

    def apply_model(self, label: str, model_id: str):
        provider_key = _PROVIDER_LABEL_TO_KEY.get(label, DEFAULT_PROVIDER_KEY)
        model_id = (model_id or "").strip()
        if not model_id or model_id == "(auto)":
            model_id = "" if provider_key == "gemini_web" else _default_model_for_provider(provider_key)

        self._selected_provider = provider_key
        self._selected_model = model_id

        midum.config.MODEL_PROVIDER = provider_key
        if provider_key == "ollama":
            midum.config.MODEL_NAME = model_id
        elif provider_key == "openrouter":
            midum.config.OPENROUTER_MODEL = model_id
        elif provider_key == "gemini_web":
            midum.config.GEMINI_WEB_MODEL = model_id
            midum.providers_gemini_web_backend._gemini_web_model_cache = None
        elif provider_key == "gemini_api":
            midum.config.GEMINI_API_MODEL = model_id
        elif provider_key == "groq":
            midum.config.GROQ_MODEL = model_id
        elif provider_key == "ollama_cloud":
            midum.config.OLLAMA_CLOUD_MODEL = model_id
        elif provider_key == "litellm":
            midum.config.LITELLM_MODEL = model_id
        # "openai_web": no per-provider model setting exists (llm_cookie_bridge
        # has no model= param to pin) -- switching MODEL_PROVIDER above is the
        # entire story, same as every other config write for this provider.

        self._push_event("log", {"text": f"🔀 [Provider switched: {label} — {model_id or '(auto)'}]\n"})
        return self.get_status()

    # ── LiteLLM (any provider) helpers for the Model tab / Settings picker ──────
    # Thin pass-throughs to providers/litellm_backend.py. Everything here is
    # safe to call whether or not the litellm package is installed.
    def litellm_catalog(self):
        b = midum.providers_litellm_backend
        try:
            return {"available": b._LITELLM_AVAILABLE, "message": b._litellm_load_msg,
                    "providers": b.list_litellm_providers()}
        except Exception as e:
            return {"available": False, "message": str(e), "providers": []}

    def litellm_provider_models(self, provider: str):
        return midum.providers_litellm_backend.list_provider_models(provider)

    def litellm_refresh_models(self, provider: str):
        return midum.providers_litellm_backend.fetch_live_models(provider)

    def litellm_provider_status(self, provider: str):
        return midum.providers_litellm_backend.provider_status(provider)

    def litellm_save_credentials(self, provider: str, api_key=None, api_base=None):
        r = midum.providers_litellm_backend.save_provider_credentials(provider, api_key, api_base)
        if r.get("ok"):
            self._push_event("log", {"text": f"🔑 [LiteLLM: saved credentials for '{provider}']\n"})
        return r

    def litellm_test(self, model: str):
        return midum.providers_litellm_backend.test_connection(model)

    def litellm_install(self):
        self._push_event("log", {"text": "📦 [Installing litellm via pip — this can take a minute...]\n"})
        r = midum.providers_litellm_backend.install_litellm()
        self._push_event("log", {"text": "✅ [litellm installed]\n" if r.get("ok") else f"⚠️ [litellm install failed: {r.get('error')}]\n"})
        return r

    # ── Persisted GUI settings (default model + theme colors) ────────────
    _SETTINGS_FILENAME = "gui_settings.json"
    # Separate default palettes per theme, so Light and Dark each have their
    # own preset that gets saved/restored independently -- switching themes
    # applies whichever colors were last saved for that theme, not a single
    # shared set re-tinted on top of it.
    _DEFAULT_COLORS_DARK = {
        "accent": "#60a5fa", "accent2": "#1d4ed8",
        "bg": "#05070c", "panel": "#0b0f19", "text": "#f3f4f6",
        "user_msg": "#0f1e33",
        "blob_center": "#60a5fa", "blob_a": "#f472b6", "blob_b": "#34d399", "blob_cursor": "#a78bfa",
    }
    _DEFAULT_COLORS_LIGHT = {
        "accent": "#1d4ed8", "accent2": "#3730a3",
        "bg": "#f7f9fc", "panel": "#ffffff", "text": "#10182b",
        "user_msg": "#dbeafe",
        "blob_center": "#60a5fa", "blob_a": "#f472b6", "blob_b": "#34d399", "blob_cursor": "#a78bfa",
    }
    _DEFAULT_THEME = "dark"
    _DEFAULT_BLOBS_ENABLED_DARK = True
    _DEFAULT_BLOBS_ENABLED_LIGHT = True

    # Built-in named presets, beyond the base Dark/Light toggle. Each is a
    # full editable-color bundle (same shape as colors_dark/colors_light)
    # plus which base theme (dark/light) it should be layered on top of and
    # whether ambient blobs should be on. These ship read-only, IN CODE --
    # they're always available, can't be renamed or deleted from the GUI,
    # and aren't written to gui_settings.json. User-created presets are the
    # opposite: they live only in storage (settings["custom_presets"], see
    # get_settings/save_preset/delete_preset below), never in this dict.
    _BUILTIN_PRESETS = {
        "professional_dark": {
            "label": "Professional Dark",
            "base": "dark",
            "blobs_enabled": False,
            "colors": {
                "accent": "#9a9a9f", "accent2": "#6e6e73",
                "bg": "#111113", "panel": "#18181b", "text": "#e8e8ea",
                "user_msg": "#232326",
                "blob_center": "#8a8f98", "blob_a": "#6e6e73", "blob_b": "#5c6068", "blob_cursor": "#4b4b50",
            },
        },
        "professional_light": {
            "label": "Professional Light",
            "base": "light",
            "blobs_enabled": False,
            "colors": {
                "accent": "#52525b", "accent2": "#3f3f46",
                "bg": "#f7f7f6", "panel": "#ffffff", "text": "#1c1c1e",
                "user_msg": "#ececec",
                "blob_center": "#71717a", "blob_a": "#a1a1aa", "blob_b": "#8a8f98", "blob_cursor": "#52525b",
            },
        },
        "midnight": {
            "label": "Midnight (dark)",
            "base": "dark",
            "blobs_enabled": True,
            "colors": {
                "accent": "#3b82f6", "accent2": "#1e3a8a",
                "bg": "#05070f", "panel": "#0a0e1a", "text": "#e6e9f5",
                "user_msg": "#12203a",
                "blob_center": "#3b82f6", "blob_a": "#6366f1", "blob_b": "#1e3a8a", "blob_cursor": "#818cf8",
            },
        },
        "obsidian": {
            "label": "Obsidian (dark)",
            "base": "dark",
            "blobs_enabled": True,
            "colors": {
                "accent": "#a78bfa", "accent2": "#6d28d9",
                "bg": "#0a0a0d", "panel": "#131316", "text": "#eae8f0",
                "user_msg": "#221c33",
                "blob_center": "#a78bfa", "blob_a": "#8b5cf6", "blob_b": "#6d28d9", "blob_cursor": "#c4b5fd",
            },
        },
        "forest": {
            "label": "Forest (dark)",
            "base": "dark",
            "blobs_enabled": True,
            "colors": {
                "accent": "#34d399", "accent2": "#059669",
                "bg": "#06120b", "panel": "#0d1f14", "text": "#e3f3e6",
                "user_msg": "#123322",
                "blob_center": "#34d399", "blob_a": "#10b981", "blob_b": "#059669", "blob_cursor": "#6ee7b7",
            },
        },
        "crimson": {
            "label": "Crimson (dark)",
            "base": "dark",
            "blobs_enabled": True,
            "colors": {
                "accent": "#f87171", "accent2": "#b91c1c",
                "bg": "#120608", "panel": "#1f0d10", "text": "#f5e6e8",
                "user_msg": "#3a1216",
                "blob_center": "#f87171", "blob_a": "#fb7185", "blob_b": "#b91c1c", "blob_cursor": "#fca5a5",
            },
        },
        "amber": {
            "label": "Amber (dark)",
            "base": "dark",
            "blobs_enabled": True,
            "colors": {
                "accent": "#fbbf24", "accent2": "#b45309",
                "bg": "#120d05", "panel": "#1f160a", "text": "#f5ecd9",
                "user_msg": "#3a2a10",
                "blob_center": "#fbbf24", "blob_a": "#f59e0b", "blob_b": "#b45309", "blob_cursor": "#fcd34d",
            },
        },
        "arctic": {
            "label": "Arctic (light)",
            "base": "light",
            "blobs_enabled": True,
            "colors": {
                "accent": "#38bdf8", "accent2": "#0284c7",
                "bg": "#f0f7fb", "panel": "#ffffff", "text": "#0f2a3d",
                "user_msg": "#dff3fc",
                "blob_center": "#38bdf8", "blob_a": "#7dd3fc", "blob_b": "#0284c7", "blob_cursor": "#bae6fd",
            },
        },
        "sand": {
            "label": "Sand (light)",
            "base": "light",
            "blobs_enabled": True,
            "colors": {
                "accent": "#d9a066", "accent2": "#b5793a",
                "bg": "#faf6ef", "panel": "#ffffff", "text": "#3b2f22",
                "user_msg": "#f2e3cc",
                "blob_center": "#d9a066", "blob_a": "#e8c496", "blob_b": "#b5793a", "blob_cursor": "#f0d9b5",
            },
        },
        "meadow": {
            "label": "Meadow (light)",
            "base": "light",
            "blobs_enabled": True,
            "colors": {
                "accent": "#4ade80", "accent2": "#16a34a",
                "bg": "#f3faf3", "panel": "#ffffff", "text": "#16321f",
                "user_msg": "#dcf5df",
                "blob_center": "#4ade80", "blob_a": "#86efac", "blob_b": "#16a34a", "blob_cursor": "#bbf7d0",
            },
        },
        "blossom": {
            "label": "Blossom (light)",
            "base": "light",
            "blobs_enabled": True,
            "colors": {
                "accent": "#f472b6", "accent2": "#db2777",
                "bg": "#fdf3f7", "panel": "#ffffff", "text": "#3a1c28",
                "user_msg": "#fbe1ec",
                "blob_center": "#f472b6", "blob_a": "#f9a8d4", "blob_b": "#db2777", "blob_cursor": "#fbcfe8",
            },
        },
        "sunrise": {
            "label": "Sunrise (light)",
            "base": "light",
            "blobs_enabled": True,
            "colors": {
                "accent": "#fb923c", "accent2": "#ea580c",
                "bg": "#fff8ee", "panel": "#ffffff", "text": "#3a2a0f",
                "user_msg": "#ffe9cf",
                "blob_center": "#fb923c", "blob_a": "#fdba74", "blob_b": "#ea580c", "blob_cursor": "#fed7aa",
            },
        },
    }
    # Main chat window text size in CSS px (SETTINGS -> Chat Text Size). It only
    # drives the chat pane, through the --chat-fs variable in the frontend.
    _CHAT_FONT_SIZE_DEFAULT = 15.0
    _CHAT_FONT_SIZE_MIN = 10.0
    _CHAT_FONT_SIZE_MAX = 32.0

    @classmethod
    def _clamp_chat_font_size(cls, value):
        if isinstance(value, bool):
            return cls._CHAT_FONT_SIZE_DEFAULT
        try:
            v = float(value)
        except (TypeError, ValueError):
            return cls._CHAT_FONT_SIZE_DEFAULT
        if v != v:  # NaN
            return cls._CHAT_FONT_SIZE_DEFAULT
        return round(max(cls._CHAT_FONT_SIZE_MIN, min(cls._CHAT_FONT_SIZE_MAX, v)), 2)

    _DEFAULT_BG_IMAGE = {
        "enabled": False, "path": "",
        "brightness": 100, "blur": 0, "opacity": 100,
    }
    _IMAGE_MIME_TYPES = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    }

    def _settings_path(self):
        return os.path.join(midum.STORAGE_DIR, self._SETTINGS_FILENAME)

    def get_settings(self):
        defaults = {
            "provider": _PROVIDER_KEY_TO_LABEL[DEFAULT_PROVIDER_KEY],
            "model": _default_model_for_provider(DEFAULT_PROVIDER_KEY),
            "theme": self._DEFAULT_THEME,
            "colors_dark": dict(self._DEFAULT_COLORS_DARK),
            "colors_light": dict(self._DEFAULT_COLORS_LIGHT),
            "blobs_enabled_dark": self._DEFAULT_BLOBS_ENABLED_DARK,
            "blobs_enabled_light": self._DEFAULT_BLOBS_ENABLED_LIGHT,
            # Per-blob on/off, independent of the master dark/light toggles
            # above -- lets each of the four blobs (center/a/b/cursor) be
            # hidden individually. Not per-theme; applies regardless of
            # which theme is active.
            "blobs_individual": {"center": True, "a": True, "b": True, "cursor": True},
            "liquid_glass_enabled": False,
            "liquid_glass_opacity": 55,
            "liquid_glass_sheen_enabled": True,
            "chat_font_size": self._CHAT_FONT_SIZE_DEFAULT,
            # Ambient Surfaces are excluded from screenshots/OCR/screen-share
            # by default (the user still sees them). Turn off to let the
            # model's own screenshots include the floating widgets.
            "ambient_hide_from_capture": True,
            "bg_image": dict(self._DEFAULT_BG_IMAGE),
            "workspaces": [],
            "active_workspace": "",
            # User-saved custom presets, keyed by name -> {base, colors, blobs_enabled}.
            # Distinct from _BUILTIN_PRESETS (read-only, shipped with the app);
            # these are created/deleted via save_preset()/delete_preset() below.
            "custom_presets": {},
        }
        path = self._settings_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if saved.get("provider"):
                    defaults["provider"] = saved["provider"]
                if saved.get("model"):
                    defaults["model"] = saved["model"]
                if saved.get("theme") in ("dark", "light"):
                    defaults["theme"] = saved["theme"]
                if isinstance(saved.get("colors_dark"), dict):
                    defaults["colors_dark"].update(saved["colors_dark"])
                if isinstance(saved.get("colors_light"), dict):
                    defaults["colors_light"].update(saved["colors_light"])
                if (not isinstance(saved.get("colors_dark"), dict)
                        and not isinstance(saved.get("colors_light"), dict)
                        and isinstance(saved.get("colors"), dict)):
                    # Legacy single-shared-palette format from before per-theme
                    # presets existed -- apply it to whichever theme was active
                    # when it was saved, rather than losing it on upgrade.
                    legacy_theme = saved["theme"] if saved.get("theme") in ("dark", "light") else self._DEFAULT_THEME
                    defaults[f"colors_{legacy_theme}"].update(saved["colors"])
                if isinstance(saved.get("blobs_enabled_dark"), bool):
                    defaults["blobs_enabled_dark"] = saved["blobs_enabled_dark"]
                if isinstance(saved.get("blobs_enabled_light"), bool):
                    defaults["blobs_enabled_light"] = saved["blobs_enabled_light"]
                if ("blobs_enabled_dark" not in saved
                        and "blobs_enabled_light" not in saved
                        and isinstance(saved.get("blobs_enabled"), bool)):
                    # Legacy single shared toggle from before per-theme presets
                    # existed -- apply it to whichever theme was active when
                    # it was saved, rather than losing it on upgrade.
                    legacy_theme = saved["theme"] if saved.get("theme") in ("dark", "light") else self._DEFAULT_THEME
                    defaults[f"blobs_enabled_{legacy_theme}"] = saved["blobs_enabled"]
                if isinstance(saved.get("blobs_individual"), dict):
                    for key in ("center", "a", "b", "cursor"):
                        if isinstance(saved["blobs_individual"].get(key), bool):
                            defaults["blobs_individual"][key] = saved["blobs_individual"][key]
                if isinstance(saved.get("liquid_glass_enabled"), bool):
                    defaults["liquid_glass_enabled"] = saved["liquid_glass_enabled"]
                if isinstance(saved.get("liquid_glass_opacity"), (int, float)):
                    defaults["liquid_glass_opacity"] = max(35, min(75, int(saved["liquid_glass_opacity"])))
                if isinstance(saved.get("liquid_glass_sheen_enabled"), bool):
                    defaults["liquid_glass_sheen_enabled"] = saved["liquid_glass_sheen_enabled"]
                if isinstance(saved.get("chat_font_size"), (int, float)) and not isinstance(saved.get("chat_font_size"), bool):
                    defaults["chat_font_size"] = self._clamp_chat_font_size(saved["chat_font_size"])
                if isinstance(saved.get("ambient_hide_from_capture"), bool):
                    defaults["ambient_hide_from_capture"] = saved["ambient_hide_from_capture"]
                if isinstance(saved.get("bg_image"), dict):
                    defaults["bg_image"].update(saved["bg_image"])
                if isinstance(saved.get("workspaces"), list):
                    defaults["workspaces"] = [p for p in saved["workspaces"] if isinstance(p, str) and p]
                if isinstance(saved.get("active_workspace"), str):
                    defaults["active_workspace"] = saved["active_workspace"]
                if isinstance(saved.get("custom_presets"), dict):
                    clean_presets = {}
                    for name, preset in saved["custom_presets"].items():
                        if not isinstance(name, str) or not name or not isinstance(preset, dict):
                            continue
                        if not isinstance(preset.get("colors"), dict):
                            continue
                        clean_presets[name] = {
                            "base": preset.get("base") if preset.get("base") in ("dark", "light") else "dark",
                            "colors": dict(preset["colors"]),
                            "blobs_enabled": bool(preset.get("blobs_enabled", True)),
                        }
                    defaults["custom_presets"] = clean_presets
        except Exception as e:
            self._push_event("log", {"text": f"⚠️ Failed to read saved settings: {e}\n"})
        return defaults

    def get_presets(self):
        """Returns every preset available to the GUI: the built-in, read-only
        Professional presets plus whatever the user has saved themselves.
        Keyed by id -> {label, base, colors, blobs_enabled, builtin}."""
        out = {}
        for pid, preset in self._BUILTIN_PRESETS.items():
            out[pid] = {**preset, "builtin": True}
        for name, preset in self.get_settings().get("custom_presets", {}).items():
            out[f"custom:{name}"] = {
                "label": name, "base": preset["base"], "colors": preset["colors"],
                "blobs_enabled": preset["blobs_enabled"], "builtin": False,
            }
        return out

    def save_preset(self, name: str, base: str, colors: dict, blobs_enabled: bool):
        """Saves (or overwrites) a user-defined custom preset under `name`."""
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "Preset name can't be empty."}
        try:
            current = self.get_settings()
            current["custom_presets"][name] = {
                "base": base if base in ("dark", "light") else "dark",
                "colors": {k: v for k, v in (colors or {}).items() if v},
                "blobs_enabled": bool(blobs_enabled),
            }
            path = self._settings_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2)
            self._push_event("log", {"text": f"💾 [Preset \"{name}\" saved]\n"})
            return {"ok": True, "settings": current}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def delete_preset(self, name: str):
        """Deletes a previously-saved custom preset. Built-in presets can't be
        removed this way -- they simply aren't stored in custom_presets."""
        try:
            current = self.get_settings()
            if name in current["custom_presets"]:
                del current["custom_presets"][name]
            path = self._settings_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2)
            return {"ok": True, "settings": current}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def save_settings(self, settings: dict):
        try:
            current = self.get_settings()
            if settings.get("provider"):
                current["provider"] = settings["provider"]
            if "model" in settings:
                current["model"] = settings["model"] or ""
            if settings.get("theme") in ("dark", "light"):
                current["theme"] = settings["theme"]
            if isinstance(settings.get("colors_dark"), dict):
                current["colors_dark"].update({k: v for k, v in settings["colors_dark"].items() if v})
            if isinstance(settings.get("colors_light"), dict):
                current["colors_light"].update({k: v for k, v in settings["colors_light"].items() if v})
            current.pop("colors", None)  # drop legacy flat key once migrated
            if isinstance(settings.get("blobs_enabled_dark"), bool):
                current["blobs_enabled_dark"] = settings["blobs_enabled_dark"]
            if isinstance(settings.get("blobs_enabled_light"), bool):
                current["blobs_enabled_light"] = settings["blobs_enabled_light"]
            current.pop("blobs_enabled", None)  # drop legacy flat key once migrated
            if isinstance(settings.get("blobs_individual"), dict):
                for key in ("center", "a", "b", "cursor"):
                    if isinstance(settings["blobs_individual"].get(key), bool):
                        current["blobs_individual"][key] = settings["blobs_individual"][key]
            if isinstance(settings.get("liquid_glass_enabled"), bool):
                current["liquid_glass_enabled"] = settings["liquid_glass_enabled"]
            if isinstance(settings.get("liquid_glass_opacity"), (int, float)):
                current["liquid_glass_opacity"] = max(35, min(75, int(settings["liquid_glass_opacity"])))
            if isinstance(settings.get("liquid_glass_sheen_enabled"), bool):
                current["liquid_glass_sheen_enabled"] = settings["liquid_glass_sheen_enabled"]
            if isinstance(settings.get("chat_font_size"), (int, float)) and not isinstance(settings.get("chat_font_size"), bool):
                current["chat_font_size"] = self._clamp_chat_font_size(settings["chat_font_size"])
            if isinstance(settings.get("ambient_hide_from_capture"), bool):
                current["ambient_hide_from_capture"] = settings["ambient_hide_from_capture"]
            if isinstance(settings.get("bg_image"), dict):
                # Path changes only ever come through pick_background_image /
                # clear_background_image (which persist immediately), so
                # this call only touches the display knobs.
                incoming = settings["bg_image"]
                for key in ("enabled", "brightness", "blur", "opacity"):
                    if key in incoming:
                        current["bg_image"][key] = incoming[key]

            path = self._settings_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(current, f, indent=2)

            # Apply the provider/model live for the current session too, so
            # "Save" doesn't require a restart to take effect.
            self.apply_model(current["provider"], current["model"])
            try:
                self._ambient.set_hide_from_capture(current["ambient_hide_from_capture"])
            except Exception:
                pass

            self._push_event("log", {"text": "💾 [Settings saved — will be restored on next launch]\n"})
            return {"ok": True, "settings": current}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_chat_font_size(self, px):
        """Persist ONLY the chat text size (SETTINGS -> Chat Text Size slider).

        The slider repaints instantly in the page and calls this (debounced) so
        the value survives a restart without needing the Save button. It is
        deliberately not routed through save_settings(): that also re-applies
        the saved provider/model, which would clobber a model picked for the
        current session just by nudging a text-size slider.
        """
        try:
            size = self._clamp_chat_font_size(px)
            path = self._settings_path()
            data = {}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                except Exception as e:
                    # Never overwrite a settings file we couldn't read.
                    return {"ok": False, "error": f"settings file unreadable: {e}"}
                if isinstance(loaded, dict):
                    data = loaded
            data["chat_font_size"] = size
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
            return {"ok": True, "chat_font_size": size}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_ambient_hide_from_capture(self, enabled: bool):
        """Live-apply the SETTINGS toggle to every open Ambient Surface (the
        checkbox calls this on change; the value is persisted by Save)."""
        try:
            self._ambient.set_hide_from_capture(bool(enabled))
            return {"ok": True, "hidden": bool(enabled)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _persist_bg_image(self, updates: dict):
        current = self.get_settings()
        current["bg_image"].update(updates)
        path = self._settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        return current

    def _persist_workspaces(self, workspaces=None, active_workspace=None):
        current = self.get_settings()
        if workspaces is not None:
            current["workspaces"] = workspaces
        if active_workspace is not None:
            current["active_workspace"] = active_workspace
        path = self._settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        return current

    def _image_to_data_url(self, path: str):
        """Bake brightness/blur/opacity into the pixels themselves (via
        Pillow) and return a plain PNG data URL with no CSS filter needed
        on the frontend. This is what makes the effect truly static: once
        baked, the browser just paints flat pixels -- there is nothing for
        it to recompute on repaint, which is what caused the continuous
        flashing and the opacity intermittently snapping back to full when
        a live CSS `filter`/`opacity` was being recomputed instead.
        """
        settings = self.get_settings()
        cfg = settings.get("bg_image") or {}
        return self._bake_image(path, cfg.get("brightness", 100), cfg.get("blur", 0), cfg.get("opacity", 100))

    def _bake_image(self, path: str, brightness: int, blur: int, opacity: int):
        try:
            from PIL import Image, ImageEnhance, ImageFilter
            import io
            img = Image.open(path).convert("RGBA")
            # Downscale first -- keeps the Gaussian blur (which is O(radius)
            # per pixel) and the final base64 payload cheap regardless of
            # how large the source photo is. 1920px is plenty for a
            # full-viewport background.
            max_dim = 1920
            if max(img.size) > max_dim:
                scale = max_dim / max(img.size)
                img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
            if brightness and brightness != 100:
                img = ImageEnhance.Brightness(img).enhance(brightness / 100.0)
            if blur and blur > 0:
                img = img.filter(ImageFilter.GaussianBlur(radius=blur))
            if opacity is not None and opacity < 100:
                r, g, b, a = img.split()
                a = a.point(lambda v: int(v * (opacity / 100.0)))
                img.putalpha(a)
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/png;base64,{b64}"
        except ImportError:
            # Pillow isn't installed -- fall back to the raw file with no
            # baking (brightness/blur/opacity controls just won't do
            # anything visually until `pip install pillow` is run).
            ext = os.path.splitext(path)[1].lower()
            mime = self._IMAGE_MIME_TYPES.get(ext, "image/png")
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            return f"data:{mime};base64,{b64}"

    def pick_background_image(self):
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("Image files (*.png;*.jpg;*.jpeg;*.gif;*.webp;*.bmp)", "All files (*.*)"),
            )
        except Exception:
            result = None
        if not result:
            return {"ok": False, "error": "No file selected."}
        path = result[0] if isinstance(result, (list, tuple)) else result
        current = self._persist_bg_image({"path": path, "enabled": True})
        try:
            data_url = self._image_to_data_url(path)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "path": path, "data_url": data_url, "settings": current}

    def get_background_image_data(self):
        settings = self.get_settings()
        path = (settings.get("bg_image") or {}).get("path") or ""
        if not path or not os.path.exists(path):
            return {"ok": False}
        try:
            return {"ok": True, "data_url": self._image_to_data_url(path)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def preview_background_image(self, brightness: int, blur: int, opacity: int):
        """Re-bake using the currently-stored image path but not-yet-saved
        slider values, for live preview. Called debounced from the
        frontend (not on every slider tick) so this stays cheap."""
        settings = self.get_settings()
        path = (settings.get("bg_image") or {}).get("path") or ""
        if not path or not os.path.exists(path):
            return {"ok": False}
        try:
            return {"ok": True, "data_url": self._bake_image(path, brightness, blur, opacity)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def clear_background_image(self):
        current = self._persist_bg_image({"path": "", "enabled": False})
        return {"ok": True, "settings": current}

    # ── Workspace / projects ─────────────────────────────────────────────
    # Workspaces are user-chosen folders (picked via a native folder dialog
    # from the "+ Project" button) whose absolute paths are persisted to
    # the settings file. The dropdown only ever shows folders the user
    # explicitly added -- nothing here is derived by scanning a directory.
    def _scan_workspace_directory(self):
        """Called once at startup. Loads the saved workspace list and
        restores whichever workspace was last active (falling back to the
        first saved one) instead of scanning any directory."""
        settings = self.get_settings()
        workspaces = settings.get("workspaces") or []
        if not workspaces:
            self._push_event("projects", {"projects": workspaces, "active": ""})
            return
        active = settings.get("active_workspace") or ""
        target = active if active in workspaces else workspaces[0]
        self._push_event("projects", {"projects": workspaces, "active": target})
        self.switch_project(target)

    def list_projects(self):
        return self.get_settings().get("workspaces") or []

    def add_workspace_folder(self):
        """Handler for the "+ Project" button: opens a native folder
        picker, saves the chosen absolute path to the persisted workspace
        list (if not already present), switches to it, and returns the
        updated list so the dropdown can be repopulated."""
        try:
            result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            result = None
        if not result:
            return {"ok": False, "error": "No folder selected."}

        folder = os.path.abspath(result[0])
        if not os.path.isdir(folder):
            return {"ok": False, "error": "Selected path is not a folder."}

        workspaces = self.list_projects()
        if folder not in workspaces:
            workspaces = workspaces + [folder]
            self._persist_workspaces(workspaces=workspaces)

        self._push_event("projects", {"projects": workspaces, "active": folder})
        self.switch_project(folder)
        return {"ok": True, "projects": workspaces}

    def remove_workspace_folder(self, path: str):
        """Removes a saved workspace from the list (does not touch the
        folder on disk)."""
        workspaces = [p for p in self.list_projects() if p != path]
        active = self.get_settings().get("active_workspace") or ""
        new_active = active if active in workspaces else (workspaces[0] if workspaces else "")
        self._persist_workspaces(workspaces=workspaces, active_workspace=new_active)
        self._push_event("projects", {"projects": workspaces, "active": new_active})
        if new_active:
            self.switch_project(new_active)
        return {"ok": True, "projects": workspaces}

    def switch_project(self, path: str):
        """Actually switches the active workspace: points memory at the
        chosen folder's project_memory.md, injects that memory into the
        live session, and persists the choice so it's restored next
        launch."""
        project_dir = os.path.abspath(path)
        project_file = os.path.join(project_dir, "project_memory.md")
        midum.memory._active_project_memory_path = project_file
        self._base_work_dir = os.path.dirname(project_dir) or project_dir
        self._persist_workspaces(active_workspace=project_dir)
        name = os.path.basename(project_dir) or project_dir

        if not os.path.exists(project_file):
            try:
                os.makedirs(project_dir, exist_ok=True)
                midum.write_local_file(
                    project_file,
                    f"# Project Memory: {name}\n"
                    f"Created: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                )
            except Exception as e:
                self._push_event("log", {"text": f"⚠️ Memory Write failure: {e}\n"})

        try:
            content = open(project_file, encoding="utf-8").read().strip()
            if content:
                self._session.memory_injections = [
                    inj for inj in self._session.memory_injections
                    if not inj.startswith("[MIDUM PROJECT MEMORY")
                ]
                self._session.memory_injections.append(f"[MIDUM PROJECT MEMORY — {name}]\n{content}")
                self._session.history = [
                    msg for msg in self._session.history
                    if not (msg.get("role") == "system" and msg.get("content", "").startswith("[MIDUM PROJECT MEMORY"))
                ]
                self._session.history.append({
                    "role": "system",
                    "content": f"[MIDUM PROJECT MEMORY — {name}]\n{content}",
                })
        except Exception as e:
            self._push_event("log", {"text": f"⚠️ Context injection failure: {e}\n"})

        #midum.memory.update_memory("master", f"Active project context switched to: {name} ({project_dir})")
        self._push_event("system_line", {"text": f"[Workspace context switched to: {name}]"})
        return self.list_files(project_dir)

    def list_files(self, directory: str = None):
        proj = midum.memory._active_project_memory_path
        directory = directory or (os.path.dirname(proj) if proj else self._base_work_dir)
        out = []
        try:
            if os.path.exists(directory):
                names = os.listdir(directory)
                names.sort(key=lambda x: os.path.isdir(os.path.join(directory, x)), reverse=True)
                for n in names:
                    out.append({"name": n, "dir": os.path.isdir(os.path.join(directory, n))})
        except Exception:
            pass
        return {"root": os.path.basename(directory) if directory else "", "files": out}

    def open_project_in_vscode(self):
        proj = midum.memory._active_project_memory_path
        if not proj:
            return {"ok": False, "error": "No active workspace selected."}
        dir_path = os.path.dirname(proj)
        try:
            subprocess.Popen(f'code "{dir_path}"', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_project_terminal(self):
        proj = midum.memory._active_project_memory_path
        if not proj:
            return {"ok": False, "error": "No active workspace selected."}
        dir_path = os.path.dirname(proj)
        try:
            subprocess.Popen(f'powershell -NoExit -Command "cd \'{dir_path}\'"', shell=True)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Chat history (sidebar) ────────────────────────────────────────────
    def get_titlebar_info(self):
        """Backs the custom titlebar's left-side label + model pill (see
        #app-titlebar / refreshTitlebarInfo() in the HTML/JS below)."""
        return {
            "title": self._chat_title or "Untitled chat",
            "model": self._selected_model or "(auto)",
            "provider": self._selected_provider,
        }

    def set_dpi_scale(self, scale):
        """Called from JS (initTitlebar / a resize listener) with the
        browser's own window.devicePixelRatio -- the ground truth for how
        many physical pixels correspond to one CSS pixel in THIS
        Chromium view. Stashed on the window object so the native
        WM_NCHITTEST hook (_install_win_snap_hook) can convert physical
        hit-test coordinates back into the logical/CSS pixels the
        titlebar's HTML was laid out in, without having to guess at (and
        potentially disagree with) Windows' own GetDpiForWindow value."""
        try:
            self.window._snap_dpi_scale = float(scale) or 1.0
        except Exception:
            pass
        return {"ok": True}

    def set_titlebar_nodrag_zones(self, zones):
        """Called from JS (reportTitlebarNodragZones) with the bounding
        rects (logical/CSS px, {left,top,right,bottom}, viewport-relative --
        which lines up with the titlebar since it's pinned to the top) of
        whatever clickable HTML currently lives inside #titlebar-center
        (the conversation-tab strip, its "+" button, the tab-group
        dropdown). The native WM_NCHITTEST hook (_install_win_snap_hook)
        otherwise treats that entire region as plain OS drag-caption --
        a decision made at the OS level, before Chromium/CSS ever gets a
        say -- so without this, nothing placed there could ever actually
        be clicked on Windows. No-op (and harmless) on platforms without
        that hook."""
        try:
            self.window._nodrag_zones = zones or []
        except Exception:
            pass
        return {"ok": True}

    def titlebar_minimize(self):
        """Minimize button on the custom titlebar. Windows: a real native
        ShowWindow(SW_MINIMIZE) so it behaves exactly like a normal
        titlebar's minimize (taskbar animation, etc.) rather than
        whatever pywebview's own minimize() does on this backend.
        Falls back to pywebview's own method elsewhere / on failure."""
        if sys.platform == "win32" and _win_show_window(self.window, 6):  # SW_MINIMIZE
            return {"ok": True}
        try:
            self.window.minimize()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def titlebar_toggle_maximize(self):
        """Maximize/Restore button. On Windows this checks IsZoomed() and
        flips it with a real ShowWindow call, so Snap Layouts / restoring
        to a previously-snapped half/quarter all behave like a normal
        window. Off Windows, toggle_fullscreen() is the closest pywebview
        equivalent."""
        if sys.platform == "win32":
            hwnd = _find_own_hwnd()
            if hwnd:
                try:
                    import ctypes
                    is_zoomed = bool(ctypes.windll.user32.IsZoomed(hwnd))
                    _win_show_window(self.window, 9 if is_zoomed else 3)  # SW_RESTORE / SW_MAXIMIZE
                    return {"ok": True, "maximized": not is_zoomed}
                except Exception as e:
                    return {"ok": False, "error": str(e)}
        try:
            self.window.toggle_fullscreen()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def titlebar_close(self):
        try:
            self.window.destroy()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def list_chats(self):
        chats = self._chat_store.list_chats()
        for c in chats:
            c["current"] = c["id"] == self._current_chat_id
        return chats

    def load_chat(self, chat_id: str):
        if self._thinking:
            return {"ok": False, "error": "Busy — wait for the current run to finish or abort first."}
        try:
            data = self._chat_store.load(chat_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        self._viewing_agent_tab_id = None
        self._current_chat_id = data.get("id", chat_id)
        self._chat_title = data.get("title")
        history = data.get("history") or []
        with self._session._lock:
            self._session.history = history
            self._session.turn_counter = max(1, sum(1 for m in history if m.get("role") == "user"))
        # Loading a saved chat replaces the CURRENT tab's conversation in
        # place (note: new_session() used to work this way too, but now
        # opens an additional tab instead -- see its own comment) rather
        # than opening a new tab, so conversation_tabs.py's record for the
        # active tab needs its history list mutated to match, not swapped
        # for a different object it doesn't know about.
        import conversation_tabs
        active_id = conversation_tabs.get_active_tab_id()
        if active_id:
            live = conversation_tabs.get_tab_history(active_id)
            if live is not None:
                live[:] = self._session.history
                self._session.history = live
        self._display_log = list(data.get("display", []))
        self._voice_stream_tag = None
        self._voice_stream_idx = None
        # A freshly loaded chat has no in-memory "most recent turn" to edit
        # or regenerate yet -- those markers describe positions in THIS
        # process's _display_log/_session.history, not anything persisted
        # to disk, so they can't be reconstructed from the loaded file.
        self._last_turn_history_start = None
        self._last_turn_display_start = None
        self._last_turn_display_end = None
        # Restore KB Only / Explain Mode toggle state + walkthrough progress
        # exactly as they were when this chat was last saved, so an
        # in-progress KB/Explain walkthrough can be continued seamlessly
        # instead of the user having to re-toggle everything and losing
        # their place in the source. Falls back to "off" defaults for chats
        # saved before this feature existed (no "kb_state" key on disk).
        saved_kb_state = data.get("kb_state") or {}
        self._kb_state = {
            "kb_only": bool(saved_kb_state.get("kb_only", False)),
            "kb_sources": list(saved_kb_state.get("kb_sources") or []),
            "explain_mode": bool(saved_kb_state.get("explain_mode", False)),
            "explain_mode_type": saved_kb_state.get("explain_mode_type") or "part",
        }
        self._explain_progress = dict(data.get("explain_progress") or {})
        self._explain_page_progress = dict(data.get("explain_page_progress") or {})
        # Restore THIS chat's own last Render Surface (see
        # _persist_current_chat) so the "reopen last Render Surface" button
        # brings back the surface that actually belongs to this chat --
        # never the previous chat's -- rather than whatever happened to
        # still be sitting in memory. Deliberately left CLOSED (not
        # auto-reopened) until the user/model asks for it; any panel left
        # open from whichever chat was previously active is force-closed
        # here too, since it belongs to a different conversation now.
        saved_render = data.get("render_surface") or None
        self._render_surface_code  = saved_render.get("code") if saved_render else None
        self._render_surface_title = saved_render.get("title") if saved_render else None
        self._render_surface_open  = False
        self._push_event("render_surface_close", {})
        artifacts.set_active_chat(self._current_chat_id)   # MidumDocs follows the chat too (closes a doc from the old chat)
        if active_id:
            self._tab_gui_state[active_id] = self._snapshot_gui_state()
        self._persist_open_tabs()
        return {"ok": True, "display": self._display_log, "kb_state": self._kb_state}

    def open_chat_in_new_tab(self, chat_id: str):
        """Opening a chat from the sidebar's History list opens it as an
        ADDITIONAL conversation tab (see conversation_tabs.py) -- whatever
        tab is currently active is left completely untouched, unlike
        load_chat() (still used e.g. by "delete_chat" bookkeeping), which
        overwrites the CURRENT tab's conversation in place. The sidebar's
        chat history is a library of saved conversations to reopen; the
        titlebar's tab strip is what's actually open in RAM right now --
        this is what turns a library entry into an open tab."""
        if self._thinking:
            return {"ok": False, "error": "Busy — wait for the current run to finish or abort first."}
        import conversation_tabs
        try:
            data = self._chat_store.load(chat_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        try:
            self._viewing_agent_tab_id = None
            # Resync the OUTGOING tab's stored history before touching any
            # of self._session/_display_log/etc. -- same reasoning as
            # switch_conversation_tab: those attributes are about to be
            # overwritten with the reopened chat's content, but they still
            # belong to whichever tab is active RIGHT NOW until this call
            # finishes and hands the new tab a snapshot of its own.
            self._save_active_tab_gui_state()

            history = list(data.get("history") or [])
            self._current_chat_id = data.get("id", chat_id)
            self._chat_title = data.get("title")
            with self._session._lock:
                self._session.history = history
                self._session.turn_counter = max(1, sum(1 for m in history if m.get("role") == "user"))
            self._display_log = list(data.get("display", []))
            self._voice_stream_tag = None
            self._voice_stream_idx = None
            saved_kb_state = data.get("kb_state") or {}
            self._kb_state = {
                "kb_only": bool(saved_kb_state.get("kb_only", False)),
                "kb_sources": list(saved_kb_state.get("kb_sources") or []),
                "explain_mode": bool(saved_kb_state.get("explain_mode", False)),
                "explain_mode_type": saved_kb_state.get("explain_mode_type") or "part",
            }
            self._explain_progress = dict(data.get("explain_progress") or {})
            self._explain_page_progress = dict(data.get("explain_page_progress") or {})
            saved_render = data.get("render_surface") or None
            self._render_surface_code  = saved_render.get("code") if saved_render else None
            self._render_surface_title = saved_render.get("title") if saved_render else None
            self._render_surface_open  = False
            self._push_event("render_surface_close", {})
            artifacts.set_active_chat(self._current_chat_id)

            # NOW register the new tab, wrapping the reopened chat's own
            # (freshly copied, not shared with any other tab) history list.
            tab_id = conversation_tabs.open_tab(kind="user", history=self._session.history, activate=True)
            self._tab_gui_state[tab_id] = self._snapshot_gui_state()
            self._persist_open_tabs()

            return {"ok": True, "tab_id": tab_id, "tabs": conversation_tabs.list_tabs(),
                    "display": self._display_log, "kb_state": self._kb_state}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def delete_chat(self, chat_id: str):
        self._chat_store.delete(chat_id)
        if chat_id == self._current_chat_id:
            self._start_new_chat_record()
            import conversation_tabs
            active_id = conversation_tabs.get_active_tab_id()
            if active_id:
                self._tab_gui_state[active_id] = self._snapshot_gui_state()
            self._persist_open_tabs()
        return {"ok": True, "chats": self.list_chats()}

    def rename_chat(self, chat_id: str, new_title: str):
        new_title = (new_title or "").strip()
        if not new_title:
            return {"ok": False, "error": "Title can't be empty."}
        try:
            self._chat_store.rename(chat_id, new_title)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if chat_id == self._current_chat_id:
            self._chat_title = new_title
        return {"ok": True, "chats": self.list_chats()}

    def _reset_session_memory_file(self):
        """(Re)creates a blank session-memory file with no active goal.
        Shared by the explicit "New Chat" button and by every app
        launch (see _startup_worker) -- both cases mean the same thing:
        start clean, don't carry the previous goal/notes forward."""
        if os.path.exists(midum.SESSION_MEMORY):
            os.remove(midum.SESSION_MEMORY)
        midum.memory._current_goal = None
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        midum.write_local_file(
            midum.SESSION_MEMORY,
            f"# Midum Session Memory\nSession started: {ts}\n\n"
            f"{midum.GOAL_SECTION_HEADER}\n_No active goal._\n\n"
            f"{midum.GOAL_SECTION_END}\n",
        )

    def new_session(self):
        # The sidebar's "New Chat" button. Now behaves exactly like the
        # titlebar's "+" (new_conversation_tab): opens an ADDITIONAL,
        # blank tab rather than resetting the current tab's conversation
        # in place -- so whatever's currently open (and its history) stays
        # exactly as it was, and "new chat" always means "a new tab",
        # never "erase what I was just looking at". Kept as its own
        # method (rather than just aliasing new_conversation_tab) since
        # the two buttons may want to diverge again later; for now the
        # bodies are intentionally identical.
        if self._thinking:
            return {"ok": False, "error": "Busy — wait for the current run to finish or abort first."}
        import conversation_tabs
        try:
            self._viewing_agent_tab_id = None
            self._save_active_tab_gui_state()
            self._reset_session_memory_file()
            self._session.reset()  # reassigns self._session.history to a brand-new list
            self._start_new_chat_record()
            tab_id = conversation_tabs.open_tab(kind="user", history=self._session.history, activate=True)
            self._tab_gui_state[tab_id] = self._snapshot_gui_state()
            self._persist_open_tabs()
            return {"ok": True, "tab_id": tab_id, "tabs": conversation_tabs.list_tabs(),
                    "display": self._display_log, "kb_state": self._kb_state}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _start_new_chat_record(self):
        self._current_chat_id = uuid.uuid4().hex
        self._chat_title = None
        self._display_log = []
        self._voice_stream_tag = None
        self._voice_stream_idx = None
        self._explain_progress = {}
        self._explain_page_progress = {}
        self._kb_state = {
            "kb_only": False, "kb_sources": [],
            "explain_mode": False, "explain_mode_type": "part",
        }
        # A brand-new chat starts with no Render Surface to reopen -- don't
        # let a surface from whatever chat was active a moment ago leak
        # into it. Also force-close any panel still showing on screen, same
        # reasoning as load_chat.
        self._render_surface_code  = None
        self._render_surface_title = None
        self._render_surface_open  = False
        self._push_event("render_surface_close", {})
        artifacts.set_active_chat(self._current_chat_id)

    # =========================================================================
    # CONVERSATION TABS -- GUI layer over conversation_tabs.py
    # =========================================================================
    # conversation_tabs.py only owns each tab's raw LLM history list (plus
    # auto open/close around a sub-agent's task). Everything else the GUI
    # shows per-conversation -- chat bubbles, KB Only / Explain Mode state,
    # the persisted-chat id/title used by the sidebar's chat history, any
    # open Render Surface -- lives in the plain self._current_chat_id /
    # _chat_title / _display_log / etc attributes, same as before tabs
    # existed. These helpers snapshot that bundle into
    # self._tab_gui_state[tab_id] when leaving a tab and load it back out
    # when entering one, so switching tabs is instant and each tab's chat
    # pane comes back exactly as it was left -- no bleeding between tabs.

    def _blank_gui_state(self) -> dict:
        return {
            'turn_counter': 1,
            'chat_id': uuid.uuid4().hex,
            'chat_title': None,
            'display_log': [],
            'kb_state': {
                'kb_only': False, 'kb_sources': [],
                'explain_mode': False, 'explain_mode_type': 'part',
            },
            'explain_progress': {},
            'explain_page_progress': {},
            'render_surface_code': None,
            'render_surface_title': None,
            # See __init__ -- fresh tab, no editable turn yet.
            'turn_seq': 0,
            'last_turn_history_start': None,
            'last_turn_display_start': None,
            'last_turn_display_end': None,
        }

    def _snapshot_gui_state(self) -> dict:
        return {
            'turn_counter': self._session.turn_counter,
            'chat_id': self._current_chat_id,
            'chat_title': self._chat_title,
            'display_log': list(self._display_log),
            'kb_state': dict(self._kb_state),
            'explain_progress': dict(self._explain_progress),
            'explain_page_progress': dict(self._explain_page_progress),
            'render_surface_code': self._render_surface_code,
            'render_surface_title': self._render_surface_title,
            # Edit Prompt / Regenerate Response markers -- see __init__.
            # Must travel with the rest of this tab's GUI state or
            # switching tabs could leave them pointing at another tab's
            # _display_log/_session.history indices.
            'turn_seq': self._turn_seq,
            'last_turn_history_start': self._last_turn_history_start,
            'last_turn_display_start': self._last_turn_display_start,
            'last_turn_display_end': self._last_turn_display_end,
        }

    def _save_active_tab_gui_state(self):
        import conversation_tabs
        active_id = conversation_tabs.get_active_tab_id()
        if active_id:
            # self._session.history gets reassigned to brand-new list
            # objects in several places while this tab is active
            # (_run_turn after every reply, switch_project, etc.) for
            # reasons that have nothing to do with tabs -- so the object
            # conversation_tabs was handed back at open_tab()/switch_tab()
            # time is very likely NOT the one holding this tab's actual
            # current content anymore. Explicitly resync content here,
            # the one moment that matters: right before switching away.
            conversation_tabs.set_tab_history(active_id, self._session.history)
            self._tab_gui_state[active_id] = self._snapshot_gui_state()

    def _load_tab_gui_state(self, tab_id: str):
        # Makes `tab_id` the live conversation: restores its GUI bundle (or
        # a blank one, for a tab that's never had a turn run in it yet)
        # into the plain self._current_chat_id/_chat_title/etc attributes,
        # and points self._session.history at that tab's OWN live history
        # list (conversation_tabs.get_tab_history) -- the exact same object
        # conversation_tabs holds for it, never a copy, so nothing written
        # during this tab's turns can bleed into any other tab.
        import conversation_tabs
        state = self._tab_gui_state.get(tab_id) or self._blank_gui_state()
        self._session.history = conversation_tabs.get_tab_history(tab_id)
        if self._session.history is None:
            self._session.history = []
        self._session.turn_counter        = state['turn_counter']
        self._current_chat_id             = state['chat_id']
        self._chat_title                  = state['chat_title']
        self._display_log                 = state['display_log']
        self._kb_state                    = state['kb_state']
        self._explain_progress            = state['explain_progress']
        self._explain_page_progress       = state['explain_page_progress']
        self._render_surface_code         = state['render_surface_code']
        self._render_surface_title        = state['render_surface_title']
        # Edit Prompt / Regenerate Response markers -- see __init__. Old
        # tabs saved before this feature existed won't have these keys, so
        # fall back to "no editable turn" rather than KeyError.
        self._turn_seq                    = state.get('turn_seq', 0)
        self._last_turn_history_start     = state.get('last_turn_history_start')
        self._last_turn_display_start     = state.get('last_turn_display_start')
        self._last_turn_display_end       = state.get('last_turn_display_end')
        # Same reasoning as load_chat(): never auto-reopen a Render Surface
        # belonging to the tab being switched TO -- force-close whatever
        # panel is on screen (it belonged to the tab being left).
        self._render_surface_open = False
        self._push_event('render_surface_close', {})
        artifacts.set_active_chat(self._current_chat_id)

    def _agent_tab_display(self, tab_id: str) -> list:
        """Builds a replay-ready 'display' list (the same (tag, text) tuple
        shape as self._display_log -- see appendRow()'s branches in the
        frontend JS) from a sub-agent's own transcript, so clicking that
        agent's conversation tab in the titlebar strip shows its actual
        tool calls and messages, just like a normal chat tab. Read-only:
        this never touches self._session.history or conversation_tabs'
        active-tab pointer -- see switch_conversation_tab for why an agent
        tab must never actually become 'the' active conversation."""
        import conversation_tabs
        tab = conversation_tabs.get_tab(tab_id)
        if not tab or tab['kind'] != 'agent':
            return []
        transcript = midum.get_agent_transcript(tab['owner'])
        display = []
        for entry in transcript:
            role = entry.get('role')
            if role == 'tool':
                try:
                    payload = json.dumps({
                        'name': entry.get('tool_name'),
                        'args': entry.get('args') or {},
                        'result': entry.get('result', ''),
                    }, default=str)
                except Exception:
                    payload = json.dumps({'name': entry.get('tool_name'), 'args': {}, 'result': str(entry.get('result', ''))})
                display.append(('toolcall', payload))
            elif role == 'user':
                # A task the Supervisor handed this agent -- not literally
                # something "the user" typed, so it's tagged "system"
                # (a neutral/dim bubble) rather than "user", with a label
                # making clear what it actually is.
                display.append(('system', f"\U0001f4cb Task: {entry.get('content', '')}"))
            elif role == 'assistant':
                # say() narration, tell_supervisor_to_inform_user() messages,
                # and the final report all land here -- all genuinely this
                # agent's own words, so rendered as normal reply bubbles.
                display.append(('midum', entry.get('content', '')))
        return display

    def list_conversation_tabs(self):
        # Every open conversation tab -- id, title, kind, message count, and
        # which one is active. Backs the titlebar's tab strip.
        #
        # Title resolution: conversation_tabs._derive_title() only knows
        # about a tab's raw history, so left alone it names "user" tabs via
        # its OWN independent model-generated title -- a second, separate
        # title from the one already shown for the same chat in the
        # sidebar's History list (self._chat_title / _tab_gui_state[...]
        # ['chat_title'], set by _set_chat_title_from_first_message and
        # persisted via _persist_current_chat). That mismatch is confusing
        # (the tab strip and the sidebar can end up disagreeing about what
        # a chat is called) and wasteful (two silent model calls to title
        # the same conversation). So here the GUI's own already-known chat
        # title -- the exact name the sidebar's History panel shows for
        # this chat -- wins whenever one exists; conversation_tabs' derived
        # title is only used as a fallback (e.g. brand-new tabs with no
        # messages yet, or agent tabs, which have no sidebar entry at all).
        import conversation_tabs
        rows = conversation_tabs.list_tabs()
        # If we're previewing an agent tab (see switch_conversation_tab),
        # that tab never actually becomes conversation_tabs' own active
        # tab -- so override which row reports active:true here purely for
        # the strip's highlighting, without touching the real active tab
        # (self._session.history keeps pointing at it the whole time).
        # Self-heals if the previewed tab closed since the last check
        # (e.g. the agent finished -- see _on_agent_done) rather than
        # leaving the strip with no active row at all.
        if self._viewing_agent_tab_id and not any(r["id"] == self._viewing_agent_tab_id for r in rows):
            self._viewing_agent_tab_id = None
        if self._viewing_agent_tab_id:
            for row in rows:
                row["active"] = (row["id"] == self._viewing_agent_tab_id)
        for row in rows:
            if row["kind"] != "user":
                continue
            if row["active"]:
                chat_title = self._chat_title
            else:
                chat_title = (self._tab_gui_state.get(row["id"]) or {}).get("chat_title")
            if chat_title:
                row["title"] = chat_title
        return rows

    # ── Persisting/restoring the open tab strip across restarts ──────────
    # Closing and reopening Midum should leave the tab strip exactly as it
    # was -- the same "restore previous session" behavior a browser gives
    # you -- rather than always coming back up with a single blank tab and
    # making the user dig through the sidebar's chat history to reopen
    # whatever they had open. What's persisted is deliberately lightweight:
    # just each open tab's PERSISTED chat id (in tab order) plus which one
    # was active -- not a duplicate copy of any conversation's content,
    # which already lives in CHATS_DIR via self._chat_store. Agent tabs are
    # excluded: they're auto-managed and belong to a specific in-progress
    # task that won't exist anymore after a restart (register_agent_tab /
    # unregister_agent_tab already handle their whole lifecycle).

    def _load_open_tabs_state(self):
        try:
            if os.path.exists(OPEN_TABS_FILE):
                with open(OPEN_TABS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            self._push_event("log", {"text": f"\u26a0\ufe0f Could not read saved tab state: {e}\n"})
        return None

    def _persist_open_tabs(self):
        """
        Snapshot which (non-agent) tabs are currently open, in order, plus
        which one is active, and write it to OPEN_TABS_FILE. Called after
        every tab-topology change (new tab, close tab, switch tab, opening
        a chat into a new tab, opening a tab group, deleting/loading a
        chat that's currently the active tab's own chat_id) so the file
        never goes stale, and read back by _restore_tabs_or_start_fresh()
        on the next launch.

        For the ACTIVE tab, self._current_chat_id is used directly (it's
        always the freshest value -- self._tab_gui_state's copy for the
        active tab is only refreshed on the way OUT of it, see
        _save_active_tab_gui_state). For every other open tab, its chat id
        is read out of self._tab_gui_state, which IS current for any tab
        that isn't the active one. Every tab (blank "New Tab" included)
        already has a real chat id assigned the moment it's created (see
        _blank_gui_state/_start_new_chat_record) -- restoring later just
        means checking whether a file actually exists on disk for that id
        yet, not treating "no id" as a special case here.
        """
        import conversation_tabs
        try:
            active_id = conversation_tabs.get_active_tab_id()
            chat_ids = []
            active_chat_id = None
            for row in conversation_tabs.list_tabs():
                if row["kind"] == "agent":
                    continue
                tab_id = row["id"]
                if tab_id == active_id:
                    chat_id = self._current_chat_id
                else:
                    chat_id = (self._tab_gui_state.get(tab_id) or {}).get("chat_id")
                if not chat_id:
                    continue
                chat_ids.append(chat_id)
                if tab_id == active_id:
                    active_chat_id = chat_id
            payload = {"active_chat_id": active_chat_id, "chat_ids": chat_ids}
            os.makedirs(os.path.dirname(OPEN_TABS_FILE), exist_ok=True)
            tmp_path = OPEN_TABS_FILE + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, OPEN_TABS_FILE)
        except Exception as e:
            self._push_event("log", {"text": f"\u26a0\ufe0f Could not save open-tab state: {e}\n"})

    def _restore_tabs_or_start_fresh(self, sys_prompt: str, memories: list):
        """
        Called once from _startup_worker, immediately after
        self._session.initialise(sys_prompt, memories) has built the base
        system-prompt-seeded history. Reopens every tab that was open the
        last time Midum closed (see _persist_open_tabs/OPEN_TABS_FILE)
        instead of always starting with a single blank tab.

        Falls back to the original single-fresh-tab behavior if there's no
        saved state, or if every saved tab fails to restore (all of their
        chat files were deleted, the save file is corrupted, etc) --
        there must always be at least one open tab (see the invariant on
        conversation_tabs.close_tab), so this can never leave zero tabs
        open even in a total-failure case.
        """
        import conversation_tabs

        def _start_single_fresh_tab():
            self._main_tab_id = conversation_tabs.open_tab(kind="user", history=self._session.history, activate=True)
            self._tab_gui_state[self._main_tab_id] = self._snapshot_gui_state()

        saved = self._load_open_tabs_state() or {}
        chat_ids = saved.get("chat_ids") or []
        if not chat_ids:
            _start_single_fresh_tab()
            self._persist_open_tabs()
            return

        active_chat_id = saved.get("active_chat_id")
        # One independent COPY of the blank seed history per restored blank
        # tab -- never the same list object shared between two tabs, which
        # would violate conversation_tabs.py's "no context bleeding"
        # guarantee the moment either tab got its first message.
        blank_seed = list(self._session.history)

        first_tab_id  = None
        active_tab_id = None

        for chat_id in chat_ids:
            try:
                data = self._chat_store.load(chat_id)
            except Exception:
                data = None

            if data is not None:
                history = list(data.get("history") or [])
                tab_id = conversation_tabs.open_tab(kind="user", history=history, activate=False)
                saved_kb_state = data.get("kb_state") or {}
                self._tab_gui_state[tab_id] = {
                    "turn_counter": max(1, sum(1 for m in history if m.get("role") == "user")),
                    "chat_id": data.get("id", chat_id),
                    "chat_title": data.get("title"),
                    "display_log": list(data.get("display", [])),
                    "kb_state": {
                        "kb_only": bool(saved_kb_state.get("kb_only", False)),
                        "kb_sources": list(saved_kb_state.get("kb_sources") or []),
                        "explain_mode": bool(saved_kb_state.get("explain_mode", False)),
                        "explain_mode_type": saved_kb_state.get("explain_mode_type") or "part",
                    },
                    "explain_progress": dict(data.get("explain_progress") or {}),
                    "explain_page_progress": dict(data.get("explain_page_progress") or {}),
                    # Never auto-reopen a Render Surface on launch -- same
                    # reasoning as load_chat()/_load_tab_gui_state, doubly
                    # so here since nothing is even visible on screen yet
                    # for the surface to belong to.
                    "render_surface_code": None,
                    "render_surface_title": None,
                }
            else:
                # Either a blank "New Tab" that had no messages yet when
                # Midum closed (nothing was ever saved to disk for it --
                # see _persist_current_chat's early-return on an empty
                # _display_log) or a chat that's since been deleted.
                # Recreate an equivalent empty tab, keeping the SAME
                # chat_id so _persist_open_tabs keeps writing a stable id
                # for this tab going forward.
                tab_id = conversation_tabs.open_tab(kind="user", history=list(blank_seed), activate=False)
                blank_state = self._blank_gui_state()
                blank_state["chat_id"] = chat_id
                self._tab_gui_state[tab_id] = blank_state

            if first_tab_id is None:
                first_tab_id = tab_id
            if chat_id == active_chat_id:
                active_tab_id = tab_id

        if first_tab_id is None:
            # Every saved tab failed to restore -- fall back rather than
            # leaving zero tabs open.
            _start_single_fresh_tab()
            self._persist_open_tabs()
            return

        chosen = active_tab_id or first_tab_id
        conversation_tabs.switch_tab(chosen)
        self._load_tab_gui_state(chosen)
        self._persist_open_tabs()

    def new_conversation_tab(self):
        # The titlebar's "+" button. Identical behavior to the sidebar's
        # "New Chat" button (new_session, above) -- both open an
        # additional, blank tab and leave whatever's currently open
        # untouched. Kept as a separate method for the same reason
        # new_session is -- see its comment.
        if self._thinking:
            return {'ok': False, 'error': 'Busy \u2014 wait for the current run to finish or abort first.'}
        import conversation_tabs
        try:
            self._viewing_agent_tab_id = None
            self._save_active_tab_gui_state()
            self._reset_session_memory_file()
            self._session.reset()  # reassigns self._session.history to a brand-new list
            self._start_new_chat_record()
            tab_id = conversation_tabs.open_tab(kind='user', history=self._session.history, activate=True)
            self._tab_gui_state[tab_id] = self._snapshot_gui_state()
            self._persist_open_tabs()
            return {'ok': True, 'tab_id': tab_id, 'tabs': conversation_tabs.list_tabs(),
                    'display': self._display_log, 'kb_state': self._kb_state}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def switch_conversation_tab(self, tab_id: str):
        # Clicking a tab in the titlebar's tab strip. Instant -- no disk
        # I/O, unlike load_chat -- since every open tab's full state
        # already lives in RAM (self._tab_gui_state + conversation_tabs'
        # own history lists).
        if self._thinking:
            return {'ok': False, 'error': 'Busy \u2014 wait for the current run to finish or abort first.'}
        import conversation_tabs
        tab = conversation_tabs.get_tab(tab_id)
        if tab is None:
            return {'ok': False, 'error': f"No tab '{tab_id}' is open."}

        if tab['kind'] == 'agent':
            # Agent tabs are VIEW-ONLY. Never let one become conversation_tabs'
            # own active tab or point self._session.history at it -- 
            # _on_agent_done/_on_agent_inform below append straight to
            # self._session.history/self._session.append assuming it's
            # always the Supervisor's own conversation, and a background
            # sub-agent thread is concurrently mutating this same list on
            # its own. Track the preview separately instead (see
            # list_conversation_tabs) and just hand back a read-only
            # rendering of that agent's transcript.
            self._viewing_agent_tab_id = tab_id
            return {'ok': True, 'tabs': conversation_tabs.list_tabs(),
                    'display': self._agent_tab_display(tab_id),
                    'kb_state': self._blank_gui_state()['kb_state'], 'readonly': True,
                    'agent_name': tab['owner']}

        self._viewing_agent_tab_id = None
        if tab_id == conversation_tabs.get_active_tab_id():
            return {'ok': True, 'tabs': conversation_tabs.list_tabs(),
                    'display': self._display_log, 'kb_state': self._kb_state}
        try:
            self._save_active_tab_gui_state()
            conversation_tabs.switch_tab(tab_id)
            self._load_tab_gui_state(tab_id)
            self._persist_open_tabs()
            return {'ok': True, 'tabs': conversation_tabs.list_tabs(),
                    'display': self._display_log, 'kb_state': self._kb_state}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def close_conversation_tab(self, tab_id: str):
        # Closing a tab in the titlebar. An "agent" tab (a sub-agent
        # actively working) can't be closed by hand -- it closes itself the
        # moment that task finishes (see conversation_tabs.register_agent_tab
        # / unregister_agent_tab). Closing the very last remaining tab isn't
        # allowed to leave zero tabs open -- it's reset in place instead,
        # same effect as clicking "New Chat" on it.
        if self._thinking:
            return {'ok': False, 'error': 'Busy \u2014 wait for the current run to finish or abort first.'}
        import conversation_tabs
        tab = conversation_tabs.get_tab(tab_id)
        if tab is None:
            return {'ok': False, 'error': f"No tab '{tab_id}' is open."}
        if tab['kind'] == 'agent':
            return {'ok': False, 'error': "That tab belongs to a sub-agent that's still working \u2014 it'll close on its own once the task finishes."}

        self._viewing_agent_tab_id = None
        try:
            if conversation_tabs.tab_count() <= 1:
                # Last tab open -- reset it in place rather than closing it.
                self._reset_session_memory_file()
                self._session.reset()
                self._start_new_chat_record()
                live = conversation_tabs.get_tab_history(tab_id)
                live[:] = self._session.history
                self._session.history = live
                self._tab_gui_state[tab_id] = self._snapshot_gui_state()
                self._persist_open_tabs()
                return {'ok': True, 'tabs': conversation_tabs.list_tabs(),
                        'display': self._display_log, 'kb_state': self._kb_state}

            was_active = (tab_id == conversation_tabs.get_active_tab_id())
            self._tab_gui_state.pop(tab_id, None)
            conversation_tabs.close_tab(tab_id)
            result = {'ok': True, 'tabs': conversation_tabs.list_tabs()}
            if was_active:
                new_active = conversation_tabs.get_active_tab_id()
                self._load_tab_gui_state(new_active)
                result['active_tab_id'] = new_active
                result['display'] = self._display_log
                result['kb_state'] = self._kb_state
            self._persist_open_tabs()
            return result
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    # ── Tab groups: named, saveable sets of tabs (see conversation_tabs.py) ──

    def list_conversation_tab_groups(self):
        import conversation_tabs
        return conversation_tabs.get_tab_groups()

    def save_conversation_tab_group(self, name: str):
        # Saves every currently open (non-agent) tab as a named group.
        # Saving again under a name that already exists OVERWRITES it --
        # same behavior as conversation_tabs.save_tab_group.
        import conversation_tabs
        name = (name or '').strip()
        if not name:
            return {'ok': False, 'error': "Group name can't be empty."}
        msg = conversation_tabs.save_tab_group(name)
        if msg.startswith('Nothing to save'):
            return {'ok': False, 'error': msg}
        return {'ok': True, 'message': msg, 'groups': conversation_tabs.get_tab_groups()}

    def open_conversation_tab_group(self, name: str):
        # Reopens a saved tab group: one fresh tab per saved seed, added
        # alongside whatever's already open, switching to the first of them.
        if self._thinking:
            return {'ok': False, 'error': 'Busy \u2014 wait for the current run to finish or abort first.'}
        import conversation_tabs
        try:
            self._save_active_tab_gui_state()
            msg = conversation_tabs.open_tab_group(name, activate_first=True)
            if msg.startswith('No saved'):
                return {'ok': False, 'error': msg}
            # Newly created tabs have no GUI-state bundle yet -- give each
            # one a blank bundle so _load_tab_gui_state below (and any
            # later switch to one of the others) has something to load.
            for row in conversation_tabs.list_tabs():
                if row['id'] not in self._tab_gui_state:
                    self._tab_gui_state[row['id']] = self._blank_gui_state()
            active_id = conversation_tabs.get_active_tab_id()
            self._load_tab_gui_state(active_id)
            self._persist_open_tabs()
            return {'ok': True, 'message': msg, 'tabs': conversation_tabs.list_tabs(),
                    'active_tab_id': active_id, 'display': self._display_log, 'kb_state': self._kb_state}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def delete_conversation_tab_group(self, name: str):
        import conversation_tabs
        msg = conversation_tabs.delete_tab_group(name)
        return {'ok': True, 'message': msg, 'groups': conversation_tabs.get_tab_groups()}

    def _set_chat_title_from_first_message(self, first_message: str):
        """
        Shared by send_message() and _voice_record_transcript(): the first
        time a chat gets a user message, this sets self._chat_title (used
        by both the custom titlebar label -- get_titlebar_info -- and the
        sidebar's persisted chat-history entry -- _persist_current_chat)
        the SAME way conversation_tabs.py names a conversation tab: a
        short, concise, model-generated title rather than a raw truncation
        of the message.

        conversation_tabs.generate_concise_title() returns the truncated
        fallback immediately -- assign that synchronously so the titlebar
        and next persisted save show SOMETHING right away -- and kicks off
        a silent background model call that, on success, calls back into
        _apply_generated_title below to swap the real title in once it's
        ready. Failures are silent (see conversation_tabs.py) and simply
        leave the fallback in place.
        """
        import conversation_tabs
        chat_id_snapshot = self._current_chat_id
        fallback = conversation_tabs.generate_concise_title(
            chat_id_snapshot, first_message,
            on_result=lambda title: self._apply_generated_title(chat_id_snapshot, fallback, title),
        )
        self._chat_title = fallback or None

    def _apply_generated_title(self, chat_id: str, fallback_used: str, title: str):
        """Called (from a background thread) once conversation_tabs has a
        real model-generated title ready. Only applies it if the chat this
        was generated for is STILL the active chat, and its title is STILL
        exactly the fallback we set when generation started -- if either
        has changed (the user switched/closed the chat, or manually
        renamed it via rename_chat in the meantime) this is a no-op rather
        than clobbering something that's no longer ours to update."""
        if chat_id != self._current_chat_id:
            return
        if self._chat_title != fallback_used:
            return
        self._chat_title = title
        self._persist_current_chat()
        self._push_event("chat_title_updated", {"chat_id": chat_id, "title": title})

    def _persist_current_chat(self):
        # Holds _persist_lock for the whole read-snapshot-and-save sequence
        # so a concurrent _display_log.append() (from _run_turn on another
        # thread) can't be interleaved between the emptiness check, the
        # snapshot() call, and list(self._display_log) below -- see the
        # comment on self._persist_lock in __init__ for why that mattered.
        with self._persist_lock:
            if not self._display_log:
                return
            try:
                title = self._chat_title or "Untitled chat"
                render_surface = (
                    {"code": self._render_surface_code, "title": self._render_surface_title}
                    if self._render_surface_code is not None else None
                )
                self._chat_store.save(
                    self._current_chat_id, title, self._session.snapshot(), list(self._display_log),
                    kb_state=dict(self._kb_state),
                    explain_progress=dict(self._explain_progress),
                    explain_page_progress=dict(self._explain_page_progress),
                    render_surface=render_surface,
                )
                # Safety net: guarantees OPEN_TABS_FILE reflects this tab's
                # (now-existing-on-disk) chat id even if some tab-topology
                # call site above ever misses a _persist_open_tabs() call --
                # cheap enough (small JSON, same directory) to just do on
                # every save rather than rely solely on catching every call
                # site by hand.
                self._persist_open_tabs()
            except Exception as e:
                self._push_event("log", {"text": f"⚠ Failed to save chat history: {e}\n"})

    # ── Send / receive ──────────────────────────────────────────────────
    _KB_ONLY_MARKER = "[MIDUM KB-ONLY MODE]"
    _EXPLAIN_MODE_MARKER = "[MIDUM EXPLAIN MODE]"

    # ── File attachments (artifact system) ────────────────────────────────────────────────
    # Files are ingested into storage/artifacts (see artifacts.py), scoped to this
    # chat, then uploaded to the Gemini Files API in the background. The composer
    # polls attach_status() for progress and passes the ids to send_message().
    def attach_limits(self):
        return {"max_bytes": artifacts.MAX_UPLOAD_BYTES, "chunk": 786432}

    def attach_pick_files(self):
        """Native multi-select dialog; files are ingested by path (nothing goes through the JS bridge)."""
        try:
            result = self.window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
        except Exception as e:
            return {"ok": False, "error": str(e), "files": [], "errors": []}
        if not result:
            return {"ok": True, "files": [], "errors": [], "cancelled": True}
        return self.attach_paths(list(result))

    def attach_paths(self, paths: list):
        files, errors = [], []
        for p in paths or []:
            try:
                rec = artifacts.add_file(src_path=p, chat_id=self._current_chat_id)
                files.append(artifacts.public_view(rec))
            except Exception as e:
                errors.append({"name": os.path.basename(str(p)), "error": str(e)})
        return {"ok": True, "files": files, "errors": errors}

    def attach_chunk(self, upload_id: str, name: str, mime: str, index: int, total: int, b64: str):
        """Chunked upload for pasted / dropped files that have no real path."""
        try:
            # Bind the upload to the chat it STARTED in, so switching tabs while a big file is still
            # streaming in can't land it in the wrong tab's composer.
            bound = self.__dict__.setdefault("_upload_chat", {})
            if int(index) == 0 or upload_id not in bound:
                bound[upload_id] = self._current_chat_id
            res = artifacts.receive_chunk(upload_id, name, mime, int(index), int(total), b64,
                                          chat_id=bound[upload_id])
            if int(index) + 1 >= int(total):
                bound.pop(upload_id, None)
            return res
        except Exception as e:
            self.__dict__.get("_upload_chat", {}).pop(upload_id, None)
            artifacts.abort_upload(upload_id)
            return {"ok": False, "error": str(e)}

    def attach_abort(self, upload_id: str):
        self.__dict__.get("_upload_chat", {}).pop(upload_id, None)
        artifacts.abort_upload(upload_id)
        return {"ok": True}

    def attach_pending(self):
        """The prompt box is a per-tab draft: unsent attachments of the ACTIVE tab's chat.
        The frontend calls this whenever the visible chat changes (tab switch, new/closed tab,
        restore on startup) to rebuild its chips -- the files themselves are already stored."""
        try:
            recs = artifacts.pending_records(self._current_chat_id)
            return {"ok": True, "chat_id": self._current_chat_id,
                    "files": [artifacts.public_view(r) for r in recs]}
        except Exception as e:
            return {"ok": False, "error": str(e), "files": []}

    def attach_status(self, ids: list):
        out = []
        for i in ids or []:
            rec = artifacts.get(i)
            if rec:
                out.append(artifacts.public_view(rec))
        return {"files": out}

    def attach_remove(self, art_id: str):
        # Only never-sent uploads are deleted; a file already attached to a sent message stays.
        return {"ok": True, "deleted": bool(artifacts.remove(art_id, only_unsent=True))}

    def attach_thumbnail(self, art_id: str):
        return artifacts.make_thumbnail(art_id)

    def send_message(self, user_input: str, kb_only: bool = False, kb_sources: list = None, explain_mode: bool = False, explain_mode_type: str = "part", attachment_ids: list = None):
        if self._thinking:
            return {"ok": False, "error": "busy"}
        user_input = (user_input or "").strip()
        if not user_input and not attachment_ids:
            return {"ok": False, "error": "empty"}

        # Mark where THIS turn starts in both logs, before appending
        # anything for it, so edit_last_prompt()/regenerate_last_response()
        # can later pop exactly this turn back off and nothing else. See
        # __init__ for why these are tab-scoped GUI state.
        self._turn_seq += 1
        this_turn_id = self._turn_seq
        self._last_turn_history_start = len(self._session.history)
        self._last_turn_display_start = len(self._display_log)
        self._last_turn_display_end = None

        # Attachments: re-tag to THIS chat (they may have been added in another
        # tab before sending) and record them on the display row for chip rendering.
        artifacts.set_active_chat(self._current_chat_id)
        _att_recs = []
        if attachment_ids:
            try:
                _att_recs = artifacts.adopt(list(attachment_ids), self._current_chat_id)
            except Exception as _att_err:
                print(f"⚠️ [attachments: {_att_err}]")
        _att_public = [artifacts.public_view(r) for r in _att_recs]
        if _att_public:
            self._display_log.append(("user", user_input, _att_public))
        else:
            self._display_log.append(("user", user_input))
        if not self._chat_title:
            self._set_chat_title_from_first_message(user_input or (_att_public[0]["name"] if _att_public else ""))
        self._persist_current_chat()

        approval_kw = ["yes", "grant", "approve", "run it", "go ahead", "y"]
        if any(kw in user_input.lower() for kw in approval_kw):
            payload = f"{user_input} [USER MANUALLY GRANTED BYPASS]"
        else:
            payload = (
                f"{user_input}\n\n"
                "[SYSTEM]: For any multi-step task or document processing: "
                "call write_response_memory with a numbered plan FIRST, then execute. "
                "If this requires a shell command, call execute_terminal_command. "
                "If it requires interacting with an app window, call click_ui_element "
                "(UI Automation) — do not use screen/OCR tools unless explicitly asked."
            )

        if _att_recs:
            payload = artifacts.attachment_block(_att_recs, user_text_empty=not user_input) + "\n\n" + payload

        self._session.append({"role": "user", "content": payload})
        self._thinking = True

        # KB Only: only takes effect if both the toggle is on AND at least
        # one PDF source is selected -- otherwise this is a normal turn.
        kb_only = bool(kb_only) and bool(kb_sources)
        # Explain Mode rides on top of KB Only -- it only ever makes sense
        # when there's a KB source to explain, so it's gated the same way.
        explain_mode = bool(explain_mode) and kb_only
        explain_mode_type = "page" if str(explain_mode_type or "part").lower() == "page" else "part"
        # Keep the persisted KB/Explain toggle state in sync with what the
        # frontend is actually sending for this turn, so reopening this chat
        # later restores exactly this configuration (see
        # _persist_current_chat / load_chat) instead of defaulting to off.
        self._kb_state = {
            "kb_only": kb_only,
            "kb_sources": list(kb_sources or []),
            "explain_mode": explain_mode,
            "explain_mode_type": explain_mode_type,
        }
        try:
            kb_context_msg = self._build_kb_only_context_message(kb_sources, explain_mode, user_input, explain_mode_type) if kb_only else None
        except Exception as e:
            # Building the KB/Explain context is the last thing that can fail
            # BEFORE the background thread (and its `finally: self._thinking
            # = False`) starts -- if it raises here instead, self._thinking
            # would stay True forever with no 'done' event ever pushed, and
            # the UI would look permanently stuck "Executing turns..." for
            # every future message too. Fail the turn cleanly instead.
            self._thinking = False
            self._push_event("error_line", {"text": f"[Engine error building KB/Explain context: {e}]"})
            self._push_event("status", {"text": "Error", "level": "err"})
            self._push_event("done", {})
            return {"ok": False, "error": str(e)}
        if explain_mode:
            self._push_event("status", {"text": "Executing turns... (Explaining)", "level": "busy"})
        elif kb_only:
            self._push_event("status", {"text": "Executing turns... (KB Only)", "level": "busy"})
        else:
            self._push_event("status", {"text": "Executing turns...", "level": "busy"})

        threading.Thread(
            target=self._run_turn,
            args=(list(self._session.snapshot()), kb_only, kb_context_msg, this_turn_id),
            daemon=True,
        ).start()
        # turn_id lets the frontend attach the "Edit Prompt" hover button
        # to this exact user row right away -- it doesn't need to wait for
        # the response to finish (or even start), matching the requirement
        # that editing can abort a still-in-progress response.
        return {"ok": True, "turn_id": this_turn_id}

    def send_interjection(self, text: str):
        """Queue a live steering message while a turn is still executing.

        Deliberately NOT send_message: it never starts a new turn, never
        touches self._thinking, and never appends to self._session.history
        directly. It just drops the text into the shared interjection queue
        (state.py), which process_chat_turn (orchestration.py) drains and
        folds into the model's conversation as a synthetic tool-response
        message right after the next tool call finishes -- see
        state.queue_interjection / orchestration.py's tool-call loop.

        Still recorded in _display_log (tagged "interjection", distinct from
        a normal "user" turn) purely so it shows up on reload/tab-switch and
        in exports -- that persistence is independent of, and doesn't affect,
        whether/when the model actually sees it.
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty"}
        if not self._thinking:
            # Nothing running right now for this to steer -- the frontend
            # already gates this (sendMessage() only takes this path while
            # state.thinking is true), so reaching here means the turn
            # finished in the gap between the click and this call landing.
            return {"ok": False, "error": "not_busy"}
        import state as _state
        _state.queue_interjection(text)
        self._display_log.append(("interjection", text))
        self._persist_current_chat()
        return {"ok": True}

    def _build_kb_only_context_message(self, kb_sources: list, explain_mode: bool = False, user_input: str = "", explain_mode_type: str = "part") -> str:
        """Render the selected PDF sources into a single ephemeral system
        message for KB Only mode. Tagged with _KB_ONLY_MARKER so _run_turn
        can strip it back out afterwards -- it must act like a system
        prompt (present every turn it's used) but never actually persist
        into session/chat history.

        Plain KB Only (not explaining) still uses the headings-only
        outline (format_pdf_sources_for_prompt) -- fine for Q&A, cheap on
        context.

        Explain Mode is different: it walks the source(s) one PART at a
        time, where a part is exactly what the user defined from the
        Knowledge tab's Heading Tagger -- the heading level(s) they chose
        as part boundaries, once per source (see
        knowledge_base.build_pdf_source_parts / set_pdf_source_part_levels).
        Progress through each source's parts is tracked server-side in
        self._explain_progress (source name -> current 0-based part index)
        so the runtime -- not the model's own judgement -- decides exactly
        which part is being narrated on any given turn. The model is given
        the FULL raw text of the source every turn (see
        format_pdf_sources_full_text_for_prompt), plus an explicit marker
        naming which part it should actually explain right now and how
        many remain -- so it always has complete context (no reason to go
        looking for the source elsewhere) while still being steered
        through one part at a time. Every line PyMuPDF extracted from the
        source lands in exactly one part (see extract_pdf_lines /
        build_pdf_source_parts), so walking every part in order guarantees
        nothing in the source gets skipped.

        Advancing to the next part only happens when the user's message
        looks like a plain continuation cue ("next", "continue", "go on",
        etc) for a source that's already mid-walkthrough -- a real
        follow-up question keeps the current part in context instead of
        skipping ahead."""
        if not explain_mode:
            rendered = midum.format_pdf_sources_for_prompt(kb_sources)
            if not rendered:
                rendered = "(None of the selected PDF sources could be loaded.)"
            header = (
                f"{self._KB_ONLY_MARKER}\n"
                "KB Only mode is active for this message. Answer using ONLY the "
                "source material below -- it is already the complete, real "
                "content you need. Do not call search_internet, open_url, or any "
                "browser/internet tool, and do not try to read_local_file, "
                "read_file_smart, list_directory, find_file, or any other local "
                "file-reading/discovery tool to go look at the source yourself -- "
                "none of those are available right now, and everything relevant "
                "has already been given to you above. If the sources don't "
                "contain a clear answer, say so plainly instead of guessing or "
                "making things up.\n\n"
            )
            return f"{header}{rendered}"

        if explain_mode_type == "page":
            return self._build_page_explain_message(kb_sources, user_input)

        continuation_re = re.compile(r"^\s*(next|continue|go on|proceed|keep going|carry on)\b", re.IGNORECASE)
        is_continuation = bool(continuation_re.match((user_input or "").strip()))

        source_blocks = []
        for name in kb_sources or []:
            parts = midum.build_pdf_source_parts(name)
            if not parts:
                source_blocks.append(f"### Source: {name}\n(No extractable parts -- check this source has headings tagged and part levels selected in the Knowledge tab.)")
                continue
            idx = self._explain_progress.get(name, 0)
            if is_continuation and name in self._explain_progress:
                idx = min(idx + 1, len(parts) - 1)
            idx = max(0, min(idx, len(parts) - 1))
            self._explain_progress[name] = idx
            part = parts[idx]
            record = midum.read_pdf_source(name)
            title = (record or {}).get("title") or name
            # Full raw text of the WHOLE source, every turn -- not just the
            # current part -- so the model always has complete context and
            # never has a reason to go look for more content elsewhere
            # (e.g. by trying a file-reading tool). The part marker below
            # tells it which part to actually narrate right now; the rest
            # of the text is there for continuity/context only.
            full_text = midum.format_pdf_sources_full_text_for_prompt([name])
            current_marker = (
                f"### EXPLAIN MODE \u2014 Source: {title}\n"
                f"You are now explaining PART {idx + 1} of {len(parts)}: "
                f"\"{part['heading']}\" (starting p.{part.get('page', '?')}). "
                f"The FULL text of this source is included below so you always "
                f"have complete context, but explain ONLY the current part named "
                f"above right now \u2014 don't jump ahead to later parts or "
                f"re-explain earlier ones unless the user asks."
                + (f" After this, {len(parts) - idx - 1} part(s) remain."
                   if idx + 1 < len(parts) else " This is the LAST part of this source.")
                + "\n\n"
            )
            source_blocks.append(current_marker + full_text)
        rendered = "\n\n---\n\n".join(source_blocks) if source_blocks else "(None of the selected PDF sources could be loaded.)"

        header = (
            f"{self._KB_ONLY_MARKER}\n"
            "KB Only mode is active for this message. Answer using ONLY the "
            "source material below -- it is already the complete, real content "
            "you need. Do not call search_internet, open_url, or any "
            "browser/internet tool, and do not try to read_local_file, "
            "read_file_smart, list_directory, find_file, or any other local "
            "file-reading/discovery tool to go look at the source yourself -- "
            "none of those are available right now, and everything relevant has "
            "already been given to you above.\n\n"
        )
        explain_block = (
            f"\n\n{self._EXPLAIN_MODE_MARKER}\n"
            "EXPLAIN MODE is active. Below is the FULL text of each selected "
            "source, exact and complete, followed by a marker telling you which "
            "PART of it the runtime wants you to explain right now (picked based "
            "on the user's own part-boundary choices for that source) -- walk "
            "the user through ONLY that current part as a detailed, guided "
            "explanation -- like a knowledgeable narrator walking someone "
            "through a chapter out loud, the way NotebookLM's 'Deep Dive' "
            "narration does, not like someone citing a document. The rest of "
            "the full text is there purely for your own context/continuity "
            "(e.g. references to earlier or later material) -- don't narrate "
            "parts other than the current one unless the user explicitly asks.\n"
            "- Do NOT go line by line or explain what each individual line "
            "'means' -- that produces a stilted, quote-by-quote commentary "
            "instead of a real explanation. Instead, synthesize the part into a "
            "flowing conceptual explanation: group related lines into the ideas "
            "they form, explain those ideas in your own words, and move "
            "naturally between them like a real lecture or narration would.\n"
            "- 'No line skipped' means every DETAIL survives into your "
            "explanation somewhere -- every name, number, term, example, "
            "definition, cause/effect, and minor point in the part shown below "
            "must show up in what you say, even the small or seemingly "
            "throwaway ones. It does NOT mean restating or interpreting lines "
            "one at a time in their original order and phrasing. Weave details "
            "into the conceptual explanation at the point where they're "
            "relevant, rather than tacking them on as a separate list.\n"
            "- Go deep: explain why these ideas matter, connect them to parts "
            "already covered, and use concrete specifics that are actually "
            "present in the text -- instead of vague, generic filler that could "
            "apply to any topic.\n"
            "- Never write like you are reporting on a document. Do NOT use "
            "phrases like 'the source says', 'the text explains', 'according to "
            "the document'. Speak the content directly and confidently, as if you "
            "are the one explaining the subject to the user.\n"
            "- Use proper explanation structure and formatting: markdown "
            "headings/subheadings, bold/italic emphasis, and bullet or numbered "
            "lists where they genuinely help readability. Render any mathematical "
            "notation, formulas, or equations with LaTeX math syntax rather than "
            "plain text. If (and ONLY if) the part contains a process, sequence, "
            "decision tree, hierarchy, or system with clear steps/branches that "
            "are genuinely easier to follow as a diagram than as prose, render a "
            "flowchart for it -- don't force a flowchart into parts that are "
            "purely conceptual/narrative and don't need one.\n"
            "- End by briefly naming which part you just covered and inviting the "
            "user to say 'next' or 'continue' when ready -- the runtime (not you) "
            "decides and injects which part comes next once they do.\n"
            "- If this is the very first message of the walkthrough, start with a "
            "brief one-line orientation of what the whole source covers, then "
            "explain the current part shown below.\n"
        )
        return f"{header}{rendered}{explain_block}"

    def _build_page_explain_message(self, kb_sources: list, user_input: str = "") -> str:
        """Page-by-Page counterpart to the Part-by-Part walkthrough built
        above. Instead of the user-tagged heading/part structure, this
        walks each selected source one raw PDF PAGE at a time -- no
        heading tagging or part-level selection required at all, since
        page_count is already known the moment a source is registered
        (see knowledge_base.add_pdf_source). Progress is tracked
        server-side in self._explain_page_progress (source name -> current
        1-based page number), advanced on the same "next/continue/..."
        cue used by Part-by-Part mode. The model still gets the FULL text
        of the source every turn (with page markers, see
        knowledge_base.format_pdf_source_page_for_prompt) so it always has
        complete context, plus an explicit marker naming exactly which
        page to explain right now."""
        continuation_re = re.compile(r"^\s*(next|continue|go on|proceed|keep going|carry on)\b", re.IGNORECASE)
        is_continuation = bool(continuation_re.match((user_input or "").strip()))

        source_blocks = []
        for name in kb_sources or []:
            record = midum.read_pdf_source(name)
            page_count = (record or {}).get("page_count") or 0
            if not record or "error" in record or page_count <= 0:
                source_blocks.append(f"### Source: {name}\n(No page count available -- this source may have failed to load.)")
                continue
            page_no = self._explain_page_progress.get(name, 1)
            if is_continuation and name in self._explain_page_progress:
                page_no = min(page_no + 1, page_count)
            page_no = max(1, min(page_no, page_count))
            self._explain_page_progress[name] = page_no
            source_blocks.append(midum.format_pdf_source_page_for_prompt(name, page_no))
        rendered = "\n\n---\n\n".join(source_blocks) if source_blocks else "(None of the selected PDF sources could be loaded.)"

        header = (
            f"{self._KB_ONLY_MARKER}\n"
            "KB Only mode is active for this message. Answer using ONLY the "
            "source material below -- it is already the complete, real content "
            "you need. Do not call search_internet, open_url, or any "
            "browser/internet tool, and do not try to read_local_file, "
            "read_file_smart, list_directory, find_file, or any other local "
            "file-reading/discovery tool to go look at the source yourself -- "
            "none of those are available right now, and everything relevant has "
            "already been given to you above.\n\n"
        )
        explain_block = (
            f"\n\n{self._EXPLAIN_MODE_MARKER}\n"
            "EXPLAIN MODE (Page-by-Page) is active. Below is the FULL text of "
            "each selected source, exact and complete with '[--- PAGE N ---]' "
            "markers showing where each page starts, followed by a marker "
            "telling you which PAGE of it the runtime wants you to explain "
            "right now -- walk the user through ONLY that current page as a "
            "detailed, guided explanation -- like a knowledgeable narrator "
            "walking someone through a page out loud, the way NotebookLM's "
            "'Deep Dive' narration does, not like someone citing a document. "
            "The rest of the full text is there purely for your own context/ "
            "continuity (e.g. references to earlier or later material) -- "
            "don't narrate pages other than the current one unless the user "
            "explicitly asks.\n"
            "- Do NOT go line by line or explain what each individual line "
            "'means' -- that produces a stilted, quote-by-quote commentary "
            "instead of a real explanation. Instead, synthesize the page into a "
            "flowing conceptual explanation: group related lines into the ideas "
            "they form, explain those ideas in your own words, and move "
            "naturally between them like a real lecture or narration would.\n"
            "- 'No line skipped' means every DETAIL survives into your "
            "explanation somewhere -- every name, number, term, example, "
            "definition, cause/effect, and minor point on the page shown above "
            "must show up in what you say, even the small or seemingly "
            "throwaway ones. It does NOT mean restating or interpreting lines "
            "one at a time in their original order and phrasing.\n"
            "- Pages are a raw PDF boundary, not a content boundary -- a "
            "sentence or paragraph can legitimately run across the edge of a "
            "page. If the current page's content clearly continues into the "
            "next page, it's fine to briefly finish that thought as a small "
            "bit of forward explanation. Likewise, if the tail end of the "
            "current page is really the start of the next idea, it's fine to "
            "leave a small amount of it for the next page rather than forcing "
            "an artificial cut. Don't use this as an excuse to drift multiple "
            "pages ahead though -- stay anchored to the current page.\n"
            "- Go deep: explain why these ideas matter, connect them to pages "
            "already covered, and use concrete specifics that are actually "
            "present in the text -- instead of vague, generic filler that could "
            "apply to any topic.\n"
            "- Never write like you are reporting on a document. Do NOT use "
            "phrases like 'the source says', 'the text explains', 'according to "
            "the document'. Speak the content directly and confidently, as if you "
            "are the one explaining the subject to the user.\n"
            "- Use proper explanation structure and formatting: markdown "
            "headings/subheadings, bold/italic emphasis, and bullet or numbered "
            "lists where they genuinely help readability. Render any mathematical "
            "notation, formulas, or equations with LaTeX math syntax rather than "
            "plain text. If (and ONLY if) the page contains a process, sequence, "
            "decision tree, hierarchy, or system with clear steps/branches that "
            "are genuinely easier to follow as a diagram than as prose, render a "
            "flowchart for it -- don't force a flowchart into pages that are "
            "purely conceptual/narrative and don't need one.\n"
            "- End by briefly naming which page you just covered and inviting the "
            "user to say 'next' or 'continue' when ready -- the runtime (not you) "
            "decides and injects which page comes next once they do.\n"
            "- If this is the very first message of the walkthrough, start with a "
            "brief one-line orientation of what the whole source covers, then "
            "explain the current page shown above.\n"
        )
        return f"{header}{rendered}{explain_block}"

    def _run_turn(self, history_snapshot: list, kb_only: bool = False, kb_context_msg: str = None, turn_id: int = None):
        # turn_id is only set for a turn started directly from send_message
        # or regenerate_last_response() -- i.e. one "most recent turn" the
        # Edit Prompt / Regenerate Response buttons are allowed to act on.
        # Agent-follow-up continuations (see the `start_followup` branch in
        # the `finally` block below) pass turn_id=None on purpose: they
        # append MORE content after that turn's own markers, so leaving
        # _last_turn_display_end unset for them makes the staleness check
        # in _pop_last_turn() correctly refuse to edit/regenerate a turn
        # that's no longer really "the last thing in the chat".
        try:
            midum._abort_event.clear()
            artifacts.set_active_chat(self._current_chat_id)
            permissions.set_kb_only_mode(kb_only)
            if kb_only and kb_context_msg:
                # Insert right before the newest (user) message so it's the
                # freshest context for this turn only. It gets stripped
                # back out below before ever touching persisted history.
                insert_at = len(history_snapshot)
                if history_snapshot and history_snapshot[-1].get("role") == "user":
                    insert_at = len(history_snapshot) - 1
                history_snapshot.insert(insert_at, {"role": "system", "content": kb_context_msg})

            reply, tool_outputs = midum.process_chat_turn(
                history_snapshot,
                force_provider=self._selected_provider,
                force_model=self._selected_model or None,
            )

            if kb_only:
                # Strip the ephemeral KB-only context back out -- it must
                # never survive into future turns or the saved chat JSON,
                # exactly like it was never added to begin with.
                history_snapshot = [
                    m for m in history_snapshot
                    if not (m.get("role") == "system" and (m.get("content") or "").startswith(self._KB_ONLY_MARKER))
                ]

            with self._session._lock:
                self._session.history = history_snapshot
                self._session.turn_counter += 1

            cleaned_reply, visuals = self._extract_and_strip_visuals(reply, tool_outputs)
            # Hold _persist_lock across the append(s) AND the persist call
            # that follows, so a concurrent close-triggered persist (see
            # _on_closing / _persist_lock comment in __init__) can never
            # observe self._display_log after the reply was appended but
            # save a snapshot from before it -- it either sees the state
            # fully before this reply or fully after, never a torn mix.
            with self._persist_lock:
                if cleaned_reply:
                    self._display_log.append(("midum", cleaned_reply))
                for lang, body in visuals:
                    block = f"```{lang}\n{body}\n```"
                    self._display_log.append(("midum", block))
                if turn_id is not None:
                    # Freezes the expected length of _display_log for this
                    # turn -- _pop_last_turn() refuses to act if anything
                    # else (an agent follow-up, another message) appended
                    # after this point, instead of silently deleting the
                    # wrong rows.
                    self._last_turn_display_end = len(self._display_log)
            if cleaned_reply:
                self._push_event("reply", {"text": cleaned_reply})
            for lang, body in visuals:
                block = f"```{lang}\n{body}\n```"
                self._push_event("reply", {"text": block})
            self._persist_current_chat()

            threading.Thread(target=midum.python_trigger_memory_update, args=(tool_outputs, reply), daemon=True).start()

            self._push_event("status", {"text": "Ready", "level": "ok"})
            if turn_id is not None:
                # Tells the frontend this turn is now the editable
                # "most recent" one -- see appendRow()/handleTurnReady() in
                # the JS for how the Edit Prompt / Regenerate Response
                # buttons get attached to exactly its two rows.
                self._push_event("turn_ready", {"turn_id": turn_id})
        except Exception as e:
            self._push_event("error_line", {"text": f"[Engine error: {e}]"})
            self._push_event("status", {"text": "Error", "level": "err"})
            self._persist_current_chat()
            if turn_id is not None:
                # Even a failed turn is still "the most recent turn" --
                # its (possibly empty) reply was still appended above via
                # the normal display_log/session.history path, so it's
                # still safe to edit/regenerate.
                with self._persist_lock:
                    self._last_turn_display_end = len(self._display_log)
                self._push_event("turn_ready", {"turn_id": turn_id})
        finally:
            permissions.set_kb_only_mode(False)
            # Atomically: clear _thinking AND drain any agent-done notes
            # that arrived (from other threads, via _on_agent_done) while
            # this turn was running. They couldn't be spliced into this
            # turn's own history_snapshot mid-flight, so they were queued
            # in self._pending_agent_notes instead -- append them onto
            # self._session.history now that it reflects this turn's
            # completed, well-formed state, and restart the Supervisor
            # immediately so it reacts to them instead of them sitting
            # unseen until the user's next message. This same lock is what
            # makes _on_agent_done's own "is a turn already running" check
            # race-free against this clearing of _thinking.
            with self._agent_turn_lock:
                pending = self._pending_agent_notes
                self._pending_agent_notes = []
                if pending:
                    with self._session._lock:
                        self._session.history.extend(pending)
                else:
                    self._thinking = False
                start_followup = bool(pending)
                if start_followup:
                    self._thinking = True
            if start_followup:
                self._push_event("status", {"text": "Executing turns... (agent report)", "level": "busy"})
                threading.Thread(
                    target=self._run_turn,
                    args=(list(self._session.snapshot()), False, None),
                    daemon=True,
                ).start()
            self._push_event("done", {})
            # The window was closed while this reply was still being
            # generated (see _on_closing) -- the close was held off
            # specifically so this reply wouldn't be lost. Now that it's
            # persisted, actually close.
            if self._close_requested:
                self._close_requested = False
                self._persist_current_chat()
                try:
                    midum.stop_scheduler()
                except Exception:
                    pass
                self._closing = True
                if self.window:
                    self._destroy_window_safe()

    _VISUAL_FENCE_LANGS = ("image_data_json", "flowchart_json", "mermaid")
    _TOOL_VISUAL_FENCE_RE = re.compile(r"```(" + "|".join(_VISUAL_FENCE_LANGS) + r")\n(.*?)```", re.DOTALL)
    _ANY_FENCE_RE = re.compile(r"```([\w_]*)\n(.*?)```", re.DOTALL)

    def _extract_and_strip_visuals(self, reply: str, tool_outputs: list):
        visuals = []
        seen = set()
        for out in tool_outputs or []:
            if not isinstance(out, str) or "```" not in out:
                continue
            for lang, body in self._TOOL_VISUAL_FENCE_RE.findall(out):
                body = body.strip()
                if body and body not in seen:
                    seen.add(body)
                    visuals.append((lang, body))
        if not visuals:
            return reply, []

        def strip_if_echoed(m):
            block = m.group(2).strip()
            for _, v in visuals:
                if block and (block in v or v in block):
                    return ""
            return m.group(0)

        cleaned = self._ANY_FENCE_RE.sub(strip_if_echoed, reply).strip()
        return cleaned, visuals

    def abort(self):
        midum._abort_event.set()
        # Also force-cancel anything already in flight on an MCP server
        # (e.g. a Filesystem MCP call) rather than just letting the
        # already-shared _abort_event polling loop notice on its own --
        # this actively cancels the underlying asyncio task instead of only
        # abandoning the wait on it. Native tool calls (run_abortable, see
        # state.py) are already covered by _abort_event.set() above with no
        # extra call needed.
        try:
            n = midum.abort_active_mcp_calls()
            if n:
                self._push_event("log", {"text": f"🛑 Cancelled {n} in-flight MCP call(s)\n"})
        except Exception as _abort_mcp_err:
            print(f"⚠️ [abort_active_mcp_calls failed: {_abort_mcp_err}]")
        self._push_event("status", {"text": "Aborted", "level": "err"})
        self._push_event("log", {"text": "🛑 Execution pipeline aborted by user\n"})
        return {"ok": True}

    # ── Edit Prompt / Regenerate Response ─────────────────────────────────
    # Both act on the single most recent user-initiated turn only (see the
    # _turn_seq/_last_turn_* markers set in send_message/_run_turn above).
    # "Edit Prompt" removes the turn entirely (user message + whatever the
    # model produced for it) and hands the raw prompt text back to the
    # frontend to drop into the input box. "Regenerate Response" keeps the
    # user message and only removes what came after it, then re-runs.
    def edit_last_prompt(self, turn_id: int):
        return self._pop_last_turn(turn_id, put_back_prompt=True)

    def regenerate_last_response(self, turn_id: int):
        return self._pop_last_turn(turn_id, put_back_prompt=False)

    def _pop_last_turn(self, turn_id: int, put_back_prompt: bool) -> dict:
        if not self._gui_edit_lock.acquire(blocking=False):
            return {"ok": False, "error": "busy"}
        try:
            # Stale click guard: the frontend only shows these buttons on
            # the latest turn, but a click that was already in flight when
            # a newer turn started (or a tab switch swapped these markers
            # out from under us) must not touch the wrong rows.
            if turn_id != self._turn_seq:
                return {"ok": False, "error": "stale"}
            if self._last_turn_history_start is None or self._last_turn_display_start is None:
                return {"ok": False, "error": "no_turn"}

            # If this exact turn is still generating, stop it and wait for
            # _run_turn's own thread to actually finish (it still appends
            # whatever partial reply comes back and clears _thinking in its
            # `finally` block -- see _run_turn) before touching history, so
            # we can never truncate out from under that thread mid-write.
            if self._thinking:
                midum._abort_event.set()
                try:
                    midum.abort_active_mcp_calls()
                except Exception as _abort_mcp_err:
                    print(f"⚠️ [abort_active_mcp_calls failed: {_abort_mcp_err}]")
                waited = 0.0
                while self._thinking and waited < 30.0:
                    time.sleep(0.05)
                    waited += 0.05
                if self._thinking:
                    return {"ok": False, "error": "still_busy"}
                # A brand-new turn (agent follow-up chaining, see the
                # `finally` block in _run_turn) may have started the
                # instant this one finished -- re-check staleness now that
                # we know _thinking settled.
                if turn_id != self._turn_seq:
                    return {"ok": False, "error": "stale"}

            # Nothing else may have appended to the log after this turn's
            # own reply finished (an agent follow-up note, for instance) --
            # if it has, the recorded end-marker won't match and we refuse
            # rather than guess which rows to delete.
            if (self._last_turn_display_end is not None
                    and len(self._display_log) != self._last_turn_display_end):
                return {"ok": False, "error": "stale"}

            start_h = self._last_turn_history_start
            start_d = self._last_turn_display_start

            prompt_text = None
            prompt_atts = []
            with self._persist_lock:
                if start_d < len(self._display_log) and self._display_log[start_d][0] == "user":
                    prompt_text = self._display_log[start_d][1]
                    if len(self._display_log[start_d]) > 2:
                        prompt_atts = list(self._display_log[start_d][2] or [])
                if put_back_prompt:
                    del self._display_log[start_d:]
                else:
                    del self._display_log[start_d + 1:]

            with self._session._lock:
                if put_back_prompt:
                    del self._session.history[start_h:]
                else:
                    del self._session.history[start_h + 1:]

            if put_back_prompt:
                self._last_turn_history_start = None
                self._last_turn_display_start = None
                self._last_turn_display_end = None
                self._persist_current_chat()
                return {"ok": True, "text": prompt_text or "", "attachments": prompt_atts}

            # Regenerate: the user message is still sitting at the end of
            # history/display_log exactly as send_message would have left
            # it -- re-run the turn the same way send_message does, just
            # without re-appending the prompt. Reuses the same turn_id
            # (it's still the same prompt, only the answer is new), so
            # _run_turn will overwrite _last_turn_display_end with the new
            # reply's end position once it completes.
            self._last_turn_display_end = None
            self._persist_current_chat()
            self._thinking = True
            self._push_event("status", {"text": "Executing turns...", "level": "busy"})
            threading.Thread(
                target=self._run_turn,
                args=(list(self._session.snapshot()), False, None, turn_id),
                daemon=True,
            ).start()
            return {"ok": True}
        finally:
            self._gui_edit_lock.release()

    # ── Inline ask (approval / choice / text / file) ─────────────────────
    def _handle_gui_ask(self, kind: str, payload: dict) -> str:
        ask_id = uuid.uuid4().hex
        done = threading.Event()
        box = {"value": "[USER CANCELLED]"}
        self._pending_ask[ask_id] = (done, box)
        self._push_event("ask", {"id": ask_id, "kind": kind, "payload": payload})
        done.wait()
        self._pending_ask.pop(ask_id, None)
        return box["value"]

    def answer_ask(self, ask_id: str, value: str):
        entry = self._pending_ask.get(ask_id)
        if not entry:
            return {"ok": False}
        done, box = entry
        box["value"] = value if value else "[USER CANCELLED]"
        done.set()
        return {"ok": True}

    # ── Render Surface ────────────────────────────────────────────────────
    # Called by tools/render_surface_tools.py's write_render_code /
    # edit_render_code / close_render_surface -- i.e. from the turn-
    # execution thread, not the GUI thread, same as _handle_gui_ask above.
    # All state lives in self._render_surface_code (RAM only); the string
    # only ever leaves Python via _push_event, which hands it to the
    # sandboxed iframe's srcdoc on the JS side -- at no point does this
    # code touch the filesystem.
    def _handle_render(self, kind: str, payload: dict) -> str:
        if kind == "write":
            code = payload.get("code", "") or ""
            title = (payload.get("title") or "").strip() or "Render Surface"
            self._render_surface_code = code
            self._render_surface_title = title
            self._render_surface_open = True
            self._push_event("render_surface_open", {"code": code, "title": title})
            return f"Render Surface opened ({len(code)} chars rendered)."

        if kind == "edit":
            if not self._render_surface_open or self._render_surface_code is None:
                return ("[RENDER SURFACE ERROR] Nothing is currently open -- "
                        "call write_render_code first.")
            old = payload.get("old_code", "") or ""
            new = payload.get("new_code", "") or ""
            count = self._render_surface_code.count(old)
            if count == 0:
                return ("[RENDER SURFACE ERROR] old_code was not found in the "
                        "current render surface -- copy it exactly, character "
                        "for character, from what you last wrote.")
            if count > 1:
                return (f"[RENDER SURFACE ERROR] old_code matches {count} "
                         "locations -- include a little more surrounding "
                         "context so it's unique before editing.")
            self._render_surface_code = self._render_surface_code.replace(old, new, 1)
            self._push_event("render_surface_update", {"code": self._render_surface_code})
            return "Render Surface updated."

        if kind == "close":
            self._render_surface_open = False
            # NOTE: intentionally NOT clearing _render_surface_code/_title
            # here (unlike a straight reset) -- keeping the last rendered
            # markup around after close is what lets the "reopen last
            # Render Surface" button (see render_surface_reopen_ui below,
            # wired to the button under Copy Conversation in the chat pane)
            # bring it back without the model having to regenerate anything.
            # It's only ever replaced by a fresh write_render_code/
            # load_render_surface call, never implicitly wiped.
            self._push_event("render_surface_close", {})
            return "Render Surface closed."

        if kind == "reopen":
            if self._render_surface_code is None:
                return ("[RENDER SURFACE ERROR] No Render Surface has been opened "
                        "yet this session -- nothing to reopen.")
            self._render_surface_open = True
            self._push_event("render_surface_open", {
                "code": self._render_surface_code,
                "title": self._render_surface_title or "Render Surface",
            })
            return "Render Surface reopened."

        if kind == "save":
            name = (payload.get("name") or "").strip()
            if not name:
                return "[RENDER SURFACE ERROR] 'name' is required to save."
            if not self._render_surface_open or self._render_surface_code is None:
                return ("[RENDER SURFACE ERROR] Nothing is currently open to save -- "
                        "call write_render_code first.")
            try:
                os.makedirs(_render_surface_tools.RENDER_SURFACES_DIR, exist_ok=True)
                path = _render_surface_tools._saved_surface_path(name)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({
                        "name": name,
                        "title": self._render_surface_title or "Render Surface",
                        "code": self._render_surface_code,
                        "saved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    }, f, indent=2)
            except Exception as e:
                return f"[RENDER SURFACE ERROR] Could not save '{name}': {e}"
            self._push_event("log", {"text": f"\U0001f4be Render Surface saved as '{name}'.\n"})
            return f"Render Surface saved as '{name}'."

        if kind == "load":
            name = (payload.get("name") or "").strip()
            if not name:
                return "[RENDER SURFACE ERROR] 'name' is required to load."
            path = _render_surface_tools._saved_surface_path(name)
            if not os.path.exists(path):
                return f"[RENDER SURFACE ERROR] No saved Render Surface named '{name}'."
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                return f"[RENDER SURFACE ERROR] Could not read saved surface '{name}': {e}"
            code  = data.get("code") or ""
            title = data.get("title") or name
            self._render_surface_code  = code
            self._render_surface_title = title
            self._render_surface_open  = True
            self._push_event("render_surface_open", {"code": code, "title": title})
            return f"Render Surface '{name}' loaded and opened ({len(code)} chars)."

        if kind == "get":
            # Read-only: current source, used by voice-mode delegation
            # (tools/surface_delegate_tools.py) to revise the open surface.
            return self._render_surface_code if (self._render_surface_open and self._render_surface_code) else ""

        return f"[RENDER SURFACE ERROR] unknown action '{kind}'."

    # ── Render Surface -> tool/MCP/Flow bridge ────────────────────────
    # Backs window.midum.call(name, args) inside every Render Surface (see
    # the MIDUM_BRIDGE_SCRIPT injected by openRenderSurface/
    # updateRenderSurfaceCode in the frontend JS below, and the matching
    # postMessage handler that turns a {__midum_call:true,...} message into
    # a call to render_surface_call_tool). This is what lets the surface's
    # own JS invoke Midum tools like ordinary functions -- awaiting a
    # Promise that resolves to the tool's return value -- instead of only
    # being able to push free-form input back into the chat via
    # render_surface_submit. Works for automatic calls the surface's script
    # fires on load/on a timer, AND for calls made in response to user
    # interaction (a button's onclick, a form submit handler, etc) -- both
    # just call window.midum.call the same way.
    #
    # `name` selects which of the three tool universes to call:
    #   "some_native_tool"        -> a built-in Midum tool (tools_schema.py)
    #   "mcp:server/tool_name"    -> one tool on one connected MCP server
    #   "flow:flow_name"          -> a saved Flow (flow_tools.py), run start
    #                                to finish, returning its last step's result
    # A bare name that ISN'T a known native tool is still auto-routed to the
    # right MCP server if exactly one connected server exposes a tool by
    # that name (same autoroute call_mcp_tool already relies on for normal
    # chat turns), so "mcp:" is a disambiguation prefix, not a requirement.
    def _call_any_tool_from_surface(self, tool_name: str, args):
        tool_name = (tool_name or "").strip()
        if not tool_name:
            return "Error: 'tool' must be a non-empty tool/MCP/flow name."
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except Exception:
                return f"Error: 'args' was not valid JSON: {args!r}"
        if not isinstance(args, dict):
            args = {} if args in (None, "") else {"value": args}

        if tool_name.startswith("flow:"):
            flow_name = tool_name[len("flow:"):].strip()
            if not flow_name:
                return "Error: 'flow:' prefix given with no flow name."
            return midum.run_flow(flow_name)

        if tool_name.startswith("mcp:"):
            rest = tool_name[len("mcp:"):]
            server, _, real_tool = rest.partition("/")
            server, real_tool = server.strip(), real_tool.strip()
            if not server or not real_tool:
                return "Error: 'mcp:' prefix must be 'mcp:server/tool_name'."
            return midum.call_mcp_tool(server, real_tool, args)

        if permissions.get_permission(tool_name) == "deny":
            return (f"[PERMISSION DENIED] '{tool_name}' is set to \"Don't Allow\" "
                    f"in the Permissions tab.")

        from midum_mcp.tools import _mcp_autoroute_tool_call
        routed_name, routed_args = _mcp_autoroute_tool_call(tool_name, args)
        return _dispatch_midum_tool(routed_name, routed_args)

    def render_surface_call_tool(self, tool_name: str, args=None):
        """
        JS-callable entry point for window.midum.call(name, args) inside a
        Render Surface. Executes synchronously (the turn-execution thread
        isn't involved -- this runs directly on whatever thread pywebview
        dispatches the JS bridge call on) and returns a JSON-serialisable
        result, which resolves the Promise on the JS side. Every call is
        also recorded through the normal tool-call hook so it shows up in
        the Log pane / tool-call cards exactly like a model-initiated call,
        since it's real tool/MCP/Flow execution either way.
        """
        result = self._call_any_tool_from_surface(tool_name, args)
        try:
            self._handle_tool_call(f"[Render Surface] {tool_name}", args or {}, result)
        except Exception:
            pass
        try:
            json.dumps(result)
            return result
        except Exception:
            return str(result)

    def render_surface_manual_close(self):
        """Called from JS when the user clicks the panel's own ✕ button, so
        Python's notion of the surface's state stays in sync with what the
        user is actually looking at (the JS side already hid it instantly,
        without waiting on this round-trip). Deliberately keeps
        _render_surface_code/_title around afterwards (same reasoning as
        the "close" branch of _handle_render) so render_surface_reopen_ui
        can bring it back."""
        self._render_surface_open = False
        return {"ok": True}

    def render_surface_reopen_ui(self):
        """Called from the 'reopen last Render Surface' button in the chat
        pane (under Copy Conversation) -- brings back whatever was last
        showing, however it was closed (model call, panel's own ✕, or this
        same button previously). No-op error if nothing has ever been
        rendered this session."""
        result = self._handle_render("reopen", {})
        return {"ok": not result.startswith("[RENDER SURFACE ERROR]"), "message": result}

    def render_surface_save_ui(self, name):
        """Called from JS's own 💾 header button -- saves the currently open
        surface under `name` without going through the model at all."""
        result = self._handle_render("save", {"name": name})
        return {"ok": not result.startswith("[RENDER SURFACE ERROR]"), "message": result}

    def render_surface_load_ui(self, name):
        """Called from JS's Saved Render Surfaces picker -- opens a saved
        snapshot without going through the model at all."""
        result = self._handle_render("load", {"name": name})
        return {"ok": not result.startswith("[RENDER SURFACE ERROR]"), "message": result}

    def render_surface_delete_ui(self, name):
        """Called from JS's Saved Render Surfaces picker -- deletes a saved
        snapshot from disk. Pure filesystem op, works even if no surface is
        currently open."""
        result = _render_surface_tools.delete_saved_render_surface(name)
        return {"ok": not result.startswith("[RENDER SURFACE ERROR]"), "message": result}

    def render_surface_list_saved(self):
        """Called from JS to populate the Saved Render Surfaces picker modal."""
        os.makedirs(_render_surface_tools.RENDER_SURFACES_DIR, exist_ok=True)
        items = []
        try:
            files = sorted(f for f in os.listdir(_render_surface_tools.RENDER_SURFACES_DIR) if f.endswith(".json"))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        for fname in files:
            path = os.path.join(_render_surface_tools.RENDER_SURFACES_DIR, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                items.append({
                    "name": data.get("name") or fname[:-5],
                    "title": data.get("title") or fname[:-5],
                    "saved_at": data.get("saved_at", ""),
                })
            except Exception:
                items.append({"name": fname[:-5], "title": fname[:-5], "saved_at": ""})
        return {"ok": True, "items": items}

    # ── Ambient Surface bridges ───────────────────────────────────────
    def _ambient_call_tool(self, surface_id, tool_name, args):
        """midum.call(...) from inside an Ambient Surface."""
        result = self._call_any_tool_from_surface(tool_name, args)
        try:
            self._handle_tool_call(f"[Ambient Surface {surface_id}] {tool_name}", args or {}, result)
        except Exception:
            pass
        try:
            json.dumps(result)
            return result
        except Exception:
            return str(result)

    def _ambient_submit(self, surface_id, data):
        """parent.postMessage(...) from an Ambient Surface -> new chat turn."""
        try:
            rendered = json.dumps(data, default=str)
        except Exception:
            rendered = str(data)
        return self.send_message(f"[Ambient Surface input: {surface_id}]\n{rendered}")

    def _close_ambient_surfaces(self):
        """Overlay windows must not outlive the app (pywebview stays alive
        while ANY window is open)."""
        try:
            self._ambient.close_all()
        except Exception:
            pass

    def render_surface_submit(self, data):
        """Called from JS whenever the sandboxed Render Surface iframe posts
        a message up to the parent window (see the 'message' listener on
        window in the frontend JS, and the postMessage bridge documented on
        write_render_code in tools/render_surface_tools.py). This is what
        lets the model accept input of ANY kind from the surface -- a form
        submit, a button click with a payload, a value picked from a custom
        widget, freeform text typed into the surface's own input, etc --
        instead of the surface being view-only.

        The payload is folded into the conversation as an ordinary user
        turn (same path as send_message) so the model sees it, can react to
        it (including calling write_render_code/edit_render_code again),
        and it's visible/persisted in the chat like anything else the user
        did. Ignored while a turn is already in flight or the surface isn't
        actually open, same busy/empty guards send_message already has.
        """
        if not self._render_surface_open:
            return {"ok": False, "error": "not_open"}
        try:
            rendered = json.dumps(data, default=str)
        except Exception:
            rendered = str(data)
        wrapped = f"[Render Surface input]\n{rendered}"
        return self.send_message(wrapped)

    # ── MidumDocs (LaTeX documents) ────────────────────────────────────────────
    # The editor itself lives in an iframe (gui/midumdocs_assets/editor.html, served by
    # midumdocs.py); it can only talk to its parent page, which relays to these methods.
    def _handle_midumdocs(self, kind, payload):
        if kind == "open":
            self._render_surface_open = False      # the pane takes over from the Render Surface
        if kind in ("open", "changed", "close"):
            self._push_event("midumdocs_" + kind, payload)
            return "ok"
        return f"unknown midumdocs hook kind: {kind}"

    def midumdocs_server_info(self):
        return midumdocs.server_info()

    def midumdocs_state(self, state):
        return midumdocs.update_live(state)

    def midumdocs_save(self, art_id, content, base_version=None, mode="auto"):
        return midumdocs.user_save(art_id, content, base_version, mode)

    def midumdocs_prompt(self, text, ctx=None):
        """The editor's small prompt box: becomes an ordinary user turn that carries the cursor/selection."""
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty"}
        snap = midumdocs.begin_request(ctx)
        return self.send_message(midumdocs.wrap_request(text, snap))

    def midumdocs_revert_last(self, art_id):
        return midumdocs.revert_last(art_id)

    def midumdocs_make_copy(self, art_id):
        return midumdocs.make_copy(art_id)

    def midumdocs_manual_close(self):
        midumdocs.on_closed()
        return {"ok": True}

    def midumdocs_reopen_ui(self):
        return midumdocs.reopen_ui()

    def midumdocs_export(self, kind, name, data):
        """Save the compiled PDF (base64) or the .tex source through a native save dialog."""
        import base64 as _b64
        try:
            res = self.window.create_file_dialog(webview.SAVE_DIALOG, save_filename=str(name or "document"))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not res:
            return {"ok": False, "cancelled": True}
        path = res[0] if isinstance(res, (list, tuple)) else res
        try:
            raw = _b64.b64decode(data) if kind == "pdf" else str(data).encode("utf-8")
            with open(path, "wb") as fh:
                fh.write(raw)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "path": path}

    def pick_file(self, must_exist: bool = True):
        try:
            if must_exist:
                result = self.window.create_file_dialog(webview.OPEN_DIALOG)
            else:
                result = self.window.create_file_dialog(webview.SAVE_DIALOG)
        except Exception:
            result = None
        if result:
            path = result[0] if isinstance(result, (list, tuple)) else result
            return {"path": path}
        return {"path": ""}

    # ── System core / knowledge / skills text files ──────────────────────
    def _sys_core_path(self, selection: str):
        return {
            "Master Memory": midum.MASTER_MEMORY,
            "Session Memory": midum.SESSION_MEMORY,
            "Instructions": midum.INSTRUCTIONS_FILE,
            "Paths": midum.PATHS_FILE,
            "Active Project": midum.memory._active_project_memory_path,
            "Scratchpad": midum.RESPONSE_MEMORY,
        }.get(selection)

    def get_sys_core(self, selection: str):
        path = self._sys_core_path(selection)
        if not path:
            return {"path": None, "content": "(No active file associated with selection)"}
        return {"path": path, "content": self._read_file(path)}

    def save_sys_core(self, selection: str, content: str):
        path = self._sys_core_path(selection)
        if not path:
            return {"ok": False, "error": "No active target resolved."}
        return self._write_file(path, content)

    def list_knowledge_files(self):
        excluded = {"master_memory.md", "session_memory.md", "instructions.md", "paths.md", "response_memory.md"}
        files = []
        if os.path.exists(midum.STORAGE_DIR):
            for f in os.listdir(midum.STORAGE_DIR):
                if f.endswith(".md") and f.lower() not in excluded and os.path.isfile(os.path.join(midum.STORAGE_DIR, f)):
                    files.append(f)
        return sorted(files)

    def get_knowledge_file(self, filename: str):
        path = os.path.join(midum.STORAGE_DIR, filename)
        return {"path": path, "content": self._read_file(path)}

    def save_knowledge_file(self, filename: str, content: str):
        return self._write_file(os.path.join(midum.STORAGE_DIR, filename), content)

    def create_knowledge(self, name: str, description: str):
        try:
            result = midum.create_domain_knowledge(name, description)
            safe = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip().lower())
            return {"ok": True, "message": result, "filename": f"{safe}.md", "files": self.list_knowledge_files()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── PDF Sources (Knowledge tab) ─────────────────────────────────────
    # A PDF source is registered by picking a .pdf file -- only its path,
    # title, and page count are recorded (via PyMuPDF), nothing about its
    # structure. The heading hierarchy comes ONLY from what the user
    # manually tags in the Heading Tagger modal (click a line of the real
    # rendered PDF page, assign it a heading level) -- see
    # get_pdf_page_image / save_pdf_headings below. The user then picks,
    # once per source, which of those tagged levels count as "part"
    # boundaries (set_pdf_source_part_levels) -- Explain Mode walks
    # exactly those parts in order (see _build_kb_only_context_message).
    def list_pdf_sources(self):
        try:
            return midum.list_pdf_sources()
        except Exception:
            return []

    # ── "#" file-path picker (prompt-box live path browser) ─────────────
    # Backs the "#" popup in the prompt box: typing "#" opens a small
    # floating list above the input showing the machine's drives, and
    # clicking/typing into it drills into folders so the user can grab a
    # real file/folder path fast without leaving the keyboard. Kept as two
    # tiny, fast calls (drives, then one dir listing per navigation step)
    # rather than walking the whole tree up front, since a full recursive
    # scan of a drive would be far too slow to feel "live".
    def list_drives(self):
        drives = []
        if sys.platform == "win32":
            import string
            try:
                import ctypes
                bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            except Exception:
                bitmask = None
            for i, letter in enumerate(string.ascii_uppercase):
                letter_path = f"{letter}:\\"
                present = (bitmask >> i) & 1 if bitmask is not None else os.path.exists(letter_path)
                if present and os.path.exists(letter_path):
                    label = letter_path
                    try:
                        if sys.platform == "win32":
                            vol_buf = ctypes.create_unicode_buffer(261)
                            ctypes.windll.kernel32.GetVolumeInformationW(
                                ctypes.c_wchar_p(letter_path), vol_buf, ctypes.sizeof(vol_buf),
                                None, None, None, None, 0,
                            )
                            if vol_buf.value:
                                label = f"{letter_path} ({vol_buf.value})"
                    except Exception:
                        pass
                    drives.append({"name": label, "path": letter_path})
        else:
            # Non-Windows: there's no drive-letter concept, so surface the
            # filesystem root and the user's home dir as the top-level
            # entry points instead.
            drives.append({"name": "/ (root)", "path": "/"})
            home = os.path.expanduser("~")
            if home and os.path.isdir(home):
                drives.append({"name": f"~ ({home})", "path": home})
        return drives

    def list_path_entries(self, path: str):
        """List folders/files directly inside `path`, for the "#" picker.
        Folders are returned before files, both alphabetically
        (case-insensitive) -- errors (permission-denied, unplugged drive,
        etc.) come back as {"ok": False, "error": ...} rather than raising,
        since this is called live as the user types/navigates."""
        try:
            if not path or not os.path.isdir(path):
                return {"ok": False, "error": "Not a folder."}
            entries = []
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        continue
                    entries.append({"name": entry.name, "path": entry.path, "is_dir": is_dir})
            entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
            return {"ok": True, "path": path, "entries": entries}
        except PermissionError:
            return {"ok": False, "error": "Permission denied."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── "/" capability picker (prompt-box live tools/skills/flows/KB browser) ──
    # Backs the "/" popup in the prompt box, mirroring the "#" path picker's
    # UX: typing "/" opens a small floating list showing five top-level
    # capability sections (Native Tools, MCP Tools, Skills, Flows, KBs).
    # Native Tools and MCP Tools drill one level deeper (by functional
    # category / by MCP server) before reaching individual items; Skills,
    # Flows and KBs go straight to their saved-item list. Selecting a leaf
    # item splices its bare name into the message in place of the "/..."
    # trigger text -- nothing here mutates state, it's all read-only listing
    # exactly like list_drives/list_path_entries above.
    def _native_tool_category(self, name: str, desc: str) -> str:
        n = (name or "").lower()
        def hit(*words):
            return any(w in n for w in words)
        if hit("terminal", "wsl", "python_code", "shell"):
            return "Terminal & Code"
        if hit("mcp"):
            return "MCP Management"
        if hit("file", "path", "directory", "docx", "find_file", "explore_path"):
            return "Files & Paths"
        if hit("search", "url", "browser", "web", "cdp"):
            return "Web & Browser"
        if hit("ui_", "uia", "click", "ocr", "window", "screen", "scan_app", "inspect_app", "interact_with_ui"):
            return "UI Automation"
        if hit("skill", "domain", "memory", "knowledge", "kb_", "pdf"):
            return "Knowledge & Memory"
        if hit("flow", "flowchart", "schedule"):
            return "Flows & Scheduling"
        if hit("agent"):
            return "Agents"
        if hit("image", "voice", "generate"):
            return "Media & Voice"
        if hit("ask_user", "say", "wait", "action_loop"):
            return "Conversation Control"
        return "General"

    def list_capability_sections(self):
        try:
            native_count = len(midum.tools)
        except Exception:
            native_count = 0
        try:
            mcp_count = sum(len(h.tools) for h in midum._MCP_SERVERS.values() if h.connected)
        except Exception:
            mcp_count = 0
        try:
            skills_count = len(self.list_skill_files())
        except Exception:
            skills_count = 0
        try:
            flows_count = len(midum.list_flows())
        except Exception:
            flows_count = 0
        try:
            kbs_count = len(self.list_knowledge_files()) + len(self.list_pdf_sources())
        except Exception:
            kbs_count = 0
        return [
            {"key": "native", "label": "Native Tools", "icon": "🔧", "count": native_count},
            {"key": "mcp",    "label": "MCP Tools",    "icon": "🔌", "count": mcp_count},
            {"key": "skills", "label": "Skills",       "icon": "🧠", "count": skills_count},
            {"key": "flows",  "label": "Flows",        "icon": "🔗", "count": flows_count},
            {"key": "kbs",    "label": "KBs",          "icon": "📚", "count": kbs_count},
        ]

    def list_native_tool_categories(self):
        buckets: dict = {}
        for t in midum.tools:
            fn = t.get("function", {})
            name = fn.get("name", "")
            desc = fn.get("description", "") or ""
            cat = self._native_tool_category(name, desc)
            buckets.setdefault(cat, []).append(name)
        return sorted(
            [{"name": cat, "count": len(v)} for cat, v in buckets.items()],
            key=lambda c: c["name"].lower(),
        )

    def list_native_tools_by_category(self, category: str):
        out = []
        for t in sorted(midum.tools, key=lambda t: t["function"]["name"]):
            fn = t["function"]
            name = fn.get("name", "")
            desc = fn.get("description", "") or ""
            if self._native_tool_category(name, desc) != category:
                continue
            short = desc.strip().splitlines()[0][:90] if desc.strip() else ""
            out.append({"name": name, "desc": short})
        return out

    def list_mcp_servers_for_picker(self):
        out = []
        for name in midum._MCP_SERVER_ORDER:
            h = midum._MCP_SERVERS.get(name)
            if not h:
                continue
            out.append({
                "name": name,
                "connected": bool(h.connected),
                "count": len(h.tools) if h.connected else 0,
            })
        return out

    def list_mcp_tools_by_server(self, server: str):
        h = midum._MCP_SERVERS.get(server)
        if not h or not h.connected:
            return []
        out = []
        for td in sorted(h.tools or [], key=lambda t: t.get("name", "")):
            desc = (td.get("description") or "").strip().splitlines()[0][:90] if td.get("description") else ""
            out.append({"name": td.get("name", ""), "desc": desc})
        return out

    def list_skills_capability(self):
        out = []
        for fname in self.list_skill_files():
            name = fname[:-3] if fname.endswith(".md") else fname
            desc = ""
            try:
                path = os.path.join(midum.SKILLS_DIR, fname)
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            desc = line[:90]
                            break
            except Exception:
                pass
            out.append({"name": name, "desc": desc})
        return out

    def list_flows_capability(self):
        out = []
        for fname in midum.list_flows():
            try:
                desc = midum.flow_description(fname) or ""
            except Exception:
                desc = ""
            out.append({"name": fname, "desc": desc[:90]})
        return out

    def list_kbs_capability(self):
        out = []
        for fname in self.list_knowledge_files():
            name = fname[:-3] if fname.endswith(".md") else fname
            out.append({"name": name, "desc": "Domain knowledge"})
        try:
            for nm in self.list_pdf_sources():
                out.append({"name": nm, "desc": "PDF source"})
        except Exception:
            pass
        return out

    def get_pdf_source(self, name: str):
        try:
            record = midum.read_pdf_source(name)
            if record is None:
                return {"ok": False, "error": f"PDF source '{name}' not found."}
            if "error" in record:
                return {"ok": False, "error": record["error"]}
            headings = sorted(record.get("headings") or [], key=lambda h: (h.get("page", 0), h.get("line_id", "")))
            return {
                "ok": True,
                "title": record.get("title"),
                "page_count": record.get("page_count"),
                "headings": headings,
                "part_levels": sorted(record.get("part_levels") or []),
                "available_levels": midum.get_pdf_source_available_levels(name),
                "source_path": record.get("source_path"),
                "description": record.get("description", ""),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_explain_next_part_name(self, kb_sources: list):
        """Read-only lookup of the heading name of the NEXT explain-mode
        part for each given source, for the frontend's 'Next Part' button
        label/prompt -- mirrors the same advance-by-one logic used in
        _build_kb_only_context_message but NEVER mutates
        self._explain_progress or touches the session. Purely cosmetic;
        the real advance still only happens through the normal
        send_message -> _build_kb_only_context_message continuation-regex
        path once the user actually sends a message."""
        try:
            names = []
            for name in kb_sources or []:
                parts = midum.build_pdf_source_parts(name)
                if not parts:
                    continue
                idx = self._explain_progress.get(name, 0)
                if name in self._explain_progress:
                    idx = min(idx + 1, len(parts) - 1)
                idx = max(0, min(idx, len(parts) - 1))
                names.append(parts[idx]["heading"])
            return {"ok": True, "name": " / ".join(n for n in names if n)}
        except Exception as e:
            return {"ok": False, "name": "", "error": str(e)}

    def get_explain_next_page_label(self, kb_sources: list):
        """Page-by-Page counterpart to get_explain_next_part_name -- a
        read-only lookup of the NEXT page number for each given source,
        for the frontend's 'Next Page' button. Never mutates
        self._explain_page_progress; the real advance only happens
        through send_message -> _build_page_explain_message once the user
        actually sends a message."""
        try:
            labels = []
            for name in kb_sources or []:
                record = midum.read_pdf_source(name)
                page_count = (record or {}).get("page_count") or 0
                if not record or "error" in record or page_count <= 0:
                    continue
                page_no = self._explain_page_progress.get(name, 1)
                if name in self._explain_page_progress:
                    page_no = min(page_no + 1, page_count)
                page_no = max(1, min(page_no, page_count))
                labels.append(f"page {page_no}")
            return {"ok": True, "name": " / ".join(labels)}
        except Exception as e:
            return {"ok": False, "name": "", "error": str(e)}

    def get_explain_current_page_index(self, kb_sources: list):
        """Read-only lookup of the CURRENT (already-narrated) page for the
        first of the given sources, used by the 'Open Source' button while
        Page-by-Page Explain Mode is active so it opens the PDF viewer on
        the exact page currently being explained instead of always page 1.
        Unlike get_explain_next_page_label this does NOT peek ahead -- it
        reads self._explain_page_progress as-is and never mutates it."""
        try:
            name = (kb_sources or [None])[0]
            if not name:
                return {"ok": False, "page_index": 0, "error": "no source"}
            page_no = self._explain_page_progress.get(name, 1)
            return {"ok": True, "page_index": max(0, page_no - 1)}
        except Exception as e:
            return {"ok": False, "page_index": 0, "error": str(e)}

    def get_pdf_source_parts(self, name: str):
        """Structured (not preformatted-text) parts list for the Knowledge
        tab's preview table: every line from the raw PDF extraction is
        guaranteed to land in exactly one part (see
        knowledge_base.build_pdf_source_parts) -- this just exposes each
        part's heading/level/page/line-count for display."""
        try:
            parts = midum.build_pdf_source_parts(name)
            return {
                "ok": True,
                "parts": [
                    {"index": i, "heading": p["heading"], "level": p.get("level"),
                     "page": p.get("page"), "line_count": len(p.get("sections") or [])}
                    for i, p in enumerate(parts)
                ],
            }
        except Exception as e:
            return {"ok": False, "error": str(e), "parts": []}

    def set_pdf_source_part_levels(self, name: str, levels: list):
        try:
            msg = midum.set_pdf_source_part_levels(name, levels or [])
            return {"ok": not msg.lower().startswith("pdf source") or "success" in msg.lower(), "message": msg}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def get_pdf_page_image(self, name: str, page_index: int):
        """Render one page of a registered PDF source (0-based page_index)
        as a PNG data URL, plus every text line's bounding box on that
        page (scaled to match the rendered image), for the frontend's
        Heading Tagger modal to draw clickable overlays over the real
        page image -- no structure detection, just what's literally on
        the page."""
        try:
            record = midum.read_pdf_source(name)
            if not record or "error" in record:
                return {"ok": False, "error": f"PDF source '{name}' not found."}
            path = record.get("source_path")
            if not path or not os.path.exists(path):
                return {"ok": False, "error": "Source PDF file not found on disk."}
            import fitz
            doc = fitz.open(path)
            try:
                page_count = doc.page_count
                page_index = max(0, min(int(page_index), page_count - 1))
                zoom = 1.6
                page = doc[page_index]
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                png_bytes = pix.tobytes("png")
                data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

                lines = []
                page_dict = page.get_text("dict")
                line_no = 0
                for block in page_dict.get("blocks", []):
                    for line in block.get("lines", []):
                        spans = line.get("spans", [])
                        text = "".join(s.get("text", "") for s in spans).strip()
                        if not text:
                            continue
                        x0, y0, x1, y1 = line.get("bbox")
                        # Formatting signature (font family, size, bold) for
                        # this line -- same computation knowledge_base.py's
                        # extract_pdf_lines uses, kept in sync so line_ids
                        # AND their reported style line up between the two.
                        # Powers the Heading Tagger's "auto-detect matching
                        # formatting" checkbox: the frontend reads this off
                        # whichever line the user just tagged, then asks
                        # auto_tag_pdf_headings_by_style to find every other
                        # line in the WHOLE document that looks the same.
                        first_span = spans[0] if spans else {}
                        font_name = first_span.get("font", "") or ""
                        size = round(float(first_span.get("size", 0) or 0), 1)
                        flags = int(first_span.get("flags", 0) or 0)
                        bold = bool(flags & (1 << 4)) or "bold" in font_name.lower()
                        lines.append({
                            "line_id": f"p{page_index + 1}_l{line_no}",
                            "page": page_index + 1,
                            "text": text,
                            "x0": x0 * zoom, "y0": y0 * zoom, "x1": x1 * zoom, "y1": y1 * zoom,
                            "font": font_name, "size": size, "bold": bold,
                        })
                        line_no += 1
                return {
                    "ok": True, "data_url": data_url, "lines": lines,
                    "width": pix.width, "height": pix.height,
                    "page_index": page_index, "page_count": page_count,
                }
            finally:
                doc.close()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def auto_tag_pdf_headings_by_style(self, name: str, style: dict, exclude: list = None):
        """Given a formatting signature ({font, size, bold}) captured off
        one line the user just manually tagged, scan the ENTIRE source --
        every page, not just the one currently open in the Heading Tagger
        -- for every other line sharing that same font/size/bold. Backs
        the Heading Tagger's "Auto-detect matching formatting" checkbox:
        tag one line as H1 and every other line that LOOKS like an H1
        (same formatting) is suggested automatically instead of having to
        be clicked and tagged one by one.

        Purely a lookup -- never writes to disk. The modal merges the
        returned lines into its own in-memory tag list at the level the
        user picked; only Save (save_pdf_headings) ever persists anything,
        and the user can still untag any auto-suggested line individually
        before saving. `exclude` is the list of {page, line_id} pairs
        already tagged, so only genuinely new suggestions come back.
        """
        try:
            record = midum.read_pdf_source(name)
            if not record or "error" in record:
                return {"ok": False, "error": f"PDF source '{name}' not found.", "lines": []}
            path = record.get("source_path")
            if not path or not os.path.exists(path):
                return {"ok": False, "error": "Source PDF file not found on disk.", "lines": []}
            exclude_keys = {
                (int(e.get("page")), e.get("line_id"))
                for e in (exclude or []) if e.get("line_id") is not None
            }
            matches = midum.find_pdf_lines_matching_style(path, style or {}, exclude_keys=exclude_keys)
            return {"ok": True, "lines": matches}
        except Exception as e:
            return {"ok": False, "error": str(e), "lines": []}

    def save_pdf_headings(self, name: str, headings: list):
        try:
            msg = midum.set_pdf_source_headings(name, headings or [])
            return {"ok": "success" in msg.lower(), "message": msg}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def add_pdf_source(self):
        try:
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("PDF files (*.pdf)", "All files (*.*)"),
            )
        except Exception:
            result = None
        if not result:
            return {"ok": False, "error": ""}   # user cancelled — no error banner needed
        path = result[0] if isinstance(result, (list, tuple)) else result
        self._push_event("log", {"text": f"📄 Registering PDF source: {path}\n"})
        try:
            safe, record = midum.add_pdf_source(path)
            self._push_event("log", {"text": f"✅ PDF source registered: {safe}.json ({record.get('page_count')} pages) — tag headings from the Knowledge tab next.\n"})
            # Warm this new source's line-extraction cache in the background
            # too (same reasoning as _warm_pdf_source_cache at startup) so
            # it doesn't reintroduce the first-open Knowledge tab lag later.
            threading.Thread(target=midum.build_pdf_source_parts, args=(safe,), daemon=True).start()
            return {"ok": True, "filename": safe, "files": self.list_pdf_sources()}
        except Exception as e:
            self._push_event("log", {"text": f"⚠️ PDF source registration failed: {e}\n"})
            return {"ok": False, "error": str(e)}

    def list_skill_files(self):
        files = []
        if os.path.exists(midum.SKILLS_DIR):
            for f in os.listdir(midum.SKILLS_DIR):
                if f.endswith(".md") and os.path.isfile(os.path.join(midum.SKILLS_DIR, f)):
                    files.append(f)
        return sorted(files)

    def get_skill_file(self, filename: str):
        path = os.path.join(midum.SKILLS_DIR, filename)
        return {"path": path, "content": self._read_file(path)}

    def save_skill_file(self, filename: str, content: str):
        return self._write_file(os.path.join(midum.SKILLS_DIR, filename), content)

    def create_skill(self, name: str, domain: str, description: str):
        try:
            initial = (
                "## Summary\n"
                f"Instructions to execute custom skill workflow on {domain}.\n\n"
                "## Action Checklist\n"
                "1. [ ] State objective details.\n"
                "2. [ ] Invoke terminal execution calls.\n"
            )
            result = midum.create_domain_skill(name, domain, description, initial)
            safe = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip().lower())
            return {"ok": True, "message": result, "filename": f"{safe}.md", "files": self.list_skill_files()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _read_file(self, path):
        try:
            if path and os.path.exists(path):
                return open(path, encoding="utf-8").read()
            return "(File empty or pending setup on disk)"
        except Exception as e:
            return f"Read Error: {e}"

    def _write_file(self, path, content):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content or "")
            self._push_event("log", {"text": f"💾 Updated context file: {path}\n"})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Manual tool runner ────────────────────────────────────────────────
    def list_tool_schemas(self):
        out = []
        for t in sorted(midum.tools, key=lambda t: t["function"]["name"]):
            fn = t["function"]
            out.append({
                "name": fn["name"],
                "properties": fn.get("parameters", {}).get("properties", {}),
                "required": fn.get("parameters", {}).get("required", []),
            })
        return out

    def run_tool(self, tool_name: str, args: dict):
        schema = next((t["function"] for t in midum.tools if t["function"]["name"] == tool_name), None)
        if not schema:
            return {"ok": False, "output": f"Error: '{tool_name}' has no registered schema."}

        props = schema.get("parameters", {}).get("properties", {})
        coerced = {}
        for k, v in (args or {}).items():
            ptype = props.get(k, {}).get("type", "string")
            try:
                if ptype == "integer":
                    coerced[k] = int(v)
                elif ptype == "number":
                    coerced[k] = float(v)
                elif ptype == "boolean":
                    coerced[k] = str(v).strip().lower() in ("1", "true", "yes", "on")
                else:
                    coerced[k] = v
            except (TypeError, ValueError):
                return {"ok": False, "output": f"Error: '{k}' has the wrong type for '{tool_name}'."}

        def worker():
            try:
                out = _dispatch_midum_tool(tool_name, coerced)
                self._push_event("tool_result", {"output": str(out)})
            except Exception as e:
                self._push_event("tool_result", {"output": f"Tool exception:\n{e}\n\n{traceback.format_exc()}"})

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "output": f"[Executing tool sandbox call: {tool_name}...]"}

    # ── Flows (node-graph tab) ──────────────────────────────────────────
    def save_flow(self, name: str, graph: dict, description: str = ""):
        try:
            msg = midum.save_flow(name, graph, description)
            ok = not msg.lower().startswith("error")
            self._push_event("log", {"text": f"{'🔗' if ok else '⚠️'} {msg}\n"})
            return {"ok": ok, "message": msg}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def list_flows(self):
        try:
            return midum.list_flows()
        except Exception:
            return []

    def list_flow_schemas(self):
        """Flow-tool schemas for the Tools tab's separate Flows dropdown."""
        try:
            return midum.list_flow_schemas()
        except Exception:
            return []

    def get_flow_graph(self, name: str):
        """The raw Drawflow graph JSON last saved for `name`, so the Flows
        tab can reload an existing flow into the canvas for editing."""
        try:
            return midum.get_flow_graph(name)
        except Exception:
            return {}

    def delete_flow(self, name: str):
        try:
            msg = midum.delete_flow(name)
            ok = not msg.lower().startswith("error")
            self._push_event("log", {"text": f"{'🗑️' if ok else '⚠️'} {msg}\n"})
            return {"ok": ok, "message": msg}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def is_flow_promoted(self, name: str):
        try:
            return {"ok": True, "promoted": bool(midum.is_flow_promoted(name))}
        except Exception as e:
            return {"ok": False, "promoted": False, "error": str(e)}

    def promote_flow(self, name: str):
        """Promote a saved flow -- mirrors promote_mcp_tool for MCP tools.
        Gives the flow its own schema alongside native tools so the model
        can call it directly by name, without list_saved_flows()/run_flow()
        discovery."""
        result = midum.promote_flow(name)
        return {"ok": not result.lower().startswith("error"), "message": result}

    def demote_flow(self, name: str):
        """Demote a promoted flow back to on-demand discovery only."""
        result = midum.demote_flow(name)
        return {"ok": True, "message": result}

    def run_flow(self, name: str, args: dict = None):
        """Run a saved flow the same way a native tool is run from the
        Tools tab -- in a background thread, pushing the result back as a
        'tool_result' event so the same output box can show it. `args`
        mirrors run_tool()'s `args` dict: one key per Argument node the
        flow declares (see list_flow_schemas()'s 'properties'), collected
        from the Tools tab's per-flow argument fields."""
        def worker():
            try:
                out = midum.run_flow(name, args)
                self._push_event("tool_result", {"output": str(out)})
            except Exception as e:
                self._push_event("tool_result", {"output": f"Flow exception:\n{e}\n\n{traceback.format_exc()}"})
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "output": f"[Running flow: {name}...]"}

    # ── Flow Scheduler ────────────────────────────────────────────────
    def list_schedules(self):
        """Every saved schedule (each with a human-readable 'description'
        field already folded in by scheduler.py) for the Schedule pane."""
        try:
            return midum.list_schedules()
        except Exception:
            return []

    def create_schedule(self, flow_name: str, kind: str, run_at: str = None,
                         every_minutes=None, at_time: str = None, days: list = None):
        try:
            result = midum.create_schedule(
                flow_name, kind, run_at=run_at, every_minutes=every_minutes,
                at_time=at_time, days=days,
            )
            ok = not str(result).lower().startswith("error")
            self._push_event("log", {"text": f"{'⏰' if ok else '⚠️'} {'Schedule created for ' + flow_name if ok else result}\n"})
            return {"ok": ok, "message": result if not ok else "Schedule created.", "id": result if ok else None}
        except Exception as e:
            return {"ok": False, "message": str(e), "id": None}

    def update_schedule(self, schedule_id: str, patch: dict):
        try:
            msg = midum.update_schedule(schedule_id, patch or {})
            ok = not msg.lower().startswith("error")
            return {"ok": ok, "message": msg}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def set_schedule_enabled(self, schedule_id: str, enabled: bool):
        try:
            msg = midum.set_schedule_enabled(schedule_id, enabled)
            ok = not msg.lower().startswith("error")
            return {"ok": ok, "message": msg}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def delete_schedule(self, schedule_id: str):
        try:
            msg = midum.delete_schedule(schedule_id)
            ok = not msg.lower().startswith("error")
            self._push_event("log", {"text": f"{'🗑️' if ok else '⚠️'} {msg}\n"})
            return {"ok": ok, "message": msg}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def list_tool_node_defs(self):
        """
        Every native tool + every tool on every connected MCP server, in
        the shape the Flows tab needs to build one Drawflow node type per
        tool: {type, label, icon, group, params:[{name, type, enum,
        description, required}]}. `type` is what gets embedded as the
        Drawflow node's "name" (tool::<name> or mcp::<server>::<name>) so
        flows.py's codegen can tell tool nodes apart from control-flow
        nodes and from each other.
        """
        def params_from_schema(props: dict, required: list) -> list:
            out = []
            for pname, pdef in (props or {}).items():
                out.append({
                    "name": pname,
                    "type": pdef.get("type", "string"),
                    "enum": pdef.get("enum"),
                    "description": pdef.get("description", ""),
                    "required": pname in (required or []),
                })
            return out

        defs = []
        for t in sorted(midum.tools, key=lambda t: t["function"]["name"]):
            fn = t["function"]
            name = fn["name"]
            props = fn.get("parameters", {}).get("properties", {})
            required = fn.get("parameters", {}).get("required", [])
            desc = (fn.get("description") or "").strip().splitlines()[0] if fn.get("description") else ""
            defs.append({
                "type": f"tool::{name}",
                "label": name,
                "icon": "🔧",
                "group": "Native Tools",
                "tool_name": name,
                "mcp_server": None,
                "desc": desc[:160],
                "kind": classify_tool_kind(name, fn.get("description", "")),
                "params": params_from_schema(props, required),
            })

        for server_name in midum._MCP_SERVER_ORDER:
            handle = midum._MCP_SERVERS.get(server_name)
            if not handle or not handle.connected:
                continue
            for tdef in (handle.tools or []):
                name = tdef["name"]
                schema = tdef.get("input_schema") or tdef.get("inputSchema") or {}
                props = schema.get("properties", {}) if isinstance(schema, dict) else {}
                required = schema.get("required", []) if isinstance(schema, dict) else []
                desc = (tdef.get("description") or "").strip().splitlines()[0] if tdef.get("description") else ""
                defs.append({
                    "type": f"mcp::{server_name}::{name}",
                    "label": name,
                    "icon": "🔌",
                    "group": f"MCP: {server_name}",
                    "tool_name": name,
                    "mcp_server": server_name,
                    "desc": desc[:160],
                    "kind": classify_tool_kind(name, tdef.get("description", "")),
                    "params": params_from_schema(props, required),
                })

        # ── Saved Flows -- registered as nodes too, so a flow can be dropped
        # into another flow's graph and run as one step (composable flows).
        # Flows currently take no external parameters (see
        # flows.list_flow_schemas()'s always-empty properties), so there's
        # no params list to build -- just name + description. Always
        # "hybrid" kind (Sequence-out AND Object-out) since a flow's return
        # value is always potentially worth wiring into a variable or
        # another node downstream, unlike native tools where only some are.
        for fname in midum.list_flows():
            desc = midum.flow_description(fname)
            tag = " [PROMOTED]" if midum.is_flow_promoted(fname) else ""
            defs.append({
                "type": f"flow::{fname}",
                "label": fname + tag,
                "icon": "🔗",
                "group": "Flows",
                "tool_name": fname,
                "mcp_server": None,
                "desc": desc[:160],
                "kind": "hybrid",
                "params": [],
            })
        return defs

    # ── MCP servers ───────────────────────────────────────────────────────
    def list_mcp(self):
        names = list(midum._MCP_SERVER_ORDER)
        out = []
        for name in names:
            h = midum._MCP_SERVERS.get(name)
            if not h:
                continue
            out.append({
                "name": name,
                "connected": bool(h.connected),
                "transport": h.config.get("transport", "stdio"),
                "tool_count": len(h.tools) if h.connected else 0,
                "error": h.error if not h.connected else "",
            })
        return {"servers": out, "sdk_available": bool(midum._MCP_SDK_AVAILABLE)}

    def connect_mcp(self, payload: dict):
        def worker():
            try:
                result = midum.connect_mcp_server(
                    name=payload["name"],
                    transport=payload.get("transport", "stdio"),
                    command=payload.get("command"),
                    args=payload.get("args"),
                    url=payload.get("url"),
                    env=payload.get("env"),
                    headers=payload.get("headers"),
                    persist=payload.get("persist", True),
                )
            except Exception as e:
                result = f"Failed to connect to '{payload['name']}': {e}"
            self._push_event("log", {"text": f"⚙️ {result}\n"})
            self._push_event("mcp_changed", {})

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    def retry_mcp(self, name: str):
        handle = midum._MCP_SERVERS.get(name)
        if not handle:
            return {"ok": False}

        def worker():
            ok, msg = midum._mcp_manager.connect(name, handle.config)
            self._push_event("log", {"text": f"⚙️ {msg}\n"})
            self._push_event("mcp_changed", {})

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    def disconnect_mcp(self, name: str, forget: bool = False):
        def worker():
            result = midum.disconnect_mcp_server(name, forget=forget)
            self._push_event("log", {"text": f"⚙️ {result}\n"})
            self._push_event("mcp_changed", {})

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    # ── Agents (Supervisor / sub-agents tab) ─────────────────────────────────────
    # Thin wrappers around multi_agent.py -- every mutation also pushes an
    # "agents_changed" event so the Agents pane (if open) refreshes itself
    # immediately instead of waiting for its own poll interval, exactly
    # like "mcp_changed" does for the MCP tab.
    def list_agents_ui(self):
        return midum.list_agents_struct()

    def create_agent_ui(self, role: str, name: str, description: str = "", personality: str = "",
                         persistence: bool = False, model: str = "", provider: str = "",
                         knowledge_bases: list = None, skills: list = None):
        msg = midum.start_agent(
            role, name, description, personality, bool(persistence),
            (model or "").strip() or None, (provider or "").strip() or None,
            list(knowledge_bases or []) or None, list(skills or []) or None,
        )
        ok = not msg.lower().startswith("error")
        self._push_event("log", {"text": f"🤖 {msg}\n"})
        self._push_event("agents_changed", {})
        return {"ok": ok, "message": msg}

    def attach_agent_knowledge_ui(self, name: str, knowledge_bases: list):
        """Attach (replacing) the given MD Knowledge Base names to an agent
        -- see multi_agent.attach_agent_knowledge. `knowledge_bases` items
        are filenames as returned by list_knowledge_files() (e.g.
        'blender_notes.md'); the .md suffix is stripped here since the
        backend keys knowledge bases by name-without-extension."""
        names = [os.path.splitext(n)[0] for n in (knowledge_bases or [])]
        msg = midum.attach_agent_knowledge(name, names)
        self._push_event("log", {"text": f"📚 {msg}\n"})
        self._push_event("agents_changed", {})
        return {"ok": True, "message": msg}

    def attach_agent_skills_ui(self, name: str, skills: list):
        """Attach (replacing) the given MD Skill filenames to an agent --
        see multi_agent.attach_agent_skills. `skills` items are filenames
        as returned by list_skill_files() (e.g. 'terrain_debugging.md');
        the .md suffix is stripped here since the backend keys skills by
        name-without-extension."""
        names = [os.path.splitext(n)[0] for n in (skills or [])]
        msg = midum.attach_agent_skills(name, names)
        self._push_event("log", {"text": f"📋 {msg}\n"})
        self._push_event("agents_changed", {})
        return {"ok": True, "message": msg}

    def send_agent_task_ui(self, name: str, task: str):
        msg = midum.send_agent_task(name, task)
        self._push_event("agents_changed", {})
        return {"ok": "queued" in msg.lower(), "message": msg}

    def stop_agent_ui(self, name: str):
        msg = midum.stop_agent(name)
        self._push_event("agents_changed", {})
        return {"ok": True, "message": msg}

    def resume_agent_ui(self, name: str):
        msg = midum.resume_agent(name)
        ok = "started" in msg.lower()
        self._push_event("log", {"text": f"🤖 {msg}\n"})
        self._push_event("agents_changed", {})
        return {"ok": ok, "message": msg}

    def forget_agent_ui(self, name: str):
        msg = midum.forget_agent(name)
        self._push_event("agents_changed", {})
        return {"ok": True, "message": msg}

    def get_agent_transcript_ui(self, name: str):
        return {"transcript": midum.get_agent_transcript(name)}

    def _on_agent_started(self, name: str, role: str):
        """Fired by multi_agent.py (on the AGENT'S OWN worker thread) the
        instant a sub-agent's conversation tab actually opens -- i.e. it
        just started working a queued task (see
        multi_agent.set_agent_started_hook / register_agent_tab). Just
        re-pushes "agents_changed", the same event create_agent_ui/
        send_agent_task_ui/etc already push -- the frontend's handler for
        it already calls buildConversationTabs() (and refreshes the Agents
        pane if it's open), so this needs no new JS: it just makes sure
        that refresh also happens right when the tab shows up, not only
        when the agent was created, queued, or finished."""
        self._push_event("agents_changed", {})

    def _on_agent_done(self, name: str, role: str, report: str):
        """Fired by multi_agent.py (on the AGENT'S OWN worker thread) the
        instant a sub-agent's Action Loop stops and its report is ready.
        Pushes the report straight into the Supervisor's own conversation
        so it's already there for the NEXT time the Supervisor thinks --
        no get_agent_report polling needed -- and, if the Supervisor isn't
        already mid-turn, immediately kicks off a fresh turn so it reacts
        to the report right away instead of only at the user's next
        message."""
        note = (
            f"[SYSTEM — AGENT REPORT]: Sub-agent '{name}' ({role}) just finished its "
            f"task and stopped its Action Loop — you do not need to call "
            f"get_agent_report for it, this is automatic.\n\nIts final report:\n\n{report}\n\n"
            "Relay anything relevant to the user now, or continue with whatever "
            "you were doing."
        )

        # Atomically decide: is a turn already running right now? If so,
        # this note CANNOT be appended straight to self._session.history --
        # a turn in flight owns its own independent history_snapshot copy
        # and self._session.history gets wholesale OVERWRITTEN with that
        # snapshot the moment the turn finishes (see _run_turn), which
        # would silently discard the note. Queue it instead; _run_turn's
        # own finally block (still holding this same lock) merges any
        # queued notes back in right after it writes the finished
        # snapshot, and restarts a follow-up turn if any arrived. When no
        # turn is running, append immediately and start one now, exactly
        # as before -- this is still the common case.
        with self._agent_turn_lock:
            turn_in_flight = self._thinking
            if turn_in_flight:
                self._pending_agent_notes.append({"role": "user", "content": note})
            else:
                self._session.append({"role": "user", "content": note})
                self._thinking = True

        with self._persist_lock:
            self._display_log.append(("system", f"🤖 Agent '{name}' finished its task — see below."))
        self._persist_current_chat()
        self._push_event("system_line", {"text": f"🤖 [Agent '{name}' finished its task]"})
        self._push_event("agents_changed", {})

        # If the person was previewing THIS agent's conversation tab (see
        # switch_conversation_tab), it just closed underneath them --
        # conversation_tabs.unregister_agent_tab already ran, in
        # multi_agent.py's own `finally` block, before this hook fired.
        # Snap the chat pane back to the real active conversation instead
        # of leaving it stuck showing a transcript for a tab that no
        # longer exists (agents_changed above only refreshes the tab
        # STRIP, not whatever's currently rendered in the chat pane).
        import conversation_tabs
        if self._viewing_agent_tab_id and conversation_tabs.get_tab(self._viewing_agent_tab_id) is None:
            self._viewing_agent_tab_id = None
            self._push_event("agent_tab_closed_while_viewing", {
                "display": self._display_log, "kb_state": self._kb_state,
            })

        # If the Supervisor was idle, we already claimed _thinking above
        # (inside the lock, atomically with the check) -- safe to start the
        # turn now. If one was already running, the note is queued and
        # _run_turn will pick it up and restart itself when it finishes.
        if not turn_in_flight:
            self._push_event("status", {"text": "Executing turns... (agent report)", "level": "busy"})
            threading.Thread(
                target=self._run_turn,
                args=(list(self._session.snapshot()), False, None),
                daemon=True,
            ).start()

    def _on_agent_inform(self, name: str, role: str, message: str):
        """Fired by multi_agent.py (on the AGENT'S OWN worker thread) the
        instant a sub-agent calls tell_supervisor_to_inform_user() mid-task.
        Mirrors _on_agent_done's delivery mechanism exactly, just for an
        interim message instead of a final report -- so the Supervisor
        finds out immediately instead of only once the whole task ends."""
        note = (
            f"[SYSTEM — AGENT MESSAGE]: Sub-agent '{name}' ({role}) sent you this "
            f"message while still working on its task (it has NOT finished yet):\n\n"
            f"{message}\n\n"
            "Relay anything relevant to the user now if it's time-sensitive, or note "
            "it and continue with whatever you were doing."
        )

        with self._agent_turn_lock:
            turn_in_flight = self._thinking
            if turn_in_flight:
                self._pending_agent_notes.append({"role": "user", "content": note})
            else:
                self._session.append({"role": "user", "content": note})
                self._thinking = True

        with self._persist_lock:
            self._display_log.append(("system", f"📢 Agent '{name}' sent a message — see below."))
        self._persist_current_chat()
        self._push_event("system_line", {"text": f"📢 [Agent '{name}' sent a message]"})
        self._push_event("agents_changed", {})

        if not turn_in_flight:
            self._push_event("status", {"text": "Executing turns... (agent message)", "level": "busy"})
            threading.Thread(
                target=self._run_turn,
                args=(list(self._session.snapshot()), False, None),
                daemon=True,
            ).start()

    def view_mcp_tools(self, name: str):
        return {"content": midum.show_server_tools(name)}

    def list_mcp_tools_for_promotion(self, name: str):
        """
        Every tool on one connected MCP server, each with its promoted
        status, for the Tools pane opened from the MCP tab.
        """
        handle = midum._MCP_SERVERS.get(name)
        if not handle:
            return {"ok": False, "error": f"Unknown server '{name}'.", "tools": []}
        if not handle.connected:
            return {"ok": False, "error": f"'{name}' is not connected ({handle.error}).", "tools": []}
        out = []
        for tdef in (handle.tools or []):
            desc = (tdef.get("description") or "").strip().splitlines()[0] if tdef.get("description") else ""
            out.append({
                "name": tdef["name"],
                "desc": desc[:200],
                "promoted": bool(midum.is_tool_promoted(name, tdef["name"])),
            })
        return {"ok": True, "server": name, "tools": out}

    def promote_mcp_tool(self, server: str, tool_name: str):
        result = midum.promote_mcp_tool(server, tool_name)
        return {"ok": True, "message": result}

    def demote_mcp_tool(self, server: str, tool_name: str):
        result = midum.demote_mcp_tool(server, tool_name)
        return {"ok": True, "message": result}

    # ── Tool permissions ──────────────────────────────────────────────
    def list_permission_targets(self):
        """
        Every gate-able tool, grouped into native tools + one group per
        connected MCP server, each with the key used to look up/set its
        permission level. MCP tools are re-enumerated live off the current
        connections, so this always reflects what's actually callable
        right now (not a stale snapshot).
        """
        native = []
        for t in sorted(midum.tools, key=lambda t: t["function"]["name"]):
            fn = t["function"]
            desc = (fn.get("description") or "").strip().splitlines()[0] if fn.get("description") else ""
            native.append({"key": fn["name"], "name": fn["name"], "desc": desc[:160]})

        mcp_groups = []
        for server_name in midum._MCP_SERVER_ORDER:
            handle = midum._MCP_SERVERS.get(server_name)
            if not handle:
                continue
            entries = []
            for tdef in (handle.tools or []):
                desc = (tdef.get("description") or "").strip().splitlines()[0] if tdef.get("description") else ""
                entries.append({
                    "key": permissions.mcp_permission_key(server_name, tdef["name"]),
                    "name": tdef["name"],
                    "desc": desc[:160],
                })
            mcp_groups.append({"server": server_name, "connected": bool(handle.connected), "tools": entries})

        return {"native": native, "mcp_groups": mcp_groups}

    def get_permissions(self):
        return permissions.get_all_permissions()

    def set_permission(self, key: str, level: str):
        msg = permissions.set_permission(key, level)
        return {"ok": not msg.lower().startswith("error"), "message": msg}

    def reset_permissions(self):
        msg = permissions.reset_all_permissions()
        return {"ok": True, "message": msg}

    # ── Filesystem MCP approval password ────────────────────────────────
    # Backs the "ask_password" permission level (see permissions.py /
    # midum_mcp/filesystem_policy.py). Deliberately NOT reachable through
    # any model tool -- these Api methods are only ever called from this
    # GUI's Permissions pane, on the GUI thread, from a user click.
    def get_filesystem_password_status(self):
        return {"configured": fs_policy.is_filesystem_password_set()}

    def set_filesystem_password(self, password: str):
        msg = fs_policy.set_filesystem_mcp_password(password)
        return {"ok": not msg.lower().startswith("error"), "message": msg}

    def remove_filesystem_password(self):
        msg = fs_policy.clear_filesystem_mcp_password()
        return {"ok": not msg.lower().startswith("error"), "message": msg}

    def _destroy_window_safe(self):
        """window.destroy() tears down the QWebEngineView (Chromium's Qt
        widget), and Qt widgets may only be destroyed on the GUI thread --
        calling this from a background thread (as _run_turn's finally
        block and shutdown() both can, since js_api calls and worker
        threads run off the GUI thread) doesn't raise, it just leaves the
        teardown half-finished and the whole window silently stops
        responding. QTimer.singleShot(0, ...) marshals the actual
        destroy() call onto the Qt event loop/GUI thread, which is the
        supported way to schedule GUI work from elsewhere. Falls back to
        a direct call only if Qt genuinely isn't available.
        """
        self._close_ambient_surfaces()
        fn = self.window.destroy
        try:
            try:
                from PySide6.QtCore import QTimer
            except ImportError:
                from PyQt5.QtCore import QTimer
            QTimer.singleShot(0, fn)
        except Exception:
            fn()

    def shutdown(self):
        self._persist_current_chat()
        self._stdout_redir.restore()
        try:
            self._ptt_manager.stop()
        except Exception:
            pass
        try:
            midum.stop_scheduler()
        except Exception:
            pass
        self._closing = True
        if self.window:
            self._destroy_window_safe()
        return {"ok": True}

    def _on_closing(self):
        """Registered on window.events.closing (fires for the titlebar X
        too, not just the in-app Shutdown button). Always flush the
        current chat first. Previously the window could close while a
        reply was still being generated: the user's turn had already been
        saved (send_message persists immediately), but the assistant's
        reply is only written once _run_turn finishes -- closing before
        that landed silently dropped the last reply from that chat's
        history. If a turn is in flight, cancel this close (returning
        False does that) and let _run_turn's own finally block finish the
        close once the reply is actually saved.

        A watchdog thread backs this up: if the in-flight turn hasn't
        wrapped up within a bounded window (stuck tool call, a scheduled
        Flow that never returns, etc), the app force-closes anyway instead
        of leaving the window looking permanently frozen.
        """
        self._persist_current_chat()
        if self._thinking:
            self._close_requested = True
            self._push_event("log", {"text": "⏳ Finishing the current response before closing...\n"})
            threading.Thread(target=self._force_close_watchdog, daemon=True).start()
            return False
        try:
            midum.stop_scheduler()
        except Exception:
            pass
        self._close_ambient_surfaces()
        self._closing = True
        return None

    def _force_close_watchdog(self, timeout: float = 20.0):
        """Backstop for _on_closing: if a deferred close (because a turn
        was still 'thinking') hasn't resolved itself within `timeout`
        seconds, force the window closed anyway rather than leaving the
        app stuck forever on a hung tool call or stalled background task.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._thinking:
                return   # _run_turn's own finally block will close it normally
            time.sleep(0.5)
        if self._thinking and self.window:
            self._thinking = False
            self._close_requested = False
            self._closing = True
            try:
                midum.stop_scheduler()
            except Exception:
                pass
            self._destroy_window_safe()


# =============================================================================
# FRONTEND — single-file HTML/CSS/JS, rendered by the OS Chromium engine.
# =============================================================================
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Midum Control Center</title>
<!-- Modern monospace font for chat code blocks (pre.code-block / code.inline-code
     below). Loaded as a plain <link> rather than through the lazy _loadKatexOnce-
     style JS loader used for KaTeX/Mermaid: it's just CSS (no heavy JS payload),
     and the font-family fallback stack (Cascadia Code/Fira Code/Consolas) already
     covers the case where this fails to load, e.g. offline. -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&display=swap">
<!-- Feather Icons -- tiny external icon library used purely for the custom
     titlebar's minimize/maximize/close glyphs (see #app-titlebar below).
     Loaded from a CDN like the Google Font above; if it fails to load
     (offline first run) initTitlebar() falls back to plain glyph text so
     the buttons still work, just without the vector icons. -->
<script src="https://unpkg.com/feather-icons"></script>
<style>
:root{
  --bg:#05070c; --panel:#0b0f19; --surface:#0d1220; --surface2:#121a2c;
  --border:#141a26; --border2:#1f2937;
  --accent:#60a5fa; --accent-dim:#3b82f6; --accent-faint:#0f1e33;
  --accent2:#1d4ed8; --green:#10b981; --red:#ef4444; --yellow:#f59e0b;
  --text:#f3f4f6; --subtext:#9ca3af; --muted:#4b5563;
  --user-msg:#0f1e33; --midum-msg:#0a0e17;
  --tool-bg:#05070c; --tool-text:#93c5fd;
  --gap:14px; --radius:24px; --ease:cubic-bezier(.65,0,.35,1);
  /* User-configurable ambient blob colors (SETTINGS → COLORS). Defaults
     match the original hardcoded hex values each blob used to have baked
     into its gradient/hue-rotate. */
  --blob-center:#60a5fa; --blob-a:#f472b6; --blob-b:#34d399; --blob-cursor:#a78bfa;
  /* Liquid-glass surface tint (SETTINGS → toggle, only meaningful once the
     panes are already transparent -- see the html.liquid-glass-on block
     below for the full explanation). Plain color-mix() against transparent,
     resolved once like any other background-color -- deliberately NOT a
     backdrop-filter recipe, so these are just colors, not anything that
     needs to be computed per-frame. */
  --glass-pct: 55%; /* live intensity, driven by the SETTINGS slider (applyGlassOpacity()) -- kept in the 35-75 range so the whole slider stays at "ideal glass" instead of fading toward opaque-looking at one end */
  --glass-fill: color-mix(in srgb, var(--panel) var(--glass-pct), transparent);
  --glass-fill-strong: color-mix(in srgb, var(--panel) calc(var(--glass-pct) + 10%), transparent);
  /* Sheen/edge brightness scale WITH --glass-pct rather than sitting at a
     fixed alpha -- a fixed-alpha white highlight over a very-low-alpha
     base tint used to read as a flat opaque white haze instead of glass
     (the highlight dominated when the base tint barely showed). Scaling
     both together keeps the highlight proportional to the tint at every
     point on the slider. */
  --glass-sheen: color-mix(in srgb, white calc(var(--glass-pct) * 0.55), transparent);
  --glass-sheen-soft: color-mix(in srgb, white calc(var(--glass-pct) * 0.2), transparent);
  --glass-edge: color-mix(in srgb, white calc(var(--glass-pct) * 0.6), transparent);
  --glass-edge-dim: color-mix(in srgb, white calc(var(--glass-pct) * 0.22), transparent);
  --glass-glow: 0 6px 18px -10px rgba(0,0,0,.45);
  /* Main chat window text size (SETTINGS -> Chat Text Size slider, driven by
     applyChatFontSize()). Only chat-pane rules read these, so the sidebar,
     tool panes, settings and titlebar are unaffected. The smaller tiers are
     fixed ratios of --chat-fs so the whole text hierarchy scales together,
     with a floor so small text never drops below a legible size. */
  --chat-fs:15px;
  --cfs-xs:max(10px, calc(var(--chat-fs) * .8));
  --cfs-sm:max(10.5px, calc(var(--chat-fs) * .875));
  --cfs-md:max(11px, calc(var(--chat-fs) * .93));
  --cfs-lg:max(11.5px, calc(var(--chat-fs) * .95));
  /* Secondary chat text (labels, timestamps, previews): brighter than
     --subtext, derived from the theme's own text/bg so it tracks light/dark. */
  --chat-sub:color-mix(in srgb, var(--text) 80%, var(--bg));
}
/* Frosted-highlight toggle (SETTINGS → checkbox next to Glass Opacity).
   This only kills the diagonal sheen gradient -- the thing that makes each
   element look brighter on one side and fade out toward the other -- NOT
   the edge/outline or the outer glow, which stay on regardless (they read
   as a normal panel border, not a "left-to-right" shine). Same "swap a
   custom property" mechanism as everything else glass-related: one
   variable flip, no new rule needs to fire, nothing to flash. */
html.glass-no-sheen{
  --glass-sheen: transparent;
  --glass-sheen-soft: transparent;
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;height:100%;background:var(--bg);color:var(--text);
  font-family:"Segoe UI",-apple-system,sans-serif;overflow:hidden;user-select:none;}
/* body is a column: custom titlebar on top (fixed height), #root fills the
   rest. This replaces the native OS titlebar removed via frameless=True
   in webview.create_window (see app.py bottom). */
body{display:flex;flex-direction:column;}
button{font-family:inherit;cursor:pointer;}
input,textarea{font-family:inherit;}
input[type="checkbox"]{
  appearance:none;-webkit-appearance:none;position:relative;display:inline-block;
  flex:0 0 auto;width:34px !important;height:20px !important;margin:0;
  border:1px solid var(--border2);border-radius:999px;background:var(--surface2);
  cursor:pointer;vertical-align:middle;transition:background-color .18s var(--ease),border-color .18s var(--ease),box-shadow .18s var(--ease);
}
input[type="checkbox"]::after{
  content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;
  border-radius:50%;background:var(--subtext);box-shadow:0 1px 3px rgba(0,0,0,.3);
  transition:left .18s var(--ease),background-color .18s var(--ease);
}
input[type="checkbox"]:checked{
  background:var(--accent);border-color:var(--accent);
}
input[type="checkbox"]:checked::after{
  left:16px;background:var(--text);
}
input[type="checkbox"]:focus-visible{
  outline:none;box-shadow:0 0 0 2px color-mix(in srgb, var(--accent) 55%, transparent);
}
/* Scrollbars: sized and weighted to read as a deliberate, professional
   surface rather than the bare OS default -- wide enough to grab, a
   visible track so the thumb has somewhere to sit, and a hover/active
   state so the control gives feedback instead of just being decorative.
   scrollbar-gutter keeps layout stable so content doesn't shift by the
   scrollbar's width when a panel's content grows past the fold. */
/* Chat transcript keeps its slightly more visible thumb (it's the pane
   the user scrolls most), styled to match the rest of the app. */
#chat-scroll{scrollbar-width:thin;scrollbar-color:var(--border2) transparent;}
#chat-scroll::-webkit-scrollbar{width:11px;height:11px;}
#chat-scroll::-webkit-scrollbar-track{background:transparent;}
#chat-scroll::-webkit-scrollbar-corner{background:transparent;}
#chat-scroll::-webkit-scrollbar-thumb{
  background-color:var(--border2);border-radius:999px;
  border:3px solid var(--panel);background-clip:padding-box;
  transition:background-color .15s;
}
#chat-scroll::-webkit-scrollbar-thumb:hover{background-color:var(--subtext);}
#chat-scroll::-webkit-scrollbar-thumb:active{background-color:var(--accent);}
/* Everywhere else: an essentially invisible scrollbar -- no track, no
   border/arrows, just a faint thin pill that only reads as "there's more
   content" rather than a visible UI chrome element. Matches the app's
   own palette (--border2) instead of falling back to the OS's native
   (often stark white/grey) scrollbar now that the chat-only scoping
   above stopped styling these. */
*:not(#chat-scroll){scrollbar-width:thin;scrollbar-color:color-mix(in srgb, var(--border2) 55%, transparent) transparent;}
*:not(#chat-scroll)::-webkit-scrollbar{width:8px;height:8px;}
*:not(#chat-scroll)::-webkit-scrollbar-track{background:transparent;}
*:not(#chat-scroll)::-webkit-scrollbar-corner{background:transparent;}
*:not(#chat-scroll)::-webkit-scrollbar-thumb{
  background-color:color-mix(in srgb, var(--border2) 55%, transparent);
  border-radius:999px;border:none;background-clip:padding-box;
  transition:background-color .15s;
}
*:not(#chat-scroll)::-webkit-scrollbar-thumb:hover{background-color:var(--border2);}
*:not(#chat-scroll)::-webkit-scrollbar-thumb:active{background-color:var(--subtext);}
/* scroll-behavior stays at the browser default (instant) everywhere
   except the chat transcript is unaffected either way since it already
   scrolls via direct scrollTop assignment during streaming. */

#root{position:relative;width:100%;height:100%;flex:1 1 auto;min-height:0;background:var(--bg);}

/* ── Custom titlebar (replaces the native OS one -- frameless window) ──
   Left: chat name + active-model pill. Middle: empty drag region. Right:
   minimize/maximize/close. Real Windows Snap (drag-to-edge, Win+arrow,
   hover-over-maximize flyout) is wired up natively in Python via a
   WM_NCHITTEST subclass hook (see _install_win_snap_hook) that treats
   #titlebar-center/#titlebar-left as the caption and the three buttons
   as real HTMINBUTTON/HTMAXBUTTON/HTCLOSE hit zones -- no JS/CSS can
   trigger genuine OS snapping on its own, only the OS can. The
   -webkit-app-region hints below are a courtesy for non-Windows
   (frameless+easy_drag) fallback and cost nothing on Windows. */
:root{ --titlebar-h:36px; }
#app-titlebar{
  flex:0 0 var(--titlebar-h);height:var(--titlebar-h);width:100%;
  display:flex;align-items:stretch;justify-content:space-between;
  position:relative;z-index:1000;
  -webkit-app-region:drag;
  background:var(--bg); /* blends seamlessly with the app when glass is off */
  border-bottom:1px solid transparent;
  transition:background-color .2s var(--ease),border-color .2s var(--ease);
}
#titlebar-left{
  display:flex;align-items:center;gap:8px;min-width:0;padding-left:12px;
  -webkit-app-region:drag;
}
#titlebar-chat-title{
  font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;max-width:38vw;
}
#titlebar-model-pill{
  font-size:10px;font-weight:700;letter-spacing:.2px;color:var(--accent);
  background:var(--accent-faint);border:1px solid var(--border2);
  border-radius:999px;padding:2px 10px;white-space:nowrap;flex:0 0 auto;
  -webkit-app-region:no-drag; /* a pill, not part of the draggable area */
}
#titlebar-center{
  flex:1 1 auto;-webkit-app-region:drag;
  display:flex;align-items:center;gap:6px;min-width:0;overflow:hidden;padding:0 8px;
}
/* ── Conversation tabs (browser-tab-like AI conversations -- see
   conversation_tabs.py) + new-tab button + saved-tab-group dropdown.
   This strip is a SIBLING row directly under #app-titlebar (not inside
   it). Styled to match #tabbar (the big subsystem pill tab bar) --
   same rounded pill shell, same sliding accent #conv-tab-highlight
   underlay -- just shrink-wrapped to its content instead of stretching
   to a fixed width, and centered in the row via #conv-tabbar-row's
   justify-content:center. Because the pill's width is content-driven
   and it's centered rather than left-anchored, opening/closing tabs
   grows or shrinks it symmetrically from the middle instead of pushing
   everything rightward. Only the pill/buttons themselves opt OUT of
   dragging; empty space in the row is still drag-to-move, same as the
   titlebar above it. */
#conv-tabbar-row{
  flex:0 0 auto;width:100%;min-height:34px;
  display:flex;align-items:flex-start;justify-content:center;padding:0 10px;
  position:relative;top:20px;z-index:900;
  -webkit-app-region:drag;
  background:transparent;
}
#conv-tab-controls{
  display:inline-flex;align-items:center;gap:6px;
  -webkit-app-region:no-drag;
}
#conv-tabbar{
  height:34px;border-radius:17px;background:var(--surface);border:1px solid var(--border2);
  display:flex;align-items:center;padding:4px;gap:2px;position:relative;
  -webkit-app-region:no-drag;
}
#conv-tab-highlight{
  position:absolute;top:4px;left:4px;height:26px;width:0;border-radius:13px;background:var(--accent);
  transition:left .28s var(--ease), width .28s var(--ease);z-index:0;pointer-events:none;
}
.conv-tab{
  position:relative;z-index:1;display:flex;align-items:center;gap:6px;
  height:26px;max-width:150px;min-width:40px;padding:0 12px;
  border-radius:13px;cursor:pointer;user-select:none;
  background:transparent;color:var(--subtext);
  transition:background-color .18s var(--ease),color .18s var(--ease);
  white-space:nowrap;overflow:hidden;
}
.conv-tab:not(.active):hover{background:var(--surface2);}
.conv-tab.active{background:transparent;color:var(--text);font-weight:600;}
.conv-tab.agent{opacity:.85;}
.conv-tab-title{
  font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.conv-tab.active .conv-tab-title{color:var(--text);font-weight:600;}
.conv-tab-close{
  flex:0 0 auto;width:16px;height:16px;border:none;border-radius:5px;background:transparent;
  color:var(--subtext);font-size:12px;line-height:1;display:flex;align-items:center;justify-content:center;
}
.conv-tab-close:hover{background:var(--border2);color:var(--text);}
.conv-tab-icon-btn{
  flex:0 0 auto;width:24px;height:24px;border:none;border-radius:12px;background:transparent;
  color:var(--subtext);font-size:14px;font-weight:700;display:flex;align-items:center;justify-content:center;
  -webkit-app-region:no-drag;
}
.conv-tab-icon-btn:hover{background:var(--surface2);color:var(--text);}
#conv-tab-group-wrap{position:relative;-webkit-app-region:no-drag;}
#conv-tab-group-menu{
  display:none;position:absolute;top:38px;left:0;min-width:200px;max-width:260px;
  background:var(--panel);border:1px solid var(--border2);border-radius:12px;
  box-shadow:0 12px 32px rgba(0,0,0,.4);padding:6px;z-index:1001;
  -webkit-app-region:no-drag;
}
#conv-tab-group-menu.open{display:block;}
.conv-tab-group-item{
  display:flex;align-items:center;justify-content:space-between;gap:8px;
  padding:7px 10px;border-radius:8px;font-size:12px;color:var(--text);cursor:pointer;
}
.conv-tab-group-item:hover{background:var(--surface2);}
.conv-tab-group-item.new{color:var(--accent);font-weight:600;}
.conv-tab-group-item .count{font-size:10px;color:var(--subtext);flex:0 0 auto;}
.conv-tab-group-sep{height:1px;background:var(--border2);margin:5px 2px;}
#titlebar-controls{display:flex;align-items:stretch;-webkit-app-region:no-drag;}
.titlebar-btn{
  width:46px;height:100%;border:none;background:transparent;color:var(--subtext);
  display:flex;align-items:center;justify-content:center;position:relative;
}
/* Circular hit-highlight: the button ITSELF stays the full 46px-wide hit
   slot (has to match _CAPTION_BTN_W in the native snap hook), but the
   visible hover/active/glass surface is a smaller centered circle drawn
   via ::before so it reads as a round button rather than a square one. */
.titlebar-btn::before{
  content:"";position:absolute;width:30px;height:30px;border-radius:50%;
  background-color:transparent;transition:background-color .12s var(--ease),box-shadow .12s var(--ease);
}
.titlebar-btn:hover::before{background-color:var(--surface2);}
.titlebar-btn:active::before{background-color:var(--border2);}
#titlebar-close:hover::before{background-color:#e81123 !important;}
.titlebar-btn:hover{color:var(--text);}
#titlebar-close:hover{color:#fff;}
.titlebar-btn svg,.titlebar-btn i{position:relative;z-index:1;width:14px;height:14px;pointer-events:none;}

/* ── Top bar : 100% wide, 15% tall, rounded pill bar ── */
#topbar-wrap{position:absolute;left:0;top:0;width:100%;height:15%;}
#topbar{
  position:absolute;inset:var(--gap);border-radius:var(--radius);
  background:var(--panel);border:1px solid var(--border2);
  display:flex;align-items:center;justify-content:space-between;padding:0 18px;
}
#topbar::before{
  content:"";position:absolute;z-index:0;left:-1px;right:-1px;top:-38px;bottom:-1px;
  border:1px solid var(--border2);border-bottom:none;
  border-radius:var(--radius) var(--radius) 0 0;background:var(--panel);
  pointer-events:none;
}
#topbar > *{position:relative;z-index:1;}
#left-cluster{display:flex;align-items:center;gap:12px;flex:0 0 auto;position:relative;}
#brand{position:absolute;top:-20px;left:2px;font-size:11px;font-weight:700;color:var(--accent);white-space:nowrap;}
.icon-btn{
  width:36px;height:36px;border-radius:50%;border:none;background:var(--surface2);
  color:var(--text);font-size:14px;display:flex;align-items:center;justify-content:center;
  transition:background .15s var(--ease),color .15s var(--ease);
}
.icon-btn:hover{background:var(--border2);}
.icon-btn.active{background:var(--accent);}
#status-row{display:flex;align-items:center;gap:6px;}
#status-row{
  width:150px;height:40px;flex:0 0 150px;box-sizing:border-box;
  padding:0 12px;border-radius:999px;position:relative;left:8px;overflow:hidden;justify-content:center;
  border:1px solid color-mix(in srgb, white 10%, var(--border2));
  background-color:color-mix(in srgb, var(--surface) 80%, transparent);
  background-image:linear-gradient(150deg, color-mix(in srgb, white 9%, transparent) 0%, color-mix(in srgb, white 3%, transparent) 34%, transparent 62%);
  box-shadow:inset 0 1px 0 color-mix(in srgb, white 13%, transparent), inset 0 -1px 0 color-mix(in srgb, white 4%, transparent), 0 6px 16px -12px rgba(0,0,0,.6);
  transition:background-color .18s var(--ease),border-color .18s var(--ease),box-shadow .18s var(--ease);
}
#status-dot{width:8px;height:8px;border-radius:50%;background:var(--yellow);transition:background .2s;}
#status-label{font-size:12px;font-weight:700;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}

/* Centered pill tab bar — 80% width of top bar, fully rounded bar + tabs */
#tabbar-wrap{width:80%;max-width:80%;flex:0 0 80%;display:flex;justify-content:center;}
#tabbar{
  width:100%;height:40px;border-radius:20px;background:var(--surface);
  border:1px solid var(--border2);display:flex;align-items:center;padding:4px;gap:4px;position:relative;
}
#tab-highlight{
  position:absolute;top:4px;left:4px;height:32px;width:0;border-radius:16px;background:var(--accent);
  transition:left .32s var(--ease), width .32s var(--ease);z-index:0;pointer-events:none;
}
.tab-btn{
  flex:1 1 0;height:32px;border:none;border-radius:16px;background:transparent;
  color:var(--subtext);font-size:12px;display:flex;align-items:center;justify-content:center;gap:6px;
  transition:background .18s var(--ease),color .18s var(--ease);white-space:nowrap;overflow:hidden;
  position:relative;z-index:1;
}
.tab-btn:hover{background:var(--surface2);}
.tab-btn.active{background:transparent;color:var(--text);font-weight:600;}
#tabbar .tab-btn:nth-of-type(1) .ic{color:#67e8f9;}
#tabbar .tab-btn:nth-of-type(2) .ic{color:#c4b5fd;}
#tabbar .tab-btn:nth-of-type(3) .ic{color:#bef264;}
#tabbar .tab-btn:nth-of-type(4) .ic{color:#f9a8d4;}
#tabbar .tab-btn:nth-of-type(5) .ic{color:#fcd34d;}
#tabbar .tab-btn:nth-of-type(6) .ic{color:#6ee7b7;}
#tabbar .tab-btn:nth-of-type(7) .ic{color:#fdba74;}
#tabbar .tab-btn:nth-of-type(8) .ic{color:#7dd3fc;}
#tabbar .tab-btn:nth-of-type(9) .ic{color:#f0abfc;}
#tabbar .tab-btn:nth-of-type(10) .ic{color:#86efac;}
#tabbar .tab-btn:nth-of-type(11) .ic{color:#fda4af;}
#tabbar .tab-btn:nth-of-type(12) .ic{color:#93c5fd;}
#tabbar .tab-btn:nth-of-type(13) .ic{color:#a5b4fc;}

#right-cluster{flex:0 0 auto;}
#abort-btn{
  height:32px;padding:0 16px;border-radius:16px;background:transparent;color:var(--red);
  border:1px solid #3f0f0f;font-size:12px;transition:background .15s;
}
#abort-btn:hover{background:#2d1010;}

/* ── Content area : 100% wide, 85% tall, below top bar ── */
#content{position:absolute;left:0;top:15%;width:100%;height:85%;}
.pane-wrap{
  position:absolute;top:0;height:100%;
  transition:left .28s var(--ease), width .28s var(--ease), opacity .2s var(--ease);
}
/* -- Background image layer: a single static, full-viewport image behind
   the panes. Brightness/blur/opacity are baked into the image's pixels
   server-side (Pillow) before it ever reaches the DOM -- this element
   just paints a flat PNG via background-image, with NO CSS filter or
   opacity property on it. That's deliberate: a live filter/opacity here
   forced the browser to recompute it on every repaint (which happens
   continuously thanks to the tool-dot pulse and row/word entrance
   animations elsewhere on the page), which is what caused the constant
   flashing and the opacity intermittently snapping back to full. */
#bg-image-layer{
  position:fixed;inset:0;z-index:0;pointer-events:none;
  background-size:cover;background-position:center;background-repeat:no-repeat;
  display:none;
}
html.has-bg-image #bg-image-layer{ display:block; }

/* ── Ambient gradient blobs ──────────────────────────────────────────────
   Purely decorative, always-on ambience layer sitting behind the panes
   (z-index:0, same stacking context as #bg-image-layer) so it only ever
   shows through the thin var(--gap) margins around the panes -- and, in
   "liquid glass" mode, softly through the panes' translucent background.
   pointer-events:none end-to-end so it can never intercept a click/drag,
   and nothing here is ever laid out (all four blobs are absolutely
   positioned circles animated via transform), so it cannot shift or
   resize any real UI element. Each blob's color is user-configurable
   (SETTINGS → COLORS) via its own --blob-* CSS custom property, plugged
   straight into that blob's radial-gradient -- no filter/hue-rotate
   trick needed, and nothing about changing it forces a repaint of the
   others. */
#blob-layer{
  position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden;
  /* isolation:isolate gives this layer its own stacking/blend context so
     nothing outside it ever has to be recomposited when a blob repaints,
     and contain:strict (paint+layout+size+style) tells the engine this
     subtree's rendering is fully self-contained -- a resize, maximize, or
     any change elsewhere on the page cannot force it to re-layout or
     re-rasterize. transition:opacity is used to hide/show the whole layer
     smoothly during a live window resize (see JS below) instead of letting
     the browser visibly re-blur it mid-drag, which is what caused the
     flash on maximize. */
  isolation:isolate;contain:strict;
  opacity:1;transition:opacity .25s linear;
}
html.blob-settling #blob-layer{opacity:0;}
html.blob-hidden #blob-layer{opacity:0;}
/* filter:blur() is set once, statically, per blob -- never animated. Animating
   `filter` forces the compositor to fully re-rasterize these huge blurred
   layers on every single frame, which is what caused the flashing/strobing
   once the blob layer was added (same class of bug as the #bg-image-layer
   note above). Only `transform` is ever animated here, which is
   compositor-only and doesn't repaint. Each blob's actual color comes from
   its own --blob-* custom property (see :root and SETTINGS → COLORS) baked
   directly into the gradient -- a plain background-color-style value the
   compositor resolves once at paint time, same as any other static color.
     mix-blend-mode is deliberately NOT used: blending several huge blurred
   layers together is expensive to recomposite and, in this embedded webview,
   visibly flickers whenever the compositor has to rebuild layers -- on
   resize/maximize, and on focus/blur when the cursor leaves the window.
   Plain stacked, semi-transparent radial gradients (already fading to
   `transparent` at their edge) give the same soft glow-through look without
   ever needing a blend context.
     Sizes are set from --blob-vw/--blob-vh (plain px, computed once in JS
   from the window size and only updated on debounced resize) rather than
   vmax/vw/vh units. Viewport-relative units force the engine to recompute
   every blob's geometry and re-rasterize its blur continuously while a
   window is actively being resized/maximized; static px doesn't. */
.blob{
  position:absolute;left:0;top:0;border-radius:50%;
  opacity:.55;will-change:transform;
  filter:blur(70px);
  transform:translateZ(0);backface-visibility:hidden;
  contain:paint;
}
#blob-center{
  width:calc(var(--blob-vmax,900px)*0.56);height:calc(var(--blob-vmax,900px)*0.56);left:50%;top:50%;
  background:radial-gradient(circle at 35% 35%, var(--blob-center) 0%, var(--accent2) 45%, transparent 72%);
  opacity:.28;transform:translate(-50%,-50%);
  animation:blobPulse 11s ease-in-out infinite;
}
#blob-a{
  width:calc(var(--blob-vmax,900px)*0.3);height:calc(var(--blob-vmax,900px)*0.3);
  background:radial-gradient(circle at 40% 40%, var(--blob-a) 0%, transparent 72%);
  transform:translate(20vw,25vh) translate(-50%,-50%);
  transition:transform 6s cubic-bezier(.45,0,.55,1);
}
#blob-b{
  width:calc(var(--blob-vmax,900px)*0.3);height:calc(var(--blob-vmax,900px)*0.3);
  background:radial-gradient(circle at 45% 35%, var(--blob-b) 0%, transparent 72%);
  transform:translate(75vw,70vh) translate(-50%,-50%);
  transition:transform 7s cubic-bezier(.45,0,.55,1);
}
#blob-cursor{
  width:calc(var(--blob-vmax,900px)*0.2);height:calc(var(--blob-vmax,900px)*0.2);
  background:radial-gradient(circle at 50% 50%, var(--blob-cursor) 0%, transparent 72%);
  transform:translate(-100px,-100px) translate(-50%,-50%);
}
@keyframes blobPulse{
  0%,100%{transform:translate(-50%,-50%) scale(1);}
  50%{transform:translate(-50%,-50%) scale(1.08);}
}

/* Panes/topbar are translucent (flat color-mix tint, resolved once at
   paint time like any normal background-color -- deliberately NOT
   backdrop-filter, which would have to continuously re-blur the blob
   layer running behind it every frame) so the ambient blob layer reads
   as glowing up through the panel surface itself, not just in the
   var(--gap) margins around it. #blob-layer sits at a lower paint layer
   than .pane (z-index:0 vs 1) so it's always underneath; anything with
   its own opaque background -- #input-box, buttons, message bubbles,
   tab pills -- still paints solid on top of that tint as a normal child,
   so the blobs never wash out real UI content, only the panel behind it.
   When a background image is active the tint goes a bit more transparent
   (via html.has-bg-image below) so the image shows through too.
     The pane surface itself is fully transparent (no fill, no border) so
   the blobs show straight through the whole window, not just the gap
   margins -- only the elements *inside* a pane (input box, buttons,
   message bubbles, tab pills, etc.) keep their own opaque/tinted
   background and remain fully visible, since those are separate child
   elements with their own background-color, not something painted by
   .pane itself. */
.pane{
  position:absolute;inset:calc(var(--gap)/2);border-radius:var(--radius);
  background:transparent;
  border:none;
  display:flex;flex-direction:column;overflow:hidden;
  z-index:1;
}
#topbar{
  /* #topbar's base rule (further up, in the "Top bar" block) still sets a
     1px border for the no-blobs/no-bg-image case below -- background alone
     was being cleared here, which left that border painting as a bare
     rounded-rect outline floating over the transparent blob area. Clear
     both together so there's nothing left to outline it. */
  background:transparent !important;
  border:none !important;
}
#topbar::before{background:transparent;border-color:transparent;}
/* A background image behaves exactly like the ambient blobs layer: panes
   stay fully transparent (their base rule above) so the image shows
   straight through the whole window, not tinted/dimmed by a translucent
   panel surface on top of it. (Previously this gave has-bg-image its own
   62%-opacity tinted-panel treatment, which made the panels visible as
   solid-ish shapes over the image instead of disappearing like they do
   for ambient blobs -- removed so both features hide the panes the same
   way.) */
/* Ambient blobs are an optional setting (SETTINGS → AMBIENT BLOBS). When
   turned off, hide the blob layer entirely and give the panes/topbar their
   normal opaque surface back -- otherwise, with #blob-layer gone, they'd
   just show flat var(--bg) through fully transparent panes. Skipped when a
   background image is active, since a background image wants the exact
   same fully-transparent pane treatment as ambient blobs do. */
html.blobs-off #blob-layer{ display:none; }
/* Individual blob toggles (SETTINGS → AMBIENT BLOBS). Independent of the
   master blobs-off switch above -- each blob can be hidden on its own via
   plain display:none, same cheap approach as blobs-off itself. */
html.blob-center-off #blob-center{ display:none; }
html.blob-a-off #blob-a{ display:none; }
html.blob-b-off #blob-b{ display:none; }
html.blob-cursor-off #blob-cursor{ display:none; }
html.blobs-off:not(.has-bg-image) .pane{
  background:var(--panel);
  border:1px solid var(--border2);
}
html.blobs-off:not(.has-bg-image) #topbar{
  background:var(--panel) !important;
  border:1px solid var(--border2) !important;
}
html.blobs-off:not(.has-bg-image) #topbar::before{
  background:var(--panel);border-color:var(--border2);
}
.pane-hidden{opacity:0;pointer-events:none;}

/* ── Liquid-glass surfaces (SETTINGS → toggle) ──────────────────────────────────────
   Only does anything once the panes themselves are already transparent --
   i.e. ambient blobs and/or a background image are on, per the pane rules
   above -- so every rule here is gated behind html.liquid-glass-on AND
   :is(.has-bg-image, :not(.blobs-off)); the settings checkbox that drives
   the class is also disabled in JS whenever both are off (see
   applyLiquidGlass()), so this never shows a control the user can't
   actually use. With the pane surface gone, every *child* element that
   still paints its own opaque background -- the pill tab strip, the
   prompt box, buttons, message bubbles, tool/settings rows, dropdowns,
   modals -- is what's left reading as a solid, ordinary panel; this block
   reskins exactly those elements.
     Deliberately NOT backdrop-filter: a live blur sitting over the
   wandering/pulsing blob layer (or a background image) would have to
   resample that moving backdrop every frame, which is the exact class of
   continuous-repaint bug #blob-layer and #bg-image-layer above go out of
   their way to avoid. Everything below is a static, one-time paint
   instead -- a translucent color-mix() fill (same technique the pane tint
   itself used to use), a diagonal "sheen" baked into background-image, and
   a bright top edge / soft outer glow via box-shadow -- so toggling the
   class only ever swaps plain background/border/box-shadow values, never
   anything that forces the compositor to keep recomputing a blur. That's
   also why the transition below is short and limited to those same
   properties: enough to not feel like a hard cut, never long/continuous
   enough to look like it's still "working".
     Code/log surfaces (.code-area) are intentionally left out -- glassing
   those hurts monospace legibility for no real benefit. Markdown code blocks
   and inline code are included as readable content accents. The Shell
   terminal is included separately because it is an interactive pane surface.
   Text-entry fields (input/
   textarea/select, minus the checkbox/radio/range/color types which have
   their own native chrome) are covered generically by tag rather than by
   id/class, since there are dozens of ad-hoc ones scattered through the
   modals/tool panels that would otherwise get missed one at a time.
     #sidebar-settings-overlay is deliberately NOT in this list -- see the
   hard override right after this block for why. */
html.liquid-glass-on.has-bg-image #app-titlebar,
html.liquid-glass-on:not(.blobs-off) #app-titlebar{
  background-color:var(--glass-fill) !important;
  background-image:linear-gradient(150deg, var(--glass-sheen) 0%, var(--glass-sheen-soft) 30%, transparent 55%) !important;
  border-bottom:1px solid var(--glass-edge-dim) !important;
}
html.liquid-glass-on.has-bg-image #titlebar-model-pill,
html.liquid-glass-on:not(.blobs-off) #titlebar-model-pill{
  background-color:var(--glass-fill) !important;
  background-image:linear-gradient(150deg, var(--glass-sheen) 0%, var(--glass-sheen-soft) 30%, transparent 55%) !important;
  border-color:var(--glass-edge) !important;
  box-shadow:inset 0 1px 0 var(--glass-edge), inset 0 -1px 0 var(--glass-edge-dim), var(--glass-glow) !important;
}
/* Titlebar buttons: visible glass chips at rest (not just on hover) so
   they read as part of the glass surface, same idea as .icon-btn's
   always-on tint elsewhere -- hover/active just deepen it. */
html.liquid-glass-on.has-bg-image .titlebar-btn::before,
html.liquid-glass-on:not(.blobs-off) .titlebar-btn::before{
  background-color:var(--glass-fill) !important;
  background-image:linear-gradient(150deg, var(--glass-sheen) 0%, var(--glass-sheen-soft) 30%, transparent 55%) !important;
  border:1px solid var(--glass-edge-dim);
  box-shadow:inset 0 1px 0 var(--glass-edge-dim);
}
html.liquid-glass-on.has-bg-image .titlebar-btn:hover::before,
html.liquid-glass-on:not(.blobs-off) .titlebar-btn:hover::before{
  background-color:var(--glass-fill-strong) !important;
  border-color:var(--glass-edge);
}
html.liquid-glass-on.has-bg-image #titlebar-close:hover::before,
html.liquid-glass-on:not(.blobs-off) #titlebar-close:hover::before{
  background-color:#e81123 !important;
  background-image:none !important;
  border-color:#e81123 !important;
}
/* Chat-row action chips (copy/edit/regenerate) -- same small circular-chip
   recipe as the titlebar buttons just above: opaque-ish fill + diagonal
   sheen + a thin top-lit edge, rather than the flat .icon-btn treatment,
   since these sit directly on top of message bubbles/blobs and need a
   touch more definition to still read as a distinct floating control.
   Covers .row-edit-btn/.row-regen-btn too -- both always carry the base
   .row-copy-btn class alongside their own (see attachEditButton/
   attachRegenButton in the JS). Rest-state opacity is still handled by
   the plain (non-glass) .row:hover .row-copy-btn rule above -- glass only
   changes what it looks like once visible, not when it appears. */
html.liquid-glass-on.has-bg-image .row-copy-btn,
html.liquid-glass-on:not(.blobs-off) .row-copy-btn{
  background-color:var(--glass-fill-strong) !important;
  background-image:linear-gradient(150deg, var(--glass-sheen) 0%, var(--glass-sheen-soft) 30%, transparent 55%) !important;
  border:1px solid var(--glass-edge-dim);
  box-shadow:inset 0 1px 0 var(--glass-edge-dim);
  transition:background-color .15s var(--ease), box-shadow .15s var(--ease), border-color .15s var(--ease), color .15s var(--ease);
}
html.liquid-glass-on.has-bg-image .row-copy-btn:hover,
html.liquid-glass-on:not(.blobs-off) .row-copy-btn:hover{
  background-color:var(--glass-fill-strong) !important;
  border-color:var(--glass-edge);
  box-shadow:inset 0 1px 0 var(--glass-edge);
}
html.liquid-glass-on.has-bg-image .row-copy-btn.copied,
html.liquid-glass-on:not(.blobs-off) .row-copy-btn.copied{
  border-color:var(--green);
  box-shadow:inset 0 1px 0 var(--glass-edge);
}
html.liquid-glass-on.has-bg-image .conv-tab:not(.active):hover,
html.liquid-glass-on:not(.blobs-off) .conv-tab:not(.active):hover,
html.liquid-glass-on.has-bg-image .conv-tab-icon-btn:hover,
html.liquid-glass-on:not(.blobs-off) .conv-tab-icon-btn:hover{
  background-color:var(--glass-fill-strong) !important;
}
html.liquid-glass-on.has-bg-image #tabbar,
html.liquid-glass-on:not(.blobs-off) #tabbar,
html.liquid-glass-on.has-bg-image #status-row,
html.liquid-glass-on:not(.blobs-off) #status-row,
html.liquid-glass-on.has-bg-image #conv-tabbar,
html.liquid-glass-on:not(.blobs-off) #conv-tabbar,
html.liquid-glass-on.has-bg-image #conv-tab-group-menu,
html.liquid-glass-on:not(.blobs-off) #conv-tab-group-menu,
html.liquid-glass-on.has-bg-image .icon-btn,
html.liquid-glass-on:not(.blobs-off) .icon-btn,
html.liquid-glass-on.has-bg-image #input-box,
html.liquid-glass-on:not(.blobs-off) #input-box,
html.liquid-glass-on.has-bg-image #abort-btn,
html.liquid-glass-on:not(.blobs-off) #abort-btn,
html.liquid-glass-on.has-bg-image select,
html.liquid-glass-on:not(.blobs-off) select,
html.liquid-glass-on.has-bg-image textarea:not(#msg-input):not(.code-area),
html.liquid-glass-on:not(.blobs-off) textarea:not(#msg-input):not(.code-area),
html.liquid-glass-on.has-bg-image input:not([type="checkbox"]):not([type="radio"]):not([type="range"]):not([type="color"]):not(#shell-input),
html.liquid-glass-on:not(.blobs-off) input:not([type="checkbox"]):not([type="radio"]):not([type="range"]):not([type="color"]):not(#shell-input),
html.liquid-glass-on.has-bg-image .modal-input,
html.liquid-glass-on:not(.blobs-off) .modal-input,
html.liquid-glass-on.has-bg-image .btn,
html.liquid-glass-on:not(.blobs-off) .btn,
html.liquid-glass-on.has-bg-image .ghost-btn,
html.liquid-glass-on:not(.blobs-off) .ghost-btn,
html.liquid-glass-on.has-bg-image #settings-back-btn,
html.liquid-glass-on:not(.blobs-off) #settings-back-btn,
html.liquid-glass-on.has-bg-image #file-list,
html.liquid-glass-on:not(.blobs-off) #file-list,
html.liquid-glass-on.has-bg-image #history-list,
html.liquid-glass-on:not(.blobs-off) #history-list,
html.liquid-glass-on.has-bg-image .history-card,
html.liquid-glass-on:not(.blobs-off) .history-card,
html.liquid-glass-on.has-bg-image .bubble.user,
html.liquid-glass-on:not(.blobs-off) .bubble.user,
html.liquid-glass-on.has-bg-image .ask-card,
html.liquid-glass-on:not(.blobs-off) .ask-card,
html.liquid-glass-on.has-bg-image .ask-opt-btn,
html.liquid-glass-on:not(.blobs-off) .ask-opt-btn,
html.liquid-glass-on.has-bg-image .mcp-tool-row,
html.liquid-glass-on:not(.blobs-off) .mcp-tool-row,
html.liquid-glass-on.has-bg-image .mcp-row,
html.liquid-glass-on:not(.blobs-off) .mcp-row,
html.liquid-glass-on.has-bg-image .tools-args,
html.liquid-glass-on:not(.blobs-off) .tools-args,
html.liquid-glass-on.has-bg-image .perm-row,
html.liquid-glass-on:not(.blobs-off) .perm-row,
html.liquid-glass-on.has-bg-image .perm-opt,
html.liquid-glass-on:not(.blobs-off) .perm-opt,
html.liquid-glass-on.has-bg-image .ptt-slot-row,
html.liquid-glass-on:not(.blobs-off) .ptt-slot-row,
html.liquid-glass-on.has-bg-image textarea.code-area,
html.liquid-glass-on:not(.blobs-off) textarea.code-area,
html.liquid-glass-on.has-bg-image pre.code-block,
html.liquid-glass-on:not(.blobs-off) pre.code-block,
html.liquid-glass-on.has-bg-image code.inline-code,
html.liquid-glass-on:not(.blobs-off) code.inline-code,
html.liquid-glass-on.has-bg-image #shell-terminal,
html.liquid-glass-on:not(.blobs-off) #shell-terminal,
html.liquid-glass-on.has-bg-image input[type="checkbox"],
html.liquid-glass-on:not(.blobs-off) input[type="checkbox"],
html.liquid-glass-on.has-bg-image #pdf-levels,
html.liquid-glass-on:not(.blobs-off) #pdf-levels,
html.liquid-glass-on.has-bg-image #pdf-tree,
html.liquid-glass-on:not(.blobs-off) #pdf-tree,
html.liquid-glass-on.has-bg-image #flow-drawer,
html.liquid-glass-on:not(.blobs-off) #flow-drawer,
html.liquid-glass-on.has-bg-image .flow-drawer-item,
html.liquid-glass-on:not(.blobs-off) .flow-drawer-item,
html.liquid-glass-on.has-bg-image .dropdown-trigger,
html.liquid-glass-on:not(.blobs-off) .dropdown-trigger,
html.liquid-glass-on.has-bg-image .dropdown-option,
html.liquid-glass-on:not(.blobs-off) .dropdown-option,
html.liquid-glass-on.has-bg-image .modal-box,
html.liquid-glass-on:not(.blobs-off) .modal-box,
html.liquid-glass-on.has-bg-image #path-picker,
html.liquid-glass-on:not(.blobs-off) #path-picker,
html.liquid-glass-on.has-bg-image .path-picker-row,
html.liquid-glass-on:not(.blobs-off) .path-picker-row,
html.liquid-glass-on.has-bg-image #capability-picker,
html.liquid-glass-on:not(.blobs-off) #capability-picker,
html.liquid-glass-on.has-bg-image .capability-picker-row,
html.liquid-glass-on:not(.blobs-off) .capability-picker-row,
html.liquid-glass-on.has-bg-image .flowchart-wrap,
html.liquid-glass-on:not(.blobs-off) .flowchart-wrap,
html.liquid-glass-on.has-bg-image .perm-fs-pw-box,
html.liquid-glass-on:not(.blobs-off) .perm-fs-pw-box,
html.liquid-glass-on.has-bg-image table.md-table{
  background-color:var(--glass-fill) !important;
  background-image:linear-gradient(150deg, var(--glass-sheen) 0%, var(--glass-sheen-soft) 30%, transparent 55%) !important;
  border-color:var(--glass-edge) !important;
  box-shadow:inset 0 1px 0 var(--glass-edge), inset 0 -1px 0 var(--glass-edge-dim), var(--glass-glow) !important;
  transition:background-color .18s var(--ease), box-shadow .18s var(--ease), border-color .18s var(--ease);
}
html.liquid-glass-on.has-bg-image input[type="checkbox"]:checked,
html.liquid-glass-on:not(.blobs-off) input[type="checkbox"]:checked{
  background-color:color-mix(in srgb, var(--accent) 72%, var(--glass-fill)) !important;
  border-color:var(--accent) !important;
}
/* Small/round controls read better a touch more opaque than big flat
   surfaces (icon glyphs and short labels need more contrast to stay
   legible over the moving blobs/image behind them). */
html.liquid-glass-on.has-bg-image .icon-btn,
html.liquid-glass-on:not(.blobs-off) .icon-btn,
html.liquid-glass-on.has-bg-image #abort-btn,
html.liquid-glass-on:not(.blobs-off) #abort-btn,
html.liquid-glass-on.has-bg-image #settings-back-btn,
html.liquid-glass-on:not(.blobs-off) #settings-back-btn,
html.liquid-glass-on.has-bg-image .btn,
html.liquid-glass-on:not(.blobs-off) .btn{
  background-color:var(--glass-fill-strong) !important;
}
/* Hover/active/selected row states need to win over the flat glass fill
   above (same rule, same specificity -- this just comes later in the
   cascade). Without this, every row in these lists reads identically
   once Liquid Glass is on and the keyboard/mouse highlight disappears --
   .dropdown-option.selected had this same latent bug already; folding
   the "#" path-picker's rows into the same fix here. */
html.liquid-glass-on.has-bg-image .dropdown-option:hover,
html.liquid-glass-on:not(.blobs-off) .dropdown-option:hover,
html.liquid-glass-on.has-bg-image .dropdown-option.selected,
html.liquid-glass-on:not(.blobs-off) .dropdown-option.selected,
html.liquid-glass-on.has-bg-image .path-picker-row:hover,
html.liquid-glass-on:not(.blobs-off) .path-picker-row:hover,
html.liquid-glass-on.has-bg-image .path-picker-row.active,
html.liquid-glass-on:not(.blobs-off) .path-picker-row.active,
html.liquid-glass-on.has-bg-image .capability-picker-row:hover,
html.liquid-glass-on:not(.blobs-off) .capability-picker-row:hover,
html.liquid-glass-on.has-bg-image .capability-picker-row.active,
html.liquid-glass-on:not(.blobs-off) .capability-picker-row.active{
  background-color:var(--glass-fill-strong) !important;
  box-shadow:inset 0 0 0 1px var(--glass-edge) !important;
}
/* #sidebar-settings-overlay is a full-panel replacement -- it covers the
   ENTIRE sidebar (see its own rule: position:absolute;inset:0) while
   open, sitting directly on top of #sidebar-main-view underneath (the
   workspace list + chat history it's replacing). Making a panel THAT
   size translucent doesn't read as "glass", it reads as the history list
   bleeding through and overlapping the settings controls' own text --
   unreadable, not stylish. Every other glass surface here is a small
   child control sitting on top of the ambient blobs/background image
   (which is what should show through); this one would sit on top of
   more opaque UI instead, so it stays a normal solid panel regardless of
   the toggle. */
html.liquid-glass-on #sidebar-settings-overlay{
  background-color:var(--panel) !important;
  background-image:none !important;
  box-shadow:none !important;
}

/* Tool pane */
#tool-pane-wrap{left:0;width:0;}
#tool-content{flex:1;padding:14px;overflow-y:auto;}
/* Editor tab: persistent iframe host (see MZED in the script below). Parked inside the tool pane,
   shown only while the Editor tab is active, never rebuilt (so open files survive tab switches). */
#mz-ed-host{position:absolute;inset:0;display:none;background:var(--bg);}
#mz-ed-host iframe{position:absolute;inset:0;width:100%;height:100%;border:0;background:var(--bg);}
.mz-ed-err{padding:30px;color:var(--subtext);text-align:center;font-size:13px;line-height:1.8;}
.tab-btn.has-dirty::after{content:"";position:absolute;top:6px;right:9px;width:6px;height:6px;border-radius:50%;background:var(--accent);}

/* Chat pane (always present) */
#chat-pane-wrap{left:0;width:100%;}
#chat-scroll{flex:1;overflow-y:auto;padding:8px 8px 0 8px;}
#chat-col{max-width:760px;margin:0 auto;display:flex;flex-direction:column;gap:2px;}
#input-row{padding:8px 8px 12px 8px;display:flex;justify-content:center;}
#input-box{
  width:100%;max-width:760px;background:var(--surface);border:1px solid var(--border2);
  border-radius:26px;display:flex;align-items:flex-end;padding:6px 6px 6px 16px;gap:8px;
}
#msg-input{
  flex:1;background:transparent;border:none;outline:none;color:var(--text);font-size:14px;
  line-height:20px;padding:7px 0;box-sizing:border-box;resize:none;font-family:inherit;
  min-height:34px;max-height:216px;overflow-y:hidden;overflow-x:hidden;
  white-space:pre-wrap;word-wrap:break-word;overflow-wrap:break-word;
}
#msg-input::placeholder{color:var(--muted);}
#send-btn{
  width:36px;height:36px;border-radius:50%;border:none;background:var(--accent);color:var(--text);
  font-size:16px;font-weight:700;display:flex;align-items:center;justify-content:center;
  transition:background .15s;
}
#send-btn:hover{background:var(--accent-dim);}
#send-hint{text-align:center;font-size:9px;color:var(--muted);padding-bottom:6px;}

/* ── Prompt-box file attachments (artifact system) ───────────────────── */
#input-box{flex-wrap:wrap;}
#attach-btn{flex:0 0 auto;}
#attach-btn svg{width:17px;height:17px;}
#attach-btn.has-files{background:var(--accent);color:#fff;}
#attach-tray{flex:0 0 100%;order:-1;display:none;gap:8px;overflow-x:auto;padding:2px 2px 6px 0;margin-left:-4px;scrollbar-width:thin;}
#attach-tray.show{display:flex;}
.att-chip,.att-chip-ro{position:relative;display:flex;align-items:center;gap:8px;flex:0 0 auto;min-width:120px;max-width:240px;padding:6px 8px 6px 6px;border-radius:12px;box-sizing:border-box;}
.att-chip{padding-right:30px;background:var(--surface2);border:1px solid var(--border2);color:var(--text);animation:attIn .18s var(--ease,ease-out);}
.att-chip.err{border-color:var(--red);}
.att-chip-ro{background:color-mix(in srgb,currentColor 12%,transparent);color:inherit;}
@keyframes attIn{from{opacity:0;transform:translateY(4px) scale(.97);}to{opacity:1;transform:none;}}
.att-thumb,.att-ico{width:34px;height:34px;border-radius:8px;flex:0 0 auto;object-fit:cover;}
.att-ico{display:flex;align-items:center;justify-content:center;background:color-mix(in srgb,var(--accent) 22%,transparent);color:var(--accent);}
.att-chip-ro .att-ico{color:inherit;background:color-mix(in srgb,currentColor 14%,transparent);}
.att-chip.err .att-ico{background:color-mix(in srgb,var(--red) 22%,transparent);color:var(--red);}
.att-ico svg{width:17px;height:17px;}
.att-meta{min-width:0;flex:1;display:flex;flex-direction:column;gap:1px;}
.att-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12px;font-weight:600;line-height:1.25;}
.att-sub{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:10px;opacity:.7;line-height:1.25;}
.att-chip.err .att-sub{color:var(--red);opacity:1;}
.att-x{position:absolute;top:50%;right:5px;transform:translateY(-50%);width:22px;height:22px;border:none;border-radius:50%;background:transparent;color:var(--subtext);display:flex;align-items:center;justify-content:center;padding:0;}
.att-x:hover{background:var(--border2);color:var(--text);}
.att-x svg{width:13px;height:13px;}
.att-prog{height:3px;border-radius:2px;background:var(--border);overflow:hidden;margin-top:3px;}
.att-prog>i{display:block;height:100%;width:0;background:var(--accent);transition:width .15s;}
.att-prog.indet>i{width:40%;animation:attSlide 1.1s ease-in-out infinite;}
@keyframes attSlide{0%{transform:translateX(-100%);}100%{transform:translateX(260%);}}
/* margin-left:-6px cancels the read-only chip's 6px left padding so the file icon/thumbnail lines up exactly with the start of the message text */
.bubble.user .att-row{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px;margin-left:-6px;}
.bubble.user .att-row:last-child{margin-bottom:0;}
#drop-overlay{position:absolute;inset:8px;z-index:60;display:none;align-items:center;justify-content:center;flex-direction:column;gap:6px;border:2px dashed var(--accent);border-radius:22px;background:color-mix(in srgb,var(--surface) 80%,transparent);backdrop-filter:blur(3px);color:var(--text);font-size:15px;font-weight:600;pointer-events:none;}
#drop-overlay small{font-size:11px;font-weight:400;opacity:.7;}
#drop-overlay svg{width:30px;height:30px;color:var(--accent);}
#drop-overlay.show{display:flex;}

/* KB Only dropdown (prompt-box knowledge-base restriction toggle) */
#kb-toggle-btn{flex:0 0 auto;font-size:11px;}
#kb-toggle-btn.active{background:var(--accent);color:#fff;}
#kb-popover{
  position:absolute;bottom:calc(100% + 8px);left:0;width:300px;max-height:280px;overflow-y:auto;
  background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:10px;
  box-shadow:0 8px 24px rgba(0,0,0,.35);display:none;z-index:20;
}
#kb-popover.open{display:block;}
.kb-only-row{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:600;color:var(--text);
  padding-bottom:8px;border-bottom:1px solid var(--border);margin-bottom:8px;}
.kb-only-row label{cursor:pointer;}
.kb-only-hint{font-size:10px;color:var(--subtext);margin-bottom:8px;}
#kb-explain-mode-row{display:none;gap:6px;margin-bottom:8px;}
#kb-explain-mode-row.show{display:flex;}
.kb-mode-btn{flex:1;font-size:10px;font-weight:600;padding:6px 4px;border-radius:10px;border:1px solid var(--border);
  background:transparent;color:var(--subtext);cursor:pointer;}
.kb-mode-btn.active{background:var(--accent);color:#fff;border-color:var(--accent);}
.kb-src-row{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--subtext);padding:4px 0;}
.kb-src-row label{cursor:pointer;color:var(--text);}
#kb-only-badge{
  display:none;align-items:center;gap:6px;font-size:10px;font-weight:700;color:var(--accent);
  background:var(--accent-faint);border:1px solid var(--accent);border-radius:999px;padding:4px 12px;
  margin:0 auto 8px auto;width:fit-content;
}
#kb-only-badge.show{display:flex;}
#input-box.kb-only-active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent);}

/* "#" file-path picker (prompt-box live drive/folder browser) */
#path-picker{
  position:absolute;bottom:calc(100% + 8px);left:0;width:100%;max-height:280px;overflow-y:auto;
  background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:6px;
  box-shadow:0 8px 24px rgba(0,0,0,.35);display:none;z-index:25;
}
#path-picker.open{display:block;}
#path-picker-crumb{
  font-size:10px;color:var(--subtext);padding:4px 8px 6px;border-bottom:1px solid var(--border);
  margin-bottom:4px;white-space:nowrap;overflow-x:auto;font-family:Consolas,'Cascadia Code',monospace;
}
.path-picker-row{
  display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:8px;font-size:12px;
  color:var(--text);cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.path-picker-row:hover, .path-picker-row.active{background:var(--surface2);}
.path-picker-icon{flex:0 0 auto;font-size:13px;}
.path-picker-name{flex:1;overflow:hidden;text-overflow:ellipsis;}
.path-picker-empty{font-size:11px;color:var(--subtext);text-align:center;padding:14px 10px;}

/* "/" capability picker (prompt-box live Native Tools / MCP Tools / Skills / Flows / KBs browser) */
#capability-picker{
  position:absolute;bottom:calc(100% + 8px);left:0;width:100%;max-height:320px;overflow-y:auto;
  background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:6px;
  box-shadow:0 8px 24px rgba(0,0,0,.35);display:none;z-index:25;
}
#capability-picker.open{display:block;}
#capability-picker-crumb{
  font-size:10px;color:var(--subtext);padding:4px 8px 6px;border-bottom:1px solid var(--border);
  margin-bottom:4px;white-space:nowrap;overflow-x:auto;
}
.capability-picker-row{
  display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:8px;font-size:12px;
  color:var(--text);cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.capability-picker-row:hover, .capability-picker-row.active{background:var(--surface2);}
.capability-picker-icon{flex:0 0 auto;font-size:13px;}
.capability-picker-main{flex:1;overflow:hidden;display:flex;flex-direction:column;min-width:0;}
.capability-picker-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.capability-picker-desc{font-size:10px;color:var(--subtext);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.capability-picker-count{flex:0 0 auto;font-size:10px;color:var(--subtext);background:var(--surface2);border-radius:999px;padding:1px 7px;}
.capability-picker-empty{font-size:11px;color:var(--subtext);text-align:center;padding:14px 10px;}

/* Sidebar pane */
#sidebar-pane-wrap{left:100%;width:0;}
#sidebar-inner{flex:1;position:relative;overflow:hidden;display:flex;flex-direction:column;}
#sidebar-main-view{flex:1;padding:14px;overflow-y:auto;display:flex;flex-direction:column;gap:8px;}
/* Settings overlay -- covers the ENTIRE sidebar pane (not just a strip)
   while open, so it gets full room for theme/background/provider controls
   instead of being squeezed under the workspace + history sections. */
#sidebar-settings-overlay{
  position:absolute;inset:0;background:var(--panel);z-index:5;
  padding:14px;overflow-y:auto;display:none;flex-direction:column;gap:6px;
}
#sidebar-settings-overlay.open{display:flex;}
#settings-back-btn{
  width:26px;height:26px;border-radius:50%;border:none;background:var(--surface2);
  color:var(--text);font-size:12px;display:flex;align-items:center;justify-content:center;
}
#settings-back-btn:hover{background:var(--border2);}
.section-label{font-size:9px;font-weight:700;color:var(--subtext);letter-spacing:.5px;}
.hdr-row{display:flex;align-items:center;justify-content:space-between;}
select, .btn, .ghost-btn{
  border-radius:16px;border:1px solid var(--border2);background:var(--surface);color:var(--text);
  font-size:12px;height:32px;padding:0 10px;
}
.btn{background:var(--surface2);border:none;transition:background .15s;}
.btn:hover{background:var(--border2);}
.ghost-btn{background:transparent;transition:background .15s;}
.ghost-btn:hover{background:var(--surface2);}
.ghost-btn:disabled{opacity:.4;cursor:default;}
.ghost-btn:disabled:hover{background:transparent;}
.btn-row{display:flex;gap:6px;}
.btn-row .ghost-btn{flex:1;font-size:10px;height:26px;}
#settings-panel-toggle .ghost-btn.active{background:var(--accent);color:#fff;}
#file-list{
  background:var(--surface);border:1px solid var(--border);border-radius:14px;
  font-size:10px;color:var(--subtext);padding:8px;height:90px;overflow-y:auto;white-space:pre;
}
.divider{height:1px;background:var(--border);margin:4px 0;}
#history-list{flex:1;overflow-y:auto;background:var(--surface);border:1px solid var(--border);
  border-radius:16px;padding:6px;display:flex;flex-direction:column;gap:6px;min-height:80px;}
.history-card{
  border-radius:14px;background:var(--panel);border:1px solid var(--border2);padding:8px 10px;
  display:flex;align-items:center;justify-content:space-between;gap:6px;
}
.history-card.current{background:var(--accent-faint);border-color:var(--accent);}
.history-title{font-size:12px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.history-ts{font-size:9px;color:var(--subtext);}
.history-actions{display:flex;gap:4px;flex:0 0 auto;}
.mini-btn{height:24px;padding:0 8px;font-size:10px;border-radius:12px;border:none;}
.mini-btn.open{background:var(--accent);color:var(--text);}
.mini-btn.del{background:transparent;color:var(--red);border:1px solid #3f0f0f;}
.mini-btn:disabled{opacity:.35;cursor:default;pointer-events:none;}
#sidebar-footer{display:flex;gap:6px;}
#sidebar-footer .ghost-btn{flex:1;font-size:10px;}

/* Render Surface -- a model-controlled HTML/CSS/JS panel that occupies
   the right half of the window. It's a real .pane-wrap/.pane, exactly
   like the tool/chat/sidebar panes (see targetGeo/applyLayout), so it
   sits flush in the normal split-pane grid -- same inset gap, same
   border-radius, same translucent/opaque surface treatment -- instead of
   floating on top as a separate overlay window. Only the sandboxed
   iframe's srcdoc ever holds the model's markup -- nothing here is ever
   written to disk (see write_render_code / edit_render_code /
   close_render_surface in tools/render_surface_tools.py and
   Api._handle_render in this file). */
#render-surface-pane-wrap .pane{padding:0;}
#render-surface-header{
  flex:0 0 auto;display:flex;align-items:center;justify-content:space-between;
  padding:10px 14px;border-bottom:1px solid var(--border2);background:var(--surface);
}
#render-surface-title{font-size:12px;font-weight:700;color:var(--text);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
#render-surface-frame{flex:1;width:100%;height:100%;border:none;background:#fff;}

/* While the Render Surface is open, every OTHER openable panel is disabled
   -- the top tab bar (Chat/Model/Knowledge/etc) and the Log/Parameters
   buttons tucked inside the sidebar's Settings overlay -- so the model's
   surface can't be knocked off-screen or fought over with the user's own
   navigation. The sidebar toggle itself (☰) is deliberately left OUT of
   this rule so the sidebar stays reachable. */
body.render-surface-active #tabbar .tab-btn,
body.midumdocs-active #tabbar .tab-btn,
body.midumdocs-active #settings-panel-toggle .ghost-btn,
body.render-surface-active #settings-panel-toggle .ghost-btn{
  opacity:.35;pointer-events:none;
}

/* MidumDocs -- LaTeX documents. A real .pane-wrap like the Render Surface, but wider (62%), with one slim header;
   everything else (source editor, compiled pages, AI prompt box) lives in the iframe (gui/midumdocs_assets/editor.html). */
#midumdocs-pane-wrap .pane{padding:0;overflow:hidden;}
#midumdocs-header{flex:0 0 auto;display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--border2);background:var(--surface);}
#midumdocs-title{font-size:12px;font-weight:700;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:30%;}
.md-status{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--subtext);white-space:nowrap;margin-right:auto;}
.md-status i{width:7px;height:7px;border-radius:50%;background:var(--muted);display:inline-block;}
.md-status.ok i{background:var(--green);}
.md-status.compiling i{background:var(--yellow);animation:mdpulse 1s ease-in-out infinite;}
.md-status.error i{background:var(--red);}
@keyframes mdpulse{50%{opacity:.35;}}
.md-seg{display:flex;padding:2px;border-radius:14px;background:var(--surface2);border:1px solid var(--border2);}
.md-seg button{border:0;background:transparent;color:var(--subtext);font-size:11px;padding:3px 11px;border-radius:12px;cursor:pointer;}
.md-seg button.active{background:var(--accent);color:#04121f;}
#midumdocs-frame{flex:1;width:100%;height:100%;border:none;background:transparent;}
body.midumdocs-active #sidebar-toggle{opacity:1;}

/* Chat bubbles */
.row{display:flex;flex-direction:column;padding:6px 0;position:relative;}
.row.user{align-items:flex-end;}
.row.midum{align-items:flex-start;}
.row-label-row{display:flex;align-items:center;gap:6px;margin-bottom:4px;}
.row.user .row-label-row{flex-direction:row-reverse;}
.row-label{font-size:11px;font-weight:700;color:var(--subtext);margin-bottom:0;}
.row.midum .row-label{color:var(--accent);}
.row-copy-btn{
  opacity:0;transition:opacity .15s,background .15s,color .15s;
  width:20px;height:20px;border-radius:50%;border:none;background:var(--surface2);
  color:var(--subtext);font-size:10px;display:flex;align-items:center;justify-content:center;
  cursor:pointer;flex:0 0 auto;padding:0;
}
.row:hover .row-copy-btn{opacity:1;}
.row-copy-btn:hover{background:var(--border2);color:var(--text);}
.row-copy-btn.copied{opacity:1;color:var(--green);}
.row-edit-btn:hover{background:var(--border2);color:var(--accent);}
.row-regen-btn:hover{background:var(--border2);color:var(--accent);}
.row-edit-btn:disabled,.row-regen-btn:disabled{opacity:.5;cursor:default;}
.bubble{border-radius:18px;padding:10px 16px;font-size:14px;line-height:1.5;max-width:78%;white-space:pre-wrap;word-wrap:break-word;
  user-select:text;-webkit-user-select:text;cursor:text;}
.bubble *{user-select:text;-webkit-user-select:text;}
.bubble.user{background:var(--user-msg);}
/* "/capability" tokens inside a sent user message (see renderUserContent) --
   distinguished from the surrounding text so an explicit capability request
   reads at a glance, mirroring the "/" picker's own accent color. */
.capability-tag{
  color:var(--accent);background:var(--accent-faint);border-radius:6px;
  padding:1px 5px;font-weight:600;font-family:"JetBrains Mono","Cascadia Code",Consolas,monospace;
}
.bubble.midum{background:transparent;max-width:100%;}
.midum-chunk{margin-top:10px;}
.midum-chunk:first-child{margin-top:0;}
.row.system, .row.error{align-items:center;text-align:center;}
.row.system .bubble{background:transparent;color:var(--subtext);font-size:12px;}
.row.error .bubble{background:transparent;color:var(--red);font-size:12px;}
/* Interjection pill -- a live steering message sent through the prompt box
   while Midum is still "Executing turns..." on a previous message (see
   sendMessage()/Api.send_interjection in the JS/Python below). Deliberately
   NOT a normal chat bubble: it spans the full chat column width (same max
   width as #chat-col, not the 78% cap ordinary bubbles use) so it reads as
   a steering bar laid across the conversation rather than another turn.
   Rendered in the same Glass Surface language as the titlebar/tool-call
   pills (--glass-fill/--glass-sheen/--glass-edge) when Liquid Glass is on;
   falls back to a flat translucent pill otherwise via the plain rule below. */
.row.interjection{align-items:stretch;}
.row.interjection .bubble{
  max-width:100%;width:100%;border-radius:999px;box-sizing:border-box;
  padding:9px 20px;font-size:12.5px;font-weight:600;letter-spacing:.1px;
  display:flex;align-items:center;gap:8px;
  background:var(--surface2);border:1px solid var(--border2);color:var(--text);
}
.row.interjection .bubble::before{
  content:"\21B4";flex:0 0 auto;font-size:13px;color:var(--accent);
}
html.liquid-glass-on.has-bg-image .row.interjection .bubble,
html.liquid-glass-on:not(.blobs-off) .row.interjection .bubble{
  background-color:var(--glass-fill) !important;
  background-image:linear-gradient(150deg, var(--glass-sheen) 0%, var(--glass-sheen-soft) 30%, transparent 55%) !important;
  border:1px solid var(--glass-edge-dim);
  box-shadow:inset 0 1px 0 var(--glass-edge-dim), var(--glass-glow);
}
.row.tool{align-items:flex-start;}
.tool-line{display:flex;gap:6px;font-size:10px;color:var(--subtext);align-items:center;cursor:pointer;user-select:none;}
.tool-line:hover{color:var(--text);}
.tool-line .gear{color:var(--muted);}
.tool-line .chevron{display:inline-block;transition:transform .15s;font-size:9px;opacity:.6;}
.tool-line.expandable .chevron{opacity:1;}
.tool-row.open .chevron{transform:rotate(90deg);}
.tool-detail{
  display:none;margin:6px 0 2px 15px;padding:8px 10px;border-radius:8px;
  background:var(--tool-bg,var(--surface));border:1px solid var(--border2);
  max-width:520px;
}
.tool-row.open .tool-detail{display:block;}
.tool-detail .tool-detail-label{font-size:9px;font-weight:600;color:var(--subtext);text-transform:uppercase;letter-spacing:.03em;margin:6px 0 2px;}
.tool-detail .tool-detail-label:first-child{margin-top:0;}
.tool-detail pre{
  margin:0;font-size:11px;font-family:Consolas,'Cascadia Code',monospace;color:var(--tool-text,var(--text));
  white-space:pre-wrap;word-break:break-word;max-height:220px;overflow:auto;
}
.tool-dot{
  width:7px;height:7px;border-radius:50%;background:var(--muted);flex:0 0 auto;
  transition:background .2s;
}
.tool-dot.active{
  background:var(--green);
  animation:toolPulse 1.1s ease-in-out infinite;box-shadow:0 0 0 rgba(16,185,129,.6);
}
/* Sub-agent tool calls: same shape as a normal tool-call row, but dimmed
   down so they read as quiet background chatter from a spawned agent,
   never mistaken for something the Supervisor itself is doing. */
.agent-tool-line{opacity:.5;font-style:italic;}
.agent-tool-line:hover{opacity:.85;font-style:normal;}
.agent-tool-dot{background:var(--muted);}
.agent-tool-row .tool-detail{opacity:.9;}

/* ── Icons ─────────────────────────────────────────────────────────────────
   One consistent stroke-icon set (see icon() in the script). Sized in em so
   an icon scales with whatever text/button it sits in, and coloured with
   currentColor so it follows hover / active / theme changes for free. */
.ic{width:1.15em;height:1.15em;flex:none;vertical-align:-.22em;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;pointer-events:none;}
.ic-fill{fill:currentColor;}
.ic-play,.ic-stop,.ic-star-fill,.ic-dot{stroke-width:1.6;}
/* semantic tints only where an icon stands alone as a status marker */
.log-view .ic-alert,.row.system .ic-alert{color:var(--yellow);}
.log-view .ic-check-circle,.log-view .ic-check,.row.system .ic-check-circle{color:var(--green);}
.log-view .ic-x-circle,.row.system .ic-x-circle{color:var(--red);}
/* Log pane (a div instead of a textarea so it can hold icons) */
div.code-area.log-view{
  width:100%;flex:1;min-height:0;box-sizing:border-box;overflow:auto;
  background:var(--tool-bg);color:var(--tool-text);border:1px solid var(--border);
  border-radius:16px;padding:10px;font-family:Consolas,"Cascadia Code",monospace;font-size:12px;
  white-space:pre-wrap;word-break:break-word;user-select:text;cursor:text;
}

/* ── Grouped tool-call pills ─────────────────────────────────────────────
   Every stretch of tool calls (until Midum say()s / replies) collapses into
   ONE "Ran N tools" pill (.tg-pill). Expanding it drops the group into one
   small pill per call (.tc-pill); clicking a call pill opens its glass
   panel (.tc-panel) with the actual call + the tool's return.
   Base look is already a soft frosted chip; with Liquid Glass on, the
   block further below swaps in the shared --glass-* tokens so these match
   every other glass surface in the app. Only compositor-friendly
   properties animate (transform/opacity/grid rows) -- no backdrop-filter,
   for the same reason the rest of the glass system avoids it. */
.row.tool-group{align-items:flex-start;padding:4px 0;}
.tg-head{display:flex;align-items:center;gap:6px;max-width:100%;}
.tg-pill,.tc-pill{
  -webkit-appearance:none;appearance:none;font-family:inherit;cursor:pointer;user-select:none;
  position:relative;overflow:hidden;
  display:inline-flex;align-items:center;max-width:100%;
  color:var(--subtext);
  border:1px solid color-mix(in srgb, white 10%, var(--border2));
  background-color:color-mix(in srgb, var(--surface) 80%, transparent);
  background-image:linear-gradient(150deg, color-mix(in srgb, white 9%, transparent) 0%, color-mix(in srgb, white 3%, transparent) 34%, transparent 62%);
  box-shadow:inset 0 1px 0 color-mix(in srgb, white 13%, transparent), inset 0 -1px 0 color-mix(in srgb, white 4%, transparent), 0 6px 16px -12px rgba(0,0,0,.6);
  transition:background-color .18s var(--ease), border-color .18s var(--ease), color .18s var(--ease), box-shadow .18s var(--ease), transform .18s var(--ease);
}
.tg-pill:hover,.tc-pill:hover{
  color:var(--text);
  border-color:color-mix(in srgb, var(--accent) 38%, var(--border2));
  background-color:color-mix(in srgb, var(--surface2) 88%, transparent);
}
.tg-pill:active,.tc-pill:active{transform:scale(.985);}
.tg-pill:focus-visible,.tc-pill:focus-visible{
  outline:none;
  box-shadow:0 0 0 2px color-mix(in srgb, var(--accent) 55%, transparent), inset 0 1px 0 color-mix(in srgb, white 13%, transparent);
}

/* main "Ran N tools" pill */
.tg-pill{height:34px;padding:0 12px 0 11px;gap:8px;border-radius:999px;font-size:12.5px;font-weight:600;}
.tg-icon{display:inline-flex;color:var(--accent);opacity:.9;}
.tg-label{color:var(--text);letter-spacing:.005em;white-space:nowrap;}
.tg-extra{
  font-family:"JetBrains Mono","Cascadia Code",Consolas,monospace;font-size:11px;font-weight:500;
  color:var(--subtext);max-width:190px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  padding-left:8px;border-left:1px solid var(--border2);
}
.tg-extra:empty{display:none;}
.tg-chev,.tc-chev{display:inline-flex;opacity:.65;transition:transform .25s var(--ease), opacity .2s;}
.tool-group.open .tg-chev{transform:rotate(90deg);opacity:.95;}

/* status dots */
.tg-dot,.tc-dot{
  width:7px;height:7px;border-radius:50%;flex:0 0 auto;
  background:color-mix(in srgb, var(--green) 78%, transparent);
  transition:background .2s;
}
/* Active (running) state: the status dot becomes the orbit spinner. Every
   length of the original 70px loader is expressed in --u (= size / 70) so it
   scales cleanly down to pill size. */
.tool-group.live .tg-dot,.tc.pending .tc-dot{
  --sz:24px;--u:calc(var(--sz) / 70);
  width:var(--sz);height:var(--sz);border-radius:0;position:relative;
  background:
    radial-gradient(farthest-side,lightskyblue 90%,#0000) center/calc(16 * var(--u)) calc(16 * var(--u)),
    radial-gradient(farthest-side,green   90%,#0000) bottom/calc(12 * var(--u)) calc(12 * var(--u));
  background-repeat:no-repeat;
  animation:l17 1s infinite linear;
}
.tc.pending .tc-dot{--sz:20px;}
.tool-group.live .tg-dot::before,.tc.pending .tc-dot::before{
  content:"";
  position:absolute;
  width:calc(8 * var(--u));
  aspect-ratio:1;
  inset:auto 0 calc(16 * var(--u));
  margin:auto;
  background:#ccc;
  border-radius:50%;
  transform-origin:50% calc(100% + 10 * var(--u));
  animation:inherit;
  animation-duration:.5s;
}
@keyframes l17{ 100%{transform:rotate(1turn)} }
.tool-group.has-err:not(.live) .tg-dot{background:var(--yellow);}
.tc.err .tc-dot{background:var(--red);}
.tc.stale .tc-dot{background:var(--muted);}
/* soft light sweep while the group is still working */
.tool-group.live .tg-pill{border-color:color-mix(in srgb, var(--accent) 34%, var(--border2));}
.tool-group.live .tg-pill::after{
  content:"";position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(100deg, transparent 32%, color-mix(in srgb, white 13%, transparent) 50%, transparent 68%);
  transform:translateX(-100%);animation:tgSweep 2.4s ease-in-out infinite;
}
@keyframes tgSweep{ to{transform:translateX(100%);} }

/* expand / collapse (animated via grid rows so height auto works) */
.tg-body,.tc-detail{display:grid;grid-template-rows:0fr;transition:grid-template-rows .3s var(--ease);width:100%;}
.tool-group.open > .tg-body,.tc.open > .tc-detail{grid-template-rows:1fr;}
.tg-body-inner,.tc-detail-inner{min-height:0;overflow:hidden;}

/* the dropped-down list of individual call pills */
.tg-list{
  display:flex;flex-direction:column;align-items:flex-start;gap:6px;
  margin:8px 0 2px 16px;padding:3px 0 3px 16px;
  border-left:1px solid color-mix(in srgb, white 12%, var(--border2));
}
.tc{position:relative;display:flex;flex-direction:column;align-items:flex-start;width:100%;max-width:100%;}
.tc::before{
  content:"";position:absolute;left:-16px;top:14px;width:12px;height:1px;
  background:color-mix(in srgb, white 12%, var(--border2));
}
@keyframes tcDrop{ from{opacity:0;transform:translateY(-6px) scale(.97);} to{opacity:1;transform:none;} }
.tool-group.open .tc{animation:tcDrop .34s var(--ease) both;animation-delay:calc(var(--i,0) * 36ms);}

/* individual call pill */
.tc-pill{height:28px;padding:0 10px 0 9px;gap:7px;border-radius:999px;font-size:12px;}
.tc-name{
  font-family:"JetBrains Mono","Cascadia Code",Consolas,monospace;font-size:11.5px;font-weight:600;
  color:var(--text);white-space:nowrap;
}
.tc-preview{
  font-family:"JetBrains Mono","Cascadia Code",Consolas,monospace;font-size:11px;
  color:var(--subtext);opacity:.85;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.tc-preview:empty,.tc-time:empty{display:none;}
.tc.pending .tc-preview{font-style:italic;}
.tc-time{font-size:10px;color:var(--subtext);opacity:.7;font-variant-numeric:tabular-nums;}
.tc.open .tc-chev{transform:rotate(90deg);opacity:.95;}
.tc.open .tc-pill{color:var(--text);border-color:color-mix(in srgb, var(--accent) 45%, var(--border2));}
.tc.agent{opacity:.62;transition:opacity .2s;}
.tc.agent:hover,.tc.agent.open{opacity:1;}
.tc-agent{
  font-size:9.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
  color:var(--accent);background:var(--accent-faint);padding:1px 7px;border-radius:999px;
}

/* the call + return panel */
.tc-detail{width:min(580px,100%);visibility:hidden;transition:grid-template-rows .3s var(--ease), visibility 0s linear .3s;}
.tc.open > .tc-detail{visibility:visible;transition-delay:0s;}
.tc-panel{
  margin:6px 1px 3px;padding:10px 12px 12px;border-radius:18px;
  border:1px solid color-mix(in srgb, white 10%, var(--border2));
  background-color:color-mix(in srgb, var(--surface) 78%, transparent);
  background-image:linear-gradient(150deg, color-mix(in srgb, white 8%, transparent) 0%, color-mix(in srgb, white 2%, transparent) 30%, transparent 55%);
  box-shadow:inset 0 1px 0 color-mix(in srgb, white 12%, transparent), 0 10px 24px -14px rgba(0,0,0,.65);
}
.tc-block + .tc-block{margin-top:10px;}
.tc-block-head{
  display:flex;align-items:center;justify-content:space-between;margin-bottom:5px;
  font-size:9.5px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--subtext);
}
.tc-copy{
  width:20px;height:20px;border-radius:50%;border:none;background:transparent;color:var(--subtext);
  font-size:11px;cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0;
  opacity:.7;transition:opacity .15s, background .15s, color .15s;
}
.tc-copy:hover{background:var(--border2);color:var(--text);opacity:1;}
.tc-copy.copied{color:var(--green);opacity:1;}
.tc-panel pre{
  margin:0;padding:9px 11px;border-radius:12px;
  background-color:color-mix(in srgb, white 4%, transparent);
  background-image:linear-gradient(150deg, color-mix(in srgb, white 7%, transparent) 0%, color-mix(in srgb, white 2%, transparent) 35%, transparent 65%);
  border:1px solid color-mix(in srgb, white 9%, transparent);
  box-shadow:inset 0 1px 0 color-mix(in srgb, white 8%, transparent);
  color:var(--text);font-size:11.5px;line-height:1.5;
  font-family:"JetBrains Mono","Cascadia Code",Consolas,monospace;
  white-space:pre-wrap;word-break:break-word;max-height:240px;overflow:auto;user-select:text;
}
.tc.err .tc-result{color:color-mix(in srgb, var(--red) 72%, var(--text));}
.tc.pending .tc-result{opacity:.6;font-style:italic;}

/* Liquid glass: same tokens + recipe as every other glass surface above */
html.liquid-glass-on.has-bg-image .tg-pill,
html.liquid-glass-on:not(.blobs-off) .tg-pill,
html.liquid-glass-on.has-bg-image .tc-pill,
html.liquid-glass-on:not(.blobs-off) .tc-pill,
html.liquid-glass-on.has-bg-image .tc-panel,
html.liquid-glass-on:not(.blobs-off) .tc-panel{
  background-color:var(--glass-fill) !important;
  background-image:linear-gradient(150deg, var(--glass-sheen) 0%, var(--glass-sheen-soft) 30%, transparent 55%) !important;
  border-color:var(--glass-edge) !important;
  box-shadow:inset 0 1px 0 var(--glass-edge), inset 0 -1px 0 var(--glass-edge-dim), var(--glass-glow) !important;
}
html.liquid-glass-on.has-bg-image .tc-panel pre,
html.liquid-glass-on:not(.blobs-off) .tc-panel pre{
  background-color:color-mix(in srgb, white calc(var(--glass-pct) * 0.08), transparent);
  background-image:linear-gradient(150deg, var(--glass-sheen-soft) 0%, transparent 60%);
  border-color:var(--glass-edge-dim);
  box-shadow:inset 0 1px 0 var(--glass-edge-dim);
}
html.liquid-glass-on.has-bg-image .tg-pill:hover,
html.liquid-glass-on:not(.blobs-off) .tg-pill:hover,
html.liquid-glass-on.has-bg-image .tc-pill:hover,
html.liquid-glass-on:not(.blobs-off) .tc-pill:hover{
  background-color:var(--glass-fill-strong) !important;
}
html.liquid-glass-on.has-bg-image .tc.open > .tc-pill,
html.liquid-glass-on:not(.blobs-off) .tc.open > .tc-pill,
html.liquid-glass-on.has-bg-image .tool-group.live .tg-pill,
html.liquid-glass-on:not(.blobs-off) .tool-group.live .tg-pill{
  box-shadow:inset 0 0 0 1px color-mix(in srgb, var(--accent) 50%, transparent), inset 0 1px 0 var(--glass-edge), var(--glass-glow) !important;
}
@media (prefers-reduced-motion: reduce){
  .tool-group.live .tg-pill::after{animation:none;display:none;}
  .tool-group.open .tc{animation:none;}
  .tg-body,.tc-detail{transition:none;}
}
@keyframes toolPulse{
  0%{  transform:scale(0.7); box-shadow:0 0 0 0 rgba(16,185,129,.55); }
  50%{ transform:scale(1.25); box-shadow:0 0 0 4px rgba(16,185,129,0); }
  100%{transform:scale(0.7); box-shadow:0 0 0 0 rgba(16,185,129,0); }
}
pre.code-block{background:var(--tool-bg);color:var(--text);border-radius:12px;padding:10px;
  overflow-x:auto;font-family:"JetBrains Mono","Cascadia Code","Fira Code",Consolas,monospace;
  font-size:12.5px;line-height:1.55;font-feature-settings:"liga" 1,"calt" 1;}
.code-block-wrap{position:relative;}
.code-copy-btn{
  position:absolute;top:8px;right:8px;opacity:0;transition:opacity .15s,background .15s,color .15s;
  width:22px;height:22px;border-radius:6px;border:1px solid var(--border2);background:var(--surface2);
  color:var(--subtext);font-size:11px;display:flex;align-items:center;justify-content:center;
  cursor:pointer;padding:0;z-index:2;
}
.code-block-wrap:hover .code-copy-btn{opacity:1;}
.code-copy-btn:hover{background:var(--border2);color:var(--text);}
.code-copy-btn.copied{opacity:1;color:var(--green);}
code.inline-code{background:var(--surface2);color:var(--accent);border-radius:4px;padding:1px 5px;
  font-family:"JetBrains Mono","Cascadia Code","Fira Code",Consolas,monospace;font-size:12.5px;}
.bubble h1,.bubble h2,.bubble h3,.bubble h4,.bubble h5,.bubble h6{margin:.4em 0;}
.bubble h4{font-size:1em;}
.bubble h5{font-size:.92em;}
.bubble h6{font-size:.85em;color:var(--tool-text);}
.bubble a{color:var(--accent);}
.bubble hr{border:none;border-top:1px solid var(--border2);margin:10px 0;}
.bubble .bullet-line{display:block;margin-top:10px;}
.bubble .bullet-line:first-child{margin-top:0;}
.md-table-wrap{overflow-x:auto;margin:10px 0;max-width:100%;}
table.md-table{border-collapse:collapse;width:100%;font-size:13px;background:var(--surface);border-radius:8px;overflow:hidden;}
table.md-table th,table.md-table td{border:1px solid var(--border2);padding:6px 12px;text-align:left;white-space:normal;}
table.md-table th{background:var(--surface2);color:var(--text);font-weight:700;white-space:nowrap;}
table.md-table tr:nth-child(even) td{background:color-mix(in srgb, var(--surface) 92%, var(--surface2));}

/* Row + text entrance animation */
@keyframes rowIn{ from{opacity:0;transform:translateY(10px);} to{opacity:1;transform:translateY(0);} }
.row{animation:rowIn .32s var(--ease) both;}
@keyframes wordIn{ from{opacity:0;transform:translateY(4px);} to{opacity:1;transform:translateY(0);} }
.word-anim{display:inline-block;opacity:0;animation:wordIn .35s var(--ease) forwards;}

/* Flowchart rendering */
.flowchart-wrap{background:var(--surface);border:1px solid var(--border2);border-radius:16px;
  padding:14px;overflow:auto;margin:8px 0;max-width:100%;}
.flowchart-wrap svg{display:block;margin:0 auto;}
/* Mermaid diagrams render into a .mermaid placeholder inside the same
   .flowchart-wrap card used for the legacy flowchart_json renderer -- the
   card chrome (background/border/padding) is shared, only the innards
   differ (Mermaid's own SVG vs the hand-rolled one above). */
.mermaid{display:flex;justify-content:center;}
.mermaid svg{max-width:100%;}

/* LaTeX math rendering (KaTeX) -- .math-tex spans are inserted by
   extractMath() during renderInline() with the raw TeX source as their
   text content, then actually rendered in place (via katex.render) by
   renderPendingMath() once the bubble is in the DOM -- same lazy-CDN-load
   pattern as Mermaid above. */
.math-tex[data-display="1"]{display:block;margin:10px 0;overflow-x:auto;overflow-y:hidden;text-align:center;}
.math-tex .katex{font-size:1.05em;color:var(--text);}
.math-tex .katex-display{margin:0;}

/* Generated-image gallery + save button */
.img-frame{position:relative;display:inline-block;margin-top:8px;max-width:100%;}
.img-frame img{max-width:100%;border-radius:8px;display:block;}
.img-save-btn{
  position:absolute;top:8px;right:8px;width:32px;height:32px;border-radius:50%;
  background:rgba(10,9,22,.72);border:1px solid var(--border2);backdrop-filter:blur(4px);
  color:var(--text);display:flex;align-items:center;justify-content:center;font-size:15px;
  text-decoration:none;opacity:0;transition:opacity .15s var(--ease),background .15s var(--ease);
}
.img-frame:hover .img-save-btn{opacity:1;}
.img-save-btn:hover{background:var(--accent);}
.fc-node-process{fill:var(--surface2);stroke:var(--border2);}
.fc-node-start{fill:var(--accent-faint);stroke:var(--accent);}
.fc-node-end{fill:var(--accent-faint);stroke:var(--accent);}
.fc-node-decision{fill:var(--surface2);stroke:var(--accent2);}
.fc-node-io{fill:var(--surface2);stroke:var(--border2);}
.fc-label{fill:var(--text);font-size:12px;font-family:inherit;}
.fc-edge{stroke:var(--muted);stroke-width:1.5;fill:none;}
.fc-edge-label{fill:var(--subtext);font-size:10px;}

/* Ask cards */
.ask-card{border-radius:16px;background:var(--surface);border:1px solid var(--border2);padding:14px 16px;max-width:78%;}
.ask-hdr{display:flex;align-items:center;gap:6px;color:var(--accent2);font-weight:700;font-size:12px;margin-bottom:8px;}
.ask-card input[type=text]{
  width:100%;background:var(--bg);border:1px solid var(--border2);border-radius:16px;height:34px;
  padding:0 12px;color:var(--text);outline:none;margin-bottom:10px;
}
.ask-actions{display:flex;justify-content:flex-end;gap:8px;}
.ask-opt-btn{width:100%;text-align:left;background:var(--surface2);border:none;border-radius:14px;
  height:32px;padding:0 12px;color:var(--text);margin-bottom:6px;}
.ask-opt-btn:hover{background:var(--border2);}

/* ── Chat text size + readability ─────────────────────────────────────────
   Scoped to the MAIN CHAT WINDOW (messages, tool-call pills, code, tables,
   ask cards, prompt box). Reads --chat-fs (see :root). This block sits after
   the original chat rules on purpose: it supersedes their old hardcoded
   9-12px sizes with the scaled tiers and brightens the dim secondary text. */
.bubble{font-size:var(--chat-fs);line-height:1.55;}
.row-label{font-size:var(--cfs-xs);letter-spacing:.015em;}
.row:not(.midum) .row-label{color:var(--chat-sub);}
.row.system .bubble,.row.error .bubble,.row.interjection .bubble{font-size:var(--cfs-md);}
.row.system .bubble{color:var(--chat-sub);}
code.inline-code{font-size:.92em;}
pre.code-block{font-size:var(--cfs-md);line-height:1.6;}
table.md-table{font-size:var(--cfs-lg);}
.tool-line{font-size:var(--cfs-sm);color:var(--chat-sub);}
.tool-line .chevron{font-size:var(--cfs-xs);opacity:.85;}
.tool-detail .tool-detail-label{font-size:var(--cfs-xs);font-weight:700;color:var(--chat-sub);}
.tool-detail pre{font-size:var(--cfs-sm);line-height:1.5;}
.agent-tool-line{opacity:.72;}
.tg-pill{height:auto;min-height:calc(var(--chat-fs) * 2.4);font-size:var(--cfs-md);color:var(--chat-sub);}
.tg-extra{font-size:var(--cfs-sm);font-weight:500;color:var(--chat-sub);}
.tc-pill{height:auto;min-height:calc(var(--chat-fs) * 2);font-size:var(--cfs-md);color:var(--chat-sub);}
.tc::before{top:var(--chat-fs);}
.tc-name{font-size:var(--cfs-sm);}
.tc-preview{font-size:var(--cfs-sm);opacity:1;color:var(--chat-sub);}
.tc-time{font-size:var(--cfs-xs);opacity:.9;color:var(--chat-sub);}
.tc-agent{font-size:var(--cfs-xs);}
.tc-block-head{font-size:var(--cfs-xs);color:var(--chat-sub);}
.tc-copy{font-size:var(--cfs-sm);}
.tc-panel pre{font-size:var(--cfs-sm);line-height:1.55;}
.ask-hdr{font-size:var(--cfs-md);}
.ask-card input[type=text]{font-size:var(--cfs-md);height:auto;min-height:calc(var(--chat-fs) * 2.4);padding-top:4px;padding-bottom:4px;}
.ask-opt-btn{font-size:var(--cfs-md);height:auto;min-height:calc(var(--chat-fs) * 2.3);padding-top:5px;padding-bottom:5px;line-height:1.35;}
.ask-card .ghost-btn,.ask-card .btn{font-size:var(--cfs-md);height:auto;min-height:calc(var(--chat-fs) * 2.3);}
#msg-input{font-size:var(--chat-fs);line-height:1.45;min-height:calc(var(--chat-fs) * 1.45 + 14px);max-height:calc(var(--chat-fs) * 14.5 + 14px);}
#msg-input::placeholder{color:var(--chat-sub);opacity:.7;}
#send-hint{font-size:var(--cfs-xs);color:var(--chat-sub);opacity:.8;}
#kb-toggle-btn{font-size:var(--cfs-xs);}
/* SETTINGS -> Chat Text Size controls */
.chat-fs-row{display:flex;align-items:center;gap:8px;}
.chat-fs-row input[type=range]{flex:1;min-width:0;}
.chat-fs-row input[type=number]{
  width:76px;height:28px;border-radius:14px;border:1px solid var(--border2);background:var(--surface);
  color:var(--text);padding:0 8px;outline:none;font-size:12px;text-align:right;font-variant-numeric:tabular-nums;
}
.chat-fs-row input[type=number]:focus{border-color:var(--accent);}
.chat-fs-unit{font-size:11px;color:var(--subtext);}

/* Tool pane inner widgets */
.field-label{font-size:9px;font-weight:700;color:var(--subtext);margin:8px 0 4px;}
textarea.code-area{
  width:100%;flex:1;background:var(--tool-bg);color:var(--text);border:1px solid var(--border);
  border-radius:16px;padding:10px;font-family:Consolas,"Cascadia Code",monospace;font-size:12px;resize:none;
}
.stat-row{padding:5px 4px 0 4px;}
.stat-lbl{font-size:11px;color:var(--subtext);}
.stat-val{font-size:13px;color:var(--text);margin:2px 0 6px;}
.mcp-tool-row{display:flex;align-items:center;gap:10px;background:var(--panel);border:1px solid var(--border);
  border-radius:8px;padding:8px 10px;margin-bottom:6px;}
.mcp-tool-info{flex:1;min-width:0;}
.mcp-tool-name{font-weight:700;font-size:12px;font-family:monospace;}
.mcp-tool-desc{font-size:10px;color:var(--subtext);margin-top:2px;}
.mcp-tool-actions{display:flex;gap:6px;flex:0 0 auto;}
.mcp-row{display:flex;align-items:center;gap:10px;background:var(--panel);border:1px solid var(--border);
  border-radius:16px;padding:10px;margin-bottom:6px;}
.mcp-dot{width:10px;height:10px;border-radius:50%;flex:0 0 auto;}
.mcp-name{font-weight:700;font-size:13px;}
.mcp-sub{font-size:10px;color:var(--subtext);}
.tools-args{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:8px;
  max-height:150px;overflow-y:auto;margin-bottom:8px;}
.arg-row{display:flex;align-items:center;gap:8px;padding:4px 0;}
.arg-row label{font-size:11px;color:var(--subtext);width:110px;flex:0 0 auto;}
.arg-row input, .arg-row select{flex:1;height:28px;}

/* Permissions pane */
.perm-search{width:100%;height:32px;border-radius:16px;border:1px solid var(--border2);
  background:var(--surface);color:var(--text);padding:0 12px;outline:none;font-size:12px;}
.perm-group-title{font-size:10px;font-weight:700;color:var(--subtext);letter-spacing:.5px;
  margin:14px 0 6px;text-transform:uppercase;}
.perm-group:first-child .perm-group-title{margin-top:4px;}
.perm-row{display:flex;align-items:center;justify-content:space-between;gap:10px;
  background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:8px 10px;margin-bottom:6px;}
.perm-info{min-width:0;flex:1;}
.perm-name{font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.perm-desc{font-size:10px;color:var(--subtext);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.perm-seg{display:flex;flex:0 0 auto;border-radius:12px;overflow:hidden;border:1px solid var(--border2);}
.perm-opt{height:26px;padding:0 10px;font-size:10px;border:none;background:var(--surface);
  color:var(--subtext);border-right:1px solid var(--border2);transition:background .15s,color .15s;}
.perm-opt:last-child{border-right:none;}
.perm-opt:hover{background:var(--surface2);}
.perm-opt.active[data-level="always"]{background:var(--green);color:#fff;}
.perm-opt.active[data-level="ask"]{background:var(--yellow);color:#1a1400;}
.perm-opt.active[data-level="ask_password"]{background:var(--accent);color:#fff;}
.perm-opt.active[data-level="deny"]{background:var(--red);color:#fff;}
.perm-empty{font-size:11px;color:var(--subtext);padding:10px;text-align:center;}

/* Filesystem MCP approval-password box, shown at the top of the
   Permissions pane -- sets the credential backing the "Password" level
   above. Fully separate control from the per-tool segmented buttons. */
.perm-fs-pw-box{background:var(--panel);border:1px solid var(--border);border-radius:14px;
  padding:10px 12px;margin-top:10px;display:flex;flex-direction:column;gap:8px;}
.perm-fs-pw-hdr{display:flex;align-items:center;justify-content:space-between;gap:8px;}
.perm-fs-pw-title{font-size:11px;font-weight:700;color:var(--text);}
.perm-fs-pw-status{font-size:10px;padding:2px 8px;border-radius:8px;border:1px solid var(--border2);}
.perm-fs-pw-status.set{color:var(--green);border-color:var(--green);}
.perm-fs-pw-status.unset{color:var(--subtext);}
.perm-fs-pw-row{display:flex;gap:8px;}
.perm-fs-pw-row input{flex:1;height:30px;border-radius:10px;border:1px solid var(--border2);
  background:var(--surface);color:var(--text);padding:0 10px;outline:none;font-size:12px;}
.perm-fs-pw-hint{font-size:10px;color:var(--subtext);line-height:1.4;}

/* Flows tab -- node-graph editor (Drawflow, loaded from CDN on first
   visit). Left: grouped node drawer, drag items onto the canvas. Right:
   the Drawflow canvas itself, full-bleed (no padding -- the graph needs
   the whole area, unlike the text-editor tool panes). */
#flows-root{display:flex;height:100%;width:100%;}
#flow-drawer{width:170px;flex:0 0 170px;background:var(--panel);border-right:1px solid var(--border2);
  overflow-y:auto;padding:12px 8px;}
#flow-drawer-title{font-size:9px;font-weight:700;color:var(--subtext);letter-spacing:.5px;
  text-transform:uppercase;padding:0 4px 10px;}
.flow-drawer-group{margin-bottom:16px;}
.flow-drawer-group-title{font-size:9px;font-weight:700;color:var(--subtext);letter-spacing:.5px;
  text-transform:uppercase;margin-bottom:6px;padding:0 4px;}
.flow-drawer-item{
  display:flex;align-items:center;gap:8px;padding:9px 10px;border-radius:12px;
  background:var(--surface);border:1px solid var(--border2);margin-bottom:6px;
  cursor:grab;font-size:12px;color:var(--text);transition:background .15s,border-color .15s;
}
.flow-drawer-item:hover{background:var(--surface2);border-color:var(--accent);}
.flow-drawer-item:active{cursor:grabbing;}
.flow-drawer-item-icon{font-size:14px;flex:0 0 auto;width:18px;text-align:center;}
#flow-canvas-wrap{flex:1;display:flex;flex-direction:column;min-width:0;height:100%;}
#flow-toolbar{padding:10px 14px;border-bottom:1px solid var(--border2);flex:0 0 auto;}
#flow-canvas{
  flex:1;position:relative;overflow:hidden;background:var(--surface);
  background-image:radial-gradient(circle, var(--border2) 1px, transparent 1px);
  background-size:20px 20px;
}
#flow-canvas-loading{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  font-size:12px;color:var(--subtext);}
/* Node body rendered inside each Drawflow node's html */
.flow-node{display:flex;align-items:center;gap:8px;padding:11px 16px;}
.flow-node-icon{font-size:16px;}
.flow-node-label{font-size:12px;font-weight:700;color:var(--text);white-space:nowrap;}
/* Tool nodes: header (icon + name) plus a param-entry body. Each
   parameter now has a REAL Drawflow input pin (input_2, input_3, ...,
   rendered by Drawflow itself along the node's left edge) -- the field
   here is just the manual fallback value used when that pin isn't wired
   to anything. A footer line (.flow-node-pin-hint) labels the pins in
   order so it's clear what lines up with what. */
.flow-node-tool{display:flex;flex-direction:column;padding:10px 12px;min-width:190px;gap:6px;}
.flow-node-tool-hdr{display:flex;align-items:center;gap:8px;}
.flow-node-tool-hdr .flow-node-icon{font-size:14px;}
.flow-node-tool-hdr .flow-node-label{font-size:11px;flex:1;}
.flow-node-kind-badge{font-size:8px;text-transform:uppercase;letter-spacing:.03em;padding:1px 6px;border-radius:8px;background:var(--surface2);color:var(--subtext);border:1px solid var(--border2);}
.flow-node-kind-badge.flow-node-kind-output{color:var(--accent2);border-color:var(--accent2);}
.flow-node-kind-badge.flow-node-kind-hybrid{color:var(--accent);border-color:var(--accent);}
.flow-node-params{display:flex;flex-direction:column;gap:5px;}
.flow-param-row{display:flex;align-items:center;gap:6px;}
.flow-param-row.required .flow-param-label{color:var(--accent);}
.flow-param-label{font-size:9px;color:var(--subtext);width:56px;flex:0 0 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.flow-node-pin-hint{display:flex;justify-content:space-between;font-size:8px;color:var(--subtext);opacity:.75;border-top:1px dashed var(--border2);padding-top:4px;margin-top:2px;}
.flow-object-out{color:var(--accent2);}
.flow-node-logic .flow-node-tool-hdr .flow-node-label{color:var(--accent);}
.flow-node-variable .flow-node-tool-hdr .flow-node-label{color:var(--accent2);}
.flow-node-ai .flow-node-tool-hdr .flow-node-label{color:var(--yellow);}
.flow-node-ai .flow-node-tool-hdr .flow-node-icon{filter:saturate(1.2);}
.flow-param-input{
  flex:1;height:22px;font-size:10px;border-radius:8px;border:1px solid var(--border2);
  background:var(--surface);color:var(--text);padding:0 6px;min-width:0;
}
.flow-node-empty-params{font-size:9px;color:var(--subtext);font-style:italic;}
/* Theming overrides for Drawflow's own default CSS -- !important since
   drawflow.min.css loads dynamically, after this stylesheet, and its
   selectors would otherwise win the cascade on equal specificity. */
#flow-canvas .drawflow-node{
  background:var(--panel) !important;border:1px solid var(--border2) !important;
  border-radius:14px !important;color:var(--text) !important;
  box-shadow:0 4px 14px rgba(0,0,0,.35) !important;min-width:0 !important;width:auto !important;
}
#flow-canvas .drawflow-node.selected{
  border-color:var(--accent) !important;box-shadow:0 0 0 2px var(--accent-faint) !important;
}
#flow-canvas .drawflow-node .input, #flow-canvas .drawflow-node .output{
  background:var(--surface2) !important;border:2px solid var(--border2) !important;
  height:14px !important;width:14px !important;position:relative;
}
#flow-canvas .drawflow-node .input:hover, #flow-canvas .drawflow-node .output:hover{
  background:var(--accent) !important;border-color:var(--accent) !important;
}
/* Always-visible per-pin label, floating just outside the node's edge next
   to its own dot -- data-pin-label is stamped onto each .input/.output
   element by _applyPinLabels() in exact Drawflow pin order (input_1,
   input_2, ... / output_1, output_2, ...), so this text is guaranteed to
   line up with the correct dot regardless of node type. Kept subtle
   (small, muted) by default; brightens on hover so it's easy to trace a
   specific dot. The dot's native `title` attribute (also set by
   _applyPinLabels) is the accessible/tooltip fallback. */
#flow-canvas .drawflow-node .input[data-pin-label]::after,
#flow-canvas .drawflow-node .output[data-pin-label]::after{
  content:attr(data-pin-label);
  position:absolute; top:50%; transform:translateY(-50%);
  font-size:8px; line-height:1; white-space:nowrap; pointer-events:none;
  color:var(--subtext); opacity:.8; letter-spacing:.01em;
}
#flow-canvas .drawflow-node .input[data-pin-label]::after{ right:20px; }
#flow-canvas .drawflow-node .output[data-pin-label]::after{ left:20px; }
#flow-canvas .drawflow-node .input:hover[data-pin-label]::after,
#flow-canvas .drawflow-node .output:hover[data-pin-label]::after{
  color:var(--text); opacity:1;
}
#flow-canvas .connection .main-path{ stroke:var(--accent) !important;stroke-width:2.5px !important;cursor:pointer !important; }
#flow-canvas .connection .main-path:hover{ stroke:var(--red) !important; }
#flow-canvas .connection .main-path.selected{ stroke:var(--red) !important;stroke-dasharray:7,4 !important; }
#flow-canvas .connection .point{ stroke:var(--border2) !important;fill:var(--surface2) !important; }
#flow-canvas .drawflow-delete{
  background:var(--red) !important;color:#fff !important;border-radius:50% !important;border:none !important;
}

/* Custom dropdown component -- replaces native <select> popups (which
   render with OS chrome and can't be height-limited/styled consistently)
   with an in-app, theme-matched, scrollable list. The underlying <select>
   stays in the DOM (hidden) so all existing code that reads/writes
   `.value`, listens for 'change', or calls `.appendChild` on it keeps
   working untouched -- enhanceSelect() just mirrors it visually. */
.real-select-hidden{ display:none !important; }
.dropdown-wrap{ position:relative; }
.hdr-row .dropdown-wrap{ flex:1 1 auto; min-width:0; }
.dropdown-trigger{
  width:100%;text-align:left;display:flex;align-items:center;justify-content:space-between;gap:6px;
  border-radius:16px;border:1px solid var(--border2);background:var(--surface);color:var(--text);
  font-size:12px;height:32px;padding:0 10px;cursor:pointer;transition:background .15s,border-color .15s;
  overflow:hidden;
}
.dropdown-trigger span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.dropdown-trigger::after{content:"\25BE";color:var(--subtext);font-size:10px;flex:0 0 auto;}
.dropdown-trigger:hover{background:var(--surface2);}
.dropdown-wrap.open .dropdown-trigger{border-color:var(--accent);}
.arg-row .dropdown-wrap{flex:1;}
.arg-row .dropdown-trigger{height:28px;}
.dropdown-list{
  position:absolute;top:calc(100% + 4px);left:0;right:0;z-index:60;
  background:var(--surface);border:1px solid var(--border2);border-radius:14px;
  padding:4px;max-height:220px;overflow-y:auto;display:none;
  box-shadow:0 12px 30px rgba(0,0,0,.4);
}
.dropdown-list.open{display:block;}
.dropdown-option{
  padding:7px 10px;border-radius:9px;font-size:12px;color:var(--text);cursor:pointer;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.dropdown-option:hover{background:var(--surface2);}
.dropdown-option.selected{background:var(--accent-faint);color:var(--accent);font-weight:600;}
.dropdown-empty{padding:8px 10px;font-size:11px;color:var(--subtext);}

/* Native-style modal dialogs -- replaces browser confirm()/prompt()/alert()
   with an in-app overlay that matches the rest of the GUI, instead of the
   OS-chrome popup that broke the illusion of a single cohesive app. */
#modal-overlay{
  position:fixed;inset:0;z-index:1000;background:rgba(2,1,10,.55);
  display:none;align-items:center;justify-content:center;
}
#modal-overlay.open{display:flex;}
.modal-box{
  background:var(--panel);border:1px solid var(--border2);border-radius:20px;
  padding:20px;width:380px;max-width:90vw;max-height:80vh;overflow-y:auto;
  box-shadow:0 20px 60px rgba(0,0,0,.5);
  animation:modalIn .18s var(--ease) both;
}
.modal-box.wide{width:640px;}
@keyframes modalIn{ from{opacity:0;transform:scale(.96) translateY(6px);} to{opacity:1;transform:scale(1) translateY(0);} }
.modal-title{font-weight:700;font-size:14px;margin-bottom:10px;color:var(--text);}
.modal-msg{font-size:13px;color:var(--subtext);margin-bottom:12px;white-space:pre-wrap;line-height:1.5;}
.modal-input, .modal-select{
  width:100%;background:var(--surface);border:1px solid var(--border2);border-radius:12px;
  height:36px;padding:0 12px;color:var(--text);margin-bottom:10px;outline:none;font-size:13px;
}
.modal-label{font-size:9px;font-weight:700;color:var(--subtext);letter-spacing:.5px;margin:0 0 4px;}
.modal-radio-row{display:flex;gap:14px;margin-bottom:10px;}
.modal-radio-row label{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text);}
.modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:6px;}
.modal-btn{
  height:34px;padding:0 16px;border-radius:14px;border:none;font-size:12px;
  background:var(--surface2);color:var(--text);transition:background .15s;
}
.modal-btn:hover{background:var(--border2);}
.modal-btn.primary{background:var(--accent);color:#fff;}
.modal-btn.primary:hover{background:var(--accent-dim);}
.modal-btn.danger{background:transparent;color:var(--red);border:1px solid #3f0f0f;}
.modal-btn.danger:hover{background:#2d1010;}
</style>
</head>
<body>
<div id="app-titlebar">
  <div id="titlebar-left">
    <span id="titlebar-chat-title">Untitled chat</span>
    <span id="titlebar-model-pill">(auto)</span>
  </div>
  <div id="titlebar-center"></div>
  <div id="titlebar-controls">
    <button class="titlebar-btn" id="titlebar-min" title="Minimize"><i data-feather="minus"></i></button>
    <button class="titlebar-btn" id="titlebar-max" title="Maximize"><i data-feather="square"></i></button>
    <button class="titlebar-btn" id="titlebar-close" title="Close"><i data-feather="x"></i></button>
  </div>
</div>
<!-- Hanging tab strip: lives BELOW #app-titlebar (not inside it) so the
     tabs can visually hang off the titlebar's bottom edge like an
     upside-down Chrome tab strip. Because it's outside the titlebar's
     36px caption band, WM_NCHITTEST never treats it as HTCAPTION on
     Windows (see _wnd_proc's `if ly < titlebar_h_px` check) -- it's
     ordinary client area, so every tab/button here is naturally
     clickable with no nodrag-zone plumbing needed. -->
<div id="conv-tabbar-row">
  <div id="conv-tab-controls">
    <div id="conv-tabbar"><div id="conv-tab-highlight"></div></div>
    <button class="conv-tab-icon-btn" id="conv-tab-new-btn" title="New tab">+</button>
    <div id="conv-tab-group-wrap">
      <button class="conv-tab-icon-btn" id="conv-tab-group-btn" title="Tab groups"><svg class="ic ic-chevron-down" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></button>
      <div id="conv-tab-group-menu"></div>
    </div>
  </div>
</div>
<div id="root">

  <div id="bg-image-layer"></div>

  <div id="blob-layer">
    <div class="blob" id="blob-center"></div>
    <div class="blob" id="blob-a"></div>
    <div class="blob" id="blob-b"></div>
    <div class="blob" id="blob-cursor"></div>
  </div>

  <div id="topbar-wrap">
    <div id="topbar">
      <div id="left-cluster">
        <div id="brand"><svg class="ic ic-zap" viewBox="0 0 24 24" aria-hidden="true"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg> Midum</div>
        <button class="icon-btn" id="sidebar-toggle" title="Toggle sidebar"><svg class="ic ic-menu" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h18M3 6h18M3 18h18"/></svg></button>
        <div id="status-row">
          <div id="status-dot"></div>
          <div id="status-label">Initializing...</div>
        </div>
      </div>
      <div id="tabbar-wrap"><div id="tabbar"><div id="tab-highlight"></div></div></div>
      <div id="right-cluster">
        <button id="abort-btn">Abort</button>
      </div>
    </div>
  </div>

  <div id="content">
    <div class="pane-wrap pane-hidden" id="tool-pane-wrap">
      <div class="pane"><div id="tool-content"></div></div>
    </div>

    <div class="pane-wrap" id="chat-pane-wrap">
      <div id="drop-overlay"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/></svg>Drop files to attach<small>PDF, Office docs, images, audio, video, code, text…</small></div>
      <div class="pane">
        <button class="icon-btn" id="copy-chat-btn" title="Copy full conversation"
          style="position:absolute;top:10px;right:10px;z-index:5;"><svg class="ic ic-copy" viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>
        <button class="icon-btn" id="reopen-render-surface-btn" title="Reopen last Render Surface"
          style="position:absolute;top:52px;right:10px;z-index:5;"><svg class="ic ic-image" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg></button>
        <button class="icon-btn" id="reopen-midumdocs-btn" title="Open MidumDocs (LaTeX editor)"
          style="position:absolute;top:94px;right:10px;z-index:5;"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/></svg></button>
        <div id="chat-scroll"><div id="chat-col"></div></div>
        <div id="input-row">
          <div style="width:100%;max-width:760px;position:relative;">
            <div id="kb-only-badge"><span><svg class="ic ic-lock" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></span><span id="kb-only-badge-text">KB Only — internet disabled, answering from selected PDF sources</span></div>
            <div id="kb-explain-actions-row" style="display:none;gap:6px;margin-bottom:8px;">
              <button id="kb-next-part-btn" style="flex:1;height:30px;border-radius:14px;border:none;background:var(--accent);color:#fff;font-size:11px;font-weight:600;">Next Part <svg class="ic ic-arrow-right" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M12 5l7 7-7 7"/></svg></button>
              <button id="kb-open-source-btn" style="flex:0 0 auto;padding:0 14px;height:30px;border-radius:14px;border:1px solid var(--border2);background:var(--surface2);color:var(--text);font-size:11px;font-weight:600;"><svg class="ic ic-file" viewBox="0 0 24 24" aria-hidden="true"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M13 2v7h7"/></svg> Open Source</button>
            </div>
            <div id="path-picker">
              <div id="path-picker-crumb"></div>
              <div id="path-picker-list"></div>
            </div>
            <div id="capability-picker">
              <div id="capability-picker-crumb"></div>
              <div id="capability-picker-list"></div>
            </div>
            <div id="kb-popover">
              <div class="kb-only-row">
                <input type="checkbox" id="kb-only-checkbox"/>
                <label for="kb-only-checkbox">KB Only</label>
              </div>
              <div class="kb-only-hint">Restricts this session to the selected PDF sources below and disables internet search — resets when Midum restarts.</div>
              <div id="kb-sources-list"></div>
              <div id="kb-explain-mode-row">
                <button class="kb-mode-btn" id="kb-mode-part-btn" data-mode="part">Part-by-Part</button>
                <button class="kb-mode-btn" id="kb-mode-page-btn" data-mode="page">Page-by-Page</button>
              </div>
              <button id="kb-start-explanation-btn" style="display:none;width:100%;margin-top:8px;height:30px;border-radius:14px;border:none;background:var(--accent);color:#fff;font-size:11px;font-weight:600;"><svg class="ic ic-play ic-fill" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4l14 8-14 8z"/></svg> Start Explanation</button>
              <div class="kb-only-hint" id="kb-explain-hint" style="display:none;">Walks through the selected source(s) part by part (one sub-sub-heading per response) — say "next" to continue.</div>
            </div>
            <div id="input-box">
              <button class="icon-btn" id="kb-toggle-btn" title="Knowledge Base options"><svg class="ic ic-chevron-down" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></button>
              <div id="attach-tray"></div>
              <button class="icon-btn" id="attach-btn" title="Attach files (or drag & drop / paste)"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg></button>
              <textarea id="msg-input" placeholder="Message Midum..." rows="1"></textarea>
              <button id="send-btn"><svg class="ic ic-arrow-up" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"/></svg></button>
            </div>
            <div id="send-hint">Enter to send</div>
          </div>
        </div>
      </div>
    </div>

    <div class="pane-wrap pane-hidden" id="sidebar-pane-wrap">
      <div class="pane"><div id="sidebar-inner"></div></div>
    </div>

    <div class="pane-wrap pane-hidden" id="render-surface-pane-wrap">
      <div class="pane" id="render-surface-pane">
        <div id="render-surface-header">
          <span id="render-surface-title">Render Surface</span>
          <button class="icon-btn" id="render-surface-save" title="Save this Render Surface"><svg class="ic ic-save" viewBox="0 0 24 24" aria-hidden="true"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/></svg></button>
          <button class="icon-btn" id="render-surface-close" title="Close"><svg class="ic ic-x" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
        </div>
        <iframe id="render-surface-frame" sandbox="allow-scripts allow-forms allow-popups allow-modals" referrerpolicy="strict-origin-when-cross-origin"></iframe>
      </div>
    </div>

    <div class="pane-wrap pane-hidden" id="midumdocs-pane-wrap">
      <div class="pane" id="midumdocs-pane">
        <div id="midumdocs-header">
          <span id="midumdocs-title">MidumDoc</span>
          <span class="md-status" id="midumdocs-status"><i></i><span id="midumdocs-status-text"></span></span>
          <div class="md-seg" id="midumdocs-modes"><button data-mode="source">Source</button><button data-mode="split" class="active">Split</button><button data-mode="page">Page</button></div>
          <button class="icon-btn" id="midumdocs-recompile" title="Recompile (Shift = clean rebuild)"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg></button>
          <button class="icon-btn" id="midumdocs-pdf" title="Save as PDF"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg></button>
          <button class="icon-btn" id="midumdocs-tex" title="Save .tex source"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/></svg></button>
          <button class="icon-btn" id="midumdocs-close" title="Close"><svg class="ic ic-x" viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
        </div>
        <iframe id="midumdocs-frame" sandbox="allow-scripts allow-same-origin allow-forms allow-modals" referrerpolicy="no-referrer"></iframe>
      </div>
    </div>
  </div>
</div>

<div id="modal-overlay"><div class="modal-box" id="modal-box"></div></div>

<script>
// "Log" and "Parameters" used to live here as ordinary tabs, but they're
// now reached as togglable buttons from the Settings panel instead (see
// the "PANELS" section in buildSidebar's settings overlay) so they no
// longer eat into the horizontal tab bar's space.
const TABS = [
  ["Chat","message"], ["Voice","mic"], ["Model","cpu"],
  ["System Core","sparkles"], ["Knowledge","book-open"], ["Skills","layers"], ["Tools","wrench"], ["Shell","terminal"], ["Flows","link"], ["Agents","bot"], ["MCP","plug"], ["Permissions","lock"], ["Editor","code"]
  // "Surfaces" used to live here as a top tab, but it's a sidebar quick-
  // access button now (see #sidebar-surfaces-btn in buildSidebar) -- it's
  // a "jump to my saved surfaces" shortcut, not a work area someone tabs
  // between repeatedly, so it reads better pinned at the top of the
  // sidebar than competing for space in the horizontal tab bar.
];

let state = {
  activeTab: "Chat",
  sidebarOpen: false,
  renderSurfaceOpen: false,
  midumDocOpen: false,
  thinking: false,
  kbOnly: false,
  kbSources: [],
  explainMode: false,
  explainModeType: "part",   // "part" (Part-by-Part) or "page" (Page-by-Page)
  // Non-null (the sub-agent's name) while previewing a read-only agent
  // conversation tab -- see switchConvTab/setViewingAgentTab. Blocks
  // sendMessage() and lets appendAgentToolRow live-append rows for that
  // one agent while it's the tab on screen.
  viewingAgentTab: null,
};

let voiceState = { running: false, connecting: false };

// Scrollback for the Shell tab -- kept here (not just in the DOM) so
// switching away to another tab and back doesn't lose history, since
// showToolPane() tears down and rebuilds #tool-content on every switch.
const shellHistories = {wsl: [], powershell: []};
let shellActive = "wsl";
let shellInfo = [];
let shellHistory = shellHistories.wsl;
const SHELL_HISTORY_CAP = 1000;

// Tracks the chat row currently receiving say()/reply text for the
// in-progress turn, so multiple say() calls (and the final reply) within
// ONE response fold into a single bubble with a single "Midum" label at
// the top, instead of each call creating its own row+label. Reset to null
// whenever a new turn starts, whenever a tool call interrupts the flow, and
// once the turn is fully done -- each of those should start a fresh bubble.
let _currentMidumRow = null;
// Companion to _currentMidumRow, tracking the most recent live USER row
// (see appendRow) -- both back the Edit Prompt / Regenerate Response
// buttons, which only ever attach to the single latest turn's two rows.
let _currentUserRow  = null;
// turn_id the backend assigned to the in-flight/just-finished turn (see
// Api.send_message / Api.regenerate_last_response) -- attached to new
// rows as they're created so their edit/regen buttons call back with the
// right id, and used to guard against stale clicks.
let _pendingTurnId   = null;

// ── Icon set ───────────────────────────────────────────────────────────────
// Every emoji / pictograph in the UI is drawn from this one stroke-icon set
// (24x24, 2px round strokes, currentColor) so they all match the wrench in
// the tool-call pills and follow the surrounding text colour and size.
//   icon("name")   -> inline <svg> markup (use inside innerHTML / templates)
//   iconHtml(v)    -> icon name, ready-made svg, or a legacy emoji -> svg
//   iconizeHtml(s) -> swaps known emoji inside already-escaped HTML for icons
//                     (used for Python-sent log / system lines, which still
//                     carry emoji)
const _ICONS = {
  "wrench": '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
  "message": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  "mic": '<path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v4"/><path d="M8 23h8"/>',
  "cpu": '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/>',
  "sparkles": '<path d="M9.94 15.5A2 2 0 0 0 8.5 14.06l-6.14-1.58a.5.5 0 0 1 0-.96L8.5 9.94A2 2 0 0 0 9.94 8.5l1.58-6.14a.5.5 0 0 1 .96 0l1.58 6.14a2 2 0 0 0 1.44 1.44l6.14 1.58a.5.5 0 0 1 0 .96l-6.14 1.58a2 2 0 0 0-1.44 1.44l-1.58 6.14a.5.5 0 0 1-.96 0z"/><path d="M20 3v4M22 5h-4M4 17v2M5 18H3"/>',
  "book-open": '<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>',
  "layers": '<path d="M12 2 2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>',
  "terminal": '<path d="M4 17l6-6-6-6"/><path d="M12 19h8"/>',
  "code": '<path d="M16 18l6-6-6-6"/><path d="M8 6l-6 6 6 6"/>',
  "link": '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
  "bot": '<rect x="4" y="8" width="16" height="12" rx="2"/><path d="M12 8V4H8M2 14h2M20 14h2M15 13v2M9 13v2"/>',
  "plug": '<path d="M12 22v-5M9 8V2M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8z"/>',
  "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  "key": '<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>',
  "package": '<path d="M16.5 9.4l-9-5.19"/><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="M3.27 6.96 12 12.01l8.73-5.05M12 22.08V12"/>',
  "octagon-x": '<path d="M7.86 2h8.28L22 7.86v8.28L16.14 22H7.86L2 16.14V7.86z"/><path d="M15 9l-6 6M9 9l6 6"/>',
  "clipboard": '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/>',
  "file": '<path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M13 2v7h7"/>',
  "file-text": '<path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M13 2v7h7M16 13H8M16 17H8M10 9H8"/>',
  "folder": '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  "chart": '<path d="M18 20V10M12 20V4M6 20v-6"/>',
  "image": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>',
  "save": '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/>',
  "trash": '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6M14 11v6"/>',
  "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  "alert": '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4M12 17h.01"/>',
  "help": '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3M12 17h.01"/>',
  "check-circle": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="M22 4 12 14.01l-3-3"/>',
  "x-circle": '<circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9l6 6"/>',
  "check": '<path d="M20 6 9 17l-5-5"/>',
  "x": '<path d="M18 6 6 18M6 6l12 12"/>',
  "play": '<path d="M6 4l14 8-14 8z"/>',
  "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  "chevron-right": '<path d="M9 18l6-6-6-6"/>',
  "chevron-left": '<path d="M15 18l-6-6 6-6"/>',
  "chevron-down": '<path d="M6 9l6 6 6-6"/>',
  "arrow-up": '<path d="M12 19V5M5 12l7-7 7 7"/>',
  "arrow-left": '<path d="M19 12H5M12 19l-7-7 7-7"/>',
  "arrow-right": '<path d="M5 12h14M12 5l7 7-7 7"/>',
  "menu": '<path d="M3 12h18M3 6h18M3 18h18"/>',
  "zap": '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
  "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  "shuffle": '<path d="M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5"/>',
  "repeat": '<path d="M17 1l4 4-4 4"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><path d="M7 23l-4-4 4-4"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/>',
  "braces": '<path d="M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5c0 1.1.9 2 2 2h1M16 21h1a2 2 0 0 0 2-2v-5c0-1.1.9-2 2-2a2 2 0 0 1-2-2V5a2 2 0 0 0-2-2h-1"/>',
  "target": '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
  "tag": '<path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><path d="M7 7h.01"/>',
  "star": '<path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/>',
  "star-fill": '<path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/>',
  "scissors": '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.12 15.88M14.47 14.48 20 20M8.12 8.12 12 12"/>',
  "pencil": '<path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>',
  "copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  "refresh": '<path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
  "rotate-ccw": '<path d="M1 4v6h6"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>',
  "power": '<path d="M18.36 6.64a9 9 0 1 1-12.73 0M12 2v10"/>',
  "moon": '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
  "sun": '<circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>',
  "megaphone": '<path d="M3 11l18-5v12L3 14v-3z"/><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6"/>',
  "dot": '<circle cx="12" cy="12" r="6"/>',
  "code": '<path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/>'
};
const _ICON_FILLED = new Set(["dot", "play", "star-fill", "stop"]);
function icon(name, extraClass){
  const p = _ICONS[name];
  if (!p) return "";
  return '<svg class="ic ic-' + name + (_ICON_FILLED.has(name) ? " ic-fill" : "") + (extraClass ? " " + extraClass : "") + '" viewBox="0 0 24 24" aria-hidden="true">' + p + '</svg>';
}
const _EMOJI_ICON = {
  "💬": "message",
  "🎤": "mic",
  "🧬": "cpu",
  "🧠": "sparkles",
  "📚": "book-open",
  "🛠": "layers",
  "🔧": "wrench",
  "💻": "terminal",
  "🐚": "terminal",
  "🔗": "link",
  "🤖": "bot",
  "🔌": "plug",
  "🔐": "lock",
  "🔒": "lock",
  "🔑": "key",
  "📦": "package",
  "🛑": "octagon-x",
  "📋": "clipboard",
  "📄": "file",
  "📁": "folder",
  "📂": "folder",
  "📊": "chart",
  "🖼": "image",
  "💾": "save",
  "🗑": "trash",
  "⚙": "settings",
  "❓": "help",
  "✅": "check-circle",
  "❌": "x-circle",
  "✓": "check",
  "✕": "x",
  "✗": "x",
  "⚠": "alert",
  "⚡": "zap",
  "⏰": "clock",
  "⏳": "clock",
  "🔀": "shuffle",
  "🔁": "repeat",
  "🧩": "braces",
  "📝": "file-text",
  "📜": "file-text",
  "🎯": "target",
  "🏷": "tag",
  "⭐": "star",
  "★": "star-fill",
  "✂": "scissors",
  "✏": "pencil",
  "✎": "pencil",
  "☰": "menu",
  "⌄": "chevron-down",
  "▾": "chevron-down",
  "↑": "arrow-up",
  "←": "arrow-left",
  "↻": "refresh",
  "⟳": "refresh",
  "↺": "rotate-ccw",
  "⧉": "copy",
  "⏻": "power",
  "🌙": "moon",
  "☀": "sun",
  "▶": "play",
  "◀": "chevron-left",
  "⏹": "stop",
  "⬤": "dot",
  "📢": "megaphone"
};
const _EMOJI_RE = new RegExp("(" + Object.keys(_EMOJI_ICON).join("|") + ")\\uFE0F?", "g");
function iconizeHtml(html){
  return String(html).replace(_EMOJI_RE, (m, e)=>icon(_EMOJI_ICON[e]) || m);
}
function iconHtml(v){
  if (v === null || v === undefined) return "";
  v = String(v);
  if (_ICONS[v]) return icon(v);
  if (v.charAt(0) === "<") return v;
  return iconizeHtml(escapeHtml(v));
}

function api(name, ...args){ return window.pywebview.api[name](...args); }

// ── "#" file-path picker ---------------------------------------------------
// Typing "#" in the prompt box (at the very start, or right after
// whitespace) opens a small live folder browser positioned right above the
// box: drives first, then whatever's inside wherever you navigate. Typing
// more characters after the "#" filters the *currently loaded* listing
// entirely client-side (no round trip), so it stays fast; only actually
// drilling into a different folder does one fresh, cheap list_path_entries
// call. Selecting a file/folder splices its full quoted path into the
// message in place of the "#..." trigger text.
const pathPicker = {
  open: false,
  triggerIndex: -1,   // index of the "#" character in msg-input's value
  path: null,         // null while showing the top-level drive list
  entries: [],        // raw entries for the currently loaded listing
  filtered: [],
  activeIndex: -1,
};

function _pathPickerEls(){
  return {
    input: document.getElementById("msg-input"),
    box: document.getElementById("path-picker"),
    crumb: document.getElementById("path-picker-crumb"),
    list: document.getElementById("path-picker-list"),
  };
}

function initPathPicker(){
  const { input, box } = _pathPickerEls();
  if (!input) return;
  input.addEventListener("keydown", (e)=>{
    if (!pathPicker.open) return;
    if (e.key === "ArrowDown"){ e.preventDefault(); _movePathPicker(1); }
    else if (e.key === "ArrowUp"){ e.preventDefault(); _movePathPicker(-1); }
    else if (e.key === "Enter"){ e.preventDefault(); e.stopPropagation(); _activatePathPickerRow(e.shiftKey); }
    else if (e.key === "Escape"){ e.preventDefault(); closePathPicker(); }
    else if (e.key === "Backspace"){
      if (input.selectionStart === pathPicker.triggerIndex + 1 && input.selectionStart === input.selectionEnd){
        // Filter text is already empty right after the "#" -- treat
        // Backspace as "go up a level" instead of deleting the "#" itself.
        e.preventDefault();
        _goUpPathPicker();
      }
    }
  }, true); // capture, so this runs before the plain Enter-to-send listener
  document.addEventListener("click", (e)=>{
    if (pathPicker.open && !box.contains(e.target) && e.target !== input){
      closePathPicker();
    }
  });
}

function _currentPathPickerFilter(){
  const { input } = _pathPickerEls();
  const cursor = input.selectionStart;
  if (cursor < pathPicker.triggerIndex + 1) return null; // cursor moved before the trigger -- no longer applies
  const text = input.value.slice(pathPicker.triggerIndex + 1, cursor);
  if (/\s/.test(text)) return null; // whitespace cancels the trigger, like most "@mention" pickers
  return text;
}

// Called from msg-input's "input" listener on every keystroke.
function checkPathPickerTrigger(){
  const { input } = _pathPickerEls();
  if (!input || capabilityPicker.open) return; // the two pickers are mutually exclusive
  const cursor = input.selectionStart;

  if (pathPicker.open){
    const filter = _currentPathPickerFilter();
    if (filter === null){ closePathPicker(); return; }
    _renderPathPickerList(filter);
    return;
  }

  const charBefore = input.value.slice(cursor - 1, cursor);
  const charBeforeThat = input.value.slice(cursor - 2, cursor - 1);
  if (charBefore === "#" && (cursor === 1 || /\s/.test(charBeforeThat))){
    pathPicker.triggerIndex = cursor - 1;
    openPathPicker();
  }
}

async function openPathPicker(){
  closeCapabilityPicker();
  pathPicker.open = true;
  pathPicker.path = null;
  const { box } = _pathPickerEls();
  box.classList.add("open");
  const drives = await api("list_drives");
  if (!pathPicker.open) return; // closed again before this resolved
  pathPicker.entries = drives.map(d=>({ name: d.name, path: d.path, is_dir: true }));
  _renderPathPickerCrumb();
  _renderPathPickerList("");
}

async function _navigatePathPicker(path){
  const res = await api("list_path_entries", path);
  if (!pathPicker.open) return; // closed again before this resolved
  if (!res || !res.ok){
    pathPicker.entries = [];
    pathPicker.path = path;
    _renderPathPickerCrumb();
    _renderPathPickerList("", (res && res.error) || "Couldn't open that folder.");
    return;
  }
  pathPicker.path = res.path;
  pathPicker.entries = res.entries;
  _renderPathPickerCrumb();
  // Drilling into a folder consumes whatever filter text got the user
  // there -- clear it out of the actual message text too, and reset the
  // trigger to right after the "#" so a fresh filter starts clean.
  const { input } = _pathPickerEls();
  const cursor = input.selectionStart;
  input.value = input.value.slice(0, pathPicker.triggerIndex + 1) + input.value.slice(cursor);
  input.selectionStart = input.selectionEnd = pathPicker.triggerIndex + 1;
  autosizeMsgInput();
  _renderPathPickerList("");
  input.focus();
}

function _goUpPathPicker(){
  if (pathPicker.path === null) return; // already at the drive list
  const parent = _pathPickerParent(pathPicker.path);
  if (parent === null){ pathPicker.path = null; openPathPicker(); return; }
  _navigatePathPicker(parent);
}

function _pathPickerParent(path){
  if (/^[A-Za-z]:\\?$/.test(path)) return null; // Windows drive root -- back to drive list
  if (path === "/") return null;
  const trimmed = path.replace(/[\\/]+$/, "");
  const idx = Math.max(trimmed.lastIndexOf("\\"), trimmed.lastIndexOf("/"));
  if (idx < 0) return null;
  return trimmed.slice(0, idx + 1) || null;
}

function _renderPathPickerCrumb(){
  const { crumb } = _pathPickerEls();
  crumb.textContent = pathPicker.path === null ? "This PC" : pathPicker.path;
}

function _renderPathPickerList(filterText, errorText){
  const { list } = _pathPickerEls();
  const q = (filterText || "").toLowerCase();
  pathPicker.filtered = pathPicker.entries.filter(e=>e.name.toLowerCase().includes(q));
  pathPicker.activeIndex = pathPicker.filtered.length ? 0 : -1;
  if (errorText){
    list.innerHTML = `<div class="path-picker-empty">${escapeHtml(errorText)}</div>`;
    return;
  }
  if (!pathPicker.filtered.length){
    list.innerHTML = `<div class="path-picker-empty">No matches.</div>`;
    return;
  }
  list.innerHTML = pathPicker.filtered.map((e, i)=>
    `<div class="path-picker-row${i===0?' active':''}" data-i="${i}" title="${escapeHtml(e.is_dir ? 'Click: open   ·   Shift+Click: insert this folder\'s path (no quotes)' : 'Click: insert this file\'s path (no quotes)')}">`
    + `<span class="path-picker-icon">${icon(e.is_dir ? "folder" : "file")}</span>`
    + `<span class="path-picker-name">${escapeHtml(e.name)}</span>`
    + `</div>`
  ).join("");
  list.querySelectorAll(".path-picker-row").forEach(row=>{
    // mousedown (not click) + preventDefault: a plain click would first let
    // the browser blur/refocus away from #msg-input, which can shift or
    // clear its selectionStart/selectionEnd before insertPathPickerResult()
    // reads them -- the classic "mention picker" bug where a selection
    // silently inserts nothing (or inserts in the wrong place). Blocking
    // the default mousedown behavior keeps the textarea focused and its
    // cursor position exactly where it was the whole time.
    row.addEventListener("mousedown", (ev)=>{
      ev.preventDefault();
      _activatePathPickerRow(ev.shiftKey, parseInt(row.dataset.i, 10));
    });
  });
}

function _movePathPicker(delta){
  if (!pathPicker.filtered.length) return;
  const n = pathPicker.filtered.length;
  pathPicker.activeIndex = ((pathPicker.activeIndex + delta) % n + n) % n;
  const { list } = _pathPickerEls();
  list.querySelectorAll(".path-picker-row").forEach((row, i)=>{
    row.classList.toggle("active", i === pathPicker.activeIndex);
  });
  const activeRow = list.children[pathPicker.activeIndex];
  if (activeRow) activeRow.scrollIntoView({ block: "nearest" });
}

// insertOnly (Shift+Enter / Shift+Click): insert the highlighted folder's
// own path instead of navigating into it -- lets you reference a folder
// itself, not just files inside it.
function _activatePathPickerRow(insertOnly, index){
  const i = index !== undefined ? index : pathPicker.activeIndex;
  const entry = pathPicker.filtered[i];
  if (!entry) return;
  if (entry.is_dir && !insertOnly){
    _navigatePathPicker(entry.path);
  } else {
    insertPathPickerResult(entry.path);
  }
}

function insertPathPickerResult(path){
  const { input } = _pathPickerEls();
  const cursor = input.selectionStart;
  const before = input.value.slice(0, pathPicker.triggerIndex);
  const after = input.value.slice(Math.max(cursor, pathPicker.triggerIndex + 1));
  const insertText = `${path} `;
  input.value = before + insertText + after;
  const newCursor = before.length + insertText.length;
  input.selectionStart = input.selectionEnd = newCursor;
  autosizeMsgInput();
  closePathPicker();
  input.focus();
}

function closePathPicker(){
  pathPicker.open = false;
  pathPicker.path = null;
  pathPicker.entries = [];
  pathPicker.filtered = [];
  pathPicker.activeIndex = -1;
  pathPicker.triggerIndex = -1;
  const { box } = _pathPickerEls();
  box.classList.remove("open");
}

// ── "/" capability picker -----------------------------------------------------
// Typing "/" in the prompt box (at the very start, or right after
// whitespace) opens a small live browser -- positioned exactly like the
// "#" path picker -- over the five capability sections: Native Tools, MCP
// Tools, Skills, Flows and KBs. Native Tools and MCP Tools are two levels
// deep (functional category, then MCP server, before reaching individual
// tools); Skills/Flows/KBs go straight from the section to their saved-item
// list. `stack` holds the navigation path taken so far (each entry is the
// picked row at that depth); the currently displayed rows for whatever
// depth we're at are (re)loaded into `entries` by _loadCapabilityLevel.
const capabilityPicker = {
  open: false,
  triggerIndex: -1,
  stack: [],      // [] at the section root; grows as the user drills in
  entries: [],
  filtered: [],
  activeIndex: -1,
};

function _capabilityPickerEls(){
  return {
    input: document.getElementById("msg-input"),
    box: document.getElementById("capability-picker"),
    crumb: document.getElementById("capability-picker-crumb"),
    list: document.getElementById("capability-picker-list"),
  };
}

function initCapabilityPicker(){
  const { input, box } = _capabilityPickerEls();
  if (!input) return;
  input.addEventListener("keydown", (e)=>{
    if (!capabilityPicker.open) return;
    if (e.key === "ArrowDown"){ e.preventDefault(); _moveCapabilityPicker(1); }
    else if (e.key === "ArrowUp"){ e.preventDefault(); _moveCapabilityPicker(-1); }
    else if (e.key === "Enter"){ e.preventDefault(); e.stopPropagation(); _activateCapabilityPickerRow(); }
    else if (e.key === "ArrowRight"){ _activateCapabilityPickerRow(true); }
    else if (e.key === "Escape"){ e.preventDefault(); closeCapabilityPicker(); }
    else if (e.key === "Backspace"){
      if (input.selectionStart === capabilityPicker.triggerIndex + 1 && input.selectionStart === input.selectionEnd){
        e.preventDefault();
        _goUpCapabilityPicker();
      }
    }
  }, true); // capture, so this runs before the plain Enter-to-send listener
  document.addEventListener("click", (e)=>{
    if (capabilityPicker.open && !box.contains(e.target) && e.target !== input){
      closeCapabilityPicker();
    }
  });
}

function _currentCapabilityPickerFilter(){
  const { input } = _capabilityPickerEls();
  const cursor = input.selectionStart;
  if (cursor < capabilityPicker.triggerIndex + 1) return null;
  const text = input.value.slice(capabilityPicker.triggerIndex + 1, cursor);
  if (/\s/.test(text)) return null; // whitespace cancels the trigger, like the "#" picker
  return text;
}

// Called from msg-input's "input" listener on every keystroke.
function checkCapabilityPickerTrigger(){
  const { input } = _capabilityPickerEls();
  if (!input || pathPicker.open) return; // the two pickers are mutually exclusive
  const cursor = input.selectionStart;

  if (capabilityPicker.open){
    const filter = _currentCapabilityPickerFilter();
    if (filter === null){ closeCapabilityPicker(); return; }
    _renderCapabilityPickerList(filter);
    return;
  }

  const charBefore = input.value.slice(cursor - 1, cursor);
  const charBeforeThat = input.value.slice(cursor - 2, cursor - 1);
  if (charBefore === "/" && (cursor === 1 || /\s/.test(charBeforeThat))){
    capabilityPicker.triggerIndex = cursor - 1;
    openCapabilityPicker();
  }
}

async function openCapabilityPicker(){
  closePathPicker();
  capabilityPicker.open = true;
  capabilityPicker.stack = [];
  const { box } = _capabilityPickerEls();
  box.classList.add("open");
  await _loadCapabilityLevel();
  if (!capabilityPicker.open) return; // closed again before this resolved
  _renderCapabilityPickerCrumb();
  _renderCapabilityPickerList("");
}

// Fetches whatever level the current `stack` points at into `entries`.
// Root (stack empty) -> the 5 sections. One level down from "native"/"mcp"
// -> categories/servers. Two levels down from those -> the actual tools.
// One level down from "skills"/"flows"/"kbs" -> their saved items directly.
async function _loadCapabilityLevel(){
  const depth = capabilityPicker.stack.length;
  if (depth === 0){
    const sections = await api("list_capability_sections");
    capabilityPicker.entries = (sections || []).map(s=>({
      section: s.key, label: s.label, icon: s.icon, count: s.count, is_leaf: false,
    }));
    return;
  }
  const root = capabilityPicker.stack[0].section;
  if (root === "native"){
    if (depth === 1){
      const cats = await api("list_native_tool_categories");
      capabilityPicker.entries = (cats || []).map(c=>({
        label: c.name, icon: "folder", count: c.count, is_leaf: false,
      }));
    } else {
      const tools = await api("list_native_tools_by_category", capabilityPicker.stack[1].label);
      capabilityPicker.entries = (tools || []).map(t=>({
        label: t.name, desc: t.desc, icon: "wrench", is_leaf: true,
      }));
    }
  } else if (root === "mcp"){
    if (depth === 1){
      const servers = await api("list_mcp_servers_for_picker");
      capabilityPicker.entries = (servers || []).map(s=>({
        label: s.name, icon: s.connected ? "plug" : "alert", count: s.count, is_leaf: false,
      }));
    } else {
      const tools = await api("list_mcp_tools_by_server", capabilityPicker.stack[1].label);
      capabilityPicker.entries = (tools || []).map(t=>({
        label: t.name, desc: t.desc, icon: "plug", is_leaf: true,
      }));
    }
  } else if (root === "skills"){
    const items = await api("list_skills_capability");
    capabilityPicker.entries = (items || []).map(i=>({ label: i.name, desc: i.desc, icon: "sparkles", is_leaf: true }));
  } else if (root === "flows"){
    const items = await api("list_flows_capability");
    capabilityPicker.entries = (items || []).map(i=>({ label: i.name, desc: i.desc, icon: "link", is_leaf: true }));
  } else if (root === "kbs"){
    const items = await api("list_kbs_capability");
    capabilityPicker.entries = (items || []).map(i=>({ label: i.name, desc: i.desc, icon: "book-open", is_leaf: true }));
  } else {
    capabilityPicker.entries = [];
  }
}

async function _drillIntoCapabilityPicker(entry){
  capabilityPicker.stack.push(entry.section !== undefined ? entry : { label: entry.label });
  await _loadCapabilityLevel();
  if (!capabilityPicker.open) return;
  // Drilling in consumes whatever filter text got the user here -- clear it
  // out of the actual message text too, mirroring _navigatePathPicker.
  const { input } = _capabilityPickerEls();
  const cursor = input.selectionStart;
  input.value = input.value.slice(0, capabilityPicker.triggerIndex + 1) + input.value.slice(cursor);
  input.selectionStart = input.selectionEnd = capabilityPicker.triggerIndex + 1;
  autosizeMsgInput();
  _renderCapabilityPickerCrumb();
  _renderCapabilityPickerList("");
  input.focus();
}

function _goUpCapabilityPicker(){
  if (!capabilityPicker.stack.length){ closeCapabilityPicker(); return; }
  capabilityPicker.stack.pop();
  _loadCapabilityLevel().then(()=>{
    if (!capabilityPicker.open) return;
    _renderCapabilityPickerCrumb();
    _renderCapabilityPickerList("");
  });
}

function _renderCapabilityPickerCrumb(){
  const { crumb } = _capabilityPickerEls();
  crumb.textContent = capabilityPicker.stack.length
    ? capabilityPicker.stack.map(s=>s.label).join(" / ")
    : "Capabilities";
}

function _renderCapabilityPickerList(filterText){
  const { list } = _capabilityPickerEls();
  const q = (filterText || "").toLowerCase();
  capabilityPicker.filtered = capabilityPicker.entries.filter(e=>e.label.toLowerCase().includes(q));
  capabilityPicker.activeIndex = capabilityPicker.filtered.length ? 0 : -1;
  if (!capabilityPicker.filtered.length){
    list.innerHTML = `<div class="capability-picker-empty">No matches.</div>`;
    return;
  }
  list.innerHTML = capabilityPicker.filtered.map((e, i)=>
    `<div class="capability-picker-row${i===0?' active':''}" data-i="${i}" `
    + `title="${escapeHtml(e.is_leaf ? 'Click: insert this name' : 'Click: open')}">`
    + `<span class="capability-picker-icon">${iconHtml(e.icon || (e.is_leaf ? "•" : "folder"))}</span>`
    + `<span class="capability-picker-main">`
    + `<span class="capability-picker-name">${escapeHtml(e.label)}</span>`
    + (e.desc ? `<span class="capability-picker-desc">${escapeHtml(e.desc)}</span>` : ``)
    + `</span>`
    + (typeof e.count === "number" ? `<span class="capability-picker-count">${e.count}</span>` : ``)
    + `</div>`
  ).join("");
  list.querySelectorAll(".capability-picker-row").forEach(row=>{
    // mousedown (not click) + preventDefault, same reasoning as the "#"
    // picker: keeps #msg-input focused with its selection intact.
    row.addEventListener("mousedown", (ev)=>{
      ev.preventDefault();
      _activateCapabilityPickerRow(false, parseInt(row.dataset.i, 10));
    });
  });
}

function _moveCapabilityPicker(delta){
  if (!capabilityPicker.filtered.length) return;
  const n = capabilityPicker.filtered.length;
  capabilityPicker.activeIndex = ((capabilityPicker.activeIndex + delta) % n + n) % n;
  const { list } = _capabilityPickerEls();
  list.querySelectorAll(".capability-picker-row").forEach((row, i)=>{
    row.classList.toggle("active", i === capabilityPicker.activeIndex);
  });
  const activeRow = list.children[capabilityPicker.activeIndex];
  if (activeRow) activeRow.scrollIntoView({ block: "nearest" });
}

function _activateCapabilityPickerRow(drillOnly, index){
  const i = index !== undefined ? index : capabilityPicker.activeIndex;
  const entry = capabilityPicker.filtered[i];
  if (!entry) return;
  if (!entry.is_leaf){
    _drillIntoCapabilityPicker(entry);
  } else {
    insertCapabilityPickerResult(entry.label);
  }
}

function insertCapabilityPickerResult(name){
  const { input } = _capabilityPickerEls();
  const cursor = input.selectionStart;
  const before = input.value.slice(0, capabilityPicker.triggerIndex);
  const after = input.value.slice(Math.max(cursor, capabilityPicker.triggerIndex + 1));
  const insertText = `/${name} `;
  input.value = before + insertText + after;
  const newCursor = before.length + insertText.length;
  input.selectionStart = input.selectionEnd = newCursor;
  autosizeMsgInput();
  closeCapabilityPicker();
  input.focus();
}

function closeCapabilityPicker(){
  capabilityPicker.open = false;
  capabilityPicker.stack = [];
  capabilityPicker.entries = [];
  capabilityPicker.filtered = [];
  capabilityPicker.activeIndex = -1;
  capabilityPicker.triggerIndex = -1;
  const { box } = _capabilityPickerEls();
  box.classList.remove("open");
}

// ── Weighted scrolling ───────────────────────────────────────────────────────────
// Native wheel scrolling in Chromium jumps by whatever raw delta the OS/
// input device reports and stops dead the instant the wheel does -- there's
// no give to it. This intercepts wheel events on scrollable panes and
// replaces that jump with a momentum glide: each tick adds to a velocity
// that decays a little every frame (friction) rather than being applied
// and forgotten, so the pane keeps drifting for a few frames after the
// wheel stops instead of snapping still. That gradual decay -- plus a
// capped top speed so a hard flick doesn't rocket past the content -- is
// what reads as "weight" rather than a toy/instant scroll.
//
// #chat-scroll and #shell-terminal opt out (see WEIGHTED_SCROLL_EXCLUDE):
// both are driven by streaming output that sets scrollTop directly on a
// tight loop to auto-follow new tokens, and layering momentum on top of
// that would fight the auto-follow and make it lag behind incoming text
// instead of tracking it accurately -- exactly the tradeoff called out for
// scroll-behavior:smooth above. They keep plain, instant native scrolling.
const WEIGHTED_SCROLL_FRICTION = 0.95;      // per-frame velocity decay -- higher = drifts longer after release
const WEIGHTED_SCROLL_SENSITIVITY = 0.55;   // fraction of each wheel tick's delta fed into velocity
const WEIGHTED_SCROLL_MAX_VELOCITY = 90;    // px/frame cap, so a hard flick still decelerates rather than teleporting
const WEIGHTED_SCROLL_MIN_VELOCITY = 0.15;  // px/frame below which momentum is considered settled and stops
const WEIGHTED_SCROLL_EXCLUDE = new Set(["chat-scroll","shell-terminal"]);
const _weightedScrollState = new WeakMap();  // scrollable element -> {velocity, raf}

function findWeightedScrollTarget(node){
  let el = node;
  while (el && el !== document.body){
    if (el.nodeType === 1){
      if (WEIGHTED_SCROLL_EXCLUDE.has(el.id)) return null;
      const cs = getComputedStyle(el);
      const canScrollY = (cs.overflowY === "auto" || cs.overflowY === "scroll") && el.scrollHeight > el.clientHeight + 1;
      if (canScrollY) return el;
    }
    el = el.parentElement;
  }
  return null;
}

function stepWeightedScroll(el){
  const st = _weightedScrollState.get(el);
  if (!st) return;
  el.scrollTop += st.velocity;
  st.velocity *= WEIGHTED_SCROLL_FRICTION;
  // Stop dead at either end instead of letting momentum keep "pushing"
  // against a wall the pane has already reached.
  if (el.scrollTop <= 0 || el.scrollTop >= el.scrollHeight - el.clientHeight){
    st.velocity = 0;
  }
  if (Math.abs(st.velocity) < WEIGHTED_SCROLL_MIN_VELOCITY){
    st.velocity = 0;
    st.raf = null;
    return;
  }
  st.raf = requestAnimationFrame(()=>stepWeightedScroll(el));
}

window.addEventListener("wheel", (e)=>{
  const target = findWeightedScrollTarget(e.target);
  if (!target) return;   // nothing weighted here (or an excluded pane) -- let native scrolling handle it
  e.preventDefault();
  let delta = e.deltaY;
  if (e.deltaMode === 1) delta *= 16;                 // "line" units -> approx px
  else if (e.deltaMode === 2) delta *= target.clientHeight;  // "page" units -> approx px
  let st = _weightedScrollState.get(target);
  if (!st){ st = {velocity:0, raf:null}; _weightedScrollState.set(target, st); }
  st.velocity += delta * WEIGHTED_SCROLL_SENSITIVITY;
  st.velocity = Math.max(-WEIGHTED_SCROLL_MAX_VELOCITY, Math.min(WEIGHTED_SCROLL_MAX_VELOCITY, st.velocity));
  if (!st.raf) st.raf = requestAnimationFrame(()=>stepWeightedScroll(target));
}, {passive:false});

// ── Layout engine ------------------------------------------------------
function targetGeo(){
  const showTool = state.activeTab !== "Chat";
  const showSide = state.sidebarOpen;
  const showRender = state.renderSurfaceOpen;
  // The Flows tab is a node-graph editor that needs real canvas space to
  // be usable, so it's the one tab given a pane larger than the chat
  // panel -- every other tab keeps the normal (smaller-than-chat) split.
  const isFlows = state.activeTab === "Flows" || state.activeTab === "Editor";   // both want a wide pane
  // Render Surface takes the right half of the window as a real pane in
  // this same split, same as any other pane -- chat keeps the left half
  // so the conversation (and the ability to send the model input) stays
  // visible and usable while it's open. Tool/sidebar panes collapse to 0
  // width while it's active (they're also dimmed via body.render-
  // surface-active, see CSS) so nothing fights the surface for space.
  if (state.midumDocOpen)        return {tool:[0,0], chat:[0,38], side:[100,0], render:[100,0], docs:[38,62]};
  if (showRender)                return {tool:[0,0], chat:[0,50], side:[100,0], render:[50,50]};
  if (showTool && showSide)      return isFlows ? {tool:[0,55], chat:[55,25], side:[80,20], render:[100,0]} : {tool:[0,30], chat:[30,50], side:[80,20], render:[100,0]};
  if (showTool && !showSide)     return isFlows ? {tool:[0,70], chat:[70,30], side:[100,0], render:[100,0]} : {tool:[0,40], chat:[40,60], side:[100,0], render:[100,0]};
  if (!showTool && showSide)     return {tool:[0,0],  chat:[0,80],  side:[80,20], render:[100,0]};
  return {tool:[0,0], chat:[0,100], side:[100,0], render:[100,0]};
}

function applyLayout(){
  const g = targetGeo();
  const toolWrap = document.getElementById("tool-pane-wrap");
  const chatWrap = document.getElementById("chat-pane-wrap");
  const sideWrap = document.getElementById("sidebar-pane-wrap");
  const renderWrap = document.getElementById("render-surface-pane-wrap");

  toolWrap.style.left = g.tool[0]+"%"; toolWrap.style.width = g.tool[1]+"%";
  chatWrap.style.left = g.chat[0]+"%"; chatWrap.style.width = g.chat[1]+"%";
  sideWrap.style.left = g.side[0]+"%"; sideWrap.style.width = g.side[1]+"%";
  if (renderWrap){ renderWrap.style.left = g.render[0]+"%"; renderWrap.style.width = g.render[1]+"%"; }

  toolWrap.classList.toggle("pane-hidden", g.tool[1] === 0);
  sideWrap.classList.toggle("pane-hidden", g.side[1] === 0);
  if (renderWrap) renderWrap.classList.toggle("pane-hidden", g.render[1] === 0);
  const docsWrap = document.getElementById("midumdocs-pane-wrap");
  if (docsWrap){ const gd = g.docs || [100,0]; docsWrap.style.left = gd[0]+"%"; docsWrap.style.width = gd[1]+"%"; docsWrap.classList.toggle("pane-hidden", gd[1] === 0); }
}

function switchTab(name){
  if (name === state.activeTab) return;
  state.activeTab = name;
  document.querySelectorAll(".tab-btn").forEach(b=>{
    b.classList.toggle("active", b.dataset.name === name);
  });
  document.querySelectorAll('#settings-panel-toggle [data-panel]').forEach(b=>{
    b.classList.toggle("active", b.dataset.panel === name);
  });
  if (name !== "Chat") showToolPane(name);
  applyLayout();
  positionTabHighlight();
}

function positionTabHighlight(){
  const bar = document.getElementById("tabbar");
  const hl  = document.getElementById("tab-highlight");
  if (!bar || !hl) return;
  const activeBtn = bar.querySelector(".tab-btn.active");
  if (!activeBtn){
    // Active pane (e.g. Log/Parameters, opened from Settings) has no
    // corresponding tab-btn anymore -- collapse the pill instead of
    // leaving it stuck on whatever tab was previously active.
    hl.style.width = "0px";
    return;
  }
  hl.style.left  = activeBtn.offsetLeft + "px";
  hl.style.width = activeBtn.offsetWidth + "px";
}

function toggleSidebar(){
  state.sidebarOpen = !state.sidebarOpen;
  document.getElementById("sidebar-toggle").classList.toggle("active", state.sidebarOpen);
  if (state.sidebarOpen) refreshHistory();
  applyLayout();
}

// ── Render Surface ---------------------------------------------------------
// A model-controlled HTML/CSS/JS panel occupying the right half of the
// window as a real pane (see #render-surface-pane-wrap CSS / targetGeo /
// applyLayout). The markup the model gives us via write_render_code/
// edit_render_code is pushed straight into a sandboxed <iframe>'s srcdoc
// -- it lives only in this tab's memory for as long as the panel is open,
// and is never written to the user's disk.
// Opening it also flips on body.render-surface-active, which the CSS above
// uses to grey out every other openable panel (top tabs, sidebar's Log/
// Parameters buttons) EXCEPT the sidebar toggle itself, which stays live.
// Auto-injected into every Render Surface (write/edit/reopen/load all
// funnel through openRenderSurface/updateRenderSurfaceCode below), giving
// the surface's own JS a `window.midum.call(name, args)` function that
// behaves like a normal async function -- awaiting it returns the tool's
// result, so it can be assigned straight to a variable. Under the hood it
// posts a {__midum_call:true, id, tool, args} message up to the parent
// (this document), which invokes render_surface_call_tool in Python (see
// gui/app.py's Api._call_any_tool_from_surface) and posts the result back
// in as {__midum_call_result:true, id, result|error} -- matched back to
// the right pending Promise by `id`. This is purely additive: the model's
// own HTML/JS (from write_render_code/edit_render_code) is untouched --
// this script is prepended only to what's actually fed into the iframe's
// srcdoc, never to the stored code itself, so edit_render_code's exact-
// substring matching keeps working against exactly what the model wrote.
const MIDUM_BRIDGE_SCRIPT = `<scr` + `ipt>
(function(){
  var __midumCallId = 0;
  var __midumPending = {};
  window.addEventListener("message", function(e){
    var d = e.data;
    if (d && d.__midum_call_result === true){
      var p = __midumPending[d.id];
      if (p){
        delete __midumPending[d.id];
        if (d.error) p.reject(new Error(d.error)); else p.resolve(d.result);
      }
    }
  });
  window.midum = window.midum || {};
  window.midum.call = function(tool, args){
    return new Promise(function(resolve, reject){
      var id = "c" + (++__midumCallId) + "_" + Date.now();
      __midumPending[id] = {resolve: resolve, reject: reject};
      parent.postMessage({__midum_call: true, id: id, tool: tool, args: args || {}}, "*");
    });
  };
})();
<\/scr` + `ipt>`;

// ── MidumDocs ----------------------------------------------------------------------
// LaTeX documents. The editor runs in an iframe served from a localhost server (see midumdocs.py) because the
// SwiftLaTeX engine needs a real http origin; it talks only to this page via postMessage, and this page relays to
// Python (Api.midumdocs_*). The iframe is kept loaded after closing so reopening is instant and the engine stays warm.
let _mdReady = false, _mdPendingInit = null, _mdMode = "split", _mdDoc = null, _mdThemeTimer = null;
const _MD_THEME_VARS = ["--bg","--panel","--surface","--surface2","--border2","--accent","--accent-faint","--text","--subtext","--muted","--red","--green","--yellow"];
function _mdTheme(){ const cs = getComputedStyle(document.documentElement), v = {}; _MD_THEME_VARS.forEach(k=>{ const x = cs.getPropertyValue(k).trim(); if (x) v[k] = x; }); return v; }
function _mdPost(msg){ const f = document.getElementById("midumdocs-frame"); if (f && f.contentWindow) f.contentWindow.postMessage(msg, "*"); }
function _mdBusy(v){ _mdPost({md:"busy", value:!!v}); }
function _mdStatus(m){
  const el = document.getElementById("midumdocs-status"); if (!el) return;
  el.className = "md-status " + (m.kind === "ok" ? "ok" : m.kind === "error" ? "error" : "compiling");
  document.getElementById("midumdocs-status-text").textContent = m.text || "";
}
function _mdSendInit(){ if (!_mdPendingInit) return; _mdPost({md:"init", doc:_mdPendingInit, theme:_mdTheme(), mode:_mdMode}); _mdPendingInit = null; }
async function openMidumDoc(doc){
  if (state.renderSurfaceOpen) closeRenderSurfaceUI();
  _mdDoc = doc; _mdPendingInit = doc;
  document.getElementById("midumdocs-title").textContent = doc.title || doc.name || "MidumDoc";
  const frame = document.getElementById("midumdocs-frame");
  if (!frame.getAttribute("src")){
    const info = await api("midumdocs_server_info");
    if (!info || !info.ok){ await showConfirm((info && info.error) || "Could not start MidumDocs.", "MidumDocs"); return; }
    _mdReady = false; frame.setAttribute("src", info.url);
  }
  state.midumDocOpen = true;
  document.body.classList.add("midumdocs-active");
  applyLayout();
  if (_mdReady) _mdSendInit();
}
function updateMidumDoc(payload){ if (state.midumDocOpen && _mdDoc && payload && payload.id === _mdDoc.id) _mdPost(Object.assign({md:"doc_changed"}, payload)); }
function closeMidumDocUI(){
  if (!state.midumDocOpen) return;
  state.midumDocOpen = false;
  document.body.classList.remove("midumdocs-active");
  applyLayout();
}
window.addEventListener("message", (e)=>{
  const f = document.getElementById("midumdocs-frame");
  if (!f || e.source !== f.contentWindow) return;
  const m = e.data; if (!m || typeof m.md !== "string") return;
  if (m.md === "ready"){ _mdReady = true; _mdSendInit(); }
  else if (m.md === "state"){ api("midumdocs_state", m.state); }
  else if (m.md === "save"){ api("midumdocs_save", m.id, m.content, m.base_version, m.mode || "auto").then(r=>_mdPost({md:"rpc_result", rid:m.rid, result:r})); }
  else if (m.md === "prompt"){ api("midumdocs_prompt", m.text, m.ctx).then(r=>{ if (r && r.ok === false) _mdBusy(false); }); }
  else if (m.md === "abort"){ api("abort"); }
  else if (m.md === "undo"){ api("midumdocs_revert_last", m.id); }
  else if (m.md === "make_copy"){ api("midumdocs_make_copy", m.id); }
  else if (m.md === "export"){ api("midumdocs_export", m.kind, m.name, m.kind === "pdf" ? m.b64 : m.text); }
  else if (m.md === "status"){ _mdStatus(m); }
});
new MutationObserver(()=>{ clearTimeout(_mdThemeTimer); _mdThemeTimer = setTimeout(()=>{ if (state.midumDocOpen) _mdPost({md:"theme", vars:_mdTheme()}); }, 250); })
  .observe(document.documentElement, {attributes:true, attributeFilter:["class","style","data-theme"]});

function openRenderSurface(code, title){
  if (state.midumDocOpen) closeMidumDocUI();
  const frame   = document.getElementById("render-surface-frame");
  const titleEl = document.getElementById("render-surface-title");
  titleEl.textContent = title && title.trim() ? title : "Render Surface";
  frame.srcdoc = MIDUM_BRIDGE_SCRIPT + (code || "");
  state.renderSurfaceOpen = true;
  document.body.classList.add("render-surface-active");
  applyLayout();
}

function updateRenderSurfaceCode(code){
  if (!state.renderSurfaceOpen) return; // nothing open to update
  document.getElementById("render-surface-frame").srcdoc = MIDUM_BRIDGE_SCRIPT + (code || "");
}

function closeRenderSurfaceUI(){
  state.renderSurfaceOpen = false;
  document.body.classList.remove("render-surface-active");
  applyLayout();
  // Drop the iframe's document entirely (rather than just hiding it) so
  // any timers/listeners the model's script set up stop running and its
  // markup isn't just sitting there invisibly in memory.
  document.getElementById("render-surface-frame").srcdoc = "";
}

// The sandboxed iframe can't call pywebview.api directly (it's an opaque
// srcdoc origin, deliberately not allow-same-origin) -- the only channel
// out of it is postMessage to its parent. This is what lets the model's
// surface accept input of ANY kind (a form, a button with a payload, a
// slider, freeform text, a custom widget's selection, etc): the surface's
// own script calls `parent.postMessage(yourData, "*")`, this listener
// catches it, confirms it actually came from the live render-surface
// iframe (not some other frame/extension), and hands it to Python via
// render_surface_submit -- which folds it into the conversation as a
// normal user turn so the model sees it and can act on it. Ignored
// whenever the surface isn't open, so stray messages from an iframe
// that's mid-teardown (srcdoc just cleared) can't leak through.
window.addEventListener("message", (e)=>{
  if (!state.renderSurfaceOpen) return;
  const frame = document.getElementById("render-surface-frame");
  if (!frame || !frame.contentWindow || e.source !== frame.contentWindow) return;
  const d = e.data;
  // Tool/MCP/Flow calls from window.midum.call() (see MIDUM_BRIDGE_SCRIPT
  // above) are tagged with __midum_call and handled separately from free-
  // form input -- they're routed to render_surface_call_tool and answered
  // back into the SAME iframe (not folded into the chat as a user turn),
  // so the surface's script can await the result like a normal function
  // call, whether it fired automatically or from a user interaction.
  if (d && d.__midum_call === true){
    api("render_surface_call_tool", d.tool, d.args || {})
      .then((result)=>{ frame.contentWindow.postMessage({__midum_call_result: true, id: d.id, result: result}, "*"); })
      .catch((err)=>{ frame.contentWindow.postMessage({__midum_call_result: true, id: d.id, error: String(err && err.message || err)}, "*"); });
    return;
  }
  api("render_surface_submit", d);
});


// ── Top bar build --------------------------------------------------------
function buildTabbar(){
  const bar = document.getElementById("tabbar");
  TABS.forEach(([name, icon])=>{
    const b = document.createElement("button");
    b.className = "tab-btn" + (name === "Chat" ? " active" : "");
    b.dataset.name = name;
    b.innerHTML = `<span>${iconHtml(icon)}</span><span>${name}</span>`;
    b.onclick = ()=>switchTab(name);
    bar.appendChild(b);
  });
  requestAnimationFrame(positionTabHighlight);
  window.addEventListener("resize", positionTabHighlight);
}

// ── Conversation tabs (titlebar) -------------------------------------------
// Each tab is an independent AI conversation living fully in RAM (see
// conversation_tabs.py) -- switching is instant, no disk I/O. A tab's
// title is ALWAYS derived from its own chat (first message), never
// editable, so there's no rename affordance here at all.
// Keeps the native Windows hit-test hook (_install_win_snap_hook, Python)
// in sync with #conv-tab-controls' actual on-screen rect -- see
// Api.set_titlebar_nodrag_zones for why this is necessary at all (the
// hook resolves drag-vs-click at the OS level, entirely bypassing
// -webkit-app-region CSS, for anything in the titlebar row). Called after
// every tab-strip rebuild (width changes as tabs open/close/rename) and on
// window resize; harmless no-op on platforms without the native hook.
function reportTitlebarNodragZones(){
  const el = document.getElementById("conv-tab-controls");
  if (!el) return;
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return;
  api("set_titlebar_nodrag_zones", [{ left: r.left, top: r.top, right: r.right, bottom: r.bottom }]);
}

async function buildConversationTabs(){
  const bar = document.getElementById("conv-tabbar");
  if (!bar) return;
  let tabs = [];
  try{ tabs = await api("list_conversation_tabs"); }catch(e){ return; }
  bar.querySelectorAll(".conv-tab").forEach(e=>e.remove());
  tabs.forEach(t=>{
    const el = document.createElement("div");
    el.className = "conv-tab" + (t.active ? " active" : "") + (t.kind === "agent" ? " agent" : "");
    el.title = t.title;
    el.innerHTML = `<span class="conv-tab-title">${(t.kind === "agent" ? icon("bot") + " " : "")}${escapeHtml(t.title)}</span>` +
      (t.kind === "agent" ? "" : `<button class="conv-tab-close" title="Close tab">${icon("x")}</button>`);
    el.onclick = ()=>{ if (t.active) return; switchConvTab(t.id); };
    const closeBtn = el.querySelector(".conv-tab-close");
    if (closeBtn){
      closeBtn.onclick = (e)=>{ e.stopPropagation(); closeConvTab(t.id); };
    }
    bar.appendChild(el);
  });
  positionConvTabHighlight();
  requestAnimationFrame(reportTitlebarNodragZones);
}

// Sliding accent pill behind the active conv-tab -- same idea as
// positionTabHighlight() for the big subsystem #tabbar, just measuring
// #conv-tabbar's own children instead.
function positionConvTabHighlight(){
  const bar = document.getElementById("conv-tabbar");
  const hl  = document.getElementById("conv-tab-highlight");
  if (!bar || !hl) return;
  const activeBtn = bar.querySelector(".conv-tab.active");
  if (!activeBtn){
    hl.style.width = "0px";
    return;
  }
  hl.style.left  = activeBtn.offsetLeft + "px";
  hl.style.width = activeBtn.offsetWidth + "px";
}

async function switchConvTab(tabId){
  const r = await api("switch_conversation_tab", tabId);
  if (!r.ok){ showAlert(r.error, "Error"); return; }
  clearChat();
  renderHistoryWindow(r.display || []);
  applyKbState(r.kb_state);
  setViewingAgentTab(r.readonly ? (r.agent_name || "agent") : null);
  await buildConversationTabs();
  refreshTitlebarInfo();
}

// Toggles the read-only "previewing a sub-agent's tab" state: disables the
// message box (agent tabs are look-only -- see Api.switch_conversation_tab)
// and remembers which agent, if any, is on screen so a LIVE
// "agent_tool_call" event for that same agent (see appendAgentToolRow) can
// append straight into the chat pane instead of only showing up the next
// time the tab is clicked.
function setViewingAgentTab(agentName){
  state.viewingAgentTab = agentName || null;
  const input = document.getElementById("msg-input");
  const btn = document.getElementById("send-btn");
  if (input){
    input.disabled = !!agentName;
    input.placeholder = agentName ? `Viewing "${agentName}" \u2014 read-only` : "Message Midum...";
  }
  if (btn) btn.disabled = !!agentName;
}

async function newConvTab(){
  const r = await api("new_conversation_tab");
  if (!r.ok){ showAlert(r.error, "Error"); return; }
  clearChat();
  applyKbState(r.kb_state);
  setViewingAgentTab(null);
  await buildConversationTabs();
  refreshTitlebarInfo();
}

async function closeConvTab(tabId){
  const r = await api("close_conversation_tab", tabId);
  if (!r.ok){ showAlert(r.error, "Error"); return; }
  if (r.display !== undefined){
    // The closed tab was the active one -- a different tab (or a freshly
    // reset version of the same tab, if it was the last one open) is now
    // showing, so the chat pane needs to catch up.
    clearChat();
    renderHistoryWindow(r.display || []);
    applyKbState(r.kb_state);
    setViewingAgentTab(null);
  }
  await buildConversationTabs();
  refreshTitlebarInfo();
}

// ── Tab groups (titlebar dropdown) -----------------------------------------
function closeConvTabGroupMenu(){
  const menu = document.getElementById("conv-tab-group-menu");
  if (menu) menu.classList.remove("open");
  document.removeEventListener("click", _onConvTabGroupOutsideClick, true);
}
function _onConvTabGroupOutsideClick(e){
  const wrap = document.getElementById("conv-tab-group-wrap");
  if (wrap && !wrap.contains(e.target)) closeConvTabGroupMenu();
}

async function toggleConvTabGroupMenu(){
  const menu = document.getElementById("conv-tab-group-menu");
  if (!menu) return;
  if (menu.classList.contains("open")){ closeConvTabGroupMenu(); return; }
  await buildConvTabGroupMenu();
  menu.classList.add("open");
  setTimeout(()=>document.addEventListener("click", _onConvTabGroupOutsideClick, true), 0);
}

async function buildConvTabGroupMenu(){
  const menu = document.getElementById("conv-tab-group-menu");
  if (!menu) return;
  let groups = [];
  try{ groups = await api("list_conversation_tab_groups"); }catch(e){}
  menu.innerHTML = "";

  const newItem = document.createElement("div");
  newItem.className = "conv-tab-group-item new";
  newItem.innerHTML = `<span>+ New Group</span>`;
  newItem.onclick = async ()=>{
    closeConvTabGroupMenu();
    const name = await showPrompt("Save all currently open tabs as a group named:", "New Tab Group");
    if (name === null) return;
    const trimmed = name.trim();
    if (!trimmed) return;
    const r = await api("save_conversation_tab_group", trimmed);
    if (!r.ok){ showAlert(r.error, "Error"); return; }
  };
  menu.appendChild(newItem);

  if (groups.length){
    const sep = document.createElement("div");
    sep.className = "conv-tab-group-sep";
    menu.appendChild(sep);
  }

  groups.forEach(g=>{
    const item = document.createElement("div");
    item.className = "conv-tab-group-item";
    item.innerHTML = `<span>${escapeHtml(g.name)}</span><span class="count">${g.tab_count} tab${g.tab_count === 1 ? "" : "s"}</span>`;
    item.onclick = async ()=>{
      closeConvTabGroupMenu();
      const r = await api("open_conversation_tab_group", g.name);
      if (!r.ok){ showAlert(r.error, "Error"); return; }
      clearChat();
      renderHistoryWindow(r.display || []);
      applyKbState(r.kb_state);
      await buildConversationTabs();
      refreshTitlebarInfo();
    };
    menu.appendChild(item);
  });
}


// ── Chat rendering --------------------------------------------------------
function escapeHtml(s){
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

// Renders a USER message's bubble: escapes HTML, applies a small subset of
// markdown (inline code, bold, italic -- deliberately not the full
// renderInline() pipeline used for Midum's own replies, since a typed
// prompt is plain input, not a document), and highlights any "/capability"
// token -- a "/" at the very start of the message or right after
// whitespace, immediately followed by a name with no spaces -- the same
// trigger shape the "/" picker itself uses (see checkCapabilityPickerTrigger)
// and the explicit capability-request convention described to the model in
// the system prompt (see system_prompt.py's "/" CAPABILITY REQUESTS section).
function renderUserContent(text){
  let t = escapeHtml(text);
  t = t.replace(/`([^`]+)`/g, (m,c)=>`<code class="inline-code">${c}</code>`);
  t = t.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  t = t.replace(/(^|[^*])\*([^*]+)\*/g, "$1<i>$2</i>");
  t = t.replace(/(^|\s)\/([A-Za-z0-9_\-.]+)/g, (m, pre, name)=>`${pre}<span class="capability-tag" title="Capability request">/${name}</span>`);
  return t;
}

function renderInline(text){
  let t = escapeHtml(text);
  // LaTeX math ($$...$$, \[...\], \(...\), $...$) is pulled out into
  // \u0000MATHSPAN-tagged placeholders BEFORE the table parser and the
  // bold/italic/etc regexes below run. This has to happen before table
  // parsing (not just before emphasis) because _splitTableRow() splits
  // cells on raw, unescaped "|" -- and plenty of real TeX contains bare
  // "|" (absolute value like $|x|$, \begin{vmatrix}...\end{vmatrix},
  // set-builder notation, etc). Left in place, that math would get
  // sliced apart as if it were extra table columns. Stashing the
  // rendered <span class="math-tex"> HTML behind an opaque placeholder
  // (no "|", "*", "_", or "$" left in the text the table/emphasis
  // regexes actually see) keeps it intact through both passes; it's
  // swapped back in at the very end, once table/code/emphasis handling
  // is safely behind us.
  const _mathSpans = [];
  t = extractMath(t, _mathSpans);
  t = renderTablesInText(t);
  // Fenced code blocks are pulled out into placeholders *before* any of the
  // markdown regexes below run -- same trick extractMath() uses for TeX
  // spans, and for the same reason: a code block's own newlines have to
  // stay real "\n" characters (rendered as line breaks purely via the
  // block's white-space:pre CSS), not get swept up by the final
  // "\n -> <br>" pass a few lines down. If they were converted to <br>,
  // reading the block back out via .textContent (done both for the copy
  // button and for the Pygments highlight call) would collapse the whole
  // block onto a single line, since <br> contributes no characters to
  // .textContent.
  const _codeBlocks = [];
  t = t.replace(/```([\w_]*)\n([\s\S]*?)```/g, (m,lang,body)=>{
    const html = `<div class="code-block-wrap"><button class="code-copy-btn" title="Copy code">${icon("copy")}</button><pre class="code-block" data-lang="${lang}">${body}</pre></div>`;
    _codeBlocks.push(html);
    return `\u0000CODEBLOCK${_codeBlocks.length - 1}\u0000`;
  });
  t = t.replace(/`([^`]+)`/g, (m,c)=>`<code class="inline-code">${c}</code>`);
  t = t.replace(/\*\*\*(.+?)\*\*\*/g, "<b><i>$1</i></b>");
  t = t.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  t = t.replace(/(^|[^*])\*([^*]+)\*/g, "$1<i>$2</i>");
  t = t.replace(/~~(.+?)~~/g, "<s>$1</s>");
  t = t.replace(/^###### (.*)$/gm, "<h6>$1</h6>");
  t = t.replace(/^##### (.*)$/gm, "<h5>$1</h5>");
  t = t.replace(/^#### (.*)$/gm, "<h4>$1</h4>");
  t = t.replace(/^### (.*)$/gm, "<h3>$1</h3>");
  t = t.replace(/^## (.*)$/gm, "<h2>$1</h2>");
  t = t.replace(/^# (.*)$/gm, "<h1>$1</h1>");
  // Horizontal rule: a line that's ONLY 3+ hyphens/asterisks/underscores
  // (optionally spaced out, e.g. "- - -"), not a table separator row
  // (those always contain at least one "|" and are already consumed by
  // renderTablesInText before this point runs).
  t = t.replace(/^ {0,3}(?:-[ \t]*){3,}$/gm, "<hr>");
  t = t.replace(/^ {0,3}(?:\*[ \t]*){3,}$/gm, "<hr>");
  t = t.replace(/^ {0,3}(?:_[ \t]*){3,}$/gm, "<hr>");
  // Bullet list items: a line starting with "- " (single hyphen, not the
  // 3+ hyphen horizontal-rule pattern handled just above) becomes a round
  // bullet marker instead of a raw hyphen. Wrapped in a block-level span
  // so .bullet-line's CSS margin can space consecutive bullets out --
  // the newline right after each one still becomes a <br> in the final
  // \n -> <br> pass below, stacking with that margin for extra spacing.
  t = t.replace(/^-[ \t]+(.+)$/gm, '<span class="bullet-line">\u2022 $1</span>');
  t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
  t = t.replace(/\n/g, "<br>");
  // Swap the real code-block HTML back in now that the \n -> <br> pass is
  // safely behind us.
  t = t.replace(/\u0000CODEBLOCK(\d+)\u0000/g, (m, idx)=>_codeBlocks[Number(idx)]);
  // Swap the real math-span HTML back in last of all -- restoring it any
  // earlier would put raw "|"/"*"/"_"/"$" TeX source back in front of the
  // table parser and emphasis regexes above, exactly what the placeholder
  // swap at the top of this function exists to avoid.
  t = t.replace(/\u0000MATHSPAN(\d+)\u0000/g, (m, idx)=>_mathSpans[Number(idx)]);
  return t;
}

// ── Markdown table rendering (GFM-style pipe tables) ----------------------
// Operates on already-HTML-escaped text (so pipe/dash chars are still
// literal), BEFORE code-fence extraction and the final \n -> <br> pass,
// so line-based table detection still sees real newlines. Emphasis/bold
// regexes run afterwards and will still reach into cell text normally
// since the table is just more inline HTML at that point.
function _splitTableRow(line){
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  // Split on unescaped pipes only (a cell can contain \| for a literal pipe)
  const cells = [];
  let cur = "", esc = false;
  for (let i = 0; i < s.length; i++){
    const ch = s[i];
    if (esc){ cur += ch; esc = false; continue; }
    if (ch === "\\"){ esc = true; continue; }
    if (ch === "|"){ cells.push(cur); cur = ""; continue; }
    cur += ch;
  }
  cells.push(cur);
  return cells.map(c=>c.trim());
}

const _TABLE_ROW_RE = /^\s*\|?.*\|.*\|?\s*$/;
const _TABLE_SEP_RE  = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function _renderTableBlock(headerLine, sepLine, bodyLines){
  const header = _splitTableRow(headerLine);
  const aligns = _splitTableRow(sepLine).map(a=>{
    const left = a.startsWith(":"), right = a.endsWith(":");
    if (left && right) return "center";
    if (right) return "right";
    if (left) return "left";
    return "";
  });
  const rows = bodyLines.map(_splitTableRow);

  let html = '<div class="md-table-wrap"><table class="md-table"><thead><tr>';
  header.forEach((h,i)=>{
    const align = aligns[i] ? ` style="text-align:${aligns[i]}"` : "";
    html += `<th${align}>${h}</th>`;
  });
  html += '</tr></thead><tbody>';
  rows.forEach(r=>{
    html += '<tr>';
    header.forEach((_, i)=>{
      const align = aligns[i] ? ` style="text-align:${aligns[i]}"` : "";
      html += `<td${align}>${r[i] !== undefined ? r[i] : ""}</td>`;
    });
    html += '</tr>';
  });
  html += '</tbody></table></div>';
  return html;
}

function renderTablesInText(text){
  const lines = text.split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length){
    if (_TABLE_ROW_RE.test(lines[i]) && lines[i].includes("|") &&
        i + 1 < lines.length && _TABLE_SEP_RE.test(lines[i+1]) && lines[i+1].includes("-") && lines[i+1].includes("|")){
      const headerLine = lines[i];
      const sepLine = lines[i+1];
      let j = i + 2;
      const bodyLines = [];
      while (j < lines.length && lines[j].trim() !== "" && lines[j].includes("|")){
        bodyLines.push(lines[j]); j++;
      }
      out.push(_renderTableBlock(headerLine, sepLine, bodyLines));
      i = j;
    } else {
      out.push(lines[i]);
      i++;
    }
  }
  return out.join("\n");
}

// ── Flowchart rendering (```flowchart_json``` blocks) --------------------
function fcWrapText(text, maxChars){
  const words = String(text == null ? "" : text).split(/\s+/);
  const lines = [];
  let cur = "";
  words.forEach(w=>{
    if ((cur + " " + w).trim().length > maxChars && cur){
      lines.push(cur); cur = w;
    } else {
      cur = (cur ? cur + " " : "") + w;
    }
  });
  if (cur) lines.push(cur);
  return lines.slice(0, 4);
}

function renderFlowchartSVG(data){
  try {
    const nodes = data.nodes || [];
    if (!nodes.length) return null;
    const byId = {}; nodes.forEach(n=>byId[n.id]=n);

    // Predecessor map, used to layer nodes top-to-bottom (level = 1 + max
    // predecessor level), like a simplified Sugiyama layering.
    const preds = {}; nodes.forEach(n=>preds[n.id]=[]);
    nodes.forEach(n=>{
      (n.next||[]).forEach(e=>{
        const to = (typeof e === "string") ? e : e.to;
        if (to && byId[to]) preds[to].push(n.id);
      });
    });

    const level = {};
    nodes.forEach(n=>{ level[n.id] = (n.type === "start") ? 0 : null; });
    if (!nodes.some(n=>n.type==="start") && nodes.length) level[nodes[0].id] = 0;

    let changed = true, iter = 0;
    while (changed && iter < nodes.length + 2){
      changed = false; iter++;
      nodes.forEach(n=>{
        const ps = preds[n.id];
        if (ps.length){
          let maxP = -1;
          ps.forEach(p=>{ if (level[p] != null) maxP = Math.max(maxP, level[p]); });
          if (maxP >= 0){
            const newLevel = maxP + 1;
            if (level[n.id] == null || newLevel > level[n.id]){
              level[n.id] = newLevel; changed = true;
            }
          }
        }
      });
    }
    let maxLevel = 0;
    nodes.forEach(n=>{ if (level[n.id] != null) maxLevel = Math.max(maxLevel, level[n.id]); });
    nodes.forEach(n=>{ if (level[n.id] == null) level[n.id] = maxLevel + 1; });

    const byLevel = {};
    nodes.forEach(n=>{ (byLevel[level[n.id]] = byLevel[level[n.id]] || []).push(n.id); });
    const levels = Object.keys(byLevel).map(Number).sort((a,b)=>a-b);

    const NODE_W = 190, NODE_H = 56, H_GAP = 50, V_GAP = 64, PAD = 30;
    const rowWidths = levels.map(lv => byLevel[lv].length * NODE_W + (byLevel[lv].length - 1) * H_GAP);
    const canvasW = Math.max(...rowWidths, NODE_W) + PAD * 2;
    const canvasH = levels.length * (NODE_H + V_GAP) + PAD * 2;

    const pos = {};
    levels.forEach(lv=>{
      const ids = byLevel[lv];
      const rowW = ids.length * NODE_W + (ids.length - 1) * H_GAP;
      const startX = (canvasW - rowW) / 2;
      ids.forEach((id, i)=>{
        pos[id] = { x: startX + i * (NODE_W + H_GAP), y: PAD + lv * (NODE_H + V_GAP) };
      });
    });

    let svg = `<svg viewBox="0 0 ${canvasW} ${canvasH}" xmlns="http://www.w3.org/2000/svg" width="100%" style="min-width:${Math.min(canvasW, 900)}px;">`;
    svg += `<defs><marker id="fc-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--muted)"/></marker></defs>`;

    // Edges first, so nodes render on top of the lines.
    nodes.forEach(n=>{
      const from = pos[n.id];
      if (!from) return;
      (n.next || []).forEach(e=>{
        const to  = (typeof e === "string") ? e : e.to;
        const lbl = (typeof e === "object" && e && e.label) ? e.label : "";
        const target = pos[to];
        if (!target) return;
        const x1 = from.x + NODE_W/2, y1 = from.y + NODE_H;
        const x2 = target.x + NODE_W/2, y2 = target.y;
        let path;
        if (Math.abs(target.y - from.y) < 1){
          const midY = from.y - 30;
          path = `M${x1},${from.y+NODE_H/2} C${x1-40},${midY} ${x2+40},${midY} ${x2},${target.y+NODE_H/2}`;
        } else if (target.y < from.y) {
          const side = (x1 <= x2) ? -70 : 70;
          path = `M${x1},${y1} C${x1+side},${(y1+y2)/2} ${x2+side},${(y1+y2)/2} ${x2},${y2}`;
        } else {
          const midY = (y1 + y2) / 2;
          path = `M${x1},${y1} C${x1},${midY} ${x2},${midY} ${x2},${y2}`;
        }
        svg += `<path class="fc-edge" d="${path}" marker-end="url(#fc-arrow)"/>`;
        if (lbl){
          const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
          const safe = escapeHtml(lbl);
          const w = Math.max(30, safe.length * 6 + 10);
          svg += `<rect x="${mx-w/2}" y="${my-9}" width="${w}" height="16" rx="8" fill="var(--panel)" stroke="var(--border2)"/>`;
          svg += `<text class="fc-edge-label" x="${mx}" y="${my+3}" text-anchor="middle">${safe}</text>`;
        }
      });
    });

    // Nodes
    nodes.forEach(n=>{
      const p = pos[n.id];
      if (!p) return;
      const cx = p.x + NODE_W/2, cy = p.y + NODE_H/2;
      const lines = fcWrapText(n.label || n.id, 24);
      const type = (n.type || "process");
      let shape;
      if (type === "decision"){
        const hw = NODE_W/2, hh = NODE_H/2 + 8;
        shape = `<polygon class="fc-node-decision" points="${cx},${cy-hh} ${cx+hw},${cy} ${cx},${cy+hh} ${cx-hw},${cy}" stroke-width="1.5"/>`;
      } else if (type === "start" || type === "end"){
        shape = `<rect class="fc-node-${type}" x="${p.x}" y="${p.y}" width="${NODE_W}" height="${NODE_H}" rx="${NODE_H/2}" stroke-width="1.5"/>`;
      } else if (type === "io"){
        shape = `<polygon class="fc-node-io" points="${p.x+16},${p.y} ${p.x+NODE_W},${p.y} ${p.x+NODE_W-16},${p.y+NODE_H} ${p.x},${p.y+NODE_H}" stroke-width="1.5"/>`;
      } else {
        shape = `<rect class="fc-node-process" x="${p.x}" y="${p.y}" width="${NODE_W}" height="${NODE_H}" rx="8" stroke-width="1.5"/>`;
      }
      svg += shape;
      const lineH = 14;
      const startY = cy - ((lines.length - 1) * lineH) / 2 + 4;
      lines.forEach((line, i)=>{
        svg += `<text class="fc-label" x="${cx}" y="${startY + i*lineH}" text-anchor="middle">${escapeHtml(line)}</text>`;
      });
    });

    svg += `</svg>`;
    return `<div class="flowchart-wrap"><div style="font-size:var(--cfs-sm);color:var(--chat-sub);margin-bottom:6px;">${icon("chart")} ${escapeHtml(data.title || "Flowchart")}</div>${svg}</div>`;
  } catch (e) {
    return null;
  }
}

const FLOWCHART_FENCE_RE = /```(flowchart_json|mermaid|image_data_json)\n([\s\S]*?)```/g;

// ── Mermaid rendering (```mermaid``` blocks) ------------------------------
// Loaded lazily from CDN on first use, same lazy-load pattern as Drawflow.
// Mermaid owns its own async rendering pipeline, so renderMidumContent just
// drops a placeholder <div class="mermaid"> with the raw (HTML-escaped, so
// it round-trips through innerHTML intact) source text, and the caller
// (appendRow / appendVoiceTranscript) kicks off renderPendingMermaid()
// afterwards to actually turn every such div into an SVG diagram in place.
const MERMAID_JS = "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js";
let _mermaidLoadPromise = null;
function _loadMermaidOnce(){
  if (_mermaidLoadPromise) return _mermaidLoadPromise;
  _mermaidLoadPromise = new Promise((resolve, reject)=>{
    if (window.mermaid){ resolve(); return; }
    const s = document.createElement("script");
    s.src = MERMAID_JS;
    s.onload = ()=>{
      try {
        window.mermaid.initialize({
          startOnLoad: false,
          theme: "dark",
          securityLevel: "strict",
          fontFamily: "Segoe UI, -apple-system, sans-serif",
        });
      } catch (e) { /* fall through -- render call below will surface any real error */ }
      resolve();
    };
    s.onerror = ()=>reject(new Error("failed to load " + MERMAID_JS));
    document.head.appendChild(s);
  });
  return _mermaidLoadPromise;
}

// Renders every not-yet-rendered `.mermaid` placeholder currently in the
// DOM. Safe to call repeatedly/concurrently -- each element is marked
// data-mermaid-done once handled so a later call skips it. On parse/render
// failure, replaces that one placeholder with a plain error note instead of
// leaving a blank box or throwing across the whole batch.
async function renderPendingMermaid(){
  const pending = Array.from(document.querySelectorAll(".mermaid:not([data-mermaid-done])"));
  if (!pending.length) return;
  try {
    await _loadMermaidOnce();
  } catch (e) {
    pending.forEach(el=>{
      el.dataset.mermaidDone = "1";
      el.outerHTML = `<div class="code-block-wrap"><button class="code-copy-btn" title="Copy code">${icon("copy")}</button><pre class="code-block">${escapeHtml(el.textContent || "")}</pre></div>`;
    });
    return;
  }
  for (const el of pending){
    el.dataset.mermaidDone = "1";
    const src = el.textContent;
    try {
      const id = "mmd-" + Math.random().toString(36).slice(2);
      const { svg } = await window.mermaid.render(id, src);
      el.innerHTML = svg;
    } catch (e) {
      const wrap = el.closest(".flowchart-wrap") || el;
      wrap.outerHTML = `<div class="code-block-wrap"><button class="code-copy-btn" title="Copy code">${icon("copy")}</button><pre class="code-block">${escapeHtml(src)}</pre></div>`;
    }
  }
}

// ── LaTeX math rendering (KaTeX) -------------------------------------
// Loaded lazily from CDN on first use, same lazy-load pattern as Mermaid
// and Drawflow. extractMath() (called from renderInline, after the text
// has already been through escapeHtml so &/</> are entities) swaps every
// recognised math span -- $$...$$, \[...\], \(...\), and inline $...$ --
// for a <span class="math-tex"> placeholder holding the raw TeX source as
// its text content, before any of the other inline markdown regexes run
// (bold/italic/etc never get a chance to mangle underscores/asterisks
// inside the TeX). renderPendingMath() (called by the same callers that
// already call renderPendingMermaid()) then actually renders every
// not-yet-rendered placeholder in place via katex.render().
const KATEX_CSS = "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css";
const KATEX_JS  = "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js";
let _katexLoadPromise = null;
function _loadKatexOnce(){
  if (_katexLoadPromise) return _katexLoadPromise;
  _katexLoadPromise = new Promise((resolve, reject)=>{
    if (window.katex){ resolve(); return; }
    if (!document.querySelector(`link[href="${KATEX_CSS}"]`)){
      const l = document.createElement("link");
      l.rel = "stylesheet"; l.href = KATEX_CSS;
      document.head.appendChild(l);
    }
    const s = document.createElement("script");
    s.src = KATEX_JS;
    s.onload = ()=>resolve();
    s.onerror = ()=>reject(new Error("failed to load " + KATEX_JS));
    document.head.appendChild(s);
  });
  return _katexLoadPromise;
}

// Matches, in order: display math ($$...$$ and \[...\]), then inline
// math (\(...\) and $...$). Inline $...$ requires no whitespace right
// after the opening $ or right before the closing $ (so "$5 and $10"
// isn't misread as one giant math span) and stays on a single line.
function extractMath(text, store){
  // Every match is pushed into `store` and replaced with an opaque
  // \u0000MATHSPANn\u0000 token rather than the <span> HTML directly, so
  // that later passes (table splitting, bold/italic, the \n -> <br> pass)
  // see no "|", "*", "_", or "$" left over from the original TeX -- see
  // the comment at the top of renderInline() for why table splitting in
  // particular needs this. Caller restores the real HTML afterwards via
  // store[n].
  const push = (html)=>{ store.push(html); return `\u0000MATHSPAN${store.length - 1}\u0000`; };
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (m, expr)=>
    push(`<span class="math-tex" data-display="1">${expr}</span>`));
  text = text.replace(/\\\[([\s\S]+?)\\\]/g, (m, expr)=>
    push(`<span class="math-tex" data-display="1">${expr}</span>`));
  text = text.replace(/\\\(([\s\S]+?)\\\)/g, (m, expr)=>
    push(`<span class="math-tex" data-display="0">${expr}</span>`));
  text = text.replace(/\$(?!\s)([^$\n]+?)(?<!\s)\$/g, (m, expr)=>
    push(`<span class="math-tex" data-display="0">${expr}</span>`));
  return text;
}

async function renderPendingMath(){
  const pending = Array.from(document.querySelectorAll(".math-tex:not([data-math-done])"));
  if (!pending.length) return;
  try {
    await _loadKatexOnce();
  } catch (e) {
    pending.forEach(el=>{ el.dataset.mathDone = "1"; });
    return;
  }
  pending.forEach(el=>{
    el.dataset.mathDone = "1";
    const tex = el.textContent;
    const displayMode = el.dataset.display === "1";
    try {
      window.katex.render(tex, el, { throwOnError: false, displayMode, output: "html" });
    } catch (e) {
      el.textContent = tex;
    }
  });
}

// Syntax-highlights fenced code blocks via the Pygments bridge on the
// Python side (Api.highlight_code). Same lazy/idempotent pattern as
// renderPendingMermaid/renderPendingMath above: called after every place
// that injects new HTML into the chat, and marks each <pre> as done so a
// later call (e.g. the next streamed chunk) doesn't re-highlight it.
async function renderPendingCodeHighlight(){
  const pending = Array.from(document.querySelectorAll("pre.code-block:not([data-hl-done])"));
  if (!pending.length) return;
  await Promise.all(pending.map(async (pre)=>{
    pre.dataset.hlDone = "1";
    // innerText (not textContent) -- besides matching what the copy button
    // copies, it correctly turns any literal <br> that might end up inside
    // the block into a real newline, whereas textContent would silently
    // drop it and collapse the code onto one line.
    const code = pre.innerText;
    const lang = pre.dataset.lang || "";
    try {
      const html = await api("highlight_code", code, lang);
      if (html) pre.innerHTML = html;
    } catch (e) {
      // Pygments unavailable or highlighting failed -- leave the plain
      // escaped text already in the block, which is a perfectly fine
      // (just uncolored) rendering on its own.
    }
  }));
}

function renderMidumContent(text){
  FLOWCHART_FENCE_RE.lastIndex = 0;
  if (!FLOWCHART_FENCE_RE.test(text)) return renderInline(text);
  FLOWCHART_FENCE_RE.lastIndex = 0;

  let out = "", lastIndex = 0, match;
  while ((match = FLOWCHART_FENCE_RE.exec(text)) !== null){
    const before = text.slice(lastIndex, match.index);
    if (before.trim()) {
      out += renderInline(before);
    }
    const lang = match[1];
    const body = match[2];
    let renderedBlock = null;
    if (lang === 'mermaid') {
      renderedBlock = `<div class="flowchart-wrap"><div class="mermaid">${escapeHtml(body.trim())}</div></div>`;
      out += renderedBlock;
      lastIndex = FLOWCHART_FENCE_RE.lastIndex;
      continue;
    }
    try {
      const payload = JSON.parse(body);
      if (lang === 'flowchart_json') {
        renderedBlock = renderFlowchartSVG(payload);
      } else if (lang === 'image_data_json') {
        // Image gallery: each image gets a hover-revealed save button so it
        // can be downloaded straight from the chat bubble. `title` mirrors
        // the flowchart block's own title field (falls back to `prompt`,
        // then a generic label) instead of only ever reading `prompt`.
        const title = payload.title || payload.prompt || "Generated Image(s)";
        const images = payload.images || [];
        let imagesHtml = images.map((img, i) => {
          const base = (img.filename || `midum_image_${i + 1}`).replace(/\.[^.\/]+$/, '');
          const fname = `${base}.png`;
          return `<div class="img-frame">
            <img src="data:image/png;base64,${img.data_b64}" alt="${escapeHtml(title)}"/>
            <a class="img-save-btn" href="data:image/png;base64,${img.data_b64}" download="${escapeHtml(fname)}" title="Save image">${icon("save")}</a>
          </div>`;
        }).join('');
        renderedBlock = `<div class="flowchart-wrap">
                           <div style="font-size:var(--cfs-sm);color:var(--chat-sub);margin-bottom:6px;">${icon("image")} ${escapeHtml(title)}</div>
                           ${imagesHtml}
                         </div>`;
      }
    } catch (e) {
      // Fallback for malformed JSON
    }

    out += renderedBlock || `<div class="code-block-wrap"><button class="code-copy-btn" title="Copy code">${icon("copy")}</button><pre class="code-block" data-lang="${lang === "mermaid" ? "" : "json"}">${escapeHtml(body)}</pre></div>`;
    lastIndex = FLOWCHART_FENCE_RE.lastIndex;
  }
  const rest = text.slice(lastIndex);
  if (rest.trim()) out += renderInline(rest);
  return out;
}

function chatCol(){ return document.getElementById("chat-col"); }

// ── Windowed rendering for long conversations ─────────────────────────────
// Opening a saved chat used to dump its ENTIRE display log into the DOM in
// one shot -- one element (often several, for tool-call cards) per stored
// row, with zero regard for how many turns the conversation actually had.
// For a long-running chat that's hundreds/thousands of DOM nodes built and
// inserted in a single frame, which is exactly what made scrolling/opening
// long conversations feel laggy.
//
// Instead we group the log into "turns" (one user message plus everything
// that followed it, up to the next user message) and only render the most
// recent HIST_WINDOW turns up front -- scrolled to the bottom, exactly like
// today. Scrolling up near the top of the pane lazily builds and prepends
// the next batch of older turns from the in-memory log; nothing beyond
// what's actually been scrolled into view is ever built. Turns are never
// removed once rendered (in particular the newest/bottom ones), so a live
// reply always appends correctly after whatever's currently shown.
const HIST_WINDOW = 10;    // turns rendered up front when a chat is opened
const HIST_SHIFT   = 10;   // extra older turns pulled in per scroll-up batch

let _histTurns    = [];    // [[ [tag,text], [tag,text], ... ], ...] -- full log, grouped by turn
let _histWinStart = 0;     // index of the oldest turn currently rendered
let _histActive   = false; // true once a loaded chat's history is being windowed

function groupDisplayIntoTurns(display){
  const turns = [];
  let cur = null;
  (display || []).forEach(([tag, text, extra])=>{
    if (tag === "user" || !cur){
      cur = [];
      turns.push(cur);
    }
    cur.push([tag, text, extra]);
  });
  return turns;
}

// Builds one row element from a stored (tag, text) pair for the windowed
// history view. Deliberately does NOT touch any of the live-turn globals
// (_currentMidumRow / _pendingToolRows / setActiveToolDot) -- those only
// make sense for an in-progress live turn. One consequence: a turn whose
// reply was streamed as several say() chunks is shown here as several
// stacked "Midum" bubbles instead of one merged bubble (a small cosmetic
// difference from the live view -- no content is lost).
function buildStaticRow(tag, text, extra){
  const row = document.createElement("div");
  if (tag === "user"){
    row.className = "row user";
    row.innerHTML = `<div class="row-label-row"><div class="row-label">You</div><button class="row-copy-btn" title="Copy message">${icon("copy")}</button></div><div class="bubble user">${attBubbleChips(extra)}${text ? renderUserContent(text) : ""}</div>`;
    wireRowCopyBtn(row);
    attHydrate(row);
  } else if (tag === "midum"){
    row.className = "row midum";
    row.innerHTML = `<div class="row-label-row"><button class="row-copy-btn" title="Copy response">${icon("copy")}</button></div><div class="bubble midum">${renderMidumContent(text)}</div>`;
    wireRowCopyBtn(row);
  } else if (tag === "system"){
    row.className = "row system";
    row.innerHTML = `<div class="bubble">${iconizeHtml(escapeHtml(text))}</div>`;
  } else if (tag === "interjection"){
    row.className = "row interjection";
    row.innerHTML = `<div class="bubble">${escapeHtml(text)}</div>`;
  } else if (tag === "error"){
    row.className = "row error";
    row.innerHTML = `<div class="bubble">${escapeHtml(text)}</div>`;
  } else if (tag === "toolcall"){
    let data;
    try { data = JSON.parse(text); } catch(e){ data = {name: "", args: {}, result: text}; }
    row.className = "row tool tool-row";
    const argsStr = (data.args && Object.keys(data.args).length) ? JSON.stringify(data.args, null, 2) : "(no arguments)";
    row.dataset.toolName = data.name || "";
    row.innerHTML = `
      <div class="tool-line expandable">
        <span class="tool-dot"></span><span class="gear">${icon("wrench")}</span><span>-> Executing: '${escapeHtml(data.name || "")}'</span>
        <span class="chevron">${icon("chevron-right")}</span>
        <button class="row-copy-btn" title="Copy tool call" style="margin-left:6px;">${icon("copy")}</button>
      </div>
      <div class="tool-detail">
        <div class="tool-detail-label">Call</div>
        <pre>${escapeHtml(data.name || "")}(${escapeHtml(argsStr)})</pre>
        <div class="tool-detail-label">Result</div>
        <pre>${escapeHtml(data.result || "(empty)")}</pre>
      </div>`;
    row.querySelector(".tool-line").onclick = (e)=>{
      if (e.target.closest(".row-copy-btn")) return;
      row.classList.toggle("open");
    };
    wireRowCopyBtn(row);
  } else {
    row.className = "row";
    row.innerHTML = `<div class="bubble">${escapeHtml(text)}</div>`;
  }
  return row;
}

function buildTurnBlock(turnIndex){
  const wrap = document.createElement("div");
  wrap.className = "turn-block";
  wrap.dataset.turnIndex = String(turnIndex);
  // Consecutive persisted "toolcall" records fold into ONE "Ran N tools"
  // group (same grouping the live view does) -- any other row (say/reply,
  // system, error, ...) closes it, so the next tool call starts a new group.
  let group = null;
  _histTurns[turnIndex].forEach(([tag, text, extra])=>{
    if (tag === "toolcall"){
      let data;
      try { data = JSON.parse(text); } catch(e){ data = {name: "", args: {}, result: text}; }
      if (data.name === "say") return;
      if (!group){ group = createToolGroup(); wrap.appendChild(group); }
      const tc = buildToolCallPill({name: data.name || "tool"});
      _tcSetData(tc, data.args, data.result);
      _tgAdd(group, tc);
    } else {
      group = null;
      wrap.appendChild(buildStaticRow(tag, text, extra));
    }
  });
  return wrap;
}

// Replaces appendRow's "forEach((tag,text)) => appendRow(...)" replay path.
// Renders only the newest HIST_WINDOW turns and scrolls to the bottom --
// visually identical to today's "open a chat, land at the bottom" behavior,
// just without paying to build every row that came before it.
function renderHistoryWindow(display){
  _histTurns    = groupDisplayIntoTurns(display);
  _histActive   = _histTurns.length > 0;
  const col     = chatCol();
  col.innerHTML = "";
  _histWinStart = Math.max(0, _histTurns.length - HIST_WINDOW);
  for (let i = _histWinStart; i < _histTurns.length; i++){
    col.appendChild(buildTurnBlock(i));
  }
  renderPendingMermaid();
  renderPendingMath();
  renderPendingCodeHighlight();
  scrollToBottom();
}

// Called when the user scrolls near the top of a windowed history -- pulls
// the next older batch of turns in from the in-memory log and prepends
// them, preserving scroll position so the viewport doesn't jump.
function prependOlderHistoryTurns(){
  if (!_histActive || _histWinStart <= 0) return;
  const col        = chatCol();
  const sc         = document.getElementById("chat-scroll");
  if (!col || !sc) return;
  const prevHeight = sc.scrollHeight;
  const prevTop    = sc.scrollTop;
  const newStart   = Math.max(0, _histWinStart - HIST_SHIFT);
  const frag       = document.createDocumentFragment();
  for (let i = newStart; i < _histWinStart; i++){
    frag.appendChild(buildTurnBlock(i));
  }
  col.insertBefore(frag, col.firstChild);
  _histWinStart = newStart;
  renderPendingMermaid();
  renderPendingMath();
  renderPendingCodeHighlight();
  requestAnimationFrame(()=>{
    sc.scrollTop = prevTop + (sc.scrollHeight - prevHeight);
  });
}

// Delegated (not queried at parse time, mirrors the existing delegated
// click listener for .code-copy-btn below) -- scroll doesn't bubble, so
// this needs the capture phase.
document.addEventListener("scroll", (e)=>{
  if (!_histActive) return;
  const sc = e.target;
  if (!sc || sc.id !== "chat-scroll") return;
  if (sc.scrollTop < 120) prependOlderHistoryTurns();
}, true);

// Tracks whichever tool-call row is currently "live" so only the most
// recent one pulses — earlier tool calls settle to a plain dot once a
// newer tool call, a reply, or turn completion supersedes them.
let _activeToolDot = null;
function setActiveToolDot(el){
  if (_activeToolDot) _activeToolDot.classList.remove("active");
  _activeToolDot = el || null;
  if (_activeToolDot) _activeToolDot.classList.add("active");
}

// ── Grouped tool-call pills ───────────────────────────────────────────────────────────────
// Every stretch of tool calls becomes ONE ".tool-group" row holding a
// "Ran N tools" pill. The group keeps absorbing tool calls until Midum
// delivers text (say() / a reply), the user speaks, an error lands, or the
// turn finishes -- then it's closed and the next tool call starts a fresh
// group. Expanding the group drops it into one small pill per call
// (".tc"); clicking a call pill reveals the actual call + the tool return.
const _TG_ICON_TOOL = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>';
const _TG_ICON_CHEV = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6"/></svg>';

let _currentToolGroup = null;   // the .tool-group row still accepting live tool calls

// FIFO queue of call pills (".tc") awaiting their 'tool_call' detail event
// (name/args/result). Pushed the instant a tool starts (appendRow "tool"),
// consumed by attachToolCallDetail() once the call finishes. Matched by
// tool name first (handles the rare overlapping-call case), otherwise the
// oldest unresolved pill.
let _pendingToolRows = [];

function _tgArgsStr(args){
  return (args && typeof args === "object" && Object.keys(args).length) ? JSON.stringify(args, null, 2) : "(no arguments)";
}

// Short one-line hint of what the call was about (first non-empty argument).
function _tgPreview(args){
  if (!args || typeof args !== "object") return "";
  for (const k of Object.keys(args)){
    const v = args[k];
    if (v === null || v === undefined) continue;
    let s = typeof v === "string" ? v : (typeof v === "object" ? JSON.stringify(v) : String(v));
    s = s.replace(/\s+/g, " ").trim();
    if (!s) continue;
    return s.length > 48 ? s.slice(0, 47) + "…" : s;
  }
  return "";
}

function _tgLooksLikeError(result){
  return /^\s*(error|tool exception|traceback|exception|failed)\b/i.test(String(result || ""));
}

function _tgDuration(ms){
  if (!(ms >= 0)) return "";
  if (ms < 1000) return Math.max(1, Math.round(ms)) + "ms";
  return (ms < 10000 ? (ms / 1000).toFixed(1) : Math.round(ms / 1000)) + "s";
}

function createToolGroup(){
  const row = document.createElement("div");
  row.className = "row tool-group";
  row.innerHTML = `
    <div class="tg-head">
      <button type="button" class="tg-pill" aria-expanded="false">
        <span class="tg-dot"></span>
        <span class="tg-icon">${_TG_ICON_TOOL}</span>
        <span class="tg-label">Ran 0 tools</span>
        <span class="tg-extra"></span>
        <span class="tg-chev">${_TG_ICON_CHEV}</span>
      </button>
      <button type="button" class="row-copy-btn" title="Copy tool calls">${icon("copy")}</button>
    </div>
    <div class="tg-body" inert><div class="tg-body-inner"><div class="tg-list"></div></div></div>`;
  const pill = row.querySelector(".tg-pill");
  const body = row.querySelector(".tg-body");
  pill.onclick = ()=>{
    const open = !row.classList.contains("open");
    row.classList.toggle("open", open);
    pill.setAttribute("aria-expanded", open ? "true" : "false");
    body.inert = !open;
  };
  wireRowCopyBtn(row);
  return row;
}

// Re-derives the group's label / live hint / error tint from its call pills.
function _tgRefresh(groupRow){
  if (!groupRow) return;
  let own = 0, running = null, errs = 0;
  groupRow.querySelectorAll(".tg-list > .tc").forEach(el=>{
    own++;
    if (el.classList.contains("pending")) running = el;
    if (el.classList.contains("err")) errs++;
  });
  const label = groupRow.querySelector(".tg-label");
  const extra = groupRow.querySelector(".tg-extra");
  // X = number of tool calls inside this group.
  label.textContent = `Ran ${own} ${own === 1 ? "tool" : "tools"}`;
  extra.textContent = running ? (running.dataset.toolName || "") : "";
  groupRow.classList.toggle("has-err", errs > 0);
}

function _tgAdd(groupRow, el){
  const list = groupRow.querySelector(".tg-list");
  el.style.setProperty("--i", Math.min(list.children.length, 8));
  list.appendChild(el);
  _tgRefresh(groupRow);
}

// The group live tool calls currently flow into (created on demand).
function _tgLiveGroup(){
  if (!_currentToolGroup || !_currentToolGroup.isConnected){
    _currentToolGroup = createToolGroup();
    _currentToolGroup.classList.add("live");
    chatCol().appendChild(_currentToolGroup);
  }
  return _currentToolGroup;
}

// Stops the current group from absorbing more calls (Midum said something,
// the user spoke, an error landed, or the turn finished).
function closeToolGroup(){
  if (_currentToolGroup){
    _currentToolGroup.classList.remove("live");
    _tgRefresh(_currentToolGroup);
  }
  _currentToolGroup = null;
}

// Turn ended with calls that never reported back (aborted / crashed).
function settleStaleToolPills(){
  _pendingToolRows.forEach(el=>{
    el.classList.remove("pending");
    el.classList.add("stale");
    const pv = el.querySelector(".tc-preview");
    if (pv) pv.textContent = "no result";
    const res = el.querySelector(".tc-result");
    if (res) res.textContent = "(no result received)";
    _tgRefresh(el.closest(".tool-group"));
  });
  _pendingToolRows = [];
}

// One call pill. info: {name, agent?, pending?}
function buildToolCallPill(info){
  const el = document.createElement("div");
  el.className = "tc" + (info.agent ? " agent" : "") + (info.pending ? " pending" : "");
  el.dataset.toolName = info.name || "";
  el.innerHTML = `
    <button type="button" class="tc-pill" aria-expanded="false">
      <span class="tc-dot"></span>
      ${info.agent ? `<span class="tc-agent">${escapeHtml(info.agent)}</span>` : ""}
      <span class="tc-name">${escapeHtml(info.name || "tool")}</span>
      <span class="tc-preview"></span>
      <span class="tc-time"></span>
      <span class="tc-chev">${_TG_ICON_CHEV}</span>
    </button>
    <div class="tc-detail" inert><div class="tc-detail-inner"><div class="tc-panel">
      <div class="tc-block">
        <div class="tc-block-head"><span>Call</span><button type="button" class="tc-copy" data-copy="call" title="Copy call">${icon("copy")}</button></div>
        <pre class="tc-call"></pre>
      </div>
      <div class="tc-block">
        <div class="tc-block-head"><span>Return</span><button type="button" class="tc-copy" data-copy="result" title="Copy return">${icon("copy")}</button></div>
        <pre class="tc-result"></pre>
      </div>
    </div></div></div>`;
  const pill = el.querySelector(".tc-pill");
  const detail = el.querySelector(".tc-detail");
  pill.onclick = ()=>{
    const open = !el.classList.contains("open");
    el.classList.toggle("open", open);
    pill.setAttribute("aria-expanded", open ? "true" : "false");
    detail.inert = !open;
  };
  el.querySelectorAll(".tc-copy").forEach(btn=>{
    btn.onclick = async (e)=>{
      e.stopPropagation();
      const d = el._tc || {};
      const ok = await copyTextToClipboard(btn.dataset.copy === "call" ? (d.call || "") : (d.result || ""));
      if (ok) flashCopyFeedback(btn);
    };
  });
  if (info.pending){
    el.dataset.t0 = String(Date.now());
    el.querySelector(".tc-preview").textContent = "running…";
    el.querySelector(".tc-call").textContent = `${info.name || "tool"}(…)`;
    el.querySelector(".tc-result").textContent = "Waiting for result…";
  }
  return el;
}

// Fills a call pill with its finished call + return and settles its state.
function _tcSetData(el, args, result){
  const name = el.dataset.toolName || "tool";
  const resStr = (result === null || result === undefined) ? "" : String(result);
  const callText = `${name}(${_tgArgsStr(args)})`;
  el._tc = { name: el._tcLabel || name, call: callText, result: resStr };
  el.querySelector(".tc-call").textContent = callText;
  el.querySelector(".tc-result").textContent = resStr || "(empty)";
  el.querySelector(".tc-preview").textContent = _tgPreview(args);
  el.classList.remove("pending", "stale");
  el.classList.toggle("err", _tgLooksLikeError(resStr));
  if (el.dataset.t0){
    const dur = Date.now() - Number(el.dataset.t0);
    el.querySelector(".tc-time").textContent = dur >= 50 ? _tgDuration(dur) : "";
    delete el.dataset.t0;
  }
}

// Shows a running tool's call in its pill BEFORE the result exists (fired by
// the 'tool_start' event, which the backend pushes right after the
// "-> Executing" line and before the tool actually runs).
function attachToolCallArgs(name, args){
  if (name === "say") return;
  const el = _pendingToolRows.find(r=>r.dataset.toolName === name && !r._argsShown);
  if (!el) return;
  el._argsShown = true;
  const callText = `${name}(${_tgArgsStr(args)})`;
  el._tc = { name: el._tcLabel || name, call: callText, result: "" };
  el.querySelector(".tc-call").textContent = callText;
  el.querySelector(".tc-preview").textContent = _tgPreview(args) || "running…";
}

function attachToolCallDetail(name, args, result){
  // say() is rendered as the reply bubble itself, never as a tool pill.
  if (name === "say") return;
  let el = null;
  const idx = _pendingToolRows.findIndex(r=>r.dataset.toolName === name);
  if (idx !== -1){ el = _pendingToolRows[idx]; _pendingToolRows.splice(idx, 1); }
  else if (_pendingToolRows.length && !String(name || "").startsWith("[")){ el = _pendingToolRows.shift(); }
  if (!el){
    // No "-> Executing" placeholder was pushed for this call (e.g. Render
    // Surface calls only report here) -- add an already-finished pill.
    el = buildToolCallPill({name});
    _tcSetData(el, args, result);
    _tgAdd(_tgLiveGroup(), el);
    scrollToBottom();
    return;
  }
  _tcSetData(el, args, result);
  _tgRefresh(el.closest(".tool-group"));
}

// A sub-agent's own tool calls (agent_tool_call events) don't show in the
// SUPERVISOR's own "Ran N tools" pills -- those only cover Midum's own
// calls. If the person is currently previewing THIS agent's (read-only)
// conversation tab (see switchConvTab/setViewingAgentTab), append it live
// as a finished tool-call pill via the same rendering appendRow("toolcall")
// already uses to replay one from a tab's stored transcript -- otherwise
// this call would only show up the next time the tab is clicked. Agent
// activity always still reaches the Log pane via the normal log events
// regardless of which tab is on screen.
function appendAgentToolRow(agent, name, args, result){
  if (!state.viewingAgentTab || state.viewingAgentTab !== agent) return;
  let payload;
  try { payload = JSON.stringify({name, args, result}); } catch(e){ payload = JSON.stringify({name, args: {}, result: String(result)}); }
  appendRow("toolcall", payload);
}

function animateWords(container){
  if (!container) return;
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, null);
  const textNodes = [];
  let node;
  while ((node = walker.nextNode())){
    if (!node.nodeValue.trim()) continue;
    if (node.parentElement && node.parentElement.closest("svg")) continue;   // don't touch SVG <text> content
    textNodes.push(node);
  }
  let wordIndex = 0;
  textNodes.forEach(tn=>{
    const parts = tn.nodeValue.split(/(\s+)/);
    const frag = document.createDocumentFragment();
    parts.forEach(part=>{
      if (part.trim() === ""){
        frag.appendChild(document.createTextNode(part));
      } else {
        const span = document.createElement("span");
        span.className = "word-anim";
        span.textContent = part;
        span.style.animationDelay = Math.min(wordIndex * 16, 900) + "ms";
        wordIndex++;
        frag.appendChild(span);
      }
    });
    tn.parentNode.replaceChild(frag, tn);
  });
}

function appendRow(tag, text, extra){
  const col = chatCol();
  const row = document.createElement("div");
  if (tag === "user"){
    closeToolGroup();          // a new user turn always starts fresh tool group(s) too
    _currentMidumRow = null;   // a new user turn always starts a fresh Midum bubble
    row.className = "row user";
    row.innerHTML = `<div class="row-label-row"><div class="row-label">You</div><button class="row-copy-btn" title="Copy message">${icon("copy")}</button></div><div class="bubble user">${attBubbleChips(extra)}${text ? renderUserContent(text) : ""}</div>`;
    wireRowCopyBtn(row);
    attHydrate(row);
    _currentUserRow = row;
  } else if (tag === "midum"){
    setActiveToolDot(null);
    closeToolGroup();   // say() / a reply ends the current "Ran N tools" group
    // If a bubble is already streaming for this response (an earlier
    // say() call, or a reply following one), append into it instead of
    // creating a new row -- keeps exactly one "Midum" label per response
    // no matter how many say()/reply chunks make it up.
    if (_currentMidumRow && _currentMidumRow.isConnected){
      const bubble = _currentMidumRow.querySelector(".bubble");
      const chunk = document.createElement("div");
      chunk.className = "midum-chunk";
      chunk.innerHTML = renderMidumContent(text);
      bubble.appendChild(chunk);
      animateWords(chunk);
      renderPendingMermaid();
      renderPendingMath();
      renderPendingCodeHighlight();
      scrollToBottom();
      return;
    }
    row.className = "row midum";
    row.innerHTML = `<div class="row-label-row"><button class="row-copy-btn" title="Copy response">${icon("copy")}</button></div><div class="bubble midum">${renderMidumContent(text)}</div>`;
    wireRowCopyBtn(row);
    col.appendChild(row);
    _currentMidumRow = row;
    // The first content chunk of a live reply is exactly when a
    // Regenerate button becomes meaningful (there's now something to
    // hover/regenerate) -- reuses whatever turn id sendMessage() or
    // regenerateLastResponse() most recently stashed here.
    if (_pendingTurnId !== null) attachRegenButton(_currentMidumRow, _pendingTurnId);
    animateWords(row.querySelector(".bubble"));
    renderPendingMermaid();
    renderPendingMath();
    renderPendingCodeHighlight();
    scrollToBottom();
    return;
  } else if (tag === "system"){
    row.className = "row system";
    row.innerHTML = `<div class="bubble">${iconizeHtml(escapeHtml(text))}</div>`;
  } else if (tag === "interjection"){
    // A live steering message sent through the prompt box while a previous
    // turn was still executing -- see sendMessage()/Api.send_interjection.
    // Deliberately not a ".row.user" bubble: it doesn't start a new turn,
    // so it shouldn't look like one.
    row.className = "row interjection";
    row.innerHTML = `<div class="bubble">${escapeHtml(text)}</div>`;
  } else if (tag === "error"){
    row.className = "row error";
    row.innerHTML = `<div class="bubble">${escapeHtml(text)}</div>`;
    setActiveToolDot(null);
    closeToolGroup();
  } else if (tag === "tool"){
    _currentMidumRow = null;   // a tool call breaks any in-progress Midum bubble
    const nameMatch = /-> Executing: '([^']+)'/.exec(text);
    const toolName = nameMatch ? nameMatch[1] : "";
    // say() is shown as the reply bubble itself (and closes the current
    // tool group) -- it never gets a tool pill of its own.
    if (toolName === "say") return;
    const tc = buildToolCallPill({name: toolName || String(text || "").trim() || "tool", pending: true});
    _pendingToolRows.push(tc);
    _tgAdd(_tgLiveGroup(), tc);
    scrollToBottom();
    return;
  } else if (tag === "toolcall"){
    // Persisted, already-resolved tool-call record (see Api._handle_tool_call):
    // call + result are both known up front, so it's added as a finished pill
    // (no _pendingToolRows entry -- there's no live 'tool_call' event coming
    // later to attach detail to).
    let data;
    try { data = JSON.parse(text); } catch(e){ data = {name: "", args: {}, result: text}; }
    if (data.name === "say") return;
    const tc = buildToolCallPill({name: data.name || "tool"});
    _tcSetData(tc, data.args, data.result);
    _tgAdd(_tgLiveGroup(), tc);
    scrollToBottom();
    return;
  }
  col.appendChild(row);
  scrollToBottom();
}

// ── Copy support (individual rows + full conversation) --------------------
// Renders a chat row back into plain text -- including tool calls, which
// are expanded into a readable "Tool call: name(args) -> result" block
// instead of raw JSON schemas -- so both the per-row copy button and the
// "copy full conversation" button produce something a person can paste
// into an email/doc/ticket as-is.
// Bubble text is read via innerText, but code blocks now carry an inline
// .code-copy-btn (⧉) button for copying just that snippet -- strip those
// out of a clone before reading innerText so row/full-conversation copies
// don't pick up stray ⧉ glyphs that aren't part of the actual message.
function bubblePlainText(b){
  if (!b) return "";
  const clone = b.cloneNode(true);
  clone.querySelectorAll(".code-copy-btn").forEach(btn=>btn.remove());
  return clone.innerText.trim();
}

function rowPlainText(row){
  if (!row) return "";
  if (row.classList.contains("user")){
    const b = row.querySelector(".bubble");
    return "You: " + bubblePlainText(b);
  }
  if (row.classList.contains("midum")){
    if (row.querySelector(".ask-card")) return "";   // inline ask cards aren't transcript text
    const b = row.querySelector(".bubble");
    return bubblePlainText(b);
  }
  if (row.classList.contains("tool-group")){
    const parts = [];
    row.querySelectorAll(".tg-list > .tc").forEach(el=>{
      const d = el._tc;
      if (!d){ parts.push(`[Tool call: ${el.dataset.toolName || "tool"}]`); return; }
      let out = `[Tool call: ${d.name}]`;
      if (d.call) out += `\n${d.call}`;
      if (d.result) out += `\n-> ${d.result}`;
      parts.push(out);
    });
    return parts.join("\n\n");
  }
  if (row.classList.contains("tool")){
    const name = row.dataset.toolName || "tool";
    const pres = row.querySelectorAll(".tool-detail pre");
    const callText = pres[0] ? pres[0].innerText.trim() : "";
    const resultText = pres[1] ? pres[1].innerText.trim() : "";
    let out = `[Tool call: ${name}]`;
    if (callText) out += `\n${callText}`;
    if (resultText) out += `\n-> ${resultText}`;
    return out;
  }
  if (row.classList.contains("system")){
    const b = row.querySelector(".bubble");
    return "[System] " + (b ? b.innerText.trim() : "");
  }
  if (row.classList.contains("interjection")){
    const b = row.querySelector(".bubble");
    return "[Interjection] " + (b ? b.innerText.trim() : "");
  }
  if (row.classList.contains("error")){
    const b = row.querySelector(".bubble");
    return "[Error] " + (b ? b.innerText.trim() : "");
  }
  return "";
}

async function copyTextToClipboard(text){
  if (!text) return false;
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    // Fallback for environments where the async Clipboard API is blocked.
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      return true;
    } catch (e2) {
      return false;
    }
  }
}

function flashCopyFeedback(btn){
  if (!btn) return;
  const orig = btn.innerHTML;
  btn.innerHTML = icon("check");
  btn.classList.add("copied");
  setTimeout(()=>{ btn.innerHTML = orig; btn.classList.remove("copied"); }, 1000);
}

function wireRowCopyBtn(row){
  const btn = row.querySelector(".row-copy-btn");
  if (!btn) return;
  btn.onclick = async (e)=>{
    e.stopPropagation();
    const ok = await copyTextToClipboard(rowPlainText(row));
    if (ok) flashCopyFeedback(btn);
  };
}

// ── Edit Prompt / Regenerate Response ──────────────────────────────────
// Only the single most recent live turn ever has these buttons -- they're
// attached directly to _currentUserRow/_currentMidumRow as those rows are
// created (see appendRow / sendMessage), and removed along with those
// rows the moment the turn is edited, regenerated, or superseded by a new
// message. Never rebuilt from persisted history (buildStaticRow never
// adds them) -- Api only tracks an editable turn for the live in-memory
// state of the CURRENTLY open tab, see gui/app.py's _last_turn_* markers.

// Removes any leftover buttons from an older turn whose row is still on
// screen (chat history keeps growing -- those rows are never deleted on
// their own). Called right before a new live turn starts.
function clearOldTurnButtons(){
  document.querySelectorAll(".row-edit-btn, .row-regen-btn").forEach(b => b.remove());
}

function attachEditButton(row, turnId){
  if (!row) return;
  const labelRow = row.querySelector(".row-label-row");
  if (!labelRow || labelRow.querySelector(".row-edit-btn")) return;
  row.dataset.turnId = String(turnId);
  const btn = document.createElement("button");
  btn.className = "row-copy-btn row-edit-btn";
  btn.title = "Edit prompt";
  btn.innerHTML = icon("pencil");
  btn.onclick = (e)=>{ e.stopPropagation(); editLastPrompt(turnId, btn); };
  labelRow.appendChild(btn);
}

function attachRegenButton(row, turnId){
  if (!row) return;
  const labelRow = row.querySelector(".row-label-row");
  if (!labelRow || labelRow.querySelector(".row-regen-btn")) return;
  row.dataset.turnId = String(turnId);
  const btn = document.createElement("button");
  btn.className = "row-copy-btn row-regen-btn";
  btn.title = "Regenerate response";
  btn.innerHTML = icon("refresh");
  btn.onclick = (e)=>{ e.stopPropagation(); regenerateLastResponse(turnId, btn); };
  labelRow.appendChild(btn);
}

// Removes `fromRow` (if includeFromRow) or everything AFTER it, through
// to the end of the chat column. Safe because these buttons only ever
// exist on the LATEST turn, so "this row and everything after" is always
// exactly that turn's own rows (prompt + response + any tool-call rows in
// between) -- nothing else can have been appended after it live.
function removeRowsFrom(fromRow, includeFromRow){
  if (!fromRow) return;
  let node = includeFromRow ? fromRow : fromRow.nextElementSibling;
  while (node){
    const next = node.nextElementSibling;
    node.remove();
    node = next;
  }
}

async function editLastPrompt(turnId, btn){
  if (btn) btn.disabled = true;
  let res;
  try {
    res = await api("edit_last_prompt", turnId);
  } catch (e) {
    res = null;
  }
  if (!res || !res.ok){
    if (btn) btn.disabled = false;
    return;
  }
  const userRow = _currentUserRow;
  removeRowsFrom(userRow, true);
  _currentUserRow = null;
  _currentMidumRow = null;
  _pendingTurnId = null;
  const input = document.getElementById("msg-input");
  input.value = res.text || "";
  (res.attachments || []).forEach(a=>attAddRecord(a, true));   // put the files back in the composer too
  autosizeMsgInput();
  input.focus();
  state.thinking = false;
  setActiveToolDot(null);
  setStatus("Ready", "ok");
}

async function regenerateLastResponse(turnId, btn){
  if (state.thinking) return;
  if (btn) btn.disabled = true;
  _pendingTurnId = turnId;   // the fresh midum row about to stream in reuses this turn's id
  let res;
  try {
    res = await api("regenerate_last_response", turnId);
  } catch (e) {
    res = null;
  }
  if (!res || !res.ok){
    if (btn) btn.disabled = false;
    _pendingTurnId = null;
    return;
  }
  removeRowsFrom(_currentUserRow, false);
  _currentMidumRow = null;
  setActiveToolDot(null);
  state.thinking = true;
  setStatus("Executing turns...", "busy");
}

// Copy-to-clipboard for individual markdown code blocks. Code blocks are
// injected into the DOM dynamically (streamed/rendered chat content), so a
// single delegated listener on the chat column -- rather than wiring each
// .code-copy-btn as it's created -- ensures every code block's copy button
// works, including ones rendered after this listener is attached.
document.addEventListener("click", async (e)=>{
  const btn = e.target.closest(".code-copy-btn");
  if (!btn) return;
  e.stopPropagation();
  const wrap = btn.closest(".code-block-wrap");
  const pre = wrap ? wrap.querySelector("pre.code-block") : null;
  const ok = await copyTextToClipboard(pre ? pre.innerText : "");
  if (ok) flashCopyFeedback(btn);
});

async function copyFullConversation(){
  const rows = Array.from(chatCol().children);
  const parts = rows.map(rowPlainText).map(t=>t.trim()).filter(Boolean);
  const ok = await copyTextToClipboard(parts.join("\n\n"));
  if (ok) flashCopyFeedback(document.getElementById("copy-chat-btn"));
}

function scrollToBottom(){
  const sc = document.getElementById("chat-scroll");
  requestAnimationFrame(()=>{ sc.scrollTop = sc.scrollHeight; });
}

function clearChat(){
  chatCol().innerHTML = "";
  _pendingToolRows = [];
  _currentToolGroup = null;
  _currentMidumRow = null;
  _currentUserRow = null;
  _pendingTurnId = null;
  // Reset windowed-history state too -- otherwise a brand-new chat (or one
  // started fresh after "New session") would still think it's showing a
  // partially-scrolled window of some previous chat's log, and the
  // scroll-up handler would try to prepend turns that no longer apply.
  _histTurns    = [];
  _histWinStart = 0;
  _histActive   = false;
  // The prompt box's attachment chips belong to the chat being shown -- reload them for it.
  try { attRestoreForTab().catch(()=>{}); } catch (e) {}
}

// ── Ask cards --------------------------------------------------------------
function appendAsk(id, kind, payload){
  closeToolGroup();   // an inline question/approval card ends the current tool group
  const col = chatCol();
  const row = document.createElement("div");
  row.className = "row midum";
  const card = document.createElement("div");
  card.className = "ask-card";

  const resolve = (label, value)=>{
    api("answer_ask", id, value);
    card.innerHTML = `<div class="ask-hdr">${label}</div>`;
  };

  if (kind === "text"){
    card.innerHTML = `<div class="ask-hdr">${icon("help")} ${escapeHtml(payload.title||"Midum needs input")}</div>
      <div style="font-size:var(--cfs-lg);margin-bottom:10px;">${escapeHtml(payload.prompt||"")}</div>
      <input type="text" placeholder="Type your answer..." />
      <div class="ask-actions">
        <button class="ghost-btn" data-act="cancel">Cancel</button>
        <button class="btn" data-act="submit" style="background:var(--accent);color:#fff;">Submit</button>
      </div>`;
    const input = card.querySelector("input");
    const submit = ()=>{ const v=input.value.trim(); resolve(`${icon("help")} → "${escapeHtml(v||"(empty)")}"`, v||"[USER SUBMITTED EMPTY TEXT]"); };
    card.querySelector('[data-act=submit]').onclick = submit;
    card.querySelector('[data-act=cancel]').onclick = ()=>resolve(`${icon("help")} cancelled`, "[USER CANCELLED]");
    input.addEventListener("keydown", e=>{ if (e.key === "Enter") submit(); });
    setTimeout(()=>input.focus(), 30);
  } else if (kind === "approval"){
    card.innerHTML = `<div class="ask-hdr">${icon("alert")} Midum requests approval</div>
      <div style="font-weight:700;font-size:var(--cfs-lg);">${escapeHtml(payload.message||"")}</div>
      <div style="font-size:var(--cfs-md);color:var(--chat-sub);margin:6px 0 10px;">${escapeHtml(payload.details||"")}</div>
      <div class="ask-actions">
        <button class="ghost-btn" data-act="decline" style="color:var(--red);border-color:#3f0f0f;">${icon("x-circle")} Decline</button>
        <button class="btn" data-act="approve" style="background:var(--green);color:#fff;">${icon("check-circle")} Approve</button>
      </div>`;
    card.querySelector('[data-act=approve]').onclick = ()=>resolve(`${icon("check-circle")} Approved`, "APPROVED");
    card.querySelector('[data-act=decline]').onclick = ()=>resolve(`${icon("x-circle")} Declined`, "DECLINED");
  } else if (kind === "choice"){
    const opts = (payload.options||[]).map(o=>`<button class="ask-opt-btn" data-v="${escapeHtml(o)}">${escapeHtml(o)}</button>`).join("");
    card.innerHTML = `<div class="ask-hdr">${icon("help")} Midum has a question</div>
      <div style="font-weight:700;font-size:var(--cfs-lg);margin-bottom:10px;">${escapeHtml(payload.question||"")}</div>
      ${opts}
      ${payload.allow_custom !== false ? '<div style="display:flex;gap:6px;margin-top:6px;"><input type="text" placeholder="Something else..." style="flex:1;"/><button class="btn" data-act="custom">Other...</button></div>' : ""}`;
    card.querySelectorAll(".ask-opt-btn").forEach(b=>{
      b.onclick = ()=>resolve(`${icon("help")} → "${escapeHtml(b.dataset.v)}"`, b.dataset.v);
    });
    const customBtn = card.querySelector('[data-act=custom]');
    if (customBtn){
      const inp = card.querySelector('input[type=text]');
      customBtn.onclick = ()=>{ const v=inp.value.trim(); if(v) resolve(`${icon("help")} → "${escapeHtml(v)}"`, v); };
    }
  } else if (kind === "file"){
    card.innerHTML = `<div class="ask-hdr">${icon("folder")} Midum needs a file</div>
      <div style="font-size:var(--cfs-lg);margin-bottom:10px;">${escapeHtml(payload.prompt||"Select a file")}</div>
      <div class="ask-actions">
        <button class="ghost-btn" data-act="cancel">Cancel</button>
        <button class="btn" data-act="browse" style="background:var(--accent);color:#fff;">Browse...</button>
      </div>`;
    card.querySelector('[data-act=cancel]').onclick = ()=>resolve(`${icon("folder")} cancelled`, "[USER CANCELLED]");
    card.querySelector('[data-act=browse]').onclick = async ()=>{
      const r = await api("pick_file", payload.must_exist !== false);
      resolve(`${icon("folder")} → ${escapeHtml(r.path||"cancelled")}`, r.path || "[USER CANCELLED]");
    };
  }

  row.appendChild(card);
  col.appendChild(row);
  scrollToBottom();
}

// ── Sending ------------------------------------------------------------
function autosizeMsgInput(){
  const el = document.getElementById("msg-input");
  if (!el) return;
  el.style.height = "auto";
  // ~10 lines + vertical padding, derived from the live font size so it still
  // fits when the chat text size slider changes the line height.
  const cs = getComputedStyle(el);
  const lh = parseFloat(cs.lineHeight) || (parseFloat(cs.fontSize) * 1.45);
  const maxHeight = Math.round(lh * 10 + 14);
  const newHeight = Math.min(el.scrollHeight, maxHeight);
  el.style.height = newHeight + "px";
  el.style.overflowY = el.scrollHeight > maxHeight ? "auto" : "hidden";
}

// ── File attachments (prompt box) ────────────────────────────────────────────────────────
// Files become ARTIFACTS server-side (artifacts.py): stored per chat, text-indexed,
// and uploaded to the Gemini Files API. A chip is: transferring (browser -> app,
// only for pasted/dropped blobs) -> ready (with background indexing/Gemini state
// polled via attach_status) or error. Sending never waits on the Gemini upload.
const ATT = { items: [], max: 100*1024*1024, chunk: 786432, poll: null, seq: 0, limitsLoaded: false, chatId: null, restoreSeq: 0 };
const _ATT_THUMBS = {};
const _ATT_SVG = {
  file:'<path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M13 2v7h7"/>',
  image:'<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>',
  music:'<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
  video:'<rect x="2" y="5" width="14" height="14" rx="2"/><path d="M22 7l-6 4 6 4z"/>',
  table:'<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18"/>',
  code:'<path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/>',
  x:'<path d="M18 6 6 18M6 6l12 12"/>',
  alert:'<circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>'
};
function attSvg(n){ return '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (_ATT_SVG[n] || _ATT_SVG.file) + '</svg>'; }
function attEsc(s){ return String(s == null ? "" : s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;"); }
function attFmtSize(n){ n = Number(n) || 0; if (n < 1024) return n + " B"; if (n < 1048576) return (n/1024).toFixed(0) + " KB"; if (n < 1073741824) return (n/1048576).toFixed(1) + " MB"; return (n/1073741824).toFixed(2) + " GB"; }
function attIconFor(kind){ return ({image:"image", audio:"music", video:"video", sheet:"table", code:"code"})[kind] || "file"; }
function attStatusText(c){
  if (c.status === "transferring") return "Reading… " + Math.round((c.progress || 0) * 100) + "%";
  if (c.status === "error") return c.error || "Failed";
  const r = c.rec || {};
  if (r.text_state === "pending") return "Reading file…";
  if (r.gemini_state === "uploading") return "Uploading to Gemini…";
  if (r.gemini_state === "active") return attFmtSize(c.size) + " · in Gemini";
  return attFmtSize(c.size);
}
function attChipTip(c){
  if (c.status === "error") return c.error || "Failed";
  if (c.rec && c.rec.gemini_state === "error") return "Gemini upload failed (still readable locally): " + (c.rec.gemini_error || "");
  return c.name;
}
function attChipHtml(c){
  const busy = c.status === "transferring" || (c.rec && c.rec.busy);
  const lead = c.thumb ? `<img class="att-thumb" src="${attEsc(c.thumb)}" alt="">`
                       : `<div class="att-ico">${attSvg(c.status === "error" ? "alert" : attIconFor(c.kind))}</div>`;
  const prog = c.status === "transferring" ? `<div class="att-prog"><i style="width:${Math.round((c.progress || 0) * 100)}%"></i></div>`
             : (busy ? '<div class="att-prog indet"><i></i></div>' : "");
  const tip = attChipTip(c);
  return `<div class="att-chip${c.status === "error" ? " err" : ""}" data-key="${c.key}" title="${attEsc(tip)}">${lead}<div class="att-meta"><div class="att-name">${attEsc(c.name)}</div><div class="att-sub">${attEsc(attStatusText(c))}</div>${prog}</div><button class="att-x" data-key="${c.key}" title="Remove">${attSvg("x")}</button></div>`;
}
// Everything about a chip that needs its inner markup rebuilt. Progress % and the status
// text are deliberately NOT part of it -- they are patched in place so a running progress
// bar / indeterminate slider is never torn down and restarted.
function attChipSig(c){
  const busy = c.status === "transferring" || !!(c.rec && c.rec.busy);
  return [c.status, c.thumb || "", c.kind, c.name, busy ? (c.status === "transferring" ? "t" : "i") : "-", attChipTip(c)].join("\u0001");
}
function attBuildChip(c){
  const t = document.createElement("div");
  t.innerHTML = attChipHtml(c);
  return t.firstElementChild;
}
function attRender(){
  const tray = document.getElementById("attach-tray");
  if (!tray) return;
  // Reconcile the chips in place (keyed by c.key) instead of rebuilding tray.innerHTML.
  // Rebuilding on every upload chunk / 1.2s status poll re-created every chip, which replayed
  // the .att-chip entry animation (the shrink-and-grow flash) and restarted the indeterminate slider.
  const live = new Map();
  Array.from(tray.children).forEach(el=>live.set(el.dataset.key, el));
  let prev = null;
  ATT.items.forEach(c=>{
    const sig = attChipSig(c);
    let el = live.get(c.key);
    if (el) live.delete(c.key);
    if (!el){
      el = attBuildChip(c);
      el._attSig = sig;
    } else if (el._attSig !== sig){
      const fresh = attBuildChip(c);
      el.className = fresh.className;
      el.title = fresh.title;
      el.innerHTML = fresh.innerHTML;
      el._attSig = sig;
    }
    const sub = el.querySelector(".att-sub"), txt = attStatusText(c);
    if (sub && sub.textContent !== txt) sub.textContent = txt;
    const bar = el.querySelector(".att-prog:not(.indet) > i");
    if (bar) bar.style.width = Math.round((c.progress || 0) * 100) + "%";
    if (el.parentNode !== tray || el.previousElementSibling !== prev) tray.insertBefore(el, prev ? prev.nextSibling : tray.firstChild);
    prev = el;
  });
  live.forEach(el=>el.remove());
  tray.classList.toggle("show", ATT.items.length > 0);
  const btn = document.getElementById("attach-btn");
  if (btn) btn.classList.toggle("has-files", ATT.items.length > 0);
}
function attClear(){   // composer only -- the files themselves stay stored (they're sent with the message)
  ATT.items.forEach(c=>{ if (c.thumb && String(c.thumb).startsWith("blob:")) URL.revokeObjectURL(c.thumb); });
  ATT.items = [];
  attRender();
}
// The prompt box is a per-tab draft. Whenever the visible chat changes (see clearChat) the
// server is asked which unsent attachments belong to the now-active chat and the chips are
// rebuilt from that -- so a file attached in tab A stays in tab A (and survives a restart),
// instead of one global chip list following you around and being sent from whichever tab.
async function attRestoreForTab(){
  const seq = ++ATT.restoreSeq;
  const before = new Set(ATT.items.map(c=>c.key));   // chips that belong to the tab we're leaving
  let r = null;
  try { r = await api("attach_pending"); } catch (e){ return; }
  if (seq !== ATT.restoreSeq || !r || !r.ok) return;  // a newer switch superseded this one
  if (r.chat_id === ATT.chatId) return;               // same chat (e.g. re-clicking the active tab): already right
  ATT.chatId = r.chat_id;
  ATT.items.forEach(c=>{ if (before.has(c.key) && c.thumb && String(c.thumb).startsWith("blob:")) URL.revokeObjectURL(c.thumb); });
  ATT.items = ATT.items.filter(c=>!before.has(c.key));
  (r.files || []).forEach(f=>attAddRecord(f));
  attRender();
}
function attAddRecord(rec, refresh){
  if (!rec || ATT.items.some(c=>c.id === rec.id)) return;
  const c = {key: "a" + (++ATT.seq), id: rec.id, name: rec.name, size: rec.size, mime: rec.mime, kind: rec.kind,
             status: "ready", rec: refresh ? Object.assign({}, rec, {busy: true}) : rec, thumb: null};
  ATT.items.push(c);
  if (rec.kind === "image"){
    api("attach_thumbnail", rec.id).then(u=>{ if (u){ c.thumb = u; attRender(); } }).catch(()=>{});
  }
  attRender();
  attPollStart();
}
function attHandleResult(r){
  if (!r) return;
  (r.files || []).forEach(f=>attAddRecord(f));
  if ((r.errors || []).length) showAlert(r.errors.map(e=>e.name + ": " + e.error).join("\n"), "Attachment");
  else if (r.ok === false && r.error) showAlert(String(r.error), "Attachment");
}
async function attPickFiles(){
  let r = null;
  try { r = await api("attach_pick_files"); } catch (e){ r = {ok: false, error: String(e)}; }
  attHandleResult(r);
}
async function attLimits(){
  if (ATT.limitsLoaded) return;
  ATT.limitsLoaded = true;
  try { const l = await api("attach_limits"); if (l){ ATT.max = l.max_bytes || ATT.max; ATT.chunk = l.chunk || ATT.chunk; } } catch (e){}
}
function attBlobToB64(blob){
  return new Promise((res, rej)=>{
    const fr = new FileReader();
    fr.onload = ()=>{ const s = String(fr.result); res(s.slice(s.indexOf(",") + 1)); };
    fr.onerror = ()=>rej(fr.error || new Error("read failed"));
    fr.readAsDataURL(blob);
  });
}
async function attUploadBlob(file){
  await attLimits();
  if (!file.size){ showAlert(`"${file.name || "This item"}" is empty or is a folder -- folders can't be attached.`, "Attachment"); return; }
  if (file.size > ATT.max){ showAlert(`"${file.name}" is ${attFmtSize(file.size)}; the limit is ${attFmtSize(ATT.max)}.`, "Attachment"); return; }
  const ext = ((file.type || "").split("/")[1] || "bin").split("+")[0];
  const name = (file.name && file.name !== "image.png") ? file.name : ("pasted-" + Date.now().toString(36) + "." + ext);
  const chip = {key: "a" + (++ATT.seq), id: null, name: name, size: file.size, mime: file.type || "", kind: (file.type || "").startsWith("image/") ? "image" : "file",
                status: "transferring", progress: 0, rec: null, thumb: (file.type || "").startsWith("image/") ? URL.createObjectURL(file) : null};
  ATT.items.push(chip);
  attRender();
  const uid = "up" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  const total = Math.max(1, Math.ceil(file.size / ATT.chunk));
  try {
    for (let i = 0; i < total; i++){
      if (chip.cancelled){ await api("attach_abort", uid); return; }
      const b64 = await attBlobToB64(file.slice(i * ATT.chunk, Math.min(file.size, (i + 1) * ATT.chunk)));
      const r = await api("attach_chunk", uid, name, file.type || "", i, total, b64);
      if (!r || !r.ok) throw new Error((r && r.error) || "upload failed");
      chip.progress = (i + 1) / total;
      if (r.done && r.file){
        Object.assign(chip, {id: r.file.id, rec: r.file, kind: r.file.kind, mime: r.file.mime, size: r.file.size, status: "ready"});
      }
      if (!chip.cancelled) attRender();
    }
    if (!chip.id && !chip.cancelled) throw new Error("upload did not finish");
  } catch (e){
    chip.status = "error"; chip.error = String((e && e.message) || e);
  }
  attRender();
  attPollStart();
}
async function attAddBrowserFiles(list){
  const files = Array.from(list || []);
  if (!files.length) return;
  const paths = files.map(f=>f.pywebviewFullPath).filter(Boolean);
  const blobs = files.filter(f=>!f.pywebviewFullPath);
  if (paths.length){ try { attHandleResult(await api("attach_paths", paths)); } catch (e){ showAlert(String(e), "Attachment"); } }
  for (const f of blobs) await attUploadBlob(f);
}
function attPollStart(){ if (!ATT.poll) ATT.poll = setInterval(attPoll, 1200); }
async function attPoll(){
  const ids = ATT.items.filter(c=>c.id && c.rec && c.rec.busy).map(c=>c.id);
  if (!ids.length){ clearInterval(ATT.poll); ATT.poll = null; return; }
  let r = null;
  try { r = await api("attach_status", ids); } catch (e){ return; }
  const seen = new Set();
  ((r && r.files) || []).forEach(rec=>{ seen.add(rec.id); const c = ATT.items.find(x=>x.id === rec.id); if (c) c.rec = rec; });
  ATT.items.forEach(c=>{ if (c.id && ids.includes(c.id) && !seen.has(c.id)){ c.status = "error"; c.error = "File no longer available"; } });
  attRender();
}
// Chips shown inside a sent user bubble (live + replayed history)
function attBubbleChips(list){
  if (!list || !list.length) return "";
  return '<div class="att-row">' + list.map(a=>{
    const lead = a.kind === "image" ? `<img class="att-thumb" alt="" data-thumb="${attEsc(a.id)}">` : `<div class="att-ico">${attSvg(attIconFor(a.kind))}</div>`;
    return `<div class="att-chip-ro" title="${attEsc(a.name)}">${lead}<div class="att-meta"><div class="att-name">${attEsc(a.name)}</div><div class="att-sub">${attEsc(attFmtSize(a.size))}</div></div></div>`;
  }).join("") + '</div>';
}
function attHydrate(root){
  root.querySelectorAll("img[data-thumb]").forEach(img=>{
    const id = img.dataset.thumb;
    if (_ATT_THUMBS[id]){ img.src = _ATT_THUMBS[id]; return; }
    api("attach_thumbnail", id).then(u=>{ if (u){ _ATT_THUMBS[id] = u; img.src = u; } else { img.style.visibility = "hidden"; } }).catch(()=>{ img.style.visibility = "hidden"; });
  });
}
function initAttachments(){
  const tray = document.getElementById("attach-tray");
  const input = document.getElementById("msg-input");
  const overlay = document.getElementById("drop-overlay");
  if (!tray || !input) return;
  document.getElementById("attach-btn").onclick = attPickFiles;
  tray.addEventListener("click", e=>{
    const b = e.target.closest(".att-x");
    if (!b) return;
    const i = ATT.items.findIndex(c=>c.key === b.dataset.key);
    if (i < 0) return;
    const c = ATT.items.splice(i, 1)[0];
    if (c.status === "transferring") c.cancelled = true;
    else if (c.id) api("attach_remove", c.id).catch(()=>{});
    if (c.thumb && String(c.thumb).startsWith("blob:")) URL.revokeObjectURL(c.thumb);
    attRender();
  });
  // Paste: files/images from the clipboard become attachments; plain text paste is untouched.
  input.addEventListener("paste", e=>{
    const files = Array.from((e.clipboardData && e.clipboardData.files) || []);
    if (!files.length) return;
    e.preventDefault();
    attAddBrowserFiles(files);
  });
  // Drag & drop anywhere in the window (also stops the webview from navigating to a dropped file).
  let depth = 0;
  const hasFiles = e=>!!(e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files"));
  window.addEventListener("dragenter", e=>{ if (!hasFiles(e)) return; e.preventDefault(); depth++; overlay.classList.add("show"); });
  window.addEventListener("dragover", e=>{ if (!hasFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = "copy"; });
  window.addEventListener("dragleave", e=>{ if (!hasFiles(e)) return; depth = Math.max(0, depth - 1); if (!depth) overlay.classList.remove("show"); });
  window.addEventListener("drop", e=>{
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0; overlay.classList.remove("show");
    attAddBrowserFiles(e.dataTransfer.files);
  });
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initAttachments);
else initAttachments();

async function sendMessage(){
  const input = document.getElementById("msg-input");
  const text = input.value.trim();
  const pendingAtt = ATT.items.filter(c=>c.status === "ready" && c.id);
  if (!text && !pendingAtt.length) return;
  if (ATT.items.some(c=>c.status === "transferring")){
    showAlert("Your files are still being read -- one moment, then send again.", "Attachments");
    return;
  }
  if (state.viewingAgentTab){
    showAlert(`You're viewing "${state.viewingAgentTab}"'s conversation, which is read-only. Switch to one of your own tabs to send a message.`, "Read-only");
    return;
  }
  if (state.thinking){
    if (pendingAtt.length){
      showAlert("Midum is still working. Send your files once the current turn finishes.", "Attachments");
      return;
    }
    if (!text) return;
    // Midum is still "Executing turns..." on a previous message -- this
    // doesn't start a new turn, it steers the one already running. Shown
    // as a wide interjection pill (not a normal user bubble) and queued
    // server-side via send_interjection; orchestration.process_chat_turn
    // folds it into the model's context right after the next tool call
    // finishes (see state.queue_interjection).
    input.value = "";
    autosizeMsgInput();
    appendRow("interjection", text);
    await api("send_interjection", text);
    return;
  }
  input.value = "";
  autosizeMsgInput();
  clearOldTurnButtons();   // this message supersedes whatever turn had them
  const attIds = pendingAtt.map(c=>c.id);
  const attPublic = pendingAtt.map(c=>({id: c.id, name: c.name, mime: c.mime, size: c.size, kind: c.kind}));
  attClear();
  appendRow("user", text, attPublic);
  setActiveToolDot(null);
  setStatus("Executing turns...", "busy");
  state.thinking = true;
  const kbOnlyActive = !!(state.kbOnly && state.kbSources.length);
  const explainActive = !!(kbOnlyActive && state.explainMode);
  const res = await api("send_message", text, kbOnlyActive, kbOnlyActive ? state.kbSources : [], explainActive, state.explainModeType, attIds);
  if (res && res.ok && typeof res.turn_id !== "undefined"){
    _pendingTurnId = res.turn_id;
    // Edit Prompt can attach right away -- it doesn't need the response to
    // exist yet, and clicking it while still "Executing turns..." is
    // exactly the "abort an in-progress response" case.
    attachEditButton(_currentUserRow, res.turn_id);
  }
}

function setStatus(text, level){
  document.getElementById("status-label").textContent = text;
  const row = document.getElementById("status-row");
  row.classList.remove("status-ok", "status-busy", "status-err");
  if (level) row.classList.add("status-" + level);
  const dot = document.getElementById("status-dot");
  dot.style.background = level === "ok" ? "var(--green)" : level === "err" ? "var(--red)" :
                         level === "busy" ? "var(--yellow)" : "var(--subtext)";
}

// ── KB Only (prompt-box dropdown) ────────────────────────────────────────────
// In-memory only (state.kbOnly / state.kbSources) -- never written to any
// settings file, so it always starts unchecked on a fresh launch even
// though the popover itself persists fine across tab switches within one
// running session.
function updateKbOnlyBadge(){
  const active = !!(state.kbOnly && state.kbSources.length);
  if (!active) state.explainMode = false;   // Explain Mode can't outlive KB Only being active
  const explainActive = !!(active && state.explainMode);
  const isPageMode = state.explainModeType === "page";
  document.getElementById("kb-only-badge").classList.toggle("show", active);
  document.getElementById("kb-only-badge-text").textContent = explainActive
    ? (isPageMode ? "Explaining — walking through the selected source(s), page by page"
                  : "Explaining — walking through the selected source(s), part by part")
    : "KB Only — internet disabled, answering from selected PDF sources";
  document.getElementById("input-box").classList.toggle("kb-only-active", active);
  document.getElementById("kb-toggle-btn").classList.toggle("active", active);
  const startBtn = document.getElementById("kb-start-explanation-btn");
  const explainHint = document.getElementById("kb-explain-hint");
  const modeRow = document.getElementById("kb-explain-mode-row");
  const modePartBtn = document.getElementById("kb-mode-part-btn");
  const modePageBtn = document.getElementById("kb-mode-page-btn");
  if (startBtn){
    startBtn.style.display = active ? "block" : "none";
    startBtn.innerHTML = icon("play") + (explainActive ? " Restart Explanation" : " Start Explanation");
  }
  if (modeRow) modeRow.classList.toggle("show", active);
  if (modePartBtn) modePartBtn.classList.toggle("active", !isPageMode);
  if (modePageBtn) modePageBtn.classList.toggle("active", isPageMode);
  if (explainHint){
    explainHint.style.display = active ? "block" : "none";
    explainHint.textContent = isPageMode
      ? "Walks through the selected source(s) page by page — say \"next\" to continue."
      : "Walks through the selected source(s) part by part (one sub-sub-heading per response) — say \"next\" to continue.";
  }
  const nextPartBtn = document.getElementById("kb-next-part-btn");
  const explainActionsRow = document.getElementById("kb-explain-actions-row");
  if (explainActionsRow) explainActionsRow.style.display = explainActive ? "flex" : "none";
  if (nextPartBtn){
    nextPartBtn.textContent = isPageMode ? "Next Page \u2192" : "Next Part \u2192";
  }
}

// Fetches the upcoming part's heading name (read-only, doesn't advance
// server-side progress) and drops "Next: {name}" straight into the
// prompt box -- it's just text in the input, so sending it goes through
// the exact same send_message() -> _build_kb_only_context_message path
// as if the user had typed "next" themselves; nothing about the turn's
// internal KB-only/Explain-mode prompt construction is touched here.
async function fillNextPartPrompt(){
  const explainActive = !!(state.kbOnly && state.kbSources.length && state.explainMode);
  if (!explainActive || state.thinking) return;
  const apiName = state.explainModeType === "page" ? "get_explain_next_page_label" : "get_explain_next_part_name";
  const res = await api(apiName, state.kbSources);
  const name = (res && res.ok && res.name) ? res.name : "";
  const input = document.getElementById("msg-input");
  input.value = name ? `Next: ${name}` : "Next";
  autosizeMsgInput();
  input.focus();
}

async function renderKbSourcesList(){
  const list = document.getElementById("kb-sources-list");
  const files = await api("list_pdf_sources");
  if (!files.length){
    list.innerHTML = `<div style="font-size:10px;color:var(--subtext);">No PDF sources yet — add one from the Knowledge tab first.</div>`;
    state.kbSources = [];
    updateKbOnlyBadge();
    return;
  }
  // Nothing is selected by default the first time the list is populated --
  // the user opts in to whichever source(s) they actually want, instead of
  // starting from "everything selected" and having to uncheck the rest.
  list.innerHTML = files.map(f=>{
    const id = "kb-src-" + f.replace(/[^a-zA-Z0-9_]/g, "_");
    const checked = state.kbSources.includes(f) ? "checked" : "";
    return `<div class="kb-src-row"><input type="checkbox" data-src="${f}" id="${id}" ${checked}/><label for="${id}">${f}</label></div>`;
  }).join("");
  list.querySelectorAll("input[data-src]").forEach(cb=>{
    cb.onchange = ()=>{
      const name = cb.dataset.src;
      if (cb.checked){ if (!state.kbSources.includes(name)) state.kbSources.push(name); }
      else { state.kbSources = state.kbSources.filter(n=>n!==name); }
      updateKbOnlyBadge();
    };
  });
  updateKbOnlyBadge();
}

// Restores the KB Only / Explain Mode toggle state saved with a chat (see
// Api.load_chat's "kb_state" field) -- called right after a chat is loaded
// from the sidebar so an in-progress KB/Explain walkthrough picks back up
// with the same source(s) selected and the same mode active, instead of
// requiring the user to re-toggle everything (and re-explain their own
// context) by hand. `kb_state` may be undefined for very old saved chats
// (before this feature existed) or absent entirely -- both are treated as
// "nothing was active", same as state's own defaults.
function applyKbState(kbState){
  kbState = kbState || {};
  state.kbOnly = !!kbState.kb_only;
  state.kbSources = Array.isArray(kbState.kb_sources) ? kbState.kb_sources.slice() : [];
  state.explainMode = !!kbState.explain_mode;
  state.explainModeType = kbState.explain_mode_type === "page" ? "page" : "part";
  const onlyCheckbox = document.getElementById("kb-only-checkbox");
  if (onlyCheckbox) onlyCheckbox.checked = state.kbOnly;
  if (state.kbOnly) renderKbSourcesList(); else updateKbOnlyBadge();
}

function initKbOnlyControls(){
  const toggleBtn = document.getElementById("kb-toggle-btn");
  const popover = document.getElementById("kb-popover");
  const onlyCheckbox = document.getElementById("kb-only-checkbox");

  toggleBtn.onclick = (e)=>{
    e.stopPropagation();
    popover.classList.toggle("open");
    if (popover.classList.contains("open")) renderKbSourcesList();
  };
  document.addEventListener("click", (e)=>{
    if (popover.classList.contains("open") && !popover.contains(e.target) && e.target !== toggleBtn){
      popover.classList.remove("open");
    }
  });
  onlyCheckbox.onchange = ()=>{
    state.kbOnly = onlyCheckbox.checked;
    if (state.kbOnly) renderKbSourcesList(); else updateKbOnlyBadge();
  };

  const modePartBtn = document.getElementById("kb-mode-part-btn");
  const modePageBtn = document.getElementById("kb-mode-page-btn");
  if (modePartBtn){
    modePartBtn.onclick = (e)=>{
      e.stopPropagation();
      state.explainModeType = "part";
      updateKbOnlyBadge();
    };
  }
  if (modePageBtn){
    modePageBtn.onclick = (e)=>{
      e.stopPropagation();
      state.explainModeType = "page";
      updateKbOnlyBadge();
    };
  }

  const startExplainBtn = document.getElementById("kb-start-explanation-btn");
  if (startExplainBtn){
    startExplainBtn.onclick = (e)=>{
      e.stopPropagation();
      if (state.thinking || !state.kbSources.length) return;
      state.explainMode = true;
      updateKbOnlyBadge();
      popover.classList.remove("open");
      const kickoff = "Start the explanation.";
      appendRow("user", kickoff);
      setActiveToolDot(null);
      setStatus("Executing turns... (Explaining)", "busy");
      state.thinking = true;
      api("send_message", kickoff, true, state.kbSources, true, state.explainModeType);
    };
  }

  const nextPartBtn = document.getElementById("kb-next-part-btn");
  if (nextPartBtn){
    nextPartBtn.onclick = (e)=>{
      e.stopPropagation();
      fillNextPartPrompt();
    };
  }

  const openSourceBtn = document.getElementById("kb-open-source-btn");
  if (openSourceBtn){
    openSourceBtn.onclick = async (e)=>{
      e.stopPropagation();
      // Explain Mode can walk multiple selected sources at once, but the
      // button just needs "the" source to view -- open the first selected
      // one, same source the walkthrough is currently narrating.
      if (!(state.kbSources && state.kbSources.length)) return;
      const name = state.kbSources[0];
      let startPageIndex = 0;
      // In Page-by-Page mode, jump straight to the page currently being
      // explained instead of always opening on page 1.
      if (state.explainMode && state.explainModeType === "page"){
        const r = await api("get_explain_current_page_index", state.kbSources);
        if (r && r.ok) startPageIndex = r.page_index;
      }
      openPdfSourceViewer(name, startPageIndex);
    };
  }

  updateKbOnlyBadge();
}

// ── Event bridge from Python (async pushes) -----------------------------
window.__midumEvent = function(evt){
  const {kind, payload} = evt;
  if (kind === "status"){ setStatus(payload.text, payload.level); if (payload.text === "Ready") buildConversationTabs(); }
  else if (kind === "tabs_restored"){
    clearChat();
    renderHistoryWindow(payload.display || []);
    applyKbState(payload.kb_state);
    buildConversationTabs();
    refreshTitlebarInfo();
  }
  else if (kind === "reply"){ appendRow("midum", payload.text); }
  else if (kind === "say"){ appendRow("midum", payload.text); }
  else if (kind === "system_line"){ appendRow("system", payload.text); }
  else if (kind === "error_line"){ appendRow("error", payload.text); }
  else if (kind === "tool_line"){ appendRow("tool", payload.text); }
  else if (kind === "shell_line"){ appendShellLine(payload); }
  else if (kind === "shell_lines"){ appendShellLines(payload.lines || []); }
  else if (kind === "tool_call"){ attachToolCallDetail(payload.name, payload.args, payload.result); }
  else if (kind === "tool_start"){ attachToolCallArgs(payload.name, payload.args); }
  else if (kind === "agent_tool_call"){ appendAgentToolRow(payload.agent, payload.name, payload.args, payload.result); }
  else if (kind === "log"){ appendLog(payload.text); }
  else if (kind === "turn_ready"){
    // Safety-net (re)attach -- covers turns whose only content came in a
    // way that bypassed the inline attach points in sendMessage()/
    // appendRow() (e.g. a reply that arrived with visuals but no plain
    // text chunk first). Idempotent: attachEditButton/attachRegenButton
    // both no-op if the button is already there.
    _pendingTurnId = payload.turn_id;
    if (_currentUserRow) attachEditButton(_currentUserRow, payload.turn_id);
    if (_currentMidumRow) attachRegenButton(_currentMidumRow, payload.turn_id);
  }
  else if (kind === "done"){ state.thinking = false; _mdBusy(false); setActiveToolDot(null); closeToolGroup(); settleStaleToolPills(); _currentMidumRow = null; refreshTitlebarInfo(); buildConversationTabs(); }
  else if (kind === "projects"){ populateProjects(payload.projects, payload.active); }
  else if (kind === "ask"){ appendAsk(payload.id, payload.kind, payload.payload); }
  else if (kind === "mcp_changed"){ if (state.activeTab === "MCP") refreshMcpList(); }
  else if (kind === "agents_changed"){ if (state.activeTab === "Agents") refreshAgentsList(); buildConversationTabs(); }
  else if (kind === "agent_tab_closed_while_viewing"){
    // The agent conversation tab we were previewing just closed (its task
    // finished -- see Api._on_agent_done) -- snap the chat pane back to
    // the real active conversation Python already fell back to, instead
    // of leaving it stuck showing a transcript for a tab that no longer
    // exists. buildConversationTabs() (from the agents_changed event right
    // before this one) has already dropped the closed tab from the strip.
    clearChat();
    renderHistoryWindow(payload.display || []);
    applyKbState(payload.kb_state);
    setViewingAgentTab(null);
  }
  else if (kind === "schedule_ran"){ if (state.activeTab === "Schedule") showToolPane("Schedule"); }
  else if (kind === "tool_result"){ const box=document.getElementById("tool-output"); if(box) box.value = payload.output; }
  else if (kind === "voice_status"){ handleVoiceStatus(payload); }
  else if (kind === "voice_transcript"){ appendVoiceTranscript(payload.role, payload.text); }
  else if (kind === "voice_say"){ _voiceStreamRow = null; appendRow("midum", payload.text); }
  else if (kind === "voice_tool_call"){ appendVoiceToolEvent("call", payload.name, payload.args); }
  else if (kind === "voice_tool_result"){ appendVoiceToolEvent("result", payload.name, payload.result); }
  else if (kind === "voice_interrupted"){ appendVoiceSystemNote("↺ interrupted"); }
  else if (kind === "voice_turn_complete"){ _voiceStreamRow = null; }
  else if (kind === "voice_error"){ appendVoiceSystemNote("⚠ " + payload.message); handleVoiceStatus({status:"stopped"}); }
  else if (kind === "ptt_captured"){ handlePttCaptured(payload); }
  else if (kind === "voice_ptt_state"){ handlePttState(payload); }
  else if (kind === "pdf_cache_warmed"){ if (typeof onPdfCacheWarmed === "function") onPdfCacheWarmed(); }
  else if (typeof kind === "string" && kind.indexOf("editor_") === 0){ mzEdEvent(kind, payload); }
  else if (kind === "render_surface_open"){ openRenderSurface(payload.code, payload.title); }
  else if (kind === "render_surface_update"){ updateRenderSurfaceCode(payload.code); }
  else if (kind === "render_surface_close"){ closeRenderSurfaceUI(); }
  else if (kind === "midumdocs_open"){ openMidumDoc(payload); }
  else if (kind === "midumdocs_changed"){ updateMidumDoc(payload); }
  else if (kind === "midumdocs_close"){ closeMidumDocUI(); }
  else if (kind === "chat_title_updated"){
    // A background model call (see Api._apply_generated_title in
    // gui/app.py) finished naming this chat after the truncated fallback
    // title was already shown. Refresh both places that display it: the
    // custom titlebar label (if this chat is still the active one -- the
    // backend already guards that before ever sending this event, but the
    // payload's chat_id is included in case a future caller wants it) and
    // the sidebar's chat-history list, which reads its titles from disk.
    refreshTitlebarInfo();
    if (typeof refreshHistory === "function") refreshHistory();
    buildConversationTabs();
  }
  else if (kind === "gemini_web_models"){
    // select_provider() returns an instant cached/fallback list for
    // Gemini-web and fetches the real gemini_webapi lineup in the
    // background (see Api.select_provider) -- this event carries that
    // real list once it lands. Only refresh dropdowns that are actually
    // showing Gemini-web right now, so we don't clobber a list the user
    // has since switched away from.
    const providerSel = document.getElementById("provider-select");
    if (providerSel && providerSel.value === "Gemini (Web)"){
      fillModelList(payload.models);
    }
    const settingsProviderSel = document.getElementById("settings-provider");
    if (settingsProviderSel && settingsProviderSel.value === "Gemini (Web)"){
      fillDatalist("settings-model-list", payload.models);
    }
  }
};

function appendLog(text){
  const box = document.getElementById("log-box");
  if (box){ box.insertAdjacentHTML("beforeend", iconizeHtml(escapeHtml(text))); box.scrollTop = box.scrollHeight; }
}

// ── Native modal dialogs (replaces confirm()/prompt()/alert()) -----------
// Every dialog the OS would normally chrome-ify (session reset, project
// creation, MCP add/remove, chat deletion, etc.) is rendered as an
// in-app overlay instead, so it looks and feels like part of Midum rather
// than a browser/Windows popup breaking the illusion.
let _modalKeyHandler = null;

function _closeModal(){
  document.getElementById("modal-overlay").classList.remove("open");
  document.getElementById("modal-box").innerHTML = "";
  if (_modalKeyHandler){
    document.removeEventListener("keydown", _modalKeyHandler);
    _modalKeyHandler = null;
  }
}

function _renderModal(title, bodyHtml, buttons, focusId){
  const overlay = document.getElementById("modal-overlay");
  const box = document.getElementById("modal-box");
  const btnHtml = buttons.map((b, i)=>
    `<button class="modal-btn${b.primary ? " primary" : ""}${b.danger ? " danger" : ""}" data-idx="${i}">${escapeHtml(b.label)}</button>`
  ).join("");
  box.innerHTML = `
    <div class="modal-title">${escapeHtml(title)}</div>
    ${bodyHtml}
    <div class="modal-actions">${btnHtml}</div>
  `;
  buttons.forEach((b, i)=>{
    box.querySelector(`[data-idx="${i}"]`).onclick = b.onClick;
  });
  overlay.classList.add("open");
  if (focusId){
    const el = document.getElementById(focusId);
    if (el){ setTimeout(()=>{ el.focus(); el.select && el.select(); }, 30); }
  }
}

function showAlert(message, title){
  return new Promise(resolve=>{
    _renderModal(title || "Notice", `<div class="modal-msg">${escapeHtml(String(message == null ? "" : message))}</div>`, [
      { label: "OK", primary: true, onClick: ()=>{ _closeModal(); resolve(); } },
    ]);
    _modalKeyHandler = e=>{ if (e.key === "Enter" || e.key === "Escape"){ _closeModal(); resolve(); } };
    document.addEventListener("keydown", _modalKeyHandler);
  });
}

function showConfirm(message, title, opts){
  opts = opts || {};
  return new Promise(resolve=>{
    _renderModal(title || "Confirm", `<div class="modal-msg">${escapeHtml(String(message == null ? "" : message))}</div>`, [
      { label: opts.cancelLabel || "Cancel", onClick: ()=>{ _closeModal(); resolve(false); } },
      { label: opts.okLabel || "OK", primary: !opts.danger, danger: !!opts.danger, onClick: ()=>{ _closeModal(); resolve(true); } },
    ]);
    _modalKeyHandler = e=>{
      if (e.key === "Escape"){ _closeModal(); resolve(false); }
      else if (e.key === "Enter"){ _closeModal(); resolve(true); }
    };
    document.addEventListener("keydown", _modalKeyHandler);
  });
}

function showPrompt(message, title, defaultValue){
  return new Promise(resolve=>{
    const inputId = "modal-input-" + Math.random().toString(36).slice(2);
    const msgHtml = message ? `<div class="modal-msg">${escapeHtml(message)}</div>` : "";
    _renderModal(title || "Input", `${msgHtml}<input type="text" class="modal-input" id="${inputId}" value="${escapeHtml(defaultValue || "")}"/>`, [
      { label: "Cancel", onClick: ()=>{ _closeModal(); resolve(null); } },
      { label: "OK", primary: true, onClick: ()=>{ const v = document.getElementById(inputId).value; _closeModal(); resolve(v); } },
    ], inputId);
    _modalKeyHandler = e=>{
      if (e.key === "Escape"){ _closeModal(); resolve(null); }
      else if (e.key === "Enter"){ const el = document.getElementById(inputId); const v = el ? el.value : ""; _closeModal(); resolve(v); }
    };
    document.addEventListener("keydown", _modalKeyHandler);
  });
}

// Multi-field modal used specifically for adding an MCP server (name +
// transport choice + command-or-url), since that needs more than a single
// text field.
function showMcpAddModal(){
  return new Promise(resolve=>{
    const nameId = "mcp-name-" + Math.random().toString(36).slice(2);
    const cmdId  = "mcp-cmd-"  + Math.random().toString(36).slice(2);
    const urlId  = "mcp-url-"  + Math.random().toString(36).slice(2);
    const radioName = "mcp-transport-" + Math.random().toString(36).slice(2);
    const body = `
      <div class="modal-label">SERVER NAME</div>
      <input type="text" class="modal-input" id="${nameId}" placeholder="e.g. filesystem"/>
      <div class="modal-label">TRANSPORT</div>
      <div class="modal-radio-row">
        <label><input type="radio" name="${radioName}" value="stdio" checked/> Command (stdio)</label>
        <label><input type="radio" name="${radioName}" value="http"/> URL (http)</label>
      </div>
      <div class="modal-label" id="${cmdId}-label">COMMAND</div>
      <input type="text" class="modal-input" id="${cmdId}" placeholder="e.g. npx -y @modelcontextprotocol/server-filesystem"/>
      <div class="modal-label" id="${urlId}-label" style="display:none;">SERVER URL</div>
      <input type="text" class="modal-input" id="${urlId}" placeholder="https://..." style="display:none;"/>
    `;
    _renderModal("Add MCP Server", body, [
      { label: "Cancel", onClick: ()=>{ _closeModal(); resolve(null); } },
      { label: "Connect", primary: true, onClick: ()=>{
          const name = document.getElementById(nameId).value.trim();
          const transport = document.querySelector(`input[name="${radioName}"]:checked`).value;
          const command = document.getElementById(cmdId).value.trim();
          const url = document.getElementById(urlId).value.trim();
          _closeModal();
          resolve({ name, transport, command, url });
        } },
    ], nameId);
    document.querySelectorAll(`input[name="${radioName}"]`).forEach(r=>{
      r.onchange = ()=>{
        const isStdio = r.value === "stdio" && r.checked;
        const anyChecked = document.querySelector(`input[name="${radioName}"]:checked`).value;
        const showCmd = anyChecked === "stdio";
        document.getElementById(cmdId).style.display = showCmd ? "" : "none";
        document.getElementById(`${cmdId}-label`).style.display = showCmd ? "" : "none";
        document.getElementById(urlId).style.display = showCmd ? "none" : "";
        document.getElementById(`${urlId}-label`).style.display = showCmd ? "none" : "";
      };
    });
    _modalKeyHandler = e=>{ if (e.key === "Escape"){ _closeModal(); resolve(null); } };
    document.addEventListener("keydown", _modalKeyHandler);
  });
}

// Tools pane opened from the MCP tab's "Tools" button — lists every tool
// on that server with Promote/Demote controls. Promoting a tool includes
// its full schema alongside Midum's native tools so the model can call it
// directly, without the usual show_server_tools()/call_mcp_tool() discovery
// hop. Built as its own function (not via _renderModal) because it needs
// live re-fetch-and-redraw on every Promote/Demote click, not a single
// submit-and-close interaction.
async function showMcpToolsPane(serverName){
  const overlay = document.getElementById("modal-overlay");
  const box = document.getElementById("modal-box");
  box.classList.add("wide");
  box.innerHTML = `
    <div class="modal-title">Tools — ${escapeHtml(serverName)}</div>
    <div id="mcp-tools-body" style="max-height:50vh;overflow-y:auto;margin:8px 0;"></div>
    <div class="modal-actions"><button class="modal-btn primary" id="mcp-tools-close">Close</button></div>
  `;
  overlay.classList.add("open");
  const bodyEl = document.getElementById("mcp-tools-body");
  bodyEl.innerHTML = `<div style="font-size:11px;color:var(--subtext);padding:10px;">Loading tools...</div>`;

  async function refresh(){
    const r = await api("list_mcp_tools_for_promotion", serverName);
    if (!r.ok){
      bodyEl.innerHTML = `<div style="font-size:11px;color:var(--red);padding:10px;">${escapeHtml(r.error || "Failed to load tools.")}</div>`;
      return;
    }
    if (!r.tools.length){
      bodyEl.innerHTML = `<div style="font-size:11px;color:var(--subtext);padding:10px;">This server exposes no tools.</div>`;
      return;
    }
    bodyEl.innerHTML = r.tools.map(t => `
      <div class="mcp-tool-row" data-tool="${escapeHtml(t.name)}">
        <div class="mcp-tool-info">
          <div class="mcp-tool-name">${escapeHtml(t.name)}</div>
          ${t.desc ? `<div class="mcp-tool-desc">${escapeHtml(t.desc)}</div>` : ""}
        </div>
        <div class="mcp-tool-actions">
          <button class="mini-btn${t.promoted ? " open" : ""}" data-act="promote" ${t.promoted ? "disabled" : ""}>Promote</button>
          <button class="mini-btn del" data-act="demote" ${t.promoted ? "" : "disabled"}>Demote</button>
        </div>
      </div>`).join("");
  }
  await refresh();

  bodyEl.onclick = async (e)=>{
    const btn = e.target.closest("[data-act]");
    if (!btn || btn.disabled) return;
    const row = btn.closest("[data-tool]");
    const toolName = row.dataset.tool;
    btn.disabled = true;
    if (btn.dataset.act === "promote"){
      await api("promote_mcp_tool", serverName, toolName);
    } else {
      await api("demote_mcp_tool", serverName, toolName);
    }
    await refresh();
  };

  function doClose(){
    box.classList.remove("wide");
    _closeModal();
  }
  document.getElementById("mcp-tools-close").onclick = doClose;
  _modalKeyHandler = e=>{ if (e.key === "Escape" || e.key === "Enter"){ doClose(); } };
  document.addEventListener("keydown", _modalKeyHandler);
}

// Saved Render Surfaces pane -- lets the user bring back a previously
// saved Render Surface (see save_render_surface/list_saved_render_surfaces/
// load_render_surface/delete_saved_render_surface in
// tools/render_surface_tools.py) without going through the model at all.
// Lives as its own top-level tab (see TABS/showToolPane) rather than a
// Settings option, since browsing/loading/deleting saved surfaces is a
// primary action, not a preference. Built as its own function for the
// same reason as buildMcpPane: it needs live re-fetch-and-redraw after
// every Load/Delete click.
function buildSurfacesPane(box){
  box.innerHTML = `
    <div class="hdr-row"><div class="section-label">SAVED RENDER SURFACES</div></div>
    <div id="render-surfaces-body" style="flex:1;overflow-y:auto;margin-top:6px;"></div>
  `;
  const bodyEl = document.getElementById("render-surfaces-body");
  bodyEl.innerHTML = `<div style="font-size:11px;color:var(--subtext);padding:10px;">Loading...</div>`;

  async function refresh(){
    const r = await api("render_surface_list_saved");
    if (!r.ok){
      bodyEl.innerHTML = `<div style="font-size:11px;color:var(--red);padding:10px;">${escapeHtml(r.error || "Failed to load saved surfaces.")}</div>`;
      return;
    }
    if (!r.items.length){
      bodyEl.innerHTML = `<div style="font-size:11px;color:var(--subtext);padding:10px;">No saved Render Surfaces yet. Open one and click ${icon("save")} in its header to save it.</div>`;
      return;
    }
    bodyEl.innerHTML = r.items.map(it => `
      <div class="mcp-tool-row" data-name="${escapeHtml(it.name)}">
        <div class="mcp-tool-info">
          <div class="mcp-tool-name">${escapeHtml(it.title || it.name)}</div>
          <div class="mcp-tool-desc">${escapeHtml(it.name)}${it.saved_at ? " · saved " + escapeHtml(it.saved_at) : ""}</div>
        </div>
        <div class="mcp-tool-actions">
          <button class="mini-btn open" data-act="load">Load</button>
          <button class="mini-btn del" data-act="delete">Delete</button>
        </div>
      </div>`).join("");
  }
  refresh();

  bodyEl.onclick = async (e)=>{
    const btn = e.target.closest("[data-act]");
    if (!btn || btn.disabled) return;
    const row = btn.closest("[data-name]");
    const name = row.dataset.name;
    btn.disabled = true;
    if (btn.dataset.act === "load"){
      const r = await api("render_surface_load_ui", name);
      if (r.ok){ switchTab("Chat"); }
      else { await showConfirm(r.message || "Failed to load.", "Error"); refresh(); }
      return;
    }
    const ok = await showConfirm(`Delete saved Render Surface "${name}"? This can't be undone.`, "Delete", {danger:true, okLabel:"Delete"});
    if (ok){ await api("render_surface_delete_ui", name); }
    refresh();
  };
}

// ── Sidebar -------------------------------------------------------------
function buildSidebar(){
  const el = document.getElementById("sidebar-inner");
  el.innerHTML = `
    <div id="sidebar-main-view">
      <button class="ghost-btn" id="sidebar-surfaces-btn" style="width:100%;text-align:left;">${icon("image")} Render Surfaces</button>
      <div class="divider"></div>
      <div class="hdr-row">
        <div class="section-label">WORKSPACE</div>
        <button class="icon-btn" style="width:26px;height:26px;font-size:11px;" id="sidebar-close">${icon("x")}</button>
      </div>
      <button class="btn" id="new-session-btn">New Chat</button>
      <select id="project-select"></select>
      <div class="btn-row">
        <button class="ghost-btn" id="proj-new">+ Project</button>
        <button class="ghost-btn" id="proj-remove">${icon("trash")} Remove</button>
        <button class="ghost-btn" id="proj-code">${icon("code")} Code</button>
      </div>
      <div id="file-list"></div>
      <div class="divider"></div>
      <div class="section-label">CHAT HISTORY</div>
      <div id="history-list"></div>
      <div class="divider"></div>
      <div class="hdr-row">
        <div class="section-label">SETTINGS</div>
        <button class="icon-btn" style="width:22px;height:22px;font-size:10px;" id="settings-toggle">${icon("settings")}</button>
      </div>
      <div id="sidebar-footer">
        <button class="ghost-btn" id="proj-term">${icon("terminal")} Terminal</button>
        <button class="ghost-btn" id="shutdown-btn" style="color:var(--red);">${icon("power")} Shutdown</button>
      </div>
    </div>
    <div id="sidebar-settings-overlay">
      <div class="hdr-row">
        <button id="settings-back-btn">${icon("arrow-left")}</button>
        <div class="section-label">SETTINGS</div>
        <div style="width:26px;"></div>
      </div>
      <div class="field-label" style="margin:2px 0 0;">PANELS</div>
      <div class="btn-row" id="settings-panel-toggle">
        <button class="ghost-btn" data-panel="Log" style="flex:1;">${icon("file-text")} Logs</button>
        <button class="ghost-btn" data-panel="Parameters" style="flex:1;">${icon("settings")} Parameters</button>
      </div>
      <div class="field-label" style="margin:2px 0 0;">THEME</div>
      <div class="btn-row" id="settings-theme-toggle">
        <button class="ghost-btn" data-theme="dark" style="flex:1;">${icon("moon")} Dark</button>
        <button class="ghost-btn" data-theme="light" style="flex:1;">${icon("sun")} Light</button>
      </div>
      <div class="field-label" style="margin:4px 0 0;">PRESETS</div>
      <div class="btn-row">
        <select id="settings-preset-select" style="flex:1;"><option value="">Choose a preset…</option></select>
        <button class="ghost-btn" id="settings-preset-delete" style="flex:0 0 auto;display:none;" title="Delete preset">${icon("x")}</button>
      </div>
      <button class="ghost-btn" id="settings-preset-save" style="width:100%;">+ Save current as preset</button>
      <div class="hdr-row" style="margin-top:4px;">
        <div class="field-label" style="margin:0;">AMBIENT BLOBS</div>
        <label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--subtext);">
          <input type="checkbox" id="settings-blobs-enabled" style="width:14px;height:14px;"/> Enabled
        </label>
      </div>
      <div id="blob-individual-toggles" style="display:flex;flex-wrap:wrap;gap:8px 14px;margin:-2px 0 2px;">
        <label style="display:flex;align-items:center;gap:5px;font-size:10px;color:var(--subtext);">
          <input type="checkbox" id="settings-blob-center-enabled" checked style="width:12px;height:12px;"/> Center
        </label>
        <label style="display:flex;align-items:center;gap:5px;font-size:10px;color:var(--subtext);">
          <input type="checkbox" id="settings-blob-a-enabled" checked style="width:12px;height:12px;"/> Blob A
        </label>
        <label style="display:flex;align-items:center;gap:5px;font-size:10px;color:var(--subtext);">
          <input type="checkbox" id="settings-blob-b-enabled" checked style="width:12px;height:12px;"/> Blob B
        </label>
        <label style="display:flex;align-items:center;gap:5px;font-size:10px;color:var(--subtext);">
          <input type="checkbox" id="settings-blob-cursor-enabled" checked style="width:12px;height:12px;"/> Cursor
        </label>
      </div>
      <div class="hdr-row" style="margin-top:4px;">
        <div class="field-label" style="margin:0;">BACKGROUND IMAGE</div>
        <label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--subtext);">
          <input type="checkbox" id="settings-bg-enabled" style="width:14px;height:14px;"/> Enabled
        </label>
      </div>
      <div class="btn-row">
        <button class="ghost-btn" id="bg-choose" style="flex:1;">${icon("image")} Choose Image...</button>
        <button class="ghost-btn" id="bg-clear" style="flex:0 0 auto;">${icon("x")}</button>
      </div>
      <div id="bg-filename" style="font-size:9px;color:var(--subtext);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"></div>
      <div class="field-label" style="margin-top:2px;">Brightness</div>
      <input type="range" id="bg-brightness" min="40" max="160" value="100" style="width:100%;"/>
      <div class="field-label">Blur</div>
      <input type="range" id="bg-blur" min="0" max="40" value="0" style="width:100%;"/>
      <div class="field-label">Opacity</div>
      <input type="range" id="bg-opacity" min="10" max="100" value="100" style="width:100%;"/>
      <div class="hdr-row" style="margin-top:4px;">
        <div class="field-label" style="margin:0;">GLASS SURFACES</div>
        <label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--subtext);">
          <input type="checkbox" id="settings-glass-enabled" style="width:14px;height:14px;"/> Enabled
        </label>
      </div>
      <div id="glass-hint" style="font-size:9px;color:var(--subtext);margin:-4px 0 2px;">
        Needs ambient blobs or a background image turned on.
      </div>
      <div class="field-label" style="margin-top:2px;">Glass Opacity</div>
      <input type="range" id="settings-glass-opacity" min="35" max="75" value="55" style="width:100%;"/>
      <label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--subtext);margin-top:4px;">
        <input type="checkbox" id="settings-glass-sheen-enabled" checked style="width:14px;height:14px;"/> Frosted highlight
      </label>
      <div class="hdr-row" style="margin-top:4px;">
        <div class="field-label" style="margin:0;">AMBIENT SURFACES</div>
        <label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--subtext);">
          <input type="checkbox" id="settings-ambient-hide-capture" checked style="width:14px;height:14px;"/> Hide from screenshots
        </label>
      </div>
      <div style="font-size:9px;color:var(--subtext);margin:-4px 0 2px;">
        When on, floating widgets are invisible to Midum's own screenshots, OCR and screen-share (you still see them). Windows 10 2004+.
      </div>
      <div class="hdr-row" style="margin-top:4px;">
        <div class="field-label" style="margin:0;">CHAT TEXT SIZE</div>
        <button class="ghost-btn" id="settings-chat-fs-reset" style="flex:0 0 auto;height:22px;font-size:10px;padding:0 8px;" title="Reset chat text size to default">Reset</button>
      </div>
      <div class="chat-fs-row">
        <input type="range" id="settings-chat-fs" min="10" max="32" step="0.05" value="15" aria-label="Chat text size"/>
        <input type="number" id="settings-chat-fs-num" min="10" max="32" step="0.05" value="15.00" aria-label="Chat text size in pixels"/>
        <span class="chat-fs-unit">px</span>
      </div>
      <div style="font-size:9px;color:var(--subtext);margin:-2px 0 2px;">
        Main chat window only (messages, tool calls, code, prompt box). Drag, type an exact value, or use the arrow keys: 0.05 px per step, Shift = 1 px.
      </div>
      <div class="field-label">DEFAULT PROVIDER</div>
      <select id="settings-provider"></select>
      <div class="field-label">DEFAULT MODEL</div>
      <input list="settings-model-list" id="settings-model" style="height:32px;border-radius:16px;border:1px solid var(--border2);background:var(--surface);color:var(--text);padding:0 10px;"/>
      <datalist id="settings-model-list"></datalist>
      <div class="field-label">COLORS</div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;">
        <label style="display:flex;flex-direction:column;align-items:center;font-size:9px;color:var(--subtext);gap:2px;">Accent<input type="color" id="settings-color-accent" style="width:32px;height:24px;padding:0;border:none;background:none;"/></label>
        <label style="display:flex;flex-direction:column;align-items:center;font-size:9px;color:var(--subtext);gap:2px;">Accent 2<input type="color" id="settings-color-accent2" style="width:32px;height:24px;padding:0;border:none;background:none;"/></label>
        <label style="display:flex;flex-direction:column;align-items:center;font-size:9px;color:var(--subtext);gap:2px;">Background<input type="color" id="settings-color-bg" style="width:32px;height:24px;padding:0;border:none;background:none;"/></label>
        <label style="display:flex;flex-direction:column;align-items:center;font-size:9px;color:var(--subtext);gap:2px;">Panel<input type="color" id="settings-color-panel" style="width:32px;height:24px;padding:0;border:none;background:none;"/></label>
        <label style="display:flex;flex-direction:column;align-items:center;font-size:9px;color:var(--subtext);gap:2px;">Text<input type="color" id="settings-color-text" style="width:32px;height:24px;padding:0;border:none;background:none;"/></label>
        <label style="display:flex;flex-direction:column;align-items:center;font-size:9px;color:var(--subtext);gap:2px;">User bubble<input type="color" id="settings-color-user_msg" style="width:32px;height:24px;padding:0;border:none;background:none;"/></label>
      </div>
      <div class="field-label">BLOB COLORS</div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;">
        <label style="display:flex;flex-direction:column;align-items:center;font-size:9px;color:var(--subtext);gap:2px;">Center<input type="color" id="settings-color-blob_center" style="width:32px;height:24px;padding:0;border:none;background:none;"/></label>
        <label style="display:flex;flex-direction:column;align-items:center;font-size:9px;color:var(--subtext);gap:2px;">Blob A<input type="color" id="settings-color-blob_a" style="width:32px;height:24px;padding:0;border:none;background:none;"/></label>
        <label style="display:flex;flex-direction:column;align-items:center;font-size:9px;color:var(--subtext);gap:2px;">Blob B<input type="color" id="settings-color-blob_b" style="width:32px;height:24px;padding:0;border:none;background:none;"/></label>
        <label style="display:flex;flex-direction:column;align-items:center;font-size:9px;color:var(--subtext);gap:2px;">Cursor<input type="color" id="settings-color-blob_cursor" style="width:32px;height:24px;padding:0;border:none;background:none;"/></label>
      </div>
      <div class="btn-row" style="margin-top:4px;">
        <button class="ghost-btn" id="settings-reset">Reset defaults</button>
        <button class="btn" id="settings-save" style="background:var(--accent);color:#fff;">Save</button>
      </div>
      <div id="settings-status" style="font-size:9px;color:var(--subtext);"></div>
    </div>
  `;
  document.getElementById("sidebar-close").onclick = toggleSidebar;
  enhanceSelect(document.getElementById("settings-preset-select"));
  document.getElementById("sidebar-surfaces-btn").onclick = ()=>{
    // Jumps straight to the Surfaces tool pane (formerly a top tab, now a
    // sidebar shortcut -- see the TABS comment above). switchTab doesn't
    // require an entry in TABS; it just needs a matching case in
    // showToolPane, so this works exactly like clicking a tab did.
    toggleSidebar();
    switchTab("Surfaces");
  };
  enhanceSelect(document.getElementById("project-select"));
  enhanceSelect(document.getElementById("settings-provider"));
  attachLitellmPicker({ providerSelId: "settings-provider", modelInputId: "settings-model", prefix: "st" });
  document.getElementById("new-session-btn").onclick = async ()=>{
    await api("new_session"); clearChat(); applyKbState(null); refreshHistory(); refreshTitlebarInfo(); buildConversationTabs();
  };
  document.getElementById("project-select").onchange = async (e)=>{
    const info = await api("switch_project", e.target.value);
    renderFileList(info);
  };
  document.getElementById("proj-new").onclick = async ()=>{
    const r = await api("add_workspace_folder");
    if (!r.ok) { if (r.error) showAlert(r.error, "Error"); return; }
    populateProjects(r.projects, r.projects[r.projects.length - 1]);
  };
  document.getElementById("proj-remove").onclick = async ()=>{
    const sel = document.getElementById("project-select");
    const current = sel && sel.value;
    if (!current) return;
    const ok = await showConfirm(`Remove "${current.split(/[\\/]/).pop()}" from the workspace list? (folder itself is untouched)`, "Remove Workspace", {danger:true, okLabel:"Remove"});
    if (!ok) return;
    const r = await api("remove_workspace_folder", current);
    if (r.ok) populateProjects(r.projects);
  };
  document.getElementById("proj-code").onclick = ()=>api("open_project_in_vscode");
  document.getElementById("proj-term").onclick = ()=>api("open_project_terminal");
  document.getElementById("shutdown-btn").onclick = async ()=>{
    const ok = await showConfirm("Shut down Midum engine?", "Shutdown", {danger:true, okLabel:"Shutdown"});
    if (ok) await api("shutdown");
  };

  document.getElementById("settings-toggle").onclick = ()=>{
    // Settings takes over the ENTIRE sidebar pane (an overlay covering the
    // whole thing), rather than a small strip squeezed in below workspace
    // + history -- there's a dedicated back button to return.
    document.getElementById("sidebar-settings-overlay").classList.add("open");
    loadSettingsPanel();
  };
  document.getElementById("settings-back-btn").onclick = ()=>{
    document.getElementById("sidebar-settings-overlay").classList.remove("open");
  };
  // "Log" and "Parameters" are opened from here rather than from the tab
  // bar. Clicking one switches to that pane (closing the settings overlay
  // + sidebar so the pane is visible); clicking the already-active one
  // toggles back to Chat instead, so the buttons behave like on/off
  // toggles rather than one-way links.
  document.querySelectorAll('#settings-panel-toggle [data-panel]').forEach(btn=>{
    btn.onclick = ()=>{
      const panel = btn.dataset.panel;
      const next = state.activeTab === panel ? "Chat" : panel;
      switchTab(next);
      document.getElementById("sidebar-settings-overlay").classList.remove("open");
      if (state.sidebarOpen) toggleSidebar();
    };
  });
  document.getElementById("settings-save").onclick = saveSettingsPanel;
  document.getElementById("settings-reset").onclick = ()=>{
    // Reset to the ACTIVE theme's defaults (not always Dark), so resetting
    // while in Light mode gives back Light's real colors. Blob colors
    // aren't theme-dependent (THEME_VARS doesn't touch them), so they
    // always come from DEFAULT_COLORS regardless of active theme.
    const defaults = {...DEFAULT_COLORS, ...(THEME_VARS[_activeTheme] || {}), user_msg: (THEME_VARS[_activeTheme] || {})["user-msg"] || DEFAULT_COLORS.user_msg};
    _themeColors[_activeTheme] = defaults;
    applyColors(defaults);
    Object.entries(defaults).forEach(([k,v])=>{
      const el = document.getElementById(`settings-color-${k}`);
      if (el) el.value = v;
    });
  };
  document.getElementById("settings-provider").onchange = async (e)=>{
    const r = await api("select_provider", e.target.value);
    fillDatalist("settings-model-list", r.models);
    document.getElementById("settings-model").value = r.default_model;
  };
  document.querySelectorAll('#settings-theme-toggle [data-theme]').forEach(btn=>{
    btn.onclick = ()=>applyTheme(btn.dataset.theme);
  });
  document.getElementById("settings-preset-save").onclick = saveCurrentAsPreset;
  document.getElementById("settings-preset-select").onchange = (e)=>{
    if (e.target.value) applyPreset(e.target.value);
    updatePresetDeleteVisibility();
  };
  document.getElementById("settings-preset-delete").onclick = async ()=>{
    const sel = document.getElementById("settings-preset-select");
    const preset = _presets[sel.value];
    if (!preset || preset.builtin) return;
    const ok = await showConfirm(`Delete saved preset "${preset.label}"? This can't be undone.`, "Delete Preset", {danger:true, okLabel:"Delete"});
    if (!ok) return;
    await api("delete_preset", preset.label);
    refreshPresets();
  };
  document.getElementById("settings-bg-enabled").onchange = (e)=>{
    _bgState.cfg.enabled = e.target.checked;
    applyBgImage(_bgState.cfg, _bgState.dataUrl);
  };
  document.getElementById("settings-blobs-enabled").onchange = (e)=>{
    applyBlobsEnabled(e.target.checked);
    _themeBlobsEnabled[_activeTheme] = e.target.checked;
  };
  ["center","a","b","cursor"].forEach(key=>{
    const cb = document.getElementById(`settings-blob-${key}-enabled`);
    if (cb) cb.onchange = (e)=> applyBlobIndividual(key, e.target.checked);
  });
  document.getElementById("settings-glass-enabled").onchange = (e)=>{
    applyLiquidGlass(e.target.checked);
  };
  document.getElementById("settings-glass-opacity").oninput = (e)=>{
    applyGlassOpacity(e.target.value);
  };
  {
    // Chat Text Size: slider + exact number box + Reset. Repaints live, and
    // persists on its own (debounced) so it doesn't depend on the Save button.
    const fsSlider = document.getElementById("settings-chat-fs");
    const fsNum = document.getElementById("settings-chat-fs-num");
    const fsReset = document.getElementById("settings-chat-fs-reset");
    let fsSaveTimer = null;
    const live = (v)=>{
      applyChatFontSize(v);
      clearTimeout(fsSaveTimer);
      fsSaveTimer = setTimeout(()=>{ api("set_chat_font_size", _chatFontSize); }, 350);
    };
    const nudge = (e)=>{
      // Shift+Arrow = 1 px jumps; plain arrows keep the native 0.05 px step.
      if (!e.shiftKey || (e.key !== "ArrowUp" && e.key !== "ArrowDown" && e.key !== "ArrowLeft" && e.key !== "ArrowRight")) return;
      e.preventDefault();
      const dir = (e.key === "ArrowUp" || e.key === "ArrowRight") ? 1 : -1;
      live(_chatFontSize + dir);
      if (fsNum) fsNum.value = _chatFontSize.toFixed(2);
    };
    if (fsSlider){
      fsSlider.value = _chatFontSize;
      fsSlider.oninput = (e)=> live(e.target.value);
      fsSlider.onkeydown = nudge;
    }
    if (fsNum){
      fsNum.value = _chatFontSize.toFixed(2);
      fsNum.oninput = (e)=>{
        const n = Number(e.target.value);
        if (e.target.value !== "" && isFinite(n) && n >= CHAT_FS_MIN && n <= CHAT_FS_MAX) live(n);
      };
      fsNum.onchange = (e)=>{ live(e.target.value); e.target.value = _chatFontSize.toFixed(2); };
      fsNum.onkeydown = nudge;
    }
    if (fsReset){
      fsReset.onclick = ()=>{ live(CHAT_FS_DEFAULT); if (fsNum) fsNum.value = _chatFontSize.toFixed(2); };
    }
  }
  document.getElementById("settings-glass-sheen-enabled").onchange = (e)=>{
    applyGlassSheen(e.target.checked);
  };
  document.getElementById("settings-ambient-hide-capture").onchange = (e)=>{
    applyAmbientHideCapture(e.target.checked);
    // Live-apply to any open widgets now; Save persists it for next launch.
    api("set_ambient_hide_from_capture", e.target.checked);
  };
  document.getElementById("bg-choose").onclick = async ()=>{
    const r = await api("pick_background_image");
    if (!r.ok){ if (r.error) showAlert(r.error, "Error"); return; }
    _bgState.cfg = r.settings.bg_image;
    _bgState.dataUrl = r.data_url;
    applyBgImage(_bgState.cfg, _bgState.dataUrl);
  };
  document.getElementById("bg-clear").onclick = async ()=>{
    const r = await api("clear_background_image");
    if (!r.ok) return;
    _bgState.cfg = r.settings.bg_image;
    _bgState.dataUrl = null;
    applyBgImage(_bgState.cfg, _bgState.dataUrl);
  };
  // Sliders trigger a debounced server-side re-bake (Pillow) rather than
  // a live CSS filter -- the checkbox toggle just swaps the already-baked
  // image in/out, which is instant and doesn't need a round trip.
  ['bg-brightness','bg-blur','bg-opacity'].forEach(id=>{
    document.getElementById(id).oninput = (e)=>{
      const key = id === 'bg-brightness' ? 'brightness' : id === 'bg-blur' ? 'blur' : 'opacity';
      _bgState.cfg[key] = Number(e.target.value);
      _scheduleBgPreview();
    };
  });
}

const DEFAULT_COLORS = {
  accent:"#60a5fa", accent2:"#1d4ed8", bg:"#05070c", panel:"#0b0f19", text:"#f3f4f6",
  user_msg:"#0f1e33",
  blob_center:"#60a5fa", blob_a:"#f472b6", blob_b:"#34d399", blob_cursor:"#a78bfa",
};

// Full palette per theme — mirrors the website's (index.html) light/dark
// color scheme exactly, and covers every CSS var, not just the 5
// user-editable swatches, so Light mode actually looks light (panes,
// borders, bubbles, tool console, etc.) rather than just re-tinting a
// couple of accent colors on a black background.
const THEME_VARS = {
  dark: {
    bg:"#05070c", panel:"#0b0f19", surface:"#0d1220", surface2:"#121a2c",
    border:"#141a26", border2:"#1f2937",
    accent:"#60a5fa", "accent-dim":"#3b82f6", "accent-faint":"#0f1e33", accent2:"#1d4ed8",
    text:"#f3f4f6", subtext:"#9ca3af", muted:"#4b5563",
    "user-msg":"#0f1e33", "midum-msg":"#0a0e17",
    "tool-bg":"#05070c", "tool-text":"#93c5fd",
  },
  light: {
    bg:"#f7f9fc", panel:"#ffffff", surface:"#eef2f9", surface2:"#e2e8f0",
    border:"#e4e7ec", border2:"#d7dbe3",
    accent:"#1d4ed8", "accent-dim":"#1e40af", "accent-faint":"#dbeafe", accent2:"#3730a3",
    text:"#10182b", subtext:"#4b5563", muted:"#94a3b8",
    "user-msg":"#dbeafe", "midum-msg":"#eef2f9",
    "tool-bg":"#eef2f9", "tool-text":"#1d4ed8",
  },
};

let _activeTheme = "dark";

// Each theme (dark/light) remembers its own saved custom-color preset AND
// its own ambient-blobs on/off preference. Populated from get_settings()
// on load/boot, updated whenever the user saves the settings panel.
// applyTheme() layers whichever bucket matches the active theme on top of
// that theme's base palette, so toggling Dark/Light auto-restores each
// theme's own saved colors and blobs setting instead of always showing
// the stock/shared values.
let _themeColors = { dark: {...DEFAULT_COLORS}, light: {...DEFAULT_COLORS} };
let _themeBlobsEnabled = { dark: true, light: true };

// Custom savable presets, on top of the base Dark/Light toggle. Each preset
// is a full editable-color bundle (see DEFAULT_COLORS keys) plus which base
// theme it layers onto and its own ambient-blobs preference. "Professional
// Dark/Light" ship built in (read-only, neutral graphite/slate palettes with
// no blue accent, blobs off); anything else here was saved by the user via
// "+ Save current as preset" and can be renamed/deleted freely.
// Populated from get_presets() on load; re-fetched after any save/delete.
let _presets = {};

async function refreshPresets(){
  _presets = await api("get_presets");
  renderPresetList();
}

// PRESETS lives as a single dropdown (rather than a button per preset) so
// it reads as one compact field alongside the Light/Dark toggle above it
// instead of a growing list of buttons -- picking a preset is a one-shot
// "apply this" action, which a <select> communicates more cleanly than a
// bank of ghost-buttons. Built-ins are starred, user-saved ones get a
// pin, and the delete button (only meaningful for user-saved presets)
// appears next to the dropdown once one of those is selected.
function renderPresetList(){
  const sel = document.getElementById("settings-preset-select");
  const delBtn = document.getElementById("settings-preset-delete");
  if (!sel) return;
  const prevValue = sel.value;
  sel.innerHTML = `<option value="">Choose a preset\u2026</option>`;
  Object.entries(_presets).forEach(([id, preset])=>{
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = (preset.builtin ? "\u2726 " : "\u2b50 ") + preset.label;
    sel.appendChild(opt);
  });
  // Re-select whatever was chosen before the refresh (e.g. after a save),
  // falling back to the placeholder if that preset no longer exists.
  sel.value = _presets[prevValue] ? prevValue : "";
  updatePresetDeleteVisibility();
}

function updatePresetDeleteVisibility(){
  const sel = document.getElementById("settings-preset-select");
  const delBtn = document.getElementById("settings-preset-delete");
  if (!sel || !delBtn) return;
  const preset = _presets[sel.value];
  delBtn.style.display = (preset && !preset.builtin) ? "" : "none";
}

// Applying a preset switches to its base theme, layers its colors + blobs
// preference on top (mirroring what "Reset defaults" already does for the
// stock palette), and marks the active theme's in-memory bucket as this
// preset's colors -- exactly like editing swatches by hand, so it previews
// immediately and is persisted normally by hitting "Save" afterward.
function applyPreset(id){
  const preset = _presets[id];
  if (!preset) return;
  applyTheme(preset.base);
  _themeColors[preset.base] = {...DEFAULT_COLORS, ...preset.colors};
  _themeBlobsEnabled[preset.base] = preset.blobs_enabled !== false;
  applyColors(_themeColors[preset.base]);
  applyBlobsEnabled(_themeBlobsEnabled[preset.base]);
  const blobsCb = document.getElementById("settings-blobs-enabled");
  if (blobsCb) blobsCb.checked = _themeBlobsEnabled[preset.base];
  Object.entries(_themeColors[preset.base]).forEach(([k,v])=>{
    const colorEl = document.getElementById(`settings-color-${k}`);
    if (colorEl) colorEl.value = v;
  });
}

async function saveCurrentAsPreset(){
  const name = await showPrompt("Name this preset:", "Save Preset");
  if (!name) return;
  const colors = {};
  ["accent","accent2","bg","panel","text","user_msg","blob_center","blob_a","blob_b","blob_cursor"].forEach(k=>{
    const el = document.getElementById(`settings-color-${k}`);
    if (el) colors[k] = el.value;
  });
  const blobsEnabled = document.getElementById("settings-blobs-enabled").checked;
  const r = await api("save_preset", name.trim(), _activeTheme, colors, blobsEnabled);
  const status = document.getElementById("settings-status");
  if (r.ok){
    if (status) status.textContent = `Preset "${name.trim()}" saved.`;
    refreshPresets();
  } else if (status) {
    status.textContent = `Error: ${r.error}`;
  }
}

function applyTheme(name){
  const vars = THEME_VARS[name] || THEME_VARS.dark;
  const root = document.documentElement.style;
  Object.entries(vars).forEach(([k,v])=> root.setProperty(`--${k}`, v));
  _activeTheme = name;
  // Apply this theme's saved custom-color preset on top of its base palette.
  applyColors(_themeColors[name]);
  // Likewise restore this theme's own ambient-blobs preference.
  applyBlobsEnabled(_themeBlobsEnabled[name] !== false);
  document.querySelectorAll('#settings-theme-toggle [data-theme]').forEach(b=>{
    b.classList.toggle("active", b.dataset.theme === name);
    b.style.background = b.dataset.theme === name ? "var(--accent)" : "transparent";
    b.style.color = b.dataset.theme === name ? "#fff" : "var(--text)";
  });
  // Keep the custom color-picker swatches in the settings panel in sync
  // with whichever theme is now active, so switching to Light mode shows
  // that theme's own saved preset instead of stale values left over from
  // Dark (or the stock palette, if nothing was customized for it yet).
  const merged = {...vars, user_msg: vars["user-msg"], ...(_themeColors[name] || {})};
  ["accent","accent2","bg","panel","text","user_msg","blob_center","blob_a","blob_b","blob_cursor"].forEach(k=>{
    const el = document.getElementById(`settings-color-${k}`);
    if (el && merged[k]) el.value = merged[k];
  });
}

function applyColors(colors){
  if (!colors) return;
  const root = document.documentElement.style;
  if (colors.accent) root.setProperty("--accent", colors.accent);
  if (colors.accent2) root.setProperty("--accent2", colors.accent2);
  if (colors.bg) root.setProperty("--bg", colors.bg);
  if (colors.panel) root.setProperty("--panel", colors.panel);
  if (colors.text) root.setProperty("--text", colors.text);
  if (colors.user_msg) root.setProperty("--user-msg", colors.user_msg);
  if (colors.blob_center) root.setProperty("--blob-center", colors.blob_center);
  if (colors.blob_a) root.setProperty("--blob-a", colors.blob_a);
  if (colors.blob_b) root.setProperty("--blob-b", colors.blob_b);
  if (colors.blob_cursor) root.setProperty("--blob-cursor", colors.blob_cursor);
}

function applyBgImage(cfg, dataUrl){
  const layer = document.getElementById("bg-image-layer");
  const on = !!(cfg && cfg.enabled && dataUrl);
  document.documentElement.classList.toggle("has-bg-image", on);
  if (layer){
    // dataUrl already has brightness/blur/opacity baked into its pixels
    // server-side -- no CSS filter or opacity assignment here, so there
    // is nothing for the browser to recompute on repaint.
    layer.style.backgroundImage = on ? `url("${dataUrl}")` : "";
  }
  const enabledCb = document.getElementById("settings-bg-enabled");
  if (enabledCb) enabledCb.checked = !!(cfg && cfg.enabled);
  if (cfg){
    const b = document.getElementById("bg-brightness"); if (b) b.value = cfg.brightness != null ? cfg.brightness : 100;
    const bl = document.getElementById("bg-blur"); if (bl) bl.value = cfg.blur != null ? cfg.blur : 0;
    const o = document.getElementById("bg-opacity"); if (o) o.value = cfg.opacity != null ? cfg.opacity : 100;
    const fn = document.getElementById("bg-filename");
    if (fn) fn.textContent = cfg.path ? cfg.path.split(/[\\/]/).pop() : "No image selected";
  }
  // A background image turning on/off changes whether the glass toggle is
  // even available (see applyLiquidGlass below) -- recheck the gate every
  // time, not just when the glass checkbox itself is touched.
  applyLiquidGlass(_glassEnabled);
}

// Ambient blobs on/off. `_blobLayerCtl` is set once initBlobLayer() runs
// (see boot below) and exposes pause()/resume() so the wander/cursor loops
// actually stop doing work while hidden, not just get display:none'd.
let _blobsEnabled = true;
let _blobLayerCtl = null;
function applyBlobsEnabled(enabled){
  _blobsEnabled = !!enabled;
  document.documentElement.classList.toggle("blobs-off", !_blobsEnabled);
  if (_blobLayerCtl) (_blobsEnabled ? _blobLayerCtl.resume : _blobLayerCtl.pause)();
  const cb = document.getElementById("settings-blobs-enabled");
  if (cb) cb.checked = _blobsEnabled;
  // Same reasoning as in applyBgImage above -- blobs are the other half
  // of the glass toggle's availability gate.
  applyLiquidGlass(_glassEnabled);
}

// Per-blob on/off (SETTINGS → AMBIENT BLOBS → Center/Blob A/Blob B/Cursor).
// Independent of the master applyBlobsEnabled() above -- this just hides
// one blob's own div via its own html.blob-<key>-off class (see CSS),
// same plain display:none approach as the master toggle, no layout work.
let _blobIndividualEnabled = {center:true, a:true, b:true, cursor:true};
function applyBlobIndividual(key, enabled){
  if (!(key in _blobIndividualEnabled)) return;
  _blobIndividualEnabled[key] = !!enabled;
  document.documentElement.classList.toggle(`blob-${key}-off`, !enabled);
  const cb = document.getElementById(`settings-blob-${key}-enabled`);
  if (cb) cb.checked = !!enabled;
}

// The glass surfaces toggle. Purely cosmetic (see the html.liquid-glass-on
// CSS block for what it actually does) and only ever meaningful while the
// panes are already transparent, i.e. while ambient blobs and/or a
// background image are on -- so this is gated on the very same two
// signals those features already maintain (the "blobs-off" class and
// "has-bg-image" class), and the checkbox itself is disabled whenever
// neither is on, rather than left checkable with no visible effect.
let _glassEnabled = false;
function applyLiquidGlass(enabled){
  _glassEnabled = !!enabled;
  const available = _blobsEnabled || document.documentElement.classList.contains("has-bg-image");
  // Toggling this class only ever changes background/border/box-shadow on
  // a fixed set of already-painted elements (see the CSS) -- no layout
  // change, no element being added/removed/hidden -- so there's nothing
  // here that can produce a flash of unstyled/blank content.
  document.documentElement.classList.toggle("liquid-glass-on", _glassEnabled && available);
  const cb = document.getElementById("settings-glass-enabled");
  if (cb){
    cb.checked = _glassEnabled;
    cb.disabled = !available;
  }
  const hint = document.getElementById("glass-hint");
  if (hint) hint.style.display = available ? "none" : "block";
}

// How translucent the glass fill is (SETTINGS → Glass Opacity slider).
// Just writes a percentage into --glass-pct, which the color-mix() calls
// in the CSS read directly -- same "swap a plain custom property" update
// as applyColors()/applyTheme() above, not a filter, so dragging the
// slider repaints instantly with nothing left for the browser to keep
// recomputing afterward.
let _glassOpacity = 55;
function applyGlassOpacity(pct){
  _glassOpacity = Math.min(75, Math.max(35, Math.round(Number(pct) || 55)));
  document.documentElement.style.setProperty("--glass-pct", _glassOpacity + "%");
  const el = document.getElementById("settings-glass-opacity");
  if (el) el.value = _glassOpacity;
}

// Main chat window text size (SETTINGS → Chat Text Size). Writes one custom
// property, --chat-fs, that only chat-pane CSS reads (see the "Chat text size
// + readability" block), so nothing outside the chat window changes size.
// Precision is 0.01 px; the slider/number box use 0.05 px steps.
const CHAT_FS_DEFAULT = 15, CHAT_FS_MIN = 10, CHAT_FS_MAX = 32;
let _chatFontSize = CHAT_FS_DEFAULT;
function clampChatFontSize(v){
  const n = Number(v);
  if (!isFinite(n)) return CHAT_FS_DEFAULT;
  return Math.min(CHAT_FS_MAX, Math.max(CHAT_FS_MIN, Math.round(n * 100) / 100));
}
function applyChatFontSize(px){
  _chatFontSize = clampChatFontSize(px);
  document.documentElement.style.setProperty("--chat-fs", _chatFontSize + "px");
  const sl = document.getElementById("settings-chat-fs");
  if (sl) sl.value = _chatFontSize;
  const num = document.getElementById("settings-chat-fs-num");
  if (num && document.activeElement !== num) num.value = _chatFontSize.toFixed(2);
  try { autosizeMsgInput(); } catch (e) {}
}

// The frosted-highlight toggle (SETTINGS → "Frosted highlight" checkbox).
// Just flips the html.glass-no-sheen class -- see that CSS rule for what
// it zeroes out. On by default, independent of the opacity slider.
let _glassSheenEnabled = true;
function applyGlassSheen(enabled){
  _glassSheenEnabled = enabled !== false;
  document.documentElement.classList.toggle("glass-no-sheen", !_glassSheenEnabled);
  const cb = document.getElementById("settings-glass-sheen-enabled");
  if (cb) cb.checked = _glassSheenEnabled;
}

// The "Hide from screenshots" toggle for Ambient Surfaces (SETTINGS →
// AMBIENT SURFACES). UI state only -- the checkbox's onchange also calls
// set_ambient_hide_from_capture to live-apply it to open widgets, and
// saveSettingsPanel persists it. On by default (matches the old behavior).
let _ambientHideCapture = true;
function applyAmbientHideCapture(enabled){
  _ambientHideCapture = enabled !== false;
  const cb = document.getElementById("settings-ambient-hide-capture");
  if (cb) cb.checked = _ambientHideCapture;
}

// Cache of the current bg config + baked data url. Slider drags call a
// debounced re-bake (Python does the Pillow work) rather than a live CSS
// filter, since a live filter was the actual source of the flashing.
let _bgState = { cfg: { enabled:false, path:"", brightness:100, blur:0, opacity:100 }, dataUrl: null };
let _bgPreviewTimer = null;
function _scheduleBgPreview(){
  clearTimeout(_bgPreviewTimer);
  _bgPreviewTimer = setTimeout(async ()=>{
    const r = await api("preview_background_image", _bgState.cfg.brightness, _bgState.cfg.blur, _bgState.cfg.opacity);
    if (r && r.ok){
      _bgState.dataUrl = r.data_url;
      applyBgImage(_bgState.cfg, _bgState.dataUrl);
    }
  }, 180);
}

function fillDatalist(id, values){
  const dl = document.getElementById(id);
  if (!dl) return;
  dl.innerHTML = "";
  (values||[]).forEach(v=>{ const o=document.createElement("option"); o.value=v; dl.appendChild(o); });
}

// ── Custom dropdown enhancer -----------------------------------------------
// Wraps a native <select> with a themed, scrollable custom dropdown so
// every dropdown in the app looks and behaves consistently instead of
// falling back to the OS's native popup styling. The underlying <select>
// is kept (hidden) so all existing code that populates it with
// `appendChild(option)`, reads/sets `.value`, or attaches `.onchange`
// keeps working exactly as before -- this only changes what's rendered.
function enhanceSelect(sel){
  if (!sel || sel.dataset.enhanced) return;
  sel.dataset.enhanced = "1";
  sel.classList.add("real-select-hidden");

  const wrap = document.createElement("div");
  wrap.className = "dropdown-wrap";
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "dropdown-trigger";
  trigger.innerHTML = "<span></span>";
  const list = document.createElement("div");
  list.className = "dropdown-list";
  wrap.appendChild(trigger);
  wrap.appendChild(list);
  sel.insertAdjacentElement("afterend", wrap);

  function closeList(){ list.classList.remove("open"); wrap.classList.remove("open"); }
  function openList(){
    document.querySelectorAll(".dropdown-list.open").forEach(l=>{ if (l !== list) l.classList.remove("open"); });
    document.querySelectorAll(".dropdown-wrap.open").forEach(w=>{ if (w !== wrap) w.classList.remove("open"); });
    list.classList.add("open"); wrap.classList.add("open");
    const sel_ = list.querySelector(".dropdown-option.selected");
    if (sel_) sel_.scrollIntoView({ block: "nearest" });
  }

  function syncOptions(){
    list.innerHTML = "";
    if (!sel.options.length){
      list.innerHTML = `<div class="dropdown-empty">No options</div>`;
      return;
    }
    Array.from(sel.options).forEach((opt, i)=>{
      const item = document.createElement("div");
      item.className = "dropdown-option" + (i === sel.selectedIndex ? " selected" : "");
      item.textContent = opt.textContent;
      item.onclick = ()=>{
        sel.selectedIndex = i;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
        closeList();
        syncTrigger();
      };
      list.appendChild(item);
    });
  }
  function syncTrigger(){
    const opt = sel.options[sel.selectedIndex];
    trigger.querySelector("span").textContent = opt ? opt.textContent : "—";
    list.querySelectorAll(".dropdown-option").forEach((el, i)=> el.classList.toggle("selected", i === sel.selectedIndex));
  }

  trigger.onclick = (e)=>{ e.stopPropagation(); list.classList.contains("open") ? closeList() : openList(); };
  document.addEventListener("click", (e)=>{ if (!wrap.contains(e.target)) closeList(); });

  // Options are usually populated dynamically after enhancement (project
  // lists, model lists, file lists, etc.) -- watch for that and re-sync.
  new MutationObserver(()=>{ syncOptions(); syncTrigger(); }).observe(sel, { childList: true });

  // Programmatic `sel.value = ...` (used throughout to restore a saved
  // selection) doesn't fire 'change' natively, and wouldn't update our
  // custom trigger label either without this -- intercept the property so
  // the visible label always matches the real underlying value.
  const nativeDesc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
  Object.defineProperty(sel, "value", {
    get(){ return nativeDesc.get.call(sel); },
    set(v){ nativeDesc.set.call(sel, v); syncTrigger(); },
    configurable: true,
  });

  syncOptions();
  syncTrigger();
}

async function loadSettingsPanel(){
  const s = await api("get_settings");
  // Populate the per-theme color + blobs caches BEFORE applying the theme,
  // so applyTheme() layers each theme's own saved preset immediately
  // instead of the stock/shared values.
  _themeColors.dark = s.colors_dark || {...DEFAULT_COLORS};
  _themeColors.light = s.colors_light || {...DEFAULT_COLORS};
  _themeBlobsEnabled.dark = s.blobs_enabled_dark !== false;
  _themeBlobsEnabled.light = s.blobs_enabled_light !== false;
  applyTheme(s.theme || "dark");
  refreshPresets();
  _bgState.cfg = s.bg_image || _bgState.cfg;
  if (_bgState.cfg.enabled && _bgState.cfg.path){
    const r = await api("get_background_image_data");
    _bgState.dataUrl = r && r.ok ? r.data_url : null;
  } else {
    _bgState.dataUrl = null;
  }
  applyBgImage(_bgState.cfg, _bgState.dataUrl);
  applyLiquidGlass(s.liquid_glass_enabled === true);
  applyGlassOpacity(s.liquid_glass_opacity != null ? s.liquid_glass_opacity : 55);
  applyGlassSheen(s.liquid_glass_sheen_enabled !== false);
  applyChatFontSize(s.chat_font_size);
  applyAmbientHideCapture(s.ambient_hide_from_capture !== false);
  const blobsIndiv = s.blobs_individual || {};
  ["center","a","b","cursor"].forEach(key=>{
    applyBlobIndividual(key, blobsIndiv[key] !== false);
  });
  const provSel = document.getElementById("settings-provider");
  if (provSel && !provSel.options.length){
    const info = await api("get_providers");
    info.options.forEach(o=>{ const op=document.createElement("option"); op.textContent=o; provSel.appendChild(op); });
  }
  if (provSel) provSel.value = s.provider;
  const modelInput = document.getElementById("settings-model");
  if (modelInput) modelInput.value = s.model;
  const modelsForProvider = await api("select_provider", s.provider);
  fillDatalist("settings-model-list", modelsForProvider.models);
  // Swatches + live colors for the active theme are already handled by
  // applyTheme() above (it reads from _themeColors), so nothing further
  // needed here.
}

async function saveSettingsPanel(){
  const provider = document.getElementById("settings-provider").value;
  const model = document.getElementById("settings-model").value;
  const bg_image = {
    enabled: document.getElementById("settings-bg-enabled").checked,
    brightness: Number(document.getElementById("bg-brightness").value),
    blur: Number(document.getElementById("bg-blur").value),
    opacity: Number(document.getElementById("bg-opacity").value),
  };
  const colors = {};
  ["accent","accent2","bg","panel","text","user_msg","blob_center","blob_a","blob_b","blob_cursor"].forEach(k=>{
    const el = document.getElementById(`settings-color-${k}`);
    if (el) colors[k] = el.value;
  });
  // Only the ACTIVE theme's swatches/checkbox are on screen, so only its
  // bucket gets updated here -- the other theme's saved preset is sent
  // through untouched so it isn't clobbered by whatever's currently showing.
  _themeColors[_activeTheme] = colors;
  _themeBlobsEnabled[_activeTheme] = document.getElementById("settings-blobs-enabled").checked;
  const blobs_individual = {..._blobIndividualEnabled};
  const r = await api("save_settings", {
    provider, model, theme: _activeTheme,
    colors_dark: _themeColors.dark, colors_light: _themeColors.light,
    blobs_enabled_dark: _themeBlobsEnabled.dark, blobs_enabled_light: _themeBlobsEnabled.light,
    blobs_individual,
    liquid_glass_enabled: _glassEnabled,
    liquid_glass_opacity: _glassOpacity,
    liquid_glass_sheen_enabled: _glassSheenEnabled,
    chat_font_size: _chatFontSize,
    ambient_hide_from_capture: _ambientHideCapture,
    bg_image,
  });
  const status = document.getElementById("settings-status");
  if (r.ok){
    _themeColors.dark = r.settings.colors_dark || _themeColors.dark;
    _themeColors.light = r.settings.colors_light || _themeColors.light;
    _themeBlobsEnabled.dark = r.settings.blobs_enabled_dark !== false;
    _themeBlobsEnabled.light = r.settings.blobs_enabled_light !== false;
    applyTheme(r.settings.theme || "dark");
    _bgState.cfg = r.settings.bg_image;
    applyBgImage(_bgState.cfg, _bgState.dataUrl);
    applyLiquidGlass(r.settings.liquid_glass_enabled === true);
    applyGlassOpacity(r.settings.liquid_glass_opacity != null ? r.settings.liquid_glass_opacity : 55);
    applyGlassSheen(r.settings.liquid_glass_sheen_enabled !== false);
    applyChatFontSize(r.settings.chat_font_size);
    applyAmbientHideCapture(r.settings.ambient_hide_from_capture !== false);
    if (status) status.textContent = "Saved — will be remembered next launch.";
  } else if (status) {
    status.textContent = `Error: ${r.error}`;
  }
}

function populateProjects(list, selectPath){
  const sel = document.getElementById("project-select");
  if (!sel) return;
  const prevValue = selectPath || sel.value;
  sel.innerHTML = "";
  if (!list.length){
    sel.innerHTML = `<option value="">+ Project to add a workspace...</option>`;
    sel.value = "";
    return;
  }
  list.forEach(p=>{
    const o = document.createElement("option");
    o.value = p;
    o.textContent = p.split(/[\\/]/).filter(Boolean).pop() || p;
    o.title = p;
    sel.appendChild(o);
  });
  // Keep the dropdown's visible selection in sync with whichever workspace
  // is actually active, instead of silently defaulting to index 0.
  if (prevValue && list.includes(prevValue)) sel.value = prevValue;
}

function renderFileList(info){
  const box = document.getElementById("file-list");
  if (!box || !info) return;
  let out = `${icon("folder")} ${escapeHtml(info.root)}\n`;
  (info.files||[]).forEach(f=>{ out += `  ${icon(f.dir ? "folder" : "file")} ${escapeHtml(f.name)}\n`; });
  box.innerHTML = out;
}

async function refreshHistory(){
  const list = await api("list_chats");
  const box = document.getElementById("history-list");
  if (!box) return;
  box.innerHTML = "";
  if (!list.length){
    box.innerHTML = `<div style="font-size:11px;color:var(--subtext);padding:8px;">No saved chats yet.</div>`;
    return;
  }
  list.forEach(chat=>{
    const card = document.createElement("div");
    card.className = "history-card" + (chat.current ? " current" : "");
    card.style.cursor = "pointer";
    let title = chat.title || "Untitled chat";
    if (title.length > 30) title = title.slice(0,29) + "…";
    card.innerHTML = `
      <div style="min-width:0;flex:1;">
        <div class="history-title">${escapeHtml(title)}</div>
        <div class="history-ts">${(chat.updated_at||"").replace("T","  ")}</div>
      </div>
      <div class="history-actions">
        <button class="mini-btn rename" title="Rename">${icon("pencil")}</button>
        <button class="mini-btn del">${icon("trash")}</button>
      </div>`;
    card.onclick = async ()=>{
      const r = await api("open_chat_in_new_tab", chat.id);
      if (!r.ok){ showAlert(r.error, "Error"); return; }
      clearChat();
      renderHistoryWindow(r.display||[]);
      applyKbState(r.kb_state);
      switchTab("Chat");
      refreshHistory();
      refreshTitlebarInfo();
      buildConversationTabs();
    };
    card.querySelector(".rename").onclick = async (e)=>{
      e.stopPropagation();
      const newTitle = await showPrompt("New chat title:", "Rename Chat", title);
      if (newTitle === null || newTitle === undefined) return;
      const trimmed = newTitle.trim();
      if (!trimmed || trimmed === title) return;
      const r = await api("rename_chat", chat.id, trimmed);
      if (!r.ok){ showAlert(r.error, "Error"); return; }
      refreshHistory();
      refreshTitlebarInfo();
    };
    card.querySelector(".del").onclick = async (e)=>{
      e.stopPropagation();
      const ok = await showConfirm(`Permanently delete "${title}"?`, "Delete Chat", {danger:true, okLabel:"Delete"});
      if (!ok) return;
      await api("delete_chat", chat.id);
      if (chat.current){ clearChat(); applyKbState(null); }
      refreshHistory();
    };
    box.appendChild(card);
  });
}

// ── Tool pane content builders -------------------------------------------
// ── Editor tab (Monaco) ────────────────────────────────────────────────────────────────
// A full code editor (explorer + file tabs + Monaco) that edits ANY file on disk, plus the chat's
// artifacts. It lives in its own page (gui/editor_assets/editor.html, served by a localhost server
// started in gui/editor_backend.py) and is shown in a PERSISTENT iframe. It cannot be built into
// #tool-content like the other panes: showToolPane() wipes that element on every tab switch, which
// would destroy the editor and every open file with it. The iframe is parked beside #tool-content and
// is only shown/hidden. Isolation also keeps Monaco's AMD loader and CSS away from this page.
// Page <-> app traffic is postMessage only: the iframe asks for editor_* Api methods (whitelisted by
// name below) and Python pushes editor_* events down the same pipe (see mzEdEvent).
var MZED = {frame:null, host:null, ready:false, queue:[], origin:"", booting:null, themeTimer:null};
const MZED_VARS = ["bg","panel","surface","surface2","border","border2","accent","accent-dim","accent-faint","text","subtext","muted","green","red","yellow"];
function mzEdThemeVars(){
  const cs = getComputedStyle(document.documentElement), out = {};
  MZED_VARS.forEach(k=>{ const v = cs.getPropertyValue("--" + k).trim(); if (v) out["--" + k] = v; });
  return out;
}
function mzEdPost(msg){
  if (!MZED.frame || !MZED.ready){ MZED.queue.push(msg); return; }
  try { MZED.frame.contentWindow.postMessage(msg, MZED.origin); } catch (e) {}
}
function mzEdShow(show){ if (typeof MZED !== "undefined" && MZED && MZED.host) MZED.host.style.display = show ? "block" : "none"; }
function mzEdEnsureFrame(){
  if (MZED.booting) return MZED.booting;
  MZED.booting = (async ()=>{
    const pane = document.querySelector("#tool-pane-wrap .pane");
    let host = MZED.host;
    if (!host){ host = document.createElement("div"); host.id = "mz-ed-host"; pane.appendChild(host); MZED.host = host; }
    let info;
    try { info = await api("editor_server_info"); } catch (e) { info = {ok:false, error:String(e)}; }
    if (!info || !info.ok){
      host.innerHTML = `<div class="mz-ed-err">The code editor could not start.<br><small>${escapeHtml((info && info.error) || "unknown error")}</small><br><button class="ghost-btn" id="mz-ed-retry">Retry</button></div>`;
      const rb = document.getElementById("mz-ed-retry");
      if (rb) rb.onclick = ()=>{ host.innerHTML = ""; MZED.booting = null; mzEdEnsureFrame(); };
      MZED.booting = null;
      return;
    }
    const fr = document.createElement("iframe");
    MZED.origin = new URL(info.url).origin;
    fr.src = info.url;
    host.appendChild(fr);
    MZED.frame = fr;
  })();
  return MZED.booting;
}
function buildEditorPane(box){
  box.style.display = "none";          // the persistent host takes this pane's place
  mzEdShow(true);
  mzEdEnsureFrame().then(()=>mzEdShow(true));
}
// Python -> editor. editor_open_* also brings the Editor tab to the front (closing the Render Surface
// first, since while it is open the layout reserves the whole right half for it).
function mzEdEvent(kind, payload){
  // editor_ai_request = the chat model reading/editing through the Editor (editor_link.py). Its open_file /
  // open_folder ops show the Editor like the plain open events; state/snapshot/edit ops must NOT steal the
  // tab, but they still need the iframe alive, so boot it in the background if it never started.
  const aiOp = (kind === "editor_ai_request" && payload) ? payload.op : "";
  const aiOpen = (aiOp === "open_file" || aiOp === "open_folder" || aiOp === "approve_edit");   // approve_edit: the dialog must be seen
  if (kind === "editor_open_artifact" || kind === "editor_open_file" || kind === "editor_open_folder" || aiOpen){
    if (state.renderSurfaceOpen){ closeRenderSurfaceUI(); api("render_surface_manual_close"); }
    switchTab("Editor");
  } else if (kind === "editor_ai_request" && !MZED.frame){
    mzEdEnsureFrame();
  }
  mzEdPost({__ed:"event", kind, payload});
}
window.addEventListener("message", (e)=>{
  if (!MZED.frame || e.source !== MZED.frame.contentWindow) return;
  const d = e.data;
  if (!d || typeof d !== "object" || !d.__ed) return;
  const reply = (o)=>{ try { MZED.frame.contentWindow.postMessage(o, MZED.origin); } catch (x) {} };
  if (d.__ed === "ready"){
    MZED.ready = true;
    mzEdPost({__ed:"theme", vars: mzEdThemeVars()});
    MZED.queue.splice(0).forEach(m=>mzEdPost(m));
  } else if (d.__ed === "rpc"){
    const m = String(d.method || "");
    if (!/^editor_[a-z_]+$/.test(m)){ reply({__ed:"rpc_result", id:d.id, error:"method not allowed: " + m}); return; }
    Promise.resolve().then(()=>api(m, ...(Array.isArray(d.args) ? d.args : [])))
      .then(r=>reply({__ed:"rpc_result", id:d.id, result:r}))
      .catch(err=>reply({__ed:"rpc_result", id:d.id, error:String((err && err.message) || err)}));
  } else if (d.__ed === "dirty"){
    const b = document.querySelector('.tab-btn[data-name="Editor"]');
    if (b) b.classList.toggle("has-dirty", d.count > 0);
  }
});
// Keep Monaco's colors in step with the app theme (applyTheme/applyColors set CSS vars on <html>).
new MutationObserver(()=>{
  clearTimeout(MZED.themeTimer);
  MZED.themeTimer = setTimeout(()=>{ if (MZED.ready) mzEdPost({__ed:"theme", vars: mzEdThemeVars()}); }, 120);
}).observe(document.documentElement, {attributes:true, attributeFilter:["style","class"]});

function showToolPane(name){
  const box = document.getElementById("tool-content");
  const builders = {
    "Voice": buildVoicePane, "Log": buildLogPane, "Model": buildModelPane, "Parameters": buildParamsPane,
    "System Core": buildSysCorePane, "Knowledge": buildKnowledgePane,
    "Skills": buildSkillsPane, "Tools": buildToolsPane, "Shell": buildShellPane, "Flows": buildFlowsPane, "Schedule": buildSchedulePane, "MCP": buildMcpPane,
    "Agents": buildAgentsPane,
    "Permissions": buildPermissionsPane,
    "Surfaces": buildSurfacesPane,
    "Editor": buildEditorPane,
  };
  box.innerHTML = "";
  mzEdShow(false);   // the Editor's persistent iframe host is hidden for every tab; buildEditorPane re-shows it
  box.style.display = "flex"; box.style.flexDirection = "column"; box.style.height = "100%";
  box.style.padding = "";  // reset any pane-specific override (e.g. Flows sets 0) before rebuilding
  (builders[name] || (()=>{}))(box);
}

function buildLogPane(box){
  box.innerHTML = `
    <div class="hdr-row"><div class="section-label">ACTIVITY LOG</div>
      <button class="ghost-btn" id="log-clear" style="height:22px;font-size:10px;">Clear</button></div>
    <div class="code-area log-view" id="log-box" style="margin-top:6px;"></div>`;
  document.getElementById("log-clear").onclick = ()=>{ document.getElementById("log-box").innerHTML=""; };
}

// ── Shell tab -- persistent WSL2 terminal shared with the model ──────────
// Every command run through execute_wsl_command (model side) or typed
// here (user side) hits the exact same shell_session.ShellSession process
// on the backend, so this really is one continuous terminal both parties
// are watching/driving -- not two separate logs glued together.
function shellLineColor(line){
  if (line.stream === "cmd")  return line.origin === "user" ? "var(--accent)" : "#8fd3ff";
  if (line.stream === "meta") return "var(--muted)";
  return "var(--tool-text)";
}

function shellLineHtml(line){
  const color = shellLineColor(line);
  if (line.stream === "cmd"){
    // Real-terminal-style prompt: "<cwd> $ command", exactly like typing
    // into an actual shell -- the origin (user vs model) is conveyed by a
    // small leading marker instead of a "[who]" bracket, so the line reads
    // like a genuine terminal transcript rather than a chat log.
    const originMark = line.origin === "model" ? '<span style="color:var(--subtext);">' + icon("bot") + ' </span>'
      : (line.origin === "editor" ? '<span style="color:var(--subtext);">[editor] </span>' : "");
    const cwd = line.cwd || "";
    const isPs = line.shell === "powershell";
    const pfx = isPs ? "PS " : "";
    const sym = isPs ? "&gt;" : "$";
    return `<div style="font-weight:600;white-space:pre-wrap;word-break:break-word;">${originMark}<span style="color:var(--subtext);">${pfx}${escapeHtml(cwd)}</span> <span style="color:${color};">${sym}</span> <span style="color:${color};">${escapeHtml(line.text)}</span></div>`;
  }
  if (line.stream === "meta"){
    return `<div style="color:${color};font-style:italic;white-space:pre-wrap;word-break:break-word;">${escapeHtml(line.text)}</div>`;
  }
  return `<div style="color:${color};white-space:pre-wrap;word-break:break-word;">${escapeHtml(line.text)}</div>`;
}

// Tracks the shell's current working directory client-side, purely for
// the live prompt label ("<cwd> $") -- kept in sync with the backend's
// real $PWD (see shell_session.py) by re-fetching it after every command
// finishes (a "meta" shell_line event -- see appendShellLine below).
let shellCwd = "~";
const shellCmdHist = {wsl: [], powershell: []};
let shellHistIdx = -1;

async function shellLoadInfo(){
  try { shellInfo = (await api("get_shell_info")) || []; } catch (e) { /* keep defaults */ }
}

function shellRenderSwitch(){
  const box = document.getElementById("shell-switch");
  if (!box) return;
  const list = shellInfo.length ? shellInfo
    : [{kind:"wsl", label:"WSL2", available:true}, {kind:"powershell", label:"PowerShell", available:true}];
  box.innerHTML = list.map(s =>
    `<button class="ghost-btn" data-k="${s.kind}" ${s.available ? "" : "disabled title='not available on this machine'"}
      style="height:22px;font-size:10px;${s.kind === shellActive ? "background:var(--accent);color:#fff;" : ""}">${escapeHtml(s.label)}</button>`
  ).join("");
  box.querySelectorAll("button").forEach(b => { b.onclick = () => shellSwitchTo(b.dataset.k); });
}

function shellSwitchTo(kind){
  if (kind === shellActive) return;
  shellActive = kind;
  shellHistory = shellHistories[kind] || (shellHistories[kind] = []);
  shellHistIdx = -1;
  const box = document.getElementById("shell-terminal");
  const host = box ? box.parentElement : null;
  if (host) buildShellPane(host);
}

function updateShellPromptLabel(){
  const el = document.getElementById("shell-cwd-label");
  if (el) el.textContent = (shellActive === "powershell" ? "PS " : "") + shellCwd;
  const sym = document.getElementById("shell-sym");
  if (sym) sym.textContent = shellActive === "powershell" ? ">" : "$";
}

async function refreshShellCwd(){
  try {
    shellCwd = (await api("get_shell_cwd", shellActive)) || shellCwd;
  } catch (e) { /* leave shellCwd as-is */ }
  updateShellPromptLabel();
}

function buildShellPane(box){
  box.innerHTML = `
    <div class="hdr-row">
      <div class="section-label">SHELL</div>
      <div id="shell-switch" style="display:flex;gap:4px;margin-left:8px;"></div>
      <span style="flex:1;"></span>
      <button class="ghost-btn" id="shell-restart" style="height:22px;font-size:10px;">Restart</button>
    </div>
    <div id="shell-desc" style="font-size:10px;color:var(--muted);margin:2px 0 6px;line-height:1.5;">
      This is a real, persistent Linux terminal the model treats as its own
      computer (cwd/env/background jobs persist between commands) -- the
      user's Windows files are reachable at /mnt/&lt;drive&gt; (e.g. C:\\Users
      → /mnt/c/Users). Type directly into the terminal below, exactly like
      a normal shell -- what you run lands in this exact same session.
    </div>
    <div id="shell-terminal" tabindex="-1" style="flex:1;min-height:0;overflow-y:auto;background:var(--tool-bg);color:var(--tool-text);
      border:1px solid var(--border2);border-radius:10px;padding:8px 10px;font-family:var(--mono-font,monospace);
      font-size:12px;line-height:1.5;cursor:text;">
      <div id="shell-output"></div>
      <div id="shell-prompt-line" style="display:flex;align-items:center;font-weight:600;">
        <span id="shell-cwd-label" style="color:var(--subtext);white-space:pre;">${shellActive === "powershell" ? "PS " : ""}${escapeHtml(shellCwd)}</span>
        <span id="shell-sym" style="color:var(--tool-text);margin:0 4px;">${shellActive === "powershell" ? ">" : "$"}</span>
        <input id="shell-input" autocomplete="off" spellcheck="false"
          style="flex:1;min-width:0;background:transparent;border:none;outline:none;color:var(--tool-text);
          font-family:var(--mono-font,monospace);font-size:12px;padding:0;"/>
      </div>
    </div>`;

  const term = document.getElementById("shell-terminal");
  const out = document.getElementById("shell-output");
  const input = document.getElementById("shell-input");
  out.innerHTML = (shellHistories[shellActive] || []).map(shellLineHtml).join("");

  // Clicking anywhere in the terminal (scrollback or the empty area below
  // the prompt line) focuses the live input, same as clicking into a real
  // terminal window puts the cursor back at the prompt.
  term.addEventListener("mousedown", (e)=>{
    if (e.target === input) return;
    // Let a text selection inside the scrollback happen normally instead
    // of yanking focus away and collapsing it.
    if (window.getSelection && String(window.getSelection())) return;
    input.focus();
  });

  const sendCmd = async ()=>{
    const cmd = input.value;
    if (!cmd || !cmd.trim()) return;
    input.value = "";
    await api("shell_run_user_command", cmd, shellActive);
  };
  input.addEventListener("keydown", (e)=>{
    const h = shellCmdHist[shellActive] || (shellCmdHist[shellActive] = []);
    if (e.key === "Enter"){
      if (input.value.trim()){ h.push(input.value); if (h.length > 200) h.shift(); }
      shellHistIdx = -1;
      sendCmd();
    } else if (e.key === "ArrowUp"){
      if (h.length){ shellHistIdx = shellHistIdx < 0 ? h.length - 1 : Math.max(0, shellHistIdx - 1); input.value = h[shellHistIdx]; }
      e.preventDefault();
    } else if (e.key === "ArrowDown"){
      if (shellHistIdx >= 0){
        shellHistIdx++;
        if (shellHistIdx >= h.length){ shellHistIdx = -1; input.value = ""; } else { input.value = h[shellHistIdx]; }
      }
      e.preventDefault();
    }
  });

  document.getElementById("shell-restart").onclick = async ()=>{
    await api("shell_restart", shellActive);
  };

  input.focus();
  const desc = document.getElementById("shell-desc");
  if (desc) desc.innerHTML = shellActive === "powershell"
    ? "A real, persistent PowerShell session. The model drives it with execute_terminal_command and you type into the same session, so the working directory and variables persist. Type <b>edit &lt;file&gt;</b> to open a file in the Editor."
    : "A real, persistent Linux (WSL2) terminal the model treats as its own computer; Windows files are at /mnt/&lt;drive&gt;. What you type lands in this exact same session. Type <b>edit &lt;file&gt;</b> to open a file in the Editor.";
  shellRenderSwitch();
  shellLoadInfo().then(shellRenderSwitch);
  refreshShellCwd();
  term.scrollTop = term.scrollHeight;
}

// Called from window.__midumEvent for every "shell_line" push -- appends
// to the persistent history array always, and to the live DOM only when
// the Shell tab is the one currently mounted (its elements won't exist
// otherwise, since showToolPane rebuilds #tool-content per tab). New
// lines are inserted right before the live prompt row, so the prompt
// (and the input the user's actively typing into) always stays pinned to
// the bottom, exactly like a real terminal scrolling up under a fixed
// cursor line.
function appendShellLine(payload){
  const sk = payload.shell || "wsl";
  const hist = shellHistories[sk] || (shellHistories[sk] = []);
  hist.push(payload);
  if (hist.length > SHELL_HISTORY_CAP) hist.shift();
  // Mirror to the Editor's terminal panel -- only if the editor is already up
  // (otherwise mzEdPost would queue forever; the panel replays history on open).
  if (MZED.frame && MZED.ready) mzEdPost({__ed:"event", kind:"editor_shell_line", payload});
  if (sk !== shellActive) return;
  const out = document.getElementById("shell-output");
  const term = document.getElementById("shell-terminal");
  if (!out || !term) return;
  out.insertAdjacentHTML("beforeend", shellLineHtml(payload));
  term.scrollTop = term.scrollHeight;
  // A command has fully finished (its exit-code/meta line just arrived) --
  // the shell's $PWD may have changed (e.g. `cd`), so refresh the prompt
  // label to match. Cheap no-op most of the time (cwd usually unchanged).
  if (payload.stream === "meta") refreshShellCwd();
}

// Batched variant: the backend coalesces output into ONE push per ~30 ms burst (instead of one
// blocking evaluate_js round trip per line, which made big outputs crawl). Same effect as calling
// appendShellLine per line, but one DOM write and one forwarded editor message per batch.
function appendShellLines(lines){
  const fwd = [];
  let html = "", metaSeen = false;
  for (const payload of lines){
    const sk = payload.shell || "wsl";
    const hist = shellHistories[sk] || (shellHistories[sk] = []);
    hist.push(payload);
    if (hist.length > SHELL_HISTORY_CAP) hist.shift();
    fwd.push(payload);
    if (sk === shellActive){ html += shellLineHtml(payload); if (payload.stream === "meta") metaSeen = true; }
  }
  if (MZED.frame && MZED.ready && fwd.length) mzEdPost({__ed:"event", kind:"editor_shell_lines", payload:{lines:fwd}});
  const out = document.getElementById("shell-output");
  const term = document.getElementById("shell-terminal");
  if (out && term && html){
    out.insertAdjacentHTML("beforeend", html);
    while (out.childNodes.length > SHELL_HISTORY_CAP) out.removeChild(out.firstChild);
    term.scrollTop = term.scrollHeight;
  }
  if (metaSeen) refreshShellCwd();
}

function buildVoicePane(box){
  box.innerHTML = `
    <div class="hdr-row"><div class="section-label">VOICE CONTROL — GEMINI LIVE</div></div>
    <div id="voice-status-row" style="display:flex;align-items:center;gap:8px;margin-top:6px;">
      <div id="voice-dot" style="width:10px;height:10px;border-radius:50%;background:var(--subtext);"></div>
      <div id="voice-status-text" style="font-size:11px;color:var(--subtext);">Checking...</div>
    </div>
    <div style="display:flex;gap:8px;margin-top:10px;">
      <button class="btn" id="voice-start-btn" style="background:var(--accent);color:#fff;flex:1;">${icon("mic")} Start talking</button>
      <button class="ghost-btn" id="voice-mute-btn" style="display:none;">Mute</button>
    </div>
    <div id="voice-deps-warning" style="font-size:10px;color:var(--red);margin-top:6px;display:none;"></div>
    <div class="divider"></div>
    <div class="field-label">MODEL</div>
    <input id="voice-model-input" placeholder="gemini-3.1-flash-live-preview"
      style="height:32px;border-radius:16px;border:1px solid var(--border2);background:var(--surface);color:var(--text);padding:0 10px;"/>
    <div class="field-label" style="margin-top:8px;">VOICE</div>
    <select id="voice-name-select">
      ${["Puck","Charon","Kore","Fenrir","Aoede","Leda","Orus","Zephyr"].map(v=>`<option value="${v}">${v}</option>`).join("")}
    </select>
    <div class="divider"></div>
    <div class="hdr-row">
      <div class="field-label" style="margin:0;">PUSH-TO-TALK HOTKEYS</div>
      <button class="ghost-btn" id="ptt-reset-btn" style="height:22px;font-size:10px;">Reset defaults</button>
    </div>
    <div style="font-size:10px;color:var(--muted);margin:2px 0 8px;line-height:1.5;">
      Hold either key/button to talk -- connects to Gemini Live automatically on first press if not already connected, then just mutes the mic (not the connection) on release. Works globally, even when Midum isn't the focused window.
    </div>
    <div id="ptt-deps-warning" style="font-size:10px;color:var(--red);margin-bottom:6px;display:none;"></div>
    <div id="ptt-slot-slot1" class="ptt-slot-row"></div>
    <div id="ptt-slot-slot2" class="ptt-slot-row" style="margin-top:6px;"></div>
  `;

  document.getElementById("voice-start-btn").onclick = onVoiceStartStopClick;
  document.getElementById("voice-mute-btn").onclick = onVoiceMuteClick;
  document.getElementById("ptt-reset-btn").onclick = async ()=>{
    await api("reset_ptt_hotkeys");
    refreshPttHotkeys();
  };

  (async ()=>{
    const status = await api("get_voice_status");
    voiceState.running = !!status.running;
    document.getElementById("voice-model-input").value = status.model || "";
    document.getElementById("voice-name-select").value = status.voice || "Puck";
    const warn = document.getElementById("voice-deps-warning");
    if (status.dependencies !== "OK"){
      warn.style.display = "block";
      warn.textContent = "Setup needed: " + status.dependencies;
    } else {
      warn.style.display = "none";
    }
    handleVoiceStatus({status: voiceState.running ? "connected" : "stopped"});
  })();

  refreshPttHotkeys();
}

// ── Push-to-talk hotkey config UI ─────────────────────────────────────
let _pttCaptureSlotId = null;

function pttSlotRowHtml(slot){
  const kindLabel = slot.kind === "mouse" ? "Mouse button" : "Keyboard key";
  return `
    <div style="flex:1;min-width:0;">
      <div style="font-size:12px;font-weight:600;color:var(--text);">${escapeHtml(slot.label || slot.value)}</div>
      <div style="font-size:10px;color:var(--subtext);">${kindLabel}</div>
    </div>
    <button class="mini-btn open" data-act="rebind">Change</button>`;
}

async function refreshPttHotkeys(){
  const st = await api("get_ptt_hotkeys");
  const warn = document.getElementById("ptt-deps-warning");
  if (warn){
    if (!st.available){
      warn.style.display = "block";
      warn.textContent = "Setup needed: pynput not installed (pip install pynput) -- global hotkeys are unavailable.";
    } else {
      warn.style.display = "none";
    }
  }
  (st.hotkeys || []).forEach(slot=>{
    const el = document.getElementById(`ptt-slot-${slot.id}`);
    if (!el) return;
    el.style.cssText = "display:flex;align-items:center;gap:10px;background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:8px 10px;";
    el.innerHTML = pttSlotRowHtml(slot);
    const btn = el.querySelector('[data-act="rebind"]');
    if (btn) btn.onclick = ()=>startPttCapture(slot.id, el);
  });
}

async function startPttCapture(slotId, rowEl){
  if (_pttCaptureSlotId) return;   // one capture at a time
  _pttCaptureSlotId = slotId;
  const btn = rowEl.querySelector('[data-act="rebind"]');
  if (btn){ btn.textContent = "Press any key or click..."; btn.disabled = true; }
  await api("start_ptt_capture", slotId);
}

function handlePttCaptured(payload){
  if (!payload || payload.slot_id !== _pttCaptureSlotId) return;
  _pttCaptureSlotId = null;
  refreshPttHotkeys();
}

function handlePttState(payload){
  const pressed = (payload && payload.pressed) || [];
  const dot = document.getElementById("voice-dot");
  if (!dot) return;
  dot.style.boxShadow = pressed.length ? "0 0 0 4px var(--accent-faint)" : "none";
}

function onVoiceStartStopClick(){
  if (voiceState.running || voiceState.connecting) {
    api("stop_voice_session");
    handleVoiceStatus({status: "stopping"});
  } else {
    const model = (document.getElementById("voice-model-input")||{}).value || "";
    const voice = (document.getElementById("voice-name-select")||{}).value || "";
    handleVoiceStatus({status: "connecting"});
    api("start_voice_session", model, voice);
  }
}

let voiceMuted = false;
function onVoiceMuteClick(){
  voiceMuted = !voiceMuted;
  api("set_voice_muted", voiceMuted);
  const btn = document.getElementById("voice-mute-btn");
  if (btn) btn.textContent = voiceMuted ? "Unmute" : "Mute";
}

function handleVoiceStatus(payload){
  const st = (payload && payload.status) || "stopped";
  voiceState.running = (st === "connected");
  voiceState.connecting = (st === "connecting" || st === "retrying");
  const dot  = document.getElementById("voice-dot");
  const text = document.getElementById("voice-status-text");
  const startBtn = document.getElementById("voice-start-btn");
  const muteBtn  = document.getElementById("voice-mute-btn");
  if (!dot || !text) return; // pane not currently mounted
  const colors = {connected:"var(--green)", connecting:"var(--yellow)", retrying:"var(--yellow)",
                   stopping:"var(--yellow)", stopped:"var(--subtext)"};
  dot.style.background = colors[st] || "var(--subtext)";
  const labels = {connected:"Listening — speak naturally", connecting:"Connecting to Gemini Live...",
                   retrying: (payload && payload.message) || "Retrying with fallback model...",
                   stopping:"Stopping...", stopped:"Not connected"};
  text.textContent = labels[st] || st;
  if (startBtn) {
    startBtn.innerHTML = voiceState.running ? icon("stop") + " Stop" : icon("mic") + " Start talking";
    startBtn.style.background = voiceState.running ? "var(--red)" : "var(--accent)";
  }
  if (muteBtn) muteBtn.style.display = voiceState.running ? "inline-block" : "none";
}

// ── Voice → main chat rendering ──────────────────────────────────────
// Voice-mode transcripts, tool calls, and system notes are rendered
// straight into the main chat column (chatCol()) using the exact same
// row/bubble/tool-row markup as text chat, so a voice conversation looks
// like a normal conversation with tool calls and everything.
//
// Gemini Live streams transcription text in small fragments rather than
// whole utterances, so consecutive fragments from the same speaker are
// appended into one growing bubble (_voiceStreamRow) instead of spawning a
// new bubble per fragment. Anything that interrupts the turn -- a tool
// call, an interruption, or turn completion -- clears _voiceStreamRow so
// the next fragment starts a fresh bubble, matching how tool calls break
// up a normal streamed reply.
let _voiceStreamRow = null;   // {role, bubbleEl} | null
let _voiceToolArgs = {};      // name -> args, remembered between call/result events

function appendVoiceTranscript(role, text){
  if (!text) return;
  const isUser = role === "user";

  if (_voiceStreamRow && _voiceStreamRow.role === role){
    _voiceStreamRow.rawText += text;
    if (isUser){
      _voiceStreamRow.bubbleEl.textContent = _voiceStreamRow.rawText;
    } else {
      _voiceStreamRow.bubbleEl.innerHTML = renderMidumContent(_voiceStreamRow.rawText);
    }
  } else {
    closeToolGroup();   // a new spoken bubble (either side) ends the current tool group
    const col = chatCol();
    const row = document.createElement("div");
    row.className = isUser ? "row user" : "row midum";
    row.innerHTML = isUser
      ? `<div class="row-label">You (voice)</div><div class="bubble user"></div>`
      : `<div class="bubble midum"></div>`;
    const bubbleEl = row.querySelector(".bubble");
    if (isUser){
      bubbleEl.textContent = text;
    } else {
      bubbleEl.innerHTML = renderMidumContent(text);
    }
    col.appendChild(row);
    _voiceStreamRow = { role, bubbleEl, rawText: text };
  }
  if (!isUser){ renderPendingMermaid(); renderPendingMath(); renderPendingCodeHighlight(); }
  scrollToBottom();
}

function appendVoiceToolEvent(kind, name, data){
  // A tool call always breaks the current streamed bubble, same as text chat.
  _voiceStreamRow = null;

  if (kind === "call"){
    _voiceToolArgs[name] = data;
    appendRow("tool", `-> Executing: '${name}'`);
  } else {
    const resStr = typeof data === "string" ? data : JSON.stringify(data);
    attachToolCallDetail(name, _voiceToolArgs[name], resStr);
  }
}

function appendVoiceSystemNote(text){
  _voiceStreamRow = null;
  appendRow("system", text);
}

function buildModelPane(box){
  box.innerHTML = `
    <div class="field-label">PROVIDER</div>
    <select id="provider-select"></select>
    <div class="hdr-row" style="margin-top:8px;"><div class="field-label">MODEL</div>
      <button class="ghost-btn" id="model-refresh" style="height:20px;font-size:10px;">${icon("refresh")}</button></div>
    <input list="model-list" id="model-input" style="height:32px;border-radius:16px;border:1px solid var(--border2);background:var(--surface);color:var(--text);padding:0 10px;"/>
    <datalist id="model-list"></datalist>
    <button class="btn" id="model-apply" style="margin-top:10px;background:var(--accent);color:#fff;">Apply</button>
    <div id="model-active" style="font-size:10px;color:var(--subtext);margin-top:8px;"></div>
    <div class="divider"></div>
    <div class="field-label">CONTEXT WINDOW (TOKENS)</div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:4px;">
      <input type="number" id="ctx-tokens-input" min="1000" step="1000" value="32000"
        style="height:32px;flex:1;border-radius:16px;border:1px solid var(--border2);background:var(--surface);color:var(--text);padding:0 10px;"/>
      <button class="btn" id="ctx-tokens-save" style="background:var(--accent);color:#fff;">Save</button>
    </div>
    <div id="ctx-tokens-status" style="font-size:10px;color:var(--subtext);margin-top:6px;"></div>
    <div style="font-size:10px;color:var(--muted);margin-top:4px;">How much context your model actually has. The summarizer uses this to know when it's approaching the limit and needs to compact older turns -- default 32000, raise it if your model supports a bigger window.</div>
    <div class="divider"></div>
    <div style="font-size:10px;color:var(--muted);">Local (Ollama) runs fully offline and is the default on every launch. Switching providers here only affects this running session. Want a service that isn't listed? Choose <b>LiteLLM (any provider)</b> — it covers 100+ (OpenAI, Anthropic, Mistral, xAI, DeepSeek, Azure, Bedrock, any OpenAI-compatible server…).</div>
  `;
  (async ()=>{
    const info = await api("get_providers");
    const sel = document.getElementById("provider-select");
    info.options.forEach(o=>{ const op=document.createElement("option"); op.textContent=o; sel.appendChild(op); });
    sel.value = info.current;
    document.getElementById("model-input").value = info.current_model;
    fillModelList(info.models);
  })();
  enhanceSelect(document.getElementById("provider-select"));
  attachLitellmPicker({ providerSelId: "provider-select", modelInputId: "model-input", prefix: "mp" });
  document.getElementById("provider-select").onchange = async (e)=>{
    const r = await api("select_provider", e.target.value);
    fillModelList(r.models); document.getElementById("model-input").value = r.default_model;
  };
  document.getElementById("model-refresh").onclick = async ()=>{
    const names = await api("refresh_ollama_models");
    if (names && names.length) fillModelList(names);
  };
  document.getElementById("model-apply").onclick = async ()=>{
    const label = document.getElementById("provider-select").value;
    const model = document.getElementById("model-input").value;
    const status = await api("apply_model", label, model);
    document.getElementById("model-active").textContent = `Active: ${status.provider} — ${status.model}`;
    refreshTitlebarInfo();
  };

  const ctxInput  = document.getElementById("ctx-tokens-input");
  const ctxStatus = document.getElementById("ctx-tokens-status");
  (async ()=>{
    const t = await api("get_context_token_limit");
    ctxInput.value = t.saved != null ? t.saved : t.effective;
    ctxStatus.textContent = t.is_override
      ? `Custom value saved — overrides the built-in per-model default.`
      : `Using the built-in default for the active model (${t.effective.toLocaleString()} tokens) — save a value to override it.`;
  })();
  document.getElementById("ctx-tokens-save").onclick = async ()=>{
    const btn = document.getElementById("ctx-tokens-save");
    btn.disabled = true;
    try {
      const r = await api("set_context_token_limit", parseInt(ctxInput.value, 10));
      ctxStatus.textContent = r.ok
        ? `Saved — the summarizer will now trigger around ${Math.round(r.saved * 0.8).toLocaleString()} tokens.`
        : (r.error || "Failed to save.");
    } finally {
      btn.disabled = false;
    }
  };
}
function fillModelList(models){
  const dl = document.getElementById("model-list");
  dl.innerHTML = "";
  (models||[]).forEach(m=>{ const o=document.createElement("option"); o.value=m; dl.appendChild(o); });
}

// ── LiteLLM (any provider) picker ---------------------------------------------
// When the Provider dropdown is set to "LiteLLM (any provider)", the plain
// free-text MODEL box is swapped for a guided picker:
//     1. SERVICE  -- searchable list of every provider LiteLLM supports, with
//                    one-click chips for the popular ones
//     2. MODEL    -- suggestions for that service (⟳ fetches the live list)
//     3. API KEY  -- saved per service into the secrets file, status shown
//     4. (Advanced) Base URL for self-hosted / OpenAI-compatible endpoints
//     5. Test     -- one tiny request to confirm key + model actually work
// The original <input> (modelInputId) stays the single source of truth and
// always holds the composed LiteLLM id ("<service>/<model>"), so the existing
// Apply / Save-settings code paths need no changes -- the picker just keeps
// that hidden input in sync, and re-reads it whenever code sets its .value.
const LITELLM_LABEL = "LiteLLM (any provider)";
let _litellmCatalogPromise = null;
function litellmCatalog(force){
  if (!_litellmCatalogPromise || force) _litellmCatalogPromise = api("litellm_catalog");
  return _litellmCatalogPromise;
}

function attachLitellmPicker(opts){
  const provSel = document.getElementById(opts.providerSelId);
  const modelInput = document.getElementById(opts.modelInputId);
  if (!provSel || !modelInput || modelInput.dataset.llAttached) return;
  modelInput.dataset.llAttached = "1";
  const pfx = opts.prefix;
  const inputStyle = "height:32px;border-radius:16px;border:1px solid var(--border2);background:var(--surface);color:var(--text);padding:0 10px;width:100%;box-sizing:border-box;";
  const smallBtn = "height:22px;font-size:10px;padding:0 8px;border-radius:11px;";

  const picker = document.createElement("div");
  picker.style.cssText = "display:none;flex-direction:column;gap:6px;margin-top:2px;";
  picker.innerHTML = `
    <div class="ll-warn" style="display:none;font-size:11px;padding:8px 10px;border-radius:10px;border:1px solid var(--border2);background:var(--surface);">
      <div>The <b>litellm</b> package isn't installed yet.</div>
      <button class="btn ll-install" style="margin-top:6px;background:var(--accent);color:#fff;">Install LiteLLM</button>
      <div class="ll-install-msg" style="font-size:10px;color:var(--subtext);margin-top:4px;"></div>
    </div>
    <div style="font-size:10px;color:var(--subtext);">Use any AI service through LiteLLM. Pick a service, a model, add your API key — done.</div>
    <div class="field-label" style="margin-top:2px;">1 · SERVICE</div>
    <input class="ll-svc" list="${pfx}-ll-svc-list" placeholder="Search all services… (openai, anthropic, groq, mistral…)" autocomplete="off" spellcheck="false" style="${inputStyle}"/>
    <datalist id="${pfx}-ll-svc-list"></datalist>
    <div class="ll-chips" style="display:flex;flex-wrap:wrap;gap:4px;"></div>
    <div class="hdr-row" style="margin-top:2px;"><div class="field-label">2 · MODEL</div>
      <button class="ghost-btn ll-fetch" title="Ask this service which models your key can use" style="${smallBtn}">${icon("refresh")} Fetch list</button></div>
    <input class="ll-mdl" list="${pfx}-ll-mdl-list" placeholder="Pick or type a model name" autocomplete="off" spellcheck="false" style="${inputStyle}"/>
    <datalist id="${pfx}-ll-mdl-list"></datalist>
    <div class="field-label" style="margin-top:2px;">3 · API KEY</div>
    <div style="display:flex;gap:6px;">
      <input class="ll-key" type="password" placeholder="Paste API key" autocomplete="off" spellcheck="false" style="${inputStyle}flex:1;"/>
      <button class="btn ll-savekey" style="background:var(--accent);color:#fff;">Save</button>
    </div>
    <div class="ll-keystatus" style="font-size:10px;color:var(--subtext);"></div>
    <details class="ll-adv">
      <summary style="font-size:10px;color:var(--subtext);cursor:pointer;">Advanced — custom Base URL (self-hosted / OpenAI-compatible)</summary>
      <input class="ll-base" placeholder="Optional" autocomplete="off" spellcheck="false" style="${inputStyle}margin-top:6px;"/>
      <div style="font-size:9px;color:var(--muted);margin-top:4px;">Saved together with the key above. Leave empty to use the service's normal endpoint.</div>
    </details>
    <div class="ll-note" style="font-size:10px;color:var(--muted);"></div>
    <div style="display:flex;align-items:center;justify-content:space-between;gap:6px;margin-top:2px;">
      <div style="font-size:10px;color:var(--subtext);min-width:0;">Will use: <code class="ll-preview" style="word-break:break-all;">—</code></div>
      <button class="ghost-btn ll-test" style="${smallBtn}flex:none;">Test</button>
    </div>
    <div class="ll-msg" style="font-size:10px;color:var(--subtext);min-height:12px;"></div>
  `;
  modelInput.insertAdjacentElement("afterend", picker);

  const q = (s)=>picker.querySelector(s);
  const svcIn = q(".ll-svc"), mdlIn = q(".ll-mdl"), keyIn = q(".ll-key"), baseIn = q(".ll-base");
  const statusEl = q(".ll-keystatus"), noteEl = q(".ll-note"), prevEl = q(".ll-preview");
  const msgEl = q(".ll-msg"), warnEl = q(".ll-warn"), advEl = q(".ll-adv"), chipsEl = q(".ll-chips");
  const nativeVal = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
  let writing = false, currentSvc = null, listsFilled = false, knownIds = new Set();

  const compose = ()=>{
    const s = svcIn.value.trim(), m = mdlIn.value.trim();
    return s && m ? `${s}/${m}` : m;
  };
  function writeBack(){
    writing = true; nativeVal.set.call(modelInput, compose()); writing = false;
    prevEl.textContent = compose() || "—";
  }
  function fillList(id, values, labels){
    const dl = document.getElementById(id);
    if (!dl) return;
    dl.innerHTML = "";
    (values||[]).forEach(v=>{
      const o = document.createElement("option"); o.value = v;
      if (labels && labels[v] && labels[v] !== v) o.label = labels[v];
      dl.appendChild(o);
    });
  }
  function renderStatus(st){
    let html;
    if (st.has_key) html = `<span style="color:#34d399;">${icon("check")} Key saved (${escapeHtml(st.key_hint || "")})</span>`;
    else if (st.env_set) html = `<span style="color:#34d399;">${icon("check")} Using ${escapeHtml(st.env_var)} from your environment</span>`;
    else if (!st.needs_key) html = "No API key needed for this service.";
    else html = `<span style="color:#fbbf24;">No key yet</span> — paste one above` + (st.env_var ? `, or set <code>${escapeHtml(st.env_var)}</code>` : "") + ".";
    statusEl.innerHTML = html;
    noteEl.textContent = st.note || "";
    keyIn.placeholder = st.has_key ? "Replace saved key…" : "Paste API key";
    baseIn.placeholder = st.base_hint ? `e.g. ${st.base_hint}` : "Optional";
    baseIn.value = st.api_base || "";
    advEl.open = !!st.api_base || svcIn.value.trim() === "azure";
  }

  async function loadService(svc, prefillModel){
    currentSvc = svc;
    msgEl.textContent = "";
    if (!svc){
      fillList(`${pfx}-ll-mdl-list`, []); statusEl.textContent = ""; noteEl.textContent = "";
      return;
    }
    if (knownIds.size && !knownIds.has(svc)){
      noteEl.textContent = `"${svc}" isn't in LiteLLM's provider list — double-check the spelling.`;
    }
    const [models, st] = await Promise.all([
      api("litellm_provider_models", svc), api("litellm_provider_status", svc),
    ]);
    if (currentSvc !== svc) return;   // user already moved on
    fillList(`${pfx}-ll-mdl-list`, models || []);
    renderStatus(st);
    if (prefillModel && !mdlIn.value.trim() && st.suggested_model){
      mdlIn.value = st.suggested_model;
      writeBack();
    }
  }

  function chooseService(svc){
    svc = (svc || "").trim();
    if (svc === currentSvc) return;
    svcIn.value = svc;
    mdlIn.value = "";          // models don't carry over between services
    writeBack();
    loadService(svc, true);
  }

  async function refresh(){
    const on = provSel.value === LITELLM_LABEL;
    picker.style.display = on ? "flex" : "none";
    modelInput.style.display = on ? "none" : "";
    // The Model tab has a "MODEL  ⟳" header row above the input; the picker
    // brings its own numbered labels (and its own fetch button), so hide it.
    const hdr = modelInput.previousElementSibling;
    if (hdr && hdr.classList.contains("hdr-row")) hdr.style.display = on ? "none" : "";
    if (!on) return;

    const cat = await litellmCatalog();
    warnEl.style.display = cat.available ? "none" : "block";
    if (!listsFilled && cat.providers && cat.providers.length){
      listsFilled = true;
      const labels = {};
      cat.providers.forEach(p=>{ labels[p.id] = p.label; knownIds.add(p.id); });
      fillList(`${pfx}-ll-svc-list`, cat.providers.map(p=>p.id), labels);
      cat.providers.filter(p=>p.popular).slice(0, 9).forEach(p=>{
        const b = document.createElement("button");
        b.type = "button"; b.className = "ghost-btn"; b.textContent = p.label.replace(/ \(.*\)$/, "");
        b.style.cssText = smallBtn; b.title = p.id;
        b.onclick = ()=>chooseService(p.id);
        chipsEl.appendChild(b);
      });
    }

    // Re-read the composed id (set by loadSettingsPanel / select_provider /
    // saved settings) and split it back into service + model.
    const full = (nativeVal.get.call(modelInput) || "").trim();
    const i = full.indexOf("/");
    const svc = i > 0 ? full.slice(0, i) : "";
    const mdl = i > 0 ? full.slice(i + 1) : full;
    svcIn.value = svc; mdlIn.value = mdl;
    prevEl.textContent = full || "—";
    if (svc !== currentSvc) loadService(svc, false);
  }

  // Any code that does `modelInput.value = ...` (init, provider switch,
  // loading saved settings) re-syncs the picker automatically.
  Object.defineProperty(modelInput, "value", {
    get(){ return nativeVal.get.call(modelInput); },
    set(v){ nativeVal.set.call(modelInput, v); if (!writing) refresh(); },
    configurable: true,
  });
  provSel.addEventListener("change", refresh);

  svcIn.addEventListener("change", ()=>chooseService(svcIn.value));
  mdlIn.addEventListener("input", writeBack);

  async function saveCreds(){
    const svc = svcIn.value.trim();
    if (!svc){ msgEl.textContent = "Choose a service first."; return false; }
    const r = await api("litellm_save_credentials", svc, keyIn.value.trim() || null, baseIn.value.trim());
    if (!r.ok){ msgEl.textContent = `Couldn't save: ${r.error}`; return false; }
    keyIn.value = "";
    renderStatus(await api("litellm_provider_status", svc));
    msgEl.textContent = "Saved.";
    return true;
  }
  q(".ll-savekey").onclick = saveCreds;

  q(".ll-fetch").onclick = async ()=>{
    const svc = svcIn.value.trim();
    if (!svc){ msgEl.textContent = "Choose a service first."; return; }
    if (keyIn.value.trim() && !(await saveCreds())) return;
    msgEl.textContent = "Fetching…";
    const r = await api("litellm_refresh_models", svc);
    fillList(`${pfx}-ll-mdl-list`, r.models || []);
    msgEl.textContent = r.live
      ? `Loaded ${r.models.length} models from ${svc}. Click the Model box to browse.`
      : `Showing LiteLLM's built-in list (${(r.models||[]).length}). Add a key to fetch the live list.`;
  };

  q(".ll-test").onclick = async ()=>{
    const id = compose();
    if (!id){ msgEl.textContent = "Pick a service and model first."; return; }
    if (keyIn.value.trim() && !(await saveCreds())) return;
    msgEl.style.color = "var(--subtext)";
    msgEl.textContent = `Testing ${id}…`;
    const r = await api("litellm_test", id);
    msgEl.style.color = r.ok ? "#34d399" : "#f87171";
    msgEl.innerHTML = r.ok ? `${icon("check")} Works — replied "${escapeHtml(String(r.reply))}" in ${r.ms} ms. Now press Apply/Save.` : `${icon("x")} ${escapeHtml(String(r.error))}`;
  };

  q(".ll-install").onclick = async (e)=>{
    const btn = e.target, out = q(".ll-install-msg");
    btn.disabled = true; out.textContent = "Installing — this can take a minute…";
    const r = await api("litellm_install");
    btn.disabled = false;
    if (r.ok){ out.textContent = "Installed."; listsFilled = false; chipsEl.innerHTML = ""; await litellmCatalog(true); currentSvc = null; refresh(); }
    else out.textContent = r.error || "Install failed.";
  };

  refresh();
}

function buildParamsPane(box){
  box.innerHTML = `<div id="stats"></div>
    <button class="ghost-btn" id="stats-refresh" style="margin-top:8px;">Refresh</button>`;
  const rows = [["Model","model"],["Active Goal","goal"],["Workspace","workspace"],
    ["Gemini Research","gemini"],["Screen OCR","ocr"],["UI Automation","uia"],["Turn Count","turns"]];
  async function load(){
    const s = await api("get_status");
    const el = document.getElementById("stats");
    el.innerHTML = rows.map(([label,key])=>{
      let val = s[key];
      if (key==="model") val = `${s.provider} — ${s.model}`;
      if (["gemini","ocr","uia"].includes(key)) val = val ? `<span style="color:var(--green);">${icon("check-circle")} System Connected</span>` : `<span style="color:var(--yellow);">${icon("alert")} Unconnected</span>`;
      return `<div class="stat-row"><div class="stat-lbl">${label}</div><div class="stat-val">${val}</div></div>`;
    }).join("");
  }
  document.getElementById("stats-refresh").onclick = load;
  load();
}

function buildSysCorePane(box){
  box.innerHTML = `
    <div class="hdr-row">
      <select id="sc-select" style="flex:1;margin-right:6px;">
        ${["Master Memory","Session Memory","Instructions","Paths","Active Project","Scratchpad"]
          .map(o=>`<option>${o}</option>`).join("")}
      </select>
      <button class="btn" id="sc-save" style="background:var(--accent);color:#fff;">Save</button>
    </div>
    <textarea class="code-area" id="sc-box" style="margin-top:6px;"></textarea>`;
  const sel = document.getElementById("sc-select");
  const box2 = document.getElementById("sc-box");
  enhanceSelect(sel);
  async function load(){ const r = await api("get_sys_core", sel.value); box2.value = r.content; }
  sel.onchange = load;
  document.getElementById("sc-save").onclick = async ()=>{
    const r = await api("save_sys_core", sel.value, box2.value);
    if (!r.ok) showAlert(r.error, "Error");
  };
  load();
}

// ── PDF Source Viewer (read-only) ───────────────────────────────────────
// Same Midum-themed modal chrome as the Heading Tagger below (same PNG
// page rendering via get_pdf_page_image, same layout/colors), but with
// none of the tagging machinery -- no clickable line overlays, no level
// buttons, no side panel, no Save. Just Prev/Next page navigation and a
// Close button, for the chat window's "Open Source" action during an
// Explain Mode walkthrough so the person can glance at the real PDF
// alongside the narration without being able to edit its tagging.
function openPdfSourceViewer(name, startPageIndex){
  return new Promise((resolve)=>{
    const overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:1000;display:flex;align-items:center;justify-content:center;";
    const modal = document.createElement("div");
    modal.style.cssText = "width:92vw;height:88vh;background:var(--panel);border:1px solid var(--border2);border-radius:20px;display:flex;flex-direction:column;overflow:hidden;";
    modal.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border);">
        <button class="ghost-btn" id="pv-prev" style="height:28px;">${icon("chevron-left")} Prev</button>
        <div id="pv-page-label" style="font-size:12px;color:var(--subtext);">Page 1</div>
        <button class="ghost-btn" id="pv-next" style="height:28px;">Next ${icon("chevron-right")}</button>
        <div style="flex:1;font-size:11px;color:var(--subtext);">${escapeHtml(name)}</div>
        <button class="btn" id="pv-close" style="height:28px;background:var(--accent);color:#fff;">Close</button>
      </div>
      <div id="pv-canvas-wrap" style="flex:1;overflow:auto;background:#12161c;position:relative;text-align:center;padding:16px;">
        <img id="pv-img" style="display:block;max-width:none;margin:0 auto;"/>
      </div>`;
    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    let pageIndex = startPageIndex > 0 ? startPageIndex : 0;
    let pageCount = 1;

    async function renderPage(){
      const r = await api("get_pdf_page_image", name, pageIndex);
      if (!r.ok){ modal.querySelector("#pv-page-label").textContent = "Failed to render page: " + (r.error||""); return; }
      pageCount = r.page_count;
      pageIndex = r.page_index;
      modal.querySelector("#pv-page-label").textContent = `Page ${pageIndex+1} / ${pageCount}`;
      const img = modal.querySelector("#pv-img");
      img.src = r.data_url;
      img.style.width = r.width + "px";
      img.style.height = r.height + "px";
    }

    function goPrev(){ if (pageIndex>0){ pageIndex--; renderPage(); } }
    function goNext(){ if (pageIndex<pageCount-1){ pageIndex++; renderPage(); } }
    modal.querySelector("#pv-prev").onclick = goPrev;
    modal.querySelector("#pv-next").onclick = goNext;
    function closeViewer(){
      document.removeEventListener("keydown", onKeyDown);
      overlay.remove();
      resolve();
    }
    modal.querySelector("#pv-close").onclick = closeViewer;
    overlay.addEventListener("click", (e)=>{ if (e.target === overlay) closeViewer(); });

    // Arrow-key page navigation while this viewer is open -- Left/Right
    // (and Up/Down as an alias) step through pages, Escape closes, same
    // as clicking Prev/Next/Close. Ignored while a text input/textarea
    // elsewhere has focus so typing isn't hijacked.
    function onKeyDown(e){
      const tag = (document.activeElement && document.activeElement.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if (e.key === "ArrowLeft" || e.key === "ArrowUp"){ e.preventDefault(); goPrev(); }
      else if (e.key === "ArrowRight" || e.key === "ArrowDown"){ e.preventDefault(); goNext(); }
      else if (e.key === "Escape"){ e.preventDefault(); closeViewer(); }
    }
    document.addEventListener("keydown", onKeyDown);

    renderPage();
  });
}

// ── PDF Heading Tagger modal ────────────────────────────────────────────
// Renders the REAL PDF page (server-side via PyMuPDF, sent as a PNG data
// URL) with an invisible clickable overlay div per text line (positioned
// from that line's real bounding box). Click a line, tap a heading level
// button, and it's tagged -- Save calls save_pdf_headings, which
// overwrites the source's tagging with exactly this list. No structure
// is ever auto-detected; every heading comes from a line the user
// personally clicked and tagged.
function openPdfHeadingTagger(name){
  return new Promise((resolve)=>{
    const overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:1000;display:flex;align-items:center;justify-content:center;";
    const modal = document.createElement("div");
    modal.style.cssText = "width:92vw;height:88vh;background:var(--panel);border:1px solid var(--border2);border-radius:20px;display:flex;flex-direction:column;overflow:hidden;";
    modal.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border);">
        <button class="ghost-btn" id="pht-prev" style="height:28px;">${icon("chevron-left")} Prev</button>
        <div id="pht-page-label" style="font-size:12px;color:var(--subtext);">Page 1</div>
        <button class="ghost-btn" id="pht-next" style="height:28px;">Next ${icon("chevron-right")}</button>
        <div style="flex:1;font-size:11px;color:var(--subtext);">Click a line of text below, then tap a heading level to tag it.</div>
        <label style="display:flex;align-items:center;gap:5px;font-size:11px;color:var(--subtext);cursor:pointer;white-space:nowrap;" title="When on, tagging a line also finds every other line in the whole document with the same font/size/bold and tags it the same level automatically. Turn off to tag every line by hand.">
          <input type="checkbox" id="pht-auto-detect" checked style="cursor:pointer;"/> Auto-detect matching formatting
        </label>
        <button class="ghost-btn" id="pht-close" style="height:28px;">Cancel</button>
        <button class="btn" id="pht-save" style="height:28px;background:var(--accent);color:#fff;">Save</button>
      </div>
      <div style="flex:1;display:flex;overflow:hidden;">
        <div id="pht-canvas-wrap" style="flex:1;overflow:auto;background:#12161c;position:relative;text-align:center;padding:16px;">
          <div id="pht-img-wrap" style="position:relative;display:inline-block;">
            <img id="pht-img" style="display:block;max-width:none;"/>
          </div>
        </div>
        <div style="width:260px;border-left:1px solid var(--border);padding:12px;display:flex;flex-direction:column;gap:8px;overflow-y:auto;">
          <div id="pht-selected" style="font-size:11px;color:var(--subtext);">No line selected</div>
          <div style="display:flex;gap:4px;flex-wrap:wrap;">
            ${[1,2,3,4,5,6].map(l=>`<button class="ghost-btn pht-level-btn" data-level="${l}" style="width:38px;height:28px;padding:0;">H${l}</button>`).join("")}
          </div>
          <div id="pht-auto-status" style="font-size:10px;color:var(--accent);min-height:14px;"></div>
          <button class="ghost-btn" id="pht-untag" style="height:26px;font-size:10px;color:var(--red);">Untag selected line</button>
          <div style="font-size:9px;font-weight:700;color:var(--subtext);margin-top:8px;">TAGGED HEADINGS</div>
          <div id="pht-tagged-list" style="flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:4px;"></div>
        </div>
      </div>`;
    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    let pageIndex = 0;
    let pageCount = 1;
    let currentLines = [];
    let selectedLineId = null;
    // line_id -> {page, line_id, text, level}
    let headings = {};

    (async ()=>{
      const r = await api("get_pdf_source", name);
      (r.headings || []).forEach(h=>{ headings[h.line_id] = h; });
      await renderPage();
    })();

    function refreshTaggedList(){
      const listEl = modal.querySelector("#pht-tagged-list");
      const items = Object.values(headings).sort((a,b)=> a.page - b.page || a.line_id.localeCompare(b.line_id));
      listEl.innerHTML = items.map(h=>`<div style="display:flex;align-items:center;gap:6px;font-size:10px;color:var(--text);">
          <span style="color:var(--accent);flex-shrink:0;">H${h.level}</span>
          <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(h.text)}</span>
          <button class="pht-remove-tag" data-lid="${h.line_id}" style="background:none;border:none;color:var(--red);cursor:pointer;">${icon("x")}</button>
        </div>`).join("") || `<div style="color:var(--subtext);font-size:10px;">None yet.</div>`;
      listEl.querySelectorAll(".pht-remove-tag").forEach(btn=>{
        btn.onclick = ()=>{ delete headings[btn.dataset.lid]; refreshTaggedList(); renderOverlays(); };
      });
    }

    function renderOverlays(){
      const wrap = modal.querySelector("#pht-img-wrap");
      wrap.querySelectorAll(".pht-line-box").forEach(el=>el.remove());
      currentLines.forEach(line=>{
        const box = document.createElement("div");
        box.className = "pht-line-box";
        const tagged = !!headings[line.line_id];
        const selected = selectedLineId === line.line_id;
        box.style.cssText = `position:absolute;left:${line.x0}px;top:${line.y0}px;width:${line.x1-line.x0}px;height:${line.y1-line.y0}px;
          cursor:pointer;border:2px solid ${selected ? "var(--yellow)" : (tagged ? "var(--accent)" : "transparent")};
          background:${selected ? "rgba(245,158,11,.15)" : (tagged ? "rgba(96,165,250,.12)" : "transparent")};`;
        box.title = line.text;
        box.onclick = ()=>{
          selectedLineId = line.line_id;
          modal.querySelector("#pht-selected").textContent = `Selected (p.${line.page}): ${line.text.slice(0,120)}`;
          renderOverlays();
        };
        wrap.appendChild(box);
      });
    }

    async function renderPage(){
      const r = await api("get_pdf_page_image", name, pageIndex);
      if (!r.ok){ modal.querySelector("#pht-page-label").textContent = "Failed to render page: " + (r.error||""); return; }
      pageCount = r.page_count;
      pageIndex = r.page_index;
      currentLines = r.lines;
      selectedLineId = null;
      modal.querySelector("#pht-selected").textContent = "No line selected";
      modal.querySelector("#pht-page-label").textContent = `Page ${pageIndex+1} / ${pageCount}`;
      const img = modal.querySelector("#pht-img");
      img.src = r.data_url;
      img.style.width = r.width + "px";
      img.style.height = r.height + "px";
      renderOverlays();
      refreshTaggedList();
    }

    function goPrev(){ if (pageIndex>0){ pageIndex--; renderPage(); } }
    function goNext(){ if (pageIndex<pageCount-1){ pageIndex++; renderPage(); } }
    modal.querySelector("#pht-prev").onclick = goPrev;
    modal.querySelector("#pht-next").onclick = goNext;

    // Arrow-key page navigation, same as the read-only PDF Source Viewer --
    // Left/Right (Up/Down alias) step pages, ignored while a text
    // input/textarea has focus so it doesn't hijack typing elsewhere.
    function onKeyDown(e){
      const tag = (document.activeElement && document.activeElement.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if (e.key === "ArrowLeft" || e.key === "ArrowUp"){ e.preventDefault(); goPrev(); }
      else if (e.key === "ArrowRight" || e.key === "ArrowDown"){ e.preventDefault(); goNext(); }
    }
    document.addEventListener("keydown", onKeyDown);
    modal.querySelectorAll(".pht-level-btn").forEach(btn=>{
      btn.onclick = async ()=>{
        if (!selectedLineId) return;
        const line = currentLines.find(l=>l.line_id===selectedLineId);
        if (!line) return;
        const level = parseInt(btn.dataset.level,10);
        headings[selectedLineId] = { page: line.page, line_id: line.line_id, text: line.text, level };
        refreshTaggedList();
        renderOverlays();

        // Auto-detect matching formatting: optional (checkbox in the
        // toolbar, default on). Once this one line is tagged, ask the
        // backend to scan the WHOLE document (every page, not just this
        // one) for every other line sharing the same font/size/bold and
        // tag those at the same level too -- so tagging a single H1
        // picks up every other H1 in the document automatically. Turning
        // the checkbox off falls back to the original fully-manual
        // behaviour: only the clicked line gets tagged.
        const autoEl = modal.querySelector("#pht-auto-detect");
        const statusEl = modal.querySelector("#pht-auto-status");
        if (autoEl && autoEl.checked && line.size){
          statusEl.textContent = "Scanning document for matching formatting…";
          const exclude = Object.values(headings).map(h=>({ page: h.page, line_id: h.line_id }));
          const style = { font: line.font, size: line.size, bold: line.bold };
          try {
            const res = await api("auto_tag_pdf_headings_by_style", name, style, exclude);
            if (res.ok && res.lines && res.lines.length){
              res.lines.forEach(m=>{
                if (!headings[m.line_id]){
                  headings[m.line_id] = { page: m.page, line_id: m.line_id, text: m.text, level };
                }
              });
              refreshTaggedList();
              renderOverlays();
              statusEl.textContent = `Auto-tagged ${res.lines.length} more line(s) as H${level} with matching formatting.`;
            } else if (res.ok){
              statusEl.textContent = "No other lines found with matching formatting.";
            } else {
              statusEl.textContent = res.error || "Auto-detect failed.";
            }
          } catch (e) {
            statusEl.textContent = "Auto-detect failed.";
          }
        }
      };
    });
    modal.querySelector("#pht-untag").onclick = ()=>{
      if (!selectedLineId) return;
      delete headings[selectedLineId];
      refreshTaggedList();
      renderOverlays();
    };
    modal.querySelector("#pht-close").onclick = ()=>{ document.removeEventListener("keydown", onKeyDown); overlay.remove(); resolve(false); };
    modal.querySelector("#pht-save").onclick = async ()=>{
      const list = Object.values(headings);
      const res = await api("save_pdf_headings", name, list);
      if (!res.ok){ await showAlert(res.message, "Error"); return; }
      document.removeEventListener("keydown", onKeyDown);
      overlay.remove();
      resolve(true);
    };
  });
}

function buildKnowledgePane(box){
  box.innerHTML = `
    <div class="hdr-row">
      <select id="kb-select" style="flex:1;margin-right:6px;"></select>
      <button class="btn" id="kb-save" style="background:var(--accent);color:#fff;margin-right:6px;">Save</button>
      <button class="ghost-btn" id="kb-new">+ New</button>
    </div>
    <textarea class="code-area" id="kb-box" style="margin-top:6px;"></textarea>
    <div class="hdr-row" style="margin-top:14px;">
      <select id="pdf-select" style="flex:1;margin-right:6px;"></select>
      <button class="ghost-btn" id="pdf-tag" style="margin-right:6px;">${icon("tag")} Tag Headings</button>
      <button class="ghost-btn" id="pdf-add">+ Add PDF Source</button>
    </div>
    <div class="hdr-row" style="margin-top:6px;">
      <button class="ghost-btn" id="pdf-warm" style="flex:1;" title="Extracts and caches text for every PDF source so Explain Mode parts load instantly. This can be slow for large/many PDFs, so it only runs when you click it.">${icon("zap")} Load Sources</button>
    </div>
    <div id="pdf-levels" style="margin-top:6px;background:var(--surface);border:1px solid var(--border);
         border-radius:14px;padding:8px 10px;font-size:11px;"></div>
    <div class="pdf-tree" id="pdf-tree" style="margin-top:6px;overflow-y:auto;max-height:180px;
         background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:8px 10px;
         font-size:11px;"></div>`;
  const sel = document.getElementById("kb-select");
  const box2 = document.getElementById("kb-box");
  enhanceSelect(sel);
  async function refresh(selectName){
    const files = await api("list_knowledge_files");
    sel.innerHTML = "";
    if (!files.length){ sel.innerHTML = `<option>No custom bases found</option>`; box2.value = "(Create a new Knowledge Base to begin writing)"; return; }
    files.forEach(f=>{ const o=document.createElement("option"); o.textContent=f; sel.appendChild(o); });
    sel.value = selectName && files.includes(selectName) ? selectName : files[0];
    await load();
  }
  async function load(){
    if (sel.value === "No custom bases found") return;
    const r = await api("get_knowledge_file", sel.value); box2.value = r.content;
  }
  sel.onchange = load;
  document.getElementById("kb-save").onclick = async ()=>{
    if (sel.value === "No custom bases found") return;
    await api("save_knowledge_file", sel.value, box2.value);
  };
  document.getElementById("kb-new").onclick = async ()=>{
    const name = await showPrompt("Knowledge base name:", "New Knowledge Base"); if (!name) return;
    const desc = (await showPrompt("Short description:", "New Knowledge Base")) || "";
    const r = await api("create_knowledge", name, desc);
    if (!r.ok) showAlert(r.error, "Error"); else refresh(r.filename);
  };
  refresh();

  // ── PDF Sources ──────────────────────────────────────────────────────
  const pdfSel = document.getElementById("pdf-select");
  const pdfTree = document.getElementById("pdf-tree");
  const pdfLevels = document.getElementById("pdf-levels");
  const pdfAddBtn = document.getElementById("pdf-add");
  const pdfTagBtn = document.getElementById("pdf-tag");
  const pdfWarmBtn = document.getElementById("pdf-warm");
  enhanceSelect(pdfSel);

  pdfWarmBtn.onclick = async ()=>{
    pdfWarmBtn.disabled = true;
    pdfWarmBtn.innerHTML = icon("zap") + " Loading sources…";
    const r = await api("warm_pdf_sources");
    if (!r || !r.ok){
      pdfWarmBtn.disabled = false;
      pdfWarmBtn.innerHTML = icon("zap") + " Load Sources";
    }
    // On success the button stays disabled/"Loading…" until the backend
    // pushes a pdf_cache_warmed event (see onPdfCacheWarmed below), since
    // the extraction runs on a background thread and may take a while.
  };
  window.onPdfCacheWarmed = ()=>{
    if (!pdfWarmBtn.isConnected) return;
    pdfWarmBtn.disabled = false;
    pdfWarmBtn.innerHTML = icon("check") + " Sources Loaded";
    setTimeout(()=>{ if (pdfWarmBtn.isConnected) pdfWarmBtn.innerHTML = icon("zap") + " Load Sources"; }, 2500);
  };

  function renderHeadingsList(headings){
    if (!headings || !headings.length){
      return `<div style="color:var(--subtext);">No headings tagged yet -- click Tag Headings to open the PDF and tag some.</div>`;
    }
    return headings.map(h=>`<div style="padding:3px 0;display:flex;align-items:baseline;gap:6px;">
      <span style="color:var(--accent);font-size:9px;font-weight:700;flex-shrink:0;">H${h.level}</span>
      <span style="color:var(--text);">${escapeHtml(h.text||"(untitled)")}</span>
      <span style="color:var(--subtext);font-size:9px;margin-left:auto;flex-shrink:0;">p.${h.page ?? "?"}</span>
    </div>`).join("");
  }

  async function renderPartsPreview(name){
    const r = await api("get_pdf_source_parts", name);
    if (!r.ok || !r.parts || !r.parts.length) return "";
    const rows = r.parts.map(p=>`<div style="padding:2px 0;display:flex;gap:6px;font-size:10px;color:var(--subtext);">
        <span style="color:var(--accent);flex-shrink:0;">#${p.index}</span>
        <span style="flex-shrink:0;">${p.level ? "H"+p.level : "-"}</span>
        <span style="flex-shrink:0;">p.${p.page ?? "?"}</span>
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text);">${escapeHtml(p.heading||"")}</span>
        <span style="flex-shrink:0;">${p.line_count} line(s)</span>
      </div>`).join("");
    return `<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border);">
      <div style="font-size:9px;font-weight:700;color:var(--subtext);margin-bottom:4px;">EXPLAIN MODE PARTS (${r.parts.length})</div>
      ${rows}</div>`;
  }

  async function refreshPdf(selectName){
    const files = await api("list_pdf_sources");
    pdfSel.innerHTML = "";
    if (!files.length){
      pdfSel.innerHTML = `<option>No PDF sources found</option>`;
      pdfTree.innerHTML = `<div style="color:var(--subtext);">Add a PDF to tag its headings here.</div>`;
      pdfLevels.innerHTML = "";
      return;
    }
    files.forEach(f=>{ const o=document.createElement("option"); o.textContent=f; pdfSel.appendChild(o); });
    pdfSel.value = selectName && files.includes(selectName) ? selectName : files[0];
    await loadPdf();
  }

  async function loadPdf(){
    if (pdfSel.value === "No PDF sources found") return;
    pdfTree.innerHTML = `<div style="color:var(--subtext);">Loading...</div>`;
    pdfLevels.innerHTML = "";
    const r = await api("get_pdf_source", pdfSel.value);
    if (!r.ok){ pdfTree.innerHTML = `<div style="color:#e5576c;">${escapeHtml(r.error||"Failed to load source.")}</div>`; return; }

    const headerHtml = `<div style="font-weight:700;margin-bottom:6px;color:var(--text);">${escapeHtml(r.title||pdfSel.value)}
      <span style="font-weight:400;color:var(--subtext);">(${r.page_count ?? "?"} pages)</span></div>`;
    pdfTree.innerHTML = headerHtml + renderHeadingsList(r.headings) + await renderPartsPreview(pdfSel.value);

    if (!r.available_levels || !r.available_levels.length){
      pdfLevels.innerHTML = `<div style="color:var(--subtext);">Tag some headings first, then pick which level(s) split this source into Explain Mode parts.</div>`;
      return;
    }
    const currentLevels = {};
    (r.part_levels || []).forEach(l=>currentLevels[l]=true);
    pdfLevels.innerHTML = `<div style="font-size:9px;font-weight:700;color:var(--subtext);margin-bottom:6px;">PART BOUNDARY LEVEL(S) -- set once for this source</div>`
      + `<div style="display:flex;gap:12px;flex-wrap:wrap;">`
      + r.available_levels.map(lvl=>`<label style="display:flex;align-items:center;gap:4px;cursor:pointer;color:var(--text);">
          <input type="checkbox" class="pdf-level-cb" value="${lvl}" ${currentLevels[lvl]?"checked":""}/> H${lvl}
        </label>`).join("")
      + `</div><button class="ghost-btn" id="pdf-levels-save" style="margin-top:8px;height:24px;font-size:10px;">Save Part Levels</button>`;
    document.getElementById("pdf-levels-save").onclick = async ()=>{
      const levels = Array.from(pdfLevels.querySelectorAll(".pdf-level-cb:checked")).map(el=>parseInt(el.value, 10));
      const res = await api("set_pdf_source_part_levels", pdfSel.value, levels);
      if (!res.ok) showAlert(res.message, "Error"); else loadPdf();
    };
  }
  pdfSel.onchange = loadPdf;

  pdfTagBtn.onclick = async ()=>{
    if (pdfSel.value === "No PDF sources found") { await showAlert("Add a PDF source first.", "No Source"); return; }
    await openPdfHeadingTagger(pdfSel.value);
    await loadPdf();
  };

  pdfAddBtn.onclick = async ()=>{
    pdfAddBtn.disabled = true;
    const prevLabel = pdfAddBtn.textContent;
    pdfAddBtn.textContent = "Registering...";
    try {
      const r = await api("add_pdf_source");
      if (!r.ok){ if (r.error) showAlert(r.error, "Error"); return; }
      await refreshPdf(r.filename);
      await openPdfHeadingTagger(r.filename);
      await loadPdf();
    } finally {
      pdfAddBtn.disabled = false;
      pdfAddBtn.textContent = prevLabel;
    }
  };
  refreshPdf();
}

function buildSkillsPane(box){
  box.innerHTML = `
    <div class="hdr-row">
      <select id="sk-select" style="flex:1;margin-right:6px;"></select>
      <button class="btn" id="sk-save" style="background:var(--accent);color:#fff;margin-right:6px;">Save</button>
      <button class="ghost-btn" id="sk-new">+ New</button>
    </div>
    <textarea class="code-area" id="sk-box" style="margin-top:6px;"></textarea>`;
  const sel = document.getElementById("sk-select");
  const box2 = document.getElementById("sk-box");
  enhanceSelect(sel);
  async function refresh(selectName){
    const files = await api("list_skill_files");
    sel.innerHTML = "";
    if (!files.length){ sel.innerHTML = `<option>No custom skills found</option>`; box2.value = "(Create a new Skill to begin writing custom logic)"; return; }
    files.forEach(f=>{ const o=document.createElement("option"); o.textContent=f; sel.appendChild(o); });
    sel.value = selectName && files.includes(selectName) ? selectName : files[0];
    await load();
  }
  async function load(){
    if (sel.value === "No custom skills found") return;
    const r = await api("get_skill_file", sel.value); box2.value = r.content;
  }
  sel.onchange = load;
  document.getElementById("sk-save").onclick = async ()=>{
    if (sel.value === "No custom skills found") return;
    await api("save_skill_file", sel.value, box2.value);
  };
  document.getElementById("sk-new").onclick = async ()=>{
    const name = await showPrompt("Skill name:", "New Skill"); if (!name) return;
    const domain = (await showPrompt("Domain:", "New Skill", "general")) || "general";
    const desc = (await showPrompt("Short description:", "New Skill")) || "";
    const r = await api("create_skill", name, domain, desc);
    if (!r.ok) showAlert(r.error, "Error"); else refresh(r.filename);
  };
  refresh();
}

function buildToolsPane(box){
  box.innerHTML = `
    <div class="hdr-row">
      <select id="tool-select" style="flex:1;margin-right:6px;"></select>
      <button class="btn" id="tool-run" style="background:var(--accent);color:#fff;">${icon("play")} Run</button>
    </div>
    <div class="hdr-row" style="margin-top:6px;">
      <select id="flow-select" style="flex:1;margin-right:6px;"></select>
      <button class="btn" id="flow-run" style="background:var(--accent2);color:#fff;">${icon("play")} Run Flow</button>
    </div>
    <div class="tools-args" id="tool-args"></div>
    <div class="tools-args" id="flow-args"></div>
    <textarea class="code-area" id="tool-output" readonly style="color:var(--tool-text);background:var(--tool-bg);"></textarea>`;
  const sel = document.getElementById("tool-select");
  const flowSel = document.getElementById("flow-select");
  const argsBox = document.getElementById("tool-args");
  const flowArgsBox = document.getElementById("flow-args");
  enhanceSelect(sel);
  enhanceSelect(flowSel);
  let schemas = [];
  let flowSchemas = [];
  // Shared by both the native-tool args box and the flow args box --
  // renders one row per JSON-schema property into `box`, using `argAttr`
  // as the data attribute each row's input/select is tagged with so the
  // Run handler can find just its own box's fields (tool args and flow
  // args are collected and submitted separately).
  function buildArgFields(box, props, required, argAttr, emptyText){
    box.innerHTML = "";
    if (!Object.keys(props).length){ box.innerHTML = `<div style="font-size:11px;color:var(--subtext);">${emptyText}</div>`; return; }
    Object.entries(props).forEach(([argName, details])=>{
      const row = document.createElement("div"); row.className = "arg-row";
      const req = required.includes(argName) ? " *" : "";
      let inputHtml;
      if (details.enum){
        inputHtml = `<select ${argAttr}="${argName}">${details.enum.map(e=>`<option>${e}</option>`).join("")}</select>`;
      } else {
        inputHtml = `<input ${argAttr}="${argName}" placeholder="${escapeHtml(details.description||"")}" type="${details.type==='integer'||details.type==='number'?'number':'text'}"/>`;
      }
      row.innerHTML = `<label>${escapeHtml(argName)}${req}</label>${inputHtml}`;
      box.appendChild(row);
      const enumSel = row.querySelector(`select[${argAttr}]`);
      if (enumSel) enhanceSelect(enumSel);
    });
  }
  function buildArgs(name){
    const schema = schemas.find(s=>s.name===name);
    if (!schema){ argsBox.innerHTML = ""; return; }
    buildArgFields(argsBox, schema.properties || {}, schema.required || [], "data-arg", "No arguments required.");
  }
  // Flow arguments come from that flow's Argument nodes (see
  // flows.flow_arguments() / list_flow_schemas()'s properties/required) --
  // same shape as a native tool's schema, so the same row-builder applies.
  function buildFlowArgs(name){
    const schema = flowSchemas.find(s=>s.name===name);
    if (!schema){ flowArgsBox.innerHTML = ""; return; }
    buildArgFields(flowArgsBox, schema.properties || {}, schema.required || [], "data-flow-arg", "This flow takes no arguments.");
  }
  (async ()=>{
    schemas = await api("list_tool_schemas");
    sel.innerHTML = schemas.map(s=>`<option>${s.name}</option>`).join("");
    if (schemas.length) buildArgs(schemas[0].name);
  })();
  (async ()=>{
    flowSchemas = await api("list_flow_schemas");
    flowSel.innerHTML = flowSchemas.length
      ? flowSchemas.map(s=>`<option value="${escapeHtml(s.name)}" title="${escapeHtml(s.description||'')}">${escapeHtml(s.name)}${s.promoted ? " [PROMOTED]" : ""}</option>`).join("")
      : `<option value="">No saved flows</option>`;
    if (flowSchemas.length) buildFlowArgs(flowSchemas[0].name);
  })();
  sel.onchange = ()=>buildArgs(sel.value);
  flowSel.onchange = ()=>buildFlowArgs(flowSel.value);
  document.getElementById("tool-run").onclick = async ()=>{
    const args = {};
    argsBox.querySelectorAll("[data-arg]").forEach(el=>{ if (el.value) args[el.dataset.arg] = el.value; });
    const r = await api("run_tool", sel.value, args);
    document.getElementById("tool-output").value = r.output;
  };
  document.getElementById("flow-run").onclick = async ()=>{
    if (!flowSel.value || flowSel.value === "No saved flows") return;
    const args = {};
    flowArgsBox.querySelectorAll("[data-flow-arg]").forEach(el=>{ if (el.value) args[el.dataset.flowArg] = el.value; });
    const r = await api("run_flow", flowSel.value, args);
    document.getElementById("tool-output").value = r.output;
  };
}

// ── Schedule pane -------------------------------------------------------------
// Lets the user schedule a saved Flow to run automatically while this app
// is open (see midum_pkg/scheduler.py). Pure config UI over Api.list_schedules
// / create_schedule / set_schedule_enabled / delete_schedule -- the actual
// tick loop runs server-side and is started once at app launch.
const SCHEDULE_WEEKDAYS = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];

async function buildSchedulePane(box){
  box.innerHTML = `
    <div class="hdr-row" id="sched-toolbar" style="flex-wrap:wrap;gap:6px;">
      <select id="sched-flow-select" style="width:160px;" title="Flow to run"></select>
      <select id="sched-kind-select" style="width:110px;" title="Schedule type">
        <option value="once">Once</option>
        <option value="interval">Interval</option>
        <option value="daily">Daily</option>
        <option value="weekly">Weekly</option>
      </select>
      <span id="sched-fields" style="display:flex;gap:6px;align-items:center;"></span>
      <button class="btn" id="sched-create" style="background:var(--accent);color:#fff;">+ Schedule</button>
      <button class="ghost-btn" id="sched-refresh" title="Refresh">${icon("refresh")}</button>
    </div>
    <div style="font-size:11px;color:var(--subtext);padding:6px 14px 0;">
      Scheduled Flows only run while Midum is open -- there's no background service. Missed runs while closed are not queued up; they just reschedule from whenever the app next opens.
    </div>
    <div id="sched-list" style="flex:1;overflow:auto;padding:10px 14px;display:flex;flex-direction:column;gap:8px;"></div>
  `;

  const flowSel = document.getElementById("sched-flow-select");
  const kindSel = document.getElementById("sched-kind-select");
  const fieldsBox = document.getElementById("sched-fields");
  const listBox = document.getElementById("sched-list");
  enhanceSelect(flowSel);
  enhanceSelect(kindSel);

  (async ()=>{
    let names = [];
    try { names = await api("list_flows"); } catch (e) { names = []; }
    flowSel.innerHTML = names.length
      ? names.map(n=>`<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("")
      : `<option value="">No saved flows</option>`;
  })();

  function renderKindFields(){
    const kind = kindSel.value;
    if (kind === "once"){
      fieldsBox.innerHTML = `<input type="datetime-local" id="sched-run-at" style="height:24px;font-size:11px;"/>`;
    } else if (kind === "interval"){
      fieldsBox.innerHTML = `<input type="number" id="sched-every-min" min="1" value="60" style="height:24px;width:70px;font-size:11px;"/><span style="font-size:11px;color:var(--subtext);">min</span>`;
    } else if (kind === "daily"){
      fieldsBox.innerHTML = `<input type="time" id="sched-at-time" value="09:00" style="height:24px;font-size:11px;"/>`;
    } else if (kind === "weekly"){
      fieldsBox.innerHTML = `<input type="time" id="sched-at-time" value="09:00" style="height:24px;font-size:11px;"/>` +
        SCHEDULE_WEEKDAYS.map((d,i)=>`<label style="font-size:10px;display:flex;gap:2px;align-items:center;"><input type="checkbox" class="sched-day" value="${i}" checked/>${d}</label>`).join("");
    }
  }
  kindSel.onchange = renderKindFields;
  renderKindFields();

  function fmtWhen(iso){
    if (!iso) return "—";
    try { return new Date(iso).toLocaleString(); } catch (e) { return iso; }
  }

  async function refreshList(){
    let scheds = [];
    try { scheds = await api("list_schedules"); } catch (e) { scheds = []; }
    if (!scheds.length){
      listBox.innerHTML = `<div style="font-size:12px;color:var(--subtext);padding:20px;text-align:center;">No schedules yet -- pick a flow above and click + Schedule.</div>`;
      return;
    }
    listBox.innerHTML = "";
    scheds.forEach(s=>{
      const card = document.createElement("div");
      card.className = "mcp-tool-card";
      card.style.cssText = "padding:10px 12px;border:1px solid var(--border2);border-radius:10px;background:var(--surface);";
      card.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
          <div>
            <div style="font-weight:600;font-size:12px;">${icon("link")} ${escapeHtml(s.flow_name)}</div>
            <div style="font-size:11px;color:var(--subtext);">${escapeHtml(s.description || s.kind)}</div>
          </div>
          <div style="display:flex;gap:6px;align-items:center;">
            <label style="font-size:10px;display:flex;gap:4px;align-items:center;">
              <input type="checkbox" class="sched-enabled" ${s.enabled ? "checked" : ""}/> enabled
            </label>
            <button class="mini-btn del" data-act="delete">${icon("trash")}</button>
          </div>
        </div>
        <div style="font-size:10px;color:var(--subtext);margin-top:6px;display:flex;gap:14px;flex-wrap:wrap;">
          <span>Next: ${fmtWhen(s.next_run_at)}</span>
          <span>Last run: ${fmtWhen(s.last_run_at)}</span>
          ${s.last_result ? `<span title="${escapeHtml(s.last_result)}">Last result: ${escapeHtml((s.last_result||"").slice(0,60))}${(s.last_result||"").length>60?"…":""}</span>` : ""}
        </div>`;
      card.querySelector(".sched-enabled").onchange = async (e)=>{
        e.target.disabled = true;
        try { await api("set_schedule_enabled", s.id, e.target.checked); }
        finally { await refreshList(); }
      };
      card.querySelector('[data-act="delete"]').onclick = async ()=>{
        const ok = await showConfirm(`Delete this schedule for '${s.flow_name}'?`, "Delete Schedule", {danger:true, okLabel:"Delete"});
        if (!ok) return;
        await api("delete_schedule", s.id);
        await refreshList();
      };
      listBox.appendChild(card);
    });
  }

  document.getElementById("sched-create").onclick = async ()=>{
    if (!flowSel.value){ await showAlert("No saved flow selected -- create a Flow first in the Flows tab.", "No Flow"); return; }
    const kind = kindSel.value;
    const args = { flow_name: flowSel.value, kind };
    if (kind === "once"){
      const v = document.getElementById("sched-run-at").value;
      if (!v){ await showAlert("Pick a date/time first.", "Missing Time"); return; }
      args.run_at = new Date(v).toISOString();
    } else if (kind === "interval"){
      args.every_minutes = parseInt(document.getElementById("sched-every-min").value, 10) || 1;
    } else if (kind === "daily"){
      args.at_time = document.getElementById("sched-at-time").value || "09:00";
    } else if (kind === "weekly"){
      args.at_time = document.getElementById("sched-at-time").value || "09:00";
      args.days = Array.from(document.querySelectorAll(".sched-day:checked")).map(el=>parseInt(el.value, 10));
    }
    const btn = document.getElementById("sched-create");
    btn.disabled = true;
    try {
      const r = await api("create_schedule", args.flow_name, args.kind, args.run_at || null, args.every_minutes || null, args.at_time || null, args.days || null);
      if (!r.ok) await showAlert(r.message, "Schedule Failed");
      await refreshList();
    } finally {
      btn.disabled = false;
    }
  };
  document.getElementById("sched-refresh").onclick = refreshList;

  refreshList();
}

// ── Flows pane -------------------------------------------------------------
// Node-graph editor, built on Drawflow (https://github.com/jerosoler/Drawflow,
// MIT) rather than a from-scratch canvas system -- loaded lazily from CDN
// the first time this tab is opened. Left: a grouped node drawer (drag an
// item onto the canvas to place it). Right: the graph canvas itself, with
// pan/zoom and draggable connectors between nodes built in by the library.
// Currently seeded with just two node types (Start / End); more groups and
// node types can be added to FLOW_NODE_GROUPS without touching anything else.
const FLOW_NODE_GROUPS = [
  {
    group: "Control Flow",
    nodes: [
      { type: "start", label: "Start", icon: "play", inputs: 0, outputs: 1 },
      { type: "end",   label: "End",   icon: "stop", inputs: 1, outputs: 0 },
    ],
  },
  {
    group: "Logic",
    nodes: [
      { type: "logic::if", label: "If", icon: "shuffle", inputs: 2, outputs: 2, isLogic: true,
        pinLabels: { in: ["Sequence", "Value"], out: ["True", "False"] } },
      { type: "logic::loop", label: "Loop (For Each)", icon: "repeat", inputs: 2, outputs: 3, isLogic: true,
        pinLabels: { in: ["Sequence", "Iterable"], out: ["Body", "Item", "After"] } },
    ],
  },
  {
    group: "Variables",
    nodes: [
      { type: "variable", label: "Variable", icon: "braces", inputs: 1, outputs: 1, isVariable: true },
    ],
  },
  {
    group: "Arguments",
    nodes: [
      // Pure data source (no Sequence pin at all, like Variable) -- reads
      // one named key out of the flow's own `args` dict at run time (see
      // flows.py's _codegen_argument_declarations / _flow_coerce_arg).
      // This is what makes a flow's Argument nodes double as the flow's
      // external parameter list -- list_flow_schemas() reads them back out
      // of the saved graph to build the JSON-schema "properties"/"required"
      // the Tools tab (and, for a promoted flow, the model) sees.
      { type: "argument", label: "Argument", icon: "arrow-right", inputs: 0, outputs: 1, isArgument: true,
        pinLabels: { in: [], out: ["Value"] } },
    ],
  },
  {
    group: "Operations",
    nodes: [
      // Pure data source too (no Sequence pin) -- computes A <op> B inline
      // wherever it's referenced, so it can be wired straight into any
      // data pin (an If's Value, a Variable's value-in, another
      // Operation's A/B, a tool param, ...) without occupying a step of
      // its own in the sequence.
      { type: "op::calc", label: "Operation", icon: "zap", inputs: 2, outputs: 1, isOperation: true,
        pinLabels: { in: ["A", "B"], out: ["Result"] } },
    ],
  },
  {
    group: "Code",
    nodes: [
      // Sequence node (in: Sequence, out: Sequence, Result) -- runs the
      // user's own Python statements inlined directly into the flow
      // function's body (see flows.py's _codegen_python). Inside the code,
      // get_argument("name", default) reads a caller-supplied parameter
      // AND registers it as one of the flow's own external parameters,
      // exactly like an Argument node does (see flows.py's
      // _extract_get_argument_calls/flow_arguments) -- so a Python node
      // can define its own flow-level parameters without a separate
      // Argument node for each one. Assigning to a local `result` variable
      // exposes it on the Result pin for downstream wiring.
      { type: "python", label: "Python Code", icon: "code", inputs: 1, outputs: 2, isPython: true,
        pinLabels: { in: ["Sequence"], out: ["Sequence", "Result"] } },
    ],
  },
  {
    group: "Output",
    nodes: [
      // Sequence node (in: Sequence + Value, out: Sequence) -- the flow's
      // way of "returning" something visible: prints the wired-in Value
      // (or its typed fallback) and also records it as this step's result,
      // exactly like a tool call would (see flows.py's _codegen_print).
      { type: "print", label: "Print", icon: "terminal", inputs: 2, outputs: 1, isPrint: true,
        pinLabels: { in: ["Sequence", "Value"], out: ["Sequence"] } },
    ],
  },
  {
    group: "AI",
    nodes: [
      { type: "ai::prompt", label: "Prompt AI", icon: "bot", inputs: 2, outputs: 2, isAI: true,
        pinLabels: { in: ["Sequence", "Context"], out: ["Sequence", "Result"] } },
      { type: "ai::summarize", label: "Ask AI to Summarize", icon: "file-text", inputs: 2, outputs: 2, isAI: true,
        pinLabels: { in: ["Sequence", "Text"], out: ["Sequence", "Summary"] } },
      { type: "ai::choose", label: "Ask AI to Choose", icon: "target", inputs: 2, outputs: 2, isAI: true,
        pinLabels: { in: ["Sequence", "Options"], out: ["Sequence", "Choice"] } },
    ],
  },
];
const FLOW_NODE_DEFS = {};
FLOW_NODE_GROUPS.forEach(g => g.nodes.forEach(def => { FLOW_NODE_DEFS[def.type] = def; }));

// Per-pin labels so it's unambiguous which connection dot on a node maps
// to which property, instead of only the aggregate "in: seq, params /
// out: seq, object" summary line in the node body. Returns {in:[...],
// out:[...]} in the exact same order Drawflow numbers a node's pins
// (input_1, input_2, ... / output_1, output_2, ...) -- which is also the
// exact order `inputs`/`outputs` were passed to addNode() for that node,
// so index i here always lines up with the i-th dot Drawflow renders.
function _flowPinLabels(def){
  if (!def) return { in: [], out: [] };
  if (def.pinLabels) return def.pinLabels;               // logic::if / logic::loop / ai::*
  if (def.type === "start") return { in: [], out: ["Sequence"] };
  if (def.type === "end")   return { in: ["Sequence"], out: [] };
  if (def.isVariable)       return { in: ["Value"], out: ["Value"] };
  // Generic tool::/mcp::/flow:: node -- Sequence pin first, then one pin
  // per parameter (same order as `_flow_param_order`), matching how
  // buildFlowsPane computed `inputs: 1 + params.length` for this def.
  const paramLabels = (def.params || []).map(p => p.name);
  const outLabels = (def.kind && def.kind !== "action") ? ["Sequence", "Object"] : ["Sequence"];
  return { in: ["Sequence", ...paramLabels], out: outLabels };
}

// Stamps data-pin-label (+ a native title tooltip as a fallback) onto each
// of a node's actual Drawflow-rendered dot elements, in pin order.
function _applyPinLabels(nodeId, def){
  const nodeEl = document.getElementById(`node-${nodeId}`);
  if (!nodeEl) return;
  const labels = _flowPinLabels(def);
  const inputEls  = nodeEl.querySelectorAll(".inputs .input");
  const outputEls = nodeEl.querySelectorAll(".outputs .output");
  inputEls.forEach((el, i)=>{
    const label = labels.in[i] || `in ${i+1}`;
    el.setAttribute("data-pin-label", label);
    el.title = label;
  });
  outputEls.forEach((el, i)=>{
    const label = labels.out[i] || `out ${i+1}`;
    el.setAttribute("data-pin-label", label);
    el.title = label;
  });
}

// Re-labels every node currently on the canvas -- called after any bulk
// structural change (seeding a blank canvas, importing a saved flow's
// graph) rather than tracking individual new-node ids through those paths.
function _relabelAllPins(){
  if (!_drawflowEditor) return;
  const data = ((_drawflowEditor.drawflow || {}).drawflow || {}).Home?.data || {};
  Object.entries(data).forEach(([id, node])=>{
    const def = FLOW_NODE_DEFS[node.name];
    if (def) _applyPinLabels(id, def);
  });
}

// Wires the Pygments-highlight overlay for every not-yet-wired
// `.flow-code-textarea` under `root` (a Python Code node's editor -- see
// _flowNodeHtml's isPython branch) -- same Api.highlight_code bridge and
// same ".code-block" CSS the chat's own fenced code blocks use (see
// renderPendingCodeHighlight above), just re-run on a debounce as the
// user types instead of once per rendered chat message. Idempotent via
// data-hl-wired, so it's safe to call again after any bulk canvas change
// (seeding a blank canvas, importing a saved flow, dropping a new node)
// without double-wiring nodes that already have a listener attached.
function _wireFlowCodeHighlight(root){
  (root || document).querySelectorAll(".flow-code-textarea:not([data-hl-wired])").forEach(ta=>{
    ta.dataset.hlWired = "1";
    const pre = ta.previousElementSibling;
    if (!pre || !pre.classList.contains("flow-code-highlight")) return;
    let timer = null;
    const refresh = async ()=>{
      const code = ta.value;
      pre.textContent = code;   // immediate plain fallback while highlighting resolves
      try {
        const html = await api("highlight_code", code, "python");
        pre.innerHTML = html || escapeHtml(code);
      } catch (e) { /* Pygments unavailable -- leave the plain text already set */ }
    };
    ta.addEventListener("input", ()=>{ clearTimeout(timer); timer = setTimeout(refresh, 250); });
    ta.addEventListener("scroll", ()=>{ pre.scrollTop = ta.scrollTop; pre.scrollLeft = ta.scrollLeft; });
    refresh();
  });
}

const DRAWFLOW_CSS = "https://cdn.jsdelivr.net/npm/drawflow@0.0.59/dist/drawflow.min.css";
const DRAWFLOW_JS  = "https://cdn.jsdelivr.net/npm/drawflow@0.0.59/dist/drawflow.min.js";

function _loadStyleOnce(href){
  if (document.querySelector(`link[href="${href}"]`)) return;
  const l = document.createElement("link");
  l.rel = "stylesheet"; l.href = href;
  document.head.appendChild(l);
}
function _loadScriptOnce(src){
  return new Promise((resolve, reject)=>{
    if (window.Drawflow || document.querySelector(`script[src="${src}"]`)){ resolve(); return; }
    const s = document.createElement("script");
    s.src = src; s.onload = ()=>resolve(); s.onerror = ()=>reject(new Error("failed to load " + src));
    document.head.appendChild(s);
  });
}

// Every real DATA/SEQUENCE wire terminates at an actual Drawflow pin now
// (rendered by Drawflow itself along the node's edges, evenly spaced by
// pin count) -- the param rows below are just the manually-typed
// fallback value for a pin that isn't wired to anything, plus a text
// label kept in the same top-to-bottom order as the pins so the two line
// up visually. flows.py reads `_flow_param_order` (set in initialData
// below) to know which pin index maps to which parameter name.
function _flowNodeHtml(def){
  if (def.isVariable){
    return `<div class="flow-node-tool flow-node-variable">`
         + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${iconHtml(def.icon)}</span><span class="flow-node-label">Variable</span></div>`
         + `<div class="flow-node-params">`
         + `<div class="flow-param-row"><span class="flow-param-label">name</span><input class="flow-param-input" type="text" df-name placeholder="my_variable"/></div>`
         + `<div class="flow-param-row"><span class="flow-param-label">value</span><input class="flow-param-input" type="text" df-value placeholder="default (used if nothing wired in)"/></div>`
         + `</div>`
         + `<div class="flow-node-pin-hint"><span>in: value</span><span>out: value</span></div>`
         + `</div>`;
  }
  if (def.isArgument){
    return `<div class="flow-node-tool flow-node-variable">`
         + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${iconHtml(def.icon)}</span><span class="flow-node-label">Argument</span></div>`
         + `<div class="flow-node-params">`
         + `<div class="flow-param-row"><span class="flow-param-label">name</span><input class="flow-param-input" type="text" df-name placeholder="arg_name"/></div>`
         + `<div class="flow-param-row"><span class="flow-param-label">type</span>`
         + `<select class="flow-param-input" df-type>`
         + `<option value="string">string</option><option value="integer">integer</option>`
         + `<option value="number">number</option><option value="boolean">boolean</option>`
         + `</select></div>`
         + `<div class="flow-param-row"><span class="flow-param-label">required</span>`
         + `<select class="flow-param-input" df-required><option value="no">no</option><option value="yes">yes</option></select></div>`
         + `<div class="flow-param-row"><span class="flow-param-label">default</span><input class="flow-param-input" type="text" df-default placeholder="fallback if not passed in"/></div>`
         + `<div class="flow-param-row"><span class="flow-param-label">description</span><input class="flow-param-input" type="text" df-description placeholder="shown in the Tools tab"/></div>`
         + `</div>`
         + `<div class="flow-node-pin-hint"><span>in: \u2014</span><span>out: value</span></div>`
         + `</div>`;
  }
  if (def.isPython){
    // The textarea's own text/caret are transparent and sit ABOVE a
    // Pygments-highlighted <pre class="code-block"> (the same class/CSS
    // the chat's own fenced code blocks use, and highlighted through the
    // exact same Api.highlight_code bridge -- see _wireFlowCodeHighlight)
    // painted underneath -- the classic "transparent textarea over a
    // highlighted backdrop" trick, so what's on screen is colorized code
    // but typing/selecting/caret behavior is still the browser's own
    // plain textarea. df-code keeps Drawflow's normal two-way binding
    // (this node's saved `code` value) working exactly like every other
    // df-<param> field on other node types.
    return `<div class="flow-node-tool flow-node-logic">`
         + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${iconHtml(def.icon)}</span><span class="flow-node-label">Python Code</span></div>`
         + `<div class="flow-node-params">`
         + `<div class="flow-code-wrap" style="position:relative;">`
         + `<pre class="code-block flow-code-highlight" data-lang="python" style="position:absolute;inset:0;margin:0;padding:6px;pointer-events:none;white-space:pre-wrap;word-break:break-word;font-family:Consolas,'Cascadia Code',monospace;font-size:11px;line-height:1.4;overflow:hidden;background:transparent;border-radius:6px;"></pre>`
         + `<textarea class="flow-param-input flow-code-textarea" df-code spellcheck="false" placeholder="# use get_argument('name', default) to add a flow parameter\n# assign to result to expose a value on the Result pin" style="position:relative;z-index:1;display:block;width:100%;min-height:110px;box-sizing:border-box;font-family:Consolas,'Cascadia Code',monospace;font-size:11px;line-height:1.4;white-space:pre;overflow:auto;background:transparent;color:transparent;caret-color:var(--text);resize:vertical;"></textarea>`
         + `</div>`
         + `</div>`
         + `<div class="flow-node-pin-hint"><span>in: seq</span><span class="flow-object-out">out: seq, ${icon("dot")} result</span></div>`
         + `</div>`;
  }
  if (def.isPrint){
    return `<div class="flow-node-tool flow-node-logic">`
         + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${iconHtml(def.icon)}</span><span class="flow-node-label">Print</span></div>`
         + `<div class="flow-node-params">`
         + `<div class="flow-param-row"><span class="flow-param-label">value</span><input class="flow-param-input" type="text" df-value placeholder="fallback if Value pin unwired"/></div>`
         + `</div>`
         + `<div class="flow-node-pin-hint"><span>in: seq, value</span><span>out: seq</span></div>`
         + `</div>`;
  }
  if (def.isOperation){
    return `<div class="flow-node-tool flow-node-logic">`
         + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${iconHtml(def.icon)}</span><span class="flow-node-label">Operation</span></div>`
         + `<div class="flow-node-params">`
         + `<div class="flow-param-row"><span class="flow-param-label">op</span>`
         + `<select class="flow-param-input" df-op>`
         + `<option value="add">A + B</option><option value="subtract">A \u2212 B</option>`
         + `<option value="multiply">A \u00d7 B</option><option value="divide">A \u00f7 B</option>`
         + `<option value="equals">A == B</option><option value="not_equals">A != B</option>`
         + `<option value="greater_than">A &gt; B</option><option value="less_than">A &lt; B</option>`
         + `<option value="greater_equal">A &gt;= B</option><option value="less_equal">A &lt;= B</option>`
         + `<option value="and">A and B</option><option value="or">A or B</option>`
         + `<option value="concat">A + B (text)</option>`
         + `</select></div>`
         + `<div class="flow-param-row"><span class="flow-param-label">a</span><input class="flow-param-input" type="text" df-a placeholder="fallback if A unwired"/></div>`
         + `<div class="flow-param-row"><span class="flow-param-label">b</span><input class="flow-param-input" type="text" df-b placeholder="fallback if B unwired"/></div>`
         + `</div>`
         + `<div class="flow-node-pin-hint"><span>in: a, b</span><span>out: result</span></div>`
         + `</div>`;
  }
  if (def.isLogic && def.type === "logic::if"){
    return `<div class="flow-node-tool flow-node-logic">`
         + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${iconHtml(def.icon)}</span><span class="flow-node-label">If</span></div>`
         + `<div class="flow-node-params">`
         + `<div class="flow-param-row"><span class="flow-param-label">op</span>`
         + `<select class="flow-param-input" df-op>`
         + `<option value="truthy">is truthy</option><option value="equals">equals</option>`
         + `<option value="not_equals">not equals</option><option value="contains">contains</option>`
         + `<option value="greater_than">&gt;</option><option value="less_than">&lt;</option>`
         + `</select></div>`
         + `<div class="flow-param-row"><span class="flow-param-label">value</span><input class="flow-param-input" type="text" df-value placeholder="fallback if Value pin unwired"/></div>`
         + `<div class="flow-param-row"><span class="flow-param-label">compare</span><input class="flow-param-input" type="text" df-compare placeholder="compare against"/></div>`
         + `</div>`
         + `<div class="flow-node-pin-hint"><span>in: seq, value</span><span>out: true, false</span></div>`
         + `</div>`;
  }
  if (def.isLogic && def.type === "logic::loop"){
    return `<div class="flow-node-tool flow-node-logic">`
         + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${iconHtml(def.icon)}</span><span class="flow-node-label">Loop (For Each)</span></div>`
         + `<div class="flow-node-params">`
         + `<div class="flow-param-row"><span class="flow-param-label">item</span><input class="flow-param-input" type="text" df-item_var placeholder="item (label only)"/></div>`
         + `</div>`
         + `<div class="flow-node-pin-hint"><span>in: seq, iterable</span><span>out: body, item, after</span></div>`
         + `</div>`;
  }
  if (def.isAI && def.type === "ai::prompt"){
    return `<div class="flow-node-tool flow-node-ai">`
         + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${iconHtml(def.icon)}</span><span class="flow-node-label">Prompt AI</span></div>`
         + `<div class="flow-node-params">`
         + `<div class="flow-param-row"><span class="flow-param-label">prompt</span><input class="flow-param-input" type="text" df-prompt placeholder="Instruction to send to the AI"/></div>`
         + `<div class="flow-param-row"><span class="flow-param-label">context</span><input class="flow-param-input" type="text" df-context placeholder="fallback if Context pin unwired"/></div>`
         + `</div>`
         + `<div class="flow-node-pin-hint"><span>in: seq, context</span><span class="flow-object-out">out: seq, ${icon("dot")} result</span></div>`
         + `</div>`;
  }
  if (def.isAI && def.type === "ai::summarize"){
    return `<div class="flow-node-tool flow-node-ai">`
         + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${iconHtml(def.icon)}</span><span class="flow-node-label">Ask AI to Summarize</span></div>`
         + `<div class="flow-node-params">`
         + `<div class="flow-param-row"><span class="flow-param-label">text</span><input class="flow-param-input" type="text" df-text placeholder="fallback if Text pin unwired"/></div>`
         + `<div class="flow-param-row"><span class="flow-param-label">length</span>`
         + `<select class="flow-param-input" df-length>`
         + `<option value="short">short</option><option value="medium" selected>medium</option><option value="long">long</option>`
         + `</select></div>`
         + `</div>`
         + `<div class="flow-node-pin-hint"><span>in: seq, text</span><span class="flow-object-out">out: seq, ${icon("dot")} summary</span></div>`
         + `</div>`;
  }
  if (def.isAI && def.type === "ai::choose"){
    return `<div class="flow-node-tool flow-node-ai">`
         + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${iconHtml(def.icon)}</span><span class="flow-node-label">Ask AI to Choose</span></div>`
         + `<div class="flow-node-params">`
         + `<div class="flow-param-row"><span class="flow-param-label">question</span><input class="flow-param-input" type="text" df-question placeholder="What should the AI decide?"/></div>`
         + `<div class="flow-param-row"><span class="flow-param-label">options</span><input class="flow-param-input" type="text" df-options placeholder="fallback: comma,separated,options"/></div>`
         + `</div>`
         + `<div class="flow-node-pin-hint"><span>in: seq, options</span><span class="flow-object-out">out: seq, ${icon("dot")} choice</span></div>`
         + `</div>`;
  }
  if (!def.params){
    return `<div class="flow-node flow-node-${def.type}">`
         + `<div class="flow-node-icon">${iconHtml(def.icon)}</div>`
         + `<div class="flow-node-label">${escapeHtml(def.label)}</div>`
         + `</div>`;
  }
  const paramsHtml = def.params.length
    ? def.params.map(p=>{
        const req = p.required ? " required" : "";
        let field;
        if (p.enum){
          field = `<select class="flow-param-input" df-${escapeHtml(p.name)}>`
            + p.enum.map(e=>`<option value="${escapeHtml(String(e))}">${escapeHtml(String(e))}</option>`).join("")
            + `</select>`;
        } else {
          const inputType = (p.type === "integer" || p.type === "number") ? "number" : "text";
          field = `<input class="flow-param-input" type="${inputType}" df-${escapeHtml(p.name)} placeholder="${escapeHtml(p.description||p.type||'')}"/>`;
        }
        return `<div class="flow-param-row${req}" title="${escapeHtml(p.description||'')}">`
             + `<span class="flow-param-label">${escapeHtml(p.name)}</span>`
             + field
             + `</div>`;
      }).join("")
    : `<div class="flow-node-empty-params">No parameters</div>`;
  const objectOutHtml = def.kind && def.kind !== "action"
    ? `<div class="flow-node-pin-hint"><span>in: seq${def.params.length?', params':''}</span><span class="flow-object-out">out: seq, ${icon("dot")} object</span></div>`
    : `<div class="flow-node-pin-hint"><span>in: seq${def.params.length?', params':''}</span><span>out: seq</span></div>`;
  return `<div class="flow-node-tool">`
       + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${iconHtml(def.icon)}</span><span class="flow-node-label">${escapeHtml(def.label)}</span>`
       + (def.kind ? `<span class="flow-node-kind-badge flow-node-kind-${def.kind}">${def.kind}</span>` : "")
       + `</div>`
       + `<div class="flow-node-params">${paramsHtml}</div>`
       + objectOutHtml
       + `</div>`;
}

let _drawflowEditor = null;

async function buildFlowsPane(box){
  box.style.padding = "0";
  box.innerHTML = `
    <div id="flows-root">
      <div id="flow-drawer">
        <div id="flow-drawer-title">Node Drawer</div>
      </div>
      <div id="flow-canvas-wrap">
        <div class="hdr-row" id="flow-toolbar">
          <div class="section-label" id="flow-hint">DRAG NODES ONTO THE CANVAS</div>
          <div style="display:flex;gap:6px;align-items:center;">
            <select id="flow-load-select" title="Load a saved flow into the canvas for editing"
              style="height:24px;width:150px;border-radius:12px;border:1px solid var(--border2);background:var(--surface);color:var(--text);padding:0 8px;font-size:11px;">
              <option value="">New flow…</option>
            </select>
            <button class="ghost-btn" id="flow-new" style="height:24px;font-size:10px;">+ New</button>
            <input id="flow-name-input" placeholder="flow_function_name" maxlength="64" autocomplete="off"
              style="height:24px;width:140px;border-radius:12px;border:1px solid var(--border2);background:var(--surface);color:var(--text);padding:0 8px;font-size:11px;font-family:Consolas,'Cascadia Code',monospace;"/>
            <input id="flow-desc-input" placeholder="Description (for the Tools tab)" maxlength="300" autocomplete="off"
              style="height:24px;width:200px;border-radius:12px;border:1px solid var(--border2);background:var(--surface);color:var(--text);padding:0 8px;font-size:11px;"/>
            <button class="btn" id="flow-save" style="height:24px;font-size:10px;background:var(--accent);color:#fff;">Save</button>
            <button class="ghost-btn" id="flow-promote" title="Promote: give this flow its own tool schema so the model can call it directly by name, without discovery" style="height:24px;font-size:10px;" disabled>${icon("star")} Promote</button>
            <button class="ghost-btn" id="flow-delete" style="height:24px;font-size:10px;color:var(--red);" disabled>${icon("trash")} Delete</button>
            <button class="ghost-btn" id="flow-zoom-out" style="height:24px;width:28px;padding:0;">−</button>
            <button class="ghost-btn" id="flow-zoom-reset" style="height:24px;font-size:10px;">Reset</button>
            <button class="ghost-btn" id="flow-zoom-in" style="height:24px;width:28px;padding:0;">+</button>
            <button class="ghost-btn" id="flow-delete-connection" style="height:24px;font-size:10px;color:var(--red);" disabled>${icon("scissors")} Break Connection</button>
            <button class="ghost-btn" id="flow-clear" style="height:24px;font-size:10px;color:var(--red);">Clear</button>
          </div>
        </div>
        <div id="flow-canvas"><div id="flow-canvas-loading">Loading node-graph engine…</div></div>
      </div>
    </div>
  `;

  // Pull every native + connected-MCP tool and fold each into its own
  // drawer group/node def (Control Flow always comes first). This runs
  // before Drawflow itself loads so the drawer content doesn't have to
  // wait on the CDN fetch.
  let toolDefs = [];
  try {
    toolDefs = await api("list_tool_node_defs");
  } catch (e) {
    toolDefs = [];
  }
  const groupsByName = {};
  FLOW_NODE_GROUPS.forEach(g=>{ groupsByName[g.group] = { group: g.group, nodes: [...g.nodes] }; });
  toolDefs.forEach(d=>{
    const params = d.params || [];
    const kind = d.kind || "action";
    const def = {
      type: d.type, label: d.label, icon: d.icon,
      inputs: 1 + params.length,
      outputs: kind === "action" ? 1 : 2,
      kind, params, desc: d.desc || "",
    };
    FLOW_NODE_DEFS[def.type] = def;
    if (!groupsByName[d.group]) groupsByName[d.group] = { group: d.group, nodes: [] };
    groupsByName[d.group].nodes.push(def);
  });
  const allGroups = Object.values(groupsByName);

  // Build the grouped node drawer up front -- it doesn't depend on Drawflow
  // being loaded, so it's visible immediately even while the library fetches.
  const drawer = document.getElementById("flow-drawer");
  allGroups.forEach(g=>{
    if (!g.nodes.length) return;
    const groupEl = document.createElement("div");
    groupEl.className = "flow-drawer-group";
    const titleEl = document.createElement("div");
    titleEl.className = "flow-drawer-group-title";
    titleEl.textContent = g.group;
    groupEl.appendChild(titleEl);
    g.nodes.forEach(def=>{
      const item = document.createElement("div");
      item.className = "flow-drawer-item";
      item.draggable = true;
      item.title = def.desc || "";
      item.innerHTML = `<span class="flow-drawer-item-icon">${iconHtml(def.icon)}</span><span>${escapeHtml(def.label)}</span>`;
      item.ondragstart = (e)=>{ e.dataTransfer.setData("application/midum-node", def.type); e.dataTransfer.effectAllowed = "copy"; };
      groupEl.appendChild(item);
    });
    drawer.appendChild(groupEl);
  });

  let editor;
  try {
    _loadStyleOnce(DRAWFLOW_CSS);
    await _loadScriptOnce(DRAWFLOW_JS);
    const canvasEl = document.getElementById("flow-canvas");
    if (!canvasEl) return;   // user already switched away from Flows before this resolved
    canvasEl.innerHTML = "";

    editor = new Drawflow(canvasEl);
    editor.reroute = true;
    editor.curvature = 0.4;
    editor.zoom_max = 1.6;
    editor.zoom_min = 0.4;
    editor.start();
    _drawflowEditor = editor;

    // Name field: live-filtered to only characters legal in a Python
    // identifier as the user types (letters, digits, underscore), and a
    // leading digit is stripped since `def 1foo():` isn't valid Python
    // either. Full validation (keyword collisions etc) happens
    // server-side in flows.py when Save is clicked -- this is just to
    // stop obviously-invalid characters from ever being typed.
    const nameInput = document.getElementById("flow-name-input");
    const descInput = document.getElementById("flow-desc-input");
    const loadSelect = document.getElementById("flow-load-select");
    const deleteBtnFlow = document.getElementById("flow-delete");
    const promoteBtnFlow = document.getElementById("flow-promote");
    nameInput.addEventListener("input", ()=>{
      let v = nameInput.value.replace(/[^A-Za-z0-9_]/g, "");
      v = v.replace(/^[0-9]+/, "");
      if (v !== nameInput.value) nameInput.value = v;
    });

    function seedBlankCanvas(){
      editor.clear();
      editor.addNode("start", 0, 1, 100, 160, "flow-node-start", {}, _flowNodeHtml(FLOW_NODE_DEFS.start));
      editor.addNode("end",   1, 0, 480, 160, "flow-node-end",   {}, _flowNodeHtml(FLOW_NODE_DEFS.end));
      _relabelAllPins();
      _wireFlowCodeHighlight(canvasEl);
    }

    // Reflects the currently-loaded saved flow's promoted state on the
    // Promote button -- mirrors the Promote/Demote controls in the MCP
    // tab's Tools pane. Disabled entirely when no saved flow is loaded
    // (an unsaved/new flow has no name to promote yet).
    async function refreshPromoteButton(name){
      if (!name){
        promoteBtnFlow.disabled = true;
        promoteBtnFlow.classList.remove("open");
        promoteBtnFlow.innerHTML = icon("star") + " Promote";
        return;
      }
      promoteBtnFlow.disabled = false;
      let promoted = false;
      try {
        const r = await api("is_flow_promoted", name);
        promoted = !!(r && r.promoted);
      } catch (e) { promoted = false; }
      promoteBtnFlow.dataset.promoted = promoted ? "1" : "";
      promoteBtnFlow.classList.toggle("open", promoted);
      promoteBtnFlow.innerHTML = promoted ? icon("star-fill") + " Promoted" : icon("star") + " Promote";
      promoteBtnFlow.title = promoted
        ? "This flow is promoted -- it has its own tool schema and can be called directly by name. Click to demote."
        : "Promote: give this flow its own tool schema so the model can call it directly by name, without discovery. Click to promote.";
    }

    async function refreshFlowLoadSelect(selectName){
      let names = [];
      try { names = await api("list_flows"); } catch (e) { names = []; }
      loadSelect.innerHTML = `<option value="">New flow…</option>` +
        names.map(n=>`<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
      loadSelect.value = selectName && names.includes(selectName) ? selectName : "";
      deleteBtnFlow.disabled = !loadSelect.value;
      await refreshPromoteButton(loadSelect.value);
    }

    loadSelect.onchange = async ()=>{
      const name = loadSelect.value;
      deleteBtnFlow.disabled = !name;
      refreshPromoteButton(name);
      if (!name){
        nameInput.value = ""; descInput.value = "";
        seedBlankCanvas();
        return;
      }
      nameInput.value = name;
      let graph = null;
      try { graph = await api("get_flow_graph", name); } catch (e) { graph = null; }
      if (graph && graph.drawflow){
        editor.clear();
        editor.import(graph);
        _relabelAllPins();
        _wireFlowCodeHighlight(canvasEl);
      } else {
        await showAlert(`'${name}' was saved before flow editing was added, so its node graph can't be reloaded. Rebuild it from scratch and Save to enable editing next time.`, "Graph Not Available");
        seedBlankCanvas();
      }
    };

    document.getElementById("flow-new").onclick = async ()=>{
      loadSelect.value = ""; deleteBtnFlow.disabled = true;
      refreshPromoteButton("");
      nameInput.value = ""; descInput.value = "";
      seedBlankCanvas();
    };

    document.getElementById("flow-save").onclick = async ()=>{
      const name = nameInput.value.trim();
      if (!name){ await showAlert("Enter a name for this flow first — it becomes the Python function name in flow_tools.py.", "Name Required"); nameInput.focus(); return; }
      const graph = editor.export();
      const btn = document.getElementById("flow-save");
      btn.disabled = true; const oldLabel = btn.textContent; btn.textContent = "Saving…";
      try {
        const r = await api("save_flow", name, graph, descInput.value.trim());
        if (!r.ok) await showAlert(r.message, "Save Failed");
        else await refreshFlowLoadSelect(name);
      } finally {
        btn.disabled = false; btn.textContent = oldLabel;
      }
    };

    promoteBtnFlow.onclick = async ()=>{
      const name = loadSelect.value;
      if (!name) return;
      promoteBtnFlow.disabled = true;
      try {
        const promoted = promoteBtnFlow.dataset.promoted === "1";
        const r = promoted ? await api("demote_flow", name) : await api("promote_flow", name);
        if (!r.ok) await showAlert(r.message, promoted ? "Demote Failed" : "Promote Failed");
      } finally {
        await refreshPromoteButton(name);
      }
    };

    deleteBtnFlow.onclick = async ()=>{
      const name = loadSelect.value;
      if (!name) return;
      const ok = await showConfirm(`Delete the flow '${name}'? This removes it from flow_tools.py and can't be undone.`, "Delete Flow", {danger:true, okLabel:"Delete"});
      if (!ok) return;
      deleteBtnFlow.disabled = true;
      try {
        const r = await api("delete_flow", name);
        if (!r.ok){ await showAlert(r.message, "Delete Failed"); deleteBtnFlow.disabled = false; return; }
        nameInput.value = ""; descInput.value = "";
        seedBlankCanvas();
        await refreshFlowLoadSelect("");
      } catch (e) {
        await showAlert(String(e), "Delete Failed");
        deleteBtnFlow.disabled = false;
      }
    };

    refreshFlowLoadSelect("");

    // Break connections: click a connection line to select it (Drawflow
    // highlights it red via the .selected CSS above), then either press
    // Delete/Backspace or click "Break Connection". Right-click a
    // connection removes it immediately, no selection step needed.
    const deleteBtn = document.getElementById("flow-delete-connection");
    const hintEl = document.getElementById("flow-hint");
    canvasEl.tabIndex = 0;   // required for the container to receive keydown at all
    canvasEl.style.outline = "none";
    canvasEl.addEventListener("mousedown", ()=>canvasEl.focus());

    editor.on("connectionSelected", ()=>{
      deleteBtn.disabled = false;
      if (hintEl) hintEl.textContent = "CONNECTION SELECTED — press Delete or click Break Connection";
    });
    editor.on("connectionUnselected", ()=>{
      deleteBtn.disabled = true;
      if (hintEl) hintEl.textContent = "DRAG NODES ONTO THE CANVAS";
    });
    editor.on("connectionRemoved", ()=>{
      deleteBtn.disabled = true;
      if (hintEl) hintEl.textContent = "DRAG NODES ONTO THE CANVAS";
    });

    deleteBtn.onclick = ()=>{
      if (editor.connection_selected){
        editor.removeConnection();
        deleteBtn.disabled = true;
      }
    };

    // Right-click a connection to break it immediately (Drawflow's own
    // contextmenu handler already selects the connection under the
    // cursor before this fires, so removeConnection() targets the right one).
    canvasEl.addEventListener("contextmenu", (e)=>{
      const onConnection = e.target.closest && e.target.closest(".main-path");
      if (onConnection){
        e.preventDefault();
        editor.connection_selected = onConnection;
        onConnection.classList.add("selected");
        editor.removeConnection();
        deleteBtn.disabled = true;
      }
    });

    function addNodeAt(type, clientX, clientY){
      const def = FLOW_NODE_DEFS[type];
      if (!def) return;
      const rect = canvasEl.getBoundingClientRect();
      const zoom = editor.zoom || 1;
      const x = (clientX - rect.left - editor.canvas_x) / zoom;
      const y = (clientY - rect.top  - editor.canvas_y) / zoom;
      // Seed node data with an empty string per parameter so Drawflow's
      // df-<param> two-way binding has something to attach to from the
      // start (an unset key would just never sync until first edited).
      // `_flow_param_order` records which parameter lives at which extra
      // input pin (input_2, input_3, ...) -- flows.py reads this straight
      // back out of the saved graph, so it never needs to know the tool's
      // schema itself to resolve wired-in vs manually-typed values.
      const initialData = {};
      if (def.isVariable){
        initialData.name = ""; initialData.value = "";
      } else if (def.isArgument){
        initialData.name = ""; initialData.type = "string"; initialData.required = "no";
        initialData.default = ""; initialData.description = "";
      } else if (def.isPython){
        initialData.code = "";
      } else if (def.isPrint){
        initialData.value = "";
      } else if (def.isOperation){
        initialData.op = "add"; initialData.a = ""; initialData.b = "";
      } else if (def.type === "logic::if"){
        initialData.op = "truthy"; initialData.value = ""; initialData.compare = "";
      } else if (def.type === "logic::loop"){
        initialData.item_var = "item";
      } else if (def.type === "ai::prompt"){
        initialData.prompt = ""; initialData.context = "";
      } else if (def.type === "ai::summarize"){
        initialData.text = ""; initialData.length = "medium";
      } else if (def.type === "ai::choose"){
        initialData.question = ""; initialData.options = "";
      } else {
        (def.params || []).forEach(p=>{ initialData[p.name] = ""; });
        initialData._flow_param_order = (def.params || []).map(p=>p.name);
      }
      editor.addNode(def.type, def.inputs, def.outputs, x, y, `flow-node-${def.type.replace(/[^A-Za-z0-9_]/g,'-')}`, initialData, _flowNodeHtml(def));
      _relabelAllPins();
      _wireFlowCodeHighlight(canvasEl);
    }

    canvasEl.ondragover = (e)=>e.preventDefault();
    canvasEl.ondrop = (e)=>{
      e.preventDefault();
      const type = e.dataTransfer.getData("application/midum-node");
      if (type) addNodeAt(type, e.clientX, e.clientY);
    };

    document.getElementById("flow-zoom-in").onclick    = ()=>editor.zoom_in();
    document.getElementById("flow-zoom-out").onclick   = ()=>editor.zoom_out();
    document.getElementById("flow-zoom-reset").onclick = ()=>editor.zoom_reset();
    document.getElementById("flow-clear").onclick = async ()=>{
      const ok = await showConfirm("Clear every node and connection from the canvas?", "Clear Flow", {danger:true, okLabel:"Clear"});
      if (ok) editor.clear();
    };

    // Seed the canvas with one Start and one End node so it isn't empty the
    // very first time this tab is opened.
    seedBlankCanvas();
  } catch (e) {
    const canvasEl = document.getElementById("flow-canvas");
    if (canvasEl){
      canvasEl.innerHTML = `<div style="padding:20px;font-size:12px;color:var(--red);">`
        + `Failed to load the node-graph library (Drawflow) from the CDN — check your internet connection and retry by switching tabs.<br><br>${escapeHtml(String(e))}</div>`;
    }
  }
}

function buildMcpPane(box){
  box.innerHTML = `
    <div class="hdr-row">
      <div class="section-label">MCP SERVERS</div>
      <div>
        <button class="ghost-btn" id="mcp-refresh" style="height:24px;">${icon("refresh")}</button>
        <button class="btn" id="mcp-add" style="background:var(--accent);color:#fff;">+ Add Server</button>
      </div>
    </div>
    <div id="mcp-banner" style="font-size:10px;color:var(--yellow);margin:4px 0;"></div>
    <div id="mcp-list" style="flex:1;overflow-y:auto;margin-top:4px;"></div>`;
  document.getElementById("mcp-refresh").onclick = refreshMcpList;
  document.getElementById("mcp-add").onclick = async ()=>{
    const result = await showMcpAddModal();
    if (!result || !result.name) return;
    await api("connect_mcp", {
      name: result.name,
      transport: result.transport,
      command: result.transport === "stdio" ? (result.command || undefined) : undefined,
      url: result.transport === "http" ? (result.url || undefined) : undefined,
      persist: true,
    });
  };
  refreshMcpList();
}

async function refreshMcpList(){
  const listEl = document.getElementById("mcp-list");
  if (!listEl) return;
  const {servers, sdk_available} = await api("list_mcp");
  document.getElementById("mcp-banner").innerHTML = sdk_available ? "" : icon("alert") + " 'mcp' package not installed — run: pip install mcp";
  listEl.innerHTML = "";
  if (!servers.length){
    listEl.innerHTML = `<div style="text-align:center;font-size:11px;color:var(--subtext);padding:24px 10px;">No MCP servers connected yet.<br>Use "+ Add Server" to connect one.</div>`;
    return;
  }
  servers.forEach(s=>{
    const row = document.createElement("div"); row.className = "mcp-row";
    const dotColor = s.connected ? "var(--green)" : "var(--red)";
    const subtitle = s.connected ? `${s.transport} · ${s.tool_count} tool(s)` : `${s.transport} · connection failed: ${s.error||"unknown error"}`;
    row.innerHTML = `
      <div class="mcp-dot" style="background:${dotColor};"></div>
      <div style="flex:1;min-width:0;">
        <div class="mcp-name">${escapeHtml(s.name)}</div>
        <div class="mcp-sub">${escapeHtml(subtitle)}</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:4px;">
        ${s.connected
          ? `<button class="mini-btn" data-act="tools">Tools</button><button class="mini-btn del" data-act="disc">Disconnect</button>`
          : `<button class="mini-btn" data-act="retry">Retry</button><button class="mini-btn del" data-act="remove">Remove</button>`}
      </div>`;
    const act = row.querySelector('[data-act=tools]');
    if (act) act.onclick = async ()=>{ showMcpToolsPane(s.name); };
    const disc = row.querySelector('[data-act=disc]');
    if (disc) disc.onclick = async ()=>{ const ok = await showConfirm(`Disconnect '${s.name}'?`, "Disconnect Server"); if (ok) await api("disconnect_mcp", s.name, false); };
    const retry = row.querySelector('[data-act=retry]');
    if (retry) retry.onclick = ()=>api("retry_mcp", s.name);
    const remove = row.querySelector('[data-act=remove]');
    if (remove) remove.onclick = async ()=>{ const ok = await showConfirm(`Remove '${s.name}' permanently?`, "Remove Server", {danger:true, okLabel:"Remove"}); if (ok) await api("disconnect_mcp", s.name, true); };
    listEl.appendChild(row);
  });
}

// ── Agents pane -------------------------------------------------------------
// Manually spawn/monitor/task the Supervisor's sub-agents (see multi_agent.py).
// Running agents get a live-ish card (polled) with Task/Stop controls; dormant
// persisted agents (persistence=true, not currently running) get Resume/Forget.
// Clicking a running agent opens a chat-style transcript -- same bubble
// styling as the main chat pane -- showing every task it's been given and
// its final report for each, with a box to send it another task inline.
let _agentsPollTimer = null;
let _agentTranscriptPollTimer = null;

function buildAgentsPane(box){
  box.innerHTML = `
    <div class="hdr-row">
      <div class="section-label">AGENTS — SUPERVISOR</div>
      <div>
        <button class="ghost-btn" id="agents-refresh" style="height:24px;">${icon("refresh")}</button>
        <button class="btn" id="agents-add" style="background:var(--accent);color:#fff;">+ New Agent</button>
      </div>
    </div>
    <div style="font-size:10px;color:var(--subtext);margin:4px 0 8px;">
      Sub-agents run their own Action Loop under the current text model (the Supervisor — never the voice model).
      Persisted agents survive a restart — reactivate them below with Resume.
    </div>
    <div id="agents-list" style="flex:1;overflow-y:auto;"></div>`;
  document.getElementById("agents-refresh").onclick = refreshAgentsList;
  document.getElementById("agents-add").onclick = async ()=>{
    const result = await showAgentAddModal();
    if (!result || !result.name || !result.role) return;
    const r = await api("create_agent_ui", result.role, result.name, result.description,
      result.personality, result.persistence, result.model, result.provider, result.knowledge_bases, result.skills);
    if (!r.ok) await showAlert(r.message, "Couldn't start agent");
    refreshAgentsList();
  };
  refreshAgentsList();
  if (_agentsPollTimer) clearInterval(_agentsPollTimer);
  _agentsPollTimer = setInterval(()=>{ if (state.activeTab === "Agents") refreshAgentsList(); }, 4000);
}

async function refreshAgentsList(){
  const listEl = document.getElementById("agents-list");
  if (!listEl) return;
  const {running, dormant} = await api("list_agents_ui");
  listEl.innerHTML = "";
  if (!running.length && !dormant.length){
    listEl.innerHTML = `<div style="text-align:center;font-size:11px;color:var(--subtext);padding:24px 10px;">No agents yet.<br>Use "+ New Agent" to spawn the Supervisor's first sub-agent.</div>`;
    return;
  }
  running.forEach(a=>{
    const row = document.createElement("div"); row.className = "mcp-row";
    const dotColor = a.status === "running" ? "var(--green)" : (a.status === "idle" ? "var(--yellow)" : "var(--red)");
    const sub = `${a.role} · ${a.provider}/${a.model} · ${a.status}` + (a.queued ? ` · ${a.queued} queued` : "") + (a.persistence ? " · persisted" : "") + (a.knowledge_bases && a.knowledge_bases.length ? ` · KB: ${a.knowledge_bases.join(", ")}` : "") + (a.skills && a.skills.length ? ` · Skills: ${a.skills.join(", ")}` : "");
    row.innerHTML = `
      <div class="mcp-dot" style="background:${dotColor};"></div>
      <div style="flex:1;min-width:0;cursor:pointer;" data-act="open">
        <div class="mcp-name">${escapeHtml(a.name)}</div>
        <div class="mcp-sub">${escapeHtml(sub)}</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:4px;">
        <button class="mini-btn open" data-act="task">Task</button>
        <button class="mini-btn" data-act="knowledge">Knowledge</button>
        <button class="mini-btn" data-act="skills">Skills</button>
        <button class="mini-btn del" data-act="stop">Stop</button>
      </div>`;
    row.querySelector('[data-act="open"]').onclick = ()=>showAgentTranscript(a.name);
    row.querySelector('[data-act="task"]').onclick = async ()=>{
      const task = await showPrompt(`Task for '${a.name}':`, "Send Agent Task");
      if (task){ await api("send_agent_task_ui", a.name, task); showAgentTranscript(a.name); }
    };
    row.querySelector('[data-act="knowledge"]').onclick = async ()=>{
      const picked = await showAgentKnowledgeModal(a.name, a.knowledge_bases || []);
      if (picked !== null){ await api("attach_agent_knowledge_ui", a.name, picked); refreshAgentsList(); }
    };
    row.querySelector('[data-act="skills"]').onclick = async ()=>{
      const picked = await showAgentSkillsModal(a.name, a.skills || []);
      if (picked !== null){ await api("attach_agent_skills_ui", a.name, picked); refreshAgentsList(); }
    };
    row.querySelector('[data-act="stop"]').onclick = async ()=>{
      const ok = await showConfirm(`Stop agent '${a.name}'?`, "Stop Agent");
      if (ok){ await api("stop_agent_ui", a.name); refreshAgentsList(); }
    };
    listEl.appendChild(row);
  });
  if (dormant.length){
    const hdr = document.createElement("div");
    hdr.style.cssText = "font-size:10px;color:var(--subtext);margin:10px 0 4px;text-transform:uppercase;letter-spacing:.04em;";
    hdr.textContent = "Persisted — not running";
    listEl.appendChild(hdr);
    dormant.forEach(a=>{
      const row = document.createElement("div"); row.className = "mcp-row";
      row.innerHTML = `
        <div class="mcp-dot" style="background:var(--muted);"></div>
        <div style="flex:1;min-width:0;">
          <div class="mcp-name">${escapeHtml(a.name)}</div>
          <div class="mcp-sub">${escapeHtml(a.role)} · ${escapeHtml(a.provider)}/${escapeHtml(a.model)}</div>
        </div>
        <div style="display:flex;flex-direction:column;gap:4px;">
          <button class="mini-btn" data-act="resume">Resume</button>
          <button class="mini-btn del" data-act="forget">Forget</button>
        </div>`;
      row.querySelector('[data-act="resume"]').onclick = async ()=>{ await api("resume_agent_ui", a.name); refreshAgentsList(); };
      row.querySelector('[data-act="forget"]').onclick = async ()=>{
        const ok = await showConfirm(`Permanently forget agent '${a.name}'?`, "Forget Agent", {danger:true, okLabel:"Forget"});
        if (ok){ await api("forget_agent_ui", a.name); refreshAgentsList(); }
      };
      listEl.appendChild(row);
    });
  }
}

// Multi-field modal for spawning a new agent -- role/name/description/
// personality/model/provider/persistence/knowledge_bases, mirroring
// start_agent()'s params.
async function showAgentAddModal(){
  const kbFiles = await api("list_knowledge_files"); // e.g. ['blender_notes.md', ...]
  const skillFiles = await api("list_skill_files");  // e.g. ['terrain_debugging.md', ...]
  return new Promise(resolve=>{
    const roleId    = "ag-role-"    + Math.random().toString(36).slice(2);
    const nameId    = "ag-name-"    + Math.random().toString(36).slice(2);
    const descId    = "ag-desc-"    + Math.random().toString(36).slice(2);
    const persId    = "ag-pers-"    + Math.random().toString(36).slice(2);
    const modelId   = "ag-model-"   + Math.random().toString(36).slice(2);
    const provId    = "ag-prov-"    + Math.random().toString(36).slice(2);
    const persistId = "ag-persist-" + Math.random().toString(36).slice(2);
    const kbListId  = "ag-kb-"      + Math.random().toString(36).slice(2);
    const skListId  = "ag-sk-"      + Math.random().toString(36).slice(2);
    const kbHtml = kbFiles.length
      ? kbFiles.map(f=>{
          const cbId = "ag-kbcb-" + Math.random().toString(36).slice(2);
          return `<label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text);padding:2px 0;">
            <input type="checkbox" class="ag-kb-cb" data-file="${escapeHtml(f)}" id="${cbId}"/> ${escapeHtml(f)}
          </label>`;
        }).join("")
      : `<div style="font-size:10px;color:var(--subtext);">No Knowledge Bases yet — save one from the Knowledge tab first.</div>`;
    const skHtml = skillFiles.length
      ? skillFiles.map(f=>{
          const cbId = "ag-skcb-" + Math.random().toString(36).slice(2);
          return `<label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text);padding:2px 0;">
            <input type="checkbox" class="ag-sk-cb" data-file="${escapeHtml(f)}" id="${cbId}"/> ${escapeHtml(f)}
          </label>`;
        }).join("")
      : `<div style="font-size:10px;color:var(--subtext);">No Skills yet — save one from the Skills tab first.</div>`;
    const body = `
      <div class="modal-label">ROLE</div>
      <input type="text" class="modal-input" id="${roleId}" placeholder="e.g. Researcher (one word to one line)"/>
      <div class="modal-label">NAME</div>
      <input type="text" class="modal-input" id="${nameId}" placeholder="e.g. Scout"/>
      <div class="modal-label">DESCRIPTION</div>
      <textarea class="modal-input" id="${descId}" rows="2" placeholder="What this agent is for, in detail..."></textarea>
      <div class="modal-label">PERSONALITY / HOW IT SHOULD ACT</div>
      <textarea class="modal-input" id="${persId}" rows="2" placeholder="Tone, style, behaviour while it works..."></textarea>
      <div class="modal-label">MODEL</div>
      <input type="text" class="modal-input" id="${modelId}" placeholder="gemini-3.5-flash-lite" value="gemini-3.5-flash-lite"/>
      <div class="modal-label">PROVIDER</div>
      <input type="text" class="modal-input" id="${provId}" placeholder="gemini_api" value="gemini_api"/>
      <div class="modal-label">KNOWLEDGE (optional — full content given fresh every task, never compounds)</div>
      <div id="${kbListId}" style="max-height:110px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;padding:6px 8px;margin-bottom:6px;">${kbHtml}</div>
      <div class="modal-label">SKILLS (optional — same fresh-every-task treatment as Knowledge)</div>
      <div id="${skListId}" style="max-height:110px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;padding:6px 8px;margin-bottom:6px;">${skHtml}</div>
      <label style="display:flex;align-items:center;gap:6px;margin-top:8px;font-size:11px;color:var(--text);">
        <input type="checkbox" id="${persistId}"/> Persist — save so it can be reactivated later, even after a restart
      </label>
    `;
    _renderModal("New Agent", body, [
      { label: "Cancel", onClick: ()=>{ _closeModal(); resolve(null); } },
      { label: "Start Agent", primary: true, onClick: ()=>{
          const role        = document.getElementById(roleId).value.trim();
          const name        = document.getElementById(nameId).value.trim();
          const description = document.getElementById(descId).value.trim();
          const personality = document.getElementById(persId).value.trim();
          const model       = document.getElementById(modelId).value.trim();
          const provider    = document.getElementById(provId).value.trim();
          const persistence = document.getElementById(persistId).checked;
          const knowledge_bases = Array.from(document.querySelectorAll(`#${kbListId} .ag-kb-cb:checked`)).map(cb=>cb.dataset.file);
          const skills = Array.from(document.querySelectorAll(`#${skListId} .ag-sk-cb:checked`)).map(cb=>cb.dataset.file);
          _closeModal();
          resolve({ role, name, description, personality, model, provider, persistence, knowledge_bases, skills });
        } },
    ], roleId);
    _modalKeyHandler = e=>{ if (e.key === "Escape"){ _closeModal(); resolve(null); } };
    document.addEventListener("keydown", _modalKeyHandler);
  });
}

// Modal for changing which Knowledge Bases are attached to an already-
// running (or dormant) agent -- calls attach_agent_knowledge_ui, which
// REPLACES the agent's whole knowledge_bases list with the checked set.
async function showAgentKnowledgeModal(name, currentFiles){
  const kbFiles = await api("list_knowledge_files");
  const current = new Set((currentFiles || []).map(n => n.endsWith(".md") ? n : `${n}.md`));
  return new Promise(resolve=>{
    const kbListId = "ag-kb2-" + Math.random().toString(36).slice(2);
    const kbHtml = kbFiles.length
      ? kbFiles.map(f=>{
          const cbId = "ag-kbcb2-" + Math.random().toString(36).slice(2);
          return `<label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text);padding:2px 0;">
            <input type="checkbox" class="ag-kb-cb2" data-file="${escapeHtml(f)}" id="${cbId}" ${current.has(f) ? "checked" : ""}/> ${escapeHtml(f)}
          </label>`;
        }).join("")
      : `<div style="font-size:10px;color:var(--subtext);">No Knowledge Bases yet — save one from the Knowledge tab first.</div>`;
    const body = `
      <div style="font-size:10px;color:var(--subtext);margin-bottom:6px;">
        Full content of the checked Knowledge Base(s) is given to '${escapeHtml(name)}' fresh every task —
        it never accumulates in its own history.
      </div>
      <div id="${kbListId}" style="max-height:160px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;padding:6px 8px;">${kbHtml}</div>
    `;
    _renderModal(`Knowledge — ${name}`, body, [
      { label: "Cancel", onClick: ()=>{ _closeModal(); resolve(null); } },
      { label: "Save", primary: true, onClick: ()=>{
          const picked = Array.from(document.querySelectorAll(`#${kbListId} .ag-kb-cb2:checked`)).map(cb=>cb.dataset.file);
          _closeModal();
          resolve(picked);
        } },
    ]);
    _modalKeyHandler = e=>{ if (e.key === "Escape"){ _closeModal(); resolve(null); } };
    document.addEventListener("keydown", _modalKeyHandler);
  });
}

// Same as showAgentKnowledgeModal() above, but for this agent's attached
// MD Skill files -- calls attach_agent_skills_ui, which REPLACES the
// agent's whole skills list with the checked set.
async function showAgentSkillsModal(name, currentFiles){
  const skillFiles = await api("list_skill_files");
  const current = new Set((currentFiles || []).map(n => n.endsWith(".md") ? n : `${n}.md`));
  return new Promise(resolve=>{
    const skListId = "ag-sk2-" + Math.random().toString(36).slice(2);
    const skHtml = skillFiles.length
      ? skillFiles.map(f=>{
          const cbId = "ag-skcb2-" + Math.random().toString(36).slice(2);
          return `<label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--text);padding:2px 0;">
            <input type="checkbox" class="ag-sk-cb2" data-file="${escapeHtml(f)}" id="${cbId}" ${current.has(f) ? "checked" : ""}/> ${escapeHtml(f)}
          </label>`;
        }).join("")
      : `<div style="font-size:10px;color:var(--subtext);">No Skills yet — save one from the Skills tab first.</div>`;
    const body = `
      <div style="font-size:10px;color:var(--subtext);margin-bottom:6px;">
        Full content of the checked Skill(s) is given to '${escapeHtml(name)}' fresh every task —
        it never accumulates in its own history.
      </div>
      <div id="${skListId}" style="max-height:160px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;padding:6px 8px;">${skHtml}</div>
    `;
    _renderModal(`Skills — ${name}`, body, [
      { label: "Cancel", onClick: ()=>{ _closeModal(); resolve(null); } },
      { label: "Save", primary: true, onClick: ()=>{
          const picked = Array.from(document.querySelectorAll(`#${skListId} .ag-sk-cb2:checked`)).map(cb=>cb.dataset.file);
          _closeModal();
          resolve(picked);
        } },
    ]);
    _modalKeyHandler = e=>{ if (e.key === "Escape"){ _closeModal(); resolve(null); } };
    document.addEventListener("keydown", _modalKeyHandler);
  });
}

// Chat-style monitor for one running agent -- reuses the same .bubble
// user/midum classes the main chat pane uses so it looks and feels
// identical, just scoped to this one agent's own task/report history.
// Polls every 3s while open so progress shows up without manual refresh,
// and lets you send the agent another task right from the same view.
async function showAgentTranscript(name){
  const overlay = document.getElementById("modal-overlay");
  const box = document.getElementById("modal-box");
  box.classList.add("wide");
  box.innerHTML = `
    <div class="modal-title">Agent — ${escapeHtml(name)}</div>
    <div id="agent-transcript-body" style="max-height:50vh;overflow-y:auto;margin:8px 0;display:flex;flex-direction:column;gap:10px;padding:4px;"></div>
    <div style="display:flex;gap:8px;">
      <input type="text" class="modal-input" id="agent-transcript-input" placeholder="Send this agent a task..." style="flex:1;margin:0;"/>
      <button class="modal-btn primary" id="agent-transcript-send">Send</button>
    </div>
    <div class="modal-actions"><button class="modal-btn" id="agent-transcript-close">Close</button></div>
  `;
  overlay.classList.add("open");

  async function refresh(){
    const {transcript} = await api("get_agent_transcript_ui", name);
    const bodyEl = document.getElementById("agent-transcript-body");
    if (!bodyEl) return;
    bodyEl.innerHTML = (transcript && transcript.length)
      ? transcript.map(m=>{
          const isUser = m.role === "user";
          return `<div class="row ${isUser ? "user" : "midum"}" style="margin:0;">
            <div class="row-label-row"><div class="row-label">${isUser ? "Supervisor" : escapeHtml(name)}</div></div>
            <div class="bubble ${isUser ? "user" : "midum"}">${escapeHtml(m.content || "")}</div>
          </div>`;
        }).join("")
      : `<div style="font-size:11px;color:var(--subtext);text-align:center;padding:20px;">No tasks sent to this agent yet.</div>`;
    bodyEl.scrollTop = bodyEl.scrollHeight;
  }
  await refresh();

  if (_agentTranscriptPollTimer) clearInterval(_agentTranscriptPollTimer);
  _agentTranscriptPollTimer = setInterval(refresh, 3000);

  const stopPolling = ()=>{ if (_agentTranscriptPollTimer){ clearInterval(_agentTranscriptPollTimer); _agentTranscriptPollTimer = null; } };

  document.getElementById("agent-transcript-send").onclick = async ()=>{
    const input = document.getElementById("agent-transcript-input");
    const task = (input.value || "").trim();
    if (!task) return;
    input.value = "";
    await api("send_agent_task_ui", name, task);
    refresh();
  };
  document.getElementById("agent-transcript-input").onkeydown = (e)=>{
    if (e.key === "Enter") document.getElementById("agent-transcript-send").click();
  };
  document.getElementById("agent-transcript-close").onclick = ()=>{ stopPolling(); _closeModal(); };
  _modalKeyHandler = e=>{ if (e.key === "Escape"){ stopPolling(); _closeModal(); } };
  document.addEventListener("keydown", _modalKeyHandler);
}

// ── Permissions pane -------------------------------------------------------
// Per-tool (native + MCP) permission control: Always Allow / Ask for
// Approval / Don't Allow. Enforced server-side in orchestration.py right
// before each tool call is dispatched -- this pane just edits the stored
// overrides via get_permissions/set_permission/reset_permissions.
async function buildPermissionsPane(box){
  box.innerHTML = `
    <div class="hdr-row">
      <div class="section-label">TOOL PERMISSIONS</div>
      <button class="ghost-btn" id="perm-reset" style="height:24px;font-size:10px;">Reset All to Always</button>
    </div>
    <div class="perm-fs-pw-box">
      <div class="perm-fs-pw-hdr">
        <div class="perm-fs-pw-title">🔑 Filesystem MCP Approval Password</div>
        <div class="perm-fs-pw-status unset" id="perm-fs-pw-status">checking...</div>
      </div>
      <div class="perm-fs-pw-hint">Backs the "Password" level below. A tool set to "Password" pops the
        same Approve/Decline dialog as "Ask", but Approve also requires typing this password.</div>
      <div class="perm-fs-pw-row">
        <input type="password" id="perm-fs-pw-input" placeholder="New approval password" autocomplete="new-password"/>
        <button class="ghost-btn" id="perm-fs-pw-set" style="height:30px;font-size:11px;">Set</button>
        <button class="ghost-btn" id="perm-fs-pw-remove" style="height:30px;font-size:11px;">Remove</button>
      </div>
    </div>
    <input type="text" id="perm-search" placeholder="Search tools..." class="perm-search" style="margin-top:10px;"/>
    <div id="perm-list" style="flex:1;overflow-y:auto;margin-top:8px;"></div>
  `;
  const listEl = document.getElementById("perm-list");
  let targets = await api("list_permission_targets");
  let overrides = await api("get_permissions");

  // ── Filesystem MCP approval password box ──────────────────────────────
  const pwStatusEl = document.getElementById("perm-fs-pw-status");
  const pwInputEl  = document.getElementById("perm-fs-pw-input");
  async function refreshPwStatus(){
    const st = await api("get_filesystem_password_status");
    pwStatusEl.textContent = st.configured ? "Configured" : "Not set";
    pwStatusEl.classList.toggle("set", !!st.configured);
    pwStatusEl.classList.toggle("unset", !st.configured);
  }
  refreshPwStatus();
  document.getElementById("perm-fs-pw-set").onclick = async ()=>{
    const pw = pwInputEl.value;
    if (!pw){ pwInputEl.focus(); return; }
    const res = await api("set_filesystem_password", pw);
    pwInputEl.value = "";
    await refreshPwStatus();
    if (res && res.message) showAlert(res.message, "Approval Password");
  };
  document.getElementById("perm-fs-pw-remove").onclick = async ()=>{
    const ok = await showConfirm("Remove the Filesystem MCP approval password? Any tool set to \"Password\" will then be blocked outright until a new one is set.", "Remove Password", {danger:true, okLabel:"Remove"});
    if (!ok) return;
    const res = await api("remove_filesystem_password");
    await refreshPwStatus();
    if (res && res.message) showAlert(res.message, "Approval Password");
  };

  function levelFor(key){ return overrides[key] || "always"; }

  function rowHtml(entry){
    const lvl = levelFor(entry.key);
    const haystack = (entry.name + " " + (entry.desc||"")).toLowerCase();
    return `<div class="perm-row" data-name="${escapeHtml(haystack)}">
      <div class="perm-info">
        <div class="perm-name">${escapeHtml(entry.name)}</div>
        ${entry.desc ? `<div class="perm-desc">${escapeHtml(entry.desc)}</div>` : ""}
      </div>
      <div class="perm-seg" data-key="${escapeHtml(entry.key)}">
        <button class="perm-opt${lvl==='always'?' active':''}" data-level="always">Always</button>
        <button class="perm-opt${lvl==='ask'?' active':''}" data-level="ask">Ask</button>
        <button class="perm-opt${lvl==='ask_password'?' active':''}" data-level="ask_password" title="Ask for approval, password required">Password</button>
        <button class="perm-opt${lvl==='deny'?' active':''}" data-level="deny">Deny</button>
      </div>
    </div>`;
  }

  function renderAll(){
    let html = `<div class="perm-group"><div class="perm-group-title">Native Tools (${targets.native.length})</div>`;
    html += targets.native.map(rowHtml).join("") + `</div>`;
    targets.mcp_groups.forEach(g=>{
      const status = g.connected ? "connected" : "disconnected";
      html += `<div class="perm-group"><div class="perm-group-title">MCP: ${escapeHtml(g.server)} (${status}, ${g.tools.length} tool(s))</div>`;
      html += g.tools.length
        ? g.tools.map(rowHtml).join("")
        : `<div class="perm-empty">No tools reported for this server.</div>`;
      html += `</div>`;
    });
    listEl.innerHTML = html || `<div class="perm-empty">No tools found.</div>`;
  }
  renderAll();

  listEl.addEventListener("click", async (e)=>{
    const btn = e.target.closest(".perm-opt");
    if (!btn) return;
    const seg = btn.closest(".perm-seg");
    const key = seg.dataset.key;
    const level = btn.dataset.level;
    seg.querySelectorAll(".perm-opt").forEach(b=>b.classList.toggle("active", b===btn));
    if (level === "always") delete overrides[key]; else overrides[key] = level;
    await api("set_permission", key, level);
  });

  document.getElementById("perm-search").oninput = (e)=>{
    const q = e.target.value.trim().toLowerCase();
    listEl.querySelectorAll(".perm-group").forEach(group=>{
      let anyVisible = false;
      group.querySelectorAll(".perm-row").forEach(row=>{
        const show = !q || row.dataset.name.includes(q);
        row.style.display = show ? "" : "none";
        if (show) anyVisible = true;
      });
      const emptyMsg = group.querySelector(".perm-empty");
      group.style.display = (anyVisible || (emptyMsg && !q)) ? "" : "none";
    });
  };

  document.getElementById("perm-reset").onclick = async ()=>{
    const ok = await showConfirm("Reset ALL tool permissions to 'Always Allow'?", "Reset Permissions", {danger:true, okLabel:"Reset All"});
    if (!ok) return;
    await api("reset_permissions");
    overrides = {};
    renderAll();
  };
}

// ── Ambient blob layer ----------------------------------------------------
// blob-center is pure CSS (pulse, no JS). blob-a/blob-b wander to a new
// random point every few seconds via a CSS transform transition (smooth
// easing, true random targets picked in JS). blob-cursor tracks the mouse
// with exponential smoothing (lerp) each animation frame so it glides
// rather than snapping straight to the pointer.
//
// Three separate things used to cause visible flashing/flicker, all fixed
// here rather than in CSS alone:
//   1. Blob sizes were in vmax, so every pixel of a live window resize/
//      maximize forced the engine to recompute geometry and re-rasterize
//      the blur for four huge layers *continuously* while dragging. Fixed
//      by only computing size once (as --blob-vmax, a plain px value) on
//      load and on a *debounced* resize -- never mid-drag.
//   2. Even with that fix, the moment of resize/maximize itself still
//      forces one unavoidable re-layout of the whole page (panes, topbar,
//      etc.), and re-compositing the blob layer at the same instant is
//      what read as a flash. Fixed by fading #blob-layer's opacity to 0
//      for the duration of the resize (html.blob-settling) and back in
//      once things have settled, so that repaint never happens on-screen.
//   3. The rAF loop driving blob-cursor ran unconditionally, including
//      while the window/webview was unfocused (e.g. cursor left the
//      window) -- pywebview/CEF can suspend and abruptly resume rAF in a
//      way that produces a visible jump/flash on refocus. Fixed by
//      pausing the loop on window "blur" and resuming cleanly on "focus".
function initBlobLayer(){
  const cursorBlob = document.getElementById("blob-cursor");
  const blobA = document.getElementById("blob-a");
  const blobB = document.getElementById("blob-b");

  function setBlobScale(){
    const vmax = Math.max(window.innerWidth, window.innerHeight);
    document.documentElement.style.setProperty("--blob-vmax", vmax + "px");
  }
  setBlobScale();

  let resizeSettleTimer = null;
  let lastW = window.innerWidth, lastH = window.innerHeight;
  window.addEventListener("resize", ()=>{
    // Windows fires spurious "resize" events with no actual dimension change
    // when the cursor hovers the native minimize/maximize/close caption
    // buttons (DWM hit-testing / snap-layout preview on the title bar).
    // Reacting to those no-op resizes by toggling blob-settling was what
    // caused the blob layer to flash every time the pointer passed over
    // those buttons. Only treat it as a real resize -- and only then fade
    // the blob layer -- if the viewport size actually changed.
    const w = window.innerWidth, h = window.innerHeight;
    if (w === lastW && h === lastH) return;
    lastW = w; lastH = h;

    document.documentElement.classList.add("blob-settling");
    clearTimeout(resizeSettleTimer);
    resizeSettleTimer = setTimeout(()=>{
      setBlobScale();
      // next frame, so the size change above lands while still invisible
      requestAnimationFrame(()=>{
        document.documentElement.classList.remove("blob-settling");
      });
    }, 180);
  }, { passive: true });

  // cursorStart/cursorStop are also exposed on the returned handle (as
  // resume/pause) so the settings toggle can fully stop this rAF loop --
  // not just hide the layer -- when the user turns blobs off.
  let cursorStart = ()=>{}, cursorStop = ()=>{};
  if (cursorBlob){
    let cx = window.innerWidth / 2, cy = window.innerHeight / 2;
    let tx = cx, ty = cy;
    let rafId = null;
    window.addEventListener("mousemove", (e)=>{ tx = e.clientX; ty = e.clientY; }, { passive: true });

    function tick(){
      cx += (tx - cx) * 0.07;
      cy += (ty - cy) * 0.07;
      cursorBlob.style.transform = `translate(${cx}px, ${cy}px) translate(-50%, -50%)`;
      rafId = requestAnimationFrame(tick);
    }
    function start(){ if (rafId === null) rafId = requestAnimationFrame(tick); }
    function stop(){ if (rafId !== null){ cancelAnimationFrame(rafId); rafId = null; } }
    // Re-sync the lerp target to the current position before every start,
    // so resuming (whether from the settings toggle or a refocus) never
    // glides in from a stale mouse position and looks like a jump.
    function resyncStart(){ tx = cx; ty = cy; start(); }
    cursorStart = resyncStart; cursorStop = stop;

    start();
  }

  // `active` gates whether wander() actually moves the blobs each leg --
  // the setTimeout chain itself keeps running either way (cheap), so
  // resuming just flips a flag rather than having to re-kick anything.
  let active = true;
  let blobsPaused = false;
  function wander(el){
    if (!el) return;
    (function step(){
      if (active){
        const x = window.innerWidth * (0.08 + Math.random() * 0.84);
        const y = window.innerHeight * (0.08 + Math.random() * 0.84);
        const dur = 5 + Math.random() * 5; // 5-10s per leg, so it never looks metronomic
        el.style.transitionDuration = dur + "s";
        el.style.transform = `translate(${x}px, ${y}px) translate(-50%, -50%)`;
        setTimeout(step, dur * 1000);
      } else {
        setTimeout(step, 1000); // paused -- just poll for resume, don't move
      }
    })();
  }
  wander(blobA);
  wander(blobB);

  // Freeze + fully hide the blob layer whenever this window isn't the
  // focused, visible foreground app -- not just during an actual resize.
  // While some OTHER app is loading, opening, or animating on screen, the
  // OS/GPU compositor does all sorts of churn around this window too (DWM
  // thumbnail capture for Alt-Tab/taskbar previews, reclaiming/restoring
  // the GPU surface on focus loss, etc.), and with isolation:isolate +
  // contain:strict promoting #blob-layer to its own composited layer,
  // that churn was showing up here as a visible flash even though nothing
  // in this window itself had changed. Rather than chase every possible
  // external trigger individually, just stop doing any work and hide the
  // layer the moment focus or visibility is lost, and fade it back in
  // only once this window is genuinely focused and visible again.
  let hidden = false;
  function setHidden(v){
    if (v === hidden) return;
    hidden = v;
    document.documentElement.classList.toggle("blob-hidden", v);
    if (v){
      cursorStop();
      active = false;
    } else if (!blobsPaused){
      active = true;
      cursorStart();
    }
  }
  window.addEventListener("blur", ()=> setHidden(true));
  window.addEventListener("focus", ()=> setHidden(false));
  document.addEventListener("visibilitychange", ()=>{
    setHidden(document.hidden || !document.hasFocus());
  });
  // In case this boots already unfocused/occluded (e.g. opened in the
  // background), start in the hidden state instead of flashing on first paint.
  setHidden(document.hidden || !document.hasFocus());

  return {
    pause(){ active = false; blobsPaused = true; cursorStop(); },
    resume(){ active = true; blobsPaused = false; if (!hidden) cursorStart(); },
  };
}

// ── Custom titlebar --------------------------------------------------------
// Real Windows Snap (edge-drag, Win+arrow, Snap Layouts flyout on hover)
// comes from the native WM_NCHITTEST hook installed on the Python side
// (_install_win_snap_hook) -- these handlers just cover: reflecting the
// current chat name / active model, the click fallback on platforms
// without that hook, and keeping the maximize icon in sync.
function initTitlebar(){
  _applyFeatherIcons();
  refreshTitlebarInfo();
  _reportDpiScale();
  // devicePixelRatio changes if the window is dragged to a monitor with
  // different scaling -- _reportDpiScale() re-arms its own matchMedia
  // listener each time it runs, so this just needs the initial call plus
  // a plain resize listener as a cheap, harmless extra signal.
  window.addEventListener("resize", _reportDpiScale);
  document.getElementById("titlebar-min").onclick = ()=>api("titlebar_minimize");
  document.getElementById("titlebar-max").onclick = async ()=>{
    const r = await api("titlebar_toggle_maximize");
    if (r) setTitlebarMaxIcon(!!r.maximized);
  };
  document.getElementById("titlebar-close").onclick = ()=>api("titlebar_close");
  // Fallback double-click-to-maximize for platforms where the native
  // caption hit-test hook isn't installed (non-Windows) -- on Windows the
  // real caption area swallows this at the OS level before it ever
  // reaches the DOM, so this handler simply never fires there.
  document.getElementById("app-titlebar").addEventListener("dblclick", (e)=>{
    if (e.target.closest(".titlebar-btn")) return;
    if (e.target.closest("#titlebar-center") && e.target !== document.getElementById("titlebar-center")) return;
    document.getElementById("titlebar-max").click();
  });

  // Conversation tabs -- new-tab button, tab-group dropdown, initial strip.
  document.getElementById("conv-tab-new-btn").onclick = newConvTab;
  document.getElementById("conv-tab-group-btn").onclick = (e)=>{ e.stopPropagation(); toggleConvTabGroupMenu(); };
  window.addEventListener("resize", reportTitlebarNodragZones);
  window.addEventListener("resize", positionConvTabHighlight);
  buildConversationTabs();
}
function _applyFeatherIcons(){
  if (window.feather){ feather.replace({ "stroke-width": 2, width: 14, height: 14 }); }
}
function _reportDpiScale(){
  api("set_dpi_scale", window.devicePixelRatio || 1);
  // Re-arm the one-shot matchMedia listener at the new ratio so a further
  // scale change (e.g. dragged to yet another monitor) is still caught.
  try{
    matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`).addEventListener("change", _reportDpiScale, { once: true });
  }catch(e){}
}
function setTitlebarMaxIcon(isMaximized){
  const btn = document.getElementById("titlebar-max");
  btn.title = isMaximized ? "Restore Down" : "Maximize";
  btn.innerHTML = isMaximized ? '<i data-feather="copy"></i>' : '<i data-feather="square"></i>';
  _applyFeatherIcons();
}
async function refreshTitlebarInfo(){
  try{
    const info = await api("get_titlebar_info");
    if (!info) return;
    document.getElementById("titlebar-chat-title").textContent = info.title || "Untitled chat";
    document.getElementById("titlebar-model-pill").textContent = info.model || "(auto)";
  }catch(e){ /* best-effort -- never block the UI on this */ }
}

// ── Boot -----------------------------------------------------------------
_blobLayerCtl = initBlobLayer(); // pure DOM/CSS, doesn't need the pywebview bridge -- start immediately
window.addEventListener("pywebviewready", async ()=>{
  initTitlebar();
  buildTabbar();
  buildSidebar();
  applyLayout();

  document.getElementById("sidebar-toggle").onclick = toggleSidebar;
  document.getElementById("render-surface-close").onclick = ()=>{
    closeRenderSurfaceUI();
    api("render_surface_manual_close"); // tell Python so a later edit_render_code/close_render_surface call doesn't operate on state the user already dismissed
  };
  document.getElementById("render-surface-save").onclick = async ()=>{
    const name = await showPrompt("Save this Render Surface as:", "Save Render Surface");
    if (name === null || !name.trim()) return;
    const r = await api("render_surface_save_ui", name.trim());
    if (!r.ok) await showConfirm(r.message || "Failed to save.", "Error");
  };
  document.getElementById("send-btn").onclick = sendMessage;
  document.getElementById("msg-input").addEventListener("keydown", e=>{ if (pathPicker.open || capabilityPicker.open) return; if (e.key==="Enter" && !e.shiftKey){ e.preventDefault(); sendMessage(); } });
  document.getElementById("msg-input").addEventListener("input", ()=>{ autosizeMsgInput(); checkPathPickerTrigger(); checkCapabilityPickerTrigger(); });
  document.getElementById("abort-btn").onclick = ()=>api("abort");
  document.getElementById("copy-chat-btn").onclick = copyFullConversation;
  document.getElementById("reopen-midumdocs-btn").onclick = async ()=>{
    const r = await api("midumdocs_reopen_ui");
    if (r && r.ok === false) await showConfirm(r.error || "Could not open MidumDocs.", "MidumDocs");
  };
  document.getElementById("midumdocs-close").onclick = ()=>{ closeMidumDocUI(); api("midumdocs_manual_close"); };
  document.getElementById("midumdocs-recompile").onclick = (e)=>_mdPost({md:"cmd", cmd:"recompile", clean:!!e.shiftKey});
  document.getElementById("midumdocs-pdf").onclick = ()=>_mdPost({md:"cmd", cmd:"download_pdf"});
  document.getElementById("midumdocs-tex").onclick = ()=>_mdPost({md:"cmd", cmd:"download_tex"});
  document.querySelectorAll("#midumdocs-modes button").forEach(b=>{ b.onclick = ()=>{
    _mdMode = b.dataset.mode;
    document.querySelectorAll("#midumdocs-modes button").forEach(x=>x.classList.toggle("active", x === b));
    _mdPost({md:"cmd", cmd:"mode", value:_mdMode});
  }; });
  document.getElementById("reopen-render-surface-btn").onclick = async ()=>{
    const r = await api("render_surface_reopen_ui");
    if (!r.ok) await showConfirm(r.message || "Nothing to reopen.", "Reopen Render Surface");
  };
  initKbOnlyControls();
  initPathPicker();
  initCapabilityPicker();

  // Apply the remembered theme colors immediately, before the heavier
  // startup() call resolves, so the UI doesn't flash default colors first.
  try {
    const s = await api("get_settings");
    _themeColors.dark = s.colors_dark || {...DEFAULT_COLORS};
    _themeColors.light = s.colors_light || {...DEFAULT_COLORS};
    _themeBlobsEnabled.dark = s.blobs_enabled_dark !== false;
    _themeBlobsEnabled.light = s.blobs_enabled_light !== false;
    applyTheme(s.theme || "dark");
    _bgState.cfg = s.bg_image || _bgState.cfg;
    if (_bgState.cfg.enabled && _bgState.cfg.path){
      const r = await api("get_background_image_data");
      _bgState.dataUrl = r && r.ok ? r.data_url : null;
    }
    applyBgImage(_bgState.cfg, _bgState.dataUrl);
    applyLiquidGlass(s.liquid_glass_enabled === true);
    applyGlassOpacity(s.liquid_glass_opacity != null ? s.liquid_glass_opacity : 55);
    applyGlassSheen(s.liquid_glass_sheen_enabled !== false);
    applyChatFontSize(s.chat_font_size);
    applyAmbientHideCapture(s.ambient_hide_from_capture !== false);
    const blobsIndiv = s.blobs_individual || {};
    ["center","a","b","cursor"].forEach(key=>{
      applyBlobIndividual(key, blobsIndiv[key] !== false);
    });
  } catch (e) { /* pywebview bridge not ready yet on some platforms — fine */ }

  // Pygments token-color CSS for syntax-highlighted code blocks -- fetched
  // once and injected as a <style> tag. Injected here (rather than baked
  // into the static CSS below) since the palette lives server-side next to
  // the highlighter itself, so both always agree on the same theme.
  try {
    const css = await api("get_pygments_css");
    if (css){
      const styleEl = document.createElement("style");
      styleEl.id = "pygments-css";
      styleEl.textContent = css;
      document.head.appendChild(styleEl);
    }
  } catch (e) { /* Pygments not installed server-side -- code blocks just stay unhighlighted */ }

  await api("startup");
});
</script>
</body>
</html>
"""


def main():
    # Fail loudly if QtWebEngine isn't actually importable, instead of
    # letting pywebview silently fall back to the broken legacy WinForms
    # renderer (which is what produces the AccessibilityObject.Bounds /
    # "Empty" spam — that fallback is silent by default). Try PySide6 first
    # (has wheels for modern Python), then PyQt5 as a fallback for anyone
    # on an older interpreter.
    _qt_ok = False
    _qt_err = None
    try:
        import qtpy  # noqa: F401 — pywebview's Qt backend imports through this
        from PySide6 import QtWebEngineWidgets  # noqa: F401
        from PySide6.QtWidgets import QApplication  # noqa: F401
        _qt_ok = True
    except ImportError as e:
        _qt_err = e
        try:
            import qtpy  # noqa: F401
            from PyQt5 import QtWebEngineWidgets  # noqa: F401
            from PyQt5.QtWidgets import QApplication  # noqa: F401
            _qt_ok = True
        except ImportError as e2:
            _qt_err = e2

    if not _qt_ok:
        print(
            "\n[FATAL] QtWebEngine is not available: " + str(_qt_err) + "\n"
            "pywebview would silently fall back to the broken legacy WinForms\n"
            "renderer here, which is what caused the AccessibilityObject.Bounds\n"
            "error you saw. pywebview's Qt backend imports through 'qtpy', a\n"
            "compatibility shim — having PySide6/PyQt5 installed is not enough\n"
            "on its own. Fix this by installing both:\n\n"
            "    pip install PySide6 qtpy\n\n"
            "Or, if you're on Python 3.11 or older, PyQt5 instead:\n\n"
            "    pip install PyQt5==5.15.9 PyQtWebEngine==5.15.6 qtpy\n"
        )
        sys.exit(1)

    api = Api()
    window = webview.create_window(
        "Midum Control Center",
        html=_HTML,
        js_api=api,
        width=1600,
        height=950,
        min_size=(1200, 750),
        background_color="#05070c",
        frameless=True,
        # Windows gets real OS drag/resize/Snap back via the WM_NCHITTEST
        # subclass hook below, so pywebview's own crude "drag anywhere"
        # mode would only fight it -- disabled there. Other platforms have
        # no such hook (yet), so they fall back to pywebview's built-in
        # whole-window drag; it moves the window but doesn't snap it.
        easy_drag=(sys.platform != "win32"),
    )
    api.window = window
    window.events.closing += api._on_closing
    if sys.platform == "win32":
        # Installed once the page has actually loaded, so the hwnd/title
        # this looks up (via _find_own_hwnd) is guaranteed to exist.
        window.events.loaded += lambda: _install_win_snap_hook(window)
    # gui="qt" renders through QtWebEngine (PySide6 or PyQt5), which
    # bundles its own Chromium build. Unlike gui="edgechromium" this has no
    # dependency on the Microsoft Edge WebView2 Runtime being installed on
    # the machine, which avoids the WinForms/legacy-Trident fallback and
    # the AccessibleObject.Bounds spam that comes with it. On Linux this
    # also uses QtWebEngine (Chromium) rather than WebKitGTK.
    webview.start(gui="qt", debug=False)


if __name__ == "__main__":
    # Required on Windows (and for the frozen .exe, which always uses the
    # "spawn" start method) before ANY ProcessPoolExecutor/multiprocessing
    # use -- see _warm_pdf_source_cache() above. Without this, a frozen
    # build would re-launch the whole GUI in every worker process it
    # spawns instead of just running the one target function.
    import multiprocessing
    multiprocessing.freeze_support()
    main()