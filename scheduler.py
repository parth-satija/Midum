"""
scheduler.py — in-process scheduler for saved Flows (see flows.py).

Lets the user schedule a saved Flow to run automatically -- but ONLY while
Midum's GUI is actually open. This is deliberately just a plain daemon
thread inside the running app: no Windows Task Scheduler entry, no cron
job, no OS-level service, nothing that persists once the process exits.

Consequences of that design (by choice, not oversight):
  - If the app isn't running, nothing fires. There is no catch-up: a
    schedule that was "due" while the app was closed is simply evaluated
    fresh against wall-clock time the next time a tick happens after the
    app reopens, and rescheduled forward from *that* moment -- it does
    NOT queue up and burst-fire everything it missed.
  - Schedule *configuration* (what to run and when) is persisted to disk
    (storage/flow_schedules.json), so schedules survive an app restart --
    only the actual firing requires the app to be open.

Storage shape -- a flat list of schedule dicts:
{
  "id":            "sch_xxxxxxxxxx",
  "flow_name":      str,                       # must match a name in flows.list_flows()
  "kind":           "once" | "interval" | "daily" | "weekly",
  # kind == "once":     "run_at":         ISO datetime string, must be in the future
  # kind == "interval": "every_minutes":  int >= 1
  # kind == "daily":    "at_time":        "HH:MM" (24h, local time)
  # kind == "weekly":   "at_time":        "HH:MM", "days": [0-6]  (Mon=0 ... Sun=6,
  #                                        matches Python's date.weekday())
  "enabled":        bool,
  "created_at":     ISO datetime string,
  "next_run_at":    ISO datetime string | None,  # authoritative -- the tick loop
                                                   # fires purely off this field
  "last_run_at":    ISO datetime string | None,
  "last_result":    str | None,                   # truncated result/error of the last run
}

`next_run_at` is advanced to the FOLLOWING occurrence the instant a
schedule fires (synchronously, before the flow itself even starts running)
-- not after the flow finishes. That's deliberate: a slow or crashing flow
must never leave a repeating schedule stuck re-firing every tick, and it
must never get stranded with a stale next_run_at if the app closes mid-run.
"""

import datetime
import json
import os
import threading
import uuid

from config import FLOW_SCHEDULES_FILE
from flows import list_flows, run_flow

TICK_SECONDS = 15   # how often the background thread checks for due schedules

_schedules_cache: list | None = None
_scheduler_thread = None
_stop_event = threading.Event()
_run_callback = None   # optional fn(schedule_dict, result_str), set via start_scheduler()

_WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# =============================================================================
# Persistence
# =============================================================================
def _load_schedules() -> list:
    global _schedules_cache
    if _schedules_cache is not None:
        return _schedules_cache
    items = []
    try:
        if os.path.exists(FLOW_SCHEDULES_FILE):
            with open(FLOW_SCHEDULES_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                items = [s for s in loaded if isinstance(s, dict) and s.get("id")]
    except Exception as e:
        print(f"⚠️ [Scheduler] Could not read {FLOW_SCHEDULES_FILE}: {e}")
        items = []
    _schedules_cache = items
    return items


def _save_schedules(items: list):
    global _schedules_cache
    _schedules_cache = items
    try:
        os.makedirs(os.path.dirname(FLOW_SCHEDULES_FILE), exist_ok=True)
        with open(FLOW_SCHEDULES_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
    except Exception as e:
        print(f"⚠️ [Scheduler] Could not save {FLOW_SCHEDULES_FILE}: {e}")


# =============================================================================
# Time helpers
# =============================================================================
def _iso(dt: "datetime.datetime | None") -> "str | None":
    return dt.isoformat(timespec="seconds") if dt else None


def _parse_iso(s) -> "datetime.datetime | None":
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None


def _parse_hhmm(s: str) -> tuple:
    try:
        hh, mm = (s or "09:00").strip().split(":")
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except Exception:
        return 9, 0


def _next_daily(at_time: str, now: datetime.datetime) -> datetime.datetime:
    hh, mm = _parse_hhmm(at_time)
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now:
        candidate += datetime.timedelta(days=1)
    return candidate


def _next_weekly(at_time: str, days: list, now: datetime.datetime) -> "datetime.datetime | None":
    hh, mm = _parse_hhmm(at_time)
    valid_days = sorted({int(d) for d in (days or [])} & set(range(7))) or list(range(7))
    for offset in range(8):
        cand_date = (now + datetime.timedelta(days=offset)).date()
        if cand_date.weekday() in valid_days:
            candidate = datetime.datetime.combine(cand_date, datetime.time(hh, mm))
            if candidate > now:
                return candidate
    return None   # unreachable given the 8-day sweep, but keeps the type honest


def _recompute_next_run(sched: dict, now: "datetime.datetime | None" = None) -> "datetime.datetime | None":
    """The single source of truth for 'when does this schedule next fire,
    counting forward from `now`'. Used both when a schedule is first
    created/edited and every time it fires (to roll it forward to the
    FOLLOWING occurrence)."""
    now = now or datetime.datetime.now()
    kind = sched.get("kind")
    if kind == "once":
        run_at = _parse_iso(sched.get("run_at"))
        return run_at if (run_at and run_at > now) else None
    if kind == "interval":
        try:
            minutes = max(1, int(sched.get("every_minutes") or 1))
        except (TypeError, ValueError):
            minutes = 1
        return now + datetime.timedelta(minutes=minutes)
    if kind == "daily":
        return _next_daily(sched.get("at_time", "09:00"), now)
    if kind == "weekly":
        return _next_weekly(sched.get("at_time", "09:00"), sched.get("days") or [], now)
    return None


def describe_schedule(sched: dict) -> str:
    """Human-readable one-liner for the Scheduler pane, e.g. 'Daily at
    09:00' or 'Weekly (Mon, Wed) at 14:00'."""
    kind = sched.get("kind")
    if kind == "once":
        return f"Once at {sched.get('run_at', '?')}"
    if kind == "interval":
        return f"Every {sched.get('every_minutes', '?')} min"
    if kind == "daily":
        return f"Daily at {sched.get('at_time', '?')}"
    if kind == "weekly":
        days = sorted(int(d) for d in (sched.get("days") or []) if 0 <= int(d) <= 6)
        day_str = ", ".join(_WEEKDAY_ABBR[d] for d in days) if days else "every day"
        return f"Weekly ({day_str}) at {sched.get('at_time', '?')}"
    return kind or "?"


# =============================================================================
# CRUD -- called both from the GUI (gui/app.py's Api) and usable standalone
# =============================================================================
def list_schedules() -> list:
    """Every saved schedule, each with a 'description' field added for
    display. Read-only snapshot -- does not mutate or recompute anything."""
    out = []
    for s in _load_schedules():
        entry = dict(s)
        entry["description"] = describe_schedule(s)
        out.append(entry)
    return out


def get_schedule(schedule_id: str) -> "dict | None":
    return next((dict(s) for s in _load_schedules() if s.get("id") == schedule_id), None)


def create_schedule(flow_name: str, kind: str, run_at: str = None,
                     every_minutes=None, at_time: str = None, days: list = None) -> str:
    """Create a new schedule. Returns the new schedule's id on success, or
    a string starting with 'Error:' on failure."""
    flow_name = (flow_name or "").strip()
    kind = (kind or "").strip().lower()
    if not flow_name:
        return "Error: a flow name is required."
    if flow_name not in list_flows():
        return f"Error: no saved flow named '{flow_name}'."
    if kind not in ("once", "interval", "daily", "weekly"):
        return "Error: kind must be one of 'once', 'interval', 'daily', 'weekly'."

    now = datetime.datetime.now()
    sched = {
        "id": f"sch_{uuid.uuid4().hex[:10]}",
        "flow_name": flow_name,
        "kind": kind,
        "enabled": True,
        "created_at": _iso(now),
        "last_run_at": None,
        "last_result": None,
    }

    if kind == "once":
        run_dt = _parse_iso(run_at)
        if not run_dt:
            return "Error: 'run_at' must be a valid ISO datetime, e.g. '2026-08-01T09:00:00'."
        if run_dt <= now:
            return "Error: 'run_at' must be in the future."
        sched["run_at"] = _iso(run_dt)
    elif kind == "interval":
        try:
            minutes = int(every_minutes)
        except (TypeError, ValueError):
            return "Error: 'every_minutes' must be an integer."
        if minutes < 1:
            return "Error: 'every_minutes' must be at least 1."
        sched["every_minutes"] = minutes
    elif kind == "daily":
        sched["at_time"] = (at_time or "09:00").strip()
    elif kind == "weekly":
        sched["at_time"] = (at_time or "09:00").strip()
        clean_days = sorted({int(d) for d in (days or []) if 0 <= int(d) <= 6})
        sched["days"] = clean_days or list(range(7))

    next_dt = _recompute_next_run(sched, now)
    sched["next_run_at"] = _iso(next_dt)

    items = _load_schedules()
    items.append(sched)
    _save_schedules(items)
    return sched["id"]


def update_schedule(schedule_id: str, patch: dict) -> str:
    """Patch an existing schedule's config (any of flow_name/kind/run_at/
    every_minutes/at_time/days/enabled) and recompute next_run_at from the
    result. Returns a status message; starts with 'Error:' on failure."""
    items = _load_schedules()
    sched = next((s for s in items if s.get("id") == schedule_id), None)
    if not sched:
        return f"Error: no schedule with id '{schedule_id}'."

    patch = patch or {}
    for key in ("flow_name", "kind", "run_at", "every_minutes", "at_time", "days", "enabled"):
        if key in patch:
            sched[key] = patch[key]

    if sched.get("flow_name") not in list_flows():
        return f"Error: no saved flow named '{sched.get('flow_name')}'."
    if sched.get("kind") not in ("once", "interval", "daily", "weekly"):
        return "Error: kind must be one of 'once', 'interval', 'daily', 'weekly'."

    if sched.get("enabled", True):
        next_dt = _recompute_next_run(sched, datetime.datetime.now())
        sched["next_run_at"] = _iso(next_dt)
    else:
        sched["next_run_at"] = None

    _save_schedules(items)
    return f"Schedule '{schedule_id}' updated."


def set_schedule_enabled(schedule_id: str, enabled: bool) -> str:
    """Enable/disable a schedule. Re-enabling recomputes next_run_at fresh
    from now (so flipping a stale 'once' back on will simply fire on the
    next tick rather than silently do nothing)."""
    return update_schedule(schedule_id, {"enabled": bool(enabled)})


def delete_schedule(schedule_id: str) -> str:
    items = _load_schedules()
    remaining = [s for s in items if s.get("id") != schedule_id]
    if len(remaining) == len(items):
        return f"Error: no schedule with id '{schedule_id}'."
    _save_schedules(remaining)
    return f"Schedule '{schedule_id}' deleted."


# =============================================================================
# Background tick loop
# =============================================================================
def _fire_async(sched: dict):
    """Actually run the flow, off the tick thread, and record the result
    once it's done. `sched`'s next_run_at has ALREADY been advanced by the
    caller before this is invoked -- this function only updates
    last_run_at/last_result and notifies the optional GUI callback."""
    def worker():
        flow_name = sched.get("flow_name")
        print(f"⏰ [Scheduler] Running flow '{flow_name}' (schedule {sched.get('id')})")
        try:
            result = run_flow(flow_name)
        except Exception as e:
            result = f"Error: {e}"

        items = _load_schedules()
        for s in items:
            if s.get("id") == sched.get("id"):
                s["last_run_at"] = _iso(datetime.datetime.now())
                s["last_result"] = str(result)[:500]
                break
        _save_schedules(items)

        if _run_callback:
            try:
                _run_callback(sched, result)
            except Exception as e:
                print(f"⚠️ [Scheduler] run_callback failed: {e}")

    threading.Thread(target=worker, daemon=True, name=f"flow-schedule-{sched.get('id', '?')}").start()


def _tick():
    now = datetime.datetime.now()
    items = _load_schedules()
    to_fire = []
    changed = False

    for s in items:
        if not s.get("enabled"):
            continue
        due_at = _parse_iso(s.get("next_run_at"))
        if due_at is None or due_at > now:
            continue

        to_fire.append(dict(s))   # snapshot for the firing worker

        # Advance (or retire) the schedule immediately, synchronously,
        # BEFORE the flow itself runs -- see module docstring for why.
        if s.get("kind") == "once":
            s["enabled"] = False
            s["next_run_at"] = None
        else:
            next_dt = _recompute_next_run(s, now)
            s["next_run_at"] = _iso(next_dt)
        changed = True

    if changed:
        _save_schedules(items)

    for s in to_fire:
        _fire_async(s)


def _loop():
    while not _stop_event.is_set():
        try:
            _tick()
        except Exception as e:
            print(f"⚠️ [Scheduler] tick failed: {e}")
        _stop_event.wait(TICK_SECONDS)


def start_scheduler(on_run=None):
    """Start the background tick thread. Safe to call more than once --
    a second call is a no-op if the thread is already running. `on_run`,
    if given, is called as on_run(schedule_dict, result_str) every time a
    scheduled flow finishes running (used by the GUI to log/notify)."""
    global _scheduler_thread, _run_callback
    if on_run is not None:
        _run_callback = on_run
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_loop, daemon=True, name="flow-scheduler")
    _scheduler_thread.start()
    print(f"⏰ [Scheduler] started (checking every {TICK_SECONDS}s) -- only fires while this app is open.")


def stop_scheduler():
    _stop_event.set()


def is_scheduler_running() -> bool:
    return bool(_scheduler_thread and _scheduler_thread.is_alive())
