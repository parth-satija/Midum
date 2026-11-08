# providers/codex_backend.py
# =============================================================================
# CODEX PROVIDER — stateless, one disposable thread per Midum model call
# =============================================================================
#
# Implements MODEL_PROVIDER == "codex": OpenAI's Codex, driven through the
# `codex app-server` JSON-RPC process (stdio, one JSON object per line) using
# the user's ChatGPT-account sign-in. No API key, no scraping.
#
# DESIGN (see the plan this was built from)
#   * Every Midum model call == ONE ephemeral Codex thread. Midum's `messages`
#     list stays the only source of truth, so context_summarizer, tab
#     switching, chat restore, regenerate and sub-agents work unchanged.
#   * The backend: thread/start (dynamic tools + Midum's system prompt as
#     developerInstructions) -> thread/inject_items (the replayed history) ->
#     turn/start (the final user message) -> watch the stream -> on the FIRST
#     dynamic tool call, interrupt the turn and hand the call back to Midum;
#     on a plain answer, return it; then discard the thread.
#   * Contract with orchestration is identical to every other provider:
#     result_q.put(("ok", {"message": {"role", "content", "tool_calls"}})).
#
# SAFETY STACK (Codex normally ships a shell, patch tool, web search, ... which
# must NEVER run on the user's machine behind Midum's own permission system):
#   1. every native capability switched off with `-c` overrides
#      (config.CODEX_DISABLE_FLAGS)
#   2. an empty scratch working directory
#   3. read-only sandbox + approval policy "untrusted"
#   4. every approval request is auto-DECLINED
#   5. LEAK TRIPWIRE: any non-dynamic item (commandExecution, fileChange,
#      mcpToolCall, webSearch, ...) interrupts the turn and fails the step
#   6. pinned Codex version (config.CODEX_PINNED_VERSION), fail closed
#   7. a one-off startup 'list your tools' check, fail closed
#
# The app-server protocol (method / field names) is marked experimental by
# OpenAI. Names used here follow `codex app-server generate-json-schema`
# for the Codex build the plan was written against; run tools/codex_spike.py
# (Phase 0) after every Codex upgrade to confirm them.
# =============================================================================

import atexit
import collections
import itertools
import json
import os
import queue
import random
import re
import shutil
import subprocess
import threading
import time
import webbrowser

import config
from state import _abort_event


# ── errors ────────────────────────────────────────────────────────────────────
class CodexError(RuntimeError):
    """Base class. Plain CodexError is NOT retried."""

class CodexTransient(CodexError):
    """Worth retrying with backoff (timeout, dropped stream, overload, crash)."""

class CodexUsageLimit(CodexError):
    """The account's Codex allowance is used up. Never retried; triggers the fallback provider."""

class CodexUnavailable(CodexError):
    """Not installed / not signed in / failed the safety checks. Never retried."""

class CodexLeak(CodexError):
    """Codex tried to use one of ITS OWN tools (the tripwire fired). Never retried."""

class CodexModelRejected(CodexError):
    """The requested model isn't available on this account; try the next fallback model."""

class CodexAborted(CodexError):
    """The user aborted (Ctrl+Q) mid-step."""


_USAGE_LIMIT_RE = re.compile(
    r"usage limit|usagelimit|hit your .{0,40}limit|upgrade to (plus|pro)|"
    r"insufficient[_ ]quota|exceeded .{0,30}(quota|allowance)|limit.{0,30}resets?", re.I)
_AUTH_RE = re.compile(
    r"unauthori[sz]ed|not signed in|log ?in required|requires? .{0,20}auth|"
    r"token .{0,20}(expired|invalid)|\b401\b", re.I)
_MODEL_RE = re.compile(
    r"model .{0,60}(not found|unsupported|not supported|not available|does not exist|unknown)|"
    r"unknown model|invalid model", re.I)
_TRANSIENT_RE = re.compile(
    r"timeout|timed out|connection|reset by peer|broken pipe|\b50[234]\b|\b429\b|overloaded|"
    r"stream (closed|disconnected|ended)|temporar|try again|empty response|not running|app-server exited", re.I)


def _classify(text: str, where: str = "") -> CodexError:
    """Map an app-server error message onto the right exception class."""
    text = str(text or "").strip() or "unknown error"
    msg = f"{where}: {text}" if where else text
    if _USAGE_LIMIT_RE.search(text):
        return CodexUsageLimit(msg)
    if _AUTH_RE.search(text):
        return CodexUnavailable(
            f"Codex is not signed in (or the sign-in expired): {text}. "
            "Open the Model tab -> Codex -> Connect, or run `codex login`.")
    if _MODEL_RE.search(text):
        return CodexModelRejected(msg)
    if _TRANSIENT_RE.search(text):
        return CodexTransient(msg)
    return CodexError(msg)


def _dig(obj, *keys, default=None):
    for k in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(k)
        if obj is None:
            return default
    return obj


def _norm_type(t) -> str:
    return re.sub(r"[^a-z]", "", str(t or "").lower())


# Item types a stateless Midum step is allowed to produce. Everything else is a
# native Codex capability (commandExecution, fileChange, mcpToolCall, webSearch,
# imageView, imageGeneration, ...) and trips the leak alarm.
_ALLOWED_ITEM_TYPES = {
    "usermessage", "agentmessage", "reasoning", "dynamictoolcall",
    "contextcompaction", "plan",
}


# ── paths / binary ────────────────────────────────────────────────────────────
def _scratch_dir() -> str:
    d = (getattr(config, "CODEX_SCRATCH_DIR", "") or "").strip() \
        or os.path.join(config.STORAGE_DIR, "codex_scratch")
    os.makedirs(d, exist_ok=True)
    return d


def _resolve_bin() -> str:
    want = (config.CODEX_BIN or "codex").strip()
    found = shutil.which(want) or (want if os.path.isfile(want) else None)
    if not found:
        raise CodexUnavailable(
            f"Codex CLI not found ('{want}'). Install it (npm i -g @openai/codex), "
            "then sign in with `codex login`, or set CODEX_BIN in config.py.")
    return found


def _read_version(exe: str) -> str:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=20, encoding="utf-8", errors="replace")
        return (out.stdout or out.stderr or "").strip()
    except Exception as e:
        raise CodexUnavailable(f"Could not run `{exe} --version`: {e}")


# ── the app-server process / JSON-RPC client ─────────────────────────────────
class _AppServer:
    """One long-lived `codex app-server` stdio process shared by every thread
    (so sub-agents / delegation can run Codex steps in parallel). Restarts
    itself after a crash on the next use."""

    def __init__(self):
        self._proc = None
        self._lock = threading.RLock()          # guards the maps below (short sections only)
        self._start_lock = threading.RLock()    # serialises spawn / startup checks
        self._write_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending = {}                      # request id -> Queue
        self._threads = {}                      # threadId   -> Queue of events
        self._early = {}                        # threadId   -> events that beat register
        self._stderr = collections.deque(maxlen=60)
        self.version = ""
        self._signed_in = False
        self._startup_ok = False
        self.account = {}
        self.login_event = threading.Event()
        self.login_result = {}

    # ---- lifecycle ----
    def alive(self) -> bool:
        p = self._proc
        return p is not None and p.poll() is None

    def start(self):
        with self._start_lock:
            if self.alive():
                return
            self._teardown("restarting")
            exe = _resolve_bin()
            self.version = _read_version(exe)
            pinned = (config.CODEX_PINNED_VERSION or "").strip()
            if pinned and pinned not in self.version:
                raise CodexUnavailable(
                    f"Codex version mismatch: found '{self.version}', config.CODEX_PINNED_VERSION "
                    f"is '{pinned}'. Re-run tools/codex_spike.py on the new version, then update the pin.")
            cmd = [exe]
            for flag in (config.CODEX_DISABLE_FLAGS or []):
                cmd += ["-c", flag]
            cmd += ["app-server"]
            kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace", bufsize=1,
                          cwd=_scratch_dir())
            if os.name == "nt":
                kwargs["creationflags"] = 0x08000000   # CREATE_NO_WINDOW
            try:
                proc = subprocess.Popen(cmd, **kwargs)
            except Exception as e:
                raise CodexUnavailable(f"Could not start `codex app-server`: {e}")
            self._proc = proc
            self._signed_in = False
            self._startup_ok = False
            threading.Thread(target=self._read_loop, args=(proc,), daemon=True, name="codex-reader").start()
            threading.Thread(target=self._err_loop, args=(proc,), daemon=True, name="codex-stderr").start()
            try:
                self.request("initialize", {
                    "clientInfo": {"name": "midum", "title": "Midum", "version": "1.0"},
                    "capabilities": {"experimentalApi": True},
                }, timeout=config.CODEX_STARTUP_TIMEOUT)
                self.notify("initialized", {})
            except Exception:
                self._teardown("initialize failed")
                raise
            print(f"   [Codex] app-server started ({self.version})")

    def _teardown(self, why: str):
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        self._fail_all(why)

    def stop(self):
        with self._start_lock:
            self._teardown("shutdown")

    def _fail_all(self, why: str):
        with self._lock:
            pend, self._pending = self._pending, {}
            threads = list(self._threads.values())
        for q in pend.values():
            q.put(("crash", why))
        for q in threads:
            q.put(("crash", f"app-server exited ({why}): {self.stderr_tail()}"))

    def stderr_tail(self) -> str:
        return " | ".join(list(self._stderr)[-4:])[:400]

    # ---- wire ----
    def _send(self, obj: dict):
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            raise CodexTransient("codex app-server is not running")
        line = json.dumps(obj, ensure_ascii=False)
        with self._write_lock:
            try:
                proc.stdin.write(line + "\n")
                proc.stdin.flush()
            except Exception as e:
                raise CodexTransient(f"codex app-server pipe broke: {e}")

    def request(self, method: str, params: dict = None, timeout: float = None):
        rid = next(self._ids)
        q = queue.Queue(1)
        with self._lock:
            self._pending[rid] = q
        self._send({"id": rid, "method": method, "params": params if params is not None else {}})
        try:
            kind, val = q.get(timeout=timeout or config.CODEX_REQUEST_TIMEOUT)
        except queue.Empty:
            with self._lock:
                self._pending.pop(rid, None)
            raise CodexTransient(f"{method} timed out")
        if kind == "crash":
            raise CodexTransient(f"{method}: app-server exited ({val}) {self.stderr_tail()}")
        if kind == "error":
            msg = (val or {}).get("message") if isinstance(val, dict) else str(val)
            raise _classify(msg, method)
        return val if val is not None else {}

    def notify(self, method: str, params: dict = None):
        self._send({"method": method, "params": params if params is not None else {}})

    def respond(self, rid, result=None, error: str = None):
        try:
            if error is not None:
                self._send({"id": rid, "error": {"code": -32601, "message": error}})
            else:
                self._send({"id": rid, "result": result if result is not None else {}})
        except CodexError:
            pass

    # ---- reader threads ----
    def _err_loop(self, proc):
        try:
            for line in proc.stderr:
                line = (line or "").strip()
                if line:
                    self._stderr.append(line[:300])
        except Exception:
            pass

    def _read_loop(self, proc):
        try:
            for line in proc.stdout:
                line = (line or "").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg)
        except Exception:
            pass
        finally:
            if proc is self._proc:
                self._proc = None
            self._fail_all("process ended")

    @staticmethod
    def _thread_id_of(params: dict):
        if not isinstance(params, dict):
            return None
        return (params.get("threadId")
                or _dig(params, "thread", "id")
                or _dig(params, "turn", "threadId")
                or _dig(params, "item", "threadId"))

    def _route(self, tid, event):
        if not tid:
            return
        with self._lock:
            q = self._threads.get(tid)
            if q is None:
                buf = self._early.setdefault(tid, [])
                if len(buf) < 500:
                    buf.append(event)
                if len(self._early) > 64:               # never grow without bound
                    self._early.pop(next(iter(self._early)), None)
                return
        q.put(event)

    def _dispatch(self, msg: dict):
        method = msg.get("method")
        if method is not None:
            params = msg.get("params") or {}
            if "id" in msg:
                self._on_server_request(msg["id"], method, params)
            else:
                self._on_notification(method, params)
            return
        if "id" in msg:
            with self._lock:
                q = self._pending.pop(msg["id"], None)
            if q is not None:
                q.put(("error", msg["error"]) if msg.get("error") is not None
                      else ("ok", msg.get("result")))

    def _on_server_request(self, rid, method: str, params: dict):
        tid = self._thread_id_of(params)
        low = method.lower()
        if low == "item/tool/call":
            self._route(tid, ("tool_call", rid, params))
            return
        # Approval / elicitation / user-input requests: auto-DECLINE, and report
        # it as a leak (Codex only asks when it is trying to use a native tool).
        if "approval" in low or "elicitation" in low or "userinput" in low:
            self.respond(rid, {"decision": "decline"})
            self._route(tid, ("leak", f"Codex asked to run something itself ({method}) - declined"))
            return
        self.respond(rid, error=f"Method not supported by Midum: {method}")

    def _on_notification(self, method: str, params: dict):
        low = method.lower()
        if low == "account/login/completed":
            self.login_result = params
            self.login_event.set()
            return
        tid = self._thread_id_of(params)
        if tid:
            self._route(tid, ("notif", method, params))

    # ---- per-thread event queues ----
    def open_thread_queue(self, tid: str) -> "queue.Queue":
        q = queue.Queue()
        with self._lock:
            self._threads[tid] = q
            for ev in self._early.pop(tid, []):
                q.put(ev)
        return q

    def close_thread_queue(self, tid: str):
        with self._lock:
            self._threads.pop(tid, None)
            self._early.pop(tid, None)

    # ---- readiness: spawn + signed in + startup tools check ----
    def ensure_ready(self):
        self.start()
        with self._start_lock:
            if not self._signed_in:
                res = self.request("account/read", {"refreshToken": False})
                account = res.get("account") if isinstance(res, dict) else None
                if not account:
                    raise CodexUnavailable(
                        "Not signed in to Codex. Open the Model tab -> Codex -> Connect, "
                        "or run `codex login` in a terminal, then try again.")
                self.account = account
                self._signed_in = True
            if config.CODEX_STARTUP_CHECK and not self._startup_ok:
                _startup_check(self)
                self._startup_ok = True


_server = None
_server_lock = threading.Lock()


def _get_server() -> _AppServer:
    global _server
    with _server_lock:
        if _server is None:
            _server = _AppServer()
        return _server


def shutdown_codex():
    """Stop the app-server (registered with atexit)."""
    global _server
    srv, _server = _server, None
    if srv is not None:
        try:
            srv.stop()
        except Exception:
            pass


atexit.register(shutdown_codex)


# ── Midum messages -> Codex input ────────────────────────────────────────────
def _text_of(content) -> str:
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") in (None, "text"))
    return str(content or "")


def _image_uri(img) -> str:
    """Midum carries images as base64 strings (or dicts); Codex wants a URL / data URI."""
    if isinstance(img, dict):
        img = img.get("url") or img.get("data_b64") or img.get("data") or img.get("b64") or ""
    img = str(img or "").strip()
    if not img:
        return ""
    if img.startswith(("data:", "http://", "https://")):
        return img
    mime = "image/jpeg" if img.startswith("/9j/") else "image/png"
    return f"data:{mime};base64,{img}"


def _message_item(role: str, text: str, images=None) -> dict:
    if role == "assistant":
        return {"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": text}]}
    content = []
    if text:
        content.append({"type": "input_text", "text": text})
    for img in (images or []):
        uri = _image_uri(img)
        if uri:
            content.append({"type": "input_image", "image_url": uri})
    return {"type": "message", "role": "user", "content": content}


def messages_to_codex_input(messages: list, mode: str = None, rename: dict = None):
    """
    Convert Midum's OpenAI/Ollama-shaped history into
        (system_text, history_items, final_input)

      system_text    every system message, joined -> developerInstructions
      history_items  Responses-API items for thread/inject_items
      final_input    the LAST user message, as turn/start `input`

    Midum's history always ends in a user message (the request or the step
    prompt); if it somehow doesn't, a neutral "Continue." turn is appended.

    Tool pairing: Midum emits exactly ONE tool call per assistant step, and the
    next 'tool' message answers it, so pairing by order is exact. Call ids are
    synthesised (Midum doesn't store them). mode == "text" renders tool
    history as plain text turns instead of function_call items (the Phase-0
    fallback if the app-server rejects injected tool items).
    """
    mode = (mode or config.CODEX_HISTORY_MODE or "items").lower()
    rename = rename or {}
    sys_parts, seq = [], []
    for m in messages:
        if m.get("role") == "system":
            t = _text_of(m.get("content")).strip()
            if t:
                sys_parts.append(t)
        else:
            seq.append(m)

    if not seq or seq[-1].get("role") != "user":
        seq.append({"role": "user", "content": "Continue."})
    head, last = seq[:-1], seq[-1]

    items, pending, counter = [], None, 0

    def flush():
        nonlocal pending
        if pending is not None and mode == "items":
            items.append({"type": "function_call_output", "call_id": pending,
                          "output": "[no result was recorded for this call]"})
        pending = None

    for m in head:
        role = m.get("role")
        text = _text_of(m.get("content")).strip()
        if role == "user":
            flush()
            if text or m.get("images"):
                items.append(_message_item("user", text, m.get("images")))
        elif role == "assistant":
            flush()
            if text:
                items.append(_message_item("assistant", text))
            calls = m.get("tool_calls") or []
            if calls:
                fn = (calls[0] or {}).get("function") or {}
                name = fn.get("name", "") or "tool"
                name = rename.get(name, name)
                args = fn.get("arguments", {})
                args_s = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
                if mode == "items":
                    cid = f"midum_call_{counter}"
                    counter += 1
                    items.append({"type": "function_call", "name": name,
                                  "arguments": args_s, "call_id": cid})
                    pending = cid
                else:
                    items.append(_message_item("assistant", f"[tool call] {name}({args_s})"))
                    pending = "text"
        elif role == "tool":
            if pending is not None and mode == "items":
                items.append({"type": "function_call_output", "call_id": pending, "output": text})
            else:
                items.append(_message_item("user", f"[tool result] {text}"))
            pending = None
            if m.get("images"):
                items.append(_message_item("user", "(image returned by the tool above)", m.get("images")))
    flush()

    final_text = _text_of(last.get("content")).strip() or "Continue."
    final_input = [{"type": "text", "text": final_text, "text_elements": []}]
    for img in (last.get("images") or []):
        uri = _image_uri(img)
        if uri:
            final_input.append({"type": "image", "url": uri})
    return "\n\n".join(sys_parts), items, final_input


_NAME_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _to_dynamic_tools(tools_schema: list):
    """
    OpenAI-style tool schemas -> Codex dynamic tool definitions. ORDER AND
    CONTENT ARE KEPT STABLE across steps so the prompt prefix stays cacheable.
    Returns (definitions, name_map: codex_name -> midum_name, rename: midum_name -> codex_name).
    """
    defs, name_map, rename, used = [], {}, {}, set()
    for t in (tools_schema or []):
        fn = (t or {}).get("function") or {}
        orig = fn.get("name") or ""
        if not orig:
            continue
        name = orig if _NAME_OK.match(orig) else (re.sub(r"[^A-Za-z0-9_-]", "_", orig)[:64] or "tool")
        base, n = name, 2
        while name in used:
            name = f"{base[:60]}_{n}"
            n += 1
        used.add(name)
        name_map[name] = orig
        rename[orig] = name
        defs.append({
            "name": name,
            "description": fn.get("description") or "",
            "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return defs, name_map, rename


# ── usage accounting ─────────────────────────────────────────────────────────
_USAGE_LOG = collections.deque(maxlen=500)


def get_codex_usage_log() -> list:
    """Per-step token usage ({'input','cached','output','reasoning','total', ...}). Used by the cost test."""
    return list(_USAGE_LOG)


def _parse_usage(params: dict) -> dict:
    tu = (params or {}).get("tokenUsage") or (params or {}).get("usage") or {}
    b = tu.get("total") or tu.get("last") or tu
    if not isinstance(b, dict):
        return {}

    def g(*keys):
        for k in keys:
            v = b.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    return {"input": g("inputTokens", "input_tokens"),
            "cached": g("cachedInputTokens", "cached_input_tokens"),
            "output": g("outputTokens", "output_tokens"),
            "reasoning": g("reasoningOutputTokens", "reasoning_output_tokens"),
            "total": g("totalTokens", "total_tokens")}


# ── one turn on one thread ───────────────────────────────────────────────────
def _drop_thread(srv: _AppServer, tid: str):
    def _go():
        try:
            method = "thread/unsubscribe" if config.CODEX_EPHEMERAL else "thread/archive"
            srv.request(method, {"threadId": tid}, timeout=10)
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True, name="codex-drop").start()


def _interrupt_and_drain(srv: _AppServer, tid: str, turn_id, q, usage_box: dict, wait: float = 10.0):
    """turn/interrupt, then swallow events until turn/completed (so the turn
    ends cleanly and its token usage is still recorded)."""
    try:
        srv.request("turn/interrupt", {"threadId": tid, **({"turnId": turn_id} if turn_id else {})}, timeout=8)
    except CodexError:
        pass
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            ev = q.get(timeout=0.25)
        except queue.Empty:
            continue
        if ev[0] == "crash":
            return
        if ev[0] == "notif":
            low = ev[1].lower()
            if low == "thread/tokenusage/updated":
                usage_box.update(_parse_usage(ev[2]))
            elif low == "turn/completed":
                return


def _run_turn(srv: _AppServer, *, system_text, history_items, final_input, model, effort,
              dyn_tools, timeout=None) -> dict:
    """
    Run ONE step on a fresh thread. Stops at the first dynamic tool call
    (interrupting the turn), or at a finished plain answer. Returns
        {"text": str, "call": (name, args_dict) | None, "usage": {...}}
    Raises CodexLeak if Codex tries anything but a Midum dynamic tool.
    """
    timeout = timeout or config.CODEX_STEP_TIMEOUT
    started = time.time()

    params = {
        "cwd": _scratch_dir(),
        "ephemeral": bool(config.CODEX_EPHEMERAL),
        "personality": "none",
        "developerInstructions": system_text or "",
        "sandbox": config.CODEX_SANDBOX or "read-only",
        "approvalPolicy": "untrusted",
        "dynamicTools": dyn_tools,
    }
    if model:
        params["model"] = model
    res = srv.request("thread/start", params)
    tid = _dig(res, "thread", "id") or (res or {}).get("threadId")
    if not tid:
        raise CodexError(f"thread/start returned no thread id: {str(res)[:200]}")

    q = srv.open_thread_queue(tid)
    text_parts, delta_parts = [], []
    call, leak, err, turn_id = None, None, None, None
    usage = {}
    completed = False
    try:
        if history_items:
            srv.request("thread/inject_items", {"threadId": tid, "items": history_items})
        turn_params = {"threadId": tid, "input": final_input}
        if effort and effort != "default":
            turn_params["effort"] = effort
        if model:
            turn_params["model"] = model
        ts = srv.request("turn/start", turn_params)
        turn_id = _dig(ts, "turn", "id") or (ts or {}).get("turnId")

        last_event = time.time()
        pending_call_rid = None
        while not completed:
            if _abort_event.is_set():
                _interrupt_and_drain(srv, tid, turn_id, q, usage, wait=4)
                raise CodexAborted("aborted by user")
            now = time.time()
            if now - started > timeout:
                _interrupt_and_drain(srv, tid, turn_id, q, usage, wait=4)
                raise CodexTransient(f"step timed out after {int(timeout)}s")
            if now - last_event > config.CODEX_IDLE_TIMEOUT:
                _interrupt_and_drain(srv, tid, turn_id, q, usage, wait=4)
                raise CodexTransient(f"stream idle for {int(config.CODEX_IDLE_TIMEOUT)}s")
            try:
                ev = q.get(timeout=0.2)
            except queue.Empty:
                continue
            last_event = time.time()
            kind = ev[0]

            if kind == "crash":
                raise CodexTransient(ev[1])

            if kind == "leak":
                leak = ev[1]
                break

            if kind == "tool_call":
                _, rid, p = ev
                args = p.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except Exception:
                        args = {"_raw": args}
                call = (p.get("tool") or p.get("name") or "", args if isinstance(args, dict) else {})
                pending_call_rid = rid
                break

            if kind != "notif":
                continue
            method, p = ev[1], ev[2]
            low = method.lower()

            if low == "turn/started":
                turn_id = turn_id or _dig(p, "turn", "id")
            elif low in ("item/started", "item/completed"):
                item = p.get("item") or {}
                typ = _norm_type(item.get("type"))
                if typ and typ not in _ALLOWED_ITEM_TYPES:
                    leak = f"Codex tried to use its own '{item.get('type')}' (item {item.get('id', '?')})"
                    break
                if typ == "dynamictoolcall" and call is None:
                    nm = item.get("tool") or item.get("name")
                    if nm:
                        a = item.get("arguments", {})
                        if isinstance(a, str):
                            try:
                                a = json.loads(a) if a.strip() else {}
                            except Exception:
                                a = {"_raw": a}
                        call = (nm, a if isinstance(a, dict) else {})
                        break
                if typ == "agentmessage" and low == "item/completed":
                    text_parts.append(item.get("text") or "")
            elif low == "item/agentmessage/delta":
                delta_parts.append(p.get("delta") or "")
            elif low == "thread/tokenusage/updated":
                usage = _parse_usage(p)
            elif low == "turn/completed":
                turn = p.get("turn") or {}
                if str(turn.get("status", "")).lower() == "failed":
                    e = turn.get("error") or {}
                    err = (e.get("message") if isinstance(e, dict) else str(e)) or "turn failed"
                completed = True
            elif low == "error":
                if p.get("willRetry"):
                    continue
                e = p.get("error") or p
                err = (e.get("message") if isinstance(e, dict) else str(e)) or "error"
                completed = True

        # Stop the turn if we left the loop before it completed on its own.
        if not completed:
            _interrupt_and_drain(srv, tid, turn_id, q, usage)
            if call is not None and pending_call_rid is not None:
                srv.respond(pending_call_rid, {"success": False, "contentItems": [
                    {"type": "inputText", "text": "Midum is executing this call."}]})
    finally:
        srv.close_thread_queue(tid)
        _drop_thread(srv, tid)

    if usage:
        entry = dict(usage, ts=time.time(), model=model or "(default)",
                     secs=round(time.time() - started, 2))
        _USAGE_LOG.append(entry)
        print(f"   [Codex] tokens in={usage.get('input', 0)} cached={usage.get('cached', 0)} "
              f"out={usage.get('output', 0)} ({entry['secs']}s)")

    if leak:
        raise CodexLeak(f"{leak}. The step was interrupted and nothing from Codex's own tools ran.")
    if err:
        raise _classify(err, "turn")

    text = "\n\n".join(t.strip() for t in text_parts if t and t.strip()) or "".join(delta_parts).strip()
    if call is None and not text:
        raise CodexTransient("Codex returned an empty response")
    return {"text": text, "call": call, "usage": usage}


# ── startup check (fails closed) ─────────────────────────────────────────────
_NATIVE_TOOL_WORDS = {
    "shell", "exec_command", "local_shell", "unified_exec", "apply_patch", "web_search",
    "view_image", "image_generation", "update_plan", "read_file", "list_dir", "grep_files",
    "js_repl", "write_stdin", "spawn_agent", "request_user_input",
}


def _startup_check(srv: _AppServer):
    """One 'list your tools' turn with a single placeholder dynamic tool. If a
    leak fires, or the answer names anything but that placeholder, refuse to
    run (fail closed) rather than risk Codex's native tools reaching the machine."""
    probe = "midum_probe"
    dyn = [{"name": probe, "inputSchema": {"type": "object", "properties": {}},
            "description": "Placeholder used only by Midum's startup safety check. Never call it."}]
    try:
        r = _run_turn(
            srv,
            system_text="You are a diagnostic. Never call any tool.",
            history_items=[],
            final_input=[{"type": "text", "text_elements": [], "text":
                          "List the exact names of every tool you are able to call, as a "
                          "comma-separated list and nothing else. If you have none, reply NONE. "
                          "Do not call any tool."}],
            model=config.CODEX_MODEL, effort=config.CODEX_REASONING_EFFORT, dyn_tools=dyn,
            timeout=90)
    except CodexLeak as e:
        raise CodexUnavailable(f"Codex startup safety check FAILED (leak): {e}")
    if r["call"] and r["call"][0] != probe:
        raise CodexUnavailable(f"Codex startup safety check FAILED: it tried to call '{r['call'][0]}'.")
    tokens = {t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_.:-]*", r["text"] or "")}
    bad = sorted(t for t in tokens if t in _NATIVE_TOOL_WORDS or (t.startswith("mcp__") and t != probe))
    if bad:
        raise CodexUnavailable(
            f"Codex startup safety check FAILED: the model reports native tools {bad}. "
            "Check config.CODEX_DISABLE_FLAGS against this Codex version (tools/codex_spike.py).")
    print("   [Codex] startup tools check passed (only Midum's tools are visible).")


def codex_startup_check() -> str:
    """Run the startup check on demand (used by the spike / GUI). Returns 'ok' or raises."""
    srv = _get_server()
    srv.start()
    srv._startup_ok = False
    srv.ensure_ready()
    return "ok"


# ── one Midum step, with retries / model fallback ────────────────────────────
_USAGE_LIMITED_UNTIL = 0.0
_USAGE_COOLDOWN_S = 120


def _step_once(messages, model, tools_schema, effort, timeout):
    srv = _get_server()
    srv.ensure_ready()
    dyn, name_map, rename = _to_dynamic_tools(tools_schema)
    system_text, items, final_input = messages_to_codex_input(messages, config.CODEX_HISTORY_MODE, rename)
    r = _run_turn(srv, system_text=system_text, history_items=items, final_input=final_input,
                  model=model, effort=effort, dyn_tools=dyn, timeout=timeout)
    msg = {"role": "assistant", "content": r["text"], "tool_calls": []}
    if r["call"]:
        name, args = r["call"]
        msg["tool_calls"] = [{"function": {"name": name_map.get(name, name), "arguments": args}}]
    return {"message": msg}


def _sleep_abortable(seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        if _abort_event.is_set():
            raise CodexAborted("aborted by user")
        time.sleep(0.1)


def codex_step(messages: list, model: str = None, tools_schema: list = None,
               effort: str = None, timeout: float = None) -> dict:
    """
    One Midum model call -> {"message": {"role": "assistant", "content": str,
    "tool_calls": [{"function": {"name", "arguments"}}]}} (the same shape every
    other backend returns). Transient errors are retried with backoff inside;
    a usage-limit error is raised as CodexUsageLimit (never retried).
    """
    global _USAGE_LIMITED_UNTIL
    if time.time() < _USAGE_LIMITED_UNTIL:
        raise CodexUsageLimit("Codex usage limit reached (cooling down before trying again)")

    model = model if model is not None else config.CODEX_MODEL
    effort = effort or config.CODEX_REASONING_EFFORT
    chain = [model] + [m for m in (config.CODEX_FALLBACK_MODELS or []) if m != model]

    last = None
    for mi, m in enumerate(chain):
        for attempt in range(config.CODEX_MAX_RETRIES + 1):
            try:
                resp = _step_once(messages, m, tools_schema, effort, timeout)
                if mi > 0:
                    print(f"   [Codex] recovered using fallback model: {m}")
                return resp
            except CodexUsageLimit:
                _USAGE_LIMITED_UNTIL = time.time() + _USAGE_COOLDOWN_S
                raise
            except CodexModelRejected as e:
                last = e
                print(f"   [Codex] model '{m or '(default)'}' rejected ({e}) - trying next fallback...")
                break                                        # next model in the chain
            except CodexTransient as e:
                last = e
                if attempt >= config.CODEX_MAX_RETRIES:
                    raise
                wait_s = min(2.0 * (2 ** attempt), 20.0) + random.uniform(0, 0.75)
                print(f"   [Codex] {e} - retrying in {wait_s:.1f}s "
                      f"(attempt {attempt + 1}/{config.CODEX_MAX_RETRIES + 1})")
                _sleep_abortable(wait_s)
    raise last or CodexError("Codex: all models failed")


# ── orchestration entry point ────────────────────────────────────────────────
def _call_codex_primary(messages, result_q, model_override: str = None):
    """
    One step of the primary tool loop on a background thread, same
    ("ok", resp) / ("err", exc) contract as every other _call_*_primary.

    On a usage-limit error THIS call is routed to config.CODEX_FALLBACK_PROVIDER
    (logged) instead of failing the turn; the system prompt is already
    native-function-calling flavoured so any such provider can continue it.
    """
    try:
        from permissions import filter_tools_schema
        from tools_registry import get_tools_for_model
        use_model = model_override if model_override is not None else config.CODEX_MODEL
        tools_schema = filter_tools_schema(get_tools_for_model("codex", use_model))
        result_q.put(("ok", codex_step(messages, model=use_model, tools_schema=tools_schema)))
    except CodexUsageLimit as e:
        fb = (config.CODEX_FALLBACK_PROVIDER or "").strip()
        if not fb or fb == "codex":
            result_q.put(("err", e))
            return
        print(f"⚠️  [Codex] usage limit reached - routing this call to '{fb}' instead")
        try:
            from orchestration import _call_primary_model
            _call_primary_model(messages, result_q, provider_override=fb)
        except Exception as e2:
            result_q.put(("err", e2))
    except CodexAborted:
        result_q.put(("err", "Codex step aborted by user"))
    except Exception as e:
        result_q.put(("err", e))


# ── consult / delegate / models (mirrors groq_backend) ───────────────────────
def consult_codex(prompt: str, context: str = "", model: str = None) -> str:
    """Plain reasoning/planning prompt to Codex - no tools exposed at all."""
    full_prompt = (prompt or "").strip()
    if (context or "").strip():
        if len(context) > 60000:
            context = context[:30000] + "\n... [TRUNCATED] ...\n" + context[-30000:]
        full_prompt = "[CONTEXT]\n" + context.strip() + "\n\n[TASK]\n" + full_prompt
    use_model = model if model is not None else config.CODEX_MODEL
    try:
        print(f"   [Codex] consult model: {use_model or '(default)'}")
        resp = codex_step([
            {"role": "system", "content": "You are a careful reasoning and planning assistant. Answer directly and completely."},
            {"role": "user", "content": full_prompt},
        ], model=use_model, tools_schema=[])
        result = (resp["message"]["content"] or "").strip()
        return f"[Codex/{use_model or 'default'}]\n" + result
    except CodexUsageLimit as e:
        return f"Codex usage limit reached: {e}"
    except Exception as e:
        return f"Codex error: {e}"


def delegate_to_codex(task: str, context: str = "", model: str = None, max_steps: int = 10) -> str:
    from orchestration import process_chat_turn
    from system_prompt import get_system_prompt
    use_model = model if model is not None else config.CODEX_MODEL
    print(f"   [Delegate -> Codex/{use_model or 'default'}] Task: {task[:80]}")

    saved = None
    try:
        if os.path.exists(config.RESPONSE_MEMORY):
            with open(config.RESPONSE_MEMORY, "r", encoding="utf-8") as f:
                saved = f.read()
    except Exception:
        pass

    try:
        sub_prompt = get_system_prompt(effective_provider="codex", effective_model=use_model or "codex")
        sub_prompt += (
            "\n\n━━━ DELEGATED TASK MODE ━━━\n"
            "You have been handed a specific task by Midum (the primary agent) to "
            "complete independently. You have FULL access to every tool listed above — "
            "act autonomously to complete it. When finished, reply with a clear "
            "plain-text summary of what you did and the result — it is relayed "
            "directly to the user, so make it complete and readable on its own."
        )
        task_message = (task or "").strip()
        if (context or "").strip():
            task_message = f"[CONTEXT FROM MIDUM]\n{context.strip()}\n\n[TASK]\n{task_message}"
        sub_history = [
            {"role": "system", "content": sub_prompt},
            {"role": "user", "content": (
                f"{task_message}\n\n[SYSTEM]: Act immediately. Call the first tool now. "
                "Give a final plain-text summary when done.")},
        ]
        summary, outputs = process_chat_turn(
            sub_history, user_request=task, force_provider="codex",
            force_model=use_model, max_steps=max_steps)
        note = f" ({len(outputs)} tool call(s) executed)" if outputs else ""
        return f"[Codex coworker/{use_model or 'default'} — task complete{note}]\n{summary}"
    except Exception as e:
        return f"Delegation to Codex failed: {e}"
    finally:
        try:
            if saved is not None:
                with open(config.RESPONSE_MEMORY, "w", encoding="utf-8") as f:
                    f.write(saved)
        except Exception:
            pass


_MODEL_CACHE = {"at": 0.0, "items": []}


def _fetch_models(force: bool = False) -> list:
    """model/list -> [{'id', 'name', 'default', 'efforts'}]; cached for 5 minutes."""
    if not force and _MODEL_CACHE["items"] and time.time() - _MODEL_CACHE["at"] < 300:
        return _MODEL_CACHE["items"]
    srv = _get_server()
    srv.ensure_ready()
    items, cursor = [], None
    for _ in range(5):                                    # paginate defensively
        res = srv.request("model/list", {"cursor": cursor} if cursor else {})
        for it in (res.get("data") or res.get("models") or []):
            mid = it.get("model") or it.get("id")
            if mid:
                items.append({"id": mid, "name": it.get("displayName") or mid,
                              "default": bool(it.get("isDefault")),
                              "efforts": [e.get("reasoningEffort") if isinstance(e, dict) else e
                                          for e in (it.get("supportedReasoningEfforts") or [])]})
        cursor = res.get("nextCursor")
        if not cursor:
            break
    _MODEL_CACHE.update(at=time.time(), items=items)
    return items


def codex_known_models() -> list:
    """Model ids for the GUI dropdown. Never raises; falls back to the configured ones."""
    try:
        ids = [m["id"] for m in _fetch_models()]
    except Exception:
        ids = []
    base = [config.CODEX_MODEL] + list(config.CODEX_FALLBACK_MODELS or [])
    return list(dict.fromkeys([m for m in (ids + base) if m]))


def list_codex_models() -> str:
    from tools_registry import _store_index
    try:
        items = _fetch_models(force=True)
    except Exception as e:
        return f"Failed to fetch Codex model list: {e}"
    if not items:
        return "No models returned by Codex."
    _store_index("codex_models", items)
    lines = [f"currently active: {config.CODEX_MODEL or '(account default)'}",
             "Use set_codex_model_by_index(index) to switch.", ""]
    for i, m in enumerate(items):
        eff = f"  efforts: {', '.join(e for e in m['efforts'] if e)}" if m["efforts"] else ""
        lines.append(f"[{i}] {m['id']}{'  (default)' if m['default'] else ''}{eff}")
    return "\n".join(lines)


def set_codex_model(model_id: str) -> str:
    old, new = config.CODEX_MODEL, (model_id or "").strip()
    if not new:
        return "Error: empty model ID."
    config.CODEX_MODEL = new
    print(f"🔀 [Codex model switched: '{old or '(default)'}' → '{new}']")
    return (f"Codex model switched: '{old or '(default)'}' → '{new}'. In-memory only for this "
            "session — edit config.CODEX_MODEL to make it the default on next launch.")


def set_codex_model_by_index(index: int) -> str:
    from tools_registry import _get_indexed
    entry = _get_indexed("codex_models", index)
    if not entry:
        return f"Index {index} not found. Call list_codex_models() first."
    return set_codex_model(entry["id"])


# ── GUI helpers: sign-in status, Connect button, allowance ───────────────────
def codex_status(with_limits: bool = False) -> dict:
    """{'installed','signed_in','version','email','plan','message', ['rate_limits']} — never raises."""
    out = {"installed": False, "signed_in": False, "version": "", "email": "", "plan": "", "message": ""}
    try:
        _resolve_bin()
        out["installed"] = True
    except CodexError as e:
        out["message"] = str(e)
        return out
    try:
        srv = _get_server()
        srv.start()
        out["version"] = srv.version
        res = srv.request("account/read", {"refreshToken": False})
        acct = res.get("account") if isinstance(res, dict) else None
        if acct:
            out.update(signed_in=True, email=acct.get("email", ""), plan=acct.get("planType", ""))
        else:
            out["message"] = "Not signed in. Click Connect to sign in with your ChatGPT account."
        if with_limits and acct:
            out["rate_limits"] = srv.request("account/rateLimits/read", {})
    except Exception as e:
        out["message"] = str(e)
    return out


def codex_rate_limits() -> dict:
    """Raw account/rateLimits/read result (allowance display), or {'error': ...}."""
    try:
        srv = _get_server()
        srv.start()
        return srv.request("account/rateLimits/read", {})
    except Exception as e:
        return {"error": str(e)}


def codex_login_start() -> dict:
    """Start the ChatGPT sign-in flow: asks the app-server for the auth URL and opens it in the browser."""
    try:
        srv = _get_server()
        srv.start()
        srv.login_event.clear()
        res = srv.request("account/login/start", {"type": "chatgpt"})
        url = res.get("authUrl") or res.get("auth_url") or ""
        if url:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        srv._signed_in = False
        return {"ok": bool(url), "auth_url": url, "message": "" if url else "The app-server returned no sign-in URL."}
    except Exception as e:
        return {"ok": False, "auth_url": "", "message": str(e)}

# =============================================================================
