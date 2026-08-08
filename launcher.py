import shutil
import subprocess
import platform
import sys
import time
from pathlib import Path
import json

# Default launcher config location (home directory) – kept for backward compatibility
DEFAULT_CONFIG_DIR = Path.home() / ".midum"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "launcher_config.json"


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
        write_launcher_config(default_data if default_data is not None else {}, repo_root)
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
        print(f"Failed to read launcher config: {e}")
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
        print(f"Failed to write launcher config: {e}")
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


def install_git():
    os_type = platform.system()
    cmd = []
    if os_type == "Windows":
        cmd = ["winget", "install", "--id", "Git.Git", "-e", "--source", "winget", "--accept-source-agreements", "--accept-package-agreements"]
    elif os_type == "Darwin":
        if not shutil.which("brew"):
            print("Installation failed: Homebrew required on macOS.")
            return
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
            print("Installation failed: No supported Linux package manager found.")
            return
    else:
        print(f"Installation failed: Unsupported OS '{os_type}'.")
        return
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        print("Git installation succeeded.")
    except subprocess.CalledProcessError as e:
        print(f"Installation failed: {e.stderr.strip() or str(e)}")
    except FileNotFoundError:
        print(f"Installation failed: Command '{cmd[0]}' not found.")
    except Exception as e:
        print(f"Installation failed: {e}")


def is_python_installed() -> bool:
    for cmd in ("python3", "python", "py"):
        try:
            result = subprocess.run([cmd, "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if result.returncode == 0:
                return True
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return False


def install_python():
    os_type = platform.system()
    cmd = []
    if os_type == "Windows":
        cmd = ["winget", "install", "--id", "Python.Python.3", "-e", "--source", "winget", "--accept-source-agreements", "--accept-package-agreements"]
    elif os_type == "Darwin":
        if not shutil.which("brew"):
            print("Installation failed: Homebrew required on macOS.")
            return
        cmd = ["brew", "install", "python"]
    elif os_type == "Linux":
        if shutil.which("apt-get"):
            cmd = ["sudo", "apt-get", "install", "-y", "python3", "python3-pip"]
        elif shutil.which("dnf"):
            cmd = ["sudo", "dnf", "install", "-y", "python3", "python3-pip"]
        elif shutil.which("pacman"):
            cmd = ["sudo", "pacman", "-S", "--noconfirm", "python", "python-pip"]
        elif shutil.which("zypper"):
            cmd = ["sudo", "zypper", "install", "-y", "python3", "python3-pip"]
        else:
            print("Installation failed: No supported Linux package manager found.")
            return
    else:
        print(f"Installation failed: Unsupported OS '{os_type}'.")
        return
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        print("Python and pip installation succeeded.")
    except subprocess.CalledProcessError as e:
        print(f"Installation failed: {e.stderr.strip() or str(e)}")
    except FileNotFoundError:
        print(f"Installation failed: Command '{cmd[0]}' not found.")
    except Exception as e:
        print(f"Installation failed: {e}")


def should_clone(repo_path: Path) -> bool:
    """Return True if the repo does not already have a config.json (i.e., needs cloning)."""
    return not _repo_config_path(repo_path).is_file()


def check_for_updates(repo_path: Path) -> bool:
    """Return True if the local repo is behind the remote origin."""
    try:
        # Fetch latest changes
        subprocess.run(["git", "-C", str(repo_path), "fetch"], check=True, capture_output=True, text=True)
        status = subprocess.run(["git", "-C", str(repo_path), "status", "-uno"], capture_output=True, text=True, check=True)
        return "behind" in status.stdout.lower()
    except Exception as e:
        print(f"Update check failed: {e}")
        return False


def pull_updates(repo_path: Path) -> bool:
    """Pull latest changes from remote. Returns True on success."""
    try:
        subprocess.run(["git", "-C", str(repo_path), "pull"], check=True, capture_output=True, text=True)
        return True
    except Exception as e:
        print(f"Git pull failed: {e}")
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
        print("Cloning failed: Provided path is not an absolute path.")
        return 1
    if not path.exists():
        print("Cloning failed: Target directory does not exist.")
        return 2
    if not path.is_dir():
        print("Cloning failed: Provided path points to a file, not a directory.")
        return 3
    if not should_clone(path):
        print("Skipping clone: configuration already present.")
        return 8
    try:
        if any(path.iterdir()):
            print("Cloning failed: Target directory exists but is not empty.")
            return 4
    except PermissionError as e:
        print(f"Cloning failed: Permission denied reading directory: {e}")
        return 6
    repo_url = "https://github.com/parth-satija/Midum.git"
    try:
        subprocess.run(["git", "clone", repo_url, str(path)], check=True, capture_output=True, text=True)
        print("Repository cloned successfully.")
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
        print(f"Config JSON created at {config_path}")
        return 0
    except FileNotFoundError:
        print("Cloning failed: 'git' executable not found.")
        return 5
    except subprocess.CalledProcessError as e:
        print(f"Cloning failed: {e.stderr.strip() if e.stderr else str(e)}")
        return 6
    except Exception as e:
        print(f"Cloning failed: {e}")
        return 7


def main():
    if not is_git_installed():
        print("Git is not installed or not working properly.")
        if input("Allow Git installation? [y/n]: ").strip().lower() == "y":
            install_git()
            main()
        else:
            sys.exit(1)

    if not is_python_installed():
        print("Python is not installed or not working properly.")
        if input("Allow Python installation? [y/n]: ").strip().lower() == "y":
            install_python()
            main()
        else:
            sys.exit(1)

    clone_path = input("Enter the absolute path where you want to clone the Midum repository: ").strip()
    result = clone_midum_repo(clone_path)
    print(f"Clone result code: {result}")

    repo_root = Path(clone_path)
    if result == 0:
        # Fresh clone – write initial config entry
        write_launcher_config("last_clone", "success", repo_root)
        val = read_launcher_config("last_clone", repo_root=repo_root)
        print(f"Config entry 'last_clone': {val}")
    elif result == 8:
        print("Repository already cloned; using existing configuration.")
        # Check for updates
        if check_for_updates(repo_root):
            print("Updates are available. Pulling latest changes...")
            if pull_updates(repo_root):
                print("Repository updated successfully.")
                write_launcher_config("updated_at", time.strftime("%Y-%m-%d %H:%M:%S"), repo_root)
            else:
                print("Failed to pull updates.")
        else:
            print("Repository is up‑to‑date.")
        # Example read of existing config
        val = read_launcher_config("repo_path", repo_root=repo_root)
        print(f"Existing repo path from config: {val}")

if __name__ == "__main__":
    main()
