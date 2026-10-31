# Dynamic Background Architecture

A Dynamic Background is an **app-wide animated backdrop** written by the model as a self-contained HTML + CSS + JS document. It is decorative: drifting starfields, aurora gradients, particle fields, a lava lamp, rain on glass, a slow animated grid. It renders behind the whole Midum window, at the very bottom of the layer stack, **directly above the static background image and below everything else**.

It shares the sandboxed-iframe idea of the [Render Surface](render_surface.md) and [Ambient Surface](ambient_surface.md), but deliberately has no `midum.call` bridge and no way to post input back to the model: a background is something to look at, not an interface.

## 1. Files

| File | Role |
|------|------|
| `tools/dynamic_background_tools.py` | The three model-facing tools, the pure `apply_dynamic_bg_edits` helper, the saved-file path helper and `DYNAMIC_BACKGROUNDS_DIR`. Inert without a GUI hook (`_gui_dynamic_bg_hook`), like the surface tools. |
| `gui/app.py` | `Api._handle_dynamic_background` (the hook), the `dynamic_background_*` UI bridges, the `#dyn-bg-layer` markup/CSS, `applyDynamicBackground` / `clearDynamicBackground`, the sidebar button and the picker pane. |
| `tools_schema.py`, `orchestration.py`, `main.py` | Schemas, dispatch branches and re-exports for the three tools. |
| `tools/surface_delegate_tools.py` | `create_dynamic_background` and `edit_dynamic_background` are in `VOICE_BLOCKED_TOOLS` (they take model-written code). |

## 2. Tools

Three, on purpose:

- `create_dynamic_background(code, title?)`: writes the document and makes it the **active** background immediately (this is how it is previewed). Replaces whatever was active. The new background is an unsaved draft until it is saved.
- `edit_dynamic_background(edits, title?)`: patches the **currently active** document in place. `edits` is a list of `{old, new, replace_all?}`; `old` must match the live text exactly and be unique unless `replace_all` is set, `new` replaces it (`""` deletes). Edits apply in order and the call is all-or-nothing (any failure changes nothing and names the edit). The result goes live at once and is persisted as the active background. It never touches saved files: the live background stops counting as the saved one it came from (the picker's `ACTIVE` marker clears), and the tool result tells the model to call `save_dynamic_background` with the same name to overwrite it. Matching and application live in the GUI-free `apply_dynamic_bg_edits` helper. Requires an active background.
- `save_dynamic_background(name)`: persists the **currently active** background (code and title) to `storage/dynamic_backgrounds/<name>.json`. Saving under an existing name overwrites it. It never changes which background is active.

There is **no set / load / apply tool, and no model-facing list or delete**. Choosing which saved background is active is the user's decision, made from the sidebar. The model can author and save backgrounds but cannot switch the user between their saved ones.

## 3. User side: the sidebar picker

A **Dynamic Backgrounds** button sits in the sidebar directly under **Render Surfaces**. It opens a tool pane listing every saved background (title, name, saved time, an `ACTIVE` marker on the one currently showing):

- **Use** (or **Reapply**): makes that saved background active (`dynamic_background_load_ui`).
- **Delete**: removes the saved file (`dynamic_background_delete_ui`). If it was the active one, it keeps running as an unsaved draft.
- **Turn off**: clears the active background (`dynamic_background_clear_ui`); saved files are untouched.

## 4. Layering

Three fixed, full-viewport, `z-index:0` siblings share one stacking context; **DOM order** decides who paints over whom:

```
#bg-image-layer   (static background image)        <- bottom
#dyn-bg-layer     (Dynamic Background iframe)      <- directly above the image
#blob-layer       (ambient blobs)
.pane / topbar ... (z-index:1, the actual UI)       <- top
```

`#dyn-bg-layer` is `pointer-events:none` end to end, so it can never intercept a click or drag.

**Pane transparency.** Panes are only transparent when something behind them is meant to show through (blobs on, or a background image). While a Dynamic Background is active, the frontend turns on the existing `html.has-bg-image` class (via `_syncBgClass`, which ORs "background image on" with "dynamic background active"). That keeps the panes transparent even with blobs off, and unlocks the liquid-glass option, with no change to the existing CSS rules. Static background image and Dynamic Background therefore cannot clobber each other's use of that class.

## 5. What the model should write

A complete HTML document that fills its viewport: `html,body{margin:0;width:100%;height:100%;overflow:hidden}` and paint everything (canvas, CSS keyframes, inline SVG, WebGL). The iframe is as large as the app window and follows resizes, so size canvases from `window.innerWidth/innerHeight` and re-measure on `resize`. Because it runs continuously behind the whole UI: use `requestAnimationFrame` or CSS animation, keep particle counts modest, avoid per-frame allocation, and choose calm, low-contrast colours so foreground text stays readable. Inline everything; do not rely on external resources.

## 6. Security model

The markup runs in `<iframe sandbox="allow-scripts">`: no `allow-same-origin` (cannot reach `pywebview.api`, cookies or storage), no forms, popups or modals, and no `midum` bridge or `postMessage` handler on the host side for it. It is untrusted decoration only. Clearing it sets `srcdoc` to empty, which drops the document so its timers and animation frames stop.

## 7. Persistence and lifecycle

- **Saved** backgrounds: one JSON file per name in `storage/dynamic_backgrounds/` (`name`, `title`, `code`, `saved_at`). The filename is the name sanitised to letters, digits, `_` and `-`.
- **Active** background: mirrored, code included, to `storage/dynamic_background_active.json` on every create / pick / save / clear, and restored at startup (`dynamic_background_get_active`, called in the frontend boot sequence). An unsaved draft therefore survives a restart too.
- It is **app-wide, not per-chat**: new chats, chat switches and tab changes never touch it.

## 8. Events and UI bridge reference

| Direction | Name | Purpose |
|-----------|------|---------|
| Python -> JS event | `dynamic_background_set` `{code, title, name}` | Render this document as the active background. |
| Python -> JS event | `dynamic_background_clear` | Remove the active background. |
| JS -> Python | `dynamic_background_get_active()` | Boot-time restore. |
| JS -> Python | `dynamic_background_list_saved()` | Populate the picker (`items`, `active_name`, `has_active`). |
| JS -> Python | `dynamic_background_load_ui(name)` | User picked a saved one. |
| JS -> Python | `dynamic_background_clear_ui()` | User turned it off. |
| JS -> Python | `dynamic_background_delete_ui(name)` | User deleted a saved one. |

## 9. Known limits

- Rendering cost is the model's responsibility; a heavy canvas or WebGL scene runs constantly behind the UI.
- The background cannot be interacted with and cannot talk back to the model.
- Voice mode: the Gemini Live model cannot call `create_dynamic_background` or `edit_dynamic_background` directly (they take code); `save_dynamic_background` takes only a name and is allowed.
