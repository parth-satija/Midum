# =============================================================================
# CHAT SURFACE TOOLS
# =============================================================================
# Chat Surfaces are the third sibling of Render Surfaces (tools/render_surface_
# tools.py) and Ambient Surfaces (tools/ambient_surface_tools.py). Same
# fundamentals -- the model writes a self-contained HTML+CSS+JS document that
# runs in a sandboxed iframe, gets the same window.midum.call(name, args)
# bridge (native tools, "mcp:server/tool", "flow:name"), and can post free-form
# input back with parent.postMessage(...) -- but a Chat Surface is neither a
# side panel nor a floating overlay: it is a BLOCK IN THE CHAT TRANSCRIPT, laid
# out in the flow of the conversation exactly where you called the tool, between
# your sentences, like a code block or an image would be.
#
# WHAT THE MODEL CONTROLS
# ------------------------
#   * code        -- the HTML document (inline <style>/<script>).
#   * height      -- optional fixed height in CSS px. Omit it (the default) and
#                    the block AUTO-FITS its content, up to max_height.
#   * max_height  -- cap for auto-fit (default 640px); taller content scrolls
#                    inside the block.
#   * frame       -- True (default): a thin rounded border and a soft card
#                    background. False: no chrome at all, the content simply
#                    sits in the chat like text.
#   * background  -- optional CSS colour for the card behind your content.
# The WIDTH is not a parameter. It is fixed to the width of the chat text (the
# message column), so a surface lines up with the paragraphs around it at every
# window size. Design for a fluid ~300-730px wide column: flex/grid, %, rem.
#
# WHAT THE HOST ADDS
# ------------------
# The page is given the app's colour theme as CSS variables (--text, --subtext,
# --muted, --surface, --surface2, --border2, --accent, --green, --red, --yellow,
# --chat-fs) with a transparent body, so it matches dark/light/custom themes
# with no extra work. Use those variables instead of hard-coded colours.
#
# LIFECYCLE
# ---------
# A Chat Surface is part of the conversation: it is saved with the chat and is
# restored when the chat is reopened (unlike Render/Ambient Surfaces, which are
# memory-only). Reusing a surface_id replaces that block IN PLACE. The user can
# dismiss one with the small x that appears when hovering it, and the model can
# close one with close_chat_surface.
#
# Like the other surface tools, these do nothing on their own: they only work
# once the desktop GUI installs a hook here at startup (see gui/app.py's
# Api.__init__:  _chat_surface_tools._gui_chat_hook = self._handle_chat_surface).
# With no GUI attached every call returns an explanatory error.
_gui_chat_hook = None

_NO_GUI = ("[CHAT SURFACE ERROR] No GUI attached -- this tool only "
           "works inside the Midum desktop app.")


def _call_hook(kind: str, payload: dict) -> str:
    if _gui_chat_hook is None:
        return _NO_GUI
    try:
        return _gui_chat_hook(kind, payload)
    except Exception as e:  # never let a UI failure kill the turn
        return f"[CHAT SURFACE ERROR] {type(e).__name__}: {e}"


def show_chat_surface(code: str, surface_id: str = "", title: str = "",
                      height=0, max_height=0, frame=True,
                      background: str = "") -> str:
    """
    Put an interactive/visual block directly INTO THE CHAT, right where you
    are in the conversation. Reach for this OFTEN -- whenever something is
    clearer seen or touched than read: comparisons and tables, charts and
    sparklines, timelines, step trackers and checklists, calculators and
    converters, option pickers and quick polls, forms, previews, colour
    palettes, diagrams, progress/status cards, flashcards, mini-games.

    `code` is a complete, self-contained HTML document (inline <style> and
    <script> are fine). It runs in a sandboxed iframe exactly like a Render
    Surface: `await midum.call(name, args)` runs any Midum tool / MCP tool /
    Flow, `parent.postMessage(data, "*")` sends input back to you as a new
    chat turn prefixed "[Chat Surface input: <id>]" (use it for buttons and
    choices instead of asking in prose), and `midum.close()` dismisses the
    block from its own script.

    LAYOUT: the width is fixed to the chat text column -- you cannot change
    it, design for a fluid ~300-730px wide block. By default the height
    AUTO-FITS the content (up to `max_height`, default 640px, then it
    scrolls); pass `height` (CSS px) only if you need a fixed size, e.g. for a
    canvas. In auto-fit mode do NOT use 100vh / height:100% on the root
    element (the block's height is derived from the content).

    THEME: the page gets the app's theme as CSS variables and a transparent
    body, so write `color: var(--text)`, `var(--subtext)`, `var(--muted)`,
    `var(--accent)`, `var(--border2)`, `var(--surface2)` (for inner cards and
    buttons), `var(--green)`, `var(--red)`, `var(--yellow)`, and size text
    with `var(--chat-fs)` (it follows the user's font-size setting). Do not
    hard-code a white/black page background. Default font is the system UI
    font. Keep the block compact -- it sits between paragraphs.

    Optional:
      surface_id  -- short name (letters/digits/_/-) so you can edit/close it
                     later and keep several blocks in one reply. Reusing an id
                     REPLACES that block in place. Auto-assigned if omitted.
      title       -- short label for list_chat_surfaces (not drawn).
      height      -- fixed height in CSS px (60-1400). Omit to auto-fit.
      max_height  -- cap for auto-fit, CSS px (default 640).
      frame       -- True (default): thin rounded border + soft card background.
                     False: no chrome, content sits in the chat like text.
      background  -- CSS colour for the card (e.g. "#111827"); default is the
                     theme's surface colour (transparent when frame=False).

    A Chat Surface is saved with the conversation and comes back when the
    chat is reopened. Use edit_chat_surface for small changes. Choose a Render
    Surface instead for a big workspace the user will spend time in, and an
    Ambient Surface for something that must float over another app.
    """
    return _call_hook("show", {
        "code": code, "surface_id": surface_id, "title": title,
        "height": height, "max_height": max_height,
        "frame": frame, "background": background,
    })


def edit_chat_surface(old_code: str, new_code: str, surface_id: str = "") -> str:
    """
    Make a targeted edit to the HTML/CSS/JS of a Chat Surface already in the
    conversation, without resending the whole document. `old_code` must be an
    EXACT, character-for-character, UNIQUE substring of what you last wrote
    for that block -- it is replaced with `new_code` and the block re-renders
    in place (same spot in the chat, same sizing). If it doesn't match, or
    matches more than once, nothing changes and you get an error.

    `surface_id` selects the block; it may be omitted only when exactly one
    Chat Surface is open in this chat. For a large change, call
    show_chat_surface again with the same id and the full new document.
    """
    return _call_hook("edit", {
        "surface_id": surface_id, "old_code": old_code, "new_code": new_code,
    })


def close_chat_surface(surface_id: str = "") -> str:
    """
    Remove a Chat Surface from the conversation and discard its code. Pass a
    `surface_id` to close that one, "all" to close every open Chat Surface in
    this chat, or omit it to close the most recent one. Use it to stop a
    running timer/animation or to tidy away a block that has served its
    purpose. The user can also dismiss any block with the x shown when they
    hover it, so an unknown id usually just means it is already gone.
    """
    return _call_hook("close", {"surface_id": surface_id})


def list_chat_surfaces() -> str:
    """
    List every Chat Surface currently open in this chat -- id, title, sizing
    mode, and how much code it holds. Use it to check what is still in the
    conversation (the user may have dismissed some, or the chat may have been
    reopened) before editing or closing one.
    """
    return _call_hook("list", {})


# =============================================================================
