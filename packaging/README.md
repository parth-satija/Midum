# Packaging & releasing Midum (Windows)

How a release works end to end:

```
git tag v1.0.0  ->  publish a GitHub Release  ->  GitHub Actions builds:
   Midum-Setup.exe                 (~3-5 MB web installer, what users download)
   Midum-windows-x64.zip           (the real app, PyInstaller one-folder build)
   Midum-windows-x64.zip.sha256    (checksum the installer verifies)
```

`Midum-Setup.exe` downloads `releases/latest/download/Midum-windows-x64.zip`,
checks the SHA-256, extracts it, and adds Start Menu / desktop shortcuts and an
uninstaller. Because it always asks GitHub for the *latest* release, the
installer itself almost never has to change. Users can re-run it any time to
update (it closes a running Midum first, replaces program files, and leaves
user data alone).

Stable download link for your README / website:
`https://github.com/parth-satija/Midum/releases/latest/download/Midum-Setup.exe`

## Files

| File | Purpose |
|---|---|
| `packaging/midum.spec` | PyInstaller spec (one-folder, no console, all project modules + data listed explicitly) |
| `packaging/rthook_midum.py` | Runtime hook: sends stdout/stderr to `%LOCALAPPDATA%\Midum\logs\midum.log` |
| `packaging/installer/midum-web.iss` | Inno Setup script for the web installer |
| `.github/workflows/build-release.yml` | Builds + attaches everything on release; manual runs give a test artifact |

## Cutting a release

1. Merge to `main`.
2. GitHub -> Releases -> *Draft a new release* -> new tag like `v1.0.1` -> **Publish**.
3. Wait for the *Build and Release (Windows)* action (typically 15-30 min). The
   three files appear on the release automatically.

**Test first without publishing:** Actions tab -> *Build and Release (Windows)* ->
*Run workflow*. Download the artifact, run `Midum-Setup.exe`. (A test installer
downloads the *latest published* release, so for a true end-to-end test of a
new build, run `Midum.exe` from the zip directly.)

Mark pre-releases as "pre-release" on GitHub: `releases/latest` skips them, so
testers can't accidentally push an unfinished build to everyone.

## Building locally

```powershell
pip install -r requirements.txt pyinstaller
pyinstaller packaging/midum.spec --noconfirm --clean     # -> dist/Midum/Midum.exe
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" /DAppVersion=0.0.0-dev packaging\installer\midum-web.iss
```

If the app misbehaves, check `%LOCALAPPDATA%\Midum\logs\midum.log` (that's where
all console output goes in the installed build).

## Where things live in an installed build

| What | Where |
|---|---|
| Program files | `%LOCALAPPDATA%\Programs\Midum` (per-user install; no admin needed) |
| Chats, memory, skills, settings, webview profile | `%LOCALAPPDATA%\Midum\storage` |
| Logs | `%LOCALAPPDATA%\Midum\logs` |
| API keys (secrets file) | `%LOCALAPPDATA%\JarvisSecrets\jarvis_secrets.json` |

Override with `MIDUM_HOME` (all user data) or `MIDUM_STORAGE_DIR` (storage only).
Running from source (`python gui/app.py`) is unchanged and still uses `./storage`.

## Known limitations of the installed build

- **Flows can't be saved/edited.** `flows.py` compiles each flow into
  `flow_tools.py` and `flow_meta.json` *inside the package folder*; a frozen
  build can't regenerate Python source. Needs a redesign (store flows as JSON in
  user data and interpret them) before it works when installed.
- **Python sandbox tool.** Already patched to use a system Python from PATH (so users need Python installed for that one tool). Any *other* code that launches `sys.executable` would
  re-launch Midum.exe in a frozen build. Tools that run user Python should use
  a system Python found on PATH.
- **VS Code workbench ("Full IDE").** Built on demand with Node.js; only
  available if the user has Node installed.
- **Antivirus / SmartScreen.** Unsigned executables that hook the keyboard and
  automate the UI are commonly flagged. A code-signing certificate fixes most of
  it (see the comment in the workflow).
