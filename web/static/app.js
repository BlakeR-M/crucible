/* Crucible, client side.
 *
 * One stream feeds this page: the run stream, replayed from the beginning if
 * the connection drops. All run state is derived from its events, and every
 * one carries its index, so a reconnect that replays lands in the same place
 * rather than double counting. A narration strip walks a first-time viewer
 * through the phases as the real run reaches them.
 *
 * Text reaches the DOM through textContent only. It is written by language
 * models reading a stranger's code, which is exactly the text that should
 * never be parsed as markup.
 */

const $ = (id) => document.getElementById(id);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt !== undefined) n.textContent = txt;
  return n;
};

const S = {
  runId: null, started: 0, timer: null, source: null, seen: -1,
  agents: new Map(), claims: new Map(),
  found: 0, keep: 0, out: 0, refused: 0,
  calls: 0, reads: 0, spend: 0,
  narStep: 0, narQueue: [], narBusy: false,
};

const money = (n) => '$' + (n || 0).toFixed(4);
const short = (p) => !p ? '' : String(p).replace(/\\/g, '/').split('/').slice(-2).join('/');
const ROMAN = ['i', 'ii', 'iii', 'iv', 'v'];
const WORDS = ['no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven',
               'eight', 'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen',
               'fifteen', 'sixteen', 'seventeen', 'eighteen', 'nineteen', 'twenty'];
const say = (n) => (n < WORDS.length ? WORDS[n] : String(n));
const Say = (n) => { const w = say(n); return w[0].toUpperCase() + w.slice(1); };

function clock() {
  if (!S.started) return;
  const s = Math.floor((Date.now() - S.started) / 1000);
  $('tick-clock').textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/* -------------------------------------------------------------- narration */

/* The step-by-step walkthrough. Each line appears the moment the real run
 * reaches that phase, so the narration is driven by events, never by a
 * script pretending. Steps only move forward. */

const NARRATE = [
  'The policy comes first: these agents may read the code, write only ' +
    'scratch, and run only the test commands. Everything else is refused ' +
    'before it executes.',
  'The planner reads the codebase and cuts the ground into lanes.',
  'Hunters spread out, one per lane, blind to each other, so four copies ' +
    'of the same easy bug cannot pass for a review.',
  'Findings start landing. Each one is a claim, and a claim has earned ' +
    'nothing yet.',
  'Now every finding is attacked: three verifiers each, told to refute it, ' +
    'with uncertainty counting as refutation. Two of three decide.',
  'The whole time, a probe has been genuinely trying to escape the ' +
    'sandbox. Refused, and every refusal is in the record.',
  '', // written by onFinished with the run's own numbers
];

/* Each line holds the screen long enough to read, even when the run's early
   events arrive in a burst; late lines queue behind it. */
const NAR_HOLD_MS = 2800;

function narrate(step, text) {
  if (step <= S.narStep) return;
  S.narStep = step;
  S.narQueue.push([step, text || NARRATE[step - 1]]);
  if (!S.narBusy) drainNarrate();
}

function drainNarrate() {
  const item = S.narQueue.shift();
  if (!item) { S.narBusy = false; return; }
  S.narBusy = true;
  $('narrate').hidden = false;
  $('narrate-step').textContent = `step ${item[0]} of ${NARRATE.length}`;
  $('narrate-text').textContent = item[1];
  setTimeout(drainNarrate, NAR_HOLD_MS);
}

async function findRun() {
  try {
    const r = await fetch('/api/runs/current');
    if (!r.ok) return;
    const d = await r.json();
    if (d.run_id && d.run_id !== S.runId) attach(d.run_id);
  } catch { /* nothing running is a normal answer */ }
}

/* ------------------------------------------------------------------- run */

function attach(runId) {
  if (S.runId === runId) return;
  // Close the previous run's stream before opening another, or the old one
  // keeps pushing events into a board that now belongs to a different run.
  if (S.source) { try { S.source.close(); } catch { /* already gone */ } }
  reset(runId);
  const source = new EventSource('/api/stream/' + runId);
  S.source = source;
  source.onmessage = (m) => {
    let e; try { e = JSON.parse(m.data); } catch { return; }
    if (typeof e.n === 'number') { if (e.n <= S.seen) return; S.seen = e.n; }
    handle(e);
  };
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED && S.started) {
      $('tick-state').textContent = 'connection closed';
    }
  };
}

function reset(runId) {
  S.runId = runId; S.started = Date.now(); S.seen = -1; S.provider = null;
  S.agents.clear(); S.claims.clear();
  S.found = S.keep = S.out = S.refused = S.calls = S.reads = 0; S.spend = 0;
  S.narStep = 0; S.narQueue = []; $('narrate').hidden = true;
  $('idle').style.display = 'none';
  $('thread').innerHTML = ''; $('closing').innerHTML = '';
  ['s-found', 's-out', 's-keep'].forEach(id => $(id).textContent = '0');
  $('runid').textContent = runId.slice(0, 8);
  clearInterval(S.timer); S.timer = setInterval(clock, 1000);
}

function handle(e) {
  switch (e.kind) {
    case 'clone_started':   return onCloneStarted(e);
    case 'clone_finished':  return onCloneFinished(e);
    case 'clone_failed':    return onCloneFailed(e);
    case 'provider':        return onProvider(e);
    case 'run_started':     return onStart(e);
    case 'phase':           return onPhase(e);
    case 'agent_started':   return onAgent(e);
    case 'agent_thought':   return onThought(e);
    case 'tool':            return onTool(e);
    case 'probe_result':    return onProbe(e);
    case 'agent_done':
    case 'agent_finished':  return onAgentDone(e);
    case 'agent_exhausted':
    case 'agent_halted':
    case 'agent_error':     return onAgentStopped(e);
    case 'finding_raised':  return onRaised(e);
    case 'finding_merged':  return onMerged(e);
    case 'verdict':         return onVerdict(e);
    case 'finding_settled': return onSettled(e);
    case 'run_finished':    return onFinished(e);
  }
}

function node(parentKids, { name, role, live }) {
  const n = el('div', 'node');
  const who = el('div', 'who' + (live ? ' live' : ''));
  who.append(el('span', 'dot'), el('span', 'name', name));
  if (role) who.append(el('span', 'role', role));
  const meta = el('span', 'meta', '');
  who.append(meta);
  const acts = el('div', 'acts');
  const kids = el('div', 'kids');
  n.append(who, acts, kids);
  parentKids.append(n);
  return { n, who, meta, acts, kids };
}

function onProvider(e) {
  // Arrives before there is a planner to hang it on; onStart writes it.
  S.provider = e;
  $('tick-state').textContent = { visitor: 'running on your key', operator: 'running on the house key',
                                  offline: 'running the stand-in model' }[e.provider_kind] || 'starting';
}

function onStart(e) {
  const root = node($('thread'), { name: 'Planner', role: 'splitting the work', live: true });
  root.acts.append(mkAct('task', e.task || ''));
  const kind = e.provider_kind || (S.provider && S.provider.provider_kind);
  if (kind) {
    const words = { visitor: 'your key', operator: 'the house key', offline: 'the stand-in model' };
    const m = e.models || (S.provider && S.provider.models) || {};
    root.acts.append(mkAct('model', (words[kind] || kind) +
      (m.planner ? `  ${m.planner} plans, ${m.worker} hunts, ${m.verifier} verifies` : '')));
  }
  if (e.repo_url) {
    root.acts.append(mkAct('source', e.repo_url + (e.commit_sha ? ' @ ' + String(e.commit_sha).slice(0, 12) : '')));
  }
  S.agents.set('__planner', root);
  $('state').textContent = 'running';
  narrate(1);
}

/* The clone, narrated. It sits above the planner in the thread, since it
 * happens before there is a planner, and it is the one part of a URL run
 * a visitor could not otherwise see. */
function onCloneStarted(e) {
  const n = node($('thread'), { name: 'Fetching', role: e.repo_url + (e.repo_ref ? ' @ ' + e.repo_ref : ''), live: true });
  S.agents.set('__clone', n);
  $('state').textContent = 'fetching';
  $('tick-state').textContent = 'cloning the repository';
}

function onCloneFinished(e) {
  const n = S.agents.get('__clone');
  if (!n) return;
  n.who.classList.remove('live'); n.who.classList.add('done');
  n.acts.append(mkAct('commit', String(e.commit_sha || '').slice(0, 12)));
  n.acts.append(mkAct('checkout', `${e.files} files, ${((e.bytes || 0) / 1e6).toFixed(2)} MB, history removed`));
}

function onCloneFailed(e) {
  const n = S.agents.get('__clone');
  if (n) {
    n.who.classList.remove('live'); n.who.classList.add('done');
    n.acts.append(mkAct('stopped', e.reason || 'the clone did not complete', true));
  }
  $('state').textContent = 'stopped';
}

function mkAct(verb, detail, bad) {
  const a = el('div', 'a' + (bad ? ' no' : ''));
  a.append(el('b', null, verb + '  '), el('span', null, String(detail)));
  return a;
}

function onPhase(e) {
  const words = { plan: 'splitting the work', hunt: 'reading the code',
                  verify: 'trying to disprove the findings' };
  $('tick-state').textContent = words[e.phase] || e.phase;
  $('state').textContent = words[e.phase] || e.phase;
  if (e.phase === 'plan') narrate(2);
  if (e.phase === 'hunt') narrate(3);
  if (e.phase === 'verify') narrate(5);
}

function onAgent(e) {
  if (e.role === 'verifier' || S.agents.has(e.agent)) return;
  const planner = S.agents.get('__planner');
  if (!planner) return;
  const isProbe = e.agent === 'prober';
  S.agents.set(e.agent, node(planner.kids, {
    name: isProbe ? 'Boundary probe' : 'Bug hunter ' + e.agent.replace('hunter-', ''),
    role: isProbe ? 'testing what it is not allowed to do' : (e.lane || ''),
    live: true,
  }));
}

function onThought(e) {
  S.calls += 1; S.spend += e.cost || 0;
  $('tick-calls').textContent = S.calls + ' calls';
  $('tick-spend').textContent = money(S.spend);
  const a = S.agents.get(e.agent);
  if (a) a.meta.textContent = 'step ' + ((e.step || 0) + 1);
}

function onTool(e) {
  if (e.refused) {
    S.refused += 1;
    $('tick-dag').textContent = S.refused + ' blocked by policy';
  } else {
    S.reads += 1;
    $('tick-reads').textContent = S.reads + ' reads';
  }
  const owner = S.agents.get(e.agent) || verifierHost(e.agent);
  if (!owner) return;
  owner.acts.append(mkAct(
    e.refused ? 'blocked' : e.tool,
    e.refused ? (e.reason || '')
      : (e.args && (short(e.args.path) || e.args.pattern || e.args.command) || ''),
    e.refused));
}

function onProbe(e) {
  const a = S.agents.get('prober');
  if (!a) return;
  const detail = e.note || e.agent_notes ||
    (e.attempts && e.attempts.length ? e.attempts.length + ' attempts' : '');
  a.acts.append(mkAct(e.held === false ? 'GOT THROUGH' : 'blocked',
                      detail, true));
  narrate(6);
}

function verifierHost(agentId) {
  const m = /^verifier-([0-9a-f]+)-\d+$/.exec(agentId || '');
  const c = m && S.claims.get(m[1]);
  return c ? { acts: c.acts } : null;
}

function onAgentDone(e) {
  const a = S.agents.get(e.agent);
  if (a) { a.who.classList.remove('live'); a.who.classList.add('done'); }
}

function onAgentStopped(e) {
  const a = S.agents.get(e.agent);
  if (!a) return;
  a.who.classList.remove('live');
  a.acts.append(mkAct('stopped', e.reason || 'reached its step limit', true));
}

function onRaised(e) {
  S.found += 1; $('s-found').textContent = S.found;
  narrate(4);
  const wrap = el('div', 'node');
  const box = el('div', 'claim trying');
  box.append(el('span', 't', e.title));
  box.append(el('div', 'where', short(e.file) + (e.line ? ':' + e.line : '') +
                                '  ' + (e.severity || 'medium')));
  box.append(el('p', 'sum', e.summary || ''));
  const acts = el('div', 'acts');
  const kids = el('div', 'kids');
  const ruling = el('div', 'ruling');
  ruling.append(el('span', 'w', 'being attacked'), el('span', 'c', 'three fact-checkers'));
  wrap.append(box, acts, kids, ruling);
  (laneHost(e.lane) || S.agents.get('__planner')).kids.append(wrap);
  S.claims.set(e.id, { wrap, box, kids, acts, ruling, seen: 0, data: e });
}

function laneHost(lane) {
  for (const [id, a] of S.agents) {
    if (id === '__planner') continue;
    const role = a.who.querySelector('.role');
    if (role && role.textContent === lane) return a;
  }
  return null;
}

function onMerged(e) {
  const drop = S.claims.get(e.dropped);
  if (drop) { drop.wrap.remove(); S.claims.delete(e.dropped); }
  if (S.found > 0) { S.found -= 1; $('s-found').textContent = S.found; }
  const keep = S.claims.get(e.into);
  if (keep) {
    keep.corr = (keep.corr || 0) + 1;
    const w = keep.box.querySelector('.where');
    if (w) w.textContent += '  ·  found independently by ' + say(keep.corr + 1) + ' hunters';
  }
}

function onVerdict(e) {
  const c = S.claims.get(e.finding);
  if (!c) return;
  c.seen += 1;
  const v = el('div', 'verdict' + (e.refuted ? ' kills' : ''));
  v.append(el('span', 'n', '(' + ROMAN[c.seen - 1] + ')'));
  v.append(el('span', 'r', (e.refuted ? 'Disproves it. ' : 'Could not disprove it. ') +
                           (e.reasoning || '')));
  c.kids.append(v);
  c.ruling.querySelector('.c').textContent = c.seen + ' of 3 back';
}

function onSettled(e) {
  const c = S.claims.get(e.id);
  if (!c) return;
  c.box.classList.remove('trying');
  const w = c.ruling.querySelector('.w'), n = c.ruling.querySelector('.c');
  const total = e.survived_by + e.refuted_by;
  if (e.survived) {
    S.keep += 1; $('s-keep').textContent = S.keep;
    c.box.classList.add('keep');
    w.textContent = 'Proven';
    n.textContent = e.survived_by + ' of ' + total + ' could not disprove it';
  } else {
    S.out += 1; $('s-out').textContent = S.out;
    c.box.classList.add('out'); c.ruling.classList.add('out');
    w.textContent = 'Disproved';
    n.textContent = e.refuted_by + ' of ' + total + ' knocked it down';
  }
}

function onFinished(e) {
  clearInterval(S.timer); clock();
  if (S.source) S.source.close();
  $('state').textContent = 'done';
  $('tick-state').textContent = 'run complete';
  S.agents.forEach(a => { a.who.classList.remove('live'); a.who.classList.add('done'); });

  (e.findings || []).forEach(f => {
    const c = S.claims.get(f.id);
    if (c && f.failure_scenario && !c.box.querySelector('.repro')) {
      c.box.append(el('p', 'repro', f.failure_scenario));
    }
  });

  const killed = e.raised - e.survived;
  narrate(7, `${Say(e.survived)} survived and ${say(killed)} were destroyed. ` +
    'The full ledger is below: verify the chain without leaving this browser.');
  const box = el('div', 'closing');
  box.append(el('h2', null, 'What survived'));
  const p = el('p');
  p.append(el('span', null, `${Say(e.raised)} possible bug${e.raised === 1 ? '' : 's'} ` +
    `${e.raised === 1 ? 'was' : 'were'} found. `));
  p.append(el('em', null, `${Say(killed)} ${killed === 1 ? 'was' : 'were'} disproved and thrown away. `));
  p.append(el('span', null, `${Say(e.survived)} ${e.survived === 1 ? 'is' : 'are'} left, ` +
    `each with a reproduction you can check.`));
  box.append(p);

  const rows = el('div', 'rows');
  const row = (k, v) => { const d = el('div'); d.append(el('span', null, k), el('span', 'mono', v)); rows.append(d); };
  row('Model spend', money(e.spend_usd) + ' over ' + e.calls + ' calls');
  row('Actions', (e.tool_calls || 0) + ' reads, ' + (e.refusals || 0) + ' blocked by policy');
  row('Elapsed', e.seconds + 's');
  row('Ledger root', (e.ledger_head || '').slice(0, 24).replace(/(.{4})/g, '$1 ').trim());
  box.append(rows);

  const link = el('p'); link.style.marginTop = '9px'; link.style.fontSize = 'var(--t-marg)';
  const a = el('a', null, 'Download the ledger');
  a.href = '/api/ledger/' + e.run_id;
  link.append(a);
  link.append(el('span', null, ' or '));
  const vbtn = el('button', 'verify-here', 'verify the chain in this browser');
  vbtn.type = 'button';
  link.append(vbtn);
  const vout = el('p', 'verify-out');
  vbtn.addEventListener('click', () => {
    vbtn.disabled = true;
    verifyChain(e.run_id, vout).finally(() => { vbtn.disabled = false; });
  });
  box.append(link, vout);

  $('closing').append(box);
  if (BYO.attached) byoRefresh();
}

/* ----------------------------------------------------------- chain check */

async function verifyChain(runId, out) {
  /* The same walk `crucible verify` does, recomputed here so the check does
     not rest on this server's word. Each file line is the canonical
     serialisation with sorted keys, so the bytes the hash covers are exactly
     the raw line with its own hash field cut out: byte surgery, never
     re-serialisation, which is what keeps this correct for every float and
     escape the file can carry. The policy replay is the CLI's half. */
  out.className = 'verify-out';
  out.textContent = 'fetching the ledger…';
  let text;
  try {
    const r = await fetch('/api/ledger/' + runId);
    if (!r.ok) { out.textContent = 'the ledger is out of reach just now'; return; }
    text = await r.text();
  } catch { out.textContent = 'the ledger is out of reach just now'; return; }
  const lines = text.split('\n').filter(l => l.trim());
  const enc = new TextEncoder();
  const fail = (msg) => { out.className = 'verify-out broke'; out.textContent = msg; };
  let prev = '0'.repeat(64);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const at = line.indexOf(',"hash":"');
    if (at < 0) { fail(`CHAIN BROKEN at entry ${i}: no hash recorded`); return; }
    const stored = line.substr(at + 9, 64);
    const body = line.slice(0, at) + line.slice(at + 9 + 64 + 1);
    const digest = await crypto.subtle.digest('SHA-256', enc.encode(body));
    const hex = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
    let entry;
    try { entry = JSON.parse(line); } catch {
      fail(`CHAIN BROKEN at entry ${i}: the line does not parse`); return;
    }
    if (entry.seq !== i) { fail(`CHAIN BROKEN: expected entry ${i}, found ${entry.seq}`); return; }
    if (hex !== stored) { fail(`CHAIN BROKEN at entry ${i}: contents do not match the recorded hash`); return; }
    if (entry.prev !== prev) { fail(`CHAIN BROKEN at entry ${i}: does not follow the entry before it`); return; }
    prev = stored;
  }
  out.textContent = `chain intact: ${lines.length} entries recomputed in this browser, ` +
    `head ${prev.slice(0, 16)}… The chain is this half of the check; ` +
    `“crucible verify” on your machine replays the policy decisions too.`;
}

/* ------------------------------------------------------------ repo form */

function hint(text, said) {
  const h = $('repo-hint');
  h.textContent = text || '';
  h.classList.toggle('said', !!said);
}

async function repoLimits() {
  try {
    const r = await fetch('/api/tasks');
    if (!r.ok) return;
    const d = await r.json();
    const hosts = (d.repo_hosts || []).join(' or ');
    hint(`Public repositories on ${hosts}, up to ${d.repo_max_mb} MB and ` +
         `${d.repo_max_files} files, one commit deep. Add @branch, @tag or @commit to pick a ref.`);
    S.offline = !!d.offline;
    if (d.offline) {
      const note = document.createElement('p');
      note.className = 'notice';
      note.id = 'offline-notice';
      const idle = $('idle');
      if (idle && !$('offline-notice')) idle.insertBefore(note, idle.firstChild);
    }
    if (d.public) {
      // An open deployment has nothing to sign out of.
      const out = document.querySelector('.head .out');
      if (out) out.hidden = true;
    }
    byoRender(d.byo || {});
  } catch { /* the field still works without the numbers */ }
}

/* -------------------------------------------------------------- your key */

const BYO = { enabled: false, ceiling: 1, max: 5, attached: false };

function offlineNotice() {
  const note = $('offline-notice');
  if (!note) return;
  note.textContent = 'This deployment runs the full arena with a stand-in model: the planner, hunters, ' +
    'verifiers, policy and ledger are real and every completion is scripted, so no findings here ' +
    'come from a live model. Live model runs switch on when the operator supplies a provider key' +
    (BYO.enabled && !BYO.attached ? ', or attach your own key below to run against a live model.' : '.');
}

function byoHint(text, said) {
  const h = $('byo-hint');
  h.textContent = text || '';
  h.classList.toggle('said', !!said);
}

function byoRender(d) {
  BYO.enabled = !!d.enabled;
  BYO.ceiling = d.run_ceiling_usd || BYO.ceiling;
  BYO.max = d.run_ceiling_max_usd || BYO.max;
  BYO.attached = !!d.attached;
  $('byo').hidden = !BYO.enabled;
  $('byo-form').hidden = BYO.attached;
  $('byo-held').hidden = !BYO.attached;
  if (BYO.attached) {
    $('byo-status').textContent =
      `Attached: ${d.provider} · this session only · spent $${(d.spent_usd || 0).toFixed(2)} of ` +
      `$${(d.ceiling_usd || BYO.ceiling).toFixed(2)} ceiling per run`;
    $('byo-key').value = '';
  } else {
    $('byo-ceiling').placeholder = `$${BYO.ceiling.toFixed(2)} per run`;
    $('byo-ceiling').max = String(BYO.max);
  }
  offlineNotice();
}

async function byoRefresh() {
  try {
    const r = await fetch('/api/key');
    if (r.ok) byoRender(await r.json());
  } catch { /* the block keeps its last state */ }
}

$('byo').addEventListener('submit', async (e) => {
  e.preventDefault();
  const key = $('byo-key').value.trim();
  if (!key) { byoHint('Paste the key you want this session to run on.', true); return; }
  $('byo-go').disabled = true;
  byoHint('Checking the key with the provider.');
  try {
    const body = { provider: $('byo-provider').value, api_key: key };
    const ceiling = parseFloat($('byo-ceiling').value);
    if (ceiling > 0) body.ceiling_usd = ceiling;
    const r = await fetch('/api/key', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { byoHint(d.error || 'The key was kept out. Check it and try again.', true); return; }
    byoHint('Attached. Runs and the conversation now use your key.');
    byoRender({ ...d, enabled: true });
  } catch {
    byoHint('The server is out of reach just now. Try again in a moment.', true);
  } finally {
    $('byo-go').disabled = false;
    $('byo-key').value = '';
  }
});

$('byo-forget').addEventListener('click', async () => {
  try {
    const r = await fetch('/api/key', { method: 'DELETE' });
    const d = await r.json().catch(() => ({}));
    byoRender({ ...d, enabled: true });
    byoHint('Forgotten. Runs go back to this deployment’s own model.');
  } catch {
    byoHint('The server is out of reach just now. Try again in a moment.', true);
  }
});

$('demo-go').addEventListener('click', async () => {
  $('demo-go').disabled = true;
  try {
    const r = await fetch('/api/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task: 'full' }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { hint(d.error || 'The arena declined that one. Try again in a moment.', true); return; }
    attach(d.run_id);
  } catch {
    hint('The server is out of reach just now. Try again in a moment.', true);
  } finally {
    $('demo-go').disabled = false;
  }
});

$('repo').addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = $('repo-url').value.trim();
  if (!url) { hint('Paste a repository address to review, such as https://github.com/org/repo.', true); return; }
  if (!/^(https?:\/\/|git@|ssh:\/\/)/i.test(url)) {
    hint('That reads as a path. This field takes a URL, in the form https://github.com/org/repo.', true);
    return;
  }
  $('repo-go').disabled = true;
  hint('Asking the arena for a slot.');
  try {
    const r = await fetch('/api/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo_url: url, task: 'full' }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { hint(d.error || 'The arena declined that one. Try again in a moment.', true); return; }
    hint('');
    attach(d.run_id);
  } catch {
    hint('The server is out of reach just now. Try again in a moment.', true);
  } finally {
    $('repo-go').disabled = false;
  }
});

findRun();
repoLimits();
