#!/usr/bin/env python3
"""
arena.py — deterministic multi-model review ARENA.

Runs YOUR agent on several models and, optionally, makes them debate each other:
independent audit -> cross-critique -> convergence-gated further rounds -> judge.

The orchestration (rounds, when to continue, judging) is FIXED CODE here — not a
model's or a wrapper's improvisation. A thin /arena command only collects your
choices (agent, models, topic, mode, min/max) and launches this.

Subcommands (none are interactive on their own):
  arena.py list                       -> {"models":[usable...], "agents":[...]}  (JSON)
  arena.py suggest-mode --topic "..." -> {"mode":"adversarial|independent","reason":".."}
  arena.py run --agent <file> --models grok,codex --topic "..." \
      --mode adversarial --min-rounds 2 --max-rounds 4 [--judge] \
      [--manual --control <file>]     -> runs the pipeline; result JSON to stdout,
                                         live progress to stderr, copy to out/.

Builds on the model-agnostic core + adapters (each model runs YOUR agent natively;
output is normalized to the findings schema). Only stdlib.
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import core          # noqa: E402
import adapters      # noqa: E402

# Models the arena can drive, in display order (only those with a working CLI show up).
ARENA_MODELS = ["claude", "grok", "codex"]

ARENA_SCHEMA_PATH = os.path.join(core.ROOT, "schema", "arena-v1.json")
ARENA_SCHEMA = json.load(open(ARENA_SCHEMA_PATH))


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# discovery: which model CLIs are usable, which agents exist
# --------------------------------------------------------------------------- #
def usable_models():
    """Models whose CLI is actually installed. Uses which() (no side effects) —
    NOT adapter.preflight(), which may exit the process on failure."""
    out = []
    for name in ARENA_MODELS:
        ad = adapters.get(name)
        if ad and shutil.which(getattr(ad, "binary", name)):
            out.append(name)
    return out


def list_agents(path):
    """Agents available to review with: the target repo's .claude/agents plus
    this tool's bundled agents/. Returns [{name, description, path, tools}]."""
    seen, agents = set(), []
    dirs = [os.path.join(os.path.abspath(path), ".claude", "agents"),
            os.path.join(core.ROOT, "agents")]
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".md"):
                continue
            fp = os.path.join(d, fn)
            try:
                ir = core.load_agent(fp)
            except SystemExit:
                continue
            except Exception:
                continue
            key = ir["name"]
            if key in seen:
                continue
            seen.add(key)
            agents.append({"name": ir["name"], "description": ir.get("description", ""),
                           "path": fp, "tools": ir.get("tools", [])})
    return agents


def cmd_list(args):
    print(json.dumps({"models": usable_models(),
                      "agents": list_agents(args.path)}, indent=2))


# --------------------------------------------------------------------------- #
# one model, one round: run YOUR agent on it with a focus, return its findings
# --------------------------------------------------------------------------- #
def dispatch(model, ir, path, focus):
    """Run the agent natively on `model` with `focus` as the scope/critique text.
    Returns (findings_list, note). Never raises — a failed model yields []."""
    adapter = adapters.get(model)
    if adapter is None:
        return [], f"no adapter for {model}"
    task = core.compile_task(ARENA_SCHEMA, path, focus=focus)
    try:
        raw, err, rc = adapter.run(task, path, ARENA_SCHEMA_PATH, ir=ir, task=task)
    except SystemExit as e:
        return [], f"{model} preflight/exit: {e}"
    except Exception as e:
        return [], f"{model} error: {e}"
    located = None
    try:
        located = adapter.parse(raw)
    except Exception:
        located = None
    if not located:
        note = ""
        try:
            note = adapter.explain_empty(raw)
        except Exception:
            note = ""
        return [], (note or "no usable output")
    return located.get("findings", []) or [], ""


def parallel_round(models, ir, path, focus_map):
    """Run all selected models for one round concurrently. Returns {model: findings}."""
    results = {}
    def one(m):
        log(f"  ▸ {m}: working…")
        findings, note = dispatch(m, ir, path, focus_map[m])
        results[m] = findings
        tag = f"{len(findings)} findings" + (f" ({note})" if note and not findings else "")
        log(f"  ✓ {m}: {tag}")
    with ThreadPoolExecutor(max_workers=max(1, len(models))) as ex:
        list(ex.map(one, models))
    return {m: results.get(m, []) for m in models}


# --------------------------------------------------------------------------- #
# round focus text (topic only vs cross-critique)
# --------------------------------------------------------------------------- #
def focus_round1(topic):
    return (topic.strip()
            + "\n\nThis is an INDEPENDENT first-pass audit. Report every real finding, "
              'severity-ranked. Set "position":"NEW" on each finding.')


def focus_adversarial(topic, mine_prev, others):
    blocks = []
    for name, findings in others:
        blocks.append(f"### {name} — current findings:\n{json.dumps(findings, ensure_ascii=False)}")
    prev = json.dumps(mine_prev, ensure_ascii=False)
    return (topic.strip()
            + "\n\n=== CROSS-CRITIQUE ROUND ===\n"
              "Below are the OTHER auditors' current findings and your own previous list. "
              "Judge every item — theirs and yours — against the ACTUAL code and return your "
              "UPDATED findings list. For each finding set \"position\":\n"
              "  KEEP     = your finding still stands (cite the confirming code line)\n"
              "  WITHDRAW = your earlier finding is wrong/unreachable/already-handled (say why)\n"
              "  ADOPT    = another auditor found something real you had missed — include it\n"
              "  NEW      = something new you found this round\n"
              "Converge on truth; do NOT pad or restate to win. Cite file:line in every description.\n\n"
            + "\n".join(blocks)
            + f"\n### your previous findings:\n{prev}")


# --------------------------------------------------------------------------- #
# convergence: has the debate stopped moving?
# --------------------------------------------------------------------------- #
def _keyset(findings):
    """Normalized set of a model's ACTIVE findings (withdrawn ones excluded)."""
    ks = set()
    for x in findings or []:
        if (x.get("position") or "").upper() == "WITHDRAW":
            continue
        f = os.path.basename(str(x.get("file", "")))
        line = "".join(ch for ch in str(x.get("line", "")) if ch.isdigit())
        sev = (x.get("severity") or "").lower()
        ks.add((f, line, sev))
    return ks


def converged(prev_by_model, cur_by_model):
    """True when NO model's active finding-set changed this round — the positions
    stopped moving, so another round would add nothing."""
    for m in cur_by_model:
        if _keyset(prev_by_model.get(m, [])) != _keyset(cur_by_model.get(m, [])):
            return False
    return True


# --------------------------------------------------------------------------- #
# manual round-gate via a control file (same handshake style as live.py)
# --------------------------------------------------------------------------- #
def ask_user_continue(control, rn, timeout=600):
    """Pause for the host: announce, then poll the control file for a decision.
    Returns True (continue) / False (stop). On timeout, returns None (caller
    falls back to the auto convergence decision)."""
    if not control:
        return None
    open(control, "a").close()
    offset = os.path.getsize(control)
    log(f"__AWAIT_DECISION__ round {rn} finished — reply 'continue' to run another round or 'stop' to end.")
    end = time.time() + timeout
    while time.time() < end:
        if os.path.getsize(control) > offset:
            with open(control, "r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                chunk = f.read()
            for line in chunk.splitlines():
                s = line.strip().lower()
                if not s:
                    continue
                if s in ("stop", "no", "n", "cancel", "close", "__stop__"):
                    return False
                if s in ("continue", "yes", "y", "next", "go"):
                    return True
            offset = os.path.getsize(control)
        time.sleep(0.5)
    return None


# --------------------------------------------------------------------------- #
# judge: one model code-verifies the debated claims
# --------------------------------------------------------------------------- #
def run_judge(models, ir, path, rounds, judge_model):
    inputs = []
    for i, rd in enumerate(rounds, 1):
        for m in models:
            inputs.append(f"R{i} {m}: {json.dumps(rd.get(m, []), ensure_ascii=False)}")
    focus = (
        "You are the JUDGE of a multi-model audit. CROSS-MODEL FINDINGS ARE UNVERIFIED "
        "CLAIMS — for every disputed or high-severity item OPEN THE ACTUAL CODE and confirm "
        "before ruling; cite the exact file:line you read. Produce the final consolidated "
        "finding list: merge duplicates, drop the disproven, keep only what the code supports. "
        'For each finding put the verdict in "category" as "CONFIRMED: <cat>", "REJECTED: <cat>", '
        'or "PARTIAL: <cat>", and set "position":"KEEP" for confirmed, "WITHDRAW" for rejected. '
        "Note the agreement level (how many models raised it) in the description. Rank most-severe first.\n\n"
        "=== ALL ROUNDS ===\n" + "\n".join(inputs))
    log(f"  ▸ judge ({judge_model}): code-verifying disputed claims…")
    findings, note = dispatch(judge_model, ir, path, focus)
    log(f"  ✓ judge: {len(findings)} verified findings" + (f" ({note})" if note and not findings else ""))
    return {"model": judge_model, "findings": findings}


def cmd_once(args):
    """One model, one run, arena-v1 schema — the atomic unit the workflow calls
    per round per model. Prints {"model":..,"findings":[...]} to stdout; live
    model progress goes to stderr (visible in the calling agent's transcript)."""
    ir = core.load_agent(args.agent)
    focus = args.focus or ""
    if args.focus_file:
        focus = open(os.path.expanduser(args.focus_file), encoding="utf-8").read()
    findings, note = dispatch(args.model, ir, os.path.abspath(args.path), focus)
    print(json.dumps({"model": args.model, "findings": findings,
                      "note": note or ""}, ensure_ascii=False))


def cmd_suggest_mode(args):
    models = usable_models()
    if not models:
        print(json.dumps({"mode": "independent", "reason": "no usable models detected"}))
        return
    asker = "claude" if "claude" in models else models[0]
    adapter = adapters.get(asker)
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"mode": {"type": "string", "enum": ["adversarial", "independent"]},
                             "reason": {"type": "string"}},
              "required": ["mode", "reason"]}
    prompt = (
        "A user wants a code review on this topic:\n\n" + (args.topic or "") + "\n\n"
        "Recommend ONE approach and return ONLY JSON {\"mode\":\"adversarial|independent\",\"reason\":\"one line\"}.\n"
        "- 'adversarial' = several models debate & critique each other. Best for high-stakes, "
        "security, financial correctness, data-integrity, or contested judgment calls.\n"
        "- 'independent' = models audit separately and you compare. Best for broad coverage, "
        "style/quality passes, or quick scans.")
    fd, sp = tempfile.mkstemp(prefix="arena-suggest-", suffix=".json")
    os.close(fd)
    with open(sp, "w") as f:
        json.dump(schema, f)
    obj = None
    try:
        raw, err, rc = adapter.run(prompt, os.path.abspath(args.path), sp)  # Level-1 (no agent)
        env = core.extract_json(raw)
        if isinstance(env, dict):
            so = env.get("structuredOutput")
            obj = so if isinstance(so, dict) else env
    except Exception:
        obj = None
    finally:
        try:
            os.unlink(sp)
        except OSError:
            pass
    mode = (obj or {}).get("mode") if isinstance(obj, dict) else None
    reason = (obj or {}).get("reason") if isinstance(obj, dict) else None
    if mode not in ("adversarial", "independent"):
        # deterministic fallback so the command always gets a suggestion
        t = (args.topic or "").lower()
        hot = any(k in t for k in ("security", "auth", "money", "payment", "finance",
                                   "webhook", "race", "concurren", "correctness", "integrity"))
        mode = "adversarial" if hot else "independent"
        reason = reason or ("high-stakes topic — models debating catches more" if hot
                            else "straightforward scope — independent compare is enough")
    print(json.dumps({"mode": mode, "reason": reason, "asked": asker}))


# --------------------------------------------------------------------------- #
# the pipeline
# --------------------------------------------------------------------------- #
def _emit_round(rn, by_model):
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for m, findings in by_model.items():
        sev = {}
        for f in findings:
            sev[f.get("severity", "?")] = sev.get(f.get("severity", "?"), 0) + 1
        brk = ", ".join(f"{k}:{sev[k]}" for k in sorted(sev, key=lambda x: order.get(x, 9))) or "none"
        log(f"  round {rn} · {m}: {len(findings)} ({brk})")


def _sevw(s):
    return {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(str(s or "").lower(), 1)


def _fkey(x):
    f = str(x.get("file", "")).split("/")[-1]
    line = "".join(c for c in str(x.get("line", "")) if c.isdigit())
    return f + "|" + line + "|" + str(x.get("severity", "")).lower()


def compute_scoreboard(rounds, judge, models):
    """Post-hoc, judge-grounded scoreboard (mirrors the arena.js logic): +confirmed
    (severity-weighted), -false positives, +unique real catches, +honest concessions.
    Returns None if there is no judge verdict to score against."""
    final_of, last_of = {}, {}
    for m in models:
        f = []
        for rd in rounds:
            if rd.get(m):
                f = rd[m]
        last_of[m] = f or []
        final_of[m] = [x for x in (f or []) if str(x.get("position", "")).upper() != "WITHDRAW"]
    verdict = {}
    for j in (judge or {}).get("findings", []) or []:
        cat = str(j.get("category", "")).upper()
        v = ("CONFIRMED" if cat.startswith("CONFIRMED")
             else "REJECTED" if cat.startswith("REJECTED")
             else "PARTIAL" if cat.startswith("PARTIAL")
             else ("REJECTED" if str(j.get("position", "")).upper() == "WITHDRAW" else "CONFIRMED"))
        verdict[_fkey(j)] = {"v": v, "sev": j.get("severity")}
    if not verdict:
        return None
    holders = {}
    for m in models:
        for x in final_of[m]:
            holders.setdefault(_fkey(x), set()).add(m)
    board = []
    for m in models:
        pts = 0.0
        confirmed = fp = unique = concessions = 0
        seen = set()
        for x in final_of[m]:
            k = _fkey(x)
            if k in seen:
                continue
            seen.add(k)
            vd = verdict.get(k)
            if not vd:
                continue
            w = _sevw(vd["sev"] or x.get("severity"))
            if vd["v"] == "CONFIRMED":
                confirmed += 1
                pts += w
                if len(holders.get(k, set())) == 1:
                    unique += 1
                    pts += 2
            elif vd["v"] == "PARTIAL":
                confirmed += 1
                pts += w / 2
            elif vd["v"] == "REJECTED":
                fp += 1
                pts -= w
        for x in last_of[m]:
            k = _fkey(x)
            p = str(x.get("position", "")).upper()
            if p == "WITHDRAW" and verdict.get(k, {}).get("v") == "REJECTED":
                concessions += 1
                pts += 1
            if p == "ADOPT" and verdict.get(k, {}).get("v") == "CONFIRMED":
                concessions += 1
                pts += 1
        board.append({"model": m, "points": round(pts * 10) / 10, "confirmed": confirmed,
                      "false_positives": fp, "unique": unique, "concessions": concessions})
    board.sort(key=lambda b: b["points"], reverse=True)
    top = board[0]["points"] if board else 0
    winners = [b["model"] for b in board if b["points"] == top]
    return {"board": board, "winner": winners[0] if len(winners) == 1 else None,
            "tie": winners if len(winners) > 1 else None}


def cmd_run(args):
    ir = core.load_agent(args.agent)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    avail = usable_models()
    models = [m for m in models if m in avail] or models  # keep user's if detection is odd
    path = os.path.abspath(args.path)
    topic = args.topic or ""
    if args.focus_file:
        topic = open(os.path.expanduser(args.focus_file), encoding="utf-8").read()
    if not topic.strip():
        log("✗ no topic/focus given"); sys.exit(2)

    log("=" * 60)
    log(f"  arena: {ir['name']}  |  models: {', '.join(models)}  |  mode: {args.mode}")
    log(f"  rounds: min {args.min_rounds}, max {args.max_rounds}"
        + ("  |  manual gate" if args.manual else "  |  auto (convergence)")
        + ("  |  +judge" if args.judge else ""))
    log("=" * 60)

    rounds = []
    log("=== Round 1 — independent audit ===")
    r1 = parallel_round(models, ir, path, {m: focus_round1(topic) for m in models})
    rounds.append(r1)
    _emit_round(1, r1)

    if args.mode == "adversarial" and len(models) >= 2:
        rn = 1
        while rn < args.max_rounds:
            rn += 1
            log(f"=== Round {rn} — cross-critique ===")
            prev = rounds[-1]
            focus_map = {}
            for m in models:
                others = [(o, prev.get(o, [])) for o in models if o != m]
                focus_map[m] = focus_adversarial(topic, prev.get(m, []), others)
            cur = parallel_round(models, ir, path, focus_map)
            rounds.append(cur)
            _emit_round(rn, cur)

            if rn >= args.min_rounds:
                decision = None
                if args.manual:
                    decision = ask_user_continue(args.control, rn)
                if decision is None:                       # auto (or manual timeout)
                    if converged(prev, cur):
                        log(f"— converged after round {rn}: positions stopped moving. stopping.")
                        break
                    log(f"— round {rn}: positions still moving, another round.")
                elif decision is False:
                    log(f"— you chose to stop after round {rn}.")
                    break
                else:
                    log(f"— you chose to continue after round {rn}.")

    judge_out = None
    if args.judge:
        log("=== Judge — code-verifying the debated claims ===")
        judge_model = args.judge_model if args.judge_model in models or args.judge_model in avail else \
            ("claude" if "claude" in avail else models[0])
        judge_out = run_judge(models, ir, path, rounds, judge_model)

    sb = compute_scoreboard(rounds, judge_out, models) if args.judge else None
    if sb:
        log("=== 🏆 Scoreboard ===")
        log("  🏆 " + (f"winner: {sb['winner']}" if sb["winner"]
                       else f"tie: {' = '.join(sb['tie'])}"))
        for b in sb["board"]:
            log(f"     {b['model']}: {b['points']} pts · {b['confirmed']} confirmed · "
                f"{b['false_positives']} false · {b['unique']} unique · {b['concessions']} concessions")

    result = {
        "agent": ir["name"], "models": models, "mode": args.mode,
        "rounds_run": len(rounds),
        "rounds": [{"round": i + 1, "by_model": rd} for i, rd in enumerate(rounds)],
        "judge": judge_out,
        "scoreboard": sb,
    }
    outdir = os.path.join(core.ROOT, "out")
    os.makedirs(outdir, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", ir["name"])
    outpath = os.path.join(outdir, f"arena-{slug}.json")
    json.dump(result, open(outpath, "w"), indent=2)
    log(f"__ARENA_DONE__ {outpath}")
    print(json.dumps(result))


def main():
    ap = argparse.ArgumentParser(description="deterministic multi-model review arena")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="usable models + available agents (JSON)")
    p.add_argument("--path", default=".")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("suggest-mode", help="ask a model which approach fits the topic")
    p.add_argument("--topic", required=True)
    p.add_argument("--path", default=".")
    p.set_defaults(func=cmd_suggest_mode)

    p = sub.add_parser("once", help="run ONE model once (arena-v1) — the workflow's per-round unit")
    p.add_argument("--model", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--path", default=".")
    p.add_argument("--focus", help="scope/critique text")
    p.add_argument("--focus-file", help="read scope/critique text from a file")
    p.set_defaults(func=cmd_once)

    p = sub.add_parser("run", help="run the arena pipeline")
    p.add_argument("--agent", required=True, help="agent .md file (or bare name in agents/)")
    p.add_argument("--models", required=True, help="comma list, e.g. grok,codex,claude")
    p.add_argument("--topic", help="what to review, in plain words")
    p.add_argument("--focus-file", help="read the topic/scope from a file (large diffs)")
    p.add_argument("--path", default=".")
    p.add_argument("--mode", choices=["adversarial", "independent"], default="adversarial")
    p.add_argument("--min-rounds", type=int, default=2)
    p.add_argument("--max-rounds", type=int, default=4)
    p.add_argument("--judge", action="store_true")
    p.add_argument("--judge-model", default="claude")
    p.add_argument("--manual", action="store_true", help="ask (via --control) after each round")
    p.add_argument("--control", help="control file the host appends continue/stop to (manual mode)")
    p.set_defaults(func=cmd_run)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
