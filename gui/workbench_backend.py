"""Midum Workbench backend: the full IDE (VS Code workbench via CodinGame monaco-vscode-api) behind the Editor tab.

The workbench is a Vite project in gui/workbench/ (TypeScript). It cannot run from source, so on first use it is built
once into <storage>/vendor/workbench (needs Node.js 18+ and npm, plus internet for `npm install`; ~2-5 minutes). After that it
is static files served by editor_backend's localhost server under /wb/ and /workbench.html.

This module owns:
  * the build manager (status, background build, atomic swap, source-hash based staleness),
  * static serving of the built bundle,
  * the token-protected /api/fs/* endpoints that back VS Code's file system with the real disk
    (stat, readdir, read, write, mkdir, delete, rename, copy, find, grep, poll, info).

It is deliberately independent of editor_backend (which imports it), so it can be tested on its own.
"""
import fnmatch
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import unquote

import config

_HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(_HERE, "workbench")                                  # sources (package.json, *.ts, ...)
VENDOR = os.path.join(config.STORAGE_DIR, "vendor")
BUILD_DIR = os.path.join(VENDOR, "workbench_build")                         # sources copy + node_modules
DIST_DIR = os.path.join(VENDOR, "workbench")                                # built bundle (served)
USER_DIR = os.path.join(config.STORAGE_DIR, "workbench_user")               # settings.json, keybindings.json, extensions
STAMP = ".midum-src-hash"

IS_WIN = sys.platform == "win32"
MAX_READ = 256 * 1024 * 1024
MAX_WRITE = 512 * 1024 * 1024
SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
             ".ruff_cache", ".tox", "site-packages", ".idea"}
WALK_MAX_FILES = 60000

_hooks = {"token": None, "default_workspace": None}


def init(token_fn=None, default_workspace_fn=None):
    """editor_backend wires in its inline-suggest token and the default workspace."""
    _hooks["token"] = token_fn
    _hooks["default_workspace"] = default_workspace_fn


_own_token = secrets.token_urlsafe(24)


def token():
    try:
        t = _hooks["token"]() if _hooks["token"] else None
    except Exception:
        t = None
    return t or _own_token


# ----------------------------------------------------------------------------------------------------------
# Build manager
# ----------------------------------------------------------------------------------------------------------
_state = {"state": "none", "step": "", "log": [], "error": "", "started": 0.0, "stale": False}
_state_lock = threading.Lock()
_build_thread = None


def _set(**kw):
    with _state_lock:
        _state.update(kw)


def _log(line):
    line = (line or "").rstrip()
    if not line:
        return
    with _state_lock:
        _state["log"].append(line[:400])
        del _state["log"][:-60]


def sources_hash():
    """Hash of everything that influences the build (not node_modules / dist)."""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(SRC_DIR):
        dirs[:] = sorted(d for d in dirs if d not in ("node_modules", "dist"))
        for f in sorted(files):
            p = os.path.join(root, f)
            h.update(os.path.relpath(p, SRC_DIR).replace("\\", "/").encode())
            try:
                with open(p, "rb") as fh:
                    h.update(fh.read())
            except OSError:
                pass
    return h.hexdigest()[:16]


def sources_present():
    return os.path.isfile(os.path.join(SRC_DIR, "package.json"))


def built_hash():
    try:
        with open(os.path.join(DIST_DIR, STAMP), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def is_built():
    return os.path.isfile(os.path.join(DIST_DIR, "index.html"))


def status():
    """-> dict for the UI: state none|building|ready|error, plus node availability and the recent build log."""
    with _state_lock:
        st = dict(_state)
        st["log"] = list(st["log"][-12:])
    st["built"] = is_built()
    if st["built"] and st["state"] == "none":
        st["state"] = "ready"
    try:
        st["stale"] = bool(st["built"] and sources_present() and built_hash() != sources_hash())
    except Exception:
        st["stale"] = False
    st["node"] = bool(_which("node") and _which("npm"))
    st["sources"] = sources_present()
    st["elapsed"] = int(time.time() - st["started"]) if st["state"] == "building" and st["started"] else 0
    return st


def _which(name):
    return shutil.which(name) or (shutil.which(name + ".cmd") if IS_WIN else None)


def _node_version():
    try:
        out = subprocess.run([_which("node"), "--version"], capture_output=True, text=True, timeout=15,
                             creationflags=_nowin()).stdout.strip()
        return tuple(int(x) for x in out.lstrip("v").split(".")[:3])
    except Exception:
        return (0, 0, 0)


def _nowin():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0


def _run(cmd, cwd, env, step):
    _set(step=step)
    _log("$ " + " ".join(cmd))
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", creationflags=_nowin())
    for line in p.stdout:
        if "externalized for browser" in line:
            continue                                    # harmless Vite warnings, hundreds of lines
        _log(line)
    code = p.wait()
    if code != 0:
        raise RuntimeError(f"{step} failed (exit {code}). Last output:\n" + "\n".join(_state["log"][-8:]))


def _sync_sources():
    os.makedirs(BUILD_DIR, exist_ok=True)
    for name in os.listdir(BUILD_DIR):                  # drop stale sources but keep node_modules
        if name in ("node_modules",):
            continue
        p = os.path.join(BUILD_DIR, name)
        shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
    for name in os.listdir(SRC_DIR):
        if name in ("node_modules", "dist"):
            continue
        s, d = os.path.join(SRC_DIR, name), os.path.join(BUILD_DIR, name)
        shutil.copytree(s, d) if os.path.isdir(s) else shutil.copy2(s, d)


def _try_remove(p):
    try:
        os.remove(p)
    except OSError:
        pass


def _build_worker(force):
    try:
        if not sources_present():
            raise RuntimeError("The workbench sources are missing (gui/workbench/package.json).")
        npm, node = _which("npm"), _which("node")
        if not (npm and node):
            raise RuntimeError("Node.js and npm are required for the one-time build. Install Node 18+ from nodejs.org, then retry.")
        if _node_version() < (18, 0, 0):
            raise RuntimeError("Node.js 18 or newer is required for the build (found an older version).")
        want = sources_hash()
        if not force and is_built() and built_hash() == want:
            _set(state="ready", step="up to date")
            return
        _set(state="building", step="Preparing sources", error="", started=time.time(), log=[])
        _sync_sources()
        env = dict(os.environ)
        env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + " --max-old-space-size=6144").strip()
        env["npm_config_fund"] = "false"
        env["npm_config_audit"] = "false"
        env["CI"] = "1"
        marker = os.path.join(BUILD_DIR, "node_modules", ".midum-pkg-hash")
        pkg_hash = hashlib.sha256(open(os.path.join(BUILD_DIR, "package.json"), "rb").read()).hexdigest()
        have = ""
        try:
            have = open(marker, "r").read().strip()
        except OSError:
            pass
        if have != pkg_hash:
            _run([npm, "install", "--no-audit", "--no-fund", "--loglevel=error"], BUILD_DIR, env, "Installing packages (first time only, needs internet)")
            os.makedirs(os.path.dirname(marker), exist_ok=True)
            with open(marker, "w") as fh:
                fh.write(pkg_hash)
        tmp = DIST_DIR + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        _run([npm, "run", "build", "--", "--outDir", tmp, "--emptyOutDir"], BUILD_DIR, env, "Building the VS Code workbench")
        if not os.path.isfile(os.path.join(tmp, "index.html")):
            raise RuntimeError("The build finished but produced no index.html.")
        with open(os.path.join(tmp, STAMP), "w", encoding="utf-8") as fh:
            fh.write(want)
        old = DIST_DIR + ".old"
        shutil.rmtree(old, ignore_errors=True)
        try:
            if os.path.isdir(DIST_DIR):
                os.replace(DIST_DIR, old)
            try:
                os.replace(tmp, DIST_DIR)
            except OSError:
                if os.path.isdir(old) and not os.path.isdir(DIST_DIR):
                    os.replace(old, DIST_DIR)                      # put the previous bundle back, then fall back below
                raise
            shutil.rmtree(old, ignore_errors=True)
        except OSError as e:
            # Windows refuses to rename a folder that something (the running app, antivirus, an indexer) holds open.
            # Fall back to updating the served folder in place: clear it, copy the new files over, write the stamp last
            # (a half-finished copy keeps a stale stamp, so the next launch simply rebuilds).
            _log("Could not swap the build folder (%s); updating it in place instead." % e)
            os.makedirs(DIST_DIR, exist_ok=True)
            for name in os.listdir(DIST_DIR):
                p = os.path.join(DIST_DIR, name)
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else _try_remove(p)
            stamp_src = os.path.join(tmp, STAMP)
            shutil.copytree(tmp, DIST_DIR, dirs_exist_ok=True, ignore=shutil.ignore_patterns(STAMP))
            shutil.copy2(stamp_src, os.path.join(DIST_DIR, STAMP))
            shutil.rmtree(tmp, ignore_errors=True)
        _set(state="ready", step="Done")
    except Exception as e:
        _set(state="error", error=str(e), step="Failed")
        _log("ERROR: " + str(e))


def build_async(force=False):
    """Start the build in the background (no-op if one is running). Returns status()."""
    global _build_thread
    with _state_lock:
        running = _build_thread is not None and _build_thread.is_alive()
    if not running:
        _set(state="building" if (force or not is_built() or built_hash() != (sources_hash() if sources_present() else "")) else "ready")
        _build_thread = threading.Thread(target=_build_worker, args=(force,), name="midum-workbench-build", daemon=True)
        _build_thread.start()
    return status()


# ----------------------------------------------------------------------------------------------------------
# Static files (/workbench.html, /wb/*)
# ----------------------------------------------------------------------------------------------------------
_MIME = {".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".json": "application/json; charset=utf-8", ".wasm": "application/wasm", ".svg": "image/svg+xml",
         ".ttf": "font/ttf", ".woff": "font/woff", ".woff2": "font/woff2", ".html": "text/html; charset=utf-8",
         ".map": "application/json", ".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8"}


def handle_static(h, path):
    """GET /workbench.html and /wb/<file>. Returns True if handled."""
    if path.startswith("/ext/"):
        return handle_ext(h, path)

    if path == "/workbench.html":
        idx = os.path.join(DIST_DIR, "index.html")
        if not os.path.isfile(idx):
            h._send(503, b"The VS Code workbench has not been built yet.")
            return True
        with open(idx, "r", encoding="utf-8") as fh:
            html = fh.read()
        h._send(200, html.replace("__MIDUM_TOKEN__", token()).encode("utf-8"), "text/html; charset=utf-8")
        return True
    if not path.startswith("/wb/"):
        return False
    root = os.path.realpath(DIST_DIR)
    rel = path[len("/wb/"):]
    full = os.path.realpath(os.path.join(root, *[p for p in rel.split("/") if p not in ("", ".", "..")]))
    if not (full == root or full.startswith(root + os.sep)) or not os.path.isfile(full):
        h._send(404, b"not found")
        return True
    ext = os.path.splitext(full)[1].lower()
    ctype = _MIME.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
    with open(full, "rb") as fh:
        data = fh.read()
    h.send_response(200)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-cache" if rel.endswith("index.html") else "public, max-age=86400")
    h.send_header("Access-Control-Allow-Origin", "*")          # webview iframes on {uuid}.localhost load fonts/scripts
    h.send_header("X-Content-Type-Options", "nosniff")
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(data)
    return True


# ----------------------------------------------------------------------------------------------------------
# Extension asset proxy (/ext/<host>/vscode/unpkg/...)
# ----------------------------------------------------------------------------------------------------------
# Marketplace extensions are installed with their location on https://open-vsx.org/vscode/unpkg/<publisher>/<name>/<ver>/...
# Icon themes (e.g. Material Icon Theme) reference their SVGs from CSS url(...). The embedded Chromium refuses to render those
# when the CDN answers with a non-image Content-Type under `nosniff` (or blocks hotlinking), so every file icon comes out
# blank. The page rewrites such CSS urls to this route, which fetches the file once, serves it with the right type and
# caches it on disk (so icons also work offline afterwards).
EXT_HOSTS = ("open-vsx.org",)
EXT_CACHE = os.path.join(VENDOR, "ext_cache")
EXT_MAX = 25 * 1024 * 1024
_EXT_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
              ".webp": "image/webp", ".ico": "image/x-icon", ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
              ".otf": "font/otf", ".json": "application/json; charset=utf-8", ".css": "text/css; charset=utf-8"}
_ext_lock = threading.Lock()


def handle_ext(h, path):
    from urllib.parse import quote
    import urllib.request
    rest = path[len("/ext/"):]
    host, _, p = rest.partition("/")
    if host not in EXT_HOSTS or not p.startswith("vscode/unpkg/") or ".." in p.split("/") or "\x00" in p:
        h._send(404, b"not found")
        return True
    ext = os.path.splitext(p)[1].lower()
    ctype = _EXT_TYPES.get(ext)
    if ctype is None:                                   # only assets a theme can reference; nothing executable
        h._send(404, b"not found")
        return True
    cache = os.path.join(EXT_CACHE, hashlib.sha256((host + "/" + p).encode("utf-8")).hexdigest() + ext)
    data = None
    try:
        with open(cache, "rb") as fh:
            data = fh.read()
    except OSError:
        pass
    if data is None:
        try:
            req = urllib.request.Request("https://" + host + "/" + quote(p, safe="/"), headers={"User-Agent": "Midum-Editor", "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read(EXT_MAX + 1)
            if len(data) > EXT_MAX:
                raise OSError("asset too large")
            with _ext_lock:
                os.makedirs(EXT_CACHE, exist_ok=True)
                tmp = cache + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, cache)
        except Exception as e:
            h._send(502, ("could not fetch the extension asset: " + str(e)).encode("utf-8", "replace"))
            return True
    h.send_response(200)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "public, max-age=86400")
    h.send_header("Access-Control-Allow-Origin", "*")
    h.send_header("Cross-Origin-Resource-Policy", "cross-origin")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(data)
    return True


# ----------------------------------------------------------------------------------------------------------
# File system API
# ----------------------------------------------------------------------------------------------------------
class FsError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _oserr(e):
    import errno
    c = getattr(e, "errno", None)
    if isinstance(e, FileNotFoundError) or c == errno.ENOENT:
        return FsError("ENOENT", str(e))
    if isinstance(e, FileExistsError) or c == errno.EEXIST:
        return FsError("EEXIST", str(e))
    if isinstance(e, NotADirectoryError) or c == errno.ENOTDIR:
        return FsError("ENOTDIR", str(e))
    if isinstance(e, IsADirectoryError) or c == errno.EISDIR:
        return FsError("EISDIR", str(e))
    if c in (getattr(errno, "ENOTEMPTY", -1), 145):                     # 145 = ERROR_DIR_NOT_EMPTY on Windows
        return FsError("EEXIST", "The folder is not empty.")
    if isinstance(e, PermissionError) or c in (errno.EACCES, errno.EPERM):
        return FsError("EACCES", str(e))
    return FsError("EUNKNOWN", str(e))


def _hp(p):
    """Client path ("D:/x/y", "/home/u", "/") -> OS path. "/" on Windows is the virtual drive list (returns None)."""
    if not isinstance(p, str) or "\x00" in p:
        raise FsError("EINVAL", "bad path")
    if IS_WIN:
        q = p.replace("/", "\\")
        if q in ("", "\\"):
            return None
        if len(q) == 2 and q[1] == ":":
            q += "\\"
        return os.path.normpath(q)
    if not p.startswith("/"):
        raise FsError("EINVAL", "path must be absolute")
    return os.path.normpath(p)


def _ft(st_mode_is_dir, is_link):
    return (2 if st_mode_is_dir else 1) | (64 if is_link else 0)


def _ms(x):
    return int(x * 1000)


def _drives():
    out = []
    if IS_WIN:
        try:
            import ctypes
            mask = ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                if mask & (1 << i):
                    out.append(chr(97 + i) + ":")
        except Exception:
            out = [c.lower() + ":" for c in "cdefgh" if os.path.exists(c + ":\\")]
    return out


def op_info(req):
    trash = False
    try:
        import send2trash  # noqa: F401
        trash = True
    except ImportError:
        pass
    ws = ""
    try:
        ws = _hooks["default_workspace"]() if _hooks["default_workspace"] else ""
    except Exception:
        pass
    os.makedirs(os.path.join(USER_DIR, "User"), exist_ok=True)
    return {"ok": True, "win": IS_WIN, "sep": os.sep, "caseSensitive": not (IS_WIN or sys.platform == "darwin"),
            "home": os.path.expanduser("~").replace("\\", "/"), "userData": USER_DIR.replace("\\", "/"),
            "defaultWorkspace": ws.replace("\\", "/") if ws else "", "trash": trash}


def op_stat(req):
    p = _hp(req.get("path"))
    if p is None:
        return {"ok": True, "type": 2, "ctime": 0, "mtime": 0, "size": 0}
    is_link = False
    try:
        lst = os.lstat(p)
        is_link = (lst.st_mode & 0o170000) == 0o120000 or (IS_WIN and os.path.islink(p))
        st = os.stat(p) if is_link else lst
    except OSError as e:
        if is_link_broken(p):
            return {"ok": True, "type": 64, "ctime": 0, "mtime": 0, "size": 0}
        raise _oserr(e)
    isdir = os.path.isdir(p)
    return {"ok": True, "type": _ft(isdir, is_link), "ctime": _ms(st.st_ctime), "mtime": _ms(st.st_mtime),
            "size": 0 if isdir else st.st_size, "readonly": not os.access(p, os.W_OK)}


def is_link_broken(p):
    try:
        return os.path.islink(p) and not os.path.exists(p)
    except OSError:
        return False


def op_readdir(req):
    p = _hp(req.get("path"))
    if p is None:
        return {"ok": True, "entries": [[d, 2] for d in _drives()]}
    entries = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    link = e.is_symlink()
                    d = e.is_dir()                                  # follows links
                    entries.append([e.name, _ft(d, link)])
                except OSError:
                    entries.append([e.name, 0])
    except OSError as e:
        raise _oserr(e)
    return {"ok": True, "entries": entries}


def op_mkdir(req):
    p = _hp(req.get("path"))
    if p is None:
        raise FsError("EACCES", "cannot create a drive")
    try:
        os.mkdir(p)
    except OSError as e:
        raise _oserr(e)
    return {"ok": True}


def op_delete(req):
    p = _hp(req.get("path"))
    if p is None or os.path.dirname(p) == p:
        raise FsError("EACCES", "Refusing to delete a drive or the filesystem root.")
    try:
        if req.get("trash"):
            try:
                from send2trash import send2trash
                send2trash(p)
                return {"ok": True, "trashed": True}
            except ImportError:
                pass
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p) if req.get("recursive") else os.rmdir(p)
        else:
            os.remove(p)
    except OSError as e:
        raise _oserr(e)
    return {"ok": True}


def _move(src, dst, overwrite):
    if os.path.exists(dst) or os.path.islink(dst):
        same = os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst))
        if not overwrite and not (same and src != dst):             # case-only rename on a case-insensitive FS is allowed
            raise FsError("EEXIST", "The destination already exists.")
        if not same:
            shutil.rmtree(dst) if os.path.isdir(dst) and not os.path.islink(dst) else os.remove(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError:
        shutil.move(src, dst)                                       # across drives


def op_rename(req):
    a, b = _hp(req.get("from")), _hp(req.get("to"))
    if a is None or b is None:
        raise FsError("EACCES", "cannot rename a drive")
    try:
        _move(a, b, bool(req.get("overwrite")))
    except OSError as e:
        raise _oserr(e)
    return {"ok": True}


def op_copy(req):
    a, b = _hp(req.get("from")), _hp(req.get("to"))
    if a is None or b is None:
        raise FsError("EACCES", "cannot copy a drive")
    try:
        if os.path.exists(b):
            if not req.get("overwrite"):
                raise FsError("EEXIST", "The destination already exists.")
            shutil.rmtree(b) if os.path.isdir(b) and not os.path.islink(b) else os.remove(b)
        os.makedirs(os.path.dirname(b), exist_ok=True)
        if os.path.isdir(a):
            shutil.copytree(a, b, symlinks=True)
        else:
            shutil.copy2(a, b)
    except OSError as e:
        raise _oserr(e)
    return {"ok": True}


def _send_json(h, obj, code=200):
    h._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")


def do_read(h, req):
    p = _hp(req.get("path"))
    if p is None:
        raise FsError("EISDIR", "is a directory")
    try:
        if os.path.isdir(p):
            raise FsError("EISDIR", "is a directory")
        st = os.stat(p)
        if st.st_size > MAX_READ:
            raise FsError("ETOOLARGE", f"File is too large ({st.st_size // (1024 * 1024)} MB)")
        with open(p, "rb") as fh:
            data = fh.read()
    except OSError as e:
        raise _oserr(e)
    h.send_response(200)
    h.send_header("Content-Type", "application/octet-stream")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("X-Midum-Mtime", str(_ms(st.st_mtime)))
    h.send_header("X-Midum-Size", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(data)


def do_write(h):
    from urllib.parse import unquote as _uq
    p = _hp(_uq(h.headers.get("X-Midum-Path", "")))
    create = h.headers.get("X-Midum-Create") == "1"
    overwrite = h.headers.get("X-Midum-Overwrite") == "1"
    n = int(h.headers.get("Content-Length") or 0)
    if n < 0 or n > MAX_WRITE:
        raise FsError("ETOOLARGE", "body too large")
    data = h.rfile.read(n) if n else b""
    if p is None:
        raise FsError("EACCES", "cannot write to the drive list")
    try:
        exists = os.path.exists(p)
        if os.path.isdir(p):
            raise FsError("EISDIR", "is a directory")
        if exists and not overwrite:
            raise FsError("EEXIST", "File already exists")
        if not exists and not create:
            raise FsError("ENOENT", "File does not exist")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        st = os.stat(p)
    except OSError as e:
        raise _oserr(e)
    _send_json(h, {"ok": True, "mtime": _ms(st.st_mtime), "size": st.st_size})


# -- glob handling (VS Code exclude patterns) ---------------------------------------------
_glob_cache = {}


def glob_re(g):
    r = _glob_cache.get(g)
    if r is not None:
        return r
    s, i, out = g.replace("\\", "/"), 0, []
    while i < len(s):
        c = s[i]
        if c == "*":
            if s[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if s[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "{":
            j = s.find("}", i)
            if j > 0:
                out.append("(?:" + "|".join(glob_re_body(x) for x in s[i + 1:j].split(",")) + ")")
                i = j + 1
                continue
            out.append(re.escape(c))
        elif c == "[":
            j = s.find("]", i)
            if j > 0:
                out.append("[" + s[i + 1:j].replace("\\", "\\\\") + "]")
                i = j + 1
                continue
            out.append(re.escape(c))
        else:
            out.append(re.escape(c))
        i += 1
    try:
        r = re.compile("^(?:" + "".join(out) + ")$", re.I if IS_WIN else 0)
    except re.error:
        r = re.compile("$^")
    _glob_cache[g] = r
    return r


def glob_re_body(x):
    """One alternative inside {a,b}: literal text with * as a wildcard."""
    return re.escape(x).replace("\\*", "[^/]*")


def _matcher(globs):
    res = [glob_re(g) for g in globs or [] if g]
    return (lambda rel: any(r.match(rel) for r in res)) if res else (lambda rel: False)


def walk_files(root, excludes, include=None, limit=WALK_MAX_FILES, deadline=None):
    """Yield (abs_path, rel_posix) for files under root, pruning SKIP_DIRS-like heavy dirs only via the client's excludes
    (VS Code sends files.exclude + search.exclude), plus .git/node_modules which are always skipped for speed."""
    ex = _matcher(excludes)
    inc = _matcher(include) if include else None
    count = 0
    for dp, dirs, files in os.walk(root, followlinks=False):
        if deadline and time.time() > deadline:
            return
        reld = os.path.relpath(dp, root).replace("\\", "/")
        reld = "" if reld == "." else reld
        dirs[:] = sorted(d for d in dirs if d not in (".git", ".svn", ".hg", "node_modules", "__pycache__")
                         and not ex((reld + "/" + d) if reld else d))
        for f in sorted(files):
            rel = (reld + "/" + f) if reld else f
            if ex(rel):
                continue
            if inc is not None and not inc(rel):
                continue
            count += 1
            if count > limit:
                return
            yield os.path.join(dp, f), rel


def _subseq_score(pat, text):
    """Fuzzy subsequence match; None if no match, else a score (higher = better)."""
    p, t = pat.lower(), text.lower()
    i = 0
    score = 0
    last = -2
    for j, ch in enumerate(t):
        if i < len(p) and ch == p[i]:
            score += 3 if j == last + 1 else 1
            if j == 0 or t[j - 1] in "/\\._- ":
                score += 4
            last = j
            i += 1
    return score if i == len(p) else None


def op_find(req):
    roots = req.get("roots") or []
    pat = (req.get("pattern") or "").strip()
    excl_global = req.get("exclude") or []
    include = req.get("include") or []
    mx = max(1, min(int(req.get("max") or 2000), 20000))
    use_glob = any(c in pat for c in "*?")
    gre = glob_re("**/" + pat) if use_glob else None
    hits = []
    deadline = time.time() + 8
    for r in roots:
        root = _hp(r.get("path"))
        if not root or not os.path.isdir(root):
            continue
        ex = list(excl_global) + list(r.get("excludes") or [])
        for ap, rel in walk_files(root, ex, include, deadline=deadline):
            if not pat:
                hits.append((0, ap))
            elif use_glob:
                if gre.match(rel):
                    hits.append((0, ap))
            else:
                base = rel.rsplit("/", 1)[-1]
                s = _subseq_score(pat, rel)
                if s is None:
                    continue
                if pat.lower() in base.lower():
                    s += 30
                s -= len(rel) // 20
                hits.append((s, ap))
            if len(hits) >= mx * 4:
                break
    if pat and not use_glob:
        hits.sort(key=lambda x: -x[0])
    limit_hit = len(hits) > mx
    return {"ok": True, "files": [a.replace("\\", "/") for _, a in hits[:mx]], "limitHit": limit_hit}


def _is_binary(chunk):
    return b"\x00" in chunk


def _u16(line, idx):
    """Python str index -> UTF-16 code unit offset (what Monaco columns use)."""
    if all(ord(c) < 0x10000 for c in line[:idx]):
        return idx
    return len(line[:idx].encode("utf-16-le")) // 2


def op_grep(req):
    roots = req.get("roots") or []
    pat = req.get("pattern") or ""
    if not pat:
        return {"ok": True, "files": [], "limitHit": False}
    body = pat if req.get("regex") else re.escape(pat)
    if req.get("word"):
        body = r"\b(?:" + body + r")\b"
    try:
        rx = re.compile(body, 0 if req.get("caseSensitive") else re.I)
    except re.error as e:
        raise FsError("EINVAL", "Invalid regular expression: " + str(e))
    mx = max(1, min(int(req.get("max") or 5000), 50000))
    out, total, limit_hit = [], 0, False
    deadline = time.time() + 20
    for r in roots:
        root = _hp(r.get("path"))
        if not root or not os.path.isdir(root):
            continue
        ex = list(req.get("exclude") or []) + list(r.get("excludes") or [])
        for ap, rel in walk_files(root, ex, req.get("include") or [], deadline=deadline):
            try:
                if os.path.getsize(ap) > 4 * 1024 * 1024:
                    continue
                with open(ap, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            if _is_binary(raw[:8192]):
                continue
            text = raw.decode("utf-8", "replace")
            matches = []
            for ln, line in enumerate(text.split("\n"), 1):
                if line.endswith("\r"):
                    line = line[:-1]
                for m in rx.finditer(line):
                    if m.end() == m.start():
                        continue
                    matches.append({"line": ln, "start": _u16(line, m.start()), "end": _u16(line, m.end()), "text": line[:1000]})
                    total += 1
                    if total >= mx:
                        break
                if total >= mx:
                    break
            if matches:
                out.append({"path": ap.replace("\\", "/"), "matches": matches})
            if total >= mx:
                limit_hit = True
                break
            if time.time() > deadline:
                limit_hit = True
                break
        if limit_hit:
            break
    return {"ok": True, "files": out, "limitHit": limit_hit}


# -- change polling -----------------------------------------------------------------------
_watch = {}                    # id -> {"path", "recursive", "snap"}
_watch_lock = threading.Lock()
SNAP_MAX = 20000
SNAP_DEPTH = 8


def _snapshot(path, recursive):
    snap = {}
    try:
        if os.path.isfile(path):
            st = os.stat(path)
            return {"": (st.st_mtime_ns, st.st_size, False)}
        if not recursive:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        st = e.stat()
                        snap[e.name] = (st.st_mtime_ns, st.st_size, e.is_dir())
                    except OSError:
                        pass
            return snap
        base_depth = path.rstrip("\\/").count(os.sep)
        for dp, dirs, files in os.walk(path, followlinks=False):
            if dp.count(os.sep) - base_depth >= SNAP_DEPTH:
                dirs[:] = []
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for nm in dirs + files:
                full = os.path.join(dp, nm)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                snap[os.path.relpath(full, path).replace("\\", "/")] = (st.st_mtime_ns, st.st_size, os.path.isdir(full))
                if len(snap) >= SNAP_MAX:
                    return snap
    except OSError:
        pass
    return snap


def op_poll(req):
    watches = req.get("watches") or []
    changes = []
    ids = set()
    for w in watches[:40]:
        wid = w.get("id")
        try:
            p = _hp(w.get("path"))
        except FsError:
            continue
        if not p:
            continue
        ids.add(wid)
        rec = bool(w.get("recursive"))
        with _watch_lock:
            cur = _watch.get(wid)
        snap = _snapshot(p, rec)
        if cur is None or cur["path"] != p or cur["recursive"] != rec:
            with _watch_lock:
                _watch[wid] = {"path": p, "recursive": rec, "snap": snap}
            continue
        old = cur["snap"]
        base = p.replace("\\", "/").rstrip("/")
        for k, v in snap.items():
            full = base if k == "" else base + "/" + k
            if k not in old:
                changes.append({"type": 1, "path": full})
            elif old[k][:2] != v[:2]:
                if not v[2]:
                    changes.append({"type": 0, "path": full})
        for k in old:
            if k not in snap:
                changes.append({"type": 2, "path": base if k == "" else base + "/" + k})
        cur["snap"] = snap
    with _watch_lock:
        for k in [k for k in _watch if k not in ids]:
            del _watch[k]
    return {"ok": True, "changes": changes[:2000]}


_JSON_OPS = {"info": op_info, "stat": op_stat, "readdir": op_readdir, "mkdir": op_mkdir, "delete": op_delete,
             "rename": op_rename, "copy": op_copy, "find": op_find, "grep": op_grep, "poll": op_poll}


def _authorized(h):
    port = h.server.server_address[1]
    hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
    origin = h.headers.get("Origin")
    if (h.headers.get("Host") or "").lower() not in hosts:
        return False
    if origin and origin not in tuple(f"http://{x}" for x in hosts):
        return False
    t = h.headers.get("X-Midum-Token", "")
    return bool(t) and secrets.compare_digest(t, token())


def handle_fs(h, op):
    """POST /api/fs/<op>. Same-origin + token protected, like /api/inline."""
    try:
        if not _authorized(h):
            return h._send(403, b"forbidden")
        if op == "write":
            return do_write(h)
        n = int(h.headers.get("Content-Length") or 0)
        if n < 0 or n > 4 * 1024 * 1024:
            return h._send(413, b"bad size")
        req = json.loads(h.rfile.read(n).decode("utf-8", "replace")) if n else {}
        if not isinstance(req, dict):
            req = {}
        if op == "read":
            return do_read(h, req)
        fn = _JSON_OPS.get(op)
        if fn is None:
            return h._send(404, b"unknown op")
        return _send_json(h, fn(req))
    except FsError as e:
        try:
            # expected errors (missing file, exists, ...) are normal VS Code probing: answer 200 so the browser console stays quiet
            _send_json(h, {"ok": False, "code": e.code, "error": str(e)})
        except Exception:
            pass
    except Exception as e:
        try:
            _send_json(h, {"ok": False, "code": "EUNKNOWN", "error": str(e)}, 500)
        except Exception:
            pass
