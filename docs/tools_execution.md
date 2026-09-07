# Tools & Action Execution Architecture

Midum provides a robust suite of native and third-party tools for system interaction, browser automation, and LLM delegation.

## 1. `tools/native.py`
- **Purpose**: Houses core system operations including file I/O, terminal command execution (`execute_terminal_command`, `execute_python_code`), and GUI automation (UIA clicks, OCR snapshots, key simulation).

## 2. `tools/browser.py`
- **Purpose**: Integrates Chrome DevTools Protocol (CDP) for browser tab management, DOM extraction, and web navigation.

## 3. `tools/mcp.py`
- **Purpose**: Manages Model Context Protocol (MCP) server connections (e.g., filesystem, git, custom servers), discovering and invoking external tools dynamically.

## 4. `tools/flows.py`
- **Purpose**: Handles saved node-graph workflows and user-defined automation sequences.
