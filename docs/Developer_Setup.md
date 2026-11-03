# Midum

<img width="1408" height="768" alt="image" src="https://github.com/user-attachments/assets/cbba478e-cd41-4eac-97c8-2ea2790eeaf5" />


Midum is an agentic framework built for local AI desktop assistance. It can help you with code, UI automation, repetitive tasks, browsing, research, file management, and more — all driven by a tool-calling AI agent that runs on **your** machine. It also builds: it edits your code and documents inside agent-aware editors, writes typeset LaTeX, authors versioned artifacts, and drops interactive blocks straight into the chat.

<img width="1280" height="800" alt="image" src="https://github.com/user-attachments/assets/4e26d709-33fc-42f8-b23b-b07d18286efa" />


<img width="1276" height="779" alt="image" src="https://github.com/user-attachments/assets/e4c30848-f9e4-4265-a871-8dff452e5575" />

Created by Parth Satija, maintained by Parth Satija in collaboration with Kairos Syndicate.

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
- **PDF Sources** — register a PDF (path/title/page-count only, via PyMuPDF), then manually tag heading lines in the Knowledge tab's PDF Heading Tagger (click a line on the real rendered page, assign it a heading level H1–H6). No structure is ever auto-detected; every heading comes from a line the user personally tagged. Once tagged, pick which heading level(s) act as "part" boundaries for that source.
- **KB Only mode** — a toggle in the chat input's Knowledge Base popover that restricts a message to only the selected PDF source(s): internet search and browsing tools are disabled for that turn, and the model is told to answer strictly from the tagged source material (or say plainly that the sources don't cover it). Selection is in-memory only and resets on restart.
- **Explain Mode** — rides on top of KB Only. Instead of just outlining headings, it walks the selected source(s) part-by-part (using the part-boundary levels set in the Knowledge tab), giving the model the exact real text of the *current* part only, with an explicit "part N of M" header. The model is instructed to narrate it like a guided walkthrough (NotebookLM-style "Deep Dive" narration) — synthesizing into flowing explanation rather than reading line-by-line — while guaranteeing every detail in that part surfaces somewhere. Hit **▶ Start Explanation** in the KB popover to kick it off, and either say "next"/"continue" or use the **Next Part →** button to advance; progress per source is tracked server-side for the current chat.

### 🤖 Multi-Model & Multi-Provider Support
Midum isn't tied to a single model or provider — it can run its primary reasoning loop on any of the following, and can also consult or delegate sub-tasks to several of them side-by-side:
- **Ollama** (fully local, no internet required) — now **optional**.
- **Gemini**, directly supported via two paths:
  - **Gemini API** — the official Google Gemini API using an API key, with native structured tool-calling.
  - **Gemini Web** — sign-in via your Google account cookies (no API key required, no per-token metering), driven through a persistent `gemini_webapi` chat session.
- **OpenRouter** — run any OpenRouter-hosted model (including free-tier models) as the primary brain, with automatic fallback across a configurable list of models if one is rate-limited.
- **GroqCloud** — fast inference on models like `llama-3.3-70b-versatile` and `qwen/qwen3-32b`, with a genuine free tier and automatic model fallback.
- **Ollama Cloud** — the same `ollama` Python client as local Ollama, just pointed at Ollama's own hosted GPUs (`https://ollama.com`) with an API key instead of a local daemon — no GPU or model download required, with native tool-calling on models like `gpt-oss:120b-cloud` and `qwen3-coder:480b-cloud`, plus automatic fallback across a configurable model list.
- **LiteLLM** — one connector to 100+ providers (any model string LiteLLM supports), with per-provider keys and base URLs managed from the app (`MODEL_PROVIDER = "litellm"`).
- **OmniRoute** — a self-hosted gateway, often usable with no key for a purely local setup (`MODEL_PROVIDER = "omniroute"`; `consult_omniroute`, `delegate_to_omniroute`, `list_omniroute_models`, `set_omniroute_model`).
- **ChatGPT Web** — a ChatGPT browser-session backend (`MODEL_PROVIDER = "openai_web"`, `delegate_to_openai_web`). Like Gemini Web it reuses a signed-in session rather than an official API.
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

### 🎙️ Voice Control (Gemini Live)
Talk to Midum out loud and have it talk back, with full tool-calling parity with text chat:
- Real-time, bidirectional speech-to-speech control via Google's official **Gemini Live API** (`google-genai`'s `client.aio.live.connect`) — a persistent WebSocket session, separate from every other single-shot provider, that streams your microphone audio to Gemini continuously and plays back its spoken replies as they arrive.
- Every tool call the model makes while in voice mode runs through the exact same dispatcher as text chat and the manual tool sandbox, so voice control has 100% tool parity — files, terminal, UI automation, browser, MCP servers, everything.
- Two independently rebindable **global push-to-talk hotkeys** (keyboard key or mouse button, including side buttons) that work no matter which window has focus — hold to stream your mic, release to mute. The very first press opens the Gemini Live connection; the connection then stays open across presses so later ones resume instantly instead of reconnecting. Bindings persist to `storage/ptt_hotkeys.json` and default to **Right Alt** and **Mouse Button 4 (Side)**.
- The voice model cannot write front-end code, so surface-building is delegated: `get_chat_surface_made`, `get_ambient_surface_made` and `get_render_surface_made` hand the request to the default text model and open the result. Tools that take model-written code (`write_render_code`, `show_chat_surface`, `show_ambient_surface`, `create_dynamic_background`, and their edit variants) are blocked from voice calls.
- Voice mode can also record finished narration clips — see 🗣️ Voiceover Studio below.
- Setup: `pip install google-genai sounddevice numpy pynput`, then add your `GEMINI_API_KEY` to the shared secrets file (the same key used by `MODEL_PROVIDER = "gemini_api"` — if that's already configured, voice control works with zero extra setup).

### 🔌 MCP (Model Context Protocol) Support
- Connect Midum to external MCP servers over stdio or HTTP/SSE (`connect_mcp_server`, `disconnect_mcp_server`).
- List connected servers and their available tools (`list_mcp_servers`, `show_server_tools`).
- Call any tool exposed by a connected MCP server directly (`call_mcp_tool`).
- Works uniformly across every model provider — the same MCP tools are available no matter which backend is driving Midum.

### 🎨 Generation Tools
- Generate images on request (`generate_image`).
- Generate flowcharts/diagrams (`create_flowchart`).

### 🗣️ Interaction & Control
- Midum can proactively ask you for input when it needs it: free text (`ask_user_text`), a file path (`ask_user_file_path`), explicit approval before a sensitive action (`ask_user_approval`), or a choice from a list of options (`ask_user_choice`). In the GUI these render as inline cards in the chat rather than separate windows.
- A `say` tool for clean, direct responses back to you.
- Tool discovery on demand — Midum can list and load additional native tools it hasn't already loaded into context (`list_native_tools`, `show_native_tool_schema`, `list_more_tools`, `load_tool_by_index`), keeping the active toolset lean for smaller/local models.
- A `wait` tool for timed pauses mid-task.

### 🤖 Multi-Agent System
- Create named background agents, each with its own history, system prompt and optional model (`start_agent`, `stop_agent`, `resume_agent`, `forget_agent`, `list_agents`).
- Assign work without blocking (`send_agent_task`) and read results back (`get_agent_report`); completed reports are pushed into the supervising conversation automatically.
- Attach per-agent knowledge bases and skills (`attach_agent_knowledge`, `attach_agent_skills`, `list_agent_knowledge`, `list_agent_skills`), refreshed from disk on every task.
- Agents can alert the user mid-task (`tell_supervisor_to_inform_user`) or hand work up to the supervisor (`delegate_agent_task_to_supervisor`).
- Optional persistence so agents survive restarts; every running agent has a live tab in the **Agents** pane.

### 💬 Discord & Action Loops
- Run a Discord bot for DMs and server channels (`start_discord_bot`, `message_discord`, `get_discord_chat_history`, `discord_upload_file`, `list_discord_servers`, `list_discord_sessions`).
- Join a voice channel for a real-time, interruptible conversation with multiple speakers (`call_discord`, `end_discord_call`, `get_discord_call_status`, `get_discord_call_transcript`, `list_discord_calls`). Add `DISCORD_BOT_TOKEN` (and optionally a default voice channel ID) in the secrets file or Settings.
- Start a continuous **action loop** (`start_action_loop`, `stop_action_loop`) and steer it live: anything you type while it runs is delivered after the current tool call as an interjection, without starting a new turn.
- Share your screen with the live voice model (`start_screen_share`, `stop_screen_share`).

### 🧩 Generative Surfaces
Four kinds of model-written HTML/CSS/JS surface, all sandboxed. Chat, Render and Ambient Surfaces can call any Midum tool, MCP tool or Flow through `await midum.call(name, args)` (same permissions as the agent).
- **Chat Surfaces** — interactive blocks placed inline in the transcript (`show_chat_surface`, `edit_chat_surface`, `close_chat_surface`, `list_chat_surfaces`). Width is fixed to the chat column and height auto-fits the content (cap 640 px by default, or a fixed 60–1400 px); the block receives the app theme as CSS variables. `parent.postMessage(data, '*')` sends input back as a new chat turn (`[Chat Surface input: <id>]`), `midum.close()` dismisses it, and reusing a `surface_id` replaces it in place. Blocks are stored in the chat's display log, so they persist and replay when a chat is reopened.
- **Render Surfaces** — a side panel for larger apps, tools, visualizations and games (`write_render_code`, `edit_render_code`, `save_render_surface`, `load_render_surface`, `list_saved_render_surfaces`, `delete_saved_render_surface`, `close_render_surface`).
- **Ambient Surfaces** — floating, focus-free desktop widgets at exact screen positions, up to eight at once, hidden from Midum's own screenshots by default (`show_ambient_surface`, `edit_ambient_surface`, `move_ambient_surface`, `save_ambient_surface`, `load_ambient_surface`, `list_ambient_surfaces`, `list_saved_ambient_surfaces`, `delete_saved_ambient_surface`, `close_ambient_surface`).
- **Dynamic Backgrounds** — an animated app-wide backdrop (`create_dynamic_background`, `edit_dynamic_background`, `save_dynamic_background`). There is deliberately no tool to switch between saved ones; the user picks from the sidebar's Dynamic Backgrounds button. Runs in a scripts-only sandbox with no tool access.

### ✍️ Code Editor & IDE
The **Editor** tab is a Monaco-based **Simple Editor** (any file on disk, ~80 languages, Run button for `.py`/`.java`, integrated terminal on the shared persistent shells) that can switch into a full **Workbench Editor** — a VS Code workbench built on `monaco-vscode-api` with Open VSX extensions, a token-protected local file API, and Midum chat docked in its right sidebar. The two hand sessions over in both directions.
- **AI inline suggestions** — Copilot-style ghost text (Tab accepts, Ctrl+Right one word, Esc dismisses, Alt+\ on demand) from Gemini 3.1 Flash-Lite, with line replace/delete shown as a diff, `ai:` instruction comments, round-robin rotation across up to five Gemini keys, and only the text near the cursor sent. `.env*`, `*secret*`, `*.pem`, `*.key` and similar files are never sent.
- **Agent link** — `open_file_in_editor`, `open_folder_in_editor`, `editor_workspace_tree`, `editor_read_active_file`, `editor_edit_active_file`, `edit_file_in_editor`. Every edit is shown as a diff first (default Reject, five-minute timeout); six exact edit kinds are applied all-or-nothing against the text the agent last read, and stale edits are refused. Generic file-write tools and file-modifying MCP calls are refused on files open in the Editor.
- All editor servers are localhost-only with per-session tokens. Monaco (~18 MB) is downloaded once and cached. The Workbench needs a one-time local build (see Setup).

### 📎 File Uploads & Artifacts
- **Uploads** — attach, paste or drag and drop any file (100 MB default). Files are copied in, hashed (duplicates stored once), classified, and text-extracted in the background (PDF via PyMuPDF with scanned-PDF detection, `.docx`, `.pptx`, `.xlsx`/`.xlsm`, CSV/text/code with encoding detection). The agent reads them with `list_artifacts`, `read_artifact`, `search_artifact`, `view_artifact`, `analyze_artifact` (native Gemini analysis of scans, audio and video) and `add_artifact_from_path`. Attachments are never executed or edited in place.
- **Artifacts** — persistent files the agent authors itself (`html`, `markdown`, `svg`, `mermaid`, `react`, `latex`, code and text) with live preview and the latest 30 versions kept: `create_artifact`, `update_artifact` (exact-match, all-or-nothing), `rewrite_artifact`, `open_artifact`, `open_in_editor`, `list_artifact_versions`, `revert_artifact`. Edits made by the user in either editor are version-checked so they never overwrite an agent change, and vice versa.

### 📄 MidumDocs (LaTeX)
- The agent writes and edits LaTeX (papers, reports, CVs, letters, Beamer slides), shown compiled beside the chat. A MidumDoc is an artifact of type `latex`, so it is versioned like any other.
- Compiles locally and offline with your installed TeX distribution (TeX Live, MiKTeX or Tectonic; pdfLaTeX by default, xelatex and lualatex supported), up to three passes, shell-escape disabled, 180 s timeout.
- **Click-to-edit** text directly on a rendered page; edits map back to the source (math, tables and split text are refused with an "Ask Midum" shortcut). Ctrl/Cmd+K sends a request with the cursor or selection frozen.
- Tools: `create_midumdoc`, `midumdoc_state`, `midumdoc_replace_selection`, `midumdoc_insert_at_cursor`, `list_midumdocs`, `open_midumdoc`, plus the standard artifact tools. The agent receives your live cursor, selection and compile status each step and checks that the document compiles after editing.

### ✅ Step Prompting
A per-chat checklist that Midum works through in order. Define steps from the checklist button in the prompt box, or let the model write its own with `write_steps` (at least two steps; locked while your list is open).
- `complete_step(step, proof)` must be called in order; up to 60 steps of 400 characters each.
- **Verification** (optional): each step needs a concrete proof; short, generic or step-repeating proofs are rejected.
- **Per-turn limit** (optional): the checklist pauses when the cap is hit and resumes in a later turn.
- A plain-text reply with steps remaining is nudged back into working (max two nudges per turn); a blocked step is left unchecked.
- State persists per chat (`storage/steps/<chat_id>.json`), reusable lists can be saved as templates, and Edit/Regenerate rewinds the checklist. Main text chat only — voice and sub-agents never see it.

### 🗣️ Voiceover Studio
Record polished narration from a text script using the same Gemini Live voice as voice mode, independent of any voice session. Each segment is recorded as its own take and checked against the script, with up to three automatic re-records. `[pause 1.5s]` lines insert silence. Revisions re-record only changed segments. Clips stay in memory until saved as 24 kHz mono `.wav`; an audio player above the prompt box controls playback.
- Tools: `record_voiceover`, `revise_voiceover`, `undo_voiceover_change`, `play_voiceover`, `stop_voiceover`, `save_voiceover`, `list_voiceovers`, `discard_voiceover`.

### 🖼️ Midum Control Centre (GUI)
Running `gui.py` gives you the full **Midum Control Centre**, a frameless desktop window (pywebview on Qt, with a custom titlebar and native Windows snapping) on top of the same agent engine, including:
- A live chat interface with real-time activity/tool-call logging, expandable tool-call rows, interjection and abort, and inline Chat Surfaces and step-checklist cards.
- **Conversation tabs** (browser-style, with automatic titles, saved tab groups and session restore) and a **left sidebar** with New Chat, saved Render Surfaces, Dynamic Backgrounds, a Workspace/project picker with file list, searchable chat history and Settings.
- A **top tab bar** with ten tabs: Chat, Library, MidumDocs, Tools, Shell, Flows, Agents, MCP, Permissions, Editor.
- A **prompt box** with a Models/Source popover (provider and model for Normal and Voice, KB Only, PDF sources, Explain Mode), file attach, a step-checklist button, and the `#` path and `/` capability pickers.
- A **Settings** popup (General, Appearance, Background & Glass, Models, Chats): themes and presets, ambient blobs, background image, glass surfaces, chat text size, push-to-talk keys, API keys (written to the secrets file, shown masked), default model, saved-chat cleanup by date, count or age, the Log and Parameters panels, and the setup guide.
- A first-run **setup wizard** (Welcome → Gemini keys → first chat).
- A **Model** popover on the prompt box to pick your provider (Ollama / OpenRouter / Gemini Web / Gemini API / Groq / Ollama Cloud / OmniRoute / LiteLLM / ChatGPT Web) and specific model, for normal and voice, with live model list refreshing (e.g. querying your local Ollama installation).
- A **Parameters** panel (opened from Settings) showing live agent state: active model, current goal, workspace, Gemini research status, OCR availability, UIA availability, and turn count.
- A **Library** tab with a segmented switcher: **System Core** (the persistent instruction set), **Knowledge** (domain knowledge files and PDF Sources) and **Skills** (domain skills), each with creation and browsing dialogs.
- A **Shell** tab with the persistent WSL and PowerShell shells shared by you and the agent, with live output streaming.
- An **Agents** tab for monitoring and managing live background agents (see 🤖 Multi-Agent System above).
- A **Tools** tab for inspecting Midum's manual/native tools, and promoting/demoting saved Flows and MCP tools to always-on status.
- An **MCP** tab for adding, viewing, and managing connected MCP servers and their tools, including a dedicated "Add MCP Server" dialog (stdio or HTTP/SSE, with command/args/env or URL/header fields) and a tool-viewer dialog.
- A **Flows** tab: the node-graph editor described above, for visually building, saving, running, promoting, and scheduling automations (see 🔗 Flows above).
- **Voice** controls (in the prompt-box popover and Settings) for enabling push-to-talk voice control, rebinding the two global hotkeys, and monitoring live connection status (see 🎙️ Voice Control above).
- An **Editor** tab (Simple Editor or full Workbench IDE) and a **MidumDocs** tab for LaTeX documents (see above).
- A **Permissions** tab for per-tool levels: Always, Ask, Ask with password, or Deny (denied tools are removed from the model's view).
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
pip install ollama pillow ddgs keyboard pymupdf mammoth python-docx rich pytesseract pywin32 uiautomation customtkinter google-genai requests mcp pywebview PySide6 qtpy litellm loguru pyperclip websocket-client pygments pdfstructx llm-cookie-bridge edge-tts SpeechRecognition PyNaCl "discord.py[voice]" discord-ext-voice-recv
pip install -U gemini_webapi
pip install -U browser-cookie3   # optional but recommended
pip install sounddevice numpy pynput   # optional, needed for Voice Control (Gemini Live push-to-talk)
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
MODEL_PROVIDER = "omniroute"    # self-hosted OmniRoute gateway
MODEL_PROVIDER = "litellm"      # any provider LiteLLM supports (100+)
MODEL_PROVIDER = "openai_web"   # ChatGPT via browser-session sign-in
```
Fill in the matching model/key settings just below `MODEL_PROVIDER` for whichever provider you chose (e.g. `GEMINI_API_MODEL`, `OPENROUTER_MODEL`, `GROQ_MODEL`).

### Step 5
Create your secrets file — a local JSON file that holds your API keys (Gemini, OpenRouter, Groq, Ollama Cloud) and/or Gemini web cookies, so Midum never needs them hardcoded in `config.py`. Run this once from inside the project folder:
```PowerShell
python -c "import config; created = config.ensure_secrets_file(); print('Created:' if created else 'Already exists:', config.SECRETS_FILE)"
```
This writes a ready-to-fill-in template (every value blank) to the platform-appropriate location — `%LOCALAPPDATA%\JarvisSecrets\jarvis_secrets.json` on Windows, `~/.config/JarvisSecrets/jarvis_secrets.json` on Linux, `~/Library/Application Support/JarvisSecrets/jarvis_secrets.json` on macOS — with the following keys (this is the full, always-current template — it's generated straight from `config.SECRETS_TEMPLATE`, so it never drifts out of sync with what the code actually reads):
```json
{
  "GEMINI_API_KEY": "",
  "GEMINI_API_KEY_2": "",
  "GEMINI_API_KEY_3": "",
  "GEMINI_API_KEY_4": "",
  "GEMINI_API_KEY_5": "",
  "OPENROUTER_API_KEY": "",
  "GROQ_API_KEY": "",
  "OLLAMA_API_KEY": "",
  "OMNIROUTE_API_KEY": "",
  "GEMINI_SECURE_1PSID": "",
  "GEMINI_SECURE_1PSIDTS": ""
}
```
The template shown above is abbreviated: newer builds also store `LITELLM_PROVIDERS` (per-provider keys for LiteLLM), `CHATGPT_SESSION_TOKEN` (plus `_0` / `_1` chunk fields), `DISCORD_BOT_TOKEN` and `DISCORD_DEFAULT_VOICE_CHANNEL_ID`, and `config.SECRETS_TEMPLATE` is the authoritative list. You can also skip editing the file by hand: keys can be pasted into **Settings** (API keys) or the first-run wizard, and Midum writes them for you and only ever shows a masked preview.

Open the file and fill in only the key(s) your chosen `MODEL_PROVIDER` (and any providers you plan to consult/delegate to) actually needs — every reader treats a blank value as "not configured", so it's safe to leave the rest empty:
- `GEMINI_API_KEY` — from [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey). Used by `MODEL_PROVIDER = "gemini_api"` and Gemini API consultation.
- `GEMINI_API_KEY_2` / `_3` / `_4` / `_5` — optional extra AI Studio keys, tried in order whenever the current one comes back quota-exhausted (HTTP 429 / RESOURCE_EXHAUSTED). Handy for stacking multiple free-tier keys; leave blank if you only have one.
- `OPENROUTER_API_KEY` — from [openrouter.ai/keys](https://openrouter.ai/keys). Used by `MODEL_PROVIDER = "openrouter"`. *(Provider not recommended)*
- `GROQ_API_KEY` — from [console.groq.com/keys](https://console.groq.com/keys) (free tier). Used by `MODEL_PROVIDER = "groq"`. *(Provider not recommended)*
- `OLLAMA_API_KEY` — from [ollama.com/settings/keys](https://ollama.com/settings/keys). Used by `MODEL_PROVIDER = "ollama_cloud"`.
- `OMNIROUTE_API_KEY` — gateway key from your OmniRoute dashboard (`http://localhost:20128/dashboard`). Used by `MODEL_PROVIDER = "omniroute"`; often optional for a purely local gateway with no auth configured, so leave blank if you haven't set one.
- `GEMINI_SECURE_1PSID` / `GEMINI_SECURE_1PSIDTS` — browser session cookies (not an API key) for `gemini.google.com`, copied from your browser's DevTools → Application → Cookies. Both are required together, and only needed if you'd rather use your Gemini account's web session (`MODEL_PROVIDER = "gemini_web"`) than the official API.

This same file is also created automatically the first time you run `main.py` or `gui.py` if it doesn't exist yet (see Step 6 below), so this step is optional — but running it explicitly here lets you fill in your keys *before* first launch instead of stopping mid-startup to go find the file.

### Optional Step — OCR (Very Highly Recommended)
Download and install Tesseract, as it enables OCR-based screen reading and clicking. Midum is completely functional without OCR — it's only used as a fallback for UI interaction.

### Optional Step — Node.js (Workbench IDE)
The Editor tab's full VS Code workbench is a TypeScript project built once on your machine. Install **Node.js 18 or newer** and keep an internet connection for the first build (roughly 2–5 minutes). The result is cached and rebuilt only when its sources change. Without Node.js you keep the full Simple Editor.

### Optional Step — LaTeX (MidumDocs)
Install a TeX distribution (TeX Live, MiKTeX or Tectonic). MidumDocs compiles locally and offline (pdfLaTeX by default; xelatex and lualatex supported). Without one, the MidumDocs pane shows installation guidance and everything else is unaffected.

### Optional Step — Gemini-powered features
Voice mode, editor inline suggestions, native file analysis (scans, audio, video) and Voiceover Studio use the Gemini API. Add at least one `GEMINI_API_KEY` (up to five rotate automatically on quota errors). Everything else works with any provider.

### Step 6 — Launch Midum
- Run the **`gui.py`** script if you want to run the **Midum Control Centre**, which gives you the full functionality of Midum plus the ability to modify any underlying file (skill files, knowledge bases, memory files, etc.) directly in the app. This is the **recommended** approach. On a true first launch a setup wizard opens automatically (Welcome → Gemini API keys → first chat); it can be re-run from Settings.
- Run the **`main.py`** script if you prefer the CLI tool instead. This requires an IDE if you want to modify any files yourself (or you can just tell Midum to do it).

Both are run the same way, from inside the project folder:
```PowerShell
python gui.py
# or
python main.py
```

---

## macOS Setup Instructions
Midum runs on macOS too, following the same Setup Instructions above with a few platform-specific swaps below. Steps not mentioned here (1, 3, 4, 6) are identical to Windows — just run the same commands in Terminal instead of PowerShell.

### Step 2 (macOS) — Install Python libraries
Install requirements the same way:
```bash
pip install -r requirements.txt
```
`requirements.txt` is already platform-conditional — Windows-only packages (`pywin32`, `uiautomation`, `comtypes`) are skipped automatically on macOS, and the macOS-only UI automation packages are installed instead:
```bash
pip install pyobjc-framework-ApplicationServices pyobjc-framework-Quartz
```
(These two are what let Midum read the accessibility tree and simulate clicks/keystrokes on macOS — they're the Mac equivalent of `uiautomation`/`pywin32` on Windows.)

### Step 5 (macOS) — Secrets file location
Same command as Step 5 above:
```bash
python -c "import config; created = config.ensure_secrets_file(); print('Created:' if created else 'Already exists:', config.SECRETS_FILE)"
```
On macOS this writes the template to `~/Library/Application Support/JarvisSecrets/jarvis_secrets.json` (the standard macOS location for per-app data, matching Linux's `~/.config` and Windows' `%LOCALAPPDATA%` equivalents), with the same full set of keys as Step 5 above:
```json
{
  "GEMINI_API_KEY": "",
  "GEMINI_API_KEY_2": "",
  "GEMINI_API_KEY_3": "",
  "GEMINI_API_KEY_4": "",
  "GEMINI_API_KEY_5": "",
  "OPENROUTER_API_KEY": "",
  "GROQ_API_KEY": "",
  "OLLAMA_API_KEY": "",
  "OMNIROUTE_API_KEY": "",
  "GEMINI_SECURE_1PSID": "",
  "GEMINI_SECURE_1PSIDTS": ""
}
```
See the key-by-key breakdown in Step 5 above for what each one is and which `MODEL_PROVIDER` needs it.

### Required macOS permissions
Grant these in **System Settings → Privacy & Security** *before* launching Midum, or UI automation and screenshots will silently fail or return blank/black results instead of raising a clear error:
- **Accessibility** — enable your terminal app (Terminal/iTerm) or Python interpreter. Required for `click_ui_element`, `manual_scan_app_layouts`, `type_text`, and all other UI-automation tools — without it, the accessibility tree comes back empty.
- **Screen Recording** — enable the same app. Required for `fallback_view_screen`, `ocr_snapshot`, and any other screenshot-based tool — without it, `ImageGrab.grab()` returns a black image with no error.

If you package Midum into a standalone `.app` later, grant these permissions to the `.app` itself instead of the terminal.

### Optional Step — OCR on macOS
Install Tesseract via Homebrew instead of the Windows installer:
```bash
brew install tesseract
```
Midum checks `PATH` first, then falls back to checking `/opt/homebrew/bin/tesseract` (Apple Silicon) and `/usr/local/bin/tesseract` (Intel) directly — so it should be found automatically either way, even if Midum was launched from a GUI/launcher without a login shell's `PATH`.

---

## Alternative Setup — Prebuilt Executable
If you'd rather not install Python or any dependencies, Windows users can skip the full Setup Instructions above entirely and just:

### Step 1
Download `midum.exe` from the releases page and place it in its own empty folder (it will create `storage/` and other supporting files alongside itself on first run, so give it a dedicated folder rather than dropping it in `Downloads`).

### Step 2
Launch `midum.exe` once (double-click it, or run it from a terminal) and then close it again. On this first run it automatically creates your secrets file — the same full blank-template JSON shown in Step 5 above (every key: `GEMINI_API_KEY`, `GEMINI_API_KEY_2`–`_5`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `OLLAMA_API_KEY`, `OMNIROUTE_API_KEY`, `GEMINI_SECURE_1PSID`, `GEMINI_SECURE_1PSIDTS`) — at `%LOCALAPPDATA%\JarvisSecrets\jarvis_secrets.json`. Open that file, fill in only the key(s) your chosen provider needs (see the key-by-key breakdown in Step 5 above), and save it.

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
