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

## 4. `gui/voice_overlay.py`
- **Purpose**: Floating HUD or overlay for voice mode sessions, displaying speech status, transcripts, and active visualizers.
