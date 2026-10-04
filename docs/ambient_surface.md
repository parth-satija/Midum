# Ambient Surface Architecture

Ambient Surfaces are the floating sibling of the [Render Surface](render_surface.md). Same fundamentals (model-written HTML + CSS + JS in a sandboxed iframe, the same `midum.call` bridge), but instead of a pane inside the Midum window, each surface is a small **widget drawn over the user's current screen**, for glanceable, in-context help: explaining what is on screen, annotating a region, a timer or checklist that stays visible while the user works in another app.

## 1. Files

| File | Role |
|------|------|
| `tools/ambient_surface_tools.py` | Model-facing tools. Inert without a GUI hook (`_gui_ambient_hook`), like the Render Surface tools. |
| `gui/ambient_surface.py` | `AmbientSurfaceManager`: one pywebview window per surface, the trusted "shell" page, Windows overlay helpers. |
| `gui/app.py` | Installs the hook in `Api.__init__`, provides `_ambient_call_tool` / `_ambient_submit`, closes all surfaces on shutdown. |
| `tools_schema.py`, `orchestration.py`, `main.py` | Schemas, dispatch branches, and re-exports for the five tools. |

## 2. Tools

- `show_ambient_surface(code, x, y, width, height, surface_id?, title?, corner_radius?, background?, coordinate_space?, focusable?, capture_visible?, auto_close_seconds?)`
- `edit_ambient_surface(old_code, new_code, surface_id?)`: exact-substring edit, re-renders in place.
- `move_ambient_surface(surface_id?, x?, y?, width?, height?, coordinate_space?, duration_ms?)`: the widget **glides** to the new position/size (time-based ease-in-out cubic, default 450 ms, max 3000, `0` = instant). On Windows each frame is a lightweight `SetWindowPos`; elsewhere pywebview `move()`/`resize()`. The call returns when it arrives; a newer move on the same surface takes over from wherever the window currently is.
- `close_ambient_surface(surface_id?)`: an id, `"all"`, or omitted for the most recent.
- `list_ambient_surfaces()`
- **Saving** (same model as the Render Surface's `save_render_surface` family): `save_ambient_surface(name, surface_id?)`, `list_saved_ambient_surfaces()`, `load_ambient_surface(name, surface_id?, x?, y?, width?, height?, coordinate_space?)`, `delete_saved_ambient_surface(name)`. A save is one JSON file in `storage/ambient_surfaces/<name>.json` holding the code, title, look (corner radius, background, focusable, capture_visible) and position + size in canvas coordinates, so it reopens in the right place even if the screen resolution changed. `load` reopens it as if by `show_ambient_surface` (id defaults to the saved name; any of x/y/width/height override just those fields). `list` and `delete` only touch disk and work with no GUI; `save`/`load` go through the manager. These take no model-written code, so the voice model keeps direct access to them.

Reusing a `surface_id` replaces that widget; if only the code, position or size changed it is updated in place with no flicker. At most 8 surfaces can be open at once.

## 3. The model chooses position and size

`x`, `y` (top-left corner), `width`, `height` are given in the tool call. By default they are **canvas coordinates**: the same 1024px-wide grid as `fallback_view_screen`, `fallback_click_grid` and OCR results, scaled to real pixels via `config.SCALE_X/SCALE_Y`. `coordinate_space="screen"` uses raw physical pixels. Canvas placements are clamped to the primary screen; minimum size is 80x60 real pixels. The tool result reports the final placement and whether it was adjusted.

## 4. Look and geometry

Every surface is a rectangle with well-rounded corners (default 48px; values under 40 are raised to 40; capped at half the shorter side) plus a circular close button placed **just outside the top-right rounded corner**: inside the window's bounding box, in the sliver the rounding cuts away, as if the corner were square.

With corner radius `R`, button radius `r` and a 1.5px gap, the button centre lies on the diagonal from the arc centre `(W-R, R)` toward the sharp corner, at distance `R - (R + r + gap)/sqrt(2)` from both the top and right edges. The largest `r` that stays inside the bounding box is where that distance equals `r`; `r` is capped at 10px. This is computed in the shell's JS (`layout()`) from the live window size, so it stays correct under DPI scaling and resizing. Default: about a 15px button with an enlarged invisible hit area.

The window is transparent; the card clips the iframe with `clip-path` + `border-radius`. The model only writes the content and must not draw its own close button or outer frame. The card is white by default (`background` or `<body>` styling overrides it).

## 5. Security model

Identical to the Render Surface. The model's HTML runs in an `<iframe srcdoc sandbox="allow-scripts allow-forms allow-popups allow-modals">` **without** `allow-same-origin`, so it cannot reach `pywebview.api`. It can only `postMessage` to the shell, which checks `e.source` is the live iframe and forwards to that window's private `_AmbientBridge` js_api:

- `midum.call(name, args)` -> `Api._ambient_call_tool` -> `_call_any_tool_from_surface` (native tool, `mcp:server/tool`, or `flow:name`; permissions apply; logged as `[Ambient Surface <id>] <tool>`).
- `parent.postMessage(data, "*")` -> `Api._ambient_submit` -> a new chat turn prefixed `[Ambient Surface input: <id>]`.
- `midum.close()` dismisses the widget from its own script.

Markup is held only in memory; nothing is written to disk.

## 6. Overlay behaviour (Windows)

Applied once the native window exists (`_finish_window`, run twice in case Qt re-applies geometry):

- Exact physical-pixel placement with `SetWindowPos(HWND_TOPMOST)`, because Qt 6 may interpret `create_window`'s coordinates in DPI-scaled units.
- `WS_EX_TOOLWINDOW`: no taskbar / Alt-Tab entry.
- `WS_EX_NOACTIVATE` unless `focusable=True`: never steals keyboard focus from the user's app.
- `WDA_EXCLUDEFROMCAPTURE` unless `capture_visible=True` **or** the global **SETTINGS → Ambient Surfaces → Hide from screenshots** toggle is off: hidden from the model's own screenshots, OCR and screen-share (the user still sees it). Requires Windows 10 2004+. The toggle (`ambient_hide_from_capture` in `storage/gui_settings.json`, default on) is applied to every open surface immediately via `AmbientSurfaceManager.set_hide_from_capture()` (display affinity only, no flicker), and persisted by **Save**. When it is off, every surface is capturable regardless of the model's `capture_visible`; when on, a surface's own `capture_visible=True` still makes that one visible.

Other platforms fall back to pywebview's `move()` / `resize()`.

## 7. Lifecycle

- User clicks the close button: the surface is destroyed and remembered; `list_ambient_surfaces` reports recently dismissed ids.
- `auto_close_seconds > 0`: a daemon timer dismisses it.
- App shutdown: `Api._close_ambient_surfaces()` (called from `_on_closing` and `_destroy_window_safe`) destroys every surface, since pywebview keeps the process alive while any window is open.

## 8. Voice mode: coding is delegated

The Gemini Live voice model doesn't get the code-taking tools (`write_render_code`, `edit_render_code`, `show_ambient_surface`, `edit_ambient_surface`; see `VOICE_BLOCKED_TOOLS`). Both the schema list and the dispatcher enforce this. It gets two request-style tools instead, defined in `tools/surface_delegate_tools.py` (and only offered to the voice backend, not text chat):

- `get_ambient_surface_made(request, x, y, width, height, surface_id?, title?, coordinate_space?, focusable?, auto_close_seconds?)`
- `get_render_surface_made(request, title?, edit_current?)`

The voice model describes what it wants in plain language, including all content verbatim, and still chooses the widget's position and size. A focused coding prompt (surface contract, size, and the current code when revising) is sent as one generation to the **default model**, i.e. whichever provider/model is currently selected in the app (`config.MODEL_PROVIDER`), through `orchestration._call_primary_model`. The HTML is extracted from the reply and opened with the normal `show_ambient_surface` / `write_render_code`. The voice model only receives a confirmation to speak. Reusing a `surface_id` (or `edit_current=true`) revises the open surface: its current code is read back through a read-only hook (`code` on the ambient manager, `get` in `Api._handle_render`). Generation runs on a worker thread (`asyncio.to_thread`) so mic and playback keep running.

## 9. Requirements and known limits

- Needs pywebview's Qt backend with `transparent=True` support (the backend `gui/app.py` already uses). If transparency is unavailable the corners render solid.
- Corners outside the rounded card are transparent but not click-through.
- Widgets are not draggable; the model repositions them with `move_ambient_surface`.
