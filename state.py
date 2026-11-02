# --- AUTO-SPLITTER: imports added by automated pass, please review ---
import concurrent.futures
import queue
import threading

# --- from main.py, section 1 ---
# ABORT FLAG — Ctrl+Q sets this to stop the current response
# =============================================================================
# A threading.Event that process_chat_turn checks at every loop iteration.
# When set, the turn is abandoned and control returns to the input prompt.
#
# CONCURRENT TABS: several conversation tabs may now run process_chat_turn at the same time, so
# "the" abort flag, action loop and interjection queue can no longer be single process-wide
# objects. Each running turn owns a TurnContext, bound to the current context (contextvars) by
# gui/app.py's _run_turn; _abort_event / _action_loop_event / _action_loop_state below are thin
# proxies that resolve to the bound turn's own objects, and fall back to one shared default
# (the old global behaviour) on threads with no bound turn -- e.g. sub-agent worker threads,
# the CLI loop. Every existing `_abort_event.is_set()` style call site keeps working unchanged.
import collections.abc
import contextvars


class TurnContext:
    def __init__(self, tab_id=None, chat_id=None):
        self.tab_id = tab_id
        self.chat_id = chat_id
        self.abort_event = threading.Event()
        self.action_loop_event = threading.Event()
        self.action_loop_state = {"goal": "", "started_steps": 0}
        self.interjections = queue.Queue()
        # Gemini API key pinned to this turn (index into the key chain) -- see
        # providers/gemini_api_backend.py. None = use the shared chain as before.
        self.gemini_key_index = None
        self.gemini_keys_tried = set()


_turn_ctx = contextvars.ContextVar("midum_turn_ctx", default=None)
_live_ctxs = __import__("weakref").WeakSet()     # every TurnContext that has been bound (and not yet dropped)


def current_ctx():
    return _turn_ctx.get()


def bind_ctx(ctx):
    """Bind ctx to the calling thread/context. Returns a token for unbind_ctx()."""
    try:
        _live_ctxs.add(ctx)
    except Exception:
        pass
    return _turn_ctx.set(ctx)


def abort_all_turns():
    """Stop everything: the shared default flag (sub-agents, CLI) AND every running tab's turn. Used by the
    global Ctrl+Q hotkey, which fires on a thread that belongs to no particular tab."""
    _global_abort_event.set()
    for c in list(_live_ctxs):
        try:
            c.abort_event.set()
        except Exception:
            pass


def unbind_ctx(token):
    try:
        _turn_ctx.reset(token)
    except Exception:
        _turn_ctx.set(None)


def in_current_context(fn):
    """Wrap fn so it runs inside a COPY of the caller's context. Use for threads / executor jobs
    started during a turn, so they see the same bound turn (abort flag, pinned key, tab)."""
    cv = contextvars.copy_context()

    def _runner(*a, **k):
        return cv.run(fn, *a, **k)
    return _runner


class _ScopedEvent:
    def __init__(self, attr):
        self._attr = attr
        self._default = threading.Event()

    def _ev(self):
        c = _turn_ctx.get()
        return getattr(c, self._attr) if c is not None else self._default

    def is_set(self):
        return self._ev().is_set()

    isSet = is_set

    def set(self):
        self._ev().set()

    def clear(self):
        self._ev().clear()

    def wait(self, timeout=None):
        return self._ev().wait(timeout)


class _ScopedDict(collections.abc.MutableMapping):
    def __init__(self, attr, default):
        self._attr = attr
        self._default = default

    def _d(self):
        c = _turn_ctx.get()
        return getattr(c, self._attr) if c is not None else self._default

    def __getitem__(self, k):
        return self._d()[k]

    def __setitem__(self, k, v):
        self._d()[k] = v

    def __delitem__(self, k):
        del self._d()[k]

    def __iter__(self):
        return iter(self._d())

    def __len__(self):
        return len(self._d())


_abort_event = _ScopedEvent("abort_event")
# The shared default flag (what unbound threads such as sub-agents poll). Api.abort() sets this too,
# so aborting still stops the agents it always stopped, in addition to the tab's own turn.
_global_abort_event = _abort_event._default

# =============================================================================

# =============================================================================
# ABORTABLE TOOL CALLS -- run a (potentially slow/hung) native tool function
# on a worker thread and stop WAITING on it the instant _abort_event is set,
# instead of blocking process_chat_turn until that call finally returns.
# This is the native-tool counterpart to the same pattern already used for
# MCP calls in midum_mcp/manager.py._MCPManager.call_tool -- both poll this
# exact _abort_event, so the GUI's existing Stop button / Ctrl+Q / Edit-
# Prompt abort path (gui/app.py's Api.abort() and _pop_last_turn(), which
# just call _abort_event.set()) already reaches BOTH without any extra
# wiring per call site.
#
# Applied in orchestration.py only to the native tools that can realistically
# run long enough to matter (terminal/WSL/Python execution, browser
# automation, web search, OCR) -- fast in-process calls (read a file, update
# memory, list skills, ...) return effectively instantly and gain nothing
# from the extra thread hop, so they're left as plain synchronous calls.
#
# NOTE, same caveat as the MCP version: a plain Python thread can't be
# safely force-killed. run_abortable() only stops MIDUM from waiting; if fn
# is genuinely stuck it keeps running in the background and its eventual
# result is discarded when it does finish. Long-running native tools should
# still carry their own internal timeouts where possible (several already
# do, e.g. execute_wsl_command's timeout= argument) -- this is the safety
# net on top of that, not a replacement for it.
# =============================================================================
_tool_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="midum-tool")


def run_abortable(fn, *args, timeout: float | None = None, poll_interval: float = 0.15, **kwargs):
    """
    Run fn(*args, **kwargs) on a worker thread; return its result normally,
    but return an '[ABORTED] ...' string immediately (without waiting any
    further) if _abort_event gets set before fn finishes. Optional `timeout`
    (seconds) gives up and returns an error string if fn simply never
    returns and the user never aborts either.
    """
    # Run the job inside a copy of the caller's context so the worker sees the same bound turn
    # (its own abort flag, tab, pinned API key) -- plain executor threads would not.
    _cv = contextvars.copy_context()
    fut = _tool_executor.submit(_cv.run, fn, *args, **kwargs)
    elapsed = 0.0
    while True:
        if _abort_event.is_set():
            fut.cancel()   # only actually takes effect if fn hasn't started running yet
            return (
                "[ABORTED] The user aborted the turn while this call was still running — "
                "Midum stopped waiting on it; its result, if any ever arrives, is discarded. "
                "Do not retry it as part of this (now-ended) turn."
            )
        try:
            return fut.result(timeout=poll_interval)
        except concurrent.futures.TimeoutError:
            elapsed += poll_interval
            if timeout is not None and elapsed >= timeout:
                fut.cancel()
                return f"Error: timed out after {timeout}s."
            continue
        except concurrent.futures.CancelledError:
            return "[ABORTED] This call was cancelled."
        except Exception as e:
            return f"Error: {e}"

# =============================================================================

# =============================================================================
# CONTINUOUS ACTION LOOP -- flipped on by start_action_loop, off by
# stop_action_loop (both defined in orchestration.py). While set,
# process_chat_turn keeps calling the model / executing tools across what
# would normally be separate turns -- a plain-text reply no longer ends the
# turn -- until stop_action_loop() clears it or a hard safety ceiling is
# hit. Voice mode uses the exact same flag: start_action_loop/stop_action_loop
# are ordinary tools dispatched through gui/dispatch.py just like any other,
# so the Gemini Live model can flip this shared state too, and its own
# turn-based tool-calling already lets it keep acting without ending the
# conversation -- this flag is mainly what lets the TEXT orchestration loop
# (process_chat_turn) do the same thing.
# =============================================================================
_action_loop_event = _ScopedEvent("action_loop_event")     # per running turn (see TurnContext)
_action_loop_lock = threading.Lock()
_action_loop_state = _ScopedDict("action_loop_state", {"goal": "", "started_steps": 0})

# =============================================================================

# =============================================================================
# USER INTERJECTIONS -- a steering message typed into the prompt box WHILE
# Midum is still "Executing turns..." on a previous message (see gui/app.py's
# Api.send_interjection and the prompt box's sendMessage() in gui/app.py's
# embedded JS). Unlike a normal message, an interjection does NOT start a
# new turn and does NOT touch conversation_history directly from the GUI
# thread -- it's dropped into this queue, and orchestration.process_chat_turn
# drains it and folds it into conversation_history as a synthetic tool-role
# message immediately after the NEXT tool call finishes executing, so it
# reaches the model in-context, mid-task, without waiting for the current
# turn to end. The model receives it exactly like a tool response -- it is
# never an actual tool call, so it never shows up as a tool-call pill.
# =============================================================================
_interjection_queue = queue.Queue()


def queue_interjection(text: str, ctx=None) -> None:
    """Queue a live steering message for a currently-executing turn. `ctx` picks WHICH turn (its
    TurnContext, as held by gui/app.py per tab); with no ctx it targets the calling thread's bound
    turn, else the shared default queue."""
    text = (text or "").strip()
    if text:
        c = ctx if ctx is not None else _turn_ctx.get()
        (c.interjections if c is not None else _interjection_queue).put(text)


def drain_interjections():
    """Pop every interjection queued so far, joined into one block (None if
    none are pending). Called by process_chat_turn right after each tool
    call's result is appended to conversation_history."""
    parts = []
    _c = _turn_ctx.get()
    _q = _c.interjections if _c is not None else _interjection_queue
    while True:
        try:
            parts.append(_q.get_nowait())
        except queue.Empty:
            break
    return "\n".join(parts) if parts else None

# =============================================================================

