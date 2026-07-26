# =============================================================================
# GLOBAL PUSH-TO-TALK HOTKEYS (Voice tab)
# =============================================================================
# Two independent, fully-configurable global hotkeys -- each bound to
# either a keyboard key or a mouse button -- that drive push-to-talk
# streaming into whatever VoiceSession (providers/gemini_live_backend.py)
# is currently active. "Global" means they fire no matter which window has
# focus, not just while Midum's own window is active (pynput installs an
# OS-level input hook for this, same mechanism as the `keyboard` package
# already in requirements.txt -- pynput is used here instead because it
# also supports mouse buttons, including the side buttons ("Mouse 4"/
# "Mouse 5", i.e. XButton1/XButton2), which `keyboard` alone can't do).
#
# Behavior (see PushToTalkManager._press_slot / _release_slot):
#   PRESS   -> if no VoiceSession is running yet, start() one (this is what
#              actually opens the Gemini Live connection). Then unmute the
#              mic so audio starts streaming.
#   RELEASE -> mute the mic only. The Gemini Live connection is left open
#              (VoiceSession.stop() is never called here), so the very next
#              press can resume streaming instantly -- no reconnect, and
#              Midum isn't listening to anything in between presses.
#
# Two slots ("slot1", "slot2"), independently rebindable at runtime via
# begin_capture()/set_hotkey(), persisted to storage/ptt_hotkeys.json.
# Defaults: Right Alt (keyboard) and the mouse side button (x1 / "Mouse 4").
# =============================================================================

import json
import os
import queue
import threading

try:
    from pynput import keyboard, mouse
    _PYNPUT_AVAILABLE = True
except ImportError:
    keyboard = None
    mouse = None
    _PYNPUT_AVAILABLE = False

_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage", "ptt_hotkeys.json")

DEFAULT_HOTKEYS = [
    {"id": "slot1", "kind": "keyboard", "value": "alt_r", "label": "Right Alt"},
    {"id": "slot2", "kind": "mouse", "value": "x1", "label": "Mouse Button 4 (Side)"},
]

# Mouse-button token <-> pynput Button + display label. Built lazily under
# the availability guard above since mouse.Button doesn't exist at all
# when pynput isn't installed.
_MOUSE_BUTTON_MAP = {}
_MOUSE_LABELS = {
    "left": "Mouse Left", "right": "Mouse Right", "middle": "Mouse Middle",
    "x1": "Mouse Button 4 (Side)", "x2": "Mouse Button 5 (Side)",
}
if _PYNPUT_AVAILABLE:
    _MOUSE_BUTTON_MAP = {
        "left": mouse.Button.left,
        "right": mouse.Button.right,
        "middle": mouse.Button.middle,
        "x1": getattr(mouse.Button, "x1", None),
        "x2": getattr(mouse.Button, "x2", None),
    }


def _load():
    try:
        if os.path.exists(_SETTINGS_FILE):
            with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            slots = data.get("hotkeys")
            if isinstance(slots, list) and len(slots) == 2:
                return slots
    except Exception:
        pass
    return [dict(h) for h in DEFAULT_HOTKEYS]


def _save(slots) -> bool:
    try:
        os.makedirs(os.path.dirname(_SETTINGS_FILE), exist_ok=True)
        with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"hotkeys": slots}, f, indent=2)
        return True
    except Exception:
        return False


def pynput_status() -> str:
    return "OK" if _PYNPUT_AVAILABLE else "pynput not installed (pip install pynput) -- global push-to-talk hotkeys are unavailable."


class PushToTalkManager:
    """Owns the two global PTT hotkey slots and drives a VoiceSession
    (start-on-first-press, mute/unmute on press/release) from a background
    keyboard+mouse listener thread. One instance per process -- created by
    the GUI's Api class via get_manager() below."""

    def __init__(self, voice_session_getter, on_state_change=None):
        # voice_session_getter(): () -> VoiceSession | None. Passed in as a
        # callable (rather than the session object itself) so this module
        # never has to import providers/gemini_live_backend.py -- and by
        # extension never has to import google-genai/sounddevice -- just
        # to manage hotkey bindings. The Api class's self._voice_session
        # is created once in __init__, so a simple lambda works fine.
        self._get_session = voice_session_getter
        self._on_state_change = on_state_change or (lambda pressed_ids: None)
        self._slots = _load()
        self._pressed = set()          # slot ids currently physically held
        self._kb_listener = None
        self._mouse_listener = None
        self._lock = threading.Lock()
        # Serial worker: every press/release/capture-resolve event is
        # queued here instead of being handled inline on whichever thread
        # detected it. The pynput callbacks (see _on_key_press etc. below)
        # run *inside the OS-level global input hook* and must return in a
        # couple hundred milliseconds or Windows treats the whole app as
        # unresponsive -- so they only ever do a fast, non-blocking
        # `queue.put`. This one dedicated thread then drains the queue in
        # order, which both keeps the hook thread unblocked AND preserves
        # press-then-release ordering (unlike spawning a fresh thread per
        # event, which offers no ordering guarantee between two threads).
        self._event_queue = queue.Queue()
        self._worker_thread = None
        self._worker_stop = threading.Event()
        # Capture mode: while armed, the next key press or mouse click is
        # recorded as the new binding for `_capture_slot_id` instead of
        # driving push-to-talk -- powers the Voice tab's "Change" /
        # "press any key..." rebind flow.
        self._capture_slot_id = None
        self._capture_result_cb = None

    # ── Config surface (called from Api's js_api methods) ────────────────
    def get_hotkeys(self):
        return [dict(s) for s in self._slots]

    def set_hotkey(self, slot_id: str, kind: str, value: str, label: str = "") -> bool:
        with self._lock:
            for s in self._slots:
                if s["id"] == slot_id:
                    s["kind"] = kind
                    s["value"] = value
                    s["label"] = label or value
                    _save(self._slots)
                    return True
        return False

    def reset_defaults(self):
        with self._lock:
            self._slots = [dict(h) for h in DEFAULT_HOTKEYS]
            _save(self._slots)
        return self.get_hotkeys()

    def begin_capture(self, slot_id: str, callback):
        """Arms capture mode for `slot_id`. `callback(kind, value, label)`
        fires once the next key/click resolves the capture."""
        self._capture_slot_id = slot_id
        self._capture_result_cb = callback

    def cancel_capture(self):
        self._capture_slot_id = None
        self._capture_result_cb = None

    # ── Lifecycle ─────────────────────────────────────────────────────
    def start(self) -> str:
        if not _PYNPUT_AVAILABLE:
            return pynput_status()
        if self._kb_listener is not None:
            return "Already running."
        self._worker_stop.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        self._kb_listener = keyboard.Listener(on_press=self._on_key_press, on_release=self._on_key_release)
        self._mouse_listener = mouse.Listener(on_click=self._on_mouse_click)
        self._kb_listener.start()
        self._mouse_listener.start()
        return "Global push-to-talk hotkey listener started."

    def stop(self):
        if self._kb_listener:
            try:
                self._kb_listener.stop()
            except Exception:
                pass
            self._kb_listener = None
        if self._mouse_listener:
            try:
                self._mouse_listener.stop()
            except Exception:
                pass
            self._mouse_listener = None
        self._worker_stop.set()
        self._event_queue.put(None)  # wake the worker so it notices the stop flag
        self._worker_thread = None

    def _worker_loop(self):
        """Drains _event_queue on its own dedicated thread, one event at a
        time and in the order they were queued -- see the comment on
        _event_queue in __init__ for why this exists instead of handling
        events inline on the pynput hook thread."""
        while not self._worker_stop.is_set():
            item = self._event_queue.get()
            if item is None:
                continue
            func, args = item
            try:
                func(*args)
            except Exception:
                pass

    def status(self):
        return {
            "available": _PYNPUT_AVAILABLE,
            "running": self._kb_listener is not None,
            "hotkeys": self.get_hotkeys(),
        }

    # ── Raw input -> normalized token ────────────────────────────────
    @staticmethod
    def _key_token(key):
        try:
            ch = getattr(key, "char", None)
            if ch:
                return ch.lower()
            return key.name  # e.g. 'alt_r', 'alt_l', 'f13', 'space', 'ctrl_l'...
        except Exception:
            return None

    def _matching_slot(self, kind, token_value):
        for s in self._slots:
            if s["kind"] == kind and s["value"] == token_value:
                return s
        return None

    # ── pynput callbacks ──────────────────────────────────────────────
    # CRITICAL: these run *synchronously inside pynput's OS-level global
    # input hook* (a Windows low-level keyboard/mouse hook, or the
    # equivalent on other platforms). That hook callback is required to
    # return almost immediately -- if it blocks for more than a couple
    # hundred milliseconds, Windows treats the whole app as unresponsive
    # (this is what caused Midum to "stop responding" the instant PTT was
    # pressed). Everything downstream of a press/release -- starting the
    # VoiceSession, pushing the PTT-state event to the frontend (which
    # does a thread + up-to-2-second join in Api._push_event), and driving
    # the overlay window -- can take arbitrarily long, so none of it may
    # ever run directly on this thread. Every handler below does the
    # absolute minimum (decode the raw event, check capture mode) and then
    # enqueues the actual work onto _event_queue for _worker_loop to run,
    # letting the hook callback return immediately. queue.Queue.put() is
    # itself fast/non-blocking (unbounded queue, no lock contention worth
    # mentioning), so it's safe to call directly from the hook thread.
    def _on_key_press(self, key):
        token = self._key_token(key)
        if token is None:
            return
        if self._capture_slot_id:
            label = token.replace("_", " ").title()
            self._event_queue.put((self._resolve_capture, ("keyboard", token, label)))
            return
        slot = self._matching_slot("keyboard", token)
        if slot:
            self._event_queue.put((self._press_slot, (slot["id"],)))

    def _on_key_release(self, key):
        token = self._key_token(key)
        if token is None:
            return
        slot = self._matching_slot("keyboard", token)
        if slot:
            self._event_queue.put((self._release_slot, (slot["id"],)))

    def _on_mouse_click(self, x, y, button, pressed):
        token = None
        for name, btn in _MOUSE_BUTTON_MAP.items():
            if btn is not None and btn == button:
                token = name
                break
        if token is None:
            return
        if pressed and self._capture_slot_id:
            label = _MOUSE_LABELS.get(token, token)
            self._event_queue.put((self._resolve_capture, ("mouse", token, label)))
            return
        slot = self._matching_slot("mouse", token)
        if not slot:
            return
        if pressed:
            self._event_queue.put((self._press_slot, (slot["id"],)))
        else:
            self._event_queue.put((self._release_slot, (slot["id"],)))

    def _resolve_capture(self, kind, value, label):
        slot_id, cb = self._capture_slot_id, self._capture_result_cb
        self._capture_slot_id = None
        self._capture_result_cb = None
        if slot_id:
            self.set_hotkey(slot_id, kind, value, label)
        if cb:
            try:
                cb(kind, value, label)
            except Exception:
                pass

    # ── Push-to-talk drive ────────────────────────────────────────────
    def _press_slot(self, slot_id):
        with self._lock:
            was_empty = not self._pressed
            self._pressed.add(slot_id)
        if not was_empty:
            # A different PTT key was already held -- streaming is already
            # active, nothing more to do.
            self._on_state_change(set(self._pressed))
            return
        session = self._get_session()
        if session is None:
            return
        if not session.is_running():
            # This is the "activate the connection" behavior: the very
            # first press, with no active session, connects to Gemini
            # Live. VoiceSession.start() spawns its own background thread
            # and returns immediately -- we don't block waiting for the
            # connection to finish here.
            session.start()
        # VoiceSession._muted is a plain flag the mic loop checks on every
        # chunk, so this is safe to call immediately even if the
        # connection above hasn't finished yet -- once it does, the mic
        # loop starts already unmuted.
        session.set_muted(False)
        self._on_state_change(set(self._pressed))

    def _release_slot(self, slot_id):
        with self._lock:
            self._pressed.discard(slot_id)
            still_held = bool(self._pressed)
        if still_held:
            return
        session = self._get_session()
        if session is not None and session.is_running():
            # Mute only -- deliberately NOT session.stop(). The connection
            # stays open so the next press resumes streaming instantly
            # instead of reconnecting.
            session.set_muted(True)
        self._on_state_change(set())


_manager_instance = None


def get_manager(voice_session_getter, on_state_change=None) -> PushToTalkManager:
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = PushToTalkManager(voice_session_getter, on_state_change)
    return _manager_instance
