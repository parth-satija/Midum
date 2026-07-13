# --- AUTO-SPLITTER: imports added by automated pass, please review ---
import main as midum

# --- from gui.py, section 1 ---
def _is_tool_line(raw_line: str) -> bool:
    from gui.app import _TOOL_LINE_EMOJI, _TOOL_LINE_KEYWORDS
    """True if a stdout line represents a tool call or major system event."""
    line = raw_line.strip()
    if not line:
        return False
    if line[0] in _TOOL_LINE_EMOJI:
        return True
    low = line.lower()
    return any(k in low for k in _TOOL_LINE_KEYWORDS)


# =============================================================================
# MANUAL TOOL SANDBOX DISPATCHER
# =============================================================================
# Mirrors process_chat_turn's real dispatch table in main.py exactly, so every
# tool in midum.tools behaves identically here as it would during a live
# agent turn — including the tools that have no 1:1 module-level function
# (snapshot/act route to CDP or UIA depending on target; several tools are
# methods on midum.ui_navigator, not the midum module itself).
# The generic fallback at the bottom means any NEW tool later added to
# main.py that doesn't need special routing works automatically with zero
# GUI changes — the tool list itself is already pulled live from midum.tools.
def _dispatch_midum_tool(tool_name: str, args: dict):
    # ── say() — sandbox-safe stand-in; doesn't touch real turn-scoped state ──
    if tool_name == "say":
        return f"[say] {args.get('message', '')}"

    # ── File I/O requiring relative-path resolution first ────────────────────
    if tool_name == "read_local_file":
        resolved, _ = midum.resolve_file_path(args.get("path", ""))
        return midum.read_local_file(resolved)
    if tool_name == "write_local_file":
        resolved, _ = midum.resolve_file_path(args.get("path", ""))
        return midum.write_local_file(resolved, args.get("content", ""))
    if tool_name == "append_local_file":
        resolved, _ = midum.resolve_file_path(args.get("path", ""))
        return midum.append_local_file(resolved, args.get("content", ""))

    # ── Unified snapshot/act — route to CDP (browser) or UIA (desktop) ───────
    # These have NO module-level midum.snapshot / midum.act function at all;
    # they only exist as inline routing inside process_chat_turn, which is
    # exactly why they silently failed before. Replicated here in full.
    if tool_name in ("snapshot", "snapshot_ui", "snapshot_browser_elements"):
        target      = args.get("target") or args.get("window_title", "")
        filter_type = args.get("filter_type", "")
        if target.lower().startswith("browser"):
            tab_index = 0
            if ":" in target:
                try:
                    tab_index = int(target.split(":", 1)[1])
                except ValueError:
                    pass
            return midum.snapshot_browser_elements(tab_index, filter_type)
        if midum.ui_navigator is None:
            return midum._uia_unavailable_message()
        return midum.ui_navigator.snapshot_ui(target, filter_type)

    if tool_name in ("act", "act_on_element", "act_on_browser_element"):
        target       = args.get("target") or args.get("window_title", "")
        index        = int(args.get("index", 0))
        action       = args.get("action", "click")
        text_to_type = args.get("text_to_type", "")
        if target.lower().startswith("browser"):
            tab_index = 0
            if ":" in target:
                try:
                    tab_index = int(target.split(":", 1)[1])
                except ValueError:
                    pass
            return midum.act_on_browser_element(index, action, text_to_type, tab_index)
        if midum.ui_navigator is None:
            return midum._uia_unavailable_message()
        return midum.ui_navigator.act_on_element_by_index(target, index, action, text_to_type)

    # ── Methods that live on ui_navigator, not the midum module itself ──────
    if tool_name in ("read_aggregated_text", "query_gemini_app", "manage_gemini_chat"):
        if midum.ui_navigator is None:
            return "UI automation is currently unavailable."
        return getattr(midum.ui_navigator, tool_name)(**args)

    # ── Screenshot — truncate the base64 payload so the textbox doesn't choke ─
    if tool_name == "fallback_view_screen":
        out = midum.capture_screen_to_ram()
        if isinstance(out, str) and len(out) > 1000 and not out.startswith("Error"):
            return (f"Screenshot captured to RAM ({len(out)} bytes of base64 data — "
                     f"hidden here to avoid GUI lag, but the tool itself is functional).")
        return out

    # ── Generic fallback — every remaining tool maps straight onto the
    # midum module or midum.ui_navigator by name. This is what makes new
    # tools "just work" automatically without touching the GUI again. ───────
    if hasattr(midum, tool_name):
        return getattr(midum, tool_name)(**args)
    if midum.ui_navigator is not None and hasattr(midum.ui_navigator, tool_name):
        return getattr(midum.ui_navigator, tool_name)(**args)

    return (f"Error: '{tool_name}' is registered in midum.tools but has no matching "
            f"function on the midum module or ui_navigator. This is a main.py gap, "
            f"not a GUI issue — add a module-level function or ui_navigator method "
            f"named '{tool_name}'.")


# =============================================================================
# REDIRECT stdout → GUI log
# =============================================================================

