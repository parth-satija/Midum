# --- AUTO-SPLITTER: imports added by automated pass, please review ---
import config
import providers.openrouter_backend as providers_openrouter_backend
from config import COMMANDS_FILE, MODEL_CANVAS_H, MODEL_CANVAS_W, OPENROUTER_CONSULT_MODE, SCALE_X, SCALE_Y, STARTUP_DIR, _is_legacy_toolcall_model
from knowledge_base import add_instruction, add_path, create_domain_knowledge, create_domain_skill, list_domain_knowledge, list_domain_skills, read_domain_knowledge, read_instructions, read_paths
from config import _IS_LINUX
from midum_mcp.manager import _mcp_resolve_name, connect_mcp_server, disconnect_mcp_server, list_mcp_servers
from midum_mcp.tools import _mcp_autoroute_tool_call, _mcp_find_tool_matches, call_mcp_tool, list_native_tools, show_native_tool_schema, show_server_tools
from memory import set_current_goal, update_memory
from providers.gemini_api_backend import _gemini_api_chat, consult_gemini_api, delegate_to_gemini_api, set_gemini_api_model
from providers.gemini_reasoning import consult_gemini
from providers.gemini_web_backend import _GEMINI_WEBAPI_AVAILABLE, _call_gemini_web_primary, _gemini_webapi_load_msg, delegate_to_gemini_web, set_gemini_web_model
from providers.groq_backend import _groq_chat_with_fallback, consult_groq, delegate_to_groq, list_groq_models, set_groq_model, set_groq_model_by_index
from providers.ollama_cloud_backend import _ollama_cloud_chat_with_fallback, consult_ollama_cloud, delegate_to_ollama_cloud, list_ollama_cloud_models, set_ollama_cloud_model
from screen_capture import capture_screen_to_ram, fallback_click_grid, fallback_click_text, fallback_find_text, type_text
from skills import list_skills, load_skill
from state import _abort_event
from tools.user_prompt_tools import ask_user_approval, ask_user_choice, ask_user_file_path, ask_user_text
from tools_registry import _get_groq_tools_schema, _uia_unavailable_message, append_local_file, append_response_memory, clear_response_memory, click_ocr_index, click_ui_element, create_flowchart, execute_terminal_command, explore_path, find_file, generate_image, get_path, list_active_windows, list_directory, list_domain_knowledge_indexed, list_domain_skills_indexed, list_more_tools, list_paths_indexed, list_skills_indexed, load_skill_by_index, load_tool_by_index, manual_inspect_app_subtree, manual_interact_with_ui, manual_scan_app_layouts, ocr_snapshot, open_path, open_path_by_index, open_search_result, open_url, read_domain_by_index, read_file_chunk, read_file_smart, read_local_file, read_response_memory, search_internet, write_docx_file, write_local_file, write_response_memory
from tools_schema import tools
from ui_automation import ui_navigator
from ui_automation.windows_uia import _UIA_AVAILABLE
from utils.path_resolver import resolve_file_path
import json
import ollama
import os
import queue as _queue
import re
import threading
import time

# --- from main.py, section 1 ---
# 9. PERSISTENT ORCHESTRATION ENGINE
# =============================================================================

# Words that signal a trivial/short turn — skip Gemini pre-reasoning for these
_TRIVIAL_PATTERNS = {
    "yes","no","ok","okay","y","n","sure","fine","good","thanks","thank you",
    "exit","quit","new session","stop","cancel","abort","go ahead","run it",
    "grant","approve","continue","done","next","skip","hello","hi","hey",
}

def _is_trivial_input(text: str) -> bool:
    """Return True if the input is short/simple enough to skip Gemini pre-reasoning."""
    stripped = text.strip().lower()
    # Exact single-word matches
    if stripped in _TRIVIAL_PATTERNS:
        return True
    # Pure approval/bypass turns
    if "[USER MANUALLY GRANTED BYPASS]" in text:
        return True
    # Very short inputs with no action words
    words = stripped.split()
    if len(words) <= 2:
        action_words = {
            "open","close","start","run","launch","find","search","type","click",
            "navigate","go","read","write","create","delete","move","copy",
            "show","hide","install","download","upload","check","get","set",
            "list","scan","save","load","send","press","scroll","zoom",
        }
        if not any(w in action_words for w in words):
            return True
    return False


def get_gemini_reasoning(user_input: str, conversation_history: list) -> str | None:
    """
    Generate a concrete, tool-by-tool execution plan for the primary model
    to follow. Despite the name (kept for compatibility with existing call
    sites), this now routes across THREE possible planning brains depending
    on OPENROUTER_CONSULT_MODE:

      "always"   — try OpenRouter FIRST (cheap/free API call, no desktop-app
                   dependency, consulted on nearly every non-trivial turn),
                   fall back to Gemini (app then API) if OpenRouter fails.
      "fallback" — try Gemini (app then API) first as before, only fall
                   back to OpenRouter if both Gemini routes fail.
      "off"      — Gemini only, exactly the original behaviour.

    When MODEL_PROVIDER == "openrouter" (OpenRouter is already the PRIMARY
    execution brain), this consult step is skipped entirely — there's no
    benefit to a strong model consulting a plan from itself.
    """
    if config.MODEL_PROVIDER in ("openrouter", "gemini_web", "gemini_api", "groq", "ollama_cloud"):
        # Primary model IS OpenRouter or Gemini-web already — skip the
        # separate consult call, it would just be the same model (or the
        # same account's Gemini session) reasoning about itself twice.
        return None

    # Build the prompt (same regardless of routing)
    non_sys = [m for m in conversation_history if m.get("role") != "system"]
    recent  = non_sys[-6:] if len(non_sys) > 6 else non_sys
    history_text = ""
    for m in recent:
        role    = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, str) and not content.startswith("[SYSTEM"):
            history_text += f"{role.upper()}: {content[:400]}\n"

    os_name = "Linux (bash)" if _IS_LINUX else "Windows (PowerShell)"
    launch  = "nohup /path/to/app &" if _IS_LINUX else "Start-Process 'C:\\path\\to\\app.exe'"
    tool_names   = [t["function"]["name"] for t in tools]
    tool_summary = ", ".join(tool_names)

    prompt = f"""You are the planning brain of Midum, a {os_name} desktop AI agent.
A weak local model ({config.MODEL_NAME}) will execute tool calls based on your plan.
The local model follows instructions literally but cannot reason well — give it explicit steps.

AVAILABLE TOOLS: {tool_summary}

KEY RULES FOR YOUR PLAN:
- To open an app: read_paths → execute_terminal_command("{launch}")
- To interact with a desktop app: list_active_windows() → snapshot(target='<window name>') → act(target='<window name>', index=N)
- To read a web page: list_browser_tabs() → read_browser_page(tab_index=N) — NOT read_aggregated_text (UIA can't read web content)
- To interact with a web page: list_browser_tabs() → snapshot(target='browser') → act(target='browser', index=N)
- To open a URL in Chrome (PREFERRED): open_url(url="https://...") — one call, no clicking needed
- To find an app path: list_paths_indexed() → get_path(index=N) then use path
- To navigate the filesystem: list_directory(path) → open_path(path, index=N)
- To type in a field: FIRST snapshot+act (or click_ui_element) to focus it, THEN type_text(text="...", expected_window="app name")
- To navigate to a URL: ALWAYS use open_url(url="...") — never click the address bar manually
- To wait for an app to open: wait(seconds=2)
- Use canonical window names (e.g. "Google Chrome" not "New Tab - Google Chrome")
- search_internet for quick lookups, then open_search_result(index) to open a result
- Always discover first (list_active_windows / list_browser_tabs) before snapshot — never guess the target name

RECENT CONVERSATION:
{history_text}
USER REQUEST: {user_input}

Write a numbered execution plan for the local model.
Each step must be ONE specific tool call with the exact arguments.
Be explicit — don't say "click the address bar", say:
  click_ui_element(window_title="Google Chrome", description="Address bar")

Format each step as:
  N. tool_name(arg1="value1", arg2="value2") — one-line reason

If this is a simple question or single-tool task, write just 1 step.
If uncertain about a path or app location, include read_paths as step 1.
Maximum 8 steps. Be concise."""

    def _try_openrouter() -> str | None:
        from providers.openrouter_backend import _openrouter_chat_with_fallback
        if not providers_openrouter_backend._OPENROUTER_AVAILABLE:
            return None
        try:
            print(f"🤖 [OpenRouter consult] Model: {config.OPENROUTER_MODEL}")
            resp = _openrouter_chat_with_fallback([{"role": "user", "content": prompt}], model=config.OPENROUTER_MODEL)
            plan = (resp["message"]["content"] or "").strip()
            if plan:
                print(f"🤖 [OpenRouter plan ({len(plan)} chars)]:\n{plan}\n")
                return plan
        except Exception as e:
            print(f"⚠️  [OpenRouter consult failed: {e}]")
        return None

    def _try_gemini() -> str | None:
        # Gemini web chat via the community gemini_webapi library — the
        # only route. No fallback to the metered API: the free web chat
        # UI's usage limits are far more generous than the API's free
        # tier, so a silent API fallback would burn through API credits
        # without anyone noticing.
        from browser_cdp import query_gemini_app
        if _GEMINI_WEBAPI_AVAILABLE:
            try:
                print("🤖 [Gemini web] Sending plan request...")
                result = query_gemini_app(prompt)
                if result and not result.startswith("Error"):
                    plan = result.strip()
                    print(f"🤖 [Gemini web plan ({len(plan)} chars)]:\n{plan}\n")
                    return plan
                else:
                    print(f"⚠️  [Gemini web returned error: {result[:80]}]")
            except Exception as e:
                print(f"⚠️  [Gemini web failed: {e}]")
        return None

    # ── Route selection based on OPENROUTER_CONSULT_MODE ──────────────────────
    if OPENROUTER_CONSULT_MODE == "always":
        plan = _try_openrouter()
        if plan:
            return plan
        return _try_gemini()

    elif OPENROUTER_CONSULT_MODE == "fallback":
        plan = _try_gemini()
        if plan:
            return plan
        return _try_openrouter()

    else:   # "off"
        return _try_gemini()

# Tools that count as "verification" — capped so Midum can't loop forever
_VERIFY_TOOLS = {"fallback_view_screen", "fallback_find_text"}
# Maximum consecutive verification tool calls allowed before we force a reply
MAX_VERIFY_CALLS = 2

def wait(seconds: float) -> str:
    """Pauses thread execution for the specified duration."""
    try:
        time.sleep(seconds)
        return f"Successfully paused for {seconds} seconds."
    except Exception as e:
        return f"Error during wait execution: {str(e)}"

# =============================================================================

# --- from main.py, section 2 ---
# LEGACY TOOL-CALL FALLBACK (qwen2.5-coder and similar)
# =============================================================================
# Models in LEGACY_TOOLCALL_MODELS are still sent the exact same `tools=`
# schema as modern models — Ollama still injects it into their chat template.
# The difference is purely on the READ side: these models frequently put the
# tool call as plain text inside `content` (raw JSON, or wrapped in
# <tool_call></tool_call> tags per the Qwen2.5 template) instead of Ollama's
# structured `tool_calls` field. This block of code ONLY runs as a fallback
# when `response["message"].get("tool_calls")` is already empty, so it never
# touches or alters behavior for models that report tool_calls natively.

# Matches a <tool_call> ... </tool_call> block (Qwen2.5 chat template style)
_TOOLCALL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
# Matches a ```json fenced block
_JSON_FENCE_RE   = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

def _find_balanced_json_objects(text: str):
    """
    Scan text for top-level {...} objects using brace counting (not regex),
    so nested objects like {"name": "x", "arguments": {"a": 1}} are captured
    whole instead of being cut off at the first inner '}'. Returns a list of
    (start, end, blob) tuples for every balanced top-level object found.
    """
    results = []
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    results.append((start, i + 1, text[start:i + 1]))
                    start = None
    return results

_KNOWN_TOOL_NAMES = None  # lazily populated from `tools` schema, see below

def _known_tool_names():
    global _KNOWN_TOOL_NAMES
    if _KNOWN_TOOL_NAMES is None:
        _KNOWN_TOOL_NAMES = {t["function"]["name"] for t in tools}
    return _KNOWN_TOOL_NAMES

def _try_parse_tool_json(blob: str):
    """Parse a JSON blob into a normalized tool_call dict, or None if invalid."""
    try:
        obj = json.loads(blob)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("function") or obj.get("tool")
    args = obj.get("arguments", obj.get("parameters", {}))
    source = obj.get("source") or obj.get("server")
    if not name:
        return None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            pass

    # Explicit source/server field (currently emitted by the Gemini-web
    # backend, see §2.3 of the Gemini implementation brief — routes
    # unambiguously instead of relying on name-collision autodetection).
    # "native" / missing / unrecognized -> fall through to normal handling.
    # "mcp:<server>" or a bare server name/index -> route straight to
    # call_mcp_tool, skipping the fuzzy name-matching autoroute entirely.
    if source and source != "native":
        server = source.split(":", 1)[1] if ":" in source else source
        resolved = _mcp_resolve_name(server)
        if resolved is not None:
            return {"function": {"name": "call_mcp_tool", "arguments": {
                "server": resolved, "tool_name": name, "arguments": args or {}
            }}}
        # Unknown server named explicitly — don't silently fall back to
        # native dispatch (that could hit an unrelated native tool that
        # happens to share this name); surface nothing so the caller's
        # normal "no valid tool call" repair path kicks in.
        return None

    if name not in _known_tool_names():
        # Not a registered top-level tool. This is the exact case that used
        # to be silently dropped (return None) before a name was ever
        # checked against connected MCP servers — which is why a model
        # calling an MCP tool directly (e.g. skipping call_mcp_tool, or
        # mangling underscores: 'get_weather' -> 'getweather') never even
        # reached dispatch. Try to autoroute it through call_mcp_tool first.
        name, args = _mcp_autoroute_tool_call(name, args)
        if name not in _known_tool_names():
            return None
    return {"function": {"name": name, "arguments": args}}

def _extract_legacy_tool_calls(content: str):
    """
    Scan a model's free-text `content` for one or more tool calls when the
    structured tool_calls field came back empty. Returns (tool_calls, leftover_text)
    where leftover_text is the content with the recognized tool-call JSON stripped
    out (so it isn't shown to the user / re-fed as a duplicate plain message).
    Returns ([], content) if nothing parseable is found — caller treats that
    as a normal plain-text reply, completely transparent for modern models.
    """
    if not content or not content.strip():
        return [], content

    found = []
    cleaned = content

    # 1) <tool_call>...</tool_call> tags — may be one or several
    tag_matches = list(_TOOLCALL_TAG_RE.finditer(content))
    if tag_matches:
        for m in tag_matches:
            parsed = _try_parse_tool_json(m.group(1))
            if parsed:
                found.append(parsed)
        if found:
            cleaned = _TOOLCALL_TAG_RE.sub("", content).strip()
            return found, cleaned

    # 2) ```json fenced block
    fence_match = _JSON_FENCE_RE.search(content)
    if fence_match:
        parsed = _try_parse_tool_json(fence_match.group(1))
        if parsed:
            cleaned = _JSON_FENCE_RE.sub("", content, count=1).strip()
            return [parsed], cleaned

    # 3) Bare JSON object(s) anywhere in the text (most common qwen2.5-coder
    #    case: the ENTIRE content is just the JSON object, nothing else).
    #    Brace-balanced scan handles nested "arguments": {...} correctly.
    candidates = _find_balanced_json_objects(content)
    if candidates:
        consumed_spans = []
        for start, end, blob in candidates:
            parsed = _try_parse_tool_json(blob)
            if parsed:
                found.append(parsed)
                consumed_spans.append((start, end))
        if found:
            cleaned = content
            for start, end in sorted(consumed_spans, reverse=True):
                cleaned = cleaned[:start] + cleaned[end:]
            cleaned = cleaned.strip()
            # Suppress leftover that is pure punctuation/whitespace — these are
            # JSON fragments the stripper didn't fully consume, not real replies.
            if re.match(r'^[{}\[\]",:\s]*$', cleaned):
                cleaned = ""
            return found, cleaned

    if content.strip():
        print(f"   [Legacy parser] No tool call found. Raw content: {content[:200]!r}")
    return [], content


def _call_ollama(messages, result_q):
    """Run ollama.chat on a background thread and put the result in result_q."""
    try:
        resp = ollama.chat(model=config.MODEL_NAME, messages=messages, tools=tools)

        # ── Legacy fallback: only engages if native tool_calls is empty AND
        #    the active model is a known weak-tool-calling family. Modern
        #    models always have tool_calls populated natively and never reach
        #    this branch, so their behavior/performance is unchanged. ──────────
        if not resp["message"].get("tool_calls") and _is_legacy_toolcall_model(config.MODEL_NAME):
            raw_content = resp["message"].get("content") or ""
            legacy_calls, cleaned_content = _extract_legacy_tool_calls(raw_content)
            if legacy_calls:
                # Enforce one tool at a time for legacy models — batching is
                # unreliable and produces malformed JSON for the second call.
                legacy_calls = legacy_calls[:1]
                msg_dict = dict(resp["message"])
                msg_dict["tool_calls"] = legacy_calls
                msg_dict["content"]    = cleaned_content
                msg_dict.setdefault("role", "assistant")
                try:
                    resp["message"] = msg_dict
                except (TypeError, KeyError):
                    try:
                        resp.message = msg_dict
                    except Exception:
                        resp = {**dict(resp), "message": msg_dict}

        result_q.put(("ok", resp))
    except Exception as e:
        result_q.put(("err", e))


def _call_openrouter_primary(messages, result_q, model_override: str = None):
    from providers.openrouter_backend import _openrouter_chat_with_fallback
    """
    Run OpenRouter chat completion on a background thread — used when
    MODEL_PROVIDER == "openrouter" (OpenRouter driving Midum directly,
    not just consulted), OR when a caller forces OpenRouter for a single
    sub-turn via _call_primary_model(provider_override="openrouter").

    model_override lets delegate_to_openrouter() run the sub-agent on a
    different model than config.OPENROUTER_MODEL for that one delegated task.

    Many free-tier OpenRouter models have inconsistent native tool-calling
    support (same failure mode as qwen2.5-coder locally) — they emit the
    tool call as JSON inside `content` instead of the structured field.
    We reuse the same legacy-parser fallback here for that reason, applied
    unconditionally (not gated by _is_legacy_toolcall_model, since we can't
    maintain a family list for every free OpenRouter model).
    """
    try:
        use_model = model_override or config.OPENROUTER_MODEL
        resp = _openrouter_chat_with_fallback(messages, model=use_model, tools_schema=tools)

        if not resp["message"].get("tool_calls"):
            raw_content = resp["message"].get("content") or ""
            legacy_calls, cleaned_content = _extract_legacy_tool_calls(raw_content)
            if legacy_calls:
                legacy_calls = legacy_calls[:1]   # one tool at a time
                resp["message"]["tool_calls"] = legacy_calls
                resp["message"]["content"]    = cleaned_content

        result_q.put(("ok", resp))
    except Exception as e:
        result_q.put(("err", e))


def _call_gemini_api_primary(messages, result_q, model_override: str = None):
    """
    Run a Gemini API (official) chat completion on a background thread —
    used when MODEL_PROVIDER == "gemini_api" (Gemini API driving Midum
    directly), OR when a caller forces it for a single sub-turn via
    _call_primary_model(provider_override="gemini_api").

    model_override lets delegate_to_gemini_api() run the sub-agent on a
    different model than config.GEMINI_API_MODEL for that one delegated task.

    Gemini's structured function-calling is reliable, but we still run the
    same legacy free-text <tool_call> fallback used for OpenRouter/Ollama
    as a safety net in case a given model ever emits the call as text
    instead of the structured field.
    """
    try:
        use_model = model_override or config.GEMINI_API_MODEL
        resp = _gemini_api_chat(messages, model=use_model, tools_schema=tools)

        if not resp["message"].get("tool_calls"):
            raw_content = resp["message"].get("content") or ""
            legacy_calls, cleaned_content = _extract_legacy_tool_calls(raw_content)
            if legacy_calls:
                legacy_calls = legacy_calls[:1]   # one tool at a time
                resp["message"]["tool_calls"] = legacy_calls
                resp["message"]["content"]    = cleaned_content

        result_q.put(("ok", resp))
    except Exception as e:
        result_q.put(("err", e))


def _call_groq_primary(messages, result_q, model_override: str = None):
    """
    Run a GroqCloud chat completion on a background thread — used when
    MODEL_PROVIDER == "groq" (GroqCloud driving Midum directly), OR when a
    caller forces it for a single sub-turn via
    _call_primary_model(provider_override="groq").

    model_override lets delegate_to_groq() run the sub-agent on a different
    model than config.GROQ_MODEL for that one delegated task.

    GroqCloud's structured function-calling is reliable on supported
    models, but we still run the same legacy free-text <tool_call> fallback
    used for OpenRouter/Gemini API/Ollama as a safety net in case a given
    model ever emits the call as text instead of the structured field.
    """
    try:
        use_model = model_override or config.GROQ_MODEL
        resp = _groq_chat_with_fallback(messages, model=use_model, tools_schema=_get_groq_tools_schema())

        if not resp["message"].get("tool_calls"):
            raw_content = resp["message"].get("content") or ""
            legacy_calls, cleaned_content = _extract_legacy_tool_calls(raw_content)
            if legacy_calls:
                legacy_calls = legacy_calls[:1]   # one tool at a time
                resp["message"]["tool_calls"] = legacy_calls
                resp["message"]["content"]    = cleaned_content

        result_q.put(("ok", resp))
    except Exception as e:
        result_q.put(("err", e))


def _call_ollama_cloud_primary(messages, result_q, model_override: str = None):
    """
    Run an Ollama Cloud chat completion on a background thread — used when
    MODEL_PROVIDER == "ollama_cloud" (Ollama Cloud driving Midum directly),
    OR when a caller forces it for a single sub-turn via
    _call_primary_model(provider_override="ollama_cloud").

    model_override lets delegate_to_ollama_cloud() run the sub-agent on a
    different model than config.OLLAMA_CLOUD_MODEL for that one delegated task.

    Ollama Cloud uses the same native `ollama` client as the local provider,
    so tool_calls already come back in the structured field — the legacy
    free-text fallback is still run as a safety net, same as every other
    provider.
    """
    try:
        use_model = model_override or config.OLLAMA_CLOUD_MODEL
        resp = _ollama_cloud_chat_with_fallback(messages, model=use_model, tools_schema=tools)

        if not resp["message"].get("tool_calls"):
            raw_content = resp["message"].get("content") or ""
            legacy_calls, cleaned_content = _extract_legacy_tool_calls(raw_content)
            if legacy_calls:
                legacy_calls = legacy_calls[:1]   # one tool at a time
                resp["message"]["tool_calls"] = legacy_calls
                resp["message"]["content"]    = cleaned_content

        result_q.put(("ok", resp))
    except Exception as e:
        result_q.put(("err", e))


def _call_primary_model(messages, result_q, provider_override: str = None, model_override: str = None):
    """
    Dispatches to the configured primary model provider (see MODEL_PROVIDER
    at the top of this file). Every branch populates result_q with the same
    ("ok", resp) / ("err", exc) contract, so process_chat_turn is completely
    unaware of which backend actually ran.

    provider_override lets a caller force a specific provider for THIS call
    only, without touching the global MODEL_PROVIDER — used by
    delegate_to_openrouter() / delegate_to_gemini_api() / delegate_to_groq()
    to run a sub-agent turn on a specific provider even when something else
    is the configured primary.
    """
    provider = provider_override or config.MODEL_PROVIDER
    if provider == "openrouter":
        _call_openrouter_primary(messages, result_q, model_override=model_override)
    elif provider == "gemini_web":
        _call_gemini_web_primary(messages, result_q, model_override=model_override)
    elif provider == "gemini_api":
        _call_gemini_api_primary(messages, result_q, model_override=model_override)
    elif provider == "groq":
        _call_groq_primary(messages, result_q, model_override=model_override)
    elif provider == "ollama_cloud":
        _call_ollama_cloud_primary(messages, result_q, model_override=model_override)
    else:
        _call_ollama(messages, result_q)


MAX_ACTION_TRIES = 3


# --- from main.py, section 3 ---
# COMMAND SAFETY
# =============================================================================

def _load_commands_whitelist() -> set:
    """Load the set of known-good command names from commands.md."""
    if not os.path.exists(COMMANDS_FILE):
        return set()
    try:
        content = open(COMMANDS_FILE, "r", encoding="utf-8").read()
        tokens  = re.findall(r"`([^`]+)`", content)
        for line in content.splitlines():
            stripped = line.strip().lstrip("-#* ")
            if stripped:
                tokens.append(stripped.split()[0])
        return {t.lower() for t in tokens if t.strip()}
    except Exception:
        return set()


def _command_looks_known(cmd: str, whitelist: set) -> bool:
    """Return True if the first token of cmd is in the whitelist."""
    if not whitelist:
        return True
    first = cmd.strip().split()[0].lower() if cmd.strip() else ""
    return first in whitelist


def _check_command_rules(cmd: str, paths_consulted: bool = False) -> str | None:
    """
    Return a violation message if the command breaks a hard rule, else None.
    Platform-specific — Linux rules block Windows commands and vice-versa.
    """
    low = cmd.lower().strip()

    if _IS_LINUX:
        win_only = ["powershell", "start-process", "get-childitem",
                    "cmd.exe", "cmd /c", "cmd /k"]
        for w in win_only:
            if low.startswith(w):
                return (
                    f"Windows command detected: '{cmd.split()[0]}'. "
                    "Use bash/Linux equivalents (ls, cd, find, grep, xdg-open, nohup, etc)."
                )
        return None

    # Windows rules
    bare_launch = re.match(
        r'^start-process\s+[\'"]?(?![a-z]:\\)[^\s\'"]+[\'"]?\s*$',
        cmd, re.IGNORECASE
    )
    if bare_launch:
        return (
            "You used Start-Process without a full path. "
            "Call read_paths first to get the correct full path, then use "
            "Start-Process 'C:\\full\\path\\to\\app.exe'."
        )
    cmd_only_patterns = ["^start ", "^cd ", "^dir ", "^echo ",
                         "^copy ", "^del ", "^mkdir "]
    for pattern in cmd_only_patterns:
        if re.match(pattern, low) and not low.startswith("start-process"):
            return (
                f"CMD command detected: '{cmd.split()[0]}'. "
                "Use PowerShell equivalents only (Start-Process, Set-Location, "
                "Get-ChildItem, Write-Output, Copy-Item, Remove-Item, New-Item)."
            )
    return None


# =============================================================================

# --- from main.py, section 4 ---
# TURN STATE — structured execution tracker
# =============================================================================

class TurnState:
    """
    Tracks exactly what has happened during a turn so the step prompt
    can give the model precise, factual context instead of generic nudges.

    Updated by the tool dispatch after every tool call. Read by the step
    prompt generator to produce a context-specific next-step message.
    """

    def __init__(self, user_request: str, gemini_plan: str = ""):
        self.user_request      = user_request
        self.gemini_plan       = gemini_plan
        self.steps_done:  list[dict] = []   # {tool, args, result, success}
        self.apps_launched:    list[str] = []
        self.windows_clicked:  list[str] = []   # window titles clicked in
        self.fields_clicked:   list[str] = []   # element descriptions clicked
        self.text_typed:       list[str] = []
        self.urls_navigated:   list[str] = []
        self.files_written:    list[str] = []
        self.commands_run:     list[str] = []
        self.last_tool:        str = ""
        self.last_args:        dict = {}
        self.last_result:      str = ""
        self.last_success:     bool = True
        # Inferred task requirements
        self.requires_typing:  bool = False
        self.requires_submit:  bool = False
        self.focused_field:    str = ""    # last input field clicked

    def record(self, tool: str, args: dict, result: str):
        """Call after every tool execution to update state."""
        success = not any(
            result.startswith(e) for e in
            ["Error", "[RULE VIOLATION]", "[TYPING ABORTED]",
             "Unknown tool", "[UNKNOWN TOOL]", "Execution failed"]
        )
        self.steps_done.append({
            "tool": tool, "args": args,
            "result": result[:200], "success": success
        })
        self.last_tool    = tool
        self.last_args    = args
        self.last_result  = result
        self.last_success = success

        # ── Infer system state changes ────────────────────────────────────────
        if tool == "open_url" and success:
            url = args.get("url", "")
            if url:
                self.urls_navigated.append(url)
                self.requires_submit = False   # open_url submits automatically

        elif tool == "execute_terminal_command" and success:
            cmd = args.get("command", "")
            self.commands_run.append(cmd)
            # Detect app launches
            for app in ["chrome", "brave", "firefox", "code", "slack",
                        "discord", "notepad", "explorer", "spotify",
                        "terminal", "vlc", "obs", "zoom", "teams"]:
                if app in cmd.lower():
                    self.apps_launched.append(app)
                    break

        elif tool in ("click_ui_element", "manual_interact_with_ui") and success:
            window = args.get("window_title", "")
            desc   = args.get("description", "").lower()
            action = args.get("action", "click")
            if window:
                self.windows_clicked.append(window)
            if action == "click":
                self.fields_clicked.append(desc)
                # Track if an input field was focused
                _INPUT_HINTS = {
                    "address bar", "address and search bar", "url", "omnibox",
                    "search", "search box", "search bar", "search field",
                    "input", "text field", "text box", "entry", "edit",
                    "message", "message input", "chat input", "compose",
                    "prompt", "enter a prompt", "type a message",
                }
                if any(hint in desc for hint in _INPUT_HINTS):
                    self.focused_field  = desc
                    self.requires_typing = True

        elif tool == "type_text" and success:
            text = args.get("text", "")
            self.text_typed.append(text)
            self.requires_typing  = False
            self.focused_field    = ""
            # If they typed a URL, check for Enter (submit)
            if text.startswith(("http", "www", "youtube", "google")):
                self.urls_navigated.append(text)
                special = args.get("special_key", "")
                if not special:
                    self.requires_submit = True
                else:
                    self.requires_submit = False

        elif tool == "write_local_file" and success:
            self.files_written.append(args.get("path", ""))

    def build_step_prompt(self) -> str:
        """
        Generate a precise, factual step prompt based on current state.
        This replaces all the generic "if task done reply" messages.
        """
        lines = ["[SYSTEM — EXECUTION STATE]:"]

        # What has been done
        if self.steps_done:
            done_summary = []
            for s in self.steps_done:
                t = s["tool"]
                a = s["args"]
                ok = "✓" if s["success"] else "✗"
                if t == "execute_terminal_command":
                    done_summary.append(f"  {ok} Ran command: {a.get('command','')[:60]}")
                elif t == "click_ui_element":
                    done_summary.append(
                        f"  {ok} Clicked '{a.get('description','')}' "
                        f"in '{a.get('window_title','')}'"
                    )
                elif t == "type_text":
                    done_summary.append(
                        f"  {ok} Typed: '{a.get('text','')[:40]}'"
                        + (f" + {a.get('special_key')}" if a.get("special_key") else "")
                    )
                elif t in ("read_paths", "read_path", "read_instructions"):
                    done_summary.append(f"  {ok} Read {t}")
                elif t == "wait":
                    done_summary.append(f"  {ok} Waited {a.get('seconds',0)}s")
                elif t == "say":
                    pass   # don't list narration as a step
                else:
                    done_summary.append(f"  {ok} {t}")
            lines.append("Completed steps:\n" + "\n".join(done_summary))

        # Current system state
        state_facts = []
        if self.apps_launched:
            state_facts.append(f"Apps launched this turn: {', '.join(self.apps_launched)}")
        if self.focused_field:
            state_facts.append(f"Input field currently focused: '{self.focused_field}'")
        if self.text_typed:
            state_facts.append(f"Text typed: {', '.join(repr(t[:30]) for t in self.text_typed)}")
        if self.urls_navigated:
            state_facts.append(f"URLs entered: {', '.join(self.urls_navigated)}")
        if state_facts:
            lines.append("Current state:\n" + "\n".join(f"  • {f}" for f in state_facts))

        # Last result
        if self.last_result and self.last_tool not in (
            "read_paths", "read_path", "read_instructions",
            "read_local_file", "read_file_smart"
        ):
            short = self.last_result[:120].replace("\n", " ")
            lines.append(f"Last result: {short}")

        # Required next action (explicit)
        lines.append("")
        if not self.last_success:
            lines.append(
                "⚠ LAST ACTION FAILED. Do NOT assume the task is done. "
                "Read the error above and retry with a corrected approach."
            )
        elif self.requires_typing and self.focused_field:
            lines.append(
                f"▶ NEXT REQUIRED ACTION: call type_text now. "
                f"The field '{self.focused_field}' is focused and waiting for input. "
                f"Do NOT reply with text — call type_text immediately."
            )
        elif self.requires_submit:
            lines.append(
                "▶ NEXT REQUIRED ACTION: the URL/text has been typed but not submitted. "
                "Call type_text with special_key='Enter' to submit, OR "
                "click_ui_element to click a submit/Go button."
            )
        elif self.apps_launched and not self.urls_navigated and not self.fields_clicked:
            lines.append(
                f"▶ App was launched. If the user asked you to do something inside it "
                f"(open a URL, click something, type something), do that now. "
                f"Use wait(2) first if the app needs time to open."
            )
        else:
            lines.append(
                "▶ If more steps remain to complete the user's request, call the next tool now. "
                "Only reply with text when the entire task is fully done."
            )

        # Remind model of the original plan if it exists
        if self.gemini_plan and len(self.steps_done) < 6:
            lines.append(
                f"\n[ORIGINAL PLAN — follow it]\n{self.gemini_plan}"
            )

        return "\n".join(lines)


def _decompose_task(user_request: str) -> str | None:
    """
    Pre-decompose a user request into explicit numbered steps that get
    injected before the first model call. This gives the model a concrete
    plan to follow rather than having to reason about the full task at
    every step.

    Returns a system message string, or None if the request is too simple
    to need decomposition (single-step tasks).
    """
    req = user_request.lower().strip()

    # ── Detect multi-step patterns ────────────────────────────────────────────
    # "open X and do Y" / "open X then do Y" / "open X, then Y"
    open_then = re.search(
        r"open\s+(\w[\w\s]*?)(?:\s+and|\s+then|,\s*then|\s*,)\s+(.+)",
        req
    )
    # "type X in Y" / "type X into Y"
    type_in = re.search(r"type\s+.+\s+in(?:to)?\s+\w", req)
    # "go to X" / "navigate to X" / "open URL X"
    navigate = re.search(
        r"(?:go to|navigate to|open url|visit|load)\s+([\w./:-]+)", req
    )
    # "search for X in Y"
    search_in = re.search(r"search\s+(?:for\s+)?(.+?)\s+in\s+(\w[\w\s]+)", req)

    steps = []

    if open_then:
        app   = open_then.group(1).strip()
        after = open_then.group(2).strip()
        steps.append(f"1. read_paths to find the path for {app}")
        steps.append(f"2. execute_terminal_command to launch {app}")
        steps.append(f"3. wait(2) for {app} to open")
        # Determine what to do after opening
        if any(w in after for w in ["url", "youtube", "google", "http", "www",
                                     "website", "site", "page", "navigate", "go to"]):
            steps.append(f"4. click_ui_element to click the address bar in {app}")
            steps.append(f"5. type_text to type the URL with special_key='Enter'")
        elif any(w in after for w in ["type", "write", "enter", "input"]):
            steps.append(f"4. click_ui_element to click the target field in {app}")
            steps.append(f"5. type_text to type the requested text")
        elif any(w in after for w in ["search"]):
            steps.append(f"4. click_ui_element to click the search box in {app}")
            steps.append(f"5. type_text to type the search query with special_key='Enter'")
        else:
            steps.append(f"4. click_ui_element or type_text as needed in {app}")

    elif navigate and not open_then:
        url = navigate.group(1).strip()
        steps.append("1. click_ui_element to click the address bar in the browser")
        steps.append(f"2. type_text to type '{url}' with special_key='Enter'")

    elif type_in:
        steps.append("1. click_ui_element to click the target input field")
        steps.append("2. type_text to type the requested text")

    elif search_in:
        query = search_in.group(1).strip()
        app   = search_in.group(2).strip()
        steps.append(f"1. click_ui_element to click the search box in {app}")
        steps.append(f"2. type_text to type '{query}' with special_key='Enter'")

    if not steps:
        return None   # single-step task, no decomposition needed

    plan = (
        f"[TASK PLAN — follow these steps IN ORDER, do not skip any]\n"
        + "\n".join(steps)
        + "\n\nComplete ALL steps above before giving a final reply."
    )
    return plan


_STALL_PATTERNS = re.compile(
    r'^\s*(i will|i\'ll|i am going to|i\'m going to|let me|now i will|next i will|'
    r'next,? i will|first,? i will|i need to|i am about to|i\'m about to|'
    r'to do this,? i will|here\'s what i\'ll do|here is what i\'ll do|'
    r'i plan to|my plan is|the plan is|steps? to (do|complete) this)\b',
    re.IGNORECASE
)

_STALL_COMPLETION_HINTS = re.compile(
    r'\b(done|completed|finished|successfully|here is the result|here\'s the result|'
    r'here is your|here\'s your|i have (opened|created|written|deleted|found|sent|updated))\b',
    re.IGNORECASE
)

def _looks_like_stalled_plan(text: str) -> bool:
    """
    True if the model's plain-text reply reads like it's ANNOUNCING an
    intended action ("I'll open the file and...") rather than reporting a
    completed result or asking the user something. Used to catch the model
    narrating steps instead of actually calling the tools that do them.
    """
    if not text:
        return False
    t = text.strip()
    if t.endswith("?"):
        return False   # legitimately waiting on the user
    if _STALL_COMPLETION_HINTS.search(t):
        return False   # reads like a completed result
    if _STALL_PATTERNS.match(t):
        return True
    # Numbered step list ("1. ...\n2. ...") with no completion language
    if re.search(r'(?:^|\n)\s*[1-9]\.\s', t) and len(t) < 800:
        return True
    return False


def process_chat_turn(conversation_history, user_request: str = "", gemini_plan: str = "",
                       force_provider: str = None, force_model: str = None,
                       max_steps: int = 20):
    from main import _print_reply
    from browser_cdp import act_on_browser_element, list_browser_tabs, query_gemini_app, read_browser_page, run_js_in_browser, snapshot_browser_elements
    from providers.openrouter_backend import consult_openrouter, delegate_to_openrouter, list_openrouter_models, set_openrouter_model, set_openrouter_model_by_index
    """
    force_provider / force_model let a caller run this ENTIRE step loop on a
    specific model backend regardless of the global MODEL_PROVIDER — used by
    delegate_to_openrouter() to spin up a self-contained OpenRouter "coworker"
    sub-agent that has full tool access via the exact same engine as the
    primary loop, without permanently switching Midum's primary provider.
    """
    clear_response_memory()
    turn_tool_outputs     = []
    _said_parts           = []
    _accumulated_reply    = []
    whitelist             = _load_commands_whitelist()
    verify_call_count     = 0
    action_attempt_counts: dict = {}
    _paths_consulted      = False
    _abort_event.clear()
    MAX_STEPS  = max_steps
    step_count = 0
    state      = TurnState(user_request, gemini_plan=gemini_plan)
    stall_nudge_count  = 0
    MAX_STALL_NUDGES   = 3

    # Keep system messages always; slide a window over the rest
    HISTORY_WINDOW = 20

    while True:
        # ── Ctrl+Q abort check ────────────────────────────────────────────────
        if _abort_event.is_set():
            print("\n🛑 [Response aborted by Ctrl+Q]")
            return "[Response terminated by user.]", turn_tool_outputs

        # ── Step ceiling ──────────────────────────────────────────────────────
        step_count += 1
        if step_count > MAX_STEPS:
            msg = "[MAX STEPS REACHED] Midum exceeded the step limit for this turn."
            print(f"\n🚫 {msg}")
            _accumulated_reply.append(msg)
            break

        sys_msgs = [m for m in conversation_history if m.get("role") == "system"]
        non_sys  = [m for m in conversation_history if m.get("role") != "system"]
        trimmed  = sys_msgs + non_sys[-HISTORY_WINDOW:]

        # ── Run the primary model on a thread so Ctrl+Q can interrupt the wait ─
        result_q = _queue.Queue()
        t = threading.Thread(
            target=_call_primary_model,
            args=(trimmed, result_q, force_provider, force_model),
            daemon=True
        )
        t.start()
        while t.is_alive():
            if _abort_event.is_set():
                print("\n🛑 [Response aborted by Ctrl+Q]")
                return "[Response terminated by user.]", turn_tool_outputs
            t.join(timeout=0.1)   # check abort flag every 100 ms

        status, payload = result_q.get()
        if status == "err":
            effective_provider = force_provider or config.MODEL_PROVIDER
            provider_name = {
                "openrouter": "OpenRouter",
                "gemini_web": "Gemini-web",
                "gemini_api": "Gemini-API",
                "groq": "Groq",
                "ollama_cloud": "Ollama Cloud",
            }.get(effective_provider, "Ollama")
            return f"[{provider_name} error: {payload}]", turn_tool_outputs
        response   = payload
        tool_calls = response["message"].get("tool_calls")
        if tool_calls:
            tool_calls = tool_calls[:1]   # one step at a time — more reliable for all models
        msg_content = (response["message"].get("content") or "").strip()

        # ── Capture any prose the model emitted alongside tool calls ──────────
        # Legacy models often put explanatory text in content even when they
        # also emit tool calls. Collect it so it isn't lost, but filter junk.
        if msg_content and not re.match(r'^[{}\[\]",:\s]*$', msg_content):
            _accumulated_reply.append(msg_content)
            if msg_content and tool_calls:
                print(f"\n💬 Midum: {msg_content}")

        if not tool_calls:
            conversation_history.append(response["message"])

            if _looks_like_stalled_plan(msg_content) and stall_nudge_count < MAX_STALL_NUDGES:
                stall_nudge_count += 1
                print(f"\n⚠️  [Midum announced an action instead of doing it — nudging ({stall_nudge_count}/{MAX_STALL_NUDGES})]")
                conversation_history.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM]: You just described what you're about to do instead of "
                        "doing it. Do not narrate steps in plain text — call the actual "
                        "tool for the next step RIGHT NOW. No commentary, just the tool call."
                    )
                })
                continue

            # Combine all text accumulated across the whole turn
            full_reply = "\n\n".join(p for p in _accumulated_reply if p.strip())
            return full_reply, turn_tool_outputs

        # ── Verification loop cap ─────────────────────────────────────────────
        all_verify = all(
            tc["function"]["name"] in _VERIFY_TOOLS for tc in tool_calls
        )
        if all_verify:
            verify_call_count += 1
            if verify_call_count > MAX_VERIFY_CALLS:
                # Force the model to stop verifying and give a final reply.
                # IMPORTANT: response["message"] still carries tool_calls, and every
                # provider (Gemini API strictly, others loosely) expects a function-call
                # turn to be immediately followed by a matching function-response turn —
                # so we must close it out with a synthetic tool response BEFORE the
                # plain user nudge, instead of leaving the tool call dangling.
                conversation_history.append(response["message"])
                for tc in tool_calls:
                    conversation_history.append({
                        "role": "tool",
                        "content": (
                            "[SKIPPED] Verification call limit reached — this call was "
                            "not executed. Stop verifying and give your final reply."
                        ),
                    })
                conversation_history.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM]: You have verified the result enough times. "
                        "Stop calling fallback_view_screen or fallback_find_text. "
                        "Give your final plain-text reply to the user now."
                    )
                })
                continue
        else:
            verify_call_count = 0   # reset counter when a real action runs

        conversation_history.append(response["message"])
        print(f"\n⚡ Midum requested {len(tool_calls)} action(s)...")

        needs_lookup = False
        unknown_cmd  = ""

        for tool in tool_calls:
            func_name = tool["function"]["name"]
            raw_args  = tool["function"]["arguments"]
            if isinstance(raw_args, str):
                try:    arguments = json.loads(raw_args)
                except: arguments = {}
            else:
                arguments = raw_args

            # MCP autoroute: covers the structured/native tool_calls path
            # (the legacy free-text JSON path is handled inside
            # _try_parse_tool_json instead, before it ever gets here).
            # If the model called an MCP server's tool directly by name —
            # bypassing call_mcp_tool, with underscores/case/hyphens
            # mangled or not — this rewrites it to the uniform
            # call_mcp_tool(server, tool_name, arguments) call transparently.
            func_name, arguments = _mcp_autoroute_tool_call(func_name, arguments)

            print(f" -> Executing: '{func_name}'")
            tool_images = None

            # ── Hard retry cap ────────────────────────────────────────────────
            _EXEMPT = {"update_memory","set_current_goal","add_instruction","add_path","explore_path","write_response_memory","append_response_memory","read_response_memory"}
            _cap_hit = False
            if func_name not in _EXEMPT:
                _karg = next((str(arguments[k])[:80] for k in
                    ("command","path","text","query","prompt","skill_name","name","instruction")
                    if k in arguments), "")
                _akey = (func_name, _karg)
                action_attempt_counts[_akey] = action_attempt_counts.get(_akey, 0) + 1
                if action_attempt_counts[_akey] > MAX_ACTION_TRIES:
                    cap_msg = (f"[RETRY CAP] '{func_name}' attempted {MAX_ACTION_TRIES} "
                               f"times with the same argument and has not succeeded. "
                               f"Stop retrying immediately. Tell the user what failed "
                               f"and ask how they want to proceed.")
                    print(f"\n🚫 [Retry cap reached for '{func_name}']")
                    turn_tool_outputs.append(cap_msg)
                    conversation_history.append({"role":"tool","content": cap_msg})
                    _cap_hit = True
            if _cap_hit:
                break   # exits the for-tool loop; then the while loop gets
                        # one more model call to produce the failure reply

            # ── Tool dispatch ──────────────────────────────────────────────────
            if func_name == "list_more_tools":
                tool_output = list_more_tools()

            elif func_name == "load_tool_by_index":
                tool_output = load_tool_by_index(int(arguments.get("index", 0)))

            elif func_name == "read_local_file":
                raw_path           = arguments.get("path", "")
                resolved, res_msg  = resolve_file_path(raw_path)
                file_result        = read_local_file(resolved)
                tool_output        = (f"[PATH RESOLVED: {res_msg}]\n{file_result}"
                                      if res_msg else file_result)

            elif func_name == "write_local_file":
                raw_path          = arguments.get("path", "")
                resolved, res_msg = resolve_file_path(raw_path)
                tool_output       = write_local_file(resolved, arguments.get("content"))
                if res_msg:
                    tool_output = f"[PATH RESOLVED: {res_msg}] {tool_output}"

            elif func_name == "append_local_file":
                raw_path          = arguments.get("path", "")
                resolved, res_msg = resolve_file_path(raw_path)
                tool_output       = append_local_file(resolved, arguments.get("content"))
                if res_msg:
                    tool_output = f"[PATH RESOLVED: {res_msg}] {tool_output}"

            elif func_name == "search_internet":
                tool_output = search_internet(arguments.get("query"))

            elif func_name == "open_url":
                url     = arguments.get("url", "")
                browser = arguments.get("browser", "chrome")
                print(f"   [open_url] {url}")
                tool_output = open_url(url, browser)

            elif func_name == "list_directory":
                tool_output = list_directory(arguments.get("path", STARTUP_DIR))

            elif func_name == "open_path":
                tool_output = open_path(
                    arguments.get("path", STARTUP_DIR),
                    int(arguments.get("index", 0))
                )

            elif func_name == "find_file":
                tool_output = find_file(
                    arguments.get("filename", ""),
                    arguments.get("search_root", "")
                )

            elif func_name == "open_path_by_index":
                tool_output = open_path_by_index(int(arguments.get("index", 0)))

            elif func_name == "list_skills_indexed":
                tool_output = list_skills_indexed()

            elif func_name == "load_skill_by_index":
                tool_output = load_skill_by_index(int(arguments.get("index", 0)))

            elif func_name == "list_paths_indexed":
                tool_output = list_paths_indexed()
                _paths_consulted = True

            elif func_name == "get_path":
                tool_output = get_path(int(arguments.get("index", 0)))
                _paths_consulted = True

            elif func_name == "list_domain_knowledge_indexed":
                tool_output = list_domain_knowledge_indexed()

            elif func_name == "read_domain_by_index":
                tool_output = read_domain_by_index(int(arguments.get("index", 0)))

            elif func_name == "list_domain_skills_indexed":
                tool_output = list_domain_skills_indexed()

            elif func_name == "open_search_result":
                tool_output = open_search_result(
                    int(arguments.get("index", 0)),
                    arguments.get("browser", "chrome")
                )

            elif func_name == "ocr_snapshot":
                print("   [OCR Snapshot] Capturing screen...")
                tool_output = ocr_snapshot()

            elif func_name == "click_ocr_index":
                tool_output = click_ocr_index(
                    int(arguments.get("index", 0)),
                    arguments.get("click_type", "left_click")
                )

            elif func_name == "read_browser_page":
                tab_index = int(arguments.get("tab_index", 0))
                print(f"   [CDP] Reading browser page tab={tab_index}")
                tool_output = read_browser_page(tab_index)

            elif func_name == "list_browser_tabs":
                print("   [CDP] Listing browser tabs")
                tool_output = list_browser_tabs()

            elif func_name in ("snapshot", "snapshot_ui", "snapshot_browser_elements"):
                target      = arguments.get("target") or arguments.get("window_title", "")
                filter_type = arguments.get("filter_type", "")

                # Determine routing: 'browser' / 'browser:N' → CDP; anything else → UIA
                if target.lower().startswith("browser"):
                    tab_index = 0
                    if ":" in target:
                        try:
                            tab_index = int(target.split(":", 1)[1])
                        except ValueError:
                            pass
                    # Legacy schema compatibility
                    if not target and "tab_index" in arguments:
                        tab_index = int(arguments.get("tab_index", 0))
                    print(f"   [CDP] snapshot browser tab={tab_index}"
                          + (f" filter={filter_type}" if filter_type else ""))
                    tool_output = snapshot_browser_elements(tab_index, filter_type)
                else:
                    print(f"   [UIA] snapshot '{target}'"
                          + (f" filter={filter_type}" if filter_type else ""))
                    if ui_navigator is None:
                        tool_output = _uia_unavailable_message()
                    else:
                        tool_output = ui_navigator.snapshot_ui(target, filter_type)

            elif func_name in ("act", "act_on_element", "act_on_browser_element"):
                target       = arguments.get("target") or arguments.get("window_title", "")
                index        = int(arguments.get("index", 0))
                action       = arguments.get("action", "click")
                text_to_type = arguments.get("text_to_type", "")

                if target.lower().startswith("browser"):
                    tab_index = 0
                    if ":" in target:
                        try:
                            tab_index = int(target.split(":", 1)[1])
                        except ValueError:
                            pass
                    if not target and "tab_index" in arguments:
                        tab_index = int(arguments.get("tab_index", 0))
                    print(f"   [CDP] act #{index} action={action} tab={tab_index}")
                    tool_output = act_on_browser_element(index, action, text_to_type, tab_index)
                else:
                    print(f"   [UIA] act #{index} action={action} in '{target}'")
                    if ui_navigator is None:
                        tool_output = _uia_unavailable_message()
                    else:
                        tool_output = ui_navigator.act_on_element_by_index(
                            target, index, action, text_to_type
                        )

            elif func_name == "run_js_in_browser":
                script    = arguments.get("script", "")
                tab_index = int(arguments.get("tab_index", 0))
                print(f"   [CDP] run_js: {script[:60]}")
                tool_output = run_js_in_browser(script, tab_index)

            elif func_name == "execute_terminal_command":
                cmd = arguments.get("command", "").strip()
                if not cmd:
                    tool_output = "Error: No command provided."
                else:
                    # ── Rule enforcement ──────────────────────────────────────
                    violation = _check_command_rules(cmd, paths_consulted=_paths_consulted)
                    if violation:
                        print(f"   [Rule violation] {violation}")
                        tool_output = (
                            f"[RULE VIOLATION] {violation} "
                            f"Fix the command and try again."
                        )
                    else:
                        print(f"   [Terminal] > {cmd}")
                        if whitelist and not _command_looks_known(cmd, whitelist):
                            needs_lookup = True
                            unknown_cmd  = cmd
                            print(f"   [ℹ️  '{cmd.split()[0]}' not in commands.md]")
                        tool_output = execute_terminal_command(
                            cmd, working_directory=arguments.get("working_directory")
                        )

            elif func_name == "fallback_view_screen":
                b64_img = capture_screen_to_ram()
                if not b64_img.startswith("Error"):
                    tool_output = (
                        f"Screenshot captured at canvas size {MODEL_CANVAS_W}x{MODEL_CANVAS_H}. "
                        f"Yellow grid labels are canvas coordinates. "
                        f"Pass them directly to fallback_click_grid — Python scales by "
                        f"({SCALE_X:.2f}x, {SCALE_Y:.2f}x) to reach real screen pixels. "
                        f"For text elements, prefer fallback_click_text for precision."
                    )
                    tool_images = [b64_img]
                else:
                    tool_output = b64_img

            elif func_name == "fallback_find_text":
                tool_output = fallback_find_text(arguments.get("text", ""))

            elif func_name == "fallback_click_grid":
                x          = arguments.get("x", 0)
                y          = arguments.get("y", 0)
                click_type = arguments.get("click_type", "left_click")
                print(f"   [Click] {click_type} at canvas ({x},{y})")
                tool_output = fallback_click_grid(x, y, click_type)

            elif func_name == "fallback_click_text":
                text       = arguments.get("text", "")
                click_type = arguments.get("click_type", "left_click")
                print(f"   [OCR Click] '{text}'")
                tool_output = fallback_click_text(text, click_type)

            elif func_name == "type_text":
                text            = arguments.get("text", "")
                special_key     = arguments.get("special_key", None)
                expected_window = arguments.get("expected_window", "")
                print(f"   [Type] '{text[:40]}{'...' if len(text)>40 else ''}'")
                tool_output = type_text(text, special_key, expected_window)

            elif func_name == "update_memory":
                tool_output = update_memory(
                    arguments.get("target", "session"),
                    arguments.get("content", "")
                )

            elif func_name == "set_current_goal":
                tool_output = set_current_goal(
                    arguments.get("goal", ""),
                    arguments.get("reason", "")
                )

            elif func_name == "list_skills":
                tool_output = list_skills()

            elif func_name == "load_skill":
                tool_output = load_skill(arguments.get("skill_name", ""))

            elif func_name == "read_instructions":
                tool_output = read_instructions()

            elif func_name == "add_instruction":
                tool_output = add_instruction(arguments.get("instruction", ""))

            elif func_name in ("read_paths", "read_path"):
                tool_output = read_paths()
                _paths_consulted = True

            elif func_name == "explore_path":
                tool_output = explore_path(arguments.get("path", STARTUP_DIR))

            elif func_name == "add_path":
                tool_output = add_path(
                    arguments.get("label", ""),
                    arguments.get("path", ""),
                    arguments.get("note", "")
                )

            elif func_name == "create_domain_knowledge":
                tool_output = create_domain_knowledge(
                    arguments.get("name", ""),
                    arguments.get("description", ""),
                    arguments.get("initial_content", "")
                )

            elif func_name == "list_domain_knowledge":
                tool_output = list_domain_knowledge()

            elif func_name == "read_domain_knowledge":
                tool_output = read_domain_knowledge(arguments.get("name", ""))

            elif func_name == "create_domain_skill":
                tool_output = create_domain_skill(
                    arguments.get("name", ""),
                    arguments.get("domain", ""),
                    arguments.get("description", ""),
                    arguments.get("content", "")
                )

            elif func_name == "list_domain_skills":
                tool_output = list_domain_skills()

            elif func_name == "consult_gemini":
                tool_output = consult_gemini(
                    arguments.get("prompt", ""),
                    arguments.get("task_type", "auto"),
                    arguments.get("context", "")
                )
            elif func_name == "consult_openrouter":
                tool_output = consult_openrouter(
                    arguments.get("prompt", ""),
                    arguments.get("context", ""),
                    arguments.get("model") or None
                )
            elif func_name == "delegate_to_openrouter":
                tool_output = delegate_to_openrouter(
                    arguments.get("task", ""),
                    arguments.get("context", ""),
                    arguments.get("model") or None,
                    int(arguments.get("max_steps", 10))
                )
            elif func_name == "list_openrouter_models":
                tool_output = list_openrouter_models(
                    bool(arguments.get("free_only", True))
                )
            elif func_name == "set_openrouter_model_by_index":
                tool_output = set_openrouter_model_by_index(int(arguments.get("index", 0)))
            elif func_name == "set_openrouter_model":
                tool_output = set_openrouter_model(arguments.get("model_id", ""))
            elif func_name == "consult_gemini_api":
                tool_output = consult_gemini_api(
                    arguments.get("prompt", ""),
                    arguments.get("context", ""),
                    arguments.get("model") or None
                )
            elif func_name == "delegate_to_gemini_api":
                tool_output = delegate_to_gemini_api(
                    arguments.get("task", ""),
                    arguments.get("context", ""),
                    arguments.get("model") or None,
                    int(arguments.get("max_steps", 10))
                )
            elif func_name == "set_gemini_api_model":
                tool_output = set_gemini_api_model(arguments.get("model_id", ""))
            elif func_name == "consult_groq":
                tool_output = consult_groq(
                    arguments.get("prompt", ""),
                    arguments.get("context", ""),
                    arguments.get("model") or None
                )
            elif func_name == "delegate_to_groq":
                tool_output = delegate_to_groq(
                    arguments.get("task", ""),
                    arguments.get("context", ""),
                    arguments.get("model") or None,
                    int(arguments.get("max_steps", 10))
                )
            elif func_name == "list_groq_models":
                tool_output = list_groq_models()
            elif func_name == "set_groq_model_by_index":
                tool_output = set_groq_model_by_index(int(arguments.get("index", 0)))
            elif func_name == "set_groq_model":
                tool_output = set_groq_model(arguments.get("model_id", ""))
            elif func_name == "consult_ollama_cloud":
                tool_output = consult_ollama_cloud(
                    arguments.get("prompt", ""),
                    arguments.get("context", ""),
                    arguments.get("model") or None
                )
            elif func_name == "delegate_to_ollama_cloud":
                tool_output = delegate_to_ollama_cloud(
                    arguments.get("task", ""),
                    arguments.get("context", ""),
                    arguments.get("model") or None,
                    int(arguments.get("max_steps", 10))
                )
            elif func_name == "list_ollama_cloud_models":
                tool_output = list_ollama_cloud_models()
            elif func_name == "set_ollama_cloud_model":
                tool_output = set_ollama_cloud_model(arguments.get("model_id", ""))
            elif func_name == "delegate_to_gemini_web":
                tool_output = delegate_to_gemini_web(
                    arguments.get("task", ""),
                    arguments.get("context", ""),
                    int(arguments.get("max_steps", 10))
                )
            elif func_name == "set_gemini_web_model":
                tool_output = set_gemini_web_model(arguments.get("model_name", ""))
            elif func_name == "read_file_smart":
                tool_output = read_file_smart(arguments.get("path", ""))
            elif func_name == "read_file_chunk":
                tool_output = read_file_chunk(arguments.get("path",""), int(arguments.get("chunk_index",1)))
            elif func_name == "write_docx_file":
                tool_output = write_docx_file(arguments.get("path",""), arguments.get("content",""))
            elif func_name == "create_flowchart":
                tool_output = create_flowchart(
                    arguments.get("title", "Flowchart"),
                    arguments.get("steps", []),
                    arguments.get("edges")
                )
            elif func_name == "generate_image":
                tool_output = generate_image(
                    arguments.get("prompt", ""),
                    int(arguments.get("count", 1) or 1)
                )
            elif func_name == "write_response_memory":
                tool_output = write_response_memory(arguments.get("content",""))
            elif func_name == "append_response_memory":
                tool_output = append_response_memory(arguments.get("content",""))
            elif func_name == "read_response_memory":
                tool_output = read_response_memory()
            elif func_name == "manual_scan_app_layouts":
                tool_output = manual_scan_app_layouts(arguments.get("window_title", ""))

            elif func_name == "manual_inspect_app_subtree":
                tool_output = manual_inspect_app_subtree(
                    arguments.get("window_title", ""),
                    arguments.get("subtree_key", "")
                )

            elif func_name == "click_ui_element":
                window_title = arguments.get("window_title", "")
                description  = arguments.get("description", "")
                action       = arguments.get("action", "click")
                text_to_type = arguments.get("text_to_type", "")
                print(f"   [UIA] click_ui_element: '{description}' in '{window_title}' (action={action})")
                tool_output = click_ui_element(window_title, description, action, text_to_type)
                if tool_output.startswith("Success"):
                    print(f"   [UIA] ✅ {tool_output}")
                else:
                    print(f"   [UIA] ⚠️  {tool_output[:120]}")

            elif func_name == "manual_interact_with_ui":
                print(f"   [UIA] {arguments.get('action')} on {arguments.get('property_value')}")
                tool_output = manual_interact_with_ui(
                    arguments.get("window_title", ""),
                    arguments.get("control_type", ""),
                    arguments.get("search_property", ""),
                    arguments.get("property_value", ""),
                    arguments.get("action", ""),
                    arguments.get("text_to_type", "")
                )

            elif func_name == "list_active_windows":
                tool_output = list_active_windows()

            elif func_name == "read_aggregated_text":
                print(f"   [UIA] Aggregating text blocks from: '{arguments.get('window_title')}'")
                if _UIA_AVAILABLE:
                    tool_output = ui_navigator.read_aggregated_text(
                        window_title=arguments.get("window_title", ""),
                        container_key=arguments.get("container_key", None)
                    )
                else:
                    tool_output = "UIA library not available."
            
            elif func_name == "query_gemini_app":
                prompt_payload = arguments.get("prompt", "")
                print(f"   [Bridge] Sending prompt to Gemini web app via gemini_webapi...")
                if _GEMINI_WEBAPI_AVAILABLE:
                    tool_output = query_gemini_app(prompt_payload)
                else:
                    tool_output = (
                        f"Execution failed: {_gemini_webapi_load_msg}"
                    )
            
            elif func_name == "manage_gemini_chat":
                tool_output = ui_navigator.manage_gemini_chat(
                    action=arguments.get("action"),
                    chat_name=arguments.get("chat_name")
                )
            elif func_name == "wait":
                seconds = float(arguments.get("seconds", 1))
                print(f"   [Wait] {seconds}s")
                tool_output = wait(seconds)

            elif func_name == "say":
                # NOTE: msg_text is intentionally NOT appended to
                # _accumulated_reply. _print_reply() already streams it to
                # the user immediately (as a live GUI bubble via the
                # _gui_say_intercept hook, or as a console line in CLI
                # mode). Adding it to _accumulated_reply as well caused it
                # to be shown a SECOND time at the end of the turn, each
                # copy carrying its own 'Midum:' label/header — i.e. every
                # say() call rendered twice.
                msg_text = arguments.get("message", "")
                _print_reply("Midum:", msg_text)
                _said_parts.append(msg_text)
                turn_tool_outputs.append(f"[say]: {msg_text}")
                tool_output = "Message displayed to user."

            elif func_name == "list_native_tools":
                tool_output = list_native_tools()

            elif func_name == "show_native_tool_schema":
                tool_output = show_native_tool_schema(arguments.get("tool_name"))

            elif func_name == "list_mcp_servers":
                tool_output = list_mcp_servers()

            elif func_name == "show_server_tools":
                tool_output = show_server_tools(arguments.get("server"))

            elif func_name == "call_mcp_tool":
                tool_output = call_mcp_tool(
                    arguments.get("server"),
                    arguments.get("tool_name"),
                    arguments.get("arguments", {})
                )

            elif func_name == "connect_mcp_server":
                tool_output = connect_mcp_server(
                    name=arguments.get("name", ""),
                    transport=arguments.get("transport", "stdio"),
                    command=arguments.get("command"),
                    args=arguments.get("args"),
                    url=arguments.get("url"),
                    env=arguments.get("env"),
                    headers=arguments.get("headers"),
                    persist=arguments.get("persist", True)
                )

            elif func_name == "disconnect_mcp_server":
                tool_output = disconnect_mcp_server(
                    arguments.get("server"),
                    forget=arguments.get("forget", False)
                )

            elif func_name == "ask_user_text":
                print(f"   [GUI] Asking user for text input...")
                tool_output = ask_user_text(
                    arguments.get("prompt", ""),
                    arguments.get("title", "Midum needs input")
                )

            elif func_name == "ask_user_file_path":
                print(f"   [GUI] Asking user to pick a file path...")
                tool_output = ask_user_file_path(
                    arguments.get("prompt", "Select a file"),
                    must_exist=arguments.get("must_exist", True)
                )

            elif func_name == "ask_user_approval":
                print(f"   [GUI] Asking user for approval...")
                tool_output = ask_user_approval(
                    arguments.get("message", ""),
                    arguments.get("details", "")
                )

            elif func_name == "ask_user_choice":
                print(f"   [GUI] Asking user to choose...")
                tool_output = ask_user_choice(
                    arguments.get("question", ""),
                    arguments.get("choice_1", ""),
                    arguments.get("choice_2", ""),
                    arguments.get("choice_3", ""),
                    arguments.get("choice_4", ""),
                    allow_custom=arguments.get("allow_custom", True)
                )

            else:
                # ── Fuzzy tool name resolver ──────────────────────────────────
                # The model sometimes drops or adds an 's', swaps underscores for
                # spaces, or abbreviates tool names. Rather than returning a hard
                # "Unknown tool" failure that breaks the turn, we find the closest
                # real tool name and tell the model to retry with the correct one.
                _KNOWN_TOOLS = {t["function"]["name"] for t in tools}

                # Build common aliases explicitly for the most-misspelled tools
                _TOOL_ALIASES: dict[str, str] = {
                    # Missing/extra 's'
                    "read_path":            "read_paths",
                    "read_instruction":     "read_instructions",
                    "list_skill":           "list_skills",
                    "load_skills":          "load_skill",
                    "list_domain_skill":    "list_domain_skills",
                    "list_domain_knowledges": "list_domain_knowledge",
                    "add_paths":            "add_path",
                    "add_instructions":     "add_instruction",
                    # Spaces instead of underscores
                    "read paths":           "read_paths",
                    "list skills":          "list_skills",
                    "load skill":           "load_skill",
                    "search internet":      "search_internet",
                    "execute terminal":     "execute_terminal_command",
                    "execute command":      "execute_terminal_command",
                    "run command":          "execute_terminal_command",
                    "terminal command":     "execute_terminal_command",
                    "view screen":          "fallback_view_screen",
                    "click text":           "fallback_click_text",
                    "find text":            "fallback_find_text",
                    "click grid":           "fallback_click_grid",
                    "update memory":        "update_memory",
                    "set goal":             "set_current_goal",
                    "write file":           "write_local_file",
                    "read file":            "read_local_file",
                    "append file":          "append_local_file",
                    # Memory
                    "read_mem":             "read_response_memory",
                    "write_mem":            "write_response_memory",
                    "append_mem":           "append_response_memory",
                    # Gemini
                    "gemini":               "consult_gemini",
                    "ask_gemini":           "consult_gemini",
                    # OpenRouter
                    "openrouter":           "consult_openrouter",
                    "ask_openrouter":       "consult_openrouter",
                    "consult_or":           "consult_openrouter",
                    "delegate":             "delegate_to_openrouter",
                    "delegate_task":        "delegate_to_openrouter",
                    "assign_task":          "delegate_to_openrouter",
                    "offload_task":         "delegate_to_openrouter",
                    "offload_to_openrouter": "delegate_to_openrouter",
                    "delegate_or":          "delegate_to_openrouter",
                    "list_models":          "list_openrouter_models",
                    "list_or_models":       "list_openrouter_models",
                    "switch_model":         "set_openrouter_model_by_index",
                    "change_model":         "set_openrouter_model_by_index",
                    "select_model":         "set_openrouter_model_by_index",
                    "set_model":            "set_openrouter_model",
                    # GroqCloud
                    "groq":                 "consult_groq",
                    "ask_groq":             "consult_groq",
                    "consult_gq":           "consult_groq",
                    "delegate_groq":        "delegate_to_groq",
                    "offload_to_groq":      "delegate_to_groq",
                    "list_groq_models":     "list_groq_models",
                    "switch_groq_model":    "set_groq_model_by_index",
                    "set_groq_model":       "set_groq_model",
                    # Gemini-web (primary-execution delegate, distinct from consult_gemini)
                    "delegate_gemini":      "delegate_to_gemini_web",
                    "delegate_to_gemini":   "delegate_to_gemini_web",
                    "offload_to_gemini":    "delegate_to_gemini_web",
                    "delegate_gw":          "delegate_to_gemini_web",
                    "set_gemini_model":     "set_gemini_web_model",
                    # UI snapshot/act — all old names → unified tools
                    "snapshot_ui":              "snapshot",
                    "snapshot_browser_elements": "snapshot",
                    "list_elements":            "snapshot",
                    "list_ui":                  "snapshot",
                    "ui_snapshot":              "snapshot",
                    "scan_ui":                  "snapshot",
                    "browser_snapshot":         "snapshot",
                    "snapshot_page":            "snapshot",
                    "act_on_element":           "act",
                    "act_on_browser_element":   "act",
                    "click_index":              "act",
                    "act_by_index":             "act",
                    "act_element":              "act",
                    "click_browser":            "act",
                    "browser_click":            "act",
                    # UI direct
                    "click_element":            "click_ui_element",
                    "click_ui":                 "click_ui_element",
                    "ui_click":                 "click_ui_element",
                    # Web / browser
                    "navigate":             "open_url",
                    "navigate_to":          "open_url",
                    "go_to_url":            "open_url",
                    "open_link":            "open_url",
                    "browse":               "open_url",
                    "open_browser":         "open_url",
                    "open_result":          "open_search_result",
                    "read_page":            "read_browser_page",
                    "read_browser":         "read_browser_page",
                    "read_browser_tab":     "read_browser_page",
                    "get_page_content":     "read_browser_page",
                    "browser_tabs":         "list_browser_tabs",
                    "list_tabs":            "list_browser_tabs",
                    "run_js":               "run_js_in_browser",
                    "execute_js":           "run_js_in_browser",
                    "js":                   "run_js_in_browser",
                    # Directory / file
                    "list_dir":             "list_directory",
                    "ls":                   "list_directory",
                    "explore_path":         "list_directory",
                    "open_file":            "open_path",
                    "search_file":          "find_file",
                    "search_files":         "find_file",
                    "locate_file":          "find_file",
                    "open_file_index":      "open_path_by_index",
                    # Skills / knowledge
                    "list_skills":          "list_skills_indexed",
                    "list_skill":           "list_skills_indexed",
                    "load_skill_index":     "load_skill_by_index",
                    "list_domain_knowledge": "list_domain_knowledge_indexed",
                    "list_domain_skills":   "list_domain_skills_indexed",
                    "read_domain_index":    "read_domain_by_index",
                    # Paths
                    "read_paths":           "list_paths_indexed",
                    "read_path":            "list_paths_indexed",
                    "get_path_index":       "get_path",
                    # OCR
                    "ocr_screen":           "ocr_snapshot",
                    "screen_snapshot":      "ocr_snapshot",
                    "click_ocr":            "click_ocr_index",
                    # Misc
                    "type":                 "type_text",
                    "screenshot":           "fallback_view_screen",
                }

                normalised = func_name.lower().replace("-", "_")
                resolved   = _TOOL_ALIASES.get(normalised)

                if not resolved:
                    # Fuzzy: find the real tool name with the most character overlap
                    from difflib import get_close_matches
                    close = get_close_matches(normalised, _KNOWN_TOOLS, n=1, cutoff=0.6)
                    resolved = close[0] if close else None

                if resolved and resolved in _KNOWN_TOOLS:
                    if resolved == func_name or resolved == normalised:
                        # Resolved to itself — tool exists but has no dispatch case
                        tool_output = (
                            f"[INTERNAL ERROR] Tool '{func_name}' is registered but has no "
                            f"dispatch handler. This is a Midum bug — report it."
                        )
                    else:
                        print(f"   [Tool resolver] '{func_name}' → '{resolved}'")
                        tool_output = (
                            f"[TOOL NAME ERROR] You called '{func_name}' which does not exist. "
                            f"The correct tool name is '{resolved}'. "
                            f"Call '{resolved}' now with the same arguments."
                        )
                else:
                    mcp_matches = _mcp_find_tool_matches(func_name)
                    if len(mcp_matches) > 1:
                        options = "; ".join(
                            f"server='{s}' tool_name='{t}'" for s, t in mcp_matches
                        )
                        tool_output = (
                            f"[AMBIGUOUS MCP TOOL] '{func_name}' matches a tool on more than "
                            f"one connected server: {options}. Call call_mcp_tool with the "
                            f"specific server you meant."
                        )
                    else:
                        known_list = ", ".join(sorted(_KNOWN_TOOLS))
                        tool_output = (
                            f"[UNKNOWN TOOL] '{func_name}' is not a valid tool. "
                            f"Available tools: {known_list}. If this was meant to be an MCP "
                            f"server tool, call list_mcp_servers() then show_server_tools(server) "
                            f"to confirm the exact name, then call_mcp_tool(server, tool_name, arguments)."
                        )

            # ── Record in TurnState ───────────────────────────────────────────
            state.record(func_name, arguments, tool_output)

            # ──────────────────────────────────────────────────────────────────

            tool_output = tool_output.replace("<", "&lt;").replace(">", "&gt;")
            turn_tool_outputs.append(tool_output)

            msg = {"role": "tool", "content": tool_output}
            if tool_images:
                msg["images"] = tool_images
            conversation_history.append(msg)

        if needs_lookup:
            conversation_history.append({
                "role": "user",
                "content": (
                    f"[SYSTEM NOTE]: '{unknown_cmd.split()[0]}' was not in commands.md. "
                    "Check commands.md or search online if you are unsure it was correct."
                )
            })

        # ── Step prompt: state-aware next-step message ────────────────────────
        conversation_history.append({
            "role": "user",
            "content": state.build_step_prompt()
        })

    # ── Reached via break (MAX_STEPS or cap_hit) ─────────────────────────────
    full_reply = "\n\n".join(p for p in _accumulated_reply if p.strip())
    return full_reply or "[Task stopped — see above for details.]", turn_tool_outputs


# =============================================================================

