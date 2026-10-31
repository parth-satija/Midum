# GUI & Frontend Architecture

The graphical user interface of `midum_pkg` is built primarily with PyQt/PySide and modern web technologies rendered via QWebEngineView/Render Surface panels.

## 1. `gui/app.py`
- **Purpose**: Main application window and Qt event loop setup.
- **Features**:
  - Tabbed interface management.
  - Sidebar orchestration for tools, agent controls, and settings.

## 2. `gui/chat_store.py`
- **Purpose**: Manages persistence and retrieval of chat session logs stored under `storage/chats/`.
- **Features**:
  - JSON-based chat message serialization and history loading.

## 3. `gui/dispatch.py`
- **Purpose**: Bridges asynchronous backend tool outputs with the Qt UI thread using signals and slots to ensure thread safety.

## 3b. Tool-call pills (chat pane, in `gui/app.py`'s embedded HTML/JS)
- **Grouping**: every stretch of tool calls is rendered as ONE `.tool-group` row with a `Ran N tools` pill (`.tg-pill`). The group keeps absorbing calls until Midum `say()`s / replies, the user speaks, an error or ask-card appears, or the turn finishes (`closeToolGroup()`); the next tool call then starts a new group. `say` itself never gets a pill.
- **Expanding**: clicking the main pill drops the group into one `.tc-pill` per call (name, first-argument preview, duration, status dot). Clicking a call pill opens its glass panel with the exact **Call** and the tool **Return** (each with a copy button).
- **Live flow**: `appendRow("tool", ...)` adds a pending pill; `attachToolCallDetail()` fills it when the `tool_call` event lands. Sub-agent calls (`agent_tool_call`) are intentionally not shown in the pills. A `tool_start` event (pushed by `orchestration.py` right before a tool runs) fills the pill's Call box immediately, before the result arrives.
- **Replay**: persisted `toolcall` display-log records are folded into the same groups by `buildTurnBlock()`.
- **Styling**: base frosted-chip look plus a Liquid Glass block that reuses the shared `--glass-*` tokens (no `backdrop-filter`).

## 3c. Icons (chat pane, in `gui/app.py`'s embedded JS/CSS)
- **One icon set**: every emoji/pictograph in the UI is drawn from a single stroke-icon set (24px grid, 2px round strokes, `currentColor`) defined once in the script as `_ICONS`. Use `icon("name")` inside `innerHTML`/templates (sized in `em`, so it scales with its text and follows theme/hover colours); static markup uses inline `<svg class="ic ...">`.
- **Legacy / Python-sent emoji**: `iconizeHtml()` swaps known emoji for icons inside already-escaped HTML, and `iconHtml()` accepts an icon name, ready-made svg, or an emoji. Log lines, system rows and the `icon` fields sent from Python still carry emoji and are converted at render time. Model chat text is deliberately left untouched.
- **Log pane** is a scrollable `div` (not a `textarea`) so it can hold icons.
- **Add an icon**: add its `<path>` markup to `_ICONS` (and, if it should replace an emoji, to `_EMOJI_ICON`).

## 3d. Chat text size (chat pane only)
- **Setting**: SETTINGS → **Chat Text Size** — a 10–32 px slider (0.05 px steps) plus an exact-value number box and a Reset button. Shift+Arrow nudges by 1 px. Stored as `chat_font_size` in `storage/gui_settings.json` (default 15).
- **Scope**: one CSS custom property, `--chat-fs`, set by `applyChatFontSize()`. Only chat-pane rules read it (bubbles, tool-call pills, code/tables, ask cards, prompt box); the sidebar, tool panes, settings and titlebar are untouched. The smaller text tiers (`--cfs-xs/sm/md/lg`) are fixed ratios of `--chat-fs` with legibility floors, and `--chat-sub` is the brighter secondary-text colour used for labels, timestamps and previews. All of it lives in the `Chat text size + readability` CSS block in `gui/app.py` (placed after the original chat rules so it supersedes their old hardcoded sizes).
- **Persistence**: the slider repaints live and calls `Api.set_chat_font_size` (debounced) which writes only that key — it intentionally does not go through `save_settings()`, which would also re-apply the saved provider/model. `save_settings()` also accepts the value.
- **Sizing**: pills, ask-card buttons and the prompt box use `min-height`/`calc()` rather than fixed pixel heights so nothing clips at large sizes; `autosizeMsgInput()` derives its ~10-line cap from the live line height.

## 3e. Glass Surfaces in the iframe panes and upload cards
- **Iframe panes** (MidumDocs, Editor): they are separate documents, so they can't see `html.liquid-glass-on`. `_glassThemeVars()` (in `gui/app.py`) adds `--mz-glass-on` / `--mz-glass-pct` / `--mz-glass-sheen` to the theme payload that `_mdTheme()` and `mzEdThemeVars()` already push on every `class`/`style` change of `<html>`, so toggling Glass Surfaces, moving the opacity slider or flipping the frosted highlight updates both panes live. The `--mz-*` keys are control values only; the pages never apply them as CSS.
- **Hosts**: `#mz-ed-host`, its iframe and `#midumdocs-frame` are forced `transparent` under `html.liquid-glass-on`, and `#midumdocs-header` / `.md-seg` get the shared glass recipe.
- **Upload cards**: `.att-chip` (prompt-box tray) and `.att-chip-ro` (inside sent prompt bubbles), plus their icon, remove button, progress track and the drag-drop overlay, use the shared `--glass-*` tokens.

## 4. `gui/voice_overlay.py`
- **Purpose**: Floating HUD or overlay for voice mode sessions, displaying speech status, transcripts, and active visualizers.
- **Icons**: the mic / gear / speaker states are drawn with PIL (`draw_icon`) in the same stroke style as the chat UI icons, rather than rendering emoji from a system font.
