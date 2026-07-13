# --- AUTO-SPLITTER: imports added by automated pass, please review ---
import config
from config import GEMINI_API_BASE, RESPONSE_MEMORY, SECRETS_FILE
from system_prompt import get_system_prompt
import json
import os
import requests
import time

# --- from main.py, section 1 ---
# GEMINI API (OFFICIAL) SETUP — real API key, no browser/cookie hacks
# =============================================================================
#
# This is a SEPARATE code path from both consult_gemini (web-chat scraping)
# and the unused google-genai SDK client (_gemini_client) loaded above. It
# talks to Google's OFFICIAL OpenAI-compatible endpoint for the Gemini API:
#     https://ai.google.dev/gemini-api/docs/openai
# which means we can reuse the exact same plain-`requests` pipeline as
# OpenRouter (_openrouter_chat) — same request shape, same normalised
# response shape, same retry logic — instead of hand-rolling anything
# genai-SDK-specific. Structured tool calling (the `tools` schema already
# used everywhere else in this file) works natively here.
#
# API KEY SETUP (one-time):
#   1. Get a free key at https://aistudio.google.com/app/apikey
#   2. Add it to the SAME secrets file used everywhere else:
#      { "GEMINI_API_KEY": "AIza...", "OPENROUTER_API_KEY": "sk-or-v1-..." }
#
_GEMINI_API_AVAILABLE = False
_GEMINI_API_KEY        = None

def _load_gemini_api():
    """Load GEMINI_API_KEY from the shared secrets file (official API path)."""
    global _GEMINI_API_AVAILABLE, _GEMINI_API_KEY
    try:
        secrets_path = os.path.abspath(SECRETS_FILE)
        if not os.path.exists(secrets_path):
            return False, f"Secrets file not found: {secrets_path}"
        with open(secrets_path, "r", encoding="utf-8") as f:
            secrets = json.load(f)
        key = secrets.get("GEMINI_API_KEY", "").strip()
        if not key:
            return False, "GEMINI_API_KEY is empty in secrets file."
        _GEMINI_API_KEY       = key
        _GEMINI_API_AVAILABLE = True
        return True, "OK"
    except Exception as e:
        return False, str(e)


_gemini_api_load_ok, _gemini_api_load_msg = _load_gemini_api()


def _sanitize_messages_for_gemini_api(messages: list) -> list:
    """
    Midum's internal conversation_history uses a loose, Ollama-shaped
    message format that OpenRouter's backend tolerates/auto-repairs, but
    Google's OFFICIAL OpenAI-compatible endpoint validates strictly and will
    400 INVALID_ARGUMENT on it. Two specific things need fixing on the way
    out, without touching the shared internal format used by every other
    provider:

    1. Assistant `tool_calls` entries here have no "id" / "type": "function"
       — added by _openrouter_chat/_call_ollama's normaliser, but OpenAI's
       schema requires both, and the FOLLOWING "tool" role message must
       carry a matching "tool_call_id".
    2. `function.arguments` is stored as a Python dict internally, but the
       OpenAI/Gemini schema requires it to be a JSON-encoded STRING.

    This walks the message list once, assigns synthetic ids to any
    assistant tool_calls that are missing them, re-serialises arguments to
    strings, and threads matching tool_call_id values onto the immediately
    following "tool" messages (FIFO, since Midum only ever emits one tool
    call per step — see "tool_calls[:1]" in process_chat_turn).
    """
    sanitized  = []
    pending_ids = []
    counter    = 0
    for m in messages:
        m = dict(m)
        role = m.get("role")

        if role == "assistant" and m.get("tool_calls"):
            new_calls = []
            for tc in m["tool_calls"]:
                fn   = (tc or {}).get("function", {}) or {}
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if not isinstance(args, str):
                    try:
                        args = json.dumps(args)
                    except Exception:
                        args = "{}"
                call_id = tc.get("id") or f"call_{counter}"
                counter += 1
                pending_ids.append(call_id)
                new_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": args},
                })
            m["tool_calls"] = new_calls
            if not m.get("content"):
                m["content"] = None   # OpenAI schema allows null content alongside tool_calls
            sanitized.append(m)

        elif role == "tool":
            if not m.get("tool_call_id"):
                m["tool_call_id"] = pending_ids.pop(0) if pending_ids else f"call_{counter}"
            sanitized.append(m)

        else:
            sanitized.append(m)

    return sanitized


def _gemini_api_chat(messages: list, model: str = None, tools_schema: list = None,
                      timeout: int = 60, _retries: int = 2) -> dict:
    """
    Call Google's OFFICIAL Gemini API through its OpenAI-compatible
    /chat/completions endpoint. Returns a dict normalised to the SAME shape
    _call_ollama / _openrouter_chat produce:
        {"message": {"role": "assistant", "content": str, "tool_calls": [...] }}
    so process_chat_turn can treat every provider identically.

    Retries on 429 (rate limit) and 502/503 (upstream overloaded), mirroring
    _openrouter_chat exactly. Raises RuntimeError with the actual error
    message from Gemini's response body on failure.
    """
    if not _GEMINI_API_AVAILABLE or not _GEMINI_API_KEY:
        raise RuntimeError(
            f"Gemini API not available: {_gemini_api_load_msg}. "
            f"Add GEMINI_API_KEY to {SECRETS_FILE}"
        )
    if requests is None:
        raise RuntimeError("requests library not installed: pip install requests")

    model = model or config.GEMINI_API_MODEL

    payload = {
        "model": model,
        "messages": _sanitize_messages_for_gemini_api(messages),
    }
    if tools_schema:
        payload["tools"] = tools_schema

    headers = {
        "Authorization": f"Bearer {_GEMINI_API_KEY}",
        "Content-Type":  "application/json",
    }

    last_err = None
    for attempt in range(_retries + 1):
        try:
            resp = requests.post(
                f"{GEMINI_API_BASE}/chat/completions",
                headers=headers, json=payload, timeout=timeout
            )

            try:
                data = resp.json()
            except Exception:
                data = {}

            if resp.status_code != 200:
                # Google's error body is sometimes a dict ({"error": {...}}) and
                # sometimes a single-element list ([{"error": {...}}]) — handle both.
                err_container = data
                if isinstance(err_container, list) and err_container:
                    err_container = err_container[0]
                err_obj = err_container.get("error", {}) if isinstance(err_container, dict) else {}
                err_msg = err_obj.get("message") or resp.text[:300] or f"HTTP {resp.status_code}"

                retryable = resp.status_code in (429, 502, 503, 504)
                if retryable and attempt < _retries:
                    wait_s = 1.5 * (attempt + 1)
                    print(f"   [Gemini API] {resp.status_code} ({err_msg[:80]}) — "
                          f"retrying in {wait_s:.1f}s...")
                    time.sleep(wait_s)
                    last_err = err_msg
                    continue

                raise RuntimeError(f"Gemini API error ({resp.status_code}): {err_msg}")

            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(f"Gemini API error: {data['error'].get('message', data['error'])}")

            break   # success

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = str(e)
            if attempt < _retries:
                wait_s = 1.5 * (attempt + 1)
                print(f"   [Gemini API] Connection issue — retrying in {wait_s:.1f}s...")
                time.sleep(wait_s)
                continue
            raise RuntimeError(f"Gemini API connection failed after {_retries + 1} attempts: {last_err}")
    else:
        raise RuntimeError(f"Gemini API failed after {_retries + 1} attempts: {last_err}")

    choice  = (data.get("choices") or [{}])[0]
    msg     = choice.get("message", {}) or {}
    content = msg.get("content") or ""
    raw_tool_calls = msg.get("tool_calls") or []

    normalised_calls = []
    for tc in raw_tool_calls:
        fn = tc.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                pass
        normalised_calls.append({"function": {"name": fn.get("name", ""), "arguments": args}})

    return {
        "message": {
            "role": "assistant",
            "content": content,
            "tool_calls": normalised_calls,
        }
    }


def consult_gemini_api(prompt: str, context: str = "", model: str = None) -> str:
    """
    Send a plain reasoning/planning prompt to the OFFICIAL Gemini API (API
    key, not web chat) — no tool schema, text generation only. Same role as
    consult_openrouter/consult_gemini, but using the metered official API.
    """
    if not _GEMINI_API_AVAILABLE:
        return f"Gemini API is not available: {_gemini_api_load_msg}"

    full_prompt = prompt.strip()
    if context.strip():
        if len(context) > 60000:
            context = context[:30000] + "\n... [TRUNCATED] ...\n" + context[-30000:]
        full_prompt = "[CONTEXT]\n" + context.strip() + "\n\n[TASK]\n" + full_prompt

    try:
        use_model = model or config.GEMINI_API_MODEL
        print(f"   [Gemini API] Model: {use_model}")
        resp   = _gemini_api_chat(
            [{"role": "user", "content": full_prompt}],
            model=use_model
        )
        result = (resp["message"]["content"] or "").strip()
        print(f"   [Gemini API] Response received ({len(result)} chars)")
        return f"[Gemini-API/{use_model}]\n" + result
    except Exception as e:
        return f"Gemini API error: {str(e)}"


def delegate_to_gemini_api(task: str, context: str = "", model: str = None,
                            max_steps: int = 10) -> str:
    from orchestration import process_chat_turn
    """
    Mirror of delegate_to_openrouter(): hand `task` off to a fresh, FULLY
    TOOL-CAPABLE agent loop running on the official Gemini API — it can call
    UIA, CDP, filesystem, terminal, MCP tools, or any other tool Midum has,
    exactly like the primary loop, then reports back a final summary.

    Runs process_chat_turn on a brand-new isolated conversation seeded with
    the task, with force_provider="gemini_api" so it runs on the Gemini API
    regardless of the global MODEL_PROVIDER. Does NOT share conversation
    history with the outer loop — only the final summary comes back.
    """
    if not _GEMINI_API_AVAILABLE:
        return f"Gemini API is not available: {_gemini_api_load_msg}"

    use_model = model or config.GEMINI_API_MODEL
    print(f"   [Delegate → Gemini-API/{use_model}] Task: {task[:80]}")

    _saved_scratchpad = None
    try:
        if os.path.exists(RESPONSE_MEMORY):
            with open(RESPONSE_MEMORY, "r", encoding="utf-8") as f:
                _saved_scratchpad = f.read()
    except Exception:
        pass

    try:
        sub_system_prompt = get_system_prompt(
            effective_provider="gemini_api", effective_model=use_model
        )
        sub_system_prompt += (
            "\n\n━━━ DELEGATED TASK MODE ━━━\n"
            "You have been handed a specific task by Midum (the primary agent) to "
            "complete independently. You have FULL access to every tool listed above — "
            "act autonomously to complete it, calling tools directly rather than asking "
            "anyone for permission. When finished, reply with a clear plain-text summary "
            "of what you did and the result — this summary is relayed directly to the "
            "user, so make it complete and readable on its own."
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
                    "[SYSTEM]: Act immediately. Execute the first tool call now. "
                    "Do not explain — just act. Give a final plain-text summary when done."
                ),
            },
        ]

        summary, sub_tool_outputs = process_chat_turn(
            sub_history,
            user_request=task,
            force_provider="gemini_api",
            force_model=use_model,
            max_steps=max_steps,
        )

        step_note = f" ({len(sub_tool_outputs)} tool call(s) executed)" if sub_tool_outputs else ""
        return f"[Gemini-API coworker/{use_model} — task complete{step_note}]\n{summary}"

    except Exception as e:
        return f"Delegation to Gemini API failed: {e}"

    finally:
        try:
            if _saved_scratchpad is not None:
                with open(RESPONSE_MEMORY, "w", encoding="utf-8") as f:
                    f.write(_saved_scratchpad)
        except Exception:
            pass


def set_gemini_api_model(model_id: str) -> str:
    """
    Switch config.GEMINI_API_MODEL at runtime — no restart needed. Applies
    immediately to consult_gemini_api, delegate_to_gemini_api, and (if
    MODEL_PROVIDER == "gemini_api") the primary execution loop.
    """

    old = config.GEMINI_API_MODEL
    new = model_id.strip()
    if not new:
        return "Error: empty model ID."
    config.GEMINI_API_MODEL = new
    print(f"🔀 [Gemini API model switched: '{old}' → '{new}']")
    return (
        f"Gemini API model switched: '{old}' → '{new}'. "
        f"This is in-memory only for the current session — edit config.GEMINI_API_MODEL "
        f"in main.py to make it the default on next launch."
    )


# =============================================================================

