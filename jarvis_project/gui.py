"""
gui.py — Jarvis Desktop GUI
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

import customtkinter as ctk
from tkinter import messagebox, filedialog
import tkinter as tk

# ── Import core Jarvis engine ─────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import main as jarvis

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
    "jarvis_msg": "#0f1623",   # Jarvis response bubble

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
_SAY_TAG = "\x02JARVIS_SAY\x02"

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


def _is_tool_line(raw_line: str) -> bool:
    """True if a stdout line represents a tool call or major system event."""
    line = raw_line.strip()
    if not line:
        return False
    if line[0] in _TOOL_LINE_EMOJI:
        return True
    low = line.lower()
    return any(k in low for k in _TOOL_LINE_KEYWORDS)


# =============================================================================
# REDIRECT stdout → GUI log
# =============================================================================
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
# JARVIS SESSION STATE (shared between GUI and engine thread)
# =============================================================================
class JarvisSession:
    def __init__(self):
        self.history          = []
        self.turn_counter     = 1
        self.system_prompt    = ""
        self.memory_injections = []
        self._lock            = threading.Lock()

    def initialise(self, system_prompt: str, memory_injections: list):
        with self._lock:
            self.system_prompt    = system_prompt
            self.memory_injections = memory_injections
            self.history = [{"role": "system", "content": system_prompt}]
            for inj in memory_injections:
                self.history.append({"role": "system", "content": inj})

    def reset(self):
        with self._lock:
            self.history = [{"role": "system", "content": self.system_prompt}]
            if self.memory_injections:
                self.history.append({"role": "system", "content": self.memory_injections[0]})
            self.turn_counter = 1

    def append(self, msg: dict):
        with self._lock:
            self.history.append(msg)

    def snapshot(self) -> list:
        with self._lock:
            return list(self.history)

# =============================================================================
# SLEEK MODAL DIALOGUES FOR FILE CREATION
# =============================================================================
class CreateKnowledgeDialog(ctk.CTkToplevel):
    def __init__(self, parent, on_success_callback):
        super().__init__(parent)
        self.title("✚ Create Domain Knowledge Base")
        self.geometry("450x250")
        self.resizable(False, False)
        self.configure(fg_color=C["bg"])
        self.on_success = on_success_callback

        main_frame = ctk.CTkFrame(self, fg_color=C["panel"], corner_radius=20, border_width=1, border_color=C["border"])
        main_frame.pack(fill="both", expand=True, padx=8, pady=8)

        ctk.CTkLabel(main_frame, text="New Knowledge Base", font=FONT_TITLE, text_color=C["text"]).pack(anchor="w", padx=16, pady=(16, 8))

        ctk.CTkLabel(main_frame, text="Name (snake_case, e.g. blender_commands):", font=FONT_LABEL, text_color=C["subtext"]).pack(anchor="w", padx=16, pady=(4, 0))
        self._entry_name = ctk.CTkEntry(main_frame, font=FONT_BODY, fg_color=C["surface"], text_color=C["text"], border_color=C["border2"], corner_radius=20, height=34)
        self._entry_name.pack(fill="x", padx=16, pady=(4, 0))

        ctk.CTkLabel(main_frame, text="One-line Description:", font=FONT_LABEL, text_color=C["subtext"]).pack(anchor="w", padx=16, pady=(10, 0))
        self._entry_desc = ctk.CTkEntry(main_frame, font=FONT_BODY, fg_color=C["surface"], text_color=C["text"], border_color=C["border2"], corner_radius=20, height=34)
        self._entry_desc.pack(fill="x", padx=16, pady=(4, 0))

        btn_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        btn_frame.pack(fill="x", side="bottom", padx=16, pady=16)

        ctk.CTkButton(btn_frame, text="Cancel", width=80, fg_color="transparent", hover_color=C["surface2"], border_width=1, border_color=C["border2"], corner_radius=20, command=self.destroy).pack(side="left")
        ctk.CTkButton(btn_frame, text="Create", width=80, fg_color=C["accent"], hover_color=C["accent_dim"], corner_radius=20, command=self._on_submit).pack(side="right")

        self.lift()
        self.focus_force()
        self.grab_set()

    def _on_submit(self):
        name = self._entry_name.get().strip()
        desc = self._entry_desc.get().strip()

        if not name:
            messagebox.showerror("Error", "Name field cannot be empty.")
            return
        if not desc:
            messagebox.showerror("Error", "Description field cannot be empty.")
            return

        self.on_success(name, desc)
        self.destroy()


class CreateSkillDialog(ctk.CTkToplevel):
    def __init__(self, parent, on_success_callback):
        super().__init__(parent)
        self.title("✚ Create Custom Skill")
        self.geometry("450x300")
        self.resizable(False, False)
        self.configure(fg_color=C["bg"])
        self.on_success = on_success_callback

        main_frame = ctk.CTkFrame(self, fg_color=C["panel"], corner_radius=20, border_width=1, border_color=C["border"])
        main_frame.pack(fill="both", expand=True, padx=8, pady=8)

        ctk.CTkLabel(main_frame, text="New Custom Skill", font=FONT_TITLE, text_color=C["text"]).pack(anchor="w", padx=16, pady=(16, 8))

        ctk.CTkLabel(main_frame, text="Name (snake_case, e.g. render_scene):", font=FONT_LABEL, text_color=C["subtext"]).pack(anchor="w", padx=16, pady=(4, 0))
        self._entry_name = ctk.CTkEntry(main_frame, font=FONT_BODY, fg_color=C["surface"], text_color=C["text"], border_color=C["border2"], corner_radius=20, height=34)
        self._entry_name.pack(fill="x", padx=16, pady=(4, 0))

        ctk.CTkLabel(main_frame, text="Domain (e.g. blender, windows, spotify):", font=FONT_LABEL, text_color=C["subtext"]).pack(anchor="w", padx=16, pady=(10, 0))
        self._entry_domain = ctk.CTkEntry(main_frame, font=FONT_BODY, fg_color=C["surface"], text_color=C["text"], border_color=C["border2"], corner_radius=20, height=34)
        self._entry_domain.pack(fill="x", padx=16, pady=(4, 0))

        ctk.CTkLabel(main_frame, text="One-line Description:", font=FONT_LABEL, text_color=C["subtext"]).pack(anchor="w", padx=16, pady=(10, 0))
        self._entry_desc = ctk.CTkEntry(main_frame, font=FONT_BODY, fg_color=C["surface"], text_color=C["text"], border_color=C["border2"], corner_radius=20, height=34)
        self._entry_desc.pack(fill="x", padx=16, pady=(4, 0))

        btn_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        btn_frame.pack(fill="x", side="bottom", padx=16, pady=16)

        ctk.CTkButton(btn_frame, text="Cancel", width=80, fg_color="transparent", hover_color=C["surface2"], border_width=1, border_color=C["border2"], corner_radius=20, command=self.destroy).pack(side="left")
        ctk.CTkButton(btn_frame, text="Create", width=80, fg_color=C["accent"], hover_color=C["accent_dim"], corner_radius=20, command=self._on_submit).pack(side="right")

        self.lift()
        self.focus_force()
        self.grab_set()

    def _on_submit(self):
        name = self._entry_name.get().strip()
        domain = self._entry_domain.get().strip()
        desc = self._entry_desc.get().strip()

        if not name:
            messagebox.showerror("Error", "Name field cannot be empty.")
            return
        if not domain:
            messagebox.showerror("Error", "Domain field cannot be empty.")
            return
        if not desc:
            messagebox.showerror("Error", "Description field cannot be empty.")
            return

        self.on_success(name, domain, desc)
        self.destroy()


# =============================================================================
# MAIN WINDOW
# =============================================================================
class JarvisGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Jarvis Control Center")
        self.geometry("1600x950")
        self.minsize(1200, 750)
        self.configure(fg_color=C["bg"])

        self._session      = JarvisSession()
        self._thinking     = False
        self._log_queue    = queue.Queue()   # stdout lines from engine
        self._reply_queue  = queue.Queue()   # (reply, tool_outputs) from engine

        # File selection paths tracking
        self._sys_core_active_file = None
        self._knowledge_active_file = None
        self._skills_active_file = None

        # Redirect stdout
        self._stdout_redir = _StdoutRedirector(self._log_queue.put)
        sys.stdout = self._stdout_redir

        # ── say() tool compatibility ────────────────────────────────────────
        # jarvis._print_reply binds a rich Console to sys.stdout at MODULE
        # IMPORT time (before our redirection above ever runs), so its rich
        # markdown branch would silently bypass the GUI entirely. We intercept
        # the function directly instead of relying on stdout capture, and tag
        # the payload so _poll() can render it as a live message in the main
        # chat rather than just a log line.
        def _gui_say_intercept(label: str, text: str):
            if not text or re.match(r'^[{}\[\]",:\s]*$', text.strip()):
                return
            self._log_queue.put(_SAY_TAG + text)
        jarvis._print_reply = _gui_say_intercept

        # Setup base directory trackers
        self._base_work_dir = r"D:\\"
        if not os.path.exists(self._base_work_dir):
            self._base_work_dir = os.path.expanduser("~/Documents")

        self._build_layout()
        self._startup()
        self._poll()

    # ──────────────────────────────────────────────────────────────────────────
    # STARTUP INITIALIZATION
    # ──────────────────────────────────────────────────────────────────────────
    def _startup(self):
        """Perform initial workspace scanning and state initialization."""
        self._scan_workspace_directory()
        self._refresh_status()

        # 1. Ensure core directories and files exist
        jarvis._bootstrap_all_files()
        
        # 2. Fetch master system prompt from the engine
        try:
            sys_prompt = jarvis.get_system_prompt()
        except AttributeError:
            # Fallback if get_system_prompt hasn't been merged into main.py yet
            sys_prompt = "You are Jarvis. Rules:\n- Proceed safely."

        # 3. Load general core memories safely (bypass CLI input loops)
        memories = []
        master_ctx = jarvis.load_memory_into_context(jarvis.MASTER_MEMORY, "master")
        if master_ctx: 
            memories.append(master_ctx)
        
        session_ctx = jarvis.load_memory_into_context(jarvis.SESSION_MEMORY, "session (continued)")
        if session_ctx: 
            memories.append(session_ctx)
            
        try:
            with open(jarvis.INSTRUCTIONS_FILE, "r", encoding="utf-8") as f:
                _instr = f.read().strip()
            if _instr: 
                memories.append("[JARVIS INSTRUCTIONS — always active]\n" + _instr)
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
        ctk.CTkLabel(brand, text="Jarvis", font=("Segoe UI", 12, "bold"), text_color=C["text"]).pack(side="left", padx=(5, 0))

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
        self._paned.add(self._sidebar, minsize=200, width=280)

        # ── New Session ───────────────────────────────────────────────────────
        ctk.CTkButton(
            self._sidebar, text="+ New Session", height=30,
            fg_color=C["surface2"], hover_color=C["border2"],
            text_color=C["text"], font=FONT_SMALL,
            corner_radius=20, command=self._new_session
        ).grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))

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
        self._tabs = ctk.CTkTabview(
            self._sidebar,
            fg_color=C["surface"],
            segmented_button_fg_color=C["surface"],
            segmented_button_selected_color=C["accent"],
            segmented_button_selected_hover_color=C["accent_dim"],
            segmented_button_unselected_hover_color=C["surface2"],
            corner_radius=0
        )
        self._tabs.grid(row=5, column=0, sticky="nsew", padx=0, pady=0)

        for tab in ("Log", "Parameters", "System Core", "Knowledge", "Skills", "Tools"):
            self._tabs.add(tab)

        self._build_activity_panel()
        self._build_status_tab()
        self._build_system_core_tab()
        self._build_knowledge_bases_tab()
        self._build_skills_tab()
        self._build_manual_tools_tab()

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
            input_box, placeholder_text="Message Jarvis...",
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
            "Master Memory": jarvis.MASTER_MEMORY,
            "Session Memory": jarvis.SESSION_MEMORY,
            "Instructions": jarvis.INSTRUCTIONS_FILE,
            "Paths": jarvis.PATHS_FILE,
            "Active Project": jarvis._active_project_memory_path,
            "Scratchpad": jarvis.RESPONSE_MEMORY
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
            if os.path.exists(jarvis.STORAGE_DIR):
                for f in os.listdir(jarvis.STORAGE_DIR):
                    if f.endswith(".md") and os.path.isfile(os.path.join(jarvis.STORAGE_DIR, f)):
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
        path = os.path.join(jarvis.STORAGE_DIR, filename)
        self._knowledge_active_file = path
        self._load_file_into_box(self._knowledge_box, path)

    def _save_knowledge_file(self):
        if not self._knowledge_active_file:
            messagebox.showwarning("Save Blocked", "No active knowledge base selected.")
            return
        self._save_box_to_file(self._knowledge_box, self._knowledge_active_file)

    def _create_knowledge_dialog(self):
        CreateKnowledgeDialog(self, self._execute_create_knowledge)

    def _execute_create_knowledge(self, name, description):
        try:
            result = jarvis.create_domain_knowledge(name, description)
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
            if os.path.exists(jarvis.SKILLS_DIR):
                for f in os.listdir(jarvis.SKILLS_DIR):
                    if f.endswith(".md") and os.path.isfile(os.path.join(jarvis.SKILLS_DIR, f)):
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
        path = os.path.join(jarvis.SKILLS_DIR, filename)
        self._skills_active_file = path
        self._load_file_into_box(self._skills_box, path)

    def _save_skill_file(self):
        if not self._skills_active_file:
            messagebox.showwarning("Save Blocked", "No active skill selected.")
            return
        self._save_box_to_file(self._skills_box, self._skills_active_file)

    def _create_skill_dialog(self):
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
            result = jarvis.create_domain_skill(name, domain, description, initial_content)
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

        tool_names = [t["function"]["name"] for t in jarvis.tools]
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
        schema = next((t["function"] for t in jarvis.tools if t["function"]["name"] == tool_name), None)
        if not schema:
            return

        props = schema.get("parameters", {}).get("properties", {})
        required = schema.get("parameters", {}).get("required", [])

        row = 0
        for arg_name, arg_details in props.items():
            req_str = " *" if arg_name in required else ""
            desc = arg_details.get("description", "")
            
            lbl = ctk.CTkLabel(self._manual_args_frame, text=f"{arg_name}{req_str}", font=FONT_BOLD, text_color=C["subtext"])
            lbl.grid(row=row, column=0, sticky="ne", padx=(5, 10), pady=(5, 5))

            entry = ctk.CTkEntry(self._manual_args_frame, font=FONT_BODY, fg_color=C["surface"], text_color=C["text"], border_color=C["border2"], corner_radius=20)
            entry.grid(row=row, column=1, sticky="ew", padx=(0, 5), pady=(5, 5))
            
            if desc:
                entry.configure(placeholder_text=desc)

            self._manual_arg_entries[arg_name] = entry
            row += 1
            
        if not props:
            ctk.CTkLabel(self._manual_args_frame, text="No arguments required for this tool.", font=FONT_SMALL, text_color=C["subtext"]).grid(row=0, column=0, pady=10)

    def _execute_manual_tool(self):
        tool_name = self._manual_tool_dropdown.get()
        # Collect parameters, filtering out empty entries
        args = {name: entry.get() for name, entry in self._manual_arg_entries.items() if entry.get().strip()}
        
        self._manual_output_box.configure(state="normal")
        self._manual_output_box.delete("1.0", "end")
        self._manual_output_box.insert("end", f"[Executing tool sandbox call: {tool_name}...]\n")
        self._manual_output_box.configure(state="disabled")

        def run_tool_background():
            try:
                out = ""
                # Dispatch explicitly via specific patterns found in main.py
                if tool_name in ["read_aggregated_text", "query_gemini_app", "manage_gemini_chat"]:
                    if jarvis._UIA_AVAILABLE and hasattr(jarvis, 'ui_navigator') and jarvis.ui_navigator:
                        func = getattr(jarvis.ui_navigator, tool_name)
                        out = func(**args)
                    else:
                        out = "UI Automation is currently unavailable."
                
                # Path resolution utilities that require processing before calling
                elif tool_name == "read_local_file":
                    res, _ = jarvis.resolve_file_path(args.get("path", ""))
                    out = jarvis.read_local_file(res)
                elif tool_name == "write_local_file":
                    res, _ = jarvis.resolve_file_path(args.get("path", ""))
                    out = jarvis.write_local_file(res, args.get("content", ""))
                elif tool_name == "append_local_file":
                    res, _ = jarvis.resolve_file_path(args.get("path", ""))
                    out = jarvis.append_local_file(res, args.get("content", ""))
                
                # Image data needs to be truncated to avoid completely freezing the textbox
                elif tool_name == "fallback_view_screen":
                    out = jarvis.capture_screen_to_ram()
                    if len(out) > 1000 and not out.startswith("Error"):
                        out = f"Screenshot successfully captured to RAM ({len(out)} bytes of Base64 Data).\n\n(Raw Base64 string is hidden here to prevent GUI lag, but tool is functional.)"

                elif tool_name == "execute_terminal_command":
                    out = jarvis.execute_terminal_command(args.get("command", ""), args.get("working_directory", ""))
                
                # Standard Direct Mapping
                else:
                    if hasattr(jarvis, tool_name):
                        func = getattr(jarvis, tool_name)
                        # Coerce datatypes safely mapping schema definition (e.g. string to int for coords)
                        schema = next((t["function"] for t in jarvis.tools if t["function"]["name"] == tool_name), None)
                        if schema:
                            props = schema.get("parameters", {}).get("properties", {})
                            for k, v in args.items():
                                if props.get(k, {}).get("type") == "integer":
                                    try: args[k] = int(v)
                                    except ValueError: pass
                                elif props.get(k, {}).get("type") == "number":
                                    try: args[k] = float(v)
                                    except ValueError: pass
                        
                        out = func(**args)
                    else:
                        out = f"Error: Function '{tool_name}' not mapped directly in Jarvis namespace."

                self.after(0, self._update_manual_output, str(out))
            except Exception as e:
                self.after(0, self._update_manual_output, f"Tool Exception Caught:\n{str(e)}\n\nTraceback:\n{traceback.format_exc()}")

        # Ensure UI does not freeze during tool execution (like waiting on Gemini or terminal)
        threading.Thread(target=run_tool_background, daemon=True).start()

    def _update_manual_output(self, text: str):
        self._manual_output_box.configure(state="normal")
        self._manual_output_box.delete("1.0", "end")
        self._manual_output_box.insert("end", text)
        self._manual_output_box.configure(state="disabled")

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
        jarvis._active_project_memory_path = project_file

        # Auto-create active memory if it is missing
        if not os.path.exists(project_file):
            try:
                os.makedirs(project_dir, exist_ok=True)
                jarvis.write_local_file(
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
                    if not inj.startswith("[JARVIS PROJECT MEMORY")
                ]
                self._session.memory_injections.append(
                    f"[JARVIS PROJECT MEMORY — {selected_project}]\n{content}"
                )
                
                # Rebuild current live session context
                self._session.history = [
                    msg for msg in self._session.history 
                    if not (msg.get("role") == "system" and msg.get("content", "").startswith("[JARVIS PROJECT MEMORY"))
                ]
                self._session.history.append({
                    "role": "system",
                    "content": f"[JARVIS PROJECT MEMORY — {selected_project}]\n{content}"
                })
        except Exception as e:
            self._activity_append(f"⚠️ Context injection failure: {e}\n")

        # Log updates
        jarvis.update_memory("master", f"Active project context switched to: {selected_project} ({project_dir})")
        self._chat_append("system", f"[Workspace context switched to: {selected_project}]\n")
        
        self._set_status("Ready", C["green"])
        self._refresh_status()
        self._refresh_file_list(project_dir)

        # Force active load if dropdown is currently viewing "Active Project"
        if self._sys_core_active_file == jarvis._active_project_memory_path or self._sys_core_dropdown.get() == "Active Project":
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
            jarvis.write_local_file(
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
        proj = jarvis._active_project_memory_path
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
        proj = jarvis._active_project_memory_path
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
            jarvis._abort_event.clear()
            reply, tool_outputs = jarvis.process_chat_turn(history_snapshot)

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
                self._chat_append("jarvis", reply)
                self._set_status("Ready", C["green"])
                
                # Dynamic memory updates
                threading.Thread(
                    target=jarvis.python_trigger_memory_update,
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
                
                proj = jarvis._active_project_memory_path
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
        jarvis._abort_event.set()
        self._set_status("Aborted", C["red"])
        self._activity_append("🛑 Execution pipeline aborted by user (Ctrl+Q)\n")

    def _new_session(self):
        if self._thinking:
            messagebox.showwarning("Active Session Execution", "Please wait for current run to finish or click Abort first.")
            return
        if not messagebox.askyesno("Confirm Clear", "Clear current session context and reset memories?"):
            return

        try:
            if os.path.exists(jarvis.SESSION_MEMORY):
                os.remove(jarvis.SESSION_MEMORY)
            jarvis._current_goal = None
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            jarvis.write_local_file(
                jarvis.SESSION_MEMORY,
                f"# Jarvis Session Memory\nSession started: {ts}\n\n"
                f"{jarvis.GOAL_SECTION_HEADER}\n_No active goal._\n\n"
                f"{jarvis.GOAL_SECTION_END}\n"
            )
            self._session.reset()
            self._clear_chat()
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
            jarvis._abort_event.set()
        
        self._activity_append("🔌 Shutting down Jarvis Engine Core...\n")
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
        self._lbl_model.configure(text=jarvis.MODEL_NAME)
        self._lbl_goal.configure(
            text=jarvis._current_goal or "None active",
            text_color=C["accent"] if jarvis._current_goal else C["subtext"]
        )
        proj = jarvis._active_project_memory_path
        self._lbl_project.configure(
            text=os.path.dirname(proj) if proj else "No project selected",
            text_color=C["green"] if proj else C["subtext"]
        )
        self._lbl_gemini.configure(
            text="✅ System Connected" if jarvis._GEMINI_AVAILABLE else "⚠️  Unconnected",
            text_color=C["green"] if jarvis._GEMINI_AVAILABLE else C["yellow"]
        )
        self._lbl_ocr.configure(
            text="✅ System Connected" if jarvis._TESSERACT_AVAILABLE else "⚠️  Unconnected",
            text_color=C["green"] if jarvis._TESSERACT_AVAILABLE else C["yellow"]
        )
        self._lbl_uia.configure(
            text="✅ System Connected" if jarvis._UIA_AVAILABLE else "⚠️  Unconnected",
            text_color=C["green"] if jarvis._UIA_AVAILABLE else C["yellow"]
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
        Jarvis message in the main chat, styled like a normal reply but
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
            header, text="Jarvis", font=FONT_BOLD, text_color=C["accent"]
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

    def _chat_append(self, tag: str, text: str):
        """Append a message into the fixed-width center column of the chat scroll."""
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

        # ── JARVIS response — full markdown, no bubble ────────────────────────
        row_frame = ctk.CTkFrame(self._chat_scroll, fg_color="transparent")
        row_frame.grid(row=self._chat_row, column=1, sticky="ew", pady=(10, 4))
        self._chat_row += 1
        row_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            row_frame, text="Jarvis", font=FONT_BOLD, text_color=C["accent"]
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
    app = JarvisGUI()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app._bind_keys()
    app.mainloop()
