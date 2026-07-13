# --- AUTO-SPLITTER: imports added by automated pass, please review ---
from midum_mcp.manager import _MCPManager, _mcp_normalize_name, _mcp_resolve_name
from tools_schema import tools
import json

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


_mcp_manager = _MCPManager()


def _mcp_find_tool_matches(name: str) -> list:
    """
    Search every connected MCP server's tool list for a tool matching `name`
    once separators/case are normalized away. Weak local models frequently
    call an MCP tool directly by its own name (skipping call_mcp_tool) and
    mangle underscores/hyphens/case while doing it — this is what lets that
    still resolve. Returns a list of (server_name, real_tool_name) — usually
    0 or 1 matches, but can be 2+ if the same tool name exists on multiple
    connected servers (caller should treat that as ambiguous, not pick one).
    """
    target = _mcp_normalize_name(name)
    if not target:
        return []
    matches = []
    for server_name in _MCP_SERVER_ORDER:
        handle = _MCP_SERVERS.get(server_name)
        if not handle or not handle.connected:
            continue
        for t in handle.tools:
            if _mcp_normalize_name(t["name"]) == target:
                matches.append((server_name, t["name"]))
    return matches


def _mcp_autoroute_tool_call(name: str, args):
    """
    If `name` isn't a registered top-level tool but matches exactly one
    tool on exactly one connected MCP server, transparently rewrite the
    call into the equivalent call_mcp_tool(...) invocation. This is what
    keeps tool-calling uniform even when a weak model shortcuts straight
    to calling the MCP tool by its own (possibly underscore-mangled) name
    instead of going through call_mcp_tool as instructed.

    Returns (name, args) — unchanged if `name` is already a known
    top-level tool, or if there's no unambiguous MCP match.
    """
    from orchestration import _known_tool_names
    if name in _known_tool_names():
        return name, args
    matches = _mcp_find_tool_matches(name)
    if len(matches) == 1:
        server_name, real_tool_name = matches[0]
        print(f"   [MCP autoroute] '{name}' → call_mcp_tool("
              f"server='{server_name}', tool_name='{real_tool_name}')")
        return "call_mcp_tool", {
            "server": server_name,
            "tool_name": real_tool_name,
            "arguments": args or {}
        }
    return name, args


def show_server_tools(server) -> str:
    """
    Returns the tool names, descriptions, and JSON input schemas for ONE
    server, identified by index (from list_mcp_servers) or name. This is
    the only place full tool schemas are shown to Midum — deliberately
    on-demand, per server, instead of always-loaded.
    """
    name = _mcp_resolve_name(server)
    if name is None:
        return (f"Unknown server '{server}'. Call list_mcp_servers() first "
                f"to see valid indices/names.")
    handle = _MCP_SERVERS[name]
    if not handle.connected:
        return f"Server '{name}' is not connected ({handle.error})."
    if not handle.tools:
        return f"Server '{name}' is connected but exposes no tools."
    return json.dumps(
        {"server": name, "tools": handle.tools},
        indent=2
    )


def call_mcp_tool(server, tool_name: str, arguments) -> str:
    """
    Uniform invocation for ANY tool on ANY connected MCP server — this is
    the ONLY tool actually needed to use MCP servers, once you've checked
    show_server_tools() for the right tool_name/arguments shape.
    """
    name = _mcp_resolve_name(server)
    if name is None:
        return (f"Unknown server '{server}'. Call list_mcp_servers() first "
                f"to see valid indices/names.")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except Exception:
            return f"Error: 'arguments' was not valid JSON: {arguments!r}"
    return _mcp_manager.call_tool(name, tool_name, arguments or {})


def list_native_tools() -> str:
    """
    Lists every built-in Midum tool by name + one-line description ONLY —
    no parameter schemas — to keep this cheap on context. Use
    show_native_tool_schema(tool_name) to get the full JSON parameter
    schema for a specific tool before calling it.

    This exists so the full native tool catalogue never has to be inlined
    into a persistent system prompt/Gem: providers that support real native
    function-calling (Ollama/OpenRouter/Gemini-API/Groq) already get the
    full `tools` schema for free via the API's tools= parameter, so this
    on-demand pair (list_native_tools / show_native_tool_schema) is mainly
    for Gemini-web, which has no native tool-calling protocol and would
    otherwise need everything inlined into one big Gem prompt.
    """
    lines = []
    for i, t in enumerate(tools):
        fn = t.get("function", {})
        name = fn.get("name", "?")
        desc = (fn.get("description") or "").strip().splitlines()[0] if fn.get("description") else ""
        lines.append(f"[{i}] {name} — {desc}")
    return "\n".join(lines)


def show_native_tool_schema(tool_name: str) -> str:
    """
    Returns the full {name, description, parameters} JSON schema for ONE
    native Midum tool, identified by index (from list_native_tools) or
    exact name. Call this right before using a tool you haven't already
    seen the schema for this session.
    """
    match = None
    if isinstance(tool_name, str) and tool_name.strip().isdigit():
        idx = int(tool_name.strip())
        if 0 <= idx < len(tools):
            match = tools[idx]
    if match is None:
        for t in tools:
            if t.get("function", {}).get("name") == tool_name:
                match = t
                break
    if match is None:
        return (f"Unknown native tool '{tool_name}'. Call list_native_tools() first "
                f"to see valid indices/names.")
    fn = match.get("function", {})
    return json.dumps(
        {
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        },
        indent=2,
    )


