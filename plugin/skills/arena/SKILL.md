---
name: arena
description: Multi-model review arena — several AI models audit the code, cross-critique each other until they converge, then a judge verifies against real code and a scoreboard names a winner. Runs as a Workflow (visible in /workflows).
argument-hint: (interactive) or <agent> <grok,codex,...> <topic>
allowed-tools: Bash(echo:*), Bash(python3:*), Bash(git diff:*), Bash(git status:*), Bash(git ls-files:*), Grep, Read, AskUserQuestion, Workflow
disable-model-invocation: true
---

Run a DETERMINISTIC multi-model review arena. The whole pipeline (rounds, cross-critique,
convergence, judge, scoreboard) is fixed in the engine — you ONLY collect the user's choices and
launch it. Do NOT greet or narrate; go straight to step 1. Never audit anything yourself.

0. **Resolve the engine path:** run `echo "${CLAUDE_PLUGIN_ROOT}"` → call the result ENGINE.
   The arena CLI is `ENGINE/arena.py`; the workflow script is `ENGINE/arena.workflow.js`.

1. **List what's available:** `python3 ENGINE/arena.py list --path .` → parse JSON `{models, agents}`.
   If `models` is empty, say no AI CLIs are usable and stop. If `$ARGUMENTS` already carries
   `<agent> <models> <topic>`, use them and skip the matching questions.

2. **Pick the agent:** `AskUserQuestion`, one option per entry in `agents` (label = `name`,
   description = its `description`).

3. **Pick the AIs:** `AskUserQuestion` (multiSelect), one option per entry in `models`. Default: all.

4. **Topic + scope:** if given in `$ARGUMENTS`, use it. Otherwise ask what to review. If they refer
   to changes / a method / a path, resolve scope with `git diff` / `git status` / `Grep` and fold it
   into the topic. (You interpret intent in any language.)

5. **Suggest the mode:** `python3 ENGINE/arena.py suggest-mode --topic "<topic>"` → `{mode, reason}`.
   Show it, then `AskUserQuestion`: keep it or switch (`adversarial` = debate & critique;
   `independent` = audit separately). The user's pick wins.

6. **Rounds & options:** `AskUserQuestion` for min/max rounds (preset min 2 / max 4; independent
   ignores this) and judge on/off (recommended on). Tip: grok+codex as debaters with claude as the
   neutral judge is a good default.

7. **Launch the WORKFLOW (shows in /workflows):**
   `Workflow({ scriptPath: "ENGINE/arena.workflow.js", args: { arenaPath: "ENGINE/arena.py", agent: "<agent path>", agentName: "<agent name>", models: ["grok","codex",...], topic: "<full topic+scope>", mode: "<mode>", minRounds: <n>, maxRounds: <m>, judge: <true|false>, path: "." } })`
   (substitute the real ENGINE). Tell the user it's running and they can watch it live in **/workflows**.

8. **Present when it finishes** (it returns `rounds`, `judge`, `scoreboard`):
   - **🏆 scoreboard first:** `winner: <model>` (or `tie: a = b`), then per model:
     `<model>: <points> pts · <confirmed> confirmed · <false> false · <unique> unique`.
   - the FINAL ranked findings — the judge's list if judging was on, else the last round; most-severe first.
   - how each model's positions evolved (NEW → KEEP / WITHDRAW / ADOPT).
   Cross-model findings are **unverified claims** unless the judge marked them CONFIRMED against real
   code; flag the rest and offer to trace any finding's call chain.
