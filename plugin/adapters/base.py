"""Adapter interface. One subclass per target CLI, each fully self-contained.

Adding a new CLI = add one file here + register it in __init__.py. The core is
never touched. Everything a model provides or cannot provide is expressed here:
its capability map, how it runs headless + read-only, and how its output is
shaped back into findings-v1.
"""

import os
import signal
import shutil

import core


class PreflightError(Exception):
    pass


def kill_tree(proc):
    """Terminate a subprocess AND its children (its whole process group) with a
    SIGKILL fallback, so no CLI leaves a grandchild running (observed with codex
    app-server: a node wrapper spawns the real binary). Requires the process to
    have been started with start_new_session=True. Safe to call more than once."""
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


class Adapter:
    name = "base"
    binary = None
    # neutral capability -> {"mode": native|equivalent|restricted|disabled, "note": ...}
    capabilities = {}

    def preflight(self):
        binp = shutil.which(self.binary or self.name)
        if not binp:
            core.die(f"{self.name} CLI not found — please install and login first.")
        return binp

    def run(self, prompt, path, schema_file, ir=None, task=None):
        """Return (raw_stdout_or_capture, stderr, returncode). Must run headless
        and read-only. Never falls back to an API.

        prompt = Level-1 flattened persona+task (role-play fallback).
        ir + task = Level-2 native path: `ir` is the agent definition to hand to
        the target's OWN --agent runtime, `task` is scope + output contract only.
        Native adapters use ir/task; others ignore them and use prompt."""
        raise NotImplementedError

    def parse(self, raw):
        """Normalize this CLI's output to {"findings": [...]} or None.
        Default is the generic recovery; override only for target-specific
        envelopes."""
        return core.generic_findings(raw)

    def explain_empty(self, raw):
        """Optional: a target-specific reason when parse() returns None."""
        return ""

    def provenance(self, raw):
        """Optional run trace for auditability (turns, tokens, mode, etc.).

        Model-specific. Core only prints/stores whatever the adapter returns.
        Empty dict = this target does not expose provenance yet.
        """
        return {}
