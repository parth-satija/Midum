# =============================================================================
# LITELLM PROVIDER  --  one backend, (almost) every LLM provider
# =============================================================================
# LiteLLM (https://docs.litellm.ai) is a thin translation layer that exposes
# 100+ providers (OpenAI, Anthropic, Gemini, Mistral, xAI, DeepSeek, Together,
# Perplexity, Cohere, Fireworks, Cerebras, NVIDIA NIM, Azure, Bedrock, Vertex,
# Ollama, LM Studio, vLLM, any OpenAI-compatible server, ...) behind ONE
# OpenAI-shaped `completion()` call, including native tool/function calling.
#
# Model IDs are always written  "<litellm-provider>/<model>", e.g.
#     openai/gpt-4o-mini          anthropic/claude-sonnet-4-5
#     gemini/gemini-2.5-flash     groq/llama-3.3-70b-versatile
#     openrouter/qwen/qwen3-coder ollama/llama3.1
#
# INSTALLATION:
#     pip install litellm
#
# API KEYS (two ways, either works):
#   1. GUI: Model tab -> Provider "LiteLLM (any provider)" -> paste the key
#      under "API key". It is stored in the shared secrets file (SECRETS_FILE)
#      under "LITELLM_PROVIDERS": { "<provider>": {"api_key": "...", "api_base": "..."} }
#   2. Standard environment variables LiteLLM already understands
#      (OPENAI_API_KEY, ANTHROPIC_API_KEY, AWS_*, VERTEXAI_*, AZURE_*, ...).
#      Needed for cloud-credential providers such as Bedrock / Vertex AI.
#
# A key saved via the GUI wins over the environment variable for that provider.
# =============================================================================
import importlib.util
import json
import os
import threading
import time

import config
from config import SECRETS_FILE

_LITELLM_AVAILABLE = importlib.util.find_spec("litellm") is not None
_litellm_load_msg = "OK" if _LITELLM_AVAILABLE else "litellm is not installed: pip install litellm"

def refresh_availability() -> bool:
    """Re-check whether litellm is importable (e.g. right after the GUI's
    'Install' button ran pip) without restarting the app."""
    global _LITELLM_AVAILABLE, _litellm_load_msg
    importlib.invalidate_caches()
    _LITELLM_AVAILABLE = importlib.util.find_spec("litellm") is not None
    _litellm_load_msg = "OK" if _LITELLM_AVAILABLE else "litellm is not installed: pip install litellm"
    return _LITELLM_AVAILABLE


def install_litellm() -> dict:
    """pip-install litellm into the interpreter running Midum (GUI 'Install' button)."""
    import subprocess
    import sys
    if getattr(sys, "frozen", False):
        return {"ok": False, "error": "This is a packaged build, so pip can't add packages to it. "
                                      "Run 'pip install litellm' in the environment you build from and rebuild."}
    try:
        proc = subprocess.run([sys.executable, "-m", "pip", "install", "litellm"],
                              capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            return {"ok": False, "error": "pip failed: " + " ".join(tail)[:300]}
        return {"ok": refresh_availability(), "error": "" if refresh_availability() else "Installed, but litellm still can't be found -- restart Midum."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


_litellm = None
_litellm_lock = threading.Lock()
_secrets_lock = threading.Lock()


def _get_litellm():
    """Import litellm lazily (its import is slow, ~1-2s) and configure it once."""
    global _litellm
    if _litellm is not None:
        return _litellm
    with _litellm_lock:
        if _litellm is None:
            if not _LITELLM_AVAILABLE:
                raise RuntimeError(_litellm_load_msg)
            import litellm
            litellm.drop_params = True          # silently drop params a provider doesn't support
            litellm.suppress_debug_info = True  # no "Give Feedback / Get Help" spam in the log
            try:
                litellm.set_verbose = False
            except Exception:
                pass
            _litellm = litellm
    return _litellm


# ── Friendly catalog of the providers people actually use ─────────────────────
# id = LiteLLM's provider prefix. Everything else LiteLLM supports is still
# listed (alphabetically, after these) via litellm.provider_list.
POPULAR_PROVIDERS = [
    # id,           label,                          env var LiteLLM reads,   default base URL hint,        note
    ("openai",       "OpenAI",                       "OPENAI_API_KEY",        "",                          "Also works for ANY OpenAI-compatible server: set a Base URL."),
    ("anthropic",    "Anthropic (Claude)",           "ANTHROPIC_API_KEY",     "",                          ""),
    ("gemini",       "Google Gemini (AI Studio)",    "GEMINI_API_KEY",        "",                          ""),
    ("groq",         "Groq",                         "GROQ_API_KEY",          "",                          ""),
    ("openrouter",   "OpenRouter",                   "OPENROUTER_API_KEY",    "",                          ""),
    ("mistral",      "Mistral AI",                   "MISTRAL_API_KEY",       "",                          ""),
    ("deepseek",     "DeepSeek",                     "DEEPSEEK_API_KEY",      "",                          ""),
    ("xai",          "xAI (Grok)",                   "XAI_API_KEY",           "",                          ""),
    ("together_ai",  "Together AI",                  "TOGETHERAI_API_KEY",    "",                          ""),
    ("fireworks_ai", "Fireworks AI",                 "FIREWORKS_AI_API_KEY",  "",                          ""),
    ("cerebras",     "Cerebras",                     "CEREBRAS_API_KEY",      "",                          ""),
    ("perplexity",   "Perplexity",                   "PERPLEXITYAI_API_KEY",  "",                          ""),
    ("cohere",       "Cohere",                       "COHERE_API_KEY",        "",                          ""),
    ("nvidia_nim",   "NVIDIA NIM",                   "NVIDIA_NIM_API_KEY",    "",                          ""),
    ("huggingface",  "Hugging Face",                 "HF_TOKEN",              "",                          ""),
    ("azure",        "Azure OpenAI",                 "AZURE_API_KEY",         "https://<resource>.openai.azure.com", "Model = your deployment name. Base URL required."),
    ("bedrock",      "AWS Bedrock",                  "AWS_ACCESS_KEY_ID",     "",                          "Uses AWS_* environment credentials, no key field needed."),
    ("vertex_ai",    "Google Vertex AI",             "GOOGLE_APPLICATION_CREDENTIALS", "",                 "Uses Google Cloud credentials from the environment."),
    ("ollama",       "Ollama (via LiteLLM)",         "",                      "http://localhost:11434",    "No key needed."),
    ("lm_studio",    "LM Studio",                    "",                      "http://localhost:1234/v1",  "No key needed."),
    ("hosted_vllm",  "vLLM server",                  "",                      "http://localhost:8000/v1",  "No key needed unless your server sets one."),
]
_POPULAR_BY_ID = {p[0]: p for p in POPULAR_PROVIDERS}

# A few sensible starting suggestions for the model box when LiteLLM's own
# catalog has nothing for a provider (custom endpoints, deployments, etc).
_MODEL_EXAMPLES = {
    "openai":      ["gpt-4o-mini", "gpt-4o", "gpt-4.1", "o4-mini"],
    "anthropic":   ["claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-1"],
    "gemini":      ["gemini-2.5-flash", "gemini-2.5-pro"],
    "groq":        ["llama-3.3-70b-versatile", "qwen/qwen3-32b"],
    "openrouter":  ["openai/gpt-4o-mini", "meta-llama/llama-3.3-70b-instruct:free", "anthropic/claude-sonnet-4.5"],
    "mistral":     ["mistral-large-latest", "mistral-small-latest", "codestral-latest"],
    "deepseek":    ["deepseek-chat", "deepseek-reasoner"],
    "xai":         ["grok-4", "grok-3-mini"],
    "together_ai": ["meta-llama/Llama-3.3-70B-Instruct-Turbo"],
    "fireworks_ai": ["accounts/fireworks/models/llama-v3p3-70b-instruct"],
    "cerebras":    ["llama-3.3-70b"],
    "perplexity":  ["sonar-pro", "sonar"],
    "cohere":      ["command-r-plus", "command-r"],
    "ollama":      ["llama3.1", "qwen2.5-coder"],
    "azure":       ["<your-deployment-name>"],
    "lm_studio":   ["<loaded-model-name>"],
    "hosted_vllm": ["<served-model-name>"],
}


# ── Credential storage (shared secrets file) ──────────────────────────────────
def _read_secrets() -> dict:
    path = os.path.abspath(SECRETS_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_provider_creds() -> dict:
    """{provider: {"api_key": str, "api_base": str}} -- read fresh every call so
    a key saved from the GUI applies on the very next request, no restart."""
    block = _read_secrets().get("LITELLM_PROVIDERS")
    return block if isinstance(block, dict) else {}


def save_provider_credentials(provider: str, api_key: str = None, api_base: str = None) -> dict:
    """Persist a provider's key and/or base URL into SECRETS_FILE.
    Pass None to leave a field untouched, "" to clear it."""
    provider = (provider or "").strip()
    if not provider:
        return {"ok": False, "error": "Choose a provider first."}
    try:
        with _secrets_lock:
            path = os.path.abspath(SECRETS_FILE)
            # Never overwrite a secrets file we couldn't parse -- treating it as
            # empty would silently wipe every other provider's keys.
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                    if not isinstance(existing, dict):
                        raise ValueError("top level is not a JSON object")
                except Exception as e:
                    return {"ok": False, "error": f"Secrets file is not valid JSON, so I left it untouched ({e}). Fix it and try again: {path}"}
            secrets = _read_secrets()
            block = secrets.get("LITELLM_PROVIDERS")
            if not isinstance(block, dict):
                block = {}
            entry = dict(block.get(provider) or {})
            if api_key is not None:
                entry["api_key"] = api_key.strip()
            if api_base is not None:
                entry["api_base"] = api_base.strip()
            if entry.get("api_key") or entry.get("api_base"):
                block[provider] = entry
            else:
                block.pop(provider, None)
            secrets["LITELLM_PROVIDERS"] = block
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Atomic write: a crash mid-save can't leave a truncated secrets file.
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(secrets, f, indent=2)
            os.replace(tmp, path)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _mask(key: str) -> str:
    key = key or ""
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:3]}…{key[-4:]}"


def provider_status(provider: str) -> dict:
    """What the GUI needs to show next to the API-key field. Never returns the full key."""
    provider = (provider or "").strip()
    saved = _load_provider_creds().get(provider) or {}
    info = _POPULAR_BY_ID.get(provider)
    env_var = info[2] if info else ""
    return {
        "provider":     provider,
        "has_key":      bool(saved.get("api_key")),
        "key_hint":     _mask(saved.get("api_key", "")),
        "api_base":     saved.get("api_base", ""),
        "env_var":      env_var,
        "env_set":      bool(env_var and os.environ.get(env_var)),
        "base_hint":    info[3] if info else "",
        "note":         info[4] if info else "",
        "suggested_model": next((m for m in _MODEL_EXAMPLES.get(provider, []) if not m.startswith("<")), ""),
        "needs_key":    not (info and provider in ("ollama", "lm_studio", "hosted_vllm", "bedrock", "vertex_ai")),
    }


# ── Provider / model catalog for the GUI ──────────────────────────────────────
def list_litellm_providers() -> list:
    """[{id, label, popular}] -- popular ones first, then every other provider
    LiteLLM knows about."""
    out = [{"id": p[0], "label": p[1], "popular": True} for p in POPULAR_PROVIDERS]
    seen = {p[0] for p in POPULAR_PROVIDERS}
    if _LITELLM_AVAILABLE:
        try:
            ll = _get_litellm()
            others = []
            for p in getattr(ll, "provider_list", []) or []:
                pid = str(getattr(p, "value", p))
                if pid and pid not in seen and not pid.startswith("_"):
                    seen.add(pid)
                    others.append(pid)
            out += [{"id": pid, "label": pid, "popular": False} for pid in sorted(others)]
        except Exception:
            pass
    return out


def _strip_provider_prefix(provider: str, model: str) -> str:
    prefix = provider + "/"
    return model[len(prefix):] if model.startswith(prefix) else model


def list_provider_models(provider: str) -> list:
    """Model names (WITHOUT the 'provider/' prefix) LiteLLM knows for `provider`."""
    provider = (provider or "").strip()
    names = []
    if _LITELLM_AVAILABLE and provider:
        try:
            ll = _get_litellm()
            raw = (getattr(ll, "models_by_provider", {}) or {}).get(provider) or []
            cost = getattr(ll, "model_cost", {}) or {}
            keep = []
            for m in raw:
                m = str(m)
                info = cost.get(m) or cost.get(f"{provider}/{m}") or {}
                mode = info.get("mode")
                # Only chat-capable models make sense as Midum's brain: drop
                # image generators, TTS/STT, embeddings, rerankers, moderation.
                if mode and mode not in ("chat", "completion", "responses"):
                    continue
                keep.append(m)
            names = [_strip_provider_prefix(provider, m) for m in keep]
        except Exception:
            names = []
    names = sorted(set(n for n in names if n))
    examples = [m for m in _MODEL_EXAMPLES.get(provider, []) if m not in names]
    return examples + names


def fetch_live_models(provider: str) -> dict:
    """Ask the provider itself which models this key can use (needs a saved key
    or env var). Falls back to LiteLLM's static catalog when unsupported."""
    provider = (provider or "").strip()
    try:
        ll = _get_litellm()
        creds = _load_provider_creds().get(provider) or {}
        live = []
        try:
            kwargs = {"check_provider_endpoint": True, "custom_llm_provider": provider}
            if creds.get("api_key"):
                kwargs["api_key"] = creds["api_key"]
            if creds.get("api_base"):
                kwargs["api_base"] = creds["api_base"]
            try:
                live = ll.get_valid_models(**kwargs)
            except TypeError:
                # Older litellm without api_key/api_base kwargs.
                live = ll.get_valid_models(check_provider_endpoint=True, custom_llm_provider=provider)
        except Exception:
            live = []
        live = [_strip_provider_prefix(provider, str(m)) for m in (live or [])]
        if live:
            return {"ok": True, "live": True, "models": sorted(set(live))}
        return {"ok": True, "live": False, "models": list_provider_models(provider)}
    except Exception as e:
        return {"ok": False, "error": str(e), "models": list_provider_models(provider)}


# ── Chat ──────────────────────────────────────────────────────────────────────
def _split_model(model: str):
    """'openai/gpt-4o' -> ('openai', 'gpt-4o'); no slash -> ('', model)."""
    if "/" in model:
        head, rest = model.split("/", 1)
        return head, rest
    return "", model


def _credential_kwargs(model: str) -> dict:
    provider, _ = _split_model(model)
    creds = _load_provider_creds().get(provider) or {}
    kwargs = {}
    if creds.get("api_key"):
        kwargs["api_key"] = creds["api_key"]
    if creds.get("api_base"):
        kwargs["api_base"] = creds["api_base"]
    return kwargs


def _sanitize_messages(messages: list) -> list:
    """Midum's internal history is loosely Ollama-shaped. LiteLLM (and the
    providers behind it) validate strictly: tool_call ids must exist and
    function.arguments must be a JSON *string*. Repair on the way out without
    touching the shared internal format used by every other provider."""
    sanitized, pending_ids, counter = [], [], 0
    for m in messages:
        m = dict(m)
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            new_calls = []
            for tc in m["tool_calls"]:
                fn = (tc or {}).get("function", {}) or {}
                args = fn.get("arguments", {})
                if not isinstance(args, str):
                    try:
                        args = json.dumps(args)
                    except Exception:
                        args = "{}"
                call_id = tc.get("id") or f"call_{counter}"
                counter += 1
                pending_ids.append(call_id)
                new_calls.append({"id": call_id, "type": "function",
                                  "function": {"name": fn.get("name", ""), "arguments": args}})
            m["tool_calls"] = new_calls
            if not m.get("content"):
                m["content"] = None
        elif role == "tool":
            if not m.get("tool_call_id"):
                m["tool_call_id"] = pending_ids.pop(0) if pending_ids else f"call_{counter}"
            if not isinstance(m.get("content"), str):
                m["content"] = "" if m.get("content") is None else str(m["content"])
        # Drop Ollama-only keys that other providers reject.
        m.pop("images", None)
        sanitized.append(m)
    return sanitized


def _get(obj, key, default=None):
    """Attribute-or-key access -- LiteLLM returns pydantic objects, but be safe."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _litellm_chat(messages: list, model: str = None, tools_schema: list = None,
                  timeout: int = 120) -> dict:
    """
    Run one chat completion through LiteLLM. Returns the SAME normalised shape
    every other Midum provider produces:
        {"message": {"role": "assistant", "content": str,
                     "tool_calls": [{"function": {"name": str, "arguments": dict}}]}}
    """
    if not _LITELLM_AVAILABLE:
        raise RuntimeError(_litellm_load_msg)
    ll = _get_litellm()

    model = (model or config.LITELLM_MODEL or "").strip()
    if not model:
        raise RuntimeError("No LiteLLM model selected. Pick a provider and model in the Model tab.")

    kwargs = {
        "model": model,
        "messages": _sanitize_messages(messages),
        "timeout": timeout,
        "num_retries": 2,
    }
    if tools_schema:
        kwargs["tools"] = tools_schema
    kwargs.update(_credential_kwargs(model))

    try:
        resp = ll.completion(**kwargs)
    except Exception as e:
        # LiteLLM errors carry a readable message; add a hint for the two most
        # common first-run mistakes.
        msg = str(e)
        low = msg.lower()
        hint = ""
        if "api key" in low or "authentication" in low or "401" in low:
            hint = " -- check the API key for this provider (Model tab -> API key)."
        elif "llm provider not provided" in low or "badrequesterror" in low and "provider" in low:
            hint = " -- model IDs need a provider prefix, e.g. openai/gpt-4o-mini."
        raise RuntimeError(f"LiteLLM error ({model}): {msg[:400]}{hint}")

    choice = (_get(resp, "choices") or [None])[0]
    msg = _get(choice, "message")
    content = _get(msg, "content") or ""
    if not isinstance(content, str):
        # Some providers return a list of content parts.
        try:
            content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        except Exception:
            content = str(content)

    normalised = []
    for tc in _get(msg, "tool_calls") or []:
        fn = _get(tc, "function")
        args = _get(fn, "arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except Exception:
                pass
        normalised.append({"function": {"name": _get(fn, "name", "") or "", "arguments": args}})

    return {"message": {"role": "assistant", "content": content, "tool_calls": normalised}}


def _litellm_chat_with_fallback(messages: list, model: str = None, tools_schema: list = None,
                                timeout: int = 120) -> dict:
    """Kept for symmetry with the other providers' call sites. Deliberately NO
    silent fallback to a different model: with 100+ possible (often paid)
    providers behind LiteLLM, quietly switching models/billing accounts on an
    error would be a nasty surprise. Errors surface as-is instead."""
    return _litellm_chat(messages, model=model, tools_schema=tools_schema, timeout=timeout)


def test_connection(model: str) -> dict:
    """Tiny 'say ok' completion so the GUI can confirm key + model work."""
    model = (model or "").strip()
    if not model:
        return {"ok": False, "error": "Pick a provider and model first."}
    start = time.time()
    try:
        ll = _get_litellm()
        kwargs = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            "max_tokens": 16,
            "timeout": 45,
            "num_retries": 0,
        }
        kwargs.update(_credential_kwargs(model))
        resp = ll.completion(**kwargs)
        choice = (_get(resp, "choices") or [None])[0]
        reply = (_get(_get(choice, "message"), "content") or "").strip()
        return {"ok": True, "reply": reply[:60], "ms": int((time.time() - start) * 1000)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def context_window_for(model: str):
    """LiteLLM's own knowledge of a model's input context size, or None."""
    if not _LITELLM_AVAILABLE or not model:
        return None
    try:
        ll = _get_litellm()
        info = ll.get_model_info(model)
        val = info.get("max_input_tokens") or info.get("max_tokens")
        return int(val) if val else None
    except Exception:
        return None


def set_litellm_model(model_id: str) -> str:
    """Switch config.LITELLM_MODEL at runtime -- no restart needed."""
    old = config.LITELLM_MODEL
    new = (model_id or "").strip()
    if not new:
        return "Error: empty model ID."
    config.LITELLM_MODEL = new
    print(f"🔀 [LiteLLM model switched: '{old}' → '{new}']")
    return f"LiteLLM model switched: '{old}' → '{new}' (this session only)."
