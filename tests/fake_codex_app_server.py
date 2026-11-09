# tests/fake_codex_app_server.py
# A tiny stand-in for `codex app-server` (JSON-RPC over stdio, one JSON object
# per line) so providers/codex_backend.py can be tested without Codex, a
# network connection or a ChatGPT account.
#
# The scenario is picked from the text of the final user message:
#   SCENARIO:tool      agent text, then ONE dynamic tool call (item/tool/call)
#   SCENARIO:parallel  two dynamic tool calls back to back (backend must keep the first)
#   SCENARIO:answer    plain final answer
#   SCENARIO:leak      the model starts a native commandExecution item
#   SCENARIO:approval  the model asks for an exec approval
#   SCENARIO:limit     turn fails with a usage-limit error
#   SCENARIO:crash     the process dies mid-turn
#   SCENARIO:empty     turn completes with no output at all
#   SCENARIO:hang      nothing ever happens (until interrupted)
# Env: FAKE_CODEX_LOG=<file>      every received request is appended (JSON per line)
#      FAKE_CODEX_SIGNED_OUT=1    account/read returns no account
#      FAKE_CODEX_BAD_STARTUP=1   the startup 'list your tools' answer names a native tool
import itertools
import json
import os
import sys

LOG = os.environ.get("FAKE_CODEX_LOG")
_thread_ids = itertools.count(1)
threads = {}


def w(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def log(msg):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg) + "\n")


def usage(tid, inp=1000, cached=800, out=20):
    w({"method": "thread/tokenUsage/updated", "params": {
        "threadId": tid, "turnId": "turn_1",
        "tokenUsage": {"total": {"inputTokens": inp, "cachedInputTokens": cached,
                                 "outputTokens": out, "reasoningOutputTokens": 0,
                                 "totalTokens": inp + out}}}})


def completed(tid, status="completed", error=None):
    turn = {"id": "turn_1", "status": status}
    if error:
        turn["error"] = {"message": error}
    w({"method": "turn/completed", "params": {"threadId": tid, "turn": turn}})


def agent_message(tid, text):
    w({"method": "item/completed", "params": {"threadId": tid, "turnId": "turn_1",
                                              "item": {"type": "agentMessage", "id": "m1", "text": text}}})


def tool_call(tid, name, args, call_id="c1", rpc_id=900):
    w({"id": rpc_id, "method": "item/tool/call", "params": {
        "threadId": tid, "turnId": "turn_1", "callId": call_id, "tool": name, "arguments": args}})


def run_scenario(tid, text):
    if "List the exact names of every tool" in text:
        agent_message(tid, "shell, midum_probe" if os.environ.get("FAKE_CODEX_BAD_STARTUP") else "NONE")
        usage(tid)
        completed(tid)
    elif "SCENARIO:tool" in text:
        agent_message(tid, "Let me check that.")
        tool_call(tid, "execute_terminal_command", {"command": "echo hi"})
    elif "SCENARIO:parallel" in text:
        tool_call(tid, "list_directory", {"path": "."}, "c1", 901)
        tool_call(tid, "read_local_file", {"path": "a.txt"}, "c2", 902)
    elif "SCENARIO:answer" in text:
        agent_message(tid, "All done.")
        usage(tid)
        completed(tid)
    elif "SCENARIO:leak" in text:
        w({"method": "item/started", "params": {"threadId": tid, "turnId": "turn_1",
                                                "item": {"type": "commandExecution", "id": "x1", "command": "rm -rf /"}}})
    elif "SCENARIO:approval" in text:
        w({"id": 950, "method": "item/commandExecution/requestApproval",
           "params": {"threadId": tid, "turnId": "turn_1", "itemId": "x1", "command": "ls"}})
    elif "SCENARIO:limit" in text:
        completed(tid, "failed", "You've hit your usage limit. Upgrade to Plus or try again later.")
    elif "SCENARIO:crash" in text:
        sys.stderr.write("fatal: simulated crash\n")
        sys.stderr.flush()
        os._exit(1)
    elif "SCENARIO:empty" in text:
        completed(tid)
    elif "SCENARIO:hang" in text:
        pass
    else:
        agent_message(tid, "default answer")
        completed(tid)


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
    log({"method": method, "params": params, "id": rid})
    if method is None:                      # a response to one of OUR server requests
        continue
    if method == "initialize":
        w({"id": rid, "result": {"userAgent": "fake-codex"}})
    elif method == "initialized":
        pass
    elif method == "account/read":
        acct = None if os.environ.get("FAKE_CODEX_SIGNED_OUT") else \
            {"type": "chatgpt", "email": "me@example.com", "planType": "plus"}
        w({"id": rid, "result": {"account": acct, "requiresOpenaiAuth": True}})
    elif method == "account/rateLimits/read":
        w({"id": rid, "result": {"rateLimits": {"primary": {"usedPercent": 12, "windowDurationMins": 300}}}})
    elif method == "model/list":
        w({"id": rid, "result": {"data": [{"model": "gpt-fake", "displayName": "GPT Fake", "isDefault": True,
                                           "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}],
                                 "nextCursor": None}})
    elif method == "thread/start":
        tid = f"thr_{next(_thread_ids)}"
        threads[tid] = params
        w({"id": rid, "result": {"thread": {"id": tid}}})
    elif method == "thread/inject_items":
        w({"id": rid, "result": {}})
    elif method == "turn/start":
        tid = params["threadId"]
        w({"id": rid, "result": {"turn": {"id": "turn_1", "status": "inProgress"}}})
        run_scenario(tid, " ".join(p.get("text", "") for p in params.get("input", [])))
    elif method == "turn/interrupt":
        w({"id": rid, "result": {}})
        usage(params["threadId"])
        completed(params["threadId"], "interrupted")
    elif method in ("thread/unsubscribe", "thread/archive"):
        w({"id": rid, "result": {}})
    else:
        w({"id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})
