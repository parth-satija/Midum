# Editor tab (Monaco)

A VS Code-style code editor as a regular GUI tab (last tab in the bar, `code` icon). It edits
**any file on disk**; chat artifacts are just one optional section in the sidebar.

## What it has

* **Explorer**: any folder (Open Folder / right-click the folder header for recents), lazy tree,
  New File / New Folder / Rename / Delete (Recycle Bin if `send2trash` is installed), Reveal in
  File Explorer, Copy Path. Keyboard: arrows, Enter, F2, Del. A collapsible **Chat Artifacts**
  section lists the current chat's text artifacts (click = preview tab, double-click = pin).
* **Tabs**: single-click opens a replaceable *preview* tab (italic), double-click or typing pins it.
  Dirty dot, close prompt (Save / Don't Save / Cancel), middle-click close, drag to reorder,
  context menu (Close Others / to the Right / Saved / All), Ctrl+Tab, session restored on restart.
* **Monaco**: syntax highlighting for ~80 languages, minimap, bracket colorization, sticky scroll,
  find/replace, multi-cursor, command palette (F1), Ctrl+wheel zoom. Colors follow the app theme
  (including light themes) live.
* **Files**: Open File... for any path, Ctrl+P fuzzy quick-open across the workspace, UTF-8 / UTF-8
  BOM / UTF-16 / cp1252 round-trip, CRLF vs LF preserved, binary and >5 MB files get a placeholder.
* **Safety**: saving checks the file's mtime (or the artifact's version); if it changed underneath
  you, you get "Reload / Keep my version" instead of a silent overwrite. Clean tabs reload
  automatically when the file changes on disk. Optional auto save (status bar).

## Artifacts and the model

* Model-created artifacts open editable; **Ctrl+S saves a new artifact version** (history kept,
  `revert_artifact` works). Attached files are read-only with a **Save as copy** button.
* When the model calls `update_artifact` / `rewrite_artifact` on an artifact you have open, the tab
  refreshes live (or shows the conflict banner if you have unsaved edits). Saving from the editor
  also refreshes a live Render Surface preview of the same artifact.
* Model tools: `open_artifact` (code/text -> Editor tab, pages/diagrams -> Render Surface preview,
  `mode='edit'` forces the editor) and `open_file_in_editor(path, line?, column?)` for **any** file on disk
  (formerly `open_in_editor`, whose schema was retired; the dispatch alias remains for old chats). See
  **Chat link** below for the rest of the Editor tools.

## Chat link (the model can see and edit the Editor)

The active model chat is linked to the Editor (`editor_link.py`, same pattern as `midumdocs.py`). Every step the
system prompt gets a short **EDITOR** block (workspace, active tab, other tabs, from `editor_state.json`), and the
model has these tools:

| Tool | What it does |
|---|---|
| `open_file_in_editor(path, line?, column?)` | open a file and make it the **active** tab (relative paths resolve against the workspace); waits for the Editor to confirm |
| `open_folder_in_editor(path)` | make a folder the workspace (Explorer, Ctrl+P, terminals follow, like Open Folder) |
| `editor_workspace_tree(path?, depth?, max_entries?)` | folder tree; open tabs marked `[open]` / `[active]` / `*unsaved`; noise folders skipped |
| `editor_read_active_file(start_line?, end_line?, path?)` | the active tab's **live** text incl. unsaved edits (numbered, paged), cursor, selection, visible lines, editor problems, other tabs |
| `edit_file_in_editor(path, edits, save?)` | **open any workspace file as the active tab, then edit it - one call.** Same edit kinds and approval as below; the file must already exist |
| `editor_edit_active_file(edits, save?)` | in-place edit of the active tab |

`open_file_in_editor` / `edit_file_in_editor` take a **bare name** (`app.py`) or **partial path** (`gui/app.py`) from the
workspace tree as well as an absolute path. A unique match opens; an ambiguous one lists the candidates; a typo
suggests close names.

**Approvals.** Before an edit touches the buffer the Editor shows a diff dialog (Approve / Reject with an optional note
that is relayed to the model / *Approve & don't ask again*). Reject is focused by default and plain Enter does nothing
(Ctrl+Enter approves), so a keystroke meant for the editor can't approve an edit; Esc rejects; the dialog times out after
5 min as a rejection. The switch is the persisted setting `aiApprove` (default **on**); `F1 > Midum: Toggle Approval for AI
Edits` flips it. The model has no tool to change it and the guard below blocks writes to `editor_state.json`. If the page is
not running the dialog can't be shown, so edits are refused (never silently written) while approvals are on.

**Don't bypass the Editor.** The EDITOR prompt block and `system_prompt.py` tell the model not to modify an open file with
`write_local_file` / `append_local_file` / terminal commands / Filesystem MCP `write_file`, `edit_file`, `move_file`. This
is enforced, not just requested: `orchestration.py` calls `editor_link.guard_external_write()` right after the permission
check (and on the MCP-resolver fallback) and blocks those calls on any file that is an open Editor tab, returning a message
that names the right tool. New files and files that are not open are unaffected.

**Edits** go into the live Monaco model: one undo step (Ctrl+Z), changed lines flash briefly, the tab turns dirty and
nothing the user typed is lost. `edits` is a list; every edit is located against the text *as the model read it*, so
several edits in one call never shift each other. Kinds: `replace_text {old,new,replace_all?}` (must match once),
`replace_lines`, `insert_after_line` (0 = top), `delete_lines`, `replace_selection` (selection, or insert at cursor),
`replace_file`. If any edit is invalid nothing is applied. `save=true` also saves through the normal save path (so the
mtime / artifact-version conflict check still applies). If the buffer changed between the model's read and the edit, the
page refuses it (`stale`); text-anchored edits are retried once on fresh text, line-based ones return an error.

**Transport**: a tool call pushes an `editor_ai_request` event `{id, op, args}` (`artifacts._gui_editor_hook("ai_request")`
-> `EditorMixin._handle_editor` -> `mzEdEvent`); the page (`aiHandle` in `editor.html`) runs `state` / `snapshot` / `edit` /
`open_file` / `open_folder` against the live models and answers with the `editor_ai_reply` Api method, which wakes the
waiting tool thread (10 s; 12 s before the page has ever said `editor_ai_hello`). `state`/`snapshot`/`edit` never switch
tabs; if the iframe never started it is booted in the background. **No live Editor** (never opened, headless): reads fall
back to the file on disk and edits are written to disk atomically with the same encoding / BOM / CRLF handling. If the
page *was* connected but stops answering, edits are **not** silently written to disk (that could clobber unsaved text).

Edit resolution (line/column math, UTF-16 columns for emoji, overlap checks) is plain Python in `editor_link.py`.
Permissions: each tool is a normal tool key, so any of them can be set to Ask / Deny in the Permissions tab.

## AI inline suggestions

Copilot-style ghost text while you type: **Tab** accepts, **Ctrl+Right** accepts one word, **Esc**
dismisses, **Alt+\** triggers on demand. Toggle from the **AI: on/off** item in the status bar or
F1 > *Midum: Toggle AI Inline Suggestions* (remembered in `editor_state.json` as `inlineAI`).

* **Model**: `gemini-3.1-flash-lite` (`INLINE_MODEL` in `gui/inline_suggest.py`) via the native
  `generateContent` endpoint with thinking set to minimal. Independent of `config.GEMINI_API_MODEL`,
  so changing the chat model never slows down typing.
* **Speed**: upstream calls reuse keep-alive HTTPS connections (no TLS handshake per keystroke; falls back to
  `urllib` when a system proxy is configured), plain completions are capped at 400 output tokens and an 8 s
  timeout per key (`MAX_TOKENS_COMPLETE`, `REQUEST_TIMEOUT`), instruction-driven code at 2500 tokens / 20 s. The
  status-bar tooltip shows the last request's latency.
* **Indentation**: when the cursor sits on an already-indented empty line (e.g. right under an instruction
  comment after Enter), the model writes the first line with its full indentation and `_clean` drops the part
  the editor already put there, so the first inserted line is not indented twice.
* **Key hopping**: keys are read from the shared secrets file (`GEMINI_API_KEY`, `GEMINI_API_KEY_2`
  .. `_5`; the file is re-read whenever it changes). Requests rotate **round-robin** over all usable
  keys, so each key's per-minute quota is used in parallel. A key that returns 429 (cooldown from the
  API's `retryDelay`, 30 min if it is a daily quota), 401/403 (30 min), 5xx or a network error (15-20 s)
  is parked and the *same request* instantly hops to the next key. If every key is cooling, the status
  bar shows `AI: cooling` and the editor pauses requests briefly instead of hammering the API.
* **What is sent**: up to 5000 chars before and 1500 chars after the cursor, plus the file name and
  language, to Google's Gemini API. Nothing is sent for read-only tabs, when the cursor is in the
  middle of an identifier, or for files named `.env*`, `*secret*`, `*.pem`, `*.key`, `*.pfx`, `*.p12`,
  `*.kdbx`, `*.ppk` or `id_rsa`-style keys. Keys are never exposed to the page.
* **Instruction comments**: a comment that starts with `ai:` (or reads like a request, e.g. `// add a
  method that does X`, `# make this async`, `-- delete this query`) is carried out. Put the cursor on the
  empty line right under the comment (press Enter after it; automatic suggestions never fire while you
  are still typing the comment line itself - use Alt+\ there). The model sees the comment and the
  **target block**: the first code line directly below it plus the rest of that statement/block
  (deeper-indented lines, a closing bracket; it stops at a blank line). It answers with one of:
  *insert* (new code at the cursor, ghost text, Tab), *replace* (rewrites only the lines the instruction
  touches, often just the single line below the comment) or *delete* (removes those lines); replace and
  delete are limited to that code block. Once a suggestion that followed an instruction is applied or
  accepted, the editor offers to delete the comment itself (shown as red lines; Tab deletes, Esc keeps).
  Ordinary descriptive comments are not treated as instructions. Accepting by word (Ctrl+Right) does not
  trigger the comment clean-up.
* **Edits and deletions in normal typing**: besides inserting, the model may *replace* or *delete* code
  in a small window of numbered lines around the cursor (2 lines above to 4 below), but only when
  something there is clearly wrong or stale (a typo in an identifier, a name left behind after a
  rename, an accidental duplicate line, a syntax error). It is told to be conservative: the line you are
  typing is completed with ghost text, never rewritten, unless it contains an obvious mistake in text
  that is already complete.
* **How replace / delete look**: Monaco's ghost text can only add text, so these are drawn as a diff
  with a marker in the gutter, left of the line numbers: **green** = lines to be added, **red** = lines
  to be deleted (struck through), **yellow** = lines to be edited (old version struck through, new
  version shown below it in green). Only the lines that really change are marked; unchanged lines
  inside a rewrite are left alone. There is no hint text: **Tab** applies it as one undoable edit and
  **Esc** (or any edit/cursor move) dismisses it.
* **Transport**: `POST /api/inline` on the editor's own localhost server (not the pywebview bridge).
  It requires a per-process token injected into the page, a localhost `Host`/`Origin`, and a body under
  256 KB. Requests are debounced (200 ms; instruction comments and Alt+\ fire immediately), cancelled when you keep typing, deduplicated by an LRU
  cache, and capped at 4 concurrent upstream calls.

## Architecture

```
gui/app.py            tab entry + persistent iframe host + postMessage bridge ("MZED" block)
gui/editor_backend.py localhost asset server, Monaco download, EditorMixin (editor_* Api methods)
gui/inline_suggest.py AI inline suggestions: Gemini call, key pool with round-robin hopping, /api/inline logic
gui/editor_assets/editor.html   the whole editor UI (one file)
artifacts.py          editor_* helpers, _gui_editor_hook, _notify_editor on every commit
storage/vendor/monaco local Monaco 0.52.2 (downloaded once from the npm registry)
storage/editor_state.json       workspace, open tabs, layout, settings
```

* The editor is a **separate page in a persistent iframe**, not built into `#tool-content`:
  `showToolPane()` wipes that element on every tab switch, which would destroy the editor and all
  open files. The iframe host is only shown/hidden. It also keeps Monaco's AMD loader and CSS away
  from the main page (Mermaid/KaTeX are AMD-sensitive).
* The iframe talks to Python only through `postMessage`; the parent forwards `editor_*` calls to
  `pywebview.api` (name-whitelisted) and pushes `editor_*` events back. The Render Surface sandbox
  has no access to any of this.
* The localhost server binds `127.0.0.1` on a random port and serves only `editor.html` and the
  vendored Monaco folder (path-traversal tested).
* First launch downloads Monaco (~18 MB, once) in the background; until then (or if offline) the
  page uses the jsDelivr CDN. If neither works the tab shows a Retry screen.

## Gotchas

* Never give an element in `editor.html` the id `monaco` (or `require` / `define`): it shadows the
  global and Monaco silently fails to initialise (DOM clobbering).
* PyInstaller: bundle `gui/editor_assets` as data (`('gui/editor_assets', 'gui/editor_assets')`);
  the backend already looks under `sys._MEIPASS/gui/editor_assets`.
* Unsaved tabs are not guarded against closing the whole app (the Editor tab button shows a dot
  while any tab is dirty).
