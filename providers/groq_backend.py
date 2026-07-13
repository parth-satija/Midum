# --- AUTO-SPLITTER: imports added by automated pass, please review ---
import config
from config import GROQ_API_BASE, GROQ_FALLBACK_MODELS, RESPONSE_MEMORY, SECRETS_FILE
from system_prompt import get_system_prompt
import json
import os
import requests
import time

# --- from main.py, section 1 ---
# GROQCLOUD SETUP
# =============================================================================
# GroqCloud (https://console.groq.com) runs open models (Llama, Qwen,
# DeepSeek, Kimi, ...) on custom LPU hardware, and exposes a genuinely free
# tier — no credit card required — through the SAME OpenAI-compatible
# /chat/completions endpoint shape as OpenRouter and the official Gemini
# API. That means we can reuse the exact same plain-`requests` pipeline
# (same request shape, same normalised response shape, same retry/fallback
# logic) instead of hand-rolling anything Groq-specific. Structured tool
# calling works natively and is reliable on Groq's supported models.
#
# API KEY SETUP (one-time):
#   1. Get a free key at https://console.groq.com/keys
#   2. Add it to the SAME secrets file used everywhere else:
#      { "GEMINI_API_KEY": "...", "OPENROUTER_API_KEY": "...", "GROQ_API_KEY": "gsk_..." }
#
_GROQ_AVAILABLE = False
_GROQ_API_KEY    = None

def _load_groq():
    """Load GROQ_API_KEY from the shared secrets file."""
    global _GROQ_AVAILABLE, _GROQ_API_KEY
    try:
        secrets_path = os.path.abspath(SECRETS_FILE)
        if not os.path.exists(secrets_path):
            return False, f"Secrets file not found: {secrets_path}"
        with open(secrets_path, "r", encoding="utf-8") as f:
            secrets = json.load(f)
        key = secrets.get("GROQ_API_KEY", "").strip()
        if not key:
            return False, "GROQ_API_KEY is empty in secrets file."
        _GROQ_API_KEY   = key
        _GROQ_AVAILABLE = True
        return True, "OK"
    except Exception as e:
        return False, str(e)


_groq_load_ok, _groq_load_msg = _load_groq()


def _sanitize_messages_for_groq(messages: list) -> list:
    """
    Same fixup as _sanitize_messages_for_gemini_api(): Midum's internal
    conversation_history uses a loose, Ollama-shaped message format.
    GroqCloud's OpenAI-compatible endpoint validates strictly and will 400
    on missing tool_call ids / non-string function.arguments, so we repair
    those on the way out without touching the shared internal format used
    by every other provider.
    """
    sanitized   = []
    pending_ids = []
    counter     = 0
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
                m["content"] = None
            sanitized.append(m)

        elif role == "tool":
            if not m.get("tool_call_id"):
                m["tool_call_id"] = pending_ids.pop(0) if pending_ids else f"call_{counter}"
            sanitized.append(m)

        else:
            sanitized.append(m)

    return sanitized


def _groq_chat(messages: list, model: str = None, tools_schema: list = None,
               timeout: int = 60, _retries: int = 2) -> dict:
    """
    Call GroqCloud's OpenAI-compatible /chat/completions endpoint. Returns a
    dict normalised to the SAME shape _call_ollama / _openrouter_chat /
    _gemini_api_chat produce:
        {"message": {"role": "assistant", "content": str, "tool_calls": [...] }}
    so process_chat_turn can treat every provider identically.

    Retries on 429 (rate limit) and 502/503 (upstream overloaded), mirroring
    _openrouter_chat / _gemini_api_chat exactly. Raises RuntimeError with the
    actual error message from Groq's response body on failure.
    """
    if not _GROQ_AVAILABLE or not _GROQ_API_KEY:
        raise RuntimeError(
            f"GroqCloud not available: {_groq_load_msg}. "
            f"Add GROQ_API_KEY to {SECRETS_FILE}"
        )
    if requests is None:
        raise RuntimeError("requests library not installed: pip install requests")

    model = model or config.GROQ_MODEL

    payload = {
        "model": model,
        "messages": _sanitize_messages_for_groq(messages),
    }
    if tools_schema:
        payload["tools"] = tools_schema

    headers = {
        "Authorization": f"Bearer {_GROQ_API_KEY}",
        "Content-Type":  "application/json",
    }

    last_err = None
    for attempt in range(_retries + 1):
        try:
            resp = requests.post(
                f"{GROQ_API_BASE}/chat/completions",
                headers=headers, json=payload, timeout=timeout
            )

            try:
                data = resp.json()
            except Exception:
                data = {}

            if resp.status_code != 200:
                err_obj = data.get("error", {}) if isinstance(data, dict) else {}
                err_msg = err_obj.get("message") or resp.text[:300] or f"HTTP {resp.status_code}"

                retryable = resp.status_code in (429, 502, 503, 504)
                if retryable and attempt < _retries:
                    wait_s = 1.5 * (attempt + 1)
                    print(f"   [Groq] {resp.status_code} ({err_msg[:80]}) — "
                          f"retrying in {wait_s:.1f}s...")
                    time.sleep(wait_s)
                    last_err = err_msg
                    continue

                raise RuntimeError(f"Groq error ({resp.status_code}): {err_msg}")

            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(f"Groq error: {data['error'].get('message', data['error'])}")

            break   # success

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = str(e)
            if attempt < _retries:
                wait_s = 1.5 * (attempt + 1)
                print(f"   [Groq] Connection issue — retrying in {wait_s:.1f}s...")
                time.sleep(wait_s)
                continue
            raise RuntimeError(f"Groq connection failed after {_retries + 1} attempts: {last_err}")
    else:
        raise RuntimeError(f"Groq failed after {_retries + 1} attempts: {last_err}")

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


def _groq_chat_with_fallback(messages: list, model: str = None, tools_schema: list = None,
                              timeout: int = 60) -> dict:
    """
    Wraps _groq_chat() with automatic fallback across GROQ_FALLBACK_MODELS,
    mirroring _openrouter_chat_with_fallback(). If the requested (or
    default) model repeatedly fails with a retryable error, try the next
    model in the fallback chain instead of failing the whole turn.
    """
    requested = model or config.GROQ_MODEL
    chain = [requested] + [m for m in GROQ_FALLBACK_MODELS if m != requested]

    last_exc = None
    for m in chain:
        try:
            resp = _groq_chat(messages, model=m, tools_schema=tools_schema, timeout=timeout)
            if m != requested:
                print(f"   [Groq] Fell back to model: {m}")
            return resp
        except Exception as e:
            last_exc = e
            print(f"   [Groq] Model '{m}' failed ({e}) — trying next fallback...")
            continue
    raise RuntimeError(f"All Groq models failed. Last error: {last_exc}")


def consult_groq(prompt: str, context: str = "", model: str = None) -> str:
    """
    Send a plain reasoning/planning prompt to GroqCloud — no tool schema,
    text generation only. Same role as consult_openrouter/consult_gemini_api,
    but using GroqCloud's free-tier fast inference.
    """
    if not _GROQ_AVAILABLE:
        return f"GroqCloud is not available: {_groq_load_msg}"

    full_prompt = prompt.strip()
    if context.strip():
        if len(context) > 60000:
            context = context[:30000] + "\n... [TRUNCATED] ...\n" + context[-30000:]
        full_prompt = "[CONTEXT]\n" + context.strip() + "\n\n[TASK]\n" + full_prompt

    try:
        use_model = model or config.GROQ_MODEL
        print(f"   [Groq] Model: {use_model}")
        resp   = _groq_chat_with_fallback(
            [{"role": "user", "content": full_prompt}],
            model=use_model
        )
        result = (resp["message"]["content"] or "").strip()
        print(f"   [Groq] Response received ({len(result)} chars)")
        return f"[Groq/{use_model}]\n" + result
    except Exception as e:
        return f"GroqCloud error: {str(e)}"


def delegate_to_groq(task: str, context: str = "", model: str = None,
                      max_steps: int = 10) -> str:
    from orchestration import process_chat_turn
    """
    Mirror of delegate_to_openrouter()/delegate_to_gemini_api(): hand `task`
    off to a fresh, FULLY TOOL-CAPABLE agent loop running on GroqCloud — it
    can call UIA, CDP, filesystem, terminal, MCP tools, or any other tool
    Midum has, exactly like the primary loop, then reports back a final
    summary.

    Runs process_chat_turn on a brand-new isolated conversation seeded with
    the task, with force_provider="groq" so it runs on GroqCloud regardless
    of the global MODEL_PROVIDER. Does NOT share conversation history with
    the outer loop — only the final summary comes back.
    """
    if not _GROQ_AVAILABLE:
        return f"GroqCloud is not available: {_groq_load_msg}"

    use_model = model or config.GROQ_MODEL
    print(f"   [Delegate → Groq/{use_model}] Task: {task[:80]}")

    _saved_scratchpad = None
    try:
        if os.path.exists(RESPONSE_MEMORY):
            with open(RESPONSE_MEMORY, "r", encoding="utf-8") as f:
                _saved_scratchpad = f.read()
    except Exception:
        pass

    try:
        sub_system_prompt = get_system_prompt(
            effective_provider="groq", effective_model=use_model
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
            force_provider="groq",
            force_model=use_model,
            max_steps=max_steps,
        )

        step_note = f" ({len(sub_tool_outputs)} tool call(s) executed)" if sub_tool_outputs else ""
        return f"[Groq coworker/{use_model} — task complete{step_note}]\n{summary}"

    except Exception as e:
        return f"Delegation to GroqCloud failed: {e}"

    finally:
        try:
            if _saved_scratchpad is not None:
                with open(RESPONSE_MEMORY, "w", encoding="utf-8") as f:
                    f.write(_saved_scratchpad)
        except Exception:
            pass


def set_groq_model(model_id: str) -> str:
    """
    Switch config.GROQ_MODEL at runtime — no restart needed. Applies immediately to
    consult_groq, delegate_to_groq, and (if MODEL_PROVIDER == "groq") the
    primary execution loop, since all three read the module-level global.
    """

    old = config.GROQ_MODEL
    new = model_id.strip()
    if not new:
        return "Error: empty model ID."
    config.GROQ_MODEL = new
    print(f"🔀 [Groq model switched: '{old}' → '{new}']")
    return (
        f"Groq model switched: '{old}' → '{new}'. "
        f"This is in-memory only for the current session — edit config.GROQ_MODEL "
        f"in main.py to make it the default on next launch."
    )


def list_groq_models() -> str:
    from tools_registry import _store_index
    """
    Fetch the live list of models currently available on GroqCloud from
    /models, format it as a numbered indexed table. Follow up with
    set_groq_model_by_index(N) or set_groq_model(model_id) to switch.
    """
    if not _GROQ_AVAILABLE:
        return f"GroqCloud is not available: {_groq_load_msg}"
    if requests is None:
        return "requests library not installed: pip install requests"

    try:
        headers = {"Authorization": f"Bearer {_GROQ_API_KEY}"}
        resp = requests.get(f"{GROQ_API_BASE}/models", headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", [])
    except Exception as e:
        return f"Failed to fetch Groq model list: {e}"

    if not items:
        return "No models returned by GroqCloud."

    items.sort(key=lambda m: m.get("id", ""))
    _store_index("groq_models", items)

    lines = [
        f"currently active: {config.GROQ_MODEL}",
        "Use set_groq_model_by_index(index) to switch.",
        ""
    ]
    for i, m in enumerate(items):
        lines.append(f"[{i}] {m.get('id', '?')} (owner: {m.get('owned_by', '?')})")
    return "\n".join(lines)


def set_groq_model_by_index(index: int) -> str:
    from tools_registry import _get_indexed
    """Switch config.GROQ_MODEL to the entry at `index` from list_groq_models."""
    entry = _get_indexed("groq_models", index)
    if not entry:
        return f"Index {index} not found. Call list_groq_models() first."
    return set_groq_model(entry["id"])


# =============================================================================

