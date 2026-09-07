# Midum Package (midum_pkg) — Complete Architecture & Developer Manual

Welcome to the official, production-grade technical documentation for **Midum (`midum_pkg`)**. This document serves as the master index and comprehensive architectural guide for developers, system architects, and AI agents contributing to or building upon the Midum codebase.

---

## 1. Executive Summary & Core Philosophy

Midum is an advanced, multi-modal, agentic desktop automation and AI orchestration framework. Unlike traditional chat interfaces or isolated API wrappers, Midum operates as an autonomous local operating system copilot capable of:
1. **Executing real-world actions** across Windows desktop shells, applications, browsers, and terminals.
2. **Spawning and supervising persistent background sub-agents** with full tool access and isolated execution loops.
3. **Multi-provider LLM orchestration** supporting Google Gemini (both official API and authenticated web session scraping via `gemini_webapi`), OpenRouter, Groq, and self-hosted OmniRoute gateways.
4. **Interactive sandboxed visualization** via the React/HTML5 Render Surface with a bidirectional asynchronous IPC bridge (`midum.call`).
5. **Model Context Protocol (MCP)** server integration for modular tool expansion.

---

## 2. High-Level System Architecture

```
+-----------------------------------------------------------------------------------+
|                                    USER LAYER                                     |
|              (Desktop UI, Voice/Discord Bot, Terminal, Web Render Surface)        |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
|                                SUPERVISOR CORE                                    |
|      (Main Reasoning Loop, Tool Dispatcher, Memory Manager, Session State)        |
+-----------------------------------------------------------------------------------+
       |                                  |                                  |
       v                                  v                                  v
+---------------+                +----------------+                +----------------+
|  LLM BACKENDS |                |  SUB-AGENTS    |                |  TOOL SUITES   |
| (Gemini/OpenR)|                | (Background    |                | (Native OS,    |
| /Groq/OmniR)  |                |  Workers)      |                |  UIA, CDP, MCP)|
+---------------+                +----------------+                +----------------+
                                          |                                  |
                                          v                                  v
                                 +------------------------------------------------+
                                 |               RENDER SURFACE                   |
                                 |      (Sandboxed HTML/JS UI & IPC Bridge)       |
                                 +------------------------------------------------+
```

---

## 3. Documentation Index

For detailed subsystem specifications, consult the dedicated documentation files located in this directory:

1. **[Core System & Initialization](core_system.md)**: Deep dive into startup sequence, configuration management, memory stores (`master`, `project`, `session`), and tool dispatching.
2. **[Agents & Supervision Framework](agents_supervision.md)**: Specifications for background sub-agent spawning, task queuing (`send_agent_task`), action loops, persistence, and supervisor routing.
3. **[LLM Providers & Gateway Routing](llm_providers.md)**: Comprehensive guide to Google Gemini (API + Web), OpenRouter, Groq, OmniRoute, model fallback strategies, and dynamic switching.
4. **[Tools & Execution Subsystems](tools_execution.md)**: Reference manual for native OS automation, Windows UI Automation (UIA), OCR fallbacks, Chrome DevTools Protocol (CDP), and MCP servers.
5. **[Render Surface & IPC Architecture](render_surface.md)**: Engineering guide for the sandboxed iframe render panel, `midum.call` bidirectional IPC, asset streaming, and persistent storage.

---

## 4. Repository Structure

```
midum_pkg/
│
├── docs/                      # Comprehensive technical documentation
│   ├── README.md              # This master index
│   ├── core_system.md         # Core runtime & memory architecture
│   ├── agents_supervision.md  # Multi-agent worker framework
│   ├── llm_providers.md       # AI model providers & gateways
│   ├── tools_execution.md     # Native tools, UIA, CDP & MCP
│   └── render_surface.md      # Sandboxed HTML/JS panel & IPC
│
├── storage/                   # Persistent runtime state & memory
│   ├── master.md              # Global system instructions & state
│   ├── project.md             # Active project context
│   ├── session.md             # Current session scratchpad
│   ├── response_memory.md     # Multi-step task checklist
│   └── mcp_servers.json       # Configured MCP server registrations
│
└── [Core Python Modules]      # Package implementation source files
```

---
*Generated automatically for midum_pkg production architecture.*
