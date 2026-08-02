# --- AUTO-SPLITTER: imports added by automated pass, please review ---
from tkinter import messagebox, filedialog
import tkinter as tk
import customtkinter as ctk

# --- from gui.py, section 1 ---
class ChatHistoryDialog(ctk.CTkToplevel):
    """
    Standalone window (opened via the sidebar's 🕘 History button) that lists
    every persisted chat, newest first, and lets the user reopen, rename, or
    delete one. Kept out of the CTkTabview on purpose — chat history isn't a
    "system tab", it's a modal browsing action.
    """

    def __init__(self, parent, chat_store: "ChatStore", current_chat_id, on_open, on_deleted_current):
        from gui.legacy.app import C, FONT_TITLE
        super().__init__(parent)
        self.title("🕘 Chat History")
        self.geometry("460x560")
        self.minsize(360, 320)
        self.configure(fg_color=C["bg"])

        self._store = chat_store
        self._current_chat_id = current_chat_id
        self._on_open = on_open
        self._on_deleted_current = on_deleted_current

        shell = ctk.CTkFrame(self, fg_color=C["panel"], corner_radius=20, border_width=1, border_color=C["border"])
        shell.pack(fill="both", expand=True, padx=8, pady=8)
        shell.grid_rowconfigure(1, weight=1)
        shell.grid_columnconfigure(0, weight=1)

        hdr = ctk.CTkFrame(shell, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hdr, text="Chat History", font=FONT_TITLE, text_color=C["text"]).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            hdr, text="✕", width=28, height=28, fg_color="transparent",
            hover_color=C["surface2"], text_color=C["subtext"], corner_radius=14,
            command=self.destroy
        ).grid(row=0, column=1, sticky="e")

        self._list_frame = ctk.CTkScrollableFrame(
            shell, fg_color=C["surface"], corner_radius=16,
            border_width=1, border_color=C["border"]
        )
        self._list_frame.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))
        self._list_frame.grid_columnconfigure(0, weight=1)

        self._populate()

        self.lift()
        self.focus_force()
        self.grab_set()

    def _populate(self):
        from gui.legacy.app import C, FONT_SMALL
        for w in self._list_frame.winfo_children():
            w.destroy()

        chats = self._store.list_chats()

        if not chats:
            ctk.CTkLabel(
                self._list_frame, text="No saved chats yet.",
                font=FONT_SMALL, text_color=C["subtext"]
            ).grid(row=0, column=0, sticky="w", padx=8, pady=12)
            return

        for i, chat in enumerate(chats):
            self._build_row(i, chat)

    def _build_row(self, row: int, chat: dict):
        from gui.legacy.app import C, FONT_BOLD, FONT_LABEL, FONT_TINY
        is_current = chat["id"] == self._current_chat_id

        card = ctk.CTkFrame(
            self._list_frame, corner_radius=14,
            fg_color=C["accent_faint"] if is_current else C["panel"],
            border_width=1, border_color=C["accent"] if is_current else C["border2"],
        )
        card.grid(row=row, column=0, sticky="ew", padx=6, pady=5)
        card.grid_columnconfigure(0, weight=1)

        info = ctk.CTkFrame(card, fg_color="transparent")
        info.grid(row=0, column=0, sticky="ew", padx=(14, 6), pady=10)
        info.grid_columnconfigure(0, weight=1)

        title = chat["title"] or "Untitled chat"
        if len(title) > 46:
            title = title[:45] + "…"
        ctk.CTkLabel(
            info, text=title, font=FONT_BOLD, text_color=C["text"],
            anchor="w", justify="left"
        ).grid(row=0, column=0, sticky="ew")

        ts = chat.get("updated_at", "")
        ts_display = ts.replace("T", "  ") if ts else ""
        ctk.CTkLabel(
            info, text=ts_display, font=FONT_TINY, text_color=C["subtext"], anchor="w"
        ).grid(row=1, column=0, sticky="ew", pady=(2, 0))

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.grid(row=0, column=1, sticky="e", padx=(0, 10), pady=10)

        ctk.CTkButton(
            btns, text="Open", width=56, height=26, font=FONT_LABEL,
            fg_color=C["accent"], hover_color=C["accent_dim"], corner_radius=13,
            command=lambda cid=chat["id"]: self._open(cid)
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            btns, text="🗑", width=30, height=26, font=FONT_LABEL,
            fg_color="transparent", hover_color="#2d1010",
            text_color=C["red"], border_width=1, border_color="#3f0f0f", corner_radius=13,
            command=lambda cid=chat["id"], t=title: self._delete(cid, t)
        ).pack(side="left")

    def _open(self, chat_id: str):
        self._on_open(chat_id)
        self.destroy()

    def _delete(self, chat_id: str, title: str):
        if not messagebox.askyesno("Delete Chat", f'Permanently delete "{title}"?'):
            return
        self._store.delete(chat_id)
        if chat_id == self._current_chat_id:
            self._on_deleted_current()
        self._populate()


# =============================================================================
# SLEEK MODAL DIALOGUES FOR FILE CREATION
# =============================================================================

# --- from gui.py, section 2 ---
class CreateKnowledgeDialog(ctk.CTkToplevel):
    def __init__(self, parent, on_success_callback):
        from gui.legacy.app import C, FONT_BODY, FONT_LABEL, FONT_TITLE
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



# --- from gui.py, section 3 ---
class CreateSkillDialog(ctk.CTkToplevel):
    def __init__(self, parent, on_success_callback):
        from gui.legacy.app import C, FONT_BODY, FONT_LABEL, FONT_TITLE
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



# --- from gui.py, section 4 ---
class AddMCPServerDialog(ctk.CTkToplevel):
    """Connect a new MCP server — stdio (local subprocess) or http/sse (remote)."""
    def __init__(self, parent, on_success_callback):
        from gui.legacy.app import C, FONT_BODY, FONT_LABEL, FONT_SMALL, FONT_TITLE
        super().__init__(parent)
        self.title("✚ Connect MCP Server")
        self.geometry("460x560")
        self.resizable(False, False)
        self.configure(fg_color=C["bg"])
        self.on_success = on_success_callback

        main_frame = ctk.CTkFrame(self, fg_color=C["panel"], corner_radius=20, border_width=1, border_color=C["border"])
        main_frame.pack(fill="both", expand=True, padx=8, pady=8)

        ctk.CTkLabel(main_frame, text="Connect MCP Server", font=FONT_TITLE, text_color=C["text"]).pack(anchor="w", padx=16, pady=(16, 8))

        body = ctk.CTkScrollableFrame(main_frame, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 4))

        ctk.CTkLabel(body, text="Server name:", font=FONT_LABEL, text_color=C["subtext"]).pack(anchor="w", pady=(0, 0))
        self._entry_name = ctk.CTkEntry(
            body, font=FONT_BODY, fg_color=C["surface"], text_color=C["text"],
            border_color=C["border2"], corner_radius=20, height=34,
            placeholder_text="e.g. filesystem"
        )
        self._entry_name.pack(fill="x", pady=(4, 0))

        ctk.CTkLabel(body, text="Transport:", font=FONT_LABEL, text_color=C["subtext"]).pack(anchor="w", pady=(10, 0))
        self._transport_var = ctk.CTkOptionMenu(
            body, values=["stdio", "http", "sse"],
            command=self._on_transport_changed,
            fg_color=C["surface"], button_color=C["border2"],
            button_hover_color=C["accent"], dropdown_fg_color=C["surface"],
            dropdown_hover_color=C["surface2"], text_color=C["text"], font=FONT_SMALL,
            corner_radius=20
        )
        self._transport_var.pack(fill="x", pady=(4, 0))

        # ── stdio fields ────────────────────────────────────────────────
        self._stdio_frame = ctk.CTkFrame(body, fg_color="transparent")
        self._entry_command = self._field_in(self._stdio_frame, "Command:", "e.g. npx")
        self._entry_args = self._field_in(self._stdio_frame, "Args (space-separated):", "-y @modelcontextprotocol/server-filesystem C:\\path")
        self._entry_env = self._field_in(self._stdio_frame, "Env vars (one KEY=VALUE per line, optional):", multiline=True)

        # ── http/sse fields ─────────────────────────────────────────────
        self._remote_frame = ctk.CTkFrame(body, fg_color="transparent")
        self._entry_url = self._field_in(self._remote_frame, "URL:", "https://example.com/mcp")
        self._entry_headers = self._field_in(self._remote_frame, "Headers (one KEY: VALUE per line, optional):", multiline=True)

        self._stdio_frame.pack(fill="x")

        self._persist_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            body, text="Remember & auto-connect on startup", variable=self._persist_var,
            font=FONT_SMALL, text_color=C["subtext"], fg_color=C["accent"],
            hover_color=C["accent_dim"], border_color=C["border2"], corner_radius=6
        ).pack(anchor="w", pady=(14, 4))

        btn_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        btn_frame.pack(fill="x", side="bottom", padx=16, pady=16)

        ctk.CTkButton(btn_frame, text="Cancel", width=80, fg_color="transparent", hover_color=C["surface2"], border_width=1, border_color=C["border2"], corner_radius=20, command=self.destroy).pack(side="left")
        ctk.CTkButton(btn_frame, text="Connect", width=90, fg_color=C["accent"], hover_color=C["accent_dim"], corner_radius=20, command=self._on_submit).pack(side="right")

        self.lift()
        self.focus_force()
        self.grab_set()

    def _field_in(self, parent, label_text, placeholder="", multiline=False):
        from gui.legacy.app import C, FONT_BODY, FONT_LABEL, FONT_MONO
        ctk.CTkLabel(parent, text=label_text, font=FONT_LABEL, text_color=C["subtext"]).pack(anchor="w", pady=(10, 0))
        if multiline:
            widget = ctk.CTkTextbox(
                parent, font=FONT_MONO, fg_color=C["surface"], text_color=C["text"],
                corner_radius=10, border_width=1, border_color=C["border2"], height=60, wrap="none"
            )
        else:
            widget = ctk.CTkEntry(
                parent, font=FONT_BODY, fg_color=C["surface"], text_color=C["text"],
                border_color=C["border2"], corner_radius=20, height=34,
                placeholder_text=placeholder
            )
        widget.pack(fill="x", pady=(4, 0))
        return widget

    def _on_transport_changed(self, choice: str):
        if choice == "stdio":
            self._remote_frame.pack_forget()
            self._stdio_frame.pack(fill="x")
        else:
            self._stdio_frame.pack_forget()
            self._remote_frame.pack(fill="x")

    @staticmethod
    def _parse_kv_lines(raw: str, sep_chars=("=",)) -> dict:
        result = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            for sep in sep_chars:
                if sep in line:
                    k, v = line.split(sep, 1)
                    result[k.strip()] = v.strip()
                    break
        return result

    def _on_submit(self):
        name = self._entry_name.get().strip()
        if not name:
            messagebox.showerror("Error", "Server name cannot be empty.")
            return
        transport = self._transport_var.get()

        payload = {"name": name, "transport": transport, "persist": self._persist_var.get()}

        if transport == "stdio":
            command = self._entry_command.get().strip()
            if not command:
                messagebox.showerror("Error", "Command is required for a stdio server.")
                return
            payload["command"] = command
            args_raw = self._entry_args.get().strip()
            payload["args"] = args_raw.split() if args_raw else []
            env_raw = self._entry_env.get("1.0", "end").strip()
            env = self._parse_kv_lines(env_raw, ("=",))
            if env:
                payload["env"] = env
        else:
            url = self._entry_url.get().strip()
            if not url:
                messagebox.showerror("Error", f"URL is required for an '{transport}' server.")
                return
            payload["url"] = url
            headers_raw = self._entry_headers.get("1.0", "end").strip()
            headers = self._parse_kv_lines(headers_raw, (":", "="))
            if headers:
                payload["headers"] = headers

        self.on_success(payload)
        self.destroy()



# --- from gui.py, section 5 ---
class ViewMCPToolsDialog(ctk.CTkToplevel):
    """Read-only view of a connected server's tools + JSON schemas."""
    def __init__(self, parent, server_name: str, content: str):
        from gui.legacy.app import C, FONT_MONO, FONT_TITLE
        super().__init__(parent)
        self.title(f"🧩 {server_name} — Tools")
        self.geometry("560x480")
        self.configure(fg_color=C["bg"])

        main_frame = ctk.CTkFrame(self, fg_color=C["panel"], corner_radius=20, border_width=1, border_color=C["border"])
        main_frame.pack(fill="both", expand=True, padx=8, pady=8)

        ctk.CTkLabel(main_frame, text=f"Tools on '{server_name}'", font=FONT_TITLE, text_color=C["text"]).pack(anchor="w", padx=16, pady=(16, 8))

        box = ctk.CTkTextbox(
            main_frame, font=FONT_MONO, fg_color=C["tool_bg"], text_color=C["tool_text"],
            wrap="word", corner_radius=12, border_width=1, border_color=C["border"]
        )
        box.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        box._textbox.configure(spacing1=3, spacing2=2, padx=8, pady=8)
        box.insert("end", content)
        box.configure(state="disabled")

        ctk.CTkButton(
            main_frame, text="Close", width=90, fg_color=C["accent"], hover_color=C["accent_dim"],
            corner_radius=20, command=self.destroy
        ).pack(anchor="e", padx=16, pady=(0, 16))

        self.lift()
        self.focus_force()
        self.grab_set()


# =============================================================================
# MAIN WINDOW
# =============================================================================
# =============================================================================
# PROVIDER / MODEL SELECTION
# =============================================================================
# Lets the user pick MODEL_PROVIDER + the model id for that provider from a
# GUI dropdown instead of hand-editing main.py. "Local (Ollama)" is the
# default on every fresh launch, regardless of whatever MODEL_PROVIDER is
# hardcoded at the top of main.py — the GUI always overrides it at startup.
PROVIDER_OPTIONS = [
    ("Local (Ollama)", "ollama"),
    ("OpenRouter",      "openrouter"),
    ("Gemini (Web)",    "gemini_web"),
    ("Gemini (API)",    "gemini_api"),
    ("Groq",            "groq"),
]
_PROVIDER_LABEL_TO_KEY = {label: key for label, key in PROVIDER_OPTIONS}
_PROVIDER_KEY_TO_LABEL = {key: label for label, key in PROVIDER_OPTIONS}
DEFAULT_PROVIDER_KEY = "ollama"


# =============================================================================
# PDF HEADING TAGGER
# =============================================================================
# Opens the ACTUAL PDF (rendered via PyMuPDF) so the user can click a real
# line of text and assign it a heading level directly -- no automatic
# structure detection anywhere in this flow. Every heading that ends up in
# the source's saved record is a line the user personally clicked and
# tagged. Saving calls midum.set_pdf_source_headings(name, headings), which
# overwrites the source's previous tagging with exactly this list.
class PdfHeadingTaggerDialog(ctk.CTkToplevel):
    def __init__(self, parent, midum_module, source_name: str, pdf_path: str, existing_headings: list, on_saved=None):
        from gui.legacy.app import C, FONT_SMALL, FONT_TITLE
        super().__init__(parent)
        self._midum = midum_module
        self._source_name = source_name
        self._on_saved = on_saved
        self._C = C

        self.title(f"🏷️ Tag Headings — {source_name}")
        self.geometry("1100x760")
        self.minsize(760, 480)
        self.configure(fg_color=C["bg"])

        import fitz
        self._doc = fitz.open(pdf_path)
        self._page_index = 0
        self._zoom = 1.6
        self._img_ref = None
        self._line_boxes = []                 # this page's clickable lines
        self._selected_line = None
        # line_id -> {page, line_id, text, level}
        self._headings = {h["line_id"]: dict(h) for h in (existing_headings or []) if h.get("line_id")}

        self._build_layout()
        self._render_page()

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.lift()
        self.focus_force()
        self.grab_set()

    # ── layout ───────────────────────────────────────────────────────────────
    def _build_layout(self):
        C = self._C
        main = ctk.CTkFrame(self, fg_color=C["panel"], corner_radius=0)
        main.pack(fill="both", expand=True)
        main.grid_columnconfigure(0, weight=3)
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(1, weight=1)

        nav = ctk.CTkFrame(main, fg_color="transparent")
        nav.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=8)
        ctk.CTkButton(nav, text="◀ Prev", width=70, height=28, font=FONT_SMALL,
                      fg_color=C["surface2"], hover_color=C["accent"],
                      command=self._prev_page).pack(side="left")
        self._page_lbl = ctk.CTkLabel(nav, text="", font=FONT_SMALL, text_color=C["text"])
        self._page_lbl.pack(side="left", padx=10)
        ctk.CTkButton(nav, text="Next ▶", width=70, height=28, font=FONT_SMALL,
                      fg_color=C["surface2"], hover_color=C["accent"],
                      command=self._next_page).pack(side="left")
        ctk.CTkLabel(
            nav, text="Click a line of text below, then tap a heading level to tag it.",
            font=FONT_SMALL, text_color=C["subtext"]
        ).pack(side="left", padx=20)

        canvas_frame = ctk.CTkFrame(main, fg_color=C["surface"], corner_radius=0)
        canvas_frame.grid(row=1, column=0, sticky="nsew", padx=(10, 4), pady=(0, 10))
        canvas_frame.grid_rowconfigure(0, weight=1)
        canvas_frame.grid_columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(canvas_frame, bg="#12161c", highlightthickness=0)
        vbar = tk.Scrollbar(canvas_frame, orient="vertical", command=self._canvas.yview)
        hbar = tk.Scrollbar(canvas_frame, orient="horizontal", command=self._canvas.xview)
        self._canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        self._canvas.bind("<Button-1>", self._on_canvas_click)

        self._build_side_panel(main)

    def _build_side_panel(self, main):
        C = self._C
        from gui.legacy.app import FONT_SMALL

        side = ctk.CTkFrame(main, fg_color=C["surface"], corner_radius=12,
                             border_width=1, border_color=C["border2"])
        side.grid(row=1, column=1, sticky="nsew", padx=(4, 10), pady=(0, 10))
        side.grid_rowconfigure(4, weight=1)
        side.grid_columnconfigure(0, weight=1)

        self._selected_lbl = ctk.CTkLabel(
            side, text="No line selected", wraplength=260,
            font=FONT_SMALL, text_color=C["subtext"], justify="left"
        )
        self._selected_lbl.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))

        lvl_row = ctk.CTkFrame(side, fg_color="transparent")
        lvl_row.grid(row=1, column=0, sticky="ew", padx=10)
        for lvl in range(1, 7):
            ctk.CTkButton(
                lvl_row, text=f"H{lvl}", width=36, height=28, font=FONT_SMALL,
                fg_color=C["surface2"], hover_color=C["accent"],
                command=lambda l=lvl: self._tag_selected(l)
            ).pack(side="left", padx=2, pady=6)

        ctk.CTkButton(
            side, text="Untag selected line", height=26, font=FONT_SMALL,
            fg_color="transparent", hover_color="#2d1010", text_color=C["red"],
            border_width=1, border_color="#3f0f0f", corner_radius=13,
            command=self._untag_selected
        ).grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 8))

        ctk.CTkLabel(
            side, text="TAGGED HEADINGS", font=("Segoe UI", 9, "bold"),
            text_color=C["subtext"]
        ).grid(row=3, column=0, sticky="nw", padx=10)

        self._tagged_list = ctk.CTkScrollableFrame(side, fg_color=C["panel"], corner_radius=10)
        self._tagged_list.grid(row=4, column=0, sticky="nsew", padx=10, pady=(4, 10))

        btn_row = ctk.CTkFrame(side, fg_color="transparent")
        btn_row.grid(row=5, column=0, sticky="ew", padx=10, pady=(0, 10))
        ctk.CTkButton(
            btn_row, text="Cancel", fg_color="transparent", hover_color=C["surface2"],
            border_width=1, border_color=C["border2"], command=self._close
        ).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ctk.CTkButton(
            btn_row, text="Save", fg_color=C["accent"], hover_color=C["accent_dim"],
            command=self._save
        ).pack(side="left", expand=True, fill="x", padx=(4, 0))

        self._refresh_tagged_list()

    # ── page rendering ───────────────────────────────────────────────────────
    def _render_page(self):
        import fitz
        page = self._doc[self._page_index]
        mat = fitz.Matrix(self._zoom, self._zoom)
        pix = page.get_pixmap(matrix=mat)
        mode = "RGB" if pix.alpha == 0 else "RGBA"
        from PIL import Image as _PILImage, ImageTk as _ImageTk
        img = _PILImage.frombytes(mode, (pix.width, pix.height), pix.samples)
        self._img_ref = _ImageTk.PhotoImage(img)
        self._canvas.delete("all")
        self._canvas.create_image(0, 0, anchor="nw", image=self._img_ref)
        self._canvas.configure(scrollregion=(0, 0, pix.width, pix.height))

        self._line_boxes = []
        page_dict = page.get_text("dict")
        line_no = 0
        for block in page_dict.get("blocks", []):
            for line in block.get("lines", []):
                text = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
                if not text:
                    continue
                x0, y0, x1, y1 = line.get("bbox")
                rect = (x0 * self._zoom, y0 * self._zoom, x1 * self._zoom, y1 * self._zoom)
                line_id = f"p{self._page_index + 1}_l{line_no}"
                self._line_boxes.append({
                    "line_id": line_id, "text": text,
                    "page": self._page_index + 1, "rect": rect,
                })
                line_no += 1
                if line_id in self._headings:
                    self._canvas.create_rectangle(*rect, outline=self._C["accent"], width=2)

        if self._selected_line and self._selected_line["page"] == self._page_index + 1:
            match = next((lb for lb in self._line_boxes if lb["line_id"] == self._selected_line["line_id"]), None)
            if match:
                self._canvas.create_rectangle(*match["rect"], outline=self._C["yellow"], width=3)

        self._page_lbl.configure(text=f"Page {self._page_index + 1} / {self._doc.page_count}")

    def _on_canvas_click(self, event):
        x = self._canvas.canvasx(event.x)
        y = self._canvas.canvasy(event.y)
        for lb in self._line_boxes:
            x0, y0, x1, y1 = lb["rect"]
            if x0 <= x <= x1 and y0 <= y <= y1:
                self._selected_line = lb
                self._selected_lbl.configure(text=f"Selected (p.{lb['page']}): {lb['text'][:120]}")
                self._render_page()
                return
        self._selected_line = None
        self._selected_lbl.configure(text="No line selected")

    # ── tagging ──────────────────────────────────────────────────────────────
    def _tag_selected(self, level):
        if not self._selected_line:
            return
        lb = self._selected_line
        self._headings[lb["line_id"]] = {
            "page": lb["page"], "line_id": lb["line_id"], "text": lb["text"], "level": level
        }
        self._refresh_tagged_list()
        self._render_page()

    def _untag_selected(self):
        if not self._selected_line:
            return
        self._headings.pop(self._selected_line["line_id"], None)
        self._refresh_tagged_list()
        self._render_page()

    def _remove_tag(self, line_id):
        self._headings.pop(line_id, None)
        self._refresh_tagged_list()
        self._render_page()

    def _refresh_tagged_list(self):
        from gui.legacy.app import FONT_SMALL
        for w in self._tagged_list.winfo_children():
            w.destroy()
        items = sorted(self._headings.values(), key=lambda h: (h["page"], h["line_id"]))
        for h in items:
            row = ctk.CTkFrame(self._tagged_list, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(
                row, text=f"H{h['level']} · p{h['page']} · {h['text'][:40]}",
                font=FONT_SMALL, text_color=self._C["text"], anchor="w"
            ).pack(side="left", fill="x", expand=True)
            ctk.CTkButton(
                row, text="✕", width=22, height=22, fg_color="transparent",
                hover_color="#2d1010", text_color=self._C["red"],
                command=lambda lid=h["line_id"]: self._remove_tag(lid)
            ).pack(side="right")

    # ── navigation ───────────────────────────────────────────────────────────
    def _prev_page(self):
        if self._page_index > 0:
            self._page_index -= 1
            self._selected_line = None
            self._render_page()

    def _next_page(self):
        if self._page_index < self._doc.page_count - 1:
            self._page_index += 1
            self._selected_line = None
            self._render_page()

    # ── save / close ─────────────────────────────────────────────────────────
    def _save(self):
        headings = list(self._headings.values())
        try:
            result = self._midum.set_pdf_source_headings(self._source_name, headings)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save headings: {e}")
            return
        if self._on_saved:
            self._on_saved(result)
        self._close()

    def _close(self):
        try:
            self._doc.close()
        except Exception:
            pass
        self.destroy()



