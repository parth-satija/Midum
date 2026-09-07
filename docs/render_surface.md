# Render Surface & Web Integration Architecture

The Render Surface subsystem provides a secure, sandboxed HTML/CSS/JS viewing and interaction pane on the right half of the UI.

## 1. `render/surface.py`
- **Purpose**: Controls the lifecycle of the Render Surface (opening, closing, snapshot saving, and state restoration).
- **Features**:
  - Automatically disables conflicting panels when active.
  - Supports persistence via `save_render_surface` and `load_render_surface`.

## 2. `render/bridge.py`
- **Purpose**: Implements the IPC bridge between the sandboxed iframe and Midum's backend tools.
- **Features**:
  - Exposes `midum.call()` for webapi fetching, asset streaming, and search queries within the rendered application.

## 3. `render/assets.py`
- **Purpose**: Manages asset discovery, file streaming as base64 data URIs, and web asset fetching.
