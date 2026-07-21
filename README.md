# Midum

Midum is an agentic framework built for local AI desktop assistance. It can help you with code, UI automation, repetitive tasks, browsing, research, file management, and more — all driven by a tool-calling AI agent that runs on **your** machine.


## What Midum Can Do

Midum is built around a large tool-calling loop, so it isn't limited to chatting — it can actually take action on your computer. Its capabilities include:

### 🖥️ Desktop & UI Automation
- Control your desktop through **UI Automation (UIA)** — click buttons, type into fields, navigate menus, and interact with virtually any native Windows application.
- Scan and inspect application UI trees (`manual_scan_app_layouts`, `manual_inspect_app_subtree`) to understand what's on screen before acting.
- Click specific UI elements directly by control ID/name (`click_ui_element`).
- Take an automatic `snapshot` of the active window and `act` on the elements it finds, letting Midum plan a sequence of interactions.
- List all active windows (`list_active_windows`) to figure out what's currently open.
- Fallback vision-based control when UIA isn't reliable enough: screen snapshots, OCR-based text finding and clicking (`fallback_view_screen`, `fallback_find_text`, `fallback_click_grid`, `fallback_click_text`, `ocr_snapshot`, `click_ocr_index`).
- Type arbitrary text into focused fields (`type_text`).
- Global **Ctrl+Q** abort hotkey to immediately interrupt a running task.

### 🌐 Browser Automation
- Full browser page reading and interaction via **Chrome DevTools Protocol (remote debugging)** for reliable navigation, clicking, and typing on complex web pages.
- Read the content of the current browser page (`read_browser_page`), list open tabs (`list_browser_tabs`), run arbitrary JavaScript in the page context (`run_js_in_browser`), and open new URLs (`open_url`).
- Falls back gracefully to UIA-based control when remote debugging isn't enabled.
- Live internet search (`search_internet`) via DuckDuckGo, with indexed result opening (`open_search_result`).

### 📁 Files & Documents
- Read and write local files directly (`read_local_file`, `write_local_file`, `append_local_file`).
- Smart file reading for large or complex files (`read_file_smart` / `read_file_chunk`) with automatic chunking, supporting `.txt`, `.md`, `.py`, `.json`, `.csv`, `.html`, `.pdf` (via `pymupdf`), and `.docx` (via `mammoth`).
- Generate real Word documents from Markdown-style text (`write_docx_file`, via `python-docx`).
- Browse and search the filesystem: list directories, open files/folders, and fuzzy-find files by name (`list_directory`, `open_path`, `find_file`, `open_path_by_index`).

### 🧠 Memory, Goals & Planning
- Persistent memory that Midum can read and update across a conversation (`update_memory`).
- Goal tracking, so Midum can set, remember, and clear an active objective (`set_current_goal`).
- A dedicated **response scratchpad** for multi-step tasks — Midum writes a plan first, appends progress notes as it works, and reads it back to assemble a final answer (`write_response_memory`, `append_response_memory`, `read_response_memory`).

### 📚 Skills & Knowledge Bases
- Create, load, and list reusable **skills** — self-contained instructions Midum can invoke for recurring tasks (`create_domain_skill`, `load_skill`, `list_skills`, `list_domain_skills`, indexed variants included).
- Create, read, and list custom **domain knowledge** files that act as a long-term reference library for specific topics (`create_domain_knowledge`, `read_domain_knowledge`, `list_domain_knowledge`).
- Manage a persistent instruction set that shapes Midum's behavior (`read_instructions`, `add_instruction`).
- Manage saved filesystem paths/shortcuts for quick access (`read_paths`, `add_path`, `get_path`, `list_paths_indexed`).
- Indexed listing variants for skills, knowledge, and paths keep large libraries easy to browse without flooding context.

### 🤖 Multi-Model & Multi-Provider Support
Midum isn't tied to a single model or provider — it can run its primary reasoning loop on any of the following, and can also consult or delegate sub-tasks to several of them side-by-side:
- **Ollama** (fully local, no internet required) — now **optional**.
- **Gemini**, directly supported via two paths:
  - **Gemini API** — the official Google Gemini API using an API key, with native structured tool-calling.
  - **Gemini Web** — sign-in via your Google account cookies (no API key required, no per-token metering), driven through a persistent `gemini_webapi` chat session.
- **OpenRouter** — run any OpenRouter-hosted model (including free-tier models) as the primary brain, with automatic fallback across a configurable list of models if one is rate-limited.
- **GroqCloud** — fast inference on models like `llama-3.3-70b-versatile` and `qwen/qwen3-32b`, with a genuine free tier and automatic model fallback.
- **Ollama Cloud** — the same `ollama` Python client as local Ollama, just pointed at Ollama's own hosted GPUs (`https://ollama.com`) with an API key instead of a local daemon — no GPU or model download required, with native tool-calling on models like `gpt-oss:120b-cloud` and `qwen3-coder:480b-cloud`, plus automatic fallback across a configurable model list.
- On-demand cross-consultation and delegation tools regardless of your primary provider: `consult_gemini`, `consult_gemini_api`, `consult_openrouter`, `consult_groq`, `consult_ollama_cloud`, `delegate_to_gemini_api`, `delegate_to_openrouter`, `delegate_to_groq`, `delegate_to_gemini_web`, `delegate_to_ollama_cloud`, plus tools to list and switch models per-provider on the fly (`list_openrouter_models`, `set_openrouter_model`, `list_groq_models`, `set_groq_model`, `set_gemini_api_model`, `set_gemini_web_model`, `list_ollama_cloud_models`, `set_ollama_cloud_model`, etc.).

### 🔗 Flows (visual automation builder)
The GUI's **Flows** tab is a node-graph editor (built on Drawflow) for building repeatable, shareable automations without writing any Python by hand:
- Drag out **Start**/**End** markers, native-tool nodes (`tool::<name>`), MCP-server tool nodes (`mcp::<server>::<tool>`), and nodes that call *other* saved flows (`flow::<name>`), then wire them together to define execution order.
- **Variable** nodes hold a named piece of data that can feed into any downstream node's parameters.
- **Logic nodes** — `if`/`else` branching and `for-each` loops — let a flow make decisions and iterate over lists instead of only running in a straight line.
- **AI nodes** — Prompt AI, Ask AI to Summarize, and Ask AI to Choose — let a flow call out to whichever provider is currently configured (`config.MODEL_PROVIDER`) mid-run, for one-off reasoning, summarization, or picking between options, without a full tool call.
- Saving a flow compiles the node graph straight into a real Python function, upserted into `flow_tools.py` between name-keyed markers (safe to re-save/edit repeatedly — resaving a flow only ever touches its own marked block), with its description and graph persisted to `flow_meta.json` so it can be reloaded into the canvas later.
- Every saved flow is automatically discoverable and callable as a tool (`list_flows_formatted` → `run_flow(name)`) — the same two-step shape as calling an MCP tool.
- **Promoted flows** — mark a flow as promoted from the Tools tab and it gets its own always-on tool schema, callable directly by name with no discovery step, exactly like a promoted MCP tool.
- **Scheduling** — saved flows can be scheduled to run automatically (once, on an interval, daily, or weekly) via `scheduler.py`, as long as Midum's GUI is open. Schedule *configuration* persists across restarts even though firing only happens while the app is running.

### 🔌 MCP (Model Context Protocol) Support
- Connect Midum to external MCP servers over stdio or HTTP/SSE (`connect_mcp_server`, `disconnect_mcp_server`).
- List connected servers and their available tools (`list_mcp_servers`, `show_server_tools`).
- Call any tool exposed by a connected MCP server directly (`call_mcp_tool`).
- Works uniformly across every model provider — the same MCP tools are available no matter which backend is driving Midum.

### 🎨 Generation Tools
- Generate images on request (`generate_image`).
- Generate flowcharts/diagrams (`create_flowchart`).

### 🗣️ Interaction & Control
- Midum can proactively ask you for input when it needs it: free text (`ask_user_text`), a file path (`ask_user_file_path`), explicit approval before a sensitive action (`ask_user_approval`), or a choice from a list of options (`ask_user_choice`).
- A `say` tool for clean, direct responses back to you.
- Tool discovery on demand — Midum can list and load additional native tools it hasn't already loaded into context (`list_native_tools`, `show_native_tool_schema`, `list_more_tools`, `load_tool_by_index`), keeping the active toolset lean for smaller/local models.
- A `wait` tool for timed pauses mid-task.

### 🖼️ Midum Control Centre (GUI)
Running `gui.py` gives you the full **Midum Control Centre**, a desktop app (built with `customtkinter`) on top of the same agent engine, including:
- A live chat interface with real-time activity/tool-call logging.
- A **Model** tab to pick your provider (Ollama / OpenRouter / Gemini Web / Gemini API / Groq / Ollama Cloud) and specific model, with live model list refreshing (e.g. querying your local Ollama installation).
- A **Parameters** tab showing live agent state: active model, current goal, workspace, Gemini research status, OCR availability, UIA availability, and turn count.
- A **System Core** tab for managing Midum's persistent instruction set.
- A **Knowledge** tab and dialog for creating and browsing domain knowledge files.
- A **Skills** tab and dialog for creating and browsing domain skills.
- A **Tools** tab for inspecting Midum's manual/native tools, and promoting/demoting saved Flows and MCP tools to always-on status.
- An **MCP** tab for adding, viewing, and managing connected MCP servers and their tools, including a dedicated "Add MCP Server" dialog (stdio or HTTP/SSE, with command/args/env or URL/header fields) and a tool-viewer dialog.
- A **Flows** tab: the node-graph editor described above, for visually building, saving, running, promoting, and scheduling automations (see 🔗 Flows above).
- Full editing access to every underlying file Midum uses — skill files, knowledge bases, memory files, instructions, and more — directly from the GUI.

This is the **recommended way to run Midum**, since it exposes everything the CLI does plus direct file editing.

---

## Setup Instructions

### Step 1
Download the ZIP file from the releases and extract it into an empty folder, or clone the repo into an empty folder. Once extracted, `cd` into the project folder — this is a proper Python package, so all commands below should be run **from inside it**, not from a parent directory:
```PowerShell
cd midum_pkg
```

### Step 2
Install the required Python libraries. The easiest way is via the included `requirements.txt`:
```PowerShell
pip install -r requirements.txt
```
If you'd rather install everything explicitly (or `requirements.txt` is ever out of date), this is the full list:
```PowerShell
pip install ollama pillow ddgs keyboard pymupdf mammoth python-docx rich pytesseract pywin32 uiautomation customtkinter google-genai requests mcp
pip install -U gemini_webapi
pip install -U browser-cookie3   # optional but recommended
```

### Step 3 (Optional — Ollama)
Ollama is now **optional**. Midum can run its primary reasoning loop directly on **Gemini** (via the official Gemini API or via `gemini_webapi` cookie sign-in) instead of a local model, so you can skip straight to Step 6 if you'd rather not run anything locally.

If you *do* want a fully local, offline-capable setup, download Ollama and pull a tool-calling-capable model of your choice (older models like `qwen2.5-coder` are also supported):
```PowerShell
ollama pull qwen2.5-coder:7b
```
Then change the first line of the `Modelfile` to configure it for the model of your choice (it currently defaults to **qwen2.5-coder:7b**), and apply it by running the following **in the folder the Modelfile is located**:
```PowerShell
ollama create midum -f ./Modelfile
```

### Step 4
Open `config.py` and set `MODEL_PROVIDER` to your provider of choice:
```python
MODEL_PROVIDER = "ollama"       # local Ollama model
MODEL_PROVIDER = "gemini_api"   # official Gemini API (API key from aistudio.google.com)
MODEL_PROVIDER = "gemini_web"   # Gemini via browser-cookie sign-in, no API key
MODEL_PROVIDER = "openrouter"   # any OpenRouter-hosted model
MODEL_PROVIDER = "groq"         # GroqCloud (free tier, fast inference)
MODEL_PROVIDER = "ollama_cloud" # Ollama's own hosted GPUs (API key from ollama.com, no local GPU needed)
```
Fill in the matching model/key settings just below `MODEL_PROVIDER` for whichever provider you chose (e.g. `GEMINI_API_MODEL`, `OPENROUTER_MODEL`, `GROQ_MODEL`).

### Step 5
Create your secrets file — a local JSON file that holds your API keys (Gemini, OpenRouter, Groq, Ollama Cloud) and/or Gemini web cookies, so Midum never needs them hardcoded in `config.py`. Run this once from inside the project folder:
```PowerShell
python -c "import config; created = config.ensure_secrets_file(); print('Created:' if created else 'Already exists:', config.SECRETS_FILE)"
```
This writes a ready-to-fill-in template (every value blank) to the platform-appropriate location — `%LOCALAPPDATA%\JarvisSecrets\jarvis_secrets.json` on Windows, `~/.config/JarvisSecrets/jarvis_secrets.json` on Linux — with the following keys:
```json
{
  "GEMINI_API_KEY": "",
  "OPENROUTER_API_KEY": "",
  "GROQ_API_KEY": "",
  "OLLAMA_API_KEY": "",
  "GEMINI_SECURE_1PSID": "",
  "GEMINI_SECURE_1PSIDTS": ""
}
```
Open the file and fill in only the key(s) your chosen `MODEL_PROVIDER` (and any providers you plan to consult/delegate to) actually needs — every reader treats a blank value as "not configured", so it's safe to leave the rest empty:
- `GEMINI_API_KEY` — from [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey). Used by `MODEL_PROVIDER = "gemini_api"` and Gemini API consultation.
- `OPENROUTER_API_KEY` — from [openrouter.ai/keys](https://openrouter.ai/keys). Used by `MODEL_PROVIDER = "openrouter"`.
- `GROQ_API_KEY` — from [console.groq.com/keys](https://console.groq.com/keys) (free tier). Used by `MODEL_PROVIDER = "groq"`.
- `OLLAMA_API_KEY` — from [ollama.com/settings/keys](https://ollama.com/settings/keys). Used by `MODEL_PROVIDER = "ollama_cloud"`.
- `GEMINI_SECURE_1PSID` / `GEMINI_SECURE_1PSIDTS` — browser session cookies (not an API key) for `gemini.google.com`, copied from your browser's DevTools → Application → Cookies. Both are required together, and only needed if you'd rather use your Gemini account's web session (`MODEL_PROVIDER = "gemini_web"`) than the official API.

This same file is also created automatically the first time you run `main.py` or `gui.py` if it doesn't exist yet (see Step 6 below), so this step is optional — but running it explicitly here lets you fill in your keys *before* first launch instead of stopping mid-startup to go find the file.

### Optional Step — OCR
Download and install Tesseract, as it enables OCR-based screen reading and clicking. Midum is completely functional without OCR — it's only used as a fallback for UI interaction.

### Step 6 — Launch Midum
- Run the **`gui.py`** script if you want to run the **Midum Control Centre**, which gives you the full functionality of Midum plus the ability to modify any underlying file (skill files, knowledge bases, memory files, etc.) directly in the app. This is the **recommended** approach.
- Run the **`main.py`** script if you prefer the CLI tool instead. This requires an IDE if you want to modify any files yourself (or you can just tell Midum to do it).

Both are run the same way, from inside the project folder:
```PowerShell
python gui.py
# or
python main.py
```

---

## Alternative Setup — Prebuilt Executable
If you'd rather not install Python or any dependencies, Windows users can skip the full Setup Instructions above entirely and just:

### Step 1
Download `midum.exe` from the releases page and place it in its own empty folder (it will create `storage/` and other supporting files alongside itself on first run, so give it a dedicated folder rather than dropping it in `Downloads`).

### Step 2
Launch `midum.exe` once (double-click it, or run it from a terminal) and then close it again. On this first run it automatically creates your secrets file — the same blank-template JSON described in Step 5 above — at `%LOCALAPPDATA%\JarvisSecrets\jarvis_secrets.json`, with the `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `OLLAMA_API_KEY`, `GEMINI_SECURE_1PSID`, and `GEMINI_SECURE_1PSIDTS` placeholders ready for you to fill in. Open that file, fill in only the key(s) your chosen provider needs (see the key-by-key breakdown in Step 5 above), and save it.

Relaunch `midum.exe` and you're straight into the Midum Control Centre — no `pip install`, no Ollama, no IDE required.

---

## Browser Support
For reliable browser page navigation and interaction, launch your browser with **Remote Debugging** enabled.

Without Remote Debugging, Midum falls back to UI Automation (UIA), which works for many tasks but may be less reliable on complex web pages.

### Example (Chrome/Brave/Edge)
On Windows, use this command (replace the path with your actual browser executable path):
```PowerShell
& "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" --remote-debugging-port=9222 --remote-allow-origins=*
```
Midum will automatically use the debugging interface when available and fall back to UIA otherwise.

---

## Models I Have Tested and Am Happy With
1. `qwen2.5-coder:7b` (Ollama)
2. `qwen3.5:4b` (Ollama)
3. Gemini (via `gemini_api` and `gemini_web`)
4. `llama-3.3-70b-versatile` (Groq)
