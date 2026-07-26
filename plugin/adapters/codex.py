"""Codex adapter.

Codex has NO per-tool permission flag — you cannot tell codex "only Read/Grep".
Its tool set is fixed and its only permission lever is the sandbox mode. So we do
not translate the agent's tools here (codex ignores tool lists) and we add no
read-only restriction of our own: codex runs with its own default, exactly as it
would if the agent were opened natively in codex. If it writes, that is codex's
native behaviour, not our policy. Output schema is enforced at the CLI level."""

import os
import subprocess
import tempfile

from adapters.base import Adapter


class CodexAdapter(Adapter):
    name = "codex-exec"  # Level-1 flattened fallback; "codex" is the app-server L2
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

    def run(self, prompt, path, schema_file, ir=None, task=None):
        # No -s restriction: codex has no per-tool grant, so it runs with its own
        # default sandbox — same as opening the agent natively in codex.
        outfile = tempfile.NamedTemporaryFile("r", suffix=".json", delete=False).name
        cmd = [
            "codex", "exec",
            "--skip-git-repo-check",
            "-C", path,
            "--output-schema", schema_file,
            "-o", outfile,
            prompt,
        ]
        # codex exec waits on stdin if it looks piped; give it immediate EOF.
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                              stdin=subprocess.DEVNULL)
        raw = ""
        if os.path.exists(outfile):
            raw = open(outfile, encoding="utf-8").read().strip()
        if not raw:
            raw = proc.stdout.strip()
        return raw, proc.stderr.strip(), proc.returncode

    def provenance(self, raw):
        return {
            "target": self.name,
            "permission_source": ("codex has no per-tool grant; runs with codex default "
                                  "(agent tool restriction not expressible on codex)"),
            "schema_enforced": True,
        }
