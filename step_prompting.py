"""
step_prompting.py -- per-chat checklist ("step prompting") for Midum.

The user (or, when the user gave none, the model itself) defines an ordered list
of steps. Midum works through them like a checklist and checks each one off with
the complete_step tool. This module owns ALL of that state and logic so the rest
of the app only needs a few small hooks:

  * gui/app.py      -- begin_turn()/end_turn() around a user-initiated turn,
                       the steps_summary card, send_message() payload tweak,
                       Edit/Regenerate rewind (rewind_last_turn).
  * gui/steps_api.py-- the Api methods the prompt-box UI calls.
  * orchestration.py-- prompt_block() injected into the system message every
                       model step, continuation_nudge() so a plain-text reply
                       doesn't end a turn early, turn_active() to raise the step
                       ceiling, and the write_steps / complete_step dispatch.

Persistence
-----------
  storage/steps/<chat_id>.json   one file per chat  -> a chat's checklist,
                                 progress and settings survive restarts / tab
                                 switches, so a run can be paused (per-turn
                                 limit, API quota) and resumed in a later turn.
  storage/steps/templates.json   saved, reusable step lists (global).

Turn context is thread-local: only the user-initiated turn thread started by
Api._run_turn has one, so sub-agents / voice / delegate runs never see the
checklist and cannot touch it.
"""
import json
import os
import re
import threading
import time
import uuid

try:
    from config import STORAGE_DIR
except Exception:  # pragma: no cover - standalone / test use
    STORAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage")

STEPS_DIR = os.path.join(STORAGE_DIR, "steps")
TEMPLATES_FILE = os.path.join(STEPS_DIR, "templates.json")

MAX_STEPS = 60
MAX_STEP_CHARS = 400
MAX_PROOF_CHARS = 400
MIN_PROOF_CHARS = 12
MAX_NUDGES = 2          # times a plain-text reply is pushed back into working per turn

_lock = threading.RLock()
_tl = threading.local()
_event_hook = None      # fn(kind: str, payload: dict) -- installed by the GUI


# =============================================================================
# Small helpers
# =============================================================================
def set_event_hook(fn):
    """GUI installs fn(kind, payload) so state changes reach the frontend live."""
    global _event_hook
    _event_hook = fn


def _emit(chat_id):
    hook = _event_hook
    if hook is None:
        return
    try:
        st = public_state(chat_id)
        hook("steps_state", st)
    except Exception:
        pass


def _emit_done(chat_id, n, total, text):
    """Tell the GUI a step was just checked off (drives the small in-chat pill)."""
    hook = _event_hook
    if hook is None:
        return
    short = text if len(text) <= 90 else text[:87].rstrip() + "..."
    try:
        hook("step_completed", {"chat_id": chat_id, "n": n, "total": total,
                                "text": f"Step {n} of {total} done \u2014 {short}"})
    except Exception:
        pass


def _safe_id(chat_id):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(chat_id or "")) or "default"


def _run_path(chat_id):
    return os.path.join(STEPS_DIR, _safe_id(chat_id) + ".json")


def _clean_text(text, limit=MAX_STEP_CHARS):
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t[:limit]


def _new_step(text):
    return {"id": uuid.uuid4().hex[:8], "text": _clean_text(text), "done": False, "proof": "", "turn": 0}


def _blank_run(verify=False, per_turn=0):
    return {
        "steps": [],
        "verify": bool(verify),
        "per_turn": _clamp_per_turn(per_turn),
        "source": "user",      # "user" -> user-defined (write_steps blocked); "model" -> self-written
        "started": False,      # becomes True once a message has been sent with the steps pending
        "turns": 0,            # how many turns have worked on this run
        "pre_turn": None,      # snapshot taken at turn start, for Edit/Regenerate rewind
    }


def _clamp_per_turn(v):
    try:
        return max(0, min(int(v or 0), MAX_STEPS))
    except (TypeError, ValueError):
        return 0


def _atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load(chat_id):
    with _lock:
        try:
            with open(_run_path(chat_id), "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return _blank_run()
    run = _blank_run(raw.get("verify"), raw.get("per_turn"))
    run["source"] = "model" if raw.get("source") == "model" else "user"
    run["started"] = bool(raw.get("started"))
    run["turns"] = int(raw.get("turns") or 0)
    run["pre_turn"] = raw.get("pre_turn")
    for s in raw.get("steps") or []:
        if not isinstance(s, dict) or not _clean_text(s.get("text")):
            continue
        run["steps"].append({
            "id": str(s.get("id") or uuid.uuid4().hex[:8]),
            "text": _clean_text(s.get("text")),
            "done": bool(s.get("done")),
            "proof": str(s.get("proof") or "")[:MAX_PROOF_CHARS],
            "turn": int(s.get("turn") or 0),
        })
    return run


def _save(chat_id, run):
    with _lock:
        _atomic_write_json(_run_path(chat_id), run)


# =============================================================================
# Read-only views
# =============================================================================
def public_state(chat_id):
    run = _load(chat_id)
    steps = run["steps"]
    total = len(steps)
    done = sum(1 for s in steps if s["done"])
    if not total:
        phase = "none"
    elif done == total:
        phase = "complete"
    elif run["started"]:
        phase = "running"
    else:
        phase = "staged"
    return {
        "chat_id": chat_id,
        "steps": [{"id": s["id"], "text": s["text"], "done": s["done"],
                   "proof": s["proof"], "turn": s["turn"]} for s in steps],
        "verify": run["verify"],
        "per_turn": run["per_turn"],
        "source": run["source"],
        "done": done,
        "total": total,
        "phase": phase,
        # True while the model must not write its own steps (user-defined list still open)
        "locked": bool(total and done < total and run["source"] == "user"),
    }


def has_pending(chat_id):
    return any(not s["done"] for s in _load(chat_id)["steps"])


# =============================================================================
# UI-side editing (called from gui/steps_api.py; never while a turn is running)
# =============================================================================
def ui_update(chat_id, rows=None, verify=None, per_turn=None):
    """
    rows: the EDITABLE (not-yet-done) rows in display order, each {"id"?, "text"}.
          Completed steps are kept as-is and always stay in front. rows=None
          changes only the options. If every step of the previous run was
          already done and new rows arrive, a fresh run starts from them.
    """
    with _lock:
        run = _load(chat_id)
        if verify is not None:
            run["verify"] = bool(verify)
        if per_turn is not None:
            run["per_turn"] = _clamp_per_turn(per_turn)

        if rows is not None:
            all_done = bool(run["steps"]) and all(s["done"] for s in run["steps"])
            done = [s for s in run["steps"] if s["done"]]
            done_ids = {s["id"] for s in done}
            pend_by_id = {s["id"]: s for s in run["steps"] if not s["done"]}
            new_pending = []
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                rid = str(r.get("id") or "")
                text = _clean_text(r.get("text"))
                if not text or rid in done_ids:
                    continue
                if rid in pend_by_id:
                    s = pend_by_id.pop(rid)
                    s["text"] = text
                    new_pending.append(s)
                else:
                    new_pending.append(_new_step(text))
            if all_done and new_pending:
                done = []
                run["started"] = False
                run["turns"] = 0
            if len(done) + len(new_pending) > MAX_STEPS:
                return {"ok": False, "error": f"Too many steps (max {MAX_STEPS})."}
            run["steps"] = done + new_pending
            if new_pending:
                run["source"] = "user"
            if not run["steps"]:
                run["started"] = False
                run["turns"] = 0

        run["pre_turn"] = None
        _save(chat_id, run)
    _emit(chat_id)
    return {"ok": True, "state": public_state(chat_id)}


def ui_clear(chat_id):
    """Drop the whole checklist (keeps the verify / per-turn options)."""
    with _lock:
        old = _load(chat_id)
        _save(chat_id, _blank_run(old["verify"], old["per_turn"]))
    _emit(chat_id)
    return {"ok": True, "state": public_state(chat_id)}


def ui_reset_progress(chat_id):
    """Uncheck every step so the same list can be run again."""
    with _lock:
        run = _load(chat_id)
        for s in run["steps"]:
            s["done"] = False
            s["proof"] = ""
            s["turn"] = 0
        run["started"] = False
        run["turns"] = 0
        run["pre_turn"] = None
        _save(chat_id, run)
    _emit(chat_id)
    return {"ok": True, "state": public_state(chat_id)}


def ui_apply_template(chat_id, template):
    with _lock:
        run = _blank_run(template.get("verify"), template.get("per_turn"))
        run["steps"] = [_new_step(t) for t in (template.get("steps") or []) if _clean_text(t)][:MAX_STEPS]
        run["source"] = "user"
        _save(chat_id, run)
    _emit(chat_id)
    return {"ok": True, "state": public_state(chat_id)}


# =============================================================================
# Saved templates (global, reusable across chats)
# =============================================================================
def _load_templates():
    with _lock:
        try:
            with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [t for t in data if isinstance(t, dict) and t.get("id")]
        except Exception:
            return []


def list_templates():
    out = []
    for t in sorted(_load_templates(), key=lambda x: str(x.get("name", "")).lower()):
        out.append({"id": t["id"], "name": t.get("name", "Untitled"),
                    "steps": list(t.get("steps") or []), "verify": bool(t.get("verify")),
                    "per_turn": _clamp_per_turn(t.get("per_turn"))})
    return out


def get_template(template_id):
    for t in list_templates():
        if t["id"] == template_id:
            return t
    return None


def save_template(name, steps, verify=False, per_turn=0):
    name = _clean_text(name, 60)
    steps = [_clean_text(s) for s in (steps or []) if _clean_text(s)][:MAX_STEPS]
    if not name:
        return {"ok": False, "error": "Give the template a name."}
    if not steps:
        return {"ok": False, "error": "Add at least one step before saving."}
    with _lock:
        items = _load_templates()
        existing = next((t for t in items if str(t.get("name", "")).lower() == name.lower()), None)
        rec = {"id": existing["id"] if existing else uuid.uuid4().hex[:10], "name": name,
               "steps": steps, "verify": bool(verify), "per_turn": _clamp_per_turn(per_turn),
               "updated": time.time()}
        items = [t for t in items if t["id"] != rec["id"]] + [rec]
        _atomic_write_json(TEMPLATES_FILE, items)
    return {"ok": True, "id": rec["id"], "overwritten": bool(existing)}


def delete_template(template_id):
    with _lock:
        items = _load_templates()
        kept = [t for t in items if t["id"] != template_id]
        if len(kept) == len(items):
            return {"ok": False, "error": "Template not found."}
        _atomic_write_json(TEMPLATES_FILE, kept)
    return {"ok": True}


# =============================================================================
# Turn lifecycle (thread-local; driven by Api._run_turn)
# =============================================================================
def begin_turn(chat_id):
    """Call at the start of a user-initiated turn, on the turn's own thread."""
    with _lock:
        run = _load(chat_id)
        pending = [s for s in run["steps"] if not s["done"]]
        run["pre_turn"] = {
            "steps": [dict(s) for s in run["steps"]],
            "source": run["source"], "started": run["started"], "turns": run["turns"],
        }
        n = 0
        if pending:
            run["started"] = True
            run["turns"] += 1
            n = run["turns"]
        _save(chat_id, run)
    _tl.turn = {
        "chat_id": chat_id,
        "n": n,
        "used": bool(pending),
        "quota": run["per_turn"] or None,   # None -> unlimited
        "completed": [],
        "nudges": 0,
    }
    if pending:
        _emit(chat_id)


def clear_turn():
    _tl.turn = None


def _turn():
    return getattr(_tl, "turn", None)


def turn_active():
    """True while this thread's turn still has unchecked steps (raises the loop's step ceiling)."""
    t = _turn()
    return bool(t and has_pending(t["chat_id"]))


def end_turn():
    """
    Call after the turn finished. Returns the summary-card payload (or None when
    this turn never used step prompting) and clears the thread's turn context.
    """
    t = _turn()
    _tl.turn = None
    if not t or not t.get("used"):
        return None
    run = _load(t["chat_id"])
    if not run["steps"]:
        return None
    total = len(run["steps"])
    done = sum(1 for s in run["steps"] if s["done"])
    this_turn = [s for s in run["steps"] if s["done"] and s["turn"] == t["n"]]
    return {
        "steps": [{"n": i + 1, "text": s["text"], "done": s["done"],
                   "proof": s["proof"] if run["verify"] else "",
                   "this_turn": bool(s["done"] and s["turn"] == t["n"])}
                  for i, s in enumerate(run["steps"])],
        "verify": run["verify"],
        "per_turn": run["per_turn"],
        "source": run["source"],
        "done": done,
        "total": total,
        "completed_this_turn": len(this_turn),
        "remaining": total - done,
    }


def rewind_last_turn(chat_id):
    """Edit Prompt / Regenerate Response: undo the checklist progress made by the last turn."""
    with _lock:
        run = _load(chat_id)
        snap = run.get("pre_turn")
        if not snap:
            return False
        run["steps"] = []
        for s in snap.get("steps") or []:
            run["steps"].append({"id": s["id"], "text": s["text"], "done": bool(s.get("done")),
                                 "proof": s.get("proof", ""), "turn": int(s.get("turn") or 0)})
        run["source"] = snap.get("source", "user")
        run["started"] = bool(snap.get("started"))
        run["turns"] = int(snap.get("turns") or 0)
        run["pre_turn"] = None
        _save(chat_id, run)
    _emit(chat_id)
    return True


# =============================================================================
# What the model sees
# =============================================================================
def prompt_block():
    """System-prompt block describing the live checklist; '' when nothing to do."""
    t = _turn()
    if not t:
        return ""
    run = _load(t["chat_id"])
    pending = [s for s in run["steps"] if not s["done"]]
    if not run["steps"] or not pending:
        return ""

    quota = t["quota"]
    left = None if quota is None else max(0, quota - len(t["completed"]))
    allowed_ids = {s["id"] for s in (pending if left is None else pending[:left])}
    next_id = pending[0]["id"]

    lines = ["", "══ STEP CHECKLIST (active) ══"]
    lines.append("Defined by the USER -- follow it exactly." if run["source"] == "user"
                 else "You wrote this list yourself with write_steps.")
    lines.append("[x] = done, [ ] = to do:")
    for i, s in enumerate(run["steps"], 1):
        if s["done"]:
            lines.append(f"[x] {i}. {s['text']}")
        elif s["id"] not in allowed_ids:
            lines.append(f"[ ] {i}. {s['text']}   (reserved for a LATER turn -- do NOT start it)")
        elif s["id"] == next_id:
            lines.append(f"[ ] {i}. {s['text']}   <-- NEXT")
        else:
            lines.append(f"[ ] {i}. {s['text']}")
    lines.append("RULES:")
    lines.append("- Work through the steps strictly IN ORDER. As soon as a step is genuinely finished, "
                 "call complete_step(step=<number>, proof=...) BEFORE starting the next one.")
    if quota is None:
        lines.append("- No per-turn limit: keep going until every step is checked off.")
    else:
        lines.append(f"- Per-turn limit: at most {quota} step(s) this turn ({left} left right now). "
                     "When the limit is reached the checklist pauses; the user resumes it in a later turn. "
                     "Do not work on steps beyond the limit.")
    if run["verify"]:
        lines.append("- VERIFICATION IS ON: before complete_step you must actually CHECK the result "
                     "(re-read the file, run the command, inspect the output, ...) and pass a short proof "
                     "(1-2 sentences) naming the concrete evidence you saw. Vague proofs like 'done' are rejected.")
    else:
        lines.append("- proof is optional; a few words on what you did is welcome.")
    lines.append("- If a step fails or you are blocked, do NOT check it off: say so in one or two sentences "
                 "and stop. The step stays unchecked and the user decides what to do.")
    if run["source"] == "user":
        lines.append("- write_steps is disabled while the user's checklist is open.")
    lines.append("- Narrate briefly with say() if useful, but do not re-print the checklist: the app shows "
                 "it to the user automatically at the end of the turn. Finish with a short wrap-up (max 3 sentences).")
    return "\n".join(lines)


def continuation_nudge():
    """
    Called when the model replied with plain text and no tool call. Returns a
    user-role nudge string to push it back into working, or None to let the turn end.
    """
    t = _turn()
    if not t:
        return None
    run = _load(t["chat_id"])
    pending = [s for s in run["steps"] if not s["done"]]
    if not pending:
        return None
    if t["quota"] is not None and len(t["completed"]) >= t["quota"]:
        return None
    if t["nudges"] >= MAX_NUDGES:
        return None
    t["nudges"] += 1
    idx = run["steps"].index(pending[0]) + 1
    return (
        f"[SYSTEM -- STEP CHECKLIST]: Unchecked steps remain. Next is step {idx}: \"{pending[0]['text']}\". "
        "Do it now with tool calls, then call complete_step. If you are genuinely blocked, "
        "reply in one sentence saying why and do not check the step off."
    )


# =============================================================================
# Model tools
# =============================================================================
_NO_CTX = ("[STEPS UNAVAILABLE] Step prompting only works inside the main text chat "
           "(not in voice mode or sub-agent runs).")

_GENERIC_PROOFS = {
    "done", "completed", "complete", "finished", "ok", "okay", "success", "successful",
    "verified", "checked", "did it", "step done", "step completed", "all good", "worked",
}


def _parse_steps_arg(steps):
    if isinstance(steps, str):
        raw = [ln for ln in re.split(r"[\r\n]+", steps)]
    elif isinstance(steps, (list, tuple)):
        raw = [str(x) for x in steps]
    else:
        raw = []
    out = []
    for ln in raw:
        ln = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s*", "", ln)   # strip bullets / "1." numbering
        ln = _clean_text(ln)
        if ln:
            out.append(ln)
    return out


def write_steps(steps):
    """Let the model write its own checklist -- refused while the user's own steps are open."""
    t = _turn()
    if not t:
        return _NO_CTX
    chat_id = t["chat_id"]
    with _lock:
        run = _load(chat_id)
        if run["steps"] and any(not s["done"] for s in run["steps"]) and run["source"] == "user":
            return ("[BLOCKED] The user defined the steps for this task explicitly, so you cannot write "
                    "your own. Follow the STEP CHECKLIST in your instructions and use complete_step.")
        parsed = _parse_steps_arg(steps)
        if len(parsed) < 2:
            return "[ERROR] Provide at least 2 concrete steps (a list of short strings)."
        if len(parsed) > MAX_STEPS:
            return f"[ERROR] Too many steps (max {MAX_STEPS}). Merge related ones."
        fresh = _blank_run(run["verify"], run["per_turn"])
        fresh["steps"] = [_new_step(p) for p in parsed]
        fresh["source"] = "model"
        fresh["started"] = True
        fresh["turns"] = 1
        fresh["pre_turn"] = run.get("pre_turn")
        _save(chat_id, fresh)
    t["used"] = True
    t["n"] = 1
    t["completed"] = []
    t["quota"] = fresh["per_turn"] or None
    _emit(chat_id)
    msg = [f"Checklist written ({len(parsed)} steps). Work through them in order and call "
           "complete_step(step, proof) after each one:"]
    msg += [f"{i}. {s}" for i, s in enumerate(parsed, 1)]
    if fresh["per_turn"]:
        msg.append(f"Per-turn limit set by the user: {fresh['per_turn']} step(s) this turn.")
    if fresh["verify"]:
        msg.append("Verification is ON: each complete_step needs a concrete proof.")
    return "\n".join(msg)


def complete_step(step, proof=""):
    """Check off ONE step (the next unchecked one), subject to order, verification and the per-turn limit."""
    t = _turn()
    if not t:
        return _NO_CTX
    chat_id = t["chat_id"]
    try:
        n = int(str(step).strip().rstrip("."))
    except (TypeError, ValueError):
        return "[ERROR] 'step' must be the step NUMBER (1-based), e.g. complete_step(step=2, proof='...')."
    with _lock:
        run = _load(chat_id)
        if not run["steps"]:
            return "[ERROR] There is no active checklist."
        if n < 1 or n > len(run["steps"]):
            return f"[ERROR] No step {n}. The checklist has {len(run['steps'])} steps."
        target = run["steps"][n - 1]
        if target["done"]:
            return f"[NOTE] Step {n} is already checked off. Continue with the next unchecked step."
        pending = [s for s in run["steps"] if not s["done"]]
        if pending[0]["id"] != target["id"]:
            nxt = run["steps"].index(pending[0]) + 1
            return f"[REJECTED] Steps must be completed in order. The next unchecked step is {nxt}."
        quota = t["quota"]
        if quota is not None and len(t["completed"]) >= quota:
            return (f"[REJECTED] Per-turn limit reached ({quota} step(s)). STOP now: reply with a short "
                    "wrap-up and make no more tool calls. The user continues the checklist in a later turn.")
        proof_txt = _clean_text(proof, MAX_PROOF_CHARS)
        if run["verify"]:
            low = proof_txt.lower().strip(" .!")
            problem = None
            if len(proof_txt) < MIN_PROOF_CHARS:
                problem = "too short"
            elif low in _GENERIC_PROOFS:
                problem = "too generic"
            elif low == target["text"].lower().strip(" ."):
                problem = "it just repeats the step"
            if problem:
                return (f"[PROOF REQUIRED] Verification is ON and your proof for step {n} is {problem}. "
                        "Actually check the outcome now (re-read the file, run the command, inspect the output), "
                        "then call complete_step again with a 1-2 sentence proof naming the concrete evidence you saw.")
        target["done"] = True
        target["proof"] = proof_txt
        target["turn"] = t["n"] or run["turns"] or 1
        _save(chat_id, run)
        t["completed"].append(target["id"])
        remaining = [s for s in run["steps"] if not s["done"]]
    _emit(chat_id)
    _emit_done(chat_id, n, len(run["steps"]), target["text"])

    done_n = len(run["steps"]) - len(remaining)
    head = f"Step {n} checked off ({done_n}/{len(run['steps'])})."
    if not remaining:
        return head + " ALL STEPS COMPLETE. Give a short wrap-up (max 3 sentences), no more tool calls."
    if quota is not None and len(t["completed"]) >= quota:
        return (head + f" Per-turn limit reached ({quota}). STOP now: short wrap-up, no more tool calls. "
                f"{len(remaining)} step(s) remain for a later turn.")
    nxt = run["steps"].index(remaining[0]) + 1
    return head + f" Next: step {nxt} -- {remaining[0]['text']}"
