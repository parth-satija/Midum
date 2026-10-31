# --- shell_session.py ---------------------------------------------------
# Persistent shells that the model can drive like its own computer AND that
# the user can type into directly -- from the GUI's Shell tab and from the
# terminal panel inside the Editor tab. Two shells are available:
#
#   "wsl"        WSL2 bash on Windows (plain bash on Linux/macOS)
#                -> model tool: execute_wsl_command
#   "powershell" Windows PowerShell on Windows (pwsh if present elsewhere)
#                -> model tool: execute_terminal_command
#
# Each shell is ONE long-lived process shared by every caller, so cwd,
# exported env vars, background jobs, activated venvs, $variables, etc. all
# persist across calls exactly like a real interactive terminal. Every
# command and every line of output, from either side, is streamed to the GUI
# through the event hook as a "shell_line" event and also kept in a bounded
# per-shell history so a UI that opens later (e.g. the Editor's terminal
# panel) can replay what it missed.
#
# On Windows, WSL2 already mounts the whole Windows filesystem under
# /mnt/<drive letter> (e.g. C:\Users -> /mnt/c/Users), so the WSL shell and
# the editor see the very same files.
#
# Wire protocol (both shells): every command is sent as ONE ascii line that
# carries the command base64-encoded, followed by a sentinel line that
# reports "<exit code>::<cwd>". That makes the stream immune to quoting
# problems, multi-line commands, syntax errors, non-ascii text, and (in
# bash) history expansion, and it lets output be cleanly separated from the
# next command's -- the same trick terminal emulators use under the hood.

import base64
import collections
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid

_IS_WINDOWS = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000

HISTORY_CAP = 2000

# CSI / OSC escape sequences -- stripped so colourised tool output doesn't
# show up as "[32m" garbage in the (non-ANSI) GUI terminals.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _sq_bash(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _sq_ps(s):
    return "'" + s.replace("'", "''") + "'"


def _isdir(p):
    try:
        return bool(p) and os.path.isdir(p)
    except Exception:
        return False


def _default_start_dir():
    """Where a fresh shell starts when the editor hasn't linked it to a
    workspace: Midum's live startup dir if it has one, else $HOME."""
    try:
        import config
        d = config.get_startup_dir()
        if _isdir(d):
            return d
    except Exception:
        pass
    return os.path.expanduser("~")


_workspace_resolver = None   # fn() -> active editor workspace folder or None (set by the GUI)


def set_workspace_resolver(fn):
    """Register how to look up the editor's active workspace folder."""
    global _workspace_resolver
    _workspace_resolver = fn


def _workspace_dir():
    try:
        d = _workspace_resolver() if _workspace_resolver else None
        return d if _isdir(d) else None
    except Exception:
        return None


class _Dispatcher:
    """Delivers shell events to the GUI OFF the shell's reader thread, coalesced.

    The GUI hook (app.py _push_event) does a blocking evaluate_js round trip
    per call. Calling it once per output line from inside the read loop made
    the shell crawl: a 500-line `ls -R` meant 500 sequential round trips, and
    run() could not return until the last one finished. Instead lines go into a
    queue and one worker sends them in batches (one push per ~30 ms burst), so
    the shell never waits on the GUI."""

    def __init__(self):
        self._q = queue.Queue()
        self._t = None
        self._lk = threading.Lock()

    def put(self, hook, payload):
        with self._lk:
            if self._t is None or not self._t.is_alive():
                self._t = threading.Thread(target=self._loop, daemon=True)
                self._t.start()
        self._q.put((hook, payload))

    def _loop(self):
        while True:
            items = [self._q.get()]
            deadline = time.time() + 0.03
            while len(items) < 500:
                try:
                    items.append(self._q.get(timeout=max(0.0, deadline - time.time())))
                except queue.Empty:
                    break
            cur, grp = None, []
            for hook, p in items + [(None, None)]:
                if grp and hook is not cur:
                    try:
                        cur("shell_lines", {"lines": grp})
                    except Exception:
                        pass
                    grp = []
                cur = hook
                if p is not None:
                    grp.append(p)


_dispatch = _Dispatcher()


class _Session:
    """One persistent shell process. Subclasses supply how to spawn it and
    how to wrap a command in the wire protocol."""

    kind = "wsl"
    prompt_style = "bash"       # how the GUI should draw the prompt

    def __init__(self):
        self._proc = None
        self._lock = threading.RLock()          # serialises whole commands
        self._emit_lock = threading.Lock()      # protects seq + history
        self._event_hook = None                 # fn(kind:str, payload:dict) -- set by the GUI
        self._cwd = "~"
        self._seq = 0
        self._history = collections.deque(maxlen=HISTORY_CAP)
        self._q = None                          # lines pumped from the current process
        self._open = {}                         # marker -> origin, for commands that outlived their timeout
        self.preferred_cwd = None               # set by the editor link (host-OS path)
        self.start_error = None

    # -- identity / availability -------------------------------------------
    def _start_dir(self):
        """Where a (re)started shell begins: the folder the editor linked to it, else the
        editor's ACTIVE WORKSPACE folder, else None (caller falls back further)."""
        return self.preferred_cwd if _isdir(self.preferred_cwd) else _workspace_dir()

    @property
    def label(self):
        return "WSL2" if (self.kind == "wsl" and _IS_WINDOWS) else "Bash"

    def available(self):
        return True

    # -- GUI plumbing ---------------------------------------------------------
    def set_event_hook(self, fn):
        self._event_hook = fn

    def get_cwd(self):
        return self._cwd

    def get_history(self, since=0):
        with self._emit_lock:
            return [dict(p) for p in self._history if p["seq"] > since]

    def _emit(self, origin, stream, text, cwd=None):
        if text is None:
            return
        if stream == "out":
            text = _ANSI_RE.sub("", text)
        with self._emit_lock:
            self._seq += 1
            payload = {"shell": self.kind, "origin": origin, "stream": stream,
                       "text": text, "seq": self._seq}
            if cwd is not None:
                payload["cwd"] = cwd
            self._history.append(payload)
        hook = self._event_hook or _module_hook
        if hook:
            _dispatch.put(hook, dict(payload))

    # -- process management -------------------------------------------------
    def alive(self):
        return self._proc is not None and self._proc.poll() is None

    def _spawn_spec(self):
        raise NotImplementedError

    def _wrap(self, command, marker):
        raise NotImplementedError

    def _post_start(self):
        pass

    def _cd_command(self, path):
        raise NotImplementedError

    @staticmethod
    def _pump(proc, q):
        """Reader thread: one per process, feeds every stdout/stderr line to
        a queue so output is never lost between commands (e.g. a background
        job finishing late) and a blocked read can never steal lines from
        the next command."""
        try:
            while True:
                line = proc.stdout.readline()
                if line == "":
                    break
                q.put(line.rstrip("\r\n"))
        except Exception:
            pass
        q.put(None)

    def _start(self):
        """Start the process if needed. Returns None or an error string."""
        if self.alive():
            return None
        try:
            argv, start_dir = self._spawn_spec()
            kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          encoding="utf-8", errors="replace", bufsize=1)
            if start_dir:
                kwargs["cwd"] = start_dir
            if _IS_WINDOWS:
                kwargs["creationflags"] = _CREATE_NO_WINDOW
            self._proc = subprocess.Popen(argv, **kwargs)
        except Exception as e:
            self._proc = None
            self.start_error = f"Failed to start {self.label} shell: {e}"
            self._emit("system", "meta", self.start_error, cwd=self._cwd)
            return self.start_error
        self.start_error = None
        self._q = queue.Queue()
        self._open = {}
        threading.Thread(target=self._pump, args=(self._proc, self._q), daemon=True).start()
        self._cwd = "~"
        self._emit("system", "meta", "(shell session started)", cwd=self._cwd)
        # Quiet init: swallows any startup chatter (e.g. bash's "no job
        # control" warning) and resolves the real starting directory so the
        # very first prompt already shows the actual cwd.
        try:
            self._post_start()
        except Exception:
            pass
        return None

    def _drop_proc(self):
        proc, self._proc = self._proc, None
        self._open = {}
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def _exec(self, command, origin, timeout, emit):
        """Send one wrapped command and collect its output.
        Returns (lines, exit_code_str_or_None, timed_out, ended)."""
        marker = f"__MIDUM_DONE_{uuid.uuid4().hex}__"
        try:
            self._proc.stdin.write(self._wrap(command, marker))
            self._proc.stdin.flush()
        except Exception as e:
            self._drop_proc()
            msg = f"Shell process died: {e}"
            if emit:
                self._emit(origin, "meta", msg, cwd=self._cwd)
            return [msg], None, False, True

        q = self._q
        lines, code = [], None
        timed_out = ended = False
        deadline = time.time() + timeout

        def out(line, who):
            line = _ANSI_RE.sub("", line)
            if who == origin:
                lines.append(line)
            if emit:
                self._emit(who, "out", line)

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = q.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                if not self.alive() and q.empty():
                    lines.append("[shell process ended unexpectedly]")
                    ended = True
                    break
                continue
            if line is None:
                lines.append("[shell process ended unexpectedly]")
                ended = True
                self._proc = None
                break

            # Marker for an EARLIER command that had outlived its timeout?
            stale = next(((m, o) for m, o in self._open.items() if m in line), None)
            if stale:
                m, who = stale
                j = line.find(m)
                if line[:j]:
                    out(line[:j], who)
                c, sep, new_cwd = line[j + len(m):].partition("::")
                del self._open[m]
                if sep:
                    self._cwd = new_cwd.strip() or self._cwd
                if emit:
                    self._emit(who, "meta", f"(earlier command finished, exit {c.strip() or '?'})", cwd=self._cwd)
                continue

            j = line.find(marker)
            if j >= 0:
                if line[:j]:
                    out(line[:j], origin)          # output that had no trailing newline
                c, sep, new_cwd = line[j + len(marker):].partition("::")
                code = c.strip()
                if sep:
                    self._cwd = new_cwd.strip() or self._cwd
                break
            out(line, origin)

        if timed_out:
            self._open[marker] = origin
        return lines, code, timed_out, ended

    # -- public API -------------------------------------------------------------
    def ensure_ready(self):
        """Start the shell if it isn't running. None on success, else an error string."""
        with self._lock:
            return self._start()

    def run(self, command, origin="model", timeout=60):
        """Run `command` in the persistent shell, streaming each output line
        to the event hook as it arrives, and return the full captured output
        once it completes (or `timeout` runs out, in which case the session
        is left running in the background -- exactly like leaving a long job
        in a real terminal)."""
        with self._lock:
            err = self._start()
            if err:
                return err
            if self._open:
                self._emit("system", "meta",
                           "(an earlier command is still running -- this one is queued behind it)", cwd=self._cwd)
            # The cwd shown next to a command is where it was TYPED (what a
            # real prompt shows); self._cwd is updated after it finishes.
            self._emit(origin, "cmd", command, cwd=self._cwd)
            lines, code, timed_out, ended = self._exec(command, origin, timeout, emit=True)
            if timed_out and code is None:
                note = f"[still running after {timeout}s -- session left open, check back with another command]"
                lines.append(note)
                self._emit(origin, "meta", note, cwd=self._cwd)
            elif not ended:
                self._emit(origin, "meta", f"(exit {code})" if code is not None else "(done)", cwd=self._cwd)
            output = "\n".join(lines)
            if code is not None:
                output += f"\n[exit code {code}]"
            return output

    def exec_silent(self, command, timeout=15):
        """Run a command WITHOUT showing it in the GUI (used for helper
        lookups such as path conversion). Returns (output, exit_code_or_None)."""
        with self._lock:
            if self._start():
                return "", None
            lines, code, timed_out, _ = self._exec(command, "system", timeout, emit=False)
            return "\n".join(lines), code

    def restart(self):
        """Kill the current session so the next run() starts a fresh one."""
        with self._lock:
            self._drop_proc()
            self._cwd = "~"
            self._emit("system", "meta", "(shell session restarted)", cwd=self._cwd)

    # -- editor linking ---------------------------------------------------------
    def to_shell_path(self, host_path):
        """Host-OS path -> path in this shell's own notation."""
        return host_path

    def to_host_path(self, typed):
        """A path typed in this shell (relative to its cwd) -> host-OS path."""
        p = (typed or "").strip()
        if p == "~" or p.startswith("~/") or p.startswith("~\\"):
            p = os.path.expanduser(p)
        if not os.path.isabs(p):
            base = self._cwd if self._cwd and self._cwd != "~" else _default_start_dir()
            p = os.path.join(base, p)
        return os.path.normpath(p)

    def link_cwd(self, host_path, force=False):
        """Point this shell at an editor workspace. Always remembered for the
        next (re)start; with force=True a running shell is also `cd`'d there,
        visibly, like typing it."""
        if not _isdir(host_path):
            return {"ok": False, "error": "not a folder"}
        self.preferred_cwd = host_path
        if force and self.alive():
            target = self.to_shell_path(host_path)
            if target:
                self.run(self._cd_command(target), origin="editor", timeout=20)
        return {"ok": True}


class WslSession(_Session):
    kind = "wsl"
    prompt_style = "bash"

    def _spawn_spec(self):
        argv = (["wsl.exe", "-e", "bash", "--noprofile", "--norc", "--noediting", "-i"] if _IS_WINDOWS
                else ["bash", "--noprofile", "--norc", "--noediting", "-i"])
        start = self._start_dir()
        return argv, start

    def _wrap(self, command, marker):
        # eval of the decoded text keeps cd/export/functions in THIS shell;
        # </dev/null stops a stray `cat`/`read` from swallowing the sentinel.
        # The sentinel is split in two so its full text never appears in the
        # command line itself -- even if something echoes the input back, the
        # echo can't be mistaken for the sentinel.
        h = len(marker) // 2
        return ("eval \"$(printf %s '" + _b64(command) + "' | base64 -d)\" </dev/null; "
                "printf '%s%s%s::%s\\n' '" + marker[:h] + "' '" + marker[h:] + "' \"$?\" \"$PWD\"\n")

    def _post_start(self):
        # Silence prompts (bash -i prints them to stderr even without a tty).
        self._exec("PS1=''; PS2=''; PROMPT_COMMAND=''", "system", 15, emit=False)

    def _cd_command(self, path):
        return "cd -- " + _sq_bash(path)

    def to_shell_path(self, host_path):
        if not _IS_WINDOWS:
            return host_path
        out, code = self.exec_silent("wslpath -a -u " + _sq_bash(host_path))
        out = out.strip().splitlines()
        return out[-1].strip() if out and code == "0" else None

    def to_host_path(self, typed):
        if not _IS_WINDOWS:
            return super().to_host_path(typed)
        p = (typed or "").strip()
        if p == "~" or p.startswith("~/"):
            esc = p[1:].replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
            expr = '"$HOME' + esc + '"'
        else:
            expr = _sq_bash(p)
        # realpath -m resolves relative to the shell's real cwd; wslpath -w
        # then maps /mnt/c/.. -> C:\.. and native paths -> \\wsl.localhost\..
        out, code = self.exec_silent("wslpath -w -- \"$(realpath -m -- " + expr + ")\"")
        out = out.strip().splitlines()
        return out[-1].strip() if out and code == "0" else None


def _find_powershell():
    override = os.environ.get("MIDUM_POWERSHELL")
    if override:
        return override
    if _IS_WINDOWS:
        return shutil.which("powershell") or shutil.which("pwsh") or "powershell.exe"
    return shutil.which("pwsh") or shutil.which("powershell")


# Quiet init for PowerShell: UTF-8 (no BOM) output, no progress bars (they
# would be written into the pipe as junk), never prompt.
_PS_INIT = (
    "try{$__e=New-Object System.Text.UTF8Encoding $false;[Console]::OutputEncoding=$__e;$OutputEncoding=$__e}catch{};"
    "$ProgressPreference='SilentlyContinue';"
)


class PowerShellSession(_Session):
    kind = "powershell"
    prompt_style = "ps"

    @property
    def label(self):
        return "PowerShell"

    def available(self):
        return bool(_find_powershell())

    def _spawn_spec(self):
        exe = _find_powershell()
        if not exe:
            raise RuntimeError("PowerShell was not found on this machine")
        start = self._start_dir() or _default_start_dir()
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "-"], start

    def _wrap(self, command, marker):
        # The footer runs INSIDE the evaluated text so $? / $LASTEXITCODE
        # describe the user's last statement, not Invoke-Expression itself.
        script = command + "\n$global:__ok=$?;$global:__ec=$LASTEXITCODE\n"
        return ("$global:__ok=$false;$global:__ec=$null;"
                "try{Invoke-Expression ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + _b64(script) + "')))}"
                "catch{[Console]::Error.WriteLine(($_|Out-String).TrimEnd())};"
                "Write-Output ('" + marker[:len(marker) // 2] + "'+'" + marker[len(marker) // 2:] + "'+$(if($global:__ok){0}elseif($global:__ec){$global:__ec}else{1})+'::'+(Get-Location).Path)\n")

    def _post_start(self):
        self._exec(_PS_INIT, "system", 20, emit=False)

    def _cd_command(self, path):
        return "Set-Location -LiteralPath " + _sq_ps(path)


_SESSIONS = {"wsl": WslSession(), "powershell": PowerShellSession()}
_module_hook = None

_ALIASES = {"wsl": "wsl", "bash": "wsl", "wsl2": "wsl",
            "powershell": "powershell", "pwsh": "powershell", "ps": "powershell"}


def canonical_kind(kind):
    return _ALIASES.get(str(kind or "wsl").strip().lower(), "wsl")


def get_shell_session(kind="wsl"):
    """The shared session for `kind` ("wsl" by default -- keeps the original
    zero-argument call working)."""
    return _SESSIONS[canonical_kind(kind)]


_autostart_done = False
_AUTOSTART_DELAY = 1.5      # lets the GUI register the workspace resolver first, so shells open in the right folder


def autostart_shells(delay=_AUTOSTART_DELAY):
    """Start both persistent shells in the background (once per process) so they are already
    running when the app, the Shell tab, the Editor's terminal panel, or the model first needs
    them. Set MIDUM_SHELL_AUTOSTART=0 to disable. Never blocks and never raises."""
    global _autostart_done
    if _autostart_done or os.environ.get("MIDUM_SHELL_AUTOSTART", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    _autostart_done = True

    def _boot(sess):
        try:
            if delay:
                time.sleep(delay)
            if not sess.available():
                return
            if sess.kind == "wsl" and _IS_WINDOWS and not shutil.which("wsl.exe") and not shutil.which("wsl"):
                return                      # no WSL installed: do not print a start error at every launch
            sess.ensure_ready()
        except Exception:
            pass

    for s in _SESSIONS.values():
        threading.Thread(target=_boot, args=(s,), name=f"midum-shell-autostart-{s.kind}", daemon=True).start()


def set_shell_event_hook(fn):
    """Register the GUI's event sink on every session at once. The GUI calls this once at startup,
    which is also the moment the shells are started in the background (see autostart_shells)."""
    global _module_hook
    _module_hook = fn
    for s in _SESSIONS.values():
        s.set_event_hook(fn)
    if fn is not None:
        autostart_shells()


def available_shells():
    return [{"kind": k, "label": s.label, "available": bool(s.available()), "alive": s.alive(),
             "cwd": s.get_cwd(), "prompt": s.prompt_style} for k, s in _SESSIONS.items()]
