# Sub-Agents & Supervision Framework (`agents_supervision.md`)

This document provides a comprehensive technical blueprint of the **Midum Sub-Agent and Supervision Architecture**, detailing background worker threads, action-loop execution isolation, task queuing, Inter-Process Communication (IPC), and persistence mechanisms.

---

## 1. Architectural Overview of Sub-Agents

Midum supports the creation of autonomous, specialized sub-agents managed by a central **Supervisor**. Sub-agents are designed to handle asynchronous, long-running, or complex multi-step tasks in the background without blocking the primary user-facing conversation thread.

```
+-----------------------------------------------------------------+
|                       SUPERVISOR (Primary)                      |
|   - Manages global state & routing                              |
|   - Spawns / Stops sub-agents (`start_agent`, `stop_agent`)     |
|   - Queues asynchronous tasks (`send_agent_task`)               |
+-----------------------------------------------------------------+
         │                                         ▲
         │ (Queue Task)                            │ (Push Final Report /
         ▼                                         │  Mid-Task Update)
+-----------------------------------------------------------------+
|                    BACKGROUND WORKER THREAD                     |
|   - Isolated Action Loop execution                              |
|   - Full tool access (Terminal, Files, UI, MCP)                 |
|   - Fresh injection of attached Knowledge Bases & Skills        |
+-----------------------------------------------------------------+
```

---

## 2. Agent Lifecycle & Persistence

### Spawning (`start_agent`)
An agent is initialized with a distinct role, name, description, personality, and LLM provider configuration. 
* **Persistence (`persistence=true`)**: Saves the agent's definition (role, name, personality, model, provider) to disk under storage registries so it can be reloaded across application restarts using `resume_agent`.
* **Ephemeral Mode (`persistence=false`)**: Operates solely in memory during the active session.

### State Transitions
1. **Idle**: Waiting in the worker pool for queued tasks.
2. **Running**: Executing an active action loop on its dedicated background thread.
3. **Stopped**: Terminated via `stop_agent`, finishing its current step before shutting down its thread.
4. **Forgotten**: Permanently deleted from disk via `forget_agent`.

---

## 3. Task Execution & Action Loop Isolation

When a task is queued via `send_agent_task(name, task)`:
1. **Asynchronous Hand-off**: The call returns immediately while the agent picks up the task on its own background thread.
2. **Context Freshness**: On every task execution, attached **Knowledge Bases** (`attach_agent_knowledge`) and **Skill Files** (`attach_agent_skills`) are re-read fresh from disk. They are **never** stored permanently in the agent's conversation history, preventing context bloat and staleness over long operational lifetimes.
3. **Action Loop Iteration**: The sub-agent executes its own multi-step tool-calling loop independently, utilizing all native tools, MCP integrations, and terminal access.

---

## 4. Communication & Mid-Task Reporting

Sub-agents communicate progress and results back to the Supervisor through two primary mechanisms:
1. **Mid-Task Updates (`tell_supervisor_to_inform_user`)**: Sub-agents can push interim status updates, important findings, or blockers directly to the Supervisor so the user is kept informed in real-time without waiting for task completion.
2. **Final Report (`get_agent_report`)**: Upon completing its action loop, the sub-agent compiles a structured final report. This report is automatically pushed into the Supervisor's context window.

---

## 5. Voice Mode Integration (`delegate_agent_task_to_supervisor`)

When operating in voice interaction mode, users can issue natural language commands regarding sub-agents (e.g., *"Spawn a researcher agent to scan the web..."*). The voice model routes these instructions via `delegate_agent_task_to_supervisor` to the text-based Supervisor, which executes the appropriate agent management tools and returns a synthesized spoken summary.

---
*End of Agents & Supervision Specification.*
