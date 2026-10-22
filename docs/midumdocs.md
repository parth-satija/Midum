# MidumDocs: LaTeX documents with a live page editor

A MidumDoc is an ordinary artifact of type `latex` (`.tex`), shown in a wide pane (62% of the window, chat keeps 38%).

## Pieces
- `midumdocs.py`: localhost asset server, SwiftLaTeX engine download (pinned SHA-256), live editor state, model tools, glue for the GUI.
- `gui/midumdocs_assets/editor.html`: the editor (CodeMirror 5 source pane, pdf.js page pane, SwiftLaTeX pdfTeX in a Web Worker, floating prompt box). Runs in an iframe and talks only to its parent via `postMessage`.
- `gui/app.py`: `#midumdocs-pane-wrap`, `Api.midumdocs_*`, `_handle_midumdocs`, layout (`targetGeo` -> `docs:[38,62]`).
- `artifacts.py`: `latex` type; `_show()` routes it to MidumDocs; `_notify_editor()` pushes model edits to the open editor.
- `orchestration.py` / `system_prompt.py`: tool registration, dispatch, per-step `prompt_block()` (cursor + selection + compile status).

## Model tools
`create_midumdoc`, `midumdoc_state(wait_compile?)`, `midumdoc_replace_selection`, `midumdoc_insert_at_cursor`; plus the normal `update_artifact` / `rewrite_artifact` / `revert_artifact` / `open_artifact`.

## Behaviour notes
- User typing autosaves (1.2 s debounce) into the artifact; saves within 90 s fold into one version.
- The prompt box (Ctrl/Cmd+K) sends `[MidumDoc request]` as a normal chat turn with the selection/cursor frozen at send time.
- Offsets are UTF-16 code units end to end; the editor reports `version`/`length` so stale offsets are refused, never applied.
- Engine files live in `storage/vendor/midumdocs/`; TeX packages are fetched on demand from `MIDUMDOCS_TEXLIVE_URL` (config, default `https://texlive2.swiftlatex.com/`).
- Page -> source jumping is approximate (typeset text is matched back to source); SwiftLaTeX provides no SyncTeX.
- Packaging: add `gui/midumdocs_assets` to the PyInstaller datas in `gui/app.spec`.
