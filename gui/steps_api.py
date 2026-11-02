# gui/steps_api.py -- Api methods for the prompt-box step-prompting UI.
#
# Mixed into gui/app.py's Api class (class Api(EditorMixin, StepsMixin)). All
# state lives in step_prompting.py (one JSON file per chat), so nothing here
# needs to be wired into the tab / chat-load / persistence code: the active
# chat is simply self._current_chat_id.
import step_prompting


class StepsMixin:
    # Underscore-prefixed -> not exposed to the JS bridge.
    def _steps_event(self, kind, payload):
        """Forwarded from step_prompting; only the chat on screen gets live updates."""
        try:
            if payload.get("chat_id") != self._current_chat_id:
                return
        except Exception:
            return
        if kind == "step_completed":
            # Small in-chat pill (like the interjection bar). Logged so it replays with the chat.
            try:
                with self._persist_lock:
                    self._display_log.append(("stepdone", payload.get("text", "")))
            except Exception:
                pass
            self._push_event("step_done", {"text": payload.get("text", "")})
            return
        self._push_event(kind, payload)

    _STEPS_BUSY_MSG = "Midum is working -- edit the checklist once this turn finishes."

    def steps_get_state(self):
        return {
            "ok": True,
            "state": step_prompting.public_state(self._current_chat_id),
            "templates": step_prompting.list_templates(),
            "busy": bool(self._thinking),
        }

    def steps_update(self, rows=None, verify=None, per_turn=None):
        # Options (verify / steps-per-turn) may change mid-run; the step list itself may not.
        if rows is not None and self._thinking:
            return {"ok": False, "error": self._STEPS_BUSY_MSG}
        return step_prompting.ui_update(self._current_chat_id, rows, verify, per_turn)

    def steps_clear(self):
        if self._thinking:
            return {"ok": False, "error": self._STEPS_BUSY_MSG}
        return step_prompting.ui_clear(self._current_chat_id)

    def steps_reset_progress(self):
        if self._thinking:
            return {"ok": False, "error": self._STEPS_BUSY_MSG}
        return step_prompting.ui_reset_progress(self._current_chat_id)

    def steps_template_save(self, name, rows, verify=False, per_turn=0):
        texts = [(r.get("text") if isinstance(r, dict) else r) for r in (rows or [])]
        res = step_prompting.save_template(name, texts, verify, per_turn)
        if res.get("ok"):
            res["templates"] = step_prompting.list_templates()
        return res

    def steps_template_apply(self, template_id):
        if self._thinking:
            return {"ok": False, "error": self._STEPS_BUSY_MSG}
        tpl = step_prompting.get_template(template_id)
        if not tpl:
            return {"ok": False, "error": "That saved list no longer exists."}
        return step_prompting.ui_apply_template(self._current_chat_id, tpl)

    def steps_template_delete(self, template_id):
        res = step_prompting.delete_template(template_id)
        if res.get("ok"):
            res["templates"] = step_prompting.list_templates()
        return res
