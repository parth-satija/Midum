"""
tool_discovery.py -- per-model "Tool discovery" mode: settings + session state.

WHAT IT DOES
------------
With Tool discovery OFF (the default for most models) the active model is sent
the full native tool schema on every call (~170 tools, tens of thousands of
tokens). With it ON, that model instead gets:

  1. a small curated set of DEFAULT tools (execute_terminal_command,
     execute_python_code, ... -- curated by the user from the Permissions tab),
  2. every PROMOTED MCP tool and PROMOTED Flow (these are appended by
     permissions.filter_tools_schema() exactly as before, so they are in
     context with no discovery hop),
  3. any tool the model has loaded this chat via the existing discovery pair
     list_more_tools() / load_tool_by_index(N)  (originally Groq-only; see
     tools_registry.py, which owns the schema building), and
  4. those two discovery meta-tools themselves.

This module only owns the *state*: the persistent toggle (keyed per
provider+model), the curated default-tool list, and the per-chat set of tools
loaded on demand. It deliberately imports nothing from tools_registry /
orchestration / permissions at module level so every one of them (and the GUI
Api) can import it without circular-import trouble.

STORAGE
-------
storage/tool_discovery.json:
    {
      "models":        {"gemini_api:gemini-3.5-flash-lite": true, ...},
      "default_tools": ["execute_terminal_command", ...]
    }
Only models the user has explicitly toggled are written. A model with no entry
falls back to its provider's default (ON for Groq, whose free tier has a tight
tokens-per-minute budget and was always lazy-loaded before this toggle existed;
OFF for everything else). A missing "default_tools" key means "use the built-in
recommended set".
"""

import json
import os
import threading

from config import STORAGE_DIR

TOOL_DISCOVERY_FILE = os.path.join(STORAGE_DIR, "tool_discovery.json")

# The two discovery meta-tools. Always present while discovery is on; they are
# not user-curatable and never appear in the default-tool list.
META_TOOL_NAMES = ("list_more_tools", "load_tool_by_index")

# Recommended starting set -- "a few default tools" the user can then curate
# from the Permissions tab. Names that don't exist on this build / platform are
# silently skipped when the schema is built.
DEFAULT_TOOL_NAMES = [
    "execute_terminal_command",
    "execute_wsl_command",
    "execute_python_code",
    "read_local_file",
    "list_directory",
    "search_internet",
    "read_search_result",
    "write_steps",
    "write_response_memory",
    "show_chat_surface",
    "edit_chat_surface",
    "complete_step",
]

# Providers whose models are ON by default until the user decides otherwise.
_DEFAULT_ON_PROVIDERS = {"groq", "codex"}

# Providers that never receive a tools= schema: they drive Midum through a JSON
# text convention and ALREADY discover everything on demand
# (list_native_tools / show_native_tool_schema), so the toggle has nothing to
# change for them. The GUI shows it disabled with an explanation.
TEXT_PROTOCOL_PROVIDERS = {"gemini_web", "openai_web"}

_lock = threading.RLock()
_cache = None   # parsed JSON, or None until first load


# ── model identity ────────────────────────────────────────────────────────────
def resolve_model(provider=None, model=None):
    """(provider_key, model_id) for a call. Falls back to the globally selected
    provider / that provider's configured model when not overridden -- the same
    precedence _call_primary_model() uses (override first, then config)."""
    import config
    provider = (provider or getattr(config, "MODEL_PROVIDER", "") or "ollama").strip().lower()
    if not model:
        attr = {
            "ollama":       "MODEL_NAME",
            "openrouter":   "OPENROUTER_MODEL",
            "gemini_web":   "GEMINI_WEB_MODEL",
            "gemini_api":   "GEMINI_API_MODEL",
            "groq":         "GROQ_MODEL",
            "ollama_cloud": "OLLAMA_CLOUD_MODEL",
            "codex":        "CODEX_MODEL",
            "litellm":      "LITELLM_MODEL",
        }.get(provider)
        model = getattr(config, attr, "") if attr else ""
    return provider, (model or "")


def model_key(provider, model) -> str:
    """Stable storage key, e.g. 'groq:llama-3.3-70b-versatile'. Blank / '(auto)'
    models (Gemini-web auto-pick, ChatGPT-web, which has no model setting) all
    collapse to '(auto)' so the GUI and the runtime always agree on the key."""
    provider = (provider or "").strip().lower()
    model = (model or "").strip()
    if provider == "openai_web" or not model or model == "(auto)":
        model = "(auto)"
    return f"{provider}:{model}"


# ── persistence ───────────────────────────────────────────────────────────────
def _load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        data = {}
        try:
            if os.path.exists(TOOL_DISCOVERY_FILE):
                with open(TOOL_DISCOVERY_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
        except Exception as e:
            print(f"⚠️ [ToolDiscovery] Could not read {TOOL_DISCOVERY_FILE}: {e}")
        if not isinstance(data.get("models"), dict):
            data["models"] = {}
        if "default_tools" in data and not isinstance(data["default_tools"], list):
            data.pop("default_tools")
        _cache = data
        return _cache


def _save(data: dict) -> None:
    global _cache
    with _lock:
        _cache = data
        tmp = TOOL_DISCOVERY_FILE + ".tmp"
        try:
            os.makedirs(os.path.dirname(TOOL_DISCOVERY_FILE), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, TOOL_DISCOVERY_FILE)   # atomic: never leaves a half-written file
        except Exception as e:
            print(f"⚠️ [ToolDiscovery] Could not save {TOOL_DISCOVERY_FILE}: {e}")


# ── per-model toggle ──────────────────────────────────────────────────────────
def has_explicit_setting(provider=None, model=None) -> bool:
    provider, model = resolve_model(provider, model)
    return model_key(provider, model) in _load()["models"]


def is_enabled(provider=None, model=None) -> bool:
    """Is tool discovery on for this model? Explicit user choice wins, otherwise
    the provider default applies."""
    provider, model = resolve_model(provider, model)
    saved = _load()["models"].get(model_key(provider, model))
    if isinstance(saved, bool):
        return saved
    return provider in _DEFAULT_ON_PROVIDERS


def set_enabled(provider, model, enabled: bool) -> str:
    provider, model = resolve_model(provider, model)
    key = model_key(provider, model)
    with _lock:
        data = dict(_load())
        models = dict(data.get("models", {}))
        models[key] = bool(enabled)
        data["models"] = models
        _save(data)
    return f"Tool discovery {'enabled' if enabled else 'disabled'} for '{key}'."


# ── curated default tools ─────────────────────────────────────────────────────
def get_default_tools() -> list:
    """The curated default-tool names (user's list, or the recommended seed)."""
    saved = _load().get("default_tools")
    names = saved if isinstance(saved, list) else DEFAULT_TOOL_NAMES
    out, seen = [], set()
    for n in names:
        if isinstance(n, str) and n and n not in seen and n not in META_TOOL_NAMES:
            seen.add(n)
            out.append(n)
    return out


def set_default_tools(names) -> list:
    clean, seen = [], set()
    for n in (names or []):
        n = (n or "").strip() if isinstance(n, str) else ""
        if n and n not in seen and n not in META_TOOL_NAMES:
            seen.add(n)
            clean.append(n)
    with _lock:
        data = dict(_load())
        data["default_tools"] = clean
        _save(data)
    return clean


def set_default_tool(name: str, on: bool) -> list:
    """Add (on=True) or remove (on=False) one tool from the default set."""
    name = (name or "").strip()
    current = get_default_tools()
    if not name or name in META_TOOL_NAMES:
        return current
    if on and name not in current:
        current.append(name)
    elif not on and name in current:
        current.remove(name)
    else:
        return current
    return set_default_tools(current)


def reset_default_tools() -> list:
    """Back to the recommended built-in set."""
    with _lock:
        data = dict(_load())
        data.pop("default_tools", None)
        _save(data)
    return get_default_tools()


# ── per-chat "loaded on demand" state ─────────────────────────────────────────
# Loaded tools persist for the rest of that chat (so the model never reloads the
# same tool), and are scoped per chat because several conversation tabs can run
# turns concurrently. Threads with no bound turn (CLI, sub-agent workers) share
# one "_global" bucket, the old single-session behaviour.
_loaded = {}        # chat_key -> [tool name, ...] in load order
_index_cache = {}   # chat_key -> [tool name, ...] backing the last list_more_tools()


def _chat_key() -> str:
    try:
        from state import current_ctx
        ctx = current_ctx()
        if ctx is not None and getattr(ctx, "chat_id", None):
            return str(ctx.chat_id)
    except Exception:
        pass
    return "_global"


def session_loaded_names() -> list:
    with _lock:
        return list(_loaded.get(_chat_key(), []))


def session_load(name: str) -> bool:
    """Mark a tool loaded for this chat. False if it already was."""
    with _lock:
        names = _loaded.setdefault(_chat_key(), [])
        if name in names:
            return False
        names.append(name)
        return True


def session_set_index(names: list) -> None:
    with _lock:
        _index_cache[_chat_key()] = list(names)


def session_get_index() -> list:
    with _lock:
        return list(_index_cache.get(_chat_key(), []))


def reset_session(all_chats: bool = False) -> None:
    """Forget on-demand-loaded tools (this chat, or every chat)."""
    with _lock:
        if all_chats:
            _loaded.clear()
            _index_cache.clear()
        else:
            key = _chat_key()
            _loaded.pop(key, None)
            _index_cache.pop(key, None)
