"""Editor tab backend (Monaco).

Three pieces live here:

1. A tiny localhost HTTP server (127.0.0.1, random port, read-only) that serves
   ``gui/editor_assets/editor.html`` and, once downloaded, a local copy of Monaco.
   The editor runs in its own iframe so its AMD loader and CSS can never clash with
   the main UI, and so open files survive switching tabs (the main UI rebuilds
   #tool-content on every tab switch, which would destroy an in-DOM editor).

2. ``ensure_monaco_async()``: downloads the Monaco npm package once into
   ``storage/vendor/monaco`` so the editor works offline and starts fast. Until that
   finishes (or if it fails) the page falls back to the jsDelivr CDN.

3. ``EditorMixin``: the ``editor_*`` methods exposed to the front-end through
   pywebview (``Api`` inherits it). Every method returns a JSON-safe dict with an
   ``ok`` flag and never raises. They are only reachable from the app's own page
   (the Render Surface iframe is sandboxed and cannot call them).
"""
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import artifacts
import config

try:                                    # AI inline suggestions (Gemini, key hopping); optional
    from gui import inline_suggest
except ImportError:
    try:
        import inline_suggest
    except ImportError:
        inline_suggest = None

MONACO_VERSION = "0.52.2"
CDN_BASE = f"https://cdn.jsdelivr.net/npm/monaco-editor@{MONACO_VERSION}/min/vs"

MAX_EDIT_BYTES = 5 * 1024 * 1024         # larger files are not opened in the editor
LIST_LIMIT = 5000                        # max entries returned for one directory
HIDDEN_NAMES = {".git", ".svn", ".hg", "__pycache__", ".DS_Store", "Thumbs.db"}
HIDDEN_SUFFIXES = (".pyc", ".pyo")
SEARCH_SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", "env",
                    "dist", "build", ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".ruff_cache",
                    ".tox", "site-packages"}
SEARCH_MAX_FILES = 30000

_HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(config.STORAGE_DIR, "vendor", "monaco")
STATE_PATH = os.path.join(config.STORAGE_DIR, "editor_state.json")


def _assets_dir():
    cands = [os.path.join(_HERE, "editor_assets")]
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        cands.append(os.path.join(mp, "gui", "editor_assets"))
    for c in cands:
        if os.path.isfile(os.path.join(c, "editor.html")):
            return c
    return cands[0]


# ── local Monaco copy ────────────────────────────────────────────────────────────────────
def monaco_local():
    try:
        if not os.path.isfile(os.path.join(VENDOR_DIR, "vs", "loader.js")):
            return False
        with open(os.path.join(VENDOR_DIR, ".version"), "r", encoding="utf-8") as fh:
            return fh.read().strip() == MONACO_VERSION
    except OSError:
        return False


_dl = {"state": "idle", "error": ""}
_dl_lock = threading.Lock()


def _download_monaco():
    url = f"https://registry.npmjs.org/monaco-editor/-/monaco-editor-{MONACO_VERSION}.tgz"
    tmp_root = VENDOR_DIR + ".tmp"
    shutil.rmtree(tmp_root, ignore_errors=True)
    os.makedirs(tmp_root, exist_ok=True)
    tgz = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tgz") as tf:
            tgz = tf.name
            req = urllib.request.Request(url, headers={"User-Agent": "Midum-Editor"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                shutil.copyfileobj(resp, tf)
        prefix = "package/min/"
        n = 0
        with tarfile.open(tgz, "r:gz") as tar:
            for m in tar:
                if not m.isfile() or not m.name.startswith(prefix + "vs/"):
                    continue
                rel = m.name[len(prefix):]
                parts = rel.split("/")
                if ".." in parts or any(p == "" for p in parts):
                    continue
                dest = os.path.join(tmp_root, *parts)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                src = tar.extractfile(m)
                if src is None:
                    continue
                with src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                n += 1
        if n < 50 or not os.path.isfile(os.path.join(tmp_root, "vs", "loader.js")):
            raise RuntimeError("downloaded Monaco package looks incomplete")
        with open(os.path.join(tmp_root, ".version"), "w", encoding="utf-8") as fh:
            fh.write(MONACO_VERSION)
        shutil.rmtree(VENDOR_DIR, ignore_errors=True)
        os.makedirs(os.path.dirname(VENDOR_DIR), exist_ok=True)
        os.replace(tmp_root, VENDOR_DIR)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        if tgz:
            try:
                os.remove(tgz)
            except OSError:
                pass


def ensure_monaco_async():
    """Start a one-time background download of Monaco (no-op if present or already running)."""
    with _dl_lock:
        if monaco_local() or _dl["state"] == "running":
            return
        _dl["state"] = "running"
        _dl["error"] = ""

    def _run():
        try:
            _download_monaco()
            _dl["state"] = "done"
        except Exception as e:                      # offline etc. -> the page keeps using the CDN
            _dl["state"] = "error"
            _dl["error"] = str(e)

    threading.Thread(target=_run, name="midum-monaco-fetch", daemon=True).start()


# ── localhost asset server ───────────────────────────────────────────────────────────────
mimetypes.add_type("font/ttf", ".ttf")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")


class _Handler(BaseHTTPRequestHandler):
    server_version = "MidumEditor/1.0"

    def log_message(self, *args):            # silence stdout spam
        pass

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_HEAD = lambda self: self.do_GET()     # noqa: E731

    def do_POST(self):
        """Only /api/inline (AI inline suggestions). Same-origin, token-protected, size-capped."""
        try:
            if urlparse(self.path).path != "/api/inline" or inline_suggest is None:
                return self._send(404, b"not found")
            port = self.server.server_address[1]
            hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
            origin = self.headers.get("Origin")
            if (self.headers.get("Host") or "").lower() not in hosts or \
                    (origin and origin not in tuple(f"http://{h}" for h in hosts)):
                return self._send(403, b"forbidden")
            if not inline_suggest.token_ok(self.headers.get("X-Midum-Token", "")):
                return self._send(403, b"forbidden")
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 256 * 1024:
                return self._send(413, b"bad size")
            req = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
            res = inline_suggest.handle(req if isinstance(req, dict) else {})
            return self._send(200, json.dumps(res).encode("utf-8"), "application/json; charset=utf-8")
        except Exception as e:
            try:
                self._send(500, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"), "application/json; charset=utf-8")
            except Exception:
                pass

    def do_GET(self):
        try:
            path = unquote(urlparse(self.path).path)
            if path in ("/", "/editor.html"):
                with open(os.path.join(_assets_dir(), "editor.html"), "r", encoding="utf-8") as fh:
                    html = fh.read()
                base = "/monaco/vs" if monaco_local() else CDN_BASE
                html = html.replace("__MONACO_BASE__", base).replace("__MONACO_VERSION__", MONACO_VERSION)
                html = html.replace("__INLINE_TOKEN__", inline_suggest.TOKEN if inline_suggest else "")
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if path.startswith("/monaco/") and monaco_local():
                root = os.path.realpath(VENDOR_DIR)
                full = os.path.realpath(os.path.join(root, *path[len("/monaco/"):].split("/")))
                if not (full == root or full.startswith(root + os.sep)) or not os.path.isfile(full):
                    return self._send(404, b"not found")
                ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
                with open(full, "rb") as fh:
                    data = fh.read()
                return self._send(200, data, ctype, "public, max-age=86400")
            return self._send(404, b"not found")
        except Exception as e:
            try:
                self._send(500, f"error: {e}".encode("utf-8", "replace"))
            except Exception:
                pass


_server = None
_server_lock = threading.Lock()


def ensure_server():
    """Start (once) and return the port of the localhost asset server."""
    global _server
    with _server_lock:
        if _server is None:
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            httpd.daemon_threads = True
            threading.Thread(target=httpd.serve_forever, name="midum-editor-http", daemon=True).start()
            _server = httpd
        return _server.server_address[1]


# ── persisted UI state ───────────────────────────────────────────────────────────────────
_state_lock = threading.Lock()


def _default_workspace():
    return os.path.dirname(_HERE)            # the midum_pkg folder


_RUN_KINDS = {".py": "python", ".pyw": "python", ".java": "java", ".html": "html", ".htm": "html"}


def _java_main_class(host_path):
    """Fully-qualified name of the class to launch: the `package` line plus the class that holds
    main() (preferring the one named like the file)."""
    import re
    stem = os.path.splitext(os.path.basename(host_path))[0]
    try:
        with open(host_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(512 * 1024)
    except OSError:
        text = ""
    pm = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M)
    cls = stem
    mm = re.search(r"static\s+void\s+main\s*\(", text)
    if mm:
        before = re.findall(r"\bclass\s+(\w+)", text[:mm.start()])
        if before:
            cls = stem if stem in before else before[-1]
    return (pm.group(1) + "." + cls) if pm else cls


def _run_command(kind, target, host_path, shell_kind):
    """The command line that runs `target` (a path in the shell's own notation)."""
    from shell_session import _sq_bash, _sq_ps
    ps = shell_kind == "powershell"
    q = _sq_ps if ps else _sq_bash
    if kind == "python":
        if ps:   # -u: unbuffered, so output streams live
            return ("if (Get-Command python -ErrorAction SilentlyContinue) { python -u " + q(target) + " } "
                    "else { py -3 -u " + q(target) + " }")
        return "python3 -u " + q(target)
    cls = _java_main_class(host_path)     # java: compile to a temp dir, run, clean up (no .class litter)
    if ps:
        return ("$__o = Join-Path ([IO.Path]::GetTempPath()) 'midum_java_run'; "
                "Remove-Item -Recurse -Force $__o -ErrorAction SilentlyContinue; "
                "javac -encoding UTF-8 -d $__o " + q(target) + "; "
                "if ($LASTEXITCODE -eq 0) { java -cp $__o " + cls + " }; "
                "Remove-Item -Recurse -Force $__o -ErrorAction SilentlyContinue")
    return ("__o=$(mktemp -d) && javac -encoding UTF-8 -d \"$__o\" " + q(target) + " && java -cp \"$__o\" " + cls +
            "; __rc=$?; rm -rf \"$__o\"; (exit $__rc)")


def _load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            st = json.load(fh)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def active_workspace():
    """The editor's active workspace folder (what the Explorer shows), or None. This is the
    default start folder of both shells; it is read from the persisted editor state so it
    works even before the Editor tab has ever been opened. Honours the terminal's
    "Link to workspace" toggle."""
    try:
        with _state_lock:
            st = _load_state()
        if st.get("termSync") is False:
            return None
        ws = st.get("workspace")
        if not ws or not os.path.isdir(ws):
            ws = _default_workspace()
        return ws if os.path.isdir(ws) else None
    except Exception:
        return None


def _clean_state(st):
    """Keep only the keys the editor owns, with sane types/sizes."""
    out = {}
    if isinstance(st.get("workspace"), str):
        out["workspace"] = st["workspace"]
    if isinstance(st.get("recent"), list):
        out["recent"] = [p for p in st["recent"] if isinstance(p, str)][:10]
    if isinstance(st.get("tabs"), list):
        tabs = []
        for t in st["tabs"][:60]:
            if isinstance(t, dict) and t.get("kind") in ("file", "artifact"):
                tabs.append({k: t.get(k) for k in ("key", "kind", "path", "id", "name", "preview") if k in t})
        out["tabs"] = tabs
    if isinstance(st.get("active"), str):
        out["active"] = st["active"]
    if isinstance(st.get("expanded"), list):
        out["expanded"] = [p for p in st["expanded"] if isinstance(p, str)][:500]
    if isinstance(st.get("collapsed"), dict):
        out["collapsed"] = {k: bool(v) for k, v in st["collapsed"].items() if k in ("artifacts", "workspace")}
    for k, lo, hi in (("sideWidth", 160, 700), ("fontSize", 8, 40)):
        if isinstance(st.get(k), (int, float)):
            out[k] = max(lo, min(hi, int(st[k])))
    for k in ("wordWrap", "minimap", "autoSave", "termOpen", "termSync", "inlineAI", "aiApprove"):
        if isinstance(st.get(k), bool):
            out[k] = st[k]
    if isinstance(st.get("termHeight"), (int, float)):
        out["termHeight"] = max(90, min(800, int(st["termHeight"])))
    if st.get("termShell") in ("wsl", "powershell"):
        out["termShell"] = st["termShell"]
    return out


def _write_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=1)
    os.replace(tmp, STATE_PATH)


# ── file helpers ─────────────────────────────────────────────────────────────────────────
def _norm(p):
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(p))))


def _decode_bytes(raw):
    """-> (text, encoding, has_bom). Never raises."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace"), "utf-8", True
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16"), "utf-16", False
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-8"), "utf-8", False
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1252"), "cp1252", False
    except UnicodeDecodeError:
        return raw.decode("latin-1"), "latin-1", False


def _encode_text(text, encoding, bom):
    enc = (encoding or "utf-8").lower()
    if enc == "utf-8":
        return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"), "utf-8"
    try:
        return text.encode(enc), enc
    except (UnicodeEncodeError, LookupError):
        return text.encode("utf-8"), "utf-8"           # cannot represent -> fall back to UTF-8


def _is_binary(raw):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    return b"\x00" in raw[:8192]


def _fuzzy(query, rel):
    """Score ``rel`` against ``query`` (subsequence match). None = no match; higher = better."""
    q, s = query.lower(), rel.lower()
    base_start = s.rfind("/") + 1
    score, pos, last = 0, 0, -2
    for ch in q:
        i = s.find(ch, pos)
        if i < 0:
            return None
        score += 3 if i == last + 1 else 1
        if i == 0 or s[i - 1] in "/\\._- ":
            score += 4
        if i >= base_start:
            score += 2
        last, pos = i, i + 1
    return score * 100 - len(rel)


_search_cache = {}      # root -> (timestamp, [rel paths])


def _index_files(root):
    now = time.time()
    hit = _search_cache.get(root)
    if hit and now - hit[0] < 10:
        return hit[1]
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SEARCH_SKIP_DIRS and not d.endswith(".egg-info")]
        for fn in filenames:
            if fn in HIDDEN_NAMES or fn.endswith(HIDDEN_SUFFIXES):
                continue
            files.append(os.path.relpath(os.path.join(dirpath, fn), root).replace("\\", "/"))
            if len(files) >= SEARCH_MAX_FILES:
                break
        if len(files) >= SEARCH_MAX_FILES:
            break
    _search_cache[root] = (now, files)
    return files


def _err(e):
    return {"ok": False, "error": f"{type(e).__name__}: {e}" if not isinstance(e, OSError) else (e.strerror or str(e))}


# ── Api mixin ────────────────────────────────────────────────────────────────────────────
class EditorMixin:
    """``editor_*`` methods exposed to the page through pywebview (see ``Api``)."""

    # -- server / state ---------------------------------------------------------------
    def editor_server_info(self):
        try:
            port = ensure_server()
            ensure_monaco_async()
            return {"ok": True, "url": f"http://127.0.0.1:{port}/editor.html", "monaco_local": monaco_local()}
        except Exception as e:
            return _err(e)

    def editor_get_state(self):
        try:
            with _state_lock:
                st = _load_state()
            ws = st.get("workspace")
            if not ws or not os.path.isdir(ws):
                ws = _default_workspace()
            st["workspace"] = ws
            return {"ok": True, "state": st, "default_workspace": _default_workspace()}
        except Exception as e:
            return _err(e)

    def editor_set_state(self, state):
        try:
            clean = _clean_state(state if isinstance(state, dict) else {})
            with _state_lock:
                _write_state(clean)
            return {"ok": True}
        except Exception as e:
            return _err(e)

    # -- terminal panel (the same persistent shells as the Shell tab) -------------------
    def editor_term_info(self):
        try:
            from shell_session import available_shells
            return {"ok": True, "shells": available_shells()}
        except Exception as e:
            return _err(e)

    def editor_term_history(self, shell="wsl", since=0):
        """Replay of what a shell printed so far (the panel may open after the fact)."""
        try:
            from shell_session import get_shell_session
            return {"ok": True, "lines": get_shell_session(shell).get_history(int(since or 0))}
        except Exception as e:
            return _err(e)

    def editor_term_run(self, shell, command):
        # Same path as typing into the Shell tab (incl. the `edit <file>` command).
        try:
            return self.shell_run_user_command(command, shell)
        except Exception as e:
            return _err(e)

    def editor_term_restart(self, shell="wsl"):
        try:
            return self.shell_restart(shell)
        except Exception as e:
            return _err(e)

    def editor_term_link(self, path, force=False):
        """Link BOTH shells to the editor workspace: remembered for (re)starts, and
        with force=True any running shell is also `cd`'d there (visibly)."""
        try:
            p = _norm(path)
            if not os.path.isdir(p):
                return {"ok": False, "error": "not a folder"}
            from shell_session import get_shell_session
            sessions = [get_shell_session("wsl"), get_shell_session("powershell")]
            for s in sessions:
                s.preferred_cwd = p
            if force:
                def _go():
                    for s in sessions:
                        try:
                            s.link_cwd(p, force=True)
                        except Exception:
                            pass
                threading.Thread(target=_go, daemon=True).start()
            return {"ok": True}
        except Exception as e:
            return _err(e)

    def editor_term_cd(self, shell, path):
        """Explorer > "Open in Terminal": cd one shell to a folder (starting it if needed)."""
        try:
            p = _norm(path)
            if not os.path.isdir(p):
                p = os.path.dirname(p)
            from shell_session import get_shell_session
            s = get_shell_session(shell)

            def _go():
                if s.ensure_ready():
                    return
                target = s.to_shell_path(p)
                if target:
                    s.run(s._cd_command(target), origin="editor", timeout=20)
            threading.Thread(target=_go, daemon=True).start()
            return {"ok": True}
        except Exception as e:
            return _err(e)

    def editor_term_cwd(self, shell="wsl"):
        """The shell's current directory as a host path (for "Open terminal folder as workspace")."""
        try:
            from shell_session import get_shell_session
            s = get_shell_session(shell)
            if not s.alive():
                return {"ok": False, "error": "shell is not running"}
            host = s.to_host_path(".")
            if host and os.path.isdir(host):
                return {"ok": True, "path": host}
            return {"ok": False, "error": "could not resolve the shell's folder"}
        except Exception as e:
            return _err(e)

    # -- file system ------------------------------------------------------------------
    def editor_list_dir(self, path):
        try:
            p = _norm(path)
            entries = []
            truncated = False
            with os.scandir(p) as it:
                for e in it:
                    if e.name in HIDDEN_NAMES or e.name.endswith(HIDDEN_SUFFIXES):
                        continue
                    try:
                        is_dir = e.is_dir()
                        st = e.stat()
                        size, mtime = st.st_size, st.st_mtime
                    except OSError:
                        is_dir, size, mtime = False, 0, 0
                    entries.append({"name": e.name, "path": os.path.join(p, e.name), "is_dir": is_dir,
                                    "size": size, "mtime": mtime})
                    if len(entries) >= LIST_LIMIT:
                        truncated = True
                        break
            entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            return {"ok": True, "path": p, "entries": entries, "truncated": truncated}
        except Exception as e:
            return _err(e)

    def editor_read_file(self, path):
        try:
            p = _norm(path)
            st = os.stat(p)
            if st.st_size > MAX_EDIT_BYTES:
                return {"ok": True, "too_large": True, "size": st.st_size, "mtime": st.st_mtime, "path": p}
            with open(p, "rb") as fh:
                raw = fh.read()
            if _is_binary(raw):
                return {"ok": True, "binary": True, "size": st.st_size, "mtime": st.st_mtime, "path": p}
            text, enc, bom = _decode_bytes(raw)
            return {"ok": True, "path": p, "content": text, "encoding": enc, "bom": bom,
                    "size": st.st_size, "mtime": st.st_mtime,
                    "writable": os.access(p, os.W_OK)}
        except Exception as e:
            return _err(e)

    def editor_write_file(self, path, content, encoding="utf-8", bom=False, expected_mtime=None):
        try:
            p = _norm(path)
            if expected_mtime is not None and os.path.exists(p):
                if abs(os.path.getmtime(p) - float(expected_mtime)) > 0.001:
                    return {"ok": False, "conflict": True, "mtime": os.path.getmtime(p)}
            data, enc = _encode_text(str(content), encoding, bool(bom))
            if len(data) > MAX_EDIT_BYTES * 4:
                return {"ok": False, "error": "content too large to save"}
            tmp = p + ".midum-tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, p)
            st = os.stat(p)
            _search_cache.clear()
            return {"ok": True, "mtime": st.st_mtime, "size": st.st_size, "encoding": enc}
        except Exception as e:
            try:
                os.remove(p + ".midum-tmp")
            except Exception:
                pass
            return _err(e)

    def editor_stat(self, paths):
        out = {}
        try:
            for raw in (paths or [])[:200]:
                p = _norm(raw)
                try:
                    st = os.stat(p)
                    out[raw] = {"exists": True, "mtime": st.st_mtime, "size": st.st_size}
                except OSError:
                    out[raw] = {"exists": False}
            return {"ok": True, "stats": out}
        except Exception as e:
            return _err(e)

    def editor_create(self, path, is_dir=False):
        try:
            p = _norm(path)
            if os.path.exists(p):
                return {"ok": False, "error": "A file or folder with that name already exists."}
            if is_dir:
                os.makedirs(p)
            else:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "xb"):
                    pass
            _search_cache.clear()
            return {"ok": True, "path": p}
        except Exception as e:
            return _err(e)

    def editor_rename(self, old_path, new_path):
        try:
            a, b = _norm(old_path), _norm(new_path)
            if not os.path.exists(a):
                return {"ok": False, "error": "The item no longer exists."}
            if os.path.exists(b) and os.path.normcase(a) != os.path.normcase(b):
                return {"ok": False, "error": "A file or folder with that name already exists."}
            os.makedirs(os.path.dirname(b), exist_ok=True)
            os.rename(a, b)
            _search_cache.clear()
            return {"ok": True, "path": b}
        except Exception as e:
            return _err(e)

    def editor_delete(self, path):
        try:
            p = _norm(path)
            if os.path.dirname(p) == p:
                return {"ok": False, "error": "Refusing to delete a drive/filesystem root."}
            trashed = False
            try:
                from send2trash import send2trash          # optional: recycle bin instead of permanent delete
                send2trash(p)
                trashed = True
            except ImportError:
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
            _search_cache.clear()
            return {"ok": True, "trashed": trashed}
        except Exception as e:
            return _err(e)

    # -- run the open file ------------------------------------------------------------
    def editor_run_file(self, path, shell="wsl"):
        """Run button: .py / .java run in the chosen persistent shell (output streams into the
        terminal panel and the Shell tab); .html/.htm open in the system browser (a new browser
        tab -- never the Render Surface). Runs from the shell's current folder (the workspace by
        default), like VS Code's Run Python File."""
        try:
            p = _norm(path)
            if not os.path.isfile(p):
                return {"ok": False, "error": "File not found - save it first"}
            kind = _RUN_KINDS.get(os.path.splitext(p)[1].lower())
            if not kind:
                return {"ok": False, "error": "Run supports .py, .java and .html files"}
            if kind == "html":
                import webbrowser, pathlib
                webbrowser.open(pathlib.Path(p).as_uri(), new=2)
                return {"ok": True, "kind": "html"}
            from shell_session import get_shell_session
            s = get_shell_session(shell)

            def _go():
                if s.ensure_ready():
                    return
                target = s.to_shell_path(p)
                if not target:
                    s._emit("system", "meta", "run: could not translate the file path for this shell", cwd=s.get_cwd())
                    return
                s.run(_run_command(kind, target, p, s.kind), origin="editor", timeout=3600)
            threading.Thread(target=_go, daemon=True).start()
            return {"ok": True, "kind": kind}
        except Exception as e:
            return _err(e)

    def editor_reveal(self, path):
        try:
            p = _norm(path)
            if sys.platform == "win32":
                subprocess.Popen(["explorer", f"/select,{p}"])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", p])
            else:
                subprocess.Popen(["xdg-open", p if os.path.isdir(p) else os.path.dirname(p)])
            return {"ok": True}
        except Exception as e:
            return _err(e)

    def editor_pick_folder(self):
        try:
            import webview
            res = self.window.create_file_dialog(webview.FOLDER_DIALOG)
            if res:
                return {"ok": True, "path": res[0] if isinstance(res, (list, tuple)) else res}
            return {"ok": True, "path": ""}
        except Exception as e:
            return _err(e)

    def editor_pick_file(self):
        try:
            import webview
            res = self.window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
            return {"ok": True, "paths": list(res) if res else []}
        except Exception as e:
            return _err(e)

    def editor_search_files(self, root, query, limit=60):
        try:
            r = _norm(root)
            files = _index_files(r)
            q = (query or "").strip()
            limit = max(1, min(int(limit or 60), 200))
            if not q:
                hits = [(0, f) for f in files[:limit]]
            else:
                hits = []
                for f in files:
                    s = _fuzzy(q, f)
                    if s is not None:
                        hits.append((s, f))
                hits.sort(key=lambda x: -x[0])
            return {"ok": True, "results": [{"path": os.path.join(r, *f.split("/")), "rel": f,
                                             "name": f.rsplit("/", 1)[-1]} for _, f in hits[:limit]]}
        except Exception as e:
            return _err(e)

    # -- artifacts --------------------------------------------------------------------
    def editor_list_artifacts(self):
        try:
            return {"ok": True, "artifacts": artifacts.editor_list()}
        except Exception as e:
            return _err(e)

    def editor_open_artifact(self, art_id):
        try:
            return artifacts.editor_read(art_id)
        except Exception as e:
            return _err(e)

    def editor_save_artifact(self, art_id, content, expected_version=None):
        try:
            return artifacts.editor_save(art_id, content, expected_version)
        except Exception as e:
            return _err(e)

    def editor_save_artifact_copy(self, name, content):
        try:
            return artifacts.editor_save_copy(name, content)
        except Exception as e:
            return _err(e)

    def editor_artifact_stat(self, ids):
        try:
            return {"ok": True, "stats": artifacts.editor_stat_ids(ids)}
        except Exception as e:
            return _err(e)

    # -- chat <-> editor link (editor_link.py): the page answers the model's requests here ----
    def editor_ai_hello(self):
        """The page has booted and can answer editor_ai_request events."""
        try:
            import editor_link
            editor_link.hello()
            return {"ok": True}
        except Exception as e:
            return _err(e)

    def editor_ai_reply(self, req_id, result):
        """The page's answer to one editor_ai_request (wakes the model's waiting tool call)."""
        try:
            import editor_link
            return {"ok": bool(editor_link.deliver(req_id, result))}
        except Exception as e:
            return _err(e)

    # -- model-facing hook (installed on artifacts._gui_editor_hook by Api.__init__) ------
    def _handle_editor(self, kind, payload):
        if kind == "open_artifact":
            self._push_event("editor_open_artifact", payload)
            return "ok"
        if kind == "open_file":
            self._push_event("editor_open_file", payload)
            return "ok"
        if kind == "open_folder":
            self._push_event("editor_open_folder", payload)
            return "ok"
        if kind == "ai_request":                       # {id, op, args}: see editor_link.py
            self._push_event("editor_ai_request", payload)
            return "ok"
        if kind == "artifact_changed":
            self._push_event("editor_artifact_changed", payload)
            return "ok"
        return f"unknown editor hook kind: {kind}"
