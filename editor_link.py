"""Chat <-> Editor link.

Gives the active model chat eyes and hands in the Editor tab (Monaco):

  open_file_in_editor      open a file (optionally at a line) in the Editor and make it the active tab
  open_folder_in_editor    make a folder the Editor's workspace (what the Explorer shows)
  editor_workspace_tree    see the workspace folder tree (open / unsaved tabs are marked)
  editor_read_active_file  see the ACTIVE tab's live text (incl. unsaved edits), cursor, selection, problems
  editor_edit_active_file  change the active file in place (undoable by the user, unsaved text is never lost)
  edit_file_in_editor      open ANY workspace file as the active tab, then edit it (one call)

Every edit goes through the user's approval (a diff dialog in the Editor) unless they switched that off; and
:func:`guard_external_write` blocks the generic file-writing tools on files that are open in the Editor.

How it talks to the page
------------------------
The Editor is a persistent iframe (see docs/editor.md). A tool call pushes an ``editor_ai_request`` event
({id, op, args}) down the normal Python -> page pipe (``artifacts._gui_editor_hook("ai_request", ...)``); the
page does the work against the live Monaco model and answers through the ``editor_ai_reply`` Api method, which
lands in :func:`deliver` and wakes the waiting tool thread. Ops the page implements: state, snapshot, edit,
open_file, open_folder.

Edits are resolved HERE (plain Python, unit-testable) into Monaco range edits against the snapshot the model
just read, and the page refuses them (``stale``) if the buffer changed in between. If the Editor page is not
running at all (never opened this session, headless), reads fall back to the file on disk and edits are written
to disk atomically (same encoding / BOM / line endings as the Editor would keep).
"""
import bisect
import json
import os
import re
import threading
import time
import uuid

TOOL_NAMES = ("open_file_in_editor", "open_folder_in_editor", "editor_workspace_tree",
              "editor_read_active_file", "editor_edit_active_file", "edit_file_in_editor")

REQUEST_TIMEOUT = 10.0           # seconds to wait for the page once it has connected
FIRST_CONTACT_TIMEOUT = 12.0     # ... before it has ever connected (Monaco may still be booting)
READ_MAX_CHARS = 60000
MAX_EDITS = 200
TREE_DEFAULT_DEPTH, TREE_MAX_DEPTH = 3, 8
TREE_DEFAULT_ENTRIES, TREE_MAX_ENTRIES = 400, 2000
TREE_PER_DIR = 80
TABS_SHOWN = 25
PROBLEMS_SHOWN = 15
APPROVAL_TIMEOUT = 300.0         # seconds the user has to answer an edit-approval dialog
APPROVAL_SLACK = 15.0            # extra seconds we wait beyond the dialog's own timeout (the page answers "timed out" itself)
DIFF_MAX_LINES = 240             # diff lines shown in the approval dialog

_lock = threading.Lock()
_pending = {}                    # request id -> {"ev": Event, "result": dict | None}
_seen = {"hello": 0.0, "reply": 0.0}


# ── plumbing ─────────────────────────────────────────────────────────────────────────────
def _eb():
    try:
        from gui import editor_backend as eb
    except ImportError:
        import editor_backend as eb
    return eb


def _hook():
    try:
        import artifacts
        return artifacts._gui_editor_hook
    except Exception:
        return None


def hello():
    """The Editor page announces that it has booted (Api.editor_ai_hello)."""
    _seen["hello"] = time.time()


def deliver(req_id, result):
    """The page answers a request (Api.editor_ai_reply). Returns False if nobody is waiting any more."""
    with _lock:
        slot = _pending.get(str(req_id))
    if slot is None:
        return False
    slot["result"] = result if isinstance(result, dict) else {"ok": False, "error": "malformed reply from the Editor"}
    _seen["reply"] = time.time()
    slot["ev"].set()
    return True


def _request(op, args=None, timeout=None):
    """Ask the live Editor page to do ``op``. -> (result_dict, None) or (None, "no GUI attached" | "timeout" | msg)."""
    hook = _hook()
    if hook is None:
        return None, "no GUI attached"
    rid = uuid.uuid4().hex[:12]
    slot = {"ev": threading.Event(), "result": None}
    with _lock:
        _pending[rid] = slot
    try:
        hook("ai_request", {"id": rid, "op": op, "args": args or {}})
        wait = timeout or (REQUEST_TIMEOUT if _seen["hello"] else FIRST_CONTACT_TIMEOUT)
        if not slot["ev"].wait(wait):
            return None, "timeout"
        return slot["result"], None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    finally:
        with _lock:
            _pending.pop(rid, None)


# ── small helpers ────────────────────────────────────────────────────────────────────────
def _s(v):
    return "" if v is None else str(v)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _truthy(v, default=False):
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(v)


def _lf(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _u16(s):
    """Length of ``s`` in UTF-16 code units (Monaco columns count these, Python counts code points)."""
    return len(s) if s.isascii() else len(s.encode("utf-16-le")) // 2


def _persisted():
    try:
        eb = _eb()
        with eb._state_lock:
            return eb._load_state()
    except Exception:
        return {}


def _default_workspace():
    try:
        return _eb()._default_workspace()
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


def _abs(path, base):
    p = os.path.expanduser(_s(path).strip().strip('"'))
    if not os.path.isabs(p):
        p = os.path.join(base or _default_workspace(), p)
    return os.path.normpath(os.path.abspath(p))


def _nk(p):
    return os.path.normcase(os.path.normpath(_s(p)))


def _approval_needed():
    """Edits need the user's approval unless they turned it off (Editor setting ``aiApprove``, F1 > Midum: Toggle
    Approval for AI Edits, or 'Approve & don't ask again'). Only the user can change it - there is no tool for it."""
    return _persisted().get("aiApprove", True) is not False


def _diff_lines(a, b, ctx=2):
    import difflib
    lines = list(difflib.unified_diff(a, b, lineterm="", n=ctx))[2:]        # drop the ---/+++ header
    out = [ln[:400] for ln in lines]
    if len(out) > DIFF_MAX_LINES:
        extra = len(out) - DIFF_MAX_LINES
        out = out[:DIFF_MAX_LINES] + [f"@@ ... {extra} more diff lines not shown @@"]
    return out


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def _find_in_workspace(raw, ws):
    """What the model typed -> a real file: the exact path, else a unique match in the workspace tree by name or
    partial path (``app.py``, ``gui/app.py``). -> (path, None) or (None, error)."""
    p = _abs(raw, ws)
    if os.path.isfile(p):
        return p, None
    if os.path.isdir(p):
        return None, f"{p} is a folder (open_folder_in_editor opens it; editor_workspace_tree lists it)"
    rawq = _s(raw).strip().strip('"')
    if not rawq or os.path.isabs(os.path.expanduser(rawq)):
        return None, f"no such file: {p}"
    q = rawq.replace("\\", "/").strip("/").lower()
    try:
        eb = _eb()
        files = eb._index_files(os.path.normpath(ws))
    except Exception:
        return None, f"no such file: {p}"
    hits = [f for f in files if f.lower() == q or f.lower().endswith("/" + q)]
    if len(hits) == 1:
        return os.path.normpath(os.path.join(ws, *hits[0].split("/"))), None
    if len(hits) > 1:
        return None, (f"'{raw}' matches {len(hits)} files in the workspace: " + ", ".join(hits[:8])
                      + (" ..." if len(hits) > 8 else "") + ". Pass a longer path.")
    scored = [(eb._fuzzy(q, f), f) for f in files]
    near = [f for sc, f in sorted((x for x in scored if x[0] is not None), key=lambda x: -x[0])[:5]]
    return None, (f"no file '{raw}' in the workspace ({ws})."
                  + (" Did you mean: " + ", ".join(near) + "?" if near else " Use editor_workspace_tree to find it."))


# ── editor state / snapshot (live page, else disk) ───────────────────────────────────────
def _disk_state():
    st = _persisted()
    ws = st.get("workspace")
    if not ws or not os.path.isdir(ws):
        ws = _default_workspace()
    active = st.get("active")
    tabs = []
    for t in (st.get("tabs") or []):
        if isinstance(t, dict):
            tabs.append({"key": t.get("key"), "kind": t.get("kind"), "path": t.get("path"),
                         "name": t.get("name") or os.path.basename(t.get("path") or "") or "?",
                         "dirty": False, "active": t.get("key") == active})
    return {"ok": True, "live": False, "workspace": ws, "active": active, "tabs": tabs, "file": None}


def get_state():
    """Workspace + tabs (no file content). -> dict with "live" telling where it came from."""
    res, err = _request("state")
    if res is not None:
        if res.get("ok"):
            res["live"] = True
            return res
        return {"ok": False, "error": res.get("error") or "the Editor reported an error"}
    if err == "timeout" and _seen["hello"]:
        return {"ok": False, "error": "The Editor tab did not respond (is the app frozen or the Editor reloading?). Try again."}
    return _disk_state()


def _disk_file(path):
    """File record for ``path`` read straight from disk (same shape the page returns)."""
    eb = _eb()
    st = os.stat(path)
    if st.st_size > eb.MAX_EDIT_BYTES:
        return {"noText": True, "reason": "too large to open in the editor", "name": os.path.basename(path), "path": path}
    with open(path, "rb") as fh:
        raw = fh.read()
    if eb._is_binary(raw):
        return {"noText": True, "reason": "binary file", "name": os.path.basename(path), "path": path}
    text, enc, bom = eb._decode_bytes(raw)
    content = _lf(text)
    return {"key": "file:" + path, "kind": "file", "name": os.path.basename(path), "path": path, "lang": "",
            "dirty": False, "readOnly": not os.access(path, os.W_OK), "version": None,
            "eol": "CRLF" if "\r\n" in text else "LF", "lineCount": content.count("\n") + 1, "content": content,
            "disk": {"enc": enc, "bom": bom, "mtime": st.st_mtime}}


def snapshot(path=None):
    """State + the active tab's (or the open tab ``path``'s) full text. -> dict; "file" may be None."""
    res, err = _request("snapshot", {"path": path} if path else {})
    if res is not None:
        if res.get("ok"):
            res["live"] = True
            return res
        return {"ok": False, "error": res.get("error") or "the Editor reported an error"}
    if err == "timeout" and _seen["hello"]:
        return {"ok": False, "error": "The Editor tab did not respond (is the app frozen or the Editor reloading?). Try again."}
    out = _disk_state()
    target = None
    if path:
        target = _abs(path, out["workspace"])
    else:
        for t in out["tabs"]:
            if t["active"] and t["kind"] == "file" and t.get("path"):
                target = t["path"]
    if target:
        try:
            out["file"] = _disk_file(target)
        except OSError as e:
            return {"ok": False, "error": f"Could not read {target}: {e.strerror or e}"}
    return out


# ── document math (LF-normalised text, Monaco 1-based line / UTF-16 column positions) ───
class _Doc:
    def __init__(self, text):
        self.text = text
        self.lines = text.split("\n")            # same line count as Monaco ("a\n" -> 2 lines)
        self.starts = []
        o = 0
        for ln in self.lines:
            self.starts.append(o)
            o += len(ln) + 1

    @property
    def n(self):
        return len(self.lines)

    def pos(self, off):
        """char offset -> (line, column) in Monaco terms."""
        off = max(0, min(off, len(self.text)))
        i = bisect.bisect_right(self.starts, off) - 1
        return i + 1, _u16(self.text[self.starts[i]:off]) + 1

    def off(self, line, col):
        """(line, column) -> char offset (clamped into the file)."""
        line = max(1, min(int(line), self.n))
        ln = self.lines[line - 1]
        target = max(0, int(col) - 1)
        if ln.isascii():
            return self.starts[line - 1] + min(target, len(ln))
        u = k = 0
        for ch in ln:
            if u >= target:
                break
            u += 2 if ord(ch) > 0xFFFF else 1
            k += 1
        return self.starts[line - 1] + k

    def line_of(self, off):
        return self.pos(off)[0]


_OP_ALIASES = {"replace": "replace_text", "str_replace": "replace_text", "edit": "replace_text",
               "replace_range": "replace_lines", "set_lines": "replace_lines",
               "insert": "insert_after_line", "insert_lines": "insert_after_line",
               "delete": "delete_lines", "remove_lines": "delete_lines",
               "replace_all_content": "replace_file", "rewrite": "replace_file", "rewrite_file": "replace_file"}


def _near_hint(doc, old):
    """Help the model when replace_text finds nothing: where does the first line of ``old`` appear?"""
    first = next((ln.strip() for ln in old.split("\n") if ln.strip()), "")
    if not first:
        return ""
    squeeze = lambda t: re.sub(r"\s+", " ", t).strip()
    if squeeze(old) and squeeze(old) in squeeze(doc.text):
        return " The text exists but whitespace/indentation differs - copy it exactly as shown by editor_read_active_file."
    hits = [i + 1 for i, ln in enumerate(doc.lines) if ln.strip() == first][:5]
    if hits:
        return (" Its first line appears at line " + ", ".join(map(str, hits)) +
                ", so the rest differs - re-read those lines and copy them exactly.")
    return " Re-read the file with editor_read_active_file (it may have changed)."


def _kind(op):
    k = _s(op.get("op") or op.get("type") or op.get("kind")).strip().lower()
    return _OP_ALIASES.get(k, k)


def _resolve(doc, ops, sel, cursor):
    """Turn the model's edits into [{"s","e","text","op"}] (offsets into doc.text). -> (edits, error)."""
    out = []
    n = doc.n
    for i, op in enumerate(ops, 1):
        if not isinstance(op, dict):
            return None, f"edit #{i} must be an object like {{\"op\": \"replace_text\", \"old\": ..., \"new\": ...}}."
        kind = _kind(op)
        tag = f"edit #{i} ({kind or 'no op'})"

        if kind == "replace_text":
            old = _lf(_s(op.get("old", op.get("old_str", op.get("find")))))
            new = _lf(_s(op.get("new", op.get("new_str", op.get("replace")))))
            if old == "":
                return None, f"{tag}: 'old' is empty. Use insert_after_line to add text or replace_file to rewrite the file."
            hits, at = [], doc.text.find(old)
            while at >= 0:
                hits.append(at)
                at = doc.text.find(old, at + len(old))
            if not hits:
                return None, f"{tag}: 'old' was not found in the file." + _near_hint(doc, old)
            if len(hits) > 1 and not _truthy(op.get("replace_all")):
                where = ", ".join(str(doc.line_of(h)) for h in hits[:8])
                return None, (f"{tag}: 'old' matches {len(hits)} places (lines {where}). Include more surrounding "
                              "text so it matches exactly once, or set replace_all=true.")
            for h in (hits if _truthy(op.get("replace_all")) else hits[:1]):
                out.append({"s": h, "e": h + len(old), "text": new, "op": i})

        elif kind in ("replace_lines", "delete_lines"):
            a = _int(op.get("start_line", op.get("from", op.get("line"))))
            b = _int(op.get("end_line", op.get("to", a)))
            if a is None or b is None:
                return None, f"{tag}: start_line and end_line (whole numbers) are required."
            if not (1 <= a <= b <= n):
                return None, f"{tag}: lines {a}-{b} are outside the file (it has {n} lines; line numbers start at 1, start_line <= end_line)."
            new = "" if kind == "delete_lines" else _lf(_s(op.get("text", op.get("new"))))
            if b < n:                                    # whole lines including their line break
                s, e = doc.starts[a - 1], doc.starts[b]
                if new and not new.endswith("\n"):
                    new += "\n"
            elif kind == "delete_lines" and a > 1:       # tail of the file: also eat the break before it
                s, e = doc.starts[a - 1] - 1, len(doc.text)
            else:
                s, e = doc.starts[a - 1], len(doc.text)
            out.append({"s": s, "e": e, "text": new, "op": i})

        elif kind == "insert_after_line":
            a = _int(op.get("line", op.get("after_line")))
            if a is None or not (0 <= a <= n):
                return None, f"{tag}: 'line' must be 0..{n} (0 = before the first line, {n} = after the last)."
            new = _lf(_s(op.get("text", op.get("new")))).rstrip("\n")
            if a < n:
                at = doc.starts[a]
                out.append({"s": at, "e": at, "text": new + "\n", "op": i})
            else:
                at = len(doc.text)
                lead = "\n" if doc.lines[-1] != "" else ""
                out.append({"s": at, "e": at, "text": lead + new + "\n", "op": i})

        elif kind == "replace_selection":
            new = _lf(_s(op.get("text", op.get("new"))))
            if sel:
                s = doc.off(sel["startLine"], sel["startCol"])
                e = doc.off(sel["endLine"], sel["endCol"])
                out.append({"s": min(s, e), "e": max(s, e), "text": new, "op": i})
            elif cursor:
                at = doc.off(cursor["line"], cursor["col"])
                out.append({"s": at, "e": at, "text": new, "op": i})
            else:
                return None, f"{tag}: nothing is selected and the cursor position is unknown. Use replace_text or the line-based edits."

        elif kind == "replace_file":
            out.append({"s": 0, "e": len(doc.text), "text": _lf(_s(op.get("text", op.get("new", op.get("content"))))), "op": i})

        else:
            return None, (f"{tag}: unknown edit kind. Use one of replace_text, replace_lines, insert_after_line, "
                          "delete_lines, replace_selection, replace_file.")

    out = [x for x in out if doc.text[x["s"]:x["e"]] != x["text"]]        # drop no-ops
    out.sort(key=lambda x: (x["s"], x["e"], x["op"]))
    for a, b in zip(out, out[1:]):
        if b["s"] < a["e"]:
            return None, (f"edits #{a['op']} and #{b['op']} overlap (lines {doc.line_of(a['s'])}-{doc.line_of(a['e'])} / "
                          f"{doc.line_of(b['s'])}-{doc.line_of(b['e'])}). Merge them into one edit.")
    return out, None


def _apply_text(text, edits):
    out = text
    for x in sorted(edits, key=lambda x: (x["s"], x["e"], x["op"]), reverse=True):
        out = out[:x["s"]] + x["text"] + out[x["e"]:]
    return out


def _flash_ranges(doc, edits):
    """Line spans of the changed text in the NEW file (the page highlights them briefly)."""
    out, delta = [], 0
    for x in edits:
        first = doc.line_of(x["s"]) + delta
        out.append({"start": first, "end": first + x["text"].count("\n")})
        delta += x["text"].count("\n") - doc.text[x["s"]:x["e"]].count("\n")
    return out


def _numbered(lines, first_no, width=None):
    last = first_no + len(lines) - 1
    width = width or len(str(max(last, 1)))
    return "\n".join(f"{first_no + i:>{width}}\u2502{ln}" for i, ln in enumerate(lines))


# ── tools ────────────────────────────────────────────────────────────────────────────────
def _tab_line(t):
    flags = ("*active* " if t.get("active") else "") + ("unsaved " if t.get("dirty") else "") + ("read-only " if t.get("readOnly") else "")
    where = t.get("path") or ("artifact " + _s(t.get("id")) if t.get("kind") == "artifact" else "")
    return f"  - {t.get('name')}" + (f"  [{flags.strip()}]" if flags else "") + (f"  {where}" if where else "")


def read_tool(args):
    args = args or {}
    snap = snapshot(_s(args.get("path")).strip() or None)
    if not snap.get("ok"):
        return "Error: " + _s(snap.get("error"))
    f = snap.get("file")
    head = ["EDITOR  workspace: " + _s(snap.get("workspace"))]
    tabs = snap.get("tabs") or []
    if not f:
        if args.get("path"):
            return (f"'{args.get('path')}' is not open in the Editor. Open it with open_file_in_editor first "
                    "(then it becomes the active tab), or read it with your file tools.")
        head.append("No file is open in the Editor (no active tab). Use open_file_in_editor to open one.")
        if tabs:
            head.append(f"Open tabs ({len(tabs)}):\n" + "\n".join(_tab_line(t) for t in tabs[:TABS_SHOWN]))
        return "\n".join(head)
    if f.get("noText"):
        return "\n".join(head + [f"Active tab: {f.get('name')} - {f.get('reason')}, so it has no text to show."])

    lines = f["content"].split("\n")
    n = len(lines)
    a = max(1, _int(args.get("start_line")) or 1)
    b = min(n, _int(args.get("end_line")) or n)
    if a > n:
        return f"Error: start_line {a} is past the end of the file ({n} lines)."
    shown, size, stop = [], 0, b
    for i in range(a - 1, b):
        size += len(lines[i]) + 8
        if size > READ_MAX_CHARS and shown:
            stop = i
            break
        shown.append(lines[i])
    state = ("UNSAVED changes in the editor buffer (the file on disk is older)" if f.get("dirty") else "saved (matches disk)")
    if not snap.get("live"):
        state = "read from disk (the Editor tab is not running)"
    head.append(f"File: {f.get('name')}  {_s(f.get('path'))}  |  {f.get('lang') or 'text'}  |  {n} lines  |  {f.get('eol', 'LF')}  |  {state}"
                + ("  |  READ-ONLY" if f.get("readOnly") else "") + ("  |  DELETED on disk" if f.get("deleted") else ""))
    if f.get("conflict"):
        head.append("Note: the file changed on disk while open - the user has an unresolved conflict banner in the Editor.")
    cur, sel = f.get("cursor"), f.get("selection")
    if cur:
        head.append(f"Cursor: line {cur['line']}, column {cur['col']}.")
    if sel:
        txt = sel.get("text") or ""
        head.append(f"Selection: line {sel['startLine']}:{sel['startCol']} to line {sel['endLine']}:{sel['endCol']} ({len(txt)} chars)"
                    f" - 'this' / 'here' / 'selected' mean this:\n<<<\n{txt[:1500]}{'...' if len(txt) > 1500 else ''}\n>>>")
    if f.get("visible"):
        head.append(f"Visible on screen: lines {f['visible']['start']}-{f['visible']['end']}.")
    probs = f.get("problems") or []
    if probs:
        head.append(f"Problems reported by the editor ({len(probs)}):\n" + "\n".join(
            f"  L{p['line']}:{p['col']} {p['sev']}: {p['msg']}" for p in probs[:PROBLEMS_SHOWN]))
    other = [t for t in tabs if not t.get("active")]
    if other:
        head.append(f"Other open tabs ({len(other)}): " + ", ".join(t.get("name", "?") + (" (unsaved)" if t.get("dirty") else "") for t in other[:TABS_SHOWN]))
    head.append("Line numbers below are display only (not part of the file):")
    body = _numbered(shown, a, len(str(n)))
    tail = ""
    if shown and a + len(shown) - 1 < b:
        tail = f"\n... output cut at line {a + len(shown) - 1} of {n}; call again with start_line={a + len(shown)} for the rest."
    return "\n".join(head) + "\n\u2500\u2500\u2500\n" + body + tail


def tree_tool(args):
    args = args or {}
    st = get_state()
    if not st.get("ok"):
        return "Error: " + _s(st.get("error"))
    ws = st.get("workspace") or _default_workspace()
    root = _abs(args.get("path"), ws) if _s(args.get("path")).strip() else ws
    if not os.path.isdir(root):
        return f"Error: not a folder: {root}" + (" (that is a file - use editor_read_active_file / open_file_in_editor)" if os.path.isfile(root) else "")
    depth = max(1, min(_int(args.get("depth")) or TREE_DEFAULT_DEPTH, TREE_MAX_DEPTH))
    cap = max(20, min(_int(args.get("max_entries")) or TREE_DEFAULT_ENTRIES, TREE_MAX_ENTRIES))
    eb = _eb()
    skip_dirs = set(eb.SEARCH_SKIP_DIRS)
    key = lambda p: os.path.normcase(os.path.normpath(p))
    marks = {}
    for t in st.get("tabs") or []:
        if t.get("path"):
            marks[key(t["path"])] = (" [active]" if t.get("active") else " [open]") + (" *unsaved" if t.get("dirty") else "")
    count = [0]
    lines = []
    clipped = [False]

    def walk(folder, level):
        try:
            with os.scandir(folder) as it:
                items = list(it)
        except OSError as e:
            lines.append("  " * level + f"(cannot read: {e.strerror or e})")
            return
        dirs, files = [], []
        for e in items:
            if e.name in eb.HIDDEN_NAMES or e.name.endswith(eb.HIDDEN_SUFFIXES):
                continue
            try:
                is_dir = e.is_dir()
            except OSError:
                is_dir = False
            if is_dir and (e.name in skip_dirs or e.name.endswith(".egg-info")):
                continue
            (dirs if is_dir else files).append(e)
        dirs.sort(key=lambda e: e.name.lower())
        files.sort(key=lambda e: e.name.lower())
        shown = 0
        entries = dirs + files
        for e in entries:
            if count[0] >= cap:
                clipped[0] = True
                return
            if shown >= TREE_PER_DIR:
                lines.append("  " * level + f"... +{len(entries) - shown} more here (pass path=\"{folder}\" to list this folder alone)")
                return
            shown += 1
            count[0] += 1
            if e.is_dir():
                lines.append("  " * level + e.name + "/")
                if level < depth:
                    walk(e.path, level + 1)
                else:
                    try:
                        inner = len(os.listdir(e.path))
                    except OSError:
                        inner = 0
                    if inner:
                        lines[-1] += f"  ({inner} items, deeper than depth {depth})"
            else:
                try:
                    size = e.stat().st_size
                except OSError:
                    size = 0
                lines.append("  " * level + e.name + (f"  ({_fmt_size(size)})" if size >= 100 * 1024 else "") + marks.get(key(e.path), ""))

    walk(root, 1)
    head = f"{os.path.basename(root) or root}/   {root}" + ("   (the Editor's workspace)" if key(root) == key(ws) else "")
    foot = []
    if clipped[0]:
        foot.append(f"(stopped at {cap} entries - pass a deeper path=... or a higher max_entries to see more)")
    foot.append("Not listed: .git, node_modules, __pycache__, virtualenvs, build/dist/caches and .pyc files. "
                "[open]/[active]/*unsaved mark Editor tabs.")
    return head + "\n" + ("\n".join(lines) if lines else "  (empty)") + "\n" + "\n".join(foot)


def _open(path, line=None, col=None):
    """Open ``path`` in the Editor (active tab) and wait for it. -> (status, text);
    status: opened | notext | failed | pending (sent, not confirmed) | nogui."""
    payload = {"path": path}
    if line and line > 0:
        payload["line"] = line
        if col and col > 0:
            payload["col"] = col
    res, err = _request("open_file", payload)
    where = f" at line {line}" if line and line > 0 else ""
    if res is not None:
        if not res.get("ok"):
            return "failed", f"could not open {path} in the Editor: {res.get('error') or 'see the toast in the Editor tab'}."
        if res.get("noText"):
            return "notext", f"{path} is {res.get('reason', 'not a text file')}, so there is no text to read or edit."
        return "opened", (f"Opened {path} in the Editor{where}; it is now the ACTIVE tab ({res.get('lineCount', '?')} lines"
                          + (", unsaved changes" if res.get("dirty") else "") + ").")
    hook = _hook()
    if hook is None:
        return "nogui", "(Not opened: no GUI attached.)"
    try:
        hook("open_file", payload)         # page not confirming: fall back to the plain fire-and-forget event
    except Exception as e:
        return "failed", f"(Could not open in the Editor tab: {e})"
    return "pending", (f"Asked the Editor to open {path}{where}, but it has not confirmed yet (it may still be starting). "
                       "Wait a moment before reading it.")


def open_file_tool(args):
    args = args or {}
    raw = _s(args.get("path")).strip().strip('"')
    if not raw:
        return "Error: 'path' is required."
    path, err = _find_in_workspace(raw, _disk_state()["workspace"])
    if err:
        return "Error: " + err
    status, text = _open(path, _int(args.get("line")), _int(args.get("column")))
    if status == "failed":
        return "Error: " + text
    if status == "opened":
        text += " editor_read_active_file shows its text."
    return text


def open_folder_tool(args):
    args = args or {}
    raw = _s(args.get("path")).strip().strip('"')
    if not raw:
        return "Error: 'path' is required."
    path = _abs(raw, _disk_state()["workspace"])
    if not os.path.isdir(path):
        return f"Error: no such folder: {path}" + (" (that is a file - use open_file_in_editor)" if os.path.isfile(path) else "")
    res, err = _request("open_folder", {"path": path})
    if res is not None:
        if not res.get("ok"):
            return f"Error: could not open {path} as the workspace: {res.get('error') or 'see the toast in the Editor tab'}."
        return (f"{path} is now the Editor's workspace (the Explorer shows it; both terminals are linked to it). "
                "editor_workspace_tree lists its contents.")
    hook = _hook()
    if hook is None:
        return "(Not opened: no GUI attached.)"
    try:
        hook("open_folder", {"path": path})
    except Exception as e:
        return f"(Could not open in the Editor tab: {e})"
    return f"Asked the Editor to open {path} as the workspace, but it has not confirmed yet (it may still be starting)."


def _parse_edits(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None, "'edits' must be a list of edit objects (it was a string that is not valid JSON)."
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return None, "'edits' must be a non-empty list of edit objects."
    if len(raw) > MAX_EDITS:
        return None, f"too many edits in one call ({len(raw)}; max {MAX_EDITS})."
    return raw, None


def edit_tool(args, target=None):
    """Edit the ACTIVE tab (``target`` = the file edit_file_in_editor just opened, verified to be the active one)."""
    args = args or {}
    ops, perr = _parse_edits(args.get("edits"))
    if perr:
        return "Error: " + perr
    save = _truthy(args.get("save"))
    anchored = all(isinstance(o, dict) and _kind(o) == "replace_text" for o in ops)
    approved = False
    for attempt in (1, 2):
        snap = snapshot(target)
        if not snap.get("ok"):
            return "Error: " + _s(snap.get("error"))
        f = snap.get("file")
        if not f:
            return ("Error: that file is not open in the Editor." if target else
                    "Error: no file is open in the Editor. Call open_file_in_editor first - then the edit applies to that tab.")
        if f.get("noText"):
            return f"Error: the active tab ({f.get('name')}) is {f.get('reason')} and cannot be edited as text."
        if f.get("readOnly"):
            why = "its file was deleted on disk" if f.get("deleted") else "it is read-only (e.g. an attached file; the user can 'Save as copy')"
            return f"Error: the active tab ({f.get('name')}) cannot be edited: {why}."
        live = bool(snap.get("live"))
        if target and live and _nk(f.get("path")) != _nk(target):
            return f"Error: could not make {os.path.basename(target)} the active tab, so it was not edited."
        doc = _Doc(f["content"])
        edits, rerr = _resolve(doc, ops, f.get("selection"), f.get("cursor"))
        if rerr:
            return "Error: " + rerr + " Nothing was changed."
        if not edits:
            return f"No changes: every edit already matches the current text of {f.get('name')}."
        new_text = _apply_text(doc.text, edits)

        if not approved and _approval_needed():
            if not live:
                return ("Error: this edit needs the user's approval, but the Editor tab is not running, so the approval dialog "
                        "cannot be shown. Ask the user to open the Editor tab, then retry. Nothing was changed.")
            ok, why = _ask_approval(f, doc, new_text, edits, save)
            if not ok:
                return why
            approved = True

        if live:
            payload = {"key": f.get("key"), "version": f.get("version"), "save": save,
                       "edits": [{"sl": doc.pos(x["s"])[0], "sc": doc.pos(x["s"])[1],
                                  "el": doc.pos(x["e"])[0], "ec": doc.pos(x["e"])[1], "text": x["text"]} for x in edits],
                       "flash": _flash_ranges(doc, edits)}
            res, err = _request("edit", payload)
            if res is None:
                return (f"Error: the Editor did not confirm the edit ({err}). It may or may not have been applied - "
                        "call editor_read_active_file to check before retrying.")
            if res.get("stale"):
                if attempt == 1 and anchored:
                    continue                       # text-anchored edits can safely be re-resolved against fresh text
                return "Error: the active tab changed while the edit was prepared. Nothing was changed - read the file again and retry."
            if not res.get("ok"):
                return "Error: " + _s(res.get("error") or "the Editor rejected the edit") + ". Nothing was changed."
            saved_note = ""
            if save:
                saved_note = (" Saved to disk." if res.get("saved") else
                              " NOT saved: the Editor could not save it (the file may have changed on disk - the user sees a banner).")
            else:
                saved_note = " Not saved yet - the user sees it in the buffer and can undo with Ctrl+Z; pass save=true to write it to disk."
            via = ""
        else:
            from_disk = f.get("disk") or {}
            eb = _eb()
            text = new_text.replace("\n", "\r\n") if f.get("eol") == "CRLF" else new_text
            r = eb.EditorMixin.editor_write_file(None, f["path"], text, from_disk.get("enc", "utf-8"),
                                                 from_disk.get("bom", False), from_disk.get("mtime"))
            if r.get("conflict"):
                return "Error: the file changed on disk while editing. Nothing was written - read it again and retry."
            if not r.get("ok"):
                return "Error: could not write the file: " + _s(r.get("error")) + "."
            saved_note, via = " Saved to disk.", " (The Editor tab was not running, so the file on disk was edited directly.)"

        nd = _Doc(new_text)
        first = nd.line_of(edits[0]["s"])
        lo, hi = max(1, first - 2), min(nd.n, first + edits[0]["text"].count("\n") + 2)
        ctx = _numbered(nd.lines[lo - 1:hi], lo, len(str(nd.n)))
        return (f"Edited {f.get('name')}: {len(edits)} change(s), now {nd.n} lines ({nd.n - doc.n:+d}).{saved_note}{via}{' Approved by the user.' if approved else ''}\n"
                f"Result around line {first}:\n{ctx}")
    return "Error: could not apply the edit."


def _ask_approval(f, doc, new_text, edits, save):
    """Show the user a diff in the Editor and wait. -> (True, "") or (False, message for the model)."""
    diff = _diff_lines(doc.lines, new_text.split("\n"))
    payload = {"name": f.get("name"), "path": f.get("path") or "", "diff": diff, "changes": len(edits),
               "added": sum(1 for ln in diff if ln.startswith("+")), "removed": sum(1 for ln in diff if ln.startswith("-")),
               "save": bool(save), "timeoutMs": int(APPROVAL_TIMEOUT * 1000)}
    res, err = _request("approve_edit", payload, timeout=APPROVAL_TIMEOUT + APPROVAL_SLACK)
    if res is None:
        return False, (f"Error: no answer to the approval request ({err}). Nothing was changed. Tell the user you are waiting "
                       "for their approval in the Editor tab; do not retry on your own.")
    if res.get("approved"):
        return True, ""
    if res.get("timedOut"):
        return False, "Error: the user did not answer the approval dialog in time. Nothing was changed. Ask them whether to retry."
    note = _s(res.get("note")).strip()
    return False, ("The user REJECTED this edit" + (f' and said: "{note}"' if note else "")
                   + ". Nothing was changed. Do not retry the same change - briefly ask what they would like instead.")


def edit_file_tool(args):
    """Open ANY workspace file as the active tab, then edit it - the two steps in one call."""
    args = args or {}
    raw = _s(args.get("path")).strip().strip('"')
    if not raw:
        return "Error: 'path' is required."
    ops, perr = _parse_edits(args.get("edits"))        # fail fast on bad input, before touching the UI
    if perr:
        return "Error: " + perr
    path, err = _find_in_workspace(raw, _disk_state()["workspace"])
    if err:
        return "Error: " + err
    status, text = _open(path)
    if status in ("failed", "notext"):
        return "Error: " + text
    return edit_tool(args, target=path)


def guard_external_write(func_name, arguments):
    """Called by orchestration before a tool runs. Returns a message (-> the call is blocked) when a generic
    file-writing tool targets a file that is OPEN in the Editor: that would bypass the user's approval and can
    clobber their unsaved text. Also protects the Editor's own settings file (it holds the approval switch)."""
    try:
        a = arguments if isinstance(arguments, dict) else {}
        targets = []
        if func_name in ("write_local_file", "append_local_file"):
            if a.get("path"):
                try:
                    from utils.path_resolver import resolve_file_path
                    targets.append(resolve_file_path(a["path"])[0])
                except Exception:
                    targets.append(a["path"])
        elif func_name == "call_mcp_tool" and _s(a.get("tool_name")).lower() in _FS_WRITE_TOOLS:
            inner = a.get("arguments") if isinstance(a.get("arguments"), dict) else {}
            targets += [inner[k] for k in ("path", "source", "destination") if inner.get(k)]
        if not targets:
            return None
        opened = {_nk(t["path"]) for t in (_persisted().get("tabs") or [])
                  if isinstance(t, dict) and t.get("kind") == "file" and t.get("path")}
        state = _nk(_eb().STATE_PATH)
        for t in targets:
            nk = _nk(os.path.abspath(os.path.expanduser(_s(t))))
            if nk == state:
                return "Blocked: that is the Editor's settings file (it holds the user's approval switch); it is not yours to edit."
            if nk in opened:
                return (f"Blocked: {t} is open in the user's Editor. Writing it with {func_name} would bypass their approval and "
                        "can overwrite unsaved edits. Use editor_edit_active_file (if it is the active tab) or "
                        "edit_file_in_editor(path, edits) instead - both go through the Editor.")
    except Exception:
        return None
    return None


_FS_WRITE_TOOLS = {"write_file", "edit_file", "move_file", "delete_file", "create_file"}


# ── prompt block + schemas ───────────────────────────────────────────────────────────────
# Appended to the MAIN model's system prompt (never the inline-autocomplete / inline-instruction model, which has its own
# prompts) while the Editor is in use. No tools are added: it teaches the habits of a coding agent with the two shells the
# model already has (execute_wsl_command, execute_terminal_command).
AGENT_WORKFLOW = r"""━━━ CODING WORKFLOW (the Editor is in use) ━━━
Work like a coding agent: explore, then edit, then verify - with your shells, not just file readers. Your WSL shell
(execute_wsl_command) is the main tool; PowerShell (execute_terminal_command) is the Windows fallback. Both normally start in the
Editor's workspace; a Windows path like D:/x is /mnt/d/x in WSL. The user watches every command live, so keep them short.
EXPLORE - search, don't read whole files
- Layout: editor_workspace_tree, or `ls`, `find . -name '*.py'`.
- Find code: `rg -n 'def name|class Name' -g '*.py'` (definitions), `rg -nw name` (every use - do it before changing a signature or
  renaming), `rg -l pattern` (which files), `rg -n -C3 pattern file` (with context). No rg? `grep -rn --include='*.py' pattern .`.
  PowerShell: `Select-String -Path *.py -Pattern x`.
- Size things up first: `wc -l file`, `rg -c pattern file`. Cap long output with `| head -n 40` or `| tail -n 40`. Never dump
  node_modules, logs, lockfiles or minified files.
- Read narrowly: once you know the line, open_file_in_editor(path, line) then editor_read_active_file(start_line, end_line), or
  `sed -n '120,180p' file`. Read a whole file only when it is short or you are restructuring it. For a file that is OPEN in the Editor
  trust editor_read_active_file - a shell only sees the saved copy on disk.
EDIT - small and exact
- Change code with the editor edit tools, never `sed -i` or `>` redirects (blocked on open files, and they skip the user's approval).
  Prefer a few small replace_text edits in one call over replace_file; match the surrounding style; do not reformat code you are not
  changing; find every call site before you change an interface.
VERIFY - never assume it works
- Shells read the file on DISK: pass save=true (or ask the user to save) before you run anything that uses an edited file.
- Then run the project's own checks and read the real output: `python -m py_compile f.py`, `ruff check f.py`, `pytest -x -q path::test`,
  `node --check f.js`, `npm test`, `tsc --noEmit`, or just run the script. On failure read the error, fix the cause, rerun - do not stack
  guesses. Unsure how an API works? `--help`, `rg` the source, read the docs - do not invent it.
- git for orientation: `git status -sb`, `git diff --stat`, `git diff path`, `git log --oneline -n 10`, `git blame -L a,b file`,
  `git grep -n x`. Check status before and after a task. Do NOT commit, push, stash, `reset --hard`, `checkout -- file` or `clean`
  unless the user asks - it can destroy their work.
STYLE
- Be autonomous: run read-only commands freely, chain them with `&&`, and find answers yourself instead of asking the user what a
  command can tell you. Ask only for real decisions.
- For a change across several files give a 2-4 line plan first; at the end summarise what changed and what you verified (command and
  result) in a few lines."""


def editor_active():
    """The Editor is 'in use' when at least one file tab is open (from its persisted state - cheap, no page round trip)."""
    return any(isinstance(t, dict) and t.get("kind") == "file" and t.get("path") for t in (_persisted().get("tabs") or []))


def prompt_block():
    """Short note appended to the system prompt every step: tells the model the Editor is linked to this chat
    and what is open (from the Editor's persisted state - cheap, no round trip to the page)."""
    st = _persisted()
    ws = st.get("workspace")
    if not ws or not os.path.isdir(ws):
        ws = _default_workspace()
    lines = ["\u2501\u2501\u2501 EDITOR (linked to this chat) \u2501\u2501\u2501", f"Workspace: {ws}"]
    tabs = [t for t in (st.get("tabs") or []) if isinstance(t, dict)]
    act = next((t for t in tabs if t.get("key") == st.get("active")), None)
    if act:
        lines.append("Active tab: " + _s(act.get("name") or act.get("path")))
    rest = [_s(t.get("name") or t.get("path")) for t in tabs if t is not act]
    if rest:
        lines.append("Other tabs: " + ", ".join(rest[:12]) + (f" (+{len(rest) - 12} more)" if len(rest) > 12 else ""))
    lines.append("The user's code Editor is linked to you. READ the file they are working on with editor_read_active_file (live text "
                 "incl. unsaved edits - the copy on disk may be older; 'this', 'here', 'the selected code' mean its cursor/selection). "
                 "CHANGE the active file with editor_edit_active_file. To change ANY OTHER file in the workspace use "
                 "edit_file_in_editor(path, edits): it opens that file from the workspace tree as the active tab and edits it in one call "
                 "(a bare name like 'app.py' or a partial path like 'gui/app.py' is enough). Navigate with editor_workspace_tree, "
                 "open_file_in_editor and open_folder_in_editor.")
    lines.append("RULE: do NOT use write_local_file, append_local_file, terminal commands (sed, echo >, python scripts) or the "
                 "Filesystem MCP tools (write_file, edit_file, move_file) to modify a file that is open in the Editor - that bypasses the "
                 "user's approval and can destroy their unsaved text; such calls are blocked. Generic file tools remain fine for "
                 "creating NEW files and for files outside the workspace. Read before you edit.")
    lines.append("Each edit is shown to the user as a diff for approval before it is applied; if they reject it, do not retry the "
                 "same change - ask what they want instead." if _approval_needed() else
                 "(The user switched edit approvals off, so your edits apply immediately - still keep them small and exact.)")
    out = "\n".join(lines)
    if editor_active():
        out += "\n\n" + AGENT_WORKFLOW
    return out


def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": list(required)}}}


_EDIT_ITEM = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["replace_text", "replace_lines", "insert_after_line", "delete_lines",
                                          "replace_selection", "replace_file"],
               "description": "Kind of edit."},
        "old": {"type": "string", "description": "replace_text: exact text to find (must match once, whitespace included)."},
        "new": {"type": "string", "description": "replace_text: replacement text."},
        "replace_all": {"type": "boolean", "description": "replace_text: replace every match instead of requiring exactly one."},
        "start_line": {"type": "integer", "description": "replace_lines / delete_lines: first line (1-based)."},
        "end_line": {"type": "integer", "description": "replace_lines / delete_lines: last line, inclusive."},
        "line": {"type": "integer", "description": "insert_after_line: insert after this line (0 = top of file)."},
        "text": {"type": "string", "description": "replace_lines / insert_after_line / replace_selection / replace_file: the new text."},
    },
    "required": ["op"],
}

TOOL_SCHEMAS = [
    _fn("open_file_in_editor",
        "Open ANY file on the user's computer (a project file, config, script - not an artifact) in the Editor tab and make it the ACTIVE "
        "tab, optionally scrolled to a line. Use it when the user asks to open, show or edit a file, or before editing a file that is not "
        "the active tab. Take the file from the workspace tree: a bare name ('app.py') or partial path ('gui/app.py') is enough "
        "when it is unique (ambiguous names list the candidates); absolute paths work too. It does not return the text: call "
        "editor_read_active_file next. Calling it again for an already-open file just brings it to the front. For artifacts use open_artifact.",
        {"path": {"type": "string", "description": "Path of the file (absolute, or relative to the Editor's workspace)."},
         "line": {"type": "integer", "description": "Optional line number to scroll to."},
         "column": {"type": "integer", "description": "Optional column (needs line)."}},
        ["path"]),
    _fn("open_folder_in_editor",
        "Make a folder the Editor's WORKSPACE: the Explorer sidebar shows it, Ctrl+P searches it, and both terminals are linked to it. Use it "
        "when the user asks to open a project / folder in the Editor or to switch projects. Relative paths are resolved against the current "
        "workspace. editor_workspace_tree then lists its contents.",
        {"path": {"type": "string", "description": "Path of the folder (absolute, or relative to the current workspace)."}},
        ["path"]),
    _fn("editor_workspace_tree",
        "See the folder tree of the Editor's workspace (or any sub-folder): folders first, large files with their size, and Editor tabs marked "
        "[open] / [active] / *unsaved. Noise folders (.git, node_modules, __pycache__, venvs, build/dist) are left out. Use it to find files "
        "before opening or reading them.",
        {"path": {"type": "string", "description": "Folder to list (default: the workspace root). Absolute or relative to the workspace."},
         "depth": {"type": "integer", "description": f"Levels to show (default {TREE_DEFAULT_DEPTH}, max {TREE_MAX_DEPTH})."},
         "max_entries": {"type": "integer", "description": f"Cap on listed entries (default {TREE_DEFAULT_ENTRIES}, max {TREE_MAX_ENTRIES})."}}),
    _fn("editor_read_active_file",
        "See the file in the Editor's ACTIVE tab exactly as the user sees it: its LIVE text including unsaved edits (numbered lines, display "
        "only), plus the cursor, the current selection, which lines are on screen, problems the editor flagged, and the other open tabs. "
        "'this file', 'here', 'the selected code' refer to this. Long files are paged with start_line / end_line. Call it before "
        "editor_edit_active_file. Pass path to read another OPEN tab instead.",
        {"start_line": {"type": "integer", "description": "First line to return (default 1)."},
         "end_line": {"type": "integer", "description": "Last line to return (default: end of file, capped by size)."},
         "path": {"type": "string", "description": "Optional: read this other open tab instead of the active one."}}),
    _fn("editor_edit_active_file",
        "Edit the file in the Editor's ACTIVE tab in place, in the live editor buffer: the user watches the change, can undo it with Ctrl+Z, "
        "and text they typed but have not saved is never lost. Read the file first (editor_read_active_file). 'edits' is a list and every edit "
        "is located against the file AS YOU READ IT, so several edits in one call never shift each other's line numbers. Kinds: "
        "replace_text {old,new,replace_all?} - PREFERRED, 'old' must match exactly once, whitespace and indentation included; "
        "replace_lines {start_line,end_line,text} - replaces whole lines; insert_after_line {line,text} - 0 = top of file; "
        "delete_lines {start_line,end_line}; replace_selection {text} - the user's current selection (or inserts at the cursor); "
        "replace_file {text} - whole file, only when most of it changes. If any edit is invalid NOTHING is applied and the error says why. "
        "The change stays unsaved unless save=true - pass save=true when the file must be on disk (before running it, or when the user asked "
        "you to save). The user must APPROVE each edit (they see a diff); if they reject it, nothing changes and you should ask what they "
        "want instead of retrying. For a file that is NOT the active tab use edit_file_in_editor. Never use write_local_file / "
        "Filesystem MCP tools on a file open in the Editor. Keep your chat reply short; do not paste the file back.",
        {"edits": {"type": "array", "items": _EDIT_ITEM, "description": "The edits to apply, all against the text as last read."},
         "save": {"type": "boolean", "description": "Also save the file to disk after editing (default false)."}},
        ["edits"]),
    _fn("edit_file_in_editor",
        "Edit ANY file of the Editor's workspace in one call: it is opened from the workspace tree into the Editor as the ACTIVE tab "
        "(a bare name like 'app.py' or a partial path like 'gui/app.py' is enough when unique; absolute paths work too) and then "
        "edited exactly like editor_edit_active_file - same edit kinds, located against the file's current text, applied to the live "
        "buffer, undoable, shown to the user as a diff for approval first. Use it instead of write_local_file / Filesystem MCP "
        "write_file/edit_file for every existing file. Read the file first (open_file_in_editor + editor_read_active_file) unless you "
        "already know its exact text. save=true writes it to disk afterwards. It cannot create new files.",
        {"path": {"type": "string", "description": "File to edit: workspace-relative name / partial path, or absolute path."},
         "edits": {"type": "array", "items": _EDIT_ITEM, "description": "The edits to apply, all against the file's current text."},
         "save": {"type": "boolean", "description": "Also save the file to disk after editing (default false)."}},
        ["path", "edits"]),
]


def register_tools(tools_list):
    """Idempotently append the editor-link tool schemas to the shared ``tools`` list."""
    have = {t.get("function", {}).get("name") for t in tools_list if isinstance(t, dict)}
    for t in TOOL_SCHEMAS:
        if t["function"]["name"] not in have:
            tools_list.append(t)
    return tools_list


def dispatch(func_name, arguments):
    """Returns (output_text, None) like artifacts.dispatch / midumdocs.dispatch."""
    try:
        if func_name == "open_file_in_editor":
            return open_file_tool(arguments), None
        if func_name == "open_folder_in_editor":
            return open_folder_tool(arguments), None
        if func_name == "editor_workspace_tree":
            return tree_tool(arguments), None
        if func_name == "editor_read_active_file":
            return read_tool(arguments), None
        if func_name == "editor_edit_active_file":
            return edit_tool(arguments), None
        if func_name == "edit_file_in_editor":
            return edit_file_tool(arguments), None
    except Exception as e:
        return f"Error in {func_name}: {type(e).__name__}: {e}", None
    return f"Error: unknown editor tool '{func_name}'", None
