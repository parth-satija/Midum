# =============================================================================
# FILESYSTEM MCP POLICY -- folder restriction, plus the password-store
# backing password-protected approval
# =============================================================================
#
# This module gates calls that are about to be dispatched to the Filesystem
# MCP server (the one connected as {"name": "filesystem", "command": "npx",
# "args": ["-y", "@modelcontextprotocol/server-filesystem", ...]} -- see
# midum_mcp/manager.py). It does NOT modify the external npm package itself;
# instead it wraps every call at the single choke point all MCP calls
# already flow through (_MCPManager.call_tool), which is enough to enforce
# folder restriction without touching the vendored server binary at all.
#
#   1. FOLDER RESTRICTION (enforced HERE, automatically, for every call) --
#      every path-shaped argument in the call is checked against an
#      allow-list of roots (defaults to whatever roots the server itself
#      was launched with -- see get_filesystem_policy()) and a deny-list of
#      specific sub-paths that stay off-limits even though they're nested
#      inside an allowed root (e.g. allow the whole project folder but
#      carve out its .git or a secrets/ directory). This applies
#      unconditionally and is model-visible: get_filesystem_mcp_policy(),
#      set_filesystem_mcp_allowed_roots(), etc. are ordinary model tools.
#
#   2. PASSWORD-PROTECTED APPROVAL is deliberately NOT triggered from this
#      module at all. It is wired ONLY into permissions.py's
#      enforce_tool_permission(), fired when a tool's Permissions-tab level
#      is "ask_password" -- something only the user sets (there is no model
#      tool that reads, sets, or even hints that this level or the password
#      store exist). This module supplies just the backing pieces
#      permissions.py calls into: request_password_approval() (pops
#      tools/user_prompt_tools.py's ask_user_password_approval dialog and
#      verifies it) and the set/clear/is-set password-hash functions below,
#      which are plain Python functions meant to be wired to a GUI
#      Permissions-tab control, never imported into the model's tool
#      namespace (see main.py/tools_schema.py -- they intentionally don't
#      appear there).
#
# Storage:
#   storage/filesystem_mcp_policy.json   -- allowed roots / blocked sub-paths
#   storage/filesystem_mcp_password.json -- PBKDF2-SHA256 hash + salt only,
#                                            the plaintext password is never
#                                            written to disk.
# =============================================================================

from config import STORAGE_DIR
import hashlib
import hmac
import json
import os

FS_POLICY_FILE   = os.path.join(STORAGE_DIR, "filesystem_mcp_policy.json")
FS_PASSWORD_FILE = os.path.join(STORAGE_DIR, "filesystem_mcp_password.json")

_DEFAULT_POLICY = {
    # Which connected MCP server name(s) this policy applies to.
    "filesystem_server_names": ["filesystem"],
    # Absolute roots the model may touch via the Filesystem MCP. Empty list
    # means "no extra restriction beyond whatever the server itself was
    # launched with" -- set this to actually narrow things further.
    "allowed_roots": [],
    # Absolute sub-paths that stay blocked even inside an allowed root.
    "blocked_subpaths": [],
}

# Argument keys that commonly carry a filesystem path across the reference
# Filesystem MCP server's tool set (read_text_file, write_file, edit_file,
# list_directory, move_file, search_files, create_directory, ...).
_PATH_ARG_KEYS = (
    "path", "source", "destination", "source_path", "destination_path",
    "root", "directory", "dir", "file_path", "old_path", "new_path",
)

_policy_cache: dict | None = None


def _load_policy() -> dict:
    global _policy_cache
    if _policy_cache is not None:
        return _policy_cache
    policy = dict(_DEFAULT_POLICY)
    try:
        if os.path.exists(FS_POLICY_FILE):
            with open(FS_POLICY_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                policy.update(loaded)
    except Exception as e:
        print(f"⚠️ [FS-MCP Policy] Could not read {FS_POLICY_FILE}: {e}")
    _policy_cache = policy
    return policy


def _save_policy(policy: dict) -> None:
    global _policy_cache
    _policy_cache = policy
    try:
        os.makedirs(os.path.dirname(FS_POLICY_FILE), exist_ok=True)
        with open(FS_POLICY_FILE, "w", encoding="utf-8") as f:
            json.dump(policy, f, indent=2)
    except Exception as e:
        print(f"⚠️ [FS-MCP Policy] Could not save {FS_POLICY_FILE}: {e}")


def get_filesystem_policy() -> dict:
    """Return the current policy dict (a copy -- mutate via the setters below)."""
    return dict(_load_policy())


def set_filesystem_allowed_roots(roots: list) -> str:
    """Restrict the Filesystem MCP to only these absolute root folders (in
    addition to whatever it was already launched with). Pass [] to remove
    the extra restriction and fall back to the server's own launch roots."""
    policy = dict(_load_policy())
    policy["allowed_roots"] = [os.path.normpath(r) for r in (roots or [])]
    _save_policy(policy)
    if policy["allowed_roots"]:
        return "Filesystem MCP restricted to:\n" + "\n".join(f"- {r}" for r in policy["allowed_roots"])
    return "Filesystem MCP folder restriction cleared -- falling back to the server's own launch roots."


# -----------------------------------------------------------------------------
# USER-MANAGED ALLOWED FOLDERS (Permissions tab dropdown)
# -----------------------------------------------------------------------------
# The Filesystem MCP server is launched with these folders as its roots (see
# midum_mcp/manager.py -> ensure_default_filesystem_server), and every call is
# additionally checked against them by check_folder_restriction() below.
# Default: exactly one entry -- Midum's own storage folder. The list can never
# be emptied, because the server needs at least one root to start.

def get_default_filesystem_root() -> str:
    """Midum's own storage folder -- the entry present by default."""
    return os.path.normpath(STORAGE_DIR)


def get_effective_allowed_roots() -> list:
    """Allowed folders as a clean list; falls back to Midum's storage folder
    if nothing is configured, so there is always at least one."""
    roots = []
    for r in (_load_policy().get("allowed_roots") or []):
        if isinstance(r, str) and r.strip():
            n = os.path.normpath(r.strip())
            if n not in roots:
                roots.append(n)
    return roots or [get_default_filesystem_root()]


def seed_default_allowed_roots() -> None:
    """Called at startup: if no allowed folders are configured yet, add the
    one default entry (Midum's storage folder)."""
    policy = dict(_load_policy())
    if not policy.get("allowed_roots"):
        policy["allowed_roots"] = [get_default_filesystem_root()]
        _save_policy(policy)


def add_allowed_root(path: str):
    """Add one folder to the allowed list. Returns (ok, message)."""
    if not path or not str(path).strip():
        return False, "A folder path is required."
    norm = os.path.normpath(os.path.abspath(str(path).strip()))
    if not os.path.isdir(norm):
        return False, f"'{norm}' is not an existing folder."
    roots = get_effective_allowed_roots()
    if any(_norm(norm) == _norm(r) for r in roots):
        return False, f"'{norm}' is already in the allowed folders."
    policy = dict(_load_policy())
    policy["allowed_roots"] = roots + [norm]
    _save_policy(policy)
    return True, f"Added '{norm}' to the allowed folders."


def remove_allowed_root(path: str):
    """Remove one folder from the allowed list. The last remaining entry
    cannot be removed. Returns (ok, message)."""
    roots = get_effective_allowed_roots()
    target = _norm(str(path or "").strip())
    remaining = [r for r in roots if _norm(r) != target]
    if len(remaining) == len(roots):
        return False, "That folder is not in the allowed list."
    if not remaining:
        return False, "At least one allowed folder is required, so the last one can't be removed."
    policy = dict(_load_policy())
    policy["allowed_roots"] = remaining
    _save_policy(policy)
    return True, "Removed from the allowed folders."


def add_filesystem_blocked_subpath(path: str) -> str:
    """Block one specific sub-path even if it sits inside an allowed root
    (e.g. a .git folder, a secrets/ directory, an .env file)."""
    if not path or not path.strip():
        return "Error: a path is required."
    policy = dict(_load_policy())
    norm = os.path.normpath(path.strip())
    blocked = list(policy.get("blocked_subpaths", []))
    if norm not in blocked:
        blocked.append(norm)
    policy["blocked_subpaths"] = blocked
    _save_policy(policy)
    return f"Blocked: {norm}"


def remove_filesystem_blocked_subpath(path: str) -> str:
    policy = dict(_load_policy())
    norm = os.path.normpath((path or "").strip())
    blocked = [p for p in policy.get("blocked_subpaths", []) if p != norm]
    policy["blocked_subpaths"] = blocked
    _save_policy(policy)
    return f"Unblocked: {norm}"


def set_filesystem_approval_tools(tool_names: list) -> str:
    """DEPRECATED / no longer used for anything: password approval is now
    triggered exclusively by setting a tool's Permissions-tab level to
    "ask_password" (see permissions.py), not by a filesystem-specific tool
    list. Kept only so an old saved policy.json with this key doesn't
    crash anything on load; it has no effect."""
    return ("No-op: password approval is now controlled per-tool from the "
            "Permissions tab (set the tool's level to \"ask_password\"), not here.")


def is_filesystem_server(server_name: str) -> bool:
    return server_name in _load_policy().get("filesystem_server_names", ["filesystem"])


# =============================================================================
# FOLDER RESTRICTION
# =============================================================================

def _extract_candidate_paths(arguments: dict) -> list:
    """Best-effort pull of every path-looking value out of a tool call's
    arguments, covering both single-path tools and list-of-paths tools
    (e.g. read_multiple_files' 'paths' array)."""
    if not isinstance(arguments, dict):
        return []
    found = []
    for key in _PATH_ARG_KEYS:
        val = arguments.get(key)
        if isinstance(val, str) and val.strip():
            found.append(val)
    paths_val = arguments.get("paths")
    if isinstance(paths_val, list):
        found.extend(p for p in paths_val if isinstance(p, str) and p.strip())
    return found


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _is_under(child: str, parent: str) -> bool:
    child, parent = _norm(child), _norm(parent)
    return child == parent or child.startswith(parent + os.sep)


def check_folder_restriction(tool_name: str, arguments: dict):
    """Returns (ok: bool, message: str|None). message is only set when
    ok is False, and explains exactly which path/rule blocked the call."""
    policy = _load_policy()
    allowed_roots   = policy.get("allowed_roots") or []
    blocked_subpaths = policy.get("blocked_subpaths") or []
    candidates = _extract_candidate_paths(arguments)

    if not candidates:
        return True, None  # nothing path-shaped in this call (e.g. list_allowed_directories)

    for raw_path in candidates:
        for blocked in blocked_subpaths:
            if _is_under(raw_path, blocked):
                return False, (
                    f"[FOLDER RESTRICTED] '{tool_name}' was blocked: '{raw_path}' falls under "
                    f"a restricted sub-path ('{blocked}'). Do not retry with a different phrasing -- "
                    f"tell the user this location is off-limits."
                )
        if allowed_roots:
            if not any(_is_under(raw_path, root) for root in allowed_roots):
                return False, (
                    f"[FOLDER RESTRICTED] '{tool_name}' was blocked: '{raw_path}' is outside every "
                    f"allowed root ({', '.join(allowed_roots)}). Tell the user this location is "
                    f"outside the folders Midum is allowed to touch."
                )
    return True, None


# =============================================================================
# PASSWORD-PROTECTED APPROVAL
# =============================================================================

_PBKDF2_ITERATIONS = 200_000


def _load_password_record() -> dict | None:
    try:
        if os.path.exists(FS_PASSWORD_FILE):
            with open(FS_PASSWORD_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("salt") and data.get("hash"):
                return data
    except Exception as e:
        print(f"⚠️ [FS-MCP Policy] Could not read {FS_PASSWORD_FILE}: {e}")
    return None


def is_filesystem_password_set() -> bool:
    return _load_password_record() is not None


def set_filesystem_mcp_password(password: str) -> str:
    """(Re)set the password required to approve gated Filesystem MCP tool
    calls (see set_filesystem_approval_tools). Only a salted PBKDF2 hash is
    ever written to disk -- the plaintext password is never persisted, and
    is never seen by the model in any tool result."""
    if not password or not password.strip():
        return "Error: password must not be empty."
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS).hex()
    try:
        os.makedirs(os.path.dirname(FS_PASSWORD_FILE), exist_ok=True)
        with open(FS_PASSWORD_FILE, "w", encoding="utf-8") as f:
            json.dump({"salt": salt, "hash": digest, "iterations": _PBKDF2_ITERATIONS}, f, indent=2)
    except Exception as e:
        return f"Error saving password: {e}"
    return "Filesystem MCP approval password set."


def clear_filesystem_mcp_password() -> str:
    try:
        if os.path.exists(FS_PASSWORD_FILE):
            os.remove(FS_PASSWORD_FILE)
    except Exception as e:
        return f"Error removing password: {e}"
    return ("Filesystem MCP approval password removed. Password-gated tools "
            "will now be blocked outright until a new password is set.")


def _verify_password(candidate: str) -> bool:
    record = _load_password_record()
    if record is None:
        return False
    try:
        digest = hashlib.pbkdf2_hmac(
            "sha256", (candidate or "").encode("utf-8"),
            bytes.fromhex(record["salt"]), int(record.get("iterations", _PBKDF2_ITERATIONS)),
        ).hex()
        return hmac.compare_digest(digest, record["hash"])
    except Exception:
        return False


def request_password_approval(tool_name: str, details: str = ""):
    """
    Pop the password-approval GUI dialog and verify what comes back. This is
    called from EXACTLY ONE place: permissions.py's enforce_tool_permission,
    when a tool's Permissions-tab level is "ask_password" -- a level the
    user sets, never the model. Every message this returns is deliberately
    worded identically to a plain "ask" decline/deny so nothing here ever
    reveals to the model (via a tool result it can read) that password-
    gated approval exists, that a password was involved, or whether one is
    configured.

    Returns (ok: bool, message: str|None) -- message is only set when
    ok is False.
    """
    if not is_filesystem_password_set():
        # Fail-safe, not fail-open: an "ask_password" tool with no password
        # configured yet is simply blocked, worded exactly like a decline.
        return False, (
            f"[PERMISSION DECLINED] The user declined to approve '{tool_name}' "
            f"for this call. Do not silently retry it — explain to the user "
            f"what you wanted to do and why, and ask if they'd like to approve it."
        )

    from tools.user_prompt_tools import ask_user_password_approval
    entered = ask_user_password_approval(f"Allow Midum to run '{tool_name}'?", details)
    if entered in ("[USER DECLINED]", "[USER CANCELLED]") or not _verify_password(entered):
        return False, (
            f"[PERMISSION DECLINED] The user declined to approve '{tool_name}' "
            f"for this call. Do not silently retry it — explain to the user "
            f"what you wanted to do and why, and ask if they'd like to approve it."
        )
    return True, None


# =============================================================================
# SINGLE ENTRY POINT -- called from midum_mcp.manager._MCPManager.call_tool
# =============================================================================

def enforce_filesystem_policy(server_name: str, tool_name: str, arguments: dict):
    """
    Returns ("allowed", None) or ("blocked", message).

    Folder restriction ONLY. Password-protected approval is intentionally
    NOT enforced here anymore -- it now lives entirely in permissions.py's
    enforce_tool_permission ("ask_password" level), which runs upstream in
    orchestration.py BEFORE a call ever reaches call_mcp_tool/this function.
    That keeps the password gate strictly a user-configured, per-tool
    Permissions-tab setting rather than something automatically triggered
    by tool name here -- and keeps this module blind to permission levels,
    same as before.

    No-ops entirely for any server not listed in policy["filesystem_server_names"].
    """
    if not is_filesystem_server(server_name):
        return "allowed", None

    ok, msg = check_folder_restriction(tool_name, arguments)
    if not ok:
        return "blocked", msg

    return "allowed", None
