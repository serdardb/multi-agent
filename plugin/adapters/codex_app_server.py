"""Codex Level-2 adapter — native via the app-server JSON-RPC protocol.

Unlike `codex exec` (a flattened one-shot prompt), this drives codex's OWN agent
runtime over `codex app-server` (stdio, newline-delimited JSON-RPC). Lifecycle,
confirmed live against codex-cli 0.145.0:

    initialize -> initialized -> thread/start(developerInstructions, cwd) ->
    turn/start(threadId, input, outputSchema) -> read item/* events ->
    assemble the final_answer agentMessage -> turn/completed

The agent's instructions become the thread's developerInstructions (native
persona). outputSchema enforces findings-v1. It is bidirectional: the server can
send approval requests, which we auto-approve (the product imposes no policy —
the agent's own tools govern; codex has no per-tool grant, so it runs with its
own default). Codex ignores a Claude-style tools array — reported by the gate.
"""

import json
import subprocess
import sys
import threading
import time

from adapters.base import Adapter, kill_tree

_TURN_TIMEOUT_S = 900   # match grok/claude ceilings; large multi-file audits are slow


class CodexAppServerAdapter(Adapter):
    name = "codex"
    binary = "codex"
    capabilities = {
        "read_files":        {"mode": "native",     "native": "read",        "note": "read tool"},
        "search_repository": {"mode": "equivalent", "native": "search",      "note": "search / shell grep"},
        "run_shell":         {"mode": "native",     "native": "shell",       "note": "CommandExecution"},
        "write_files":       {"mode": "native",     "native": "apply_patch", "note": "FileChange"},
        "web_search":        {"mode": "native",     "native": "web_search",  "note": "WebSearch"},
        "subagents":         {"mode": "native",     "native": "spawn_agent", "note": "multi-agent (app-server)"},
        "todo":              {"mode": "equivalent", "native": "update_plan", "note": "Plan"},
    }

    def __init__(self):
        self._meta = {}

    def run(self, prompt, path, schema_file, ir=None, task=None):
        schema = json.load(open(schema_file))
        instructions = (ir or {}).get("instructions") or prompt
        user_text = task or prompt

        proc = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True,   # own process group -> killable as a tree
        )
        state = {
            "next_id": 1,
            "responses": {},          # id -> result/error
            "final_by_item": {},      # itemId -> assembled text
            "final_item": None,       # itemId of the final_answer message
            "turn_done": False,
            "turn_error": None,
            "usage": None,
            "commands": 0,
            "lock": threading.Lock(),
        }

        def _p(msg):  # live progress -> stderr, so a watching panel sees codex work
            print(f"  {msg}", file=sys.stderr, flush=True)

        def send(obj):
            try:
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, ValueError):
                pass

        def request(method, params=None):
            with state["lock"]:
                rid = state["next_id"]
                state["next_id"] += 1
            msg = {"jsonrpc": "2.0", "method": method, "id": rid}
            if params is not None:
                msg["params"] = params
            send(msg)
            return rid

        def notify(method, params=None):
            msg = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            send(msg)

        def wait_response(rid, timeout):
            end = time.time() + timeout
            while time.time() < end:
                with state["lock"]:
                    if rid in state["responses"]:
                        return state["responses"][rid]
                time.sleep(0.05)
            return None

        def on_message(m):
            method = m.get("method")
            mid = m.get("id")
            # response to one of our requests
            if method is None and mid is not None:
                with state["lock"]:
                    state["responses"][mid] = m.get("result", m.get("error"))
                return
            # server-initiated request (e.g. approvals) -> auto-approve
            if method and mid is not None:
                if "Approval" in method or "approval" in method:
                    send({"jsonrpc": "2.0", "id": mid, "result": {"decision": "approved"}})
                elif "requestUserInput" in method or "elicitation" in method:
                    send({"jsonrpc": "2.0", "id": mid, "result": {}})
                return
            # notifications / events
            p = m.get("params") or {}
            if method == "item/started":
                it = p.get("item") or {}
                itype = it.get("type")
                if itype == "commandExecution" and it.get("command"):
                    _p("codex ▸ " + str(it["command"])[:90])
                elif itype == "agentMessage" and it.get("phase") == "final_answer":
                    _p("codex ▸ writing findings…")
                    state["final_item"] = it.get("id")
            elif method == "item/agentMessage/delta":
                iid = p.get("itemId")
                state["final_by_item"][iid] = state["final_by_item"].get(iid, "") + (p.get("delta") or "")
            elif method == "item/completed":
                it = p.get("item") or {}
                if it.get("type") == "agentMessage" and it.get("phase") == "final_answer" and it.get("text"):
                    state["final_by_item"][it.get("id")] = it["text"]
                    state["final_item"] = it.get("id")
                elif it.get("type") == "commandExecution":
                    state["commands"] += 1
            elif method == "thread/tokenUsage/updated":
                state["usage"] = (p.get("tokenUsage") or {}).get("total")
            elif method == "turn/completed":
                _p("codex ▸ turn complete")
                state["turn_done"] = True
            elif method == "turn/failed" or method == "turn/aborted":
                state["turn_error"] = method
                state["turn_done"] = True

        def reader():
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    on_message(json.loads(line))
                except Exception:
                    continue

        threading.Thread(target=reader, daemon=True).start()
        # drain stderr so the pipe never blocks
        threading.Thread(target=lambda: [None for _ in proc.stderr], daemon=True).start()

        try:
            rid = request("initialize", {"clientInfo": {"name": "multi-agent", "title": "multi-agent", "version": "0.1"}})
            if wait_response(rid, 20) is None:
                return "", "codex app-server: no initialize response", 1
            notify("initialized")

            rid = request("thread/start", {
                "cwd": path,
                "ephemeral": True,
                "developerInstructions": instructions,
            })
            res = wait_response(rid, 30)
            thread_id = None
            if isinstance(res, dict):
                th = res.get("thread")
                thread_id = (th or {}).get("id") or res.get("threadId")
            if not thread_id:
                return "", f"codex app-server: no threadId ({res})", 1
            _p(f"codex: native session started (thread {thread_id[:8]})")

            request("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": user_text}],
                "outputSchema": schema,
            })
            _p("codex: working…")

            end = time.time() + _TURN_TIMEOUT_S
            while not state["turn_done"] and time.time() < end:
                time.sleep(0.1)

            # Turn never finished (timed out): do NOT coerce a half-streamed
            # mid-thought into a bogus finding — report the timeout cleanly so the
            # arena records "no result" instead of garbage.
            if not state["turn_done"]:
                self._meta = {"turn_completed": False, "turn_error": "timeout",
                              "commands_run": state["commands"], "usage": state["usage"]}
                return "", f"codex turn timed out after {_TURN_TIMEOUT_S}s (scope too large for one turn)", 1

            iid = state["final_item"]
            raw = state["final_by_item"].get(iid, "")
            if not raw:
                # fall back to the largest assembled message
                raw = max(state["final_by_item"].values(), key=len, default="")
            self._meta = {
                "turn_completed": state["turn_done"],
                "turn_error": state["turn_error"],
                "commands_run": state["commands"],
                "usage": state["usage"],
            }
            rc = 0 if (raw and not state["turn_error"]) else 1
            return raw, (state["turn_error"] or ""), rc
        finally:
            # Kill the WHOLE app-server tree (node wrapper + the codex binary it
            # spawns) — terminate() alone leaves the grandchild running (observed
            # as an orphan).
            kill_tree(proc)

    def provenance(self, raw):
        m = self._meta or {}
        prov = {
            "target": self.name,
            "invocation": "native codex app-server (thread/turn, developerInstructions)",
            "schema_enforced": True,
            "permission_source": ("codex has no per-tool grant; runs with codex default "
                                  "(agent tool restriction not expressible on codex)"),
            "commands_run": m.get("commands_run"),
            "turn_completed": m.get("turn_completed"),
        }
        u = m.get("usage") or {}
        if u:
            prov["tokens"] = {"input": u.get("inputTokens"), "output": u.get("outputTokens"),
                              "total": u.get("totalTokens")}
        if m.get("turn_error"):
            prov["turn_error"] = m["turn_error"]
        return prov
