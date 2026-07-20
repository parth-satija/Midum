# --- Midum GUI v2 — Chromium (pywebview/WebView2) UI, animated rounded panes ---
import os
import sys

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
import base64
import traceback
import subprocess
import re
import uuid

import webview  # pywebview — renders through the OS Chromium engine (WebView2 on
                 # Windows, WebKitGTK on Linux, WKWebView on macOS). Replaces the
                 # previous customtkinter/Tkinter shell entirely.

from gui.chat_store import ChatStore, MidumSession
from gui.dispatch import _dispatch_midum_tool
from flows import classify_tool_kind
import tools.user_prompt_tools as _user_prompt_tools

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

        self._session      = MidumSession()
        self._thinking     = False
        self._log_queue    = queue.Queue()

        self._chat_store       = ChatStore(CHATS_DIR)
        self._current_chat_id  = uuid.uuid4().hex
        self._chat_title       = None
        self._display_log      = []
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

        self._pending_ask = {}  # ask_id -> threading.Event / result box

    # ── Low-level plumbing ──────────────────────────────────────────────
    def _push_event(self, kind: str, payload: dict):
        """Push an async event to the frontend via window.evaluate_js."""
        if not self.window:
            return
        try:
            data = json.dumps({"kind": kind, "payload": payload})
            self.window.evaluate_js(f"window.__midumEvent && window.__midumEvent({data})")
        except Exception:
            pass

    def _on_log_line(self, line: str):
        if line.startswith(_SAY_TAG):
            self._push_event("say", {"text": line[len(_SAY_TAG):]})
            return
        self._push_event("log", {"text": line})
        if _is_tool_line(line):
            self._push_event("tool_line", {"text": line.strip()})

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
        "accent": "#f97316", "accent2": "#7c3aed",
        "bg": "#02010a", "panel": "#0a0916", "text": "#e2e8f0",
    }
    _DEFAULT_THEME = "dark"
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
        return {"ok": True, "display": self._display_log}

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

    def _persist_current_chat(self):
        if not self._display_log:
            return
        try:
            title = self._chat_title or "Untitled chat"
            self._chat_store.save(self._current_chat_id, title, self._session.snapshot(), list(self._display_log))
        except Exception as e:
            self._push_event("log", {"text": f"⚠ Failed to save chat history: {e}\n"})

    # ── Send / receive ──────────────────────────────────────────────────
    def send_message(self, user_input: str):
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
        self._push_event("status", {"text": "Executing turns...", "level": "busy"})

        threading.Thread(target=self._run_turn, args=(list(self._session.snapshot()),), daemon=True).start()
        return {"ok": True}

    def _run_turn(self, history_snapshot: list):
        try:
            midum._abort_event.clear()
            reply, tool_outputs = midum.process_chat_turn(
                history_snapshot,
                force_provider=self._selected_provider,
                force_model=self._selected_model or None,
            )
            with self._session._lock:
                self._session.history = history_snapshot
                self._session.turn_counter += 1

            cleaned_reply, visuals = self._extract_and_strip_visuals(reply, tool_outputs)
            if cleaned_reply:
                self._display_log.append(("midum", cleaned_reply))
                self._push_event("reply", {"text": cleaned_reply})
            for lang, body in visuals:
                block = f"```{lang}\n{body}\n```"
                self._display_log.append(("midum", block))
                self._push_event("reply", {"text": block})
            self._persist_current_chat()

            threading.Thread(target=midum.python_trigger_memory_update, args=(tool_outputs, reply), daemon=True).start()

            self._push_event("status", {"text": "Ready", "level": "ok"})
        except Exception as e:
            self._push_event("error_line", {"text": f"[Engine error: {e}]"})
            self._push_event("status", {"text": "Error", "level": "err"})
            self._persist_current_chat()
        finally:
            self._thinking = False
            self._push_event("done", {})
            # The window was closed while this reply was still being
            # generated (see _on_closing) -- the close was held off
            # specifically so this reply wouldn't be lost. Now that it's
            # persisted, actually close.
            if self._close_requested:
                self._close_requested = False
                self._persist_current_chat()
                if self.window:
                    self._destroy_window_safe()

    _VISUAL_FENCE_LANGS = ("image_data_json", "flowchart_json")
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
        close once the reply is actually saved."""
        self._persist_current_chat()
        if self._thinking:
            self._close_requested = True
            self._push_event("log", {"text": "⏳ Finishing the current response before closing...\n"})
            return False
        return None


# =============================================================================
# FRONTEND — single-file HTML/CSS/JS, rendered by the OS Chromium engine.
# =============================================================================
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Midum Control Center</title>
<style>
:root{
  --bg:#02010a; --panel:#0a0916; --surface:#100f1f; --surface2:#171629;
  --border:#1c1a30; --border2:#2b2847;
  --accent:#f97316; --accent-dim:#c2410c; --accent-faint:#3a1f0f;
  --accent2:#7c3aed; --green:#10b981; --red:#ef4444; --yellow:#f59e0b;
  --text:#e2e8f0; --subtext:#64748b; --muted:#38364f;
  --user-msg:#2a1f12; --midum-msg:#0f1120;
  --tool-bg:#040309; --tool-text:#fbbf24;
  --gap:14px; --radius:24px; --ease:cubic-bezier(.65,0,.35,1);
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

.pane{
  position:absolute;inset:calc(var(--gap)/2);border-radius:var(--radius);
  background:var(--panel);border:1px solid var(--border2);
  display:flex;flex-direction:column;overflow:hidden;
  z-index:1;
}
/* "Liquid glass" look, only when a background image is active. This is a
   flat translucent tint (color-mix, resolved once at paint time like any
   normal background-color) with NO backdrop-filter: backdrop-filter has
   to continuously re-blur whatever's behind it, and this UI has several
   always-running animations behind the panes, so it was never actually
   static in practice -- that mismatch between the comment's intent and
   the compositor's real behavior was the root cause of the flashing. */
html.has-bg-image .pane{
  background:color-mix(in srgb, var(--panel) 62%, transparent);
  border:1px solid color-mix(in srgb, var(--text) 12%, transparent);
  box-shadow:inset 0 1px 0 color-mix(in srgb, var(--text) 8%, transparent), 0 8px 30px rgba(0,0,0,.35);
}
html.has-bg-image #topbar{
  background:color-mix(in srgb, var(--panel) 62%, transparent);
  border:1px solid color-mix(in srgb, var(--text) 12%, transparent);
  box-shadow:inset 0 1px 0 color-mix(in srgb, var(--text) 8%, transparent), 0 8px 30px rgba(0,0,0,.35);
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
  border-radius:26px;display:flex;align-items:center;padding:6px 6px 6px 16px;gap:8px;
}
#msg-input{flex:1;background:transparent;border:none;outline:none;color:var(--text);font-size:14px;height:34px;}
#msg-input::placeholder{color:var(--muted);}
#send-btn{
  width:36px;height:36px;border-radius:50%;border:none;background:var(--accent);color:var(--text);
  font-size:16px;font-weight:700;display:flex;align-items:center;justify-content:center;
  transition:background .15s;
}
#send-btn:hover{background:var(--accent-dim);}
#send-hint{text-align:center;font-size:9px;color:var(--muted);padding-bottom:6px;}

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
.row{display:flex;flex-direction:column;padding:6px 0;}
.row.user{align-items:flex-end;}
.row.midum{align-items:flex-start;}
.row-label{font-size:11px;font-weight:700;color:var(--subtext);margin-bottom:4px;}
.row.midum .row-label{color:var(--accent);}
.bubble{border-radius:18px;padding:10px 16px;font-size:14px;line-height:1.5;max-width:78%;white-space:pre-wrap;word-wrap:break-word;}
.bubble.user{background:var(--user-msg);}
.bubble.midum{background:transparent;max-width:100%;}
.row.system, .row.error{align-items:center;text-align:center;}
.row.system .bubble{background:transparent;color:var(--subtext);font-size:12px;}
.row.error .bubble{background:transparent;color:var(--red);font-size:12px;}
.row.tool{align-items:flex-start;}
.tool-line{display:flex;gap:6px;font-size:10px;color:var(--subtext);align-items:center;}
.tool-line .gear{color:var(--muted);}
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
  overflow-x:auto;font-family:Consolas,"Cascadia Code",monospace;font-size:12px;}
code.inline-code{background:var(--surface2);color:var(--tool-text);border-radius:4px;padding:1px 5px;
  font-family:Consolas,"Cascadia Code",monospace;font-size:12.5px;}
.bubble h1,.bubble h2,.bubble h3{margin:.4em 0;}
.bubble a{color:var(--accent);}
.bubble hr{border:none;border-top:1px solid var(--border2);margin:10px 0;}
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
  height:14px !important;width:14px !important;
}
#flow-canvas .drawflow-node .input:hover, #flow-canvas .drawflow-node .output:hover{
  background:var(--accent) !important;border-color:var(--accent) !important;
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
        <div id="chat-scroll"><div id="chat-col"></div></div>
        <div id="input-row">
          <div style="width:100%;max-width:760px;">
            <div id="input-box">
              <input id="msg-input" placeholder="Message Midum..." />
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
  ["Chat","💬"], ["Log","📜"], ["Model","🧬"], ["Parameters","⚙"],
  ["System Core","🧠"], ["Knowledge","📚"], ["Skills","🛠"], ["Tools","🔧"], ["Flows","🔗"], ["MCP","🔌"], ["Permissions","🔐"]
];

let state = {
  activeTab: "Chat",
  sidebarOpen: false,
  thinking: false,
};

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
  t = t.replace(/```([\w_]*)\n([\s\S]*?)```/g, (m,lang,body)=>`<pre class="code-block">${body}</pre>`);
  t = t.replace(/`([^`]+)`/g, (m,c)=>`<code class="inline-code">${c}</code>`);
  t = t.replace(/\*\*\*(.+?)\*\*\*/g, "<b><i>$1</i></b>");
  t = t.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  t = t.replace(/(^|[^*])\*([^*]+)\*/g, "$1<i>$2</i>");
  t = t.replace(/~~(.+?)~~/g, "<s>$1</s>");
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
  t = t.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
  return t.replace(/\n/g, "<br>");
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

const FLOWCHART_FENCE_RE = /```(flowchart_json|image_data_json)\n([\s\S]*?)```/g;
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

    out += renderedBlock || `<pre class="code-block">${escapeHtml(body)}</pre>`;
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
    row.innerHTML = `<div class="row-label">You</div><div class="bubble user">${escapeHtml(text)}</div>`;
  } else if (tag === "midum"){
    row.className = "row midum";
    row.innerHTML = `<div class="row-label">Midum</div><div class="bubble midum">${renderMidumContent(text)}</div>`;
    setActiveToolDot(null);
    col.appendChild(row);
    animateWords(row.querySelector(".bubble"));
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
    row.className = "row tool";
    row.innerHTML = `<div class="tool-line"><span class="tool-dot"></span><span class="gear">⚙</span><span>${escapeHtml(text)}</span></div>`;
    col.appendChild(row);
    setActiveToolDot(row.querySelector(".tool-dot"));
    scrollToBottom();
    return;
  }
  col.appendChild(row);
  scrollToBottom();
}

function scrollToBottom(){
  const sc = document.getElementById("chat-scroll");
  requestAnimationFrame(()=>{ sc.scrollTop = sc.scrollHeight; });
}

function clearChat(){ chatCol().innerHTML = ""; }

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
async function sendMessage(){
  if (state.thinking) return;
  const input = document.getElementById("msg-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  appendRow("user", text);
  setActiveToolDot(null);
  setStatus("Executing turns...", "busy");
  state.thinking = true;
  await api("send_message", text);
}

function setStatus(text, level){
  document.getElementById("status-label").textContent = text;
  const dot = document.getElementById("status-dot");
  dot.style.background = level === "ok" ? "var(--green)" : level === "err" ? "var(--red)" :
                         level === "busy" ? "var(--yellow)" : "var(--subtext)";
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
  else if (kind === "log"){ appendLog(payload.text); }
  else if (kind === "done"){ state.thinking = false; setActiveToolDot(null); }
  else if (kind === "projects"){ populateProjects(payload.projects); }
  else if (kind === "ask"){ appendAsk(payload.id, payload.kind, payload.payload); }
  else if (kind === "mcp_changed"){ if (state.activeTab === "MCP") refreshMcpList(); }
  else if (kind === "tool_result"){ const box=document.getElementById("tool-output"); if(box) box.value = payload.output; }
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
    await api("new_session"); clearChat(); refreshHistory();
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
    applyColors(DEFAULT_COLORS);
    Object.entries(DEFAULT_COLORS).forEach(([k,v])=>{
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

const DEFAULT_COLORS = {accent:"#f97316", accent2:"#7c3aed", bg:"#02010a", panel:"#0a0916", text:"#e2e8f0"};

// Full palette per theme — covers every CSS var, not just the 5
// user-editable swatches, so Light mode actually looks light (panes,
// borders, bubbles, tool console, etc.) rather than just re-tinting a
// couple of accent colors on a black background.
const THEME_VARS = {
  dark: {
    bg:"#02010a", panel:"#0a0916", surface:"#100f1f", surface2:"#171629",
    border:"#1c1a30", border2:"#2b2847",
    accent:"#f97316", "accent-dim":"#c2410c", "accent-faint":"#3a1f0f", accent2:"#7c3aed",
    text:"#e2e8f0", subtext:"#64748b", muted:"#38364f",
    "user-msg":"#2a1f12", "midum-msg":"#0f1120",
    "tool-bg":"#040309", "tool-text":"#fbbf24",
  },
  light: {
    bg:"#f4f3fb", panel:"#ffffff", surface:"#f0eef9", surface2:"#e6e3f5",
    border:"#ddd9ee", border2:"#cfc9e6",
    accent:"#f97316", "accent-dim":"#c2410c", "accent-faint":"#ffe4cc", accent2:"#7c3aed",
    text:"#1c1a2e", subtext:"#5b5876", muted:"#b8b3d6",
    "user-msg":"#ffe4cc", "midum-msg":"#f0eef9",
    "tool-bg":"#1c1a2e", "tool-text":"#f59e0b",
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
}

function applyColors(colors){
  if (!colors) return;
  const root = document.documentElement.style;
  if (colors.accent) root.setProperty("--accent", colors.accent);
  if (colors.accent2) root.setProperty("--accent2", colors.accent2);
  if (colors.bg) root.setProperty("--bg", colors.bg);
  if (colors.panel) root.setProperty("--panel", colors.panel);
  if (colors.text) root.setProperty("--text", colors.text);
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
  ["accent","accent2","bg","panel","text"].forEach(k=>{
    const el = document.getElementById(`settings-color-${k}`);
    if (el) colors[k] = el.value;
  });
  const r = await api("save_settings", {provider, model, theme: _activeTheme, colors, bg_image});
  const status = document.getElementById("settings-status");
  if (r.ok){
    applyTheme(r.settings.theme || "dark");
    applyColors(r.settings.colors);
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
      switchTab("Chat");
      refreshHistory();
    };
    card.querySelector(".del").onclick = async ()=>{
      const ok = await showConfirm(`Permanently delete "${title}"?`, "Delete Chat", {danger:true, okLabel:"Delete"});
      if (!ok) return;
      await api("delete_chat", chat.id);
      if (chat.current) clearChat();
      refreshHistory();
    };
    box.appendChild(card);
  });
}

// ── Tool pane content builders -------------------------------------------
function showToolPane(name){
  const box = document.getElementById("tool-content");
  const builders = {
    "Log": buildLogPane, "Model": buildModelPane, "Parameters": buildParamsPane,
    "System Core": buildSysCorePane, "Knowledge": buildKnowledgePane,
    "Skills": buildSkillsPane, "Tools": buildToolsPane, "Flows": buildFlowsPane, "MCP": buildMcpPane,
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

function buildKnowledgePane(box){
  box.innerHTML = `
    <div class="hdr-row">
      <select id="kb-select" style="flex:1;margin-right:6px;"></select>
      <button class="btn" id="kb-save" style="background:var(--accent);color:#fff;margin-right:6px;">Save</button>
      <button class="ghost-btn" id="kb-new">+ New</button>
    </div>
    <textarea class="code-area" id="kb-box" style="margin-top:6px;"></textarea>`;
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

// ── Boot -----------------------------------------------------------------
window.addEventListener("pywebviewready", async ()=>{
  buildTabbar();
  buildSidebar();
  applyLayout();

  document.getElementById("sidebar-toggle").onclick = toggleSidebar;
  document.getElementById("send-btn").onclick = sendMessage;
  document.getElementById("msg-input").addEventListener("keydown", e=>{ if (e.key==="Enter") sendMessage(); });
  document.getElementById("abort-btn").onclick = ()=>api("abort");

  // Apply the remembered theme colors immediately, before the heavier
  // startup() call resolves, so the UI doesn't flash default colors first.
  try {
    const s = await api("get_settings");
    applyTheme(s.theme || "dark");
    applyColors(s.colors);
    _bgState.cfg = s.bg_image || _bgState.cfg;
    if (_bgState.cfg.enabled && _bgState.cfg.path){
      const r = await api("get_background_image_data");
      _bgState.dataUrl = r && r.ok ? r.data_url : null;
    }
    applyBgImage(_bgState.cfg, _bgState.dataUrl);
  } catch (e) { /* pywebview bridge not ready yet on some platforms — fine */ }

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
        background_color="#02010a",
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
