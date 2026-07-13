# --- AUTO-SPLITTER: imports added by automated pass, please review ---
from config import COMMANDS_FILE, DOMAIN_INDEX, DOMAIN_SKILLS_INDEX, INSTRUCTIONS_FILE, MASTER_MEMORY, PATHS_FILE, RESPONSE_MEMORY, SESSION_MEMORY, SKILLS_INDEX, STARTUP_DIR
import os

# --- from main.py, section 1 ---
# 7. PATH RESOLVER
# =============================================================================

_SYSTEM_FILES = {
    os.path.normcase(COMMANDS_FILE),
    os.path.normcase(INSTRUCTIONS_FILE),
    os.path.normcase(PATHS_FILE),
    os.path.normcase(DOMAIN_INDEX),
    os.path.normcase(DOMAIN_SKILLS_INDEX),
    os.path.normcase(MASTER_MEMORY),
    os.path.normcase(SESSION_MEMORY),
    os.path.normcase(RESPONSE_MEMORY),
    os.path.normcase(SKILLS_INDEX),
}


def _is_absolute(path):
    return os.path.isabs(path) or (len(path) > 1 and path[1] == ":")


def resolve_file_path(path):
    """
    Resolve a relative path using safe BFS with plain Get-ChildItem at each level.
    Never uses -Recurse or -Filter. Stops as soon as the filename is matched.
    """
    MAX_EXPLORE_DEPTH = 4

    if not path:
        return path, ""
    if _is_absolute(path):
        return path, ""

    filename = os.path.basename(path)
    for sp in _SYSTEM_FILES:
        if os.path.basename(sp) == os.path.normcase(filename):
            return sp, ""

    print(f"   [Resolver] '{path}' is relative — BFS exploring under {STARTUP_DIR}...")

    import platform
    from collections import deque

    def _list_entries(dirpath):
        from tools_registry import execute_terminal_command
        try:
            if platform.system() == "Windows":
                ps = (
                    f"Get-ChildItem -Path '{dirpath}' | "
                    f"Select-Object Name,"
                    f"@{{n='D';e={{if($_.PSIsContainer){{'1'}}else{{'0'}}}}}} | "
                    f"ConvertTo-Csv -NoTypeInformation | Out-String"
                )
                out    = execute_terminal_command(ps)
                stdout = out.split("STDOUT:")[-1].split("STDERR:")[0].strip()
                entries = []
                for line in stdout.splitlines()[1:]:
                    line = line.strip().strip('"')
                    if not line:
                        continue
                    parts = [p.strip().strip('"') for p in line.split('","')]
                    if len(parts) >= 2:
                        name, is_dir = parts[0], parts[1] == "1"
                        entries.append((name, is_dir, os.path.join(dirpath, name)))
                return entries
            else:
                return [
                    (e, os.path.isdir(os.path.join(dirpath, e)), os.path.join(dirpath, e))
                    for e in os.listdir(dirpath)
                ]
        except Exception:
            return []

    queue   = deque([(STARTUP_DIR, 0)])
    visited = set()
    while queue:
        current_dir, depth = queue.popleft()
        if current_dir in visited or depth > MAX_EXPLORE_DEPTH:
            continue
        visited.add(current_dir)
        for name, is_dir, full_path in _list_entries(current_dir):
            if name.lower() == filename.lower() and not is_dir:
                msg = f"Resolved '{path}' -> '{full_path}'"
                print(f"   [Resolver] {msg}")
                return full_path, msg
            if is_dir and depth < MAX_EXPLORE_DEPTH:
                queue.append((full_path, depth + 1))

    msg = f"Could not find '{filename}' within {MAX_EXPLORE_DEPTH} levels of {STARTUP_DIR}. Path used as given."
    print(f"   [Resolver] {msg}")
    return path, msg


# =============================================================================

