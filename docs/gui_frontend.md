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
- **Live flow**: `appendRow("tool", ...)` adds a pending pill; `attachToolCallDetail()` fills it when the `tool_call` event lands. Sub-agent calls (`agent_tool_call`) join the group as dimmed, agent-tagged pills (shown as `+N agent calls`).
- **Replay**: persisted `toolcall` display-log records are folded into the same groups by `buildTurnBlock()`.
- **Styling**: base frosted-chip look plus a Liquid Glass block that reuses the shared `--glass-*` tokens (no `backdrop-filter`).

## 4. `gui/voice_overlay.py`
- **Purpose**: Floating HUD or overlay for voice mode sessions, displaying speech status, transcripts, and active visualizers.
