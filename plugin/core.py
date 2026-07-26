"""
Model-agnostic core.

Nothing in this file knows about any specific CLI. It normalizes a host agent
into a portable IR, compiles a prompt, runs the capability gate, and normalizes
whatever an adapter returns back into the common findings-v1 contract.

Rule (per project directive): this file changes only when a NEW FEATURE is added
— never to accommodate a particular model. Anything a model can or cannot do
lives in its own adapter under adapters/.
"""

import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))

# Neutral capability vocabulary (the "constants"). Every source tool maps to one
# of these; each adapter maps these to its target's OWN native tool name. Tools
# with no entry here are host-specific and carried as unmapped_tools (reported,
# never silently dropped).
TOOL_TO_CAP = {
    "Read": "read_files",
    "Grep": "search_repository",
    "Glob": "search_repository",
    "Bash": "run_shell",
    "PowerShell": "run_shell",
    "Edit": "write_files",
    "Write": "write_files",
    "NotebookEdit": "write_files",
    "WebSearch": "web_search",
    "WebFetch": "web_fetch",
    "Agent": "subagents",
    "TodoWrite": "todo",
}

# Capabilities a review agent genuinely needs. write_files is intentionally not
# here — a read-only reviewer wants it disabled.
REQUIRED_CAPS = {"read_files", "search_repository"}


def die(msg, code=2):
    print(f"\n\033[31m✗ {msg}\033[0m", file=sys.stderr)
    sys.exit(code)


def info(msg):
    print(f"\033[36m{msg}\033[0m")


# --------------------------------------------------------------------------- #
# host agent definition -> portable IR
# --------------------------------------------------------------------------- #
def load_agent(name):
    # Accept a real project's own agent file (path), or a bare name in agents/.
    # This is the "without installing it there" promise: point straight at the
    # agent the user already has in their project — nothing is copied.
    candidates = []
    if name.endswith(".md") or os.sep in name:
        candidates.append(os.path.abspath(os.path.expanduser(name)))
    candidates.append(os.path.join(ROOT, "agents", f"{name}.md"))
    path = next((c for c in candidates if os.path.exists(c)), None)
    if not path:
        die(f"agent not found: tried {candidates}")
    text = open(path, encoding="utf-8").read()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        die(f"agent {name} has no frontmatter")
    fm_raw, body = m.group(1), m.group(2).strip()
    fm = {}
    for line in fm_raw.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()

    tools = [t.strip() for t in fm.get("tools", "").split(",") if t.strip()]
    caps = []
    unmapped = []
    for t in tools:
        c = TOOL_TO_CAP.get(t)
        if c:
            if c not in caps:
                caps.append(c)
        elif t not in unmapped:
            unmapped.append(t)  # host-specific: no neutral capability

    return {
        "name": fm.get("name", name),
        "description": fm.get("description", ""),
        "source_tool": fm.get("source_tool", "claude"),
        "instructions": body,
        "tools": tools,
        "capabilities": caps,
        "unmapped_tools": unmapped,
        "constraints": [],  # no product-imposed policy; the agent's tools govern
        "output": {"format": "json", "schema": "findings-v1"},
    }


def schema_path():
    return os.path.join(ROOT, "schema", "findings-v1.json")


def load_schema():
    return json.load(open(schema_path()))


def compile_prompt(ir, schema, path, focus=None):
    schema_txt = json.dumps(schema, indent=2)
    if focus and focus.strip():
        scope_block = (
            focus.strip()
            + f"\n\nWorking directory: {path}. You may read any neighbouring files you need for "
              "context, but restrict your FINDINGS to the scope described above."
        )
    else:
        scope_block = f"Review the source code under the current working directory ({path})."
    return f"""You are being invoked as a one-shot, stateless review agent named "{ir['name']}".
Adopt exactly this persona and rules — do not add capabilities beyond them.

=== AGENT INSTRUCTIONS ===
{ir['instructions']}

=== SCOPE ===
{scope_block}

=== OUTPUT CONTRACT ===
Return ONLY a single JSON object, no prose, no markdown fences, matching this JSON Schema:
{schema_txt}

If you find nothing, return {{"findings": []}}."""


def compile_task(schema, path, focus=None):
    """Task message for a NATIVELY-invoked agent (--agent): scope + output
    contract ONLY. The persona/rules come from the target's own agent runtime,
    not from this text. This is the difference between role-playing a prompt and
    running the target's real agent."""
    schema_txt = json.dumps(schema, indent=2)
    if focus and focus.strip():
        scope_block = (
            focus.strip()
            + f"\n\nWorking directory: {path}. You may read neighbouring files for context, "
              "but restrict your FINDINGS to the scope above."
        )
    else:
        scope_block = f"Review the source code under the current working directory ({path})."
    return f"""=== SCOPE ===
{scope_block}

=== OUTPUT CONTRACT ===
Return ONLY a single JSON object, no prose, no markdown fences, matching this JSON Schema:
{schema_txt}

If you find nothing, return {{"findings": []}}."""


# Turn-lifecycle contract for LIVE, steerable sessions. This is MECHANISM (how a
# turn completes and how mid-turn user messages are handled), NOT audit-content
# policy — same category as enforcing the findings-v1 schema. Set once at session
# start (systemPromptOverride / developerInstructions), so it governs every turn
# and every interjection for any agent on any target, without mutating the user's
# messages or re-introducing host-side gatekeeping.
TURN_DISCIPLINE = """=== HARNESS TURN DISCIPLINE (mechanism, not audit policy) ===
You run headlessly, driven by a client that may relay user messages to you WHILE
you work. Turn-lifecycle rules:
- Your PRIMARY deliverable is the review described in the task message. A turn is
  complete ONLY once you have output the findings JSON for the scoped files.
- If a user message arrives mid-review (a question like "which AI are you", a
  version/architecture question, or a directive like "also check X"), you MUST
  FIRST reply to it in a brief PLAIN-TEXT message — actually address the user in
  words, do not merely think about it silently. Emitting this short plain-text
  reply is EXPLICITLY ALLOWED as an exception to the "JSON only, no prose" output
  rule below: the harness separates your spoken reply from the final findings
  JSON, so your reply reaches the user and your JSON stays clean. AFTER replying,
  CONTINUE the review and end the turn with the findings JSON. Never end your turn
  on the user's message itself, and never leave it unanswered.
- Keep every finding strictly within the scoped files named in the task. You may
  read neighbouring files for context, but never report findings outside scope."""


# --------------------------------------------------------------------------- #
# generic JSON recovery — model-agnostic helpers adapters may reuse
# --------------------------------------------------------------------------- #
def extract_json(text):
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    for candidate in (text, _first_object(text)):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _first_object(text):
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def locate_findings(node, depth=0):
    """Find a {"findings": [...]} object, preferring a non-empty list."""
    if depth > 6:
        return None
    if isinstance(node, dict):
        empty_match = None
        if isinstance(node.get("findings"), list):
            if node["findings"]:
                return node
            empty_match = node
        for v in node.values():
            got = locate_findings(v, depth + 1)
            if got is not None and got.get("findings"):
                return got
        return empty_match
    if isinstance(node, list):
        for v in node:
            got = locate_findings(v, depth + 1)
            if got is not None and got.get("findings"):
                return got
        return None
    if isinstance(node, str):
        decoder = json.JSONDecoder()
        empty_match = None
        for start, char in enumerate(node):
            if char != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(node[start:])
            except json.JSONDecodeError:
                continue
            got = locate_findings(parsed, depth + 1)
            if got is not None:
                if got.get("findings"):
                    return got
                empty_match = got
        return empty_match
    return None


def generic_findings(raw):
    """Default output normalization. Adapters override only if their CLI wraps
    output in a target-specific envelope."""
    data = extract_json(raw)
    located = locate_findings(data) if data is not None else None
    if located is None:
        located = locate_findings(raw)
    return located


# --------------------------------------------------------------------------- #
# capability gate
# --------------------------------------------------------------------------- #
def gate(ir, adapter):
    """Translate the agent's neutral capabilities into the target's OWN native
    tool names. Returns (rows, blocking, dropped):
      rows     = [(capability, native_name, mode), ...]
      blocking = required capabilities the target has no equivalent for (refuse)
      dropped  = the agent's host-specific tools with no neutral capability at
                 all — reported, never silently hidden.
    """
    rows, blocking = [], []
    for cap in ir["capabilities"]:
        entry = adapter.capabilities.get(cap)
        if entry is None:
            rows.append((cap, "—", "UNSUPPORTED"))
            if cap in REQUIRED_CAPS:
                blocking.append(cap)
        else:
            rows.append((cap, entry.get("native", cap), entry["mode"]))
    dropped = list(ir.get("unmapped_tools", []))
    return rows, blocking, dropped


def wants_write(ir):
    """Does the agent's OWN definition grant write capability (Edit/Write tools)?
    Target enforcement is derived from this — the product imposes no read-only or
    write policy of its own. Author gave write tools => writes allowed; author
    gave none => the target is locked read-only to honour that intent."""
    return bool(ir) and "write_files" in (ir.get("capabilities") or [])


# --------------------------------------------------------------------------- #
# orchestration: wire core + one adapter
# --------------------------------------------------------------------------- #
def run(agent_name, adapter, path, focus=None):
    ir = load_agent(agent_name)
    schema = load_schema()
    path = os.path.abspath(path)

    print("\n" + "=" * 64)
    print(f"  agent   : {ir['name']}  (source: {ir['source_tool']})")
    print(f"  target  : {adapter.name}")
    print(f"  scope   : {path}")
    print("=" * 64)

    binp = adapter.preflight()  # raises -> handled by adapter/core die path
    info(f"✓ preflight: {adapter.name} found at {binp}")

    rows, blocking, dropped = gate(ir, adapter)
    print(f"\n  permission / tool mapping (agent → {adapter.name}):")
    for cap, native, mode in rows:
        print(f"    {cap:18s} → {native:24s} [{mode}]")
    if dropped:
        print(f"  \033[33m⚠ host-specific tools with no {adapter.name} equivalent "
              f"(dropped): {', '.join(dropped)}\033[0m")
    if blocking:
        die(f"required capabilities have no {adapter.name} equivalent: {blocking} "
            f"— refusing to run.")
    info("✓ gate: required capabilities mapped")

    prompt = compile_prompt(ir, schema, path, focus=focus)  # Level-1 flattened fallback
    task = compile_task(schema, path, focus=focus)           # Level-2 native task
    info(f"\n▶ running {ir['name']} on {adapter.name} (headless, read-only)…")
    t0 = time.time()
    raw, err, rc = adapter.run(prompt, path, schema_path(), ir=ir, task=task)
    dt = time.time() - t0
    print(f"  finished in {dt:.1f}s (exit {rc}, {len(raw)} bytes captured)")

    slug = re.sub(r"[^A-Za-z0-9._-]", "-", ir["name"])
    outdir = os.path.join(ROOT, "out")
    os.makedirs(outdir, exist_ok=True)
    open(os.path.join(outdir, f"{slug}.{adapter.name}.raw.txt"), "w").write(raw)

    prov = {}
    try:
        prov = adapter.provenance(raw) or {}
    except Exception:
        prov = {}
    prov["tool_mapping"] = {cap: native for cap, native, _ in rows}
    if dropped:
        prov["dropped_tools"] = dropped

    located = adapter.parse(raw)
    if located is None:
        # Even on failure, print provenance so cancel/empty is auditable.
        _print_provenance(prov)
        note = adapter.explain_empty(raw)
        die(f"no usable output from {adapter.name}"
            + (f" — {note}" if note else "")
            + f"\n--- raw (first 800) ---\n{raw[:800]}")

    findings = located.get("findings", [])
    outpath = os.path.join(outdir, f"{slug}.{adapter.name}.json")
    payload = {
        "agent": ir["name"],
        "target": adapter.name,
        "seconds": round(dt, 1),
        "findings": findings,
        "provenance": prov,
    }
    json.dump(payload, open(outpath, "w"), indent=2)

    print(f"\n\033[32m✓ {len(findings)} findings (findings-v1) → {outpath}\033[0m")
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for f in sorted(findings, key=lambda x: order.get(x.get("severity"), 9)):
        print(f"\n  [{f.get('severity','?').upper()}] {f.get('category','?')} — "
              f"{f.get('file','?')}:{f.get('line','?')}")
        print(f"    {f.get('description','')}")
        print(f"    fix: {f.get('fix','')}")
    _print_provenance(prov)
    print()
    return findings


def _print_provenance(prov):
    """Human-readable run trace beneath findings (or under a failed empty run)."""
    if not prov:
        return
    print("\n  ── provenance ──")
    order = [
        "stop_reason", "cancel_class", "turns", "attempts", "max_attempts",
        "retry_reason", "permission_mode", "sandbox", "write_protection",
        "read_only", "schema_enforced", "tokens",
        "usage_telemetry_usd", "usage_telemetry_note", "models",
        "had_reasoning", "reasoning_chars",
        "text_was_empty_findings_placeholder", "structured_output_error", "note",
    ]
    shown = set()
    for key in order:
        if key not in prov:
            continue
        shown.add(key)
        val = prov[key]
        if key == "tokens" and isinstance(val, dict):
            parts = [f"{k}={v}" for k, v in val.items() if v is not None]
            print(f"    tokens: {', '.join(parts)}")
        elif key == "usage_telemetry_usd":
            print(f"    usage_telemetry_usd: {val}  (not a card charge — CLI estimate)")
        else:
            print(f"    {key}: {val}")
    for key, val in prov.items():
        if key in shown or key == "target":
            continue
        print(f"    {key}: {val}")
