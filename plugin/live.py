#!/usr/bin/env python3
"""
Persistent, steerable cross-model session with LIVE mid-turn steering.

    python3 live.py --target codex --agent <file.md> --path <dir> \
                    --focus-file <task.txt> --control <control.txt>

Runs the agent on the target in a PERSISTENT native session (not a one-shot).
- Streams progress to stdout as it happens.
- Watches --control for lines the host appends. Each new line is forwarded LIVE
  into the running turn (codex `turn/steer`); if no turn is active it starts a
  new turn with that message. A line `__STOP__` ends the session.
- Cleanup is guaranteed: on ANY exit (STOP, parent death, signal, error) the
  underlying model process is terminated — no orphaned background processes.

Only stdlib. The target CLI runs locally (no API).
"""

import argparse
import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def _looks_like_json(s):
    """Heuristic: has this assistant message turned into the findings JSON?
    Used to stop surfacing an agent's spoken reply once it starts emitting the
    structured output (which is captured separately for extraction)."""
    t = s.lstrip()
    if t[:1] in ("{", "[") or t.startswith("```"):
        return True
    return '{"findings"' in "".join(s.split())


def _kill_tree(proc):
    """Terminate the model process AND its children (own process group), with a
    SIGKILL fallback — so nothing (grok leader, codex app-server, shells) is left."""
    if not proc:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=3)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# codex: persistent app-server session with turn/steer
# --------------------------------------------------------------------------- #
class CodexLiveSession:
    def __init__(self, instructions, cwd, schema):
        self.instructions = instructions
        self.cwd = cwd
        self.schema = schema
        self.proc = None
        self._next = 1
        self._resp = {}
        self._lock = threading.Lock()
        self.thread_id = None
        self.turn_id = None
        self.turn_active = False
        self._final = {}
        self._final_item = None
        self._phase = {}        # itemId -> agentMessage phase
        self._say = ""          # codex's spoken reply (non-final agentMessage)
        self._say_done = False
        self._thought = ""      # codex's live reasoning summary (its "thinking")
        self.last_findings = None

    # --- jsonrpc plumbing ---
    def _send(self, obj):
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, AttributeError):
            pass

    def _req(self, method, params=None):
        with self._lock:
            rid = self._next
            self._next += 1
        m = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params is not None:
            m["params"] = params
        self._send(m)
        return rid

    def _notify(self, method, params=None):
        m = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            m["params"] = params
        self._send(m)

    def _wait(self, rid, timeout):
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if rid in self._resp:
                    return self._resp[rid]
            time.sleep(0.03)
        return None

    def _flush_thought(self, force=False):
        """Surface codex's live REASONING SUMMARY (its 'thinking') as it works, so
        the agent visibly reasons underneath — the same live feel as grok. This is
        a separate channel from the findings JSON and is never captured as output."""
        s = self._thought.strip().replace("\n", " ")
        if s and (force or len(s) >= 70 or s[-1] in ".!?:)*"):
            log("  codex ▸ " + s[:200])
            self._thought = ""

    def _flush_say(self, force=False):
        """Surface codex's SPOKEN reply (a non-final agentMessage answering a
        mid-turn question) live, so the host can relay it verbatim."""
        if self._say_done:
            self._say = ""
            return
        if _looks_like_json(self._say):
            self._say_done = True
            self._say = ""
            return
        s = self._say.strip().replace("\n", " ")
        if s and (force or len(s) >= 60 or s[-1] in ".!?:)"):
            log("  codex ▸ 💬 " + s[:400])
            self._say = ""

    def _on(self, m):
        method, mid = m.get("method"), m.get("id")
        if method is None and mid is not None:
            with self._lock:
                self._resp[mid] = m.get("result", m.get("error"))
            return
        if method and mid is not None:  # server request -> auto-approve (no policy)
            if "Approval" in method or "approval" in method:
                self._send({"jsonrpc": "2.0", "id": mid, "result": {"decision": "approved"}})
            elif "requestUserInput" in method or "elicitation" in method:
                self._send({"jsonrpc": "2.0", "id": mid, "result": {}})
            return
        p = m.get("params") or {}
        if method == "item/reasoning/summaryTextDelta":
            # codex's live reasoning summary streams here token-by-token
            self._thought += p.get("delta") or ""
            self._flush_thought()
            return
        if method == "item/reasoning/summaryPartAdded":
            # a new reasoning paragraph begins — flush the current one so they split
            self._flush_thought(force=True)
            return
        if method == "item/started":
            it = p.get("item") or {}
            t = it.get("type")
            if t == "commandExecution" and it.get("command"):
                self._flush_thought(force=True)
                log("  codex ▸ " + str(it["command"])[:80])
            elif t == "agentMessage":
                self._phase[it.get("id")] = it.get("phase")
                if it.get("phase") == "final_answer":
                    log("  codex ▸ writing findings…")
                    self._final_item = it.get("id")
        elif method == "item/agentMessage/delta":
            i = p.get("itemId")
            delta = p.get("delta") or ""
            self._final[i] = self._final.get(i, "") + delta
            if self._phase.get(i) != "final_answer" and delta:
                self._say += delta        # a spoken reply, not the findings JSON
                self._flush_say()
        elif method == "item/completed":
            it = p.get("item") or {}
            if it.get("type") == "agentMessage" and it.get("phase") == "final_answer" and it.get("text"):
                self._final[it.get("id")] = it["text"]
                self._final_item = it.get("id")
        elif method == "turn/completed":
            self.turn_active = False
            self._flush_thought(force=True)  # surface any trailing reasoning
            self._flush_say(force=True)  # surface any trailing spoken reply
            raw = self._final.get(self._final_item, "") or max(self._final.values(), key=len, default="")
            self.last_findings = raw
            log("  codex ▸ turn complete")

    def _reader(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                try:
                    self._on(json.loads(line))
                except Exception:
                    pass

    # --- lifecycle ---
    def start(self):
        self.proc = subprocess.Popen(
            ["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        rid = self._req("initialize", {"clientInfo": {"name": "multi-agent-live", "title": "multi-agent-live", "version": "0.1"}})
        if self._wait(rid, 20) is None:
            raise RuntimeError("codex app-server: no initialize response")
        self._notify("initialized")
        # Turn ON codex's reasoning-summary stream so it visibly THINKS as it works
        # (item/reasoning/summaryTextDelta). This is a separate channel from the
        # findings JSON — surfaced live as `codex ▸ …`, giving codex the same
        # real-agent feel grok gets from its agent_thought_chunk stream.
        rid = self._req("thread/start", {"cwd": self.cwd, "ephemeral": True,
                                         "developerInstructions": self.instructions,
                                         "config": {"model_reasoning_effort": "high",
                                                    "model_reasoning_summary": "detailed"}})
        res = self._wait(rid, 30)
        self.thread_id = ((res or {}).get("thread") or {}).get("id")
        if not self.thread_id:
            raise RuntimeError(f"codex app-server: no threadId ({res})")
        log(f"  codex: native session started (thread {self.thread_id[:8]})")

    def start_turn(self, text):
        self._final = {}
        self._final_item = None
        self._phase = {}
        self._say = ""
        self._say_done = False
        self._thought = ""
        self.last_findings = None
        rid = self._req("turn/start", {"threadId": self.thread_id,
                                       "input": [{"type": "text", "text": text}],
                                       "outputSchema": self.schema})
        res = self._wait(rid, 15)
        self.turn_id = ((res or {}).get("turn") or {}).get("id")
        self.turn_active = True
        log("  codex: working…")

    def steer(self, text):
        if not self.turn_active:
            return False
        self._req("turn/steer", {"threadId": self.thread_id, "expectedTurnId": self.turn_id,
                                 "input": [{"type": "text", "text": text}]})
        log(f"  codex ◂ steer forwarded into running turn: {text[:60]}")
        return True

    def close(self):
        p, self.proc = self.proc, None
        _kill_tree(p)


# --------------------------------------------------------------------------- #
# grok: persistent ACP session (grok agent stdio) with mid-turn interject
# --------------------------------------------------------------------------- #
class GrokLiveSession:
    def __init__(self, instructions, cwd, schema):
        self.instructions = instructions
        self.cwd = cwd
        self.schema = schema
        self.proc = None
        self._next = 1
        self._resp = {}
        self._lock = threading.Lock()
        self.sid = None
        self.turn_active = False
        self._active_prompt = None
        self._buf = ""
        self._thought = ""
        self._say = ""          # grok's spoken reply (agent_message prose)
        self._say_done = False  # once the message turns into findings JSON, stop surfacing
        self._btw_ids = set()   # ids of mid-turn _x.ai/btw questions awaiting an answer
        self.last_findings = None

    def _send(self, obj):
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, AttributeError):
            pass

    def _req(self, method, params=None):
        with self._lock:
            rid = self._next
            self._next += 1
        m = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            m["params"] = params
        self._send(m)
        return rid

    def _notify(self, method, params=None):
        m = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            m["params"] = params
        self._send(m)

    def _wait(self, rid, timeout):
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if rid in self._resp:
                    return self._resp[rid]
            time.sleep(0.03)
        return None

    def _flush_thought(self, force=False):
        s = self._thought.strip().replace("\n", " ")
        if s and (force or len(s) >= 70 or s[-1] in ".!?:"):
            log("  grok ▸ " + s[:110])
            self._thought = ""

    def _flush_say(self, force=False):
        """Surface grok's SPOKEN reply (its answer to a mid-turn question) live,
        so the host can relay it to the user verbatim. Stop once the message
        becomes the findings JSON — that is captured in _buf for extraction."""
        if self._say_done:
            self._say = ""
            return
        if _looks_like_json(self._say):
            self._say_done = True
            self._say = ""
            return
        s = self._say.strip().replace("\n", " ")
        if s and (force or len(s) >= 60 or s[-1] in ".!?:)"):
            log("  grok ▸ 💬 " + s[:400])
            self._say = ""

    def _on(self, m):
        method, mid = m.get("method"), m.get("id")
        if method is None and mid is not None:
            result = m.get("result", m.get("error"))
            with self._lock:
                self._resp[mid] = result
            # a mid-turn _x.ai/btw question was answered -> surface it verbatim
            if mid in self._btw_ids:
                self._btw_ids.discard(mid)
                ans = None
                if isinstance(result, dict):
                    ans = result.get("answer")
                    if ans is None and isinstance(result.get("result"), dict):
                        ans = result["result"].get("answer")  # btw nests under result.result
                if ans:
                    log("  grok ▸ 💬 " + str(ans).strip().replace("\n", " ")[:800])
                else:
                    log("  grok ▸ 💬 (btw returned no answer: " + str(result)[:200] + ")")
                return
            # the ACTIVE prompt returning a stopReason == turn finished
            if mid == self._active_prompt and isinstance(result, dict) and "stopReason" in result:
                self.turn_active = False
                self._flush_say(force=True)  # surface any trailing spoken reply
                self.last_findings = self._buf
                log("  grok ▸ turn complete (" + str(result.get("stopReason")) + ")")
            return
        if method and mid is not None:  # agent -> client request
            if "request_permission" in method or "permission" in method.lower():
                # no policy: allow. Pick an allow-ish option if offered.
                p = m.get("params") or {}
                opts = p.get("options") or []
                pick = None
                for o in opts:
                    if "allow" in json.dumps(o).lower():
                        pick = o.get("optionId") or o.get("id")
                        break
                if pick is None and opts:
                    pick = opts[0].get("optionId") or opts[0].get("id")
                self._send({"jsonrpc": "2.0", "id": mid,
                            "result": {"outcome": {"outcome": "selected", "optionId": pick}}})
            else:
                self._send({"jsonrpc": "2.0", "id": mid, "result": {}})
            return
        # notifications
        p = m.get("params") or {}
        if method == "session/update":
            up = p.get("update") or {}
            kind = up.get("sessionUpdate")
            c = up.get("content") or {}
            txt = c.get("text") if isinstance(c, dict) else ""
            if kind == "agent_message_chunk" and txt:
                self._buf += txt
                self._say += txt
                self._flush_say()
            elif kind == "agent_thought_chunk" and txt:
                self._thought += txt
                self._flush_thought()
            elif kind == "tool_call":
                title = up.get("title") or (up.get("rawInput") or {}).get("command") or ""
                if title:
                    self._flush_thought(force=True)
                    log("  grok ▸ " + str(title)[:80])

    def _reader(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                try:
                    self._on(json.loads(line))
                except Exception:
                    pass

    def start(self):
        self.proc = subprocess.Popen(
            ["grok", "agent", "--always-approve", "stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, start_new_session=True,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        rid = self._req("initialize", {"protocolVersion": 1,
                                       "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
                                       "clientInfo": {"name": "multi-agent-live", "version": "0.1"}})
        if self._wait(rid, 20) is None:
            raise RuntimeError("grok agent stdio: no initialize response")
        rid = self._req("session/new", {"cwd": self.cwd, "mcpServers": [],
                                        "_meta": {"yoloMode": True, "systemPromptOverride": self.instructions}})
        res = self._wait(rid, 30)
        self.sid = (res or {}).get("sessionId")
        if not self.sid:
            raise RuntimeError(f"grok agent stdio: no sessionId ({res})")
        log(f"  grok: native session started ({self.sid[:8]})")

    def start_turn(self, text):
        self._buf = ""
        self._say = ""
        self._say_done = False
        self.last_findings = None
        rid = self._req("session/prompt", {"sessionId": self.sid,
                                           "prompt": [{"type": "text", "text": text}]})
        self._active_prompt = rid
        self.turn_active = True
        log("  grok: working…")

    def steer(self, text):
        if not self.turn_active:
            self.start_turn(text)  # no active turn -> new turn that fully acts on it
            return True
        # Turn is running. Deliver the message via _x.ai/btw: grok answers it as a
        # side Q&A (the answer arrives in the RPC result -> surfaced as 💬), while
        # the main audit turn keeps running UNDISTURBED and still completes.
        # This is reliable where interject was not: in JSON-output mode grok would
        # not emit a visible reply to an interjected message, and did not reliably
        # act on it either. (To have new instructions actually change the audit,
        # send them AFTER the findings land — that starts a fresh turn.)
        with self._lock:
            rid = self._next
            self._next += 1
        self._btw_ids.add(rid)   # register BEFORE sending: a fast reply must not race the reader
        self._send({"jsonrpc": "2.0", "id": rid, "method": "_x.ai/btw",
                    "params": {"sessionId": self.sid, "question": text}})
        log(f"  grok ◂ asked mid-turn — audit continues, answer will follow: {text[:60]}")
        return True

    def close(self):
        p, self.proc = self.proc, None
        _kill_tree(p)


# --------------------------------------------------------------------------- #
# main loop: initial turn + control-file watch + guaranteed cleanup
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, choices=["codex", "grok"])
    ap.add_argument("--agent", required=True)
    ap.add_argument("--path", default=".")
    ap.add_argument("--focus-file")
    ap.add_argument("--control", required=True, help="file the host appends steer messages to")
    ap.add_argument("--idle-timeout", type=int, default=150,
                    help="exit after this many idle seconds (no active turn, no new "
                         "control input) — backstop so nothing is left running")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import core

    ir = core.load_agent(args.agent)
    schema = core.load_schema()
    path = os.path.abspath(args.path)
    focus = open(os.path.expanduser(args.focus_file), encoding="utf-8").read() if args.focus_file else None
    task = core.compile_task(schema, path, focus=focus)

    # System instructions = the agent's OWN persona/rules + a turn-lifecycle
    # contract (mechanism, set once at session start) so mid-turn user questions
    # don't end the audit before findings land, and scope stays bounded. The
    # agent's own instructions are left intact above the clearly-labelled block.
    agent_instr = ir.get("instructions") or ""
    instructions = (agent_instr + "\n\n" + core.TURN_DISCIPLINE).strip()

    if args.target == "codex":
        session = CodexLiveSession(instructions, path, schema)
    else:
        session = GrokLiveSession(instructions, path, schema)

    # --- guaranteed cleanup: kill the model process on any exit ---
    atexit.register(session.close)

    def _sig(*_):
        session.close()
        os._exit(0)
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    # No parent-PID watchdog: the host launches us detached (nohup &), so our
    # parent is a short-lived shell, not the subagent. Cleanup is via __STOP__
    # (clean) and the idle timeout (backstop) so no process is ever orphaned.
    try:
        session.start()
        # make sure the control file exists and read from the end
        open(args.control, "a").close()
        offset = os.path.getsize(args.control)

        session.start_turn(task)
        printed = False
        last_activity = time.time()
        # dedupe: the host UI can echo a typed steer twice; collapse an exact
        # duplicate of the immediately-previous control line seen within a short
        # window so grok is steered ONCE, not twice.
        last_ctrl_text = None
        last_ctrl_time = 0.0
        DEDUPE_WINDOW = 8.0

        while True:
            # 1) drain new control lines
            size = os.path.getsize(args.control)
            if size > offset:
                with open(args.control, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    chunk = f.read()
                    offset = f.tell()
                for line in chunk.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    now = time.time()
                    if (line != "__STOP__" and line == last_ctrl_text
                            and now - last_ctrl_time < DEDUPE_WINDOW):
                        log(f"  ⋯ duplicate steer ignored (within {int(DEDUPE_WINDOW)}s): {line[:50]}")
                        continue
                    last_ctrl_text = line
                    last_ctrl_time = now
                    last_activity = now
                    if line == "__STOP__":
                        raise KeyboardInterrupt
                    if session.turn_active:
                        session.steer(line)          # LIVE mid-turn steer
                    else:
                        printed = False
                        session.start_turn(line)     # new turn with the message

            # 2) print findings when a turn finishes — extract the clean findings
            #    JSON out of any conversational chatter (ACP has no schema enforce)
            if not session.turn_active and session.last_findings and not printed:
                found = core.generic_findings(session.last_findings)
                out = json.dumps(found) if found else session.last_findings
                print(out, flush=True)
                print("__TURN_DONE__", flush=True)
                printed = True
                last_activity = time.time()

            # 3) idle safety
            if not session.turn_active and time.time() - last_activity > args.idle_timeout:
                break

            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


if __name__ == "__main__":
    main()
