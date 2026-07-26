"""Grok adapter.

Grok wraps headless output in a JSON envelope:
  { text, stopReason, structuredOutput, structuredOutputError, usage, num_turns, ... }

===========================================================================
ROOT CAUSES of Cancelled / empty structuredOutput (proven with --debug-file)
===========================================================================

1) PermissionCancelled under --permission-mode plan
   Debug line (sample security review):
     cancellationCategory=PermissionCancelled
   while the model requested tool name='run_terminal_command'.
   Plan mode does not soft-deny the tool and continue — it cancels the *entire*
   headless prompt. Result: stopReason=Cancelled, structuredOutput=null,
   text often only empty {"findings":[]} placeholders.
   Successful control: same prompt with --permission-mode bypassPermissions
   → EndTurn, structuredOutput with findings.

2) max-turns ceiling on heavy multi-file reviews
   CLI messages: "max-turns limit reached, stopping" / "turn ended: max_turns reached".
   With --json-schema, after tool use Grok requires a final StructuredOutput
   tool turn. Default headless max-turns is low (~5). Hermès security-auditor
   stopped at num_turns=5 mid-draft. Fix: --max-turns high enough for review.

Read-only without plan:
  Use bypassPermissions (tools can run) + --disallowed-tools for write tools
  + prompt contract "do not modify files". Plan mode is unsafe for this product
  because one wrong tool choice aborts the whole run.

Other quirks still owned here:
- empty {"findings":[]} may appear in text before real findings
- Cancelled provisional empty findings must never be reported as clean 0
- stdin=DEVNULL
- large prompts via --prompt-file
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time

import core
from adapters.base import Adapter, kill_tree

# Ceiling, not a target. Multi-file review + final StructuredOutput needs headroom.
_MAX_TURNS = 40

# Write tools removed so we stay review-only without plan-mode full-cancel.
# Names are grok internal tool ids (see headless --disallowed-tools docs).
_DISALLOWED_TOOLS = "search_replace"

_MAX_ATTEMPTS = 3
_RETRY_SLEEP_S = 1.5
_WORKLOAD_MIN_TURNS = 3
_WORKLOAD_MIN_TOKENS = 40000

# Agent tool (Claude name) -> grok's own built-in tool name, for --tools.
# This is the whole job: translate the agent's tools; the target enforces.
_CLAUDE_TO_GROK_TOOL = {
    "Read": "read_file",
    "Grep": "grep",
    "Glob": "glob",
    "Bash": "run_terminal_command",
    "PowerShell": "run_terminal_command",
    "Edit": "search_replace",
    "Write": "write",
    "NotebookEdit": "write",
    "WebSearch": "web_search",
    "WebFetch": "web_fetch",
    "Agent": "task",
    "TodoWrite": "todo_write",
}


class GrokAdapter(Adapter):
    name = "grok"
    binary = "grok"
    capabilities = {
        "read_files":        {"mode": "native", "native": "read_file",             "note": "read_file"},
        "search_repository": {"mode": "native", "native": "grep / glob / list_dir", "note": "search"},
        "run_shell":         {"mode": "native", "native": "run_terminal_command",   "note": "bash class"},
        "write_files":       {"mode": "native", "native": "search_replace / write",  "note": "edit class"},
        "web_search":        {"mode": "native", "native": "web_search",             "note": "websearch class"},
        "web_fetch":         {"mode": "native", "native": "web_fetch",              "note": "webfetch class"},
        "subagents":         {"mode": "native", "native": "task",                   "note": "subagent"},
        "todo":              {"mode": "native", "native": "todo_write",             "note": "todo"},
    }

    def __init__(self):
        self._last_meta = {}
        self._native = False
        self._granted_tools = ""

    def run(self, prompt, path, schema_file, ir=None, task=None):
        schema_inline = json.dumps(json.load(open(schema_file)))
        self._native = bool(ir and task)
        invoke_text = task if self._native else prompt
        agent_meta = ir if self._native else None
        last = ("", "", 1)
        attempts_used = 0
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            attempts_used = attempt
            raw, err, rc = self._invoke(invoke_text, path, schema_inline, agent_meta)
            last = (raw, err, rc)
            env = core.extract_json(raw)
            if self._envelope_complete(env):
                self._last_meta = {
                    "attempts": attempt,
                    "max_attempts": _MAX_ATTEMPTS,
                    "max_turns": _MAX_TURNS,
                    "permission_mode": "bypassPermissions",
                    "retry_reason": None,
                }
                if attempt > 1:
                    note = f"[grok adapter] succeeded on attempt {attempt}/{_MAX_ATTEMPTS}"
                    err = (err + "\n" + note).strip() if err else note
                return raw, err, rc

            if not self._is_transient_incomplete(env):
                self._last_meta = {
                    "attempts": attempt,
                    "max_attempts": _MAX_ATTEMPTS,
                    "max_turns": _MAX_TURNS,
                    "permission_mode": "bypassPermissions",
                    "retry_reason": "skipped_non_transient_cancel",
                    "cancel_class": self._cancel_class(env),
                }
                return raw, err, rc

            if attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_SLEEP_S)

        self._last_meta = {
            "attempts": attempts_used,
            "max_attempts": _MAX_ATTEMPTS,
            "max_turns": _MAX_TURNS,
            "permission_mode": "bypassPermissions",
            "retry_reason": "exhausted_transient_retries",
            "cancel_class": "transient",
        }
        return last

    def _p(self, msg):  # live progress -> stderr, so a watching panel sees grok work
        print(f"  {msg}", file=sys.stderr, flush=True)

    def _invoke(self, text, path, schema_inline, ir=None):
        prompt_path = None
        agent_path = None
        proc = None
        try:
            fd, prompt_path = tempfile.mkstemp(prefix="multi-agent-grok-", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            cmd = ["grok"]
            if ir:
                # NATIVE: grok loads the agent-definition file (persona/model).
                agent_path = self._write_agent_file(ir)
                cmd += ["--agent", agent_path]
                grok_tools = self._grok_tools(ir)
                self._granted_tools = grok_tools
                if grok_tools:
                    # The agent's OWN tools, translated to grok names. This IS the
                    # permission — we add no read-only/sandbox restriction.
                    cmd += ["--tools", grok_tools]
            cmd += [
                "--prompt-file", prompt_path,
                # bypassPermissions is only non-interactive plumbing (avoids the
                # headless PermissionCancelled); the granted --tools are the permission.
                "--permission-mode", "bypassPermissions",
                "--cwd", path,
                "--max-turns", str(_MAX_TURNS),
                # Both together (grok confirmed): schema-enforced final result in the
                # `end` event, AND live NDJSON progress we stream to stderr.
                "--json-schema", schema_inline,
                "--output-format", "streaming-json",
            ]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, stdin=subprocess.DEVNULL, bufsize=1,
                start_new_session=True,   # own process group -> killable as a tree
            )
            result = {"end": None}
            buf = {"thought": "", "wrote": False}

            def flush_thought(force=False):
                s = buf["thought"].strip().replace("\n", " ")
                if s and (force or len(s) >= 70 or s[-1] in ".!?:"):
                    self._p("grok ▸ " + s[:120])
                    buf["thought"] = ""

            def reader():
                # thought deltas arrive token-by-token — buffer into readable lines
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    t = ev.get("type")
                    if t == "thought":
                        buf["thought"] += (ev.get("data") or "")
                        flush_thought()
                    elif t == "text":
                        if not buf["wrote"]:
                            buf["wrote"] = True
                            flush_thought(force=True)
                            self._p("grok ▸ writing findings…")
                    elif t in ("end", "error"):
                        result["end"] = ev  # end == the old --output-format json envelope

            th = threading.Thread(target=reader, daemon=True)
            th.start()
            deadline = time.time() + 900   # big multi-file audits are slow
            while result["end"] is None and proc.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            th.join(timeout=2)

            end = result["end"] or {}
            raw = json.dumps(end) if end else ""
            return raw, "", (0 if end else 1)
        finally:
            kill_tree(proc)   # kill the whole grok tree — no orphan on return/timeout
            for p in (prompt_path, agent_path):
                if p and os.path.exists(p):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

    def _grok_tools(self, ir):
        """The agent's own tools, translated to grok's native tool names for
        --tools. Unmappable tools are simply not granted (reported by the gate)."""
        names = []
        for t in ir.get("tools", []):
            g = _CLAUDE_TO_GROK_TOOL.get(t)
            if g and g not in names:
                names.append(g)
        return ",".join(names)

    def _write_agent_file(self, ir):
        """Translate the portable IR into grok's own agent-definition format
        (frontmatter + body). Permission is expressed by the granted --tools,
        not by any read-only mode we add."""
        desc = ir.get("description") or f"{ir['name']} review agent"
        desc_lines = desc.splitlines() or [desc]
        body = ir.get("instructions", "")
        content = (
            "---\n"
            f"name: {ir['name']}\n"
            "description: |\n"
            + "".join(f"  {ln}\n" for ln in desc_lines)
            + "model: inherit\n"
            "---\n"
            f"{body}\n"
        )
        fd, p = tempfile.mkstemp(prefix="multi-agent-grok-agent-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def _envelope_complete(self, env):
        if not isinstance(env, dict):
            return False
        so = env.get("structuredOutput")
        if isinstance(so, dict) and isinstance(so.get("findings"), list):
            return True
        stop = env.get("stopReason")
        if stop == "Cancelled":
            return False
        if stop == "EndTurn":
            return True
        return False

    def _is_transient_incomplete(self, env):
        if not isinstance(env, dict):
            return True
        if env.get("stopReason") != "Cancelled":
            return True
        turns = env.get("num_turns") or 0
        usage = env.get("usage") or {}
        tokens = usage.get("total_tokens") or 0
        if turns >= _WORKLOAD_MIN_TURNS or tokens >= _WORKLOAD_MIN_TOKENS:
            return False
        return True

    def _cancel_class(self, env):
        if not isinstance(env, dict):
            return "unknown"
        turns = env.get("num_turns") or 0
        if turns >= _MAX_TURNS and env.get("structuredOutput") is None:
            return "max_turns_exhausted"
        if turns >= 3 and env.get("structuredOutput") is None:
            return "aborted_before_structured_output"
        if not self._is_transient_incomplete(env):
            return "non_transient"
        return "transient"

    def parse(self, raw):
        env = core.extract_json(raw)
        if isinstance(env, dict):
            so = env.get("structuredOutput")
            if isinstance(so, dict) and isinstance(so.get("findings"), list):
                return so
            if env.get("stopReason") == "Cancelled":
                return None
            deep = core.locate_findings(env)
            if deep is not None:
                return deep
        return core.generic_findings(raw)

    def explain_empty(self, raw):
        env = core.extract_json(raw)
        if not isinstance(env, dict):
            return ""
        bits = []
        stop = env.get("stopReason")
        if stop:
            bits.append(f"stopReason={stop}")
        if env.get("structuredOutputError"):
            bits.append(str(env["structuredOutputError"]))
        if env.get("num_turns") is not None:
            bits.append(f"turns={env['num_turns']}")
        usage = env.get("usage") or {}
        if usage.get("total_tokens"):
            bits.append(f"tokens={usage['total_tokens']}")
        if stop == "Cancelled":
            cls = self._cancel_class(env)
            bits.append(f"cancel_class={cls}")
            if cls == "max_turns_exhausted":
                bits.append(f"hit --max-turns={_MAX_TURNS} before StructuredOutput")
        meta = getattr(self, "_last_meta", None) or {}
        if meta.get("attempts"):
            bits.append(f"attempts={meta['attempts']}/{meta.get('max_attempts', '?')}")
        return ("grok: " + "; ".join(bits)) if bits else ""

    def provenance(self, raw):
        env = core.extract_json(raw)
        prov = {
            "target": self.name,
            "invocation": ("native --agent (subagent runtime)"
                           if getattr(self, "_native", False)
                           else "flattened prompt (role-play)"),
            "permission_mode": "bypassPermissions (plumbing only)",
            "granted_tools": getattr(self, "_granted_tools", ""),
            "permission_source": "agent's own tools → grok --tools (no added restriction)",
            "schema_enforced": True,
            "max_turns": _MAX_TURNS,
        }
        meta = getattr(self, "_last_meta", None) or {}
        for k in ("attempts", "max_attempts", "cancel_class", "retry_reason"):
            if meta.get(k) is not None:
                prov[k] = meta[k]
        if not isinstance(env, dict):
            return prov
        if env.get("stopReason") is not None:
            prov["stop_reason"] = env["stopReason"]
        if env.get("num_turns") is not None:
            prov["turns"] = env["num_turns"]
        if env.get("stopReason") == "Cancelled":
            prov["cancel_class"] = self._cancel_class(env)
        if env.get("structuredOutputError"):
            prov["structured_output_error"] = env["structuredOutputError"]
        usage = env.get("usage") or {}
        if usage:
            prov["tokens"] = {
                "input": usage.get("input_tokens"),
                "output": usage.get("output_tokens"),
                "reasoning": usage.get("reasoning_tokens"),
                "cache_read": usage.get("cache_read_input_tokens"),
                "total": usage.get("total_tokens"),
            }
        if env.get("total_cost_usd") is not None:
            prov["usage_telemetry_usd"] = env["total_cost_usd"]
            prov["usage_telemetry_note"] = (
                "CLI-reported equivalent usage; subscription/quota, not a direct card charge"
            )
        model_usage = env.get("modelUsage") or {}
        if model_usage:
            prov["models"] = list(model_usage.keys())
        thought = env.get("thought") or ""
        if thought:
            prov["had_reasoning"] = True
            prov["reasoning_chars"] = len(thought)
        text = env.get("text") or ""
        if text.strip() in (
            '{ "findings": [] }',
            '{"findings":[]}',
            '{ "findings": [] }{ "findings": [] }',
        ):
            prov["text_was_empty_findings_placeholder"] = True
        return prov
