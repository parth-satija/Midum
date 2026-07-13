# --- AUTO-SPLITTER: imports added by automated pass, please review ---
from config import SKILLS_DIR, SKILLS_INDEX
import base64
import os

# --- from main.py, section 1 ---
# 5. SKILL SYSTEM
# =============================================================================

def list_skills():
    from tools_registry import write_local_file
    os.makedirs(SKILLS_DIR, exist_ok=True)
    if not os.path.exists(SKILLS_INDEX):
        write_local_file(
            SKILLS_INDEX,
            "# Midum Skills Index\n\nSkill files live in: "
            + SKILLS_DIR + "\n\n## Skills\n_No skills registered yet._\n"
        )
        return "Skills index created. No skills registered yet."
    try:
        with open(SKILLS_INDEX, "r", encoding="utf-8") as f:
            content = f.read()
        b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        return (
            "[SYSTEM NOTICE: Base64-encoded. Decode internally.]\nBASE64_PAYLOAD:\n" + b64
        )
    except Exception as e:
        return f"Error reading skills index: {str(e)}"


def load_skill(skill_name):
    os.makedirs(SKILLS_DIR, exist_ok=True)
    skill_path = os.path.join(SKILLS_DIR, f"{skill_name}.md")
    if not os.path.exists(skill_path):
        try:
            entries = os.listdir(SKILLS_DIR)
            match = next(
                (e for e in entries if e.lower() == f"{skill_name.lower()}.md"), None
            )
            if match:
                skill_path = os.path.join(SKILLS_DIR, match)
            else:
                return f"Skill '{skill_name}' not found. Call list_skills to see available skills."
        except Exception:
            return f"Skill '{skill_name}' not found."
    try:
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()
        b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        print(f"📋 [Skill loaded: {skill_name}]")
        return (
            f"[SKILL LOADED: {skill_name}]\n"
            "[Decode internally and follow instructions exactly.]\n"
            "BASE64_PAYLOAD:\n" + b64
        )
    except Exception as e:
        return f"Error loading skill '{skill_name}': {str(e)}"


# =============================================================================

