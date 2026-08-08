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
import tools.user_prompt_tools as _user_prompt_tools

import config
import main as midum
import permissions

CHATS_DIR = os.path.join(midum.STORAGE_DIR, "chats")

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
    return [midum.config.MODEL_NAME]


def _list_gemini_web_model_options() -> list:
    """
    Real, current model lineup for the logged-in Gemini web account (via
    gemini_webapi's list_models()), with "(auto)" always offered first so
    the user can go back to auto-selection. Falls back to a small
    hardcoded guess if the account/session can't be reached yet (not
    logged in, library missing, network hiccup, etc) instead of leaving
    the dropdown looking broken.
    """
    try:
        from providers.gemini_web_backend import list_gemini_web_models
        models = list_gemini_web_models()
    except Exception:
        models = []
    if not models:
        fallback = [midum.config.GEMINI_WEB_MODEL, "gemini-3-flash"]
        return list(dict.fromkeys(["(auto)"] + [m for m in fallback if m]))
    return list(dict.fromkeys(["(auto)"] + models))


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
    ("Groq",            "groq"),
]
_PROVIDER_LABEL_TO_KEY = {label: key for label, key in PROVIDER_OPTIONS}
_PROVIDER_KEY_TO_LABEL = {key: label for label, key in PROVIDER_OPTIONS}
DEFAULT_PROVIDER_KEY = "ollama"

# Tabs — "Chat" is the permanent, always-visible pane. Every other entry is
# an auxiliary tool pane that slides in beside it when selected.
TAB_DEFS = [
    ("Chat",         "💬"),
    ("Log",          "📜"),
    ("Model",        "🧬"),
    ("Parameters",   "⚙"),
    ("System Core",  "🧠"),
    ("Knowledge",    "📚"),
    ("Skills",       "🛠"),
    ("Tools",        "🔧"),
    ("Flows",        "🔗"),
    ("MCP",          "🔌"),
    ("Permissions",  "🔐"),
]


# =============================================================================
# JS <-> Python bridge — every method here is callable from the frontend as
# `pywebview.api.<method>(...)` and returns JSON-serialisable data.
# =============================================================================
class Api:
    def __init__(self):
        self.window = None
        self._closing       = False   # set once real teardown has started -- see _push_event / _on_closing

        self._session      = MidumSession()
        self._thinking     = False
        self._log_queue    = queue.Queue()

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
        self._current_chat_id  = uuid.uuid4().hex
        self._chat_title       = None
        self._display_log      = []
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

        self._base_work_dir = r"D:\\"
        if not os.path.exists(self._base_work_dir):
            self._base_work_dir = os.path.expanduser("~/Documents")

        self._stdout_redir = _StdoutRedirector(self._on_log_line)
        sys.stdout = self._stdout_redir

        def _gui_say_intercept(label, text):
            if not text or re.match(r'^[{}\[\]",:\s]*$', text.strip()):
                return
            self._push_event("say", {"text": text})
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

        # Structured tool-call detail (name + args + result) for every tool
        # executed during a turn -- powers the expandable tool-call cards in
        # the chat pane. Separate from the plain-text log line the console
        # already gets from _on_log_line's "-> Executing: ..." interception.
        midum.set_tool_call_hook(self._handle_tool_call)

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

    def _handle_tool_call(self, name: str, args: dict, result: str):
        """Called by orchestration.py right after every tool call executes,
        with the exact arguments it ran and the raw (pre-HTML-escaped)
        result. Pushed to the frontend as a 'tool_call' event so the chat
        pane's tool-call row can be clicked open to show both."""
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
        self._push_event("tool_call", {
            "name": name,
            "args": safe_args,
            "result": str(result)[:8000],
        })

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

            configs = midum._load_mcp_config()
            if configs:
                self._push_event("log", {"text": f"🔌 Reconnecting {len(configs)} saved MCP server(s)...\n"})
                midum.init_mcp_servers_from_config()
                self._push_event("mcp_changed", {})

            midum.memory._bootstrap_all_files()

            # Scheduler: only fires while this app is open (see scheduler.py's
            # module docstring) -- start the background tick thread now, once
            # per app launch. Safe to call again; start_scheduler() is a no-op
            # if a tick thread is already alive.
            try:
                midum.start_scheduler(on_run=self._on_schedule_run)
                self._push_event("log", {"text": "⏰ [Scheduler started -- scheduled Flows will run while this app is open]\n"})
            except Exception as e:
                self._push_event("log", {"text": f"⚠️ Scheduler failed to start: {e}\n"})

            # Every launch starts a genuinely new session -- the same reset
            # the "New Session" button performs. Full continuity across
            # restarts is already covered by the persisted chat history
            # (sidebar -> open any past chat), so there's no need to
            # silently carry the previous session's goal/notes forward
            # into what the UI is showing as a brand-new, empty chat.
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
        try:
            for name in midum.list_pdf_sources():
                try:
                    midum.build_pdf_source_parts(name)
                except Exception:
                    pass
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
        return {
            "options": [label for label, _ in PROVIDER_OPTIONS],
            "current": _PROVIDER_KEY_TO_LABEL[self._selected_provider],
            "models": _known_models_for_provider(self._selected_provider),
            "current_model": self._selected_model,
        }

    # Syntax highlighting (Pygments) --------------------------------------
    # ```-fenced code blocks in chat are highlighted by running the block's
    # text through Pygments here and handing back ready-to-inject HTML (see
    # renderPendingCodeHighlight() in the frontend JS). Pure string
    # transform, safe to call for every code block rendered, including ones
    # re-rendered from a loaded chat history.
    _PYGMENTS_STYLE = "one-dark"

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
                self._chat_title = text[:60] or None
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

        self._push_event("log", {"text": f"🔀 [Provider switched: {label} — {model_id or '(auto)'}]\n"})
        return self.get_status()

    # ── Persisted GUI settings (default model + theme colors) ────────────
    _SETTINGS_FILENAME = "gui_settings.json"
    _DEFAULT_COLORS = {
        "accent": "#60a5fa", "accent2": "#1d4ed8",
        "bg": "#05070c", "panel": "#0b0f19", "text": "#f3f4f6",
        "blob_center": "#60a5fa", "blob_a": "#f472b6", "blob_b": "#34d399", "blob_cursor": "#a78bfa",
    }
    _DEFAULT_THEME = "dark"
    _DEFAULT_BLOBS_ENABLED = True
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
            "colors": dict(self._DEFAULT_COLORS),
            "blobs_enabled": self._DEFAULT_BLOBS_ENABLED,
            "bg_image": dict(self._DEFAULT_BG_IMAGE),
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
                if isinstance(saved.get("colors"), dict):
                    defaults["colors"].update(saved["colors"])
                if isinstance(saved.get("blobs_enabled"), bool):
                    defaults["blobs_enabled"] = saved["blobs_enabled"]
                if isinstance(saved.get("bg_image"), dict):
                    defaults["bg_image"].update(saved["bg_image"])
        except Exception as e:
            self._push_event("log", {"text": f"⚠️ Failed to read saved settings: {e}\n"})
        return defaults

    def save_settings(self, settings: dict):
        try:
            current = self.get_settings()
            if settings.get("provider"):
                current["provider"] = settings["provider"]
            if "model" in settings:
                current["model"] = settings["model"] or ""
            if settings.get("theme") in ("dark", "light"):
                current["theme"] = settings["theme"]
            if isinstance(settings.get("colors"), dict):
                current["colors"].update({k: v for k, v in settings["colors"].items() if v})
            if isinstance(settings.get("blobs_enabled"), bool):
                current["blobs_enabled"] = settings["blobs_enabled"]
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

            self._push_event("log", {"text": "💾 [Settings saved — will be restored on next launch]\n"})
            return {"ok": True, "settings": current}
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
    def _scan_workspace_directory(self):
        if not os.path.exists(self._base_work_dir):
            os.makedirs(self._base_work_dir, exist_ok=True)
        try:
            subdirs = sorted(
                d for d in os.listdir(self._base_work_dir)
                if os.path.isdir(os.path.join(self._base_work_dir, d))
            )
            if not subdirs:
                subdirs = []
            self._push_event("projects", {"projects": subdirs})
            if subdirs:
                self.switch_project(subdirs[0])
        except Exception as e:
            self._push_event("log", {"text": f"⚠️ Scan failed: {e}\n"})

    def list_projects(self):
        try:
            return sorted(
                d for d in os.listdir(self._base_work_dir)
                if os.path.isdir(os.path.join(self._base_work_dir, d))
            )
        except Exception:
            return []

    def switch_project(self, name: str):
        project_dir = os.path.join(self._base_work_dir, name)
        project_file = os.path.join(project_dir, "project_memory.md")
        midum.memory._active_project_memory_path = project_file

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

        midum.memory.update_memory("master", f"Active project context switched to: {name} ({project_dir})")
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

    def create_project(self, name: str):
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "Name required."}
        project_dir = os.path.join(self._base_work_dir, name)
        if os.path.exists(project_dir):
            return {"ok": False, "error": "A project with this name already exists."}
        os.makedirs(project_dir, exist_ok=True)
        midum.write_local_file(
            os.path.join(project_dir, "project_memory.md"),
            f"# Project Memory: {name}\nCreated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n",
        )
        self.switch_project(name)
        return {"ok": True, "projects": self.list_projects()}

    def change_base_work_directory(self):
        try:
            result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            result = None
        if result:
            self._base_work_dir = os.path.abspath(result[0])
            self._push_event("system_line", {"text": f"[Base scan directory moved to: {self._base_work_dir}]"})
            self._scan_workspace_directory()
        return {"base_dir": self._base_work_dir}

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

        self._current_chat_id = data.get("id", chat_id)
        self._chat_title = data.get("title")
        history = data.get("history") or []
        with self._session._lock:
            self._session.history = history
            self._session.turn_counter = max(1, sum(1 for m in history if m.get("role") == "user"))
        self._display_log = list(data.get("display", []))
        self._voice_stream_tag = None
        self._voice_stream_idx = None
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
        return {"ok": True, "display": self._display_log, "kb_state": self._kb_state}

    def delete_chat(self, chat_id: str):
        self._chat_store.delete(chat_id)
        if chat_id == self._current_chat_id:
            self._start_new_chat_record()
        return {"ok": True, "chats": self.list_chats()}

    def _reset_session_memory_file(self):
        """(Re)creates a blank session-memory file with no active goal.
        Shared by the explicit "New Session" button and by every app
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
        if self._thinking:
            return {"ok": False, "error": "Busy — wait for the current run to finish or abort first."}
        try:
            self._reset_session_memory_file()
            self._session.reset()
            self._start_new_chat_record()
            return {"ok": True}
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
                self._chat_store.save(
                    self._current_chat_id, title, self._session.snapshot(), list(self._display_log),
                    kb_state=dict(self._kb_state),
                    explain_progress=dict(self._explain_progress),
                    explain_page_progress=dict(self._explain_page_progress),
                )
            except Exception as e:
                self._push_event("log", {"text": f"⚠ Failed to save chat history: {e}\n"})

    # ── Send / receive ──────────────────────────────────────────────────
    _KB_ONLY_MARKER = "[MIDUM KB-ONLY MODE]"
    _EXPLAIN_MODE_MARKER = "[MIDUM EXPLAIN MODE]"

    def send_message(self, user_input: str, kb_only: bool = False, kb_sources: list = None, explain_mode: bool = False, explain_mode_type: str = "part"):
        if self._thinking:
            return {"ok": False, "error": "busy"}
        user_input = (user_input or "").strip()
        if not user_input:
            return {"ok": False, "error": "empty"}

        self._display_log.append(("user", user_input))
        if not self._chat_title:
            self._chat_title = user_input[:60] or None
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
            args=(list(self._session.snapshot()), kb_only, kb_context_msg),
            daemon=True,
        ).start()
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

    def _run_turn(self, history_snapshot: list, kb_only: bool = False, kb_context_msg: str = None):
        try:
            midum._abort_event.clear()
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
            if cleaned_reply:
                self._push_event("reply", {"text": cleaned_reply})
            for lang, body in visuals:
                block = f"```{lang}\n{body}\n```"
                self._push_event("reply", {"text": block})
            self._persist_current_chat()

            threading.Thread(target=midum.python_trigger_memory_update, args=(tool_outputs, reply), daemon=True).start()

            self._push_event("status", {"text": "Ready", "level": "ok"})
        except Exception as e:
            self._push_event("error_line", {"text": f"[Engine error: {e}]"})
            self._push_event("status", {"text": "Error", "level": "err"})
            self._persist_current_chat()
        finally:
            permissions.set_kb_only_mode(False)
            self._thinking = False
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
        self._push_event("status", {"text": "Aborted", "level": "err"})
        self._push_event("log", {"text": "🛑 Execution pipeline aborted by user\n"})
        return {"ok": True}

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

    def run_flow(self, name: str):
        """Run a saved flow the same way a native tool is run from the
        Tools tab -- in a background thread, pushing the result back as a
        'tool_result' event so the same output box can show it."""
        def worker():
            try:
                out = midum.run_flow(name)
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
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;height:100%;background:var(--bg);color:var(--text);
  font-family:"Segoe UI",-apple-system,sans-serif;overflow:hidden;user-select:none;}
button{font-family:inherit;cursor:pointer;}
input,textarea{font-family:inherit;}
::-webkit-scrollbar{width:8px;height:8px;}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:8px;}
::-webkit-scrollbar-track{background:transparent;}

#root{position:relative;width:100vw;height:100vh;background:var(--bg);}

/* ── Top bar : 100% wide, 15% tall, rounded pill bar ── */
#topbar-wrap{position:absolute;left:0;top:0;width:100%;height:15%;}
#topbar{
  position:absolute;inset:var(--gap);border-radius:var(--radius);
  background:var(--panel);border:1px solid var(--border2);
  display:flex;align-items:center;justify-content:space-between;padding:0 18px;
}
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
#status-dot{width:8px;height:8px;border-radius:50%;background:var(--yellow);transition:background .2s;}
#status-label{font-size:12px;color:var(--subtext);white-space:nowrap;}

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
html.has-bg-image .pane{
  background:color-mix(in srgb, var(--panel) 62%, transparent);
  border:1px solid color-mix(in srgb, var(--text) 12%, transparent);
  box-shadow:inset 0 1px 0 color-mix(in srgb, var(--text) 8%, transparent), 0 8px 30px rgba(0,0,0,.35);
}
html.has-bg-image #topbar{
  background:color-mix(in srgb, var(--panel) 62%, transparent) !important;
  border:1px solid color-mix(in srgb, var(--text) 12%, transparent);
  box-shadow:inset 0 1px 0 color-mix(in srgb, var(--text) 8%, transparent), 0 8px 30px rgba(0,0,0,.35);
}
/* Ambient blobs are an optional setting (SETTINGS → AMBIENT BLOBS). When
   turned off, hide the blob layer entirely and give the panes/topbar their
   normal opaque surface back -- otherwise, with #blob-layer gone, they'd
   just show flat var(--bg) through fully transparent panes. Skipped when a
   background image is active (html.has-bg-image above already gives that
   case its own tinted-panel treatment). */
html.blobs-off #blob-layer{ display:none; }
html.blobs-off:not(.has-bg-image) .pane{
  background:var(--panel);
  border:1px solid var(--border2);
}
html.blobs-off:not(.has-bg-image) #topbar{
  background:var(--panel) !important;
  border:1px solid var(--border2) !important;
}
.pane-hidden{opacity:0;pointer-events:none;}

/* Tool pane */
#tool-pane-wrap{left:0;width:0;}
#tool-content{flex:1;padding:14px;overflow-y:auto;}

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
.bubble{border-radius:18px;padding:10px 16px;font-size:14px;line-height:1.5;max-width:78%;white-space:pre-wrap;word-wrap:break-word;}
.bubble.user{background:var(--user-msg);}
.bubble.midum{background:transparent;max-width:100%;}
.row.system, .row.error{align-items:center;text-align:center;}
.row.system .bubble{background:transparent;color:var(--subtext);font-size:12px;}
.row.error .bubble{background:transparent;color:var(--red);font-size:12px;}
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
@keyframes toolPulse{
  0%{  transform:scale(0.7); box-shadow:0 0 0 0 rgba(16,185,129,.55); }
  50%{ transform:scale(1.25); box-shadow:0 0 0 4px rgba(16,185,129,0); }
  100%{transform:scale(0.7); box-shadow:0 0 0 0 rgba(16,185,129,0); }
}
pre.code-block{background:var(--tool-bg);color:var(--tool-text);border-radius:12px;padding:10px;
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
code.inline-code{background:var(--surface2);color:var(--tool-text);border-radius:4px;padding:1px 5px;
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
.perm-opt.active[data-level="deny"]{background:var(--red);color:#fff;}
.perm-empty{font-size:11px;color:var(--subtext);padding:10px;text-align:center;}

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
        <div id="brand">⚡ Midum</div>
        <button class="icon-btn" id="sidebar-toggle" title="Toggle sidebar">☰</button>
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
      <div class="pane">
        <button class="icon-btn" id="copy-chat-btn" title="Copy full conversation"
          style="position:absolute;top:10px;right:10px;z-index:5;">⧉</button>
        <div id="chat-scroll"><div id="chat-col"></div></div>
        <div id="input-row">
          <div style="width:100%;max-width:760px;position:relative;">
            <div id="kb-only-badge"><span>🔒</span><span id="kb-only-badge-text">KB Only — internet disabled, answering from selected PDF sources</span></div>
            <div id="kb-explain-actions-row" style="display:none;gap:6px;margin-bottom:8px;">
              <button id="kb-next-part-btn" style="flex:1;height:30px;border-radius:14px;border:none;background:var(--accent);color:#fff;font-size:11px;font-weight:600;">Next Part →</button>
              <button id="kb-open-source-btn" style="flex:0 0 auto;padding:0 14px;height:30px;border-radius:14px;border:1px solid var(--border2);background:var(--surface2);color:var(--text);font-size:11px;font-weight:600;">📄 Open Source</button>
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
              <button id="kb-start-explanation-btn" style="display:none;width:100%;margin-top:8px;height:30px;border-radius:14px;border:none;background:var(--accent);color:#fff;font-size:11px;font-weight:600;">▶ Start Explanation</button>
              <div class="kb-only-hint" id="kb-explain-hint" style="display:none;">Walks through the selected source(s) part by part (one sub-sub-heading per response) — say "next" to continue.</div>
            </div>
            <div id="input-box">
              <button class="icon-btn" id="kb-toggle-btn" title="Knowledge Base options">▾</button>
              <textarea id="msg-input" placeholder="Message Midum..." rows="1"></textarea>
              <button id="send-btn">↑</button>
            </div>
            <div id="send-hint">Enter to send</div>
          </div>
        </div>
      </div>
    </div>

    <div class="pane-wrap pane-hidden" id="sidebar-pane-wrap">
      <div class="pane"><div id="sidebar-inner"></div></div>
    </div>
  </div>
</div>

<div id="modal-overlay"><div class="modal-box" id="modal-box"></div></div>

<script>
const TABS = [
  ["Chat","💬"], ["Voice","🎤"], ["Log","📜"], ["Model","🧬"], ["Parameters","⚙"],
  ["System Core","🧠"], ["Knowledge","📚"], ["Skills","🛠"], ["Tools","🔧"], ["Flows","🔗"], ["MCP","🔌"], ["Permissions","🔐"]
];

let state = {
  activeTab: "Chat",
  sidebarOpen: false,
  thinking: false,
  kbOnly: false,
  kbSources: [],
  explainMode: false,
  explainModeType: "part",   // "part" (Part-by-Part) or "page" (Page-by-Page)
};

let voiceState = { running: false, connecting: false };

function api(name, ...args){ return window.pywebview.api[name](...args); }

// ── Layout engine ------------------------------------------------------
function targetGeo(){
  const showTool = state.activeTab !== "Chat";
  const showSide = state.sidebarOpen;
  // The Flows tab is a node-graph editor that needs real canvas space to
  // be usable, so it's the one tab given a pane larger than the chat
  // panel -- every other tab keeps the normal (smaller-than-chat) split.
  const isFlows = state.activeTab === "Flows";
  if (showTool && showSide)      return isFlows ? {tool:[0,55], chat:[55,25], side:[80,20]} : {tool:[0,30], chat:[30,50], side:[80,20]};
  if (showTool && !showSide)     return isFlows ? {tool:[0,70], chat:[70,30], side:[100,0]} : {tool:[0,40], chat:[40,60], side:[100,0]};
  if (!showTool && showSide)     return {tool:[0,0],  chat:[0,80],  side:[80,20]};
  return {tool:[0,0], chat:[0,100], side:[100,0]};
}

function applyLayout(){
  const g = targetGeo();
  const toolWrap = document.getElementById("tool-pane-wrap");
  const chatWrap = document.getElementById("chat-pane-wrap");
  const sideWrap = document.getElementById("sidebar-pane-wrap");

  toolWrap.style.left = g.tool[0]+"%"; toolWrap.style.width = g.tool[1]+"%";
  chatWrap.style.left = g.chat[0]+"%"; chatWrap.style.width = g.chat[1]+"%";
  sideWrap.style.left = g.side[0]+"%"; sideWrap.style.width = g.side[1]+"%";

  toolWrap.classList.toggle("pane-hidden", g.tool[1] === 0);
  sideWrap.classList.toggle("pane-hidden", g.side[1] === 0);
}

function switchTab(name){
  if (name === state.activeTab) return;
  state.activeTab = name;
  document.querySelectorAll(".tab-btn").forEach(b=>{
    b.classList.toggle("active", b.dataset.name === name);
  });
  if (name !== "Chat") showToolPane(name);
  applyLayout();
  positionTabHighlight();
}

function positionTabHighlight(){
  const bar = document.getElementById("tabbar");
  const hl  = document.getElementById("tab-highlight");
  const activeBtn = bar && bar.querySelector(".tab-btn.active");
  if (!bar || !hl || !activeBtn) return;
  hl.style.left  = activeBtn.offsetLeft + "px";
  hl.style.width = activeBtn.offsetWidth + "px";
}

function toggleSidebar(){
  state.sidebarOpen = !state.sidebarOpen;
  document.getElementById("sidebar-toggle").classList.toggle("active", state.sidebarOpen);
  if (state.sidebarOpen) refreshHistory();
  applyLayout();
}

// ── Top bar build --------------------------------------------------------
function buildTabbar(){
  const bar = document.getElementById("tabbar");
  TABS.forEach(([name, icon])=>{
    const b = document.createElement("button");
    b.className = "tab-btn" + (name === "Chat" ? " active" : "");
    b.dataset.name = name;
    b.innerHTML = `<span>${icon}</span><span>${name}</span>`;
    b.onclick = ()=>switchTab(name);
    bar.appendChild(b);
  });
  requestAnimationFrame(positionTabHighlight);
  window.addEventListener("resize", positionTabHighlight);
}

// ── Chat rendering --------------------------------------------------------
function escapeHtml(s){
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

function renderInline(text){
  let t = escapeHtml(text);
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
    const html = `<div class="code-block-wrap"><button class="code-copy-btn" title="Copy code">⧉</button><pre class="code-block" data-lang="${lang}">${body}</pre></div>`;
    _codeBlocks.push(html);
    return `\u0000CODEBLOCK${_codeBlocks.length - 1}\u0000`;
  });
  t = t.replace(/`([^`]+)`/g, (m,c)=>`<code class="inline-code">${c}</code>`);
  // LaTeX math ($$...$$, \[...\], \(...\), $...$) is swapped for a
  // placeholder <span class="math-tex"> BEFORE the bold/italic/etc
  // regexes below run, so things like x_i or a*b inside a formula never
  // get misread as markdown emphasis -- see extractMath()'s docstring.
  t = extractMath(t);
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
    return `<div class="flowchart-wrap"><div style="font-size:11px;color:var(--subtext);margin-bottom:6px;">📊 ${escapeHtml(data.title || "Flowchart")}</div>${svg}</div>`;
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
      el.outerHTML = `<div class="code-block-wrap"><button class="code-copy-btn" title="Copy code">⧉</button><pre class="code-block">${escapeHtml(el.textContent || "")}</pre></div>`;
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
      wrap.outerHTML = `<div class="code-block-wrap"><button class="code-copy-btn" title="Copy code">⧉</button><pre class="code-block">${escapeHtml(src)}</pre></div>`;
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
function extractMath(text){
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (m, expr)=>
    `<span class="math-tex" data-display="1">${expr}</span>`);
  text = text.replace(/\\\[([\s\S]+?)\\\]/g, (m, expr)=>
    `<span class="math-tex" data-display="1">${expr}</span>`);
  text = text.replace(/\\\(([\s\S]+?)\\\)/g, (m, expr)=>
    `<span class="math-tex" data-display="0">${expr}</span>`);
  text = text.replace(/\$(?!\s)([^$\n]+?)(?<!\s)\$/g, (m, expr)=>
    `<span class="math-tex" data-display="0">${expr}</span>`);
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
            <a class="img-save-btn" href="data:image/png;base64,${img.data_b64}" download="${escapeHtml(fname)}" title="Save image">💾</a>
          </div>`;
        }).join('');
        renderedBlock = `<div class="flowchart-wrap">
                           <div style="font-size:11px;color:var(--subtext);margin-bottom:6px;">🖼️ ${escapeHtml(title)}</div>
                           ${imagesHtml}
                         </div>`;
      }
    } catch (e) {
      // Fallback for malformed JSON
    }

    out += renderedBlock || `<div class="code-block-wrap"><button class="code-copy-btn" title="Copy code">⧉</button><pre class="code-block" data-lang="${lang === "mermaid" ? "" : "json"}">${escapeHtml(body)}</pre></div>`;
    lastIndex = FLOWCHART_FENCE_RE.lastIndex;
  }
  const rest = text.slice(lastIndex);
  if (rest.trim()) out += renderInline(rest);
  return out;
}

function chatCol(){ return document.getElementById("chat-col"); }

// Tracks whichever tool-call row is currently "live" so only the most
// recent one pulses — earlier tool calls settle to a plain dot once a
// newer tool call, a reply, or turn completion supersedes them.
let _activeToolDot = null;
function setActiveToolDot(el){
  if (_activeToolDot) _activeToolDot.classList.remove("active");
  _activeToolDot = el || null;
  if (_activeToolDot) _activeToolDot.classList.add("active");
}

// FIFO queue of tool-call rows awaiting their 'tool_call' detail event
// (name/args/result), pushed in appendRow() the instant a tool starts and
// consumed by attachToolCallDetail() once the call finishes. Matched by
// tool name first (handles the rare case of overlapping calls), otherwise
// just the oldest unresolved row.
let _pendingToolRows = [];

function attachToolCallDetail(name, args, result){
  let row = null;
  const idx = _pendingToolRows.findIndex(r=>r.dataset.toolName === name);
  if (idx !== -1){ row = _pendingToolRows[idx]; _pendingToolRows.splice(idx, 1); }
  else if (_pendingToolRows.length){ row = _pendingToolRows.shift(); }
  if (!row) return;
  const detail = row.querySelector(".tool-detail");
  if (!detail) return;
  const argsStr = (args && Object.keys(args).length) ? JSON.stringify(args, null, 2) : "(no arguments)";
  detail.innerHTML = `
    <div class="tool-detail-label">Call</div>
    <pre>${escapeHtml(name)}(${escapeHtml(argsStr)})</pre>
    <div class="tool-detail-label">Result</div>
    <pre>${escapeHtml(result || "(empty)")}</pre>`;
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

function appendRow(tag, text){
  const col = chatCol();
  const row = document.createElement("div");
  if (tag === "user"){
    row.className = "row user";
    row.innerHTML = `<div class="row-label-row"><div class="row-label">You</div><button class="row-copy-btn" title="Copy message">⧉</button></div><div class="bubble user">${escapeHtml(text)}</div>`;
    wireRowCopyBtn(row);
  } else if (tag === "midum"){
    row.className = "row midum";
    row.innerHTML = `<div class="row-label-row"><div class="row-label">Midum</div><button class="row-copy-btn" title="Copy response">⧉</button></div><div class="bubble midum">${renderMidumContent(text)}</div>`;
    wireRowCopyBtn(row);
    setActiveToolDot(null);
    col.appendChild(row);
    animateWords(row.querySelector(".bubble"));
    renderPendingMermaid();
    renderPendingMath();
    renderPendingCodeHighlight();
    scrollToBottom();
    return;
  } else if (tag === "system"){
    row.className = "row system";
    row.innerHTML = `<div class="bubble">${escapeHtml(text)}</div>`;
  } else if (tag === "error"){
    row.className = "row error";
    row.innerHTML = `<div class="bubble">${escapeHtml(text)}</div>`;
    setActiveToolDot(null);
  } else if (tag === "tool"){
    row.className = "row tool tool-row";
    const nameMatch = /-> Executing: '([^']+)'/.exec(text);
    row.dataset.toolName = nameMatch ? nameMatch[1] : "";
    row.innerHTML = `
      <div class="tool-line expandable">
        <span class="tool-dot"></span><span class="gear">⚙</span><span>${escapeHtml(text)}</span>
        <span class="chevron">▶</span>
        <button class="row-copy-btn" title="Copy tool call" style="margin-left:6px;">⧉</button>
      </div>
      <div class="tool-detail"><div class="tool-detail-label">Waiting for result…</div></div>`;
    row.querySelector(".tool-line").onclick = (e)=>{
      if (e.target.closest(".row-copy-btn")) return;
      row.classList.toggle("open");
    };
    wireRowCopyBtn(row);
    col.appendChild(row);
    setActiveToolDot(row.querySelector(".tool-dot"));
    _pendingToolRows.push(row);
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
    return "Midum: " + bubblePlainText(b);
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
  const orig = btn.textContent;
  btn.textContent = "✓";
  btn.classList.add("copied");
  setTimeout(()=>{ btn.textContent = orig; btn.classList.remove("copied"); }, 1000);
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

function clearChat(){ chatCol().innerHTML = ""; _pendingToolRows = []; }

// ── Ask cards --------------------------------------------------------------
function appendAsk(id, kind, payload){
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
    card.innerHTML = `<div class="ask-hdr">❓ ${escapeHtml(payload.title||"Midum needs input")}</div>
      <div style="font-size:13px;margin-bottom:10px;">${escapeHtml(payload.prompt||"")}</div>
      <input type="text" placeholder="Type your answer..." />
      <div class="ask-actions">
        <button class="ghost-btn" data-act="cancel">Cancel</button>
        <button class="btn" data-act="submit" style="background:var(--accent);color:#fff;">Submit</button>
      </div>`;
    const input = card.querySelector("input");
    const submit = ()=>{ const v=input.value.trim(); resolve(`❓ → "${v||"(empty)"}"`, v||"[USER SUBMITTED EMPTY TEXT]"); };
    card.querySelector('[data-act=submit]').onclick = submit;
    card.querySelector('[data-act=cancel]').onclick = ()=>resolve("❓ cancelled", "[USER CANCELLED]");
    input.addEventListener("keydown", e=>{ if (e.key === "Enter") submit(); });
    setTimeout(()=>input.focus(), 30);
  } else if (kind === "approval"){
    card.innerHTML = `<div class="ask-hdr">⚠ Midum requests approval</div>
      <div style="font-weight:700;font-size:13px;">${escapeHtml(payload.message||"")}</div>
      <div style="font-size:12px;color:var(--subtext);margin:6px 0 10px;">${escapeHtml(payload.details||"")}</div>
      <div class="ask-actions">
        <button class="ghost-btn" data-act="decline" style="color:var(--red);border-color:#3f0f0f;">❌ Decline</button>
        <button class="btn" data-act="approve" style="background:var(--green);color:#fff;">✅ Approve</button>
      </div>`;
    card.querySelector('[data-act=approve]').onclick = ()=>resolve("✅ Approved", "APPROVED");
    card.querySelector('[data-act=decline]').onclick = ()=>resolve("❌ Declined", "DECLINED");
  } else if (kind === "choice"){
    const opts = (payload.options||[]).map(o=>`<button class="ask-opt-btn" data-v="${escapeHtml(o)}">${escapeHtml(o)}</button>`).join("");
    card.innerHTML = `<div class="ask-hdr">❓ Midum has a question</div>
      <div style="font-weight:700;font-size:13px;margin-bottom:10px;">${escapeHtml(payload.question||"")}</div>
      ${opts}
      ${payload.allow_custom !== false ? '<div style="display:flex;gap:6px;margin-top:6px;"><input type="text" placeholder="Something else..." style="flex:1;"/><button class="btn" data-act="custom">Other...</button></div>' : ""}`;
    card.querySelectorAll(".ask-opt-btn").forEach(b=>{
      b.onclick = ()=>resolve(`❓ → "${b.dataset.v}"`, b.dataset.v);
    });
    const customBtn = card.querySelector('[data-act=custom]');
    if (customBtn){
      const inp = card.querySelector('input[type=text]');
      customBtn.onclick = ()=>{ const v=inp.value.trim(); if(v) resolve(`❓ → "${v}"`, v); };
    }
  } else if (kind === "file"){
    card.innerHTML = `<div class="ask-hdr">📁 Midum needs a file</div>
      <div style="font-size:13px;margin-bottom:10px;">${escapeHtml(payload.prompt||"Select a file")}</div>
      <div class="ask-actions">
        <button class="ghost-btn" data-act="cancel">Cancel</button>
        <button class="btn" data-act="browse" style="background:var(--accent);color:#fff;">Browse...</button>
      </div>`;
    card.querySelector('[data-act=cancel]').onclick = ()=>resolve("📁 cancelled", "[USER CANCELLED]");
    card.querySelector('[data-act=browse]').onclick = async ()=>{
      const r = await api("pick_file", payload.must_exist !== false);
      resolve(`📁 → ${r.path||"cancelled"}`, r.path || "[USER CANCELLED]");
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
  const maxHeight = 216; // ~10 lines
  const newHeight = Math.min(el.scrollHeight, maxHeight);
  el.style.height = newHeight + "px";
  el.style.overflowY = el.scrollHeight > maxHeight ? "auto" : "hidden";
}

async function sendMessage(){
  if (state.thinking) return;
  const input = document.getElementById("msg-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  autosizeMsgInput();
  appendRow("user", text);
  setActiveToolDot(null);
  setStatus("Executing turns...", "busy");
  state.thinking = true;
  const kbOnlyActive = !!(state.kbOnly && state.kbSources.length);
  const explainActive = !!(kbOnlyActive && state.explainMode);
  await api("send_message", text, kbOnlyActive, kbOnlyActive ? state.kbSources : [], explainActive, state.explainModeType);
}

function setStatus(text, level){
  document.getElementById("status-label").textContent = text;
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
    startBtn.textContent = explainActive ? "▶ Restart Explanation" : "▶ Start Explanation";
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
  if (kind === "status"){ setStatus(payload.text, payload.level); }
  else if (kind === "reply"){ appendRow("midum", payload.text); }
  else if (kind === "say"){ appendRow("midum", payload.text); }
  else if (kind === "system_line"){ appendRow("system", payload.text); }
  else if (kind === "error_line"){ appendRow("error", payload.text); }
  else if (kind === "tool_line"){ appendRow("tool", payload.text); }
  else if (kind === "tool_call"){ attachToolCallDetail(payload.name, payload.args, payload.result); }
  else if (kind === "log"){ appendLog(payload.text); }
  else if (kind === "done"){ state.thinking = false; setActiveToolDot(null); }
  else if (kind === "projects"){ populateProjects(payload.projects); }
  else if (kind === "ask"){ appendAsk(payload.id, payload.kind, payload.payload); }
  else if (kind === "mcp_changed"){ if (state.activeTab === "MCP") refreshMcpList(); }
  else if (kind === "schedule_ran"){ if (state.activeTab === "Schedule") showToolPane("Schedule"); }
  else if (kind === "tool_result"){ const box=document.getElementById("tool-output"); if(box) box.value = payload.output; }
  else if (kind === "voice_status"){ handleVoiceStatus(payload); }
  else if (kind === "voice_transcript"){ appendVoiceTranscript(payload.role, payload.text); }
  else if (kind === "voice_tool_call"){ appendVoiceToolEvent("call", payload.name, payload.args); }
  else if (kind === "voice_tool_result"){ appendVoiceToolEvent("result", payload.name, payload.result); }
  else if (kind === "voice_interrupted"){ appendVoiceSystemNote("↺ interrupted"); }
  else if (kind === "voice_turn_complete"){ _voiceStreamRow = null; }
  else if (kind === "voice_error"){ appendVoiceSystemNote("⚠ " + payload.message); handleVoiceStatus({status:"stopped"}); }
  else if (kind === "ptt_captured"){ handlePttCaptured(payload); }
  else if (kind === "voice_ptt_state"){ handlePttState(payload); }
  else if (kind === "pdf_cache_warmed"){ if (typeof onPdfCacheWarmed === "function") onPdfCacheWarmed(); }
};

function appendLog(text){
  const box = document.getElementById("log-box");
  if (box){ box.textContent += text; box.scrollTop = box.scrollHeight; }
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

// ── Sidebar -------------------------------------------------------------
function buildSidebar(){
  const el = document.getElementById("sidebar-inner");
  el.innerHTML = `
    <div id="sidebar-main-view">
      <div class="hdr-row">
        <div class="section-label">WORKSPACE</div>
        <button class="icon-btn" style="width:26px;height:26px;font-size:11px;" id="sidebar-close">✕</button>
      </div>
      <button class="btn" id="new-session-btn">+ New Session</button>
      <select id="project-select"></select>
      <div class="btn-row">
        <button class="ghost-btn" id="proj-new">+ Project</button>
        <button class="ghost-btn" id="proj-scan">📂 Scan</button>
        <button class="ghost-btn" id="proj-code">💻 Code</button>
      </div>
      <div id="file-list"></div>
      <div class="divider"></div>
      <div class="section-label">CHAT HISTORY</div>
      <div id="history-list"></div>
      <div class="divider"></div>
      <div class="hdr-row">
        <div class="section-label">SETTINGS</div>
        <button class="icon-btn" style="width:22px;height:22px;font-size:10px;" id="settings-toggle">⚙</button>
      </div>
      <div id="sidebar-footer">
        <button class="ghost-btn" id="proj-term">🐚 Terminal</button>
        <button class="ghost-btn" id="shutdown-btn" style="color:var(--red);">⏻ Shutdown</button>
      </div>
    </div>
    <div id="sidebar-settings-overlay">
      <div class="hdr-row">
        <button id="settings-back-btn">←</button>
        <div class="section-label">SETTINGS</div>
        <div style="width:26px;"></div>
      </div>
      <div class="field-label" style="margin:2px 0 0;">THEME</div>
      <div class="btn-row" id="settings-theme-toggle">
        <button class="ghost-btn" data-theme="dark" style="flex:1;">🌙 Dark</button>
        <button class="ghost-btn" data-theme="light" style="flex:1;">☀️ Light</button>
      </div>
      <div class="hdr-row" style="margin-top:4px;">
        <div class="field-label" style="margin:0;">AMBIENT BLOBS</div>
        <label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--subtext);">
          <input type="checkbox" id="settings-blobs-enabled" style="width:14px;height:14px;"/> Enabled
        </label>
      </div>
      <div class="hdr-row" style="margin-top:4px;">
        <div class="field-label" style="margin:0;">BACKGROUND IMAGE</div>
        <label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--subtext);">
          <input type="checkbox" id="settings-bg-enabled" style="width:14px;height:14px;"/> Enabled
        </label>
      </div>
      <div class="btn-row">
        <button class="ghost-btn" id="bg-choose" style="flex:1;">🖼 Choose Image...</button>
        <button class="ghost-btn" id="bg-clear" style="flex:0 0 auto;">✕</button>
      </div>
      <div id="bg-filename" style="font-size:9px;color:var(--subtext);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"></div>
      <div class="field-label" style="margin-top:2px;">Brightness</div>
      <input type="range" id="bg-brightness" min="40" max="160" value="100" style="width:100%;"/>
      <div class="field-label">Blur</div>
      <input type="range" id="bg-blur" min="0" max="40" value="0" style="width:100%;"/>
      <div class="field-label">Opacity</div>
      <input type="range" id="bg-opacity" min="10" max="100" value="100" style="width:100%;"/>
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
  enhanceSelect(document.getElementById("project-select"));
  enhanceSelect(document.getElementById("settings-provider"));
  document.getElementById("new-session-btn").onclick = async ()=>{
    const ok = await showConfirm("Clear current session context and reset memories?", "New Session");
    if (!ok) return;
    await api("new_session"); clearChat(); applyKbState(null); refreshHistory();
  };
  document.getElementById("project-select").onchange = async (e)=>{
    const info = await api("switch_project", e.target.value);
    renderFileList(info);
  };
  document.getElementById("proj-new").onclick = async ()=>{
    const name = await showPrompt("Enter new Project/Workspace name:", "New Project");
    if (!name) return;
    const r = await api("create_project", name);
    if (!r.ok) showAlert(r.error, "Error"); else populateProjects(r.projects);
  };
  document.getElementById("proj-scan").onclick = async ()=>{ await api("change_base_work_directory"); };
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
  document.getElementById("settings-save").onclick = saveSettingsPanel;
  document.getElementById("settings-reset").onclick = ()=>{
    // Reset to the ACTIVE theme's defaults (not always Dark), so resetting
    // while in Light mode gives back Light's real colors. Blob colors
    // aren't theme-dependent (THEME_VARS doesn't touch them), so they
    // always come from DEFAULT_COLORS regardless of active theme.
    const defaults = {...DEFAULT_COLORS, ...(THEME_VARS[_activeTheme] || {})};
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
  document.getElementById("settings-bg-enabled").onchange = (e)=>{
    _bgState.cfg.enabled = e.target.checked;
    applyBgImage(_bgState.cfg, _bgState.dataUrl);
  };
  document.getElementById("settings-blobs-enabled").onchange = (e)=>{
    applyBlobsEnabled(e.target.checked);
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

function applyTheme(name){
  const vars = THEME_VARS[name] || THEME_VARS.dark;
  const root = document.documentElement.style;
  Object.entries(vars).forEach(([k,v])=> root.setProperty(`--${k}`, v));
  _activeTheme = name;
  document.querySelectorAll('#settings-theme-toggle [data-theme]').forEach(b=>{
    b.classList.toggle("active", b.dataset.theme === name);
    b.style.background = b.dataset.theme === name ? "var(--accent)" : "transparent";
    b.style.color = b.dataset.theme === name ? "#fff" : "var(--text)";
  });
  // Keep the custom color-picker swatches in the settings panel in sync
  // with whichever theme is now active, so switching to Light mode shows
  // that theme's real colors instead of stale values left over from Dark.
  ["accent","accent2","bg","panel","text"].forEach(k=>{
    const el = document.getElementById(`settings-color-${k}`);
    if (el && vars[k]) el.value = vars[k];
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
  applyTheme(s.theme || "dark");
  applyBlobsEnabled(s.blobs_enabled !== false);
  _bgState.cfg = s.bg_image || _bgState.cfg;
  if (_bgState.cfg.enabled && _bgState.cfg.path){
    const r = await api("get_background_image_data");
    _bgState.dataUrl = r && r.ok ? r.data_url : null;
  } else {
    _bgState.dataUrl = null;
  }
  applyBgImage(_bgState.cfg, _bgState.dataUrl);
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
  Object.entries(s.colors || {}).forEach(([k,v])=>{
    const el = document.getElementById(`settings-color-${k}`);
    if (el) el.value = v;
  });
  applyColors(s.colors);
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
  ["accent","accent2","bg","panel","text","blob_center","blob_a","blob_b","blob_cursor"].forEach(k=>{
    const el = document.getElementById(`settings-color-${k}`);
    if (el) colors[k] = el.value;
  });
  const blobs_enabled = document.getElementById("settings-blobs-enabled").checked;
  const r = await api("save_settings", {provider, model, theme: _activeTheme, colors, bg_image, blobs_enabled});
  const status = document.getElementById("settings-status");
  if (r.ok){
    applyTheme(r.settings.theme || "dark");
    applyColors(r.settings.colors);
    applyBlobsEnabled(r.settings.blobs_enabled !== false);
    _bgState.cfg = r.settings.bg_image;
    applyBgImage(_bgState.cfg, _bgState.dataUrl);
    if (status) status.textContent = "Saved — will be remembered next launch.";
  } else if (status) {
    status.textContent = `Error: ${r.error}`;
  }
}

function populateProjects(list){
  const sel = document.getElementById("project-select");
  if (!sel) return;
  sel.innerHTML = "";
  if (!list.length){
    sel.innerHTML = `<option>Create first project...</option>`;
    return;
  }
  list.forEach(p=>{
    const o = document.createElement("option"); o.value = p; o.textContent = p; sel.appendChild(o);
  });
}

function renderFileList(info){
  const box = document.getElementById("file-list");
  if (!box || !info) return;
  let out = `📁 ${info.root}\n`;
  (info.files||[]).forEach(f=>{ out += `  ${f.dir ? "📁" : "📄"} ${f.name}\n`; });
  box.textContent = out;
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
    let title = chat.title || "Untitled chat";
    if (title.length > 30) title = title.slice(0,29) + "…";
    card.innerHTML = `
      <div style="min-width:0;flex:1;">
        <div class="history-title">${escapeHtml(title)}</div>
        <div class="history-ts">${(chat.updated_at||"").replace("T","  ")}</div>
      </div>
      <div class="history-actions">
        <button class="mini-btn open">Open</button>
        <button class="mini-btn del">🗑</button>
      </div>`;
    card.querySelector(".open").onclick = async ()=>{
      const r = await api("load_chat", chat.id);
      if (!r.ok){ showAlert(r.error, "Error"); return; }
      clearChat();
      (r.display||[]).forEach(([tag,text])=>appendRow(tag, text));
      applyKbState(r.kb_state);
      switchTab("Chat");
      refreshHistory();
    };
    card.querySelector(".del").onclick = async ()=>{
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
function showToolPane(name){
  const box = document.getElementById("tool-content");
  const builders = {
    "Voice": buildVoicePane, "Log": buildLogPane, "Model": buildModelPane, "Parameters": buildParamsPane,
    "System Core": buildSysCorePane, "Knowledge": buildKnowledgePane,
    "Skills": buildSkillsPane, "Tools": buildToolsPane, "Flows": buildFlowsPane, "Schedule": buildSchedulePane, "MCP": buildMcpPane,
    "Permissions": buildPermissionsPane,
  };
  box.innerHTML = "";
  box.style.display = "flex"; box.style.flexDirection = "column"; box.style.height = "100%";
  box.style.padding = "";  // reset any pane-specific override (e.g. Flows sets 0) before rebuilding
  (builders[name] || (()=>{}))(box);
}

function buildLogPane(box){
  box.innerHTML = `
    <div class="hdr-row"><div class="section-label">ACTIVITY LOG</div>
      <button class="ghost-btn" id="log-clear" style="height:22px;font-size:10px;">Clear</button></div>
    <textarea class="code-area" id="log-box" readonly style="color:var(--tool-text);background:var(--tool-bg);margin-top:6px;"></textarea>`;
  document.getElementById("log-clear").onclick = ()=>{ document.getElementById("log-box").value=""; };
}

function buildVoicePane(box){
  box.innerHTML = `
    <div class="hdr-row"><div class="section-label">VOICE CONTROL — GEMINI LIVE</div></div>
    <div id="voice-status-row" style="display:flex;align-items:center;gap:8px;margin-top:6px;">
      <div id="voice-dot" style="width:10px;height:10px;border-radius:50%;background:var(--subtext);"></div>
      <div id="voice-status-text" style="font-size:11px;color:var(--subtext);">Checking...</div>
    </div>
    <div style="display:flex;gap:8px;margin-top:10px;">
      <button class="btn" id="voice-start-btn" style="background:var(--accent);color:#fff;flex:1;">🎤 Start talking</button>
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
    startBtn.textContent = voiceState.running ? "⏹ Stop" : "🎤 Start talking";
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
    const col = chatCol();
    const row = document.createElement("div");
    row.className = isUser ? "row user" : "row midum";
    row.innerHTML = isUser
      ? `<div class="row-label">You (voice)</div><div class="bubble user"></div>`
      : `<div class="row-label">Midum (voice)</div><div class="bubble midum"></div>`;
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
      <button class="ghost-btn" id="model-refresh" style="height:20px;font-size:10px;">⟳</button></div>
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
    <div style="font-size:10px;color:var(--muted);">Local (Ollama) runs fully offline and is the default on every launch. Switching providers here only affects this running session.</div>
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
      if (["gemini","ocr","uia"].includes(key)) val = val ? "✅ System Connected" : "⚠️ Unconnected";
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
        <button class="ghost-btn" id="pv-prev" style="height:28px;">◀ Prev</button>
        <div id="pv-page-label" style="font-size:12px;color:var(--subtext);">Page 1</div>
        <button class="ghost-btn" id="pv-next" style="height:28px;">Next ▶</button>
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

    modal.querySelector("#pv-prev").onclick = ()=>{ if (pageIndex>0){ pageIndex--; renderPage(); } };
    modal.querySelector("#pv-next").onclick = ()=>{ if (pageIndex<pageCount-1){ pageIndex++; renderPage(); } };
    modal.querySelector("#pv-close").onclick = ()=>{ overlay.remove(); resolve(); };
    overlay.addEventListener("click", (e)=>{ if (e.target === overlay){ overlay.remove(); resolve(); } });

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
        <button class="ghost-btn" id="pht-prev" style="height:28px;">◀ Prev</button>
        <div id="pht-page-label" style="font-size:12px;color:var(--subtext);">Page 1</div>
        <button class="ghost-btn" id="pht-next" style="height:28px;">Next ▶</button>
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
          <button class="pht-remove-tag" data-lid="${h.line_id}" style="background:none;border:none;color:var(--red);cursor:pointer;">✕</button>
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

    modal.querySelector("#pht-prev").onclick = ()=>{ if (pageIndex>0){ pageIndex--; renderPage(); } };
    modal.querySelector("#pht-next").onclick = ()=>{ if (pageIndex<pageCount-1){ pageIndex++; renderPage(); } };
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
    modal.querySelector("#pht-close").onclick = ()=>{ overlay.remove(); resolve(false); };
    modal.querySelector("#pht-save").onclick = async ()=>{
      const list = Object.values(headings);
      const res = await api("save_pdf_headings", name, list);
      if (!res.ok){ await showAlert(res.message, "Error"); return; }
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
      <button class="ghost-btn" id="pdf-tag" style="margin-right:6px;">🏷️ Tag Headings</button>
      <button class="ghost-btn" id="pdf-add">+ Add PDF Source</button>
    </div>
    <div class="hdr-row" style="margin-top:6px;">
      <button class="ghost-btn" id="pdf-warm" style="flex:1;" title="Extracts and caches text for every PDF source so Explain Mode parts load instantly. This can be slow for large/many PDFs, so it only runs when you click it.">⚡ Load Sources</button>
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
    pdfWarmBtn.textContent = "⚡ Loading sources…";
    const r = await api("warm_pdf_sources");
    if (!r || !r.ok){
      pdfWarmBtn.disabled = false;
      pdfWarmBtn.textContent = "⚡ Load Sources";
    }
    // On success the button stays disabled/"Loading…" until the backend
    // pushes a pdf_cache_warmed event (see onPdfCacheWarmed below), since
    // the extraction runs on a background thread and may take a while.
  };
  window.onPdfCacheWarmed = ()=>{
    if (!pdfWarmBtn.isConnected) return;
    pdfWarmBtn.disabled = false;
    pdfWarmBtn.textContent = "✓ Sources Loaded";
    setTimeout(()=>{ if (pdfWarmBtn.isConnected) pdfWarmBtn.textContent = "⚡ Load Sources"; }, 2500);
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
      <button class="btn" id="tool-run" style="background:var(--accent);color:#fff;">▶ Run</button>
    </div>
    <div class="hdr-row" style="margin-top:6px;">
      <select id="flow-select" style="flex:1;margin-right:6px;"></select>
      <button class="btn" id="flow-run" style="background:var(--accent2);color:#fff;">▶ Run Flow</button>
    </div>
    <div class="tools-args" id="tool-args"></div>
    <textarea class="code-area" id="tool-output" readonly style="color:var(--tool-text);background:var(--tool-bg);"></textarea>`;
  const sel = document.getElementById("tool-select");
  const flowSel = document.getElementById("flow-select");
  const argsBox = document.getElementById("tool-args");
  enhanceSelect(sel);
  enhanceSelect(flowSel);
  let schemas = [];
  function buildArgs(name){
    const schema = schemas.find(s=>s.name===name);
    argsBox.innerHTML = "";
    if (!schema) return;
    const props = schema.properties || {};
    const required = schema.required || [];
    if (!Object.keys(props).length){ argsBox.innerHTML = `<div style="font-size:11px;color:var(--subtext);">No arguments required.</div>`; return; }
    Object.entries(props).forEach(([argName, details])=>{
      const row = document.createElement("div"); row.className = "arg-row";
      const req = required.includes(argName) ? " *" : "";
      let inputHtml;
      if (details.enum){
        inputHtml = `<select data-arg="${argName}">${details.enum.map(e=>`<option>${e}</option>`).join("")}</select>`;
      } else {
        inputHtml = `<input data-arg="${argName}" placeholder="${details.description||""}" type="${details.type==='integer'||details.type==='number'?'number':'text'}"/>`;
      }
      row.innerHTML = `<label>${argName}${req}</label>${inputHtml}`;
      argsBox.appendChild(row);
      const enumSel = row.querySelector("select[data-arg]");
      if (enumSel) enhanceSelect(enumSel);
    });
  }
  (async ()=>{
    schemas = await api("list_tool_schemas");
    sel.innerHTML = schemas.map(s=>`<option>${s.name}</option>`).join("");
    if (schemas.length) buildArgs(schemas[0].name);
  })();
  (async ()=>{
    const flowSchemas = await api("list_flow_schemas");
    flowSel.innerHTML = flowSchemas.length
      ? flowSchemas.map(s=>`<option value="${escapeHtml(s.name)}" title="${escapeHtml(s.description||'')}">${escapeHtml(s.name)}${s.promoted ? " [PROMOTED]" : ""}</option>`).join("")
      : `<option value="">No saved flows</option>`;
  })();
  sel.onchange = ()=>buildArgs(sel.value);
  document.getElementById("tool-run").onclick = async ()=>{
    const args = {};
    argsBox.querySelectorAll("[data-arg]").forEach(el=>{ if (el.value) args[el.dataset.arg] = el.value; });
    const r = await api("run_tool", sel.value, args);
    document.getElementById("tool-output").value = r.output;
  };
  document.getElementById("flow-run").onclick = async ()=>{
    if (!flowSel.value || flowSel.value === "No saved flows") return;
    const r = await api("run_flow", flowSel.value);
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
      <button class="ghost-btn" id="sched-refresh" title="Refresh">↻</button>
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
            <div style="font-weight:600;font-size:12px;">🔗 ${escapeHtml(s.flow_name)}</div>
            <div style="font-size:11px;color:var(--subtext);">${escapeHtml(s.description || s.kind)}</div>
          </div>
          <div style="display:flex;gap:6px;align-items:center;">
            <label style="font-size:10px;display:flex;gap:4px;align-items:center;">
              <input type="checkbox" class="sched-enabled" ${s.enabled ? "checked" : ""}/> enabled
            </label>
            <button class="mini-btn del" data-act="delete">🗑</button>
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
      { type: "start", label: "Start", icon: "▶", inputs: 0, outputs: 1 },
      { type: "end",   label: "End",   icon: "⏹", inputs: 1, outputs: 0 },
    ],
  },
  {
    group: "Logic",
    nodes: [
      { type: "logic::if", label: "If", icon: "🔀", inputs: 2, outputs: 2, isLogic: true,
        pinLabels: { in: ["Sequence", "Value"], out: ["True", "False"] } },
      { type: "logic::loop", label: "Loop (For Each)", icon: "🔁", inputs: 2, outputs: 3, isLogic: true,
        pinLabels: { in: ["Sequence", "Iterable"], out: ["Body", "Item", "After"] } },
    ],
  },
  {
    group: "Variables",
    nodes: [
      { type: "variable", label: "Variable", icon: "🧩", inputs: 1, outputs: 1, isVariable: true },
    ],
  },
  {
    group: "AI",
    nodes: [
      { type: "ai::prompt", label: "Prompt AI", icon: "🤖", inputs: 2, outputs: 2, isAI: true,
        pinLabels: { in: ["Sequence", "Context"], out: ["Sequence", "Result"] } },
      { type: "ai::summarize", label: "Ask AI to Summarize", icon: "📝", inputs: 2, outputs: 2, isAI: true,
        pinLabels: { in: ["Sequence", "Text"], out: ["Sequence", "Summary"] } },
      { type: "ai::choose", label: "Ask AI to Choose", icon: "🎯", inputs: 2, outputs: 2, isAI: true,
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
         + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${def.icon}</span><span class="flow-node-label">Variable</span></div>`
         + `<div class="flow-node-params">`
         + `<div class="flow-param-row"><span class="flow-param-label">name</span><input class="flow-param-input" type="text" df-name placeholder="my_variable"/></div>`
         + `<div class="flow-param-row"><span class="flow-param-label">value</span><input class="flow-param-input" type="text" df-value placeholder="default (used if nothing wired in)"/></div>`
         + `</div>`
         + `<div class="flow-node-pin-hint"><span>in: value</span><span>out: value</span></div>`
         + `</div>`;
  }
  if (def.isLogic && def.type === "logic::if"){
    return `<div class="flow-node-tool flow-node-logic">`
         + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${def.icon}</span><span class="flow-node-label">If</span></div>`
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
         + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${def.icon}</span><span class="flow-node-label">Loop (For Each)</span></div>`
         + `<div class="flow-node-params">`
         + `<div class="flow-param-row"><span class="flow-param-label">item</span><input class="flow-param-input" type="text" df-item_var placeholder="item (label only)"/></div>`
         + `</div>`
         + `<div class="flow-node-pin-hint"><span>in: seq, iterable</span><span>out: body, item, after</span></div>`
         + `</div>`;
  }
  if (def.isAI && def.type === "ai::prompt"){
    return `<div class="flow-node-tool flow-node-ai">`
         + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${def.icon}</span><span class="flow-node-label">Prompt AI</span></div>`
         + `<div class="flow-node-params">`
         + `<div class="flow-param-row"><span class="flow-param-label">prompt</span><input class="flow-param-input" type="text" df-prompt placeholder="Instruction to send to the AI"/></div>`
         + `<div class="flow-param-row"><span class="flow-param-label">context</span><input class="flow-param-input" type="text" df-context placeholder="fallback if Context pin unwired"/></div>`
         + `</div>`
         + `<div class="flow-node-pin-hint"><span>in: seq, context</span><span class="flow-object-out">out: seq, ⬤ result</span></div>`
         + `</div>`;
  }
  if (def.isAI && def.type === "ai::summarize"){
    return `<div class="flow-node-tool flow-node-ai">`
         + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${def.icon}</span><span class="flow-node-label">Ask AI to Summarize</span></div>`
         + `<div class="flow-node-params">`
         + `<div class="flow-param-row"><span class="flow-param-label">text</span><input class="flow-param-input" type="text" df-text placeholder="fallback if Text pin unwired"/></div>`
         + `<div class="flow-param-row"><span class="flow-param-label">length</span>`
         + `<select class="flow-param-input" df-length>`
         + `<option value="short">short</option><option value="medium" selected>medium</option><option value="long">long</option>`
         + `</select></div>`
         + `</div>`
         + `<div class="flow-node-pin-hint"><span>in: seq, text</span><span class="flow-object-out">out: seq, ⬤ summary</span></div>`
         + `</div>`;
  }
  if (def.isAI && def.type === "ai::choose"){
    return `<div class="flow-node-tool flow-node-ai">`
         + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${def.icon}</span><span class="flow-node-label">Ask AI to Choose</span></div>`
         + `<div class="flow-node-params">`
         + `<div class="flow-param-row"><span class="flow-param-label">question</span><input class="flow-param-input" type="text" df-question placeholder="What should the AI decide?"/></div>`
         + `<div class="flow-param-row"><span class="flow-param-label">options</span><input class="flow-param-input" type="text" df-options placeholder="fallback: comma,separated,options"/></div>`
         + `</div>`
         + `<div class="flow-node-pin-hint"><span>in: seq, options</span><span class="flow-object-out">out: seq, ⬤ choice</span></div>`
         + `</div>`;
  }
  if (!def.params){
    return `<div class="flow-node flow-node-${def.type}">`
         + `<div class="flow-node-icon">${def.icon}</div>`
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
    ? `<div class="flow-node-pin-hint"><span>in: seq${def.params.length?', params':''}</span><span class="flow-object-out">out: seq, ⬤ object</span></div>`
    : `<div class="flow-node-pin-hint"><span>in: seq${def.params.length?', params':''}</span><span>out: seq</span></div>`;
  return `<div class="flow-node-tool">`
       + `<div class="flow-node-tool-hdr"><span class="flow-node-icon">${def.icon}</span><span class="flow-node-label">${escapeHtml(def.label)}</span>`
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
            <button class="ghost-btn" id="flow-promote" title="Promote: give this flow its own tool schema so the model can call it directly by name, without discovery" style="height:24px;font-size:10px;" disabled>⭐ Promote</button>
            <button class="ghost-btn" id="flow-delete" style="height:24px;font-size:10px;color:var(--red);" disabled>🗑 Delete</button>
            <button class="ghost-btn" id="flow-zoom-out" style="height:24px;width:28px;padding:0;">−</button>
            <button class="ghost-btn" id="flow-zoom-reset" style="height:24px;font-size:10px;">Reset</button>
            <button class="ghost-btn" id="flow-zoom-in" style="height:24px;width:28px;padding:0;">+</button>
            <button class="ghost-btn" id="flow-delete-connection" style="height:24px;font-size:10px;color:var(--red);" disabled>✂ Break Connection</button>
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
      item.innerHTML = `<span class="flow-drawer-item-icon">${def.icon}</span><span>${escapeHtml(def.label)}</span>`;
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
    }

    // Reflects the currently-loaded saved flow's promoted state on the
    // Promote button -- mirrors the Promote/Demote controls in the MCP
    // tab's Tools pane. Disabled entirely when no saved flow is loaded
    // (an unsaved/new flow has no name to promote yet).
    async function refreshPromoteButton(name){
      if (!name){
        promoteBtnFlow.disabled = true;
        promoteBtnFlow.classList.remove("open");
        promoteBtnFlow.textContent = "⭐ Promote";
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
      promoteBtnFlow.textContent = promoted ? "★ Promoted" : "⭐ Promote";
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
        <button class="ghost-btn" id="mcp-refresh" style="height:24px;">⟳</button>
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
  document.getElementById("mcp-banner").textContent = sdk_available ? "" : "⚠️ 'mcp' package not installed — run: pip install mcp";
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
    <input type="text" id="perm-search" placeholder="Search tools..." class="perm-search" style="margin-top:6px;"/>
    <div id="perm-list" style="flex:1;overflow-y:auto;margin-top:8px;"></div>
  `;
  const listEl = document.getElementById("perm-list");
  let targets = await api("list_permission_targets");
  let overrides = await api("get_permissions");

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

// ── Boot -----------------------------------------------------------------
_blobLayerCtl = initBlobLayer(); // pure DOM/CSS, doesn't need the pywebview bridge -- start immediately
window.addEventListener("pywebviewready", async ()=>{
  buildTabbar();
  buildSidebar();
  applyLayout();

  document.getElementById("sidebar-toggle").onclick = toggleSidebar;
  document.getElementById("send-btn").onclick = sendMessage;
  document.getElementById("msg-input").addEventListener("keydown", e=>{ if (e.key==="Enter" && !e.shiftKey){ e.preventDefault(); sendMessage(); } });
  document.getElementById("msg-input").addEventListener("input", autosizeMsgInput);
  document.getElementById("abort-btn").onclick = ()=>api("abort");
  document.getElementById("copy-chat-btn").onclick = copyFullConversation;
  initKbOnlyControls();

  // Apply the remembered theme colors immediately, before the heavier
  // startup() call resolves, so the UI doesn't flash default colors first.
  try {
    const s = await api("get_settings");
    applyTheme(s.theme || "dark");
    applyColors(s.colors);
    applyBlobsEnabled(s.blobs_enabled !== false);
    _bgState.cfg = s.bg_image || _bgState.cfg;
    if (_bgState.cfg.enabled && _bgState.cfg.path){
      const r = await api("get_background_image_data");
      _bgState.dataUrl = r && r.ok ? r.data_url : null;
    }
    applyBgImage(_bgState.cfg, _bgState.dataUrl);
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
    )
    api.window = window
    window.events.closing += api._on_closing
    # gui="qt" renders through QtWebEngine (PySide6 or PyQt5), which
    # bundles its own Chromium build. Unlike gui="edgechromium" this has no
    # dependency on the Microsoft Edge WebView2 Runtime being installed on
    # the machine, which avoids the WinForms/legacy-Trident fallback and
    # the AccessibleObject.Bounds spam that comes with it. On Linux this
    # also uses QtWebEngine (Chromium) rather than WebKitGTK.
    webview.start(gui="qt", debug=False)


if __name__ == "__main__":
    main()
