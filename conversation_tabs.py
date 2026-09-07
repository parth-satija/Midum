# =============================================================================
# CONVERSATION TABS — backend only (no GUI yet)
# =============================================================================
# A "conversation tab" is like a browser tab, but instead of a web page it
# holds an entire independent AI conversation (its own message history, its
# own goal/context). Conceptually different from chat "sessions"/history
# files on disk (see memory.py's SESSION_MEMORY, "new session" in main.py):
#
#   • ALL open tabs live fully in RAM at once (their whole message history,
#     not just a summary) — but only the tabs the user actually opened are
#     instantiated. Nothing is opened implicitly.
#   • Exactly ONE tab is "active" at a time. Switching tabs is instant and
#     changes which conversation subsequent turns are appended to/read
#     from — there is no partial state left over from the previous tab.
#   • NO CONTEXT BLEEDING: each tab owns its own distinct Python list object
#     for its message history. Nothing is ever copied, merged, or shared
#     between two tabs' histories. Reading/writing "the active tab" always
#     resolves to that one tab's own list — never anyone else's.
#   • Sub-agents (see multi_agent.py) get a tab of their own automatically:
#     the moment an agent starts working on a task it registers a tab, and
#     the moment that task finishes (success, error, or stop) the tab
#     closes itself. The user never has to manage agent tabs by hand.
#   • A tab's TITLE IS ALWAYS THE NAME OF THE CHAT ITSELF — there is no
#     manual rename. For an ordinary tab that's the first user message
#     (like a browser tab takes the page's own title); for an agent tab
#     it's the agent's name/role; for a not-yet-started tab it's just
#     "New Tab" until the conversation actually begins. See _derive_title().
#   • TAB GROUPS: a named, saveable set of tabs (see the "TAB GROUPS"
#     section below) that can be reopened as a batch at any time, the same
#     way a browser lets you save and reopen a group of tabs.
#
# This module is intentionally UI-agnostic. It exposes a small, thread-safe
# API that a future GUI layer (or the CLI loop in main.py) can sit on top
# of. `process_chat_turn` (orchestration.py) already takes a `history` list
# and mutates it in place — so "switching tabs" is nothing more than handing
# it a different tab's own history list on the next call.
# =============================================================================

import itertools
import json
import os
import queue as _queue
import threading
import time

from config import STORAGE_DIR

# Monotonic counter used to make auto-generated tab ids unique and stable
# within a process run (PIDs/uuids would work too, but a simple counter is
# easiest to read in logs and easiest to test against).
_id_counter = itertools.count(1)

_tabs        = {}          # tab_id -> tab record (see _new_tab_record)
_tabs_lock   = threading.RLock()
_active_tab_id = None       # currently active tab, or None if no tabs are open

# Where saved tab groups persist across restarts — same STORAGE_DIR/JSON
# pattern as multi_agent.py's AGENTS_FILE.
TAB_GROUPS_FILE = os.path.join(STORAGE_DIR, "tab_groups.json")


# ── Tab record ────────────────────────────────────────────────────────────

def _new_tab_record(tab_id, kind, owner, role, system_prompt, history, position):
    """
    history: the list object this tab owns for its whole life. Callers may
    pass an existing list (e.g. main.py's own `history` variable, so the
    CLI's primary conversation and its "Main" tab are literally the same
    object and never drift out of sync) or leave it None to get a fresh
    one (optionally seeded with a system prompt).

    position: a monotonically increasing insertion index, fixed for this
    tab's entire life -- see list_tabs()'s docstring for why this exists
    as its own field instead of sorting by created_at/last_active_at.
    """
    if history is None:
        history = []
        if system_prompt:
            history.append({"role": "system", "content": system_prompt})
    return {
        "id":           tab_id,
        # "user"  — an ordinary conversation the user opened themselves.
        # "agent" — auto-registered for the duration of a sub-agent's task.
        "kind":         kind,
        # For "agent" tabs: the agent's name (see multi_agent.py). None
        # for ordinary user tabs.
        "owner":        owner,
        # For "agent" tabs: the agent's role, used in its derived title.
        "role":         role,
        "history":      history,      # <-- the one and only copy, never duplicated
        "created_at":   time.time(),
        "last_active_at": time.time(),
        # Fixed display position -- see list_tabs(). Switching to/away from
        # a tab must NEVER change this, only closing tabs does (implicitly,
        # since closed tabs just drop out of the sorted list).
        "position":     position,
        # "open"    — normal, usable tab.
        # "closing" — being torn down (agent task wrapping up); present so a
        #             racing close_tab() call can't double-remove it.
        "status":       "open",
    }


# Monotonic counter for _new_tab_record's `position` -- separate from
# _id_counter (which is per-kind) so tab ordering has one single, global,
# ever-increasing sequence regardless of tab kind or id reuse patterns.
_position_counter = itertools.count(1)


def _generate_tab_id(kind: str) -> str:
    return f"{kind}-{next(_id_counter)}"


def _first_user_message_text(history: list):
    for m in history or []:
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return None


# -- Model-generated titles -------------------------------------------------
# Rather than just truncating the first user message verbatim, a "user"
# tab's title is generated by asking the SAME primary model/provider
# currently driving Midum to boil that first message down into a short,
# concise chat title. This is a separate, silent call -- same pattern as
# context_summarizer.py's _call_model_for_summary -- so it never shows up
# in the visible chat and never touches the tab's own history.
#
# Generating a title costs a real model call, so it must not fire on every
# _derive_title() lookup (that function is called constantly, e.g. once
# per row in list_tabs()). Instead each tab caches its generated title
# keyed off the exact first-user-message text it was generated from; the
# cache is reused as long as that text hasn't changed, and regenerated
# (once) if it has (e.g. the first message was edited before the model
# replied).
_TITLE_PROMPT = (
    "Give this chat a short, descriptive title based on the user's first "
    "message below.\n\n"
    "Rules:\n"
    "- 2 to 6 words.\n"
    "- No quotes, no punctuation at the end, no leading label.\n"
    "- Plain text only, one line.\n"
    "- Capture the specific topic/task, not a generic label like "
    "'User question'.\n\n"
    "FIRST MESSAGE:\n{message}\n\nTITLE:"
)

_title_cache_lock = threading.Lock()
# tab_id -> (source_text, title, status) where status is "pending" while a
# background generation is in flight, so concurrent _derive_title() calls
# (e.g. from a fast-polling GUI) don't spawn a second model call for the
# same tab while the first is still running.
_title_cache = {}


def _fallback_title(first_line: str) -> str:
    # Simple truncation, used until the model-generated title is ready and
    # as a safe fallback if the model call fails/times out.
    return (first_line[:47] + "...") if len(first_line) > 50 else first_line


def _generate_title_via_model(cache_key: str, source_text: str, on_done=None) -> None:
    # Runs on its own daemon thread: makes a blocking, silent call to the
    # primary model asking it to name the chat from `source_text`, then
    # stores the result in _title_cache under `cache_key`. Never raises
    # out of the thread -- on any failure/timeout the cache entry is
    # dropped back to a non-pending state so callers keep using the
    # truncated fallback (and can retry later). If a real title was
    # generated, `on_done(title)` is called (when given) so a caller that
    # isn't polling the cache on every render -- e.g. gui/app.py's
    # self._chat_title, which is a plain attribute, not something
    # re-derived live like a tab's title -- can push the update out (to
    # disk / to the frontend) the moment it's ready.
    from orchestration import _call_primary_model   # local import: avoids a circular import at module load

    result_q = _queue.Queue()
    messages = [{"role": "user", "content": _TITLE_PROMPT.format(message=source_text[:2000])}]
    title = None
    try:
        t = threading.Thread(target=_call_primary_model, args=(messages, result_q), daemon=True)
        t.start()
        t.join(timeout=30)
        if not t.is_alive():
            status, payload = result_q.get_nowait()
            if status == "ok":
                raw = (payload["message"].get("content") or "").strip()
                # Models sometimes wrap the title in quotes or add a
                # trailing period despite the prompt -- strip that off,
                # then hard-cap length so a rule-breaking response still
                # can't blow out the tab strip.
                raw = raw.strip().strip('"').strip("'").strip()
                raw = raw.splitlines()[0].strip() if raw else ""
                if raw.endswith((".", "!")):
                    raw = raw[:-1].strip()
                if raw:
                    title = (raw[:47] + "...") if len(raw) > 50 else raw
    except Exception as e:
        print(f"[conversation_tabs] Title generation failed for '{cache_key}': {e}")
        title = None

    with _title_cache_lock:
        if title:
            _title_cache[cache_key] = (source_text, title, "done")
        else:
            _title_cache.pop(cache_key, None)

    if title and on_done is not None:
        try:
            on_done(title)
        except Exception as e:
            print(f"[conversation_tabs] Title on_done callback failed for '{cache_key}': {e}")


def generate_concise_title(cache_key: str, first_message: str, on_result=None) -> str:
    """
    Public entry point for anything OUTSIDE this module that needs the same
    model-generated, concise chat title conversation tabs use -- currently
    gui/app.py's chat-history sidebar entry and custom titlebar label,
    which each keep their own plain `self._chat_title` string rather than
    re-deriving a tab's title live on every render the way _derive_title()
    does. Same caching/dedup rules as the internal tab path: at most one
    model call in flight per `cache_key` for a given `first_message`, and
    calling again with an unchanged `first_message` just returns whatever
    is already cached (or the fallback, while still pending) instead of
    re-triggering generation.

    Returns the truncated first-line fallback IMMEDIATELY -- callers should
    display that right away. If `on_result` is given, it's invoked once
    (from a background thread, not this one) with the model-generated
    title, but ONLY on success -- a failed/timed-out generation leaves the
    fallback in place and never calls back, so callers don't need to
    special-case a "regenerate later" branch.

    Returns "" (and starts nothing) if `first_message` has no usable text.
    """
    if not first_message or not first_message.strip():
        return ""
    first_line = first_message.strip().splitlines()[0].strip()
    if not first_line:
        return ""
    fallback = _fallback_title(first_line)

    with _title_cache_lock:
        cached = _title_cache.get(cache_key)
        if cached is not None:
            source_text, title, status = cached
            if source_text == first_message:
                return fallback if status == "pending" else title
            _title_cache.pop(cache_key, None)
        _title_cache[cache_key] = (first_message, fallback, "pending")

    threading.Thread(
        target=_generate_title_via_model, args=(cache_key, first_message, on_result), daemon=True
    ).start()
    return fallback


def _derive_title(tab: dict) -> str:
    """
    The tab's title is ALWAYS derived from the chat itself — there is no
    stored/renamable title field. This is what "the name of the tab is
    always the name of the chat" means in practice:

      - "agent" tabs: named after the agent working in them (its identity
        IS the chat, since the whole tab exists only for its task).
      - "user" tabs: named by asking the primary model to give the
        conversation a short, concise title based on its first user
        message (see _generate_title_via_model above) -- like a browser
        tab taking the page's own title, but summarized rather than
        copied verbatim. While that model call is in flight (or if it
        ever fails), the truncated first line is shown instead so the
        tab always has SOME title immediately. Before anything has been
        said yet, it's simply "New Tab" (same as a browser tab that
        hasn't navigated anywhere yet).
    """
    if tab["kind"] == "agent":
        return f"Agent: {tab['owner']} ({tab['role']})" if tab.get("role") else f"Agent: {tab['owner']}"

    first = _first_user_message_text(tab["history"])
    if not first:
        return "New Tab"

    return generate_concise_title(tab["id"], first) or _fallback_title(first.splitlines()[0].strip())


# ── Core lifecycle API ───────────────────────────────────────────────────

def open_tab(kind: str = "user", owner: str = None, role: str = None,
             system_prompt: str = None, tab_id: str = None,
             history: list = None, activate: bool = True) -> str:
    """
    Register a new conversation tab and return its id. There is no `title`
    parameter — a tab's title is always derived live from its own chat
    content (see _derive_title()), never set or renamed by hand.

    - kind: "user" (default) or "agent".
    - owner: for agent tabs, the owning agent's name (see multi_agent.py).
    - role: for agent tabs, the agent's role (used in its derived title).
    - system_prompt: only used when `history` is None — seeds a brand-new
      history list with a single system message.
    - tab_id: pass an explicit id to reuse/predict it (e.g. "main" for the
      CLI's primary conversation). Errors if that id is already taken.
    - history: pass an EXISTING list to have this tab wrap it directly
      (same object, not a copy) rather than start a fresh conversation.
      This is how main.py's own `history` variable can double as the
      "Main" tab without keeping two separate copies in sync by hand.
    - activate: if True, this becomes the active tab immediately,
      regardless of whatever else is currently active. If False (used by
      register_agent_tab and open_tab_group's non-first tabs), the tab is
      created but left in the background -- whatever's currently active
      stays active.

    Returns the new tab's id.
    """
    global _active_tab_id
    with _tabs_lock:
        if tab_id is None:
            tab_id = _generate_tab_id(kind)
        elif tab_id in _tabs:
            raise ValueError(f"A tab with id '{tab_id}' already exists.")

        _tabs[tab_id] = _new_tab_record(tab_id, kind, owner, role, system_prompt, history, next(_position_counter))

        # Unconditional: `activate` already says exactly what the caller
        # wants. A prior version of this only activated when there was no
        # valid active tab yet, which meant activate=True silently did
        # nothing for every tab after the very first one -- e.g. clicking
        # "+" while "main" was active would create the new tab, but
        # conversation_tabs' own bookkeeping would keep insisting "main"
        # was still the active tab, even though the caller had already
        # moved all its live conversation state over to the new one.
        # Switching back to "main" would then see tab_id == active_id,
        # treat it as a same-tab no-op, and hand back the NEW tab's
        # (empty) state under "main"'s name.
        if activate:
            _active_tab_id = tab_id

    return tab_id


def close_tab(tab_id: str) -> str:
    """
    Close (discard) a tab and free its in-RAM history. If it was the
    active tab, activity automatically falls back to the most recently
    active remaining tab (if any) — never silently leaves a stale
    reference to a tab that no longer exists.

    INVARIANT: at least one ordinary ("user") conversation tab must
    always stay open. A running Midum with zero tabs doesn't work --
    there would be no active conversation for a new message to land in,
    nothing for get_active_history()/process_chat_turn to hand back, and
    nothing for a tab-strip UI to show as selected. So closing the LAST
    remaining "user" tab is refused here, at the library level, rather
    than relying on every caller (a GUI, a CLI, a tool call the model
    makes directly via the close_conversation_tab wrapper below) to
    remember to check first. "agent" tabs are exempt from this count --
    they're auto-managed (register_agent_tab/unregister_agent_tab) and
    must always be closable the instant their task finishes, regardless
    of what else happens to be open at that moment.
    """
    global _active_tab_id
    with _tabs_lock:
        tab = _tabs.get(tab_id)
        if tab is None:
            return f"No tab '{tab_id}' is open."

        if tab["kind"] == "user" and sum(1 for t in _tabs.values() if t["kind"] == "user") <= 1:
            return (
                f"Cannot close tab '{tab_id}' — it's the only open conversation. "
                "At least one tab must always stay open. Open a new tab first "
                "(open_conversation_tab) if you want to start fresh before closing this one."
            )

        title = _derive_title(tab)
        tab["status"] = "closing"
        del _tabs[tab_id]

        if _active_tab_id == tab_id:
            _active_tab_id = None
            if _tabs:
                # Fall back to whichever remaining tab was active most
                # recently — feels least surprising to whoever's watching.
                newest = max(_tabs.values(), key=lambda t: t["last_active_at"])
                _active_tab_id = newest["id"]

    return f"Tab '{tab_id}' ({title}) closed."


def switch_tab(tab_id: str) -> str:
    """
    Make `tab_id` the active tab. This is the ONLY thing that changes —
    the previously active tab's history is left exactly as it was
    (nothing is flushed, merged, or copied out of it), so switching back
    later resumes it with full context intact.
    """
    global _active_tab_id
    with _tabs_lock:
        tab = _tabs.get(tab_id)
        if tab is None:
            return f"No tab '{tab_id}' is open. Use list_tabs() to see open tabs."
        _active_tab_id = tab_id
        tab["last_active_at"] = time.time()
    return f"Switched to tab '{tab_id}' ({_derive_title(tab)})."


# ── Read access ───────────────────────────────────────────────────────────

def get_active_tab_id():
    with _tabs_lock:
        return _active_tab_id


def get_active_tab():
    """Metadata for the active tab (NOT including the raw history list —
    use get_active_history() for that). Returns None if no tab is open."""
    with _tabs_lock:
        if _active_tab_id is None:
            return None
        tab = _tabs.get(_active_tab_id)
        if tab is None:
            return None
        return {**{k: v for k, v in tab.items() if k != "history"}, "title": _derive_title(tab)}


def get_active_history():
    """
    The LIVE history list object belonging to the active tab. This is the
    exact list a caller should hand to orchestration.process_chat_turn(...)
    so new turns land in the right (and only the right) conversation.
    Returns None if no tab is currently open/active.
    """
    with _tabs_lock:
        if _active_tab_id is None:
            return None
        tab = _tabs.get(_active_tab_id)
        return tab["history"] if tab else None


def get_tab(tab_id: str):
    with _tabs_lock:
        tab = _tabs.get(tab_id)
        if tab is None:
            return None
        return {**{k: v for k, v in tab.items() if k != "history"}, "title": _derive_title(tab)}


def get_tab_history(tab_id: str):
    """The live history list for a specific (not necessarily active) tab."""
    with _tabs_lock:
        tab = _tabs.get(tab_id)
        return tab["history"] if tab else None


def set_tab_history(tab_id: str, history: list) -> None:
    """Replace tab_id's stored history with `history`'s CURRENT contents.

    Some callers (e.g. a GUI whose turn-processing code reassigns its own
    `session.history` to a brand-new list object after every turn, for
    thread-safety reasons unrelated to tabs) can't guarantee the exact
    same list object they handed to open_tab() is still the one being
    mutated later. Rather than requiring every such reassignment site to
    know about conversation_tabs.py, callers can just call this once,
    right before switching away from `tab_id`, to bring its stored record
    back in sync with whatever the live conversation actually contains
    right now. Mutates the record's own list in place (slice-assign)
    rather than swapping in `history` itself, so anything else already
    holding a reference to the tab's list (e.g. a sub-agent's own
    history, for "agent" tabs) keeps working -- though agent tabs never
    need this, since multi_agent.py's history list IS the live object
    the whole time (see register_agent_tab). A no-op if `history` is
    already that exact object.
    """
    with _tabs_lock:
        tab = _tabs.get(tab_id)
        if tab is None or tab["history"] is history:
            return
        tab["history"][:] = history


def list_tabs() -> list:
    """
    Structured summary of every open tab IN A STABLE, NEVER-REORDERING
    POSITION (the order they were opened in, like a real browser tab strip),
    without dumping full histories — just enough for a future tab-strip UI
    to render: id, title (derived live from the chat), kind, owner,
    message count, active flag, and each tab's own last-active timestamp.

    Deliberately sorted by each tab's fixed `position`, NOT by
    `last_active_at` — switching to a tab must never move it (or any
    other tab) to a different slot. last_active_at is still tracked and
    returned (close_tab uses it to pick a sensible fallback active tab),
    it's just not what determines display order.
    """
    with _tabs_lock:
        active = _active_tab_id
        rows = sorted(_tabs.values(), key=lambda t: t["position"])
        return [
            {
                "id":            t["id"],
                "title":         _derive_title(t),
                "kind":          t["kind"],
                "owner":         t["owner"],
                "message_count": len(t["history"]),
                "active":        t["id"] == active,
                "created_at":    t["created_at"],
                "last_active_at": t["last_active_at"],
            }
            for t in rows
        ]


def tab_count() -> int:
    with _tabs_lock:
        return len(_tabs)


# =============================================================================
# TOOL-STYLE WRAPPERS
# =============================================================================
# Thin wrappers with plain-text returns, matching the calling convention
# used by tools_registry.py / multi_agent.py so these can be dropped
# straight into tools_schema.py + orchestration.py's dispatch later,
# without any change to this module. No GUI/tool-schema wiring is added
# yet, per the current scope.

def open_conversation_tab() -> str:
    """Open a new, empty conversation tab and switch to it. It's called
    "New Tab" until the first message is sent — its title then becomes
    that message automatically, just like a browser tab. Returns the new
    tab's id."""
    tab_id = open_tab(kind="user", activate=True)
    switch_tab(tab_id)
    return f"Opened and switched to new tab '{tab_id}'."


def close_conversation_tab(tab_id: str) -> str:
    """Close an open conversation tab by id, freeing its in-RAM history."""
    return close_tab(tab_id)


def switch_conversation_tab(tab_id: str) -> str:
    """Switch the active conversation to the given tab id."""
    return switch_tab(tab_id)


def list_conversation_tabs() -> str:
    """List every currently open conversation tab."""
    rows = list_tabs()
    if not rows:
        return "No conversation tabs are open."
    lines = []
    for r in rows:
        marker = "* " if r["active"] else "  "
        lines.append(
            f"{marker}{r['id']} | {r['title']} | kind={r['kind']}"
            f"{' owner=' + r['owner'] if r['owner'] else ''} | "
            f"messages={r['message_count']}"
        )
    return "\n".join(lines)


# =============================================================================
# SUB-AGENT AUTO-REGISTRATION
# =============================================================================
# Wired up from multi_agent.py: a tab is registered the moment an agent
# starts working a task, and closed the moment that task finishes — the
# agent's own `history` list (multi_agent._new_agent_record's "history"
# key) is passed straight in as the tab's history, so the tab is a live,
# zero-copy window onto exactly what that agent is doing, never a
# snapshot/duplicate that could drift out of sync.
#
# This never changes which tab the USER is looking at — opening an agent
# tab does not `activate` it, so the user's own active conversation is left
# completely undisturbed while agents work in the background.
# =============================================================================

# agent_name -> tab_id, for agents that currently have a tab registered
# (i.e. are actively working a task right now).
_agent_tab_ids = {}
_agent_tab_lock = threading.RLock()


def register_agent_tab(agent_name: str, role: str, history: list) -> str:
    """
    Called when a sub-agent starts working on a task. Registers a tab for
    it (kind="agent") wrapping the agent's OWN history list directly, and
    does NOT switch the user's active tab. Returns the new tab's id.
    If this agent already has a tab open (e.g. a stale one from a crash
    that never got closed), that one is closed first so agents never end
    up with more than one open tab at a time.
    """
    with _agent_tab_lock:
        existing = _agent_tab_ids.get(agent_name)
        if existing is not None:
            close_tab(existing)
        tab_id = open_tab(
            kind="agent",
            owner=agent_name,
            role=role,
            history=history,
            activate=False,
        )
        _agent_tab_ids[agent_name] = tab_id
    return tab_id


def unregister_agent_tab(agent_name: str) -> None:
    """Called when a sub-agent's task finishes (success, error, or stop) —
    closes its tab. Safe to call even if no tab is currently registered
    for this agent (e.g. it never started one, or it was already closed)."""
    with _agent_tab_lock:
        tab_id = _agent_tab_ids.pop(agent_name, None)
    if tab_id is not None:
        close_tab(tab_id)


def get_agent_tab_id(agent_name: str):
    with _agent_tab_lock:
        return _agent_tab_ids.get(agent_name)


# =============================================================================
# TAB GROUPS
# =============================================================================
# A tab group is a NAMED, SAVEABLE set of tabs — the same idea as a browser's
# saved tab group ("Work", "Research", ...) that you can reopen as a batch
# any time, instead of reopening each tab by hand.
#
# What gets saved per tab is just enough to recreate an equivalent tab later
# — its kind and its seed system_prompt (for "user" tabs) or owner/role (for
# "agent" tabs) — NOT its full conversation history. Reopening a group always
# starts each tab fresh, exactly like reopening a browser tab group loads
# each page fresh rather than restoring your old scroll position and form
# input. This also means agent tabs are never saved into a group: an agent
# tab's whole reason to exist is one specific in-progress task, which can't
# be "reopened" later in any meaningful sense.
#
# Groups persist to disk (STORAGE_DIR/tab_groups.json) so they survive
# restarts, the same pattern multi_agent.py uses for persisted agents.
# =============================================================================

_tab_groups_lock = threading.RLock()


def _load_tab_groups() -> dict:
    if not os.path.exists(TAB_GROUPS_FILE):
        return {}
    try:
        with open(TAB_GROUPS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_tab_groups(data: dict) -> None:
    try:
        os.makedirs(STORAGE_DIR, exist_ok=True)
        with open(TAB_GROUPS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️ [conversation_tabs] Could not save {TAB_GROUPS_FILE}: {e}")


def _tab_to_group_spec(tab: dict) -> dict | None:
    """Reduce a live tab record down to the minimal spec needed to recreate
    an equivalent tab later. Returns None for tabs that can't/shouldn't be
    saved into a group (agent tabs — see module docstring above)."""
    if tab["kind"] == "agent":
        return None
    system_prompt = None
    if tab["history"] and tab["history"][0].get("role") == "system":
        system_prompt = tab["history"][0].get("content")
    return {"kind": "user", "system_prompt": system_prompt}


def save_tab_group(name: str, tab_ids: list = None) -> str:
    """
    Save a named, reopenable group of tabs.

    - name: the group's name. Saving again under the same name overwrites
      the previous saved group.
    - tab_ids: which currently-open tabs to include. If omitted, every
      currently open "user" tab is saved (agent tabs are always excluded
      — see module docstring above).

    Only each tab's SEED (kind + system prompt) is saved, not its full
    conversation — reopening the group later starts each tab fresh, same
    as reopening a browser's saved tab group loads each page fresh.
    """
    name = (name or "").strip()
    if not name:
        return "Error: 'name' is required."

    with _tabs_lock:
        if tab_ids:
            source_tabs = [_tabs[t] for t in tab_ids if t in _tabs]
        else:
            source_tabs = list(_tabs.values())
        specs = [s for s in (_tab_to_group_spec(t) for t in source_tabs) if s is not None]

    if not specs:
        return "Nothing to save — no eligible (non-agent) tabs were found."

    with _tab_groups_lock:
        data = _load_tab_groups()
        data[name] = {
            "name": name,
            "tabs": specs,
            "saved_at": time.time(),
        }
        _save_tab_groups(data)

    return f"Saved tab group '{name}' with {len(specs)} tab(s)."


def open_tab_group(name: str, activate_first: bool = True) -> str:
    """
    Reopen a previously saved tab group: creates one fresh tab per saved
    spec (each with its own brand-new, empty history — see save_tab_group)
    and adds them alongside whatever tabs are already open, WITHOUT
    closing or disturbing any of them.
    """
    name = (name or "").strip()
    with _tab_groups_lock:
        data = _load_tab_groups()
        group = data.get(name)
    if not group:
        return f"No saved tab group named '{name}'. Use list_tab_groups() to see saved groups."

    new_ids = []
    for spec in group.get("tabs", []):
        tab_id = open_tab(
            kind=spec.get("kind", "user"),
            system_prompt=spec.get("system_prompt"),
            activate=False,
        )
        new_ids.append(tab_id)

    if activate_first and new_ids:
        switch_tab(new_ids[0])

    return f"Opened tab group '{name}' — {len(new_ids)} tab(s): {', '.join(new_ids)}."


def list_tab_groups() -> str:
    """List every saved tab group and how many tabs each contains."""
    with _tab_groups_lock:
        data = _load_tab_groups()
    if not data:
        return "No tab groups saved."
    lines = []
    for name, group in data.items():
        lines.append(f"- {name} | {len(group.get('tabs', []))} tab(s)")
    return "\n".join(lines)


def get_tab_groups() -> list:
    """Structured version of list_tab_groups(), for a GUI to render (e.g.
    the titlebar's tab-group dropdown) instead of parsing the plain-text
    listing meant for CLI/tool output. Returns
    [{name, tab_count, saved_at}, ...] sorted most-recently-saved first."""
    with _tab_groups_lock:
        data = _load_tab_groups()
    rows = [
        {
            "name": name,
            "tab_count": len(group.get("tabs", [])),
            "saved_at": group.get("saved_at", 0),
        }
        for name, group in data.items()
    ]
    rows.sort(key=lambda r: r["saved_at"], reverse=True)
    return rows


def delete_tab_group(name: str) -> str:
    """Permanently delete a saved tab group (does not affect any currently
    open tabs, even ones that originated from it)."""
    name = (name or "").strip()
    with _tab_groups_lock:
        data = _load_tab_groups()
        if name not in data:
            return f"No saved tab group named '{name}'."
        del data[name]
        _save_tab_groups(data)
    return f"Tab group '{name}' deleted."

# =============================================================================
