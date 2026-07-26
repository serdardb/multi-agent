---
name: multi-agent
description: Run one of THIS project's agents on ANOTHER AI model (codex / grok) as a LIVE, STEERABLE background agent — watch it work underneath, and type to it while it runs to steer the running turn. Scope from plain words (git diff / method / path), no flags.
argument-hint: <agent> <codex|grok> <what to review, in your own words>
context: fork
background: true
agent: general-purpose
disable-model-invocation: true
allowed-tools: Bash(echo:*), Bash(nohup:*), Bash(python3:*), Bash(git diff:*), Bash(git status:*), Bash(git ls-files:*), Bash(cat:*), Bash(printf:*), Bash(sleep:*), Grep, Read
---

> You are ALREADY the forked background subagent for this command. Execute the
> steps directly with your tools. Do NOT call the Skill tool for `multi-agent`.
> IMPORTANT: `BashOutput` does NOT work inside subagents — poll by `Read`-ing the
> log file below, never BashOutput.

You run a LIVE, steerable cross-model session. Arguments: `$ARGUMENTS`
- first token = AGENT name (a file in the project's `.claude/agents/<name>.md`)
- second token = TARGET model: `codex` or `grok`
- the rest = a natural-language description of WHAT to review.

Do NOT greet; go straight to the steps.

0. **Resolve the engine path:** run `echo "${CLAUDE_PLUGIN_ROOT}"` → call it ENGINE.
   The engine is `ENGINE/live.py`. Use these FIXED paths (substitute the real
   agent & target names):
   - FOCUS = `/tmp/ma-live-<AGENT>-<TARGET>.focus`
   - CTRL  = `/tmp/ma-live-<AGENT>-<TARGET>.ctrl`
   - LOG   = `/tmp/ma-live-<AGENT>-<TARGET>.log`

1. **Resolve the SCOPE** from the sentence with your own tools:
   - changes / "yaptığım / değişiklik / son işler / this branch / what I changed" →
     `git status` + `git diff` (+ `git ls-files --others --exclude-standard`) for the
     changed files and the diff.
   - a method / class / symbol → `Grep` to locate it, collect the file(s) and lines.
   - a path → use it directly. Unclear → ask ONE short question and stop.

2. **Write FOCUS, reset CTRL:**
   `printf '%s' "Audit ONLY these changes/files. Restrict findings to this scope. <files + diff>" > FOCUS`
   then `printf '' > CTRL`.

3. **Launch the LIVE session DETACHED** (so you stay responsive; output goes to LOG):
   `nohup python3 ENGINE/live.py --target <TARGET> --agent .claude/agents/<AGENT>.md --path . --focus-file FOCUS --control CTRL > LOG 2>&1 &`
   then `echo launched`.

4. **Stream while it runs:** loop — `sleep 3`, then `Read` LOG — relaying the new
   progress lines (`grok ▸` / `codex ▸`) so the user can watch the agent think and
   work. Keep looping ONLY while the turn is running.
   - Lines marked `💬` (e.g. `grok ▸ 💬 …` / `codex ▸ 💬 …`) are the AGENT's OWN
     spoken reply to a message the user sent it — show these to the user VERBATIM
     (quote them), do NOT paraphrase or summarize them. That is the agent answering
     the user directly; the user must see its actual words.

5. **FORWARD-BY-DEFAULT (the whole point) — do NOT gatekeep.** While a turn is
   running you are a COURIER, not a chatbot. EVERY message the user types goes
   STRAIGHT to the running `<TARGET>`:
   `echo "<the user's message>" >> CTRL`
   → the running turn receives it live WITHOUT being cancelled (grok `_x.ai/btw`
   side-channel / codex `turn/steer`); it weaves the message into its ongoing work.
   Then keep polling (step 4) and relay `<TARGET>`'s reply.
   - Forward ALL of it — including "which AI are you", meta questions, follow-ups,
     everything. It is the AGENT being addressed, NOT you. NEVER answer on the
     agent's behalf, and NEVER decide a message "isn't really a steer" and swallow
     it — that judgment is EXACTLY the bug this replaces. When unsure: forward.
   - Send each message to CTRL exactly ONCE, even if your input pane appears to show
     it twice (a UI echo). live.py also de-dupes an exact repeat within 8s as a
     safety net, but you still send it once.
   - The ONLY thing you handle yourself instead of forwarding is explicit control of
     THIS run — the user telling you to stop / close / cancel → go to step 6.

6. **Finish (stop polling, don't blind-loop):** the MOMENT LOG contains
   `__TURN_DONE__`, STOP polling — the turn is done. Present the findings JSON (the
   line just before `__TURN_DONE__`) as a clean severity-ranked list — this IS the
   `<AGENT>` agent's result as executed by `<TARGET>`. Cross-model findings are
   **unverified claims**; flag downstream-dependent ones and offer to trace call
   chains in the real code. Then tell the user the session is still open: they can
   send a follow-up (you'll forward it as the next turn per step 5 and resume
   polling) or say "kapat / close / stop" to end it. On close — or if they go quiet
   — `echo "__STOP__" >> CTRL`. (live.py also self-exits after ~150s idle, so
   nothing is ever left running.) Do NOT keep blind-polling after `__TURN_DONE__`.
