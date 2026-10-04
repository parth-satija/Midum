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

## 4. `gui/voice_overlay.py`
- **Purpose**: Floating HUD or overlay for voice mode sessions, displaying speech status, transcripts, and active visualizers.
- **Icons**: the mic / gear / speaker states are drawn with PIL (`draw_icon`) in the same stroke style as the chat UI icons, rather than rendering emoji from a system font.
