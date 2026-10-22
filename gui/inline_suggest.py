"""AI inline code suggestions for the Editor tab (ghost text, accepted with Tab).

The page (``editor_assets/editor.html``) POSTs the text before/after the cursor to the editor's
localhost server (``editor_backend._Handler.do_POST`` -> :func:`handle`), which asks Gemini for a
suggestion and returns it as a small structured result.

What a suggestion can be
------------------------
``{action, is_instruction, text, start, end}`` where ``action`` is

* ``insert``  - ``text`` goes at the cursor (shown as ghost text),
* ``replace`` - lines ``start..end`` (absolute, 1-based) are rewritten with ``text``,
* ``delete``  - lines ``start..end`` are removed.

replace / delete are only ever allowed inside the EDITABLE LINES the page sends (``edit``):
  * normal typing: a small window of numbered lines around the cursor (the model is told to be
    conservative: typos, stale names, accidental duplicates, obvious mistakes);
  * instruction mode (``instr``: the cursor sits on / right under a comment): the code block directly
    below the comment. The model decides whether the comment is an instruction (``ai:`` prefix or a
    request such as "add a method that ...") and which of those lines the instruction touches.
The page draws replace/delete as a diff with green / red / yellow gutter markers (added / deleted /
edited) and applies it on Tab; once an instruction has been carried out it offers to delete the comment.

Model and keys
--------------
* Model: ``INLINE_MODEL`` (gemini-3.1-flash-lite) through the NATIVE ``generateContent`` endpoint
  (lower latency than the OpenAI-compat layer, and lets us turn thinking down to "minimal").
  It is deliberately independent of ``config.GEMINI_API_MODEL``.
* Key hopping: the keys come from the shared secrets file (GEMINI_API_KEY, GEMINI_API_KEY_2 ..
  _5, re-read whenever the file changes). Requests are spread ROUND-ROBIN over every usable key
  (each free-tier key has its own per-minute quota, so N keys = N x the headroom), and a key that
  answers 429 / 403 / 5xx / times out is put on a cooldown and the same request immediately hops
  to the next key. Keys are never sent to the page or logged.
* Security: the endpoint needs the per-process ``TOKEN`` (injected into the served page), the
  Host/Origin must be the editor's own localhost origin, and the body is size-capped.
"""
import hashlib
import http.client
import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

import config

INLINE_MODEL = "gemini-3.1-flash-lite"
KEY_FIELDS = ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY_4", "GEMINI_API_KEY_5")

REQUEST_TIMEOUT = 8.0          # seconds per attempt for plain completions (a slow key is skipped quickly)
REQUEST_TIMEOUT_INSTR = 20.0   # instruction-driven generations can be long
MAX_PREFIX_CHARS = 5000
MAX_SUFFIX_CHARS = 1500
MAX_TOKENS_COMPLETE = 400      # ghost text is short; an unbounded generation is what makes it feel slow
MAX_TOKENS_INSTR = 2500        # instruction-driven code ("add a method that ...") may be longer
MAX_LINES = None               # None = no cap on the length of one suggestion
MAX_EDIT_LINES = 120           # editable window the page may send
MAX_EDIT_LINE_CHARS = 600
MAX_CONCURRENT = 4             # in-flight upstream calls; extra requests get {"busy": true}
CACHE_SIZE = 200

TOKEN = secrets.token_urlsafe(24)       # per-process secret, injected into editor.html
CURSOR = "<|cursor|>"


def token_ok(candidate):
    return hmac.compare_digest(str(candidate or ""), TOKEN)


def _api_root():
    base = (getattr(config, "GEMINI_API_BASE", "") or "").rstrip("/")
    if base.endswith("/openai"):
        base = base[: -len("/openai")]
    return base or "https://generativelanguage.googleapis.com/v1beta"


# ── key pool with round-robin hopping + per-key cooldowns ────────────────────────────────
class _KeyPool:
    def __init__(self):
        self._lock = threading.Lock()
        self._keys = []
        self._until = {}          # key -> (monotonic time it becomes usable again, reason)
        self._cursor = 0
        self._stamp = None

    def _reload_locked(self):
        path = os.path.abspath(config.SECRETS_FILE)
        try:
            stamp = (path, os.path.getmtime(path))
        except OSError:
            self._keys, self._stamp = [], None
            return
        if stamp == self._stamp:
            return
        keys = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for field in KEY_FIELDS:
                k = str(data.get(field, "") or "").strip()
                if k and k not in keys:
                    keys.append(k)
        except (OSError, ValueError, AttributeError):
            keys = []
        self._keys, self._stamp = keys, stamp
        self._until = {k: v for k, v in self._until.items() if k in keys}
        self._cursor = self._cursor % len(keys) if keys else 0

    def count(self):
        with self._lock:
            self._reload_locked()
            return len(self._keys)

    def acquire(self, tried):
        """Next usable key (round-robin) that is not in ``tried`` -> (position, key) or None."""
        with self._lock:
            self._reload_locked()
            n, now = len(self._keys), time.monotonic()
            for step in range(n):
                i = (self._cursor + step) % n
                k = self._keys[i]
                if k in tried or self._until.get(k, (0, ""))[0] > now:
                    continue
                self._cursor = (i + 1) % n          # the NEXT request starts on the next key
                return i + 1, k
            return None

    def penalize(self, key, seconds, why):
        with self._lock:
            self._until[key] = (time.monotonic() + seconds, why)

    def snapshot(self):
        with self._lock:
            self._reload_locked()
            now = time.monotonic()
            waits = [max(0.0, self._until.get(k, (0, ""))[0] - now) for k in self._keys]
            return {"total": len(self._keys),
                    "available": sum(1 for w in waits if w <= 0),
                    "retry_after": round(min(waits), 1) if waits and min(waits) > 0 else 0}


POOL = _KeyPool()


class _Hop(Exception):
    """This key cannot serve right now (rate limit / bad key / upstream hiccup): try the next one."""
    def __init__(self, cooldown, msg):
        super().__init__(msg)
        self.cooldown, self.msg = cooldown, msg


class _Fatal(Exception):
    """A problem another key will not fix (bad request, unknown model)."""
    def __init__(self, msg):
        super().__init__(msg)
        self.msg = msg


def _classify(code, raw):
    msg, low = "", raw.lower()
    try:
        j = json.loads(raw)
        j = j[0] if isinstance(j, list) and j else j
        msg = str((j.get("error") or {}).get("message") or "")
        low = (msg + " " + raw).lower()
    except (ValueError, AttributeError):
        pass
    if code == 429:
        m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', raw)
        if "perday" in low.replace(" ", ""):
            cooldown = 1800                                  # daily quota: do not poll it every request
        else:
            cooldown = min(max(float(m.group(1)) if m else 60.0, 5.0), 180.0)
        return _Hop(cooldown, "rate limited")
    if code in (401, 403) or (code == 400 and ("api key not valid" in low or "api_key_invalid" in low)):
        return _Hop(1800, "key rejected" + (": " + msg[:80] if msg else ""))
    if code in (500, 502, 503, 504):
        return _Hop(15, f"upstream {code}")
    if code == 404:
        return _Fatal(f"model {INLINE_MODEL} not available (404)")
    return _Fatal(f"HTTP {code}: {msg[:160]}")


# ── prompts ──────────────────────────────────────────────────────────────────────────────
_REPLY_FORMAT = (
    "\n\nReply with a single JSON object: {\"is_instruction\": boolean, \"action\": \"insert\" | "
    "\"replace\" | \"delete\", \"start_line\": integer, \"end_line\": integer, \"text\": string}. "
    "Use 0 for start_line and end_line when the action is 'insert'."
)

_SYSTEM = (
    "You are the inline code assistant of a code editor (like GitHub Copilot with next-edit "
    "suggestions). The user message contains a file with a <|cursor|> marker showing where the user "
    "is typing, followed by EDITABLE LINES: a short window of numbered lines around the cursor (the "
    "'N|' prefix is the absolute line number and is NOT part of the code; the cursor marker is "
    "repeated on the cursor line). is_instruction must always be false here.\n\n"
    "action 'insert' (the normal case, use it almost always): text = ONLY the exact text to insert at "
    "the cursor: no explanation, no markdown fences, and never repeat any text that already appears "
    "before the cursor or after it. Finish the current statement or line and, when it is clearly the "
    "natural next step, continue with more lines (e.g. the body of the function being written), as "
    "many as the code genuinely needs, but keep it short (normally under about 15 lines). Match the file's indentation, naming and style; "
    "when the cursor line is only whitespace, that indentation is already in the file, but still write the first line of text with its COMPLETE "
    "indentation (as if the line started at column 1): the editor removes the part that is already there. Make "
    "bracket/quote usage consistent with the text after the cursor. In comments or prose, continue "
    "the sentence naturally. If nothing useful can be inserted, use an empty text.\n\n"
    "action 'replace': ONLY when code inside the EDITABLE LINES is clearly wrong or stale given what "
    "the user is doing: a typo in an identifier, a wrong operator, an old name left behind after a "
    "rename, a line that the user's last change made incorrect or redundant, a syntax error. "
    "start_line..end_line = the inclusive absolute line range to rewrite, as small as possible (a "
    "single line whenever you can); text = the complete new version of exactly those lines (no line "
    "numbers, no trailing newline, same indentation). It must differ from the original.\n\n"
    "action 'delete': ONLY when lines inside the EDITABLE LINES are clearly redundant, duplicated or "
    "dead, e.g. an accidental duplicate line. start_line..end_line = the lines to remove; text = an "
    "empty string.\n\n"
    "Be conservative: the line the cursor is on is usually still being typed, so complete it with "
    "'insert' and replace it only for an obvious mistake in text that is already complete. When in "
    "doubt use 'insert'. Never refactor, reformat or restyle code that works, and never touch lines "
    "outside the EDITABLE LINES."
    + _REPLY_FORMAT
)

_SYSTEM_INSTR = (
    "You are the inline code-editing engine of a code editor (like GitHub Copilot). The user "
    "message contains a file with a <|cursor|> marker, followed by the INSTRUCTION COMMENT (the "
    "comment lines at / directly above the cursor), the EDITABLE LINES (the code directly below the "
    "comment/cursor line, numbered with absolute line numbers - the 'N|' prefix is NOT part of the "
    "code; may be empty) and where the cursor is.\n\n"
    "Step 1 - decide is_instruction. It is true when the comment starts with 'ai:' (any case, any "
    "comment syntax) OR reads like a request addressed to a programmer, e.g. '// Add a method here "
    "to do X', '# make this async', '-- delete this query'. Ordinary comments that merely describe "
    "code are NOT instructions (is_instruction=false); for those, use action 'insert' with a normal "
    "completion in text, exactly like a plain code-completion engine would.\n\n"
    "Step 2 - when is_instruction is true, choose an action:\n"
    "* 'insert': the instruction asks for NEW code (add a method/function/test/field...). text = the "
    "code to insert at the cursor. If the cursor is at the end of the comment line, text must start "
    "with a newline. If the cursor is on an empty line that is already indented, that indentation is already in the file: still write the "
    "first line of text with its COMPLETE indentation (as if the line started at column 1) and indent every following line the same absolute "
    "way; the editor removes the part that is already there. Existing code is left untouched.\n"
    "* 'replace': the instruction asks to change, fix, rename, convert, optimize or otherwise rewrite "
    "the code below it. start_line..end_line = the inclusive absolute line range inside the EDITABLE "
    "LINES that must change - only the lines the instruction really touches, often just the single "
    "line right below the comment; text = the complete new version of exactly those lines (no line "
    "numbers, no trailing newline, same indentation).\n"
    "* 'delete': the instruction asks to remove the code below it. start_line..end_line = the lines "
    "to remove; text = an empty string.\n"
    "Edit ONLY lines inside the EDITABLE LINES, never any other code. 'replace' and 'delete' are only "
    "possible when there are EDITABLE LINES, otherwise use 'insert'. Never include the instruction "
    "comment itself in text, and never add explanatory prose or markdown fences. Use the identifiers, "
    "imports and helpers already in the file, and match its style and indentation. Instruction-driven "
    "code may be as long as the task needs, but stay focused on exactly what was asked."
    + _REPLY_FORMAT
)

_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_instruction": {"type": "BOOLEAN"},
        "action": {"type": "STRING", "enum": ["insert", "replace", "delete"]},
        "start_line": {"type": "INTEGER"},
        "end_line": {"type": "INTEGER"},
        "text": {"type": "STRING"},
    },
    "required": ["is_instruction", "action", "start_line", "end_line", "text"],
}

_use_thinking_cfg = True
_use_schema = True


def _numbered(edit, cursor):
    """EDITABLE LINES block: 'N| code' per line, with the cursor marker on the cursor line."""
    out = []
    for i, line in enumerate(edit["lines"]):
        n = edit["start"] + i
        if cursor and cursor["line"] == n:
            c = max(0, min(len(line), cursor["col"] - 1))
            line = line[:c] + CURSOR + line[c:]
        out.append(f"{n}| {line}")
    return "\n".join(out)


def _build_body(name, lang, prefix, suffix, instr, edit, cursor):
    gen = {"temperature": 0.2, "topP": 0.95, "candidateCount": 1}
    gen["maxOutputTokens"] = MAX_TOKENS_INSTR if instr else MAX_TOKENS_COMPLETE
    if _use_thinking_cfg:
        gen["thinkingConfig"] = {"thinkingLevel": "minimal"}
    if _use_schema:
        gen["responseMimeType"] = "application/json"
        gen["responseSchema"] = _SCHEMA
    text = f"File: {name or 'untitled'}\nLanguage: {lang or 'plaintext'}\n\n{prefix}{CURSOR}{suffix}"
    if instr:
        where = ("at the END of the instruction comment line" if instr["cursor"] == "end_of_comment_line"
                 else "on an empty line directly below the instruction comment")
        text += f"\n\n=== INSTRUCTION COMMENT ===\n{instr['comment']}"
        text += ("\n\n=== EDITABLE LINES ===\n" + _numbered(edit, cursor)) if edit else \
                "\n\n=== EDITABLE LINES ===\n(none - only 'insert' is possible)"
        text += f"\n\n=== CURSOR ===\nThe cursor is {where}."
        system = _SYSTEM_INSTR
    else:
        if edit:
            text += "\n\n=== EDITABLE LINES ===\n" + _numbered(edit, cursor)
        if cursor:
            text += f"\n\n=== CURSOR ===\nLine {cursor['line']}, column {cursor['col']}."
        system = _SYSTEM
    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": gen,
    }


class _ConnPool:
    """Keep-alive connections to the Gemini API. A fresh TCP + TLS handshake per request costs 150-500 ms,
    which is a big slice of the time a ghost-text suggestion takes."""
    MAX_IDLE = 8

    def __init__(self):
        self._lock = threading.Lock()
        self._idle = []           # [(scheme, netloc, connection)]

    def _take(self, scheme, netloc):
        with self._lock:
            for i, (s, n, c) in enumerate(self._idle):
                if s == scheme and n == netloc:
                    del self._idle[i]
                    return c
        return None

    def _give(self, scheme, netloc, conn):
        with self._lock:
            self._idle.append((scheme, netloc, conn))
            while len(self._idle) > self.MAX_IDLE:
                _, _, old = self._idle.pop(0)
                try:
                    old.close()
                except Exception:
                    pass

    def post(self, url, data, headers, timeout):
        """-> (status, text). Raises OSError / http.client.HTTPException on transport problems."""
        u = urllib.parse.urlsplit(url)
        scheme, netloc = u.scheme, u.netloc
        path = u.path + ("?" + u.query if u.query else "")
        for attempt in (0, 1):
            conn = self._take(scheme, netloc) if attempt == 0 else None
            reused = conn is not None
            if conn is None:
                cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
                conn = cls(netloc, timeout=timeout)
            try:
                conn.timeout = timeout
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
                conn.request("POST", path, body=data, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
            except (socket.timeout, TimeoutError):
                try:
                    conn.close()
                except Exception:
                    pass
                raise
            except (OSError, http.client.HTTPException):
                try:
                    conn.close()
                except Exception:
                    pass
                if reused:        # a kept-alive socket the server already closed: retry once on a fresh one
                    continue
                raise
            if resp.will_close:
                conn.close()
            else:
                self._give(scheme, netloc, conn)
            return resp.status, raw.decode("utf-8", "replace")
        raise OSError("connection failed")


_CONNS = _ConnPool()
_proxy_memo = {}                  # url -> (monotonic time, direct?)


def _direct(url):
    """True when urllib would connect without a proxy, so the keep-alive pool can be used. With a system / env
    proxy configured we fall back to urllib (which honours it) instead of silently bypassing the proxy."""
    now = time.monotonic()
    hit = _proxy_memo.get(url)
    if hit and now - hit[0] < 60:
        return hit[1]
    direct = True
    try:
        u = urllib.parse.urlsplit(url)
        if urllib.request.getproxies().get(u.scheme) and not urllib.request.proxy_bypass(u.hostname or ""):
            direct = False
    except Exception:
        direct = False
    _proxy_memo[url] = (now, direct)
    return direct


def _post_urllib(url, data, headers, timeout):
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _call(key, body, timeout=REQUEST_TIMEOUT):
    """One generateContent call with ``key`` -> raw model text. Raises _Hop / _Fatal."""
    global _use_thinking_cfg, _use_schema
    url = f"{_api_root()}/models/{INLINE_MODEL}:generateContent"
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    try:
        if _direct(url):
            code, text = _CONNS.post(url, data, headers, timeout)
        else:
            code, text = _post_urllib(url, data, headers, timeout)
    except (socket.timeout, TimeoutError):
        raise _Hop(5, "timed out")
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise _Hop(20, f"network: {getattr(e, 'reason', e)}")
    if code != 200:
        raw = text[:3000]
        gen = body.get("generationConfig", {})
        low = raw.lower()
        if code == 400 and "thinkingConfig" in gen and "thinking" in low:
            _use_thinking_cfg = False                         # this model rejects the setting: drop it for good
            gen.pop("thinkingConfig", None)
            return _call(key, body, timeout)
        if code == 400 and "responseSchema" in gen and ("schema" in low or "mime" in low):
            _use_schema = False                               # fall back to prompt-only JSON
            gen.pop("responseSchema", None)
            gen.pop("responseMimeType", None)
            return _call(key, body, timeout)
        raise _classify(code, raw)
    try:
        payload = json.loads(text)
    except ValueError:
        raise _Hop(10, "bad response")
    cands = payload.get("candidates") or []
    if not cands:
        return ""                                             # blocked / nothing to say
    parts = (cands[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


# ── output cleanup / validation ──────────────────────────────────────────────────────────
def _unfence(text):
    t = (text or "").replace("\r\n", "\n").replace(CURSOR, "")
    m = re.match(r"^\s*```[\w+.#-]*\n(.*?)(?:\n?```)?\s*$", t, re.S)
    return m.group(1) if m else t


def _drop_existing_indent(t, existing):
    """The cursor already sits after ``existing`` (whitespace only, typically the auto-indent the editor added after
    Enter). The model writes the first line with its full indentation, so inserting it as-is would indent that
    line twice (first line one level too far right while the following lines are fine). Drop what is already there."""
    lead = re.match(r"[ \t]*", t).group(0)
    if not lead:
        return t
    if lead.startswith(existing):
        return t[len(existing):]
    return t[len(lead):]                  # other whitespace style / shallower: the line keeps the indent already present


def _clean(text, prefix, suffix, max_lines=MAX_LINES):
    """Cleanup for an INSERTION: strip fences / echoed context; '' when nothing useful is left."""
    t = _unfence(text)
    line_prefix = prefix.rsplit("\n", 1)[-1]
    if not line_prefix.strip():
        t = t.lstrip("\n")
        if line_prefix:
            t = _drop_existing_indent(t, line_prefix)
    elif t.startswith(line_prefix):                           # model re-typed the start of the line
        t = t[len(line_prefix):]
    first_suffix_line = suffix.split("\n", 1)[0]              # closer already after the cursor
    for k in range(min(len(t), len(first_suffix_line), 80), 0, -1):
        if first_suffix_line[:k].strip() and t.endswith(first_suffix_line[:k]):
            t = t[:-k]
            break
    lines = t.split("\n")
    if max_lines and len(lines) > max_lines:
        t = "\n".join(lines[:max_lines])
    t = t.rstrip()
    return t if t.strip() else ""


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _parse(raw):
    """-> (is_instruction, action, start, end, text) or None when the reply is not usable JSON."""
    s = (raw or "").strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.S)
    if m:
        s = m.group(1)
    try:
        j = json.loads(s)
    except ValueError:
        return None
    if not isinstance(j, dict):
        return None
    action = j.get("action")
    if action not in ("insert", "replace", "delete"):
        action = "insert"
    return bool(j.get("is_instruction")), action, _as_int(j.get("start_line")), _as_int(j.get("end_line")), str(j.get("text") or "")


def _finish(raw, prefix, suffix, instr, edit):
    """Raw model text -> the cacheable result dict {action, is_instruction, text[, start, end]}."""
    nothing = {"action": "insert", "is_instruction": False, "text": ""}
    parsed = _parse(raw)
    if parsed is None:
        if raw.lstrip().startswith("{") and '"action"' in raw:      # truncated / broken JSON: never insert it
            return nothing
        return {**nothing, "text": _clean(raw, prefix, suffix)}     # model ignored the format: plain completion
    is_instr, action, s, e, text = parsed
    is_instr = is_instr and bool(instr)                             # only meaningful next to a comment
    if instr and not is_instr:
        action = "insert"                                           # ordinary comment: plain completion only
    if action == "insert":
        return {"action": "insert", "is_instruction": is_instr, "text": _clean(text, prefix, suffix)}
    # replace / delete: only inside the editable lines the page sent
    if not edit:
        return {**nothing, "is_instruction": is_instr}
    first, last = edit["start"], edit["start"] + len(edit["lines"]) - 1
    if not (first <= s <= e <= last):
        return {**nothing, "is_instruction": is_instr}
    old = edit["lines"][s - first: e - first + 1]
    new = _unfence(text).rstrip() if action == "replace" else ""
    if action == "replace" and not new.strip():
        action = "delete"
    if action == "replace" and [x.rstrip() for x in new.split("\n")] == [x.rstrip() for x in old]:
        return {**nothing, "is_instruction": is_instr}               # no actual change
    return {"action": action, "is_instruction": is_instr, "text": new if action == "replace" else "", "start": s, "end": e}


# ── public API ───────────────────────────────────────────────────────────────────────────
_cache = OrderedDict()
_cache_lock = threading.Lock()
_sem = threading.BoundedSemaphore(MAX_CONCURRENT)


def _norm_instr(raw):
    if not isinstance(raw, dict):
        return None
    return {"comment": str(raw.get("comment") or "")[:3000],
            "cursor": "end_of_comment_line" if raw.get("cursor") == "end_of_comment_line" else "blank_line_below_comment"}


def _norm_edit(raw):
    """{'start': int >= 1, 'lines': [str]} (the editable window) or None."""
    if not isinstance(raw, dict) or not isinstance(raw.get("lines"), list):
        return None
    start = _as_int(raw.get("start"))
    lines = [str(x).replace("\r", "") for x in raw["lines"][:MAX_EDIT_LINES]]
    if start < 1 or not lines or any(len(x) > MAX_EDIT_LINE_CHARS for x in lines) or not any(x.strip() for x in lines):
        return None
    return {"start": start, "lines": lines}


def _norm_cursor(raw):
    if not isinstance(raw, dict):
        return None
    line, col = _as_int(raw.get("line")), _as_int(raw.get("col"))
    return {"line": line, "col": col} if line >= 1 and col >= 1 else None


def suggest(req):
    t0 = time.time()
    prefix = str(req.get("prefix") or "").replace("\r\n", "\n")[-MAX_PREFIX_CHARS:]
    suffix = str(req.get("suffix") or "").replace("\r\n", "\n")[:MAX_SUFFIX_CHARS]
    lang, name = str(req.get("lang") or "")[:40], os.path.basename(str(req.get("name") or ""))[:120]
    instr, edit, cursor = _norm_instr(req.get("instr")), _norm_edit(req.get("edit")), _norm_cursor(req.get("cursor"))
    if len(prefix.strip()) < 2 and not suffix.strip():
        return {"ok": True, "text": "", "action": "insert", "is_instruction": False}
    ck_src = f"{lang}\0{prefix[-1500:]}\0{suffix[:500]}"
    if edit:
        ck_src += f"\0{edit['start']}\0" + "\n".join(edit["lines"])
    if instr:
        ck_src += f"\0{instr['cursor']}\0{instr['comment']}"
    ck = hashlib.sha1(ck_src.encode("utf-8", "replace")).hexdigest()
    with _cache_lock:
        if ck in _cache:
            _cache.move_to_end(ck)
            return {"ok": True, **_cache[ck], "cached": True}
    if not _sem.acquire(blocking=False):
        return {"ok": False, "busy": True, "error": "busy"}
    try:
        body = _build_body(name, lang, prefix, suffix, instr, edit, cursor)
        timeout = REQUEST_TIMEOUT_INSTR if instr else REQUEST_TIMEOUT
        tried, last = set(), ""
        for _ in range(max(1, POOL.count())):
            got = POOL.acquire(tried)
            if not got:
                break
            pos, key = got
            tried.add(key)
            try:
                raw = _call(key, body, timeout)
            except _Hop as h:
                POOL.penalize(key, h.cooldown, h.msg)
                last = h.msg
                print(f"   [Inline AI] key #{pos} {h.msg} (cooling {h.cooldown:.0f}s) - hopping to the next key")
                continue
            except _Fatal as f:
                return {"ok": False, "error": f.msg}
            res = _finish(raw, prefix, suffix, instr, edit)
            with _cache_lock:
                _cache[ck] = res
                while len(_cache) > CACHE_SIZE:
                    _cache.popitem(last=False)
            return {"ok": True, **res, "key": pos, "keys": POOL.count(), "ms": int((time.time() - t0) * 1000)}
        snap = POOL.snapshot()
        if snap["total"] == 0:
            return {"ok": False, "error": "no_keys", **snap}
        return {"ok": False, "error": last or "all keys are cooling down", "cooling": True, **snap}
    finally:
        _sem.release()


def handle(req):
    """Entry point used by the editor's HTTP handler. Never raises."""
    try:
        if req.get("op") == "status":
            return {"ok": True, "model": INLINE_MODEL, **POOL.snapshot()}
        return suggest(req)
    except Exception as e:                                    # noqa: BLE001 - the editor must never see a 500
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
