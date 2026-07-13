# --- AUTO-SPLITTER: imports added by automated pass, please review ---
from config import GRID_STEP, MODEL_CANVAS_H, MODEL_CANVAS_W, SCALE_X, SCALE_Y
from config import _IS_LINUX
from ui_automation.windows_uia import _TESSERACT_AVAILABLE, _UIA_AVAILABLE
from PIL import Image as _PILImage
from PIL import ImageGrab as _ImageGrab
import base64
import io
import pytesseract
import subprocess
import time
import win32gui

# --- from main.py, section 1 ---
# 3. SCREEN CAPTURE & OCR
# =============================================================================

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
            return _ImageGrab.grab()
        raise RuntimeError("PIL ImageGrab not available.")


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
    Find text on screen via OCR and click its center in one step.
    Pass _screenshot to reuse an existing grab.
    Returns a status string.
    """
    if not _TESSERACT_AVAILABLE:
        return (
            "Tesseract OCR is not installed. "
            "Use fallback_view_screen + fallback_click_grid instead."
        )
    words = ocr_screen(screenshot=_screenshot)
    if words is None:
        return "OCR failed — cannot locate text."

    query   = text.strip().lower()
    matches = [w for w in words if query in w["text"].lower()]
    if not matches:
        all_words = sorted(set(w["text"] for w in words))
        return (
            f"Text '{text}' not found on screen. "
            f"Detected text includes: {', '.join(repr(w) for w in all_words[:40])}"
        )

    matches.sort(key=lambda w: w["conf"], reverse=True)
    best = matches[0]
    sx, sy = best["screen_x"], best["screen_y"]
    cx, cy = best["canvas_x"], best["canvas_y"]

    print(f"   [OCR Click] '{best['text']}' conf={best['conf']}% "
          f"canvas({cx},{cy}) → screen({sx},{sy})")
    return _do_click(sx, sy, click_type, label=f"OCR '{best['text']}'")


def fallback_click_grid(x, y, click_type="left_click"):
    """
    x, y are CANVAS coordinates from the grid screenshot.
    Python scales to real screen pixels before clicking.
    """
    real_x, real_y = _scale_canvas_to_screen(x, y)
    print(f"   [Grid Click] canvas({x},{y}) → screen({real_x},{real_y})")
    return _do_click(real_x, real_y, click_type, label=f"grid ({x},{y})")


def _do_click(screen_x, screen_y, click_type, label=""):
    from tools_registry import execute_terminal_command
    from ui_automation.linux_navigator import _run
    """
    Perform a mouse click at real screen coordinates.
    On Linux: uses xdotool. On Windows: uses PowerShell + user32.dll.
    """
    if _IS_LINUX:
        button = {"left_click": "1", "right_click": "3", "double_click": "1"}.get(click_type, "1")
        _run(["xdotool", "mousemove", "--sync", str(screen_x), str(screen_y)])
        if click_type == "double_click":
            _, err = _run(["xdotool", "click", "--clearmodifiers", "--repeat", "2", button])
        else:
            _, err = _run(["xdotool", "click", "--clearmodifiers", button])
        if err:
            return f"{click_type} at screen({screen_x},{screen_y}) [{label}] — warning: {err[:100]}"
        return f"Success: {click_type} at screen({screen_x},{screen_y}) [{label}]"

    # Windows path
    try:
        if click_type == "double_click":
            events = (
                "$m::mouse_event(0x0002,0,0,0,0)\n"
                "$m::mouse_event(0x0004,0,0,0,0)\n"
                "Start-Sleep -Milliseconds 50\n"
                "$m::mouse_event(0x0002,0,0,0,0)\n"
                "$m::mouse_event(0x0004,0,0,0,0)"
            )
        elif click_type == "right_click":
            events = (
                "$m::mouse_event(0x0008,0,0,0,0)\n"
                "$m::mouse_event(0x0010,0,0,0,0)"
            )
        else:
            events = (
                "$m::mouse_event(0x0002,0,0,0,0)\n"
                "$m::mouse_event(0x0004,0,0,0,0)"
            )

        ps_script = (
            f"Add-Type -AssemblyName System.Windows.Forms\n"
            f"[System.Windows.Forms.Cursor]::Position = "
            f"New-Object System.Drawing.Point({screen_x},{screen_y})\n"
            f"Start-Sleep -Milliseconds 50\n"
            f"$sig = '[DllImport(\"user32.dll\")] public static extern void "
            f"mouse_event(int flags, int dx, int dy, int data, int extra);'\n"
            f"$m = Add-Type -MemberDefinition $sig -Name 'Win32M' -Namespace W32 -PassThru\n"
            f"{events}"
        )
        result  = execute_terminal_command(ps_script)
        stderr  = result.split("STDERR:")[-1].strip() if "STDERR:" in result else ""
        if stderr:
            return f"{click_type} at screen({screen_x},{screen_y}) [{label}] — warning: {stderr[:150]}"
        return f"Success: {click_type} at screen({screen_x},{screen_y}) [{label}]"
    except Exception as e:
        return f"Error simulating click: {str(e)}"


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

