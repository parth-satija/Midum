# Artifacts: model-authored, editable, versioned files

The model can create and edit artifacts the way Claude does: author a page / document / diagram /
component once, then change it in place across turns, with live preview and undo.

Implementation: `artifacts.py` (store, tools, preview). Routing is automatic: `orchestration.py`
already dispatches every name in `artifacts.TOOL_NAMES` and registers `TOOL_SCHEMAS`.

## Tools (model-facing)

| Tool | Purpose |
|---|---|
| `create_artifact(name, content, type?, title?, show?)` | New artifact. Same name in the same chat = new version, never a duplicate. |
| `update_artifact(artifact, old_str, new_str \| edits[], replace_all?, note?, show?)` | Exact-match targeted edit. `old_str` must match once (or `replace_all`). `edits[]` is all-or-nothing. |
| `rewrite_artifact(artifact, content, title?, note?, show?)` | Replace the whole content. |
| `open_artifact(artifact, version?)` | Show in the Render Surface (any version, or an attached text/code/image file). |
| `list_artifact_versions(artifact)` / `revert_artifact(artifact, version)` | History and non-destructive undo (a revert is saved as a new version). |

`read_artifact` / `search_artifact` return the **raw source** of artifacts the model created
(HTML is not tag-stripped), so the model can copy exact text for `old_str`.

## Types and preview

| type | extensions | shown in Render Surface as |
|---|---|---|
| `html` | .html | the document itself (can use `midum.call` / `parent.postMessage`) |
| `markdown` | .md | rendered via marked + DOMPurify (falls back to source offline) |
| `svg` | .svg | `<img>` data URI (scripts cannot run) |
| `mermaid` | .mmd | mermaid (strict security level) |
| `react` | .jsx / .tsx | React 18 + Babel + Tailwind CDN; one component, default export, hooks only |
| `code` / `text` | everything else | escaped source view |

Renderable types open automatically on create/update; `code`/`text` only with `show=true` or
`open_artifact`. Preview CDN scripts (jsdelivr / tailwind) need internet; when they fail the
panel shows the source or an error instead of a blank page.

## Behaviour and safety

* Only `origin == "generated"` artifacts are editable. User attachments are never modified in
  place; the model is told to create a modified copy.
* Attached `.html` / `.jsx` are previewed as inert source, never executed, so an attachment
  cannot reach `midum.call()` (tool access).
* Versions: `storage/artifacts/files/<id>/.versions/vN.bin`, newest 30 kept.
* If the model takes the surface over with `write_render_code` / `edit_render_code` /
  `load_render_surface` / `close_render_surface`, auto-refresh stops until an artifact is
  opened again (`artifacts.clear_shown()`), so edits never clobber an unrelated page.
