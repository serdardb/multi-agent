# multi-agent

**Run your own agent on a different AI model's CLI — as an independent second opinion.**

You already have an agent defined for one coding CLI (a `.claude/agents/*.md`). `multi-agent` runs *that same agent* on **other model CLIs** — `grok`, `codex`, `claude` — each one executing it **natively** through its own agent runtime. No API keys, no re-implementation: if the CLI is installed and logged in, the agent runs there.

The point is diversity, not redundancy: when independent models **converge** on a finding → higher confidence; when they **diverge** → wider coverage.

> **Honest note.** Built for and tested on **Claude Code**; the multi-round arena runs on top of Claude Code's **Workflow** system (the deterministic `agent()` / `parallel()` / `phase()` runtime shown in `/workflows`). The orchestration engine itself is plain Python and host-agnostic, so Codex/Grok-native wrappers can be added later — not shipped yet. If you need one, open an issue.

## Two commands

### `/arena` — deterministic multi-model arena
Every selected model audits the target **independently**, then they **cross-critique** each other round after round until positions **converge** (or hit your max), then a **judge** verifies the disputed and high-severity claims **against the real code**, and a **scoreboard** ranks the models. Runs as a native Workflow — watch it live in `/workflows`.

```
Round 1        each model audits independently
Round 2        first cross-critique (KEEP / WITHDRAW / ADOPT / NEW, citing code)
Round 3, 4…    more critique — added ONLY while positions keep moving,
               up to your max (convergence-gated: stops the moment nobody's
               position changes, at or after your min)
Judge          opens the code, confirms/rejects each claim, emits the final
               ranked list + 🏆 scoreboard   (runs only if you enable it)
```

You pick the models, the min/max rounds, and whether to run the judge — and `/workflows` shows **exactly the rounds that execute** (a 1-round run shows only `Round 1`; `min 2 / max 2` shows `Round 1`, `Round 2`; a higher max extends only if the debate keeps moving). The judge defaults to **claude** (the host model, always available) and can be overridden.

The scoreboard is **post-hoc and judge-grounded** (models are never told they're being scored, so the debate stays about truth): `+` confirmed findings (severity-weighted), `−` false positives, `+` unique real catches, `+` honest concessions the judge agreed with.

### `/multi-agent` — single-model, live, steerable review
Runs as a **forked background agent** — it appears underneath the main thread and works live: you watch the target model **think and run tools in real time** (`codex ▸ …` / `grok ▸ …`), and you can **talk to it while it works** — type a question or a directive and it answers you, then keeps auditing. When it finishes it posts a severity-ranked findings list; the session stays open for follow-ups.

```
/multi-agent <agent> <codex|grok> <what to review, in plain words>
```

## How it works

```
your agent (.md)  ──►  multi-agent engine (Python)  ──►  target CLI runs it natively
                       core.py · adapters/ · arena.py       (claude / grok / codex)
                              └─►  normalized findings (findings-v1 schema)
```

- **`core.py`** — model-agnostic: loads the agent, compiles the task, maps the agent's own tools to each target's native tool names, normalizes output. It imposes **no policy of its own** — the agent's own tools *are* its permission.
- **`adapters/`** — one per CLI (`claude`, `grok`, `codex` native app-server, plus a `codex-exec` fallback). Each runs that CLI headless and shapes its output back into the common schema. Every spawned process is killed as a group on exit — no orphans.
- **`arena.py`** — the deterministic arena engine (`list`, `suggest-mode`, `once`, `run`). The multi-round logic is fixed here; nothing is improvised at runtime.
- **`arena.workflow.js`** — the Claude Code Workflow that drives the rounds and shows them in `/workflows`. It calls `arena.py` under the hood.

## Requirements

- **Python 3** (standard library only).
- The target CLIs you want to use, already **installed and logged in**: any of `grok`, `codex`, `claude`. The tool drives whichever are present. **It uses no API keys of its own.**

## Install

Inside Claude Code:

```
/plugin marketplace add serdardb/multi-agent
/plugin install multi-agent@multi-agent
/reload-plugins
```

`/reload-plugins` is **required the first time** — Claude Code prints *"Run /reload-plugins to apply"* after install. (Shell equivalents: `claude plugin marketplace add serdardb/multi-agent` then `claude plugin install multi-agent@multi-agent`.)

Then, in any project, use the commands directly:

- `/arena` — pick an agent, pick which models, describe what to review; watch it in `/workflows`.
- `/multi-agent <agent> <codex|grok> <what to review>` — live, steerable single-model review.

(Claude Code also exposes them namespaced as `/multi-agent:arena` and `/multi-agent:multi-agent`.)

## Use the engine directly (host-agnostic CLI)

```bash
python3 plugin/arena.py list
python3 plugin/arena.py run --agent <agent.md> --models grok,codex,claude \
    --topic "audit the webhook subsystem" --mode adversarial \
    --min-rounds 2 --max-rounds 4 --judge
```

## Honesty & limitations

- **Cross-model findings are unverified claims** until the judge confirms them against real code.
- **A full adversarial run is heavy** — many model turns, several rounds, minutes to tens of minutes, real token spend. For quick work use fewer models/rounds or `/multi-agent`.
- Models vary a lot in speed and thoroughness; a round waits for the slowest.
- It only orchestrates CLIs you already have; it adds no models of its own.

## Layout

```
.claude-plugin/marketplace.json   marketplace catalog
plugin/
  .claude-plugin/plugin.json      plugin manifest
  skills/arena/SKILL.md           /arena
  skills/multi-agent/SKILL.md     /multi-agent
  arena.workflow.js               the arena Workflow (rounds + convergence + judge + scoreboard)
  agents/                         sample agents (design-reviewer, security-auditor, security-guard)
  arena.py  core.py  live.py      engine
  adapters/                       one per target CLI
  schema/                         findings-v1, arena-v1
sample/app.py                     tiny insecure fixture for trying the tools
```

## Roadmap

- Codex / Grok native skill wrappers (the engine already supports them as targets).
- An MCP server exposing the arena as a single tool callable from any MCP-capable CLI.

## License

MIT — see [LICENSE](LICENSE).
