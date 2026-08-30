import getpass
import hashlib
import os
import shutil
import subprocess
import platform
import sys
import time
from pathlib import Path
import json

# ANSI escape codes
RED = "\033[91m"
HEADER = "\033[95m"
INFO = "\033[94m"
SUCCESS = "\033[92m"
WARNING = "\033[93m"
RESET = "\033[0m"

def log_error(message: str): print(f"{RED}[ERROR]{RESET} {message}")
def log_info(message: str): print(f"{INFO}[INFO]{RESET} {message}")
def log_success(message: str): print(f"{SUCCESS}[SUCCESS]{RESET} {message}")
def log_warn(message: str): print(f"{WARNING}[WARN]{RESET} {message}")

# Default launcher config location (home directory). This is the *global*
# launcher config - it just remembers where the Midum repo was cloned so we
# never have to ask the user for the path more than once.
DEFAULT_CONFIG_DIR = Path.home() / ".midum"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "launcher_config.json"

# Name of the requirements file relative to the repo root, and where the
# app entrypoint lives once cloned.
REQUIREMENTS_REL_PATH = Path("midum_pkg") / "requirements.txt"
APP_ENTRY_REL_PATH = Path("midum_pkg") / "gui" / "app.py"


def _repo_config_path(repo_root: Path) -> Path:
    """Return the path to the config.json inside the given repo's .midum folder."""
    return repo_root / ".midum" / "config.json"


def _ensure_config_path(path: Path) -> Path:
    """Make sure the parent directory exists and return the file path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def create_launcher_config(default_data: dict | None = None, repo_root: Path | None = None) -> Path:
    """Ensure the launcher config file exists.

    If *repo_root* is provided, the config file will be ``repo_root/.midum/config.json``.
    Otherwise the default home‑based path is used.
    """
    config_path = _repo_config_path(repo_root) if repo_root else DEFAULT_CONFIG_PATH
    _ensure_config_path(config_path)
    if not config_path.exists():
        _save_full_config(default_data if default_data is not None else {}, repo_root)
    return config_path


def _load_full_config(repo_root: Path | None = None) -> dict:
    """Load the entire JSON config.

    Uses the repo‑specific config if *repo_root* is given; otherwise falls back to the
    home‑based config.
    """
    config_path = _repo_config_path(repo_root) if repo_root else DEFAULT_CONFIG_PATH
    create_launcher_config(repo_root=repo_root)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log_error(f"Failed to read launcher config: {e}")
        return {}


def _save_full_config(data: dict, repo_root: Path | None = None) -> bool:
    """Overwrite the JSON config with *data*.

    Writes to the repo‑specific config when *repo_root* is supplied.
    """
    config_path = _repo_config_path(repo_root) if repo_root else DEFAULT_CONFIG_PATH
    _ensure_config_path(config_path)
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        return True
    except OSError as e:
        log_error(f"Failed to write launcher config: {e}")
        return False


def read_launcher_config(key: str, default=None, repo_root: Path | None = None):
    """Read a single entry from the config JSON.

    The optional *repo_root* selects the .midum/config.json inside a cloned repository.
    """
    config = _load_full_config(repo_root)
    return config.get(key, default)


def write_launcher_config(key: str, value, repo_root: Path | None = None) -> bool:
    """Write a single entry to the config JSON without altering other keys.

    If *repo_root* is supplied the operation targets ``repo_root/.midum/config.json``.
    """
    config = _load_full_config(repo_root)
    config[key] = value
    return _save_full_config(config, repo_root)


# --------------------------------------------------------------------------
# Git / Python prerequisite handling
# --------------------------------------------------------------------------

def is_git_installed() -> bool:
    """Check whether Git is available on the system."""
    git_path = shutil.which("git")
    if not git_path:
        return False
    try:
        result = subprocess.run([git_path, "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def install_git() -> bool:
    os_type = platform.system()
    cmd = []
    if os_type == "Windows":
        cmd = ["winget", "install", "--id", "Git.Git", "-e", "--source", "winget", "--accept-source-agreements", "--accept-package-agreements"]
    elif os_type == "Darwin":
        if not shutil.which("brew"):
            log_error("Installation failed: Homebrew required on macOS.")
            return False
        cmd = ["brew", "install", "git"]
    elif os_type == "Linux":
        if shutil.which("apt-get"):
            cmd = ["sudo", "apt-get", "install", "-y", "git"]
        elif shutil.which("dnf"):
            cmd = ["sudo", "dnf", "install", "-y", "git"]
        elif shutil.which("pacman"):
            cmd = ["sudo", "pacman", "-S", "--noconfirm", "git"]
        elif shutil.which("zypper"):
            cmd = ["sudo", "zypper", "install", "-y", "git"]
        else:
            log_error("Installation failed: No supported Linux package manager found.")
            return False
    else:
        log_error(f"Installation failed: Unsupported OS '{os_type}'.")
        return False
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        log_success("Git installation succeeded.")
        return True
    except subprocess.CalledProcessError as e:
        log_error(f"Installation failed: {e.stderr.strip() or str(e)}")
    except FileNotFoundError:
        log_error(f"Installation failed: Command '{cmd[0]}' not found.")
    except Exception as e:
        log_error(f"Installation failed: {e}")
    return False


def is_python_installed() -> bool:
    for cmd in ("python3", "python", "py"):
        try:
            result = subprocess.run([cmd, "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if result.returncode == 0:
                return True
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return False


def install_python() -> bool:
    os_type = platform.system()
    cmd = []
    if os_type == "Windows":
        cmd = ["winget", "install", "--id", "Python.Python.3", "-e", "--source", "winget", "--accept-source-agreements", "--accept-package-agreements"]
    elif os_type == "Darwin":
        if not shutil.which("brew"):
            log_error("Installation failed: Homebrew required on macOS.")
            return False
        cmd = ["brew", "install", "python"]
    elif os_type == "Linux":
        if shutil.which("apt-get"):
            cmd = ["sudo", "apt-get", "install", "-y", "python3", "python3-pip", "python3-venv"]
        elif shutil.which("dnf"):
            cmd = ["sudo", "dnf", "install", "-y", "python3", "python3-pip"]
        elif shutil.which("pacman"):
            cmd = ["sudo", "pacman", "-S", "--noconfirm", "python", "python-pip"]
        elif shutil.which("zypper"):
            cmd = ["sudo", "zypper", "install", "-y", "python3", "python3-pip"]
        else:
            log_error("Installation failed: No supported Linux package manager found.")
            return False
    else:
        log_error(f"Installation failed: Unsupported OS '{os_type}'.")
        return False
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        log_success("Python and pip installation succeeded.")
        return True
    except subprocess.CalledProcessError as e:
        log_error(f"Installation failed: {e.stderr.strip() or str(e)}")
    except FileNotFoundError:
        log_error(f"Installation failed: Command '{cmd[0]}' not found.")
    except Exception as e:
        log_error(f"Installation failed: {e}")
    return False


def ensure_prerequisites() -> bool:
    """Make sure Git and Python are available, offering to install them.

    Returns True if both are available (or were successfully installed),
    False if the user declined or installation failed.
    """
    if not is_git_installed():
        log_info("Git is not installed or not working properly.")
        if input("Allow Git installation? [y/n]: ").strip().lower() != "y":
            return False
        if not install_git() or not is_git_installed():
            log_error("Git still isn't available after installation. Please install it manually.")
            return False

    if not is_python_installed():
        log_info("Python is not installed or not working properly.")
        if input("Allow Python installation? [y/n]: ").strip().lower() != "y":
            return False
        if not install_python() or not is_python_installed():
            log_error("Python still isn't available after installation. Please install it manually (and restart your shell so PATH updates take effect).")
            return False

    return True


# --------------------------------------------------------------------------
# Repo location (asked once, then remembered)
# --------------------------------------------------------------------------

def get_remembered_repo_path() -> Path | None:
    """Return a previously-used repo path, if it's still valid, else None."""
    raw = read_launcher_config("repo_path", default=None)
    if not raw:
        return None
    path = Path(raw)
    if not path.exists() or not path.is_dir():
        log_warn(f"Previously configured repo path no longer exists: {path}")
        return None
    return path


def remember_repo_path(repo_root: Path) -> None:
    write_launcher_config("repo_path", str(repo_root))


def prompt_for_repo_path() -> Path:
    while True:
        raw = input("Enter the absolute path where you want to clone/use the Midum repository: ").strip().strip('"')
        path = Path(raw)
        if not path.is_absolute():
            log_error("Please enter an absolute path.")
            continue
        return path


def resolve_repo_path() -> Path:
    """Get the repo path to use, remembering it across launches so the user
    is only ever asked once."""
    remembered = get_remembered_repo_path()
    if remembered:
        log_info(f"Using previously configured Midum location: {remembered}")
        return remembered

    path = prompt_for_repo_path()
    path.mkdir(parents=True, exist_ok=True)
    remember_repo_path(path)
    return path


def forget_repo_path() -> None:
    """Clear the remembered repo path (e.g. if the user wants to relocate)."""
    write_launcher_config("repo_path", None)


# --------------------------------------------------------------------------
# Git operations
# --------------------------------------------------------------------------

def should_clone(repo_path: Path) -> bool:
    """Return True if the repo does not already have a config.json (i.e., needs cloning)."""
    return not _repo_config_path(repo_path).is_file()


def check_for_updates(repo_path: Path) -> bool:
    """Return True if the local repo is behind the remote origin."""
    try:
        subprocess.run(["git", "-C", str(repo_path), "fetch"], check=True, capture_output=True, text=True)
        status = subprocess.run(["git", "-C", str(repo_path), "status", "-uno"], capture_output=True, text=True, check=True)
        return "behind" in status.stdout.lower()
    except Exception as e:
        log_error(f"Update check failed: {e}")
        return False


def pull_updates(repo_path: Path) -> bool:
    """Pull latest changes from remote. Returns True on success."""
    try:
        subprocess.run(["git", "-C", str(repo_path), "pull"], check=True, capture_output=True, text=True)
        return True
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").lower()
        if "conflict" in stderr or "overwritten by merge" in stderr:
            log_error("Git pull failed due to local changes conflicting with the update.")
            log_info("Resolve manually, or discard local changes with: git reset --hard origin/main")
        else:
            log_error(f"Git pull failed: {e.stderr.strip() if e.stderr else str(e)}")
        return False
    except Exception as e:
        log_error(f"Git pull failed: {e}")
        return False


def clone_midum_repo(folder_path: str) -> int:
    """Clone the Midum repository into *folder_path* if not already present.

    Return codes:
        0 – cloned successfully
        1 – path not absolute
        2 – target does not exist
        3 – target not a directory
        4 – target not empty (when cloning is required)
        5 – git missing
        6 – git clone error
        7 – other error
        8 – already cloned (config exists)
    """
    path = Path(folder_path)
    if not path.is_absolute():
        log_error("Cloning failed: Provided path is not an absolute path.")
        return 1
    if not path.exists():
        log_error("Cloning failed: Target directory does not exist.")
        return 2
    if not path.is_dir():
        log_error("Cloning failed: Provided path points to a file, not a directory.")
        return 3
    if not should_clone(path):
        return 8
    try:
        if any(path.iterdir()):
            log_error("Cloning failed: Target directory exists but is not empty.")
            return 4
    except PermissionError as e:
        log_error(f"Cloning failed: Permission denied reading directory: {e}")
        return 6
    repo_url = "https://github.com/parth-satija/Midum.git"
    try:
        subprocess.run(["git", "clone", repo_url, str(path)], check=True, capture_output=True, text=True)
        log_success("Repository cloned successfully.")
        config_dir = path / ".midum"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.json"
        default_cfg = {
            "repo_path": str(path),
            "cloned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "repo_url": repo_url,
        }
        with open(config_path, "w", encoding="utf-8") as cfg:
            json.dump(default_cfg, cfg, indent=4)
        log_success(f"Config JSON created at {config_path}")
        return 0
    except FileNotFoundError:
        log_error("Cloning failed: 'git' executable not found.")
        return 5
    except subprocess.CalledProcessError as e:
        log_error(f"Cloning failed: {e.stderr.strip() if e.stderr else str(e)}")
        return 6
    except Exception as e:
        log_error(f"Cloning failed: {e}")
        return 7


def sync_repo(repo_root: Path) -> bool:
    """Make sure *repo_root* has a Midum checkout that is present and up to date.

    Handles both the "fresh clone" and "already cloned, maybe stale" cases.
    Returns True on success (repo is ready to run), False otherwise.
    """
    result = clone_midum_repo(str(repo_root))

    if result == 0:
        write_launcher_config("last_clone", "success", repo_root)
        return True

    if result == 8:
        log_info("Repository already present; checking for updates...")
        if check_for_updates(repo_root):
            log_info("Updates are available. Pulling latest changes...")
            if pull_updates(repo_root):
                log_success("Repository updated successfully.")
                write_launcher_config("updated_at", time.strftime("%Y-%m-%d %H:%M:%S"), repo_root)
            else:
                log_warn("Failed to pull updates; continuing with existing local copy.")
        else:
            log_success("Repository is up-to-date.")
        return True

    log_error(f"Could not prepare the Midum repository (code {result}).")
    return False


# --------------------------------------------------------------------------
# Virtual environment + dependency management (version agnostic)
# --------------------------------------------------------------------------

def _system_python() -> str:
    """Find a working Python interpreter on PATH, preferring python3."""
    for cmd in ("python3", "python", "py"):
        if shutil.which(cmd):
            return cmd
    return sys.executable


def _venv_paths(repo_root: Path) -> tuple[Path, Path]:
    """Return (venv_dir, venv_python) for the repo-local virtual environment."""
    venv_dir = repo_root / ".midum" / "venv"
    if platform.system() == "Windows":
        venv_python = venv_dir / "Scripts" / "python.exe"
    else:
        venv_python = venv_dir / "bin" / "python"
    return venv_dir, venv_python


def ensure_venv(repo_root: Path) -> Path | None:
    """Create the virtual environment if needed. Returns the venv's python path."""
    venv_dir, venv_python = _venv_paths(repo_root)
    if venv_python.exists():
        return venv_python

    log_info("Setting up an isolated Python environment for Midum (first run only)...")
    system_python = _system_python()
    try:
        subprocess.run([system_python, "-m", "venv", str(venv_dir)], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        log_error(f"Failed to create virtual environment: {e.stderr.strip() if e.stderr else str(e)}")
        return None
    except Exception as e:
        log_error(f"Failed to create virtual environment: {e}")
        return None

    if not venv_python.exists():
        log_error("Virtual environment creation reported success but the interpreter is missing.")
        return None

    log_success("Virtual environment created.")
    return venv_python


def _requirements_hash(requirements_path: Path) -> str | None:
    try:
        data = requirements_path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def ensure_dependencies(repo_root: Path, venv_python: Path) -> bool:
    """Install/upgrade dependencies from requirements.txt, skipping the (slow)
    pip install step if the requirements file hasn't changed since last time."""
    requirements_path = repo_root / REQUIREMENTS_REL_PATH
    if not requirements_path.is_file():
        log_warn(f"No requirements.txt found at {requirements_path}; skipping dependency install.")
        return True

    current_hash = _requirements_hash(requirements_path)
    installed_hash = read_launcher_config("requirements_hash", repo_root=repo_root)

    if current_hash and current_hash == installed_hash:
        log_info("Dependencies already up to date.")
        return True

    log_info("Installing/updating Python dependencies (this may take a while on first run)...")
    try:
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-r", str(requirements_path)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        log_error(f"Dependency installation failed: {e.stderr.strip() if e.stderr else str(e)}")
        return False
    except Exception as e:
        log_error(f"Dependency installation failed: {e}")
        return False

    log_success("Dependencies installed.")
    if current_hash:
        write_launcher_config("requirements_hash", current_hash, repo_root)
    return True


# --------------------------------------------------------------------------
# Tesseract OCR (optional, but HIGHLY RECOMMENDED)
# --------------------------------------------------------------------------
# Midum uses Tesseract for on-screen OCR (screen_capture.ocr_screen /
# fallback_find_text / fallback_click_text) so the agent can locate and
# click on text it sees on screen. Without it, Midum silently falls back to
# much less reliable grid-based clicking. It is NOT a hard requirement to
# launch Midum, so this step is skippable, but strongly encouraged.

def _tesseract_default_windows_path() -> Path:
    return Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


def is_tesseract_working(tesseract_cmd: str | None = None) -> bool:
    """Check whether Tesseract is reachable, either via an explicit path or PATH."""
    cmd = tesseract_cmd or shutil.which("tesseract")
    if not cmd or not (shutil.which(cmd) or Path(cmd).is_file()):
        return False
    try:
        result = subprocess.run([cmd, "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def install_tesseract() -> bool:
    """Best-effort automatic install via the platform's package manager."""
    os_type = platform.system()
    cmd = []
    if os_type == "Windows":
        cmd = ["winget", "install", "--id", "UB-Mannheim.TesseractOCR", "-e",
               "--source", "winget", "--accept-source-agreements", "--accept-package-agreements"]
    elif os_type == "Darwin":
        if not shutil.which("brew"):
            log_error("Installation failed: Homebrew required on macOS.")
            return False
        cmd = ["brew", "install", "tesseract"]
    elif os_type == "Linux":
        if shutil.which("apt-get"):
            cmd = ["sudo", "apt-get", "install", "-y", "tesseract-ocr"]
        elif shutil.which("dnf"):
            cmd = ["sudo", "dnf", "install", "-y", "tesseract"]
        elif shutil.which("pacman"):
            cmd = ["sudo", "pacman", "-S", "--noconfirm", "tesseract"]
        elif shutil.which("zypper"):
            cmd = ["sudo", "zypper", "install", "-y", "tesseract"]
        else:
            log_error("Installation failed: No supported Linux package manager found.")
            return False
    else:
        log_error(f"Installation failed: Unsupported OS '{os_type}'.")
        return False
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        log_success("Tesseract installation succeeded.")
        return True
    except subprocess.CalledProcessError as e:
        log_error(f"Installation failed: {e.stderr.strip() if e.stderr else str(e)}")
    except FileNotFoundError:
        log_error(f"Installation failed: Command '{cmd[0]}' not found.")
    except Exception as e:
        log_error(f"Installation failed: {e}")
    return False


def resolve_tesseract_path() -> str | None:
    """Find, install, or ask for a Tesseract location; remembers the answer
    globally so the user is only ever asked about this once. Returns an
    explicit path/command to use (or None to rely on PATH / skip), and
    True/False/None isn't used here since "skipped" and "on PATH" are both
    represented by returning None but with different config markers.
    """
    remembered_path = read_launcher_config("tesseract_path", default=None)
    remembered_state = read_launcher_config("tesseract_state", default=None)

    # We've already resolved this before — reuse the answer without asking again.
    if remembered_state == "skipped":
        return None
    if remembered_state == "on_path" and is_tesseract_working():
        return None
    if remembered_state == "custom_path" and remembered_path and is_tesseract_working(remembered_path):
        return remembered_path

    # Already installed and discoverable — no need to ask anything.
    if is_tesseract_working():
        log_success("Tesseract OCR detected on PATH.")
        write_launcher_config("tesseract_state", "on_path")
        return None

    if platform.system() == "Windows" and _tesseract_default_windows_path().is_file():
        default_path = str(_tesseract_default_windows_path())
        log_success(f"Tesseract OCR detected at {default_path}.")
        write_launcher_config("tesseract_state", "custom_path")
        write_launcher_config("tesseract_path", default_path)
        return default_path

    log_warn("Tesseract OCR was not found.")
    log_info("Tesseract is [HIGHLY RECOMMENDED]: Midum uses it to read and click on")
    log_info("on-screen text (OCR-based navigation). Without it, Midum falls back to")
    log_info("much less reliable click-by-grid-coordinate navigation.")

    choice = input(
        "Set up Tesseract now? [i]nstall automatically / [p]ath to existing install / [s]kip: "
    ).strip().lower()

    if choice.startswith("i"):
        if install_tesseract() and is_tesseract_working():
            log_success("Tesseract installed successfully.")
            write_launcher_config("tesseract_state", "on_path")
            return None
        # Automatic install may have worked but not landed on PATH yet
        # (common on Windows right after winget install) — check the
        # default location before giving up.
        if platform.system() == "Windows" and _tesseract_default_windows_path().is_file():
            default_path = str(_tesseract_default_windows_path())
            write_launcher_config("tesseract_state", "custom_path")
            write_launcher_config("tesseract_path", default_path)
            return default_path
        log_error("Automatic Tesseract installation failed or wasn't detected afterward.")
        log_info("You can download it manually from https://github.com/UB-Mannheim/tesseract/wiki")
        choice = input("Enter the path to tesseract now instead? [y/n]: ").strip().lower()
        if choice != "y":
            write_launcher_config("tesseract_state", "skipped")
            return None
        choice = "p"

    if choice.startswith("p"):
        while True:
            raw = input("Enter the full path to your tesseract executable: ").strip().strip('"')
            if not raw:
                write_launcher_config("tesseract_state", "skipped")
                return None
            if is_tesseract_working(raw):
                write_launcher_config("tesseract_state", "custom_path")
                write_launcher_config("tesseract_path", raw)
                log_success("Tesseract path verified and saved.")
                return raw
            log_error("That path didn't work (couldn't run 'tesseract --version'). Try again, or leave blank to skip.")

    log_warn("Skipping Tesseract setup — OCR-based screen navigation will be unavailable.")
    log_info("You can re-run the launcher's Tesseract setup later by clearing it from ~/.midum/launcher_config.json")
    write_launcher_config("tesseract_state", "skipped")
    return None


# --------------------------------------------------------------------------
# User secrets file (API keys) — created in LocalAppData, never in the repo
# --------------------------------------------------------------------------
# Mirrors the location config.py computes at import time, so the launcher
# can guarantee the file exists (with a blank template) *before* the app
# even starts, and so a first-time user gets a clear "go fill this in"
# message instead of a cryptic runtime KeyError from deep inside a provider
# backend. Kept as a self-contained copy (not `import config`) because the
# launcher intentionally runs with the *system* interpreter, before the
# venv (and therefore config.py's own dependencies) necessarily exist.

SECRETS_TEMPLATE = {
    "GEMINI_API_KEY": "",
    "GEMINI_API_KEY_2": "",
    "GEMINI_API_KEY_3": "",
    "GEMINI_API_KEY_4": "",
    "GEMINI_API_KEY_5": "",
    "OPENROUTER_API_KEY": "",
    "GROQ_API_KEY": "",
    "OLLAMA_API_KEY": "",
    "OMNIROUTE_API_KEY": "",
    "GEMINI_SECURE_1PSID": "",
    "GEMINI_SECURE_1PSIDTS": "",
}

# The keys we actually prompt for interactively (a subset of
# SECRETS_TEMPLATE) — one per real "get an API key" provider, each with a
# one-line hint of where to get it and whether it's a required core key or
# an optional numbered fallback. The two GEMINI_SECURE_* cookie values are
# deliberately excluded: they're browser session cookies, not something a
# user should be pasting into a terminal prompt, and are better set from
# the app's own Settings UI.
PROMPTABLE_SECRETS = [
    ("GEMINI_API_KEY", "Gemini API key", "https://aistudio.google.com/app/apikey"),
    ("OPENROUTER_API_KEY", "OpenRouter API key", "https://openrouter.ai/keys"),
    ("GROQ_API_KEY", "GroqCloud API key (free tier)", "https://console.groq.com/keys"),
    ("OLLAMA_API_KEY", "Ollama Cloud API key", "https://ollama.com/settings/keys"),
    ("OMNIROUTE_API_KEY", "OmniRoute gateway key (optional, local gateway)", "http://localhost:20128/dashboard"),
]


def _secrets_file_path() -> Path:
    """Return the platform-appropriate path for the user's secrets file.

    Matches config.py's SECRETS_FILE exactly so both the launcher and the
    running app agree on one location:
      - Windows: %LocalAppData%\\JarvisSecrets\\jarvis_secrets.json
      - macOS:   ~/Library/Application Support/JarvisSecrets/jarvis_secrets.json
      - Linux:   ~/.config/JarvisSecrets/jarvis_secrets.json
    """
    os_type = platform.system()
    home = Path.home()
    if os_type == "Windows":
        local_app_data = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        return local_app_data / "JarvisSecrets" / "jarvis_secrets.json"
    elif os_type == "Darwin":
        return home / "Library" / "Application Support" / "JarvisSecrets" / "jarvis_secrets.json"
    else:
        return home / ".config" / "JarvisSecrets" / "jarvis_secrets.json"


def ensure_secrets_file() -> Path:
    """Create the secrets file with a blank template if it doesn't exist yet.

    Never overwrites an existing file (so real keys already filled in are
    left untouched). Returns the resolved path either way.
    """
    path = _secrets_file_path()
    if path.exists():
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(SECRETS_TEMPLATE, f, indent=2)
        log_success(f"Created a blank secrets file at {path}")
        log_info("Fill in your API keys there (or from the app's Settings tab) before using a cloud provider.")
    except OSError as e:
        log_error(f"Failed to create secrets file at {path}: {e}")
    return path


def _load_secrets(path: Path) -> dict:
    """Read the secrets file, tolerating a missing/corrupt file by falling
    back to a blank template rather than crashing the launcher."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("secrets file did not contain a JSON object")
        return data
    except (OSError, json.JSONDecodeError, ValueError) as e:
        log_error(f"Failed to read secrets file at {path}: {e}")
        return dict(SECRETS_TEMPLATE)


def _save_secrets(path: Path, data: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except OSError as e:
        log_error(f"Failed to write secrets file at {path}: {e}")
        return False


def prompt_for_missing_secrets(path: Path) -> None:
    """Interactively ask for any API keys that are still blank in the
    secrets file and write whatever the user provides straight back into
    it. Purely local: values only ever move between this terminal prompt
    and the JSON file on disk at *path* — nothing here sends them anywhere.

    Only asked once per machine (tracked in the global launcher config, the
    same pattern used for Tesseract) so returning users aren't nagged on
    every launch; entries left blank stay blank and can always be filled in
    later by re-running with `--setup-keys`, editing the file directly, or
    from the app's own Settings tab.
    """
    if read_launcher_config("secrets_prompted", default=False):
        return

    data = _load_secrets(path)
    missing = [(k, label, url) for (k, label, url) in PROMPTABLE_SECRETS if not str(data.get(k, "")).strip()]

    if not missing:
        write_launcher_config("secrets_prompted", True)
        return

    log_info("Midum can use one or more cloud AI providers. You can enter API keys now")
    log_info("(stored locally in your secrets file, never transmitted anywhere by the")
    log_info("launcher) or skip and fill them in later.")

    changed = False
    for key, label, url in missing:
        prompt = f"{label} [{url}] (Enter to skip): "
        try:
            value = getpass.getpass(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            log_warn("Skipping remaining key prompts.")
            break
        if value:
            data[key] = value
            changed = True
            log_success(f"Saved {label}.")

    if changed:
        if _save_secrets(path, data):
            log_success(f"Updated secrets file at {path}")
    write_launcher_config("secrets_prompted", True)


# --------------------------------------------------------------------------
# Launching the app
# --------------------------------------------------------------------------

def launch_app(repo_root: Path, venv_python: Path, tesseract_path: str | None = None, secrets_path: Path | None = None) -> int:
    """Launch midum_pkg/gui/app.py using the venv's interpreter.

    Returns the app's exit code (0 on clean exit).
    """
    app_entry = repo_root / APP_ENTRY_REL_PATH
    if not app_entry.is_file():
        log_error(f"Could not find the app entrypoint at {app_entry}.")
        return 1

    log_info("Launching Midum...")
    env = os.environ.copy()
    # Make sure Midum's own package directory is importable regardless of
    # the current working directory the launcher was started from.
    package_dir = str(repo_root / "midum_pkg")
    env["PYTHONPATH"] = os.pathsep.join([package_dir, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    if tesseract_path:
        # Read by ui_automation/windows_uia.py to point pytesseract at a
        # non-default Tesseract install.
        env["MIDUM_TESSERACT_PATH"] = tesseract_path
    if secrets_path:
        # Tells config.py exactly which secrets file the launcher already
        # resolved/created, so the app reads keys from the same place the
        # launcher just guaranteed exists, instead of recomputing (and
        # potentially disagreeing on) the path itself.
        env["MIDUM_SECRETS_FILE"] = str(secrets_path)

    try:
        result = subprocess.run([str(venv_python), str(app_entry)], cwd=str(app_entry.parent), env=env)
        return result.returncode
    except KeyboardInterrupt:
        log_info("Midum was interrupted by the user.")
        return 130
    except Exception as e:
        log_error(f"Failed to launch Midum: {e}")
        return 1


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> int:
    if "--setup-keys" in sys.argv:
        write_launcher_config("secrets_prompted", False)

    if not ensure_prerequisites():
        log_error("Required prerequisites are missing. Exiting.")
        return 1

    repo_root = resolve_repo_path()

    if not sync_repo(repo_root):
        log_error("Could not prepare the Midum repository. Exiting.")
        return 1

    venv_python = ensure_venv(repo_root)
    if venv_python is None:
        log_error("Could not set up a Python environment for Midum. Exiting.")
        return 1

    if not ensure_dependencies(repo_root, venv_python):
        log_error("Could not install required dependencies. Exiting.")
        return 1

    tesseract_path = resolve_tesseract_path()
    secrets_path = ensure_secrets_file()
    prompt_for_missing_secrets(secrets_path)

    return launch_app(repo_root, venv_python, tesseract_path, secrets_path)


if __name__ == "__main__":
    sys.exit(main())
