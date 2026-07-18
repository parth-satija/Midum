# --- AUTO-SPLITTER: imports added by automated pass, please review ---
from config import STORAGE_DIR
import json
import os

# --- from main.py, section 1 ---
# TOOL-CALL PERMISSIONS
# =============================================================================
# Lets the user set, per tool (native or MCP), whether Midum may call it:
#
#   "always" — call it freely, no prompt (default for everything).
#   "ask"    — pop an Approve/Decline dialog (ask_user_approval) before
#              every call; proceeds only if the user approves.
#   "deny"   — never call it; the model is told it was blocked and to
#              explain that to the user instead of retrying.
#
# Native tools are keyed by their plain function name (e.g.
# "execute_terminal_command"). MCP tools are keyed as
# "mcp:<server>:<tool_name>" since the same tool name can exist on more
# than one connected server, and a server can be renamed/reconnected —
# the key always reflects the CURRENT server name at call time.
#
# Enforced in orchestration.py's process_chat_turn dispatch loop, right
# before a tool call is actually executed — see enforce_tool_permission().
# Storage is storage/permissions.json; only non-default ("ask"/"deny")
# entries are written, so an empty/missing file just means "everything
# is Always Allow", matching pre-existing behavior with zero migration.
# =============================================================================

PERMISSIONS_FILE = os.path.join(STORAGE_DIR, "permissions.json")

DEFAULT_LEVEL = "always"
VALID_LEVELS = ("always", "ask", "deny")

_permissions_cache: dict | None = None


def _load() -> dict:
    global _permissions_cache
    if _permissions_cache is not None:
        return _permissions_cache
    perms = {}
    try:
        if os.path.exists(PERMISSIONS_FILE):
            with open(PERMISSIONS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                perms = loaded
    except Exception:
        perms = {}
    _permissions_cache = perms
    return perms


def _save(perms: dict) -> None:
    global _permissions_cache
    _permissions_cache = perms
    try:
        os.makedirs(os.path.dirname(PERMISSIONS_FILE), exist_ok=True)
        with open(PERMISSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(perms, f, indent=2, sort_keys=True)
    except Exception as e:
        print(f"⚠️ [Permissions] Could not save {PERMISSIONS_FILE}: {e}")


def get_permission(tool_key: str) -> str:
    """Return the permission level for a tool key ('always' if not overridden)."""
    return _load().get(tool_key, DEFAULT_LEVEL)


def set_permission(tool_key: str, level: str) -> str:
    """Set (or clear, if level=='always') the permission level for a tool key."""
    tool_key = (tool_key or "").strip()
    if not tool_key:
        return "Error: a tool key is required."
    level = (level or "").strip().lower()
    if level not in VALID_LEVELS:
        return f"Error: '{level}' is not a valid permission level. Use one of: {', '.join(VALID_LEVELS)}."

    perms = dict(_load())
    if level == DEFAULT_LEVEL:
        # Keep the file minimal: default-level entries don't need storing.
        perms.pop(tool_key, None)
    else:
        perms[tool_key] = level
    _save(perms)
    return f"Permission for '{tool_key}' set to '{level}'."


def get_all_permissions() -> dict:
    """Return the raw stored overrides (any key not present here is 'always')."""
    return dict(_load())


def filter_tools_schema(tools_list: list) -> list:
    """
    Return `tools_list` with every entry whose permission is 'deny' removed,
    PLUS the schemas of any promoted MCP tools appended at the end.

    Promoted MCP tools (set via the Tools pane in the MCP tab -> Promote)
    get their full JSON schema included right alongside the native tools
    from here on, so the model can call them directly by name exactly like
    a native tool -- no list_mcp_servers()/show_server_tools()/
    call_mcp_tool() discovery hop needed. Dispatch already knows how to
    route a bare MCP tool name transparently (see
    _mcp_autoroute_tool_call in midum_mcp/tools.py), so no other wiring
    is required for this to work end to end.

    Lazy-imported to avoid a circular import (midum_mcp.tools imports
    get_permission/mcp_permission_key from this module already).
    """
    perms = _load()
    filtered = [
        t for t in tools_list
        if perms.get(t["function"]["name"], DEFAULT_LEVEL) != "deny"
    ]

    try:
        from midum_mcp.tools import get_promoted_tool_schemas
        native_names = {t["function"]["name"] for t in filtered}
        for promoted in get_promoted_tool_schemas():
            # A promoted tool whose name collides with a native tool (or a
            # tool from another server also promoted under the same name)
            # is skipped -- ambiguous direct-call names would be worse than
            # just leaving it to on-demand discovery via call_mcp_tool.
            name = promoted["function"]["name"]
            if name in native_names:
                continue
            native_names.add(name)
            filtered.append(promoted)
    except Exception as e:
        print(f"⚠️ [Permissions] Could not append promoted MCP tool schemas: {e}")

    return filtered


def reset_all_permissions() -> str:
    """Clear every override — everything goes back to 'Always Allow'."""
    _save({})
    return "All tool permissions reset to 'Always Allow'."


# Tools exempt from permission gating entirely. Only ask_user_approval
# itself needs this — gating it could otherwise let an 'ask' override on
# ask_user_approval require approving-the-approval-dialog, an unresolvable
# deadlock since there'd be no dialog left to grant it.
_PERMISSION_EXEMPT = {"ask_user_approval"}


def mcp_permission_key(server: str, tool_name: str) -> str:
    """Build the storage/lookup key for one tool on one MCP server."""
    return f"mcp:{server}:{tool_name}"


def enforce_tool_permission(perm_key: str, func_name: str, arguments: dict):
    """
    Check permission for a tool call about to be dispatched.

    Returns ("blocked", message) if the call must NOT proceed (denied
    outright, or the user declined an approval prompt) — the caller should
    use `message` as the tool's output instead of actually dispatching it.
    Returns ("allowed", None) if normal dispatch should continue.
    """
    if func_name in _PERMISSION_EXEMPT:
        return "allowed", None

    level = get_permission(perm_key)

    if level == "deny":
        return "blocked", (
            f"[PERMISSION DENIED] '{func_name}' is set to \"Don't Allow\" in the "
            f"Permissions tab. Do not retry it. Tell the user this action was "
            f"blocked, and that they can change the permission in the "
            f"Permissions tab if they want to allow it."
        )

    if level == "ask":
        from tools.user_prompt_tools import ask_user_approval
        try:
            details = ", ".join(f"{k}={str(v)[:80]}" for k, v in (arguments or {}).items())
        except Exception:
            details = ""
        decision = ask_user_approval(f"Allow Midum to run '{func_name}'?", details)
        if decision != "APPROVED":
            return "blocked", (
                f"[PERMISSION DECLINED] The user declined to approve '{func_name}' "
                f"for this call. Do not silently retry it — explain to the user "
                f"what you wanted to do and why, and ask if they'd like to "
                f"approve it."
            )

    return "allowed", None
