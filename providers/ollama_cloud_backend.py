# --- AUTO-SPLITTER: imports added by automated pass, please review ---
import config
from config import OLLAMA_CLOUD_FALLBACK_MODELS, OLLAMA_CLOUD_HOST, RESPONSE_MEMORY, SECRETS_FILE
from system_prompt import get_system_prompt
import json
import os
import time

import ollama

# --- from main.py, section 1 ---
# OLLAMA CLOUD SETUP
# =============================================================================
# Ollama Cloud (https://ollama.com) runs the same open-weight models Ollama
# supports locally (gpt-oss, qwen3, deepseek-v3.1, kimi-k2, ...) on Ollama's
# own hosted GPUs, exposed through the SAME `ollama` Python client used for
# the local provider — just pointed at https://ollama.com instead of
# localhost, with a bearer token attached. That means tool-calling responses
# come back in the exact native `message.tool_calls` shape as local Ollama,
# so no JSON-translation/sanitisation layer is needed like OpenRouter/Groq/
# Gemini API require.
#
# API KEY SETUP (one-time):
#   1. Create a free key at https://ollama.com/settings/keys
#   2. Add it to the SAME secrets file used everywhere else:
#      { "GEMINI_API_KEY": "...", "OPENROUTER_API_KEY": "...",
#        "GROQ_API_KEY": "...", "OLLAMA_API_KEY": "..." }
#
_OLLAMA_CLOUD_AVAILABLE = False
_OLLAMA_CLOUD_API_KEY   = None
_ollama_cloud_client     = None


def _load_ollama_cloud():
    """Load OLLAMA_API_KEY from the shared secrets file and build a Client."""
    global _OLLAMA_CLOUD_AVAILABLE, _OLLAMA_CLOUD_API_KEY, _ollama_cloud_client
    try:
        secrets_path = os.path.abspath(SECRETS_FILE)
        if not os.path.exists(secrets_path):
            return False, f"Secrets file not found: {secrets_path}"
        with open(secrets_path, "r", encoding="utf-8") as f:
            secrets = json.load(f)
        key = secrets.get("OLLAMA_API_KEY", "").strip()
        if not key:
            return False, "OLLAMA_API_KEY is empty in secrets file."
        _OLLAMA_CLOUD_API_KEY = key
        _ollama_cloud_client = ollama.Client(
            host=OLLAMA_CLOUD_HOST,
            headers={"Authorization": f"Bearer {key}"},
        )
        _OLLAMA_CLOUD_AVAILABLE = True
        return True, "OK"
    except Exception as e:
        return False, str(e)


_ollama_cloud_load_ok, _ollama_cloud_load_msg = _load_ollama_cloud()


def _ollama_cloud_chat(messages: list, model: str = None, tools_schema: list = None,
                        timeout: int = 120, _retries: int = 2) -> dict:
    """
    Call Ollama Cloud's chat endpoint via the native `ollama` client (same
    request/response shape as local Ollama — client.chat() already returns
    message.tool_calls natively, so no normalisation is needed here, unlike
    the OpenAI-compatible providers).

    Retries on transient connection errors, mirroring the other backends.
    """
    if not _OLLAMA_CLOUD_AVAILABLE or not _ollama_cloud_client:
        raise RuntimeError(
            f"Ollama Cloud not available: {_ollama_cloud_load_msg}. "
            f"Add OLLAMA_API_KEY to {SECRETS_FILE}"
        )

    model = model or config.OLLAMA_CLOUD_MODEL

    last_err = None
    for attempt in range(_retries + 1):
        try:
            kwargs = {"model": model, "messages": messages}
            if tools_schema:
                kwargs["tools"] = tools_schema
            resp = _ollama_cloud_client.chat(**kwargs)
            return {
                "message": {
                    "role": "assistant",
                    "content": resp["message"].get("content", "") or "",
                    "tool_calls": resp["message"].get("tool_calls") or [],
                }
            }
        except Exception as e:
            last_err = e
            retryable = any(s in str(e).lower() for s in
                             ("timeout", "connection", "502", "503", "504", "rate limit", "429"))
            if retryable and attempt < _retries:
                wait_s = 1.5 * (attempt + 1)
                print(f"   [Ollama Cloud] {e} — retrying in {wait_s:.1f}s...")
                time.sleep(wait_s)
                continue
            raise RuntimeError(f"Ollama Cloud error: {e}")

    raise RuntimeError(f"Ollama Cloud failed after {_retries + 1} attempts: {last_err}")


def _ollama_cloud_chat_with_fallback(messages: list, model: str = None, tools_schema: list = None,
                                      timeout: int = 120) -> dict:
    """
    Wraps _ollama_cloud_chat() with automatic fallback across
    OLLAMA_CLOUD_FALLBACK_MODELS, mirroring _groq_chat_with_fallback().
    """
    requested = model or config.OLLAMA_CLOUD_MODEL
    chain = [requested] + [m for m in OLLAMA_CLOUD_FALLBACK_MODELS if m != requested]

    last_exc = None
    for m in chain:
        try:
            resp = _ollama_cloud_chat(messages, model=m, tools_schema=tools_schema, timeout=timeout)
            if m != requested:
                print(f"   [Ollama Cloud] Fell back to model: {m}")
            return resp
        except Exception as e:
            last_exc = e
            print(f"   [Ollama Cloud] Model '{m}' failed ({e}) — trying next fallback...")
            continue
    raise RuntimeError(f"All Ollama Cloud models failed. Last error: {last_exc}")


def consult_ollama_cloud(prompt: str, context: str = "", model: str = None) -> str:
    """Plain reasoning/planning call to Ollama Cloud — no tool schema."""
    if not _OLLAMA_CLOUD_AVAILABLE:
        return f"Ollama Cloud is not available: {_ollama_cloud_load_msg}"

    full_prompt = prompt.strip()
    if context.strip():
        if len(context) > 60000:
            context = context[:30000] + "\n... [TRUNCATED] ...\n" + context[-30000:]
        full_prompt = "[CONTEXT]\n" + context.strip() + "\n\n[TASK]\n" + full_prompt

    try:
        use_model = model or config.OLLAMA_CLOUD_MODEL
        print(f"   [Ollama Cloud] Model: {use_model}")
        resp = _ollama_cloud_chat_with_fallback([{"role": "user", "content": full_prompt}], model=use_model)
        result = (resp["message"]["content"] or "").strip()
        print(f"   [Ollama Cloud] Response received ({len(result)} chars)")
        return f"[Ollama Cloud/{use_model}]\n" + result
    except Exception as e:
        return f"Ollama Cloud error: {str(e)}"


def delegate_to_ollama_cloud(task: str, context: str = "", model: str = None,
                              max_steps: int = 10) -> str:
    from orchestration import process_chat_turn
    """Mirror of delegate_to_groq(): hand `task` off to a fresh, fully
    tool-capable agent loop running on Ollama Cloud."""
    if not _OLLAMA_CLOUD_AVAILABLE:
        return f"Ollama Cloud is not available: {_ollama_cloud_load_msg}"

    use_model = model or config.OLLAMA_CLOUD_MODEL
    print(f"   [Delegate → Ollama Cloud/{use_model}] Task: {task[:80]}")

    _saved_scratchpad = None
    try:
        if os.path.exists(RESPONSE_MEMORY):
            with open(RESPONSE_MEMORY, "r", encoding="utf-8") as f:
                _saved_scratchpad = f.read()
    except Exception:
        pass

    try:
        sub_system_prompt = get_system_prompt(
            effective_provider="ollama_cloud", effective_model=use_model
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
            force_provider="ollama_cloud",
            force_model=use_model,
            max_steps=max_steps,
        )

        step_note = f" ({len(sub_tool_outputs)} tool call(s) executed)" if sub_tool_outputs else ""
        return f"[Ollama Cloud coworker/{use_model} — task complete{step_note}]\n{summary}"

    except Exception as e:
        return f"Delegation to Ollama Cloud failed: {e}"

    finally:
        try:
            if _saved_scratchpad is not None:
                with open(RESPONSE_MEMORY, "w", encoding="utf-8") as f:
                    f.write(_saved_scratchpad)
        except Exception:
            pass


def set_ollama_cloud_model(model_id: str) -> str:
    """Switch config.OLLAMA_CLOUD_MODEL at runtime — no restart needed."""
    old = config.OLLAMA_CLOUD_MODEL
    new = model_id.strip()
    if not new:
        return "Error: empty model ID."
    config.OLLAMA_CLOUD_MODEL = new
    print(f"🔀 [Ollama Cloud model switched: '{old}' → '{new}']")
    return (
        f"Ollama Cloud model switched: '{old}' → '{new}'. "
        f"This is in-memory only for the current session."
    )


def list_ollama_cloud_models() -> str:
    """
    Fetch the live list of cloud models available on ollama.com via the
    native client's list() call, formatted as a numbered table.
    """
    if not _OLLAMA_CLOUD_AVAILABLE:
        return f"Ollama Cloud is not available: {_ollama_cloud_load_msg}"

    try:
        resp = _ollama_cloud_client.list()
        models = resp.get("models", []) if isinstance(resp, dict) else getattr(resp, "models", [])
        names = []
        for m in models:
            name = m.get("model") or m.get("name") if isinstance(m, dict) else getattr(m, "model", None)
            if name:
                names.append(name)
    except Exception as e:
        return f"Failed to fetch Ollama Cloud model list: {e}"

    if not names:
        return "No models returned by Ollama Cloud."

    names.sort()
    lines = [f"currently active: {config.OLLAMA_CLOUD_MODEL}", ""]
    for i, n in enumerate(names):
        lines.append(f"[{i}] {n}")
    return "\n".join(lines)


# =============================================================================
