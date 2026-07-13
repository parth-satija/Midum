# --- AUTO-SPLITTER: imports added by automated pass, please review ---
from config import COMMANDS_FILE, DOMAIN_INDEX, DOMAIN_SKILLS_INDEX, GOAL_SECTION_END, GOAL_SECTION_HEADER, INSTRUCTIONS_FILE, MASTER_MEMORY, PATHS_FILE, RESPONSE_MEMORY, SESSION_MEMORY, SKILLS_DIR, SKILLS_INDEX, STORAGE_DIR, TARGET_DIR
from knowledge_base import _ensure_kb_files
from config import _IS_LINUX, _IS_WINDOWS
import datetime
import os
import re

# --- from main.py, section 1 ---
# 6. MEMORY SYSTEM
# =============================================================================

_active_project_memory_path = None
_current_goal               = None


def _memory_path_for_target(target):
    t = target.strip().lower()
    if t == "master":  return MASTER_MEMORY
    if t == "session": return SESSION_MEMORY
    if t == "project": return _active_project_memory_path
    return None


def update_memory(target, content):
    from tools_registry import append_local_file
    path = _memory_path_for_target(target)
    if not path:
        return f"Memory update skipped: no path for target '{target}'."
    ts    = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"[{ts}] {content.strip()}"
    result = append_local_file(path, entry)
    print(f"🧠 [Memory → {target}]: {content.strip()[:80]}{'...' if len(content)>80 else ''}")
    return result


def set_current_goal(goal, reason=""):
    from tools_registry import clear_response_memory
    global _current_goal
    ts         = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    clean_goal = goal.strip()
    old_goal   = _current_goal or "none"

    raw = (
        open(SESSION_MEMORY, "r", encoding="utf-8").read()
        if os.path.exists(SESSION_MEMORY)
        else f"# Midum Session Memory\nSession started: {ts}\n"
    )
    lines      = raw.splitlines(keepends=True)
    header_end = 0
    for i, line in enumerate(lines):
        header_end = i
        if i > 0 and line.strip() == "":
            header_end = i + 1
            break

    header_block = "".join(lines[:header_end])
    body         = re.sub(
        r"## Current Goal.*?(?=\n## |\Z)", "", "".join(lines[header_end:]), flags=re.DOTALL
    ).lstrip("\n")

    goal_block  = (
        f"{GOAL_SECTION_HEADER}\n_No active goal._\n\n"
        if clean_goal.lower() == "none"
        else f"{GOAL_SECTION_HEADER}\n{clean_goal}\n\n"
    )
    new_content = header_block + goal_block + body
    reason_note = f" ({reason.strip()})" if reason.strip() else ""
    h_entry     = f"\n[{ts}] [GOAL CHANGED] {old_goal!r} → {clean_goal!r}{reason_note}"

    if GOAL_SECTION_END not in new_content:
        new_content += f"\n{GOAL_SECTION_END}\n{h_entry}\n"
    else:
        new_content = new_content.replace(GOAL_SECTION_END, GOAL_SECTION_END + h_entry)

    try:
        with open(SESSION_MEMORY, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        return f"Error writing session memory: {str(e)}"

    _current_goal = clean_goal if clean_goal.lower() != "none" else None
    label = f"Goal set: {clean_goal}" if _current_goal else "Goal cleared."
    print(f"🎯 [{label}]")
    if clean_goal.lower() == "none":
        clear_response_memory()
    return f"Success: {label}"


def get_current_goal_from_file():
    if not os.path.exists(SESSION_MEMORY):
        return None
    try:
        content = open(SESSION_MEMORY, "r", encoding="utf-8").read()
        m = re.search(r"## Current Goal\n(.+?)(?=\n## |\Z)", content, re.DOTALL)
        if not m:
            return None
        g = m.group(1).strip()
        return None if (g == "_No active goal._" or not g) else g
    except Exception:
        return None


def load_memory_into_context(path, label):
    if not path or not os.path.exists(path):
        return None
    try:
        content = open(path, "r", encoding="utf-8").read().strip()
        return f"[MIDUM {label.upper()} MEMORY]\n{content}" if content else None
    except Exception:
        return None


def python_trigger_memory_update(turn_tool_outputs, assistant_reply):
    combined = " ".join(turn_tool_outputs).lower() + " " + assistant_reply.lower()
    update_memory("session", f"Turn summary: {assistant_reply.strip()[:200]}")

    if _IS_LINUX:
        for p in re.findall(r'/[\w./\-_]+', assistant_reply):
            if any(ext in p.lower() for ext in [".py", ".md", ".json", ".txt", ".sh"]):
                update_memory("master", f"Referenced file path: {p}")
                break
    else:
        for p in re.findall(r'[a-zA-Z]:\\[^\s\'"<>|?*]+', assistant_reply):
            if any(ext in p.lower() for ext in [".py", ".md", ".json", ".txt", ".exe", ".ps1"]):
                update_memory("master", f"Referenced file path: {p}")
                break

    m = re.search(r"stdout:\s*\n(.+)", combined)
    if m:
        update_memory("session", f"Terminal output: {m.group(1).strip()[:120]}")

    if _active_project_memory_path and "success:" in combined:
        if re.search(r"[a-zA-Z]:\\[^\s]+" if _IS_WINDOWS else r"/[\w./\-_]+", combined):
            update_memory("project", f"Action completed: {assistant_reply.strip()[:150]}")

    completion_signals = ["done", "completed", "finished", "task complete", "all done"]
    if _current_goal and any(s in combined for s in completion_signals):
        set_current_goal("none", reason="Python auto-detected completion")


def _bootstrap_all_files():
    """
    Create every folder and file Midum needs on first run.
    All calls are no-ops if the file already exists.
    Ship only main.py + gui.py — everything else is generated here.
    """
    # ── Directories ───────────────────────────────────────────────────────────
    for d in (TARGET_DIR, STORAGE_DIR, SKILLS_DIR):
        os.makedirs(d, exist_ok=True)

    # app_maps directory (used by UIA blueprint cache)
    os.makedirs(os.path.join(STORAGE_DIR, "app_maps"), exist_ok=True)

    # ── Helper: create a file only if it doesn't exist ────────────────────────
    def seed(path, content):
        if not os.path.exists(path):
            try:
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                print(f"⚠️  Could not create {path}: {e}")

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Core knowledge files ──────────────────────────────────────────────────
    if _IS_LINUX:
        seed(COMMANDS_FILE,
            "# Midum Commands\n"
            "# Add preferred bash commands below.\n"
            "# Format: `CommandName` — description\n\n"
            "## Commands\n"
            "- `ls` — list directory contents\n"
            "- `cd` — change directory\n"
            "- `find` — search for files\n"
            "- `grep` — search file contents\n"
            "- `xdg-open` — open a file or URL with the default app\n"
            "- `nohup` — run a command that persists after terminal closes\n"
        )
        seed(INSTRUCTIONS_FILE,
            "# Midum Instructions & Preferences\n"
            "# Add user preferences and behavioural rules below.\n"
            "# Format: one rule per line, starting with '- '\n\n"
            "## Preferences\n"
            "- Always use bash commands. Never use PowerShell or Windows commands.\n"
            "- Use xdg-open to launch apps and files.\n"
            "- Use nohup <command> & to launch GUI apps from the terminal.\n"
        )
    else:
        seed(COMMANDS_FILE,
            "# Midum Commands\n"
            "# Add preferred PowerShell commands below.\n"
            "# Format: `CommandName` — description\n\n"
            "## Commands\n"
            "- `Get-Location` — print the current working directory\n"
            "- `Get-ChildItem` — list folder contents\n"
            "- `Start-Process` — launch an application\n"
            "- `start` — shorthand to open files/apps\n"
        )
        seed(INSTRUCTIONS_FILE,
            "# Midum Instructions & Preferences\n"
            "# Add user preferences and behavioural rules below.\n"
            "# Format: one rule per line, starting with '- '\n\n"
            "## Preferences\n"
            "- Always use PowerShell commands, never CMD or Linux commands.\n"
            "- Use 'start' instead of 'Start-Process' when launching apps.\n"
        )

    seed(PATHS_FILE,
        "# Midum Paths\n"
        "# Absolute paths to applications, folders and files on this machine.\n\n"
        "## Paths\n"
    )

    seed(DOMAIN_INDEX,
        "# Midum Domain Knowledge Index\n"
        "# Registered domain-specific knowledge files.\n"
        "# Format: `filename_without_ext` - description\n\n"
        "## Files\n"
    )

    seed(DOMAIN_SKILLS_INDEX,
        "# Midum Domain Skills Index\n"
        "# Registered domain-specific skill files.\n"
        "# Format: [domain] `filename_without_ext` - description\n\n"
        "## Skills\n"
    )

    seed(SKILLS_INDEX,
        "# Midum Skills Index\n\n"
        "Each entry lists a skill filename and its description.\n"
        f"Skill files live in: {SKILLS_DIR}\n\n"
        "## Skills\n"
        "_No skills registered yet._\n"
    )

    # ── Memory files ──────────────────────────────────────────────────────────
    seed(MASTER_MEMORY,
        "# Midum Master Memory\n"
        f"Initialised: {ts}\n"
    )

    seed(SESSION_MEMORY,
        "# Midum Session Memory\n"
        f"Session started: {ts}\n\n"
        "## Current Goal\n"
        "_No active goal._\n\n"
        "## Goal History\n"
    )

    seed(RESPONSE_MEMORY, "")   # starts empty every run; cleared by set_current_goal(none)

    print("✅ [All Midum files and folders verified/created.]")


def init_memory_at_startup():
    from tools_registry import write_local_file
    global _active_project_memory_path, _current_goal
    _bootstrap_all_files()   # always runs first — safe no-op on subsequent launches
    os.makedirs(STORAGE_DIR, exist_ok=True)
    os.makedirs(SKILLS_DIR, exist_ok=True)
    _ensure_kb_files()
    injections = []

    master_ctx = load_memory_into_context(MASTER_MEMORY, "master")
    if master_ctx:
        injections.append(master_ctx)
        print("🧠 [Master memory loaded.]")
    else:
        print("🧠 [No master memory — starting fresh.]")
        if not os.path.exists(MASTER_MEMORY):
            write_local_file(MASTER_MEMORY, "# Midum Master Memory\n")

    if os.path.exists(SESSION_MEMORY):
        session_ctx = load_memory_into_context(SESSION_MEMORY, "session (continued)")
        if session_ctx:
            injections.append(session_ctx)
            print("🧠 [Session memory loaded.]")
        restored = get_current_goal_from_file()
        if restored:
            _current_goal = restored
            print(f"🎯 [Goal restored: {restored}]")
    else:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        write_local_file(
            SESSION_MEMORY,
            f"# Midum Session Memory\nSession started: {ts}\n\n"
            f"{GOAL_SECTION_HEADER}\n_No active goal._\n\n{GOAL_SECTION_END}\n"
        )
        print("🧠 [New session memory created.]")

    if os.path.exists(INSTRUCTIONS_FILE):
        try:
            with open(INSTRUCTIONS_FILE, "r", encoding="utf-8") as _f:
                _instr = _f.read().strip()
            if _instr:
                injections.append("[MIDUM INSTRUCTIONS — always active]\n" + _instr)
                print("📌 [Instructions loaded.]")
        except Exception:
            pass

    skill_count = len([f for f in os.listdir(SKILLS_DIR) if f.endswith(".md")])
    print(f"📋 [Skills: {skill_count} available]")

    print("\n──────────────────────────────────────────")
    print("📁 Active project (Enter to skip).")
    print("   Type a plain folder name, e.g.  jarvis_project")
    project_name = input("   > ").strip()
    print("──────────────────────────────────────────")

    # ── Validate project name ─────────────────────────────────────────────────
    # Reject anything that looks like a system path, drive root, or garbage.
    _INVALID_NAMES = {
        "", "$recycle.bin", "recycle.bin", "system volume information",
        "windows", "program files", "program files (x86)", "programdata",
        "users", "appdata", "temp", "tmp", "desktop", "documents",
        "downloads", "music", "pictures", "videos",
    }
    # Also reject if it contains path separators or starts with $ or .
    def _is_valid_project_name(name: str) -> bool:
        if not name:
            return False
        low = name.lower()
        if low in _INVALID_NAMES:
            return False
        if name.startswith(("$", ".", "\\", "/")):
            return False
        if os.sep in name or "/" in name or "\\" in name:
            return False
        # Must contain at least one alphanumeric character
        if not any(c.isalnum() for c in name):
            return False
        return True

    if project_name and not _is_valid_project_name(project_name):
        print(f"⚠️  '{project_name}' is not a valid project name — skipping.")
        project_name = ""

    if project_name:
        if _IS_LINUX:
            project_dir = os.path.join(os.path.expanduser("~"), "Projects", project_name)
        else:
            project_dir = os.path.join(r"D:\\", project_name)
        project_file = os.path.join(project_dir, "project_memory.md")
        _active_project_memory_path = project_file
        if os.path.exists(project_file):
            ctx = load_memory_into_context(project_file, f"project ({project_name})")
            if ctx:
                injections.append(ctx)
                print(f"🧠 [Project memory loaded: {project_file}]")
        else:
            os.makedirs(project_dir, exist_ok=True)
            write_local_file(
                project_file,
                f"# Project Memory: {project_name}\n"
                f"Created: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            )
            print(f"🧠 [New project memory: {project_file}]")
        update_memory("master", f"Active project: {project_name} ({project_dir})")
    else:
        print("🧠 [No active project.]")

    return injections


# =============================================================================

