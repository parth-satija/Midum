# --- AUTO-SPLITTER: imports added by automated pass, please review ---
from config import GRID_STEP, MODEL_CANVAS_H, MODEL_CANVAS_W, SCALE_X, SCALE_Y
from config import _IS_LINUX, _IS_MAC
from ui_automation.windows_uia import _TESSERACT_AVAILABLE, _UIA_AVAILABLE
from PIL import Image as _PILImage
from PIL import ImageGrab as _ImageGrab
import base64
import io
import pytesseract
import subprocess
import time
import os
import sys
import time

if sys.platform == "win32":
    import win32gui
else:
    win32gui = None
# --- from main.py, section 1 ---
# 3. SCREEN CAPTURE & OCR
# =============================================================================

_IS_LINUX = sys.platform.startswith("linux")
_IS_WINDOWS = sys.platform == "win32"
_IS_MAC = sys.platform == "darwin"

if _IS_WINDOWS:
    import ctypes
    # Crucial: Force process DPI awareness so physical OCR pixels match Windows screen coordinates
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor DPI Aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDpiAware()
        except Exception:
            pass

    # Define C Structures for SendInput API
    PUL = ctypes.POINTER(ctypes.c_ulong)

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", PUL),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("iu", INPUT_UNION)]

    # Input Flags
    INPUT_MOUSE = 0
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010

def _grab_full_screenshot():
    from main import _IMAGEGRAB_AVAILABLE
    """Grab the full-resolution screen and return a PIL Image."""
    from PIL import Image as _PILImage
    if _IS_LINUX:
        # Try scrot first (most reliable, works on X11 and XWayland)
        try:
            tmp = "/tmp/jarvis_screenshot.png"
            subprocess.run(["scrot", "-z", tmp], timeout=5, check=True,
                           capture_output=True)
            img = _PILImage.open(tmp)
            img.load()   # fully load before the file is potentially reused
            return img
        except Exception:
            pass
        # Fallback: gnome-screenshot
        try:
            tmp = "/tmp/jarvis_screenshot.png"
            subprocess.run(["gnome-screenshot", "-f", tmp], timeout=5,
                           capture_output=True)
            return _PILImage.open(tmp)
        except Exception:
            pass
        # Fallback: PIL ImageGrab with X display (requires python3-xlib)
        if _IMAGEGRAB_AVAILABLE:
            return _ImageGrab.grab()
        raise RuntimeError(
            "No screenshot tool available. "
            "Install scrot:  sudo apt install scrot"
        )
    else:
        if _IMAGEGRAB_AVAILABLE:
            # PIL's ImageGrab supports macOS natively (shells out to the
            # built-in `screencapture` binary under the hood since Pillow
            # 6.0), so this same branch covers both Windows and macOS. On
            # macOS this requires the Screen Recording permission (System
            # Settings → Privacy & Security → Screen Recording) — without
            # it, ImageGrab.grab() silently returns a black image instead
            # of raising, so a suspiciously blank screenshot on Mac usually
            # means that permission hasn't been granted.
            return _ImageGrab.grab()
        raise RuntimeError("PIL ImageGrab not available.")


def capture_screen_frame_jpeg_bytes(max_w: int = 1024, quality: int = 70) -> bytes:
    """
    Grab the full screen and return raw JPEG bytes (NOT base64), downscaled so
    the long edge is at most `max_w` px. No grid overlay, no OCR — this is the
    lightweight per-frame capture used by the Gemini Live screen-share task
    (providers/gemini_live_backend.py) to stream the desktop to the model as
    realtime video while voice mode is active. Kept separate from
    capture_screen_to_ram() (which is the on-demand, grid-annotated,
    base64-encoded single-shot screenshot tool) since the live-share path
    needs to run several times a second with minimal overhead.
    """
    screenshot = _grab_full_screenshot()
    w, h = screenshot.size
    if w > max_w:
        new_h = int(h * (max_w / w))
        screenshot = screenshot.resize((max_w, new_h), resample=_PILImage.LANCZOS)
    buf = io.BytesIO()
    screenshot.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _scale_canvas_to_screen(cx, cy):
    """Convert canvas coordinates to real screen coordinates."""
    return int(round(cx * SCALE_X)), int(round(cy * SCALE_Y))


def _scale_screen_to_canvas(rx, ry):
    """Convert real screen coordinates to canvas coordinates."""
    return int(round(rx / SCALE_X)), int(round(ry / SCALE_Y))


def capture_screen_to_ram():
    """
    Grab screen → downscale to canvas → burn coordinate grid → return base64 JPEG.
    The grid labels are at canvas resolution. The model reads them and passes them
    directly to fallback_click_grid; Python scales back to real pixels.
    """
    try:
        from PIL import ImageDraw, ImageFont
        screenshot = _grab_full_screenshot()

        # Downscale to canvas
        from PIL import Image as _PILImage
        canvas = screenshot.resize((MODEL_CANVAS_W, MODEL_CANVAS_H), resample=_PILImage.LANCZOS)
        draw   = ImageDraw.Draw(canvas)
        cw, ch = canvas.size

        try:
            font = ImageFont.truetype("cour.ttf", 10)
        except Exception:
            font = ImageFont.load_default()

        line_col   = (60, 60, 60)
        label_fg   = (255, 255, 0)
        label_shad = (0, 0, 0)

        for x in range(0, cw, GRID_STEP):
            draw.line([(x, 0), (x, ch)], fill=line_col, width=1)
            draw.text((x + 2, 3), str(x), font=font, fill=label_shad)
            draw.text((x + 1, 2), str(x), font=font, fill=label_fg)

        for y in range(0, ch, GRID_STEP):
            draw.line([(0, y), (cw, y)], fill=line_col, width=1)
            draw.text((3, y + 2), str(y), font=font, fill=label_shad)
            draw.text((2, y + 1), str(y), font=font, fill=label_fg)

        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=75)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        return f"Error capturing screen: {str(e)}"


# In-memory OCR cache: (screenshot_id, results_list)
# Avoids re-running Tesseract when find_text and click_text are called in the
# same turn from the same screenshot. Cache is invalidated by a new grab.
_ocr_cache: tuple = (None, None)   # (id(PIL_image), results)

def ocr_screen(screenshot=None):
    """
    Run Tesseract entirely in RAM — no temp files, no disk writes.

    pytesseract.image_to_data() accepts a PIL Image directly and pipes it
    to the tesseract process via stdin (using the 'pipe:' input method
    internally). No intermediate file is created on disk.

    Pass an existing PIL screenshot to reuse a grab; omit to grab fresh.
    Results are cached per PIL image object so the same screenshot is never
    OCR'd twice in one turn.

    Returns a list of word dicts or None if Tesseract is unavailable.
    """
    global _ocr_cache
    if not _TESSERACT_AVAILABLE:
        return None
    try:
        import pytesseract
        if screenshot is None:
            screenshot = _grab_full_screenshot()

        # Cache hit — same PIL object (same turn, same grab)
        if _ocr_cache[0] is id(screenshot):
            return _ocr_cache[1]

        # image_to_data with a PIL Image uses stdin piping internally —
        # no temp file is written to disk.
        data = pytesseract.image_to_data(
            screenshot,
            output_type=pytesseract.Output.DICT,
            nice=0,          # don't lower process priority
        )
        results = []
        n = len(data["text"])
        for i in range(n):
            word = data["text"][i].strip()
            conf = int(data["conf"][i])
            if not word or conf < 30:
                continue
            left = data["left"][i]
            top  = data["top"][i]
            w    = data["width"][i]
            h    = data["height"][i]
            sx   = left + w // 2
            sy   = top  + h // 2
            cx, cy = _scale_screen_to_canvas(sx, sy)
            results.append({
                "text":     word,
                "conf":     conf,
                "screen_x": sx,
                "screen_y": sy,
                "canvas_x": cx,
                "canvas_y": cy,
                "left": left, "top": top, "w": w, "h": h,
            })
        _ocr_cache = (id(screenshot), results)
        return results
    except Exception:
        return None


def fallback_find_text(text, _screenshot=None):
    """
    Tool implementation for fallback_find_text.
    Returns a structured text report of all matches with canvas coordinates.
    Pass _screenshot to reuse an existing grab (avoids a second screen capture).
    """
    if not _TESSERACT_AVAILABLE:
        return (
            "Tesseract OCR is not installed or not found. "
            "Cannot use text-based screen search. "
            "Install Tesseract from https://github.com/UB-Mannheim/tesseract/wiki "
            "and set TESSERACT_PATH in main.py. "
            "Fall back to fallback_view_screen + fallback_click_grid with grid coordinates."
        )
    words = ocr_screen(screenshot=_screenshot)
    if words is None:
        return "OCR failed — screen could not be read."

    query  = text.strip().lower()
    # Collect all words whose text contains the query (substring, case-insensitive)
    matches = [w for w in words if query in w["text"].lower()]

    if not matches:
        # Show everything Tesseract found so the model can adapt
        all_words = sorted(set(w["text"] for w in words))
        return (
            f"No text matching '{text}' found on screen.\n"
            f"All detected text on screen:\n"
            + ", ".join(f'"{w}"' for w in all_words[:80])
            + (" ... (truncated)" if len(all_words) > 80 else "")
        )

    # Sort by confidence descending; best match first
    matches.sort(key=lambda w: w["conf"], reverse=True)
    best = matches[0]

    lines = [
        f"Found {len(matches)} match(es) for '{text}'.",
        f"Best match: '{best['text']}' (conf={best['conf']}%) "
        f"at canvas ({best['canvas_x']}, {best['canvas_y']}) "
        f"→ screen ({best['screen_x']}, {best['screen_y']})",
        "",
        "All matches (canvas coords):",
    ]
    for m in matches[:10]:   # cap at 10 to keep output compact
        lines.append(
            f"  '{m['text']}' conf={m['conf']}% "
            f"canvas=({m['canvas_x']},{m['canvas_y']})"
        )
    return "\n".join(lines)


def fallback_click_text(text, click_type="left_click", _screenshot=None):
    """
    Find text on screen via OCR and click the geometric center of its 
    bounding rectangle (handles multi-word phrases and line bounding boxes).
    """
    if not _TESSERACT_AVAILABLE:
        return (
            "Tesseract OCR is not installed. "
            "Use fallback_view_screen + fallback_click_grid instead."
        )

    words = ocr_screen(screenshot=_screenshot)
    if not words:
        return "OCR failed — cannot locate text."

    query_tokens = text.strip().lower().split()
    if not query_tokens:
        return "Empty search query provided."

    matched_boxes = []

    # Strategy 1: Match multi-word sequences across adjacent tokens
    for i in range(len(words) - len(query_tokens) + 1):
        sequence = words[i : i + len(query_tokens)]
        seq_text = " ".join(w["text"].strip().lower() for w in sequence)
        
        if " ".join(query_tokens) in seq_text:
            matched_boxes.append(sequence)

    # Strategy 2: Single word / substring fallback if sequence match fails
    if not matched_boxes:
        query_str = " ".join(query_tokens)
        single_matches = [w for w in words if query_str in w["text"].lower()]
        if single_matches:
            matched_boxes = [[w] for w in single_matches]

    if not matched_boxes:
        all_words = sorted(set(w["text"] for w in words))
        return (
            f"Text '{text}' not found on screen. "
            f"Detected text includes: {', '.join(repr(w) for w in all_words[:40])}"
        )

    # Sort grouped matches by average confidence
    def get_avg_conf(group):
        return sum(w.get("conf", 0) for w in group) / len(group)

    matched_boxes.sort(key=get_avg_conf, reverse=True)
    best_group = matched_boxes[0]

    # Compute enclosing bounding rectangle across all tokens in the match group
    min_x = min(w["screen_x"] for w in best_group)
    min_y = min(w["screen_y"] for w in best_group)
    
    max_x = max(
        w["screen_x"] + w.get("width", w.get("w", 0)) for w in best_group
    )
    max_y = max(
        w["screen_y"] + w.get("height", w.get("h", 0)) for w in best_group
    )

    # Geometric center of the entire bounding box
    center_x = min_x + (max_x - min_x) // 2
    center_y = min_y + (max_y - min_y) // 2

    matched_text_str = " ".join(w["text"] for w in best_group)
    avg_conf = get_avg_conf(best_group)

    print(
        f"   [OCR Click] Match: '{matched_text_str}' (conf={avg_conf:.1f}%) "
        f"| Box: ({min_x},{min_y})->({max_x},{max_y}) "
        f"| Center Target: ({center_x}, {center_y})"
    )

    return _do_click(center_x, center_y, click_type, label=f"OCR '{matched_text_str}'")


def fallback_click_grid(x, y, click_type="left_click"):
    """
    x, y are CANVAS coordinates from the grid screenshot.
    Python scales to real screen pixels before clicking.
    """
    real_x, real_y = _scale_canvas_to_screen(x, y)
    print(f"   [Grid Click] canvas({x},{y}) → screen({real_x},{real_y})")
    return _do_click(real_x, real_y, click_type, label=f"grid ({x},{y})")


def _do_click(screen_x, screen_y, click_type="left_click", label=""):
    """
    Perform a mouse click at physical screen coordinates without invoking slow subshells.
    Handles Windows DPI scaling natively and supports both X11/Wayland on Linux.
    """
    screen_x, screen_y = int(screen_x), int(screen_y)

    # -------------------------------------------------------------------------
    # LINUX PATH
    # -------------------------------------------------------------------------
    if _IS_LINUX:
        from ui_automation.linux_navigator import _run

        is_wayland = os.environ.get("XDG_SESSION_TYPE") == "wayland"

        if is_wayland:
            # ydotool is required for Wayland display servers
            btn = {"left_click": "0xC0", "right_click": "0xC1", "double_click": "0xC0"}.get(click_type, "0xC0")
            _run(["ydotool", "mousemove", "-a", str(screen_x), str(screen_y)])
            time.sleep(0.05)
            if click_type == "double_click":
                _run(["ydotool", "click", "--repeat", "2", btn])
            else:
                _run(["ydotool", "click", btn])
        else:
            # standard X11 fallback via xdotool
            btn = {"left_click": "1", "right_click": "3", "double_click": "1"}.get(click_type, "1")
            _run(["xdotool", "mousemove", "--sync", str(screen_x), str(screen_y)])
            time.sleep(0.05)
            if click_type == "double_click":
                _run(["xdotool", "click", "--clearmodifiers", "--repeat", "2", btn])
            else:
                _run(["xdotool", "click", "--clearmodifiers", btn])

        return f"Success: {click_type} at screen({screen_x},{screen_y}) [{label}]"

    # -------------------------------------------------------------------------
    # WINDOWS PATH (Direct User32 API via ctypes — ultra-fast & DPI aware)
    # -------------------------------------------------------------------------
    if _IS_WINDOWS:
        try:
            # Step 1: Warp cursor to exact physical coordinates
            ctypes.windll.user32.SetCursorPos(screen_x, screen_y)
            time.sleep(0.05)  # Let UI thread process hover state

            # Helper for low-level mouse events
            def send_mouse_event(down_flag, up_flag):
                extra = ctypes.c_ulong(0)
                ii_down = INPUT(
                    type=INPUT_MOUSE,
                    iu=INPUT_UNION(mi=MOUSEINPUT(0, 0, 0, down_flag, 0, ctypes.pointer(extra)))
                )
                ii_up = INPUT(
                    type=INPUT_MOUSE,
                    iu=INPUT_UNION(mi=MOUSEINPUT(0, 0, 0, up_flag, 0, ctypes.pointer(extra)))
                )
                
                ctypes.windll.user32.SendInput(1, ctypes.pointer(ii_down), ctypes.sizeof(ii_down))
                time.sleep(0.03)  # 30ms hold time
                ctypes.windll.user32.SendInput(1, ctypes.pointer(ii_up), ctypes.sizeof(ii_up))

            # Step 2: Trigger requested click type
            if click_type == "right_click":
                send_mouse_event(MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)
            elif click_type == "double_click":
                send_mouse_event(MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
                time.sleep(0.08)
                send_mouse_event(MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)
            else:
                send_mouse_event(MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)

            return f"Success: {click_type} at screen({screen_x},{screen_y}) [{label}]"

        except Exception as e:
            return f"Error simulating click: {str(e)}"

    # -------------------------------------------------------------------------
    # MACOS PATH (Quartz CGEvent — same mechanism used by ui_automation/mac_navigator.py)
    # -------------------------------------------------------------------------
    if _IS_MAC:
        try:
            import Quartz
            button = Quartz.kCGMouseButtonLeft if click_type != "right_click" else Quartz.kCGMouseButtonRight
            down_t = Quartz.kCGEventLeftMouseDown if click_type != "right_click" else Quartz.kCGEventRightMouseDown
            up_t   = Quartz.kCGEventLeftMouseUp   if click_type != "right_click" else Quartz.kCGEventRightMouseUp

            def _once():
                d = Quartz.CGEventCreateMouseEvent(None, down_t, (screen_x, screen_y), button)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, d)
                u = Quartz.CGEventCreateMouseEvent(None, up_t, (screen_x, screen_y), button)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, u)

            _once()
            if click_type == "double_click":
                time.sleep(0.05)
                _once()
            return f"Success: {click_type} at screen({screen_x},{screen_y}) [{label}]"
        except ImportError:
            return (
                "Error: pyobjc (Quartz) not installed. "
                "pip install pyobjc-framework-Quartz"
            )
        except Exception as e:
            return f"Error simulating click: {str(e)}"

    return "Unsupported OS platform."


def type_text(text, special_key=None, expected_window: str = ""):
    from tools_registry import execute_terminal_command
    from ui_automation.linux_navigator import _run
    """Type text at the current cursor position. Works on Windows and Linux."""
    try:
        # ── Foreground window guard ───────────────────────────────────────────
        if expected_window:
            if _IS_LINUX:
                focused_out, _ = _run(["xdotool", "getactivewindow"])
                wid = focused_out.strip()
                if wid:
                    name_out, _ = _run(["xdotool", "getwindowname", wid])
                    fg_title = name_out.strip()
                    if expected_window.lower() not in fg_title.lower():
                        return (
                            f"[TYPING ABORTED] Expected foreground window containing "
                            f"'{expected_window}' but active window is '{fg_title}'. "
                            f"Call click_ui_element to focus the correct window first, "
                            f"then call type_text again."
                        )
            elif _UIA_AVAILABLE:
                fg_hwnd  = win32gui.GetForegroundWindow()
                fg_title = win32gui.GetWindowText(fg_hwnd).strip()
                if expected_window.lower() not in fg_title.lower():
                    return (
                        f"[TYPING ABORTED] Expected foreground window containing "
                        f"'{expected_window}' but active window is '{fg_title}'. "
                        f"Call click_ui_element to focus the correct window first, "
                        f"then call type_text again."
                    )

        # ── Linux: xdotool type via clipboard (handles all special chars) ──────
        if _IS_LINUX:
            # xdotool type --clearmodifiers breaks on +, $, ", etc.
            # Safest approach: copy text to clipboard and paste it.
            # This works for all characters including Unicode.
            try:
                clip_proc = subprocess.run(
                    ["xclip", "-selection", "clipboard"],
                    input=text, text=True, capture_output=True, timeout=5
                )
                if clip_proc.returncode != 0:
                    # Try xsel as fallback
                    subprocess.run(
                        ["xsel", "--clipboard", "--input"],
                        input=text, text=True, timeout=5
                    )
                # Small delay then paste
                time.sleep(0.05)
                _run(["xdotool", "key", "--clearmodifiers", "ctrl+v"])
                time.sleep(0.05)
            except FileNotFoundError:
                # xclip/xsel not installed — fall back to xdotool type with escaping
                safe = text.replace("\\", "\\\\").replace("'", "\\'")
                _, err = _run(["xdotool", "type", "--clearmodifiers", "--delay", "20", safe])
                if err:
                    return f"Warning typing text: {err[:100]}"

            if special_key:
                xdotool_keys = {
                    "enter": "Return", "tab": "Tab", "escape": "Escape",
                    "backspace": "BackSpace", "delete": "Delete",
                    "home": "Home", "end": "End",
                    "pageup": "Page_Up", "pagedown": "Page_Down",
                    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
                    "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4",
                    "f5": "F5", "f6": "F6", "f7": "F7", "f8": "F8",
                    "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
                }
                key = xdotool_keys.get(special_key.lower(), special_key)
                _run(["xdotool", "key", "--clearmodifiers", key])
            suffix = f" + {special_key}" if special_key else ""
            return f"Success: typed '{text[:40]}{'...' if len(text) > 40 else ''}'{suffix}"

        # ── macOS: Quartz CGEvent Unicode keyboard events ────────────────────────────
        if _IS_MAC:
            try:
                import Quartz
                for ch in text:
                    ev = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
                    Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                    ev_up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
                    Quartz.CGEventKeyboardSetUnicodeString(ev_up, len(ch), ch)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_up)

                _MAC_KEYCODES = {
                    "enter": 36, "tab": 48, "escape": 53,
                    "backspace": 51, "delete": 117,
                    "home": 115, "end": 119,
                    "pageup": 116, "pagedown": 121,
                    "up": 126, "down": 125, "left": 123, "right": 124,
                }
                if special_key:
                    code = _MAC_KEYCODES.get(special_key.lower())
                    if code is not None:
                        kd = Quartz.CGEventCreateKeyboardEvent(None, code, True)
                        Quartz.CGEventPost(Quartz.kCGHIDEventTap, kd)
                        ku = Quartz.CGEventCreateKeyboardEvent(None, code, False)
                        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ku)
                suffix = f" + {special_key}" if special_key else ""
                return f"Success: typed '{text[:40]}{'...' if len(text) > 40 else ''}'{suffix}"
            except ImportError:
                return "Error: pyobjc (Quartz) not installed. pip install pyobjc-framework-Quartz"
            except Exception as e:
                return f"Error typing text on macOS: {str(e)}"

        # ── Windows: PowerShell SendKeys ──────────────────────────────────────
        special_chars = "~%^+{}[]()"
        escaped = ""
        for ch in text:
            escaped += ("{" + ch + "}") if ch in special_chars else ch

        key_map = {
            "enter": "~", "tab": "{TAB}", "escape": "{ESC}",
            "backspace": "{BACKSPACE}", "delete": "{DELETE}",
            "home": "{HOME}", "end": "{END}",
            "pageup": "{PGUP}", "pagedown": "{PGDN}",
            "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
        }
        if special_key:
            sk = special_key.lower()
            escaped += key_map.get(sk, "{" + special_key.upper() + "}")

        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms\n"
            f'[System.Windows.Forms.SendKeys]::SendWait("{escaped}")'
        )
        result = execute_terminal_command(ps_script)
        suffix = f" + {special_key}" if special_key else ""
        return f"Success: typed '{text[:40]}{'...' if len(text)>40 else ''}'{suffix}"
    except Exception as e:
        return f"Error typing text: {str(e)}"


# =============================================================================

