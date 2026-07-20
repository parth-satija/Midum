# =============================================================================
# flows.py — backend logic for the GUI's "🔗 Flows" tab.
#
# The Flows tab is a node-graph editor (Drawflow, in gui/app.py). This
# module has NO knowledge of the GUI or Drawflow's JS API — it only deals
# with the plain-dict JSON shape Drawflow's editor.export() produces:
#
#   {
#     "drawflow": {
#       "Home": {
#         "data": {
#           "<node_id>": {
#             "id": <int>, "name": "<node type, see below>",
#             "data": {...}, "class": "...", "html": "...",
#             "pos_x": <int>, "pos_y": <int>,
#             "inputs":  {"input_1": {"connections":[{"node":"<id>","output":"output_1"}]}, ...},
#             "outputs": {"output_1": {"connections":[{"node":"<id>","input":"input_1"}]}, ...},
#           }, ...
#         }
#       }
#     }
#   }
#
# NODE TYPES ("name" field)
# --------------------------
#   "start"                       — entry marker. 0 inputs, 1 output (Sequence).
#   "end"                         — exit marker. 1 input (Sequence), 0 outputs.
#   "tool::<tool_name>"           — a native Midum tool call.
#   "mcp::<server>::<tool_name>"  — a call to a tool on a connected MCP server.
#   "variable"                    — a named data slot. 1 input (data-in, optional),
#                                    1 output (data-out).
#   "logic::if"                   — branch. 2 inputs (Sequence, Value data-in),
#                                    2 outputs (True Sequence, False Sequence).
#   "logic::loop"                 — for-each. 2 inputs (Sequence, Iterable data-in),
#                                    3 outputs (Body Sequence, Item data-out, After Sequence).
#
# PIN CONVENTION
# --------------
# Every node type that participates in execution ORDER (start, end, tool/mcp,
# if, loop) has a Sequence pin at "input_1" and its primary Sequence
# continuation at "output_1" (if/loop additionally use output_2/output_3 for
# their second/third branch — see per-node docs below). Nodes NEVER visited
# by the sequence walk (variable nodes) don't have a meaningful "input_1"/
# "output_1" role even though Drawflow still numbers their single pin
# "output_1" internally — they're excluded from the sequence walk by node
# type, not by pin name, so this doesn't collide.
#
# DATA pins (tool/if/loop parameter inputs, tool Object-outputs, variable
# in/out, loop Item-out) are resolved separately, structurally, by
# `_data_source_ref()` walking the graph connection rather than by name —
# so two variables can be labelled the same thing in the GUI without any
# ambiguity in the generated code (each becomes `_var_<node_id>`).
#
# For tool/mcp nodes specifically: `node["data"]["_flow_param_order"]` (set
# by the GUI when the node is created) lists the tool's parameter names in
# the same order as its extra input pins, i.e. param `_flow_param_order[i]`
# lives at pin `input_{i+2}` (input_1 being reserved for Sequence). Each
# param's value comes from whatever is wired into that pin (via
# `_data_source_ref`) if anything is, otherwise from the manually-typed
# value at `node["data"][param_name]` (bound live from the GUI's field via
# Drawflow's df-<param> two-way binding).
#
# Responsibilities:
#   1. validate_flow_name() / classify_tool_kind() — small pure helpers.
#   2. compile_flow()      — recursively walk the graph in execution order
#      starting from Start, emitting properly-indented Python for straight
#      runs, `if/else:` blocks, and `for ... in ...:` loops as the graph
#      structure dictates, resolving parameter values (literal vs
#      variable-wired) and Object-output capture per node along the way.
#   3. save_flow()          — validate, compile, and upsert the result into
#      flow_tools.py between name-keyed markers; persists description +
#      schema info into flow_meta.json.
#   4. list_flows() / list_flow_schemas() / run_flow() — same as before,
#      make saved flows discoverable and callable as tools.
# =============================================================================

import importlib
import json
import keyword
import os
import re

from config import FLOW_PROMOTED_TOOLS_FILE

_PKG_ROOT = os.path.dirname(os.path.abspath(__file__))
FLOW_TOOLS_FILE = os.path.join(_PKG_ROOT, "flow_tools.py")
FLOW_META_FILE = os.path.join(_PKG_ROOT, "flow_meta.json")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PY_KEYWORDS = set(keyword.kwlist) | set(getattr(keyword, "softkwlist", []))


# =============================================================================
# 1. Small pure helpers
# =============================================================================
def validate_flow_name(name: str):
    """Returns (ok: bool, error: str). Must be usable verbatim as `def <n>():`."""
    name = (name or "").strip()
    if not name:
        return False, "Name is required."
    if not _IDENTIFIER_RE.match(name):
        return False, (
            "Name must be a valid Python identifier: letters, digits, and "
            "underscores only, and it can't start with a digit."
        )
    if name in _PY_KEYWORDS:
        return False, f"'{name}' is a reserved Python keyword and can't be used as a function name."
    return True, ""


def _sanitize_identifier_fragment(raw: str, fallback: str) -> str:
    """Best-effort sanitizer for free-text labels (variable/loop-item
    friendly names) that only ever end up embedded in a comment, never in
    generated code directly (codegen always addresses variables/loop items
    by node id, e.g. `_var_12` — see module docstring) — so this is just
    for making comments readable, not for correctness."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", (raw or "").strip())
    s = re.sub(r"^[0-9]+", "", s)
    return s or fallback


# Output-tool-name heuristic prefixes: tools whose job is to *report*
# information (their return value is the point) rather than *do* something.
_OUTPUT_PREFIXES = (
    "list_", "read_", "get_", "search_", "find_", "show_", "explore_",
    "query_", "view_", "inspect_", "scan_", "check_", "describe_",
)
# Tools that both perform an action AND return a substantial result worth
# wiring into a variable (get a second, Object output pin in the GUI).
_HYBRID_TOOL_NAMES = {
    "execute_python_code", "execute_terminal_command", "generate_image",
    "capture_screen_to_ram", "act_on_element", "click_ocr_index",
    "manual_inspect_app_subtree", "manual_scan_app_layouts",
    "query_gemini_app", "read_browser_page", "screenshot_active_window",
}


def classify_tool_kind(tool_name: str, description: str = "") -> str:
    """
    "output"  — result is the point (list_active_windows, read_browser_page);
                represented in generated code as `<var> = <tool>(...)`.
    "action"  — does a task, no meaningful return value;
                represented as `<tool>(...)`.
    "hybrid"  — does a task AND returns something worth capturing; gets both
                a Sequence-out and an Object-out pin in the GUI.
    This is a best-effort heuristic based on the tool's name (and
    optionally its description) — it's used purely to decide how many
    output pins the GUI gives a tool node, and has no bearing on whether
    the call actually executes; every tool node call always executes as
    part of the flow's sequence regardless of kind.
    """
    name = (tool_name or "").lower()
    if name in _HYBRID_TOOL_NAMES:
        return "hybrid"
    if any(name.startswith(p) for p in _OUTPUT_PREFIXES):
        return "output"
    desc = (description or "").lower()
    if desc.startswith(("returns ", "reads ", "lists ", "gets ", "fetches ", "retrieves ")):
        return "output"
    return "action"


# =============================================================================
# 2. Graph -> Python compilation
# =============================================================================
def _extract_nodes(graph: dict) -> dict:
    """node_id (str) -> node dict, straight out of a Drawflow editor.export()."""
    try:
        data = graph.get("drawflow", {}).get("Home", {}).get("data", {}) or {}
        return {str(k): v for k, v in data.items()}
    except Exception:
        return {}


def _pin_targets(node: dict, pin_key: str) -> list:
    """Every node id connected out of `pin_key` on `node`'s outputs, in
    connection order (usually just one, but a pin can fan out to several)."""
    conns = ((node.get("outputs") or {}).get(pin_key) or {}).get("connections") or []
    return [str(c.get("node")) for c in conns if c.get("node") is not None]


def _pin_source(node: dict, pin_key: str):
    """The single node id feeding into `pin_key` on `node`'s inputs, or None."""
    conns = ((node.get("inputs") or {}).get(pin_key) or {}).get("connections") or []
    if not conns:
        return None
    return str(conns[0].get("node"))


_TOOL_NODE_RE = re.compile(r"^tool::(.+)$")
_MCP_NODE_RE = re.compile(r"^mcp::([^:]+)::(.+)$")


def _data_source_ref(nodes: dict, node_id):
    """
    If `node_id` is a node that can act as a DATA source for a downstream
    pin (a variable node's data-out, a loop's per-item data-out, or a
    tool/mcp node's Object-out), return the Python expression a consumer
    should reference. Returns None if `node_id` doesn't produce data (e.g.
    it's a plain action tool with no Object pin, or doesn't exist).
    """
    if node_id is None or node_id not in nodes:
        return None
    node = nodes[node_id]
    ntype = node.get("name", "")
    if ntype == "variable":
        return f"_var_{node_id}"
    if ntype == "logic::loop":
        return f"_loop_item_{node_id}"
    if ntype.startswith("tool::") or ntype.startswith("mcp::"):
        # Only a real data source if this node was actually created with an
        # Object-out pin (output/hybrid kind) — that shows up as the
        # "output_2" key simply existing on the node, whether or not it's
        # currently wired to anything downstream.
        if "output_2" in (node.get("outputs") or {}):
            return f"_out_{node_id}"
    if ntype in ("ai::prompt", "ai::summarize", "ai::choose"):
        # Same Object-out convention as tool/mcp nodes -- these always have
        # an output_2 (their AI result), so any wired data param can pull
        # from the AI response of an upstream AI node too.
        if "output_2" in (node.get("outputs") or {}):
            return f"_out_{node_id}"
    return None


def _resolve_tool_args_src(node: dict, node_id: str, nodes: dict) -> str:
    """
    Build the Python source text for a tool/mcp call's argument dict,
    preferring a wired-in data source (variable / loop item / another
    tool's Object-out) over the manually-typed value for each parameter.
    """
    raw_data = node.get("data") or {}
    param_order = raw_data.get("_flow_param_order") or []
    inputs = node.get("inputs") or {}
    parts = []
    for i, pname in enumerate(param_order):
        pin_key = f"input_{i + 2}"
        src_id = _pin_source(node, pin_key) if pin_key in inputs else None
        ref = _data_source_ref(nodes, src_id) if src_id else None
        if ref:
            parts.append(f"{pname!r}: {ref}")
            continue
        val = raw_data.get(pname)
        if val not in (None, ""):
            parts.append(f"{pname!r}: {val!r}")
    return "{" + ", ".join(parts) + "}"


def _codegen_tool_call(node: dict, node_id: str, nodes: dict, tool_name: str, mcp_server: str = None) -> list:
    args_src = _resolve_tool_args_src(node, node_id, nodes)
    header = f"Tool: {tool_name}" + (f" (MCP: {mcp_server})" if mcp_server else "")
    lines = [f"# --- {header} ---", f"_args = {args_src}"]
    call = (f"_call_mcp_tool_step({mcp_server!r}, {tool_name!r}, _args)" if mcp_server
            else f"_dispatch_midum_tool({tool_name!r}, _args)")

    has_object_pin = "output_2" in (node.get("outputs") or {})
    if has_object_pin:
        lines.append(f"_out_{node_id} = {call}")
        lines.append(f"_flow_results.append(_out_{node_id})")
    else:
        lines.append(f"_step = {call}")
        lines.append("_flow_results.append(_step)")
    return lines


def _codegen_ai_prompt(node: dict, node_id: str, nodes: dict) -> list:
    """'Prompt AI' node -- sends a static instruction (typed in the node)
    plus optional wired Context data to whichever provider is currently
    configured (see _flow_consult_ai in the generated file's header), and
    captures the plain-text reply as this node's Object-out."""
    data = node.get("data") or {}
    ctx_id = _pin_source(node, "input_2")
    ctx_ref = _data_source_ref(nodes, ctx_id) if ctx_id else None
    ctx_src = ctx_ref if ctx_ref else repr(data.get("context", ""))
    prompt_src = repr(data.get("prompt", ""))
    return [
        "# --- Prompt AI ---",
        f"_out_{node_id} = _flow_consult_ai({prompt_src}, context={ctx_src})",
        f"_flow_results.append(_out_{node_id})",
    ]


def _codegen_ai_summarize(node: dict, node_id: str, nodes: dict) -> list:
    """'Ask AI to Summarize' node -- summarizes wired-in Text (or the typed
    fallback) at the chosen length, captures the summary as Object-out."""
    data = node.get("data") or {}
    text_id = _pin_source(node, "input_2")
    text_ref = _data_source_ref(nodes, text_id) if text_id else None
    text_src = text_ref if text_ref else repr(data.get("text", ""))
    length = (data.get("length") or "medium").strip() or "medium"
    instruction = (
        f"Summarize the following text at a {length} length. "
        f"Respond with only the summary, no preamble."
    )
    return [
        "# --- Ask AI to Summarize ---",
        f"_out_{node_id} = _flow_consult_ai({instruction!r}, context={text_src})",
        f"_flow_results.append(_out_{node_id})",
    ]


def _codegen_ai_choose(node: dict, node_id: str, nodes: dict) -> list:
    """'Ask AI to Choose' node -- asks the AI to pick exactly one option
    (wired-in list/text, or the typed comma-separated fallback) for the
    given question, and captures the chosen option's exact text as
    Object-out (see _flow_ai_choose in the generated file's header for how
    the reply gets snapped back to one of the real option strings)."""
    data = node.get("data") or {}
    opts_id = _pin_source(node, "input_2")
    opts_ref = _data_source_ref(nodes, opts_id) if opts_id else None
    opts_src = opts_ref if opts_ref else repr(data.get("options", ""))
    question_src = repr(data.get("question", ""))
    return [
        "# --- Ask AI to Choose ---",
        f"_out_{node_id} = _flow_ai_choose({question_src}, {opts_src})",
        f"_flow_results.append(_out_{node_id})",
    ]


def _condition_src(node: dict, node_id: str, nodes: dict) -> str:
    data = node.get("data") or {}
    op = (data.get("op") or "truthy").strip()
    compare = data.get("compare", "")
    src_id = _pin_source(node, "input_2")
    ref = _data_source_ref(nodes, src_id) if src_id else None
    left = ref if ref else repr(data.get("value", ""))
    if op == "equals":
        return f"({left}) == {compare!r}"
    if op == "not_equals":
        return f"({left}) != {compare!r}"
    if op == "contains":
        return f"{compare!r} in ({left} or '')"
    if op == "greater_than":
        return f"_flow_num({left}) > _flow_num({compare!r})"
    if op == "less_than":
        return f"_flow_num({left}) < _flow_num({compare!r})"
    return f"bool({left})"


class _CodegenWarnings(list):
    def add(self, msg):
        self.append(msg)


def _emit_variable_captures(node: dict, node_id: str, nodes: dict, pad: str, out: list):
    """After a tool/mcp/ai node's Object-out (_out_<node_id>) is computed,
    capture it into any Variable node(s) whose data-in pin is wired
    directly to that Object-out pin -- otherwise a variable "fed from an
    output" would only ever see its typed fallback, never the live wired
    value, since Variable nodes sit outside the Sequence walk entirely."""
    for target_id in _pin_targets(node, "output_2"):
        target = nodes.get(target_id)
        if target and target.get("name") == "variable":
            out.append(pad + f"_var_{target_id} = _out_{node_id}")


def _emit_block(node_id, nodes: dict, indent: int, visited: set, out: list, warnings: _CodegenWarnings):
    """
    Recursively emit indented Python for the run of nodes starting at
    `node_id`, following each node's Sequence pin(s). `if`/`loop` nodes
    open a nested block (recursing with indent+1 for their branch/body)
    and, for `if`, each branch is a dead end in this simplified model (no
    automatic re-convergence after an if/else — each branch should lead to
    its own End, or the flow just stops there). `loop` DOES resume the
    outer indent afterwards, via its After pin.
    """
    pad = "    " * indent
    while node_id is not None:
        if node_id not in nodes:
            return
        if node_id in visited:
            out.append(pad + "# (cycle detected in the flow graph -- stopping here to avoid an infinite generated loop)")
            return
        visited.add(node_id)
        node = nodes[node_id]
        ntype = node.get("name", "")

        if ntype == "start":
            node_id = (_pin_targets(node, "output_1") or [None])[0]
            continue

        if ntype == "end":
            out.append(pad + "return _flow_results[-1] if _flow_results else None")
            return

        if ntype == "logic::if":
            cond = _condition_src(node, node_id, nodes)
            out.append(pad + f"if {cond}:")
            true_id = (_pin_targets(node, "output_1") or [None])[0]
            false_id = (_pin_targets(node, "output_2") or [None])[0]
            if true_id:
                _emit_block(true_id, nodes, indent + 1, set(visited), out, warnings)
            else:
                out.append(pad + "    pass")
            if false_id:
                out.append(pad + "else:")
                _emit_block(false_id, nodes, indent + 1, set(visited), out, warnings)
            return  # branches are dead ends in this simplified model

        if ntype == "logic::loop":
            data = node.get("data") or {}
            item_label = _sanitize_identifier_fragment(data.get("item_var", ""), "item")
            src_id = _pin_source(node, "input_2")
            iterable_ref = _data_source_ref(nodes, src_id) if src_id else None
            if not iterable_ref:
                warnings.add(f"Loop node {node_id} has no Iterable input wired in -- looping over an empty list.")
                iterable_ref = "[]"
            out.append(pad + f"for _loop_item_{node_id} in (_flow_iter({iterable_ref}) or []):  # {item_label}")
            body_id = (_pin_targets(node, "output_1") or [None])[0]
            if body_id:
                _emit_block(body_id, nodes, indent + 1, set(visited), out, warnings)
            else:
                out.append(pad + "    pass")
            node_id = (_pin_targets(node, "output_3") or [None])[0]
            continue

        if ntype == "ai::prompt":
            lines = _codegen_ai_prompt(node, node_id, nodes)
            out.extend(pad + line for line in lines)
            _emit_variable_captures(node, node_id, nodes, pad, out)
            node_id = (_pin_targets(node, "output_1") or [None])[0]
            continue

        if ntype == "ai::summarize":
            lines = _codegen_ai_summarize(node, node_id, nodes)
            out.extend(pad + line for line in lines)
            _emit_variable_captures(node, node_id, nodes, pad, out)
            node_id = (_pin_targets(node, "output_1") or [None])[0]
            continue

        if ntype == "ai::choose":
            lines = _codegen_ai_choose(node, node_id, nodes)
            out.extend(pad + line for line in lines)
            _emit_variable_captures(node, node_id, nodes, pad, out)
            node_id = (_pin_targets(node, "output_1") or [None])[0]
            continue

        m = _MCP_NODE_RE.match(ntype)
        if m:
            lines = _codegen_tool_call(node, node_id, nodes, m.group(2), mcp_server=m.group(1))
            out.extend(pad + line for line in lines)
            _emit_variable_captures(node, node_id, nodes, pad, out)
            node_id = (_pin_targets(node, "output_1") or [None])[0]
            continue

        m = _TOOL_NODE_RE.match(ntype)
        if m:
            lines = _codegen_tool_call(node, node_id, nodes, m.group(1))
            out.extend(pad + line for line in lines)
            _emit_variable_captures(node, node_id, nodes, pad, out)
            node_id = (_pin_targets(node, "output_1") or [None])[0]
            continue

        if ntype == "variable":
            # Never reached via the sequence walk (nothing wires a Sequence
            # pin into a variable node) -- if we somehow got here, skip it
            # rather than crash.
            return

        out.append(pad + f"# TODO: no codegen registered yet for node type '{ntype}'")
        return


def _codegen_variable_declarations(nodes: dict) -> list:
    """One `_var_<id> = <literal or None>` line per variable node, up
    front, so every variable name referenced later is always defined even
    if the node producing its value never runs (e.g. it's inside a
    not-taken if-branch)."""
    lines = []
    for node_id, node in nodes.items():
        if node.get("name") != "variable":
            continue
        data = node.get("data") or {}
        raw_val = data.get("value", "")
        label = _sanitize_identifier_fragment(data.get("name", ""), f"var_{node_id}")
        lines.append(f"    _var_{node_id} = {raw_val!r} or None  # {label}")
    return lines


def compile_flow(name: str, graph: dict) -> str:
    """
    Compile one Drawflow graph export into a complete `def <n>(): ...`
    Python source block (including docstring), ready to be written into
    flow_tools.py. Does NOT validate `name` -- call validate_flow_name()
    first (save_flow() does this for you).
    """
    nodes = _extract_nodes(graph)
    warnings = _CodegenWarnings()

    body_lines = ["    _flow_results = []"]
    body_lines.extend(_codegen_variable_declarations(nodes))

    if not nodes:
        body_lines.append("    pass  # empty flow -- add and connect nodes, then Save again")
    else:
        start_ids = [nid for nid, n in nodes.items() if n.get("name") == "start"]
        if not start_ids:
            warnings.add("No Start node found -- add one so the flow has a defined entry point.")
            body_lines.append("    pass  # no Start node")
        else:
            if len(start_ids) > 1:
                warnings.add(f"{len(start_ids)} Start nodes found -- only the first is used.")
            out = []
            _emit_block(start_ids[0], nodes, 1, set(), out, warnings)
            if not out or "return" not in out[-1]:
                out.append("    return _flow_results[-1] if _flow_results else None")
            body_lines.extend(out)
            if not any(n.get("name") == "end" for n in nodes.values()):
                warnings.add("No End node found -- the flow will fall through and return its last step's result.")

    if warnings:
        body_lines.append("")
        for w in warnings:
            body_lines.append(f"    # \u26a0\ufe0f {w}")

    body = "\n".join(body_lines) if body_lines else "    pass"

    header = (
        f"def {name}():\n"
        f'    """\n'
        f"    Auto-generated from the Flows tab's node graph. Edit the graph in\n"
        f"    the GUI and click Save to regenerate this function — manual edits\n"
        f"    made directly to this function body will be OVERWRITTEN the next\n"
        f"    time this flow is saved.\n"
        f'    """\n'
    )
    return header + body + "\n"


# =============================================================================
# 3. Persistence — upsert into flow_tools.py + flow_meta.json
# =============================================================================
_START_MARKER_TMPL = "# === FLOW: {name} ==="
_END_MARKER_TMPL = "# === END FLOW: {name} ==="
_FLOW_NAME_IN_MARKER_RE = re.compile(r"# === FLOW: (\w+) ===")

_FLOW_TOOLS_HEADER = (
    '"""\n'
    "flow_tools.py — auto-generated by the GUI's Flows tab. Each function\n"
    "below corresponds to one saved node-graph flow.\n"
    "\n"
    "DO NOT hand-edit the body of a generated function — re-saving that flow\n"
    "from the GUI overwrites everything between its START/END markers.\n"
    '"""\n\n'
    "from gui.dispatch import _dispatch_midum_tool\n"
    "import main as _midum\n\n\n"
    "def _call_mcp_tool_step(server, tool_name, args):\n"
    "    return _midum.call_mcp_tool(server, tool_name, args)\n\n\n"
    "def _flow_iter(value):\n"
    "    \"\"\"Best-effort coercion of a wired-in value into something a\n"
    "    `for` loop can iterate: a real list/tuple passes through, a JSON\n"
    "    array-as-string gets parsed, anything else becomes a 1-item list\n"
    "    (or an empty list for None/empty string) so a Loop node never hard\n"
    "    crashes just because its Iterable turned out to be a scalar.\"\"\"\n"
    "    if value is None or value == '':\n"
    "        return []\n"
    "    if isinstance(value, (list, tuple)):\n"
    "        return list(value)\n"
    "    if isinstance(value, str):\n"
    "        try:\n"
    "            import json as _json\n"
    "            parsed = _json.loads(value)\n"
    "            if isinstance(parsed, list):\n"
    "                return parsed\n"
    "        except Exception:\n"
    "            pass\n"
    "    return [value]\n\n\n"
    "def _flow_num(value):\n"
    "    \"\"\"Best-effort numeric coercion for If-node greater/less-than\n"
    "    comparisons -- non-numeric input compares as 0 rather than raising.\"\"\"\n"
    "    try:\n"
    "        return float(value)\n"
    "    except Exception:\n"
    "        return 0\n\n\n"
    "def _flow_consult_ai(prompt, context=\"\"):\n"
    "    \"\"\"Send a one-off prompt (optionally with extra context text) to\n"
    "    whichever AI provider is currently configured as Midum's primary\n"
    "    model (config.MODEL_PROVIDER), and return its plain-text reply.\n"
    "    Shared by the Flows tab's Prompt AI / Ask AI to Summarize / Ask AI\n"
    "    to Choose nodes -- falls back to GroqCloud if the configured\n"
    "    provider is unrecognised.\"\"\"\n"
    "    import config as _flow_cfg\n"
    "    provider = getattr(_flow_cfg, 'MODEL_PROVIDER', 'groq')\n"
    "    try:\n"
    "        if provider == 'openrouter':\n"
    "            from providers.openrouter_backend import consult_openrouter\n"
    "            return consult_openrouter(prompt, context=context)\n"
    "        if provider == 'gemini_api':\n"
    "            from providers.gemini_api_backend import consult_gemini_api\n"
    "            return consult_gemini_api(prompt, context=context)\n"
    "        if provider == 'ollama_cloud':\n"
    "            from providers.ollama_cloud_backend import consult_ollama_cloud\n"
    "            return consult_ollama_cloud(prompt, context=context)\n"
    "        if provider == 'gemini_web':\n"
    "            from providers.gemini_reasoning import consult_gemini\n"
    "            return consult_gemini(prompt, context=context)\n"
    "        from providers.groq_backend import consult_groq\n"
    "        return consult_groq(prompt, context=context)\n"
    "    except Exception as e:\n"
    "        return f'AI consult error: {e}'\n\n\n"
    "def _flow_ai_choose(question, options):\n"
    "    \"\"\"Ask the AI to pick exactly one option from `options` (a list,\n"
    "    or a comma-separated string) for `question`, and return that\n"
    "    option's exact text. Snaps the model's free-form reply back to the\n"
    "    closest real option so downstream branching (e.g. an If node) can\n"
    "    compare against a known value instead of loose AI phrasing.\"\"\"\n"
    "    opts = options\n"
    "    if isinstance(opts, str):\n"
    "        opts = [o.strip() for o in opts.split(',') if o.strip()]\n"
    "    elif not isinstance(opts, (list, tuple)):\n"
    "        opts = [str(opts)] if opts not in (None, '') else []\n"
    "    else:\n"
    "        opts = [str(o) for o in opts]\n"
    "    if not opts:\n"
    "        return _flow_consult_ai(question)\n"
    "    prompt = (\n"
    "        f\"{question}\\n\\nChoose exactly ONE of the following options and reply \"\n"
    "        f\"with ONLY that option's exact text, nothing else:\\n- \" + '\\n- '.join(opts)\n"
    "    )\n"
    "    raw = (_flow_consult_ai(prompt) or '').strip()\n"
    "    for o in opts:\n"
    "        if o.strip().lower() == raw.lower():\n"
    "            return o\n"
    "    for o in opts:\n"
    "        if o.strip().lower() in raw.lower():\n"
    "            return o\n"
    "    return raw\n\n\n"
)

_REQUIRED_HEADER_SNIPPETS = (
    "from gui.dispatch import _dispatch_midum_tool",
    "import main as _midum",
    "def _call_mcp_tool_step(",
    "def _flow_iter(",
    "def _flow_num(",
    "def _flow_consult_ai(",
    "def _flow_ai_choose(",
)


def _ensure_flow_tools_header(content: str) -> str:
    """If flow_tools.py already existed from before this feature (missing
    the shared helpers), inject them once rather than leaving every saved
    flow calling undefined names."""
    if all(snippet in content for snippet in _REQUIRED_HEADER_SNIPPETS):
        return content
    helpers = _FLOW_TOOLS_HEADER.split('"""\n\n', 1)[-1]
    if content.startswith('"""'):
        end = content.find('"""', 3)
        if end != -1:
            end += 3
            return content[:end] + "\n\n" + helpers + content[end:].lstrip("\n")
    return helpers + content


def _load_flow_meta() -> dict:
    if not os.path.exists(FLOW_META_FILE):
        return {}
    try:
        with open(FLOW_META_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_flow_meta(meta: dict):
    try:
        os.makedirs(os.path.dirname(FLOW_META_FILE), exist_ok=True)
        with open(FLOW_META_FILE, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
    except Exception:
        pass


def save_flow(name: str, graph: dict, description: str = "") -> str:
    """
    Validate `name`, compile `graph` into a Python function, and upsert it
    into flow_tools.py. Also stores `description` in flow_meta.json so the
    flow can be listed/run as a tool from the Tools tab. Returns a
    human-readable status message; messages starting with "Error:" mean
    failure.
    """
    ok, err = validate_flow_name(name)
    if not ok:
        return f"Error: {err}"

    try:
        fn_source = compile_flow(name, graph or {})
    except Exception as e:
        return f"Error: failed to compile flow '{name}': {e}"

    start_tag = _START_MARKER_TMPL.format(name=name)
    end_tag = _END_MARKER_TMPL.format(name=name)
    block = f"{start_tag}\n{fn_source}{end_tag}\n"

    try:
        if os.path.exists(FLOW_TOOLS_FILE):
            with open(FLOW_TOOLS_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            content = _ensure_flow_tools_header(content)
        else:
            content = _FLOW_TOOLS_HEADER

        pattern = re.compile(re.escape(start_tag) + r".*?" + re.escape(end_tag) + r"\n?", re.DOTALL)
        if pattern.search(content):
            content = pattern.sub(block, content)
            action = "updated"
        else:
            if not content.endswith("\n\n"):
                content = content.rstrip("\n") + "\n\n\n"
            content += block + "\n"
            action = "saved"

        os.makedirs(os.path.dirname(FLOW_TOOLS_FILE), exist_ok=True)
        with open(FLOW_TOOLS_FILE, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return f"Error: failed to write flow_tools.py: {e}"

    meta = _load_flow_meta()
    meta[name] = {"description": (description or "").strip(), "graph": graph or {}}
    _save_flow_meta(meta)

    return f"Flow '{name}' {action} in flow_tools.py"


def get_flow_graph(name: str) -> dict:
    """The raw Drawflow graph JSON last saved for `name`, so the Flows tab
    can reload it into the canvas for editing. Returns {} if the flow
    doesn't exist or was saved before graphs were persisted (pre-existing
    flow_meta.json entries only had a description)."""
    meta = _load_flow_meta()
    return (meta.get(name) or {}).get("graph") or {}


def delete_flow(name: str) -> str:
    """Remove `name`'s function block from flow_tools.py and its entry
    from flow_meta.json. Returns a human-readable status message;
    messages starting with "Error:" mean failure."""
    if name not in list_flows():
        return f"Error: no saved flow named '{name}'."

    start_tag = _START_MARKER_TMPL.format(name=name)
    end_tag = _END_MARKER_TMPL.format(name=name)
    try:
        with open(FLOW_TOOLS_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        pattern = re.compile(re.escape(start_tag) + r".*?" + re.escape(end_tag) + r"\n?\n*", re.DOTALL)
        new_content, n = pattern.subn("", content)
        if n == 0:
            return f"Error: could not find flow '{name}' in flow_tools.py."
        with open(FLOW_TOOLS_FILE, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        return f"Error: failed to update flow_tools.py: {e}"

    meta = _load_flow_meta()
    meta.pop(name, None)
    _save_flow_meta(meta)

    return f"Flow '{name}' deleted."


def list_flows() -> list:
    """Every flow name currently saved in flow_tools.py, in file order."""
    if not os.path.exists(FLOW_TOOLS_FILE):
        return []
    try:
        with open(FLOW_TOOLS_FILE, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return []
    return _FLOW_NAME_IN_MARKER_RE.findall(content)


# =============================================================================
# 4. Tool-schema generation + running — makes saved flows callable
# =============================================================================
def flow_description(name: str) -> str:
    meta = _load_flow_meta()
    desc = (meta.get(name) or {}).get("description") or ""
    return desc.strip() or f"Runs the '{name}' flow (a saved sequence of tool calls from the Flows tab)."


def list_flow_schemas() -> list:
    """One {"name","description","properties","required","promoted"} entry per
    saved flow -- same shape as list_tool_schemas() for native tools, so the
    Tools tab's Flows dropdown can list them uniformly. Flows take no
    external parameters (their steps get values from what was wired up /
    typed into each node when the flow was built). "promoted" reflects
    whether this flow currently has its own always-on tool schema (see the
    PROMOTED FLOWS section below) -- purely informational here, doesn't
    change how run_flow(name) behaves."""
    out = []
    for name in list_flows():
        out.append({
            "name": name,
            "description": flow_description(name),
            "properties": {},
            "required": [],
            "promoted": is_flow_promoted(name),
        })
    return out


def run_flow(name: str, args: dict = None) -> str:
    """Import flow_tools.py fresh and call the named flow function."""
    if name not in list_flows():
        return f"Error: no saved flow named '{name}'."
    try:
        import flow_tools
        importlib.reload(flow_tools)
    except Exception as e:
        return f"Error: failed to load flow_tools.py: {e}"

    fn = getattr(flow_tools, name, None)
    if not callable(fn):
        return f"Error: '{name}' is listed but flow_tools.{name} is not a callable function."

    try:
        result = fn()
        return str(result)
    except Exception as e:
        return f"Error: flow '{name}' raised an exception during execution: {e}"


def list_flows_formatted() -> str:
    """
    On-demand discovery for saved Flows, mirroring list_mcp_servers() for
    MCP servers: cheap (name + one-line description only, no schemas) so
    it's safe to always include as a native tool. Follow up with
    run_flow(name) to execute one -- exactly the same two-step shape as
    list_mcp_servers() -> call_mcp_tool(...).

    Flows marked [PROMOTED] are ALSO directly callable by their own name
    without this discovery step at all (see get_promoted_flow_schemas()),
    same as a promoted MCP tool.
    """
    names = list_flows()
    if not names:
        return "No saved flows yet. Build one in the Flows tab first."
    lines = [f"Saved flows ({len(names)}):"]
    for i, name in enumerate(names):
        tag = " [PROMOTED]" if is_flow_promoted(name) else ""
        lines.append(f"[{i}] {name}{tag} — {flow_description(name)}")
    lines.append("\nCall run_flow(name=\"<flow name>\") to execute one.")
    return "\n".join(lines)


# =============================================================================
# 5. PROMOTED FLOWS
# =============================================================================
#
# A promoted flow is a saved Flow the user has explicitly marked (via the
# Flows tab) to have a schema included directly alongside Midum's native
# tools -- so the model can call it straight away, by its own name, like
# any native tool, instead of having to discover it first via
# list_flows()/run_flow(name). This exactly mirrors "promoted" MCP tools
# (see midum_mcp/manager.py's PROMOTED MCP TOOLS section) -- same opt-in
# escape hatch from on-demand discovery, same trade of a little context
# budget for zero-friction single-call invocation.
#
# Persisted as a flat list of flow-name strings in
# storage/flow_promoted_tools.json, independent of flow_meta.json (a flow
# can be promoted even if it's since been deleted -- it just won't
# contribute a schema entry until/unless a flow by that name exists again).
_promoted_flows_cache: list | None = None


def _load_promoted_flows() -> list:
    """Read storage/flow_promoted_tools.json. Returns [] if missing/invalid."""
    global _promoted_flows_cache
    if _promoted_flows_cache is not None:
        return _promoted_flows_cache
    items = []
    try:
        if os.path.exists(FLOW_PROMOTED_TOOLS_FILE):
            with open(FLOW_PROMOTED_TOOLS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                items = [n for n in loaded if isinstance(n, str) and n]
    except Exception as e:
        print(f"⚠️ [Flows] Could not read {FLOW_PROMOTED_TOOLS_FILE}: {e}")
        items = []
    _promoted_flows_cache = items
    return items


def _save_promoted_flows(items: list):
    global _promoted_flows_cache
    _promoted_flows_cache = items
    try:
        os.makedirs(os.path.dirname(FLOW_PROMOTED_TOOLS_FILE), exist_ok=True)
        with open(FLOW_PROMOTED_TOOLS_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
    except Exception as e:
        print(f"⚠️ [Flows] Could not save {FLOW_PROMOTED_TOOLS_FILE}: {e}")


def is_flow_promoted(name: str) -> bool:
    return name in _load_promoted_flows()


def promote_flow(name: str) -> str:
    """Mark one saved flow as promoted -- its schema will be included with
    the native tools list from now on, and it becomes directly callable by
    name."""
    if not name:
        return "Error: a flow 'name' is required."
    if name not in list_flows():
        return f"Error: no saved flow named '{name}'."
    items = _load_promoted_flows()
    if name in items:
        return f"Flow '{name}' is already promoted."
    _save_promoted_flows(list(items) + [name])
    return f"Promoted flow '{name}' -- it will now be offered directly to the model."


def demote_flow(name: str) -> str:
    """Unmark a promoted flow -- it goes back to on-demand discovery only."""
    items = _load_promoted_flows()
    if name not in items:
        return f"Flow '{name}' was not promoted."
    _save_promoted_flows([n for n in items if n != name])
    return f"Demoted flow '{name}' -- back to on-demand discovery only."


def get_promoted_flows() -> list:
    """Return the raw list of promoted flow names."""
    return list(_load_promoted_flows())


def get_promoted_flow_schemas() -> list:
    """
    Build native-style {"type":"function","function":{...}} schema entries
    for every currently-promoted flow that still actually exists in
    flow_tools.py. Mirrors midum_mcp.tools.get_promoted_tool_schemas() for
    MCP tools -- names are kept exactly as the flow was saved (no prefix)
    so a direct call by name dispatches straight to run_flow(name) (see
    the flow-name fallback in orchestration.py's tool dispatch, which
    mirrors _mcp_autoroute_tool_call for MCP tools).

    Entries for flows that were promoted and later deleted are silently
    skipped (they reappear automatically if a flow by that name is saved
    again), same as a promoted MCP tool whose server/tool disappears.
    """
    schemas = []
    valid_names = set(list_flows())
    for name in get_promoted_flows():
        if name not in valid_names:
            continue
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": f"[FLOW] {flow_description(name)}".strip(),
                "parameters": {"type": "object", "properties": {}},
            },
        })
    return schemas
