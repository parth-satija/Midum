# Multi-Agent & Orchestration Architecture

The multi-agent system enables Midum to delegate complex, multi-step operations to independent background worker threads.

## 1. `agents/supervisor.py`
- **Purpose**: Controls the spawning, task queueing, monitoring, and termination of sub-agents.
- **Features**:
  - Handles routing of high-level tasks to specialized agents (e.g., Researcher, Coder).
  - Listens for sub-agent completion reports and integrates findings back into the primary conversation context.

## 2. `agents/worker.py`
- **Purpose**: Defines the isolated action-loop worker threads (`ActionLoopWorker`).
- **Features**:
  - Executes tool calls iteratively without blocking the main UI thread.
  - Supports persistence so agent definitions can be restored after a system restart.

## 3. `agents/memory.py`
- **Purpose**: Manages ephemeral and persistent knowledge attachment (`memory.md`, knowledge bases, and skill files) for agents, ensuring context does not compound indefinitely.
