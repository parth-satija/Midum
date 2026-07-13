# --- AUTO-SPLITTER: imports added by automated pass, please review ---
from config import _IS_LINUX
from ui_automation.windows_uia import _UIA_AVAILABLE
import json
import os
import re
import subprocess
import time

# --- from main.py, section 1 ---
# LINUX UI AUTOMATION — AppMapNavigatorLinux
# =============================================================================
# Mirrors every public method of AppMapNavigator but uses:
#   - AT-SPI2 via pyatspi for accessibility tree walking (replaces uiautomation)
#   - python-xlib / subprocess xdotool for mouse/keyboard (replaces win32gui + SendKeys)
#   - xdotool for window finding and focusing (replaces win32gui.FindWindow)
#   - xclip/xsel via subprocess for clipboard (replaces pyperclip on Wayland/X11)
#
# INSTALLATION (Debian/Ubuntu/Fedora):
#   sudo apt install python3-pyatspi xdotool xclip   # Debian/Ubuntu
#   sudo dnf install at-spi2-core xdotool xclip      # Fedora
#   pip install pyatspi                               # Python binding
#
# AT-SPI2 must be enabled in your desktop session. It is on by default in
# GNOME. For KDE/XFCE run: gsettings set org.gnome.desktop.interface
# toolkit-accessibility true  (or enable in Accessibility settings).
#
# Wayland note: xdotool works under XWayland for most apps. For native
# Wayland windows you may need ydotool (requires root or uinput group).

_PYATSPI_AVAILABLE = False
if _IS_LINUX:
    try:
        import pyatspi  # type: ignore[import-untyped]  # Linux-only package
        _PYATSPI_AVAILABLE = True
    except ImportError:
        pass

# AT-SPI role names that correspond to containers (mirrors _CONTAINER_TYPES)
_ATSPI_CONTAINER_ROLES = {
    "panel", "filler", "scroll pane", "split pane", "layered pane",
    "frame", "window", "dialog", "tool bar", "menu bar", "page tab list",
    "page tab", "tree", "tree table", "table",
}
# AT-SPI role names that are interactive (mirrors _ACTIONABLE_TYPES)
_ATSPI_ACTIONABLE_ROLES = {
    "push button", "toggle button", "check box", "radio button",
    "text", "entry", "password text", "combo box", "list item",
    "menu item", "check menu item", "radio menu item", "link",
    "slider", "spin button", "image", "label",
}


def _run(cmd: list, timeout: int = 10) -> tuple[str, str]:
    """Run a subprocess command, return (stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return "", str(e)


class AppMapNavigatorLinux:
    """
    Linux equivalent of AppMapNavigator.

    Uses AT-SPI2 for accessibility tree inspection and xdotool for all
    mouse/keyboard simulation. The public API is identical to AppMapNavigator
    so the rest of Midum (tool dispatch, tool schemas) requires no changes —
    just swap which navigator is assigned to ui_navigator at startup.

    Window identification uses xdotool search --name (substring, case-insensitive)
    mirroring the Windows fuzzy title match.

    Coordinate system: all click coordinates are real screen pixels. There is no
    canvas scaling layer on Linux — xdotool takes absolute screen coordinates.
    """

    MAX_DISCOVER_DEPTH = 12
    MAX_INSPECT_DEPTH  = 12
    MAX_RESULTS        = 60

    def __init__(self, maps_dir="storage/app_maps_linux"):
        self.maps_dir = os.path.abspath(maps_dir)
        os.makedirs(self.maps_dir, exist_ok=True)
        self._live_cache: dict = {}   # window_title -> {key -> atspi node}

    # ── Window finding ────────────────────────────────────────────────────────

    def _find_window_id(self, window_title: str) -> str | None:
        """
        Return the first xdotool window ID (as string) whose name contains
        window_title (case-insensitive). Returns None if not found.
        """
        stdout, _ = _run(["xdotool", "search", "--name", window_title])
        ids = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not ids:
            return None
        # Prefer the focused window among matches
        focused_stdout, _ = _run(["xdotool", "getactivewindow"])
        focused = focused_stdout.strip()
        if focused in ids:
            return focused
        return ids[0]

    def _find_atspi_window(self, window_title: str):
        """
        Walk the AT-SPI desktop tree looking for a top-level window whose
        name contains window_title (case-insensitive). Returns the AT-SPI
        Accessible or None.
        """
        if not _PYATSPI_AVAILABLE:
            return None
        try:
            desktop = pyatspi.Registry.getDesktop(0)
            low = window_title.lower()
            # Collect all matches
            matches = []
            for app in desktop:
                if app is None:
                    continue
                try:
                    for win in app:
                        if win is None:
                            continue
                        name = (win.name or "").lower()
                        if low in name:
                            matches.append(win)
                except Exception:
                    continue
            if not matches:
                return None
            if len(matches) == 1:
                return matches[0]
            # Prefer focused window
            try:
                focused = pyatspi.Registry.getDesktop(0)   # re-fetch for state
                for w in matches:
                    try:
                        state = w.getState()
                        if state.contains(pyatspi.STATE_ACTIVE):
                            return w
                    except Exception:
                        pass
            except Exception:
                pass
            return matches[0]
        except Exception:
            return None

    # ── AT-SPI tree walking ───────────────────────────────────────────────────

    def _walk_atspi(self, node, max_depth: int, _depth: int = 0):
        """Generator: yield (node, depth) for the entire subtree."""
        if node is None or _depth > max_depth:
            return
        yield node, _depth
        try:
            for i in range(node.childCount):
                child = node.getChildAtIndex(i)
                yield from self._walk_atspi(child, max_depth, _depth + 1)
        except Exception:
            pass

    def _role_name(self, node) -> str:
        try:
            return node.getRole().name.lower().replace("_", " ")
        except Exception:
            return ""

    # ── Public API ────────────────────────────────────────────────────────────

    def discover_ui_subtrees(self, window_title: str) -> list[dict]:
        """
        Scan the AT-SPI tree of window_title and return a list of named
        container nodes — equivalent to AppMapNavigator.discover_ui_subtrees.
        """
        win = self._find_atspi_window(window_title)
        if not win:
            return []

        if window_title not in self._live_cache:
            self._live_cache[window_title] = {}

        containers = []
        seen_keys  = set()

        for node, depth in self._walk_atspi(win, self.MAX_DISCOVER_DEPTH):
            role = self._role_name(node)
            if role not in _ATSPI_CONTAINER_ROLES:
                continue
            name    = (node.name or "").strip()
            desc    = ""
            try:
                desc = (node.description or "").strip()
            except Exception:
                pass
            if not (name or desc):
                continue

            key = f"{name or desc}::{role}::{depth}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            self._live_cache[window_title][key] = node
            containers.append({
                "subtree_key":   key,
                "type":          role,
                "name":          name,
                "description":   desc,
                "depth":         depth,
            })
            if len(containers) >= self.MAX_RESULTS:
                break

        return containers

    def inspect_subtree_controls(self, window_title: str, subtree_key: str) -> list[dict]:
        """
        Return all actionable controls under subtree_key — equivalent to
        AppMapNavigator.inspect_subtree_controls.
        """
        app_cache    = self._live_cache.get(window_title, {})
        subtree_root = app_cache.get(subtree_key)
        if not subtree_root:
            self.discover_ui_subtrees(window_title)
            subtree_root = self._live_cache.get(window_title, {}).get(subtree_key)
            if not subtree_root:
                return []

        controls   = []
        seen_names = set()

        for node, depth in self._walk_atspi(subtree_root, self.MAX_INSPECT_DEPTH):
            role = self._role_name(node)
            if role not in _ATSPI_ACTIONABLE_ROLES:
                continue
            name = (node.name or "").strip()
            desc = ""
            try:
                desc = (node.description or "").strip()
            except Exception:
                pass
            if not (name or desc):
                continue
            dedup = f"{name}::{desc}::{role}"
            if dedup in seen_names:
                continue
            seen_names.add(dedup)
            controls.append({"type": role, "name": name, "description": desc, "depth": depth})
            if len(controls) >= self.MAX_RESULTS:
                break

        return controls

    def _score_node(self, node, desc_lower: str, desc_tokens: set, depth: int) -> float:
        """Score an AT-SPI node against a plain-English description."""
        name_l = (node.name or "").lower()
        try:
            node_desc_l = (node.description or "").lower()
        except Exception:
            node_desc_l = ""
        role = self._role_name(node)
        s = 0.0

        if desc_lower == name_l:
            s += 200
        elif desc_lower == node_desc_l:
            s += 150

        if len(desc_lower) >= 3:
            if desc_lower in name_l:
                s += 40
            if desc_lower in node_desc_l:
                s += 20

        name_tokens = set(re.findall(r"[a-z0-9]+", name_l))
        desc_tok    = set(re.findall(r"[a-z0-9]+", node_desc_l))
        s += 8 * len(desc_tokens & name_tokens)
        s += 3 * len(desc_tokens & desc_tok)

        if role in _ATSPI_ACTIONABLE_ROLES:
            s += 30
        else:
            s -= 20

        s -= depth * 0.2
        return s

    def _get_node_center(self, node) -> tuple[int, int] | None:
        """Return screen (x, y) center of node's bounding box, or None."""
        try:
            comp = node.queryComponent()
            box  = comp.getExtents(pyatspi.DESKTOP_COORDS)
            if box.width <= 0 or box.height <= 0:
                return None
            return box.x + box.width // 2, box.y + box.height // 2
        except Exception:
            return None

    def _do_click_linux(self, x: int, y: int, click_type: str = "left_click") -> str:
        """Click at absolute screen coordinates using xdotool."""
        button = {"left_click": "1", "right_click": "3", "double_click": "1"}.get(click_type, "1")
        cmds = (
            ["xdotool", "mousemove", "--sync", str(x), str(y)],
            ["xdotool", "click", "--clearmodifiers", button],
        )
        if click_type == "double_click":
            cmds = (
                ["xdotool", "mousemove", "--sync", str(x), str(y)],
                ["xdotool", "click", "--clearmodifiers", "--repeat", "2", button],
            )
        for cmd in cmds:
            _, err = _run(cmd)
            if err:
                return f"{click_type} at ({x},{y}) — warning: {err[:100]}"
        return f"Success: {click_type} at screen ({x},{y})"

    def find_and_act(self, window_title: str, description: str,
                     action: str = "click", text_to_type: str = "") -> str:
        """
        One-call UI interaction — mirrors AppMapNavigator.find_and_act.

        1. Find the AT-SPI window.
        2. Walk the entire tree, score every node against description.
        3. Act on the best match:
           click     → try AT-SPI DoAction("click"), fall back to xdotool coordinate click
           set_text  → try AT-SPI SetValue, fall back to focus + xdotool type
           get_text  → AT-SPI GetText or Name
        """
        if not _PYATSPI_AVAILABLE:
            return "Error: pyatspi not installed. Run: pip install pyatspi"

        win = self._find_atspi_window(window_title)
        if not win:
            return (
                f"Error: Window '{window_title}' not found. "
                f"Run list_active_windows to see open window titles."
            )

        desc_lower  = description.strip().lower()
        desc_tokens = set(re.findall(r"[a-z0-9]+", desc_lower))

        candidates = []
        try:
            for node, depth in self._walk_atspi(win, self.MAX_INSPECT_DEPTH):
                role = self._role_name(node)
                if role not in _ATSPI_ACTIONABLE_ROLES and role not in _ATSPI_CONTAINER_ROLES:
                    continue
                name = (node.name or "").strip()
                try:
                    ndesc = (node.description or "").strip()
                except Exception:
                    ndesc = ""
                if not (name or ndesc):
                    continue
                candidates.append((node, depth))
                if len(candidates) >= 400:
                    break
        except Exception as e:
            return f"Error walking AT-SPI tree for '{window_title}': {e}"

        if not candidates:
            return (
                f"No interactive elements found in '{window_title}'. "
                f"The app may not expose an AT-SPI tree. "
                f"Try fallback_click_text instead."
            )

        scored = sorted(candidates, key=lambda nd: self._score_node(nd[0], desc_lower, desc_tokens, nd[1]), reverse=True)
        best_node, best_depth = scored[0]
        best_score = self._score_node(best_node, desc_lower, desc_tokens, best_depth)

        if best_score <= 0:
            sample = ", ".join(
                f"'{n.name}' ({self._role_name(n)})"
                for n, _ in scored[:25]
                if (n.name or "").strip()
            )
            return (
                f"No element matched '{description}' in '{window_title}'. "
                f"Available elements include: {sample}. "
                f"Retry with one of these exact names."
            )

        elem_desc = (best_node.name or "").strip() or self._role_name(best_node)

        # ── get_text ──────────────────────────────────────────────────────────
        if action == "get_text":
            try:
                text_iface = best_node.queryText()
                return f"Text of '{elem_desc}': {text_iface.getText(0, -1)}"
            except Exception:
                return f"Text of '{elem_desc}': {best_node.name}"

        # ── set_text ──────────────────────────────────────────────────────────
        if action == "set_text":
            # Try AT-SPI EditableText interface first
            try:
                edit = best_node.queryEditableText()
                edit.setTextContents(text_to_type)
                return f"Success: set text of '{elem_desc}' to '{text_to_type[:40]}'"
            except Exception:
                pass
            # Fall back: focus element, clear, type via xdotool
            center = self._get_node_center(best_node)
            if center:
                self._do_click_linux(*center)
                time.sleep(0.1)
                # Select all + delete existing text
                _run(["xdotool", "key", "--clearmodifiers", "ctrl+a"])
                time.sleep(0.05)
                _run(["xdotool", "key", "--clearmodifiers", "Delete"])
                time.sleep(0.05)
                _, err = _run(["xdotool", "type", "--clearmodifiers", "--delay", "20", text_to_type])
                if err:
                    return f"set_text via xdotool on '{elem_desc}' — warning: {err[:100]}"
                return f"Success: typed into '{elem_desc}' via xdotool"
            return f"Error: Could not set text on '{elem_desc}' — no bounding box."

        # ── click ─────────────────────────────────────────────────────────────
        # (a) AT-SPI DoAction "click"
        try:
            actions = best_node.queryAction()
            for i in range(actions.nActions):
                if actions.getName(i).lower() in ("click", "activate", "press"):
                    actions.doAction(i)
                    return f"Success: invoked '{elem_desc}' (AT-SPI DoAction)."
        except Exception:
            pass

        # (b) Coordinate click via xdotool
        center = self._get_node_center(best_node)
        if center:
            # Focus the window first
            win_id = self._find_window_id(window_title)
            if win_id:
                _run(["xdotool", "windowfocus", "--sync", win_id])
                time.sleep(0.1)
            return self._do_click_linux(*center, click_type=action if action in ("left_click", "right_click", "double_click") else "left_click")

        return (
            f"Found '{elem_desc}' but could not determine its screen position. "
            f"Try fallback_click_text instead."
        )

    def safely_trigger_ui_element(self, window_title: str, control_type: str,
                                   search_property: str, property_value: str,
                                   action: str, text_to_type: str = "") -> str:
        """
        Manual precise interaction — mirrors AppMapNavigator.safely_trigger_ui_element.
        Searches by name or description match, then acts.
        """
        win = self._find_atspi_window(window_title)
        if not win:
            return f"Error: Window '{window_title}' not found."

        target = property_value.lower()
        best   = None

        for node, _ in self._walk_atspi(win, self.MAX_INSPECT_DEPTH):
            name = (node.name or "").lower()
            try:
                ndesc = (node.description or "").lower()
            except Exception:
                ndesc = ""
            if search_property == "name" and target in name:
                best = node
                break
            if search_property in ("automation_id", "class_name") and target in ndesc:
                best = node
                break

        if not best:
            return (
                f"Error: Could not locate {control_type} matching "
                f"{search_property}='{property_value}' in '{window_title}'."
            )

        return self.find_and_act(window_title, best.name or property_value, action, text_to_type)

    def read_aggregated_text(self, window_title: str, container_key: str = None) -> str:
        """
        Aggregate all text from a window's AT-SPI tree into readable lines —
        mirrors AppMapNavigator.read_aggregated_text.
        """
        win = self._find_atspi_window(window_title)
        if not win:
            return "Window not found."

        root = win
        if container_key:
            root = self._live_cache.get(window_title, {}).get(container_key, win)

        lines_by_y: dict[int, list[str]] = {}

        for node, _ in self._walk_atspi(root, self.MAX_INSPECT_DEPTH):
            role = self._role_name(node)
            if role not in ("label", "text", "static text"):
                continue
            text = (node.name or "").strip()
            if not text:
                continue
            y = 0
            try:
                comp = node.queryComponent()
                box  = comp.getExtents(pyatspi.DESKTOP_COORDS)
                y    = box.y
            except Exception:
                pass
            # Group text on roughly the same Y line (within 12px)
            bucket = next((k for k in lines_by_y if abs(k - y) < 12), y)
            lines_by_y.setdefault(bucket, []).append(text)

        if not lines_by_y:
            return ""
        return "\n".join(" ".join(words) for _, words in sorted(lines_by_y.items()))

    def query_gemini_app(self, prompt: str, wait_for_response: int = 90) -> str:
        from browser_cdp import query_gemini_app
        """
        Thin delegator kept ONLY so external dispatchers that call tools as
        getattr(ui_navigator, tool_name)(**args) (e.g. gui.pyw) keep working.
        Does NOT use AT-SPI/xdotool — routes straight to the module-level,
        CDP/browser-based query_gemini_app(), same as the CLI dispatcher in
        this file uses directly.
        """
        return query_gemini_app(prompt, wait_for_response=wait_for_response)

    def manage_gemini_chat(self, action: str, chat_name: str = None) -> str:
        """Mirrors AppMapNavigator.manage_gemini_chat."""
        window_title = "Gemini"
        win = self._find_atspi_window(window_title)
        if not win:
            return "Error: Gemini window not found."

        if action == "new_chat":
            result = self.find_and_act(window_title, "New chat", action="click")
            return result if "Success" in result else "Error: Could not find 'New chat' button."

        if action == "open_recent" and chat_name:
            result = self.find_and_act(window_title, chat_name, action="click")
            return result if "Success" in result else f"Error: Could not find recent chat '{chat_name}'."

        return "Invalid action."

    def _get_map_path(self, window_title: str) -> str:
        safe = "".join(c if c.isalnum() else "_" for c in window_title.lower()).strip("_")
        return os.path.join(self.maps_dir, f"{safe}.json")

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


# ── Pick the right navigator based on OS ─────────────────────────────────────
if _IS_LINUX and _PYATSPI_AVAILABLE:
    ui_navigator = AppMapNavigatorLinux()
    print("🐧 [Linux UI navigator: AppMapNavigatorLinux (AT-SPI2 + xdotool)]")
elif _IS_LINUX and not _PYATSPI_AVAILABLE:
    ui_navigator = AppMapNavigatorLinux()   # still usable for xdotool fallbacks
    print("🐧 [Linux UI navigator: AppMapNavigatorLinux (xdotool only — install pyatspi for full tree access)]")
else:
    from ui_automation.windows_uia import ui_navigator

# =============================================================================

