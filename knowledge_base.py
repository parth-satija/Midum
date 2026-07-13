# --- AUTO-SPLITTER: imports added by automated pass, please review ---
from config import DOMAIN_INDEX, DOMAIN_SKILLS_INDEX, INSTRUCTIONS_FILE, PATHS_FILE, SKILLS_DIR, SKILLS_INDEX, STORAGE_DIR
import datetime
import os
import re

# --- from main.py, section 1 ---
# 4. KNOWLEDGE BASE — instructions.md, paths.md, domain files
# =============================================================================

def _ensure_kb_files():
    from tools_registry import write_local_file
    os.makedirs(STORAGE_DIR, exist_ok=True)
    if not os.path.exists(INSTRUCTIONS_FILE):
        write_local_file(INSTRUCTIONS_FILE,
            "# Midum Instructions & Preferences\n"
            "User preferences and behavioural rules.\n"
            "Format: one rule per line, starting with '- '.\n\n"
            "## Preferences\n")
    if not os.path.exists(PATHS_FILE):
        write_local_file(PATHS_FILE,
            "# Midum Paths\n"
            "Absolute paths to applications, folders and files.\n\n"
            "## Paths\n")
    if not os.path.exists(DOMAIN_INDEX):
        write_local_file(DOMAIN_INDEX,
            "# Midum Domain Knowledge Index\n"
            "Registered domain-specific knowledge files.\n"
            "Format: `filename_without_ext` - description\n\n"
            "## Files\n")
    if not os.path.exists(DOMAIN_SKILLS_INDEX):
        write_local_file(DOMAIN_SKILLS_INDEX,
            "# Midum Domain Skills Index\n"
            "Registered domain-specific skill files.\n"
            "Format: [domain] `filename_without_ext` - description\n\n"
            "## Skills\n")


def read_instructions():
    from tools_registry import read_local_file
    _ensure_kb_files()
    return read_local_file(INSTRUCTIONS_FILE)


def add_instruction(instruction):
    from tools_registry import append_local_file
    _ensure_kb_files()
    result = append_local_file(INSTRUCTIONS_FILE, f"- {instruction.strip()}")
    print(f"📌 [Instruction added]: {instruction.strip()[:80]}")
    return result


def read_paths():
    from tools_registry import read_local_file
    _ensure_kb_files()
    return read_local_file(PATHS_FILE)


def add_path(label, path, note=""):
    from tools_registry import append_local_file
    _ensure_kb_files()
    note_part = f"  _{note.strip()}_" if note.strip() else ""
    result = append_local_file(PATHS_FILE, f"- **{label.strip()}**: `{path.strip()}`{note_part}")
    print(f"📍 [Path added]: {label} -> {path}")
    return result


def create_domain_knowledge(name, description, initial_content=""):
    from tools_registry import append_local_file, write_local_file
    _ensure_kb_files()
    safe = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip().lower())
    fpath = os.path.join(STORAGE_DIR, f"{safe}.md")
    if os.path.exists(fpath):
        return f"Domain knowledge '{safe}.md' already exists at {fpath}."
    header = (f"# Domain Knowledge: {safe}\n_{description.strip()}_\n\n"
              f"Created: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
    write_local_file(fpath, header + (initial_content.strip() + "\n" if initial_content.strip() else ""))
    append_local_file(DOMAIN_INDEX, f"- `{safe}` - {description.strip()}")
    print(f"📚 [Domain knowledge created]: {safe}.md")
    return f"Success: created '{safe}.md' at {fpath} and registered in domain index."


def list_domain_knowledge():
    from tools_registry import read_local_file
    _ensure_kb_files()
    return read_local_file(DOMAIN_INDEX)


def read_domain_knowledge(name):
    from tools_registry import read_local_file
    _ensure_kb_files()
    safe  = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip().lower())
    fpath = os.path.join(STORAGE_DIR, f"{safe}.md")
    if not os.path.exists(fpath):
        try:
            match = next((e for e in os.listdir(STORAGE_DIR) if e.lower() == f"{safe}.md"), None)
            if match:
                fpath = os.path.join(STORAGE_DIR, match)
            else:
                return f"Domain knowledge '{safe}.md' not found. Call list_domain_knowledge."
        except Exception:
            return f"Domain knowledge '{safe}.md' not found."
    return read_local_file(fpath)


def create_domain_skill(name, domain, description, content):
    from tools_registry import append_local_file, write_local_file
    _ensure_kb_files()
    os.makedirs(SKILLS_DIR, exist_ok=True)
    safe  = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip().lower())
    fpath = os.path.join(SKILLS_DIR, f"{safe}.md")
    if os.path.exists(fpath):
        return f"Domain skill '{safe}.md' already exists."
    header = (f"# Domain Skill: {safe}\n**Domain**: {domain.strip()}\n"
              f"_{description.strip()}_\n\n"
              f"Created: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n---\n\n")
    write_local_file(fpath, header + content.strip() + "\n")
    entry = f"- [{domain.strip()}] `{safe}` - {description.strip()}"
    append_local_file(DOMAIN_SKILLS_INDEX, entry)
    append_local_file(SKILLS_INDEX, entry)
    print(f"📋 [Domain skill created]: {safe}.md (domain: {domain})")
    return f"Success: created '{safe}.md' registered in both indexes."


def list_domain_skills():
    from tools_registry import read_local_file
    _ensure_kb_files()
    return read_local_file(DOMAIN_SKILLS_INDEX)


# =============================================================================

