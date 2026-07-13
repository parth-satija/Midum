# --- AUTO-SPLITTER: imports added by automated pass, please review ---
from config import SECRETS_FILE
import json
import os

# --- from main.py, section 1 ---
# GEMINI SETUP (UPDATED FOR GOOGLE-GENAI & FREE TIER PROTECTION)
# =============================================================================
#
# INSTALLATION:
#   pip install google-genai
#
# API KEY SETUP (one-time):
#   1. Get a free key at https://aistudio.google.com/app/apikey
#   2. Create the secrets file at the path shown in SECRETS_FILE above.
#      Example:  { "GEMINI_API_KEY": "AIza..." }
#
# CREDIT DEFENSE MECHANISM:
#   To prevent running out of requests or hitting tight Token Per Minute (TPM) 
#   limits immediately on the free tier, we default routing to gemini-2.0-flash 
#   and window large text contexts safely.
#
_GEMINI_AVAILABLE = False
_gemini_client    = None

def _load_gemini():
    """
    Load API key from secrets file and initialise the unified GenAI client.

    NOTE: as of this rewrite, the Gemini API is NOT used by consult_gemini
    or query_gemini_app anymore — both route exclusively through the free
    web chat interface (gemini.google.com/app) via browser/CDP automation,
    since the API's free-tier request/token-per-minute limits are far
    tighter than the web chat UI's own limits. This loader is kept only
    so _GEMINI_AVAILABLE can still be reported in the startup banner if a
    key happens to be configured; nothing in the active code path calls
    into _gemini_client.
    """
    global _GEMINI_AVAILABLE, _gemini_client
    try:
        from google import genai
        secrets_path = os.path.abspath(SECRETS_FILE)
        if not os.path.exists(secrets_path):
            return False, f"Secrets file not found: {secrets_path}"
        with open(secrets_path, "r", encoding="utf-8") as f:
            secrets = json.load(f)
        key = secrets.get("GEMINI_API_KEY", "").strip()
        if not key:
            return False, "GEMINI_API_KEY is empty in secrets file."
        
        # Initialize modern unified client
        _gemini_client = genai.Client(api_key=key)
        _GEMINI_AVAILABLE = True
        return True, "OK"
    except ImportError:
        return False, "google-genai not installed. Run: pip install google-genai"
    except Exception as e:
        return False, str(e)


# Attempt load at module import time; failure is non-fatal
_gemini_load_ok, _gemini_load_msg = _load_gemini()


def consult_gemini(prompt, task_type="auto", context=""):
    from providers.gemini_web_backend import _GEMINI_WEBAPI_AVAILABLE, _gemini_webapi_load_msg
    from browser_cdp import query_gemini_app
    """
    Send a prompt to Gemini via the actual WEB CHAT INTERFACE
    (https://gemini.google.com/app), using the community `gemini_webapi`
    library in query_gemini_app() — cookie-based, no browser automation
    or UIA involved. This intentionally does NOT fall back to the metered
    Gemini API — the API's free-tier request/token-per-minute limits are
    far tighter than what the web chat UI itself allows, so a silent
    fallback would burn through API credits without the caller ever
    knowing. If the web route fails, this returns a clear error instead
    so you can retry rather than unknowingly hitting the API.

    task_type is accepted for backward compatibility but has no effect
    now that there's no model to route between — it's always whatever
    model gemini.google.com/app is currently serving.
    """
    full_prompt = prompt.strip()
    if context.strip():
        if len(context) > 60000:
            context = context[:30000] + "\n... [TRUNCATED] ...\n" + context[-30000:]
        full_prompt = "[CONTEXT]\n" + context.strip() + "\n\n[TASK]\n" + full_prompt

    if not _GEMINI_WEBAPI_AVAILABLE:
        return f"Error: Gemini web chat is unavailable — {_gemini_webapi_load_msg}"

    print("   [Gemini web] Sending consult request...")
    try:
        result = query_gemini_app(full_prompt)
    except Exception as e:
        return f"Error: Gemini web chat request failed: {e}"

    if result and not result.startswith("Error"):
        print(f"   [Gemini web] Response received ({len(result)} chars)")
        return "[Gemini/web]\n" + result

    return f"Error: Gemini web chat failed — {result}"


# =============================================================================

