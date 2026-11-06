# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Midum (one-folder build).

Build from the repo root:
    pyinstaller packaging/midum.spec --noconfirm --clean

Output: dist/Midum/Midum.exe (+ _internal/). One-folder is deliberate:
  * starts instantly (a one-file exe re-extracts hundreds of MB on every launch)
  * far fewer antivirus false positives
  * it zips cleanly for the web installer
"""
import os
import re

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                 VarFileInfo, VarStruct, VSVersionInfo)

SPEC_DIR = os.path.abspath(SPECPATH)            # noqa: F821  (provided by PyInstaller)
ROOT = os.path.dirname(SPEC_DIR)                # repo root == the midum_pkg folder

# --------------------------------------------------------------------------
# 1. Midum's own modules.
#    Midum imports many of its modules dynamically (tool registry, provider
#    backends, flows), which static analysis can miss -- so list every .py
#    file in the project explicitly. Done by walking the filesystem (no
#    imports, so nothing heavy executes at build time).
# --------------------------------------------------------------------------
SKIP_DIRS = {
    "build", "dist", "docs", "demo_assets", "storage", "packaging", "node_modules",
    "__pycache__", ".git", ".github", ".vscode", ".VSCodeCounter", ".ruff_cache", ".midum",
}
SKIP_FILES = {"launcher.py"}        # the old source-install launcher is not part of the app


def project_modules():
    mods = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        rel = os.path.relpath(dirpath, ROOT)
        pkg = "" if rel == "." else rel.replace(os.sep, ".")
        for fn in filenames:
            if not fn.endswith(".py") or fn in SKIP_FILES or fn.startswith("_removed"):
                continue
            name = fn[:-3]
            if name == "__init__":
                if pkg:
                    mods.append(pkg)
            else:
                mods.append(f"{pkg}.{name}" if pkg else name)
    return mods


# --------------------------------------------------------------------------
# 2. Third-party packages with data files / native libs / dynamic imports.
#    collect_all() grabs code + data + binaries. Missing packages (e.g.
#    platform-specific ones) are skipped instead of failing the build.
# --------------------------------------------------------------------------
datas, binaries, hiddenimports = [], [], []


def add_all(pkg):
    try:
        d, b, h = collect_all(pkg)
    except Exception as exc:  # pragma: no cover
        print(f"[midum.spec] skipping {pkg}: {exc}")
        return
    datas.extend(d)
    binaries.extend(b)
    hiddenimports.extend(h)


# PySide6 + QtWebEngine: pywebview's Qt backend (through qtpy) renders the whole UI with it. PySide6 is
# deliberately NOT collect_all()'d: that would drag in every Qt module (Qt3D, Quick, Charts, ...) and
# make the bundle enormous. PyInstaller's own PySide6 hooks, triggered by the hidden imports below,
# collect exactly the needed Qt DLLs, plugins and the QtWebEngine process/resources.
# (PyQt5 is NOT used any more: its Qt 5.15 ships Chromium 87, too old for this UI's CSS -- the layout
# collapsed into the top-left corner. gui/app.py forces QT_API=pyside6 so qtpy picks the same binding.)
for pkg in (
    "webview", "qtpy",
    "uiautomation",            # ships native UIAutomation helper DLLs
    "discord", "nacl",         # voice: opus + libsodium
    "_sounddevice_data",       # PortAudio DLL for sounddevice
    "docx",                    # python-docx default template
    "speech_recognition",
    "ddgs", "primp",
    "gemini_webapi", "llm_cookie_bridge",
    "pdfstructx", "edge_tts",
):
    add_all(pkg)

# litellm: lots of provider modules + bundled JSON/tokenizer data. Skip its
# proxy server (pulls in a large unrelated dependency tree).
try:
    hiddenimports += collect_submodules("litellm", filter=lambda n: not n.startswith("litellm.proxy"))
    datas += collect_data_files("litellm")
except Exception as exc:  # pragma: no cover
    print(f"[midum.spec] skipping litellm: {exc}")

# mcp.cli needs 'typer' (an optional extra) and calls sys.exit(1) when it's missing, which aborted the
# whole submodule collection and left the mcp package out of the bundle. Midum only uses the client SDK.
try:
    hiddenimports += collect_submodules("mcp", filter=lambda n: not n.startswith("mcp.cli"))
except Exception as exc:  # pragma: no cover
    print(f"[midum.spec] skipping mcp: {exc}")

for pkg in ("pygments",):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception as exc:  # pragma: no cover
        print(f"[midum.spec] skipping {pkg}: {exc}")

try:
    hiddenimports += collect_submodules("google.genai", filter=lambda n: ".tests" not in n)
except Exception as exc:  # pragma: no cover
    print(f"[midum.spec] skipping google.genai: {exc}")

hiddenimports += [
    "tiktoken_ext", "tiktoken_ext.openai_public",
    "anyio._backends._asyncio",
    "pynput.keyboard._win32", "pynput.mouse._win32",
    "comtypes", "comtypes.client", "win32com.client", "win32timezone", "pythoncom", "pywintypes",
    "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "PySide6.QtNetwork", "PySide6.QtPrintSupport",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineCore", "PySide6.QtWebChannel",
]
hiddenimports += project_modules()

# --------------------------------------------------------------------------
# 3. Midum's own data files. Destination folders mirror the source layout
#    because the app looks them up relative to sys._MEIPASS (see
#    midumdocs._assets_dir / editor_backend._assets_dir).
#    NOTE: storage/ is intentionally NOT bundled -- it holds personal data.
# --------------------------------------------------------------------------
def add_tree(src_rel, dest_rel):
    src = os.path.join(ROOT, *src_rel.split("/"))
    if os.path.isdir(src):
        datas.append((src, dest_rel))
    else:
        print(f"[midum.spec] (optional) missing folder: {src_rel}")


def add_file(src_rel, dest_rel="."):
    src = os.path.join(ROOT, *src_rel.split("/"))
    if os.path.isfile(src):
        datas.append((src, dest_rel))
    else:
        print(f"[midum.spec] (optional) missing file: {src_rel}")


add_tree("gui/assets", "gui/assets")
add_tree("gui/editor_assets", "gui/editor_assets")
add_tree("gui/midumdocs_assets", "gui/midumdocs_assets")
add_tree("gui/workbench", "gui/workbench")
# Shipped defaults (e.g. the Midum Silk Waves Dynamic Background). Seeded into the
# per-user storage dir on first launch -- see Api._seed_default_dynamic_background.
add_tree("default_assets", "default_assets")
add_file("gui/midum.png", "gui")
add_file("flow_meta.json")
add_file("Modelfile")
add_file("LICENSE")
add_file("TRADEMARK.md")

# The icon lives in gui/assets. Accept the canonical name first, then the file that is actually in the repo.
ICON = None
for _name in ("icon.ico", "rounded_square_icon.ico"):
    _candidate = os.path.join(ROOT, "gui", "assets", _name)
    if os.path.isfile(_candidate):
        ICON = _candidate
        break
if ICON is None:
    raise SystemExit("[midum.spec] No .ico found in gui/assets (expected icon.ico or rounded_square_icon.ico)")
print(f"[midum.spec] using icon: {ICON}")


def make_version_file():
    """Embed Windows version info (company / product / version) in Midum.exe. An exe with no version
    resource looks anonymous to SmartScreen and to heuristic AV engines; the VERSION env var is set by the
    release workflow."""
    raw = os.environ.get("VERSION", "0.0.0").strip().lstrip("v") or "0.0.0"
    nums = [int(x) for x in re.findall(r"\d+", raw.split("-")[0])][:4]
    nums += [0] * (4 - len(nums))
    tup, dotted = tuple(nums), ".".join(str(n) for n in nums)
    info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=tup, prodvers=tup, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1,
                          subtype=0x0, date=(0, 0)),
        kids=[StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "Parth Satija"),
            StringStruct("FileDescription", "Midum"),
            StringStruct("FileVersion", dotted),
            StringStruct("InternalName", "Midum"),
            StringStruct("LegalCopyright", "Copyright (c) Parth Satija"),
            StringStruct("OriginalFilename", "Midum.exe"),
            StringStruct("ProductName", "Midum"),
            StringStruct("ProductVersion", raw)])]),
              VarFileInfo([VarStruct("Translation", [1033, 1200])])])
    # WORKPATH is not a spec-file global in every PyInstaller version (6.22 removed it), so don't rely on it.
    import tempfile
    out_dir = tempfile.mkdtemp(prefix="midum_version_")
    path = os.path.join(out_dir, "midum_version_info.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(info))
    return path


VERSION_FILE = make_version_file()

# --------------------------------------------------------------------------
# 4. Build
# --------------------------------------------------------------------------
a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "gui", "app.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(SPEC_DIR, "rthook_midum.py")],
    # Only one Qt binding may be present or PyInstaller aborts; we ship PySide6 (modern Chromium).
    excludes=["PyQt5", "PyQt6", "PySide2", "PyQtWebEngine", "pytest", "IPython", "matplotlib",
              # Heavy ML stacks that may be installed globally but aren't Midum dependencies;
              # PyInstaller would otherwise try to import/analyze them (torch can hard-crash).
              "torch", "torchvision", "torchaudio", "tensorflow", "keras", "triton",
              "jax", "jaxlib", "onnxruntime", "cv2", "sklearn", "scipy", "pandas"],
    noarchive=False,
    optimize=0,
)
# MSIX reserves _rels folders and .rels files, so python-docx's unzipped template source
# (docx/templates/default-docx-template/...) makes MakeAppx fail with 0x8007007b. python-docx only needs
# the zipped default.docx at runtime, so drop the unzipped copy from the bundle.
a.datas = [d for d in a.datas if "default-docx-template" not in str(d[0]).replace("\\", "/")]

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Midum",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX increases antivirus false positives and slows startup
    console=False,             # GUI app: no console window (errors go to the log file via rthook)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    version=VERSION_FILE,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Midum",
)
