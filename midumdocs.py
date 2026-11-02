"""MidumDocs -- LaTeX documents as artifacts, edited in a page editor compiled locally or by an online service.

The editor POSTs the source to this module's localhost server, which compiles it and returns the PDF:
  * with a TeX distribution installed on this PC (TeX Live / MiKTeX / Tectonic: pdflatex by default), fully offline; or
  * when none is installed, on an online compiler (latex.ytotech.com, falling back to texlive.net), so users do not
    need a multi-gigabyte TeX install. Online compiling uploads the document source to that service.
config.py overrides: ``MIDUMDOCS_LATEX_BACKEND`` (auto | local | online), ``MIDUMDOCS_ONLINE_PROVIDER``
(auto | ytotech | texlive.net), ``MIDUMDOCS_LATEX_PATH`` (file or folder) and ``MIDUMDOCS_LATEX_ENGINE``.

A MidumDoc is an ordinary artifact of type ``latex`` (extension ``.tex``): it lives in the artifact
store, is versioned, and the model edits it with the same tools as every other artifact
(create_artifact / update_artifact / rewrite_artifact / revert_artifact). What this module adds is:

1. A tiny localhost HTTP server (127.0.0.1, random port) that serves the editor page
   (``gui/midumdocs_assets/editor.html``), a few pinned, cached CDN libraries (CodeMirror 5, pdf.js; downloaded
   once, then served from disk) and a ``POST /compile`` endpoint that runs the local LaTeX compiler.
2. Local compiler detection (``compiler_info``) and the compile routine (``compile_latex``).
3. Live editor state (cursor, selection, dirty flag, last compile result) pushed by the editor, which
   the model sees on every step (``prompt_block``) and can query/act on with the ``midumdoc_*`` tools.
4. The glue the GUI calls (``user_save``, ``revert_last``, ...) and the hook the artifact store uses to
   open / refresh the editor when the model creates or changes a ``latex`` artifact.

Offsets exchanged with the editor are UTF-16 code units (what JavaScript uses); the helpers below
convert, so emoji and other astral characters cannot shift a selection.
"""
import base64
import glob
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import html as _html
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import artifacts
import config

# ── constants ────────────────────────────────────────────────────────────────────────────
TOOL_NAMES = ("create_midumdoc", "midumdoc_state", "midumdoc_replace_selection", "midumdoc_insert_at_cursor",
              "list_midumdocs", "open_midumdoc")

USER_NOTE = "edited in MidumDocs"
COALESCE_SECS = 90                  # user autosaves within this window overwrite the same version
REQUEST_TTL = 600                   # seconds a prompt-box selection snapshot stays usable
WAIT_SYNC_SECS = 4.0
WAIT_COMPILE_SECS = 75.0
SELECTION_PROMPT_CHARS = 2500
CONTEXT_LINES = 5
LIST_MAX = 40                       # most MidumDocs list_midumdocs returns in one call

# Local LaTeX compiler (offline). Optional config.py overrides:
#   MIDUMDOCS_LATEX_PATH   full path to the compiler exe, or to the folder that contains it
#   MIDUMDOCS_LATEX_ENGINE pdflatex (default) | xelatex | lualatex | tectonic
LATEX_ENGINE = str(getattr(config, "MIDUMDOCS_LATEX_ENGINE", "") or "pdflatex")
LATEX_PATH = str(getattr(config, "MIDUMDOCS_LATEX_PATH", "") or os.environ.get("MIDUMDOCS_LATEX_PATH", "") or "")
COMPILE_TIMEOUT = 180               # seconds per compiler run
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_PASSES = 3                      # re-runs for references / table of contents
_ENGINE_NAMES = ("pdflatex", "xelatex", "lualatex", "tectonic")
NO_COMPILER_MSG = ("No local LaTeX compiler found, and online compiling is switched off. Install TeX Live (or MiKTeX), "
                   "set MIDUMDOCS_LATEX_PATH in config.py to pdflatex.exe (or the folder containing it), or set "
                   "MIDUMDOCS_LATEX_BACKEND to \"auto\" / \"online\" to compile on an online service instead.")

# Online compiling (no TeX install needed). Optional config.py overrides:
#   MIDUMDOCS_LATEX_BACKEND   auto (default: a local compiler if one exists, otherwise online) | local | online
#   MIDUMDOCS_ONLINE_PROVIDER auto (default: try each service in turn) | ytotech | texlive.net
# NOTE: online compiling uploads the document's source text to that third-party service. Set the backend to
# "local" to guarantee nothing ever leaves this PC.
BACKEND = str(getattr(config, "MIDUMDOCS_LATEX_BACKEND", "") or os.environ.get("MIDUMDOCS_LATEX_BACKEND", "") or "auto")
ONLINE_PROVIDER = str(getattr(config, "MIDUMDOCS_ONLINE_PROVIDER", "") or "auto").strip().lower()
ONLINE_TIMEOUT = 120                # seconds per online request
YTOTECH_URL = "https://latex.ytotech.com/builds/sync"
TEXLIVE_NET_URL = "https://texlive.net/cgi-bin/latexcgi"

# Pinned, allow-listed third-party files. Fetched once through the local server and cached on disk, so the
# editor page stays same-origin (pdf.js needs that for its worker).
_JSD = "https://cdn.jsdelivr.net/npm/"
VENDOR_FILES = {
    "codemirror/codemirror.js": _JSD + "codemirror@5.65.16/lib/codemirror.js",
    "codemirror/codemirror.css": _JSD + "codemirror@5.65.16/lib/codemirror.css",
    "codemirror/stex.js": _JSD + "codemirror@5.65.16/mode/stex/stex.js",
    "codemirror/matchbrackets.js": _JSD + "codemirror@5.65.16/addon/edit/matchbrackets.js",
    "codemirror/closebrackets.js": _JSD + "codemirror@5.65.16/addon/edit/closebrackets.js",
    "codemirror/active-line.js": _JSD + "codemirror@5.65.16/addon/selection/active-line.js",
    "pdfjs/pdf.min.js": _JSD + "pdfjs-dist@3.11.174/build/pdf.min.js",
    "pdfjs/pdf.worker.min.js": _JSD + "pdfjs-dist@3.11.174/build/pdf.worker.min.js",
}

_HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(config.STORAGE_DIR, "vendor", "midumdocs")
ENGINE_DIR = os.path.join(VENDOR_DIR, "swiftlatex")  # legacy path, unused (kept so old imports do not break)

NEW_DOC_TEMPLATE = r"""\documentclass[11pt]{article}
\usepackage[margin=1in]{geometry}
\usepackage{amsmath,amssymb}

\title{Untitled}
\author{}
\date{\today}

\begin{document}
\maketitle

\section{Introduction}
Start writing here.

\end{document}
"""

_gui_hook = None        # set by gui.app.Api: hook(kind, payload) -> str   (kinds: open | changed | close)

_lock = threading.RLock()
_cv = threading.Condition(_lock)
_tls = threading.local()

_live = {"id": None, "open": False, "version": 0, "dirty": False, "length": -1,
         "cursor": None, "selection": None, "compile": None, "ts": 0.0}
_request = None         # frozen selection from the prompt box: {id, version, from, to, text, ...}
_last_id = None         # last document that was open (for the "reopen" button)
_last_chat = None       # last chat id seen by on_chat_changed (None = not seen yet)


# ── assets + engine + local server ───────────────────────────────────────────────────────
def _assets_dir():
    cands = [os.path.join(_HERE, "gui", "midumdocs_assets")]
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        cands.append(os.path.join(mp, "gui", "midumdocs_assets"))
    for c in cands:
        if os.path.isfile(os.path.join(c, "editor.html")):
            return c
    return cands[0]


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


# ── local LaTeX compiler (offline) ───────────────────────────────────────────────────────
def _exe_names(name):
    return [name + ".exe", name] if os.name == "nt" else [name]


def _extra_bin_dirs():
    """Usual install locations, for when the TeX bin folder is not on PATH (e.g. Midum started before the install)."""
    dirs = []
    if os.name == "nt":
        bases = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA")]
        for b in filter(None, bases):
            dirs += glob.glob(os.path.join(b, "MiKTeX*", "miktex", "bin", "x64"))
            dirs += glob.glob(os.path.join(b, "Programs", "MiKTeX*", "miktex", "bin", "x64"))
        for root in ("C:\\", "D:\\", "E:\\"):
            dirs += glob.glob(os.path.join(root, "texlive", "*", "bin", "*"))
        dirs += glob.glob(os.path.join(os.path.expanduser("~"), "texlive", "*", "bin", "*"))
    else:
        dirs += ["/Library/TeX/texbin", "/usr/local/bin", "/opt/homebrew/bin"]
        dirs += glob.glob("/usr/local/texlive/*/bin/*")
        dirs += glob.glob(os.path.join(os.path.expanduser("~"), "texlive", "*", "bin", "*"))
    return [d for d in dirs if os.path.isdir(d)]


def _find_in_dir(folder, names):
    for n in names:
        for c in _exe_names(n):
            f = os.path.join(folder, c)
            if os.path.isfile(f):
                return f
    return None


_compiler_cache = {"path": None}


def _find_latex():
    """Locate the LaTeX compiler: config/env path, then PATH, then the usual install folders."""
    cached = _compiler_cache["path"]
    if cached and os.path.isfile(cached):
        return cached
    names = [LATEX_ENGINE] + [n for n in _ENGINE_NAMES if n != LATEX_ENGINE]
    found = None
    if LATEX_PATH:
        p = os.path.expandvars(os.path.expanduser(LATEX_PATH))
        if os.path.isfile(p):
            found = p
        elif os.path.isdir(p):
            found = _find_in_dir(p, names)
    if not found:
        for n in names:
            found = shutil.which(n)
            if found:
                break
    if not found:
        for d in _extra_bin_dirs():
            found = _find_in_dir(d, names)
            if found:
                break
    _compiler_cache["path"] = found
    return found


_engine_state = {"state": "idle", "error": ""}      # idle | ready | error  (kept for older callers)


def _backend_mode():
    m = BACKEND.strip().lower()
    return m if m in ("auto", "local", "online") else "auto"


def compiler_info():
    mode = _backend_mode()
    exe = _find_latex() if mode != "online" else None
    if exe:
        _engine_state.update(state="ready", error="")
        return {"ready": True, "state": "ready", "error": "", "compiler": exe, "engine": os.path.basename(exe), "online": False}
    if mode != "local":
        _engine_state.update(state="ready", error="")
        return {"ready": True, "state": "ready", "error": "", "compiler": "", "engine": "online", "online": True}
    _engine_state.update(state="error", error=NO_COMPILER_MSG)
    return {"ready": False, "state": "error", "error": NO_COMPILER_MSG, "compiler": "", "engine": "", "online": False}


def engine_ready():
    return compiler_info()["ready"]


def ensure_engine():
    """Compatibility shim: there is nothing to download any more; True when a local compiler exists."""
    return compiler_info()["ready"]


def ensure_engine_async():
    compiler_info()


_compile_lock = threading.Lock()
_RERUN_RE = re.compile(r"Rerun to get|undefined references|Label\(s\) may have changed", re.I)


def _decode(raw):
    return (raw or b"").decode("utf-8", "replace")


# ── online compilers ─────────────────────────────────────────────────────────────────────
def _online_engine():
    return LATEX_ENGINE if LATEX_ENGINE in ("pdflatex", "xelatex", "lualatex") else "pdflatex"


def _http_post(url, data, content_type):
    """POST and return (status, body) -- HTTP error statuses are returned, only transport failures raise."""
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": content_type, "User-Agent": "Midum-MidumDocs/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=ONLINE_TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _b64_or_text(v):
    if not isinstance(v, str):
        return ""
    try:
        return base64.b64decode(v, validate=True).decode("utf-8", "replace")
    except Exception:
        return v


def _ytotech_error_text(body):
    """Pull a readable TeX log out of latex.ytotech.com's JSON error body (logs are base64 per file)."""
    try:
        j = json.loads(_decode(body))
    except ValueError:
        return _decode(body)
    if not isinstance(j, dict):
        return _decode(body)
    parts = [str(j[k]) for k in ("error", "message") if j.get(k)]
    logs = j.get("logs")
    if isinstance(logs, dict):
        parts += [_b64_or_text(v) for v in logs.values()]
    elif isinstance(logs, str):
        parts.append(_b64_or_text(logs))
    return "\n".join(p for p in parts if p).strip() or _decode(body)


def _online_ytotech(src, engine):
    """(pdf_bytes | None, log, transient). latex.ytotech.com runs latexmk, so references/TOC reruns are handled there."""
    payload = {"compiler": engine, "resources": [{"main": True, "content": src}],
               "options": {"response": {"log_files_on_failure": True}}}
    code, body = _http_post(YTOTECH_URL, json.dumps(payload).encode("utf-8"), "application/json")
    if body[:5] == b"%PDF-":
        return body, "", False
    return None, _ytotech_error_text(body), code >= 500 or code in (408, 429)


def _online_texlive_net(src, engine):
    """(pdf_bytes | None, log, transient) via texlive.net's latexcgi endpoint."""
    boundary = "----midumdocs" + hashlib.sha1(os.urandom(16)).hexdigest()
    fields = [("filecontents[]", src), ("filename[]", "document.tex"), ("engine", engine), ("return", "pdf")]
    body = b"".join(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode("utf-8")
                    for k, v in fields) + ("--%s--\r\n" % boundary).encode("ascii")
    code, out = _http_post(TEXLIVE_NET_URL, body, "multipart/form-data; boundary=" + boundary)
    if out[:5] == b"%PDF-":
        return out, "", False
    text = _html.unescape(re.sub(r"<[^>]+>", "", _decode(out))).strip()
    return None, text, code >= 500 or code in (408, 429)


def _online_providers():
    table = {"ytotech": ("latex.ytotech.com", _online_ytotech), "texlive.net": ("texlive.net", _online_texlive_net)}
    if ONLINE_PROVIDER in table:
        return [table[ONLINE_PROVIDER]]
    return [table["ytotech"], table["texlive.net"]]


def _compile_online(src):
    """Compile on an online service; falls through to the next one when a service is down or unreachable.
    A real LaTeX error is returned as-is (status 1), not retried elsewhere."""
    engine = _online_engine()
    problems = []
    with _compile_lock:
        for name, fn in _online_providers():
            try:
                pdf, log, transient = fn(src, engine)
            except Exception as e:
                problems.append(f"{name}: {e}")
                continue
            if pdf is None and transient:
                problems.append(f"{name}: {log[:200] or 'service unavailable'}")
                continue
            return {"status": 0 if pdf else 1, "log": log[-200000:] if not pdf else "",
                    "pdf": base64.b64encode(pdf).decode("ascii") if pdf else None,
                    "engine": f"online ({name}, {engine})", "online": True}
    return {"status": -4, "online": True, "pdf": None, "engine": "online",
            "log": "Online LaTeX compile failed -- check your internet connection, or install TeX Live / MiKTeX.\n" + "\n".join(problems)}


def compile_latex(src):
    """Compile ``src``: with a local TeX install when there is one, otherwise on an online service (see
    MIDUMDOCS_LATEX_BACKEND). Returns {status, log, pdf(base64 or None), engine}.
    status 0 = success; anything else = failure (the log holds the TeX errors)."""
    mode = _backend_mode()
    exe = _find_latex() if mode != "online" else None
    if exe:
        return _compile_local(src, exe)
    if mode != "local":
        return _compile_online(src)
    return {"status": -1, "log": NO_COMPILER_MSG, "pdf": None, "engine": ""}


def _compile_local(src, exe):
    low = exe.lower()
    tectonic = "tectonic" in os.path.basename(low)
    if tectonic:
        cmd = [exe, "--keep-logs", "main.tex"]
    else:
        cmd = [exe, "-interaction=nonstopmode", "-no-shell-escape"]
        if "miktex" in low:
            cmd.append("--enable-installer")          # let MiKTeX fetch a missing package itself, without a dialog
        cmd.append("main.tex")
    env = dict(os.environ)
    env.update(openin_any="p", openout_any="p")      # TeX may not read/write outside the job folder
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with _compile_lock, tempfile.TemporaryDirectory(prefix="midumdocs-") as tmp:
        with open(os.path.join(tmp, "main.tex"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(src)
        status, log, out = 1, "", b""
        for _ in range(MAX_PASSES):
            try:
                p = subprocess.run(cmd, cwd=tmp, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                   timeout=COMPILE_TIMEOUT, creationflags=flags)
            except subprocess.TimeoutExpired:
                return {"status": -2, "log": f"LaTeX compile timed out after {COMPILE_TIMEOUT}s.", "pdf": None,
                        "engine": os.path.basename(exe)}
            except OSError as e:
                return {"status": -3, "log": f"Could not run {exe}: {e}", "pdf": None, "engine": os.path.basename(exe)}
            status = p.returncode
            out = (p.stdout or b"") + b"\n" + (p.stderr or b"")
            try:
                with open(os.path.join(tmp, "main.log"), "rb") as fh:
                    log = _decode(fh.read())
            except OSError:
                log = _decode(out)
            if status != 0 or tectonic or not _RERUN_RE.search(log):
                break
        if status != 0 and tectonic:
            log = (log + "\n" + _decode(out)).strip()
        pdf = None
        if status == 0:
            try:
                with open(os.path.join(tmp, "main.pdf"), "rb") as fh:
                    pdf = base64.b64encode(fh.read()).decode("ascii")
            except OSError:
                status, log = 1, log or "The compiler finished but produced no PDF."
        return {"status": status, "log": log[-200000:], "pdf": pdf, "engine": os.path.basename(exe)}


PREFS_PATH = os.path.join(os.path.dirname(os.path.dirname(VENDOR_DIR)), "midumdocs_prefs.json")
_PREF_DEFAULTS = {"pageEdit": True, "boxOutline": True}


def load_prefs():
    out = dict(_PREF_DEFAULTS)
    try:
        with open(PREFS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for k in out:
            if isinstance(data.get(k), bool):
                out[k] = data[k]
    except (OSError, ValueError, AttributeError):
        pass
    return out


def save_prefs(patch):
    cur = load_prefs()
    if isinstance(patch, dict):
        for k in cur:
            if isinstance(patch.get(k), bool):
                cur[k] = patch[k]
    try:
        with open(PREFS_PATH, "w", encoding="utf-8") as fh:
            json.dump(cur, fh)
    except OSError:
        pass
    return cur


def prefetch_vendor_async():
    """Cache the editor's JS libraries (CodeMirror, pdf.js) on disk the first time Midum runs online."""
    def run():
        for key in VENDOR_FILES:
            try:
                _vendor_fetch(key)
            except Exception:
                pass
    threading.Thread(target=run, name="midumdocs-vendor", daemon=True).start()


def _vendor_fetch(key):
    """Return the bytes of an allow-listed vendor file, downloading + caching it on first use."""
    url = VENDOR_FILES.get(key)
    if not url:
        return None
    root = os.path.realpath(os.path.join(VENDOR_DIR, "cdn"))
    path = os.path.realpath(os.path.join(root, *key.split("/")))
    if not path.startswith(root + os.sep):
        return None
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            return fh.read()
    req = urllib.request.Request(url, headers={"User-Agent": "Midum-MidumDocs/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return data


_CTYPES = {".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
           ".wasm": "application/wasm", ".html": "text/html; charset=utf-8"}


class _Handler(BaseHTTPRequestHandler):
    server_version = "MidumDocs/1.0"

    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", cache="no-store", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_HEAD = lambda self: self.do_GET()        # noqa: E731

    def do_POST(self):
        try:
            ppath = unquote(urlparse(self.path).path)
            if ppath not in ("/compile", "/prefs"):
                return self._send(404, b"not found")
            port = self.server.server_address[1]
            ok_hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
            origin = self.headers.get("Origin")
            if self.headers.get("Host", "") not in ok_hosts or (origin and origin not in ["http://" + h for h in ok_hosts]):
                return self._send(403, b"forbidden")          # only the editor page itself may compile
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_SOURCE_BYTES:
                return self._send(413, b"bad size")
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            if ppath == "/prefs":
                return self._send(200, json.dumps(save_prefs(body)).encode(), "application/json")
            src = body.get("source")
            if not isinstance(src, str):
                return self._send(400, b"'source' must be a string")
            return self._send(200, json.dumps(compile_latex(src)).encode(), "application/json")
        except Exception as e:
            try:
                self._send(500, f"error: {e}".encode("utf-8", "replace"))
            except Exception:
                pass

    def do_GET(self):
        try:
            path = unquote(urlparse(self.path).path)
            if path in ("/", "/editor.html"):
                with open(os.path.join(_assets_dir(), "editor.html"), "rb") as fh:
                    return self._send(200, fh.read(), _CTYPES[".html"])
            if path == "/pageedit.js":
                with open(os.path.join(_assets_dir(), "pageedit.js"), "rb") as fh:
                    return self._send(200, fh.read(), _CTYPES[".js"])
            if path == "/engine-status":
                return self._send(200, json.dumps(compiler_info()).encode(), "application/json")
            if path == "/prefs":
                return self._send(200, json.dumps(load_prefs()).encode(), "application/json")
            name = path.lstrip("/")
            if name.startswith("vendor/"):
                key = name[len("vendor/"):]
                data = _vendor_fetch(key)
                if data is None:
                    return self._send(404, b"not found")
                ctype = _CTYPES.get(os.path.splitext(key)[1]) or mimetypes.guess_type(key)[0] or "application/octet-stream"
                return self._send(200, data, ctype, "public, max-age=86400")
            return self._send(404, b"not found")
        except Exception as e:
            try:
                self._send(502, f"error: {e}".encode("utf-8", "replace"))
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
            threading.Thread(target=httpd.serve_forever, name="midumdocs-http", daemon=True).start()
            _server = httpd
            prefetch_vendor_async()
        return _server.server_address[1]


def server_info():
    try:
        port = ensure_server()
        ensure_engine_async()
        return {"ok": True, "url": f"http://127.0.0.1:{port}/editor.html", "engine_ready": engine_ready()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── UTF-16 helpers (JS offsets) ──────────────────────────────────────────────────────────
def _b16(text):
    return text.encode("utf-16-le", "surrogatepass")


def _from16(raw):
    return raw.decode("utf-16-le", "surrogatepass")


def _len16(text):
    return len(_b16(text)) // 2


def _slice16(text, a, b):
    return _from16(_b16(text)[a * 2:b * 2])


def _splice16(text, a, b, new):
    raw = _b16(text)
    return _from16(raw[:a * 2] + _b16(new) + raw[b * 2:])


def _line_col(text, off16):
    head = _slice16(text, 0, max(0, off16))
    line = head.count("\n") + 1
    col = len(head) - (head.rfind("\n") + 1) + 1
    return line, col


def _norm(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ── artifact helpers ─────────────────────────────────────────────────────────────────────
def _rec(art_id):
    return artifacts._load().get(str(art_id)) if art_id else None


def _text(rec, version=None):
    return _norm(artifacts._read_raw(rec, version))


def is_latex(rec):
    return bool(rec) and artifacts.art_type(rec) == "latex"


# ── live state (pushed by the editor) ────────────────────────────────────────────────────
def _num(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def update_live(state):
    """Called by Api.midumdocs_state with the editor's cursor/selection/compile snapshot."""
    if not isinstance(state, dict):
        return {"ok": False}
    with _cv:
        if state.get("id") and state.get("id") != _live["id"]:
            return {"ok": False, "stale": True}
        for key in ("version", "length"):
            if key in state:
                _live[key] = _num(state[key], _live[key])
        if "dirty" in state:
            _live["dirty"] = bool(state["dirty"])
        if isinstance(state.get("cursor"), dict):
            c = state["cursor"]
            _live["cursor"] = {"offset": _num(c.get("offset")), "line": _num(c.get("line"), 1), "col": _num(c.get("col"), 1)}
        if "selection" in state:
            s = state["selection"]
            if isinstance(s, dict) and _num(s.get("to")) > _num(s.get("from")):
                _live["selection"] = {"from": _num(s.get("from")), "to": _num(s.get("to")),
                                      "fromLine": _num(s.get("fromLine"), 1), "toLine": _num(s.get("toLine"), 1),
                                      "text": str(s.get("text") or "")[:20000]}
            else:
                _live["selection"] = None
        if isinstance(state.get("compile"), dict):
            c = state["compile"]
            errs = [str(e)[:300] for e in (c.get("errors") or [])][:8]
            _live["compile"] = {"version": _num(c.get("version")), "running": bool(c.get("running")),
                                "ok": bool(c.get("ok")), "pages": _num(c.get("pages")), "errors": errs,
                                "error": str(c.get("error") or "")[:300], "ts": time.time()}
        _live["ts"] = time.time()
        _cv.notify_all()
    return {"ok": True}


def _gui(kind, payload):
    hook = _gui_hook
    if hook is None:
        return None
    try:
        return hook(kind, payload)
    except Exception as e:
        return f"[MidumDocs GUI error: {e}]"


def on_closed():
    """The user closed the panel."""
    with _cv:
        _live.update(open=False, dirty=False)
        _cv.notify_all()


def on_chat_changed(chat_id):
    """Active chat actually switched: a doc from the previous chat must not stay open.
    (artifacts.set_active_chat calls this on EVERY message send, so a repeat of the same chat id must be a no-op.)"""
    global _last_chat
    chat_id = chat_id or ""
    prev, _last_chat = _last_chat, chat_id
    if not _live["open"] or chat_id == prev:
        return
    rec = _rec(_live["id"])
    if rec is not None and not (rec.get("chat_id") or "") and not prev:
        rec["chat_id"] = chat_id            # the doc was made before this chat had an id: it belongs to this chat
        try:
            artifacts._save()
        except Exception:
            pass
        return
    if rec is None or (rec.get("chat_id") or "") != chat_id:
        on_closed()
        _gui("close", {})


def on_artifact_changed(art_id, select=None):
    """Called by artifacts after any new version: push the new text to an open editor."""
    if getattr(_tls, "suppress", False):
        return
    if not (_live["open"] and _live["id"] == art_id):
        return
    rec = _rec(art_id)
    if not rec:
        return
    _gui("changed", {"id": art_id, "version": int(rec.get("version") or 0), "content": _text(rec),
                     "source": "model", "select": select})


# ── show / create ────────────────────────────────────────────────────────────────────────
def show(rec, version=None):
    """Open ``rec`` (optionally an older version, read-only) in the MidumDocs pane."""
    global _last_id
    if _gui_hook is None:
        return "(Not displayed: no GUI attached. The document is saved.)"
    cur = int(rec.get("version") or 0)
    older = bool(version) and cur and int(version) != cur
    try:
        text = _text(rec, int(version) if older else None)
    except Exception as e:
        return f"(Could not open the document: {e})"
    readonly = older or not artifacts.is_editable(rec)
    payload = {"id": rec["id"], "name": rec["name"], "title": rec.get("title") or rec["name"],
               "version": int(version) if older else cur, "content": text, "readonly": readonly,
               "reason": "older version" if older else ("attached file" if readonly else ""),
               "saved": bool(rec.get("md_saved"))}
    if _live["open"] and _live["id"] == rec["id"] and not readonly:
        out = _gui("changed", {**payload, "source": "model", "select": None})
    else:
        with _cv:
            _live.update(id=rec["id"], open=True, version=payload["version"], dirty=False, length=-1,
                         cursor=None, selection=None, compile=None, ts=time.time())
        out = _gui("open", payload)
    _last_id = rec["id"]
    try:
        rec["md_opened"] = time.time()          # per-chat "last document" (survives restarts, like a chat's Render Surface)
        artifacts._save()
    except Exception:
        pass
    try:
        artifacts.clear_shown()
    except Exception:
        pass
    if isinstance(out, str) and out.startswith("["):
        return f"(Could not open MidumDocs: {out})"
    return "Opened in MidumDocs." + (" (read-only: " + payload["reason"] + ")" if readonly else "")


def last_doc_for_chat(chat_id=None):
    """The MidumDoc most recently worked on in this chat (None if the chat has none)."""
    if chat_id is None:
        chat_id = artifacts.get_active_chat()
    chat_id = chat_id or ""
    best, best_t = None, -1.0
    try:
        recs = list(artifacts._load().values())
    except Exception:
        return None
    for rec in recs:
        if (rec.get("chat_id") or "") != chat_id or not artifacts.is_editable(rec) or not is_latex(rec):
            continue
        t = float(rec.get("md_opened") or 0)
        if t >= best_t:
            best, best_t = rec, t
    return best


def reopen_ui():
    """Reopen THIS chat's last document (so closed work can be picked up where it was left), or start a new blank one."""
    rec = last_doc_for_chat()
    if rec is None:
        name = _unique_name("Untitled.tex")
        rec = artifacts.add_file(data=NEW_DOC_TEMPLATE.encode("utf-8"), name=name, chat_id=artifacts.get_active_chat(),
                                 origin="generated", upload=False, sent=True)
        artifacts._init_generated(rec, "latex", "")
    return {"ok": True, "message": show(rec)}


def _is_blank_unsaved(rec):
    """An untouched, never-saved blank document (still exactly the new-document template)."""
    try:
        return (not rec.get("md_saved") and int(rec.get("version") or 0) <= 1
                and _text(rec) == _norm(NEW_DOC_TEMPLATE))
    except Exception:
        return False


def new_doc():
    """Open a fresh, unsaved blank document (the MidumDocs tab). Autosave stays off until the user presses Save.
    An untouched blank from this chat is reused instead of piling up empty Untitled files."""
    chat = artifacts.get_active_chat() or ""
    rec = None
    for r in _all_docs():
        if (r.get("chat_id") or "") == chat and _is_blank_unsaved(r):
            rec = r
            break
    if rec is None:
        name = _unique_name("Untitled.tex")
        rec = artifacts.add_file(data=NEW_DOC_TEMPLATE.encode("utf-8"), name=name, chat_id=artifacts.get_active_chat(),
                                 origin="generated", upload=False, sent=True)
        artifacts._init_generated(rec, "latex", "")
    return {"ok": True, "message": show(rec), "id": rec["id"]}


def list_saved_docs():
    """Every MidumDoc from any chat, newest first, for the MidumDocs tab's dropdown (blank untouched ones are skipped)."""
    here = artifacts.get_active_chat() or ""
    out = []
    for r in _all_docs():
        if _is_blank_unsaved(r):
            continue
        out.append({"id": r["id"], "name": r["name"], "title": r.get("title") or r["name"],
                    "version": int(r.get("version") or 1), "stamp": _doc_stamp(r),
                    "saved": bool(r.get("md_saved")), "here": (r.get("chat_id") or "") == here,
                    "open": bool(_live["open"] and _live["id"] == r["id"])})
    return {"ok": True, "docs": out}


def open_saved_doc(art_id):
    """Open an existing MidumDoc by id for editing."""
    rec = _rec(art_id)
    if not rec or not is_latex(rec):
        return {"ok": False, "error": "That document no longer exists."}
    return {"ok": True, "message": show(rec), "id": rec["id"]}


def _unique_name(name):
    base, ext = os.path.splitext(name)
    i, cand = 1, name
    while artifacts._find_generated(cand):
        i += 1
        cand = f"{base}-{i}{ext}"
    return cand


def make_copy(art_id):
    """Turn an attached (read-only) .tex into an editable MidumDoc copy and open it."""
    rec = _rec(art_id)
    if not rec:
        return {"ok": False, "error": "Document not found."}
    name = _unique_name("copy-of-" + rec["name"] if not rec["name"].lower().startswith("copy-of-") else rec["name"])
    res = artifacts.editor_save_copy(name, artifacts._read_raw(rec))
    if not res.get("ok"):
        return res
    new = _rec(res["id"])
    if new is not None and not is_latex(new):
        new["art_type"] = "latex"
        artifacts._save()
    return {"ok": True, "message": show(new), "id": res["id"]}


# ── saving from the editor ───────────────────────────────────────────────────────────────
def _user_commit(rec, data):
    """Commit user-typed text. Rapid autosaves are folded into one version so history is not flooded."""
    with artifacts._lock:
        vs = rec.get("versions") or []
        last = vs[-1] if vs else None
        cur = int(rec.get("version") or 0)
        if last and last.get("note") == USER_NOTE and last.get("n") == cur and time.time() - last.get("ts", 0) < COALESCE_SECS:
            with open(artifacts._vpath(rec, cur), "wb") as fh:
                fh.write(data)
            blob = artifacts._blob_path(rec)
            with open(blob + ".tmp", "wb") as fh:
                fh.write(data)
            os.replace(blob + ".tmp", blob)
            rec["size"] = len(data)
            rec["sha256"] = hashlib.sha256(data).hexdigest()
            last["ts"], last["size"] = time.time(), len(data)
            try:
                os.remove(artifacts._text_cache_path(rec))
            except OSError:
                pass
            artifacts._save()
            return cur
    return artifacts._commit(rec, data, USER_NOTE)


def _mark_saved(rec):
    """The user saved this document by hand: from now on it autosaves."""
    if not rec.get("md_saved"):
        rec["md_saved"] = True
        artifacts._save()


def user_save(art_id, content, base_version=None, mode="auto"):
    """mode: 'manual' = the user pressed Save (commits, and switches autosave on for this document);
    'auto'   = autosave (only honoured once the document has been saved manually);
    'sync'   = push unsaved text so the model can see it (e.g. before a prompt); does NOT switch autosave on."""
    mode = mode if mode in ("auto", "manual", "sync") else "auto"
    rec = _rec(art_id)
    if not rec:
        return {"ok": False, "error": "Document not found (its chat may have been deleted)."}
    if not artifacts.is_editable(rec):
        return {"ok": False, "error": "This file cannot be edited as text, so it is read-only. Use 'Make editable copy'."}
    cur = int(rec.get("version") or 0)
    if mode == "auto" and not rec.get("md_saved"):
        return {"ok": True, "skipped": True, "saved": False, "version": cur}
    if base_version is not None and cur != int(base_version):
        return {"ok": False, "conflict": True, "version": cur, "content": _text(rec)}
    text = _norm(str(content))
    try:
        if _text(rec) == text:
            if mode == "manual":
                _mark_saved(rec)
            return {"ok": True, "version": cur, "unchanged": True, "saved": bool(rec.get("md_saved"))}
    except OSError:
        pass
    _tls.suppress = True
    try:
        n = _user_commit(rec, text.encode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"Could not save: {e}"}
    finally:
        _tls.suppress = False
    if mode == "manual":
        _mark_saved(rec)
    return {"ok": True, "version": n, "saved": bool(rec.get("md_saved"))}


def revert_last(art_id):
    """Undo the model's last change (non-destructive: saved as a new version)."""
    rec = _rec(art_id)
    if not rec or not artifacts.is_editable(rec):
        return {"ok": False, "error": "Nothing to undo."}
    vs = rec.get("versions") or []
    if len(vs) < 2:
        return {"ok": False, "error": "There is no earlier version."}
    prev = vs[-2]["n"]
    out = artifacts.revert_tool({"artifact": rec["id"], "version": prev, "show": False})
    return {"ok": not str(out).startswith("Error"), "message": out}


# ── prompt box -> model ──────────────────────────────────────────────────────────────────
def begin_request(ctx):
    """Freeze the editor's selection/cursor at the moment the user pressed Enter in the prompt box."""
    global _request
    ctx = ctx if isinstance(ctx, dict) else {}
    sel = ctx.get("selection") if isinstance(ctx.get("selection"), dict) else None
    snap = {"id": _live["id"], "version": _num(ctx.get("version"), _live["version"]), "ts": time.time(),
            "cursor": ctx.get("cursor") if isinstance(ctx.get("cursor"), dict) else None, "sel": None}
    if sel and _num(sel.get("to")) > _num(sel.get("from")):
        snap["sel"] = {"from": _num(sel["from"]), "to": _num(sel["to"]), "text": str(sel.get("text") or ""),
                       "fromLine": _num(sel.get("fromLine"), 1), "toLine": _num(sel.get("toLine"), 1)}
    _request = snap
    return snap


def wrap_request(text, snap):
    parts = ["[MidumDoc request]", str(text).strip()]
    rec = _rec(snap.get("id"))
    if rec:
        parts.append(f"(Document: {rec['name']} [{rec['id']}], open in MidumDocs.)")
    sel = snap.get("sel")
    if sel:
        body = sel["text"]
        if len(body) > SELECTION_PROMPT_CHARS:
            body = body[:SELECTION_PROMPT_CHARS] + "\n... [selection truncated]"
        parts.append(f"Selected text (lines {sel['fromLine']}-{sel['toLine']}) -- 'this' / 'selected' / 'here' refer to it:\n"
                     f"<<<SELECTION\n{body}\nSELECTION>>>\n"
                     "Change it with midumdoc_replace_selection, or update_artifact for edits elsewhere.")
    elif snap.get("cursor"):
        c = snap["cursor"]
        parts.append(f"Cursor was at line {_num(c.get('line'), 1)}, column {_num(c.get('col'), 1)} (no selection). "
                     "'here' means that spot; insert with midumdoc_insert_at_cursor.")
    return "\n".join(parts)


# ── model-facing tools ───────────────────────────────────────────────────────────────────
def _wait(pred, timeout):
    end = time.time() + timeout
    with _cv:
        while not pred():
            left = end - time.time()
            if left <= 0:
                return False
            _cv.wait(min(left, 0.5))
        return True


def _compile_line(rec):
    c = _live["compile"]
    ver = int(rec.get("version") or 0) if rec else 0
    if not c:
        return "not compiled yet (the editor compiles automatically)"
    stale = c["version"] != ver
    if c["running"]:
        return "compiling..."
    if c["ok"]:
        s = f"OK, {c['pages']} page(s)"
    else:
        msgs = c["errors"] or ([c["error"]] if c["error"] else ["failed (see the page pane)"])
        s = "FAILED: " + " | ".join(msgs[:3])
    return s + (f" [this is for v{c['version']}; the document is now v{ver}, a newer compile is pending]" if stale else "")


def state_text(context_lines=CONTEXT_LINES, wait_compile=False):
    if not _live["open"] or not _rec(_live["id"]):
        names = [r["name"] for r in artifacts.list_records() if is_latex(r)]
        extra = (" LaTeX documents in this chat: " + ", ".join(names) + ". Open one with open_artifact.") if names else \
                " Create one with create_midumdoc."
        return "No MidumDoc is open right now." + extra
    rec = _rec(_live["id"])
    if wait_compile:
        _wait(lambda: not _live["compile"] or (not _live["compile"]["running"] and _live["compile"]["version"] >= int(rec.get("version") or 0)),
              WAIT_COMPILE_SECS)
    _wait(lambda: not _live["dirty"], 0.6)
    text = _text(rec)
    lines = text.split("\n")
    out = [f"MidumDoc: {rec['name']} [{rec['id']}] v{rec.get('version')}  ({len(lines)} lines, {len(text)} chars)"]
    if _live["dirty"]:
        out.append("NOTE: the user has typed changes that are not saved yet; line numbers below may be a moment out of date.")
    cur = _live["cursor"]
    if cur:
        out.append(f"Cursor: line {cur['line']}, column {cur['col']}")
        n = max(0, min(int(context_lines), 30))
        a, b = max(1, cur["line"] - n), min(len(lines), cur["line"] + n)
        for i in range(a, b + 1):
            ln = lines[i - 1]
            if i == cur["line"]:
                k = max(0, min(len(ln), cur["col"] - 1))
                ln = ln[:k] + "▌" + ln[k:]
            out.append(f"  {i:>4} | {ln}")
    else:
        out.append("Cursor: unknown (the user has not clicked in the editor yet)")
    sel = _live["selection"]
    if sel:
        body = sel["text"]
        if len(body) > 4000:
            body = body[:4000] + "\n... [truncated]"
        out.append(f"Selection: lines {sel['fromLine']}-{sel['toLine']}, {len(sel['text'])} chars:\n<<<SELECTION\n{body}\nSELECTION>>>")
    else:
        out.append("Selection: none")
    out.append("Compile: " + _compile_line(rec))
    return "\n".join(out)


def prompt_block():
    """Injected into the system prompt on every step while a MidumDoc is open (never persisted)."""
    if not _live["open"] or not _rec(_live["id"]):
        rec = last_doc_for_chat()
        if rec is None:
            n = len(_all_docs())
            if not n:
                return ""
            return ("━━━ MIDUMDOCS (saved) ━━━\n"
                    f"{n} MidumDoc(s) saved in other chats. If the user refers to an earlier document, list_midumdocs shows them "
                    "and open_midumdoc(artifact) opens one in the editor (never create it again).")
        return ("━━━ MIDUMDOC (closed) ━━━\n"
                f"This chat has a LaTeX MidumDoc, \"{rec.get('name')}\" (v{int(rec.get('version') or 0)}), that the user has closed. "
                "If they want to continue it, read it with read_artifact and edit it with update_artifact / rewrite_artifact "
                "(never create it again); they can reopen the editor with the MidumDocs button.")
    return ("━━━ MIDUMDOC OPEN (live editor state) ━━━\n"
            "The user has this LaTeX document open in the MidumDocs editor right now and can see every change you make.\n"
            + state_text(context_lines=4) + "\n"
            "Edit it with update_artifact (targeted) / rewrite_artifact (whole file), or midumdoc_replace_selection / "
            "midumdoc_insert_at_cursor when the user says 'this', 'selected', or 'here'. Read the full source with "
            "read_artifact first if you have not. After an edit, call midumdoc_state(wait_compile=true) to confirm it compiles; "
            "if it FAILED, fix the error. Never create_artifact the same document again.")


def _resolve_target(kind):
    """Find (rec, text, a16, b16) for the selection or cursor, or return an error string."""
    if not _live["open"]:
        return None, "No MidumDoc is open. Use update_artifact / rewrite_artifact on the artifact instead."
    rec = _rec(_live["id"])
    if not rec or not artifacts.is_editable(rec):
        return None, "The open document is read-only."
    cur_v = int(rec.get("version") or 0)
    if not _wait(lambda: _live["version"] == cur_v and not _live["dirty"], WAIT_SYNC_SECS):
        return None, "The editor has not synced with the latest version yet. Call midumdoc_state and retry."
    text = _text(rec)
    if _live["length"] >= 0 and _len16(text) != _live["length"]:
        return None, "The editor's text no longer matches the saved document. Call midumdoc_state and retry."
    if kind == "selection":
        snap = _request
        if snap and snap["id"] == rec["id"] and snap.get("sel") and time.time() - snap["ts"] < REQUEST_TTL:
            s = snap["sel"]
            if snap["version"] == cur_v and _slice16(text, s["from"], s["to"]) == s["text"]:
                return (rec, text, s["from"], s["to"]), None
            if s["text"] and text.count(s["text"]) == 1:          # document moved on: relocate the exact text
                a = _len16(text[:text.index(s["text"])])
                return (rec, text, a, a + _len16(s["text"])), None
            return None, "The text that was selected has changed since the request. Call midumdoc_state and use update_artifact."
        sel = _live["selection"]
        if not sel:
            return None, "Nothing is selected in the editor. Use midumdoc_insert_at_cursor or update_artifact."
        return (rec, text, sel["from"], sel["to"]), None
    snap = _request
    if snap and snap["id"] == rec["id"] and snap.get("cursor") and snap["version"] == cur_v and time.time() - snap["ts"] < REQUEST_TTL \
            and not snap.get("sel"):
        off = _num(snap["cursor"].get("offset"), -1)
        if 0 <= off <= _len16(text):
            return (rec, text, off, off), None
    cur = _live["cursor"]
    if not cur:
        return None, "The cursor position is unknown. Use update_artifact with surrounding text instead."
    return (rec, text, min(cur["offset"], _len16(text)), min(cur["offset"], _len16(text))), None


def _apply(target, new, note):
    global _request
    rec, text, a, b = target
    new = _norm(str(new))
    updated = _splice16(text, a, b, new)
    if updated == text:
        return "No change: the new text is identical to what was there."
    end = a + _len16(new)
    _tls.suppress = True                      # one combined push below (text + the range to highlight)
    try:
        artifacts._commit(rec, updated.encode("utf-8"), note)
    finally:
        _tls.suppress = False
    on_artifact_changed(rec["id"], select=[a, end])
    _request = None
    l1, _ = _line_col(updated, a)
    l2, _ = _line_col(updated, end)
    return (f"Updated {rec['name']} -> v{rec.get('version')} (lines {l1}-{l2} now hold the new text). "
            "Call midumdoc_state(wait_compile=true) to check it compiles.")


def replace_selection_tool(args):
    if args.get("text") is None:
        return "Error: 'text' (the replacement LaTeX) is required."
    target, err = _resolve_target("selection")
    if err:
        return "Error: " + err
    return _apply(target, args["text"], "replaced selection")


def insert_at_cursor_tool(args):
    if args.get("text") is None:
        return "Error: 'text' (the LaTeX to insert) is required."
    target, err = _resolve_target("cursor")
    if err:
        return "Error: " + err
    return _apply(target, args["text"], "inserted at cursor")


def state_tool(args):
    try:
        ctx = int(args.get("context_lines", CONTEXT_LINES))
    except (TypeError, ValueError):
        ctx = CONTEXT_LINES
    wait = artifacts._truthy(args.get("wait_compile"))
    return state_text(context_lines=ctx, wait_compile=wait)


def create_tool(args):
    args = dict(args or {})
    name = os.path.basename(str(args.get("name") or "").strip())
    if not name:
        return "Error: 'name' is required (e.g. 'report.tex')."
    if not name.lower().endswith(".tex"):
        name += ".tex"
    content = args.get("content")
    args.update(name=name, type="latex", content=NEW_DOC_TEMPLATE if content in (None, "") else content)
    args.setdefault("show", True)
    return artifacts.create_tool(args)


# ── saved documents from ANY chat ────────────────────────────────────────────────────────
def _doc_stamp(rec):
    return max(float(rec.get("md_opened") or 0), float(rec.get("created") or 0))


def _all_docs(query=""):
    """Every model-created MidumDoc (type latex) in the artifact store, whatever chat it belongs to, newest first."""
    try:
        recs = list(artifacts._load().values())
    except Exception:
        return []
    q = str(query or "").strip().lower()
    out = [r for r in recs if artifacts.is_editable(r) and is_latex(r)
           and (not q or q in str(r.get("name") or "").lower() or q in str(r.get("title") or "").lower()
                or q in str(r.get("id") or "").lower())]
    out.sort(key=_doc_stamp, reverse=True)
    return out


def _resolve_doc(ref):
    """(rec, None) for the MidumDoc named by ``ref`` (id, id prefix, file name or part of it), else (None, error).
    An empty ref means the most recently used document. Documents of the current chat win ties."""
    docs = _all_docs()
    ref = str(ref or "").strip()
    if not docs:
        return None, "Error: there are no saved MidumDocs in any chat yet. Create one with create_midumdoc."
    if not ref:
        return docs[0], None
    low = ref.lower()
    cand = [r for r in docs if str(r["id"]).lower() == low or str(r["name"]).lower() == low]
    if not cand and len(low) >= 6:
        cand = [r for r in docs if str(r["id"]).lower().startswith(low)]
    if not cand:
        cand = [r for r in docs if low in str(r["name"]).lower() or low in str(r.get("title") or "").lower()]
    if not cand:
        return None, f"Error: no saved MidumDoc matches '{ref}'. Call list_midumdocs to see them."
    here = artifacts.get_active_chat() or ""
    pool = [r for r in cand if (r.get("chat_id") or "") == here] or cand
    if len(pool) > 1:
        names = "; ".join(f"{r['id']} ({r['name']})" for r in pool[:8])
        return None, f"Error: '{ref}' matches several MidumDocs: {names}. Call open_midumdoc again with the exact id."
    return pool[0], None


def list_docs_tool(args):
    query = str((args or {}).get("query") or "").strip()
    docs = _all_docs(query)
    if not docs:
        return (f"No saved MidumDoc matches '{query}'." if query
                else "No saved MidumDocs in any chat yet. Create one with create_midumdoc.")
    here = artifacts.get_active_chat() or ""
    lines = []
    for r in docs[:LIST_MAX]:
        where = "this chat" if (r.get("chat_id") or "") == here else "another chat"
        try:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(_doc_stamp(r)))
        except (OverflowError, OSError, ValueError):
            when = "?"
        now_open = " · open now" if _live["open"] and _live["id"] == r["id"] else ""
        lines.append(f"{r['id']}  {r['name']}  [v{int(r.get('version') or 1)} · {where} · last used {when}{now_open}]")
    if len(docs) > LIST_MAX:
        lines.append(f"({len(docs) - LIST_MAX} older MidumDocs not shown - narrow it with query)")
    lines.append("Open one with open_midumdoc(artifact=<id or name>).")
    return "\n".join(lines)


def open_doc_tool(args):
    args = args or {}
    rec, err = _resolve_doc(args.get("artifact"))
    if err:
        return err
    version = None
    if args.get("version") not in (None, ""):
        try:
            version = int(args["version"])
        except (TypeError, ValueError):
            return "Error: 'version' must be a whole number (see list_artifact_versions)."
        if not os.path.exists(artifacts._vpath(rec, version)):
            return f"Error: {rec['name']} has no version {version}. Call list_artifact_versions."
    msg = f"{rec['name']} ({rec['id']}): {show(rec, version)}"
    if (rec.get("chat_id") or "") != (artifacts.get_active_chat() or ""):
        msg += (" It was saved in another chat; read_artifact / update_artifact / rewrite_artifact work on it by id as usual. "
                "It closes when the user switches chats; open_midumdoc brings it back.")
    return msg


def dispatch(func_name, arguments):
    """Returns (output_text, images_or_None)."""
    arguments = arguments or {}
    try:
        if func_name == "create_midumdoc":
            return create_tool(arguments), None
        if func_name == "midumdoc_state":
            return state_tool(arguments), None
        if func_name == "midumdoc_replace_selection":
            return replace_selection_tool(arguments), None
        if func_name == "midumdoc_insert_at_cursor":
            return insert_at_cursor_tool(arguments), None
        if func_name == "list_midumdocs":
            return list_docs_tool(arguments), None
        if func_name == "open_midumdoc":
            return open_doc_tool(arguments), None
    except Exception as e:
        return f"Error in {func_name}: {e}", None
    return f"Error: unknown MidumDocs tool '{func_name}'", None


TOOL_SCHEMAS = [
    artifacts._fn(
        "create_midumdoc",
        "Create a NEW LaTeX document (a MidumDoc) and open it in the MidumDocs editor, where the user sees it compiled as real pages "
        "and can edit the source. Pass the COMPLETE .tex source as `content` (omit it for a blank article template). Stored as a normal "
        "artifact (type latex), so afterwards change it with update_artifact / rewrite_artifact -- never create it again. It is compiled "
        "with pdfLaTeX by the LaTeX installation on the user's PC, or by an online compiler when none is installed: use standard "
        "CTAN packages; avoid shell-escape, minted, and anything needing external tools or local files.",
        {"name": {"type": "string", "description": "File name, e.g. 'report.tex' (.tex is added if missing)."},
         "content": {"type": "string", "description": "The COMPLETE LaTeX source. Optional."},
         "title": {"type": "string", "description": "Optional human-readable title for the panel header."}},
        ["name"]),
    artifacts._fn(
        "midumdoc_state",
        "See what the user sees in the open MidumDoc: cursor line/column with surrounding source, the selected text (if any), unsaved-edit "
        "status and the last compile result (page count or LaTeX errors). Use wait_compile=true after an edit to wait until the new "
        "version has compiled, so you can verify your change or fix its errors.",
        {"wait_compile": {"type": "boolean", "description": "Wait (up to ~75s) for the latest version to finish compiling."},
         "context_lines": {"type": "integer", "description": f"Source lines shown around the cursor (default {CONTEXT_LINES}, max 30)."}}),
    artifacts._fn(
        "midumdoc_replace_selection",
        "Replace the text the user has selected in the open MidumDoc (or that they had selected when they sent their request) with `text`. "
        "Use for 'rewrite this', 'fix the selected', 'make this a table'. Pass only the replacement LaTeX, not the whole file.",
        {"text": {"type": "string", "description": "Replacement LaTeX source for the selection."}}, ["text"]),
    artifacts._fn(
        "midumdoc_insert_at_cursor",
        "Insert `text` at the user's cursor in the open MidumDoc (where it was when they sent their request). Use for 'add a ... here'. "
        "If something is selected use midumdoc_replace_selection instead.",
        {"text": {"type": "string", "description": "LaTeX source to insert."}}, ["text"]),
    artifacts._fn(
        "list_midumdocs",
        "List the MidumDocs (LaTeX documents) saved in ANY chat, newest first, with id, name, version and whether each belongs to "
        "this chat. Use it when the user mentions a document from an earlier chat, or asks what documents they have.",
        {"query": {"type": "string", "description": "Optional text to match against document names, titles or ids."}}),
    artifacts._fn(
        "open_midumdoc",
        "Open a MidumDoc saved earlier -- in this chat OR any other chat -- in the MidumDocs editor so the user sees it and you can "
        "continue it. Afterwards edit it with update_artifact / rewrite_artifact by its id (never create it again). Omit `artifact` "
        "to open the most recently used one. Use `version` to view an older version read-only.",
        {"artifact": {"type": "string", "description": "MidumDoc id (art_xxxxxxxx), file name, or part of the name (see list_midumdocs)."},
         "version": {"type": "integer", "description": "Optional older version to view (read-only)."}}),
]


def register_tools(tools_list):
    """Idempotently append the MidumDocs tool schemas to the shared ``tools`` list."""
    have = {t.get("function", {}).get("name") for t in tools_list if isinstance(t, dict)}
    for t in TOOL_SCHEMAS:
        if t["function"]["name"] not in have:
            tools_list.append(t)
    return tools_list


PROMPT_HELP = (
    "━━━ MIDUMDOCS (LaTeX documents) ━━━\n"
    "For papers, reports, CVs, letters, slides (beamer), problem sets -- anything the user wants typeset -- write LaTeX as a\n"
    "MidumDoc: create_midumdoc(name, content) (or create_artifact with type 'latex'). It opens in a wide editor beside the chat,\n"
    "compiled live to real pages, and it is an ordinary artifact: change it with update_artifact / rewrite_artifact and undo with\n"
    "revert_artifact. While one is open the system prompt shows the user's CURSOR and SELECTION; 'this', 'here' and 'selected' mean\n"
    "those. Use midumdoc_replace_selection / midumdoc_insert_at_cursor for them, midumdoc_state(wait_compile=true) to check that your\n"
    "edit compiles (fix any LaTeX error it reports). It is compiled by the user's local LaTeX install if they have one, otherwise by an online compiler (pdfLaTeX): stick to standard CTAN packages, no shell-escape, no local files.\n"
)
