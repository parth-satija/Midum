# tools/codex_spike.py  --  Phase 0 go/no-go for the Codex provider.
#
# Run from midum_pkg (signed in with `codex login` first):
#     python tools/codex_spike.py            # all checks
#     python tools/codex_spike.py --steps 10 # length of the cost loop
#
# It talks to the REAL `codex app-server` through providers/codex_backend.py's
# client and reports PASS / FAIL per check, plus the decision gate to apply.
# Run it again after every Codex upgrade, then update CODEX_PINNED_VERSION.
import argparse
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from providers import codex_backend as cb

RESULTS = []


def check(name, fn, gate=""):
    try:
        detail = fn()
        RESULTS.append((name, True, detail or "", gate))
        print(f"PASS  {name}  {detail or ''}")
    except Exception as e:
        RESULTS.append((name, False, f"{type(e).__name__}: {e}", gate))
        print(f"FAIL  {name}  {type(e).__name__}: {e}")


PROBE = [{"name": "echo", "description": "Echo back text.",
          "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]


def turn(srv, text, history=None, dyn=None, instructions="", **kw):
    return cb._run_turn(srv, system_text=instructions, history_items=history or [],
                        final_input=[{"type": "text", "text": text, "text_elements": []}],
                        model=config.CODEX_MODEL, effort=config.CODEX_REASONING_EFFORT,
                        dyn_tools=PROBE if dyn is None else dyn, timeout=120)


def schema_names():
    exe = cb._resolve_bin()
    out = tempfile.mkdtemp()
    # Dynamic tools / inject_items are EXPERIMENTAL API: the generator omits
    # them unless asked. Try with --experimental first, then without.
    last_err = None
    for extra in ("--experimental", None):
        cmd = [exe, "app-server", "generate-json-schema", "--out", out] + ([extra] if extra else [])
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
            last_err = None
            break
        except subprocess.CalledProcessError as e:
            last_err = (e.stderr or b"").decode("utf-8", "replace")[:200]
    if last_err:
        raise RuntimeError(f"generate-json-schema failed: {last_err}")
    blob = ""
    for root, _, files in os.walk(out):
        for f in files:
            blob += open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
    wanted = ["thread/start", "thread/inject_items", "turn/start", "turn/interrupt", "item/tool/call",
              "account/read", "account/rateLimits/read", "model/list", "dynamicTools",
              "developerInstructions", "ephemeral", "personality", "thread/tokenUsage/updated"]
    missing = [w for w in wanted if w not in blob]
    if missing:
        raise RuntimeError(f"names NOT in the generated schema (fix codex_backend.py): {missing}")
    return f"schema dir: {out}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=10)
    args = ap.parse_args()

    srv = cb._get_server()
    config.CODEX_STARTUP_CHECK = False

    check("schema: every method/field name used exists", schema_names,
          "Rename the mismatching names in providers/codex_backend.py.")

    def start():
        srv.start()
        acct = srv.request("account/read", {"refreshToken": False}).get("account")
        if not acct:
            raise RuntimeError("not signed in: run `codex login`")
        srv._signed_in = True
        return f"{srv.version}; plan={acct.get('planType')}"
    check("app-server starts, account/read works (free sign-in)", start,
          "If this fails on a free account the whole approach is a NO-GO.")

    check("ephemeral thread/start with dynamicTools accepted",
          lambda: turn(srv, "Reply with the single word OK. Do not call tools.")["text"][:40],
          "Rejected on free -> set CODEX_EPHEMERAL=False (threads archived after). "
          "Dynamic tools rejected -> STOP; fall back to a consult-only design.")

    hist = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Echo the word banana."}]},
        {"type": "function_call", "name": "echo", "arguments": json.dumps({"text": "banana"}), "call_id": "midum_call_0"},
        {"type": "function_call_output", "call_id": "midum_call_0", "output": "banana"},
    ]
    check("thread/inject_items accepts function_call + function_call_output",
          lambda: turn(srv, "What did the echo tool return? One word.", history=hist)["text"][:40],
          "Rejected -> set CODEX_HISTORY_MODE='text' (tool history rendered as text, like openai_web).")

    def tools_only():
        config.CODEX_STARTUP_CHECK = True
        srv._startup_ok = False
        cb._startup_check(srv)
        return "model sees only the dynamic tool"
    check("'list your tools' shows only dynamic tools (disable flags work)", tools_only,
          "Fails -> fix config.CODEX_DISABLE_FLAGS for this Codex version (the tripwire still protects you).")

    def interrupt_clean():
        r = turn(srv, "Call the echo tool with text 'hi', then call it again with 'there'.")
        if not r["call"]:
            raise RuntimeError("model did not call the dynamic tool")
        return f"stopped at first call: {r['call']}"
    check("interrupt after first call leaves a clean turn/completed", interrupt_clean)

    def instructions():
        r = turn(srv, "Hello!", instructions="Whatever the user says, reply with exactly the word PINEAPPLE.", dyn=[])
        if "PINEAPPLE" not in r["text"].upper():
            raise RuntimeError(f"developerInstructions ignored; got: {r['text'][:60]!r}")
        return "developerInstructions honoured"
    check("developerInstructions works", instructions,
          "Ignored -> switch thread/start to baseInstructions in codex_backend._run_turn.")

    def cost():
        cb._USAGE_LOG.clear()
        history = []
        for i in range(args.steps):
            r = turn(srv, f"Step {i + 1}: call echo with text 'step{i + 1}'.", history=history)
            if r["call"]:
                cid = f"midum_call_{i}"
                history += [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"Step {i + 1}"}]},
                    {"type": "function_call", "name": "echo", "arguments": json.dumps(r["call"][1]), "call_id": cid},
                    {"type": "function_call_output", "call_id": cid, "output": "ok"},
                ]
        rows = cb.get_codex_usage_log()
        print("\n   step   input  cached  output")
        for n, e in enumerate(rows, 1):
            print(f"   {n:>4} {e['input']:>7} {e['cached']:>7} {e['output']:>7}")
        tot_in, tot_c = sum(e["input"] for e in rows), sum(e["cached"] for e in rows)
        pct = (100 * tot_c / tot_in) if tot_in else 0
        return f"{len(rows)} steps, {tot_in} input tokens, {pct:.0f}% cached  <-- THE KEY NUMBER"
    check(f"token usage / prefix caching over a {args.steps}-step loop", cost,
          "Low cached % or fast-growing input -> Phase 5 (reasoning re-injection, warm process, persistent threads).")

    print("\n=== SUMMARY ===")
    for name, ok, detail, gate in RESULTS:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok and gate:
            print("      GATE:", gate)
    cb.shutdown_codex()
    sys.exit(0 if all(ok for _, ok, _, _ in RESULTS) else 1)


if __name__ == "__main__":
    main()
