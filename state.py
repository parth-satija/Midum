# --- AUTO-SPLITTER: imports added by automated pass, please review ---
import threading

# --- from main.py, section 1 ---
# ABORT FLAG — Ctrl+Q sets this to stop the current response
# =============================================================================
# A threading.Event that process_chat_turn checks at every loop iteration.
# When set, the turn is abandoned and control returns to the input prompt.
_abort_event = threading.Event()

# =============================================================================

