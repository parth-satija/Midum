# --- shell_session.py ---------------------------------------------------
# A persistent WSL2 (Windows) / bash (Linux/Mac) shell that the model can
# drive like its own computer via the execute_wsl_command tool, and that
# the user can also type into directly from the GUI's Shell tab -- both
# sides share the exact same underlying process, so cwd, exported env
# vars, background jobs, activated venvs, etc. all persist across calls
# exactly like a real interactive terminal.
#
# On Windows, WSL2 already mounts the whole Windows filesystem under
# /mnt/<drive letter> (e.g. C:\Users -> /mnt/c/Users), so this gives the
# model (and the user, from this same tab) direct access to the real
# files on the main computer with no extra bridging needed.

import os
import subprocess
import threading
import time
import uuid

_IS_WINDOWS = os.name == "nt"


class ShellSession:
    """One persistent bash process, talked to over stdin/stdout with a
    unique sentinel line after each command so its output can be cleanly
    separated from the next -- the same trick real terminal emulators use
    under the hood."""

    def __init__(self):
        self._proc = None
        self._lock = threading.RLock()
        self._event_hook = None  # fn(kind:str, payload:dict) -- set by the GUI
        # Current working directory of the persistent shell, mirrored here
        # so the GUI can show a real-terminal-style prompt ("<cwd> $")
        # without having to round-trip a `pwd` call of its own. Refreshed
        # from the shell's own $PWD after every command (see run()) and
        # re-primed on (re)start.
        self._cwd = "~"

    def set_event_hook(self, fn):
        """Registered by the GUI (see gui/app.py) so every command and
        every line of output, from either the model or the user, gets
        streamed live to the Shell tab as it happens."""
        self._event_hook = fn

    def get_cwd(self):
        """Best-known current working directory of the persistent shell,
        for the GUI's prompt label -- see Api.get_shell_cwd in gui/app.py."""
        return self._cwd

    def _emit(self, origin, stream, text, cwd=None):
        if self._event_hook and text is not None:
            try:
                payload = {"origin": origin, "stream": stream, "text": text}
                if cwd is not None:
                    payload["cwd"] = cwd
                self._event_hook("shell_line", payload)
            except Exception:
                pass

    def _ensure_started(self):
        if self._proc is not None and self._proc.poll() is None:
            return
        if _IS_WINDOWS:
            argv = ["wsl.exe", "-e", "bash", "--noprofile", "--norc", "-i"]
        else:
            argv = ["bash", "--noprofile", "--norc", "-i"]
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        self._cwd = "~"
        self._emit("system", "meta", "(shell session started)", cwd=self._cwd)
        # Resolve the real starting directory immediately (rather than
        # waiting for the first command) so the very first prompt the
        # user/model sees already shows the actual cwd, not the "~"
        # placeholder above.
        self._refresh_cwd_silently()

    def _refresh_cwd_silently(self):
        """Runs a bare `pwd` through the same marker protocol run() uses,
        but without emitting any cmd/out events -- purely to (re)sync
        self._cwd, e.g. right after (re)starting the process."""
        if self._proc is None:
            return
        marker = f"__MIDUM_PWD_{uuid.uuid4().hex}__"
        try:
            self._proc.stdin.write(f'echo "{marker}$PWD"\n')
            self._proc.stdin.flush()
        except Exception:
            return
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                line = self._proc.stdout.readline()
            except Exception:
                return
            if line == "":
                return
            line = line.rstrip("\n")
            if line.startswith(marker):
                self._cwd = line[len(marker):].strip() or self._cwd
                return

    def run(self, command, origin="model", timeout=60):
        """Run `command` in the persistent shell, streaming each output
        line to the event hook as it arrives, and returning the full
        captured output once the command completes (or `timeout` runs
        out, in which case the session is left running in the
        background -- exactly like leaving a long job in a real
        terminal)."""
        with self._lock:
            try:
                self._ensure_started()
            except Exception as e:
                msg = f"Failed to start WSL2 shell: {e}"
                self._emit(origin, "meta", msg)
                return msg

            marker = f"__MIDUM_DONE_{uuid.uuid4().hex}__"
            # The cwd shown alongside this command is whatever it was
            # BEFORE the command ran (a real terminal's prompt reflects
            # where you typed the command from, not where it left you) --
            # self._cwd only gets updated below, AFTER the command and its
            # trailing $PWD probe both finish.
            self._emit(origin, "cmd", command, cwd=self._cwd)
            try:
                self._proc.stdin.write(command + "\n")
                # Encode both the exit code and the post-command $PWD in one
                # marker line (separated by "::") so a `cd` inside `command`
                # is reflected in the NEXT prompt's cwd, without a second
                # round trip through the shell.
                self._proc.stdin.write(f'echo "{marker}$?::$PWD"\n')
                self._proc.stdin.flush()
            except Exception as e:
                self._proc = None
                msg = f"Shell process died: {e}"
                self._emit(origin, "meta", msg, cwd=self._cwd)
                return msg

            lines = []
            exit_code = [None]
            timed_out = [False]

            def _reader():
                start = time.time()
                while True:
                    if time.time() - start > timeout:
                        timed_out[0] = True
                        break
                    try:
                        line = self._proc.stdout.readline()
                    except Exception:
                        break
                    if line == "":
                        lines.append("[shell process ended unexpectedly]")
                        break
                    line = line.rstrip("\n")
                    if line.startswith(marker):
                        # "<exit_code>::<new_cwd>" -- see the echo written
                        # above. Splitting on "::" rather than a plain ":"
                        # avoids ambiguity with paths that legitimately
                        # contain a colon (rare, but WSL mount points under
                        # /mnt/<drive> can't, so this is just extra safety).
                        raw = line[len(marker):]
                        code_part, sep, cwd_part = raw.partition("::")
                        exit_code[0] = code_part.strip()
                        if sep:
                            self._cwd = cwd_part.strip() or self._cwd
                        break
                    lines.append(line)
                    self._emit(origin, "out", line)

            t = threading.Thread(target=_reader, daemon=True)
            t.start()
            t.join(timeout + 5)

            if timed_out[0] and exit_code[0] is None:
                note = f"[still running after {timeout}s -- session left open, check back with another command]"
                lines.append(note)
                self._emit(origin, "meta", note, cwd=self._cwd)
            else:
                code_txt = f"(exit {exit_code[0]})" if exit_code[0] is not None else "(done)"
                self._emit(origin, "meta", code_txt, cwd=self._cwd)

            output = "\n".join(lines)
            if exit_code[0] is not None:
                output += f"\n[exit code {exit_code[0]}]"
            return output

    def alive(self):
        return self._proc is not None and self._proc.poll() is None

    def restart(self):
        """Kill and drop the current session so the next run() starts a
        fresh one -- exposed to the GUI for a 'restart shell' action."""
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
            self._cwd = "~"
            self._emit("system", "meta", "(shell session restarted)", cwd=self._cwd)


_session = ShellSession()


def get_shell_session() -> ShellSession:
    return _session
