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

# Fallback key chain -- GEMINI_API_KEY, GEMINI_API_KEY_2, _3, _4, _5
# (whichever are actually filled in), tried in order. _GEMINI_API_KEY_INDEX
# tracks which one is currently active so repeated calls keep using the
# last-good key instead of restarting from the front every time.
_GEMINI_API_KEYS       = []
_GEMINI_API_KEY_INDEX  = 0

def _load_gemini_api():
    """Load GEMINI_API_KEY (+ up to 4 numbered fallbacks, 5 keys total) from
    the shared secrets file (official API path)."""
    global _GEMINI_API_AVAILABLE, _GEMINI_API_KEY, _GEMINI_API_KEYS, _GEMINI_API_KEY_INDEX
    try:
        secrets_path = os.path.abspath(SECRETS_FILE)
        if not os.path.exists(secrets_path):
            return False, f"Secrets file not found: {secrets_path}"
        with open(secrets_path, "r", encoding="utf-8") as f:
            secrets = json.load(f)
        keys = []
        for field in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY_4", "GEMINI_API_KEY_5"):
            k = secrets.get(field, "").strip()
            if k:
                keys.append(k)
        if not keys:
            return False, "GEMINI_API_KEY is empty in secrets file."
        _GEMINI_API_KEYS      = keys
        _GEMINI_API_KEY_INDEX = 0
        _GEMINI_API_KEY       = keys[0]
        _GEMINI_API_AVAILABLE = True
        return True, "OK"
    except Exception as e:
        return False, str(e)


def _advance_gemini_api_key() -> bool:
    """Switch to the next fallback key in the chain when the current one
    is exhausted (429 / RESOURCE_EXHAUSTED). Returns True if there was a
    next key to switch to, False if we've run out of keys."""
    global _GEMINI_API_KEY, _GEMINI_API_KEY_INDEX
    _pinned_ctx = _turn_ctx_with_pinned_key()
    if _pinned_ctx is not None:
        return _advance_pinned_key(_pinned_ctx)
    if _GEMINI_API_KEY_INDEX + 1 >= len(_GEMINI_API_KEYS):
        return False
    _GEMINI_API_KEY_INDEX += 1
    _GEMINI_API_KEY = _GEMINI_API_KEYS[_GEMINI_API_KEY_INDEX]
    print(f"   [Gemini API] Key exhausted — switching to fallback key #{_GEMINI_API_KEY_INDEX + 1}/{len(_GEMINI_API_KEYS)}")
    return True


# ── Per-turn key pinning (concurrent conversation tabs) ──────────────────────
# With several tabs running at once, sharing ONE active key makes them fight over a single quota. When
# more than one Gemini key is configured, each running turn (state.TurnContext) is pinned to the
# least-used key of the chain, so two tabs naturally land on two different keys. A 429 then moves only
# THAT turn to another key (preferring the least-used one it hasn't tried yet) and never touches the
# shared chain pointer other turns/threads use. Turns with no pinned key behave exactly as before.
_KEY_USERS = {}                       # key index -> number of running turns pinned to it
_KEY_USERS_LOCK = __import__("threading").Lock()


def _turn_ctx_with_pinned_key():
    try:
        from state import current_ctx
        c = current_ctx()
        if c is not None and c.gemini_key_index is not None and c.gemini_key_index < len(_GEMINI_API_KEYS):
            return c
    except Exception:
        pass
    return None


def acquire_turn_key(ctx):
    """Pin the least-used key to `ctx`. Returns the index, or None when pinning isn't useful
    (fewer than two keys configured)."""
    with _KEY_USERS_LOCK:
        n = len(_GEMINI_API_KEYS)
        if n < 2:
            ctx.gemini_key_index = None
            return None
        idx = min(range(n), key=lambda i: (_KEY_USERS.get(i, 0), 0 if i == _GEMINI_API_KEY_INDEX else 1, i))
        _KEY_USERS[idx] = _KEY_USERS.get(idx, 0) + 1
        ctx.gemini_key_index = idx
        ctx.gemini_keys_tried = set()
        return idx


def release_turn_key(ctx):
    with _KEY_USERS_LOCK:
        idx = ctx.gemini_key_index
        if idx is not None:
            _KEY_USERS[idx] = max(0, _KEY_USERS.get(idx, 1) - 1)
        ctx.gemini_key_index = None
        ctx.gemini_keys_tried = set()


def _advance_pinned_key(ctx) -> bool:
    with _KEY_USERS_LOCK:
        cur = ctx.gemini_key_index
        ctx.gemini_keys_tried.add(cur)
        candidates = [i for i in range(len(_GEMINI_API_KEYS)) if i not in ctx.gemini_keys_tried]
        if not candidates:
            return False
        nxt = min(candidates, key=lambda i: (_KEY_USERS.get(i, 0), i))
        _KEY_USERS[cur] = max(0, _KEY_USERS.get(cur, 1) - 1)
        _KEY_USERS[nxt] = _KEY_USERS.get(nxt, 0) + 1
        ctx.gemini_key_index = nxt
    print(f"   [Gemini API] Key #{cur + 1} exhausted for this tab — switching it to key #{nxt + 1}/{len(_GEMINI_API_KEYS)}")
    return True


def _active_key():
    """(index, key) for the calling turn: its pinned key if it has one, else the shared chain's."""
    c = _turn_ctx_with_pinned_key()
    if c is not None:
        return c.gemini_key_index, _GEMINI_API_KEYS[c.gemini_key_index]
    return _GEMINI_API_KEY_INDEX, _GEMINI_API_KEY


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
    sanitized    = []
    pending_calls = []  # list of (call_id, function_name), FIFO
    counter      = 0
    # Artifact attachments: inline images for the most recent attachment-bearing
    # user message(s); everything else is reachable through the artifact tools.
    try:
        import artifacts as _art
        _inline_idx = _art.inline_eligible_indices(messages)
    except Exception as _art_err:
        print(f"   [Gemini API] artifact inlining unavailable: {_art_err}")
        _art, _inline_idx = None, set()
    for _idx, m in enumerate(messages):
        m = dict(m)
        role = m.get("role")
        # Ollama-style raw-base64 `images` (screenshots, view_artifact) are not valid on
        # Google's endpoint -- lift them out and re-send as a following user message.
        imgs = m.pop("images", None)

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
                pending_calls.append((call_id, name))
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
            # Google's endpoint maps "tool" messages to function_response and
            # requires "name" to be non-empty ("Name cannot be empty" 400s
            # otherwise) — Midum's internal format never sets it, so fill
            # both tool_call_id and name in from the matching pending call.
            call_id = m.get("tool_call_id")
            name    = m.get("name")
            if call_id and pending_calls:
                # find matching call by id (usually the front of the queue)
                for i, (cid, cname) in enumerate(pending_calls):
                    if cid == call_id:
                        name = name or cname
                        del pending_calls[i]
                        break
                else:
                    if not name and pending_calls:
                        _, name = pending_calls.pop(0)
            elif pending_calls:
                call_id, pending_name = pending_calls.pop(0)
                name = name or pending_name
            if not call_id:
                call_id = f"call_{counter}"
            if not name:
                name = "unknown_function"
            m["tool_call_id"] = call_id
            m["name"] = name
            sanitized.append(m)

        elif role == "user" and _art is not None and _idx in _inline_idx and isinstance(m.get("content"), str):
            try:
                parts = _art.build_user_parts(m["content"])
            except Exception as _part_err:
                print(f"   [Gemini API] could not inline attachments: {_part_err}")
                parts = None
            if parts:
                m["content"] = parts
            sanitized.append(m)

        else:
            sanitized.append(m)

        if imgs:
            img_parts = [{"type": "text", "text": "[Image result from the previous tool call]"}]
            for b64 in imgs:
                if not isinstance(b64, str) or not b64:
                    continue
                if b64.startswith("data:"):
                    url = b64
                else:
                    mime = "image/jpeg" if b64.startswith("/9j/") else "image/webp" if b64.startswith("UklGR") else "image/png"
                    url = f"data:{mime};base64,{b64}"
                img_parts.append({"type": "image_url", "image_url": {"url": url}})
            if len(img_parts) > 1:
                sanitized.append({"role": "user", "content": img_parts})

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
        "Authorization": f"Bearer {_active_key()[1]}",
        "Content-Type":  "application/json",
    }

    last_err = None
    attempt = 0
    while attempt <= _retries:
        try:
            headers["Authorization"] = f"Bearer {_active_key()[1]}"
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

                # 429 = this key's quota is exhausted. Prefer switching to the
                # next fallback key in the chain (instant) over just sleeping
                # and retrying the same dead key — only fall through to the
                # sleep/retry if there's no fresh key left to try.
                if resp.status_code == 429 and _advance_gemini_api_key():
                    last_err = err_msg
                    continue   # retry immediately on the new key, same attempt budget

                retryable = resp.status_code in (429, 502, 503, 504)
                if retryable and attempt < _retries:
                    wait_s = 1.5 * (attempt + 1)
                    print(f"   [Gemini API] {resp.status_code} ({err_msg[:80]}) — "
                          f"retrying in {wait_s:.1f}s...")
                    time.sleep(wait_s)
                    last_err = err_msg
                    attempt += 1
                    continue

                raise RuntimeError(f"Gemini API error ({resp.status_code}): {err_msg}")

            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(f"Gemini API error: {data['error'].get('message', data['error'])}")

            _c_ok = _turn_ctx_with_pinned_key()
            if _c_ok is not None:
                _c_ok.gemini_keys_tried = set()   # a successful call: this turn may rotate again on a later 429
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


# =============================================================================
# GEMINI FILES API (native, NOT the OpenAI-compat layer)
# =============================================================================
# Google's OpenAI-compatible endpoint cannot reference uploaded files, so the
# artifact system uploads attachments to the native Files API (kept ~48h) and
# the `analyze_artifact` tool calls native generateContent with the file URI.
# NOTE: uploaded files are scoped to the API key's project, so we remember which
# key (index in the fallback chain) did the upload and reuse that key for reads.

class GeminiFilesError(RuntimeError):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def _gemini_native_root() -> str:
    base = GEMINI_API_BASE.rstrip("/")
    if base.endswith("/openai"):
        base = base[: -len("/openai")]
    return base                                  # e.g. https://generativelanguage.googleapis.com/v1beta


def _gemini_upload_root() -> str:
    return _gemini_native_root().replace("/v1beta", "/upload/v1beta", 1)


def _gemini_key_at(index=None):
    """(index, key) for an explicit chain index, else the currently active key."""
    if index is not None and 0 <= index < len(_GEMINI_API_KEYS):
        return index, _GEMINI_API_KEYS[index]
    return _active_key()


def _files_error(resp) -> GeminiFilesError:
    msg = None
    try:
        j = resp.json()
        j = j[0] if isinstance(j, list) and j else j
        msg = (j.get("error") or {}).get("message")
    except Exception:
        pass
    return GeminiFilesError(f"Gemini API error ({resp.status_code}): {msg or resp.text[:200]}", resp.status_code)


def gemini_files_upload(path: str, mime_type: str, display_name: str = None, timeout: int = 900) -> dict:
    """Upload a local file to the Gemini Files API (resumable protocol). Returns the file
    resource dict plus ``key_index``. Rotates to the next key on 429 like the chat path."""
    if not _GEMINI_API_AVAILABLE or not _GEMINI_API_KEY:
        raise GeminiFilesError("No Gemini API key configured (GEMINI_API_KEY in the secrets file).")
    size = os.path.getsize(path)
    while True:
        idx, key = _gemini_key_at(None)
        start = requests.post(
            f"{_gemini_upload_root()}/files",
            headers={
                "x-goog-api-key": key,
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": mime_type,
                "Content-Type": "application/json",
            },
            json={"file": {"display_name": (display_name or os.path.basename(path))[:128]}},
            timeout=60,
        )
        if start.status_code == 429 and _advance_gemini_api_key():
            continue
        if start.status_code != 200:
            raise _files_error(start)
        upload_url = start.headers.get("x-goog-upload-url")
        if not upload_url:
            raise GeminiFilesError("Gemini Files API did not return an upload URL.")
        with open(path, "rb") as fh:
            up = requests.post(
                upload_url,
                headers={"Content-Length": str(size), "X-Goog-Upload-Offset": "0",
                         "X-Goog-Upload-Command": "upload, finalize"},
                data=fh, timeout=timeout,
            )
        if up.status_code != 200:
            raise _files_error(up)
        info = up.json().get("file") or {}
        break
    # Video/large files are PROCESSING for a bit before they can be referenced.
    deadline = time.time() + 180
    while info.get("state") == "PROCESSING" and time.time() < deadline:
        time.sleep(2)
        r = requests.get(f"{_gemini_native_root()}/{info['name']}", headers={"x-goog-api-key": key}, timeout=30)
        if r.status_code == 200:
            info = r.json()
    if info.get("state") in ("FAILED", "PROCESSING"):
        raise GeminiFilesError(f"Gemini could not process the file (state={info.get('state')}).")
    info["key_index"] = idx
    return info


def gemini_files_delete(name: str, key_index=None) -> bool:
    if not name or not _GEMINI_API_AVAILABLE:
        return False
    _, key = _gemini_key_at(key_index)
    try:
        r = requests.delete(f"{_gemini_native_root()}/{name}", headers={"x-goog-api-key": key}, timeout=30)
        return r.status_code in (200, 204, 404)
    except Exception:
        return False


def gemini_generate_with_files(prompt: str, files: list = None, inline: list = None, model: str = None,
                               system: str = None, key_index=None, timeout: int = 300) -> str:
    """Native generateContent call. ``files`` = [{"uri", "mime"}] (Files API),
    ``inline`` = [{"mime", "data"(base64)}]. Returns the model's text."""
    if not _GEMINI_API_AVAILABLE or not _GEMINI_API_KEY:
        raise GeminiFilesError("No Gemini API key configured.")
    model_id = (model or config.GEMINI_API_MODEL or "").split("/")[-1]
    parts = []
    for f in files or []:
        parts.append({"file_data": {"mime_type": f["mime"], "file_uri": f["uri"]}})
    for f in inline or []:
        parts.append({"inline_data": {"mime_type": f["mime"], "data": f["data"]}})
    parts.append({"text": prompt})
    body = {"contents": [{"role": "user", "parts": parts}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    last = None
    for attempt in range(3):
        _, key = _gemini_key_at(key_index)
        resp = requests.post(f"{_gemini_native_root()}/models/{model_id}:generateContent",
                             headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                             json=body, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            cands = data.get("candidates") or []
            if not cands:
                raise GeminiFilesError(f"Gemini returned no answer (blocked?): {data.get('promptFeedback')}")
            out = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", [])
                          if not p.get("thought"))
            if not out.strip():
                raise GeminiFilesError(f"Gemini returned an empty answer (finishReason={cands[0].get('finishReason')}).")
            return out.strip()
        last = _files_error(resp)
        if resp.status_code == 429 and key_index is None and _advance_gemini_api_key():
            continue
        if resp.status_code in (429, 500, 503) and attempt < 2:
            time.sleep(2 * (attempt + 1))
            continue
        break
    raise last or GeminiFilesError("Gemini request failed.")


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

