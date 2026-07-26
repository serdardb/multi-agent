"""Claude adapter.

Level-2 native path: define the agent inline with `--agents` and select it with
`--agent`, so Claude's OWN subagent runtime loads the definition and runs it as a
real subagent — not a flattened role-play prompt. The task message carries only
scope + output contract; the persona/rules come from the agent definition.
Read-only via --permission-mode plan. Falls back to the flattened prompt when no
ir/task is supplied."""

import json
import subprocess

import core
from adapters.base import Adapter, kill_tree

_TIMEOUT_S = 900   # big multi-file audits are slow; detach+poll callers exceed the Bash cap


class ClaudeAdapter(Adapter):
    name = "claude"
    binary = "claude"
    capabilities = {
        "read_files":        {"mode": "native", "native": "Read",         "note": "Read"},
        "search_repository": {"mode": "native", "native": "Grep / Glob",  "note": "Grep/Glob"},
        "run_shell":         {"mode": "native", "native": "Bash",         "note": "Bash"},
        "write_files":       {"mode": "native", "native": "Edit / Write", "note": "Edit/Write"},
        "web_search":        {"mode": "native", "native": "WebSearch",    "note": "WebSearch"},
        "web_fetch":         {"mode": "native", "native": "WebFetch",     "note": "WebFetch"},
        "subagents":         {"mode": "native", "native": "Agent",        "note": "Agent"},
        "todo":              {"mode": "native", "native": "TodoWrite",    "note": "TodoWrite"},
    }

    def __init__(self):
        self._native = False
        self._tools = ""

    def run(self, prompt, path, schema_file, ir=None, task=None):
        # We pass the agent's OWN tools and add NO read-only/plan restriction.
        # bypassPermissions is only non-interactive plumbing so headless doesn't
        # prompt — the tool set IS the permission. If a granted tool writes,
        # that's the agent's own grant, exactly as it would run natively here.
        self._tools = ", ".join(ir.get("tools", [])) if ir else ""
        if ir and task:
            # NATIVE: hand the agent definition to Claude's own subagent runtime.
            self._native = True
            agent_def = {
                "description": ir.get("description") or f"{ir['name']} review agent",
                "prompt": ir["instructions"],
            }
            if ir.get("tools"):
                agent_def["tools"] = ir["tools"]  # the agent's tools = its permission
            agents_json = json.dumps({ir["name"]: agent_def})
            cmd = [
                "claude", "-p", task,
                "--agents", agents_json,
                "--agent", ir["name"],
                "--permission-mode", "bypassPermissions",
            ]
        else:
            self._native = False
            cmd = ["claude", "-p", prompt, "--permission-mode", "bypassPermissions"]
        # Popen + own process group so a timeout kills the WHOLE claude tree
        # (no orphaned child), not just the direct process. stdin=DEVNULL: some
        # CLIs block if stdin looks piped.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=path, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            out, err = proc.communicate(timeout=_TIMEOUT_S)
            return out.strip(), err.strip(), proc.returncode
        except subprocess.TimeoutExpired:
            return "", f"claude timed out after {_TIMEOUT_S}s", 1
        finally:
            kill_tree(proc)   # guarantee nothing is left running

    def provenance(self, raw):
        return {
            "target": self.name,
            "invocation": ("native --agent (subagent runtime)"
                           if self._native else "flattened prompt (role-play)"),
            "permission_mode": "bypassPermissions (plumbing only)",
            "granted_tools": self._tools,
            "permission_source": "agent's own tools passed to subagent (no added restriction)",
            "schema_enforced": False,
            "note": "claude -p; tool trace not in default stdout",
        }
