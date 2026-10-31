"""Artifact system: user-attached / model-created files the model can read at any time.

Design
------
* Every file lives in ``storage/artifacts/files/<id>/<name>`` with a manifest in
  ``storage/artifacts/index.json``. Artifacts are scoped to the chat they were
  attached in (``chat_id``); ``list_artifacts(scope="all")`` reaches the rest.
* On ingest a background worker (1) extracts text (cached) and (2) uploads the
  file to the Gemini Files API using the configured Gemini API key, so
  ``analyze_artifact`` can reference it natively (PDF layout, audio, video...).
* The model sees the available files via ``prompt_block()`` which the
  orchestrator appends to the system prompt on EVERY step, and reads them with
  the tools defined in ``TOOL_SCHEMAS`` / ``dispatch``.
* Image attachments are inlined into the Gemini request by the Gemini backend
  (see ``inline_eligible_indices`` / ``build_user_parts``).

Authoring (Claude-style artifacts)
----------------------------------
Artifacts the MODEL creates (``origin == "generated"``) are editable and versioned:
``create_artifact`` -> ``update_artifact`` (exact-match edits) / ``rewrite_artifact`` ->
``list_artifact_versions`` / ``revert_artifact``. Every write is a new version (snapshots live in
``files/<id>/.versions``). html / markdown / svg / mermaid / react artifacts are shown live in the
Render Surface panel via the GUI render hook.

Uploaded text / code / csv files (``origin == "upload"``) are ALSO editable and versioned: on ingest they get an
``editable`` working copy (v1 = the upload). Edits only ever change Midum's own copy under ``files/<id>/``; the original
file on the user's computer (``source_path``) is never opened for writing. Uploaded PDF / Office / image / media files
stay read-only (the model can read them and create_artifact a modified copy).

Only ``config`` is imported at module level so this is safe to import from
``tools_schema`` / ``orchestration`` / the GUI without circular imports.
"""
import base64
import csv
import hashlib
import html as _html
import io
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from datetime import datetime

import config

ARTIFACTS_DIR = os.path.join(config.STORAGE_DIR, "artifacts")
FILES_DIR = os.path.join(ARTIFACTS_DIR, "files")
INDEX_FILE = os.path.join(ARTIFACTS_DIR, "index.json")
INCOMING_DIR = os.path.join(ARTIFACTS_DIR, "_incoming")

MAX_UPLOAD_BYTES = int(getattr(config, "ARTIFACT_MAX_UPLOAD_MB", 100)) * 1024 * 1024
GEMINI_UPLOAD_ENABLED = bool(getattr(config, "ARTIFACT_GEMINI_UPLOAD", True))
INLINE_IMAGE_MAX_BYTES = 15 * 1024 * 1024
INLINE_IMAGE_MAX_COUNT = 6
INLINE_MESSAGES = 2          # how many recent attachment-bearing user messages keep images inline
INLINE_ANALYZE_MAX_BYTES = 18 * 1024 * 1024
READ_DEFAULT_CHARS = 20000
READ_MAX_CHARS = 60000
PROMPT_LIST_MAX = 40
GEMINI_INLINE_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}

TOOL_NAMES = ("list_artifacts", "read_artifact", "search_artifact",
              "view_artifact", "analyze_artifact", "create_artifact", "add_artifact_from_path",
              "update_artifact", "rewrite_artifact", "open_artifact",
              "list_artifact_versions", "revert_artifact", "open_in_editor")
MAX_VERSIONS = 30            # snapshots kept per model-created artifact

_lock = threading.RLock()
_index = None            # id -> record (lazy)
_active_chat_id = ""
_incoming = {}           # upload_id -> {"path":..., "next":int, "size":int}

_TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".env", ".xml", ".html", ".htm", ".css", ".scss", ".js", ".jsx",
    ".ts", ".tsx", ".py", ".pyi", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb",
    ".php", ".sh", ".bat", ".ps1", ".sql", ".kt", ".swift", ".lua", ".r", ".tex", ".vue", ".svelte",
    ".gradle", ".properties", ".diff", ".patch", ".mmd", ".mermaid",
}
_CODE_EXT = {".js", ".jsx", ".ts", ".tsx", ".py", ".pyi", ".java", ".c", ".h", ".cpp", ".hpp", ".cs",
             ".go", ".rs", ".rb", ".php", ".sh", ".bat", ".ps1", ".sql", ".kt", ".swift", ".lua", ".r",
             ".vue", ".svelte", ".css", ".scss", ".html", ".htm", ".xml"}
_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".aiff"}
_VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".mpeg", ".mpg", ".wmv", ".flv", ".3gp"}
_ARCHIVE_EXT = {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bz2"}
_MIME_FIXES = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".json": "application/json", ".m4a": "audio/mp4", ".opus": "audio/ogg", ".heic": "image/heic",
    ".heif": "image/heif", ".webp": "image/webp",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


# ── helpers ────────────────────────────────────────────────────────────
def set_active_chat(chat_id):
    global _active_chat_id
    _active_chat_id = chat_id or ""
    try:
        import midumdocs
        midumdocs.on_chat_changed(_active_chat_id)
    except Exception:
        pass


def get_active_chat():
    return _active_chat_id


def _safe_name(name):
    base = os.path.basename(str(name or "")).strip() or "file"
    base = re.sub(r"[^\w.\- ()\[\]]+", "_", base, flags=re.UNICODE)
    return base[:120] or "file"


def _guess_mime(name, mime=None):
    ext = os.path.splitext(name)[1].lower()
    if mime and mime not in ("application/octet-stream", ""):
        return mime
    if ext in _MIME_FIXES:
        return _MIME_FIXES[ext]
    guessed, _ = mimetypes.guess_type(name)
    if guessed:
        return guessed
    if ext in _TEXT_EXT:
        return "text/plain"
    return "application/octet-stream"


def classify(name, mime):
    ext = os.path.splitext(name)[1].lower()
    mime = (mime or "").lower()
    if ext == ".svg":           # SVG is XML text: editable/readable as source, not a raster image
        return "code"
    if mime.startswith("image/"):
        return "image"
    if ext == ".pdf" or mime == "application/pdf":
        return "pdf"
    if ext == ".docx":
        return "doc"
    if ext in (".xlsx", ".xlsm", ".csv", ".tsv"):
        return "sheet"
    if ext == ".pptx":
        return "slides"
    if mime.startswith("audio/") or ext in _AUDIO_EXT:
        return "audio"
    if mime.startswith("video/") or ext in _VIDEO_EXT:
        return "video"
    if ext in _ARCHIVE_EXT:
        return "archive"
    if ext in _CODE_EXT:
        return "code"
    if ext in _TEXT_EXT or mime.startswith("text/") or mime in ("application/json", "application/xml"):
        return "text"
    return "binary"


def _fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _load():
    global _index
    with _lock:
        if _index is None:
            _index = {}
            try:
                if os.path.exists(INDEX_FILE):
                    with open(INDEX_FILE, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    if isinstance(data, dict):
                        _index = data
            except Exception as e:
                print(f"⚠️ [artifacts] could not read index ({e}); starting empty")
                _index = {}
            _upgrade_old_uploads(_index)
        return _index


def _save():
    with _lock:
        idx = _load()
        os.makedirs(ARTIFACTS_DIR, exist_ok=True)
        tmp = INDEX_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(idx, fh, indent=1)
        os.replace(tmp, INDEX_FILE)


EDITABLE_UPLOAD_MAX = 5 * 1024 * 1024      # larger uploads stay read-only (the editor/read tools page text anyway)


def is_editable(rec):
    """True for model-created artifacts and for uploaded text/code files that have an editable working copy."""
    return bool(rec) and (rec.get("origin") == "generated" or bool(rec.get("editable")))


def _make_editable(rec):
    """Give an uploaded text/code/csv file a versioned, editable working copy (v1 = the upload as received).

    Only Midum's stored copy is ever written; ``source_path`` (the original on the user's disk) is not touched.
    Returns True when the record was changed."""
    if rec.get("origin") != "upload" or rec.get("editable"):
        return False
    ext = os.path.splitext(rec.get("name") or "")[1].lower()
    kind = rec.get("kind")
    if not (kind in ("text", "code") or (kind == "sheet" and ext in (".csv", ".tsv"))):
        return False
    if int(rec.get("size") or 0) > EDITABLE_UPLOAD_MAX:
        return False
    blob = _blob_path(rec)
    if not os.path.isfile(blob):
        return False
    os.makedirs(_vdir(rec), exist_ok=True)
    if not os.path.exists(_vpath(rec, 1)):
        shutil.copyfile(blob, _vpath(rec, 1))
    rec["editable"] = True
    rec["art_type"] = _infer_type(rec["name"])
    rec.setdefault("title", "")
    rec["version"] = 1
    rec["versions"] = [{"n": 1, "ts": rec.get("created", time.time()), "size": int(rec.get("size") or 0), "note": "uploaded"}]
    return True


def _upgrade_old_uploads(idx):
    """Once per run: files uploaded before editable uploads existed get their working copy too."""
    changed = False
    for rec in list(idx.values()):
        try:
            changed = _make_editable(rec) or changed
        except Exception as e:
            print(f"⚠️ [artifacts] could not make {rec.get('id')} editable ({e})")
    if changed:
        try:
            _save()
        except Exception:
            pass


def _blob_path(rec):
    return os.path.join(FILES_DIR, rec["id"], rec["stored_name"])


def _text_cache_path(rec):
    return os.path.join(FILES_DIR, rec["id"], ".text.txt")


def public_view(rec):
    """Small JSON-safe dict for the GUI."""
    gem = rec.get("gemini") or {}
    text_state = (rec.get("text") or {}).get("status", "none")
    busy = text_state == "pending" or gem.get("state") == "uploading"
    return {
        "id": rec["id"], "name": rec["name"], "mime": rec["mime"], "size": rec["size"],
        "kind": rec["kind"], "origin": rec.get("origin", "upload"),
        "pages": (rec.get("text") or {}).get("pages", 0),
        "text_state": text_state, "gemini_state": gem.get("state", "none"),
        "gemini_error": gem.get("error", ""), "busy": busy,
        "art_type": art_type(rec) if is_editable(rec) else "", "editable": is_editable(rec),
        "version": rec.get("version", 0),
    }


# ── store ──────────────────────────────────────────────────────────────
def _finalize(tmp_path, name, mime, size, sha, chat_id, origin, source_path, move, upload, sent=False):
    name = os.path.basename(str(name or "file")) or "file"
    mime = _guess_mime(name, mime)
    with _lock:
        idx = _load()
        if origin == "upload":
            for r in idx.values():
                if r.get("sha256") == sha and r.get("chat_id") == chat_id and r.get("origin") == "upload":
                    if move:
                        try: os.remove(tmp_path)
                        except OSError: pass
                    return r
        art_id = "art_" + uuid.uuid4().hex[:8]
        stored = _safe_name(name)
        folder = os.path.join(FILES_DIR, art_id)
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, stored)
        if move:
            shutil.move(tmp_path, dest)
        else:
            shutil.copyfile(tmp_path, dest)
        rec = {
            "id": art_id, "name": name, "stored_name": stored, "mime": mime, "size": size,
            "sha256": sha, "kind": classify(name, mime), "origin": origin, "chat_id": chat_id,
            "created": time.time(), "source_path": source_path or "",
            "text": {"status": "pending", "chars": 0, "pages": 0, "preview": "", "note": ""},
            "gemini": {"state": "none"},
            "sent": bool(sent),     # True = already referenced by a message / added by the model -> never a composer chip
        }
        if origin == "upload":
            try:
                _make_editable(rec)
            except Exception as e:
                print(f"⚠️ [artifacts] could not make {art_id} editable ({e})")
        idx[art_id] = rec
        _save()
    threading.Thread(target=_process, args=(art_id, upload), daemon=True, name=f"artifact-{art_id}").start()
    return rec


def add_file(src_path=None, data=None, name=None, chat_id=None, origin="upload", mime=None, upload=True, sent=False):
    """Ingest a file from disk (``src_path``) or bytes (``data``). Returns the record."""
    chat_id = chat_id if chat_id is not None else _active_chat_id
    os.makedirs(INCOMING_DIR, exist_ok=True)
    tmp = os.path.join(INCOMING_DIR, uuid.uuid4().hex + ".tmp")
    h = hashlib.sha256()
    size = 0
    try:
        if src_path:
            src_path = os.path.abspath(os.path.expanduser(str(src_path)))
            if not os.path.isfile(src_path):
                raise FileNotFoundError(f"Not a file: {src_path}")
            if os.path.getsize(src_path) > MAX_UPLOAD_BYTES:
                raise ValueError(f"File is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")
            name = name or os.path.basename(src_path)
            with open(src_path, "rb") as fi, open(tmp, "wb") as fo:
                for chunk in iter(lambda: fi.read(1 << 20), b""):
                    h.update(chunk); fo.write(chunk); size += len(chunk)
        else:
            data = data if isinstance(data, (bytes, bytearray)) else str(data or "").encode("utf-8")
            if len(data) > MAX_UPLOAD_BYTES:
                raise ValueError("File exceeds the upload limit")
            with open(tmp, "wb") as fo:
                fo.write(data)
            h.update(data); size = len(data)
        if size == 0 and origin == "upload":
            raise ValueError("File is empty")
        return _finalize(tmp, name, mime, size, h.hexdigest(), chat_id, origin,
                         src_path if src_path else "", True, upload, sent)
    except Exception:
        try:
            if os.path.exists(tmp): os.remove(tmp)
        except OSError:
            pass
        raise


def receive_chunk(upload_id, name, mime, index, total, b64, chat_id=None):
    """Chunked upload for browser-side files (paste / drag-drop without a path)."""
    uid = re.sub(r"[^A-Za-z0-9_-]", "", str(upload_id))[:40]
    if not uid:
        raise ValueError("bad upload id")
    os.makedirs(INCOMING_DIR, exist_ok=True)
    with _lock:
        st = _incoming.get(uid)
        if st is None:
            if index != 0:
                raise ValueError("upload out of order")
            st = {"path": os.path.join(INCOMING_DIR, uid + ".part"), "next": 0, "size": 0}
            _incoming[uid] = st
        if index != st["next"]:
            raise ValueError("upload chunk out of order")
        raw = base64.b64decode(b64 or "")
        st["size"] += len(raw)
        if st["size"] > MAX_UPLOAD_BYTES:
            abort_upload(uid)
            raise ValueError(f"File is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")
        with open(st["path"], "ab") as fh:
            fh.write(raw)
        st["next"] += 1
        done = st["next"] >= total
        if not done:
            return {"ok": True, "done": False}
        _incoming.pop(uid, None)
    try:
        rec = add_file(src_path=st["path"], name=name, mime=mime, chat_id=chat_id)
    finally:
        try:
            if os.path.exists(st["path"]): os.remove(st["path"])
        except OSError:
            pass
    return {"ok": True, "done": True, "file": public_view(rec)}


def abort_upload(upload_id):
    uid = re.sub(r"[^A-Za-z0-9_-]", "", str(upload_id))[:40]
    with _lock:
        st = _incoming.pop(uid, None)
    if st:
        try: os.remove(st["path"])
        except OSError: pass


def get(ref, chat_id=None):
    """Resolve an id, id prefix, or file name (current chat first)."""
    if not ref:
        return None
    ref = str(ref).strip()
    chat_id = chat_id if chat_id is not None else _active_chat_id
    idx = _load()
    with _lock:
        recs = sorted(idx.values(), key=lambda r: r.get("created", 0), reverse=True)
    if ref in idx:
        return idx[ref]
    low = ref.lower()
    for pool in ([r for r in recs if r.get("chat_id") == chat_id], recs):
        for r in pool:
            if r["id"].lower() == low or r["name"].lower() == low:
                return r
        for r in pool:
            if r["id"].lower().startswith(low) and len(low) >= 6:
                return r
        for r in pool:
            if low in r["name"].lower():
                return r
    return None


def list_records(chat_id=None, scope="chat"):
    chat_id = chat_id if chat_id is not None else _active_chat_id
    with _lock:
        recs = list(_load().values())
    if scope != "all":
        recs = [r for r in recs if r.get("chat_id") == chat_id]
    return sorted(recs, key=lambda r: r.get("created", 0))


def pending_records(chat_id=None):
    """Uploads attached in the prompt box of ``chat_id`` that were never sent (the composer's chips).

    The prompt box is a per-chat draft: it is rebuilt from this list whenever the visible chat
    changes (tab switch, restore on startup...), so unsent attachments stay with their own tab.
    """
    return [r for r in list_records(chat_id=chat_id)
            if r.get("origin") == "upload" and not r.get("sent")]


def adopt(ids, chat_id):
    """Re-tag unsent attachments to the chat they are actually sent in."""
    out = []
    with _lock:
        idx = _load()
        for i in ids or []:
            r = idx.get(i)
            if r:
                r["chat_id"] = chat_id
                r["sent"] = True        # referenced by a message now -> chip-removal must not delete it
                out.append(r)
        _save()
    return out


def remove(art_id, only_unsent=False):
    with _lock:
        idx = _load()
        rec = idx.get(art_id)
        if rec and only_unsent and (rec.get("sent") or rec.get("origin") != "upload"):
            return False
        rec = idx.pop(art_id, None)
        if rec:
            _save()
    if not rec:
        return False
    shutil.rmtree(os.path.join(FILES_DIR, art_id), ignore_errors=True)
    g = rec.get("gemini") or {}
    if g.get("name"):
        def _del():
            try:
                from providers import gemini_api_backend as gb
                gb.gemini_files_delete(g["name"], g.get("key_index"))
            except Exception:
                pass
        threading.Thread(target=_del, daemon=True).start()
    return True


# ── text extraction ────────────────────────────────────────────────────
def _decode(raw):
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _extract_pdf(path):
    try:
        import pymupdf as fitz  # PyMuPDF >= 1.24.3 name
    except ImportError:
        import fitz             # older PyMuPDF
    doc = fitz.open(path)
    try:
        return "\f".join(page.get_text("text") for page in doc)
    finally:
        doc.close()


def _xml_text(xml, tag):
    return " ".join(re.findall(rf"<{tag}[^>]*>([^<]*)</{tag}>", xml))


def _extract_docx(path):
    try:
        import docx
        d = docx.Document(path)
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(parts)
    except ImportError:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        return re.sub(r"</w:p>", "\n", re.sub(r"<(?!/w:p>)[^>]+>", "", xml))


def _extract_pptx(path):
    with zipfile.ZipFile(path) as z:
        names = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                       key=lambda n: int(re.findall(r"\d+", n)[-1]))
        return "\f".join(_xml_text(z.read(n).decode("utf-8", "replace"), "a:t") for n in names)


def _extract_xlsx(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"## Sheet: {ws.title}")
        for row in ws.iter_rows(values_only=True):
            if any(c is not None for c in row):
                out.append("\t".join("" if c is None else str(c) for c in row))
            if sum(len(x) for x in out) > 3_000_000:
                out.append("[...truncated]"); break
    return "\n".join(out)


def _extract(rec):
    path, kind = _blob_path(rec), rec["kind"]
    ext = os.path.splitext(rec["name"])[1].lower()
    if kind == "pdf":
        return _extract_pdf(path)
    if kind == "doc":
        return _extract_docx(path)
    if kind == "slides":
        return _extract_pptx(path)
    if kind == "sheet" and ext in (".xlsx", ".xlsm"):
        return _extract_xlsx(path)
    if kind in ("text", "code", "sheet"):
        with open(path, "rb") as fh:
            raw = fh.read(25 * 1024 * 1024)
        text = _decode(raw)
        # Model-created html must come back as raw SOURCE (it needs the exact text to edit it).
        if ext in (".html", ".htm") and not is_editable(rec):
            text = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"[ \t]+", " ", text)
        return text
    return None


def get_text(rec):
    """Return cached extracted text ('\\f' separates pages/slides) or None."""
    cache = _text_cache_path(rec)
    if os.path.exists(cache):
        with open(cache, "r", encoding="utf-8") as fh:
            return fh.read()
    text = _extract(rec)
    if text is None:
        return None
    text = text[:5_000_000]
    with open(cache, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


def _process(art_id, upload):
    rec = _load().get(art_id)
    if not rec:
        return
    t = rec["text"]
    try:
        text = get_text(rec)
        if text is None:
            hint = {"audio": "use analyze_artifact", "video": "use analyze_artifact", "binary": "binary file, no readable text", "archive": "archive, no readable text"}.get(rec["kind"], "")
            t.update(status="none", note=hint)
        else:
            pages = text.count("\f") + 1 if rec["kind"] in ("pdf", "slides") else 0
            t.update(status="ready", chars=len(text), pages=pages,
                     preview=re.sub(r"\s+", " ", text[:240]).strip())
            if rec["kind"] == "pdf" and len(text.strip()) < 20:
                t["note"] = "scanned PDF (no text layer) - use analyze_artifact"
    except Exception as e:
        t.update(status="error", note=f"text extraction failed: {e}")
    try:
        _save()
    except Exception:
        pass
    if upload and GEMINI_UPLOAD_ENABLED and rec.get("origin") == "upload":
        try:
            ensure_gemini_file(rec)
        except Exception as e:
            print(f"⚠️ [artifacts] Gemini upload failed for {rec['name']}: {e}")


# ── Gemini Files API ───────────────────────────────────────────────────
_GEMINI_TEXT_MIMES = {"text/plain", "text/markdown", "text/csv", "text/html", "application/json", "text/xml"}


def _gemini_mime(rec):
    """Mime type to declare to the Files API (code/text files are sent as text/plain unless clearly supported)."""
    m = rec["mime"]
    if rec["kind"] in ("text", "code") and m not in _GEMINI_TEXT_MIMES:
        return "text/plain"
    return m


def _expired(g):
    exp = g.get("expires") or 0
    return bool(exp) and time.time() > exp - 300


def ensure_gemini_file(rec, force=False):
    """Make sure ``rec`` has a live Gemini Files API upload; returns the gemini info dict."""
    with _lock:
        g = rec.setdefault("gemini", {"state": "none"})
        if g.get("state") == "active" and not force and not _expired(g):
            return g
        if g.get("state") == "uploading":
            waiting = True
        else:
            waiting = False
            g.clear(); g["state"] = "uploading"
    if waiting:
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(1)
            if rec["gemini"].get("state") != "uploading":
                break
        if rec["gemini"].get("state") == "active":
            return rec["gemini"]
        raise RuntimeError(rec["gemini"].get("error") or "Gemini upload did not finish")
    try:
        from providers import gemini_api_backend as gb
        info = gb.gemini_files_upload(_blob_path(rec), _gemini_mime(rec), rec["name"])
        exp = 0
        try:
            exp = datetime.fromisoformat(re.sub(r"(\.\d+)?Z$", "+00:00", info.get("expirationTime", ""))).timestamp()
        except Exception:
            exp = time.time() + 47 * 3600
        new = {"state": "active", "name": info.get("name"), "uri": info.get("uri"),
               "mime": info.get("mimeType") or _gemini_mime(rec), "expires": exp,
               "key_index": info.get("key_index")}
    except Exception as e:
        new = {"state": "error", "error": str(e)[:300]}
    with _lock:
        rec["gemini"] = new
        _save()
    if new["state"] != "active":
        raise RuntimeError(new.get("error", "Gemini upload failed"))
    return new


def analyze_tool(arguments):
    arguments = arguments or {}
    rec = get(arguments.get("artifact"))
    if not rec:
        return _not_found(arguments.get("artifact"))
    question = (arguments.get("question") or "").strip() or "Summarize this file in detail, including key facts, structure and any notable content."
    try:
        from providers import gemini_api_backend as gb
    except Exception as e:
        return f"Error: Gemini backend unavailable: {e}"
    model = arguments.get("model") or None
    last_err = None
    for attempt in range(2):
        try:
            g = ensure_gemini_file(rec, force=(attempt == 1))
            text = gb.gemini_generate_with_files(
                question, files=[{"uri": g["uri"], "mime": g["mime"]}], model=model, key_index=g.get("key_index"))
            return f"[analysis of {rec['name']} ({rec['id']}) by Gemini]\n{text}"
        except Exception as e:
            last_err = e
            status = getattr(e, "status", None)
            if attempt == 0 and status in (400, 403, 404):
                continue
            break
    if rec["size"] <= INLINE_ANALYZE_MAX_BYTES:
        try:
            with open(_blob_path(rec), "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode()
            text = gb.gemini_generate_with_files(question, inline=[{"mime": _gemini_mime(rec), "data": b64}], model=model)
            return f"[analysis of {rec['name']} ({rec['id']}) by Gemini - inline]\n{text}"
        except Exception as e:
            last_err = e
    return f"Error: could not analyze {rec['name']}: {last_err}"


# ── model-facing operations ────────────────────────────────────────────
def _not_found(ref):
    names = ", ".join(f"{r['id']} ({r['name']})" for r in list_records(scope="all")[-12:]) or "none"
    return f"Error: no artifact matches '{ref}'. Known artifacts: {names}"


def _describe(r):
    t = r.get("text") or {}
    bits = [r["kind"], _fmt_size(r["size"])]
    if t.get("pages"):
        bits.append(f"{t['pages']} {'slides' if r['kind'] == 'slides' else 'pages'}")
    if t.get("status") == "ready" and t.get("chars", 0) > 20:
        bits.append(f"{t.get('chars', 0):,} chars of text")
    if t.get("note"):
        bits.append(t["note"])
    if r.get("origin") == "generated":
        bits.append(f"{art_type(r)} · created by you · v{r.get('version', 1)}")
    elif is_editable(r):
        bits.append(f"{art_type(r)} · attached, editable copy · v{r.get('version', 1)}")
    return f"{r['id']}  {r['name']}  [{' · '.join(bits)}]"


def list_tool(arguments):
    scope = (arguments or {}).get("scope", "chat")
    recs = list_records(scope=scope)
    if not recs:
        return "No artifacts" + (" in this chat." if scope != "all" else ".")
    return "\n".join(_describe(r) for r in recs)


def _parse_pages(spec, total):
    spec = str(spec).strip().lower().replace(" ", "")
    out = []
    for part in spec.split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a or 1), int(b or total)
            out.extend(range(a, b + 1))
        else:
            out.append(int(part))
    return [p for p in out if 1 <= p <= total]


def read_tool(arguments):
    arguments = arguments or {}
    rec = get(arguments.get("artifact"))
    if not rec:
        return _not_found(arguments.get("artifact"))
    if rec["kind"] == "image":
        return f"{rec['name']} is an image - call view_artifact to look at it (or analyze_artifact for a detailed description)."
    try:
        text = get_text(rec)
    except Exception as e:
        return f"Error: could not extract text from {rec['name']}: {e}. Try analyze_artifact."
    if text is None:
        return (f"{rec['name']} ({rec['kind']}) has no readable text layer. "
                f"Use analyze_artifact to have Gemini examine it natively.")
    try:
        length = int(arguments.get("length") or READ_DEFAULT_CHARS)
    except (TypeError, ValueError):
        length = READ_DEFAULT_CHARS
    length = max(200, min(length, READ_MAX_CHARS))
    paged = "\f" in text
    pages_spec = arguments.get("pages")
    if paged and pages_spec:
        pages = text.split("\f")
        try:
            wanted = _parse_pages(pages_spec, len(pages))
        except ValueError:
            return "Error: 'pages' must look like '3', '2-5' or '1,4,7'."
        if not wanted:
            return f"Error: no valid pages in '{pages_spec}' (document has {len(pages)})."
        label = "slide" if rec["kind"] == "slides" else "page"
        body = "\n\n".join(f"--- {label} {p} ---\n{pages[p - 1].strip()}" for p in wanted)
        clipped = len(body) > length
        out = body[:length]
        return (f"[{rec['name']} · {len(pages)} {label}s · showing {label}s {pages_spec}]\n{out}"
                + ("\n[...clipped: request fewer pages or call again with a smaller range]" if clipped else ""))
    if paged:
        label = "slide" if rec["kind"] == "slides" else "page"
        parts = text.split("\f")
        text = "\n\n".join(f"--- {label} {i + 1} ---\n{p.strip()}" for i, p in enumerate(parts))
    try:
        offset = max(0, int(arguments.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    total = len(text)
    chunk = text[offset:offset + length]
    end = offset + len(chunk)
    head = f"[{rec['name']} · chars {offset}-{end} of {total}]"
    tail = f"\n[more: call read_artifact again with offset={end}]" if end < total else "\n[end of file]"
    return head + "\n" + chunk + tail


def search_tool(arguments):
    arguments = arguments or {}
    rec = get(arguments.get("artifact"))
    if not rec:
        return _not_found(arguments.get("artifact"))
    query = str(arguments.get("query") or "").strip()
    if not query:
        return "Error: 'query' is required."
    try:
        limit = max(1, min(int(arguments.get("max_results") or 10), 40))
    except (TypeError, ValueError):
        limit = 10
    try:
        text = get_text(rec)
    except Exception as e:
        return f"Error: could not extract text: {e}"
    if text is None:
        return f"{rec['name']} has no searchable text; use analyze_artifact."
    hits, low, q = [], text.lower(), query.lower()
    pos = 0
    while len(hits) < limit:
        i = low.find(q, pos)
        if i < 0:
            break
        page = text.count("\f", 0, i) + 1 if "\f" in text else None
        snippet = re.sub(r"\s+", " ", text[max(0, i - 120): i + len(q) + 120]).strip()
        hits.append(f"- {'page ' + str(page) + ', ' if page else ''}char {i}: …{snippet}…")
        pos = i + len(q)
    if not hits:
        return f"No matches for '{query}' in {rec['name']}."
    return f"{len(hits)} match(es) for '{query}' in {rec['name']}:\n" + "\n".join(hits)


def _image_b64(rec, max_dim=2048):
    path = _blob_path(rec)
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.load()
            if max(im.size) > max_dim or rec["size"] > 4 * 1024 * 1024 or rec["mime"] not in GEMINI_INLINE_IMAGE_MIMES:
                im = im.convert("RGB")
                im.thumbnail((max_dim, max_dim))
                buf = io.BytesIO(); im.save(buf, "JPEG", quality=88)
                return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"
    except Exception:
        pass
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode(), rec["mime"]


def view_tool(arguments):
    """Returns (text, [base64 images])."""
    arguments = arguments or {}
    rec = get(arguments.get("artifact"))
    if not rec:
        return _not_found(arguments.get("artifact")), None
    if rec["kind"] != "image":
        return (f"{rec['name']} is a {rec['kind']}, not an image. Use read_artifact for its text or "
                f"analyze_artifact to have Gemini examine it natively (layout, figures, audio, video)."), None
    try:
        b64, _mime = _image_b64(rec)
    except Exception as e:
        return f"Error: could not load image: {e}", None
    return f"Image {rec['name']} ({rec['id']}) is attached below.", [b64]


# ── authored artifacts: types, versions, edit, live preview ───────────
RENDERABLE_TYPES = ("html", "markdown", "svg", "mermaid", "react", "latex")   # latex = MidumDocs (.tex)
ART_TYPES = RENDERABLE_TYPES + ("code", "text")
_TYPE_ALIASES = {
    "htm": "html", "web": "html", "webpage": "html", "page": "html", "app": "html",
    "md": "markdown", "doc": "markdown", "document": "markdown",
    "mmd": "mermaid", "diagram": "mermaid",
    "jsx": "react", "tsx": "react",
    "tex": "latex", "midumdoc": "latex", "midumdocs": "latex",
    "txt": "text", "plain": "text",
    "script": "code", "source": "code",
}
_TYPE_EXT = {"html": ".html", "markdown": ".md", "svg": ".svg", "mermaid": ".mmd",
             "react": ".jsx", "latex": ".tex", "code": ".txt", "text": ".txt"}

# Which artifact the Render Surface is currently showing (so edits can refresh it live).
_shown = {"id": None, "version": None, "sha": None, "ts": 0.0}
_gui_editor_hook = None      # set by gui.app.Api: hook(kind, payload) -> str, drives the Editor tab


def clear_shown():
    """Called by tools/render_surface_tools when the model takes the surface over directly."""
    _shown["id"] = None
    _shown["version"] = None
    _shown["sha"] = None
    _shown["ts"] = 0.0


def _notify_editor(art_id):
    """Tell the Editor tab that an artifact's content changed (so an open tab can refresh live)."""
    try:                                   # MidumDocs: push the new text to an open LaTeX editor
        import midumdocs
        midumdocs.on_artifact_changed(art_id)
    except Exception:
        pass
    hook = _gui_editor_hook
    if hook is None:
        return
    try:
        hook("artifact_changed", {"id": art_id})
    except Exception:
        pass


def _norm_type(t):
    t = str(t or "").strip().lower().lstrip(".")
    if not t:
        return None
    t = _TYPE_ALIASES.get(t, t)
    return t if t in ART_TYPES else None


def _infer_type(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in (".html", ".htm"):
        return "html"
    if ext in (".md", ".markdown"):
        return "markdown"
    if ext == ".svg":
        return "svg"
    if ext in (".mmd", ".mermaid"):
        return "mermaid"
    if ext in (".jsx", ".tsx"):
        return "react"
    if ext == ".tex":
        return "latex"
    if ext in _CODE_EXT:
        return "code"
    return "text"


def art_type(rec):
    return rec.get("art_type") or _infer_type(rec["name"])


def _truthy(v, default=False):
    if v is None or v == "":
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(v)


# ── versions ──
def _vdir(rec):
    return os.path.join(FILES_DIR, rec["id"], ".versions")


def _vpath(rec, n):
    return os.path.join(_vdir(rec), f"v{int(n)}.bin")


def _init_generated(rec, atype, title):
    """Turn a freshly ingested generated file into a versioned, typed artifact (version 1)."""
    with _lock:
        os.makedirs(_vdir(rec), exist_ok=True)
        shutil.copyfile(_blob_path(rec), _vpath(rec, 1))
        rec["art_type"] = atype
        rec["title"] = title
        rec["version"] = 1
        rec["versions"] = [{"n": 1, "ts": rec.get("created", time.time()), "size": rec["size"], "note": "created"}]
        rec["sent"] = True
        _save()


def _commit(rec, data, note=""):
    """Write ``data`` as the new current content of a generated artifact (= a new version). Returns the version number."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("content exceeds the size limit")
    src = rec.get("source_path") or ""
    if src:                                   # hard guard: an uploaded file's ORIGINAL on the user's disk is never written
        try:
            if os.path.normcase(os.path.realpath(src)) == os.path.normcase(os.path.realpath(_blob_path(rec))):
                raise ValueError("refusing to overwrite the original file")
        except OSError:
            pass
    with _lock:
        n = int(rec.get("version") or 0) + 1
        os.makedirs(_vdir(rec), exist_ok=True)
        with open(_vpath(rec, n), "wb") as fh:
            fh.write(data)
        blob = _blob_path(rec)
        tmp = blob + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, blob)
        rec["size"] = len(data)
        rec["sha256"] = hashlib.sha256(data).hexdigest()
        rec["version"] = n
        vs = rec.setdefault("versions", [])
        vs.append({"n": n, "ts": time.time(), "size": len(data), "note": str(note or "")[:120]})
        while len(vs) > MAX_VERSIONS:
            old = vs.pop(0)
            try:
                os.remove(_vpath(rec, old["n"]))
            except OSError:
                pass
        try:
            os.remove(_text_cache_path(rec))     # extracted-text cache is now stale
        except OSError:
            pass
        _save()
    _process(rec["id"], False)                    # refresh text metadata (synchronous, cheap)
    _notify_editor(rec["id"])
    return n


def _read_raw(rec, version=None):
    path = _vpath(rec, version) if version else _blob_path(rec)
    with open(path, "rb") as fh:
        return _decode(fh.read())


def _find_generated(name):
    low = name.lower()
    for r in reversed(list_records()):            # this chat, newest first
        if r.get("origin") == "generated" and r["name"].lower() == low:
            return r
    return None


def _resolve_editable(ref):
    rec = get(ref)
    if not rec:
        return None, _not_found(ref)
    if not is_editable(rec):
        return None, (f"Error: {rec['name']} ({rec['id']}) is an attached file that cannot be edited as text (PDF, Office, image, "
                      f"media or very large file). To change it: read_artifact it, then create_artifact with the full modified "
                      f"content under a new name (e.g. 'edited-{rec['name']}').")
    ext = os.path.splitext(rec["name"])[1].lower()
    if rec["kind"] not in ("text", "code") and not (rec["kind"] == "sheet" and ext in (".csv", ".tsv")):
        return None, f"Error: {rec['name']} is not a text artifact and cannot be edited."
    return rec, None


# ── live preview in the Render Surface ──
_PREVIEW_CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1f2328;--muted:#656d76;--border:#d0d7de;--code-bg:#f6f8fa}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8b949e;--border:#30363d;--code-bg:#161b22}}
html,body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:24px 28px}
.center{text-align:center}
pre{background:var(--code-bg);border:1px solid var(--border);border-radius:8px;padding:14px 16px;overflow:auto;
  font:13px/1.5 ui-monospace,Consolas,monospace;white-space:pre}
code{font-family:ui-monospace,Consolas,monospace}
table{border-collapse:collapse;margin:1em 0}th,td{border:1px solid var(--border);padding:6px 12px}
blockquote{margin:0;padding:0 1em;color:var(--muted);border-left:4px solid var(--border)}
img,svg{max-width:100%}
.err{color:#d1242f;font:13px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap;padding:16px}
"""

_MD_TPL = """<div class="wrap" id="out"></div>
<script>var SRC=__SRC__;
function raw(){var p=document.createElement('pre');p.textContent=SRC;document.getElementById('out').replaceChildren(p);}
function draw(){try{if(window.marked&&window.DOMPurify){document.getElementById('out').innerHTML=DOMPurify.sanitize(marked.parse(SRC));}else{raw();}}catch(e){raw();}}
</script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
<script>draw();</script>"""

_MERMAID_TPL = """<div class="wrap center"><div id="g"></div></div>
<script>var SRC=__SRC__;
function fail(e){var d=document.getElementById('g');d.innerHTML='';var m=document.createElement('div');m.className='err';m.textContent='Diagram error: '+(e&&e.message||e);var p=document.createElement('pre');p.textContent=SRC;d.appendChild(m);d.appendChild(p);}
</script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>
<script>(function(){if(!window.mermaid){fail('Mermaid could not be loaded (offline?)');return;}
var dark=window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches;
try{mermaid.initialize({startOnLoad:false,securityLevel:'strict',theme:dark?'dark':'default'});
mermaid.render('mg',SRC).then(function(r){document.getElementById('g').innerHTML=r.svg;}).catch(fail);}catch(e){fail(e);}})();</script>"""

_REACT_TPL = r"""<div id="root"></div>
<script>var SRC=__SRC__, TSX=__TSX__;
function fail(e){var r=document.getElementById('root');r.innerHTML='';var d=document.createElement('div');d.className='err';d.textContent=String(e&&e.stack||e&&e.message||e);r.appendChild(d);}
window.addEventListener('error',function(ev){fail(ev.error||ev.message);});
</script>
<script src="https://cdn.jsdelivr.net/npm/react@18.2.0/umd/react.production.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/react-dom@18.2.0/umd/react-dom.production.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@babel/standalone@7.24.7/babel.min.js"></script>
<script src="https://cdn.tailwindcss.com/3.4.17"></script>
<script>(function(){
if(!window.React||!window.ReactDOM||!window.Babel){fail('React/Babel could not be loaded (offline?)');return;}
try{
var src=SRC,def=null;
src=src.replace(/^[ \t]*import\s+[^;'"]*?from\s+['"]react['"];?[ \t]*$/gm,function(m){var n=m.match(/\{([^}]*)\}/);return n?'const {'+n[1].replace(/\s+as\s+/g,': ')+'} = React;':'';});
src=src.replace(/^[ \t]*import\s+[^;'"]*?from\s+['"][^'"]+['"];?[ \t]*$/gm,'');
src=src.replace(/^[ \t]*import\s+['"][^'"]+['"];?[ \t]*$/gm,'');
src=src.replace(/export\s+default\s+(async\s+)?function\s*([A-Za-z_$][\w$]*)?/,function(m,a,n){def=n||'__Default';return (a||'')+'function '+def;});
if(!def){src=src.replace(/export\s+default\s+class\s+([A-Za-z_$][\w$]*)/,function(m,n){def=n;return 'class '+n;});}
if(!def){src=src.replace(/export\s+default\s+/,function(){def='__Default';return 'var __Default = ';});}
src=src.replace(/^[ \t]*export\s+(?=(const|let|var|function|class|async)\b)/gm,'');
if(!def){var re=/(?:function|class|const|let|var)\s+([A-Z][A-Za-z0-9_$]*)\b/g,mm,last=null;while((mm=re.exec(src))){if(mm[1]==='App'){last='App';break;}last=mm[1];}def=last;}
if(!def)throw new Error('No component found - give the artifact a default export, e.g. export default function App() {...}');
var presets=TSX?[['typescript',{isTSX:true,allExtensions:true}],'react']:['react'];
var out=Babel.transform(src,{presets:presets,filename:TSX?'a.tsx':'a.jsx'}).code;
var Comp=new Function('React','ReactDOM',out+'\n;return '+def+';')(React,ReactDOM);
class EB extends React.Component{constructor(p){super(p);this.state={e:null};}static getDerivedStateFromError(e){return{e:e};}render(){return this.state.e?React.createElement('div',{className:'err'},String(this.state.e&&this.state.e.stack||this.state.e)):this.props.children;}}
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(EB,null,React.createElement(Comp)));
}catch(e){fail(e);}
})();</script>"""


def _js_str(s):
    """JSON string literal that is safe to embed inside an inline <script>."""
    return (json.dumps(s, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _page(title, body):
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{title}</title><style>{_PREVIEW_CSS}</style></head><body>{body}</body></html>")


def _preview_html(rec, text):
    """Build the HTML document pushed into the Render Surface for ``rec``.

    Only model-authored html / react run as live code (same trust level as write_render_code). A
    user-attached .html/.jsx is shown as inert source, and markdown/svg/mermaid are rendered through
    sanitising paths (DOMPurify, <img>, mermaid strict), so no attachment can reach midum.call().
    """
    t = art_type(rec)
    generated = rec.get("origin") == "generated"
    if t in ("html", "react") and not generated:
        t = "code"
    title = _html.escape(rec.get("title") or rec["name"])
    if t == "html":
        if re.search(r"<\s*(html|!doctype)", text[:2000], re.I):
            return text
        return _page(title, text)
    if t == "svg":
        b64 = base64.b64encode(text.encode("utf-8")).decode()
        return _page(title, f'<div class="wrap center"><img alt="{title}" src="data:image/svg+xml;base64,{b64}"></div>')
    if t == "markdown":
        return _page(title, _MD_TPL.replace("__SRC__", _js_str(text)))
    if t == "mermaid":
        return _page(title, _MERMAID_TPL.replace("__SRC__", _js_str(text)))
    if t == "react":
        tsx = "true" if rec["name"].lower().endswith(".tsx") else "false"
        return _page(title, _REACT_TPL.replace("__TSX__", tsx).replace("__SRC__", _js_str(text)))
    return _page(title, f'<div class="wrap"><pre>{_html.escape(text)}</pre></div>')


def _image_page(rec):
    b64, mime = _image_b64(rec)
    title = _html.escape(rec["name"])
    return _page(title, f'<div class="wrap center"><img alt="{title}" src="data:{mime};base64,{b64}"></div>')


def _previewable(rec):
    ext = os.path.splitext(rec["name"])[1].lower()
    return rec["kind"] in ("image", "text", "code") or ext in (".csv", ".tsv")


def _show(rec, version=None):
    """Push ``rec`` (optionally an older ``version``) into the Render Surface. Returns a short status string."""
    try:
        from tools import render_surface_tools as _rst
    except Exception as e:
        return f"(Not displayed: render surface unavailable: {e})"
    hook = getattr(_rst, "_gui_render_hook", None)
    if hook is None:
        return "(Not displayed: no GUI attached. The artifact is saved.)"
    if art_type(rec) == "latex":            # MidumDocs has its own pane
        import midumdocs
        return midumdocs.show(rec, version)
    target = version or rec.get("version")
    now = time.time()
    # Re-pushing identical content reloads the iframe (visible flash). If the same artifact/version/content
    # was pushed a moment ago (e.g. the model fired several identical open calls), do nothing.
    if (_shown["id"] == rec["id"] and _shown["version"] == target and _shown["sha"] == rec.get("sha256")
            and now - _shown["ts"] < 2.0):
        return "Already showing this artifact (unchanged)."
    try:
        if rec["kind"] == "image":
            page = _image_page(rec)
        else:
            page = _preview_html(rec, _read_raw(rec, version))
        label = rec.get("title") or rec["name"]
        cur = rec.get("version")
        if version and cur and int(version) != int(cur):
            label += f" (v{int(version)}, older)"
        out = hook("write", {"code": page, "title": label})
    except Exception as e:
        return f"(Could not display in the Render Surface: {e})"
    if isinstance(out, str) and out.lstrip().startswith("[RENDER SURFACE ERROR]"):
        return f"(Could not display in the Render Surface: {out})"
    _shown["id"] = rec["id"]
    _shown["version"] = target
    _shown["sha"] = rec.get("sha256")
    _shown["ts"] = now
    return "Shown live in the Render Surface."


def _refresh_preview(rec, arguments):
    """After a change: show/refresh the artifact (unless show=false). Renderable types use the Render Surface;
    code/text open in the Editor tab only when show=true is requested."""
    on_screen = _shown["id"] == rec["id"]
    want = _truthy((arguments or {}).get("show"), default=(art_type(rec) in RENDERABLE_TYPES or on_screen))
    if not want:
        return ""
    if art_type(rec) in RENDERABLE_TYPES or on_screen or not _textual(rec):
        return _show(rec)
    return _open_in_editor(rec)


def _textual(rec):
    ext = os.path.splitext(rec["name"])[1].lower()
    return rec.get("kind") in ("text", "code") or ext in (".csv", ".tsv")


def _open_in_editor(rec):
    """Open ``rec`` in the GUI's Editor tab (Monaco). Returns a short status string."""
    hook = _gui_editor_hook
    if hook is None:
        return "(Not opened: no GUI attached. The artifact is saved.)"
    try:
        hook("open_artifact", {"id": rec["id"]})
    except Exception as e:
        return f"(Could not open in the Editor tab: {e})"
    return "Opened in the Editor tab (the user can edit it there; their saves create new versions)."


# ── Editor-tab support (gui/editor_backend.py calls these) ──
def editor_list():
    """Text artifacts of the active chat for the Editor's explorer: model-created first, newest first."""
    out = []
    for r in list_records():
        if not _textual(r):
            continue
        gen = is_editable(r)
        out.append({"id": r["id"], "name": r["name"], "kind": r["kind"], "origin": r.get("origin", "upload"),
                    "art_type": art_type(r) if gen else "", "version": int(r.get("version") or 0) if gen else 0,
                    "size": r.get("size", 0), "editable": gen, "created": r.get("created", 0)})
    out.sort(key=lambda x: (not x["editable"], -x["created"]))
    return out


def editor_read(art_id):
    rec = _load().get(str(art_id))
    if not rec:
        return {"ok": False, "error": "Artifact not found (its chat may have been deleted)."}
    if not _textual(rec):
        return {"ok": False, "error": f"{rec['name']} is not a text file."}
    if rec.get("size", 0) > 5 * 1024 * 1024:
        return {"ok": False, "error": f"{rec['name']} is too large to open in the editor."}
    try:
        text = _read_raw(rec)
    except OSError as e:
        return {"ok": False, "error": f"Could not read {rec['name']}: {e}"}
    gen = is_editable(rec)
    return {"ok": True, "id": rec["id"], "name": rec["name"], "content": text, "editable": gen,
            "origin": rec.get("origin", "upload"), "version": int(rec.get("version") or 0) if gen else 0,
            "art_type": art_type(rec) if gen else "", "title": rec.get("title", "")}


def editor_save(art_id, content, expected_version=None):
    """Save text typed in the Editor tab as a new version of a model-created artifact."""
    rec = _load().get(str(art_id))
    if not rec:
        return {"ok": False, "error": "Artifact not found."}
    if not is_editable(rec):
        return {"ok": False, "error": "This attached file cannot be edited as text, so it is read-only. Use Save as copy."}
    cur = int(rec.get("version") or 0)
    if expected_version is not None and cur != int(expected_version):
        return {"ok": False, "conflict": True, "version": cur}
    text = str(content)
    try:
        if _read_raw(rec) == text:
            return {"ok": True, "version": cur, "unchanged": True}
    except OSError:
        pass
    n = _commit(rec, text.encode("utf-8"), "edited in the Editor tab")
    if _shown["id"] == rec["id"]:                      # keep a live preview in step with the edit
        _show(rec)
    return {"ok": True, "version": n}


def editor_save_copy(name, content):
    name = os.path.basename(str(name or "").strip())
    if not name:
        return {"ok": False, "error": "A file name is required."}
    if _find_generated(name):
        return {"ok": False, "error": f"An artifact named '{name}' already exists in this chat."}
    rec = add_file(data=str(content).encode("utf-8"), name=name, chat_id=_active_chat_id,
                   origin="generated", upload=False, sent=True)
    _init_generated(rec, _infer_type(rec["name"]), "")
    return {"ok": True, "id": rec["id"], "name": rec["name"]}


def editor_stat_ids(ids):
    idx = _load()
    out = {}
    for i in list(ids or [])[:100]:
        r = idx.get(str(i))
        out[i] = {"exists": bool(r),
                  "version": int(r.get("version") or 0) if r and is_editable(r) else 0,
                  "sha": (r or {}).get("sha256", "")}
    return out


# ── model-facing authoring tools ──
def create_tool(arguments):
    arguments = arguments or {}
    name = os.path.basename(str(arguments.get("name") or "").strip())
    content = arguments.get("content")
    if not name or content is None:
        return "Error: 'name' and 'content' are required."
    if not isinstance(content, str):
        try:
            content = json.dumps(content, indent=2, ensure_ascii=False) if isinstance(content, (dict, list)) else str(content)
        except Exception:
            content = str(content)
    atype = _norm_type(arguments.get("type"))
    if not os.path.splitext(name)[1]:
        name += _TYPE_EXT.get(atype or "text", ".txt")
    atype = atype or _infer_type(name)
    title = str(arguments.get("title") or "").strip()[:120]
    data = content.encode("utf-8")
    try:
        existing = _find_generated(name)
        if existing:
            _commit(existing, data, "re-created via create_artifact")
            existing["art_type"] = atype
            if title:
                existing["title"] = title
            _save()
            rec = existing
            head = (f"Rewrote existing artifact (an artifact named '{name}' already existed in this chat, so this was saved "
                    f"as a new version instead of a duplicate)")
        else:
            rec = add_file(data=data, name=name, chat_id=_active_chat_id, origin="generated", upload=False, sent=True)
            _init_generated(rec, atype, title)
            head = "Created artifact"
    except Exception as e:
        return f"Error: could not create artifact: {e}"
    show = _truthy(arguments.get("show"), default=(atype in RENDERABLE_TYPES))
    status = _show(rec) if show else ""
    return (f"{head} {rec['id']} ({rec['name']}, {atype}, {_fmt_size(rec['size'])}, v{rec['version']}). {status}\n"
            f"To change it later call update_artifact('{rec['id']}', old_str, new_str) or rewrite_artifact - do NOT create it again.").strip()


def _nomatch_hint(text, old):
    first = next((ln.strip() for ln in old.splitlines() if ln.strip()), "")
    if first and first in text:
        i = text.find(first)
        snippet = text[max(0, i - 60): i + len(first) + 240]
        return ("The first line of old_str DOES appear in the file, but the full block differs. Actual text around it:\n---\n"
                + snippet + "\n---")
    return "Use read_artifact / search_artifact to copy the exact text (whitespace and line breaks must match)."


def update_tool(arguments):
    arguments = arguments or {}
    rec, err = _resolve_editable(arguments.get("artifact"))
    if err:
        return err
    edits = arguments.get("edits")
    if isinstance(edits, str):
        try:
            edits = json.loads(edits)
        except Exception:
            return "Error: 'edits' must be a list of {old_str, new_str} objects."
    if not edits:
        if arguments.get("old_str") is None:
            return "Error: give old_str (+ new_str) for one edit, or edits=[{old_str, new_str}, ...] for several."
        edits = [{"old_str": arguments.get("old_str"), "new_str": arguments.get("new_str", "")}]
    if not isinstance(edits, list) or not all(isinstance(e, dict) for e in edits):
        return "Error: 'edits' must be a list of {old_str, new_str} objects."
    try:
        text = _read_raw(rec)
    except Exception as e:
        return f"Error: could not read {rec['name']}: {e}"
    replace_all = _truthy(arguments.get("replace_all"))
    new_text = text
    for i, e in enumerate(edits, 1):
        old, new = e.get("old_str"), e.get("new_str")
        new = "" if new is None else str(new)
        tag = f"edit {i}: " if len(edits) > 1 else ""
        if not isinstance(old, str) or old == "":
            return f"Error: {tag}old_str is required and must not be empty. Nothing was changed."
        n = new_text.count(old)
        if n == 0:
            return f"Error: {tag}old_str was not found in {rec['name']}. Nothing was changed. {_nomatch_hint(new_text, old)}"
        if n > 1 and not replace_all:
            return (f"Error: {tag}old_str matches {n} places in {rec['name']}. Nothing was changed. Include more surrounding "
                    f"lines to make it unique, or pass replace_all=true to change every occurrence.")
        new_text = new_text.replace(old, new) if replace_all else new_text.replace(old, new, 1)
    if new_text == text:
        return f"No change: the edits produce identical content for {rec['name']}."
    note = str(arguments.get("note") or "").strip() or f"{len(edits)} edit(s)"
    try:
        n = _commit(rec, new_text.encode("utf-8"), note)
    except Exception as e:
        return f"Error: could not save the update: {e}"
    status = _refresh_preview(rec, arguments)
    return f"Updated {rec['id']} ({rec['name']}) -> v{n}, {len(edits)} edit(s), {_fmt_size(rec['size'])}. {status}".strip()


def rewrite_tool(arguments):
    arguments = arguments or {}
    rec, err = _resolve_editable(arguments.get("artifact"))
    if err:
        return err
    content = arguments.get("content")
    if content is None:
        return "Error: 'content' is required (the COMPLETE new content)."
    content = content if isinstance(content, str) else str(content)
    note = str(arguments.get("note") or "").strip() or "rewritten"
    try:
        n = _commit(rec, content.encode("utf-8"), note)
        title = str(arguments.get("title") or "").strip()[:120]
        if title:
            rec["title"] = title
            _save()
    except Exception as e:
        return f"Error: could not save the rewrite: {e}"
    status = _refresh_preview(rec, arguments)
    return f"Rewrote {rec['id']} ({rec['name']}) -> v{n}, {_fmt_size(rec['size'])}. {status}".strip()


def open_tool(arguments):
    arguments = arguments or {}
    rec = get(arguments.get("artifact"))
    if not rec:
        return _not_found(arguments.get("artifact"))
    mode = str(arguments.get("mode") or "").strip().lower()
    if mode not in ("", "edit", "editor", "preview", "view"):
        return "Error: 'mode' must be 'edit' (code editor tab) or 'preview' (Render Surface)."
    version = None
    if arguments.get("version") not in (None, ""):
        try:
            version = int(arguments["version"])
        except (TypeError, ValueError):
            return "Error: 'version' must be a whole number (see list_artifact_versions)."
        if not is_editable(rec) or not os.path.exists(_vpath(rec, version)):
            return f"Error: {rec['name']} has no version {version}. Call list_artifact_versions."
    # Default: pages/diagrams/documents preview in the Render Surface; code & text open in the Editor tab.
    # Opening for editing (mode=edit) always uses the Editor tab. An old version can only be previewed.
    if version is None and _textual(rec):
        if mode in ("edit", "editor") or (mode == "" and art_type(rec) not in RENDERABLE_TYPES):
            return f"{rec['name']}: {_open_in_editor(rec)}"
    if not _previewable(rec):
        return (f"{rec['name']} ({rec['kind']}) cannot be shown in the Render Surface. "
                f"Use read_artifact for its text or analyze_artifact for a native look.")
    return f"{rec['name']}: {_show(rec, version)}"


def open_file_tool(arguments):
    """Open ANY file on disk (not just an artifact) in the GUI's Editor tab. Gives the model no file content."""
    arguments = arguments or {}
    raw = str(arguments.get("path") or "").strip().strip('"')
    if not raw:
        return "Error: 'path' is required."
    path = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isfile(path):
        return f"Error: no such file: {path}" + (" (that is a folder - give a file path)" if os.path.isdir(path) else "")
    hook = _gui_editor_hook
    if hook is None:
        return "(Not opened: no GUI attached.)"
    payload = {"path": path}
    try:
        line = int(arguments.get("line"))
        if line > 0:
            payload["line"] = line
    except (TypeError, ValueError):
        line = None
    try:
        hook("open_file", payload)
    except Exception as e:
        return f"(Could not open in the Editor tab: {e})"
    return f"Opened {path} in the Editor tab" + (f" at line {payload['line']}" if "line" in payload else "") + "."


def versions_tool(arguments):
    arguments = arguments or {}
    rec = get(arguments.get("artifact"))
    if not rec:
        return _not_found(arguments.get("artifact"))
    vs = rec.get("versions") or []
    if not is_editable(rec) or not vs:
        return f"{rec['name']} has no version history (only artifacts you created and editable attached text files are versioned)."
    lines = [f"{rec['name']} ({rec['id']}): {len(vs)} version(s), current = v{rec.get('version')}"]
    for v in vs:
        ts = datetime.fromtimestamp(v.get("ts", 0)).strftime("%Y-%m-%d %H:%M")
        cur = "   <- current" if v["n"] == rec.get("version") else ""
        lines.append(f"  v{v['n']:<3} {ts}  {_fmt_size(v.get('size', 0)):>9}  {v.get('note', '')}{cur}")
    lines.append("open_artifact(artifact, version=N) previews an old one; revert_artifact(artifact, N) makes it current again.")
    return "\n".join(lines)


def revert_tool(arguments):
    arguments = arguments or {}
    rec, err = _resolve_editable(arguments.get("artifact"))
    if err:
        return err
    try:
        target = int(arguments.get("version"))
    except (TypeError, ValueError):
        return "Error: 'version' (a whole number) is required - see list_artifact_versions."
    if target == rec.get("version"):
        return f"{rec['name']} is already at v{target}."
    if not os.path.exists(_vpath(rec, target)):
        return f"Error: {rec['name']} has no stored v{target}. Call list_artifact_versions."
    try:
        n = _commit(rec, _read_raw(rec, target).encode("utf-8"), f"revert to v{target}")
    except Exception as e:
        return f"Error: could not revert: {e}"
    status = _refresh_preview(rec, arguments)
    return f"Reverted {rec['id']} ({rec['name']}) to the content of v{target}; saved as v{n} (history is kept). {status}".strip()


def add_path_tool(arguments):
    """Add ANY existing file on disk as an artifact, by absolute path.

    The file is copied into the artifact store (the original is never moved or modified) and goes
    through the normal ingest pipeline: text extraction, Gemini Files API upload (so
    ``analyze_artifact`` works natively), and per-chat de-duplication by content hash.
    """
    arguments = arguments or {}
    raw = str(arguments.get("path") or "").strip().strip('"').strip("'").strip()
    if not raw:
        return "Error: 'path' is required - the absolute path of the file to add."
    path = os.path.expanduser(os.path.expandvars(raw))
    if not os.path.isabs(path):
        return (f"Error: '{raw}' is not an absolute path. Pass the full path (e.g. C:\\Users\\me\\report.pdf "
                f"or /home/me/report.pdf). Use find_file or list_directory to locate it first.")
    path = os.path.normpath(path)
    if os.path.isdir(path):
        return f"Error: '{path}' is a folder. Pass the path of a single file (list_directory shows what is inside)."
    if not os.path.isfile(path):
        return f"Error: no file exists at '{path}'."
    name = str(arguments.get("name") or "").strip() or None
    started = time.time()
    try:
        rec = add_file(src_path=path, name=name, chat_id=_active_chat_id, origin="upload", sent=True)
    except Exception as e:
        return f"Error: could not add '{path}' as an artifact: {e}"
    if rec.get("created", 0) < started:
        return (f"That exact file is already an artifact in this chat, nothing new was added: {_describe(rec)}\n"
                f"Read it with read_artifact('{rec['id']}').")
    nxt = {"image": "view_artifact", "audio": "analyze_artifact", "video": "analyze_artifact"}.get(
        rec["kind"], "read_artifact")
    return (f"Added artifact {_describe(rec)}\n"
            f"Source: {path} (copied - the original was not moved or changed).\n"
            f"Next: {nxt}('{rec['id']}') to look at it; it now appears under AVAILABLE FILES for this chat.")


def dispatch(func_name, arguments):
    """Returns (output_text, images_or_None)."""
    try:
        if func_name == "list_artifacts":
            return list_tool(arguments), None
        if func_name == "read_artifact":
            return read_tool(arguments), None
        if func_name == "search_artifact":
            return search_tool(arguments), None
        if func_name == "view_artifact":
            return view_tool(arguments)
        if func_name == "analyze_artifact":
            return analyze_tool(arguments), None
        if func_name == "create_artifact":
            return create_tool(arguments), None
        if func_name == "add_artifact_from_path":
            return add_path_tool(arguments), None
        if func_name == "update_artifact":
            return update_tool(arguments), None
        if func_name == "rewrite_artifact":
            return rewrite_tool(arguments), None
        if func_name == "open_artifact":
            return open_tool(arguments), None
        if func_name == "list_artifact_versions":
            return versions_tool(arguments), None
        if func_name == "revert_artifact":
            return revert_tool(arguments), None
        if func_name == "open_in_editor":
            return open_file_tool(arguments), None
    except Exception as e:
        return f"Error in {func_name}: {e}", None
    return f"Error: unknown artifact tool '{func_name}'", None


# ── prompt integration ─────────────────────────────────────────────────
_ATTACH_OPEN = "[ATTACHED FILES"
_ATTACH_RE = re.compile(r"\[ATTACHED FILES.*?\[/ATTACHED FILES\]", re.S)


def attachment_block(recs, user_text_empty=False):
    lines = ["[ATTACHED FILES - stored as artifacts; read them any time with the artifact tools]"]
    for r in recs:
        lines.append("- " + _describe(r))
    if any(r["kind"] == "image" for r in recs):
        lines.append("(Image attachments for this message are included inline when the model supports it; "
                     "otherwise call view_artifact.)")
    lines.append("[/ATTACHED FILES]")
    if user_text_empty:
        lines.append("The user sent the files without a message. Briefly look at them (read_artifact / view_artifact) "
                     "and summarize what they are, then ask what they want done.")
    return "\n".join(lines)


def extract_ids(text):
    m = _ATTACH_RE.search(text or "")
    return re.findall(r"\b(art_[0-9a-f]{8})\b", m.group(0)) if m else []


def inline_eligible_indices(messages):
    """Indices of the most recent attachment-bearing user messages whose images should be inlined."""
    found = []
    for i, m in enumerate(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            ids = extract_ids(m["content"])
            if ids and any((_load().get(a) or {}).get("kind") == "image" for a in ids):
                found.append(i)
    return set(found[-INLINE_MESSAGES:])


def build_user_parts(content):
    """OpenAI-style content parts (text + image_url) for an attachment-bearing user message, or None."""
    parts = [{"type": "text", "text": content}]
    total, count = 0, 0
    for a in extract_ids(content):
        rec = _load().get(a)
        if not rec or rec["kind"] != "image" or rec["size"] > INLINE_IMAGE_MAX_BYTES:
            continue
        if count >= INLINE_IMAGE_MAX_COUNT or total + rec["size"] > 4 * INLINE_IMAGE_MAX_BYTES:
            break
        try:
            b64, mime = _image_b64(rec)
        except Exception:
            continue
        parts.append({"type": "text", "text": f"[Image: {rec['name']} ({rec['id']})]"})
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        count += 1; total += rec["size"]
    return parts if count else None


def prompt_block(chat_id=None):
    recs = list_records(chat_id=chat_id)
    others = len(list_records(scope="all")) - len(recs)
    if not recs and not others:
        return ""
    lines = ["━━━ AVAILABLE FILES (artifacts) ━━━",
             "These files are stored locally and can be read at ANY time with read_artifact / search_artifact / "
             "view_artifact / analyze_artifact - the user does not need to re-attach them. Prefer reading them over guessing."]
    if any(r.get("origin") == "generated" for r in recs):
        lines.append("Files marked 'created by you' are yours to maintain: change them with update_artifact / "
                     "rewrite_artifact (each change is a new version; open_artifact re-shows one) - never re-create "
                     "them or paste their full content back into chat.")
    if any(r.get("origin") != "generated" and r.get("editable") for r in recs):
        lines.append("Attached text/code files marked 'attached, editable copy' can be edited in place with update_artifact / "
                     "rewrite_artifact -- do NOT create a separate copy. Edits change Midum's stored copy only; the original file "
                     "on the user's computer is never modified, so tell the user the original is unchanged if it matters.")
    shown = recs[-PROMPT_LIST_MAX:]
    if len(recs) > len(shown):
        lines.append(f"({len(recs) - len(shown)} older files not shown - call list_artifacts)")
    for r in shown:
        lines.append("  " + _describe(r))
    if not recs:
        lines.append("  (no files in this chat yet)")
    if others > 0:
        lines.append(f'({others} more in other chats - list_artifacts(scope="all"))')
    return "\n".join(lines)


def inject_into_system(messages, block):
    """Return a copy of ``messages`` with ``block`` appended to the first system message (never persisted)."""
    if not block:
        return messages
    out = list(messages)
    for i, m in enumerate(out):
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            m = dict(m); m["content"] = m["content"] + "\n\n" + block
            out[i] = m
            return out
    return [{"role": "system", "content": block}] + out


def make_thumbnail(art_id, size=160):
    rec = _load().get(art_id)
    if not rec or rec["kind"] != "image":
        return ""
    cache = os.path.join(FILES_DIR, art_id, ".thumb.jpg")
    try:
        if not os.path.exists(cache):
            from PIL import Image
            with Image.open(_blob_path(rec)) as im:
                im = im.convert("RGB"); im.thumbnail((size * 2, size * 2)); im.save(cache, "JPEG", quality=80)
        with open(cache, "rb") as fh:
            return "data:image/jpeg;base64," + base64.b64encode(fh.read()).decode()
    except Exception:
        return ""


# ── tool schemas ───────────────────────────────────────────────────────
def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": list(required)}}}


_REF = {"type": "string", "description": "Artifact id (art_xxxxxxxx) or file name, as listed under AVAILABLE FILES."}

TOOL_SCHEMAS = [
    _fn("list_artifacts", "List files (artifacts) the user attached or you created. Defaults to this chat; use scope='all' for every chat.",
        {"scope": {"type": "string", "enum": ["chat", "all"], "description": "chat (default) or all."}}),
    _fn("read_artifact", "Read the TEXT of an attached file (PDF, Word, slides, spreadsheet, text, code...). Paged: for long files "
        "continue with offset; for PDFs/slides pass pages like '3' or '2-5'. Use this first for any attached document.",
        {"artifact": _REF,
         "offset": {"type": "integer", "description": "Character offset to start from (default 0)."},
         "length": {"type": "integer", "description": f"Max characters to return (default {READ_DEFAULT_CHARS}, max {READ_MAX_CHARS})."},
         "pages": {"type": "string", "description": "Page/slide selection for paged files, e.g. '3', '2-5', '1,4,7'."}},
        ["artifact"]),
    _fn("search_artifact", "Case-insensitive text search inside an attached file; returns matches with page numbers and context. "
        "Use on large files instead of reading everything.",
        {"artifact": _REF, "query": {"type": "string"},
         "max_results": {"type": "integer", "description": "Default 10, max 40."}}, ["artifact", "query"]),
    _fn("view_artifact", "Look at an attached IMAGE yourself (the image is returned to you). For non-images use read_artifact or analyze_artifact.",
        {"artifact": _REF}, ["artifact"]),
    _fn("analyze_artifact", "Have Gemini examine an attached file NATIVELY via the Gemini Files API and answer a question about it. "
        "Best for PDF layout/figures/scans, audio, video, and detailed image questions. Slower than read_artifact; needs the internet.",
        {"artifact": _REF, "question": {"type": "string", "description": "What to find out about the file."},
         "model": {"type": "string", "description": "Optional Gemini model override."}}, ["artifact"]),
    _fn("create_artifact", "Create a NEW persistent artifact that YOU author: a web page / app / game / dashboard (html), a document or report "
        "(markdown), a vector graphic (svg), a diagram (mermaid), a React component (react), or a code/text file. Pass the COMPLETE content. "
        "html, markdown, svg, mermaid and react artifacts open LIVE in the Render Surface panel. Use this for substantial standalone content "
        "the user will keep, reuse or iterate on - not for short answers or small snippets (put those in chat). If an artifact with the same "
        "name already exists in this chat it is rewritten as a new version instead of duplicated. After creating, change it with "
        "update_artifact / rewrite_artifact - never create it again. html: one complete self-contained document (inline CSS/JS, CDN scripts "
        "ok); it can call midum.call(...) and parent.postMessage(...) like any Render Surface page. react: one component with a default "
        "export, React hooks and Tailwind classes available, NO other imports (for libraries use an html artifact with CDN scripts).",
        {"name": {"type": "string", "description": "File name, e.g. 'dashboard.html', 'report.md', 'logo.svg', 'flow.mmd', 'App.jsx', 'tool.py'. If it has no extension one is added from `type`."},
         "content": {"type": "string", "description": "The COMPLETE file content."},
         "type": {"type": "string", "enum": ["html", "markdown", "svg", "mermaid", "react", "latex", "code", "text"],
                  "description": "Optional; inferred from the file extension when omitted."},
         "title": {"type": "string", "description": "Optional human-readable title shown in the panel header."},
         "show": {"type": "boolean", "description": "Open it in the Render Surface now. Default: true for html/markdown/svg/mermaid/react, false for code/text."}},
        ["name", "content"]),
    _fn("update_artifact", "Make a TARGETED EDIT to an artifact you created: replace the exact text old_str with new_str. old_str must match "
        "EXACTLY ONCE (including whitespace and line breaks) - add a neighbouring line to make it unique, or set replace_all=true to change "
        "every occurrence. To make several changes in one go send edits=[{old_str,new_str},...] (all-or-nothing). Prefer this over "
        "rewrite_artifact whenever you are changing part of a file. If you are not sure of the exact current text, read_artifact it first. "
        "Each successful call saves a new version and refreshes the live preview. Attached text/code/csv files can be edited too (marked 'editable copy'): the edit changes Midum's stored copy, never the original file on the user's computer. Attached PDF/Office/image/media files cannot be edited.",
        {"artifact": _REF,
         "old_str": {"type": "string", "description": "Exact existing text to replace (must be unique unless replace_all)."},
         "new_str": {"type": "string", "description": "Replacement text (empty string deletes old_str)."},
         "edits": {"type": "array", "description": "Several edits applied in order, all-or-nothing. Use instead of old_str/new_str.",
                   "items": {"type": "object", "properties": {"old_str": {"type": "string"}, "new_str": {"type": "string"}},
                             "required": ["old_str", "new_str"]}},
         "replace_all": {"type": "boolean", "description": "Replace every occurrence of old_str instead of requiring exactly one."},
         "note": {"type": "string", "description": "Optional short description for the version history."},
         "show": {"type": "boolean", "description": "Refresh the Render Surface preview. Default true for renderable types."}},
        ["artifact"]),
    _fn("rewrite_artifact", "Replace the ENTIRE content of an artifact you created with new content (saved as a new version). Use this when "
        "most of the file changes; for a smaller change use update_artifact. Pass the COMPLETE new content, not a diff.",
        {"artifact": _REF, "content": {"type": "string", "description": "The COMPLETE new file content."},
         "title": {"type": "string", "description": "Optional new title."},
         "note": {"type": "string", "description": "Optional short description for the version history."},
         "show": {"type": "boolean", "description": "Refresh the Render Surface preview. Default true for renderable types."}},
        ["artifact", "content"]),
    _fn("open_artifact", "Open an artifact for the user. Pages, diagrams, documents and graphics (html/markdown/svg/mermaid/react) open as a "
        "live preview in the Render Surface; code and text files open in the Editor tab (a full code editor with tabs and an explorer). "
        "Use mode='edit' when the user wants to edit or see the SOURCE of any artifact, mode='preview' to force the Render Surface preview. "
        "Also use it to bring an artifact back after the panel was closed, or (with version) to preview an OLDER version of one you created. "
        "Calling it again for something already showing does nothing, so call it once.",
        {"artifact": _REF,
         "mode": {"type": "string", "enum": ["edit", "preview"],
                  "description": "edit = open in the Editor tab; preview = open in the Render Surface. Default depends on the type."},
         "version": {"type": "integer", "description": "Optional version number to preview (see list_artifact_versions)."}},
        ["artifact"]),
    # open_in_editor: superseded by open_file_in_editor (editor_link.py). Its schema is no longer offered to the model, but the
    # dispatch branch above stays so older chats / prompts that still call it keep working.
    _fn("list_artifact_versions", "List the saved versions (number, time, size, note) of an artifact you created.",
        {"artifact": _REF}, ["artifact"]),
    _fn("revert_artifact", "Restore an artifact you created to the content of an earlier version. Non-destructive: it is saved as a NEW version, "
        "so nothing is lost. Use after an edit went wrong or the user asks to undo.",
        {"artifact": _REF, "version": {"type": "integer", "description": "Version number to restore (see list_artifact_versions)."},
         "show": {"type": "boolean", "description": "Refresh the Render Surface preview. Default true for renderable types."}},
        ["artifact", "version"]),
    _fn("add_artifact_from_path", "Add ANY existing file on the user's computer as an artifact, given its ABSOLUTE file path "
        "(PDF, Word, slides, sheet, code, image, audio, video, text, anything). The file is copied (the original is left untouched), "
        "indexed, and listed under AVAILABLE FILES so you can then use read_artifact / search_artifact / view_artifact / analyze_artifact on it. "
        "Use this when the user points to a file by path or name that was not attached in the prompt box. "
        "Locate unknown files first with find_file / list_directory. Folders are not supported - pass one file per call.",
        {"path": {"type": "string", "description": "Absolute path of the file, e.g. C:\\Users\\me\\Documents\\report.pdf or /home/me/report.pdf."},
         "name": {"type": "string", "description": "Optional display name to store it under (defaults to the file's own name)."}},
        ["path"]),
]


def register_tools(tools_list):
    """Idempotently append the artifact tool schemas to the shared ``tools`` list."""
    have = {t.get("function", {}).get("name") for t in tools_list if isinstance(t, dict)}
    for t in TOOL_SCHEMAS:
        if t["function"]["name"] not in have:
            tools_list.append(t)
    return tools_list
