# =============================================================================
# MULTI-AGENT SUPPORT
# =============================================================================
# Adds a lightweight Supervisor / sub-agent system on top of Midum's existing
# single-loop architecture.
#
# ARCHITECTURE
# ------------
#   • The SUPERVISOR is always the currently selected PRIMARY TEXT model
#     (config.MODEL_PROVIDER / config.MODEL_NAME or whichever provider is
#     active) — NEVER the voice model (Gemini Live). It is the only thing
#     that spawns, tasks, and tears down sub-agents.
#   • Each sub-agent is its own persistent worker THREAD with its own
#     conversation history, running its own tool-calling loop through
#     orchestration.process_chat_turn — the exact same engine the primary
#     loop uses, so a sub-agent gets full native/MCP tool parity.
#   • Every sub-agent runs an Action Loop (start_action_loop/stop_action_loop,
#     see orchestration.py) for the duration of each task it's given: it is
#     instructed to call start_action_loop() the moment it receives a task
#     and stop_action_loop() the moment that task is done/blocked.
#   • Sub-agents default to gemini-3.5-flash-lite on the official Gemini API
#     (provider="gemini_api"), reusing the SAME multi-key fallback chain as
#     everything else in providers/gemini_api_backend.py (GEMINI_API_KEY,
#     _2, _3, _4, _5 in the shared secrets file) — no separate key handling
#     needed here, _gemini_api_chat() already rotates keys on 429.
#   • persistence=True agents have their DEFINITION (role/name/description/
#     personality/model/provider) saved to storage/agents.json so they can
#     be resumed later via resume_agent() — conversation history is NOT
#     persisted across restarts, only the agent's identity/config.
#   • VOICE COMMANDS: the voice model (Gemini Live) never manages agents
#     directly. Its tool schema instead exposes
#     delegate_agent_task_to_supervisor(instruction) — this hands the raw
#     instruction to a fresh Supervisor turn (running on Midum's current
#     TEXT provider/model) which has the actual start_agent / send_agent_task
#     / stop_agent / resume_agent / list_agents / get_agent_report tools and
#     carries the request out, returning a short summary for the voice
#     model to speak back.
# =============================================================================

import json
import os
import threading
import time

import config
from config import STORAGE_DIR
from system_prompt import get_system_prompt

AGENTS_FILE = os.path.join(STORAGE_DIR, "agents.json")

# Default sub-agent brain: cheap, fast, and — via _gemini_api_chat's existing
# GEMINI_API_KEY / _2 / _3 / _4 / _5 fallback chain — resilient to any single
# key running out of quota.
DEFAULT_AGENT_MODEL    = "gemini-3.5-flash-lite"
DEFAULT_AGENT_PROVIDER = "gemini_api"

# name -> runtime agent dict (see _new_agent_record)
_agents      = {}
_agents_lock = threading.RLock()

# Thread-local pointer to the agent record currently running on THIS worker
# thread, set once at the top of _worker_loop (each agent owns one dedicated
# thread for its whole lifetime). This is what lets tool functions like
# tell_supervisor_to_inform_user() below figure out which agent is calling
# them without orchestration.py's dispatch loop having to pass agent
# identity through explicitly -- that dispatch is agent-agnostic by design,
# it just executes whatever tool the model asked for.
_current_agent_ctx = threading.local()

# Optional hook the front-end (GUI's Api, or a CLI equivalent) installs to
# be notified the MOMENT a sub-agent's Action Loop stops (task finished,
# crashed, or was cut off) — see set_agent_done_hook(). This is what lets
# the Supervisor learn about a finished agent automatically, without ever
# having to poll get_agent_report itself. Signature: hook(name, role, report).
_agent_done_hook = None


def set_agent_done_hook(fn) -> None:
    """Register a callback fired every time a sub-agent finishes a task
    (successfully or not) and its Action Loop stops. Pass None to remove.
    The front-end uses this to push the report straight into the
    Supervisor's own conversation (and optionally wake it up to act on it
    immediately), so the Supervisor never has to call get_agent_report in
    a polling loop — it's told the moment there's something to know."""
    global _agent_done_hook
    _agent_done_hook = fn


# Optional hook the front-end installs to receive INTERIM "tell the user
# this now" messages from a sub-agent — distinct from _agent_done_hook,
# which only fires once, when the whole task finishes. Signature:
# hook(name, role, message) -> None.
_agent_inform_hook = None


def get_current_agent_name() -> str | None:
    """Return the name of the sub-agent whose worker thread is CURRENTLY
    executing, if the calling code is running on that thread (e.g. deep
    inside process_chat_turn's tool dispatch, or a print() call that ends
    up in the redirected stdout callback on the same thread). Returns None
    when called from the Supervisor's own thread. Lets other modules (the
    GUI's tool-call/log rendering) tell a sub-agent's own activity apart
    from the Supervisor's without agent identity being threaded through
    every call site explicitly."""
    agent = getattr(_current_agent_ctx, "agent", None)
    return agent["name"] if agent else None


def set_agent_inform_hook(fn) -> None:
    """Register a callback fired every time a sub-agent calls
    tell_supervisor_to_inform_user() mid-task. Pass None to remove. The
    front-end should push this into the Supervisor's own conversation (the
    same way a finished report is pushed via the done hook) so the
    Supervisor can decide whether/how to relay it to the user right away —
    e.g. via say() — instead of the user having to wait for the agent's
    whole task to complete."""
    global _agent_inform_hook
    _agent_inform_hook = fn


# ── Persistence (definitions only — not live history) ───────────────────────

def _load_persisted_agents() -> dict:
    if not os.path.exists(AGENTS_FILE):
        return {}
    try:
        with open(AGENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_persisted_agents(data: dict) -> None:
    try:
        os.makedirs(STORAGE_DIR, exist_ok=True)
        with open(AGENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️ [multi_agent] Could not save {AGENTS_FILE}: {e}")


def _persist_agent(agent: dict) -> None:
    data = _load_persisted_agents()
    data[agent["name"]] = {
        "role":        agent["role"],
        "name":        agent["name"],
        "description": agent["description"],
        "personality": agent["personality"],
        "model":       agent["model"],
        "provider":    agent["provider"],
        "created_at":  agent["created_at"],
        # Names of MD Knowledge Bases (see knowledge_base.py's
        # create_domain_knowledge/list_domain_knowledge -- the same files
        # shown in the GUI's Knowledge tab) currently attached to this
        # agent -- see attach_agent_knowledge() below.
        "knowledge_bases": list(agent.get("knowledge_bases") or []),
        # Names of MD Skill files (see knowledge_base.py's
        # create_domain_skill/list_domain_skills -- the same files shown in
        # the GUI's Skills tab) currently attached to this agent -- see
        # attach_agent_skills() below.
        "skills": list(agent.get("skills") or []),
    }
    _save_persisted_agents(data)


def _forget_persisted_agent(name: str) -> None:
    data = _load_persisted_agents()
    if name in data:
        del data[name]
        _save_persisted_agents(data)


# ── Agent record + system prompt ─────────────────────────────────────────────

def _new_agent_record(role, name, description, personality, persistence, model, provider, knowledge_bases=None, skills=None) -> dict:
    return {
        "role":         role.strip(),
        "name":         name.strip(),
        "description":  (description or "").strip(),
        "personality":  (personality or "").strip(),
        "persistence":  bool(persistence),
        "model":        (model or DEFAULT_AGENT_MODEL).strip(),
        "provider":     (provider or DEFAULT_AGENT_PROVIDER).strip(),
        # Names of MD Knowledge Bases attached to this agent -- see
        # attach_agent_knowledge() and _run_agent_task() below. Their full
        # content is re-fetched fresh and injected EVERY task turn (never
        # written into agent['history']), so it never compounds across
        # turns the way ordinary conversation history does.
        "knowledge_bases": [n.strip() for n in (knowledge_bases or []) if n and n.strip()],
        # Names of MD Skill files attached to this agent -- see
        # attach_agent_skills() and _run_agent_task() below. Same
        # fresh-every-turn / never-persisted-into-history treatment as
        # knowledge_bases above, just for the Skills tab's files instead of
        # the Knowledge tab's.
        "skills": [n.strip() for n in (skills or []) if n and n.strip()],
        "created_at":   time.time(),
        "status":       "idle",     # idle | running | stopped
        "history":      [],         # this agent's own conversation_history
        "last_report":  "",
        "task_queue":   [],
        "thread":       None,
        "stop_flag":    threading.Event(),
        "lock":         threading.RLock(),
        # Wall-clock time (time.time()) the CURRENT task started running, or
        # None if idle. Lets get_agent_report tell the Supervisor how long
        # the agent has actually been working instead of it assuming a task
        # completes instantly.
        "task_started_at": None,
        # Wall-clock time of the last get_agent_report() call while this
        # agent was running — backs a short server-side poll cooldown so an
        # impatient Supervisor that ignores the tool description's guidance
        # still can't hammer get_agent_report every turn; see get_agent_report.
        "last_checked_at": None,
        # Chat-style log for the GUI's Agents tab: list of
        # {"role": "user"|"assistant", "content": str, "ts": float}.
        # "user" entries are tasks sent BY the Supervisor, "assistant"
        # entries are the agent's final reports — mirrors the shape of the
        # main chat pane so the same bubble rendering can be reused.
        "transcript":   [],
    }


def _agent_system_prompt(agent: dict) -> str:
    base = get_system_prompt(effective_provider=agent["provider"], effective_model=agent["model"])
    kb_names = agent.get("knowledge_bases") or []
    kb_note = (
        f"ATTACHED KNOWLEDGE BASES: {', '.join(kb_names)} — the full current "
        "content of each is included as its own system message directly "
        "below, fresh every task. Treat it as ground truth alongside your "
        "own knowledge.\n\n"
        if kb_names else ""
    )
    skill_names = agent.get("skills") or []
    skill_note = (
        f"ATTACHED SKILLS: {', '.join(skill_names)} — the full current "
        "content of each is included as its own system message directly "
        "below, fresh every task. Follow the instructions/procedures in "
        "them as part of how you work.\n\n"
        if skill_names else ""
    )
    return base + (
        "\n\n━━━ SUB-AGENT MODE ━━━\n"
        f"You are '{agent['name']}', a sub-agent spawned and managed by Midum's "
        "SUPERVISOR (Midum's primary text model). You report to the Supervisor, "
        "not directly to the end user.\n\n"
        f"ROLE: {agent['role']}\n"
        f"DESCRIPTION: {agent['description'] or '(none given)'}\n"
        f"PERSONALITY / HOW TO ACT: {agent['personality'] or '(none given — act professionally)'}\n\n"
        f"{kb_note}"
        f"{skill_note}"
        "You have full native/MCP tool access, identical to the Supervisor. "
        "For every task you receive:\n"
        "  1. Call start_action_loop(goal=<short task summary>) immediately.\n"
        "  2. Work the task to completion, chaining tool calls (use say() to "
        "narrate progress if useful).\n"
        "  3. Call stop_action_loop(reason=...) the MOMENT it's done, blocked on "
        "something only the Supervisor/user can resolve, or you're told to stop.\n"
        "  4. Give a clear final plain-text report — this is relayed directly "
        "to the Supervisor, so make it complete and self-contained.\n"
        "Stay in character for your role and personality the whole time.\n"
        "If something comes up mid-task that the user should know about RIGHT "
        "AWAY — an important finding, a status update, a question only they "
        "can answer, a heads-up something looks wrong — do NOT wait for your "
        "final report to say it. Call tell_supervisor_to_inform_user(message=...) "
        "to send it to the Supervisor immediately, then keep working."
    )


# ── Worker thread ────────────────────────────────────────────────────────────

# Marker prefix tagging the ephemeral per-turn knowledge-base system
# message so it can be stripped back out of agent['history'] once the task
# finishes -- see _build_agent_kb_context_message() and the strip step at
# the end of _run_agent_task() below. Mirrors the GUI's own
# Api._KB_ONLY_MARKER pattern for the exact same reason: the content must
# be present for this one turn's model calls, but never persist into the
# agent's own history, or it would be re-sent (and keep growing) on every
# subsequent task.
_AGENT_KB_MARKER = "[MIDUM AGENT KNOWLEDGE]"

# Same purpose as _AGENT_KB_MARKER above, but for the ephemeral per-turn
# SKILLS context message -- kept as a separate marker/message so knowledge
# bases and skills are each their own clearly-labelled block and either
# can be stripped/rebuilt independently.
_AGENT_SKILLS_MARKER = "[MIDUM AGENT SKILLS]"


def _build_agent_kb_context_message(agent: dict) -> str | None:
    """Render the FULL current content of every MD Knowledge Base attached
    to this agent (see attach_agent_knowledge()) into one ephemeral system
    message, re-read from disk fresh on every call so edits made from the
    Knowledge tab are picked up immediately. Returns None if the agent has
    no knowledge bases attached."""
    kb_names = agent.get("knowledge_bases") or []
    if not kb_names:
        return None
    from knowledge_base import read_domain_knowledge
    blocks = []
    for name in kb_names:
        content = read_domain_knowledge(name)
        blocks.append(f"### Knowledge Base: {name}\n{content}")
    body = "\n\n---\n\n".join(blocks)
    return (
        f"{_AGENT_KB_MARKER}\n"
        "The following is the full, current content of this agent's attached "
        "MD Knowledge Base(s) (from the Knowledge tab). Use it as ground "
        "truth for this task.\n\n" + body
    )


def _build_agent_skills_context_message(agent: dict) -> str | None:
    """Same as _build_agent_kb_context_message() above, but for this
    agent's attached MD Skill files (see attach_agent_skills()) -- the
    same files shown/created in the GUI's Skills tab
    (knowledge_base.create_domain_skill/list_domain_skills). Re-read from
    disk fresh on every call. Returns None if the agent has no skills
    attached."""
    skill_names = agent.get("skills") or []
    if not skill_names:
        return None
    from knowledge_base import read_domain_skill
    blocks = []
    for name in skill_names:
        content = read_domain_skill(name)
        blocks.append(f"### Skill: {name}\n{content}")
    body = "\n\n---\n\n".join(blocks)
    return (
        f"{_AGENT_SKILLS_MARKER}\n"
        "The following is the full, current content of this agent's attached "
        "MD Skill(s) (from the Skills tab). Follow their instructions/procedures "
        "as part of how you carry out this task.\n\n" + body
    )


def _run_agent_task(agent: dict, task: str) -> None:
    from orchestration import process_chat_turn
    with agent["lock"]:
        agent["status"] = "running"
        agent["task_started_at"] = time.time()
    if not agent["history"]:
        agent["history"] = [{"role": "system", "content": _agent_system_prompt(agent)}]
    agent["history"].append({
        "role": "user",
        "content": (
            f"[TASK FROM SUPERVISOR]\n{task}\n\n"
            "[SYSTEM]: Call start_action_loop now and begin working immediately. "
            "Do not just describe a plan — act."
        ),
    })

    # Insert this task's fresh knowledge-base and skills content right
    # before the new task message so it's the newest context for THIS
    # task -- and strip both back out below before they ever settle into
    # agent['history'], so neither can compound (grow with every past
    # task's copy still sitting in history) across a persistent agent's
    # many tasks the way ordinary conversation turns do.
    kb_context_msg = _build_agent_kb_context_message(agent)
    skills_context_msg = _build_agent_skills_context_message(agent)
    for ctx_msg in (kb_context_msg, skills_context_msg):
        if ctx_msg:
            insert_at = len(agent["history"]) - 1 if (
                agent["history"] and agent["history"][-1].get("role") == "user"
            ) else len(agent["history"])
            agent["history"].insert(insert_at, {"role": "system", "content": ctx_msg})

    # Route this agent's say() narration to its OWN transcript (Agents tab)
    # instead of the main chat -- process_chat_turn's default say() behavior
    # is to print straight into the primary user-facing chat pane, which is
    # correct for the Supervisor/primary loop but wrong here: a sub-agent's
    # in-progress narration belongs with the agent, not mixed into the
    # conversation the user is actually looking at.
    def _agent_say_hook(msg_text: str, _agent=agent) -> None:
        print(f"\U0001f5e3\ufe0f [Agent '{_agent['name']}' says] {msg_text[:200]}")
        _agent["transcript"].append({
            "role": "assistant", "content": msg_text, "ts": time.time()
        })

    try:
        reply, _outputs = process_chat_turn(
            agent["history"],
            user_request=task,
            force_provider=agent["provider"],
            force_model=agent["model"],
            max_steps=60,
            say_hook=_agent_say_hook,
        )
    except Exception as e:
        reply = f"[Agent '{agent['name']}' crashed while working the task: {e}]"

    if kb_context_msg or skills_context_msg:
        # Strip both ephemeral messages back out -- they must never persist
        # into this agent's own history (see the insert comment above),
        # exactly like the GUI's KB Only mode strips its own marker back
        # out of the Supervisor's history after each turn.
        agent["history"] = [
            m for m in agent["history"]
            if not (m.get("role") == "system" and (
                (m.get("content") or "").startswith(_AGENT_KB_MARKER)
                or (m.get("content") or "").startswith(_AGENT_SKILLS_MARKER)
            ))
        ]

    with agent["lock"]:
        agent["last_report"] = reply
        agent["status"] = "idle"
        agent["task_started_at"] = None
        agent["last_checked_at"] = None
        agent["transcript"].append({"role": "assistant", "content": reply, "ts": time.time()})
    print(f"🤖 [Agent '{agent['name']}' report] {reply[:200]}")

    # Fire the notify hook OUTSIDE agent["lock"] so a slow/misbehaving hook
    # can never block this agent's own worker thread from picking up its
    # next queued task. This is what lets the Supervisor find out a task
    # finished automatically, instead of having to poll get_agent_report.
    if _agent_done_hook is not None:
        try:
            _agent_done_hook(agent["name"], agent["role"], reply)
        except Exception as e:
            print(f"⚠️ [multi_agent] agent_done_hook failed: {e}")


def _worker_loop(agent: dict) -> None:
    print(f"🔁 [Agent '{agent['name']}' — Action Loop worker started, role: {agent['role']}]")
    # This thread is this agent's home for its whole life — stamp it once so
    # tell_supervisor_to_inform_user() (called from deep inside
    # process_chat_turn on this same thread) can identify which agent is
    # calling without any extra plumbing through the dispatch loop.
    _current_agent_ctx.agent = agent
    while not agent["stop_flag"].is_set():
        task = None
        with agent["lock"]:
            if agent["task_queue"]:
                task = agent["task_queue"].pop(0)
        if task is None:
            time.sleep(0.5)
            continue
        _run_agent_task(agent, task)
    with agent["lock"]:
        agent["status"] = "stopped"
    print(f"🛑 [Agent '{agent['name']}' — worker stopped]")


# =============================================================================
# TOOLS — exposed to the Supervisor (and, indirectly, the voice model via
# delegate_agent_task_to_supervisor) through tools_schema.py + orchestration.py
# =============================================================================

def start_agent(role: str, name: str, description: str = "", personality: str = "",
                 persistence: bool = False, model: str = DEFAULT_AGENT_MODEL,
                 provider: str = DEFAULT_AGENT_PROVIDER, knowledge_bases: list = None,
                 skills: list = None) -> str:
    """
    Spawn a new sub-agent controlled by the Supervisor. The agent starts an
    idle Action-Loop worker thread immediately and waits for tasks via
    send_agent_task(). If persistence=True its definition is saved to disk
    so it can be brought back later with resume_agent() even after Midum
    restarts (conversation history is not preserved — a resumed agent starts
    fresh, but keeps its role/name/description/personality/model).

    knowledge_bases (optional): names of MD Knowledge Bases -- the same
    files saved from the GUI's Knowledge tab (see knowledge_base.py's
    create_domain_knowledge/list_domain_knowledge) -- to attach to this
    agent. Their full current content is re-read and given to the agent
    fresh on every task it runs (see _build_agent_kb_context_message), never
    written into the agent's own conversation history, so it cannot compound
    turn over turn. Use attach_agent_knowledge() to change this later.

    skills (optional): names of MD Skill files -- the same files saved from
    the GUI's Skills tab (see knowledge_base.py's create_domain_skill/
    list_domain_skills) -- to attach to this agent. Same fresh-every-task,
    never-compounding treatment as knowledge_bases above. Use
    attach_agent_skills() to change this later.
    """
    role = (role or "").strip()
    name = (name or "").strip()
    if not role or not name:
        return "Error: both 'role' and 'name' are required to start an agent."

    with _agents_lock:
        existing = _agents.get(name)
        if existing and existing["thread"] and existing["thread"].is_alive():
            return f"Agent '{name}' is already running (role: {existing['role']})."

        agent = _new_agent_record(role, name, description, personality, persistence, model, provider, knowledge_bases, skills)
        t = threading.Thread(target=_worker_loop, args=(agent,), daemon=True, name=f"agent-{name}")
        agent["thread"] = t
        _agents[name] = agent
        t.start()
        if agent["persistence"]:
            _persist_agent(agent)

    kb_note = f", knowledge: {', '.join(agent['knowledge_bases'])}" if agent["knowledge_bases"] else ""
    skills_note = f", skills: {', '.join(agent['skills'])}" if agent["skills"] else ""
    return (
        f"Agent '{name}' started — role: '{role}', brain: {agent['provider']}/{agent['model']}, "
        f"persistence: {agent['persistence']}{kb_note}{skills_note}. It is running an idle Action Loop worker. "
        f"Give it work with send_agent_task('{name}', <task>)."
    )


def attach_agent_knowledge(name: str, knowledge_bases: list) -> str:
    """
    Attach (replacing any previous selection) one or more MD Knowledge Bases
    to a sub-agent -- the same files saved from the GUI's Knowledge tab (see
    knowledge_base.py's create_domain_knowledge/list_domain_knowledge).
    Their full current content is given to the agent fresh on every task it
    runs from now on, never written into the agent's own conversation
    history, so it cannot compound turn over turn. Pass an empty list to
    detach all knowledge bases from this agent. Works on both a currently
    running agent and a dormant persisted one (updates the saved definition
    either way).
    """
    name = (name or "").strip()
    clean = [n.strip() for n in (knowledge_bases or []) if n and n.strip()]

    with _agents_lock:
        agent = _agents.get(name)
        if agent:
            agent["knowledge_bases"] = clean
            if agent["persistence"]:
                _persist_agent(agent)

    if not agent:
        # Not currently running -- try updating a dormant persisted definition.
        data = _load_persisted_agents()
        if name not in data:
            return f"No agent named '{name}' is currently loaded (running or dormant)."
        data[name]["knowledge_bases"] = clean
        _save_persisted_agents(data)
        return (
            f"Updated persisted (dormant) agent '{name}'s knowledge bases to: "
            f"{', '.join(clean) if clean else '(none)'}. Takes effect next time it's resumed."
        )

    return (
        f"Agent '{name}' knowledge bases set to: {', '.join(clean) if clean else '(none)'}. "
        "Takes effect on its next task."
    )


def list_agent_knowledge(name: str) -> str:
    """List the MD Knowledge Base(s) currently attached to a sub-agent (running or dormant)."""
    name = (name or "").strip()
    with _agents_lock:
        agent = _agents.get(name)
        if agent:
            kbs = agent.get("knowledge_bases") or []
            return f"Agent '{name}' has {len(kbs)} knowledge base(s) attached: {', '.join(kbs) if kbs else '(none)'}"
    data = _load_persisted_agents()
    if name in data:
        kbs = data[name].get("knowledge_bases") or []
        return f"Agent '{name}' (dormant) has {len(kbs)} knowledge base(s) attached: {', '.join(kbs) if kbs else '(none)'}"
    return f"No agent named '{name}' is currently loaded (running or dormant)."


def attach_agent_skills(name: str, skills: list) -> str:
    """
    Attach (replacing any previous selection) one or more MD Skill files to
    a sub-agent -- the same files saved from the GUI's Skills tab (see
    knowledge_base.py's create_domain_skill/list_domain_skills). Their full
    current content is given to the agent fresh on every task it runs from
    now on, never written into the agent's own conversation history, so it
    cannot compound turn over turn. Pass an empty list to detach all skills
    from this agent. Works on both a currently running agent and a dormant
    persisted one (updates the saved definition either way).
    """
    name = (name or "").strip()
    clean = [n.strip() for n in (skills or []) if n and n.strip()]

    with _agents_lock:
        agent = _agents.get(name)
        if agent:
            agent["skills"] = clean
            if agent["persistence"]:
                _persist_agent(agent)

    if not agent:
        # Not currently running -- try updating a dormant persisted definition.
        data = _load_persisted_agents()
        if name not in data:
            return f"No agent named '{name}' is currently loaded (running or dormant)."
        data[name]["skills"] = clean
        _save_persisted_agents(data)
        return (
            f"Updated persisted (dormant) agent '{name}'s skills to: "
            f"{', '.join(clean) if clean else '(none)'}. Takes effect next time it's resumed."
        )

    return (
        f"Agent '{name}' skills set to: {', '.join(clean) if clean else '(none)'}. "
        "Takes effect on its next task."
    )


def list_agent_skills(name: str) -> str:
    """List the MD Skill(s) currently attached to a sub-agent (running or dormant)."""
    name = (name or "").strip()
    with _agents_lock:
        agent = _agents.get(name)
        if agent:
            sk = agent.get("skills") or []
            return f"Agent '{name}' has {len(sk)} skill(s) attached: {', '.join(sk) if sk else '(none)'}"
    data = _load_persisted_agents()
    if name in data:
        sk = data[name].get("skills") or []
        return f"Agent '{name}' (dormant) has {len(sk)} skill(s) attached: {', '.join(sk) if sk else '(none)'}"
    return f"No agent named '{name}' is currently loaded (running or dormant)."


def send_agent_task(name: str, task: str) -> str:
    """
    Queue a task for a running sub-agent. The agent works it inside its own
    Action Loop on its OWN thread, in the background — this call returns
    immediately, BEFORE the agent has done any work at all. It is NOT a
    synchronous/blocking call and the task is NOT done when this returns.

    Real sub-agent tasks take real time — anywhere from ~20 seconds to
    several minutes depending on complexity, since the agent is running its
    own multi-step tool-calling loop, not returning a single instant reply.

    You will be told AUTOMATICALLY the moment this agent's Action Loop stops
    and its report is ready — it's injected straight into your own context,
    so you do NOT need to call get_agent_report to find out when it's done.
    Only use get_agent_report if you specifically need to re-check an old
    report or the agent's status for some other reason.
    """
    name = (name or "").strip()
    task = (task or "").strip()
    if not task:
        return "Error: 'task' is required."
    with _agents_lock:
        agent = _agents.get(name)
        if not agent or not agent["thread"] or not agent["thread"].is_alive():
            return (
                f"No running agent named '{name}'. Call start_agent first, "
                f"or resume_agent('{name}') if it was persisted."
            )
        agent["task_queue"].append(task)
        agent["transcript"].append({"role": "user", "content": task, "ts": time.time()})
    return (
        f"Task queued for agent '{name}' ({len(agent['task_queue'])} pending). "
        "It is now working in the background on its OWN thread and Action Loop — "
        "this is NOT instant, expect roughly 20 seconds to several minutes depending "
        "on the task. You will be notified automatically in your own context the "
        "moment it finishes — you do NOT need to call get_agent_report to check."
    )


def get_agent_report(name: str) -> str:
    """
    Check a sub-agent's current status and its most recent final report.
    You normally do NOT need to call this: the moment an agent finishes,
    its report is pushed automatically into your own context (see
    send_agent_task). Only use this to re-check an old report, check
    status for some other reason, or if you suspect a notification was
    missed.
    """
    name = (name or "").strip()
    with _agents_lock:
        agent = _agents.get(name)
        if not agent:
            return f"No agent named '{name}' is currently loaded (running or dormant)."
        status  = agent["status"]
        queued  = len(agent["task_queue"])
        started = agent.get("task_started_at")
        report  = agent["last_report"]

        if status == "running":
            now = time.time()
            last_checked = agent.get("last_checked_at")
            # Server-side cooldown: refuse to re-report "still running" more
            # than once every 15s so an impatient Supervisor that ignores the
            # tool description can't burn turns polling every step. This is a
            # backstop, not the primary fix — the tool/return-message wording
            # above is what should stop it from checking in the first place.
            if last_checked is not None and (now - last_checked) < 15:
                wait_more = 15 - (now - last_checked)
                return (
                    f"[Agent '{name}' — checked too recently, {wait_more:.0f}s left on cooldown]\n"
                    "You just checked this agent. It has not had time to change state. "
                    "Do something else or call wait(seconds=20) before checking again — "
                    "do not call get_agent_report again immediately."
                )
            agent["last_checked_at"] = now

    if status == "running":
        elapsed = f"{time.time() - started:.0f}s" if started else "unknown time"
        return (
            f"[Agent '{name}' — STILL WORKING, elapsed: {elapsed}, queued after this: {queued}]\n"
            "It has not finished yet — this is normal, sub-agent tasks are not instant. "
            "Do NOT treat this as a failure or an empty result. Do NOT call get_agent_report "
            "again immediately — continue with other work, or call wait(seconds=20-30), then "
            "check back. Only escalate/investigate if it's been running for several minutes "
            "with no change."
        )
    if status == "idle" and queued == 0 and not report:
        return f"[Agent '{name}' — idle, no task run yet]\nThis agent hasn't been given a task. Use send_agent_task first."
    if status == "idle" and queued > 0:
        return (
            f"[Agent '{name}' — idle but {queued} task(s) still queued, about to start]\n"
            f"Most recent finished report:\n{report or '(none yet)'}"
        )
    return f"[Agent '{name}' — status: {status}, queued tasks: {queued}]\n{report or '(no completed task yet)'}"


def stop_agent(name: str) -> str:
    """
    Stop a running sub-agent's Action Loop worker. If it's mid-task, it
    finishes that task first, then shuts its thread down. Its persisted
    definition (if persistence=True) is kept on disk unless forgotten — call
    resume_agent(name) later to bring it back.
    """
    name = (name or "").strip()
    with _agents_lock:
        agent = _agents.get(name)
        if not agent or not agent["thread"] or not agent["thread"].is_alive():
            return f"No running agent named '{name}'."
        agent["stop_flag"].set()
    return f"Agent '{name}' stop requested — it will finish any in-progress task, then shut down."


def resume_agent(name: str) -> str:
    """Reactivate a persisted (persistence=True) agent that isn't currently running."""
    name = (name or "").strip()
    rec = _load_persisted_agents().get(name)
    if not rec:
        return f"No persisted agent named '{name}' found."
    return start_agent(
        rec.get("role", ""), rec.get("name", name),
        rec.get("description", ""), rec.get("personality", ""),
        True, rec.get("model", DEFAULT_AGENT_MODEL), rec.get("provider", DEFAULT_AGENT_PROVIDER),
        rec.get("knowledge_bases", []), rec.get("skills", []),
    )


def list_agents() -> str:
    """List every currently running sub-agent, plus persisted-but-dormant ones."""
    lines = []
    with _agents_lock:
        for n, a in _agents.items():
            alive = bool(a["thread"] and a["thread"].is_alive())
            kb_note = f" | knowledge={','.join(a['knowledge_bases'])}" if a.get("knowledge_bases") else ""
            skills_note = f" | skills={','.join(a['skills'])}" if a.get("skills") else ""
            lines.append(
                f"- {n} | role={a['role']} | status={a['status']} | alive={alive} | "
                f"brain={a['provider']}/{a['model']} | persistence={a['persistence']} | "
                f"queued={len(a['task_queue'])}{kb_note}{skills_note}"
            )
    persisted = _load_persisted_agents()
    dormant = [n for n in persisted if n not in _agents or not (_agents[n]["thread"] and _agents[n]["thread"].is_alive())]
    if dormant:
        lines.append("Persisted but not currently running (resume_agent to bring back):")
        for n in dormant:
            p = persisted[n]
            lines.append(f"  - {n} | role={p.get('role','')} | brain={p.get('provider','')}/{p.get('model','')}")
    return "\n".join(lines) if lines else "No agents running or persisted."


def get_agent_transcript(name: str) -> list:
    """GUI helper: the agent's chat-style task/report log, for the Agents
    tab's monitor view (mirrors the shape of the main chat pane's bubbles).
    Returns [] if the agent isn't currently loaded (e.g. a dormant
    persisted agent that hasn't been resumed)."""
    with _agents_lock:
        agent = _agents.get((name or "").strip())
        if not agent:
            return []
        return list(agent["transcript"])


def list_agents_struct() -> dict:
    """GUI helper (structured counterpart to list_agents()'s text summary)
    for the Agents tab: {"running": [...], "dormant": [...]}, each entry a
    plain JSON-serializable dict."""
    running = []
    with _agents_lock:
        for n, a in _agents.items():
            alive = bool(a["thread"] and a["thread"].is_alive())
            if not alive:
                continue
            running.append({
                "name": n,
                "role": a["role"],
                "description": a["description"],
                "personality": a["personality"],
                "status": a["status"],
                "model": a["model"],
                "provider": a["provider"],
                "persistence": a["persistence"],
                "queued": len(a["task_queue"]),
                "created_at": a["created_at"],
                "knowledge_bases": list(a.get("knowledge_bases") or []),
                "skills": list(a.get("skills") or []),
            })
        running_names = {n for n, a in _agents.items() if a["thread"] and a["thread"].is_alive()}
    persisted = _load_persisted_agents()
    dormant = [
        {
            "name": n,
            "role": p.get("role", ""),
            "description": p.get("description", ""),
            "personality": p.get("personality", ""),
            "model": p.get("model", DEFAULT_AGENT_MODEL),
            "provider": p.get("provider", DEFAULT_AGENT_PROVIDER),
            "created_at": p.get("created_at", 0),
            "knowledge_bases": list(p.get("knowledge_bases") or []),
            "skills": list(p.get("skills") or []),
        }
        for n, p in persisted.items() if n not in running_names
    ]
    return {"running": running, "dormant": dormant}


def tell_supervisor_to_inform_user(message: str) -> str:
    """
    Sub-agent tool: send the Supervisor a message RIGHT NOW so it can inform
    the user of something mid-task — a status update, an important finding,
    a question only the user can answer, a heads-up — WITHOUT waiting for
    the whole task to finish. Your final report (returned when you call
    stop_action_loop and the task ends) only reaches the Supervisor once,
    at the very end; this delivers a message the same way, immediately,
    as many times as you need during a long task.

    Only meaningful when called from inside a running sub-agent's own task
    — the Supervisor has no supervisor above it to tell.
    """
    agent = getattr(_current_agent_ctx, "agent", None)
    message = (message or "").strip()
    if agent is None:
        return (
            "Error: tell_supervisor_to_inform_user can only be called by a "
            "running sub-agent from inside its own task — there is no "
            "Supervisor above the Supervisor itself."
        )
    if not message:
        return "Error: 'message' is required."

    agent["transcript"].append({
        "role": "assistant", "content": f"[to Supervisor] {message}", "ts": time.time()
    })
    print(f"\U0001f4e2 [Agent '{agent['name']}' \u2192 Supervisor] {message[:200]}")

    if _agent_inform_hook is not None:
        try:
            _agent_inform_hook(agent["name"], agent["role"], message)
        except Exception as e:
            print(f"⚠️ [multi_agent] agent_inform_hook failed: {e}")
        return f"Message relayed to the Supervisor: \"{message}\". Continue with your task."

    return (
        f"Message noted for the Supervisor: \"{message}\" (no front-end inform "
        "hook is installed right now, so this was only logged, not actively "
        "pushed to the Supervisor). Continue with your task."
    )


def forget_agent(name: str) -> str:
    """Stop (if running) and permanently delete a persisted agent's saved definition."""
    name = (name or "").strip()
    stop_msg = ""
    with _agents_lock:
        agent = _agents.pop(name, None)
        if agent and agent["thread"] and agent["thread"].is_alive():
            agent["stop_flag"].set()
            stop_msg = " (was running — stop requested)"
    _forget_persisted_agent(name)
    return f"Agent '{name}' forgotten and removed from persisted storage.{stop_msg}"


def delegate_agent_task_to_supervisor(instruction: str) -> str:
    """
    Voice-mode entry point. Call this when the user gives a voice command
    about sub-agents (spawn one, give one a task, check on one, stop one,
    list them, etc). This hands the raw instruction to the SUPERVISOR —
    Midum's current primary TEXT model/provider, NEVER the voice model
    itself — which has the actual agent-management tools (start_agent,
    send_agent_task, stop_agent, resume_agent, list_agents, get_agent_report,
    forget_agent) and carries the request out, then returns a short summary
    for the voice model to speak back to the user.
    """
    from orchestration import process_chat_turn

    instruction = (instruction or "").strip()
    if not instruction:
        return "Error: empty instruction."

    sup_provider = config.MODEL_PROVIDER
    sup_model    = getattr(config, "MODEL_NAME", None)
    sys_prompt   = get_system_prompt(effective_provider=sup_provider, effective_model=sup_model)
    sys_prompt += (
        "\n\n━━━ SUPERVISOR MODE (voice-delegated) ━━━\n"
        "The voice model just relayed an agent-management instruction from the "
        "user. You are the Supervisor of Midum's multi-agent system — use "
        "start_agent, send_agent_task, stop_agent, resume_agent, list_agents, "
        "get_agent_report, and forget_agent to carry this out right now. When "
        "done, reply with a SHORT plain-text summary — it will be spoken back "
        "to the user by the voice model, so keep it conversational and brief."
    )
    history = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": instruction},
    ]
    try:
        summary, _outputs = process_chat_turn(history, user_request=instruction, max_steps=25)
    except Exception as e:
        summary = f"The Supervisor couldn't complete that agent request: {e}"
    return f"[Supervisor]\n{summary}"

# =============================================================================
