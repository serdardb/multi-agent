export const meta = {
  name: 'arena',
  description: 'Deterministic multi-model review arena: independent audit -> cross-critique (convergence-gated) -> code-verifying judge. Parameterized by args (agent, models, topic, mode, rounds). Round logic is FIXED here — nothing is improvised.',
  phases: [
    { title: 'Round 1 · Recon', detail: 'each model audits independently' },
    { title: 'Round 2 · Clash', detail: 'first cross-critique' },
    { title: 'Round 3 · Duel', detail: 'further cross-critique (dynamic, until convergence)' },
    { title: 'Round 4 · Rematch', detail: 'more rounds if positions still move' },
    { title: 'Judge', detail: 'code-verifying judge + scoreboard' },
  ],
}

// ---- parameters (from the /arena command via args) ----
// args may arrive as an object OR as a JSON string depending on how it was passed;
// accept both so a stringified payload doesn't silently fall back to defaults.
let A = args || {}
if (typeof A === 'string') {
  try { A = JSON.parse(A) } catch (e) { A = {} }
}
const AGENT = A.agent                                   // path to the agent .md
const AGENT_NAME = A.agentName || AGENT || 'agent'
const MODELS = Array.isArray(A.models)
  ? A.models
  : String(A.models || 'claude,grok,codex').split(',').map((s) => s.trim()).filter(Boolean)
const TOPIC = String(A.topic || '')
const MODE = A.mode === 'independent' ? 'independent' : 'adversarial'
const MIN = Math.max(1, Number(A.minRounds || 2))
const MAX = Math.max(MIN, Number(A.maxRounds || 4))
const JUDGE = A.judge !== false && A.judge !== 'false'
const PATH = A.path || '.'
// Judge should ideally NOT be one of the debaters (avoid self-judging bias).
// Prefer a usable model outside the debate; fall back to claude, then any debater.
const ALL_MODELS = ['claude', 'grok', 'codex']
const NEUTRAL = ALL_MODELS.filter((x) => !MODELS.includes(x))
const JUDGE_MODEL = A.judgeModel
  || (NEUTRAL.includes('claude') ? 'claude' : NEUTRAL[0])
  || (MODELS.includes('claude') ? 'claude' : MODELS[0])
const JUDGE_NEUTRAL = !MODELS.includes(JUDGE_MODEL)
// arena.py path: the /arena command resolves ${CLAUDE_PLUGIN_ROOT} and passes it
// as args.arenaPath (workflow JS can't rely on the plugin-root var itself). The
// fallback is only for direct/dev invocation outside the plugin.
const ARENA = A.arenaPath || 'arena.py'

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    model: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' }, line: { type: 'string' }, severity: { type: 'string' },
          category: { type: 'string' }, description: { type: 'string' }, fix: { type: 'string' },
          position: { type: 'string' },
        },
        required: ['file', 'severity', 'description'],
      },
    },
    note: { type: 'string' },
  },
  required: ['model', 'findings'],
}

const F = (o) => JSON.stringify((o && o.findings) || [])

// fun phase names for /workflows (round-by-round, so each round is its own node)
const ROUND_TAGS = { 1: 'Recon', 2: 'Clash', 3: 'Duel', 4: 'Rematch', 5: 'Finale' }
function roundName(rn) { return `Round ${rn} · ${ROUND_TAGS[rn] || ('Overtime ' + (rn - 5))}` }
const JUDGE_PHASE = 'Judge'

// One model, one round: a general-purpose agent drives `arena.py once` (which runs
// YOUR agent natively on that model with the arena schema) and returns its findings.
// The model's live reasoning/commands stream into THIS agent's node in /workflows.
function runModel(model, phaseTitle, tag, focusText) {
  const focusPath = `/tmp/arena-wf/${tag}-${model}.focus`
  return agent(
    `You have Bash. Drive ONE model auditor and return its findings JSON. It may take many minutes — that is expected; let it run to completion, use the LONGEST Bash timeout available, and do NOT kill it early. Steps EXACTLY:
1. mkdir -p /tmp/arena-wf
2. Write everything between <FOCUS> and </FOCUS> (verbatim) to ${focusPath}
3. Run this and WAIT for it to finish (it self-bounds internally, up to ~15 min):
   python3 ${ARENA} once --model ${model} --agent ${AGENT} --path ${PATH} --focus-file ${focusPath}
4. It prints ONE JSON object {"model":"${model}","findings":[...]}. Return EXACTLY that object. If it errors or prints no findings, return {"model":"${model}","findings":[],"note":"<what happened>"}.
<FOCUS>
${focusText}
</FOCUS>`,
    { label: `${phaseTitle}:${model}`, phase: phaseTitle, agentType: 'general-purpose', effort: 'low', schema: FINDINGS_SCHEMA }
  )
}

function round1Focus() {
  return TOPIC.trim() +
    '\n\nThis is an INDEPENDENT first-pass audit. Report every real finding, severity-ranked. ' +
    'Set "position":"NEW" on each finding.'
}

function critiqueFocus(minePrev, others) {
  const blocks = others.map(([name, f]) => `### ${name} — current findings:\n${JSON.stringify(f)}`)
  return TOPIC.trim() +
    '\n\n=== CROSS-CRITIQUE ROUND ===\n' +
    "Below are the OTHER auditors' current findings and your own previous list. Judge every item — " +
    'theirs and yours — against the ACTUAL code and return your UPDATED findings list. For each finding set "position":\n' +
    '  KEEP     = your finding still stands (cite the confirming code line)\n' +
    '  WITHDRAW = your earlier finding is wrong/unreachable/already-handled (say why)\n' +
    '  ADOPT    = another auditor found something real you had missed — include it\n' +
    '  NEW      = something new you found this round\n' +
    'Converge on truth; do NOT pad or restate to win. Cite file:line in every description.\n\n' +
    blocks.join('\n') +
    `\n### your previous findings:\n${JSON.stringify(minePrev)}`
}

// ---- convergence: has the debate stopped moving? ----
function keyset(findings) {
  const s = new Set()
  for (const x of findings || []) {
    if (String(x.position || '').toUpperCase() === 'WITHDRAW') continue
    const f = String(x.file || '').split('/').pop()
    const line = String(x.line == null ? '' : x.line).replace(/\D/g, '')
    const sev = String(x.severity || '').toLowerCase()
    s.add(f + '|' + line + '|' + sev)
  }
  return s
}
function sameSet(a, b) {
  if (a.size !== b.size) return false
  for (const k of a) if (!b.has(k)) return false
  return true
}
function converged(prevByModel, curByModel) {
  for (const m of Object.keys(curByModel)) {
    if (!sameSet(keyset((prevByModel[m] || {}).findings || []), keyset((curByModel[m] || {}).findings || []))) return false
  }
  return true
}

// ---- scoreboard (post-hoc, grounded in the judge's code-verification) ----
// Computed from data we already have — NO extra model calls, and the models are
// never told they're being scored (the debate stays about truth, not winning).
function sevW(s) { return ({ critical: 4, high: 3, medium: 2, low: 1 })[String(s || '').toLowerCase()] || 1 }
function fkey(x) { return String(x.file || '').split('/').pop() + '|' + String(x.line == null ? '' : x.line).replace(/\D/g, '') }

function computeScoreboard(roundMaps, judgeObj, models) {
  // each model's FINAL held findings (drop the ones it withdrew)
  const finalOf = {}, lastOf = {}
  for (const m of models) {
    let f = []
    for (const rd of roundMaps) if (rd[m] && rd[m].findings) f = rd[m].findings
    lastOf[m] = f || []
    finalOf[m] = (f || []).filter((x) => String(x.position || '').toUpperCase() !== 'WITHDRAW')
  }
  // ground truth: the judge's verdict per finding
  const verdict = {}
  if (judgeObj && judgeObj.findings) {
    for (const j of judgeObj.findings) {
      const cat = String(j.category || '').toUpperCase()
      let v = cat.startsWith('CONFIRMED') ? 'CONFIRMED'
        : cat.startsWith('REJECTED') ? 'REJECTED'
        : cat.startsWith('PARTIAL') ? 'PARTIAL'
        : (String(j.position || '').toUpperCase() === 'WITHDRAW' ? 'REJECTED' : 'CONFIRMED')
      verdict[fkey(j)] = { v, sev: j.severity }
    }
  }
  if (Object.keys(verdict).length === 0) return null   // no judge => no fair winner
  const holders = {}
  for (const m of models) for (const x of finalOf[m]) (holders[fkey(x)] = holders[fkey(x)] || new Set()).add(m)

  const board = []
  for (const m of models) {
    let pts = 0, confirmed = 0, falsePos = 0, unique = 0, concessions = 0
    const seen = new Set()
    for (const x of finalOf[m]) {
      const k = fkey(x); if (seen.has(k)) continue; seen.add(k)
      const vd = verdict[k]; if (!vd) continue
      const w = sevW(vd.sev || x.severity)
      if (vd.v === 'CONFIRMED') { confirmed++; pts += w; if ((holders[k] || new Set()).size === 1) { unique++; pts += 2 } }
      else if (vd.v === 'PARTIAL') { confirmed++; pts += w / 2 }
      else if (vd.v === 'REJECTED') { falsePos++; pts -= w }
    }
    for (const x of lastOf[m]) {   // reward honest position changes the judge agreed with
      const k = fkey(x), p = String(x.position || '').toUpperCase()
      if (p === 'WITHDRAW' && verdict[k] && verdict[k].v === 'REJECTED') { concessions++; pts += 1 }
      if (p === 'ADOPT' && verdict[k] && verdict[k].v === 'CONFIRMED') { concessions++; pts += 1 }
    }
    board.push({ model: m, points: Math.round(pts * 10) / 10, confirmed, false_positives: falsePos, unique, concessions })
  }
  board.sort((a, b) => b.points - a.points)
  const top = board.length ? board[0].points : 0
  const winners = board.filter((b) => b.points === top).map((b) => b.model)
  return { board, winner: winners.length === 1 ? winners[0] : null, tie: winners.length > 1 ? winners : null }
}

// ---- pipeline ----
log(`arena: ${AGENT_NAME} | models: ${MODELS.join(', ')} | mode: ${MODE} | rounds ${MIN}..${MAX}${JUDGE ? ' | +judge' : ''}`)

phase(roundName(1))
const r1 = await parallel(MODELS.map((m) => () => runModel(m, roundName(1), 'r1', round1Focus())))
let prev = {}
MODELS.forEach((m, i) => { prev[m] = r1[i] || { model: m, findings: [] } })
const rounds = [prev]

if (MODE === 'adversarial' && MODELS.length >= 2) {
  let rn = 1
  while (rn < MAX) {
    rn++
    phase(roundName(rn))
    const focusFor = {}
    for (const m of MODELS) {
      const others = MODELS.filter((o) => o !== m).map((o) => [o, (prev[o] || {}).findings || []])
      focusFor[m] = critiqueFocus((prev[m] || {}).findings || [], others)
    }
    const arr = await parallel(MODELS.map((m) => () => runModel(m, roundName(rn), 'r' + rn, focusFor[m])))
    const cur = {}
    MODELS.forEach((m, i) => { cur[m] = arr[i] || { model: m, findings: [] } })
    rounds.push(cur)
    const isConv = rn >= MIN && converged(prev, cur)
    prev = cur
    if (isConv) { log(`converged after round ${rn} — positions stopped moving`); break }
    log(`round ${rn}: positions still moving, another round`)
  }
}

let judge = null
if (JUDGE) {
  phase(JUDGE_PHASE)
  const inputs = []
  rounds.forEach((rd, i) => { for (const m of MODELS) inputs.push(`R${i + 1} ${m}: ${F(rd[m])}`) })
  const judgeFocus =
    'You are the JUDGE of a multi-model audit. CROSS-MODEL FINDINGS ARE UNVERIFIED CLAIMS — for every ' +
    'disputed or high-severity item OPEN THE ACTUAL CODE and confirm before ruling; cite the exact file:line ' +
    'you read. Produce the final consolidated finding list: merge duplicates, drop the disproven, keep only ' +
    'what the code supports. In each finding\'s "category" put "CONFIRMED: <cat>", "REJECTED: <cat>", or ' +
    '"PARTIAL: <cat>", and set "position":"KEEP" for confirmed, "WITHDRAW" for rejected. Note in the ' +
    'description how many models raised it. Rank most-severe first.\n\n' + TOPIC.trim() +
    '\n\n=== ALL ROUNDS ===\n' + inputs.join('\n')
  judge = await runModel(JUDGE_MODEL, JUDGE_PHASE, 'judge', judgeFocus)
}

const scoreboard = computeScoreboard(rounds, judge, MODELS)
if (scoreboard) {
  log('🏆 ' + (scoreboard.winner ? `winner: ${scoreboard.winner}` : `tie: ${scoreboard.tie.join(' = ')}`))
  for (const b of scoreboard.board) {
    log(`   ${b.model}: ${b.points} pts · ${b.confirmed} confirmed · ${b.false_positives} false · ${b.unique} unique · ${b.concessions} concessions`)
  }
}

return {
  agent: AGENT_NAME, models: MODELS, mode: MODE, rounds_run: rounds.length,
  judge_model: JUDGE ? JUDGE_MODEL : null,
  judge_neutral: JUDGE ? JUDGE_NEUTRAL : null,
  rounds: rounds.map((rd, i) => ({ round: i + 1, by_model: rd })),
  judge,
  scoreboard,
}
