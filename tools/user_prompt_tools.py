# --- from main.py, section 1 ---
# 0b. GUI USER-PROMPT TOOLS
# =============================================================================
# Lets Midum pop up a small native GUI dialog to get something from the user
# without guessing — a missing file path, an approval, a disambiguating
# choice, or arbitrary free text. Every call BLOCKS until the user responds
# (or dismisses the window), then the answer is fed back as a normal tool
# result so Midum can continue the same turn. The model can request several
# of these in a single turn (e.g. ask a question AND request a file path);
# by default it requests none — these are opt-in, situational tools, not a
# forced step at the end of every turn.
try:
    import tkinter as _tk
    from tkinter import filedialog as _tk_filedialog
    _TKINTER_AVAILABLE = True
except ImportError:
    _tk = None
    _tk_filedialog = None
    _TKINTER_AVAILABLE = False

# When Midum is running inside the CustomTkinter desktop GUI (gui.pyw), the
# GUI installs a callable here at startup. If present, every ask_user_*
# tool below routes through it instead of popping up a separate native
# tkinter window — the request/response instead renders as an inline card
# in the main chat, styled to match the rest of the app. The hook's
# signature is: hook(kind: str, payload: dict) -> str, and it BLOCKS the
# calling thread (the engine worker thread) until the user responds, so
# every ask_user_* call below still behaves exactly as documented from the
# model's point of view. When running main.py standalone (no GUI attached),
# this stays None and the original native tkinter dialogs are used as-is.
_gui_ask_hook = None


def _gui_root():
    """Create a hidden, always-on-top root window to anchor a dialog to."""
    root = _tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    return root


def ask_user_text(prompt: str, title: str = "Midum needs input") -> str:
    """
    Pop up a GUI textbox asking the user to type a free-form answer — e.g. a
    missing file path, a name, a value, or anything else with no fixed set
    of options. Returns the typed text, '[USER SUBMITTED EMPTY TEXT]' if
    they submitted blank, or '[USER CANCELLED]' if they closed the dialog.
    """
    if _gui_ask_hook is not None:
        return _gui_ask_hook("text", {"prompt": prompt, "title": title})
    if not _TKINTER_AVAILABLE:
        return "[GUI ERROR] tkinter not available on this system — ask the user in plain text instead."
    result = {"value": None}
    root = _gui_root()
    win = _tk.Toplevel(root)
    win.title(title)
    try: win.attributes("-topmost", True)
    except Exception: pass

    _tk.Label(win, text=prompt, wraplength=420, justify="left", padx=16, pady=12).pack()
    entry = _tk.Entry(win, width=52)
    entry.pack(padx=16, pady=(0, 8))
    entry.focus_set()

    def submit(event=None):
        result["value"] = entry.get()
        win.destroy()

    def cancel():
        result["value"] = None
        win.destroy()

    entry.bind("<Return>", submit)
    btn_frame = _tk.Frame(win)
    btn_frame.pack(pady=(0, 12))
    _tk.Button(btn_frame, text="Submit", command=submit, width=10).pack(side="left", padx=6)
    _tk.Button(btn_frame, text="Cancel", command=cancel, width=10).pack(side="left", padx=6)

    win.protocol("WM_DELETE_WINDOW", cancel)
    win.grab_set()
    root.wait_window(win)
    root.destroy()

    if result["value"] is None:
        return "[USER CANCELLED]"
    return result["value"] if result["value"] else "[USER SUBMITTED EMPTY TEXT]"


def ask_user_file_path(prompt: str = "Select a file", must_exist: bool = True) -> str:
    """
    Pop up a native file-picker dialog. Use this whenever the user tells you
    to open/read/edit/save something involving a file but does not give you
    a path — instead of guessing, ask. Returns the absolute path chosen, or
    '[USER CANCELLED]' if they closed the dialog without picking anything.
    """
    if _gui_ask_hook is not None:
        return _gui_ask_hook("file", {"prompt": prompt, "must_exist": must_exist})
    if not _TKINTER_AVAILABLE:
        return "[GUI ERROR] tkinter not available on this system — ask the user for the path in plain text instead."
    root = _gui_root()
    try:
        if must_exist:
            path = _tk_filedialog.askopenfilename(title=prompt, parent=root)
        else:
            path = _tk_filedialog.asksaveasfilename(title=prompt, parent=root)
    finally:
        root.destroy()
    return path if path else "[USER CANCELLED]"


def ask_user_approval(message: str, details: str = "") -> str:
    """
    Pop up an Approve / Decline GUI dialog with two buttons. Use this before
    doing something the user should explicitly sign off on — deleting or
    overwriting files, sending a message, spending money, running a risky
    or irreversible command, etc. Returns 'APPROVED' or 'DECLINED'.
    """
    if _gui_ask_hook is not None:
        return _gui_ask_hook("approval", {"message": message, "details": details})
    if not _TKINTER_AVAILABLE:
        return "[GUI ERROR] tkinter not available on this system — ask the user to approve in plain text instead."
    result = {"value": "DECLINED"}
    root = _gui_root()
    win = _tk.Toplevel(root)
    win.title("Midum requests approval")
    try: win.attributes("-topmost", True)
    except Exception: pass

    _tk.Label(win, text=message, wraplength=420, justify="left",
              padx=16, pady=(16, 4), font=("Segoe UI", 10, "bold")).pack()
    if details:
        _tk.Label(win, text=details, wraplength=420, justify="left", padx=16, pady=(0, 8)).pack()

    def approve():
        result["value"] = "APPROVED"
        win.destroy()

    def decline():
        result["value"] = "DECLINED"
        win.destroy()

    btn_frame = _tk.Frame(win)
    btn_frame.pack(pady=12)
    _tk.Button(btn_frame, text="\u2705 Approve", command=approve, width=12, bg="#d7f5d7").pack(side="left", padx=8)
    _tk.Button(btn_frame, text="\u274c Decline", command=decline, width=12, bg="#f5d7d7").pack(side="left", padx=8)

    win.protocol("WM_DELETE_WINDOW", decline)
    win.grab_set()
    root.wait_window(win)
    root.destroy()
    return result["value"]


def ask_user_choice(question: str, choice_1: str = "", choice_2: str = "",
                     choice_3: str = "", choice_4: str = "", allow_custom: bool = True) -> str:
    """
    Pop up a multiple-choice GUI dialog: your question plus up to 4 options
    you define, and (unless allow_custom=False) a 5th free-text box so the
    user can type something else entirely. Use this to disambiguate what the
    user wants with a couple of taps instead of a back-and-forth in text.
    Returns the exact text of the option the user picked, their custom text,
    or '[USER CANCELLED]' if they closed the dialog.
    """
    if _gui_ask_hook is not None:
        return _gui_ask_hook("choice", {
            "question": question,
            "options": [c for c in (choice_1, choice_2, choice_3, choice_4) if c],
            "allow_custom": allow_custom,
        })
    if not _TKINTER_AVAILABLE:
        return "[GUI ERROR] tkinter not available on this system — ask the user in plain text instead."
    options = [c for c in (choice_1, choice_2, choice_3, choice_4) if c]
    result = {"value": None}
    root = _gui_root()
    win = _tk.Toplevel(root)
    win.title("Midum has a question")
    try: win.attributes("-topmost", True)
    except Exception: pass

    _tk.Label(win, text=question, wraplength=420, justify="left",
              padx=16, pady=(16, 8), font=("Segoe UI", 10, "bold")).pack()

    def choose(opt):
        result["value"] = opt
        win.destroy()

    for opt in options:
        _tk.Button(win, text=opt, width=48, anchor="w",
                   command=lambda o=opt: choose(o)).pack(padx=16, pady=3, fill="x")

    if allow_custom:
        row = _tk.Frame(win)
        row.pack(padx=16, pady=(10, 4), fill="x")
        custom_entry = _tk.Entry(row, width=36)
        custom_entry.pack(side="left", fill="x", expand=True)

        def submit_custom(event=None):
            txt = custom_entry.get().strip()
            if txt:
                choose(txt)

        custom_entry.bind("<Return>", submit_custom)
        _tk.Button(row, text="Other...", command=submit_custom).pack(side="left", padx=(6, 0))

    win.protocol("WM_DELETE_WINDOW", lambda: choose("[USER CANCELLED]"))
    win.grab_set()
    root.wait_window(win)
    root.destroy()
    return result["value"] if result["value"] is not None else "[USER CANCELLED]"


# =============================================================================

