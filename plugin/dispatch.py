#!/usr/bin/env python3
"""
multi-agent dispatch — run your OWN agent on another CLI's real session,
headless + read-only, and normalize the result into a common contract.

    python3 dispatch.py --agent security-guard --target codex
    python3 dispatch.py --agent design-reviewer --target grok

This entry point is deliberately thin: it only picks an adapter and hands off to
the model-agnostic core. All per-CLI behavior lives in adapters/.
"""

import argparse
import os

import adapters
import core


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--target", required=True, choices=adapters.names())
    ap.add_argument("--path", default=os.path.join(core.ROOT, "sample"))
    ap.add_argument("--focus", help="Free-text scope instruction injected into the prompt "
                                    "(e.g. which files / the diff to audit). The host resolves it.")
    ap.add_argument("--focus-file", help="Read the focus text from a file (for large diffs).")
    args = ap.parse_args()

    focus = args.focus
    if args.focus_file:
        focus = open(os.path.expanduser(args.focus_file), encoding="utf-8").read()

    adapter = adapters.get(args.target)
    core.run(args.agent, adapter, os.path.abspath(args.path), focus=focus)


if __name__ == "__main__":
    main()
