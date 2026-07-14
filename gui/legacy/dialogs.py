# --- AUTO-SPLITTER: imports added by automated pass, please review ---
from tkinter import messagebox, filedialog
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



