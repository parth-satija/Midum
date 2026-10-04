# --- AUTO-SPLITTER: imports added by automated pass, please review ---
import config
from config import OPENAI_WEB_HOP_TIMEOUT, OPENAI_WEB_TOOL_RESULT_MARKER, RESPONSE_MEMORY, SECRETS_FILE
from system_prompt import get_system_prompt
import asyncio
import json
import os
import threading

# --- from main.py, section 1 ---
# CHATGPT WEB APP CLIENT — via community `llm_cookie_bridge` library
# =============================================================================
#
# query_openai_app() drives ChatGPT the same way query_gemini_app() drives
# Gemini: not a real Chrome tab over CDP, and not the metered OpenAI API,
# but a small community-maintained library that talks directly to
# chatgpt.com's internal endpoints using your browser's session cookie.
# No page load, no clicking into a prompt box, no clipboard — just a plain
# async HTTP call through the FREE ChatGPT web app's own usage limits.
#
# Library: `llm_cookie_bridge` (https://pypi.org/project/llm-cookie-bridge/)
# — the same package this codebase could in principle also use for the
# Gemini/Claude/Perplexity web apps it supports, but here it's used purely
# as the ChatGPT/OpenAI-web backend, parallel to gemini_web_backend.py's
# use of `gemini_webapi`.
#
# INSTALLATION:
#   pip install -U llm-cookie-bridge
#
# AUTHENTICATION (one-time):
#   Log into https://chatgpt.com, open DevTools (F12) -> Application ->
#   Cookies -> chatgpt.com, and copy the value of the
#   `__Secure-next-auth.session-token` cookie into the secrets file:
#     { "CHATGPT_SESSION_TOKEN": "..." }
#
#   NOTE — chunked cookie: NextAuth splits the session token across several
#   cookies (`__Secure-next-auth.session-token.0`, `.1`, sometimes more)
#   whenever the JWT is too long to fit in one ~4KB cookie, which is the
#   common case for ChatGPT. If you see numbered variants instead of a
#   single `__Secure-next-auth.session-token` cookie, either:
#     a) paste each chunk into its own secrets key — CHATGPT_SESSION_TOKEN_0,
#        CHATGPT_SESSION_TOKEN_1, CHATGPT_SESSION_TOKEN_2, ... — and leave
#        CHATGPT_SESSION_TOKEN blank; they're reassembled in order
#        automatically (see _resolve_chatgpt_session_token below), or
#     b) manually concatenate the chunk values yourself, in order, with no
#        separator, and put the single resulting string in
#        CHATGPT_SESSION_TOKEN directly.
# =============================================================================

_OPENAI_WEB_AVAILABLE = False
_openai_web_load_msg  = ""
try:
    from llm_cookie_bridge import LLMCookieBridge as _LLMCookieBridge
    _OPENAI_WEB_AVAILABLE = True
except ImportError as _e:
    _LLMCookieBridge = None
    _openai_web_load_msg = f"llm_cookie_bridge not installed: {_e}. Run: pip install -U llm-cookie-bridge"

_openai_web_bridge      = None   # lazily-initialised LLMCookieBridge (singleton)
_openai_web_bridge_lock = threading.Lock()

_openai_async_loop   = None       # persistent background asyncio loop
_openai_async_thread = None


def _get_openai_async_loop():
    """
    Start (once) a background thread running a persistent asyncio event
    loop. llm_cookie_bridge is async; this lets the rest of this otherwise-
    sync script call into it and block for a result, mirroring
    gemini_web_backend._get_gemini_async_loop() exactly.
    """
    global _openai_async_loop, _openai_async_thread
    if _openai_async_loop is not None:
        return _openai_async_loop

    _openai_async_loop = asyncio.new_event_loop()

    def _runner():
        asyncio.set_event_loop(_openai_async_loop)
        _openai_async_loop.run_forever()

    _openai_async_thread = threading.Thread(target=_runner, daemon=True, name="openai-webbridge-loop")
    _openai_async_thread.start()
    return _openai_async_loop


def _run_openai_coro(coro, timeout: float = 90.0):
    """Run an llm_cookie_bridge coroutine on the background loop and block for the result."""
    loop = _get_openai_async_loop()
    fut  = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)


def _resolve_chatgpt_session_token(secrets: dict) -> str:
    """
    Resolve the ChatGPT session token from the secrets dict, handling both
    shapes:

      1. A single already-assembled token under CHATGPT_SESSION_TOKEN
         (the common case — either the site only issued one cookie, or the
         user manually concatenated the chunks themselves).
      2. NextAuth's chunked-cookie shape, where the site issued
         __Secure-next-auth.session-token.0 / .1 / .2 / ... instead of a
         single cookie because the JWT was too long for one ~4KB cookie —
         common for ChatGPT. Reassembled here from CHATGPT_SESSION_TOKEN_0,
         CHATGPT_SESSION_TOKEN_1, ... (as many as are present, in numeric
         order, concatenated with NO separator — that's how NextAuth itself
         reconstructs the JWT from the chunked cookies).

    (1) takes priority if both are somehow present. Returns "" if neither
    shape yields anything.
    """
    single = (secrets.get("CHATGPT_SESSION_TOKEN", "") or "").strip()
    if single:
        return single

    chunks = []
    i = 0
    while True:
        key = f"CHATGPT_SESSION_TOKEN_{i}"
        if key not in secrets:
            break
        chunks.append((secrets.get(key) or "").strip())
        i += 1
    return "".join(chunks)


def _get_openai_web_bridge():
    """
    Lazily create (and cache) the LLMCookieBridge("chatgpt") singleton,
    already entered as an async context manager (so its underlying
    httpx.AsyncClient stays open across calls instead of being torn down
    after every single turn).

    Cookie resolution: CHATGPT_SESSION_TOKEN from the secrets file. Unlike
    gemini_webapi, llm_cookie_bridge has no browser-cookie3 auto-detection
    fallback — the session token must be supplied explicitly.

    Returns (bridge, None) on success, or (None, error_message) on failure.
    """
    global _openai_web_bridge
    if _openai_web_bridge is not None:
        return _openai_web_bridge, None
    if not _OPENAI_WEB_AVAILABLE:
        return None, _openai_web_load_msg

    with _openai_web_bridge_lock:
        if _openai_web_bridge is not None:
            return _openai_web_bridge, None

        session_token = ""
        secrets_path = os.path.abspath(SECRETS_FILE)
        try:
            if os.path.exists(secrets_path):
                with open(secrets_path, "r", encoding="utf-8") as f:
                    secrets = json.load(f)
                session_token = _resolve_chatgpt_session_token(secrets)
        except Exception:
            pass

        if not session_token:
            return None, (
                f"No CHATGPT_SESSION_TOKEN (or CHATGPT_SESSION_TOKEN_0/_1/...) configured. "
                f"Add it to the secrets file ({secrets_path}) — log into https://chatgpt.com, "
                f"open DevTools (F12) -> Application -> Cookies -> chatgpt.com, and copy the "
                f"'__Secure-next-auth.session-token' cookie value (or, if NextAuth split it into "
                f"'.0'/'.1'/... chunks, paste each chunk into CHATGPT_SESSION_TOKEN_0, "
                f"CHATGPT_SESSION_TOKEN_1, etc. instead)."
            )

        try:
            bridge = _LLMCookieBridge.create(
                "chatgpt",
                cookies={"__Secure-next-auth.session-token": session_token},
            )
            _run_openai_coro(bridge.__aenter__(), timeout=35)
            _openai_web_bridge = bridge
            return _openai_web_bridge, None
        except Exception as e:
            return None, (
                f"Failed to initialise ChatGPT web bridge: {e}. Refresh "
                f"CHATGPT_SESSION_TOKEN in the secrets file ({secrets_path}) "
                f"from a fresh https://chatgpt.com login."
            )


def _reset_openai_web_bridge():
    """Drop the cached bridge (used on unrecoverable auth/session failures,
    so the next call re-initialises from scratch instead of hammering a
    dead session)."""
    global _openai_web_bridge
    bridge, _openai_web_bridge = _openai_web_bridge, None
    if bridge is not None:
        try:
            _run_openai_coro(bridge.__aexit__(None, None, None), timeout=10)
        except Exception:
            pass


# =============================================================================

# --- from main.py, section 2 ---
# OPENAI-WEB PRIMARY EXECUTION BACKEND
# =============================================================================
# Implements MODEL_PROVIDER == "openai_web": ChatGPT, driven through the
# same llm_cookie_bridge client/session plumbing set up above, as a full
# tool-calling execution brain sitting alongside Ollama/OpenRouter/
# Gemini-web. Mirrors gemini_web_backend.py's design one-for-one:
#
#   - NO custom GPT / persistent server-side object. The persona + JSON
#     tool-call-format instructions are sent as a plain-text priming
#     message — the FIRST message of a fresh conversation — exactly once
#     per conversation_history object, no custom-GPT create/update calls
#     anywhere in this backend.
#   - The native tool schema is NOT inlined into that priming message
#     either. ChatGPT gets list_native_tools()/show_native_tool_schema(...)
#     and discovers native tools on demand in-turn, the exact same pattern
#     gemini_web_backend.py uses. Keeps every hop's prompt small.
#   - Unlike gemini_webapi's ChatSession, llm_cookie_bridge's chat()/
#     stream() calls are NOT confirmed to carry an updatable server-side
#     session object across independent calls beyond returning a
#     conversation_id — so, to avoid silently losing context on a library
#     whose multi-turn threading contract isn't guaranteed, every hop here
#     resends the full rendered transcript (system persona once, then
#     every user/tool message so far) as ONE prompt string, the same
#     "diverged/fresh session" fallback path gemini_web_backend.py already
#     falls back to when its own session state goes stale. This costs more
#     tokens per hop than a true persistent session would, but is the
#     honest, correct behavior given what the library actually guarantees.
#   - Output is parsed with the exact same legacy JSON tool-call parser used
#     for Ollama/OpenRouter/Gemini-web free-text tool calls
#     (_extract_legacy_tool_calls).
# =============================================================================

# Concurrency guard: the scraped-cookie session backing _openai_web_bridge is
# a SINGLE account session. Multiple simultaneous tool-loop tasks hammering
# it concurrently would interleave conversation state unpredictably. Every
# ChatGPT-web primary call is serialized through this lock, mirroring
# gemini_web_backend._gemini_primary_call_lock.
_openai_primary_call_lock = threading.Lock()

# One rendered-transcript state per conversation_history object, keyed by
# id() (conversation_history is a plain list owned/mutated by the caller,
# never replaced mid-conversation in this codebase, so id() is stable for
# the conversation's lifetime).
#   id(conversation_history) -> {
#       "sent":            list[dict],  # exact message objects already delivered
#       "conversation_id": str | None,  # last conversation_id llm_cookie_bridge returned, FYI/logging only
#   }
_openai_web_sessions: dict = {}


def _openai_web_persona_prompt() -> str:
    """
    Builds the plain-text persona + JSON tool-call output convention sent
    as the FIRST message of every fresh conversation. Deliberately mirrors
    gemini_web_backend._gemini_web_persona_prompt() — same tool-call JSON
    contract, same on-demand tool discovery via list_native_tools()/
    show_native_tool_schema()/list_mcp_servers()/show_server_tools(), so
    the rest of the harness (parser, dispatcher) doesn't need to care which
    web-scraped provider is driving the loop.
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


def _openai_web_render_message(msg: dict) -> str | None:
    """
    Render one OpenAI-style conversation_history message into ChatGPT-web
    prompt text. Mirrors gemini_web_backend._gemini_web_render_message(),
    EXCEPT assistant turns are NOT dropped: because every hop here resends
    the full transcript as one prompt (no confirmed persistent session —
    see the module docstring above), ChatGPT's own prior replies must be
    included so it has continuity, unlike the gemini_webapi ChatSession
    case where the server already remembers them.
    """
    role    = msg.get("role", "")
    content = msg.get("content", "")
    if isinstance(content, list):
        content = "\n".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    content = (content or "").strip()
    if not content:
        return None

    if role == "system":
        # The persona/system prompt is primed once as the first turn of a
        # fresh transcript (see _openai_web_persona_prompt /
        # _call_openai_web_primary) — resending conversation_history's own
        # system entries on every hop would just bloat each round trip.
        return None
    if role == "assistant":
        return f"[ASSISTANT]: {content}"
    if role == "tool":
        return f"{OPENAI_WEB_TOOL_RESULT_MARKER} {content}"
    # role == "user" (or unknown, treated as a plain user turn)
    return f"[USER]: {content}"


def _openai_web_get_transcript(conversation_history: list):
    """
    Get-or-create the tracked state for this conversation, and return
    (delta_messages, is_new) where delta_messages is the list of
    conversation_history entries not yet delivered, and is_new is True the
    very first time this conversation is seen (so the caller primes it
    with the persona prompt).

    If conversation_history has been rewritten out from under us in a way
    that isn't a clean append (sliding history window, or a genuinely new
    task reusing the same list object), the safe move — same as
    gemini_web_backend._gemini_web_get_session() — is to treat it as a
    fresh conversation and resend the full current window as one prompt.
    """
    key   = id(conversation_history)
    state = _openai_web_sessions.get(key)

    if state is not None:
        sent = state["sent"]
        if len(conversation_history) >= len(sent) and conversation_history[:len(sent)] == sent:
            delta = conversation_history[len(sent):]
            state["sent"] = list(conversation_history)
            return delta, False
        print("   [OpenAI-web] Conversation history diverged from tracked state — "
              "starting a fresh conversation for this task.")

    _openai_web_sessions[key] = {"sent": list(conversation_history), "conversation_id": None}
    return list(conversation_history), True


def _openai_web_reset_transcript(conversation_history: list):
    """Drop the cached transcript state for this conversation (used on
    unrecoverable session/auth failures)."""
    _openai_web_sessions.pop(id(conversation_history), None)


def _call_openai_web_primary(messages, result_q, model_override: str = None):
    from orchestration import _extract_legacy_tool_calls
    """
    Drives ONE step of the primary tool-calling loop via ChatGPT through
    llm_cookie_bridge. Populates result_q with the exact same ("ok", resp) /
    ("err", exc) contract as _call_gemini_web_primary/_call_ollama/
    _call_openrouter_primary, so process_chat_turn is completely unaware
    ChatGPT-web is driving instead of any other backend.

    `messages` here is the SAME trimmed conversation_history list
    process_chat_turn already threads through every other backend.

    Session-recovery: on an auth/session failure mid-loop, one bridge
    reinit + one retry is attempted before surfacing a clear recoverable
    error — never silently corrupting the loop by pretending the call
    succeeded.
    """
    def _attempt():
        bridge, err = _get_openai_web_bridge()
        if err:
            raise RuntimeError(f"ChatGPT web bridge unavailable: {err}")

        delta, is_new = _openai_web_get_transcript(messages)

        rendered = [r for r in (_openai_web_render_message(m) for m in delta) if r]
        if is_new:
            # No custom GPT here — prime the fresh conversation with the
            # persona + tool-call-format instructions as the very first
            # turn, exactly once per conversation.
            rendered = [_openai_web_persona_prompt()] + rendered
        if not rendered:
            prompt = "[SYSTEM]: Continue with the task."
        else:
            prompt = "\n\n".join(rendered)

        with _openai_primary_call_lock:
            response = _run_openai_coro(
                bridge.chat(prompt),
                timeout=OPENAI_WEB_HOP_TIMEOUT,
            )

        state = _openai_web_sessions.get(id(messages))
        if state is not None:
            state["conversation_id"] = getattr(response, "conversation_id", None)

        return response

    try:
        try:
            response = _attempt()
        except Exception as e:
            # Session fragility is a first-class risk here too (expired
            # session token, Cloudflare challenge, etc) — one recovery
            # attempt: drop the dead bridge/transcript and retry once
            # before giving up.
            print(f"⚠️  [OpenAI-web] Session/request error mid-loop ({e}) — "
                  f"reinitializing and retrying once...")
            _reset_openai_web_bridge()
            _openai_web_reset_transcript(messages)
            response = _attempt()

        raw_text = getattr(response, "text", "") or ""

        legacy_calls, cleaned_content = _extract_legacy_tool_calls(raw_text)
        if legacy_calls:
            legacy_calls = legacy_calls[:1]   # one tool at a time

        resp = {
            "message": {
                "role": "assistant",
                "content": cleaned_content,
                "tool_calls": legacy_calls,
            }
        }
        result_q.put(("ok", resp))

    except Exception as e:
        _openai_web_reset_transcript(messages)
        result_q.put(("err", (
            f"OpenAI-web session/request failed: {e}. If this persists, the "
            f"scraped session token likely expired — refresh "
            f"CHATGPT_SESSION_TOKEN in the secrets file from a fresh "
            f"https://chatgpt.com login."
        )))


def delegate_to_openai_web(task: str, context: str = "", max_steps: int = 10) -> str:
    from orchestration import process_chat_turn
    """
    Mirror of delegate_to_gemini_web(): hand `task` off to a fresh, FULLY
    TOOL-CAPABLE agent loop running on ChatGPT-web — full access to every
    tool Midum has, in an ISOLATED conversation (its own transcript state,
    not shared with the caller's), reporting back a final summary.
    """
    if not _OPENAI_WEB_AVAILABLE:
        return f"ChatGPT web bridge is not available: {_openai_web_load_msg}"

    print(f"   [Delegate → OpenAI-web] Task: {task[:80]}")

    _saved_scratchpad = None
    try:
        if os.path.exists(RESPONSE_MEMORY):
            with open(RESPONSE_MEMORY, "r", encoding="utf-8") as f:
                _saved_scratchpad = f.read()
    except Exception:
        pass

    try:
        sub_system_prompt = get_system_prompt(effective_provider="openai_web")
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
            force_provider="openai_web",
            max_steps=max_steps,
        )
        step_note = f" ({len(sub_tool_outputs)} tool call(s) executed)" if sub_tool_outputs else ""
        return f"[OpenAI-web coworker — task complete{step_note}]\n{summary}"

    except Exception as e:
        return f"Delegation to OpenAI-web failed: {e}"

    finally:
        try:
            if _saved_scratchpad is not None:
                with open(RESPONSE_MEMORY, "w", encoding="utf-8") as f:
                    f.write(_saved_scratchpad)
        except Exception:
            pass
