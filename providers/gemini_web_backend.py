# --- AUTO-SPLITTER: imports added by automated pass, please review ---
import config
from config import GEMINI_WEB_HOP_TIMEOUT, GEMINI_WEB_TOOL_RESULT_MARKER, RESPONSE_MEMORY, SECRETS_FILE
from system_prompt import get_system_prompt
import asyncio
import json
import os
import re
import threading

# --- from main.py, section 1 ---
# GEMINI WEB APP CLIENT — via community `gemini_webapi` library
# =============================================================================
#
# query_gemini_app() no longer drives a real Chrome tab over CDP. Instead it
# uses the community-maintained `gemini_webapi` library
# (https://github.com/HanaokaYuzu/Gemini-API), which talks directly to
# gemini.google.com's internal endpoints using your browser's session
# cookies. No page load, no clicking into a prompt box, no clipboard —
# just a plain async HTTP call. It still uses the FREE web app's own
# usage limits, not the metered developer API.
#
# INSTALLATION:
#   pip install -U gemini_webapi
#   pip install -U browser-cookie3      (optional, see below)
#
# AUTHENTICATION (one-time), whichever is easier:
#   A) Automatic — install browser-cookie3 and be logged into
#      https://gemini.google.com in a supported browser (Chrome, Firefox,
#      Edge, Brave, etc — see the project README for the full list).
#      gemini_webapi will pull the session cookies straight from your
#      browser's cookie store; no further config needed.
#   B) Manual — log into https://gemini.google.com, open DevTools (F12)
#      -> Network tab -> refresh -> find the __Secure-1PSID and
#      __Secure-1PSIDTS cookies (Application/Storage tab, or a request's
#      Cookie header), then add them to the secrets file:
#        { "GEMINI_SECURE_1PSID": "...", "GEMINI_SECURE_1PSIDTS": "..." }
# =============================================================================

_GEMINI_WEBAPI_AVAILABLE = False
_gemini_webapi_load_msg  = ""
try:
    from gemini_webapi import GeminiClient as _GeminiWebClient
    from gemini_webapi.constants import Model as _GeminiModelEnum
    _GEMINI_WEBAPI_AVAILABLE = True

    # gemini_webapi logs internally via loguru at DEBUG/WARNING level,
    # which by default dumps raw HTTP response bodies (the ")]}'" /
    # array-of-arrays payloads you saw printed) straight to stderr on
    # every request — none of that is an actual Midum error, it's just
    # unfiltered library-internal logging. Cap it to ERROR so only real
    # failures surface; auth/session recovery is handled by our own
    # retry logic below, not by reading these log lines.
    try:
        from loguru import logger as _gemini_loguru_logger
        _gemini_loguru_logger.remove()
        _gemini_loguru_logger.add(lambda msg: None, level="ERROR")
    except Exception:
        pass
except ImportError as _e:
    _GeminiWebClient = None
    _GeminiModelEnum = None
    _gemini_webapi_load_msg = f"gemini_webapi not installed: {_e}. Run: pip install -U gemini_webapi"

_gemini_web_client      = None   # lazily-initialised GeminiClient (singleton)
_gemini_web_client_lock = threading.Lock()

_gemini_async_loop   = None       # persistent background asyncio loop
_gemini_async_thread = None


def _get_gemini_async_loop():
    """
    Start (once) a background thread running a persistent asyncio event
    loop. gemini_webapi is async; this lets the rest of this otherwise-sync
    script call into it and block for a result, while still letting the
    library's background cookie-refresh task keep running between calls.
    """
    global _gemini_async_loop, _gemini_async_thread
    if _gemini_async_loop is not None:
        return _gemini_async_loop

    _gemini_async_loop = asyncio.new_event_loop()

    def _runner():
        asyncio.set_event_loop(_gemini_async_loop)
        _gemini_async_loop.run_forever()

    _gemini_async_thread = threading.Thread(target=_runner, daemon=True, name="gemini-webapi-loop")
    _gemini_async_thread.start()
    return _gemini_async_loop


def _run_gemini_coro(coro, timeout: float = 90.0):
    """Run a gemini_webapi coroutine on the background loop and block for the result."""
    loop = _get_gemini_async_loop()
    fut  = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)


def _get_gemini_web_client():
    """
    Lazily create (and cache) the GeminiClient singleton.

    Cookie resolution order:
      1. GEMINI_SECURE_1PSID / GEMINI_SECURE_1PSIDTS from the secrets file,
         if present.
      2. Otherwise, pass no cookies and let gemini_webapi try to pull them
         automatically via browser-cookie3 (if installed) from whatever
         browser you're logged into gemini.google.com in.

    Returns (client, None) on success, or (None, error_message) on failure.
    """
    global _gemini_web_client
    if _gemini_web_client is not None:
        return _gemini_web_client, None
    if not _GEMINI_WEBAPI_AVAILABLE:
        return None, _gemini_webapi_load_msg

    with _gemini_web_client_lock:
        if _gemini_web_client is not None:
            return _gemini_web_client, None

        secure_1psid, secure_1psidts = "", ""
        secrets_path = os.path.abspath(SECRETS_FILE)
        try:
            if os.path.exists(secrets_path):
                with open(secrets_path, "r", encoding="utf-8") as f:
                    secrets = json.load(f)
                secure_1psid   = secrets.get("GEMINI_SECURE_1PSID", "").strip()
                secure_1psidts = secrets.get("GEMINI_SECURE_1PSIDTS", "").strip()
        except Exception:
            pass   # fall through to browser-cookie3 auto-detection

        try:
            client = _GeminiWebClient(secure_1psid or None, secure_1psidts or None)
            _run_gemini_coro(
                client.init(timeout=30, auto_close=False, auto_refresh=True),
                timeout=35,
            )
            _gemini_web_client = client
            return _gemini_web_client, None
        except Exception as e:
            return None, (
                f"Failed to initialise Gemini web client: {e}. Either add "
                f"GEMINI_SECURE_1PSID/GEMINI_SECURE_1PSIDTS to the secrets file "
                f"({secrets_path}), or install browser-cookie3 (pip install -U "
                f"browser-cookie3) and make sure you're logged into "
                f"https://gemini.google.com in a supported browser."
            )




# =============================================================================

# --- from main.py, section 2 ---
# GEMINI-WEB PRIMARY EXECUTION BACKEND
# =============================================================================
# Implements MODEL_PROVIDER == "gemini_web": Gemini, driven through the same
# gemini_webapi client/session plumbing set up above, as a full tool-calling
# execution brain sitting alongside Ollama and OpenRouter. See the module
# docstring / implementation brief for the full architecture. Summary:
#
#   - NO GEM. Gems proved problematic (flaky create/update/fetch round trips,
#     an extra persistent server-side object to get out of sync, silent
#     "no-gem mode" degradation) for what they bought us. Instead the same
#     persona + JSON tool-call-format instructions are sent as a single
#     plain-text priming message — the FIRST message of a fresh ChatSession
#     — exactly once per conversation, no create_gem/update_gem/fetch_gems
#     calls anywhere in this backend anymore.
#   - The native tool schema is NOT inlined into that priming message either.
#     Instead Gemini gets two native tools of its own — list_native_tools()
#     and show_native_tool_schema(tool_name) — and discovers native tools on
#     demand in-turn, the exact same pattern already used for MCP tools via
#     list_mcp_servers()/show_server_tools(). This keeps every hop's prompt
#     small and means the tool catalogue can never drift out of sync with a
#     stale cached Gem prompt, since there's nothing cached server-side to
#     go stale.
#   - One persistent gemini_webapi ChatSession per conversation_history
#     object carries multi-hop tool-loop context across round trips; new
#     tool results are injected into that SAME session as new messages
#     (prefixed with GEMINI_WEB_TOOL_RESULT_MARKER), never as independent
#     generate_content() calls (which would lose the session).
#   - Output is parsed with the exact same legacy JSON tool-call parser used
#     for Ollama/OpenRouter free-text tool calls (_extract_legacy_tool_calls),
#     scanning ONLY output.text — never output.thoughts.
#   - A `source`/`server` field on the emitted JSON (see _try_parse_tool_json)
#     disambiguates native vs. MCP dispatch; this is threaded through the
#     shared parser so it benefits every provider, not just Gemini.
# =============================================================================

# Concurrency guard: the scraped-cookie session backing _gemini_web_client is
# a SINGLE account session. Multiple simultaneous tool-loop tasks hammering
# it concurrently would interleave ChatSession state unpredictably. Rather
# than silently allowing that, every Gemini-web primary call is serialized
# through this lock — a second concurrent task simply waits its turn instead
# of corrupting the first task's in-progress tool loop.
_gemini_primary_call_lock = threading.Lock()

# One gemini_webapi ChatSession per conversation_history object, keyed by
# id() (conversation_history is a plain list owned/mutated by the caller,
# never replaced mid-conversation in this codebase, so id() is stable for
# the conversation's lifetime). Also tracks how much of that list has
# already been delivered to Gemini, so each hop only sends the NEW
# messages (the session already has everything sent previously) rather
# than re-sending the whole transcript as text every step.
#   id(conversation_history) -> {
#       "chat": ChatSession,
#       "sent": list[dict]   # exact message objects already delivered
#   }
_gemini_web_sessions: dict = {}

# Cached "fastest available model" resolution, refreshed once per process
# (or whenever config.GEMINI_WEB_MODEL changes) rather than on every hop.
_gemini_web_model_cache = None


def _gemini_web_persona_prompt() -> str:
    """
    Builds the plain-text persona + JSON tool-call output convention that
    used to live in a Gem. Sent as the FIRST message of every fresh
    ChatSession (see _gemini_web_get_session/_call_gemini_web_primary) —
    no create_gem/update_gem/fetch_gems round trip, nothing pinned
    server-side, nothing that can go stale or silently fall back to
    "no-gem mode".

    Deliberately does NOT inline the native tool schema. Instead Gemini
    gets list_native_tools()/show_native_tool_schema(tool_name) — the same
    on-demand discovery pattern already used for MCP tools via
    list_mcp_servers()/show_server_tools() — so this priming message stays
    small and the tool catalogue can never drift out of sync.
    """
    return (
        "You are Midum, a desktop AI agent. You act by emitting tool calls "
        "as JSON — you have no native function-calling protocol here, so "
        "this JSON convention IS your only way to act.\n"
        "\n"
        "\u2501\u2501\u2501 TOOL-CALL OUTPUT FORMAT \u2501\u2501\u2501\n"
        "When you want to call a tool, your ENTIRE reply must be ONE JSON "
        "object and nothing else \u2014 no commentary before or after, no markdown "
        "fences required (but tolerated if you include them):\n"
        '  {"name": "<tool_name>", "arguments": {<args>}, "source": "<source>"}\n'
        "\n"
        "`source` tells the harness which catalogue the tool comes from:\n"
        '  - "native"   \u2014 a built-in Midum tool. This is almost every call.\n'
        '  - "mcp:<server>" \u2014 the tool came from show_server_tools(<server>) for\n'
        "                 a connected MCP server (e.g. \"mcp:jira\"). Only use this\n"
        "                 after you've actually called show_server_tools for that\n"
        "                 server this session \u2014 don't guess an MCP tool exists.\n"
        "If you omit `source`, the harness assumes \"native\".\n"
        "\n"
        "Call exactly ONE tool per reply. Never batch multiple tool calls into one "
        "JSON object or array \u2014 one at a time, wait for the [TOOL_RESULT], then "
        "decide the next step.\n"
        "\n"
        "When you are done and have a final answer for the user (no more tools "
        "needed), reply with PLAIN TEXT \u2014 no JSON, no `name`/`arguments` keys.\n"
        "\n"
        "\u2501\u2501\u2501 WHERE TOOLS COME FROM (nothing is pre-loaded \u2014 discover, don't guess) \u2501\u2501\u2501\n"
        "- NATIVE tools: every tool Midum has built in. You are NOT given their "
        "full schemas up front. Call list_native_tools() (a native tool) to see "
        "names + one-line descriptions, then show_native_tool_schema(tool_name) "
        "for the exact arguments a specific tool expects, before calling it for "
        "the first time this session.\n"
        "- MCP tools: external servers connected to Midum, which can change "
        "independently of this prompt. Call list_mcp_servers() to see what's "
        "connected, then show_server_tools(server) to get that ONE server's tool "
        "schemas before calling any of its tools. Never assume an MCP tool's "
        "name or arguments without having called show_server_tools for it first "
        "this session.\n"
        "- call_mcp_tool(server, tool_name, arguments) is the uniform way to "
        "invoke any MCP tool once you know its schema, if you'd rather route "
        "through it explicitly than emit source=\"mcp:<server>\" directly \u2014 both "
        "work identically, the harness normalizes to the same dispatch.\n"
        "- Once you've discovered a tool's schema this session, you don't need "
        "to look it up again \u2014 just call it.\n"
        "\n"
        "\u2501\u2501\u2501 ACT, DON'T NARRATE \u2501\u2501\u2501\n"
        "If you have a next step to take, emit the tool-call JSON for it. Never "
        "write out what you're 'about to do' as plain text instead of doing it. "
        "Plain-text replies with no tool call are ONLY for a genuine final "
        "answer, or a question you need the user to answer before continuing.\n"
    )


def _clean_gemini_web_text(text: str) -> str:
    """
    Strip internal artifacts the gemini.google.com web app occasionally
    leaks straight through gemini_webapi's parsed output.text.

    The one we've actually observed: rich/canvas ("immersive") content
    blocks are internally tagged with a "?chameleon" suffix on the code
    fence's language marker (Google's internal codename for that render
    surface) -- e.g. a JSON canvas block comes back as
    "```json?chameleon\n...\n```" instead of a plain "```json". That's not
    meaningful content, just leaked internal plumbing, and it breaks
    downstream JSON/tool-call parsing (which expects a clean language tag)
    as well as looking wrong if ever shown to the user. Strip the suffix
    off any fence language tag, and drop it entirely if "chameleon" is the
    whole tag.
    """
    if not text or "chameleon" not in text:
        return text
    # ```json?chameleon -> ```json   (keep the real language, drop the tag)
    text = re.sub(r'```(\w+)\?chameleon\b', r'```\1', text)
    # ```?chameleon or ```chameleon on its own -> ``` (no language)
    text = re.sub(r'```\??chameleon\b', '```', text)
    # Any other stray "<word>?chameleon" outside a fence context
    text = re.sub(r'(\w+)\?chameleon\b', r'\1', text)
    return text


def _gemini_web_pick_model(client):
    """
    Resolve the model to drive the primary execution loop with.

      - If config.GEMINI_WEB_MODEL is set, use it verbatim (matched against
        client.list_models() by model_name/display_name).
      - Otherwise auto-pick the fastest suitable model from whatever the
        account currently has available (never hardcode a specific fast/
        lite model name — the web app's lineup isn't stable over time).
        Heuristic: prefer a "flash"-tier model over "pro"/"advanced"/
        "thinking" variants, since latency is a real cost here (§2.4/§3.5).

    Returns a Model/AvailableModel/str suitable for the `model=` kwarg on
    ChatSession/generate_content, or Model.UNSPECIFIED (let Gemini pick)
    if nothing usable was found.
    """
    global _gemini_web_model_cache
    if _gemini_web_model_cache is not None:
        return _gemini_web_model_cache

    if config.GEMINI_WEB_MODEL:
        _gemini_web_model_cache = config.GEMINI_WEB_MODEL
        return _gemini_web_model_cache

    try:
        available = client.list_models() or []
    except Exception:
        available = []

    if not available:
        _gemini_web_model_cache = _GeminiModelEnum.UNSPECIFIED if _GeminiModelEnum else None
        return _gemini_web_model_cache

    def _rank(m):
        name = (m.model_name or "").lower() + " " + (m.display_name or "").lower()
        if "flash" in name and "thinking" not in name and "advanced" not in name and "plus" not in name:
            return 0
        if "flash" in name:
            return 1
        return 2

    available.sort(key=_rank)
    chosen = available[0]
    print(f"🧠 [Gemini-web] Auto-selected model: {chosen.model_name} ({chosen.display_name})")
    _gemini_web_model_cache = chosen
    return _gemini_web_model_cache


def _gemini_web_render_message(msg: dict) -> str | None:
    """
    Render one OpenAI-style conversation_history message into Gemini-web
    prompt text. Returns None for messages that shouldn't be sent at all
    (Gemini's own prior assistant turns are already in the ChatSession
    server-side — re-sending them would duplicate context).
    """
    role    = msg.get("role", "")
    content = msg.get("content", "")
    if isinstance(content, list):
        # Some call sites build OpenAI-style multi-part content; flatten to text.
        content = "\n".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    content = (content or "").strip()

    if role == "system":
        # The persona/system prompt is primed once as the first turn of a
        # fresh ChatSession (see _gemini_web_persona_prompt /
        # _call_gemini_web_primary) — resending conversation_history's own
        # system entries on every hop would just bloat each round trip for
        # no benefit. Skip entirely.
        return None
    if role == "assistant":
        # Gemini's own prior turns already live server-side in the
        # ChatSession; nothing to inject back.
        return None
    if role == "tool":
        if not content:
            return None
        return f"{GEMINI_WEB_TOOL_RESULT_MARKER} {content}"
    # role == "user" (or unknown, treated as a plain user turn)
    return content if content else None


def _gemini_web_get_session(conversation_history: list, client, model):
    """
    Get-or-create the persistent ChatSession for this conversation, and
    return (chat_session, delta_messages, is_new) where delta_messages is
    the list of conversation_history entries not yet delivered to Gemini,
    and is_new is True the very first time this session is created (so the
    caller knows to prime it with the persona prompt as the first turn —
    no Gem, so nothing is pinned server-side automatically).

    If conversation_history has been rewritten out from under us in a way
    that isn't a clean append (e.g. the sliding HISTORY_WINDOW in
    process_chat_turn dropped older entries, or this is a genuinely new
    task reusing the same list object), the safe move is to start a FRESH
    ChatSession and resend the full current window as one prompt — silent
    corruption of an old session's dangling state is worse than one extra
    full-context turn.
    """
    key   = id(conversation_history)
    state = _gemini_web_sessions.get(key)

    if state is not None:
        sent = state["sent"]
        if len(conversation_history) >= len(sent) and conversation_history[:len(sent)] == sent:
            delta = conversation_history[len(sent):]
            state["sent"] = list(conversation_history)
            return state["chat"], delta, False
        # History diverged from what we last sent (window slid, or history
        # was mutated) — treat as a fresh task on a fresh session.
        print("   [Gemini-web] Conversation history diverged from session state — "
              "starting a fresh ChatSession for this task.")

    chat = client.start_chat(model=model)
    _gemini_web_sessions[key] = {"chat": chat, "sent": list(conversation_history)}
    return chat, list(conversation_history), True


def _gemini_web_reset_session(conversation_history: list):
    """Drop the cached session for this conversation (used on unrecoverable
    session/auth failures, so the next call starts clean instead of
    repeatedly hammering a dead session)."""
    _gemini_web_sessions.pop(id(conversation_history), None)


def _call_gemini_web_primary(messages, result_q, model_override: str = None):
    from orchestration import _extract_legacy_tool_calls
    """
    Drives ONE step of the primary tool-calling loop via Gemini through
    gemini_webapi, on a persistent ChatSession. Populates result_q with the
    exact same ("ok", resp) / ("err", exc) contract as
    _call_ollama/_call_openrouter_primary, so process_chat_turn is
    completely unaware Gemini is driving instead of a local/OpenRouter
    model — this is the entire point of matching the existing dispatch
    pattern instead of a parallel one.

    `messages` here is the SAME trimmed conversation_history list
    process_chat_turn already threads through the other two backends
    (system messages + a sliding window of the rest) — used both to derive
    the ChatSession delta (§2.4) and, on a fresh/diverged session, as the
    full resend content.

    Session-recovery (§3.5): on an auth/session failure mid-loop, one
    reinit + one retry is attempted before surfacing a clear recoverable
    error — never silently corrupting the loop by pretending the call
    succeeded.
    """
    try:
        from gemini_webapi.exceptions import AuthError, GeminiError, TimeoutError as _GeminiTimeoutError
    except ImportError:
        AuthError = GeminiError = _GeminiTimeoutError = Exception  # pragma: no cover

    def _attempt():
        client, err = _get_gemini_web_client()
        if err:
            raise RuntimeError(f"Gemini web client unavailable: {err}")

        model = model_override or _gemini_web_pick_model(client)

        chat, delta, is_new = _gemini_web_get_session(messages, client, model)

        rendered = [r for r in (_gemini_web_render_message(m) for m in delta) if r]
        if is_new:
            # No Gem here — prime the fresh ChatSession with the persona +
            # tool-call-format instructions as the very first turn, exactly
            # once per conversation, instead of a pinned server-side object.
            rendered = [_gemini_web_persona_prompt()] + rendered
        if not rendered:
            # Nothing new to say (can happen right after a fresh-session
            # full resend where everything was system/assistant messages).
            # Fall back to a neutral nudge so we never send an empty prompt.
            prompt = "[SYSTEM]: Continue with the task."
        else:
            prompt = "\n\n".join(rendered)

        with _gemini_primary_call_lock:
            output = _run_gemini_coro(
                chat.send_message(prompt),
                timeout=GEMINI_WEB_HOP_TIMEOUT,
            )
        return output

    try:
        try:
            output = _attempt()
        except (AuthError, _GeminiTimeoutError, GeminiError) as e:
            # Session fragility is a first-class risk (§ context) — one
            # recovery attempt: drop the dead client/session and retry
            # exactly once before giving up.
            print(f"⚠️  [Gemini-web] Session/auth error mid-loop ({e}) — "
                  f"reinitializing and retrying once...")
            global _gemini_web_client
            _gemini_web_client = None
            _gemini_web_reset_session(messages)
            output = _attempt()

        raw_text = output.text or ""   # NEVER read output.thoughts here (§3.3)
        raw_text = _clean_gemini_web_text(raw_text)

        legacy_calls, cleaned_content = _extract_legacy_tool_calls(raw_text)
        if legacy_calls:
            legacy_calls = legacy_calls[:1]   # one tool at a time (§ non-goals)

        resp = {
            "message": {
                "role": "assistant",
                "content": cleaned_content,
                "tool_calls": legacy_calls,
            }
        }
        result_q.put(("ok", resp))

    except Exception as e:
        _gemini_web_reset_session(messages)
        result_q.put(("err", (
            f"Gemini-web session/request failed: {e}. If this persists, the "
            f"scraped cookie session likely expired — refresh "
            f"GEMINI_SECURE_1PSID/GEMINI_SECURE_1PSIDTS in the secrets file, "
            f"or re-login in the browser gemini_webapi pulls cookies from."
        )))


def list_gemini_web_models() -> list:
    """
    Return every model name currently available on the logged-in Gemini web
    account (via gemini_webapi's client.list_models()), for populating
    model pickers with the real, current lineup instead of a hardcoded
    guess. Falls back to an empty list if the client can't be reached
    (library not installed, no valid session/cookies yet, network error,
    etc) — callers should fall back to a small hardcoded list in that case.

    Note: this may lazily initialise the Gemini web client (cookie
    resolution + session init) the first time it's called, which can take
    a couple of seconds — the same cost _gemini_web_pick_model already
    pays on first use.
    """
    if not _GEMINI_WEBAPI_AVAILABLE:
        return []
    client, err = _get_gemini_web_client()
    if client is None:
        return []
    try:
        available = client.list_models() or []
    except Exception:
        return []
    names = []
    for m in available:
        name = getattr(m, "model_name", None) or getattr(m, "display_name", None)
        if name and name not in names:
            names.append(name)
    return names


def set_gemini_web_model(model_name: str) -> str:
    """
    Pin config.GEMINI_WEB_MODEL to an exact model name/display name, or pass ""
    to go back to auto-selecting the fastest available model. Takes effect
    on the next primary-loop call — no restart needed.
    """
    global _gemini_web_model_cache
    old = config.GEMINI_WEB_MODEL
    config.GEMINI_WEB_MODEL = (model_name or "").strip()
    _gemini_web_model_cache = None
    return (f"Gemini-web model changed: '{old or '(auto)'}' → "
            f"'{config.GEMINI_WEB_MODEL or '(auto)'}'. Takes effect on the next call.")


def delegate_to_gemini_web(task: str, context: str = "", max_steps: int = 10) -> str:
    from orchestration import process_chat_turn
    """
    Mirror of delegate_to_openrouter(): hand `task` off to a fresh,
    FULLY TOOL-CAPABLE agent loop running on Gemini-web — full access to
    every tool Midum has, in an ISOLATED conversation (its own
    ChatSession, not shared with the caller's), reporting back a final
    summary.
    """
    if not _GEMINI_WEBAPI_AVAILABLE:
        return f"Gemini web client is not available: {_gemini_webapi_load_msg}"

    print(f"   [Delegate → Gemini-web] Task: {task[:80]}")

    _saved_scratchpad = None
    try:
        if os.path.exists(RESPONSE_MEMORY):
            with open(RESPONSE_MEMORY, "r", encoding="utf-8") as f:
                _saved_scratchpad = f.read()
    except Exception:
        pass

    try:
        sub_system_prompt = get_system_prompt(effective_provider="gemini_web")
        sub_system_prompt += (
            "\n\n━━━ DELEGATED TASK MODE ━━━\n"
            "You have been handed a specific task by Midum (the primary agent) to "
            "complete independently. Act autonomously to complete it. When finished, "
            "reply with a clear plain-text summary of what you did and the result — "
            "this summary is relayed directly to the user."
        )
        task_message = task.strip()
        if context.strip():
            task_message = f"[CONTEXT FROM MIDUM]\n{context.strip()}\n\n[TASK]\n{task_message}"

        sub_history = [
            {"role": "system", "content": sub_system_prompt},
            {
                "role": "user",
                "content": (
                    f"{task_message}\n\n"
                    "[SYSTEM]: Act immediately. Emit the first tool-call JSON now. "
                    "Do not explain — just act. Give a final plain-text summary when done."
                ),
            },
        ]

        summary, sub_tool_outputs = process_chat_turn(
            sub_history,
            user_request=task,
            force_provider="gemini_web",
            max_steps=max_steps,
        )
        step_note = f" ({len(sub_tool_outputs)} tool call(s) executed)" if sub_tool_outputs else ""
        return f"[Gemini-web coworker — task complete{step_note}]\n{summary}"

    except Exception as e:
        return f"Delegation to Gemini-web failed: {e}"

    finally:
        try:
            if _saved_scratchpad is not None:
                with open(RESPONSE_MEMORY, "w", encoding="utf-8") as f:
                    f.write(_saved_scratchpad)
        except Exception:
            pass




# JS helpers injected into pages
_JS_GET_TEXT = """
(function() {
    // Remove script/style/noscript nodes then return innerText
    var clone = document.body.cloneNode(true);
    ['script','style','noscript','nav','footer','header'].forEach(function(tag) {
        Array.from(clone.querySelectorAll(tag)).forEach(function(el) { el.remove(); });
    });
    var text = clone.innerText || clone.textContent || '';
    // Collapse excess whitespace
    return text.replace(/[ \\t]+/g, ' ').replace(/\\n{3,}/g, '\\n\\n').trim().slice(0, 15000);
})()
"""

_JS_GET_ELEMENTS = """
(function(filterType) {
    var selectors = {
        'button':   'button, [role="button"], input[type="button"], input[type="submit"]',
        'link':     'a[href]',
        'input':    'input:not([type="hidden"]), textarea',
        'select':   'select',
        'textarea': 'textarea',
        '':         'a[href], button, input:not([type="hidden"]), select, textarea, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="option"], [tabindex]:not([tabindex="-1"])'
    };
    var sel = selectors[filterType] || selectors[''];
    var els = Array.from(document.querySelectorAll(sel));
    var results = [];
    els.forEach(function(el, i) {
        var rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return;   // hidden
        if (rect.top < -100 || rect.bottom > window.innerHeight + 100) return;  // off-screen
        var type = el.tagName.toLowerCase();
        if (el.getAttribute('role')) type = el.getAttribute('role');
        var label = el.innerText || el.value || el.placeholder ||
                    el.getAttribute('aria-label') || el.getAttribute('title') ||
                    el.getAttribute('name') || el.getAttribute('id') || '';
        label = label.trim().replace(/\\s+/g, ' ').slice(0, 80);
        results.push({
            idx: results.length,
            type: type,
            label: label,
            tag: el.tagName.toLowerCase(),
            href: el.href || '',
            x: Math.round(rect.left + rect.width/2),
            y: Math.round(rect.top  + rect.height/2),
        });
    });
    return results;
})(FILTER_TYPE_PLACEHOLDER)
"""

# Per-call DOM element snapshot cache: tab_index → list of element dicts
_browser_element_cache: dict[int, list] = {}



