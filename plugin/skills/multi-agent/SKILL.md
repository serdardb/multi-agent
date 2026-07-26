---
name: multi-agent
description: Run one of your agents on ANOTHER AI model (codex / grok) as a live, steerable review — watch it work and type to it while it runs (it answers, then keeps auditing). Scope from plain words.
argument-hint: <agent> <codex|grok> <what to review>
allowed-tools: Bash(echo:*), Bash(nohup:*), Bash(python3:*), Bash(git diff:*), Bash(git status:*), Bash(git ls-files:*), Bash(cat:*), Bash(printf:*), Bash(sleep:*), Grep, Read
disable-model-invocation: true
---

Run one of the user's agents on another model CLI as a live review. Arguments: `$ARGUMENTS` —
first token = AGENT name (a file in the project's `.claude/agents/`), second = TARGET (`codex` or
`grok`), the rest = what to review. Do NOT greet; go straight to the steps.

0. **Resolve the engine path:** run `echo "${CLAUDE_PLUGIN_ROOT}"` → call it ENGINE. Engine = `ENGINE/live.py`.
   Fixed paths (substitute agent & target): FOCUS=`/tmp/ma-live-<AGENT>-<TARGET>.focus`
   CTRL=`/tmp/ma-live-<AGENT>-<TARGET>.ctrl`  LOG=`/tmp/ma-live-<AGENT>-<TARGET>.log`

1. **Resolve the SCOPE** from the sentence: the diff / "what I changed / this branch" → `git status` +
   `git diff`; a method/symbol → `Grep`; a path → use it. Unclear → ask ONE short question and stop.

2. **Write FOCUS, reset CTRL:**
   `printf '%s' "Audit ONLY these changes/files. Restrict findings to this scope. <files + diff>" > FOCUS`
   then `printf '' > CTRL`.

3. **Launch DETACHED:**
   `nohup python3 ENGINE/live.py --target <TARGET> --agent .claude/agents/<AGENT>.md --path . --focus-file FOCUS --control CTRL > LOG 2>&1 &`
   then `echo launched`.

4. **Stream while it runs:** loop — `sleep 3`, then `Read` LOG — relaying new `grok ▸` / `codex ▸`
   lines. Lines marked `💬` are the AGENT's OWN reply to a user message — show them VERBATIM.

5. **Forward-by-default:** while a turn runs, EVERY message the user sends goes straight to the model:
   `echo "<the user's message>" >> CTRL` → it receives it live and keeps auditing. Forward all of it;
   never answer on the agent's behalf. Send each once (live.py de-dupes an 8s repeat). Only explicit
   stop/close you handle yourself.

6. **Finish:** the MOMENT LOG has `__TURN_DONE__`, STOP polling and present the findings JSON (the line
   before it) as a clean severity-ranked list. Cross-model findings are **unverified claims**. The
   session stays open: a follow-up is forwarded as the next turn; "close / stop" → `echo "__STOP__" >> CTRL`
   (live.py also self-exits after ~150s idle).
