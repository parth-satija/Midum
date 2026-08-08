import shutil
import subprocess
import platform
import sys
import time
from pathlib import Path
import json

# Folder where Midum stores its launcher configuration
LAUNCHER_CONFIG_DIR = Path.home() / ".midum"
LAUNCHER_CONFIG_PATH = LAUNCHER_CONFIG_DIR / "launcher_config.json"


def create_launcher_config(default_data: dict | None = None) -> Path:
    """
    Ensures the launcher config folder and launcher_config.json file exist.

    Args:
        default_data: Optional dict to seed the config file with if it doesn't
            exist yet. Defaults to an empty dict.

    Returns:
        Path: The path to the launcher_config.json file.
    """
    LAUNCHER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if not LAUNCHER_CONFIG_PATH.exists():
        write_launcher_config(default_data if default_data is not None else {})

    return LAUNCHER_CONFIG_PATH


def _load_full_config() -> dict:
    """
    Internal helper: loads the entire launcher_config.json as a dict.
    Creates the file first if it doesn't exist.
    """
    create_launcher_config()

    try:
        with open(LAUNCHER_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Failed to read launcher config: {e}")
        return {}


def _save_full_config(data: dict) -> bool:
    """
    Internal helper: overwrites launcher_config.json with the entire given dict.
    """
    LAUNCHER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with open(LAUNCHER_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        return True
    except OSError as e:
        print(f"Failed to write launcher config: {e}")
        return False


def read_launcher_config(key: str, default=None):
    """
    Reads a single entry from launcher_config.json.

    Args:
        key: The top-level key to look up in the config file.
        default: Value to return if the key is not present. Defaults to None.

    Returns:
        The value stored under `key`, or `default` if not found.
    """
    config = _load_full_config()
    return config.get(key, default)


def write_launcher_config(key: str, value) -> bool:
    """
    Writes/overwrites a single entry in launcher_config.json without touching
    any other existing entries (appends the key if new, overwrites if it
    already exists).

    Args:
        key: The top-level key to set in the config file.
        value: The value to store under `key` (must be JSON-serializable).

    Returns:
        bool: True if the write succeeded, False otherwise.
    """
    config = _load_full_config()
    config[key] = value
    return _save_full_config(config)

def is_git_installed() -> bool:
    """
    Deterministically verifies whether Git is installed and functional.

    Returns:
        bool: True if Git is verified operational, False otherwise.
    """
    git_path = shutil.which("git")
    if not git_path:
        return False

    try:
        # Discard output to keep invocation lightweight while validating exit code
        result = subprocess.run(
            [git_path, "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False

def install_git():
    os_type = platform.system()
    cmd = []

    if os_type == "Windows":
        # Uses winget (built into modern Windows 10/11)
        cmd = [
            "winget", "install", 
            "--id", "Git.Git", 
            "-e", 
            "--source", "winget", 
            "--accept-source-agreements", 
            "--accept-package-agreements"
        ]

    elif os_type == "Darwin":
        # macOS via Homebrew
        if not shutil.which("brew"):
            print("Installation failed: Homebrew package manager ('brew') is required on macOS.")
            return
        cmd = ["brew", "install", "git"]

    elif os_type == "Linux":
        # Detect available Linux package manager
        if shutil.which("apt-get"):
            cmd = ["sudo", "apt-get", "install", "-y", "git"]
        elif shutil.which("dnf"):
            cmd = ["sudo", "dnf", "install", "-y", "git"]
        elif shutil.which("pacman"):
            cmd = ["sudo", "pacman", "-S", "--noconfirm", "git"]
        elif shutil.which("zypper"):
            cmd = ["sudo", "zypper", "install", "-y", "git"]
        else:
            print("Installation failed: No supported Linux package manager found (apt-get, dnf, pacman, or zypper).")
            return

    else:
        print(f"Installation failed: Unsupported OS '{os_type}'.")
        return

    try:
        # Run command and capture stderr for error reporting
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        print("Git installation succeeded.")
    except subprocess.CalledProcessError as e:
        error_details = e.stderr.strip() if e.stderr else str(e)
        print(f"Installation failed: {error_details}")
    except FileNotFoundError:
        print(f"Installation failed: Command '{cmd[0]}' not found in PATH.")
    except Exception as e:
        print(f"Installation failed: {e}")

def is_python_installed() -> bool:
    candidates = ["python3", "python", "py"]
    
    for cmd in candidates:
        try:
            result = subprocess.run(
                [cmd, "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False
            )
            if result.returncode == 0:
                return True
        except (FileNotFoundError, PermissionError, OSError):
            continue
            
    return False

def install_python():
    os_type = platform.system()
    cmd = []

    if os_type == "Windows":
        cmd = [
            "winget", "install", 
            "--id", "Python.Python.3", 
            "-e", 
            "--source", "winget", 
            "--accept-source-agreements", 
            "--accept-package-agreements"
        ]

    elif os_type == "Darwin":
        if not shutil.which("brew"):
            print("Installation failed: Homebrew package manager ('brew') is required on macOS.")
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
        error_details = e.stderr.strip() if e.stderr else str(e)
        print(f"Installation failed: {error_details}")
    except FileNotFoundError:
        print(f"Installation failed: Command '{cmd[0]}' not found in PATH.")
    except Exception as e:
        print(f"Installation failed: {e}")

def clone_midum_repo(folder_path: str) -> int:
    """
    Clones the Midum repository into the specified directory.

    Return Codes:
        0: Success
        1: Path is not absolute
        2: Path does not exist
        3: Path points to a file, not a directory
        4: Directory is not empty
        5: Git binary not found in system PATH
        6: Git clone execution error (network issue, permission denied, etc.)
        7: Unexpected general error
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

    # Check for non-empty directory (including hidden files/OS artifacts)
    try:
        if any(path.iterdir()):
            print("Cloning failed: Target directory exists but is not empty.")
            return 4
    except PermissionError as e:
        print(f"Cloning failed: Permission denied reading directory contents: {e}")
        return 6

    repo_url = "https://github.com/parth-satija/Midum.git"

    try:
        subprocess.run(
            ["git", "clone", repo_url, str(path)],
            check=True,
            capture_output=True,
            text=True
        )
        print("Repository cloned successfully.")
        return 0

    except FileNotFoundError:
        print("Cloning failed: 'git' executable not found in system PATH.")
        return 5

    except subprocess.CalledProcessError as e:
        error_details = e.stderr.strip() if e.stderr else str(e)
        print(f"Cloning failed: {error_details}")
        return 6

    except Exception as e:
        print(f"Cloning failed: {e}")
        return 7

def main():
    if is_git_installed():
        print("Git found. The launcher will now continue.")
    
        if is_python_installed():
            print("Python found. The launcher will now continue.")

            clone_path = input("Enter the absolute path where you want to clone the Midum repository: ").strip()
            clone_result = clone_midum_repo(clone_path)

        else: 
            print("Python is not installed or not working properly.")
            choice = input("Midum requires Python to function. Press confirm if you allow the installation of Python on your system [y/n]: ")
            if choice.lower() == "y":
                install_python()
                main()
            else:
                print("Installation cancelled/failed. This application requires Python to function. Please install Python manually and try again. This application will now exit in 10 seconds.")
                time.sleep(10)
                sys.exit(1)
    else:
        print("Git is not installed or not working properly.")
        choice = input("Midum requires Git to function. Press confirm if you allow the installation of Git on your system [y/n]: ")
        if choice.lower() == "y":
            install_git()
            main()
        else:
            print("Installation cancelled/failed. This application requires Git to function. Please install Git manually and try again. This application will now exit in 10 seconds.")
            time.sleep(10)
            sys.exit(1)

if __name__ == "__main__":
    main()
            