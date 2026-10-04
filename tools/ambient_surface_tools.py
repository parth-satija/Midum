# =============================================================================
# AMBIENT SURFACE TOOLS
# =============================================================================
# Ambient Surfaces are the sibling of Render Surfaces (tools/render_surface_
# tools.py). Same fundamentals -- the model writes a self-contained
# HTML+CSS+JS document that runs in a sandboxed iframe, gets the same
# window.midum.call(name, args) bridge, and can post free-form input back
# with parent.postMessage(...) -- but instead of living in a pane INSIDE the
# Midum window, an Ambient Surface is a small floating widget drawn OVER
# whatever the user currently has on screen, on top of every other window.
# Use them for quick, glanceable, in-context help: explaining something the
# user is looking at ("this error means..."), annotating a region of the
# screen, a timer/checklist that should stay visible while they work in some
# other app, a translated snippet next to the original text, and so on.
# Reach for a Render Surface instead when you want a full-size panel inside
# Midum (dashboards, documents, tools the user will spend time in).
#
# WHAT THE MODEL CONTROLS
# ------------------------
# Every call to show_ambient_surface carries, besides the code:
#   * x, y            -- where the widget's TOP-LEFT corner goes on screen
#   * width, height   -- the widget's size
# Coordinates are in the same 1024px-wide CANVAS space as fallback_view_screen
# screenshots / fallback_click_grid / OCR results, so a position read off a
# screenshot can be used directly (Python scales it to real pixels). Pass
# coordinate_space="screen" to use raw physical screen pixels instead.
#
# WHAT THE WIDGET LOOKS LIKE
# ---------------------------
# Every Ambient Surface is a rectangle with well-rounded corners (default
# 48px radius). A small circular ✕ button sits just OUTSIDE the top-right
# rounded corner: exactly where that corner would be if the rectangle had
# square corners, i.e. inside the widget's bounding box but in the empty
# sliver the rounding cuts away. The user clicks it to dismiss the widget.
# The model writes only the CONTENT -- the rounded clipping, border and close
# button are added by the host shell (gui/ambient_surface.py), so the model's
# HTML just needs to fill its box; the corners are clipped for it. The card
# background defaults to white (like a Render Surface); pass `background` or
# style <body> yourself for anything else.
#
# HOW IT WORKS UNDER THE HOOD
# ---------------------------
# Like the Render Surface tools, these do nothing on their own: they're only
# useful once the desktop GUI installs a hook here at startup (see
# gui/app.py's Api.__init__:
#     _ambient_surface_tools._gui_ambient_hook = self._ambient.handle
# ). With no GUI attached (e.g. running main.py standalone) every call just
# returns an explanatory error. The markup only ever exists in memory -- it is
# never written to disk.
_gui_ambient_hook = None

# ── Persisted Ambient Surfaces ─────────────────────────────────────────────
# Same idea as save_render_surface/load_render_surface: unlike a live widget
# (RAM-only, gone when dismissed), a SAVED one survives across turns, sessions
# and app restarts. One plain JSON file per saved surface under
# storage/ambient_surfaces/, named after a sanitised `name`: its code, title,
# look (corner radius / background / focusable / capture_visible) AND its
# position + size in canvas coordinates (resolution-independent). list/delete
# only touch disk, so they work with no GUI attached; save/load need the live
# widget / a window to open, so they go through the GUI hook.
import os
import re

from config import STORAGE_DIR

AMBIENT_SURFACES_DIR = os.path.join(STORAGE_DIR, "ambient_surfaces")


def _saved_ambient_path(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip()).strip("_")
    safe = safe or "untitled"
    return os.path.join(AMBIENT_SURFACES_DIR, f"{safe}.json")

_NO_GUI = ("[AMBIENT SURFACE ERROR] No GUI attached -- this tool only "
           "works inside the Midum desktop app.")


def _call_hook(kind: str, payload: dict) -> str:
    if _gui_ambient_hook is None:
        return _NO_GUI
    try:
        return _gui_ambient_hook(kind, payload)
    except Exception as e:  # never let a windowing failure kill the turn
        return f"[AMBIENT SURFACE ERROR] {type(e).__name__}: {e}"


def show_ambient_surface(code: str, x, y, width, height,
                         surface_id: str = "", title: str = "",
                         corner_radius=0, background: str = "",
                         coordinate_space: str = "canvas",
                         focusable: bool = False,
                         capture_visible: bool = False,
                         auto_close_seconds=0) -> str:
    """
    Show (or replace) an Ambient Surface: a rounded-rectangle widget floating
    OVER the user's current screen, at the position and size YOU choose.

    `code` is a complete, self-contained HTML document (inline <style> and
    <script> are fine). It runs in a sandboxed iframe exactly like a Render
    Surface: it can call `await midum.call(name, args)` to run any Midum
    tool / MCP tool / Flow, and `parent.postMessage(data, "*")` to send
    free-form input back to you as a new chat turn (prefixed
    "[Ambient Surface input: <id>]"). Inside it, `midum.close()` dismisses
    the widget from its own script.

    `x`, `y`, `width`, `height` place the widget: (x, y) is its top-left
    corner. By default they are CANVAS coordinates -- the same 1024px-wide
    grid space that fallback_view_screen screenshots and
    fallback_click_grid/fallback_find_text use, so you can read a position
    off a screenshot and use it directly (e.g. put an explanation right next
    to the thing it explains without covering it). Pass
    coordinate_space="screen" for raw physical screen pixels. The widget is
    kept fully on the primary screen and never smaller than roughly 80x60
    real pixels; the result tells you the final placement.

    The host draws the rounded card, its thin border and the circular ✕
    close button (just outside the top-right rounded corner) for you -- do
    NOT draw your own close button or your own outer border/rounded frame.
    Just fill the box: give <html>/<body> `margin:0; height:100%` and
    design for the exact width x height you chose; the corners are clipped
    automatically. The card is white by default; set `background` (any CSS
    color, e.g. "#111827" or "rgba(20,20,30,.92)") or style <body> yourself
    for a dark/tinted look.

    Optional:
      surface_id        -- a short name (letters/digits/_/-) so you can
                           edit/move/close this widget later and keep
                           several on screen at once (max 8). Reusing an id
                           REPLACES that widget. Omit to auto-assign one.
      title             -- short label for list_ambient_surfaces (never
                           shown on the widget itself).
      corner_radius     -- corner radius in CSS px. Default 48; values below
                           40 are raised to 40 so the close button stays
                           usable; capped at half the shorter side.
      focusable         -- False (default): the widget never steals keyboard
                           focus from the app the user is working in --
                           ideal for read-only explanations. Set True when
                           the widget has text inputs the user must type
                           into.
      capture_visible   -- False (default): the widget is hidden from
                           screenshots/OCR/screen-share (so your own
                           fallback_view_screen never sees, or reads, your
                           own overlay; the user still sees it normally).
                           Set True if you need it to show up in screen
                           captures.
      auto_close_seconds-- if > 0, the widget dismisses itself after that
                           many seconds (handy for transient hints).

    Prefer a small number of compact, glanceable widgets over big ones;
    anything the user will spend time in belongs in a Render Surface.
    Use edit_ambient_surface for small changes and move_ambient_surface to
    reposition/resize without resending the code.
    """
    return _call_hook("show", {
        "code": code, "x": x, "y": y, "width": width, "height": height,
        "surface_id": surface_id, "title": title,
        "corner_radius": corner_radius, "background": background,
        "coordinate_space": coordinate_space, "focusable": focusable,
        "capture_visible": capture_visible,
        "auto_close_seconds": auto_close_seconds,
    })


def edit_ambient_surface(old_code: str, new_code: str, surface_id: str = "") -> str:
    """
    Make a targeted edit to the HTML/CSS/JS of an already-open Ambient
    Surface without resending the whole document. `old_code` must be an
    EXACT, character-for-character, UNIQUE substring of what you last wrote
    for that widget -- it is replaced with `new_code`, and the widget
    re-renders in place (same position and size). If it doesn't match, or
    matches more than once, nothing changes and you get an error.

    `surface_id` selects the widget; it may be omitted only when exactly one
    Ambient Surface is open. For a large change, calling
    show_ambient_surface again with the same id and the full new document is
    often simpler.
    """
    return _call_hook("edit", {
        "surface_id": surface_id, "old_code": old_code, "new_code": new_code,
    })


def move_ambient_surface(surface_id: str = "", x=None, y=None,
                         width=None, height=None,
                         coordinate_space: str = "canvas",
                         duration_ms=None) -> str:
    """
    Reposition and/or resize an open Ambient Surface without touching its
    content. Pass only the values you want to change (any of x, y, width,
    height); the rest stay as they are. Same coordinate rules as
    show_ambient_surface (canvas coordinates by default). `surface_id` may be
    omitted only when exactly one Ambient Surface is open.
    """
    return _call_hook("move", {
        "surface_id": surface_id, "x": x, "y": y,
        "width": width, "height": height,
        "coordinate_space": coordinate_space, "duration_ms": duration_ms,
    })


def close_ambient_surface(surface_id: str = "") -> str:
    """
    Dismiss an Ambient Surface and discard its HTML/CSS/JS (it only ever
    lived in memory). Pass a `surface_id` to close that one, "all" to close
    every open Ambient Surface, or omit it to close the most recently opened
    one. The user can also dismiss any widget themselves at any time with its
    ✕ button, so an unknown id usually just means it was already closed.
    """
    return _call_hook("close", {"surface_id": surface_id})


def save_ambient_surface(name: str, surface_id: str = "") -> str:
    """
    Save a snapshot of an OPEN Ambient Surface -- its HTML/CSS/JS, title,
    look, and its current position and size (canvas coordinates) -- to disk
    under `name`, so it can be brought back later with
    load_ambient_surface(name), even in a future session or after the app
    restarts. The live widget stays open and untouched; this only writes a
    copy out. Saving again under a `name` that already exists OVERWRITES it.

    `surface_id` selects which open widget to save; it may be omitted only
    when exactly one Ambient Surface is open.
    """
    return _call_hook("save", {"name": name, "surface_id": surface_id})


def list_saved_ambient_surfaces() -> str:
    """
    List every Ambient Surface previously saved with save_ambient_surface,
    by name, with its title, size and when it was saved. Follow up with
    load_ambient_surface(name) to put one back on screen, or
    delete_saved_ambient_surface(name) to remove one. Works even with no GUI
    attached, since it only reads from disk.
    """
    import json
    os.makedirs(AMBIENT_SURFACES_DIR, exist_ok=True)
    try:
        files = sorted(f[:-5] for f in os.listdir(AMBIENT_SURFACES_DIR) if f.endswith(".json"))
    except Exception as e:
        return f"[AMBIENT SURFACE ERROR] Could not list saved surfaces: {e}"

    if not files:
        return ("No saved Ambient Surfaces yet. Use save_ambient_surface(name) "
                "while one is open.")

    lines = [f"Saved Ambient Surfaces ({len(files)}):"]
    for safe_name in files:
        path = os.path.join(AMBIENT_SURFACES_DIR, f"{safe_name}.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            real_name = data.get("name") or safe_name
            title = data.get("title") or real_name
            size = f", {data.get('width')}x{data.get('height')}" if data.get("width") else ""
            saved_at = data.get("saved_at", "")
            suffix = f", saved {saved_at}" if saved_at else ""
            lines.append(f'- {real_name}  (title: "{title}"{size}{suffix})')
        except Exception:
            lines.append(f"- {safe_name}")
    lines.append("\nUse load_ambient_surface(name) to put one back on screen.")
    return "\n".join(lines)


def load_ambient_surface(name: str, surface_id: str = "", x=None, y=None,
                         width=None, height=None,
                         coordinate_space: str = "canvas") -> str:
    """
    Put a previously SAVED Ambient Surface (see save_ambient_surface /
    list_saved_ambient_surfaces) back on screen -- same effect as
    show_ambient_surface, but the code, look, position and size come from
    disk. By default it reopens exactly where and how big it was saved; pass
    any of x, y, width, height to override just those (same coordinate rules
    as show_ambient_surface; canvas by default). `surface_id` sets the id of
    the opened widget (defaults to the saved name); reusing an id of an
    already-open widget replaces it. Call list_saved_ambient_surfaces() first
    if you're not sure of the exact name.
    """
    return _call_hook("load", {
        "name": name, "surface_id": surface_id, "x": x, "y": y,
        "width": width, "height": height, "coordinate_space": coordinate_space,
    })


def delete_saved_ambient_surface(name: str) -> str:
    """
    Permanently delete a saved Ambient Surface by name (see
    list_saved_ambient_surfaces). Does not affect a widget currently on
    screen -- only the saved copy on disk. Works even with no GUI attached.
    """
    path = _saved_ambient_path(name)
    if not os.path.exists(path):
        return f"[AMBIENT SURFACE ERROR] No saved Ambient Surface named '{name}'."
    try:
        os.remove(path)
        return f"Deleted saved Ambient Surface '{name}'."
    except Exception as e:
        return f"[AMBIENT SURFACE ERROR] Could not delete '{name}': {e}"


def list_ambient_surfaces() -> str:
    """
    List every Ambient Surface currently open -- its id, title, position and
    size (canvas + screen pixels), and how much code it holds. Use it to
    check what's still on screen (the user may have dismissed some) before
    editing, moving or closing one.
    """
    return _call_hook("list", {})


# =============================================================================
