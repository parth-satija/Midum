# --- AUTO-SPLITTER: imports added by automated pass, please review ---
from config import SCREEN_H, SCREEN_W
from config import _IS_WINDOWS
import json
import os
import re
import time

# --- from main.py, section 1 ---
# TESSERACT SETUP
# =============================================================================
# Windows: download from https://github.com/UB-Mannheim/tesseract/wiki
# Linux:   sudo apt install tesseract-ocr   OR   sudo dnf install tesseract
#          pip install pytesseract
#
_TESSERACT_AVAILABLE = False

try:
    import pytesseract
    if _IS_WINDOWS:
        TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
    # On Linux, tesseract is on PATH after apt/dnf install — no path needed
    pytesseract.get_tesseract_version()
    _TESSERACT_AVAILABLE = True
except Exception:
    pass


# ── UI Automation (UIA) SETUP — Windows only ─────────────────────────────────
# pip install uiautomation pywin32
#
# NOTE: The `uiautomation` package sometimes prints a generic
# "update your Windows" message when its internal COM initialization
# (via comtypes) fails. That message is almost always misleading — the real
# cause is usually one of:
#   1. pywin32 post-install script never ran (fixes most import-time COM issues)
#      Fix: run `python Scripts/pywin32_postinstall.py -install` as admin
#   2. COM apartment threading conflict — a prior library already called
#      CoInitialize with a different threading model in this process
#   3. Running inside a sandboxed/restricted session without COM access
#   4. UIA COM components genuinely missing (rare on any Windows 8+ system)
# We capture the REAL underlying exception below so the startup print shows
# the actual cause instead of the library's generic advice.
_UIA_AVAILABLE   = False
_UIA_INIT_ERROR  = None
win32gui = None
auto     = None

if _IS_WINDOWS:
    try:
        import win32gui
    except ImportError as e:
        _UIA_INIT_ERROR = f"pywin32 not installed: {e}"

    if win32gui is not None:
        try:
            import uiautomation as auto
            # Force a real COM call now (not just import) so init failures
            # surface here instead of silently later during the first real
            # UIA operation deep inside a tool call.
            _ = auto.GetRootControl()
            _UIA_AVAILABLE = True
        except Exception as e:
            _UIA_INIT_ERROR = f"{type(e).__name__}: {e}"
            auto = None

            # ── Retry once with explicit COM apartment initialization ────────
            # Fixes the most common cause: another library (or Ollama's own
            # threading) already initialized COM with an incompatible model
            # before uiautomation got a chance to.
            try:
                import comtypes
                comtypes.CoInitialize()
                import importlib
                if "uiautomation" in globals():
                    importlib.reload(auto)
                else:
                    import uiautomation as auto
                _ = auto.GetRootControl()
                _UIA_AVAILABLE  = True
                _UIA_INIT_ERROR = None
            except Exception as e2:
                _UIA_INIT_ERROR = (
                    f"{_UIA_INIT_ERROR} | Retry with CoInitialize also failed: "
                    f"{type(e2).__name__}: {e2}"
                )
                auto = None

# Control types that can host meaningful child content — used as scan roots
_CONTAINER_TYPES = {
    "PaneControl", "GroupControl", "CustomControl", "ToolbarControl",
    "DocumentControl", "WindowControl", "TabControl", "TabItemControl",
    "TreeControl", "ScrollBarControl", "MenuBarControl", "MenuControl",
}
# Control types that represent something a user can interact with
_ACTIONABLE_TYPES = {
    "ButtonControl", "EditControl", "CheckBoxControl", "HyperlinkControl",
    "ListItemControl", "MenuItemControl", "TabItemControl", "RadioButtonControl",
    "ComboBoxControl", "SliderControl", "ImageControl", "SplitButtonControl",
    "TextControl",
}

if _UIA_AVAILABLE:
    class AppMapNavigator:
        """
        UIA navigator tuned for deep Electron/Chromium UI trees (VS Code, Modrinth,
        Discord, etc). These apps wrap real content in many layers of generic
        Pane/Group/Custom containers, often 8-15 levels deep, so shallow depth
        cutoffs (3-4) never reach anything actionable.

        Strategy:
          - discover_ui_subtrees: walk up to MAX_DISCOVER_DEPTH levels, collect
            ANY named/automation-id'd container as a candidate subtree, not just
            top-level ones. Electron apps nest meaningfully-named panes deep.
          - inspect_subtree_controls: walk up to MAX_INSPECT_DEPTH levels below
            a chosen subtree root, collecting all actionable controls regardless
            of how deep they sit.
          - If a subtree yields nothing, automatically retry one level shallower
            in the tree (parent) so Midum doesn't have to manually backtrack.
        """

        MAX_DISCOVER_DEPTH = 12
        MAX_INSPECT_DEPTH  = 12
        MAX_RESULTS        = 60   # cap result lists so they don't blow the context

        def __init__(self, maps_dir="storage/app_maps"):
            self.maps_dir = os.path.abspath(maps_dir)
            os.makedirs(self.maps_dir, exist_ok=True)
            self._live_cache     = {}
            self._snapshot_cache = {}   # window_title → last snapshot element list

        def _get_map_path(self, window_title: str) -> str:
            safe_name = "".join([c if c.isalnum() else "_" for c in window_title.lower()]).strip("_")
            return os.path.join(self.maps_dir, f"{safe_name}.json")

        def load_app_blueprint(self, window_title: str) -> dict:
            path = self._get_map_path(window_title)
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass
            return {"window_title": window_title, "subtrees": {}, "known_controls": {}}

        def save_app_blueprint(self, window_title: str, blueprint: dict):
            path = self._get_map_path(window_title)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(blueprint, f, indent=2)
            except Exception:
                pass

        # ── Known Windows shell surface class names ───────────────────────────
        # These windows exist permanently but have no useful title and are never
        # the foreground window. They must be found by class name, not title.
        # Keys are the canonical aliases the model can pass as window_title.
        _SHELL_ALIASES: dict[str, list[str]] = {
            # Primary taskbar (Win10 + Win11)
            "taskbar":          ["Shell_TrayWnd"],
            "start":            ["Shell_TrayWnd"],   # Start button lives inside
            "start button":     ["Shell_TrayWnd"],
            "start menu":       ["Windows.UI.Core.CoreWindow",   # Win11 Start
                                 "Shell_TrayWnd"],
            # Secondary taskbars on multi-monitor setups
            "taskbar2":         ["Shell_SecondaryTrayWnd"],
            "secondary taskbar":["Shell_SecondaryTrayWnd"],
            # System tray / notification area
            "tray":             ["Shell_TrayWnd"],
            "system tray":      ["Shell_TrayWnd"],
            "notification area":["Shell_TrayWnd"],
            "clock":            ["Shell_TrayWnd"],
            # Overflow popup for hidden tray icons
            "tray overflow":    ["NotifyIconOverflowWindow"],
            "overflow":         ["NotifyIconOverflowWindow"],
            # Desktop itself
            "desktop":          ["Progman", "WorkerW"],
            # Action Center / Quick Settings (Win11)
            "action center":    ["Windows.UI.Core.CoreWindow"],
            "quick settings":   ["Windows.UI.Core.CoreWindow"],
            "notifications":    ["Windows.UI.Core.CoreWindow"],
            # Search bar / Search panel
            "search":           ["SearchHost",
                                 "Windows.UI.Core.CoreWindow"],
            "search bar":       ["Shell_TrayWnd"],
            # Task View / Virtual Desktops button
            "task view":        ["Shell_TrayWnd"],
            # Volume / Wi-Fi / Battery flyout
            "volume":           ["Windows.UI.Core.CoreWindow",
                                 "Shell_TrayWnd"],
        }

        def _find_shell_hwnd(self, alias: str) -> int | None:
            """
            Find a Windows shell surface by its canonical alias.
            Tries each class name in order and returns the first valid hwnd.
            """
            for cls in self._SHELL_ALIASES.get(alias.lower(), []):
                h = win32gui.FindWindow(cls, None)
                if h:
                    return h
            return None

        # ── App name → window class / title fragment mapping ──────────────────
        # Maps natural app names to (win32_class, title_fragment) pairs.
        # win32_class is used with FindWindow for a fast exact match.
        # title_fragment is the stable part of the title that never changes
        # regardless of what document/page/tab is currently open.
        _APP_ALIASES: dict[str, tuple[str, str]] = {
            "chrome":         ("Chrome_WidgetWin_1",  "Google Chrome"),
            "google chrome":  ("Chrome_WidgetWin_1",  "Google Chrome"),
            "brave":          ("Chrome_WidgetWin_1",  "Brave"),
            "brave browser":  ("Chrome_WidgetWin_1",  "Brave"),
            "firefox":        ("MozillaWindowClass",  "Firefox"),
            "edge":           ("Chrome_WidgetWin_1",  "Microsoft Edge"),
            "microsoft edge": ("Chrome_WidgetWin_1",  "Microsoft Edge"),
            "vscode":         ("Chrome_WidgetWin_1",  "Visual Studio Code"),
            "vs code":        ("Chrome_WidgetWin_1",  "Visual Studio Code"),
            "visual studio code": ("Chrome_WidgetWin_1", "Visual Studio Code"),
            "notepad":        ("Notepad",             "Notepad"),
            "notepad++":      ("Notepad++",           "Notepad++"),
            "explorer":       ("CabinetWClass",       "File Explorer"),
            "file explorer":  ("CabinetWClass",       "File Explorer"),
            "discord":        ("Chrome_WidgetWin_1",  "Discord"),
            "slack":          ("Chrome_WidgetWin_1",  "Slack"),
            "whatsapp":       ("Chrome_WidgetWin_1",  "WhatsApp"),
            "spotify":        ("Chrome_WidgetWin_1",  "Spotify"),
            "teams":          ("Chrome_WidgetWin_1",  "Microsoft Teams"),
            "obs":            ("Qt5152QWindowIcon",   "OBS"),
            "terminal":       ("CASCADIA_HOSTING_WINDOW_CLASS", "Windows Terminal"),
            "windows terminal": ("CASCADIA_HOSTING_WINDOW_CLASS", "Windows Terminal"),
            "powershell":     ("ConsoleWindowClass",  "Windows PowerShell"),
            "cmd":            ("ConsoleWindowClass",  "Command Prompt"),
            "paint":          ("MSPaintApp",          "Paint"),
            "word":           ("OpusApp",             "Word"),
            "excel":          ("XLMAIN",              "Excel"),
            "powerpoint":     ("PPTFrameClass",       "PowerPoint"),
            "outlook":        ("rctrl_renwnd32",      "Outlook"),
            "zoom":           ("zoom",                "Zoom"),
            "vlc":            ("Qt5QWindowIcon",      "VLC"),
            "calculator":     ("ApplicationFrameWindow", "Calculator"),
            "settings":       ("ApplicationFrameWindow", "Settings"),
            "task manager":   ("TaskManagerWindow",   "Task Manager"),
        }

        def _resolve_app_window(self, name: str) -> int | None:
            """
            Find the best window for a given app name, regardless of its
            current title (which changes as pages/documents change).

            Strategy:
            1. Check _APP_ALIASES for a known win32 class → FindWindow by class
               (fast, exact, title-independent).
            2. Enumerate all windows and find those whose title contains the
               stable title_fragment, preferring the foreground window.
            3. If multiple instances exist, prefer the one most recently
               brought to the foreground.
            """
            low = name.strip().lower()
            entry = self._APP_ALIASES.get(low)

            if entry:
                win32_class, title_frag = entry

                # Fast path: FindWindow by class (finds ANY window of this app)
                # This returns the topmost window of that class.
                hwnd = win32gui.FindWindow(win32_class, None)
                if hwnd and win32gui.IsWindowVisible(hwnd):
                    return hwnd

                # Enumerate all windows of this class (multi-window apps)
                matches: list[tuple[int, str]] = []
                def cb_class(h, _):
                    try:
                        cls = win32gui.GetClassName(h)
                        if cls == win32_class and win32gui.IsWindowVisible(h):
                            title = win32gui.GetWindowText(h).strip()
                            if title:
                                matches.append((h, title))
                    except Exception:
                        pass
                win32gui.EnumWindows(cb_class, None)

                if matches:
                    # Prefer foreground
                    try:
                        fg = win32gui.GetForegroundWindow()
                        for h, _ in matches:
                            if h == fg:
                                return h
                    except Exception:
                        pass
                    # Prefer the one whose title contains the stable fragment
                    frag_matches = [
                        (h, t) for h, t in matches
                        if title_frag.lower() in t.lower()
                    ]
                    if frag_matches:
                        return frag_matches[0][0]
                    return matches[0][0]

            # No alias entry — fall through to normal title search
            return None

        def _canonical_app_name(self, window_title: str) -> str:
            """
            Given any window title (possibly with dynamic content like page
            names), return the canonical app name for blueprint keying.

            "Python vs C++ - Google Search - Google Chrome" → "Google Chrome"
            "main.py - Midum - Visual Studio Code"         → "Visual Studio Code"
            "New tab - Google Chrome"                       → "Google Chrome"
            "Discord"                                       → "Discord"
            """
            # Check if any app alias's stable fragment appears in the title
            for alias, (_, frag) in self._APP_ALIASES.items():
                if frag.lower() in window_title.lower():
                    return frag   # return the stable display name
            # Fall back to stripping dynamic prefix: take the last " - " segment
            parts = [p.strip() for p in re.split(r"\s+[-–|]\s+", window_title)]
            return parts[-1] if parts else window_title

        def _find_window(self, window_title: str) -> int | None:
            """
            Find a window by title, with layered resolution:

            1. Shell alias (taskbar, start, tray, desktop…)
            2. App name alias (_APP_ALIASES) — class-based, title-independent.
               This is the key fix for dynamic titles: "New tab - Google Chrome",
               "Python vs C++ - Google Chrome" etc. all resolve to Chrome's hwnd
               by class name, not by matching the title string.
            3. Exact title match
            4. Raw Win32 class name
            5. Substring match across all windows
            """
            # ── 1. Shell alias ────────────────────────────────────────────────
            alias = window_title.strip().lower()
            shell_hwnd = self._find_shell_hwnd(alias)
            if shell_hwnd:
                return shell_hwnd

            # ── 2. App name alias (title-independent) ─────────────────────────
            app_hwnd = self._resolve_app_window(window_title)
            if app_hwnd:
                return app_hwnd

            # ── 3. Exact title match ──────────────────────────────────────────
            hwnd = win32gui.FindWindow(None, window_title)
            if hwnd:
                return hwnd

            # ── 4. Raw class name ─────────────────────────────────────────────
            hwnd = win32gui.FindWindow(window_title, None)
            if hwnd:
                return hwnd

            # ── 5. Substring match across ALL top-level windows ───────────────
            matches: list[tuple[int, str, bool]] = []

            def cb(h, _):
                title = win32gui.GetWindowText(h).strip()
                if not title:
                    return
                visible = bool(win32gui.IsWindowVisible(h))
                if window_title.lower() in title.lower():
                    try:
                        rect = win32gui.GetWindowRect(h)
                        if (rect[2] - rect[0]) <= 0 or (rect[3] - rect[1]) <= 0:
                            if visible:
                                return
                    except Exception:
                        pass
                    matches.append((h, title, visible))

            win32gui.EnumWindows(cb, None)

            if not matches:
                return None
            if len(matches) == 1:
                return matches[0][0]

            visible_matches = [m for m in matches if m[2]]
            pool = visible_matches if visible_matches else matches

            try:
                fg = win32gui.GetForegroundWindow()
                for h, _t, _v in pool:
                    if h == fg:
                        return h
            except Exception:
                pass

            pool.sort(key=lambda m: len(m[1]), reverse=True)
            return pool[0][0]

        def _resolve_shell_element(self, description: str) -> tuple[int | None, str | None]:
            """
            For descriptions that directly name a shell control ("Start button",
            "Search", "Show hidden icons", "Clock", etc.), return the hwnd of the
            shell surface that contains it, and the canonical alias to use.
            Returns (None, None) if description doesn't match any shell surface.
            """
            desc_l = description.strip().lower()
            # Map description keywords to shell surface aliases
            _DESC_TO_ALIAS = {
                "start":        "start",
                "search bar":   "taskbar",
                "search":       "taskbar",
                "task view":    "taskbar",
                "show desktop": "taskbar",
                "clock":        "taskbar",
                "date":         "taskbar",
                "time":         "taskbar",
                "tray":         "tray",
                "notification": "tray",
                "system tray":  "tray",
                "wifi":         "tray",
                "volume":       "tray",
                "battery":      "tray",
                "speaker":      "tray",
                "network":      "tray",
                "hidden icons": "tray overflow",
                "overflow":     "tray overflow",
                "desktop":      "desktop",
            }
            for keyword, alias in _DESC_TO_ALIAS.items():
                if keyword in desc_l:
                    hwnd = self._find_shell_hwnd(alias)
                    if hwnd:
                        return hwnd, alias
            return None, None

        def discover_ui_subtrees(self, window_title: str) -> list[dict]:
            hwnd = self._find_shell_hwnd(window_title.strip().lower()) or self._find_window(window_title)
            if not hwnd:
                return []

            root_element = auto.ControlFromHandle(hwnd)
            if window_title not in self._live_cache:
                self._live_cache[window_title] = {}

            containers = []
            seen_keys  = set()

            for element, depth in auto.WalkControl(root_element, maxDepth=self.MAX_DISCOVER_DEPTH):
                try:
                    type_name = element.ControlTypeName
                    name      = (element.Name or "").strip()
                    auto_id   = (element.AutomationId or "").strip()
                except Exception:
                    continue

                if type_name not in _CONTAINER_TYPES:
                    continue
                if not (name or auto_id):
                    continue

                key = f"{name or auto_id}::{type_name}::{depth}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                self._live_cache[window_title][key] = element
                containers.append({
                    "subtree_key":  key,
                    "type":         type_name,
                    "name":         name,
                    "automation_id": auto_id,
                    "depth":        depth,
                })

                if len(containers) >= self.MAX_RESULTS:
                    break

            return containers

        def inspect_subtree_controls(self, window_title: str, subtree_key: str) -> list[dict]:
            app_cache = self._live_cache.get(window_title, {})
            subtree_root = app_cache.get(subtree_key)

            if not subtree_root:
                self.discover_ui_subtrees(window_title)
                subtree_root = self._live_cache.get(window_title, {}).get(subtree_key)
                if not subtree_root:
                    return []

            controls   = []
            seen_names = set()

            try:
                walker = auto.WalkControl(subtree_root, maxDepth=self.MAX_INSPECT_DEPTH)
            except Exception:
                return []

            for element, depth in walker:
                try:
                    type_name = element.ControlTypeName
                    name      = (element.Name or "").strip()
                    auto_id   = (element.AutomationId or "").strip()
                except Exception:
                    continue

                if type_name not in _ACTIONABLE_TYPES:
                    continue
                # TextControl is extremely common as static labels — only keep
                # it if it has an AutomationId (suggests it's a real interactive
                # element wrapper, common in Electron) or looks like a button label
                if type_name == "TextControl" and not auto_id:
                    continue
                if not (name or auto_id):
                    continue

                dedup_key = f"{name}::{auto_id}::{type_name}"
                if dedup_key in seen_names:
                    continue
                seen_names.add(dedup_key)

                controls.append({
                    "type": type_name, "name": name, "automation_id": auto_id, "depth": depth
                })

                if len(controls) >= self.MAX_RESULTS:
                    break

            return controls

        def safely_trigger_ui_element(self, window_title: str, control_type: str,
                                       search_property: str, property_value: str,
                                       action: str, text_to_type: str = "") -> str:
            hwnd = self._find_shell_hwnd(window_title.strip().lower()) or self._find_window(window_title)
            if not hwnd:
                return f"Error: Window '{window_title}' not found."

            root = auto.ControlFromHandle(hwnd)
            search_args = {}
            if search_property == "automation_id":
                search_args["AutomationId"] = property_value
            elif search_property == "name":
                search_args["Name"] = property_value
            elif search_property == "class_name":
                search_args["ClassName"] = property_value

            c_type = control_type.lower()
            # searchDepth=0 means unlimited in uiautomation — search the whole subtree
            search_args["searchDepth"] = 0

            if c_type == "button":
                element = root.ButtonControl(**search_args)
            elif c_type in ("edit", "input"):
                element = root.EditControl(**search_args)
            elif c_type == "text":
                element = root.TextControl(**search_args)
            elif c_type == "checkbox":
                element = root.CheckBoxControl(**search_args)
            elif c_type == "menuitem":
                element = root.MenuItemControl(**search_args)
            elif c_type == "listitem":
                element = root.ListItemControl(**search_args)
            elif c_type == "image":
                element = root.ImageControl(**search_args)
            else:
                element = root.Control(**search_args)

            if not element.Exists(1, 0.5):
                return (
                    f"Error: Could not locate {control_type} matching "
                    f"{search_property}='{property_value}' anywhere in '{window_title}'. "
                    f"Try manual_inspect_app_subtree on a different container, or fall back to "
                    f"fallback_click_text if this element has visible text."
                )

            try:
                if action == "click":
                    try:
                        element.GetInvokePattern().Invoke()
                        return f"Programmatically invoked '{property_value}'."
                    except Exception:
                        try:
                            element.SetFocus()
                        except Exception:
                            pass
                        element.Click(simulateMove=False)
                        return f"Background clicked '{property_value}'."
                elif action == "set_text":
                    try:
                        element.GetValuePattern().SetValue(text_to_type)
                    except Exception:
                        element.SetValue(text_to_type)
                    return f"Populated text field '{property_value}'."
                elif action == "get_text":
                    try:
                        return element.GetValuePattern().Value
                    except Exception:
                        return element.Name
            except Exception as e:
                return f"Action failed: {str(e)}"
            return "Unknown action."

        def _blueprint_key(self, window_title: str) -> str:
            """
            Normalise any window title to a stable blueprint key using
            _canonical_app_name, so dynamic titles like
            'Python vs C++ - Google Chrome' and 'New tab - Google Chrome'
            both map to 'Google Chrome' and share the same blueprint.
            """
            return self._canonical_app_name(window_title)

        def _lookup_blueprint(self, window_title: str, description: str) -> dict | None:
            """
            Return a saved control record for (app, description) if one exists,
            else None. Record shape:
              {"automation_id": str, "type": str, "name": str, "depth": int}
            """
            key  = self._blueprint_key(window_title)
            bp   = self.load_app_blueprint(key)
            desc = description.strip().lower()
            return bp.get("known_controls", {}).get(desc)

        def _save_to_blueprint(self, window_title: str, description: str,
                               element, type_name: str, depth: int):
            """
            Persist a successful element lookup so future calls skip the tree walk.
            Only saves elements that have an AutomationId — name-only lookups are
            less stable across sessions.
            """
            try:
                auto_id = (element.AutomationId or "").strip()
                name    = (element.Name or "").strip()
                if not auto_id:
                    return   # not worth caching without a stable ID
                key  = self._blueprint_key(window_title)
                bp   = self.load_app_blueprint(key)
                bp.setdefault("known_controls", {})[description.strip().lower()] = {
                    "automation_id": auto_id,
                    "type":          type_name,
                    "name":          name,
                    "depth":         depth,
                }
                self.save_app_blueprint(key, bp)
                print(f"   [Blueprint] Saved '{description}' → AutomationId='{auto_id}' for '{key}'")
            except Exception:
                pass

        def _find_by_automation_id(self, root, auto_id: str, type_name: str):
            """
            Fast direct lookup by AutomationId. Returns the element or None.
            Uses uiautomation's built-in search which is much faster than a
            manual tree walk.
            """
            try:
                search_args = {"AutomationId": auto_id, "searchDepth": 0}
                t = type_name.lower().replace("control", "").strip()
                ctrl_map = {
                    "button":      lambda: root.ButtonControl(**search_args),
                    "edit":        lambda: root.EditControl(**search_args),
                    "text":        lambda: root.TextControl(**search_args),
                    "checkbox":    lambda: root.CheckBoxControl(**search_args),
                    "menuitem":    lambda: root.MenuItemControl(**search_args),
                    "listitem":    lambda: root.ListItemControl(**search_args),
                    "image":       lambda: root.ImageControl(**search_args),
                    "hyperlink":   lambda: root.HyperlinkControl(**search_args),
                    "combobox":    lambda: root.ComboBoxControl(**search_args),
                    "radiobutton": lambda: root.RadioButtonControl(**search_args),
                    "tabitem":     lambda: root.TabItemControl(**search_args),
                    "splitbutton": lambda: root.SplitButtonControl(**search_args),
                }
                factory = ctrl_map.get(t, lambda: root.Control(**search_args))
                el = factory()
                if el.Exists(0.5, 0.1):
                    return el
            except Exception:
                pass
            return None

        def snapshot_ui(self, window_title: str, filter_type: str = "") -> str:
            """
            Walk the visible, actionable portion of a window's UIA tree and
            return a compact numbered table the model can use to pick elements
            by index via act_on_element_by_index.

            Visibility rules (all must pass):
              - BoundingRectangle width > 0 AND height > 0
              - Not fully off-screen
              - IsEnabled == True  (grayed-out controls excluded)
              - ControlType in _ACTIONABLE_TYPES (containers excluded)
              - Name or AutomationId not empty

            The snapshot is stored in _snapshot_cache[window_title] so
            act_on_element_by_index can find the element object later.
            """
            hwnd = (
                self._find_shell_hwnd(window_title.strip().lower())
                or self._resolve_app_window(window_title)
                or self._find_window(window_title)
            )
            if not hwnd:
                return (
                    f"Error: Window '{window_title}' not found. "
                    f"Call list_active_windows to see open windows."
                )

            root = auto.ControlFromHandle(hwnd)

            # Short type aliases for the table
            _TYPE_SHORT = {
                "ButtonControl":      "btn",
                "EditControl":        "edit",
                "CheckBoxControl":    "chk",
                "RadioButtonControl": "radio",
                "ComboBoxControl":    "combo",
                "ListItemControl":    "item",
                "MenuItemControl":    "menu",
                "TabItemControl":     "tab",
                "HyperlinkControl":   "link",
                "SliderControl":      "slider",
                "SplitButtonControl": "split",
                "ImageControl":       "img",
                "TextControl":        "text",
            }

            filter_low = filter_type.strip().lower()

            elements = []
            seen_ids  = set()

            try:
                for el, depth in auto.WalkControl(root, maxDepth=self.MAX_INSPECT_DEPTH):
                    try:
                        type_name = el.ControlTypeName
                        name      = (el.Name or "").strip()
                        auto_id   = (el.AutomationId or "").strip()
                    except Exception:
                        continue

                    if type_name not in _ACTIONABLE_TYPES:
                        continue
                    if not (name or auto_id):
                        continue

                    # ── Visibility + enabled check ────────────────────────────
                    try:
                        rect = el.BoundingRectangle
                        if rect.width() <= 0 or rect.height() <= 0:
                            continue
                        if rect.right < 0 or rect.bottom < 0:
                            continue
                        if rect.left > SCREEN_W * 2 or rect.top > SCREEN_H * 2:
                            continue
                    except Exception:
                        continue   # no rect = definitely not visible

                    try:
                        if not el.IsEnabled:
                            continue
                    except Exception:
                        pass   # can't check enabled state — include anyway

                    # ── Optional type filter ──────────────────────────────────
                    short = _TYPE_SHORT.get(type_name, type_name.replace("Control", "").lower())
                    if filter_low and filter_low not in short and filter_low not in type_name.lower():
                        continue

                    # ── Dedup: same name+type at the SAME depth — likely the same
                    # element appearing in multiple subtree walks.
                    # Different depths = different elements (e.g. two "Close" buttons
                    # in VS Code at depth 4 and depth 9 are different controls).
                    dedup_key = f"{name or auto_id}::{type_name}::{depth // 3}"
                    if dedup_key in seen_ids:
                        continue
                    seen_ids.add(dedup_key)

                    # ── State flags ───────────────────────────────────────────
                    flags = []
                    try:
                        if el.HasKeyboardFocus:
                            flags.append("focused")
                    except Exception:
                        pass
                    try:
                        state = el.CurrentToggleState   # 0=off 1=on 2=indeterminate
                        if state == 1:
                            flags.append("checked")
                        elif state == 0:
                            flags.append("unchecked")
                    except Exception:
                        pass
                    try:
                        if el.GetSelectionItemPattern().IsSelected:
                            flags.append("selected")
                    except Exception:
                        pass

                    label = name or auto_id
                    elements.append({
                        "element":  el,
                        "type":     type_name,
                        "short":    short,
                        "name":     name,
                        "auto_id":  auto_id,
                        "label":    label,
                        "flags":    flags,
                        "depth":    depth,
                    })

                    if len(elements) >= 120:   # hard cap — keeps output readable
                        break
            except Exception as e:
                return f"Error walking UI tree: {e}"

            if not elements:
                return (
                    f"No visible, enabled, actionable elements found in '{window_title}'. "
                    f"The window may render via canvas/WebGL — try fallback_click_text."
                )

            # Store for act_on_element_by_index
            self._snapshot_cache[window_title] = elements

            # ── Build compact table ───────────────────────────────────────────
            lines = [
                f"UI snapshot of '{window_title}' — {len(elements)} visible elements",
                f"Use act(target='{window_title}', index=N) to interact.",
                "",
                f"{'IDX':>4}  {'TYPE':<7}  {'STATUS':<12}  NAME",
                "─" * 72,
            ]
            for i, el in enumerate(elements):
                status = ", ".join(el["flags"]) if el["flags"] else "enabled"
                label  = el["label"][:48]   # truncate long names
                lines.append(f"{i:>4}  {el['short']:<7}  {status:<12}  {label}")

            return "\n".join(lines)

        def act_on_element_by_index(self, window_title: str, index: int,
                                     action: str = "click",
                                     text_to_type: str = "") -> str:
            """
            Act on an element from the last snapshot() call by its index.
            Exact — no scoring, no tree walk, no ambiguity.
            """
            cache = self._snapshot_cache.get(window_title)
            if not cache:
                return (
                    f"No snapshot found for '{window_title}'. "
                    f"Call snapshot_ui('{window_title}') first."
                )
            if index < 0 or index >= len(cache):
                return (
                    f"Index {index} out of range (snapshot has {len(cache)} elements, "
                    f"indices 0–{len(cache)-1})."
                )

            entry   = cache[index]
            element = entry["element"]
            label   = entry["label"]
            t_name  = entry["type"]

            # Verify the element is still alive (window may have changed)
            try:
                rect = element.BoundingRectangle
                if rect.width() <= 0 or rect.height() <= 0:
                    return (
                        f"Element #{index} ('{label}') is no longer visible. "
                        f"Call snapshot again to refresh."
                    )
            except Exception:
                return (
                    f"Element #{index} ('{label}') is stale — window may have changed. "
                    f"Call snapshot again to refresh."
                )

            hwnd = (
                self._find_shell_hwnd(window_title.strip().lower())
                or self._resolve_app_window(window_title)
                or self._find_window(window_title)
            )

            print(f"   [Snapshot act] #{index} '{label}' ({t_name}) → {action}")
            return self._act_on_element(element, label, t_name, action, text_to_type, hwnd or 0)

        def find_and_act(self, window_title: str, description: str, action: str = "click",
                          text_to_type: str = "") -> str:
            """
            ONE-CALL UI interaction — finds the best-matching element and acts on it.

            Lookup order:
              1. Blueprint cache  — direct AutomationId lookup, O(1), exact.
              2. Full tree search — scored walk, saves result to blueprint on success.

            Supports shell surfaces: pass window_title as "taskbar", "start",
            "tray", "desktop", "action center", etc.
            """
            # ── Window resolution ─────────────────────────────────────────────────
            hwnd = self._find_shell_hwnd(window_title.strip().lower())
            if not hwnd:
                hwnd = self._find_window(window_title)
            if not hwnd:
                shell_hwnd, _alias = self._resolve_shell_element(description)
                if shell_hwnd:
                    hwnd = shell_hwnd
            if not hwnd:
                return (
                    f"Error: Window '{window_title}' not found. "
                    f"Call list_active_windows to see exact titles. "
                    f"For shell controls use: 'taskbar', 'start', 'tray', "
                    f"'desktop', 'action center', 'search', 'tray overflow'."
                )

            root = auto.ControlFromHandle(hwnd)

            # ── 1. Blueprint fast path ────────────────────────────────────────────
            cached = self._lookup_blueprint(window_title, description)
            if cached:
                el = self._find_by_automation_id(
                    root, cached["automation_id"], cached["type"]
                )
                if el:
                    print(f"   [Blueprint] ✓ Hit for '{description}' "
                          f"(AutomationId='{cached['automation_id']}')")
                    result = self._act_on_element(
                        el, cached["name"], cached["type"],
                        action, text_to_type, hwnd
                    )
                    if "Error" not in result and "failed" not in result.lower():
                        return result
                    # Cache hit but action failed — element may have moved.
                    # Fall through to full search and update the blueprint.
                    print(f"   [Blueprint] Cache hit but action failed — refreshing.")
                else:
                    print(f"   [Blueprint] Cache miss (element gone) — doing full search.")

            # ── 2. Full tree search ───────────────────────────────────────────────
            # Keyword expansion
            _EXPANSIONS = {
                "close":        ["close", "close button", "x"],
                "minimize":     ["minimize", "minimise", "minimize button"],
                "maximize":     ["maximize", "maximise", "restore", "maximize button"],
                "send":         ["send", "send message", "send button", "submit"],
                "search":       ["search", "search box", "search bar", "find", "search field"],
                "address bar":  ["address bar", "address and search bar", "url", "location", "omnibox"],
                "new tab":      ["new tab", "open new tab", "add tab"],
                "back":         ["back", "back button", "navigate back", "go back"],
                "forward":      ["forward", "forward button", "navigate forward"],
                "settings":     ["settings", "preferences", "options", "gear"],
                "menu":         ["menu", "hamburger", "app menu", "main menu"],
                "ok":           ["ok", "okay", "confirm", "yes", "accept"],
                "cancel":       ["cancel", "no", "dismiss", "close"],
                "save":         ["save", "save file", "save document"],
                "open":         ["open", "open file", "browse"],
                "refresh":      ["refresh", "reload", "f5"],
                "new":          ["new", "new file", "new document", "create"],
                "delete":       ["delete", "remove", "trash"],
                "copy":         ["copy", "copy text"],
                "paste":        ["paste", "paste text"],
                "input":        ["input", "text field", "text box", "entry", "edit"],
                "chat input":   ["chat input", "message input", "type a message", "message box",
                                 "enter a prompt", "prompt", "compose"],
            }
            desc_lower    = description.strip().lower()
            desc_variants = {desc_lower}
            for key, variants in _EXPANSIONS.items():
                if key in desc_lower or desc_lower in key:
                    desc_variants.update(variants)
            desc_tokens = set(re.findall(r"[a-z0-9]+", desc_lower))

            candidates = []
            try:
                for element, depth in auto.WalkControl(root, maxDepth=self.MAX_INSPECT_DEPTH):
                    try:
                        type_name = element.ControlTypeName
                        name      = (element.Name or "").strip()
                        auto_id   = (element.AutomationId or "").strip()
                    except Exception:
                        continue
                    if type_name not in _ACTIONABLE_TYPES and type_name not in _CONTAINER_TYPES:
                        continue
                    if not (name or auto_id):
                        continue
                    try:
                        rect = element.BoundingRectangle
                        if rect.width() <= 0 or rect.height() <= 0:
                            continue
                        if rect.right < 0 or rect.bottom < 0:
                            continue
                        if rect.left > SCREEN_W * 2 or rect.top > SCREEN_H * 2:
                            continue
                        visible_area = rect.width() * rect.height()
                    except Exception:
                        visible_area = 0
                    candidates.append({
                        "element": element, "type": type_name,
                        "name": name, "automation_id": auto_id,
                        "depth": depth, "visible_area": visible_area,
                    })
                    if len(candidates) >= 600:
                        break
            except Exception as e:
                return f"Error walking UI tree for '{window_title}': {str(e)}"

            if not candidates:
                return (
                    f"No interactive elements found in '{window_title}'. "
                    f"This window likely renders via canvas/WebGL — UIA cannot see inside it. "
                    f"Try fallback_click_text or fallback_view_screen instead."
                )

            _STRIP_SUFFIXES = [" button", " tab", " field", " box", " bar",
                               " control", " panel", " window", " icon", " link"]

            def _strip_suffix(s: str) -> str:
                for sfx in _STRIP_SUFFIXES:
                    if s.endswith(sfx):
                        return s[:-len(sfx)].strip()
                return s

            _TYPE_WEIGHT = {
                "ButtonControl": 1.5, "EditControl": 1.4, "HyperlinkControl": 1.3,
                "CheckBoxControl": 1.3, "RadioButtonControl": 1.3, "ComboBoxControl": 1.2,
                "MenuItemControl": 1.2, "ListItemControl": 1.1, "TabItemControl": 1.1,
                "SplitButtonControl": 1.1, "SliderControl": 1.0,
                "ImageControl": 0.8, "TextControl": 0.6,
            }

            def score(c) -> float:
                name_l    = c["name"].lower()
                id_l      = c["automation_id"].lower()
                name_core = _strip_suffix(name_l)
                id_core   = _strip_suffix(id_l)
                s = 0.0
                if desc_lower == name_l or desc_lower == name_core:
                    s += 300
                elif desc_lower == id_l or desc_lower == id_core:
                    s += 220
                for variant in desc_variants:
                    if variant == name_l or variant == name_core:
                        s += 180; break
                    if variant == id_l or variant == id_core:
                        s += 140; break
                if len(desc_lower) >= 3:
                    if desc_lower in name_l:   s += 60
                    elif desc_lower in name_core: s += 55
                    if desc_lower in id_l:     s += 30
                    for variant in desc_variants:
                        if len(variant) >= 3 and variant in name_l:
                            s += 40; break
                name_tokens = set(re.findall(r"[a-z0-9]+", name_l))
                id_tokens   = set(re.findall(r"[a-z0-9]+", id_l))
                s += sum(min(len(t) * 3, 15) for t in desc_tokens & name_tokens)
                s += sum(min(len(t) * 1, 5)  for t in desc_tokens & id_tokens)
                type_w = _TYPE_WEIGHT.get(c["type"], 0.5)
                if s > 0:   s *= type_w
                elif c["type"] in _ACTIONABLE_TYPES: s += 5 * type_w
                else:        s -= 15
                if c["visible_area"] > 0:
                    s += min(c["visible_area"] / 5000, 8)
                if c["depth"] <= 2: s -= 10
                else:               s -= c["depth"] * 0.1
                return s

            scored     = sorted(candidates, key=score, reverse=True)
            best       = scored[0]
            best_score = score(best)

            if best_score <= 0:
                by_type: dict[str, list[str]] = {}
                for c in candidates:
                    label = c["name"] or c["automation_id"]
                    if label:
                        by_type.setdefault(c["type"], []).append(f"'{label}'")
                lines = []
                for t in sorted(by_type):
                    items = by_type[t][:8]
                    lines.append(f"  {t}: {', '.join(items)}"
                                 + (f" (+{len(by_type[t])-8} more)" if len(by_type[t]) > 8 else ""))
                return (
                    f"No element matched '{description}' in '{window_title}'.\n"
                    f"All visible elements by type:\n" + "\n".join(lines) + "\n"
                    f"Retry with an exact name from the list above, or use fallback_click_text."
                )

            close_matches = [c for c in scored[1:5] if best_score - score(c) <= 10]
            elem_desc = best["name"] or best["automation_id"] or best["type"]
            if close_matches and best_score < 150:
                alt_names = ", ".join(
                    f"'{c['name'] or c['automation_id']}' ({c['type']})"
                    for c in close_matches[:3]
                )
                print(f"   [UIA] ⚠ Ambiguous: chose '{elem_desc}' ({best['type']}), "
                      f"alternatives: {alt_names}")

            print(f"   [UIA] Best match: '{elem_desc}' ({best['type']}) "
                  f"score={best_score:.1f} depth={best['depth']}")

            # ── Act on the best match ─────────────────────────────────────────────
            result = self._act_on_element(
                best["element"], elem_desc, best["type"],
                action, text_to_type, hwnd
            )

            # ── Save to blueprint on success ──────────────────────────────────────
            if "Success" in result or "invoked" in result or "clicked" in result:
                self._save_to_blueprint(
                    window_title, description,
                    best["element"], best["type"], best["depth"]
                )

            return result

        def _act_on_element(self, element, elem_desc: str, type_name: str,
                             action: str, text_to_type: str, hwnd: int) -> str:
            from tools_registry import execute_terminal_command
            from screen_capture import _do_click, type_text
            """
            Perform the requested action on a resolved UIA element.
            Extracted from find_and_act so both the blueprint fast-path and
            the full search path share the same action logic.
            """
            # ── get_text ──────────────────────────────────────────────────────────
            if action == "get_text":
                try:
                    val = element.GetValuePattern().Value
                    if val:
                        return f"Text of '{elem_desc}': {val}"
                except Exception:
                    pass
                try:
                    txt = element.GetTextPattern().DocumentRange.GetText(-1)
                    if txt:
                        return f"Text of '{elem_desc}': {txt}"
                except Exception:
                    pass
                return f"Text of '{elem_desc}': {element.Name}"

            # ── set_text ──────────────────────────────────────────────────────────
            if action == "set_text":
                try:
                    element.GetValuePattern().SetValue(text_to_type)
                    return f"Success: set text of '{elem_desc}' to '{text_to_type[:40]}'"
                except Exception:
                    pass
                try:
                    element.SetValue(text_to_type)
                    return f"Success: set text of '{elem_desc}' (SetValue)."
                except Exception:
                    pass
                try:
                    rect = element.BoundingRectangle
                    cx = rect.left + rect.width() // 2
                    cy = rect.top  + rect.height() // 2
                    _do_click(cx, cy, "left_click", label=f"focus '{elem_desc}'")
                    time.sleep(0.15)
                    import pyperclip
                    pyperclip.copy(text_to_type)
                    ps = (
                        "Add-Type -AssemblyName System.Windows.Forms\n"
                        "[System.Windows.Forms.SendKeys]::SendWait('^a')\n"
                        "Start-Sleep -Milliseconds 50\n"
                        "[System.Windows.Forms.SendKeys]::SendWait('^v')"
                    )
                    execute_terminal_command(ps)
                    return f"Success: set text of '{elem_desc}' via clipboard paste."
                except Exception:
                    pass
                try:
                    element.Click(simulateMove=False)
                    time.sleep(0.1)
                    type_text(text_to_type)
                    return f"Set text via click+type on '{elem_desc}'."
                except Exception as e:
                    return f"Error: all set_text methods failed on '{elem_desc}': {e}"

            # ── click ─────────────────────────────────────────────────────────────
            # (a) InvokePattern
            try:
                element.GetInvokePattern().Invoke()
                return f"Success: invoked '{elem_desc}' ({type_name}) via UIA InvokePattern."
            except Exception:
                pass
            # (b) TogglePattern
            try:
                element.GetTogglePattern().Toggle()
                return f"Success: toggled '{elem_desc}' ({type_name}) via UIA TogglePattern."
            except Exception:
                pass
            # (c) SelectionItemPattern
            try:
                element.GetSelectionItemPattern().Select()
                return f"Success: selected '{elem_desc}' ({type_name}) via UIA SelectionItemPattern."
            except Exception:
                pass
            # (d) Coordinate click — universal fallback
            try:
                rect = element.BoundingRectangle
                if rect.width() > 0 and rect.height() > 0:
                    cx = rect.left + rect.width()  // 2
                    cy = rect.top  + rect.height() // 2
                    try:
                        import win32con
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                        win32gui.SetForegroundWindow(hwnd)
                        time.sleep(0.08)
                    except Exception:
                        pass
                    return _do_click(cx, cy, "left_click",
                                     label=f"UIA-coord '{elem_desc}'")
            except Exception:
                pass
            # (e) UIA Click — last resort
            try:
                try:
                    element.SetFocus()
                except Exception:
                    pass
                element.Click(simulateMove=False)
                return f"Success: clicked '{elem_desc}' ({type_name}) via UIA Click."
            except Exception as e:
                return (
                    f"Error: all click methods failed for '{elem_desc}' ({type_name}). "
                    f"Last error: {e}. Try fallback_click_text if it has visible text."
                )
        def read_aggregated_text(self, window_title: str, container_key: str = None) -> str:
            """
            Aggregates hundreds of small TextControl siblings into readable paragraphs.
            Resolves window by app name so dynamic titles don't break it.
            """
            hwnd = (
                self._find_shell_hwnd(window_title.strip().lower())
                or self._resolve_app_window(window_title)
                or self._find_window(window_title)
            )
            if not hwnd:
                # Tell the model what the window is actually called now
                current_titles = []
                def cb(h, _):
                    if win32gui.IsWindowVisible(h):
                        t = win32gui.GetWindowText(h).strip()
                        if t:
                            current_titles.append(t)
                win32gui.EnumWindows(cb, None)
                suggestions = [t for t in current_titles if window_title.split("-")[-1].strip().lower() in t.lower()]
                hint = f" Did you mean one of: {suggestions[:3]}?" if suggestions else ""
                return (
                    f"Window '{window_title}' not found (title may have changed).{hint} "
                    f"Call list_active_windows to see current titles, then retry."
                )
            
            root = auto.ControlFromHandle(hwnd)
            # If a specific container was requested, use that as the root
            search_root = self._live_cache.get(window_title, {}).get(container_key, root)
            
            aggregated_lines = []
            last_y = -1
            current_line = []
            
            # Walk the subtree, collecting TextControls
            for element, depth in auto.WalkControl(search_root, maxDepth=self.MAX_INSPECT_DEPTH):
                if element.ControlTypeName == "TextControl":
                    text = (element.Name or "").strip()
                    if not text: continue
                    
                    try:
                        rect = element.BoundingRectangle
                        # Logic: If elements are on the same Y-level (roughly), 
                        # treat them as part of the same line/paragraph
                        if abs(rect.top - last_y) < 15:
                            current_line.append(text)
                        else:
                            if current_line:
                                aggregated_lines.append(" ".join(current_line))
                            current_line = [text]
                            last_y = rect.top
                    except:
                        current_line.append(text)
            
            if current_line:
                aggregated_lines.append(" ".join(current_line))
                
            return "\n".join(aggregated_lines)

        def query_gemini_app(self, prompt: str, wait_for_response: int = 90) -> str:
            from browser_cdp import query_gemini_app
            """
            Thin delegator kept ONLY so external dispatchers that call tools
            as getattr(ui_navigator, tool_name)(**args) (e.g. gui.pyw) keep
            working. Does NOT use UIA — routes straight to the module-level,
            CDP/browser-based query_gemini_app(), same as the CLI dispatcher
            in this file uses directly.
            """
            return query_gemini_app(prompt, wait_for_response=wait_for_response)

        def manage_gemini_chat(self, action: str, chat_name: str = None) -> str:
            window_title = "Gemini"
            hwnd = win32gui.FindWindow(None, window_title)
            if not hwnd: return "Error: Gemini window not found."
            
            root = auto.ControlFromHandle(hwnd)
            
            if action == "new_chat":
                # Search for the "New chat" button/icon
                for element, _ in auto.WalkControl(root):
                    if element.Name and "New chat" in element.Name:
                        element.Click()
                        return "Started a new Gemini chat."
                return "Error: Could not find 'New chat' button."
                
            elif action == "open_recent" and chat_name:
                # Search for the chat in the sidebar list
                for element, _ in auto.WalkControl(root):
                    if element.Name and chat_name.lower() in element.Name.lower():
                        element.Click()
                        return f"Opened recent chat: {chat_name}."
                return f"Error: Could not find recent chat named '{chat_name}'."
                
            return "Invalid action."
    ui_navigator = AppMapNavigator()
else:
    ui_navigator = None

# =============================================================================

