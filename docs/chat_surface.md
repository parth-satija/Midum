# Chat Surfaces

A **Chat Surface** is an interactive HTML block that lives *inside the chat
transcript*, at the exact point in the conversation where the model called the
tool — between its sentences, like an image or a code block would be.

It is the third sibling of [Render Surfaces](render_surface.md) (a side panel) and
[Ambient Surfaces](ambient_surface.md) (a floating overlay on the desktop). The
core idea is identical: the model writes one self-contained HTML+CSS+JS document,
it runs in a sandboxed iframe, and it gets the same `window.midum` bridge. What
changes is *where it lives* and *how often the model should reach for it*:
Chat Surfaces are meant to be used **constantly** — any time something is clearer
seen or touched than read.

| | Render Surface | Ambient Surface | **Chat Surface** |
|---|---|---|---|
| Lives in | right-hand panel | frameless overlay window over the desktop | **the chat transcript** |
| Size | resizable panel | model picks x/y/width/height | **width fixed to the chat text; height auto-fits** |
| Lifetime | memory (saveable by name) | memory (saveable by name) | **saved with the chat, restored on reopen** |
| Best for | workspaces the user stays in | glanceable widgets next to another app | **comparisons, charts, checklists, pickers, forms, calculators, previews, status cards** |

## Tools (model-facing)

Defined in `tools/chat_surface_tools.py`, schemas in `tools_schema.py`, dispatched
in `orchestration.py`.

| Tool | Purpose |
|---|---|
| `show_chat_surface(code, surface_id?, title?, height?, max_height?, frame?, background?)` | Put a block in the chat. Reusing a `surface_id` replaces that block **in place**. |
| `edit_chat_surface(old_code, new_code, surface_id?)` | Exact, unique-substring edit; re-renders in place. `surface_id` optional only when exactly one block is open. |
| `close_chat_surface(surface_id?)` | Close one block, `"all"`, or (omitted) the most recent. Use to stop timers/animations. |
| `list_chat_surfaces()` | List open blocks (id, title, sizing, code size). |

### `show_chat_surface` parameters

- **`code`** — complete HTML document (inline `<style>`/`<script>`).
- **`height`** — optional fixed height in CSS px (60–1400). Omit for **auto-fit**.
- **`max_height`** — cap for auto-fit (default 640, max 1600); taller content scrolls inside the block.
- **`frame`** — `true` (default): thin rounded border + soft card background. `false`: no chrome, content sits in the chat like text.
- **`background`** — optional CSS colour for the card. Values containing `; { } < > " ' \` are discarded.
- There is deliberately **no width parameter**: the width is the chat text column.

## What the host provides

- **Width.** The row uses the same 16px inset as message text, and the block fills
  it, so edges line up with the surrounding paragraphs at every window size.
- **Auto height.** The injected bridge reports the document height (ResizeObserver +
  MutationObserver + 500ms poll) and the host sizes the iframe, keeping the chat
  pinned to the bottom if the user was already there. Content must not depend on
  `100vh` / `height:100%` of the root in auto-fit mode.
- **Theme.** Before the model's own markup, the host injects the app's CSS variables
  (`--bg --panel --surface --surface2 --border2 --accent --accent-faint --text
  --subtext --muted --red --green --yellow --chat-fs`), the matching `color-scheme`
  (otherwise the iframe paints an opaque canvas), a transparent body, and default
  padding/font. Anything the model writes overrides these. Theme changes are pushed
  to live blocks via `postMessage`.
- **Dismiss.** A small `×` appears on hover; a block can also call `midum.close()`.

## The `midum` bridge inside a block

Same contract as the other surfaces:

- `await midum.call(name, args)` — run any native tool, `"mcp:server/tool"`, or
  `"flow:name"`. Goes through `Api.chat_surface_call_tool` →
  `_call_any_tool_from_surface`, so **permissions are identical** to the other
  surfaces. Calls are logged to the Log pane only (not persisted into the
  transcript) so a polling block can't flood the chat history.
- `parent.postMessage(data, "*")` — sent back as a new chat turn prefixed
  `[Chat Surface input: <id>]` (`Api.chat_surface_submit`). Rejected with `busy`
  while a turn is running, like the other surfaces.
- `midum.close()` — dismiss the block.

## Persistence

The display log is the single source of truth. Each block is a
`("chatsurface", <json>)` record: `{id, title, code, height, max_height, frame,
background, closed}`.

- Written by `Api._handle_chat_surface`; replayed by `buildStaticRow` /
  `buildTurnBlock` (history) and `appendRow` (live replay paths).
- Records are only ever **replaced in place**, never removed, because edit/regenerate
  rely on display-log indices (`_last_turn_display_start/_end`). Closing writes
  `{id, closed: true}` (code dropped).
- Because the state is the log, edits/closes work after reopening a chat and across
  conversation tabs with no extra bookkeeping.
- Ordering: the record is written when the tool runs, i.e. *before* the "Ran N
  tools" pill record. The live view shows pill → block, so `buildTurnBlock` holds
  block records back and flushes them after the current tool group.

## Voice mode

The Gemini Live model can't write front-end code, so `show_chat_surface` and
`edit_chat_surface` are in `VOICE_BLOCKED_TOOLS`. It gets
`get_chat_surface_made(request, surface_id?, title?, height?)` instead
(`tools/surface_delegate_tools.py`): the default model writes the HTML under a
chat-specific rule set, then it is shown through `show_chat_surface`.
`close_chat_surface` / `list_chat_surfaces` remain available directly.

## Prompting

`system_prompt.py` has a **CHAT SURFACES** section that tells the model to treat
them as its default for anything visual or interactive: use them far more often than
feels necessary, in place of markdown tables, option lists, piles of figures and
step lists, without asking permission or announcing them. The tool descriptions in
`tools_schema.py` repeat the same encouragement. It also points choices at
`parent.postMessage` buttons rather than asking in prose, and keeps permissions /
approvals on the `ask_user_*` tools.

## Files touched

| File | Change |
|---|---|
| `tools/chat_surface_tools.py` | new — tools + `_gui_chat_hook` |
| `tools_schema.py` | 4 schemas |
| `orchestration.py`, `main.py` | import + dispatch / re-export |
| `gui/app.py` | hook install, `_handle_chat_surface` + `chat_surface_*` API methods, CSS, bridge/theme/row JS, event handlers, history + live replay, copy-conversation text |
| `system_prompt.py` | CHAT SURFACES section |
| `tools/surface_delegate_tools.py`, `providers/gemini_live_backend.py` | voice delegate + prompt text |

## Known limits

- No named save/load (Render/Ambient have it): blocks already persist with the chat.
- Sandbox flags: `allow-scripts allow-forms allow-popups allow-modals`; no same-origin.
- A block inside an old turn that scrolls out of the rendered history window is
  re-created from its record when that window renders it.
