# tests/test_codex_backend.py  --  run from midum_pkg:  python -m unittest tests.test_codex_backend -v
import json
import os
import stat
import sys
import tempfile
import threading
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
if "ollama" not in sys.modules:                       # config imports it; not needed here
    try:
        import ollama  # noqa: F401
    except Exception:
        sys.modules["ollama"] = types.ModuleType("ollama")

import config                                          # noqa: E402
from providers import codex_backend as cb              # noqa: E402

TOOLS = [
    {"type": "function", "function": {"name": "execute_terminal_command", "description": "run",
                                      "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "list_directory", "description": "ls",
                                      "parameters": {"type": "object", "properties": {}}}},
]


def _make_launcher(tmp):
    fake = os.path.join(HERE, "fake_codex_app_server.py")
    if os.name == "nt":
        p = os.path.join(tmp, "fake_codex.cmd")
        with open(p, "w") as f:
            f.write(f'@echo off\r\nif "%1"=="--version" (echo codex-cli 0.0.0-fake& exit /b 0)\r\n'
                    f'"{sys.executable}" "{fake}" %*\r\n')
    else:
        p = os.path.join(tmp, "fake_codex")
        with open(p, "w") as f:
            f.write(f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo codex-cli 0.0.0-fake; exit 0; fi\n'
                    f'exec "{sys.executable}" "{fake}" "$@"\n')
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return p


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "log.jsonl")
        for k in ("FAKE_CODEX_SIGNED_OUT", "FAKE_CODEX_BAD_STARTUP"):
            os.environ.pop(k, None)
        os.environ["FAKE_CODEX_LOG"] = self.log
        self._saved = {k: getattr(config, k) for k in (
            "CODEX_BIN", "CODEX_SCRATCH_DIR", "CODEX_MAX_RETRIES", "CODEX_STEP_TIMEOUT",
            "CODEX_IDLE_TIMEOUT", "CODEX_MODEL", "CODEX_STARTUP_CHECK", "CODEX_FALLBACK_PROVIDER")}
        config.CODEX_BIN = _make_launcher(self.tmp)
        config.CODEX_SCRATCH_DIR = os.path.join(self.tmp, "scratch")
        config.CODEX_MAX_RETRIES = 1
        config.CODEX_STARTUP_CHECK = False
        config.CODEX_MODEL = ""
        cb._USAGE_LIMITED_UNTIL = 0.0
        self._sleep = cb._sleep_abortable
        cb._sleep_abortable = lambda s: None
        cb.shutdown_codex()

    def tearDown(self):
        cb.shutdown_codex()
        cb._sleep_abortable = self._sleep
        for k, v in self._saved.items():
            setattr(config, k, v)
        os.environ.pop("FAKE_CODEX_LOG", None)

    def requests(self, method=None):
        if not os.path.exists(self.log):
            return []
        rows = [json.loads(l) for l in open(self.log, encoding="utf-8") if l.strip()]
        return [r for r in rows if method is None or r["method"] == method]

    def step(self, user_text, tools=TOOLS, history=None):
        msgs = [{"role": "system", "content": "SYS PROMPT"}] + (history or []) + \
               [{"role": "user", "content": user_text}]
        return cb.codex_step(msgs, tools_schema=tools)


class HistoryConverter(unittest.TestCase):
    def test_system_dropped_and_final_user_split(self):
        sysx, items, final = cb.messages_to_codex_input([
            {"role": "system", "content": "A"}, {"role": "system", "content": "B"},
            {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "do it"}])
        self.assertEqual(sysx, "A\n\nB")
        self.assertEqual([i["role"] for i in items], ["user", "assistant"])
        self.assertEqual(final[0]["text"], "do it")

    def test_tool_pairing_and_call_ids(self):
        _, items, _ = cb.messages_to_codex_input([
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "a", "arguments": {"x": 1}}}]},
            {"role": "tool", "content": "out1"},
            {"role": "user", "content": "[SYSTEM] step"},
            {"role": "assistant", "content": "ok", "tool_calls": [{"function": {"name": "b", "arguments": "{}"}}]},
            {"role": "tool", "content": "out2"},
            {"role": "user", "content": "[SYSTEM] step 2"}])
        calls = [i for i in items if i["type"] == "function_call"]
        outs = [i for i in items if i["type"] == "function_call_output"]
        self.assertEqual([c["call_id"] for c in calls], ["midum_call_0", "midum_call_1"])
        self.assertEqual([o["call_id"] for o in outs], ["midum_call_0", "midum_call_1"])
        self.assertEqual(json.loads(calls[0]["arguments"]), {"x": 1})
        self.assertEqual(outs[1]["output"], "out2")

    def test_images_and_rename(self):
        _, items, final = cb.messages_to_codex_input([
            {"role": "user", "content": "look", "images": ["AAAA"]},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "mcp.tool", "arguments": {}}}]},
            {"role": "tool", "content": "shot", "images": ["/9j/BBBB"]},
            {"role": "user", "content": "next"}], rename={"mcp.tool": "mcp_tool"})
        self.assertEqual(items[0]["content"][1]["image_url"], "data:image/png;base64,AAAA")
        self.assertEqual([i for i in items if i["type"] == "function_call"][0]["name"], "mcp_tool")
        self.assertTrue(items[-1]["content"][1]["image_url"].startswith("data:image/jpeg;base64,"))

    def test_dangling_call_is_closed_and_missing_user_added(self):
        _, items, final = cb.messages_to_codex_input([
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "a", "arguments": {}}}]}])
        self.assertEqual(items[-1]["type"], "function_call_output")
        self.assertEqual(final[0]["text"], "Continue.")

    def test_text_mode_has_no_function_items(self):
        _, items, _ = cb.messages_to_codex_input([
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "a", "arguments": {"k": 1}}}]},
            {"role": "tool", "content": "res"}, {"role": "user", "content": "n"}], mode="text")
        self.assertTrue(all(i["type"] == "message" for i in items))
        self.assertIn("[tool call] a(", items[1]["content"][0]["text"])
        self.assertIn("[tool result] res", items[2]["content"][0]["text"])

    def test_dynamic_tool_names_stable_and_sanitized(self):
        t = TOOLS + [{"type": "function", "function": {"name": "mcp.fs/read", "description": "d"}}]
        defs, name_map, rename = cb._to_dynamic_tools(t)
        self.assertEqual([d["name"] for d in defs], ["execute_terminal_command", "list_directory", "mcp_fs_read"])
        self.assertEqual(name_map["mcp_fs_read"], "mcp.fs/read")
        self.assertEqual(cb._to_dynamic_tools(t)[0], defs)          # deterministic -> cacheable prefix


class StepRunner(_Base):
    def test_tool_call_step(self):
        r = self.step("SCENARIO:tool", history=[{"role": "user", "content": "earlier"},
                      {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "list_directory", "arguments": {}}}]},
                      {"role": "tool", "content": "files"}])
        m = r["message"]
        self.assertEqual(m["content"], "Let me check that.")
        self.assertEqual(m["tool_calls"][0]["function"]["name"], "execute_terminal_command")
        self.assertEqual(m["tool_calls"][0]["function"]["arguments"], {"command": "echo hi"})
        start = self.requests("thread/start")[0]["params"]
        self.assertTrue(start["ephemeral"])
        self.assertEqual(start["personality"], "none")
        self.assertEqual(start["developerInstructions"], "SYS PROMPT")
        self.assertEqual([d["name"] for d in start["dynamicTools"]], ["execute_terminal_command", "list_directory"])
        inj = self.requests("thread/inject_items")[0]["params"]["items"]
        self.assertEqual([i["type"] for i in inj], ["message", "function_call", "function_call_output"])
        self.assertEqual(len(self.requests("turn/interrupt")), 1)   # stopped after the first call

    def test_parallel_calls_keep_only_first(self):
        r = self.step("SCENARIO:parallel")
        self.assertEqual(len(r["message"]["tool_calls"]), 1)
        self.assertEqual(r["message"]["tool_calls"][0]["function"]["name"], "list_directory")

    def test_plain_answer(self):
        r = self.step("SCENARIO:answer")
        self.assertEqual(r["message"]["content"], "All done.")
        self.assertEqual(r["message"]["tool_calls"], [])
        self.assertEqual(self.requests("turn/interrupt"), [])

    def test_leak_tripwire(self):
        with self.assertRaises(cb.CodexLeak):
            self.step("SCENARIO:leak")
        self.assertEqual(len(self.requests("turn/interrupt")), 1)

    def test_approval_is_declined_and_trips_wire(self):
        with self.assertRaises(cb.CodexLeak):
            self.step("SCENARIO:approval")

    def test_usage_limit_not_retried(self):
        with self.assertRaises(cb.CodexUsageLimit):
            self.step("SCENARIO:limit")
        self.assertEqual(len(self.requests("thread/start")), 1)     # no retry
        with self.assertRaises(cb.CodexUsageLimit):                 # cooldown short-circuits
            self.step("SCENARIO:answer")
        self.assertEqual(len(self.requests("thread/start")), 1)

    def test_crash_retried_then_restarts(self):
        with self.assertRaises(cb.CodexTransient):
            self.step("SCENARIO:crash")
        self.assertEqual(self.step("SCENARIO:answer")["message"]["content"], "All done.")

    def test_empty_response_is_transient(self):
        with self.assertRaises(cb.CodexTransient):
            self.step("SCENARIO:empty")
        self.assertEqual(len(self.requests("thread/start")), 2)     # 1 try + 1 retry

    def test_abort(self):
        from state import _abort_event
        threading.Timer(0.6, _abort_event.set).start()
        try:
            with self.assertRaises(cb.CodexAborted):
                self.step("SCENARIO:hang")
        finally:
            _abort_event.clear()

    def test_idle_timeout(self):
        config.CODEX_IDLE_TIMEOUT = 1
        try:
            with self.assertRaises(cb.CodexTransient):
                self.step("SCENARIO:hang")
        finally:
            config.CODEX_IDLE_TIMEOUT = 120

    def test_signed_out_fails_clearly(self):
        os.environ["FAKE_CODEX_SIGNED_OUT"] = "1"
        with self.assertRaises(cb.CodexUnavailable) as c:
            self.step("SCENARIO:answer")
        self.assertIn("sign", str(c.exception).lower())

    def test_startup_check_passes_and_fails_closed(self):
        config.CODEX_STARTUP_CHECK = True
        self.assertEqual(self.step("SCENARIO:answer")["message"]["content"], "All done.")
        cb.shutdown_codex()
        os.environ["FAKE_CODEX_BAD_STARTUP"] = "1"
        with self.assertRaises(cb.CodexUnavailable):
            self.step("SCENARIO:answer")

    def test_version_pin_fails_closed(self):
        config.CODEX_PINNED_VERSION = "9.9.9"
        try:
            with self.assertRaises(cb.CodexUnavailable):
                self.step("SCENARIO:answer")
        finally:
            config.CODEX_PINNED_VERSION = ""

    def test_cost_loop_records_usage(self):
        cb._USAGE_LOG.clear()
        for i in range(10):
            self.step("SCENARIO:answer")
        log = cb.get_codex_usage_log()
        self.assertEqual(len(log), 10)
        self.assertTrue(all(e["cached"] <= e["input"] for e in log))

    def test_models_and_status(self):
        self.assertEqual(cb.codex_known_models()[0], "gpt-fake")
        st = cb.codex_status(with_limits=True)
        self.assertTrue(st["signed_in"] and st["plan"] == "plus" and "rate_limits" in st)


class PrimaryEntryPoint(_Base):
    def _stub_modules(self, fallback):
        sys.modules["permissions"] = types.SimpleNamespace(filter_tools_schema=lambda t: t)
        sys.modules["tools_registry"] = types.SimpleNamespace(get_tools_for_model=lambda p, m: TOOLS)
        sys.modules["orchestration"] = types.SimpleNamespace(_call_primary_model=fallback)

    def tearDown(self):
        for m in ("permissions", "tools_registry", "orchestration"):
            sys.modules.pop(m, None)
        super().tearDown()

    def test_ok_contract(self):
        import queue
        self._stub_modules(lambda *a, **k: None)
        q = queue.Queue()
        cb._call_codex_primary([{"role": "user", "content": "SCENARIO:tool"}], q)
        kind, payload = q.get(timeout=5)
        self.assertEqual(kind, "ok")
        self.assertEqual(payload["message"]["tool_calls"][0]["function"]["name"], "execute_terminal_command")

    def test_usage_limit_routes_to_fallback(self):
        import queue
        seen = {}

        def fb(messages, result_q, provider_override=None, **kw):
            seen["p"] = provider_override
            result_q.put(("ok", {"message": {"role": "assistant", "content": "from fallback", "tool_calls": []}}))
        self._stub_modules(fb)
        config.CODEX_FALLBACK_PROVIDER = "gemini_api"
        q = queue.Queue()
        cb._call_codex_primary([{"role": "user", "content": "SCENARIO:limit"}], q)
        kind, payload = q.get(timeout=5)
        self.assertEqual((kind, seen["p"]), ("ok", "gemini_api"))

    def test_leak_surfaces_as_error(self):
        import queue
        self._stub_modules(lambda *a, **k: None)
        q = queue.Queue()
        cb._call_codex_primary([{"role": "user", "content": "SCENARIO:leak"}], q)
        kind, payload = q.get(timeout=5)
        self.assertEqual(kind, "err")
        self.assertIsInstance(payload, cb.CodexLeak)


if __name__ == "__main__":
    unittest.main()
