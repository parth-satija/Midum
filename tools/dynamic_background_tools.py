# =============================================================================
# DYNAMIC BACKGROUND TOOLS
# =============================================================================
# A Dynamic Background is a full-viewport, animated backdrop for the ENTIRE
# Midum app, written by the model as a self-contained HTML+CSS+JS document.
# It runs in a sandboxed <iframe> pinned to the very bottom of the app's
# layer stack: it sits directly ABOVE the static background-image layer
# (#bg-image-layer) and BELOW everything else (the ambient blobs, every pane,
# every control). It never receives pointer events, so it can't intercept a
# click, and it is purely decorative -- think drifting starfields, aurora
# gradients, slow particle fields, a lava lamp, rain on glass, a subtle
# animated grid, and so on.
#
# THREE MODEL TOOLS, ON PURPOSE
# -----------------------------
#   create_dynamic_background(code, title?)  -- write the document; it goes
#                                               live immediately as the app's
#                                               active background (that is how
#                                               you and the user preview it).
#   edit_dynamic_background(edits, title?)   -- patch the live document with
#                                               exact-text replacements instead
#                                               of resending all of it.
#   save_dynamic_background(name)            -- persist whatever is currently
#                                               showing to disk under `name`.
#
# There is deliberately NO "set"/"load"/"apply" tool. Choosing which SAVED
# Dynamic Background is active is the USER's decision, made from the
# "Dynamic Backgrounds" button in the sidebar (right under "Render Surfaces").
# The model can author and save backgrounds, but it can't switch the user
# between their saved ones. (Listing / deleting saved ones is likewise a
# user-side action in that same picker.)
#
# WHAT THE MODEL SHOULD WRITE
# ---------------------------
# A complete HTML document that fills its viewport: give <html>/<body>
# `margin:0; width:100%; height:100%; overflow:hidden` and paint the whole
# thing (a canvas, CSS animation, SVG, WebGL...). The page is shown at the
# size of the app window and resized with it, so size canvases from
# window.innerWidth/innerHeight and re-measure on "resize". Keep it cheap:
# it runs continuously behind the whole UI, so prefer requestAnimationFrame
# or CSS animations, avoid heavy per-frame allocation, and keep particle /
# element counts modest. Use colours that leave the foreground UI readable
# (generally dark or low-contrast); the panes sit on top of it.
# The iframe is sandboxed (allow-scripts only): no midum.call bridge, no
# storage, no network access to the app, no navigation of the parent.
#
# HOW IT WORKS UNDER THE HOOD
# ---------------------------
# Like the Render/Ambient Surface tools, these do nothing on their own: the
# desktop GUI installs a hook at startup (gui/app.py's Api.__init__:
#     _dynamic_background_tools._gui_dynamic_bg_hook = self._handle_dynamic_background
# ). With no GUI attached (e.g. running main.py standalone) every call just
# returns an explanatory error. A saved Dynamic Background is one plain JSON
# file under storage/dynamic_backgrounds/, named after a sanitised `name`.
_gui_dynamic_bg_hook = None

import os
import re

from config import STORAGE_DIR

DYNAMIC_BACKGROUNDS_DIR = os.path.join(STORAGE_DIR, "dynamic_backgrounds")


def _saved_dynamic_bg_path(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip()).strip("_")
    safe = safe or "untitled"
    return os.path.join(DYNAMIC_BACKGROUNDS_DIR, f"{safe}.json")


_NO_GUI = ("[DYNAMIC BACKGROUND ERROR] No GUI attached -- this tool only "
           "works inside the Midum desktop app.")


def _call_hook(kind: str, payload: dict) -> str:
    if _gui_dynamic_bg_hook is None:
        return _NO_GUI
    try:
        return _gui_dynamic_bg_hook(kind, payload)
    except Exception as e:  # never let a GUI failure kill the turn
        return f"[DYNAMIC BACKGROUND ERROR] {type(e).__name__}: {e}"


def create_dynamic_background(code: str, title: str = "") -> str:
    """
    Create a Dynamic Background: an animated backdrop for the ENTIRE Midum
    app, written as one self-contained HTML+CSS+JS document in `code`. It
    goes live immediately (replacing whatever dynamic background was active)
    so you and the user can see it, and sits at the very bottom of the app's
    layers -- directly above the static background image, beneath the
    ambient blobs and every pane, button and message. It is decorative only:
    it never receives clicks.

    Write the document so it fills the viewport: `html,body{margin:0;
    width:100%;height:100%;overflow:hidden}` and paint everything (a
    <canvas>, CSS keyframes, inline SVG, WebGL -- anything that runs in a
    browser). It is displayed at the app window's size and follows resizes,
    so size canvases from window.innerWidth/innerHeight and re-measure on
    the "resize" event. Because it runs continuously behind the whole UI,
    keep it cheap (requestAnimationFrame or CSS animation, modest particle
    counts, no per-frame garbage) and keep colours calm/low-contrast so the
    foreground UI stays readable. No external resources are guaranteed to
    load: inline everything. The iframe is sandboxed with scripts only --
    there is no midum.call bridge, storage or parent access.

    `title` is an optional short human label (shown in the picker).

    This only CREATES and previews it. To keep it for later, call
    save_dynamic_background(name). There is no tool for switching to an
    already-saved one: the user picks from their saved Dynamic Backgrounds
    themselves via the sidebar's "Dynamic Backgrounds" button.
    """
    return _call_hook("create", {"code": code, "title": title})


def save_dynamic_background(name: str) -> str:
    """
    Save the Dynamic Background that is CURRENTLY showing -- its HTML/CSS/JS
    and title, exactly as it is right now -- to disk under `name`, so the
    user can pick it again later from the sidebar's "Dynamic Backgrounds"
    button, even in a future session or after the app restarts. The live
    background stays active and untouched; this only writes a copy out.
    Saving again under an existing `name` OVERWRITES it.

    Requires a Dynamic Background to be active (create_dynamic_background
    first). This tool only saves; it never changes which background is
    active, and there is no tool to apply a saved one -- that choice belongs
    to the user.
    """
    return _call_hook("save", {"name": name})


def apply_dynamic_bg_edits(code: str, edits):
    """
    Pure helper (no GUI): apply `edits` to `code` in order and return
    (new_code, None) on success or (None, error_message) on the first
    problem. All-or-nothing -- on any error the caller keeps the original.
    Each edit is {old, new, replace_all?}; `old` must match the CURRENT text
    exactly (whitespace included) and be unique unless replace_all is true.
    Later edits see the result of earlier ones.
    """
    if not isinstance(edits, list) or not edits:
        return None, "'edits' must be a non-empty list of {old, new, replace_all?} objects."
    out = code
    for i, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            return None, f"edit {i}: must be an object with 'old' and 'new'."
        old, new = e.get("old"), e.get("new", "")
        if not isinstance(old, str) or old == "":
            return None, f"edit {i}: 'old' must be a non-empty string."
        if not isinstance(new, str):
            return None, f"edit {i}: 'new' must be a string (use \"\" to delete)."
        n = out.count(old)
        if n == 0:
            return None, (f"edit {i}: 'old' text was not found. It must match the live document exactly, "
                          "including whitespace and indentation.")
        replace_all = bool(e.get("replace_all"))
        if n > 1 and not replace_all:
            return None, (f"edit {i}: 'old' matches {n} places. Add surrounding context to make it "
                          "unique, or set replace_all=true.")
        out = out.replace(old, new) if replace_all else out.replace(old, new, 1)
    if not out.strip():
        return None, "the edits would leave the document empty."
    return out, None


def edit_dynamic_background(edits: list, title: str = "") -> str:
    """
    Edit the Dynamic Background that is CURRENTLY showing, in place, with
    exact-text replacements -- far cheaper than resending the whole document
    through create_dynamic_background for a tweak (a colour, a speed, a
    particle count, one extra effect). The edited version goes live at once.

    `edits` is a list of {old, new, replace_all?}. `old` must match the live
    document's text exactly (whitespace included) and occur once, unless
    replace_all is true; `new` replaces it ("" deletes). Edits apply in
    order, each seeing the result of the previous, and the call is
    all-or-nothing: if any edit fails nothing changes and the error says
    which one. `title` optionally renames it.

    Requires an active Dynamic Background. Editing never touches saved
    copies on disk: if the live one had been saved, call
    save_dynamic_background(name) again (same name overwrites) to keep the
    change. There is still no tool for switching to a saved one.
    """
    return _call_hook("edit", {"edits": edits, "title": title})


# =============================================================================
