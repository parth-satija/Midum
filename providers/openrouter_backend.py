# --- AUTO-SPLITTER: imports added by automated pass, please review ---
import config
from config import OPENROUTER_API_BASE, OPENROUTER_FALLBACK_MODELS, RESPONSE_MEMORY, SECRETS_FILE
import json
import os
import requests
import time

# --- from main.py, section 1 ---
# OPENROUTER SETUP
# =============================================================================
#
# INSTALLATION:
#   pip install requests   (already required elsewhere in this file)
#
# API KEY SETUP (one-time):
#   1. Get a key at https://openrouter.ai/keys
#   2. Add it to the SAME secrets file used for Gemini:
#      { "GEMINI_API_KEY": "...", "OPENROUTER_API_KEY": "sk-or-v1-..." }
#
# OpenRouter exposes an OpenAI-compatible /chat/completions endpoint, so this
# uses plain `requests` rather than a dedicated SDK — one less dependency.
#
_OPENROUTER_AVAILABLE = False
_OPENROUTER_API_KEY   = None

def _load_openrouter():
    """Load OpenRouter API key from the shared secrets file."""
    global _OPENROUTER_AVAILABLE, _OPENROUTER_API_KEY
    try:
        secrets_path = os.path.abspath(SECRETS_FILE)
        if not os.path.exists(secrets_path):
            return False, f"Secrets file not found: {secrets_path}"
        with open(secrets_path, "r", encoding="utf-8") as f:
            secrets = json.load(f)
        key = secrets.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            return False, "OPENROUTER_API_KEY is empty in secrets file."
        _OPENROUTER_API_KEY   = key
        _OPENROUTER_AVAILABLE = True
        return True, "OK"
    except Exception as e:
        return False, str(e)


_openrouter_load_ok, _openrouter_load_msg = _load_openrouter()


def _openrouter_chat(messages: list, model: str = None, tools_schema: list = None,
                      timeout: int = 60, _retries: int = 4) -> dict:
    """
    Call OpenRouter's OpenAI-compatible /chat/completions endpoint.
    Returns a dict normalised to the SAME shape _call_ollama produces:
        {"message": {"role": "assistant", "content": str, "tool_calls": [...] }}
    so process_chat_turn can treat Ollama and OpenRouter identically.

    Retries on 429 (rate limit) and 502/503 (upstream overloaded) — both are
    common on OpenRouter's free-tier models, which is the primary use case
    for OPENROUTER_CONSULT_MODE="always". Raises RuntimeError with the
    ACTUAL error message from OpenRouter's response body (not just a generic
    HTTP status), since that body usually says exactly what went wrong
    (rate-limited, model overloaded, invalid key, etc).
    """
    if not _OPENROUTER_AVAILABLE or not _OPENROUTER_API_KEY:
        raise RuntimeError(
            f"OpenRouter not available: {_openrouter_load_msg}. "
            f"Add OPENROUTER_API_KEY to {SECRETS_FILE}"
        )
    if requests is None:
        raise RuntimeError("requests library not installed: pip install requests")

    model = model or config.OPENROUTER_MODEL

    payload = {
        "model": model,
        "messages": messages,
    }
    if tools_schema:
        payload["tools"] = tools_schema

    headers = {
        "Authorization": f"Bearer {_OPENROUTER_API_KEY}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://github.com/jarvis-local-agent",
        "X-Title":       "Midum Desktop Agent",
    }

    last_err = None
    for attempt in range(_retries + 1):
        try:
            resp = requests.post(
                f"{OPENROUTER_API_BASE}/chat/completions",
                headers=headers, json=payload, timeout=timeout
            )

            # Pull the response body regardless of status — OpenRouter puts
            # the real error reason in JSON even on 4xx/5xx responses.
            try:
                data = resp.json()
            except Exception:
                data = {}

            if resp.status_code != 200:
                err_obj = data.get("error", {}) if isinstance(data, dict) else {}
                err_msg = err_obj.get("message") or resp.text[:200] or f"HTTP {resp.status_code}"

                retryable = resp.status_code in (429, 502, 503, 504)
                if retryable and attempt < _retries:
                    # Exponential backoff with a cap, plus small jitter, since
                    # free-tier 429s are a SHARED rate-limit pool — a fixed
                    # short wait often isn't enough for it to clear.
                    import random as _random
                    wait_s = min(2.0 * (2 ** attempt), 20.0) + _random.uniform(0, 0.75)
                    print(f"   [OpenRouter] {resp.status_code} ({err_msg[:80]}) — "
                          f"retrying in {wait_s:.1f}s... (attempt {attempt + 1}/{_retries + 1})")
                    time.sleep(wait_s)
                    last_err = err_msg
                    continue

                raise RuntimeError(f"OpenRouter API error ({resp.status_code}): {err_msg}")

            # 200 OK but the API-level payload can still carry an error object
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(f"OpenRouter error: {data['error'].get('message', data['error'])}")

            break   # success

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = str(e)
            if attempt < _retries:
                wait_s = 1.5 * (attempt + 1)
                print(f"   [OpenRouter] Connection issue — retrying in {wait_s:.1f}s...")
                time.sleep(wait_s)
                continue
            raise RuntimeError(f"OpenRouter connection failed after {_retries + 1} attempts: {last_err}")
    else:
        raise RuntimeError(f"OpenRouter failed after {_retries + 1} attempts: {last_err}")

    choice  = (data.get("choices") or [{}])[0]
    msg     = choice.get("message", {}) or {}
    content = msg.get("content") or ""
    raw_tool_calls = msg.get("tool_calls") or []

    # Normalise tool_calls to the same shape Ollama's client produces:
    # [{"function": {"name": str, "arguments": dict}}]
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


def _openrouter_chat_with_fallback(messages: list, model: str = None, tools_schema: list = None,
                                    timeout: int = 60) -> dict:
    """
    Wraps _openrouter_chat() with automatic fallback across
    OPENROUTER_FALLBACK_MODELS. A single free-tier model can 429 for
    minutes at a time because the rate limit is shared across ALL
    OpenRouter users of that model, not just you — retrying the SAME
    model harder doesn't help. This tries the requested model first
    (with its own internal retries), and if it still fails with a
    retryable error, moves on to the next model in the fallback list
    before giving up.
    """
    requested = model or config.OPENROUTER_MODEL
    chain = [requested] + [m for m in OPENROUTER_FALLBACK_MODELS if m != requested]

    last_err = None
    for i, m in enumerate(chain):
        try:
            resp = _openrouter_chat(messages, model=m, tools_schema=tools_schema, timeout=timeout)
            if i > 0:
                print(f"   [OpenRouter] Recovered using fallback model: {m}")
            return resp
        except RuntimeError as e:
            last_err = e
            msg = str(e)
            retryable = any(code in msg for code in ("429", "502", "503", "504")) or "connection failed" in msg.lower()
            if retryable and i < len(chain) - 1:
                print(f"   [OpenRouter] {m} unavailable ({msg[:100]}) — trying next fallback model...")
                continue
            raise
    raise last_err or RuntimeError("OpenRouter: all fallback models failed.")


def consult_openrouter(prompt: str, context: str = "", model: str = None) -> str:
    """
    Send a plain reasoning/planning prompt to OpenRouter (no tool schema —
    this is for text generation, same role as consult_gemini). Used as the
    secondary planning brain, consulted far more often than Gemini since
    it's a direct cheap/free API call with no desktop-app UI cost.
    """
    if not _OPENROUTER_AVAILABLE:
        return f"OpenRouter is not available: {_openrouter_load_msg}"

    full_prompt = prompt.strip()
    if context.strip():
        if len(context) > 60000:
            context = context[:30000] + "\n... [TRUNCATED] ...\n" + context[-30000:]
        full_prompt = "[CONTEXT]\n" + context.strip() + "\n\n[TASK]\n" + full_prompt

    try:
        use_model = model or config.OPENROUTER_MODEL
        print(f"   [OpenRouter] Model: {use_model}")
        resp   = _openrouter_chat_with_fallback(
            [{"role": "user", "content": full_prompt}],
            model=use_model
        )
        result = (resp["message"]["content"] or "").strip()
        print(f"   [OpenRouter] Response received ({len(result)} chars)")
        return f"[OpenRouter/{use_model}]\n" + result
    except Exception as e:
        return f"OpenRouter error: {str(e)}"


def delegate_to_openrouter(task: str, context: str = "", model: str = None,
                            max_steps: int = 10) -> str:
    from system_prompt import get_system_prompt
    from orchestration import process_chat_turn
    """
    Turn OpenRouter into an actual coworker instead of just a text consultant.
    Hands `task` off to a fresh, FULLY TOOL-CAPABLE agent loop running on
    OpenRouter — it can call UIA, CDP, filesystem, terminal, or any other
    tool Midum has, exactly like the primary loop, then reports back a
    final summary that gets relayed to the user.

    Architecturally this spins up process_chat_turn on a brand-new isolated
    conversation seeded with the task, with force_provider="openrouter" so
    it runs on OpenRouter regardless of the global MODEL_PROVIDER. The outer
    Midum loop and this delegated sub-loop do NOT share conversation
    history — only the final summary comes back to the caller.

    The shared response-memory scratchpad file is saved and restored around
    the delegated call, since process_chat_turn wipes it at the start of
    every invocation (including this nested one) and we don't want a
    delegated sub-task to clobber the outer task's in-progress notes.
    """
    if not _OPENROUTER_AVAILABLE:
        return f"OpenRouter is not available: {_openrouter_load_msg}"

    use_model = model or config.OPENROUTER_MODEL
    print(f"   [Delegate → OpenRouter/{use_model}] Task: {task[:80]}")

    _saved_scratchpad = None
    try:
        if os.path.exists(RESPONSE_MEMORY):
            with open(RESPONSE_MEMORY, "r", encoding="utf-8") as f:
                _saved_scratchpad = f.read()
    except Exception:
        pass

    try:
        sub_system_prompt = get_system_prompt(
            effective_provider="openrouter", effective_model=use_model
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
            force_provider="openrouter",
            force_model=use_model,
            max_steps=max_steps,
        )

        step_note = f" ({len(sub_tool_outputs)} tool call(s) executed)" if sub_tool_outputs else ""
        return f"[OpenRouter coworker/{use_model} — task complete{step_note}]\n{summary}"

    except Exception as e:
        return f"Delegation to OpenRouter failed: {e}"

    finally:
        # Restore the outer task's scratchpad exactly as it was before we
        # ran a nested process_chat_turn call that would have wiped it.
        try:
            if _saved_scratchpad is not None:
                with open(RESPONSE_MEMORY, "w", encoding="utf-8") as f:
                    f.write(_saved_scratchpad)
        except Exception:
            pass


def set_openrouter_model(model_id: str) -> str:
    """
    Switch config.OPENROUTER_MODEL at runtime — no restart needed. Applies
    immediately to consult_openrouter, delegate_to_openrouter, and (if
    MODEL_PROVIDER == "openrouter") the primary execution loop, since all
    of those read the global fresh on every call rather than caching it.
    """

    old = config.OPENROUTER_MODEL
    new = model_id.strip()
    if not new:
        return "Error: empty model ID."
    config.OPENROUTER_MODEL = new
    print(f"🔀 [OpenRouter model switched: '{old}' → '{new}']")
    return (
        f"OpenRouter model switched: '{old}' → '{new}'. "
        f"This is in-memory only for the current session — edit config.OPENROUTER_MODEL "
        f"in main.py to make it the default on next launch."
    )


def list_openrouter_models(free_only: bool = True) -> str:
    from tools_registry import _store_index
    """
    Fetch the live list of models available on OpenRouter and return a
    numbered indexed table. Follow up with set_openrouter_model_by_index(N)
    to switch — consistent with the choose-over-search pattern used
    elsewhere (list_paths_indexed, list_skills_indexed, etc).
    """
    if requests is None:
        return "requests library not installed: pip install requests"
    try:
        headers = {}
        if _OPENROUTER_API_KEY:
            headers["Authorization"] = f"Bearer {_OPENROUTER_API_KEY}"
        resp = requests.get(f"{OPENROUTER_API_BASE}/models", headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as e:
        return f"Error fetching OpenRouter model list: {e}"

    items = []
    for m in data:
        model_id = m.get("id", "")
        pricing  = m.get("pricing", {}) or {}
        is_free  = model_id.endswith(":free") or (
            str(pricing.get("prompt", "1")) == "0" and str(pricing.get("completion", "1")) == "0"
        )
        if free_only and not is_free:
            continue
        items.append({
            "id":      model_id,
            "name":    m.get("name", model_id),
            "context": m.get("context_length", "?"),
            "free":    is_free,
        })

    if not items:
        return "No models found matching the filter."

    _store_index("openrouter_models", items)

    lines = [
        f"OpenRouter models ({len(items)}{' free' if free_only else ''}) — "
        f"currently active: {config.OPENROUTER_MODEL}",
        "Use set_openrouter_model_by_index(index) to switch.",
        "",
        f"{'IDX':>4}  {'CTX':>8}  {'FREE':<5}  MODEL ID",
        "─" * 72,
    ]
    for i, m in enumerate(items):
        lines.append(f"{i:>4}  {str(m['context']):>8}  {'yes' if m['free'] else 'no':<5}  {m['id']}")
    return "\n".join(lines)


def set_openrouter_model_by_index(index: int) -> str:
    from tools_registry import _get_indexed
    """Switch config.OPENROUTER_MODEL to the entry at `index` from list_openrouter_models."""
    entry = _get_indexed("openrouter_models", index)
    if entry is None:
        return f"Index {index} not found. Call list_openrouter_models() first."
    return set_openrouter_model(entry["id"])


# =============================================================================

