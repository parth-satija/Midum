# --- AUTO-SPLITTER: imports added by automated pass, please review ---
import queue
import threading

# --- from main.py, section 1 ---
# ABORT FLAG — Ctrl+Q sets this to stop the current response
# =============================================================================
# A threading.Event that process_chat_turn checks at every loop iteration.
# When set, the turn is abandoned and control returns to the input prompt.
_abort_event = threading.Event()

# =============================================================================

# =============================================================================
# CONTINUOUS ACTION LOOP -- flipped on by start_action_loop, off by
# stop_action_loop (both defined in orchestration.py). While set,
# process_chat_turn keeps calling the model / executing tools across what
# would normally be separate turns -- a plain-text reply no longer ends the
# turn -- until stop_action_loop() clears it or a hard safety ceiling is
# hit. Voice mode uses the exact same flag: start_action_loop/stop_action_loop
# are ordinary tools dispatched through gui/dispatch.py just like any other,
# so the Gemini Live model can flip this shared state too, and its own
# turn-based tool-calling already lets it keep acting without ending the
# conversation -- this flag is mainly what lets the TEXT orchestration loop
# (process_chat_turn) do the same thing.
# =============================================================================
_action_loop_event = threading.Event()
_action_loop_lock = threading.Lock()
_action_loop_state = {"goal": "", "started_steps": 0}

# =============================================================================

# =============================================================================
# USER INTERJECTIONS -- a steering message typed into the prompt box WHILE
# Midum is still "Executing turns..." on a previous message (see gui/app.py's
# Api.send_interjection and the prompt box's sendMessage() in gui/app.py's
# embedded JS). Unlike a normal message, an interjection does NOT start a
# new turn and does NOT touch conversation_history directly from the GUI
# thread -- it's dropped into this queue, and orchestration.process_chat_turn
# drains it and folds it into conversation_history as a synthetic tool-role
# message immediately after the NEXT tool call finishes executing, so it
# reaches the model in-context, mid-task, without waiting for the current
# turn to end. The model receives it exactly like a tool response -- it is
# never an actual tool call, so it never shows up as a tool-call pill.
# =============================================================================
_interjection_queue = queue.Queue()


def queue_interjection(text: str) -> None:
    """Queue a live steering message for the currently-executing turn."""
    text = (text or "").strip()
    if text:
        _interjection_queue.put(text)


def drain_interjections():
    """Pop every interjection queued so far, joined into one block (None if
    none are pending). Called by process_chat_turn right after each tool
    call's result is appended to conversation_history."""
    parts = []
    while True:
        try:
            parts.append(_interjection_queue.get_nowait())
        except queue.Empty:
            break
    return "\n".join(parts) if parts else None

# =============================================================================

