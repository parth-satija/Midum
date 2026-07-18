from config import _IS_LINUX, _IS_WINDOWS
# --- AUTO-SPLITTER: facade re-exports (consumers use 'import ... as midum' as a whole-namespace alias) ---
from config import GEMINI_API_MODEL, GEMINI_WEB_MODEL, GROQ_FALLBACK_MODELS, GROQ_MODEL, INSTRUCTIONS_FILE, MASTER_MEMORY, OPENROUTER_FALLBACK_MODELS, OPENROUTER_MODEL, PATHS_FILE, RESPONSE_MEMORY, SKILLS_DIR, STORAGE_DIR
from config import _MCP_SDK_AVAILABLE
from knowledge_base import add_instruction, add_path, create_domain_knowledge, create_domain_skill, list_domain_knowledge, list_domain_skills, read_domain_knowledge, read_instructions, read_paths
from midum_mcp.manager import _load_mcp_config, _mcp_manager, connect_mcp_server, disconnect_mcp_server, list_mcp_servers
from midum_mcp.tools import call_mcp_tool, list_native_tools, show_native_tool_schema, show_server_tools
from tools.user_prompt_tools import ask_user_approval, ask_user_choice, ask_user_file_path, ask_user_text
from tools_schema import tools
from ui_automation import ui_navigator

# --- AUTO-SPLITTER: imports added by automated pass, please review ---
import config
import memory
import providers.gemini_api_backend as providers_gemini_api_backend
import providers.gemini_reasoning as providers_gemini_reasoning
import providers.gemini_web_backend as providers_gemini_web_backend
import providers.groq_backend as providers_groq_backend
import providers.openrouter_backend as providers_openrouter_backend
from browser_cdp import _CDP_AVAILABLE, _cdp_get_tabs, act_on_browser_element, list_browser_tabs, query_gemini_app, read_browser_page, run_js_in_browser, snapshot_browser_elements
from config import GOAL_SECTION_END, GOAL_SECTION_HEADER, LOG_FILE, MCP_SERVERS_FILE, MODEL_NAME, MODEL_PROVIDER, OPENROUTER_CONSULT_MODE, SCREEN_H, SCREEN_W, SECRETS_FILE, SESSION_MEMORY, TARGET_DIR
from flows import compile_flow, list_flows, list_flow_schemas, run_flow, save_flow, validate_flow_name
from midum_mcp.manager import _MCP_SERVERS, _MCP_SERVER_ORDER, init_mcp_servers_from_config
from midum_mcp.manager import demote_mcp_tool, get_promoted_tools, is_tool_promoted, promote_mcp_tool
from memory import init_memory_at_startup, python_trigger_memory_update, set_current_goal, update_memory
from orchestration import _decompose_task, _is_trivial_input, get_gemini_reasoning, process_chat_turn, wait
from providers.gemini_api_backend import _gemini_api_load_msg, consult_gemini_api, delegate_to_gemini_api, set_gemini_api_model
from providers.gemini_reasoning import consult_gemini
from providers.gemini_web_backend import _GEMINI_WEBAPI_AVAILABLE, _gemini_webapi_load_msg, _get_gemini_web_client, delegate_to_gemini_web, set_gemini_web_model
from providers.groq_backend import _groq_load_msg, consult_groq, delegate_to_groq, list_groq_models, set_groq_model, set_groq_model_by_index
from providers.ollama_cloud_backend import consult_ollama_cloud, delegate_to_ollama_cloud, list_ollama_cloud_models, set_ollama_cloud_model
from providers.openrouter_backend import _openrouter_load_msg, consult_openrouter, delegate_to_openrouter, list_openrouter_models, set_openrouter_model, set_openrouter_model_by_index
from screen_capture import capture_screen_to_ram, fallback_click_grid, fallback_click_text, fallback_find_text, type_text
from skills import list_skills, load_skill
from state import _abort_event
from system_prompt import get_system_prompt
from tools_registry import _uia_unavailable_message, append_local_file, append_response_memory, clear_response_memory, click_ocr_index, click_ui_element, create_flowchart, execute_python_code, execute_terminal_command, explore_path, find_file, generate_image, get_path, list_active_windows, list_directory, list_domain_knowledge_indexed, list_domain_skills_indexed, list_more_tools, list_paths_indexed, list_skills_indexed, load_skill_by_index, load_tool_by_index, manual_inspect_app_subtree, manual_interact_with_ui, manual_scan_app_layouts, ocr_snapshot, open_path, open_path_by_index, open_search_result, open_url, read_domain_by_index, read_file_chunk, read_file_smart, read_local_file, read_response_memory, read_search_result, reset_groq_loaded_tools, search_internet, write_docx_file, write_local_file, write_response_memory
from ui_automation.linux_navigator import _PYATSPI_AVAILABLE
from ui_automation.windows_uia import _TESSERACT_AVAILABLE, _UIA_AVAILABLE, _UIA_INIT_ERROR
from utils.path_resolver import resolve_file_path

# --- from main.py, section 1 ---
import io
import os
import re
import json
import base64
import tempfile
import datetime
import subprocess
import threading
import asyncio
import queue as _queue
import platform as _platform
import ollama
from ddgs import DDGS
import time

# requests is used by OpenRouter (chat completions) and CDP (Chrome tab
# listing). Imported once at module level; both subsystems degrade
# gracefully (return a clear "not installed" message) if missing.
try:
    import requests
except ImportError:
    requests = None


# PIL ImageGrab works on Windows natively. On Linux it requires either
# python3-xlib (X11) or a scrot/gnome-screenshot fallback.
try:
    from PIL import ImageGrab as _ImageGrab
    _IMAGEGRAB_AVAILABLE = True
except ImportError:
    _ImageGrab = None
    _IMAGEGRAB_AVAILABLE = False

# keyboard is used for the Ctrl+Q abort shortcut.
# Install with:  pip install keyboard
# Note: on Windows, keyboard requires no extra drivers. Run as admin if hotkeys
# don't fire (rare — usually works in normal user sessions inside a terminal).
try:
    import keyboard as _keyboard
    _KEYBOARD_AVAILABLE = True
except ImportError:
    _KEYBOARD_AVAILABLE = False

# ── Optional file-format libraries ────────────────────────────────────────────
# PDF reading:   pip install pymupdf
# DOCX reading:  pip install mammoth
# DOCX writing:  pip install python-docx
try:
    import fitz as _fitz
    _PDF_AVAILABLE = True
except ImportError:
    _fitz = None
    _PDF_AVAILABLE = False

try:
    import mammoth as _mammoth
    _MAMMOTH_AVAILABLE = True
except ImportError:
    _mammoth = None
    _MAMMOTH_AVAILABLE = False

try:
    import docx as _docx
    _DOCX_AVAILABLE = True
except ImportError:
    _docx = None
    _DOCX_AVAILABLE = False

# MCP (Model Context Protocol) client SDK — lets Midum connect to external
# MCP servers (stdio subprocesses, or remote HTTP/SSE endpoints) and call
# their tools. Install with:  pip install mcp



# rich renders Markdown in the terminal (headers, bold, code blocks, tables).
# Pure formatting — zero effect on model logic or performance.
# Install with:  pip install rich
try:
    from rich.console import Console as _Console
    from rich.markdown import Markdown as _Markdown
    _console          = _Console()
    _RICH_AVAILABLE   = True
except ImportError:
    _RICH_AVAILABLE   = False

def _print_reply(label: str, text: str):
    """Print Midum's reply, rendering Markdown if rich is available."""
    # Suppress replies that are pure JSON/punctuation leftovers from legacy parsing
    if not text or re.match(r'^[{}\[\]",:\s]*$', text.strip()):
        return
    print(f"\n{label}")
    if _RICH_AVAILABLE and text.strip():
        _console.print(_Markdown(text))
    else:
        print(text)

# =============================================================================

# --- from main.py, section 2 ---
# 10. INTERACTIVE MAIN LOOP
# =============================================================================

if __name__ == "__main__":
    os.makedirs(TARGET_DIR, exist_ok=True)

    # ── Ctrl+Q abort hotkey ────────────────────────────────────────────────────
    if _KEYBOARD_AVAILABLE:
        _keyboard.add_hotkey("ctrl+q", lambda: _abort_event.set())
        print("⌨️  [Ctrl+Q registered — press to abort the current response]")
    else:
        print("⚠️  [keyboard package not found — Ctrl+Q abort unavailable]")
        print("    Install with: pip install keyboard")

    # ── Tesseract status ───────────────────────────────────────────────────────
    if _TESSERACT_AVAILABLE:
        print("👁️  [Tesseract OCR: available — fallback_click_text is active]")
    else:
        print("⚠️  [Tesseract OCR not found — fallback_click_text will not work]")
        if _IS_LINUX:
            print("    Install with: sudo apt install tesseract-ocr && pip install pytesseract")
        else:
            print("    Install from: https://github.com/UB-Mannheim/tesseract/wiki")
            print("    Then: pip install pytesseract")

    # ── UI automation status ───────────────────────────────────────────────────
    if _IS_LINUX:
        if _PYATSPI_AVAILABLE:
            print("🖱️  [Linux UI automation: AT-SPI2 + xdotool available]")
        else:
            print("⚠️  [pyatspi not found — AT-SPI tree inspection unavailable]")
            print("    Install: sudo apt install python3-pyatspi xdotool xclip")
    else:
        if _UIA_AVAILABLE:
            print("🖱️  [Windows UI automation: uiautomation available]")
        else:
            print("⚠️  [UI automation unavailable — click_ui_element/snapshot will not work]")
            if _UIA_INIT_ERROR:
                print(f"    Real cause: {_UIA_INIT_ERROR}")
                low = _UIA_INIT_ERROR.lower()
                if "pywin32 not installed" in low:
                    print("    Fix: pip install pywin32 uiautomation")
                elif "coinitialize" in low or "com" in low:
                    print("    Fix: this is a COM threading conflict, not a Windows version issue.")
                    print("    Try: pip install --force-reinstall pywin32, then run:")
                    print("      python <python_dir>\\Scripts\\pywin32_postinstall.py -install")
                    print("    (run that command as Administrator)")
                else:
                    print("    This is NOT actually a 'Windows needs updating' issue — ignore that advice.")
                    print("    Try: pip install --force-reinstall uiautomation pywin32")
            else:
                print("    pip install uiautomation pywin32")

    # ── Gemini status (web chat only, via gemini_webapi — no API fallback) ─────
    if _GEMINI_WEBAPI_AVAILABLE:
        print("🤖 [Gemini: gemini_webapi installed — consult_gemini is active "
              "(client/cookies initialise lazily on first use)]")
    else:
        print(f"⚠️  [Gemini web chat unavailable: {_gemini_webapi_load_msg}]")
        print( "    Install: pip install -U gemini_webapi")
        print( "    Optional (auto cookie import): pip install -U browser-cookie3")
        print(f"    Or add cookies manually to the secrets file "
              f"({os.path.abspath(SECRETS_FILE)}):")
        print( "      { \"GEMINI_SECURE_1PSID\": \"...\", \"GEMINI_SECURE_1PSIDTS\": \"...\" }")
    if providers_gemini_reasoning._GEMINI_AVAILABLE:
        print("    (Gemini API key is also configured, but is unused — consult_gemini "
              "only uses the web chat interface now.)")

    # ── Gemini API (official) status ────────────────────────────────────────────
    if providers_gemini_api_backend._GEMINI_API_AVAILABLE:
        print(f"🔑 [Gemini API (official): available — model={config.GEMINI_API_MODEL}]")
    else:
        print(f"⚠️  [Gemini API (official) not available: {_gemini_api_load_msg}]")
        print(f"    Secrets file expected at: {os.path.abspath(SECRETS_FILE)}")
        print( "    Add key: { \"GEMINI_API_KEY\": \"AIza...\" } (same file as everything else)")
        print( "    Get a key: https://aistudio.google.com/app/apikey")

    # ── OpenRouter status ──────────────────────────────────────────────────────
    if providers_openrouter_backend._OPENROUTER_AVAILABLE:
        print(f"🌍 [OpenRouter: available — model={config.OPENROUTER_MODEL}, "
              f"consult_mode={OPENROUTER_CONSULT_MODE}]")
    else:
        print(f"⚠️  [OpenRouter not available: {_openrouter_load_msg}]")
        print(f"    Secrets file expected at: {os.path.abspath(SECRETS_FILE)}")
        print( "    Add key: { \"OPENROUTER_API_KEY\": \"sk-or-v1-...\" } (same file as Gemini)")
        print( "    Get a key: https://openrouter.ai/keys")

    # ── GroqCloud status ────────────────────────────────────────────────────────
    if providers_groq_backend._GROQ_AVAILABLE:
        print(f"⚡ [GroqCloud: available — model={config.GROQ_MODEL}]")
    else:
        print(f"⚠️  [GroqCloud not available: {_groq_load_msg}]")
        print(f"    Secrets file expected at: {os.path.abspath(SECRETS_FILE)}")
        print( "    Add key: { \"GROQ_API_KEY\": \"gsk_...\" } (same file as everything else)")
        print( "    Get a free key: https://console.groq.com/keys")

    # ── Primary model provider summary ────────────────────────────────────────
    if MODEL_PROVIDER == "openrouter":
        if not providers_openrouter_backend._OPENROUTER_AVAILABLE:
            print("🛑 [MODEL_PROVIDER='openrouter' but OpenRouter is NOT configured — "
                  "Midum cannot run until OPENROUTER_API_KEY is set!]")
        else:
            print(f"🧠 [PRIMARY MODEL: OpenRouter/{config.OPENROUTER_MODEL} — driving Midum directly]")
    elif MODEL_PROVIDER == "gemini_web":
        if not _GEMINI_WEBAPI_AVAILABLE:
            print(f"🛑 [MODEL_PROVIDER='gemini_web' but gemini_webapi is NOT installed — "
                  f"{_gemini_webapi_load_msg}]")
        else:
            _gw_client, _gw_err = _get_gemini_web_client()
            if _gw_err:
                print(f"🛑 [MODEL_PROVIDER='gemini_web' but the client failed to initialize: {_gw_err}]")
            else:
                print(f"🧠 [PRIMARY MODEL: Gemini-web/{config.GEMINI_WEB_MODEL or 'auto'} — "
                      f"driving Midum directly via gemini_webapi ChatSession]")
    elif MODEL_PROVIDER == "gemini_api":
        if not providers_gemini_api_backend._GEMINI_API_AVAILABLE:
            print("🛑 [MODEL_PROVIDER='gemini_api' but the Gemini API is NOT configured — "
                  "Midum cannot run until GEMINI_API_KEY is set!]")
        else:
            print(f"🧠 [PRIMARY MODEL: Gemini-API/{config.GEMINI_API_MODEL} — driving Midum directly "
                  f"via the official API]")
    elif MODEL_PROVIDER == "groq":
        if not providers_groq_backend._GROQ_AVAILABLE:
            print("🛑 [MODEL_PROVIDER='groq' but GroqCloud is NOT configured — "
                  "Midum cannot run until GROQ_API_KEY is set!]")
        else:
            print(f"🧠 [PRIMARY MODEL: Groq/{config.GROQ_MODEL} — driving Midum directly "
                  f"via GroqCloud's free-tier API]")
    else:
        print(f"🧠 [PRIMARY MODEL: Ollama/{MODEL_NAME} — local execution brain]")
        if OPENROUTER_CONSULT_MODE != "off" and providers_openrouter_backend._OPENROUTER_AVAILABLE:
            print(f"    Planning consult: OpenRouter/{config.OPENROUTER_MODEL} "
                  f"({OPENROUTER_CONSULT_MODE}), Gemini as {'primary consult' if OPENROUTER_CONSULT_MODE=='fallback' else 'fallback'}")

    # ── CDP browser status ─────────────────────────────────────────────────────
    if _CDP_AVAILABLE:
        tabs = _cdp_get_tabs()
        if tabs:
            print(f"🌐 [CDP: connected — {len(tabs)} tab(s) open]")
        else:
            print("🌐 [CDP: installed but Chrome not in debug mode]")
            print("    Launch Chrome with: --remote-debugging-port=9222")
    else:
        print("⚠️  [CDP not available — browser DOM tools disabled]")
        print("    Install: pip install websocket-client requests")

    # ── MCP servers ─────────────────────────────────────────────────────────
    if _MCP_SDK_AVAILABLE:
        init_mcp_servers_from_config()
        if _MCP_SERVER_ORDER:
            connected_n = sum(1 for n in _MCP_SERVER_ORDER if _MCP_SERVERS[n].connected)
            print(f"🧩 [MCP: {connected_n}/{len(_MCP_SERVER_ORDER)} server(s) connected — "
                  f"list_mcp_servers() for details]")
        else:
            print(f"🧩 [MCP: SDK installed, no servers configured yet — "
                  f"connect_mcp_server(...) to add one, or edit "
                  f"{os.path.abspath(MCP_SERVERS_FILE)}]")
    else:
        print("⚠️  [MCP not available — 'mcp' package not installed]")
        print("    Install: pip install mcp")

    print(f"🖥️  [Platform: {'Linux' if _IS_LINUX else 'Windows'} | "
          f"Screen: {SCREEN_W}x{SCREEN_H} | "
          f"Shell: {'bash' if _IS_LINUX else 'PowerShell'}]")

    memory_injections = init_memory_at_startup()

    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write("# Midum Master Interaction Log\n")
            f.write(f"Session started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            f.write("=========================================\n\n")
    except Exception as e:
        print(f"⚠️ Warning: Could not initialise log file: {e}")

    print("\n====================================================")
    print("Midum local agent started. Persistent Chat Ready.")
    print(f"Tracking live session in: {LOG_FILE}")
    print("Type 'new session' to wipe session memory.")
    print("Type 'exit' or 'quit' to close.")
    print("====================================================\n")

    system_prompt = get_system_prompt()

    goal_reminder = ""
    if memory._current_goal:
        goal_reminder = (
            f"\n\n[GOAL REMINDER]\nCurrent goal: {memory._current_goal}\n"
            "Continue unless redirected."
        )

    history = [{"role": "system", "content": system_prompt + goal_reminder}]
    for inj in memory_injections:
        history.append({"role": "system", "content": inj})

    turn_counter = 1

    while True:
        try:
            user_input = input("\nYou: ").strip()

            if user_input.lower() == "new session":
                if os.path.exists(SESSION_MEMORY):
                    os.remove(SESSION_MEMORY)
                    print("🗑️ Session memory cleared.")
                memory._current_goal = None
                reset_groq_loaded_tools()
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                write_local_file(
                    SESSION_MEMORY,
                    f"# Midum Session Memory\nSession started: {ts}\n\n"
                    f"{GOAL_SECTION_HEADER}\n_No active goal._\n\n{GOAL_SECTION_END}\n"
                )
                print("🧠 New session started.\n")
                history = [{"role": "system", "content": system_prompt}]
                for inj in memory_injections[:1]:
                    history.append({"role": "system", "content": inj})
                turn_counter = 1
                continue

            if user_input.lower() in ("exit", "quit"):
                print("\nCleaning up...")
                if os.path.exists(LOG_FILE):
                    try:
                        os.remove(LOG_FILE)
                        print(f"🗑️ Deleted: {LOG_FILE}")
                    except Exception as e:
                        print(f"⚠️ Could not delete log: {e}")
                print("Goodbye!")
                break

            if not user_input:
                continue

            gemini_plan = ""   # always defined before process_chat_turn
            approval_keywords = ["yes", "grant", "approve", "run it", "go ahead", "y"]
            if any(kw in user_input.lower() for kw in approval_keywords):
                payload = f"{user_input} [USER MANUALLY GRANTED BYPASS]"
                history.append({"role": "user", "content": payload})
            else:
                # ── Gemini pre-planning ───────────────────────────────────────
                if not _is_trivial_input(user_input):
                    plan = get_gemini_reasoning(user_input, history)
                    gemini_plan = plan or ""
                else:
                    gemini_plan = ""

                # ── Task decomposition (fallback if Gemini unavailable) ────────
                task_plan = _decompose_task(user_input) if not gemini_plan else None

                plan_text = ""
                if gemini_plan:
                    plan_text = f"\n\n[EXECUTION PLAN FROM PLANNING BRAIN]\n{gemini_plan}"
                elif task_plan:
                    plan_text = f"\n\n{task_plan}"

                payload = (
                    f"{user_input}{plan_text}\n\n"
                    "[SYSTEM]: Follow the execution plan above step by step. "
                    "Execute the first tool call now. Do not explain — just act."
                )
                history.append({"role": "user", "content": payload})

            print("\n[Thinking...]")
            assistant_reply, turn_tool_outputs = process_chat_turn(
                history,
                user_request=user_input,
                gemini_plan=gemini_plan
            )
            _print_reply("Midum:", assistant_reply)

            python_trigger_memory_update(turn_tool_outputs, assistant_reply)

            try:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(f"# Response {turn_counter}\n\n")
                    f.write(f"### **User Prompt:**\n> {user_input}\n\n")
                    f.write(f"_Goal: {memory._current_goal or 'none'}_\n\n")
                    f.write(f"### **Midum Reply:**\n{assistant_reply}\n\n---\n\n")
                print(f"💾 [Logged response {turn_counter}]")
                turn_counter += 1
            except Exception as e:
                print(f"⚠️ Could not append to log: {e}")

        except KeyboardInterrupt:
            print("\n\nAborted.")
            if os.path.exists(LOG_FILE):
                try: os.remove(LOG_FILE)
                except: pass
            break
        except Exception as e:
            print(f"\nUnexpected error: {e}")
