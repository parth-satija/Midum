"""PyInstaller runtime hook: runs before any Midum code in a frozen build.

A windowed (console=False) exe has sys.stdout / sys.stderr set to None, which
makes print(), rich, loguru and friends fail silently or crash -- and hides
every startup error. This redirects both streams to a log file in the user
data directory so problems are diagnosable:

    Windows: %LOCALAPPDATA%\\Midum\\logs\\midum.log   (override root with MIDUM_HOME)
"""
import os
import sys


def _log_path() -> str:
    root = os.environ.get("MIDUM_HOME")
    if not root:
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
            root = os.path.join(base, "Midum")
        elif sys.platform == "darwin":
            root = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Midum")
        else:
            base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
            root = os.path.join(base, "Midum")
    log_dir = os.path.join(root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "midum.log")


if getattr(sys, "frozen", False):
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        if sys.stdout is None or sys.stderr is None:
            path = _log_path()
            # Simple rotation so the log can't grow without bound.
            try:
                if os.path.exists(path) and os.path.getsize(path) > 5 * 1024 * 1024:
                    os.replace(path, path + ".1")
            except OSError:
                pass
            _log = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
            if sys.stdout is None:
                sys.stdout = _log
            if sys.stderr is None:
                sys.stderr = _log
    except Exception:
        # Never let logging setup stop the app from starting.
        pass

    # A windowed (console=False) exe has no console of its own, so every console program it launches
    # (cmd, powershell, tasklist, git, node, python, winget, ...) gets a brand-new console window that
    # flashes on screen for a split second. Make every child process hidden by default. Callers that
    # explicitly ask for a visible console (CREATE_NEW_CONSOLE) or a detached process are left alone.
    if sys.platform == "win32":
        try:
            import subprocess as _subprocess

            _CREATE_NEW_CONSOLE = 0x00000010
            _DETACHED_PROCESS = 0x00000008
            _CREATE_NO_WINDOW = 0x08000000
            _orig_popen_init = _subprocess.Popen.__init__

            def _popen_init_no_window(self, *args, **kwargs):
                # creationflags is the 14th positional parameter of Popen(); if it was passed
                # positionally, don't touch the call.
                if len(args) <= 13:
                    flags = kwargs.get("creationflags", 0) or 0
                    if not (flags & (_CREATE_NEW_CONSOLE | _DETACHED_PROCESS)):
                        kwargs["creationflags"] = flags | _CREATE_NO_WINDOW
                return _orig_popen_init(self, *args, **kwargs)

            _subprocess.Popen.__init__ = _popen_init_no_window
        except Exception:
            pass
