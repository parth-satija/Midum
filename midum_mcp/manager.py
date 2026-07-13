# --- AUTO-SPLITTER: imports added by automated pass, please review ---
from config import MCP_SERVERS_FILE
from config import _MCP_SDK_AVAILABLE
import asyncio
import json
import os
import re
import threading

# --- from main.py, section 1 ---
# MCP (MODEL CONTEXT PROTOCOL) SERVER SUPPORT
# =============================================================================
#
# Lets Midum connect to external MCP servers and use their tools WITHOUT
# dumping every registered tool's JSON schema into Midum's context window
# (which is exactly what would happen if each MCP tool were added directly
# to the `tools` list below — with several servers connected that can blow
# past the context budget fast). Instead, Midum gets three uniform tools:
#
#   list_mcp_servers()                       — which servers are connected (no tool detail)
#   show_server_tools(server)                — on demand: tools + schemas for ONE server
#   call_mcp_tool(server, tool_name, args)    — uniform invocation for ANY tool on ANY server
#
# plus two management tools to make connecting a server easy:
#
#   connect_mcp_server(name, transport, ...) — connect now, and remember it for next time
#   disconnect_mcp_server(server, forget)    — disconnect, optionally forgetting it too
#
# CONFIG FILE (auto-loaded and auto-connected at startup):
#   storage/mcp_servers.json — a JSON list of server configs, e.g.:
#   [
#     {"name": "filesystem", "transport": "stdio", "command": "npx",
#      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user"]},
#     {"name": "weather", "transport": "http", "url": "https://example.com/mcp"}
#   ]
#   Editing this file by hand works too — it's just what connect_mcp_server writes to.
#
# INSTALL:
#   pip install mcp
#
_MCP_SERVER_ORDER: list = []     # server names, in the order they were connected (gives indices)
_MCP_SERVERS: dict      = {}     # name -> _MCPServerHandle


class _MCPServerHandle:
    """Everything Midum knows about one connected (or attempted) MCP server."""
    def __init__(self, name: str, config: dict):
        self.name        = name
        self.config      = config
        self.session     = None     # mcp.ClientSession, once connected
        self.tools       = []       # [{"name","description","input_schema"}, ...]
        self.exit_stack  = None     # contextlib.AsyncExitStack keeping the transport open
        self.connected   = False
        self.error       = None


def _load_mcp_config() -> list:
    """Read storage/mcp_servers.json. Returns [] if missing or invalid."""
    try:
        if not os.path.exists(MCP_SERVERS_FILE):
            return []
        with open(MCP_SERVERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"⚠️ [MCP] Could not read {MCP_SERVERS_FILE}: {e}")
        return []


def _save_mcp_config(configs: list):
    """Write the given list of server configs to storage/mcp_servers.json."""
    try:
        os.makedirs(os.path.dirname(MCP_SERVERS_FILE), exist_ok=True)
        with open(MCP_SERVERS_FILE, "w", encoding="utf-8") as f:
            json.dump(configs, f, indent=2)
    except Exception as e:
        print(f"⚠️ [MCP] Could not save {MCP_SERVERS_FILE}: {e}")


def _mcp_upsert_config(new_entry: dict):
    """Add or replace (by name) one server entry in the persisted config file."""
    configs = _load_mcp_config()
    configs = [c for c in configs if c.get("name") != new_entry.get("name")]
    configs.append(new_entry)
    _save_mcp_config(configs)


def _mcp_remove_config(name: str):
    configs = [c for c in _load_mcp_config() if c.get("name") != name]
    _save_mcp_config(configs)


class _MCPManager:
    """
    Owns a single background asyncio event loop that MCP client sessions run
    on for their whole lifetime (they're async context managers that need to
    stay entered between calls), and exposes plain synchronous methods so
    the rest of Midum — which is entirely sync/threaded — never has to
    touch asyncio directly.
    """
    def __init__(self):
        self._loop   = None
        self._thread = None
        self._ready  = threading.Event()

    def _ensure_loop(self):
        if self._loop is not None:
            return
        def _runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._ready.set()
            loop.run_forever()
        self._thread = threading.Thread(target=_runner, daemon=True, name="mcp-loop")
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run(self, coro, timeout: float = 30):
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    async def _connect_async(self, config: dict):
        from contextlib import AsyncExitStack
        from mcp import ClientSession

        transport = (config.get("transport") or "stdio").lower()
        stack = AsyncExitStack()
        try:
            if transport == "stdio":
                from mcp import StdioServerParameters
                from mcp.client.stdio import stdio_client
                env = None
                if config.get("env"):
                    env = {**os.environ, **config["env"]}
                params = StdioServerParameters(
                    command=config["command"],
                    args=config.get("args", []),
                    env=env,
                )
                read, write = await stack.enter_async_context(stdio_client(params))

            elif transport in ("http", "streamable_http", "streamable-http"):
                from mcp.client.streamable_http import streamablehttp_client
                read, write, _ = await stack.enter_async_context(
                    streamablehttp_client(config["url"], headers=config.get("headers"))
                )

            elif transport == "sse":
                from mcp.client.sse import sse_client
                read, write = await stack.enter_async_context(
                    sse_client(config["url"], headers=config.get("headers"))
                )

            else:
                raise ValueError(
                    f"Unknown transport '{transport}'. Use 'stdio', 'http', or 'sse'."
                )

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listed = await session.list_tools()
            tool_list = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": t.inputSchema or {"type": "object", "properties": {}},
                }
                for t in listed.tools
            ]
            return stack, session, tool_list
        except Exception:
            await stack.aclose()
            raise

    def connect(self, name: str, config: dict) -> tuple:
        """Connect (or reconnect) to a server. Returns (ok: bool, message: str)."""
        if not _MCP_SDK_AVAILABLE:
            return False, "The 'mcp' package is not installed. Run: pip install mcp"

        # Reconnecting: tear down any existing connection under this name first.
        existing = _MCP_SERVERS.get(name)
        if existing and existing.exit_stack:
            try:
                self._run(existing.exit_stack.aclose(), timeout=10)
            except Exception:
                pass

        handle = _MCPServerHandle(name, config)
        _MCP_SERVERS[name] = handle
        if name not in _MCP_SERVER_ORDER:
            _MCP_SERVER_ORDER.append(name)

        try:
            stack, session, tool_list = self._run(
                self._connect_async(config), timeout=config.get("connect_timeout", 30)
            )
            handle.exit_stack = stack
            handle.session    = session
            handle.tools      = tool_list
            handle.connected  = True
            handle.error      = None
            return True, f"Connected to '{name}' — {len(tool_list)} tool(s) available."
        except Exception as e:
            handle.connected = False
            handle.error     = str(e)
            return False, f"Failed to connect to '{name}': {e}"

    def disconnect(self, name: str) -> str:
        handle = _MCP_SERVERS.get(name)
        if not handle:
            return f"No connected server named '{name}'."
        if handle.exit_stack:
            try:
                self._run(handle.exit_stack.aclose(), timeout=10)
            except Exception:
                pass
        del _MCP_SERVERS[name]
        if name in _MCP_SERVER_ORDER:
            _MCP_SERVER_ORDER.remove(name)
        return f"Disconnected '{name}'."

    def call_tool(self, name: str, tool_name: str, arguments: dict) -> str:
        handle = _MCP_SERVERS.get(name)
        if not handle:
            return f"Error: no connected server named '{name}'. Call list_mcp_servers() first."
        if not handle.connected:
            return f"Error: server '{name}' is not connected ({handle.error})."
        if not any(t["name"] == tool_name for t in handle.tools):
            known = ", ".join(t["name"] for t in handle.tools) or "(none)"
            return (f"Error: '{tool_name}' is not a tool on server '{name}'. "
                    f"Known tools: {known}. Call show_server_tools() to double-check.")

        async def _call():
            return await handle.session.call_tool(tool_name, arguments or {})

        try:
            result = self._run(_call(), timeout=handle.config.get("call_timeout", 60))
        except Exception as e:
            return f"Error calling '{tool_name}' on '{name}': {e}"

        parts = []
        for block in getattr(result, "content", []) or []:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
            else:
                parts.append(str(block))
        text = "\n".join(parts) if parts else "(no output)"
        if getattr(result, "isError", False):
            return f"[Tool error from '{tool_name}' on '{name}'] {text}"
        return text


_mcp_manager = _MCPManager()


def _mcp_resolve_name(server) -> str:
    """
    Accepts either an index (int/numeric string, matching list_mcp_servers
    order) or a server name directly, and returns the resolved name — or
    None if it doesn't match anything currently connected.
    """
    if server is None:
        return None
    server_str = str(server).strip()
    if server_str.isdigit():
        idx = int(server_str)
        if 0 <= idx < len(_MCP_SERVER_ORDER):
            return _MCP_SERVER_ORDER[idx]
        return None
    return server_str if server_str in _MCP_SERVERS else None


def _mcp_normalize_name(s: str) -> str:
    """Lowercase and strip everything but letters/digits — so 'get_weather',
    'getWeather', 'get-weather', and 'getweather' all compare equal."""
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def init_mcp_servers_from_config():
    """
    Called once at startup: auto-connects every server saved in
    storage/mcp_servers.json. Failures are non-fatal and left visible in
    list_mcp_servers() so Midum (or the user) can see what went wrong.
    """
    configs = _load_mcp_config()
    if not configs:
        return
    if not _MCP_SDK_AVAILABLE:
        print("⚠️ [MCP] mcp_servers.json has entries but the 'mcp' package "
              "isn't installed — run: pip install mcp")
        return
    for cfg in configs:
        name = cfg.get("name")
        if not name:
            continue
        ok, msg = _mcp_manager.connect(name, cfg)
        icon = "🔌" if ok else "⚠️"
        print(f"{icon} [MCP] {msg}")


def list_mcp_servers() -> str:
    """
    Lists connected MCP servers ONLY — not their tools — to keep this cheap
    on context. Use show_server_tools(server) to see what a given server offers.
    """
    if not _MCP_SERVER_ORDER:
        hint = "" if _MCP_SDK_AVAILABLE else " (install the 'mcp' package first: pip install mcp)"
        return f"No MCP servers connected.{hint} Use connect_mcp_server(...) to add one."

    lines = []
    for i, name in enumerate(_MCP_SERVER_ORDER):
        handle = _MCP_SERVERS[name]
        if handle.connected:
            transport = handle.config.get("transport", "stdio")
            lines.append(f"[{i}] {name} — connected ({transport}) — {len(handle.tools)} tool(s)")
        else:
            lines.append(f"[{i}] {name} — connection failed: {handle.error}")
    return "\n".join(lines)


def connect_mcp_server(name: str, transport: str = "stdio", command: str = None,
                        args: list = None, url: str = None, env: dict = None,
                        headers: dict = None, persist: bool = True) -> str:
    """
    Connect to a new MCP server right now. If persist=True (default), it's
    also saved to storage/mcp_servers.json so it auto-connects on every
    future startup — this is the "easy to connect" path: one tool call and
    it's remembered.

    transport='stdio' needs command (+ optional args, env) — for local
    servers launched as a subprocess, e.g. command='npx',
    args=['-y', '@modelcontextprotocol/server-filesystem', '/home/user'].

    transport='http' or 'sse' needs url (+ optional headers) — for remote
    MCP servers reachable over the network.
    """
    if not name or not name.strip():
        return "Error: a server 'name' is required."
    name = name.strip()

    config = {"name": name, "transport": transport}
    if transport == "stdio":
        if not command:
            return "Error: transport='stdio' requires 'command'."
        config["command"] = command
        if args:
            config["args"] = args
        if env:
            config["env"] = env
    elif transport in ("http", "streamable_http", "sse"):
        if not url:
            return f"Error: transport='{transport}' requires 'url'."
        config["url"] = url
        if headers:
            config["headers"] = headers
    else:
        return "Error: transport must be 'stdio', 'http', or 'sse'."

    ok, msg = _mcp_manager.connect(name, config)
    if ok and persist:
        _mcp_upsert_config(config)
        msg += " Saved — will auto-connect on future startups."
    return msg


def disconnect_mcp_server(server, forget: bool = False) -> str:
    """
    Disconnect a currently-connected MCP server. If forget=True, also
    remove it from storage/mcp_servers.json so it stops auto-connecting.
    """
    name = _mcp_resolve_name(server)
    if name is None:
        return f"Unknown server '{server}'. Call list_mcp_servers() first."
    msg = _mcp_manager.disconnect(name)
    if forget:
        _mcp_remove_config(name)
        msg += " Removed from saved config."
    return msg


# =============================================================================

