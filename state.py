# --- AUTO-SPLITTER: imports added by automated pass, please review ---
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

