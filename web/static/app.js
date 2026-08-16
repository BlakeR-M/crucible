/* Crucible, client side.
 *
 * The page is one growing tree. The planner is the root; each examiner is a
 * branch under it; each claim an examiner raises is a branch under that
 * examiner; each verifier's verdict is a leaf under the claim. Nothing moves
 * once it is placed, so the structure of the run is readable at any moment and
 * still readable afterwards.
 *
 * All state is derived from the event stream, and every event carries its
 * index, so a reconnect that replays the run from the beginning lands in the
 * same place instead of counting everything twice.
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
  agents: new Map(),    // agent id  -> {node, kids, acts, who, meta}
  claims: new Map(),    // finding id -> {node, kids, box, ruling, seen}
  raised: 0, stood: 0, out: 0, refused: 0,
  calls: 0, reads: 0, spend: 0,
};

const money = (n) => '$' + (n || 0).toFixed(4);
const short = (p) => !p ? '' : String(p).replace(/\\/g, '/').split('/').slice(-2).join('/');
const ROMAN = ['i', 'ii', 'iii', 'iv', 'v'];
const WORDS = ['no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven',
               'eight', 'nine', 'ten', 'eleven', 'twelve', 'thirteen',
               'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen',
               'nineteen', 'twenty'];
const say = (n) => (n < WORDS.length ? WORDS[n] : String(n));
const Say = (n) => { const w = say(n); return w[0].toUpperCase() + w.slice(1); };

function clock() {
  if (!S.started) return;
  const s = Math.floor((Date.now() - S.started) / 1000);
  $('clock').textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/* A node is a speaker, what it said, what it did, and its children. */
function node(parentKids, { name, role, live }) {
  const n = el('div', 'node in-ink');
  const who = el('div', 'who' + (live ? ' live' : ''));
  who.append(el('span', 'dot'));
  who.append(el('span', 'name', name));
  if (role) who.append(el('span', 'role', role));
  const meta = el('span', 'meta', '');
  who.append(meta);
  const says = el('p', 'says');
  const acts = el('div', 'acts');
  const kids = el('div', 'kids');
  n.append(who, says, acts, kids);
  parentKids.append(n);
  return { n, who, meta, says, acts, kids };
}

/* ------------------------------------------------------------------ start */

async function boot() {
  try {
    const res = await fetch('/api/tasks');
    if (res.status === 401) { location.href = '/login'; return; }
    const data = await res.json();
    const names = { full: 'The whole codebase', money: 'Money handling and totals',
                    concurrency: 'Ordering, races and idempotency',
                    auth: 'Access control and validation' };
    data.tasks.forEach((task, i) => {
      const label = el('label', i === 0 ? 'on' : '');
      const input = el('input');
      input.type = 'radio'; input.name = 'task'; input.value = task.key;
      input.checked = i === 0;
      input.addEventListener('change', () => {
        document.querySelectorAll('.choose label').forEach(l => l.classList.remove('on'));
        label.classList.add('on');
        $('subject').textContent = names[task.key] || task.key;
      });
      const t = el('span', 't');
      t.append(el('span', null, names[task.key] || task.key),
               el('span', 'd', ' — ' + task.label));
      label.append(input, el('span', 'm', ROMAN[i] + '.'), t);
      $('choose').append(label);
    });
    $('subject').textContent = names[data.tasks[0].key] || 'The whole codebase';
    $('state').textContent =
      `Bounded at ${data.max_concurrent} concurrent runs and ` +
      `$${data.ceiling_usd.toFixed(2)} of model spend per run`;
  } catch (err) {
    notice('The server could not be reached. ' + err.message);
  }
}

function notice(text) {
  $('notice').innerHTML = '';
  $('notice').append(el('p', 'notice', text));
}

$('begin').addEventListener('click', async () => {
  const picked = document.querySelector('input[name=task]:checked');
  $('begin').disabled = true; $('begin').textContent = 'Starting';
  $('notice').innerHTML = '';
  try {
    const res = await fetch('/api/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task: picked ? picked.value : 'full' }),
    });
    if (res.status === 401) { location.href = '/login'; return; }
    const data = await res.json();
    if (!res.ok) {
      notice(data.error || 'The run was refused.');
      $('begin').disabled = false; $('begin').textContent = 'Begin';
      return;
    }
    $('choose').style.display = 'none';
    $('begin').style.display = 'none';
    S.started = Date.now();
    S.timer = setInterval(clock, 1000);
    listen(data.run_id);
  } catch (err) {
    notice('Could not start. ' + err.message);
    $('begin').disabled = false; $('begin').textContent = 'Begin';
  }
});

/* ----------------------------------------------------------------- stream */

function listen(runId) {
  S.runId = runId;
  const source = new EventSource('/api/stream/' + runId);
  S.source = source;
  source.onmessage = (m) => {
    let e; try { e = JSON.parse(m.data); } catch { return; }
    if (typeof e.n === 'number') { if (e.n <= S.seen) return; S.seen = e.n; }
    handle(e);
  };
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED && S.started) {
      $('state').textContent = 'Connection closed. Reload to reattach.';
    }
  };
}

function handle(e) {
  switch (e.kind) {
    case 'run_started':     return onStart(e);
    case 'phase':           return onPhase(e);
    case 'lane':            return;
    case 'agent_started':   return onAgent(e);
    case 'agent_thought':   return onThought(e);
    case 'tool':            return onTool(e);
    case 'agent_done':
    case 'agent_finished':  return onAgentDone(e);
    case 'agent_exhausted':
    case 'agent_halted':
    case 'agent_error':     return onAgentStopped(e);
    case 'finding_raised':  return onRaised(e);
    case 'finding_merged':  return onMerged(e);
    case 'verdict':         return onVerdict(e);
    case 'finding_settled': return onSettled(e);
    case 'run_failed':      return notice(e.reason);
    case 'run_finished':    return onFinished(e);
  }
}

/* ------------------------------------------------------------------ nodes */

function onStart(e) {
  $('runid').textContent = e.run_id.slice(0, 8);
  $('phase').textContent = 'planning';

  const root = node($('thread'), { name: 'Planner', role: 'dividing the work', live: true });
  root.says.textContent = e.task || '';
  S.agents.set('__planner', root);

  const tools = (e.policy && e.policy.tools) || {};
  const line = el('div', 'a');
  line.append(el('b', null, 'authority  '),
              el('span', null, Object.keys(tools).sort().join(', ') +
                               ' · network refused'));
  root.acts.append(line);
  tally();
}

function onPhase(e) {
  const words = { plan: 'planning', hunt: 'reading the code', verify: 'under attack' };
  $('phase').textContent = words[e.phase] || e.phase;
  if (e.phase === 'verify') {
    const p = S.agents.get('__planner');
    if (p) p.who.classList.remove('live');
  }
}

function onAgent(e) {
  if (e.role === 'verifier') return;          // verifiers live under their claim
  if (S.agents.has(e.agent)) return;
  const planner = S.agents.get('__planner');
  if (!planner) return;
  const n = node(planner.kids, {
    name: 'Examiner ' + e.agent.replace('hunter-', ''),
    role: e.lane || '', live: true,
  });
  S.agents.set(e.agent, n);
}

function onThought(e) {
  S.calls += 1; S.spend += e.cost || 0;
  $('calls').textContent = S.calls + ' calls';
  $('spend').textContent = money(S.spend);
  const a = S.agents.get(e.agent);
  if (a) a.meta.textContent = 'step ' + ((e.step || 0) + 1);
}

function onTool(e) {
  if (e.refused) {
    S.refused += 1;
    $('dag').textContent = '† ' + S.refused + ' refused for want of authority';
  } else {
    S.reads += 1;
    $('reads').textContent = S.reads + ' reads';
  }
  // A verifier's actions belong to the claim it is judging, so they are shown
  // there rather than in a lane of their own.
  const owner = S.agents.get(e.agent) || verifierHost(e.agent);
  if (!owner) return;
  const line = el('div', 'a in-ink' + (e.refused ? ' no' : ''));
  line.append(el('b', null, (e.refused ? 'refused' : e.tool) + '  '));
  line.append(el('span', null, e.refused ? (e.reason || '')
    : (e.args && (short(e.args.path) || e.args.pattern || e.args.command) || '')));
  owner.acts.append(line);
  while (owner.acts.children.length > 40) owner.acts.removeChild(owner.acts.firstChild);
}

function verifierHost(agentId) {
  // verifier-<findingId>-<n>
  const m = /^verifier-([0-9a-f]+)-\d+$/.exec(agentId || '');
  const c = m && S.claims.get(m[1]);
  return c ? { acts: c.acts } : null;
}

function onAgentDone(e) {
  const a = S.agents.get(e.agent);
  if (!a) return;
  a.who.classList.remove('live'); a.who.classList.add('done');
}

function onAgentStopped(e) {
  const a = S.agents.get(e.agent);
  if (!a) return;
  a.who.classList.remove('live');
  const line = el('div', 'a no');
  line.append(el('b', null, 'stopped  '),
              el('span', null, e.reason || 'reached its step limit'));
  a.acts.append(line);
}

/* ----------------------------------------------------------------- claims */

function onRaised(e) {
  S.raised += 1;
  const wrap = el('div', 'node in-ink');
  const box = el('div', 'claim trying');
  box.append(el('span', 't', e.title));
  box.append(el('div', 'where', short(e.file) + (e.line ? ':' + e.line : '') +
                                '  ' + (e.severity || 'material')));
  box.append(el('p', 'sum', e.summary || ''));
  const acts = el('div', 'acts');
  const kids = el('div', 'kids');
  const ruling = el('div', 'ruling');
  ruling.append(el('span', 'w', 'under attack'), el('span', 'c', 'three verifiers'));
  wrap.append(box, acts, kids, ruling);

  // Attach under the examiner that raised it when we can identify it, else
  // under the planner, so a claim is never orphaned.
  const parent = laneHost(e.lane) || S.agents.get('__planner');
  parent.kids.append(wrap);

  S.claims.set(e.id, { wrap, box, kids, acts, ruling, seen: 0, data: e });
  tally();
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
  if (S.raised > 0) S.raised -= 1;
  const keep = S.claims.get(e.into);
  if (keep) {
    keep.corr = (keep.corr || 0) + 1;
    const w = keep.box.querySelector('.where');
    if (w) w.textContent += '  ·  raised independently by ' + say(keep.corr + 1) + ' examiners';
  }
  tally();
}

function onVerdict(e) {
  const c = S.claims.get(e.finding);
  if (!c) return;
  c.seen += 1;
  const v = el('div', 'verdict in-ink' + (e.refuted ? ' kills' : ''));
  v.append(el('span', 'n', '(' + ROMAN[c.seen - 1] + ')'));
  v.append(el('span', 'r',
    (e.refuted ? 'Refutes. ' : 'Cannot refute. ') + (e.reasoning || '')));
  c.kids.append(v);
  c.ruling.querySelector('.c').textContent = c.seen + ' of 3 returned';
}

function onSettled(e) {
  const c = S.claims.get(e.id);
  if (!c) return;
  c.box.classList.remove('trying');
  const w = c.ruling.querySelector('.w');
  const n = c.ruling.querySelector('.c');
  if (e.survived) {
    S.stood += 1;
    c.box.classList.add('stands');
    w.textContent = 'Stands';
    n.textContent = e.survived_by + ' of ' + (e.survived_by + e.refuted_by) +
                    ' could not refute it';
  } else {
    S.out += 1;
    c.box.classList.add('out');
    c.ruling.classList.add('out');
    w.textContent = 'Refuted';
    n.textContent = 'destroyed by ' + e.refuted_by + ' of ' +
                    (e.survived_by + e.refuted_by);
  }
  tally();
}

function tally() {
  const t = $('tally');
  t.innerHTML = '';
  if (!S.raised) { t.textContent = S.started ? 'nothing raised yet' : ''; return; }
  t.append(el('span', null, say(S.raised) + ' raised · '));
  if (S.out) t.append(el('b', 'out', say(S.out)), el('span', null, ' refuted · '));
  t.append(el('b', null, S.stood ? say(S.stood) : 'none'), el('span', null, ' standing'));
}

/* ---------------------------------------------------------------- closing */

function onFinished(e) {
  clearInterval(S.timer); clock();
  if (S.source) S.source.close();
  $('phase').textContent = 'closed';
  S.agents.forEach(a => { a.who.classList.remove('live'); a.who.classList.add('done'); });

  (e.findings || []).forEach(f => {
    const c = S.claims.get(f.id);
    if (c && f.failure_scenario && !c.box.querySelector('.repro')) {
      c.box.append(el('p', 'repro', f.failure_scenario));
    }
  });

  const killed = e.raised - e.survived;
  const attrition = e.raised ? Math.round(killed / e.raised * 100) : 0;

  const box = el('div', 'closing in-ink');
  box.append(el('h2', null, 'What survived'));
  const p = el('p');
  p.append(el('span', null,
    `${Say(e.lanes ? e.lanes.length : 0)} lines of attack were opened and ` +
    `${say(e.raised)} claim${e.raised === 1 ? '' : 's'} ` +
    `${e.raised === 1 ? 'was' : 'were'} raised. `));
  p.append(el('em', null,
    `${Say(killed)} ${killed === 1 ? 'was' : 'were'} destroyed under ` +
    `verification, an attrition of ${attrition} per cent. `));
  p.append(el('span', null,
    `${Say(e.survived)} stand${e.survived === 1 ? 's' : ''}, each with a ` +
    `reproduction you can check.`));
  box.append(p);

  const rows = el('div', 'rows');
  const row = (k, v) => { const d = el('div'); d.append(el('span', null, k), el('span', 'mono', v)); rows.append(d); };
  row('Model spend', money(e.spend_usd) + ' over ' + e.calls + ' calls');
  row('Actions taken', (e.tool_calls || 0) + ' reads, ' + (e.refusals || 0) + ' refused by policy');
  row('Elapsed', e.seconds + 's');
  row('Ledger root', (e.ledger_head || '').slice(0, 24).replace(/(.{4})/g, '$1 ').trim());
  box.append(rows);

  const link = el('p'); link.style.marginTop = '10px'; link.style.fontSize = 'var(--t-marg)';
  const a = el('a', null, 'Download the ledger and verify the chain yourself');
  a.href = '/api/ledger/' + e.run_id;
  link.append(a);
  box.append(link);

  $('closing').append(box);
  $('state').textContent = 'Run closed';
  if (e.halted) notice('The run stopped early: ' + e.halted);
}

boot();
