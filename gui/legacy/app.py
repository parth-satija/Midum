# --- AUTO-SPLITTER: imports added by automated pass, please review ---
import os
import sys

# Allow running this file directly (`python gui/legacy/app.py`) by ensuring
# the package root (midum_pkg, two levels up from this gui/legacy/ folder)
# is on sys.path. Without this, Python only sees the gui/legacy/ folder
# itself on sys.path when the script is launched directly, so the top-level
# "gui" package can't be found.
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from gui.chat_store import ChatStore, MidumSession

# --- from gui.py, section 1 ---
"""
gui.py — Midum Desktop GUI
Entry point: python gui.py

Requires:
    pip install customtkinter

All AI logic lives in main.py — this file handles presentation only.
Run this instead of main.py. main.py's __main__ block is never executed
when imported, so there is no conflict.
"""

import os
import sys
import threading
import datetime
import queue
import json
import traceback
import subprocess
import re
import io
import base64
import uuid

import customtkinter as ctk
from tkinter import messagebox, filedialog
import tkinter as tk

# Pillow is used only for inline image thumbnails (generate_image tool
# output). The GUI works fine without it — thumbnails just fall back to a
# clickable file-path line.
try:
    from PIL import Image as _PILImage
    _PIL_AVAILABLE = True
except ImportError:
    _PILImage = None
    _PIL_AVAILABLE = False

# ── Import core Midum engine ─────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import main as midum

# Where persisted chat sessions live on disk (one JSON file per chat).
CHATS_DIR = os.path.join(midum.STORAGE_DIR, "chats")

# =============================================================================
# THEME & PALETTE (Refined Dark — Slate Carbon)
# =============================================================================
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

C = {
    # Structure
    "bg":         "#0a0c0f",   # True background — deepest layer
    "panel":      "#0f1318",   # Panel / column surface
    "surface":    "#161c24",   # Inset surface — inputs, boxes
    "surface2":   "#1c2333",   # Slightly raised surface for hover targets
    "border":     "#1f2937",   # Structural border, subtle
    "border2":    "#2d3748",   # Visible border for active / hovered states

    # Brand accent
    "accent":     "#3b82f6",   # Primary blue — buttons, links, active states
    "accent_dim": "#1d4ed8",   # Hover state for accent
    "accent_faint": "#1e3a5f", # Background tint for accent rows

    # Semantic colours
    "accent2":    "#7c3aed",   # Purple — Gemini / secondary indicator
    "green":      "#10b981",   # Ready / connected
    "red":        "#ef4444",   # Error / abort
    "yellow":     "#f59e0b",   # Processing / warning

    # Text hierarchy
    "text":       "#e2e8f0",   # Primary text — slightly softer than pure white
    "subtext":    "#64748b",   # Secondary / labels
    "muted":      "#374151",   # Disabled / placeholder

    # Chat bubbles
    "user_msg":   "#1a2744",   # User message bubble
    "midum_msg": "#0f1623",   # Midum response bubble

    # Console
    "tool_bg":    "#050810",   # Terminal / mono background
    "tool_text":  "#38bdf8",   # Monospaced output colour
}

FONT_BODY   = ("Segoe UI",      12)
FONT_BOLD   = ("Segoe UI",      12, "bold")
FONT_ITALIC = ("Segoe UI",      12, "italic")
FONT_SMALL  = ("Segoe UI",      11)
FONT_TINY   = ("Segoe UI",       9)
FONT_MONO   = ("Cascadia Code", 11)
FONT_LABEL  = ("Segoe UI",      10)
FONT_TITLE  = ("Segoe UI",      13, "bold")
FONT_HEAD   = ("Segoe UI",      15, "bold")
# =============================================================================
# LIVE CHAT MIRRORING — tool events & say() narration
# =============================================================================
# Sentinel prefix used to tag say()-tool output pushed through the stdout
# queue so _poll() can route it to a live chat bubble instead of only the
# sidebar log. Unlikely to collide with real engine output.
_SAY_TAG = "\x02MIDUM_SAY\x02"

# Any line whose stripped text starts with one of these emoji is treated as
# a "major system event" (memory writes, goal changes, skill/instruction/path
# updates, shutdowns, aborts, etc.) and mirrored into the main chat as a
# compact grey ticker line using a single shared tool icon.
_TOOL_LINE_EMOJI = (
    "🧠", "🎯", "📋", "📌", "📍", "📚", "🗑", "💾", "🔌", "🛑", "🚫", "✅",
    "⚠️", "🤖", "👁️", "🖥️", "🌐", "⌨️", "🐧", "📁", "🔍", "⚡",
)

# Substrings (checked case-insensitively) that also mark a line as a
# tool-call / execution-status event worth mirroring into the main chat.
_TOOL_LINE_KEYWORDS = (
    "-> executing:", "requested", "[uia]", "[cdp]", "[terminal]", "[type]",
    "[click]", "[wait]", "[blueprint]", "[snapshot", "[gemini", "[resolver]",
    "[legacy parser]", "[ocr", "[grid click]", "[open_url]",
    "[rule violation]", "[retry cap]", "[tool name error]",
    "[unknown tool]", "[internal error]", "[tool resolver]",
    "[max steps reached]", "[response aborted", "[path resolved",
)



# --- from gui.py, section 2 ---
class _StdoutRedirector:
    """Capture everything that would go to the terminal and route it to the GUI."""
    def __init__(self, callback):
        self._cb  = callback
        self._old = sys.stdout

    def write(self, text):
        if text.strip():
            self._cb(text)

    def flush(self):
        pass

    def restore(self):
        sys.stdout = self._old

# =============================================================================
# MIDUM SESSION STATE (shared between GUI and engine thread)
# =============================================================================

# --- from gui.py, section 3 ---
def _default_model_for_provider(provider_key: str) -> str:
    """Returns the model id currently configured in main.py for a provider."""
    return {
        "ollama":     midum.config.MODEL_NAME,
        "openrouter": midum.config.OPENROUTER_MODEL,
        "gemini_web": midum.config.GEMINI_WEB_MODEL or "(auto)",
        "gemini_api": midum.config.GEMINI_API_MODEL,
        "groq":       midum.config.GROQ_MODEL,
    }.get(provider_key, "")


def _known_models_for_provider(provider_key: str) -> list:
    """Best-effort list of model choices to seed the dropdown with."""
    if provider_key == "openrouter":
        return list(dict.fromkeys(midum.config.OPENROUTER_FALLBACK_MODELS))
    if provider_key == "groq":
        return list(dict.fromkeys(midum.config.GROQ_FALLBACK_MODELS))
    if provider_key == "gemini_api":
        return list(dict.fromkeys([midum.config.GEMINI_API_MODEL, "gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]))
    if provider_key == "gemini_web":
        return list(dict.fromkeys([midum.config.GEMINI_WEB_MODEL or "(auto)", "(auto)", "gemini-3-flash"]))
    return [midum.config.MODEL_NAME]


def _list_ollama_models() -> list:
    """Queries the local Ollama daemon for installed model names. Never raises."""
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



# --- from gui.py, section 4 ---
class MidumGUI(ctk.CTk):
    def __init__(self):
        from gui.dialogs import DEFAULT_PROVIDER_KEY
        super().__init__()

        self.title("Midum Control Center")
        self.geometry("1600x950")
        self.minsize(1200, 750)
        self.configure(fg_color=C["bg"])

        self._session      = MidumSession()
        self._thinking     = False
        self._log_queue    = queue.Queue()   # stdout lines from engine
        self._reply_queue  = queue.Queue()   # (reply, tool_outputs) from engine

        # ── Persistent chat history ─────────────────────────────────────────
        self._chat_store      = ChatStore(CHATS_DIR)
        self._current_chat_id = uuid.uuid4().hex
        self._chat_title      = None      # derived from first user message
        self._display_log     = []        # [(tag, text), ...] replay log
        self._replaying_chat  = False     # True while re-rendering a loaded chat

        # Provider / model selection — defaults to Local (Ollama) on every
        # launch, regardless of MODEL_PROVIDER hardcoded in main.py.
        self._selected_provider = DEFAULT_PROVIDER_KEY
        self._selected_model    = _default_model_for_provider(DEFAULT_PROVIDER_KEY)

        # File selection paths tracking
        self._sys_core_active_file = None
        self._knowledge_active_file = None
        self._skills_active_file = None

        # Redirect stdout
        self._stdout_redir = _StdoutRedirector(self._log_queue.put)
        sys.stdout = self._stdout_redir

        # ── say() tool compatibility ────────────────────────────────────────
        # midum._print_reply binds a rich Console to sys.stdout at MODULE
        # IMPORT time (before our redirection above ever runs), so its rich
        # markdown branch would silently bypass the GUI entirely. We intercept
        # the function directly instead of relying on stdout capture, and tag
        # the payload so _poll() can render it as a live message in the main
        # chat rather than just a log line.
        def _gui_say_intercept(label: str, text: str):
            if not text or re.match(r'^[{}\[\]",:\s]*$', text.strip()):
                return
            self._log_queue.put(_SAY_TAG + text)
        midum._print_reply = _gui_say_intercept

        # ── ask_user_* inline chat interaction hook ─────────────────────────
        # Wires main.py's ask_user_text / ask_user_file_path / ask_user_approval
        # / ask_user_choice tools to render as inline cards in the main chat
        # instead of separate native tkinter popups. See _handle_gui_ask below.
        midum._gui_ask_hook = self._handle_gui_ask

        # Setup base directory trackers
        self._base_work_dir = r"D:\\"
        if not os.path.exists(self._base_work_dir):
            self._base_work_dir = os.path.expanduser("~/Documents")

        self._build_layout()
        self._apply_model_selection(startup=True)
        self._startup()
        self._poll()

    # ──────────────────────────────────────────────────────────────────────────
    # STARTUP INITIALIZATION
    # ──────────────────────────────────────────────────────────────────────────
    def _startup(self):
        """Perform initial workspace scanning and state initialization."""
        self._scan_workspace_directory()
        self._refresh_status()
        self._init_saved_mcp_servers()

    def _init_saved_mcp_servers(self):
        """
        Auto-connects every server saved in storage/mcp_servers.json, same as
        main.py does in its own __main__ block — which never runs here since
        gui.pyw imports main.py as a module instead of executing it directly.
        Runs off the main thread since each connection can block on a
        subprocess spawn / network handshake.
        """
        configs = midum._load_mcp_config()
        if not configs:
            return

        self._activity_append(f"🔌 Reconnecting {len(configs)} saved MCP server(s)...\n")

        def worker():
            midum.init_mcp_servers_from_config()
            self.after(0, self._refresh_mcp_list)
            self.after(0, lambda: self._activity_append("🔌 MCP auto-connect pass complete.\n"))

        threading.Thread(target=worker, daemon=True).start()

        # 1. Ensure core directories and files exist
        midum.memory._bootstrap_all_files()
        
        # 2. Fetch master system prompt from the engine
        try:
            sys_prompt = midum.get_system_prompt()
        except AttributeError:
            # Fallback if get_system_prompt hasn't been merged into main.py yet
            sys_prompt = "You are Midum. Rules:\n- Proceed safely."

        # 3. Load general core memories safely (bypass CLI input loops)
        memories = []
        master_ctx = midum.memory.load_memory_into_context(midum.MASTER_MEMORY, "master")
        if master_ctx: 
            memories.append(master_ctx)
        
        session_ctx = midum.memory.load_memory_into_context(midum.SESSION_MEMORY, "session (continued)")
        if session_ctx: 
            memories.append(session_ctx)
            
        try:
            with open(midum.INSTRUCTIONS_FILE, "r", encoding="utf-8") as f:
                _instr = f.read().strip()
            if _instr: 
                memories.append("[MIDUM INSTRUCTIONS — always active]\n" + _instr)
        except Exception:
            pass

        # 4. Inject prompt and memories into the active GUI session
        self._session.initialise(sys_prompt, memories)

    # ──────────────────────────────────────────────────────────────────────────
    # LAYOUT — Slim topbar · resizable left sidebar · full-width chat
    # ──────────────────────────────────────────────────────────────────────────
    def _build_layout(self):
        # Outer shell: topbar on top, PanedWindow below
        self._root_frame = ctk.CTkFrame(self, fg_color=C["bg"], corner_radius=0)
        self._root_frame.pack(fill="both", expand=True)

        self._build_topbar()

        # PanedWindow fills all space below the topbar
        self._paned = tk.PanedWindow(
            self._root_frame, orient=tk.HORIZONTAL,
            bg=C["border"],          # sash colour matches divider line
            bd=0, sashwidth=4, sashpad=0,
            sashrelief="flat", opaqueresize=True
        )
        self._paned.pack(fill="both", expand=True)

        self._build_sidebar_panel()
        self._build_chat_area()

    # ─────────────────────────────────────────────────────────────────────────
    # SLIM TOP BAR  (32px — brand + status + abort)
    # ─────────────────────────────────────────────────────────────────────────
    def _build_topbar(self):
        bar = ctk.CTkFrame(
            self._root_frame, fg_color=C["panel"],
            corner_radius=0, border_width=0, height=32
        )
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)
        bar.grid_columnconfigure(1, weight=1)

        # Brand (left)
        brand = ctk.CTkFrame(bar, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="w", padx=(12, 0))
        ctk.CTkLabel(brand, text="⚡", font=("Segoe UI", 13), text_color=C["accent"]).pack(side="left")
        ctk.CTkLabel(brand, text="Midum", font=("Segoe UI", 12, "bold"), text_color=C["text"]).pack(side="left", padx=(5, 0))

        # Status (center-left)
        status_row = ctk.CTkFrame(bar, fg_color="transparent")
        status_row.grid(row=0, column=1, sticky="w", padx=(20, 0))
        self._status_dot = ctk.CTkFrame(status_row, width=6, height=6, corner_radius=3, fg_color=C["yellow"])
        self._status_dot.pack(side="left", padx=(0, 5))
        self._status_label = ctk.CTkLabel(status_row, text="Initializing...", font=("Segoe UI", 10), text_color=C["subtext"])
        self._status_label.pack(side="left")

        # Abort (right)
        self._abort_btn = ctk.CTkButton(
            bar, text="Abort", width=64, height=22,
            fg_color="transparent", hover_color="#2d1010",
            text_color=C["red"], border_width=1, border_color="#3f0f0f",
            font=("Segoe UI", 10), corner_radius=11, command=self._abort
        )
        self._abort_btn.grid(row=0, column=2, sticky="e", padx=(0, 10))

        # 1px bottom border
        ctk.CTkFrame(self._root_frame, fg_color=C["border"], height=1, corner_radius=0
        ).pack(side="top", fill="x")

    # ─────────────────────────────────────────────────────────────────────────
    # LEFT SIDEBAR  (fixed 280px — workspace + all system tabs)
    # ─────────────────────────────────────────────────────────────────────────
    def _build_sidebar_panel(self):
        self._sidebar = ctk.CTkFrame(
            self._paned, fg_color=C["panel"], corner_radius=0, border_width=0
        )
        self._sidebar.grid_columnconfigure(0, weight=1)
        self._sidebar.grid_rowconfigure(5, weight=1)  # tabs expand

        # Add to paned window — sets initial width and enforces minimum
        self._paned.add(self._sidebar, minsize=200, width=420)

        # ── New Session · Chat History ──────────────────────────────────────
        session_row = ctk.CTkFrame(self._sidebar, fg_color="transparent")
        session_row.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        session_row.grid_columnconfigure(0, weight=1)
        session_row.grid_columnconfigure(1, weight=0)

        ctk.CTkButton(
            session_row, text="+ New Session", height=30,
            fg_color=C["surface2"], hover_color=C["border2"],
            text_color=C["text"], font=FONT_SMALL,
            corner_radius=20, command=self._new_session
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6))

        ctk.CTkButton(
            session_row, text="🕘", width=36, height=30,
            fg_color=C["surface2"], hover_color=C["border2"],
            text_color=C["text"], font=FONT_SMALL,
            corner_radius=20, command=self._open_history_dialog
        ).grid(row=0, column=1, sticky="e")

        # ── Workspace label ───────────────────────────────────────────────────
        ctk.CTkLabel(
            self._sidebar, text="WORKSPACE", font=("Segoe UI", 9, "bold"),
            text_color=C["subtext"]
        ).grid(row=1, column=0, sticky="w", padx=14, pady=(2, 2))

        ws = ctk.CTkFrame(self._sidebar, fg_color="transparent")
        ws.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 4))
        ws.grid_columnconfigure(0, weight=1)

        self._project_dropdown = ctk.CTkOptionMenu(
            ws, values=["Scanning..."],
            command=self._on_project_switched,
            fg_color=C["surface"], button_color=C["border2"],
            button_hover_color=C["accent"], dropdown_fg_color=C["surface"],
            dropdown_hover_color=C["surface2"], text_color=C["text"], font=FONT_SMALL,
            corner_radius=20, anchor="w"
        )
        self._project_dropdown.grid(row=0, column=0, sticky="ew", pady=(0, 4))

        proj_btns = ctk.CTkFrame(ws, fg_color="transparent")
        proj_btns.grid(row=1, column=0, sticky="ew")
        proj_btns.grid_columnconfigure(0, weight=1)
        proj_btns.grid_columnconfigure(1, weight=1)
        proj_btns.grid_columnconfigure(2, weight=1)

        for col, (lbl, cmd) in enumerate([
            ("+ Project", self._create_project_dialog),
            ("📂 Scan",   self._change_base_work_directory),
            ("💻 Code",   self._open_project_in_vscode),
        ]):
            ctk.CTkButton(
                proj_btns, text=lbl, height=24, font=("Segoe UI", 10),
                fg_color="transparent", hover_color=C["surface2"],
                border_width=1, border_color=C["border2"], corner_radius=12,
                command=cmd
            ).grid(row=0, column=col, sticky="ew", padx=2)

        # ── File list (compact fixed height) ─────────────────────────────────
        self._file_list_box = ctk.CTkTextbox(
            self._sidebar, font=("Segoe UI", 9), fg_color="transparent",
            text_color=C["subtext"], wrap="none", corner_radius=0,
            state="disabled", border_width=0, height=90
        )
        self._file_list_box.grid(row=3, column=0, sticky="ew", padx=14, pady=(2, 0))

        # ── Divider ───────────────────────────────────────────────────────────
        ctk.CTkFrame(self._sidebar, fg_color=C["border"], height=1, corner_radius=0
        ).grid(row=4, column=0, sticky="ew", padx=10, pady=(4, 0))

        # ── System tabs (expands) ─────────────────────────────────────────────
        # Rounded card look: the whole tabview sits on a raised, bordered
        # surface, and the segmented-button strip (the row of tab labels)
        # gets its own pill-shaped buttons with generous spacing instead of
        # the flat, square default look.
        tabs_outer = ctk.CTkFrame(
            self._sidebar, fg_color=C["surface"], corner_radius=16,
            border_width=1, border_color=C["border2"]
        )
        tabs_outer.grid(row=5, column=0, sticky="nsew", padx=8, pady=(8, 0))
        tabs_outer.grid_rowconfigure(0, weight=1)
        tabs_outer.grid_columnconfigure(0, weight=1)

        self._tabs = ctk.CTkTabview(
            tabs_outer,
            fg_color="transparent",
            segmented_button_fg_color=C["panel"],
            segmented_button_selected_color=C["accent"],
            segmented_button_selected_hover_color=C["accent_dim"],
            segmented_button_unselected_hover_color=C["surface2"],
            segmented_button_unselected_color=C["panel"],
            text_color=C["text"],
            text_color_disabled=C["subtext"],
            corner_radius=14,
        )
        self._tabs.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        for tab in ("Log", "Model", "Parameters", "System Core", "Knowledge", "Skills", "Tools", "MCP"):
            self._tabs.add(tab)

        # The segmented button is the actual row of tab labels — style it
        # directly for rounded pills, tighter/cleaner font, and breathing
        # room between each label instead of the cramped default strip.
        try:
            seg = self._tabs._segmented_button
            seg.configure(font=FONT_LABEL, corner_radius=12, height=30)
            for child in seg._buttons_dict.values():
                child.configure(corner_radius=12, font=FONT_LABEL)
        except Exception:
            pass

        self._build_activity_panel()
        self._build_model_tab()
        self._build_status_tab()
        self._build_system_core_tab()
        self._build_knowledge_bases_tab()
        self._build_skills_tab()
        self._build_manual_tools_tab()
        self._build_mcp_tab()

        # ── Footer ───────────────────────────────────────────────────────────
        footer = ctk.CTkFrame(self._sidebar, fg_color="transparent")
        footer.grid(row=6, column=0, sticky="ew", padx=8, pady=(2, 8))
        footer.grid_columnconfigure(0, weight=1)
        footer.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(
            footer, text="🐚 Terminal", height=26, font=("Segoe UI", 10),
            fg_color="transparent", hover_color=C["surface2"],
            text_color=C["subtext"], corner_radius=12,
            command=self._open_project_terminal
        ).grid(row=0, column=0, sticky="ew", padx=(0, 3))

        ctk.CTkButton(
            footer, text="⏻ Shutdown", height=26, font=("Segoe UI", 10),
            fg_color="transparent", hover_color="#2d1010",
            text_color=C["red"], corner_radius=12,
            command=self._shutdown_engine
        ).grid(row=0, column=1, sticky="ew", padx=(3, 0))

        # (sash between sidebar and chat is provided by PanedWindow)

    # ─────────────────────────────────────────────────────────────────────────
    # CENTER CHAT AREA  (fills all remaining width, content pinned to 760px col)
    # ─────────────────────────────────────────────────────────────────────────
    # Shared column width — messages AND input are constrained to this width.
    _CHAT_COL_W = 760

    def _build_chat_area(self):
        self._chat_panel_frame = ctk.CTkFrame(
            self._paned, fg_color=C["bg"], corner_radius=0
        )
        self._chat_panel_frame.grid_rowconfigure(0, weight=1)
        self._chat_panel_frame.grid_columnconfigure(0, weight=1)
        self._paned.add(self._chat_panel_frame, minsize=400)

        # ── Scrollable message area ───────────────────────────────────────────
        # Uses the same 3-col centering: spacer | fixed-760 | spacer
        self._chat_scroll = ctk.CTkScrollableFrame(
            self._chat_panel_frame, fg_color=C["bg"], corner_radius=0, border_width=0
        )
        self._chat_scroll.grid(row=0, column=0, sticky="nsew")
        # 3-column layout inside the scroll frame
        self._chat_scroll.grid_columnconfigure(0, weight=1)   # left spacer
        self._chat_scroll.grid_columnconfigure(1, weight=0)   # fixed content col
        self._chat_scroll.grid_columnconfigure(2, weight=1)   # right spacer
        # Invisible fixed-width anchor that enforces the content column width
        self._chat_col_anchor = ctk.CTkFrame(
            self._chat_scroll, fg_color="transparent",
            width=self._CHAT_COL_W, height=1
        )
        self._chat_col_anchor.grid(row=0, column=1, sticky="ew")
        self._chat_col_anchor.grid_propagate(False)
        # Messages are appended starting at row 1
        self._chat_row = 1

        # ── Centered input pill ───────────────────────────────────────────────
        input_outer = ctk.CTkFrame(self._chat_panel_frame, fg_color=C["bg"], corner_radius=0)
        input_outer.grid(row=1, column=0, sticky="ew", pady=(8, 14))
        input_outer.grid_columnconfigure(0, weight=1)
        input_outer.grid_columnconfigure(1, weight=0)
        input_outer.grid_columnconfigure(2, weight=1)

        input_center = ctk.CTkFrame(input_outer, fg_color="transparent", width=self._CHAT_COL_W)
        input_center.grid(row=0, column=1, sticky="ew")
        input_center.grid_propagate(False)
        input_center.grid_columnconfigure(0, weight=1)

        input_box = ctk.CTkFrame(
            input_center, fg_color=C["surface"],
            corner_radius=26, border_width=1, border_color=C["border2"]
        )
        input_box.grid(row=0, column=0, sticky="ew")
        input_box.grid_columnconfigure(0, weight=1)

        self._input = ctk.CTkEntry(
            input_box, placeholder_text="Message Midum...",
            font=FONT_BODY, fg_color="transparent",
            text_color=C["text"], border_width=0,
            height=46, corner_radius=26
        )
        self._input.grid(row=0, column=0, sticky="ew", padx=(16, 6), pady=6)
        self._input.bind("<Return>", self._send)

        self._send_btn = ctk.CTkButton(
            input_box, text="↑", width=36, height=36,
            fg_color=C["accent"], hover_color=C["accent_dim"],
            font=("Segoe UI", 16, "bold"), corner_radius=20, command=self._send
        )
        self._send_btn.grid(row=0, column=1, padx=(0, 6), pady=6)

        ctk.CTkLabel(
            input_center, text="Ctrl+Q to abort  ·  Enter to send",
            font=("Segoe UI", 9), text_color=C["muted"]
        ).grid(row=1, column=0, pady=(4, 0))

    # ─────────────────────────────────────────────────────────────────────────
    # ACTIVITY LOG  (Log tab in sidebar)
    # ─────────────────────────────────────────────────────────────────────────
    def _build_activity_panel(self):
        tab = self._tabs.tab("Log")
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        hdr = ctk.CTkFrame(tab, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            hdr, text="Clear", width=46, height=22, font=FONT_LABEL,
            fg_color="transparent", hover_color=C["surface2"],
            border_width=1, border_color=C["border2"], corner_radius=11,
            command=lambda: self._clear_box(self._activity_box)
        ).grid(row=0, column=1, sticky="e")

        self._activity_box = ctk.CTkTextbox(
            tab, font=FONT_MONO, fg_color=C["tool_bg"],
            text_color=C["tool_text"], wrap="word",
            state="disabled", corner_radius=8, border_width=1, border_color=C["border"]
        )
        self._activity_box.grid(row=1, column=0, sticky="nsew")
        self._activity_box._textbox.configure(spacing1=3, spacing2=2, padx=6, pady=6)

    # ── Model Tab (provider + model selection) ────────────────────────────────
    def _build_model_tab(self):
        from gui.dialogs import PROVIDER_OPTIONS, _PROVIDER_KEY_TO_LABEL
        tab = self._tabs.tab("Model")
        tab.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            tab, text="PROVIDER", font=("Segoe UI", 9, "bold"), text_color=C["subtext"]
        ).grid(row=0, column=0, sticky="w", padx=4, pady=(6, 2))

        self._provider_dropdown = ctk.CTkOptionMenu(
            tab, values=[label for label, _ in PROVIDER_OPTIONS],
            command=self._on_provider_selected,
            fg_color=C["surface"], button_color=C["border2"],
            button_hover_color=C["accent"], dropdown_fg_color=C["surface"],
            dropdown_hover_color=C["surface2"], text_color=C["text"], font=FONT_SMALL,
            corner_radius=20
        )
        self._provider_dropdown.set(_PROVIDER_KEY_TO_LABEL[self._selected_provider])
        self._provider_dropdown.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 8))

        model_hdr = ctk.CTkFrame(tab, fg_color="transparent")
        model_hdr.grid(row=2, column=0, sticky="ew", padx=4)
        model_hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            model_hdr, text="MODEL", font=("Segoe UI", 9, "bold"), text_color=C["subtext"]
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            model_hdr, text="⟳", width=24, height=20, font=FONT_TINY,
            fg_color="transparent", hover_color=C["surface2"],
            border_width=1, border_color=C["border2"], corner_radius=10,
            command=self._refresh_model_choices
        ).grid(row=0, column=1, sticky="e")

        self._model_combobox = ctk.CTkComboBox(
            tab, values=_known_models_for_provider(self._selected_provider),
            fg_color=C["surface"], border_color=C["border2"],
            button_color=C["border2"], button_hover_color=C["accent"],
            dropdown_fg_color=C["surface"], dropdown_hover_color=C["surface2"],
            text_color=C["text"], font=FONT_SMALL, corner_radius=20
        )
        self._model_combobox.set(self._selected_model)
        self._model_combobox.grid(row=3, column=0, sticky="ew", padx=4, pady=(2, 8))

        ctk.CTkButton(
            tab, text="Apply", height=30, font=FONT_SMALL,
            fg_color=C["accent"], hover_color=C["accent_dim"], corner_radius=20,
            command=self._apply_model_selection
        ).grid(row=4, column=0, sticky="ew", padx=4, pady=(0, 4))

        self._model_active_lbl = ctk.CTkLabel(
            tab, text="", font=FONT_TINY, text_color=C["subtext"],
            justify="left", anchor="w", wraplength=320
        )
        self._model_active_lbl.grid(row=5, column=0, sticky="w", padx=4, pady=(4, 8))

        ctk.CTkFrame(tab, fg_color=C["border"], height=1, corner_radius=0
        ).grid(row=6, column=0, sticky="ew", padx=4, pady=(0, 8))
        ctk.CTkLabel(
            tab,
            text=(
                "Local (Ollama) runs fully offline and is the default on "
                "every launch. Switching providers here only affects this "
                "running session — it does not edit main.py."
            ),
            font=FONT_TINY, text_color=C["muted"], justify="left",
            anchor="w", wraplength=320
        ).grid(row=7, column=0, sticky="w", padx=4)

    def _on_provider_selected(self, selected_label: str):
        from gui.dialogs import DEFAULT_PROVIDER_KEY, _PROVIDER_LABEL_TO_KEY
        """Repopulates the model dropdown to match the newly chosen provider."""
        provider_key = _PROVIDER_LABEL_TO_KEY.get(selected_label, DEFAULT_PROVIDER_KEY)
        self._refresh_model_choices(provider_key=provider_key)

    def _refresh_model_choices(self, provider_key: str = None):
        from gui.dialogs import DEFAULT_PROVIDER_KEY, _PROVIDER_LABEL_TO_KEY
        """Reloads the model dropdown's values for the currently selected provider.
        For Ollama, tries a live query of installed models off the main thread."""
        provider_key = provider_key or _PROVIDER_LABEL_TO_KEY.get(
            self._provider_dropdown.get(), DEFAULT_PROVIDER_KEY
        )
        default_model = _default_model_for_provider(provider_key)

        if provider_key == "ollama":
            self._model_combobox.configure(values=[default_model])
            self._model_combobox.set(default_model)

            def worker():
                names = _list_ollama_models()
                if names:
                    self.after(0, lambda: self._model_combobox.configure(values=names))
            threading.Thread(target=worker, daemon=True).start()
        else:
            choices = _known_models_for_provider(provider_key)
            self._model_combobox.configure(values=choices)
            self._model_combobox.set(default_model)

    def _apply_model_selection(self, startup: bool = False):
        from gui.dialogs import DEFAULT_PROVIDER_KEY, _PROVIDER_KEY_TO_LABEL, _PROVIDER_LABEL_TO_KEY
        """Commits the chosen provider/model. Mutates the matching midum.py
        global(s) so every part of the engine (system prompt, status labels,
        consult/delegate helpers) sees the new selection consistently, and
        stores it locally so _run_turn can pass it as force_provider/force_model."""
        provider_key = _PROVIDER_LABEL_TO_KEY.get(
            self._provider_dropdown.get(), DEFAULT_PROVIDER_KEY
        )
        model_id = self._model_combobox.get().strip()
        if not model_id or model_id == "(auto)":
            model_id = "" if provider_key == "gemini_web" else _default_model_for_provider(provider_key)

        self._selected_provider = provider_key
        self._selected_model    = model_id

        midum.config.MODEL_PROVIDER = provider_key
        if provider_key == "ollama":
            midum.config.MODEL_NAME = model_id
        elif provider_key == "openrouter":
            midum.config.OPENROUTER_MODEL = model_id
        elif provider_key == "gemini_web":
            midum.config.GEMINI_WEB_MODEL = model_id
            # _gemini_web_pick_model() caches its resolved model per-process
            # and only re-reads config.GEMINI_WEB_MODEL when this cache is
            # None (see set_gemini_web_model in gemini_web_backend.py) —
            # clear it here so a GUI-driven switch takes effect immediately
            # instead of silently keeping whatever model was picked first.
            midum.providers_gemini_web_backend._gemini_web_model_cache = None
        elif provider_key == "gemini_api":
            midum.config.GEMINI_API_MODEL = model_id
        elif provider_key == "groq":
            midum.config.GROQ_MODEL = model_id

        label = _PROVIDER_KEY_TO_LABEL[provider_key]
        shown = model_id or "(auto)"
        if hasattr(self, "_model_active_lbl"):
            self._model_active_lbl.configure(text=f"Active: {label} — {shown}")
        if not startup:
            self._activity_append(f"🔀 [Provider switched: {label} — {shown}]\n")
        if hasattr(self, "_lbl_model"):
            self._refresh_status()

    # ── Parameters Tab ────────────────────────────────────────────────────────
    def _build_status_tab(self):
        tab = self._tabs.tab("Parameters")
        tab.grid_columnconfigure(0, weight=1)

        def _stat_row(parent, label, row):
            row_frame = ctk.CTkFrame(parent, fg_color="transparent")
            row_frame.grid(row=row, column=0, sticky="ew", padx=4, pady=(5, 0))
            row_frame.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(row_frame, text=label, font=FONT_LABEL, text_color=C["subtext"]).grid(row=0, column=0, sticky="w")
            val = ctk.CTkLabel(row_frame, text="—", font=FONT_SMALL, text_color=C["text"], wraplength=320, justify="left")
            val.grid(row=1, column=0, sticky="w", pady=(1, 3))
            ctk.CTkFrame(parent, fg_color=C["border"], height=1, corner_radius=0).grid(row=row+1, column=0, sticky="ew", padx=4)
            return val

        self._lbl_model   = _stat_row(tab, "Model",           0)
        self._lbl_goal    = _stat_row(tab, "Active Goal",      2)
        self._lbl_project = _stat_row(tab, "Workspace",        4)
        self._lbl_gemini  = _stat_row(tab, "Gemini Research",  6)
        self._lbl_ocr     = _stat_row(tab, "Screen OCR",       8)
        self._lbl_uia     = _stat_row(tab, "UI Automation",   10)
        self._lbl_turns   = _stat_row(tab, "Turn Count",      12)

        ctk.CTkButton(
            tab, text="Refresh", height=30, font=FONT_SMALL,
            fg_color="transparent", hover_color=C["surface2"],
            border_width=1, border_color=C["border2"],
            corner_radius=20, command=self._refresh_status
        ).grid(row=14, column=0, padx=4, pady=12, sticky="ew")

    # ── System Core Tab ───────────────────────────────────────────────────────
    def _build_system_core_tab(self):
        tab = self._tabs.tab("System Core")
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        ctrl = ctk.CTkFrame(tab, fg_color="transparent")
        ctrl.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ctrl.grid_columnconfigure(0, weight=1)

        self._sys_core_dropdown = ctk.CTkOptionMenu(
            ctrl,
            values=["Master Memory", "Session Memory", "Instructions", "Paths", "Active Project", "Scratchpad"],
            command=self._on_sys_core_selected,
            fg_color=C["surface"], button_color=C["border2"],
            button_hover_color=C["accent"], dropdown_fg_color=C["surface"],
            dropdown_hover_color=C["surface2"], text_color=C["text"], font=FONT_SMALL,
            corner_radius=20
        )
        self._sys_core_dropdown.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        ctk.CTkButton(
            ctrl, text="Save", width=58, height=28, font=FONT_SMALL,
            fg_color=C["accent"], hover_color=C["accent_dim"], corner_radius=20,
            command=self._save_sys_core_file
        ).grid(row=0, column=1)

        self._sys_core_box = ctk.CTkTextbox(
            tab, font=FONT_MONO, fg_color=C["tool_bg"],
            text_color=C["text"], wrap="word", corner_radius=10,
            border_width=1, border_color=C["border"]
        )
        self._sys_core_box.grid(row=1, column=0, sticky="nsew")
        self._sys_core_box._textbox.configure(spacing1=3, spacing2=2, padx=8, pady=8)

        self._on_sys_core_selected("Master Memory")

    def _get_sys_core_path(self, selection: str) -> str:
        mapping = {
            "Master Memory": midum.MASTER_MEMORY,
            "Session Memory": midum.SESSION_MEMORY,
            "Instructions": midum.INSTRUCTIONS_FILE,
            "Paths": midum.PATHS_FILE,
            "Active Project": midum.memory._active_project_memory_path,
            "Scratchpad": midum.RESPONSE_MEMORY
        }
        return mapping.get(selection)

    def _on_sys_core_selected(self, selected_label: str):
        path = self._get_sys_core_path(selected_label)
        self._sys_core_active_file = path
        if path:
            self._load_file_into_box(self._sys_core_box, path)
        else:
            self._sys_core_box.configure(state="normal")
            self._sys_core_box.delete("1.0", "end")
            self._sys_core_box.insert("end", "(No active file associated with selection)")

    def _save_sys_core_file(self):
        if not self._sys_core_active_file:
            messagebox.showwarning("Save Blocked", "No active target resolved for saving.")
            return
        self._save_box_to_file(self._sys_core_box, self._sys_core_active_file)

    # ── Knowledge Bases Tab ───────────────────────────────────────────────────
    def _build_knowledge_bases_tab(self):
        tab = self._tabs.tab("Knowledge")
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        control_frame = ctk.CTkFrame(tab, fg_color="transparent")
        control_frame.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 4))
        control_frame.grid_columnconfigure(0, weight=1)

        self._knowledge_dropdown = ctk.CTkOptionMenu(
            control_frame, values=["Loading..."],
            command=self._on_knowledge_selected,
            fg_color=C["surface"], button_color=C["border2"],
            button_hover_color=C["accent"], dropdown_fg_color=C["surface"],
            dropdown_hover_color=C["surface2"], text_color=C["text"], font=FONT_SMALL,
            corner_radius=20
        )
        self._knowledge_dropdown.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        ctk.CTkButton(
            control_frame, text="Save", width=54, height=28, font=FONT_SMALL,
            fg_color=C["accent"], hover_color=C["accent_dim"], corner_radius=20,
            command=self._save_knowledge_file
        ).grid(row=0, column=1, padx=(0, 4))

        ctk.CTkButton(
            control_frame, text="+ New", width=54, height=28, font=FONT_SMALL,
            fg_color="transparent", hover_color=C["surface2"],
            border_width=1, border_color=C["border2"],
            corner_radius=20, command=self._create_knowledge_dialog
        ).grid(row=0, column=2)

        self._knowledge_box = ctk.CTkTextbox(
            tab, font=FONT_MONO, fg_color=C["tool_bg"],
            text_color=C["text"], wrap="word", corner_radius=12,
            border_width=1, border_color=C["border"]
        )
        self._knowledge_box.grid(row=1, column=0, sticky="nsew", padx=0, pady=(4, 0))
        self._knowledge_box._textbox.configure(spacing1=3, spacing2=2, padx=8, pady=8)

        self._refresh_knowledge_dropdown()

    def _refresh_knowledge_dropdown(self, select_name=None):
        try:
            files = []
            if os.path.exists(midum.STORAGE_DIR):
                for f in os.listdir(midum.STORAGE_DIR):
                    if f.endswith(".md") and os.path.isfile(os.path.join(midum.STORAGE_DIR, f)):
                        # Exclude system files from custom list
                        if f.lower() not in ("master_memory.md", "session_memory.md", "instructions.md", "paths.md", "response_memory.md"):
                            files.append(f)
            files.sort()
            
            if not files:
                files = ["No custom bases found"]
                self._knowledge_active_file = None
                self._knowledge_dropdown.configure(values=files)
                self._knowledge_dropdown.set(files[0])
                self._knowledge_box.configure(state="normal")
                self._knowledge_box.delete("1.0", "end")
                self._knowledge_box.insert("end", "(Create a new Knowledge Base to begin writing)")
                self._knowledge_box.configure(state="disabled")
            else:
                self._knowledge_dropdown.configure(values=files)
                target = select_name if select_name in files else files[0]
                self._knowledge_dropdown.set(target)
                self._on_knowledge_selected(target)
        except Exception as e:
            self._activity_append(f"⚠️ Knowledge scan failed: {e}\n")

    def _on_knowledge_selected(self, filename: str):
        if filename == "No custom bases found":
            return
        path = os.path.join(midum.STORAGE_DIR, filename)
        self._knowledge_active_file = path
        self._load_file_into_box(self._knowledge_box, path)

    def _save_knowledge_file(self):
        if not self._knowledge_active_file:
            messagebox.showwarning("Save Blocked", "No active knowledge base selected.")
            return
        self._save_box_to_file(self._knowledge_box, self._knowledge_active_file)

    def _create_knowledge_dialog(self):
        from gui.dialogs import CreateKnowledgeDialog
        CreateKnowledgeDialog(self, self._execute_create_knowledge)

    def _execute_create_knowledge(self, name, description):
        try:
            result = midum.create_domain_knowledge(name, description)
            self._activity_append(f"⚙️ {result}\n")
            
            safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip().lower())
            expected_file = f"{safe_name}.md"
            self._refresh_knowledge_dropdown(select_name=expected_file)
            
            # Sync metadata changes dynamically
            self._load_file_into_box(self._sys_core_box, self._sys_core_active_file)
        except Exception as e:
            messagebox.showerror("Error", f"Failed creating knowledge base: {e}")

    # ── Skills Tab ────────────────────────────────────────────────────────────
    def _build_skills_tab(self):
        tab = self._tabs.tab("Skills")
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        control_frame = ctk.CTkFrame(tab, fg_color="transparent")
        control_frame.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 4))
        control_frame.grid_columnconfigure(0, weight=1)

        self._skills_dropdown = ctk.CTkOptionMenu(
            control_frame, values=["Loading..."],
            command=self._on_skill_selected,
            fg_color=C["surface"], button_color=C["border2"],
            button_hover_color=C["accent"], dropdown_fg_color=C["surface"],
            dropdown_hover_color=C["surface2"], text_color=C["text"], font=FONT_SMALL,
            corner_radius=20
        )
        self._skills_dropdown.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        ctk.CTkButton(
            control_frame, text="Save", width=54, height=28, font=FONT_SMALL,
            fg_color=C["accent"], hover_color=C["accent_dim"], corner_radius=20,
            command=self._save_skill_file
        ).grid(row=0, column=1, padx=(0, 4))

        ctk.CTkButton(
            control_frame, text="+ New", width=54, height=28, font=FONT_SMALL,
            fg_color="transparent", hover_color=C["surface2"],
            border_width=1, border_color=C["border2"],
            corner_radius=20, command=self._create_skill_dialog
        ).grid(row=0, column=2)

        self._skills_box = ctk.CTkTextbox(
            tab, font=FONT_MONO, fg_color=C["tool_bg"],
            text_color=C["text"], wrap="word", corner_radius=12,
            border_width=1, border_color=C["border"]
        )
        self._skills_box.grid(row=1, column=0, sticky="nsew", padx=0, pady=(4, 0))
        self._skills_box._textbox.configure(spacing1=3, spacing2=2, padx=8, pady=8)

        self._refresh_skills_dropdown()

    def _refresh_skills_dropdown(self, select_name=None):
        try:
            files = []
            if os.path.exists(midum.SKILLS_DIR):
                for f in os.listdir(midum.SKILLS_DIR):
                    if f.endswith(".md") and os.path.isfile(os.path.join(midum.SKILLS_DIR, f)):
                        files.append(f)
            files.sort()

            if not files:
                files = ["No custom skills found"]
                self._skills_active_file = None
                self._skills_dropdown.configure(values=files)
                self._skills_dropdown.set(files[0])
                self._skills_box.configure(state="normal")
                self._skills_box.delete("1.0", "end")
                self._skills_box.insert("end", "(Create a new Skill to begin writing custom logic)")
                self._skills_box.configure(state="disabled")
            else:
                self._skills_dropdown.configure(values=files)
                target = select_name if select_name in files else files[0]
                self._skills_dropdown.set(target)
                self._on_skill_selected(target)
        except Exception as e:
            self._activity_append(f"⚠️ Skills scan failed: {e}\n")

    def _on_skill_selected(self, filename: str):
        if filename == "No custom skills found":
            return
        path = os.path.join(midum.SKILLS_DIR, filename)
        self._skills_active_file = path
        self._load_file_into_box(self._skills_box, path)

    def _save_skill_file(self):
        if not self._skills_active_file:
            messagebox.showwarning("Save Blocked", "No active skill selected.")
            return
        self._save_box_to_file(self._skills_box, self._skills_active_file)

    def _create_skill_dialog(self):
        from gui.dialogs import CreateSkillDialog
        CreateSkillDialog(self, self._execute_create_skill)

    def _execute_create_skill(self, name, domain, description):
        try:
            initial_content = (
                f"## Summary\n"
                f"Instructions to execute custom skill workflow on {domain}.\n\n"
                f"## Action Checklist\n"
                f"1. [ ] State objective details.\n"
                f"2. [ ] Invoke terminal execution calls.\n"
            )
            result = midum.create_domain_skill(name, domain, description, initial_content)
            self._activity_append(f"⚙️ {result}\n")

            safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip().lower())
            expected_file = f"{safe_name}.md"
            self._refresh_skills_dropdown(select_name=expected_file)
            
            # Sync indexes dynamically
            self._load_file_into_box(self._sys_core_box, self._sys_core_active_file)
        except Exception as e:
            messagebox.showerror("Error", f"Failed creating custom skill: {e}")

    # ── Manual Tools Tab ──────────────────────────────────────────────────────
    def _build_manual_tools_tab(self):
        tab = self._tabs.tab("Tools")
        tab.grid_rowconfigure(2, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        control_frame = ctk.CTkFrame(tab, fg_color="transparent")
        control_frame.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 4))
        control_frame.grid_columnconfigure(0, weight=1)

        tool_names = [t["function"]["name"] for t in midum.tools]
        tool_names.sort()

        self._manual_tool_dropdown = ctk.CTkOptionMenu(
            control_frame, values=tool_names,
            command=self._on_manual_tool_selected,
            fg_color=C["surface"], button_color=C["border2"],
            button_hover_color=C["accent"], dropdown_fg_color=C["surface"],
            dropdown_hover_color=C["surface2"], text_color=C["text"], font=FONT_SMALL,
            corner_radius=20
        )
        self._manual_tool_dropdown.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        ctk.CTkButton(
            control_frame, text="▶ Run", width=64, height=28, font=FONT_SMALL,
            fg_color=C["accent"], hover_color=C["accent_dim"], corner_radius=20,
            command=self._execute_manual_tool
        ).grid(row=0, column=1)

        # Dynamic arg inputs area
        self._manual_args_frame = ctk.CTkScrollableFrame(
            tab, fg_color=C["surface"], height=130,
            corner_radius=12, border_width=1, border_color=C["border"]
        )
        self._manual_args_frame.grid(row=1, column=0, sticky="ew", padx=0, pady=(0, 4))
        self._manual_args_frame.grid_columnconfigure(1, weight=1)

        self._manual_arg_entries = {}

        self._manual_output_box = ctk.CTkTextbox(
            tab, font=FONT_MONO, fg_color=C["tool_bg"],
            text_color=C["tool_text"], wrap="word", corner_radius=12,
            border_width=1, border_color=C["border"]
        )
        self._manual_output_box.grid(row=2, column=0, sticky="nsew", padx=0, pady=(0, 0))
        self._manual_output_box._textbox.configure(spacing1=3, spacing2=2, padx=8, pady=8)

        if tool_names:
            self._manual_tool_dropdown.set(tool_names[0])
            self._on_manual_tool_selected(tool_names[0])

    def _on_manual_tool_selected(self, tool_name: str):
        # Destroy old argument inputs
        for widget in self._manual_args_frame.winfo_children():
            widget.destroy()
        self._manual_arg_entries.clear()

        # Locate tool schema to build arguments dynamically
        schema = next((t["function"] for t in midum.tools if t["function"]["name"] == tool_name), None)
        if not schema:
            return

        props = schema.get("parameters", {}).get("properties", {})
        required = schema.get("parameters", {}).get("required", [])

        row = 0
        for arg_name, arg_details in props.items():
            req_str = " *" if arg_name in required else ""
            desc = arg_details.get("description", "")
            enum = arg_details.get("enum")

            lbl = ctk.CTkLabel(self._manual_args_frame, text=f"{arg_name}{req_str}", font=FONT_BOLD, text_color=C["subtext"])
            lbl.grid(row=row, column=0, sticky="ne", padx=(5, 10), pady=(5, 5))

            if enum:
                # Constrained fields (click_type, action, browser, ...) get a
                # dropdown so the value can never be an invalid typo — this is
                # a big source of "tool doesn't work" reports in the wild.
                widget = ctk.CTkOptionMenu(
                    self._manual_args_frame, values=list(enum),
                    fg_color=C["surface"], button_color=C["border2"],
                    button_hover_color=C["accent"], dropdown_fg_color=C["surface"],
                    dropdown_hover_color=C["surface2"], text_color=C["text"],
                    font=FONT_BODY, corner_radius=20
                )
                widget.set(enum[0])
            else:
                widget = ctk.CTkEntry(
                    self._manual_args_frame, font=FONT_BODY, fg_color=C["surface"],
                    text_color=C["text"], border_color=C["border2"], corner_radius=20
                )
                if desc:
                    widget.configure(placeholder_text=desc)

            widget.grid(row=row, column=1, sticky="ew", padx=(0, 5), pady=(5, 5))
            self._manual_arg_entries[arg_name] = widget
            row += 1

        if not props:
            ctk.CTkLabel(self._manual_args_frame, text="No arguments required for this tool.", font=FONT_SMALL, text_color=C["subtext"]).grid(row=0, column=0, pady=10)

    def _execute_manual_tool(self):
        tool_name = self._manual_tool_dropdown.get()
        schema = next((t["function"] for t in midum.tools if t["function"]["name"] == tool_name), None)
        if not schema:
            self._update_manual_output(f"Error: '{tool_name}' has no registered schema in midum.tools.")
            return

        props    = schema.get("parameters", {}).get("properties", {})
        required = schema.get("parameters", {}).get("required", [])

        # ── Collect + type-coerce every argument against the tool's own
        # JSON schema, exactly like main.py does before dispatch. Centralizing
        # coercion here (instead of duplicating it per-tool) means every tool
        # — including ones added to main.py later — gets correct types
        # automatically without any GUI changes.
        args    = {}
        missing = []
        for name, widget in self._manual_arg_entries.items():
            raw = widget.get()
            if not raw.strip():
                if name in required:
                    missing.append(name)
                continue
            ptype = props.get(name, {}).get("type", "string")
            if ptype == "integer":
                try:
                    args[name] = int(raw)
                except ValueError:
                    self._update_manual_output(f"Error: '{name}' must be an integer, got '{raw}'.")
                    return
            elif ptype == "number":
                try:
                    args[name] = float(raw)
                except ValueError:
                    self._update_manual_output(f"Error: '{name}' must be a number, got '{raw}'.")
                    return
            elif ptype == "boolean":
                args[name] = raw.strip().lower() in ("1", "true", "yes", "on")
            else:
                args[name] = raw

        if missing:
            self._update_manual_output(
                f"Error: missing required argument(s) for '{tool_name}': {', '.join(missing)}"
            )
            return

        self._update_manual_output(f"[Executing tool sandbox call: {tool_name}...]")

        def run_tool_background():
            from gui.dispatch import _dispatch_midum_tool
            try:
                out = _dispatch_midum_tool(tool_name, args)
                self.after(0, self._update_manual_output, str(out))
            except TypeError as e:
                expected = ", ".join(props.keys()) or "(none)"
                self.after(0, self._update_manual_output,
                           f"Argument error calling '{tool_name}':\n{e}\n\n"
                           f"Expected parameters: {expected}")
            except Exception as e:
                self.after(0, self._update_manual_output,
                           f"Tool Exception Caught:\n{e}\n\nTraceback:\n{traceback.format_exc()}")

        # Ensure UI does not freeze during tool execution (like waiting on Gemini or terminal)
        threading.Thread(target=run_tool_background, daemon=True).start()

    def _update_manual_output(self, text: str):
        self._manual_output_box.configure(state="normal")
        self._manual_output_box.delete("1.0", "end")
        self._manual_output_box.insert("end", text)
        self._manual_output_box.configure(state="disabled")

    # ── MCP Tab ────────────────────────────────────────────────────────────────
    def _build_mcp_tab(self):
        tab = self._tabs.tab("MCP")
        tab.grid_rowconfigure(2, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        # Control row — add server / refresh
        control_frame = ctk.CTkFrame(tab, fg_color="transparent")
        control_frame.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 4))
        control_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            control_frame, text="MCP SERVERS", font=("Segoe UI", 9, "bold"),
            text_color=C["subtext"]
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            control_frame, text="⟳", width=28, height=28, font=FONT_SMALL,
            fg_color="transparent", hover_color=C["surface2"],
            border_width=1, border_color=C["border2"], corner_radius=20,
            command=self._refresh_mcp_list
        ).grid(row=0, column=1, padx=(6, 4))

        ctk.CTkButton(
            control_frame, text="+ Add Server", width=100, height=28, font=FONT_SMALL,
            fg_color=C["accent"], hover_color=C["accent_dim"], corner_radius=20,
            command=self._add_mcp_server_dialog
        ).grid(row=0, column=2)

        # SDK-missing banner (only shown when the 'mcp' package isn't installed)
        self._mcp_sdk_banner = ctk.CTkLabel(
            tab, text="", font=FONT_TINY, text_color=C["yellow"],
            fg_color="transparent", anchor="w", justify="left", wraplength=380
        )
        self._mcp_sdk_banner.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 4))

        # Scrollable list of connected/known servers
        self._mcp_list_frame = ctk.CTkScrollableFrame(
            tab, fg_color=C["surface"], corner_radius=12,
            border_width=1, border_color=C["border"]
        )
        self._mcp_list_frame.grid(row=2, column=0, sticky="nsew", padx=0, pady=(0, 4))
        self._mcp_list_frame.grid_columnconfigure(0, weight=1)

        self._refresh_mcp_list()

    def _refresh_mcp_list(self):
        """Rebuilds the server list from midum's live MCP state."""
        if not midum._MCP_SDK_AVAILABLE:
            self._mcp_sdk_banner.configure(
                text="⚠️ 'mcp' package not installed — run: pip install mcp"
            )
        else:
            self._mcp_sdk_banner.configure(text="")

        for widget in self._mcp_list_frame.winfo_children():
            widget.destroy()

        names = list(midum._MCP_SERVER_ORDER)
        if not names:
            ctk.CTkLabel(
                self._mcp_list_frame,
                text="No MCP servers connected yet.\nUse “+ Add Server” to connect one.",
                font=FONT_SMALL, text_color=C["subtext"], justify="center"
            ).grid(row=0, column=0, pady=24, padx=10)
            return

        for i, name in enumerate(names):
            self._build_mcp_row(i, name)

    def _build_mcp_row(self, row: int, name: str):
        handle = midum._MCP_SERVERS.get(name)
        if handle is None:
            return

        row_frame = ctk.CTkFrame(
            self._mcp_list_frame, fg_color=C["panel"], corner_radius=12,
            border_width=1, border_color=C["border"]
        )
        row_frame.grid(row=row, column=0, sticky="ew", padx=6, pady=4)
        row_frame.grid_columnconfigure(1, weight=1)

        dot_color = C["green"] if handle.connected else C["red"]
        ctk.CTkLabel(
            row_frame, text="●", font=("Segoe UI", 14), text_color=dot_color, width=18
        ).grid(row=0, column=0, rowspan=2, sticky="ns", padx=(10, 2), pady=8)

        ctk.CTkLabel(
            row_frame, text=name, font=FONT_BOLD, text_color=C["text"], anchor="w"
        ).grid(row=0, column=1, sticky="ew", padx=(4, 4), pady=(8, 0))

        transport = handle.config.get("transport", "stdio")
        if handle.connected:
            subtitle = f"{transport} · {len(handle.tools)} tool(s)"
            sub_color = C["subtext"]
        else:
            subtitle = f"{transport} · connection failed: {handle.error or 'unknown error'}"
            sub_color = C["red"]
        ctk.CTkLabel(
            row_frame, text=subtitle, font=FONT_TINY, text_color=sub_color,
            anchor="w", wraplength=210, justify="left"
        ).grid(row=1, column=1, sticky="ew", padx=(4, 4), pady=(0, 8))

        btn_col = ctk.CTkFrame(row_frame, fg_color="transparent")
        btn_col.grid(row=0, column=2, rowspan=2, sticky="e", padx=8, pady=6)

        if handle.connected:
            ctk.CTkButton(
                btn_col, text="Tools", width=54, height=24, font=FONT_TINY,
                fg_color="transparent", hover_color=C["surface2"],
                border_width=1, border_color=C["border2"], corner_radius=14,
                command=lambda n=name: self._view_mcp_tools(n)
            ).pack(side="top", pady=(0, 4))
            ctk.CTkButton(
                btn_col, text="Disconnect", width=54, height=24, font=FONT_TINY,
                fg_color="transparent", hover_color="#2d1010",
                text_color=C["red"], border_width=1, border_color=C["border2"], corner_radius=14,
                command=lambda n=name: self._disconnect_mcp_server(n, forget=False)
            ).pack(side="top")
        else:
            ctk.CTkButton(
                btn_col, text="Retry", width=54, height=24, font=FONT_TINY,
                fg_color="transparent", hover_color=C["surface2"],
                border_width=1, border_color=C["border2"], corner_radius=14,
                command=lambda n=name: self._retry_mcp_server(n)
            ).pack(side="top", pady=(0, 4))
            ctk.CTkButton(
                btn_col, text="Remove", width=54, height=24, font=FONT_TINY,
                fg_color="transparent", hover_color="#2d1010",
                text_color=C["red"], border_width=1, border_color=C["border2"], corner_radius=14,
                command=lambda n=name: self._disconnect_mcp_server(n, forget=True)
            ).pack(side="top")

    def _add_mcp_server_dialog(self):
        from gui.dialogs import AddMCPServerDialog
        AddMCPServerDialog(self, self._execute_connect_mcp_server)

    def _execute_connect_mcp_server(self, payload: dict):
        self._activity_append(f"🔌 Connecting to MCP server '{payload['name']}'...\n")

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
            self.after(0, lambda: self._activity_append(f"⚙️ {result}\n"))
            self.after(0, self._refresh_mcp_list)

        threading.Thread(target=worker, daemon=True).start()

    def _retry_mcp_server(self, name: str):
        handle = midum._MCP_SERVERS.get(name)
        if not handle:
            return
        self._activity_append(f"🔌 Retrying MCP server '{name}'...\n")

        def worker():
            ok, msg = midum._mcp_manager.connect(name, handle.config)
            self.after(0, lambda: self._activity_append(f"⚙️ {msg}\n"))
            self.after(0, self._refresh_mcp_list)

        threading.Thread(target=worker, daemon=True).start()

    def _disconnect_mcp_server(self, name: str, forget: bool = False):
        if forget:
            confirm = messagebox.askyesno(
                "Remove Server",
                f"Disconnect '{name}' and remove it from saved config?\n"
                f"It will no longer auto-connect on startup."
            )
        else:
            confirm = messagebox.askyesno("Disconnect Server", f"Disconnect '{name}'?")
        if not confirm:
            return

        def worker():
            result = midum.disconnect_mcp_server(name, forget=forget)
            self.after(0, lambda: self._activity_append(f"⚙️ {result}\n"))
            self.after(0, self._refresh_mcp_list)

        threading.Thread(target=worker, daemon=True).start()

    def _view_mcp_tools(self, name: str):
        from gui.dialogs import ViewMCPToolsDialog
        content = midum.show_server_tools(name)
        ViewMCPToolsDialog(self, name, content)

    # ──────────────────────────────────────────────────────────────────────────
    # INTERACTIVE WORKSPACE ENGINE & SIDEBAR MECHANICS
    # ──────────────────────────────────────────────────────────────────────────
    def _scan_workspace_directory(self):
        """Scans the designated workspace directory for active project profiles."""
        self._set_status("Scanning workspace directories...", C["yellow"])
        
        if not os.path.exists(self._base_work_dir):
            os.makedirs(self._base_work_dir, exist_ok=True)

        try:
            # Enumerate folders inside base workspace
            subdirs = [
                d for d in os.listdir(self._base_work_dir) 
                if os.path.isdir(os.path.join(self._base_work_dir, d))
            ]
            
            project_list = []
            for subdir in subdirs:
                project_list.append(subdir)

            project_list.sort()
            
            if not project_list:
                project_list = ["Create first project..."]

            # Update option dropdown cleanly on main thread
            self.after(0, lambda: self._project_dropdown.configure(values=project_list))
            
            # Select first available workspace cleanly
            if project_list and project_list[0] != "Create first project...":
                self.after(0, lambda: self._project_dropdown.set(project_list[0]))
                self.after(0, lambda: self._on_project_switched(project_list[0]))
            else:
                self.after(0, lambda: self._set_status("Workspace Empty", C["subtext"]))

        except Exception as e:
            self._activity_append(f"⚠️ Scan failed: {e}\n")

    def _on_project_switched(self, selected_project: str):
        """Dispatches configuration changes, updates relative memory variables, and switches contexts."""
        if selected_project == "Create first project...":
            return

        project_dir = os.path.join(self._base_work_dir, selected_project)
        project_file = os.path.join(project_dir, "project_memory.md")
        midum.memory._active_project_memory_path = project_file

        # Auto-create active memory if it is missing
        if not os.path.exists(project_file):
            try:
                os.makedirs(project_dir, exist_ok=True)
                midum.write_local_file(
                    project_file,
                    f"# Project Memory: {selected_project}\n"
                    f"Created: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                )
            except Exception as e:
                self._activity_append(f"⚠️ Memory Write failure: {e}\n")

        # Pull contents dynamically into the active conversational context
        try:
            content = open(project_file, encoding="utf-8").read().strip()
            if content:
                # Flush old project memory and append newly updated profile
                self._session.memory_injections = [
                    inj for inj in self._session.memory_injections 
                    if not inj.startswith("[MIDUM PROJECT MEMORY")
                ]
                self._session.memory_injections.append(
                    f"[MIDUM PROJECT MEMORY — {selected_project}]\n{content}"
                )
                
                # Rebuild current live session context
                self._session.history = [
                    msg for msg in self._session.history 
                    if not (msg.get("role") == "system" and msg.get("content", "").startswith("[MIDUM PROJECT MEMORY"))
                ]
                self._session.history.append({
                    "role": "system",
                    "content": f"[MIDUM PROJECT MEMORY — {selected_project}]\n{content}"
                })
        except Exception as e:
            self._activity_append(f"⚠️ Context injection failure: {e}\n")

        # Log updates
        midum.memory.update_memory("master", f"Active project context switched to: {selected_project} ({project_dir})")
        self._chat_append("system", f"[Workspace context switched to: {selected_project}]\n")
        
        self._set_status("Ready", C["green"])
        self._refresh_status()
        self._refresh_file_list(project_dir)

        # Force active load if dropdown is currently viewing "Active Project"
        if self._sys_core_active_file == midum.memory._active_project_memory_path or self._sys_core_dropdown.get() == "Active Project":
            self._on_sys_core_selected("Active Project")

    def _refresh_file_list(self, directory: str):
        """Displays direct physical folders and files contained in the active workspace sidebar."""
        self._file_list_box.configure(state="normal")
        self._file_list_box.delete("1.0", "end")
        
        try:
            if os.path.exists(directory):
                files = os.listdir(directory)
                files.sort(key=lambda x: os.path.isdir(os.path.join(directory, x)), reverse=True)
                
                self._file_list_box.insert("end", f"📁 {os.path.basename(directory)}\n")
                for f in files:
                    icon = "📁 " if os.path.isdir(os.path.join(directory, f)) else "📄 "
                    self._file_list_box.insert("end", f"  {icon}{f}\n")
            else:
                self._file_list_box.insert("end", "Empty Directory Context")
        except Exception as e:
            self._file_list_box.insert("end", f"Error scanning files: {e}")
            
        self._file_list_box.configure(state="disabled")

    def _create_project_dialog(self):
        """Launches a sleek, non-blocking window input dialog to generate a new project context."""
        dialog = ctk.CTkInputDialog(text="Enter new Project/Workspace name:", title="✚ New Project")
        p_name = dialog.get_input()
        
        if p_name and p_name.strip():
            clean_name = p_name.strip()
            project_dir = os.path.join(self._base_work_dir, clean_name)
            
            if os.path.exists(project_dir):
                messagebox.showerror("Conflict", "A directory with this name already exists inside current base path.")
                return
                
            os.makedirs(project_dir, exist_ok=True)
            project_file = os.path.join(project_dir, "project_memory.md")
            midum.write_local_file(
                project_file,
                f"# Project Memory: {clean_name}\n"
                f"Created: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"Base Execution context initialized successfully.\n"
            )
            
            # Recalibrate workspace directory values
            self._scan_workspace_directory()
            self._project_dropdown.set(clean_name)
            self._on_project_switched(clean_name)

    def _change_base_work_directory(self):
        """Modifies physical folder context pointing the base scanning workspace."""
        new_dir = filedialog.askdirectory(title="Select Base Scan Workspace Directory")
        if new_dir:
            self._base_work_dir = os.path.abspath(new_dir)
            self._chat_append("system", f"[Base scan directory moved to: {self._base_work_dir}]\n")
            self._scan_workspace_directory()

    def _open_project_in_vscode(self):
        """Shortcut action that directly deploys the active workspace inside Visual Studio Code."""
        proj = midum.memory._active_project_memory_path
        if proj:
            dir_path = os.path.dirname(proj)
            try:
                subprocess.Popen(f'code "{dir_path}"', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._activity_append(f"⚙️ VS Code deployed on: {dir_path}\n")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to execute VS Code command alias: {e}")
        else:
            messagebox.showwarning("Context Missing", "No active workspace is selected.")

    def _open_project_terminal(self):
        """Shortcut command launching a detached PowerShell window directly focused on the active workspace."""
        proj = midum.memory._active_project_memory_path
        if proj:
            dir_path = os.path.dirname(proj)
            try:
                subprocess.Popen(f'powershell -NoExit -Command "cd \'{dir_path}\'"', shell=True)
                self._activity_append(f"⚙️ PowerShell launched focused on: {dir_path}\n")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to launch shell console: {e}")
        else:
            messagebox.showwarning("Context Missing", "No active workspace is selected.")

    # ──────────────────────────────────────────────────────────────────────────
    # SEND & RECEIVE INTERACTIVE MANAGEMENT
    # ──────────────────────────────────────────────────────────────────────────
    def _send(self, event=None):
        if self._thinking:
            return

        user_input = self._input.get().strip()
        if not user_input:
            return

        self._input.delete(0, "end")
        self._chat_append("user", user_input)

        # Format execution instruction payload
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

        # Launch background engine thread
        self._thinking = True
        self._set_status("Executing turns...", C["yellow"])
        self._send_btn.configure(state="disabled")
        self._abort_btn.configure(fg_color=C["red"])

        t = threading.Thread(
            target=self._run_turn,
            args=(list(self._session.snapshot()),),
            daemon=True
        )
        t.start()

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

            self._reply_queue.put(("ok", reply, tool_outputs))
        except Exception as e:
            self._reply_queue.put(("err", str(e), []))

    # ──────────────────────────────────────────────────────────────────────────
    # MAIN THREAD POLLING & DYNAMIC REFRESH LOOP (Every 80 ms)
    # ──────────────────────────────────────────────────────────────────────────
    def _poll(self):
        # Flush standard output stream lines to UI Activity monitor,
        # mirroring say() narration and major tool/system events live
        # into the main chat as well.
        from gui.dispatch import _is_tool_line
        while not self._log_queue.empty():
            try:
                line = self._log_queue.get_nowait()
            except queue.Empty:
                break

            if line.startswith(_SAY_TAG):
                say_text = line[len(_SAY_TAG):]
                self._chat_append_say(say_text)
                self._activity_append(f"💬 [say]: {say_text}\n")
                continue

            self._activity_append(line)
            if _is_tool_line(line):
                self._chat_append_tool(line.strip())

        # Render responses when tool execution finishes
        if not self._reply_queue.empty():
            try:
                status, reply, tool_outputs = self._reply_queue.get_nowait()
            except queue.Empty:
                status = None

            if status == "ok":
                cleaned_reply, visuals = self._extract_and_strip_visuals(reply, tool_outputs)
                if cleaned_reply:
                    self._chat_append("midum", cleaned_reply)
                for lang, body in visuals:
                    self._chat_append("midum", f"```{lang}\n{body}\n```")
                self._set_status("Ready", C["green"])
                
                # Dynamic memory updates
                threading.Thread(
                    target=midum.python_trigger_memory_update,
                    args=(tool_outputs, reply),
                    daemon=True
                ).start()
                
                # Auto-refresh active loaded boxes representing state files on disk
                if hasattr(self, "_sys_core_active_file") and self._sys_core_active_file:
                    self._load_file_into_box(self._sys_core_box, self._sys_core_active_file)
                if hasattr(self, "_knowledge_active_file") and self._knowledge_active_file:
                    self._load_file_into_box(self._knowledge_box, self._knowledge_active_file)
                if hasattr(self, "_skills_active_file") and self._skills_active_file:
                    self._load_file_into_box(self._skills_box, self._skills_active_file)
                
                proj = midum.memory._active_project_memory_path
                if proj:
                    self._refresh_file_list(os.path.dirname(proj))
                
                self._refresh_status()

            elif status == "err":
                self._chat_append("error", f"[Engine error: {reply}]\n")
                self._set_status("Error Exception", C["red"])

            self._thinking = False
            self._send_btn.configure(state="normal")
            self._abort_btn.configure(fg_color=C["red"]) # Reset abort color if successful

        self.after(80, self._poll)

    # ──────────────────────────────────────────────────────────────────────────
    # CORE CONTROLLER ACTIONS
    # ──────────────────────────────────────────────────────────────────────────
    def _abort(self):
        midum._abort_event.set()
        self._set_status("Aborted", C["red"])
        self._activity_append("🛑 Execution pipeline aborted by user (Ctrl+Q)\n")

    def _new_session(self):
        if self._thinking:
            messagebox.showwarning("Active Session Execution", "Please wait for current run to finish or click Abort first.")
            return
        if not messagebox.askyesno("Confirm Clear", "Clear current session context and reset memories?"):
            return

        try:
            if os.path.exists(midum.SESSION_MEMORY):
                os.remove(midum.SESSION_MEMORY)
            midum.memory._current_goal = None
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            midum.write_local_file(
                midum.SESSION_MEMORY,
                f"# Midum Session Memory\nSession started: {ts}\n\n"
                f"{midum.GOAL_SECTION_HEADER}\n_No active goal._\n\n"
                f"{midum.GOAL_SECTION_END}\n"
            )
            self._session.reset()
            self._clear_chat()
            self._start_new_chat_record()
            self._chat_append("system", "[Session wiped, starting fresh context]\n")
            self._refresh_status()
            self._set_status("Ready", C["green"])
        except Exception as e:
            messagebox.showerror("Error resetting session", str(e))

    def _shutdown_engine(self):
        """Safely stops model threads, flushes logging streams, and terminates application gracefully."""
        if self._thinking:
            if not messagebox.askyesno("Engine Busy", "A processing cycle is currently executing. Force shut down?"):
                return
            midum._abort_event.set()
        
        self._activity_append("🔌 Shutting down Midum Engine Core...\n")
        self._set_status("Shutting Down", C["red"])
        self.after(500, self._complete_shutdown)

    def _complete_shutdown(self):
        # Restore stream handlers cleanly
        self._stdout_redir.restore()
        self.destroy()

    # ──────────────────────────────────────────────────────────────────────────
    # DASHBOARD HELPERS & RENDERERS
    # ──────────────────────────────────────────────────────────────────────────
    def _refresh_status(self):
        from gui.dialogs import _PROVIDER_KEY_TO_LABEL
        _prov_label = _PROVIDER_KEY_TO_LABEL.get(self._selected_provider, self._selected_provider)
        self._lbl_model.configure(text=f"{_prov_label} — {self._selected_model or '(auto)'}")
        self._lbl_goal.configure(
            text=midum.memory._current_goal or "None active",
            text_color=C["accent"] if midum.memory._current_goal else C["subtext"]
        )
        proj = midum.memory._active_project_memory_path
        self._lbl_project.configure(
            text=os.path.dirname(proj) if proj else "No project selected",
            text_color=C["green"] if proj else C["subtext"]
        )
        self._lbl_gemini.configure(
            text="✅ System Connected" if midum.providers_gemini_reasoning._GEMINI_AVAILABLE else "⚠️  Unconnected",
            text_color=C["green"] if midum.providers_gemini_reasoning._GEMINI_AVAILABLE else C["yellow"]
        )
        self._lbl_ocr.configure(
            text="✅ System Connected" if midum._TESSERACT_AVAILABLE else "⚠️  Unconnected",
            text_color=C["green"] if midum._TESSERACT_AVAILABLE else C["yellow"]
        )
        self._lbl_uia.configure(
            text="✅ System Connected" if midum._UIA_AVAILABLE else "⚠️  Unconnected",
            text_color=C["green"] if midum._UIA_AVAILABLE else C["yellow"]
        )
        self._lbl_turns.configure(
            text=str(self._session.turn_counter)
        )

    def _refresh_skills(self):
        self._refresh_skills_dropdown()

    def _load_file_into_box(self, box, filepath):
        box.configure(state="normal")
        box.delete("1.0", "end")
        try:
            if filepath and os.path.exists(filepath):
                content = open(filepath, encoding="utf-8").read()
                box.insert("end", content)
            else:
                box.insert("end", f"(File empty or pending setup on disk)")
        except Exception as e:
            box.insert("end", f"Read Error: {e}")

    def _save_box_to_file(self, box, filepath):
        if not filepath:
            messagebox.showwarning("Save Stopped", "No valid file target path is resolved.")
            return
        content = box.get("1.0", "end-1c") # Remove trailing newline added by tkinter
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            self._activity_append(f"💾 Updated context file: {filepath}\n")
        except Exception as e:
            messagebox.showerror("Failed to write parameters to disk", str(e))

    # ──────────────────────────────────────────────────────────────────────────
    # CORE INTERACTIVE GRAPHICAL HELPERS & RICH MARKDOWN RENDERING
    # ──────────────────────────────────────────────────────────────────────────
    # ─────────────────────────────────────────────────────────────────────────
    # FULL MARKDOWN RENDERER
    # Supports: H1-H6, bold, italic, bold-italic, strikethrough, inline code,
    #   fenced code blocks (with lang label), blockquotes, unordered/ordered
    #   lists (nested), horizontal rules, tables, links, paragraph spacing.
    # ─────────────────────────────────────────────────────────────────────────

    def _md_setup_tags(self, tb: tk.Text) -> None:
        """Register every tag the markdown renderer may use on a tk.Text widget."""
        bg = tb["background"]
        tb.tag_config("h1",          font=("Segoe UI", 22, "bold"),   foreground=C["text"])
        tb.tag_config("h2",          font=("Segoe UI", 18, "bold"),   foreground=C["text"])
        tb.tag_config("h3",          font=("Segoe UI", 15, "bold"),   foreground=C["text"])
        tb.tag_config("h4",          font=("Segoe UI", 13, "bold"),   foreground=C["subtext"])
        tb.tag_config("h5",          font=("Segoe UI", 12, "bold"),   foreground=C["subtext"])
        tb.tag_config("h6",          font=("Segoe UI", 11, "bold"),   foreground=C["subtext"])
        tb.tag_config("bold",        font=("Segoe UI", 12, "bold"),   foreground=C["text"])
        tb.tag_config("italic",      font=("Segoe UI", 12, "italic"), foreground=C["text"])
        tb.tag_config("bold_italic", font=("Segoe UI", 12, "bold italic"), foreground=C["text"])
        tb.tag_config("strike",      font=("Segoe UI", 12),           foreground=C["subtext"],
                      overstrike=True)
        tb.tag_config("inline_code", font=FONT_MONO,                  foreground=C["tool_text"],
                      background=C["surface2"])
        tb.tag_config("code_lang",   font=("Segoe UI", 9, "bold"),    foreground=C["subtext"],
                      background=C["tool_bg"])
        tb.tag_config("code_body",   font=FONT_MONO,                  foreground=C["tool_text"],
                      background=C["tool_bg"])
        tb.tag_config("code_rule",   font=("Segoe UI", 6),            foreground=C["border2"],
                      background=C["tool_bg"])
        tb.tag_config("blockquote",  font=("Segoe UI", 12, "italic"), foreground=C["subtext"],
                      lmargin1=16, lmargin2=16)
        tb.tag_config("bq_bar",      background=C["accent"], foreground=C["accent"])
        tb.tag_config("ul_bullet",   foreground=C["accent"],           font=("Segoe UI", 14, "bold"))
        tb.tag_config("ol_num",      foreground=C["accent"],           font=("Segoe UI", 12, "bold"))
        tb.tag_config("list_body",   font=FONT_BODY,                   foreground=C["text"],
                      lmargin1=8, lmargin2=24)
        tb.tag_config("hr",          font=("Segoe UI", 4),            foreground=C["border2"],
                      background=C["border2"])
        tb.tag_config("th",          font=("Segoe UI", 12, "bold"),   foreground=C["text"],
                      background=C["surface2"])
        tb.tag_config("td",          font=FONT_BODY,                   foreground=C["text"])
        tb.tag_config("td_alt",      font=FONT_BODY,                   foreground=C["text"],
                      background=C["surface"])
        tb.tag_config("link",        font=FONT_BODY,                   foreground=C["accent"],
                      underline=True)
        tb.tag_config("normal",      font=FONT_BODY,                   foreground=C["text"])

        # ── Flowchart diagram tags ──────────────────────────────────────────
        tb.tag_config("fc_title",    font=("Segoe UI", 13, "bold"),   foreground=C["text"])
        tb.tag_config("fc_rule",     font=FONT_MONO,                  foreground=C["border2"])
        tb.tag_config("fc_arrow",    font=FONT_MONO,                  foreground=C["subtext"])
        tb.tag_config("fc_edge_lbl", font=("Segoe UI", 10, "italic"), foreground=C["subtext"])
        tb.tag_config("fc_node_start", font=FONT_MONO, foreground=C["green"],   background=C["surface"])
        tb.tag_config("fc_node_end",   font=FONT_MONO, foreground=C["red"],     background=C["surface"])
        tb.tag_config("fc_node_decision", font=FONT_MONO, foreground=C["yellow"], background=C["surface"])
        tb.tag_config("fc_node_io",    font=FONT_MONO, foreground=C["accent2"], background=C["surface"])
        tb.tag_config("fc_node_process", font=FONT_MONO, foreground=C["accent"], background=C["surface"])
        tb.tag_config("fc_node_type",  font=("Segoe UI", 9, "italic"), foreground=C["subtext"],
                      background=C["surface"])

    def _md_inline(self, tb: tk.Text, text: str, base_tag: str = "normal") -> None:
        """
        Insert inline-formatted text into tb, handling:
        ***bold italic***, **bold**, *italic*, ~~strike~~, `code`, [link](url)
        in a single left-to-right pass.
        """
        # Pattern captures each inline token in priority order
        INLINE = re.compile(
            r"(\*\*\*(.+?)\*\*\*"          # ***bold italic***
            r"|\*\*(.+?)\*\*"              # **bold**
            r"|\*(.+?)\*"                  # *italic*
            r"|___(.+?)___"                # ___bold italic___
            r"|__(.+?)__"                  # __bold__
            r"|_(.+?)_"                    # _italic_
            r"|~~(.+?)~~"                  # ~~strike~~
            r"|`(.+?)`"                    # `code`
            r"|\[([^\]]+)\]\(([^)]+)\)"   # [text](url)
            r")",
            re.DOTALL
        )
        pos = 0
        for m in INLINE.finditer(text):
            # Plain text before this match
            if m.start() > pos:
                tb.insert("end", text[pos:m.start()], base_tag)
            full = m.group(0)
            if full.startswith("***") or full.startswith("___"):
                tb.insert("end", m.group(2) or m.group(5), "bold_italic")
            elif full.startswith("**") or full.startswith("__"):
                tb.insert("end", m.group(3) or m.group(6), "bold")
            elif full.startswith("~~"):
                tb.insert("end", m.group(8), "strike")
            elif full.startswith("*") or full.startswith("_"):
                tb.insert("end", m.group(4) or m.group(7), "italic")
            elif full.startswith("`"):
                tb.insert("end", m.group(9), "inline_code")
            elif full.startswith("["):
                link_text, url = m.group(10), m.group(11)
                tag_name = f"link_{id(url)}"
                tb.tag_config(tag_name, font=FONT_BODY, foreground=C["accent"], underline=True)
                tb.tag_bind(tag_name, "<Button-1>",
                            lambda e, u=url: __import__("webbrowser").open(u))
                tb.tag_bind(tag_name, "<Enter>",
                            lambda e, t=tb, n=tag_name: t.tag_config(n, foreground=C["accent_dim"]))
                tb.tag_bind(tag_name, "<Leave>",
                            lambda e, t=tb, n=tag_name: t.tag_config(n, foreground=C["accent"]))
                tb.insert("end", link_text, (tag_name,))
            pos = m.end()
        # Remaining plain text
        if pos < len(text):
            tb.insert("end", text[pos:], base_tag)

    def _copy_image_bytes_to_clipboard(self, png_bytes: bytes) -> tuple[bool, str]:
        """
        Copy raw image bytes to the SYSTEM clipboard (so it can be pasted
        into other apps as an actual image, not just a file path).
        Tries the platform-appropriate mechanism; returns (ok, message).
        """
        try:
            if sys.platform.startswith("win"):
                import win32clipboard  # pywin32
                from PIL import Image as _Img
                bmp = io.BytesIO()
                _Img.open(io.BytesIO(png_bytes)).convert("RGB").save(bmp, "BMP")
                data = bmp.getvalue()[14:]  # strip BMP file header -> DIB
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
                win32clipboard.CloseClipboard()
                return True, "Image copied to clipboard."
            else:
                # Linux (X11): requires xclip. Wayland users: wl-copy.
                for cmd in (["xclip", "-selection", "clipboard", "-t", "image/png"],
                            ["wl-copy", "--type", "image/png"]):
                    try:
                        subprocess.run(cmd, input=png_bytes, check=True,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        return True, "Image copied to clipboard."
                    except (FileNotFoundError, subprocess.CalledProcessError):
                        continue
                return False, "Clipboard copy needs 'xclip' (X11) or 'wl-copy' (Wayland) installed."
        except ImportError:
            return False, "Clipboard copy on Windows needs pywin32: pip install pywin32"
        except Exception as e:
            return False, f"Clipboard copy failed: {e}"

    def _render_image_gallery(self, tb: tk.Text, payload: dict) -> None:
        """
        Render the images produced by main.py's generate_image tool inline
        in the chat, entirely from base64 bytes kept in memory — nothing is
        read from or written to disk unless the user clicks Download.
        payload = {"prompt": str, "images": [{"filename": str, "data_b64": str}, ...]}
        """
        prompt = payload.get("prompt", "")
        images = payload.get("images", []) or []

        tb.insert("end", f"\n🖼️  Generated image(s)", "fc_title")
        if prompt:
            tb.insert("end", f"  —  \"{prompt}\"\n", "fc_edge_lbl")
        else:
            tb.insert("end", "\n")

        if not _PIL_AVAILABLE:
            tb.insert("end", "(Install Pillow — pip install pillow — to preview images inline.)\n",
                      "fc_edge_lbl")

        # Keep CTkImage/PhotoImage references alive for the widget's lifetime.
        if not hasattr(self, "_image_refs"):
            self._image_refs = []

        for img_entry in images:
            filename = img_entry.get("filename") or "image.png"
            b64      = img_entry.get("data_b64", "")
            try:
                raw_bytes = base64.b64decode(b64)
            except Exception:
                tb.insert("end", f"⚠ could not decode image data for {filename}\n", "fc_edge_lbl")
                continue

            if _PIL_AVAILABLE:
                try:
                    pil_img = _PILImage.open(io.BytesIO(raw_bytes))
                    pil_img.load()
                    thumb = pil_img.copy()
                    thumb.thumbnail((320, 320))
                    ctk_img = ctk.CTkImage(light_image=thumb, dark_image=thumb, size=thumb.size)
                    self._image_refs.append(ctk_img)

                    frame = ctk.CTkFrame(tb, fg_color=C["surface"], border_width=1,
                                          border_color=C["border2"])
                    img_lbl = ctk.CTkLabel(frame, image=ctk_img, text="")
                    img_lbl.pack(padx=6, pady=(6, 4))

                    btn_row = ctk.CTkFrame(frame, fg_color="transparent")
                    btn_row.pack(padx=6, pady=(0, 6), fill="x")

                    dl_btn = ctk.CTkButton(
                        btn_row, text="⬇ Download", width=100, height=26,
                        fg_color=C["accent"], hover_color=C["accent_dim"],
                        font=FONT_SMALL,
                        command=lambda b=raw_bytes, fn=filename: self._download_image_bytes(b, fn),
                    )
                    dl_btn.pack(side="left", padx=(0, 6))

                    cp_btn = ctk.CTkButton(
                        btn_row, text="⧉ Copy", width=90, height=26,
                        fg_color=C["surface2"], hover_color=C["border2"],
                        font=FONT_SMALL,
                        command=lambda b=raw_bytes: self._copy_image_button_clicked(b),
                    )
                    cp_btn.pack(side="left")

                    tb.insert("end", "\n")
                    tb.window_create("end", window=frame, padx=4, pady=4)
                    tb.insert("end", "\n")
                    tb.insert("end", f"{filename}  ({len(raw_bytes)/1024:.0f} KB, in-memory only)\n",
                              "fc_node_type")
                    continue
                except Exception:
                    pass  # fall through to the no-preview branch below

            # No Pillow, or thumbnailing failed — still offer Download/Copy as text-row actions.
            row = ctk.CTkFrame(tb, fg_color=C["surface"], border_width=1, border_color=C["border2"])
            ctk.CTkLabel(row, text=f"📄 {filename} ({len(raw_bytes)/1024:.0f} KB)",
                         font=FONT_SMALL, text_color=C["text"]).pack(side="left", padx=8, pady=6)
            ctk.CTkButton(row, text="⬇ Download", width=100, height=26,
                          command=lambda b=raw_bytes, fn=filename: self._download_image_bytes(b, fn)
                          ).pack(side="left", padx=(4, 4), pady=6)
            ctk.CTkButton(row, text="⧉ Copy", width=90, height=26,
                          command=lambda b=raw_bytes: self._copy_image_button_clicked(b)
                          ).pack(side="left", padx=(0, 8), pady=6)
            tb.insert("end", "\n")
            tb.window_create("end", window=row, padx=4, pady=4)
            tb.insert("end", "\n")

        tb.insert("end", "─" * 46 + "\n", "fc_rule")

    def _download_image_bytes(self, raw_bytes: bytes, suggested_name: str) -> None:
        """Save in-memory image bytes to disk only when the user explicitly asks."""
        path = filedialog.asksaveasfilename(
            defaultextension=".png",
            initialfile=suggested_name,
            filetypes=[("PNG image", "*.png"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "wb") as f:
                f.write(raw_bytes)
            messagebox.showinfo("Saved", f"Image saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def _copy_image_button_clicked(self, raw_bytes: bytes) -> None:
        ok, msg = self._copy_image_bytes_to_clipboard(raw_bytes)
        if ok:
            messagebox.showinfo("Copied", msg)
        else:
            messagebox.showwarning("Copy unavailable", msg)

    def _render_flowchart_diagram(self, tb: tk.Text, payload: dict) -> None:
        """
        Render a structured flowchart (produced by main.py's create_flowchart tool)
        as an indented box-and-arrow diagram directly inside the chat Text widget.
        payload = {"title": str, "starts": [id,...], "nodes": [{id,label,type,next:[{to,label}]}]}
        """
        node_tag = {
            "start":    "fc_node_start",
            "end":      "fc_node_end",
            "decision": "fc_node_decision",
            "io":       "fc_node_io",
        }
        nodes = {n["id"]: n for n in payload.get("nodes", [])}
        starts = payload.get("starts") or (list(nodes.keys())[:1])
        title = payload.get("title", "Flowchart")

        tb.insert("end", f"\n📊  {title}\n", "fc_title")
        tb.insert("end", "─" * 46 + "\n", "fc_rule")

        visited = set()

        def draw_node(nid, indent):
            pad = "   " * indent
            node = nodes.get(nid)
            if node is None:
                tb.insert("end", f"{pad}⚠ missing node '{nid}'\n", "fc_edge_lbl")
                return
            if nid in visited:
                tb.insert("end", f"{pad}↩ back to: ", "fc_arrow")
                tb.insert("end", f" {node['label']} \n", node_tag.get(node.get("type"), "fc_node_process"))
                return
            visited.add(nid)

            tag = node_tag.get(node.get("type"), "fc_node_process")
            label = node.get("label", nid)
            tb.insert("end", pad)
            tb.insert("end", f" {label} ", tag)
            tb.insert("end", f"  [{node.get('type','process')}]\n", "fc_node_type")

            nexts = node.get("next") or []
            multi = len(nexts) > 1
            for edge in nexts:
                to = edge.get("to") if isinstance(edge, dict) else edge
                lbl = edge.get("label") if isinstance(edge, dict) else None
                tb.insert("end", pad + "   │\n", "fc_arrow")
                arrow_line = pad + "   ▼"
                tb.insert("end", arrow_line, "fc_arrow")
                if lbl:
                    tb.insert("end", f"  ({lbl})", "fc_edge_lbl")
                tb.insert("end", "\n")
                draw_node(to, indent + (1 if multi else 0))

        for sid in starts:
            draw_node(sid, 0)
            tb.insert("end", "\n")

        unreached = [nid for nid in nodes if nid not in visited]
        if unreached:
            tb.insert("end", "Not reachable from a start node:\n", "fc_edge_lbl")
            for nid in unreached:
                draw_node(nid, 0)

        tb.insert("end", "─" * 46 + "\n", "fc_rule")

    def _render_markdown(self, tb: tk.Text, raw: str) -> None:
        """
        Full block-level markdown parser → renders into tb.
        Blocks handled: headings, fenced code, blockquotes, HR, tables,
        unordered/ordered lists (with nesting), and paragraphs.
        Each block may contain inline markdown.
        """
        self._md_setup_tags(tb)

        lines = raw.splitlines()
        i = 0
        n = len(lines)

        def nl(extra: str = "") -> None:
            tb.insert("end", "\n" + extra)

        while i < n:
            line = lines[i]

            # ── Blank line → paragraph gap ────────────────────────────────────
            if line.strip() == "":
                tb.insert("end", "\n")
                i += 1
                continue

            # ── Fenced code block ─────────────────────────────────────────────
            fence = re.match(r"^(`{3,}|~{3,})(.*)", line)
            if fence:
                fence_char, lang = fence.group(1), fence.group(2).strip()
                i += 1
                if lang == "flowchart_json":
                    body_lines = []
                    while i < n and not lines[i].startswith(fence_char):
                        body_lines.append(lines[i])
                        i += 1
                    i += 1  # closing fence
                    try:
                        payload = json.loads("\n".join(body_lines))
                        self._render_flowchart_diagram(tb, payload)
                    except Exception:
                        # Malformed payload — fall back to plain code display
                        tb.insert("end", "─" * 62 + "\n", "code_rule")
                        for bl in body_lines:
                            tb.insert("end", bl + "\n", "code_body")
                        tb.insert("end", "─" * 62 + "\n", "code_rule")
                    continue
                if lang == "image_data_json":
                    body_lines = []
                    while i < n and not lines[i].startswith(fence_char):
                        body_lines.append(lines[i])
                        i += 1
                    i += 1  # closing fence
                    try:
                        payload = json.loads("\n".join(body_lines))
                        self._render_image_gallery(tb, payload)
                    except Exception:
                        tb.insert("end", "─" * 62 + "\n", "code_rule")
                        for bl in body_lines:
                            tb.insert("end", bl + "\n", "code_body")
                        tb.insert("end", "─" * 62 + "\n", "code_rule")
                    continue
                if lang:
                    tb.insert("end", f" {lang} \n", "code_lang")
                tb.insert("end", "─" * 62 + "\n", "code_rule")
                while i < n and not lines[i].startswith(fence_char):
                    tb.insert("end", lines[i] + "\n", "code_body")
                    i += 1
                tb.insert("end", "─" * 62 + "\n", "code_rule")
                i += 1  # closing fence
                continue

            # ── Heading (ATX: # … ######) ─────────────────────────────────────
            m = re.match(r"^(#{1,6})\s+(.*)", line)
            if m:
                level = len(m.group(1))
                htag = f"h{level}"
                self._md_inline(tb, m.group(2).strip(), htag)
                nl()
                i += 1
                continue

            # ── Setext headings (underline with === or ---) ───────────────────
            if i + 1 < n:
                under = lines[i + 1].strip()
                if re.match(r"^=+$", under):
                    self._md_inline(tb, line.strip(), "h1")
                    nl(); i += 2; continue
                if re.match(r"^-+$", under) and len(under) >= 2:
                    self._md_inline(tb, line.strip(), "h2")
                    nl(); i += 2; continue

            # ── Horizontal rule ───────────────────────────────────────────────
            if re.match(r"^\s*(\*{3,}|-{3,}|_{3,})\s*$", line):
                tb.insert("end", "\n" + "─" * 64 + "\n\n", "hr")
                i += 1
                continue

            # ── Blockquote ────────────────────────────────────────────────────
            if line.startswith(">"):
                # Collect all consecutive blockquote lines
                bq_lines = []
                while i < n and lines[i].startswith(">"):
                    bq_lines.append(lines[i].lstrip(">").lstrip(" "))
                    i += 1
                tb.insert("end", "▌ ", "bq_bar")
                self._md_inline(tb, " ".join(bq_lines), "blockquote")
                nl()
                continue

            # ── Table (pipe syntax) ───────────────────────────────────────────
            if "|" in line and i + 1 < n and re.match(r"^\|?[\s\-|:]+\|", lines[i + 1]):
                # Parse header row
                header_cells = [c.strip() for c in line.strip().strip("|").split("|")]
                i += 2  # skip separator row
                # Calculate equal column width
                col_w = max(12, 64 // max(len(header_cells), 1))
                # Header
                for ci, cell in enumerate(header_cells):
                    tb.insert("end", f" {cell:<{col_w}} ", "th")
                    if ci < len(header_cells) - 1:
                        tb.insert("end", "│", "th")
                nl()
                tb.insert("end", "─" * 64 + "\n", "code_rule")
                # Data rows
                row_idx = 0
                while i < n and "|" in lines[i]:
                    cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                    row_tag = "td" if row_idx % 2 == 0 else "td_alt"
                    for ci, cell in enumerate(cells):
                        self._md_inline(tb, f" {cell:<{col_w}} ", row_tag)
                        if ci < len(cells) - 1:
                            tb.insert("end", "│", row_tag)
                    nl()
                    row_idx += 1
                    i += 1
                nl()
                continue

            # ── Unordered list ────────────────────────────────────────────────
            ul_m = re.match(r"^(\s*)([-*+])\s+(.*)", line)
            if ul_m:
                indent = len(ul_m.group(1))
                bullet = "•" if indent == 0 else ("◦" if indent <= 4 else "▪")
                tb.insert("end", "  " * (indent // 2) + f"{bullet} ", "ul_bullet")
                self._md_inline(tb, ul_m.group(3), "list_body")
                nl()
                i += 1
                continue

            # ── Ordered list ──────────────────────────────────────────────────
            ol_m = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", line)
            if ol_m:
                indent = len(ol_m.group(1))
                tb.insert("end", "  " * (indent // 2) + f"{ol_m.group(2)}. ", "ol_num")
                self._md_inline(tb, ol_m.group(3), "list_body")
                nl()
                i += 1
                continue

            # ── Normal paragraph line ─────────────────────────────────────────
            self._md_inline(tb, line, "normal")
            # Soft-wrap: if next non-empty line is also a plain paragraph, join
            if i + 1 < n and lines[i + 1].strip() != "" \
                    and not re.match(r"^[#>`\-*+|~\d]", lines[i + 1]) \
                    and not re.match(r"^(`{3}|~{3})", lines[i + 1]):
                tb.insert("end", " ")
            else:
                nl()
            i += 1

    # ─────────────────────────────────────────────────────────────────────────
    # LIVE MIRRORING — tool/system ticker lines & say() narration
    # ─────────────────────────────────────────────────────────────────────────
    def _chat_append_tool(self, text: str):
        """
        Render a tool-call or major system event as a compact grey ticker
        line directly in the main chat, using one shared icon for every
        kind of tool/system event (execution, memory, goals, clicks, etc).
        """
        if not text:
            return
        if not self._replaying_chat:
            self._display_log.append(("tool", text))
        self._chat_scroll.update_idletasks()

        row = ctk.CTkFrame(self._chat_scroll, fg_color="transparent")
        row.grid(row=self._chat_row, column=1, sticky="ew", pady=(1, 1))
        self._chat_row += 1

        inner = ctk.CTkFrame(row, fg_color="transparent")
        inner.pack(anchor="w", fill="x", padx=2)

        ctk.CTkLabel(
            inner, text="⚙", font=("Segoe UI", 11), text_color=C["muted"], width=14
        ).pack(side="left", padx=(0, 6))

        ctk.CTkLabel(
            inner, text=text, font=("Segoe UI", 10), text_color=C["subtext"],
            justify="left", anchor="w", wraplength=720,
        ).pack(side="left", fill="x", expand=True)

        self.after(30, self._scroll_to_bottom)

    def _chat_append_say(self, text: str):
        """
        Render a live say()-tool narration from the model as an interim
        Midum message in the main chat, styled like a normal reply but
        marked with a 💬 icon so it reads as "thinking out loud" rather
        than the final answer.
        """
        if not text:
            return
        self._chat_scroll.update_idletasks()

        row_frame = ctk.CTkFrame(self._chat_scroll, fg_color="transparent")
        row_frame.grid(row=self._chat_row, column=1, sticky="ew", pady=(8, 2))
        self._chat_row += 1
        row_frame.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(row_frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="w", pady=(0, 3))
        ctk.CTkLabel(
            header, text="💬", font=("Segoe UI", 12), text_color=C["accent"]
        ).pack(side="left", padx=(2, 5))
        ctk.CTkLabel(
            header, text="Midum", font=FONT_BOLD, text_color=C["accent"]
        ).pack(side="left")

        tb = tk.Text(
            row_frame,
            font=FONT_BODY, bg=C["bg"], fg=C["text"],
            bd=0, highlightthickness=0, wrap="word",
            padx=2, pady=2, width=68,
            insertbackground=C["text"], relief="flat",
            cursor="arrow",
        )
        tb.grid(row=1, column=0, sticky="ew")

        self._render_markdown(tb, text)

        if tb.get("end-2c", "end-1c") == "\n":
            tb.delete("end-2c", "end-1c")
        tb.configure(state="disabled")

        tb.update_idletasks()
        try:
            lines_count = tb.count("1.0", "end", "displaylines")[0]
        except Exception:
            lines_count = int(tb.index("end-1c").split(".")[0])
        tb.configure(height=max(1, lines_count))

        self.after(30, self._scroll_to_bottom)

    # Tool outputs that carry a rendered visual (image thumbnails, flowchart
    # diagrams, ...) get pulled straight from the raw tool_output text and
    # rendered directly — we do NOT trust the model's own reply to carry the
    # payload correctly. In practice models often echo the JSON body back
    # but "normalize" our custom fence tag (```flowchart_json```) to a
    # generic one like ```json```, which would otherwise dump raw JSON into
    # the chat with no diagram ever rendering. So: always render visuals
    # from tool_outputs directly, and strip any fenced block from the
    # model's reply whose body matches one of those payloads (regardless of
    # what language tag the model gave it) so it isn't shown twice as ugly
    # raw text next to the real render.
    _VISUAL_FENCE_LANGS = ("image_data_json", "flowchart_json")
    _TOOL_VISUAL_FENCE_RE = re.compile(
        r"```(" + "|".join(_VISUAL_FENCE_LANGS) + r")\n(.*?)```", re.DOTALL
    )
    _ANY_FENCE_RE = re.compile(r"```([\w_]*)\n(.*?)```", re.DOTALL)

    def _extract_and_strip_visuals(self, reply: str, tool_outputs: list) -> tuple[str, list]:
        visuals = []
        seen_bodies = set()
        for tool_output in tool_outputs or []:
            if not isinstance(tool_output, str) or "```" not in tool_output:
                continue
            for lang, body in self._TOOL_VISUAL_FENCE_RE.findall(tool_output):
                body = body.strip()
                if body and body not in seen_bodies:
                    seen_bodies.add(body)
                    visuals.append((lang, body))

        if not visuals:
            return reply, []

        def _strip_if_echoed(m: re.Match) -> str:
            block_body = m.group(2).strip()
            for _, vbody in visuals:
                # Substring match (not exact-equal) so it still catches the
                # payload even if the model trimmed whitespace or added a
                # trailing comment when it re-typed the block.
                if block_body and (block_body in vbody or vbody in block_body):
                    return ""  # drop — the real render replaces it below
            return m.group(0)

        cleaned_reply = self._ANY_FENCE_RE.sub(_strip_if_echoed, reply).strip()
        return cleaned_reply, visuals

    def _render_missed_tool_visuals(self, reply: str, tool_outputs: list) -> None:
        # Kept for compatibility; superseded by _extract_and_strip_visuals,
        # which is now called from _poll() before the reply is even shown.
        pass

    def _chat_append(self, tag: str, text: str):
        """Append a message into the fixed-width center column of the chat scroll."""
        if not self._replaying_chat:
            self._display_log.append((tag, text))
            if tag == "user" and not self._chat_title:
                self._chat_title = text.strip()[:60] or None
            self._persist_current_chat()
        self._chat_scroll.update_idletasks()

        # ── System / error notices ────────────────────────────────────────────
        if tag in ("system", "error"):
            lbl_text = text if tag == "system" else f"[ Engine Error ]\n{text}"
            color = C["subtext"] if tag == "system" else C["red"]
            ctk.CTkLabel(
                self._chat_scroll, text=lbl_text,
                font=FONT_SMALL, text_color=color, justify="center"
            ).grid(row=self._chat_row, column=1, sticky="ew", pady=4)
            self._chat_row += 1
            self.after(50, self._scroll_to_bottom)
            return

        # ── USER bubble — right-aligned rounded card (plain text, no MD) ─────
        if tag == "user":
            row_frame = ctk.CTkFrame(self._chat_scroll, fg_color="transparent")
            row_frame.grid(row=self._chat_row, column=1, sticky="ew", pady=(10, 2))
            self._chat_row += 1
            row_frame.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                row_frame, text="You", font=FONT_BOLD, text_color=C["subtext"]
            ).grid(row=0, column=1, sticky="e", padx=(0, 4), pady=(0, 3))

            bubble = ctk.CTkFrame(row_frame, fg_color=C["user_msg"], corner_radius=18)
            bubble.grid(row=1, column=1, sticky="e")
            ctk.CTkLabel(
                bubble, text=text.strip(), font=FONT_BODY,
                text_color=C["text"], justify="left",
                wraplength=380, anchor="w",
            ).pack(padx=16, pady=10)
            self.after(50, self._scroll_to_bottom)
            return

        # ── MIDUM response — full markdown, no bubble ────────────────────────
        row_frame = ctk.CTkFrame(self._chat_scroll, fg_color="transparent")
        row_frame.grid(row=self._chat_row, column=1, sticky="ew", pady=(10, 4))
        self._chat_row += 1
        row_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            row_frame, text="Midum", font=FONT_BOLD, text_color=C["accent"]
        ).grid(row=0, column=0, sticky="w", padx=(2, 0), pady=(0, 4))

        tb = tk.Text(
            row_frame,
            font=FONT_BODY, bg=C["bg"], fg=C["text"],
            bd=0, highlightthickness=0, wrap="word",
            padx=2, pady=2, width=68,
            insertbackground=C["text"], relief="flat",
            cursor="arrow",
        )
        tb.grid(row=1, column=0, sticky="ew")

        self._render_markdown(tb, text)

        # Trim trailing newline
        if tb.get("end-2c", "end-1c") == "\n":
            tb.delete("end-2c", "end-1c")
        tb.configure(state="disabled")

        # Auto-fit height
        tb.update_idletasks()
        try:
            lines_count = tb.count("1.0", "end", "displaylines")[0]
        except Exception:
            lines_count = int(tb.index("end-1c").split(".")[0])
        tb.configure(height=max(1, lines_count))

        self.after(50, self._scroll_to_bottom)

    # ─────────────────────────────────────────────────────────────────────────
    # INLINE GUI-INTERACTION CARDS — ask_user_text / _file_path / _approval /
    # _choice, rendered mid-turn directly inside the main chat instead of a
    # separate native popup. midum._gui_ask_hook (installed in __init__)
    # calls _handle_gui_ask() from the ENGINE WORKER THREAD; it hands widget
    # construction off to the main thread via self.after() and blocks on a
    # threading.Event until the person responds, then returns the answer
    # back into the running tool call exactly like the original tkinter
    # dialogs did.
    # ─────────────────────────────────────────────────────────────────────────
    def _handle_gui_ask(self, kind: str, payload: dict) -> str:
        done   = threading.Event()
        result = {"value": "[USER CANCELLED]"}
        self.after(0, self._render_inline_ask, kind, payload, done, result)
        done.wait()
        return result["value"]

    def _inline_ask_card(self, icon: str, header: str):
        """Shared card shell for every inline ask_user_* widget."""
        self._chat_scroll.update_idletasks()

        row_frame = ctk.CTkFrame(self._chat_scroll, fg_color="transparent")
        row_frame.grid(row=self._chat_row, column=1, sticky="ew", pady=(10, 4))
        self._chat_row += 1
        row_frame.grid_columnconfigure(0, weight=1)

        hdr = ctk.CTkFrame(row_frame, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="w", pady=(0, 4))
        ctk.CTkLabel(
            hdr, text=icon, font=("Segoe UI", 13), text_color=C["accent2"]
        ).pack(side="left", padx=(2, 5))
        ctk.CTkLabel(
            hdr, text=header, font=FONT_BOLD, text_color=C["accent2"]
        ).pack(side="left")

        card = ctk.CTkFrame(
            row_frame, fg_color=C["surface"], corner_radius=16,
            border_width=1, border_color=C["border2"],
        )
        card.grid(row=1, column=0, sticky="ew")

        self.after(30, self._scroll_to_bottom)
        return row_frame, card

    def _inline_ask_resolve(self, card, footer_text: str, done: threading.Event,
                             result: dict, value: str):
        """Locks in the user's answer, greys the card out, and unblocks the worker thread."""
        result["value"] = value
        for child in list(card.winfo_children()):
            child.destroy()
        ctk.CTkLabel(
            card, text=footer_text, font=FONT_SMALL, text_color=C["subtext"],
            justify="left", anchor="w", wraplength=680,
        ).pack(anchor="w", padx=16, pady=12, fill="x")
        self.after(30, self._scroll_to_bottom)
        done.set()

    def _render_inline_ask(self, kind: str, payload: dict,
                            done: threading.Event, result: dict):
        """MAIN-THREAD ONLY. Builds the correct inline card for `kind`."""

        # ── Free-text prompt ────────────────────────────────────────────────
        if kind == "text":
            title  = payload.get("title") or "Midum needs input"
            prompt = payload.get("prompt", "")
            _, card = self._inline_ask_card("❓", title)

            ctk.CTkLabel(
                card, text=prompt, font=FONT_BODY, text_color=C["text"],
                justify="left", anchor="w", wraplength=680,
            ).pack(anchor="w", padx=16, pady=(14, 10), fill="x")

            entry = ctk.CTkEntry(
                card, font=FONT_BODY, fg_color=C["bg"], text_color=C["text"],
                border_color=C["border2"], corner_radius=16, height=34,
                placeholder_text="Type your answer...",
            )
            entry.pack(padx=16, pady=(0, 10), fill="x")
            entry.focus_set()

            btn_row = ctk.CTkFrame(card, fg_color="transparent")
            btn_row.pack(anchor="e", padx=16, pady=(0, 14))

            def submit(event=None):
                text = entry.get().strip()
                shown = text if text else "[USER SUBMITTED EMPTY TEXT]"
                self._inline_ask_resolve(card, f"❓ {title} → \"{shown}\"", done, result, shown)

            def cancel():
                self._inline_ask_resolve(card, f"❓ {title} → cancelled", done, result, "[USER CANCELLED]")

            entry.bind("<Return>", submit)
            ctk.CTkButton(
                btn_row, text="Cancel", width=80, fg_color="transparent",
                hover_color=C["surface2"], border_width=1, border_color=C["border2"],
                corner_radius=16, command=cancel,
            ).pack(side="left", padx=(0, 8))
            ctk.CTkButton(
                btn_row, text="Submit", width=90, fg_color=C["accent"],
                hover_color=C["accent_dim"], corner_radius=16, command=submit,
            ).pack(side="left")
            return

        # ── Approve / Decline ───────────────────────────────────────────────
        if kind == "approval":
            message = payload.get("message", "")
            details = payload.get("details", "")
            _, card = self._inline_ask_card("⚠", "Midum requests approval")

            ctk.CTkLabel(
                card, text=message, font=FONT_BOLD, text_color=C["text"],
                justify="left", anchor="w", wraplength=680,
            ).pack(anchor="w", padx=16, pady=(14, 4 if details else 14), fill="x")
            if details:
                ctk.CTkLabel(
                    card, text=details, font=FONT_SMALL, text_color=C["subtext"],
                    justify="left", anchor="w", wraplength=680,
                ).pack(anchor="w", padx=16, pady=(0, 14), fill="x")

            btn_row = ctk.CTkFrame(card, fg_color="transparent")
            btn_row.pack(anchor="e", padx=16, pady=(0, 14))

            def decide(verdict):
                icon = "✅" if verdict == "APPROVED" else "❌"
                self._inline_ask_resolve(
                    card, f"⚠ {message} → {icon} {verdict.title()}", done, result, verdict
                )

            ctk.CTkButton(
                btn_row, text="❌ Decline", width=100, fg_color="transparent",
                hover_color="#2d1010", text_color=C["red"], border_width=1,
                border_color="#3f0f0f", corner_radius=16,
                command=lambda: decide("DECLINED"),
            ).pack(side="left", padx=(0, 8))
            ctk.CTkButton(
                btn_row, text="✅ Approve", width=100, fg_color=C["green"],
                hover_color="#0d9668", corner_radius=16,
                command=lambda: decide("APPROVED"),
            ).pack(side="left")
            return

        # ── Multiple choice (+ optional free-text "Other...") ──────────────
        if kind == "choice":
            question     = payload.get("question", "")
            options      = payload.get("options") or []
            allow_custom = payload.get("allow_custom", True)
            _, card = self._inline_ask_card("❓", "Midum has a question")

            ctk.CTkLabel(
                card, text=question, font=FONT_BOLD, text_color=C["text"],
                justify="left", anchor="w", wraplength=680,
            ).pack(anchor="w", padx=16, pady=(14, 10), fill="x")

            def choose(opt):
                self._inline_ask_resolve(card, f"❓ {question} → \"{opt}\"", done, result, opt)

            for opt in options:
                ctk.CTkButton(
                    card, text=opt, anchor="w", fg_color=C["surface2"],
                    hover_color=C["border2"], text_color=C["text"],
                    corner_radius=14, height=32,
                    command=lambda o=opt: choose(o),
                ).pack(padx=16, pady=3, fill="x")

            if allow_custom:
                custom_row = ctk.CTkFrame(card, fg_color="transparent")
                custom_row.pack(padx=16, pady=(8, 14), fill="x")
                custom_entry = ctk.CTkEntry(
                    custom_row, font=FONT_BODY, fg_color=C["bg"], text_color=C["text"],
                    border_color=C["border2"], corner_radius=16, height=32,
                    placeholder_text="Something else...",
                )
                custom_entry.pack(side="left", fill="x", expand=True)

                def submit_custom(event=None):
                    txt = custom_entry.get().strip()
                    if txt:
                        choose(txt)

                custom_entry.bind("<Return>", submit_custom)
                ctk.CTkButton(
                    custom_row, text="Other...", width=80, fg_color=C["accent"],
                    hover_color=C["accent_dim"], corner_radius=16,
                    command=submit_custom,
                ).pack(side="left", padx=(8, 0))
            else:
                ctk.CTkFrame(card, fg_color="transparent", height=6).pack(fill="x")
            return

        # ── File picker ──────────────────────────────────────────────────
        if kind == "file":
            prompt     = payload.get("prompt", "Select a file")
            must_exist = payload.get("must_exist", True)
            _, card = self._inline_ask_card("📁", "Midum needs a file")

            ctk.CTkLabel(
                card, text=prompt, font=FONT_BODY, text_color=C["text"],
                justify="left", anchor="w", wraplength=680,
            ).pack(anchor="w", padx=16, pady=(14, 10), fill="x")

            btn_row = ctk.CTkFrame(card, fg_color="transparent")
            btn_row.pack(anchor="w", padx=16, pady=(0, 14))

            def pick():
                try:
                    if must_exist:
                        path = filedialog.askopenfilename(title=prompt, parent=self)
                    else:
                        path = filedialog.asksaveasfilename(title=prompt, parent=self)
                except Exception:
                    path = ""
                shown = path if path else "[USER CANCELLED]"
                self._inline_ask_resolve(card, f"📁 {prompt} → {shown}", done, result, shown)

            def cancel():
                self._inline_ask_resolve(card, f"📁 {prompt} → cancelled", done, result, "[USER CANCELLED]")

            ctk.CTkButton(
                btn_row, text="Cancel", width=80, fg_color="transparent",
                hover_color=C["surface2"], border_width=1, border_color=C["border2"],
                corner_radius=16, command=cancel,
            ).pack(side="left", padx=(0, 8))
            ctk.CTkButton(
                btn_row, text="Browse...", width=100, fg_color=C["accent"],
                hover_color=C["accent_dim"], corner_radius=16, command=pick,
            ).pack(side="left")
            return

        # ── Unknown kind fallback — never leave the worker thread hanging ──
        result["value"] = "[USER CANCELLED]"
        done.set()

    def _scroll_to_bottom(self):
        """Forces the scroll container layout to slide smoothly to reveal new turns."""
        try:
            if hasattr(self._chat_scroll, "_parent_canvas"):
                self._chat_scroll._parent_canvas.yview_moveto(1.0)
            elif hasattr(self._chat_scroll, "_canvas"):
                self._chat_scroll._canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _clear_chat(self):
        """Removes all message widgets and resets the grid row counter."""
        for widget in self._chat_scroll.winfo_children():
            widget.destroy()
        # Re-insert the invisible width anchor at row 0
        self._chat_col_anchor = ctk.CTkFrame(
            self._chat_scroll, fg_color="transparent",
            width=self._CHAT_COL_W, height=1
        )
        self._chat_col_anchor.grid(row=0, column=1, sticky="ew")
        self._chat_col_anchor.grid_propagate(False)
        self._chat_row = 1

    # ──────────────────────────────────────────────────────────────────────────
    # PERSISTENT CHAT HISTORY — save/load/switch, browsed via the History dialog
    # ──────────────────────────────────────────────────────────────────────────
    def _persist_current_chat(self):
        """Writes the current chat (LLM history + display log) to disk. Cheap
        enough to call after every message since it's a single small JSON
        file write, and keeps history durable even if the app is killed."""
        if self._replaying_chat:
            return
        if not self._display_log:
            return  # nothing said yet — don't litter disk with empty chats
        try:
            title = self._chat_title or "Untitled chat"
            self._chat_store.save(
                self._current_chat_id, title,
                self._session.snapshot(), list(self._display_log)
            )
        except Exception as e:
            self._activity_append(f"⚠ Failed to save chat history: {e}\n")

    def _start_new_chat_record(self):
        """Begins a brand-new chat id/title/display-log without touching the
        engine session — used on startup and after _new_session()."""
        self._current_chat_id = uuid.uuid4().hex
        self._chat_title = None
        self._display_log = []

    def _open_history_dialog(self):
        from gui.dialogs import ChatHistoryDialog
        ChatHistoryDialog(
            self, self._chat_store, self._current_chat_id,
            on_open=self._load_chat_by_id,
            on_deleted_current=self._start_new_chat_record,
        )

    def _load_chat_by_id(self, chat_id: str):
        if self._thinking:
            messagebox.showwarning("Active Session Execution", "Please wait for current run to finish or click Abort first.")
            return
        try:
            data = self._chat_store.load(chat_id)
        except Exception as e:
            messagebox.showerror("Error loading chat", str(e))
            return

        self._current_chat_id = data.get("id", chat_id)
        self._chat_title = data.get("title")

        history = data.get("history") or []
        with self._session._lock:
            self._session.history = history
            self._session.turn_counter = max(1, sum(1 for m in history if m.get("role") == "user"))

        self._clear_chat()
        self._replaying_chat = True
        try:
            for tag, text in data.get("display", []):
                if tag == "tool":
                    self._chat_append_tool(text)
                else:
                    self._chat_append(tag, text)
        finally:
            self._replaying_chat = False
        self._display_log = list(data.get("display", []))

        self._set_status("Ready", C["green"])
        self._refresh_status()

    def _activity_append(self, text: str):
        self._activity_box.configure(state="normal")
        self._activity_box.insert("end", text)
        self._activity_box.configure(state="disabled")
        self._activity_box.see("end")

    def _clear_box(self, box):
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.configure(state="disabled")

    def _set_status(self, text: str, colour: str = C["subtext"]):
        self._status_label.configure(text=text, text_color=colour)
        if colour == C["green"]:
            self._status_dot.configure(fg_color=C["green"])
        elif colour == C["yellow"]:
            self._status_dot.configure(fg_color=C["yellow"])
        elif colour == C["red"]:
            self._status_dot.configure(fg_color=C["red"])
        else:
            self._status_dot.configure(fg_color=C["subtext"])

    def _bind_keys(self):
        self.bind_all("<Control-q>", lambda e: self._abort())

    def on_close(self):
        self._shutdown_engine()

# =============================================================================
# ENTRYPOINT DETECTORS
# =============================================================================
if __name__ == "__main__":
    app = MidumGUI()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app._bind_keys()
    app.mainloop()
