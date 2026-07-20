"""
Context window summarizer -- Midum
===================================
When conversation_history is about to exceed 80% of the active model's
context window, this module makes a SEPARATE, silent call to the SAME
model/provider currently driving Midum to compress the OLDEST 60% of the
non-system turns into a dense, detail-preserving summary. The newest 40%
of turns are left completely unchanged. The user never sees the
summarization call -- it's not narrated, not passed through say()/_print_reply,
and doesn't consume a turn in the visible chat.

Usage: call summarize_context_if_needed(conversation_history) once per
step, right before the history is trimmed/sent to the model. It mutates
conversation_history in place and also returns it.
"""

import queue as _queue
import threading
import json
import os

import config

# -- Context window sizes (tokens), per model --------------------------------
# Conservative published/estimated context lengths. Anything not listed
# falls back to _DEFAULT_CONTEXT_TOKENS. Update as models change.
_DEFAULT_CONTEXT_TOKENS = 32_000

_MODEL_CONTEXT_WINDOWS = {
    # Local Ollama
    "jarvishehe":        32_000,
    "qwen2.5-coder":      32_000,
    # OpenRouter
    "meta-llama/llama-3.3-70b-instruct:free": 131_000,
    "meta-llama/llama-3.1-8b-instruct:free":  131_000,
    "google/gemini-2.0-flash-exp:free":       1_000_000,
    "deepseek/deepseek-chat-v3.1:free":       64_000,
    "qwen/qwen3-235b-a22b:free":              131_000,
    # Gemini API (official)
    "gemini-3.1-flash-lite":                  1_000_000,
    # GroqCloud
    "llama-3.3-70b-versatile":                128_000,
    "llama-3.1-8b-instant":                   128_000,
    "qwen/qwen3-32b":                         131_000,
    "moonshotai/kimi-k2-instruct":            128_000,
    "deepseek-r1-distill-llama-70b":          128_000,
    # Ollama Cloud
    "gpt-oss:120b-cloud":                     128_000,
    "gpt-oss:20b-cloud":                      128_000,
    "qwen3-coder:480b-cloud":                 256_000,
    "deepseek-v3.1:671b-cloud":               128_000,
    "kimi-k2:1t-cloud":                       128_000,
    # Gemini web
    "gemini-3-flash":                         1_000_000,
}

SUMMARIZE_TRIGGER_RATIO = 0.80   # summarize once usage hits 80% of the window
SUMMARIZE_OLDEST_RATIO  = 0.60   # compress the oldest 60% of eligible turns
MIN_TURNS_TO_SUMMARIZE  = 6      # don't bother on short histories

# -- User-configurable override (Model tab) -----------------------------------
# By default the context window is looked up from _MODEL_CONTEXT_WINDOWS /
# _DEFAULT_CONTEXT_TOKENS above, per active model. The Model tab lets the
# user instead pin an exact token count (e.g. for a fine-tune or a model
# not in the table); once saved, that value always wins over the table,
# for any model, until cleared. Persisted so it survives app restarts.
def _load_context_token_override():
    try:
        if os.path.exists(config.CONTEXT_TOKENS_FILE):
            with open(config.CONTEXT_TOKENS_FILE, "r", encoding="utf-8") as f:
                val = json.load(f).get("max_tokens")
            if isinstance(val, int) and val > 0:
                return val
    except Exception as e:
        print(f"[context_summarizer] Failed to load token override: {e}")
    return None


_USER_CONTEXT_TOKENS = _load_context_token_override()


def get_user_context_tokens() -> int | None:
    """The saved override, or None if the user hasn't set one (in which
    case get_context_window() falls back to the per-model table)."""
    return _USER_CONTEXT_TOKENS


def set_user_context_tokens(n: int) -> int:
    """Save a new context-token override, used immediately by every
    subsequent get_context_window() / should_summarize() call."""
    global _USER_CONTEXT_TOKENS
    n = max(1000, int(n))
    _USER_CONTEXT_TOKENS = n
    try:
        os.makedirs(os.path.dirname(config.CONTEXT_TOKENS_FILE), exist_ok=True)
        with open(config.CONTEXT_TOKENS_FILE, "w", encoding="utf-8") as f:
            json.dump({"max_tokens": n}, f)
    except Exception as e:
        print(f"[context_summarizer] Failed to persist token override: {e}")
    return _USER_CONTEXT_TOKENS

_SUMMARY_PROMPT = """You are compressing part of a conversation between a user and Midum, a desktop AI agent, so it fits in a smaller context window.

Summarize the conversation segment below into a dense, factual summary. Preserve ALL: user goals/requests, decisions made, file paths, commands run, tool calls and their results, values/numbers, names, URLs, and any unresolved or pending tasks. Do not omit any detail -- condense phrasing, not information. Write it as a compact narrated log, not commentary, and do not address the user directly.

CONVERSATION SEGMENT TO SUMMARIZE:
{segment}
"""


def _active_model_name() -> str:
    provider = config.MODEL_PROVIDER
    return {
        "openrouter":   config.OPENROUTER_MODEL,
        "gemini_web":   config.GEMINI_WEB_MODEL or "gemini-3-flash",
        "gemini_api":   config.GEMINI_API_MODEL,
        "groq":         config.GROQ_MODEL,
        "ollama_cloud": config.OLLAMA_CLOUD_MODEL,
    }.get(provider, config.MODEL_NAME)


def get_context_window(model_name: str = None) -> int:
    if _USER_CONTEXT_TOKENS is not None:
        return _USER_CONTEXT_TOKENS
    model_name = model_name or _active_model_name()
    return _MODEL_CONTEXT_WINDOWS.get(model_name, _DEFAULT_CONTEXT_TOKENS)


def estimate_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate (~4 chars/token for English)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def estimate_history_tokens(conversation_history: list) -> int:
    total = 0
    for m in conversation_history:
        content = m.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    total += estimate_tokens(part["text"])
        tool_calls = m.get("tool_calls")
        if tool_calls:
            total += estimate_tokens(str(tool_calls))
    return total


def should_summarize(conversation_history: list) -> bool:
    window = get_context_window()
    used   = estimate_history_tokens(conversation_history)
    return used >= window * SUMMARIZE_TRIGGER_RATIO


def _turn_to_text(m: dict) -> str:
    role    = m.get("role", "?")
    content = m.get("content", "")
    if not isinstance(content, str):
        content = str(content)
    line = f"{role.upper()}: {content}"
    tool_calls = m.get("tool_calls")
    if tool_calls:
        line += f"\n  [tool_calls: {tool_calls}]"
    return line


def _call_model_for_summary(segment_text: str) -> str | None:
    """
    Blocking call to the SAME primary model/provider driving Midum right
    now, used purely to summarize `segment_text`. Runs on its own thread
    with its own isolated message list -- never touches conversation_history,
    never goes through say()/_print_reply, so nothing reaches the visible
    chat. Returns the summary text, or None on failure/timeout.
    """
    from orchestration import _call_primary_model   # local import: avoids a circular import at module load

    result_q = _queue.Queue()
    messages = [{"role": "user", "content": _SUMMARY_PROMPT.format(segment=segment_text)}]
    t = threading.Thread(target=_call_primary_model, args=(messages, result_q), daemon=True)
    t.start()
    t.join(timeout=180)
    if t.is_alive():
        print("[context_summarizer] Summary call timed out -- skipping compaction this turn.")
        return None
    try:
        status, payload = result_q.get_nowait()
    except Exception:
        return None
    if status != "ok":
        print(f"[context_summarizer] Summary call failed: {payload}")
        return None
    content = (payload["message"].get("content") or "").strip()
    return content or None


def summarize_context_if_needed(conversation_history: list) -> list:
    """
    Call once per step, right before conversation_history is trimmed/sent
    to the model. If usage is below SUMMARIZE_TRIGGER_RATIO (80%) of the
    active model's context window, returns conversation_history UNCHANGED
    (cheap check, safe to call every step). Otherwise:

      1. Splits the non-system turns into oldest 60% / newest 40%.
      2. Sends the oldest 60% to a separate, silent call to the SAME
         model/provider, asking it to summarize with full detail retention.
      3. Replaces that oldest 60% in place with a single system message
         carrying the summary. The newest 40% is left byte-for-byte as-is.
      4. If the summary call fails for any reason, history is left
         untouched rather than risk silently losing turns.

    Mutates conversation_history in place AND returns it, so either call
    style (`x = summarize_context_if_needed(x)` or just calling it) works.
    """
    if not should_summarize(conversation_history):
        return conversation_history

    sys_msgs = [m for m in conversation_history if m.get("role") == "system"]
    non_sys  = [m for m in conversation_history if m.get("role") != "system"]

    if len(non_sys) < MIN_TURNS_TO_SUMMARIZE:
        return conversation_history

    split_idx = int(len(non_sys) * SUMMARIZE_OLDEST_RATIO)
    split_idx = max(1, min(split_idx, len(non_sys) - 1))   # always keep >=1 recent turn

    oldest_chunk = non_sys[:split_idx]
    recent_chunk = non_sys[split_idx:]

    segment_text = "\n".join(_turn_to_text(m) for m in oldest_chunk)
    summary = _call_model_for_summary(segment_text)

    if not summary:
        return conversation_history

    summary_msg = {
        "role": "system",
        "content": (
            f"[CONVERSATION SUMMARY -- earlier turns compacted to save context. "
            f"All facts, decisions, paths, commands, and results below are preserved "
            f"from the original {len(oldest_chunk)} messages]:\n{summary}"
        ),
    }

    used_before = estimate_history_tokens(sys_msgs + non_sys)
    new_history = sys_msgs + [summary_msg] + recent_chunk
    used_after  = estimate_history_tokens(new_history)

    conversation_history.clear()
    conversation_history.extend(new_history)

    print(f"[context_summarizer] Compacted {len(oldest_chunk)} turns -> 1 summary "
          f"(~{used_before} -> ~{used_after} tokens)")

    return conversation_history
