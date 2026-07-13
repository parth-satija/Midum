import platform as _platform

_IS_LINUX   = _platform.system() == "Linux"
_IS_WINDOWS = _platform.system() == "Windows"

# --- AUTO-SPLITTER: imports added by automated pass, please review ---
import ollama
import os
import re
import subprocess

try:
    import mcp as _mcp_sdk  # noqa: F401
    _MCP_SDK_AVAILABLE = True
except ImportError:
    _mcp_sdk = None
    _MCP_SDK_AVAILABLE = False
# --- from main.py, section 1 ---
# CONFIGURATION
# =============================================================================
STARTUP_DIR    = os.getcwd()
MODEL_NAME     = "jarvishehe"

# ── PRIMARY MODEL PROVIDER ────────────────────────────────────────────────────
# "ollama"     — use the local Ollama model (MODEL_NAME above) as the primary
#                execution brain. OpenRouter is available as a frequent
#                secondary consultant (see OPENROUTER_CONSULT_MODE below).
# "openrouter" — use OPENROUTER_MODEL as the PRIMARY execution brain instead
#                of Ollama. Every tool-calling turn is a metered API call.
#                Use this if you have an OpenRouter key and want a stronger
#                model driving Midum directly instead of qwen2.5-coder.
# "gemini_web" — use Gemini, accessed through the `gemini_webapi` library
#                (account cookie sign-in, no API key, no per-token metering)
#                as the PRIMARY execution brain. Reuses the same account
#                session as consult_gemini/query_gemini_app (see the
#                GEMINI WEB APP CLIENT section below). Every tool-calling
#                step is a real web request to gemini.google.com's internal
#                endpoints through a persistent ChatSession, so it is slower
#                per-step than Ollama/OpenRouter — expect real latency on
#                multi-hop tool loops. See GEMINI_WEB_MODEL below to pick
#                the model tier.
# "gemini_api" — use Gemini as the PRIMARY execution brain through the
#                OFFICIAL Google Gemini API (an API key, not a browser/cookie
#                hack), talking to Gemini's OpenAI-compatible /chat/completions
#                endpoint with real structured function-calling. Same request
#                shape as "openrouter", so it gets the exact same reliability
#                (native tool_calls field, no scraping, no session juggling)
#                and is fully wired into tool calling / MCP servers exactly
#                like every other provider. See GEMINI_API_MODEL below.
#
# A future GUI will expose this as a dropdown — for now, edit directly.
# "groq"       — use GROQ_MODEL as the PRIMARY execution brain instead of
#                Ollama. GroqCloud offers a genuinely free tier (no credit
#                card) with fast inference and native structured tool
#                calling on models like llama-3.3-70b-versatile and
#                qwen/qwen3-32b. Same request shape as "openrouter" /
#                "gemini_api" (OpenAI-compatible /chat/completions), so it
#                is fully wired into tool calling / MCP servers exactly
#                like every other provider. See GROQ_MODEL below.
MODEL_PROVIDER = "gemini_web"   # "ollama" | "openrouter" | "gemini_web" | "gemini_api" | "groq"

# ── OpenRouter model selection ────────────────────────────────────────────────
# Used when MODEL_PROVIDER == "openrouter" (primary), and always used for
# secondary consultation regardless of MODEL_PROVIDER (see OPENROUTER_CONSULT_MODE).
#
# OpenRouter model IDs: https://openrouter.ai/models
# Free-tier examples (subject to change, check openrouter.ai/models?max_price=0):
#   "meta-llama/llama-3.1-8b-instruct:free"
#   "meta-llama/llama-3.3-70b-instruct:free"
#   "google/gemini-2.0-flash-exp:free"
#   "deepseek/deepseek-chat-v3.1:free"
#   "qwen/qwen3-235b-a22b:free"
# Paid examples (for MODEL_PROVIDER == "openrouter" as a strong primary):
#   "anthropic/claude-3.7-sonnet"
#   "openai/gpt-4o"
#   "google/gemini-2.5-pro"
OPENROUTER_MODEL          = "meta-llama/llama-3.3-70b-instruct:free"   # primary (if selected) AND consult model
OPENROUTER_API_BASE       = "https://openrouter.ai/api/v1"

# ── Fallback free models ──────────────────────────────────────────────────────
# OpenRouter's ":free" models share a global rate-limit pool across ALL
# OpenRouter users, not just you — so a single free model (like the
# llama-3.3-70b:free default above) will very commonly return 429
# ("Provider returned error") during busy periods, no matter how you
# configure retries. When that happens, _openrouter_chat_with_fallback()
# below automatically tries the next model in this list instead of just
# failing the turn. Order = preference. Keep OPENROUTER_MODEL first (it's
# tried first), the rest are only used if it 429s/502s/503s repeatedly.
OPENROUTER_FALLBACK_MODELS = [
    OPENROUTER_MODEL,
    "meta-llama/llama-3.1-8b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "deepseek/deepseek-chat-v3.1:free",
    "qwen/qwen3-235b-a22b:free",
]

# ── Secondary consultation mode ───────────────────────────────────────────────
# Controls how often OpenRouter is consulted as a planning brain in addition
# to (or instead of) Gemini. OpenRouter free models are consulted far more
# aggressively than Gemini since there's no desktop-app UI-automation cost —
# it's a direct API call, so it's cheap to call on every non-trivial turn.
#   "always"    — consult OpenRouter on every non-trivial turn (most reliable,
#                 more API usage — fine for free-tier models)
#   "fallback"  — only consult OpenRouter if Gemini (app + API) both fail
#   "off"       — never consult OpenRouter as a secondary planner
OPENROUTER_CONSULT_MODE   = "always"

# ── Gemini API (official) model selection ─────────────────────────────────────
# Used when MODEL_PROVIDER == "gemini_api". This is the REAL Google Gemini API
# (an API key from https://aistudio.google.com/app/apikey), NOT the web-chat
# scraping used by "gemini_web" — no cookies, no browser session, no auto
# model detection. Structured function calling is native, so tool calling is
# reliable step to step.
GEMINI_API_MODEL = "gemini-3.1-flash-lite"   # exact model ID as listed at https://ai.google.dev/gemini-api/docs/models
GEMINI_API_BASE  = "https://generativelanguage.googleapis.com/v1beta/openai"

# ── GroqCloud model selection ─────────────────────────────────────────────────
# Used when MODEL_PROVIDER == "groq" (primary), and always available for
# on-demand consultation/delegation regardless of MODEL_PROVIDER, exactly
# like OpenRouter/Gemini API. GroqCloud's free tier needs only a key from
# https://console.groq.com/keys — no credit card — and gives very fast
# inference plus reliable native structured tool-calling.
#
# Model IDs: https://console.groq.com/docs/models (subject to change):
#   "llama-3.3-70b-versatile"   — strong general-purpose, good tool calling
#   "llama-3.1-8b-instant"      — fastest, smaller
#   "qwen/qwen3-32b"            — strong reasoning/coding
#   "deepseek-r1-distill-llama-70b" — reasoning-heavy
#   "moonshotai/kimi-k2-instruct"   — large MoE, strong tool use
GROQ_MODEL          = "llama-3.3-70b-versatile"   # primary (if selected) AND consult model
GROQ_API_BASE        = "https://api.groq.com/openai/v1"

# GroqCloud's free tier has generous per-minute/per-day request & token
# limits that vary by model, and can occasionally 429 during bursts. When
# that happens, _groq_chat_with_fallback() automatically tries the next
# model in this list instead of just failing the turn. Order = preference.
GROQ_FALLBACK_MODELS = [
    GROQ_MODEL,
    "llama-3.1-8b-instant",
    "qwen/qwen3-32b",
    "moonshotai/kimi-k2-instruct",
]

# ── Gemini-web (gemini_webapi) primary-execution settings ─────────────────────
# Used only when MODEL_PROVIDER == "gemini_web". Separate from consult_gemini/
# query_gemini_app's ad-hoc single-shot calls — this drives the ENTIRE
# tool-calling loop through one persistent gemini_webapi ChatSession per
# conversation (see "GEMINI WEB APP CLIENT" section for the underlying
# client, and "GEMINI-WEB PRIMARY EXECUTION BACKEND" for the loop wiring).
#
# GEMINI_WEB_MODEL: "" (default) = auto-pick the fastest available model via
# client.list_models() at runtime (never hardcode a model name — the web
# app's exposed lineup changes over time and isn't guaranteed to match the
# name you last saw). Set to an exact model_name/display_name string
# (e.g. "gemini-3-flash") to pin one instead.
GEMINI_WEB_MODEL              = "gemini-3-flash"   # exact model name as listed in gemini_webapi client.list_models()

# NOTE: deliberately NOT using a Gem here. Gems proved problematic (flaky
# create/update/fetch round trips, an extra persistent server-side object
# that can drift out of sync, silent "no-gem mode" degradation). Instead
# persona + tool JSON-output-format instructions are sent as a plain-text
# priming message on the first turn of every fresh ChatSession (see
# _gemini_web_persona_prompt()), and the native tool schema is discovered
# on demand via list_native_tools()/show_native_tool_schema(tool_name)
# instead of being inlined into anything persistent. Same capability, no
# Gem dependency.

# Per-hop and whole-task timeout budgets (seconds). Each tool-loop hop is a
# real round trip through gemini.google.com, so these are generous compared
# to the local-model/OpenRouter equivalents.
GEMINI_WEB_HOP_TIMEOUT        = 120
GEMINI_WEB_TOTAL_TASK_TIMEOUT = 1800

# Marker prefixed to every tool-result message injected into the Gemini
# ChatSession, so Gemini can reliably tell an injected tool result apart
# from a genuine new user message. Keep this exact string in sync anywhere
# else results are formatted for Gemini.
GEMINI_WEB_TOOL_RESULT_MARKER = "[TOOL_RESULT]"

# ── Legacy / weak native-tool-calling models ────────────────────────────────
LEGACY_TOOLCALL_MODELS = (
    "qwen2.5-coder",
    "qwen2.5",
    "codeqwen",
    "deepseek-coder",
    "codellama",
)

def _is_legacy_toolcall_model(model_name: str) -> bool:
    """True if model_name or its underlying base model matches a known weak tool-calling family."""
    low = model_name.lower()
    if any(fam in low for fam in LEGACY_TOOLCALL_MODELS):
        return True
    try:
        info = ollama.show(model_name)
        base = info.get("modelinfo", {}).get("general.basename", "").lower()
        return any(fam in base for fam in LEGACY_TOOLCALL_MODELS)
    except Exception:
        return False

# ── Platform-aware paths ──────────────────────────────────────────────────────
if _IS_LINUX:
    _HOME           = os.path.expanduser("~")
    TARGET_DIR      = os.path.join(_HOME, "Jarvis")
    STORAGE_DIR     = os.path.join(TARGET_DIR, "jarvis_project", "storage")
    SKILLS_INDEX    = os.path.join(TARGET_DIR, "jarvis_project", "skills.md")
    SECRETS_FILE    = os.path.join(_HOME, ".config", "JarvisSecrets", "jarvis_secrets.json")
else:
    TARGET_DIR      = r"D:\Jarvis"
    STORAGE_DIR     = r"D:\Jarvis\jarvis_project\storage"
    SKILLS_INDEX    = r"D:\Jarvis\jarvis_project\skills.md"
    SECRETS_FILE    = os.path.join(
        os.path.expanduser("~"), "AppData", "Local", "JarvisSecrets", "jarvis_secrets.json"
    )

COMMANDS_FILE       = os.path.join(STORAGE_DIR, "commands.md")
INSTRUCTIONS_FILE   = os.path.join(STORAGE_DIR, "instructions.md")
PATHS_FILE          = os.path.join(STORAGE_DIR, "paths.md")
DOMAIN_INDEX        = os.path.join(STORAGE_DIR, "domain_index.md")
SKILLS_DIR          = os.path.join(STORAGE_DIR, "skills")
DOMAIN_SKILLS_INDEX = os.path.join(STORAGE_DIR, "domain_skills_index.md")
MASTER_MEMORY       = os.path.join(STORAGE_DIR, "master_memory.md")
SESSION_MEMORY      = os.path.join(STORAGE_DIR, "session_memory.md")
RESPONSE_MEMORY     = os.path.join(STORAGE_DIR, "response_memory.md")
MCP_SERVERS_FILE    = os.path.join(STORAGE_DIR, "mcp_servers.json")
LOG_FILE            = os.path.join(TARGET_DIR, "chat_log.md")

GOAL_SECTION_HEADER = "## Current Goal"
GOAL_SECTION_END    = "## Goal History"

# ── Screen resolution — detected at runtime on Linux, hardcoded on Windows ───
def _detect_screen_resolution() -> tuple[int, int]:
    """Detect screen resolution. Falls back to 1920x1080 if detection fails."""
    if _IS_LINUX:
        try:
            out, _ = subprocess.run(
                ["xdpyinfo"], capture_output=True, text=True, timeout=5
            ).stdout, None
            m = re.search(r"dimensions:\s*(\d+)x(\d+)", out)
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:
            pass
        try:
            out, _ = subprocess.run(
                ["xrandr", "--current"], capture_output=True, text=True, timeout=5
            ).stdout, None
            m = re.search(r"current (\d+) x (\d+)", out)
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:
            pass
        return 1920, 1080   # safe fallback
    else:
        return 2560, 1600   # Windows default — update if your resolution differs

SCREEN_W, SCREEN_H = _detect_screen_resolution()

# The model's internal canvas — Ollama vision models downscale to 1024px long edge
MODEL_CANVAS_W = 1024
MODEL_CANVAS_H = int(1024 * SCREEN_H / SCREEN_W)

# Scale factors: real_px = canvas_px * SCALE
SCALE_X = SCREEN_W / MODEL_CANVAS_W
SCALE_Y = SCREEN_H / MODEL_CANVAS_H

# Grid drawn on screenshots at canvas resolution; every GRID_STEP canvas-px
GRID_STEP = 100

# =============================================================================

