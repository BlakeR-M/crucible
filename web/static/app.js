/* Crucible, client side.
 *
 * The page holds no state of its own. Every number, every lane and every card
 * is derived from the event stream, so a dropped connection that reconnects
 * and replays lands in exactly the same place rather than in a plausible
 * looking wrong one.
 *
 * Nothing reaches the DOM except through textContent. The content here is
 * written by language models reading a stranger's code, which is precisely the
 * text that should never be parsed as markup.
 */

const $ = (id) => document.getElementById(id);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt !== undefined) n.textContent = txt;
  return n;
};

const S = {
  runId: null, started: 0, timer: null, source: null,
  agents: new Map(), trials: new Map(),
  raised: 0, kept: 0, killed: 0, refused: 0,
  calls: 0, tools: 0, spend: 0,
  ledger: [],
  // The server buffers every event and replays the whole run to a browser that
  // reconnects, which is what makes a dropped connection recoverable. The
  // counters below are accumulated rather than derived, so a replayed event
  // would be counted twice and the page would show a confident wrong total.
  // Each event carries its index; anything at or below the high-water mark has
  // already been applied.
  seen: -1,
};

const money = (n) => '$' + (n || 0).toFixed(4);
const pad2 = (n) => String(n).padStart(2, '0');
const shortPath = (p) => !p ? '' : String(p).replace(/\\/g, '/').split('/').slice(-2).join('/');

function clock() {
  if (!S.started) return;
  const s = Math.floor((Date.now() - S.started) / 1000);
  const t = `T+ ${pad2(Math.floor(s / 60))}:${pad2(s % 60)}`;
  $('clock').textContent = t;
  $('elapsed').textContent = t;
}

/* -------------------------------------------------------------------- idle */

async function boot() {
  try {
    const res = await fetch('/api/tasks');
    if (res.status === 401) { location.href = '/login'; return; }
    const data = await res.json();
    const names = { full: 'Full review', money: 'Money handling',
                    concurrency: 'Concurrency', auth: 'Access control' };
    data.tasks.forEach((task, i) => {
      const label = el('label', 'choice' + (i === 0 ? ' on' : ''));
      const input = el('input');
      input.type = 'radio'; input.name = 'task'; input.value = task.key;
      input.checked = i === 0;
      input.addEventListener('change', () => {
        document.querySelectorAll('.choice').forEach(c => c.classList.remove('on'));
        label.classList.add('on');
      });
      label.append(input, el('span', 't', names[task.key] || task.key),
                   el('span', 'd', task.label));
      $('choices').append(label);
    });
    $('ruleline').textContent =
      `ruleset review/1 · ${data.max_concurrent} concurrent runs · ` +
      `$${data.ceiling_usd.toFixed(2)} model ceiling per run, held against ` +
      `calls in flight`;
  } catch (err) {
    $('ruleline').textContent = 'Could not reach the server. ' + err.message;
  }
}

$('ignite').addEventListener('click', async () => {
  const picked = document.querySelector('input[name=task]:checked');
  const button = $('ignite');
  button.disabled = true; button.textContent = 'Igniting';
  try {
    const res = await fetch('/api/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task: picked ? picked.value : 'full' }),
    });
    if (res.status === 401) { location.href = '/login'; return; }
    const data = await res.json();
    if (!res.ok) {
      $('ruleline').textContent = data.error || 'The arena refused the run.';
      button.disabled = false; button.textContent = 'Ignite';
      return;
    }
    $('task').textContent = (picked && picked.parentElement
      .querySelector('.d').textContent) || '';
    document.body.classList.add('running', 'hot');
    S.started = Date.now();
    S.timer = setInterval(clock, 1000);
    listen(data.run_id);
  } catch (err) {
    $('ruleline').textContent = 'Could not start a run. ' + err.message;
    button.disabled = false; button.textContent = 'Ignite';
  }
});

/* ------------------------------------------------------------------ stream */

function listen(runId) {
  S.runId = runId;
  const source = new EventSource('/api/stream/' + runId);
  S.source = source;
  source.onopen = () => {
    $('dot').classList.remove('off');
    $('link').textContent = 'connected';
  };
  source.onmessage = (m) => {
    let e; try { e = JSON.parse(m.data); } catch { return; }
    if (typeof e.n === 'number') {
      if (e.n <= S.seen) return;   // already applied, this is a replay
      S.seen = e.n;
    }
    handle(e);
  };
  source.onerror = () => {
    $('dot').classList.add('off');
    // The server retires a stream before the edge does and replays the whole
    // buffer on reconnect, so this is usually a scheduled handover.
    $('link').textContent = source.readyState === EventSource.CLOSED
      ? 'closed' : 'reconnecting';
  };
}

function handle(e) {
  switch (e.kind) {
    case 'run_started':     return onStart(e);
    case 'phase':           return onPhase(e);
    case 'agent_started':   return onAgent(e);
    case 'agent_thought':   return onThought(e);
    case 'tool':            return onTool(e);
    case 'agent_done':
    case 'agent_finished':  return onAgentEnd(e, 'survived');
    case 'agent_exhausted':
    case 'agent_halted':
    case 'agent_error':     return onAgentStopped(e);
    case 'finding_raised':  return onRaised(e);
    case 'finding_merged':  return onMerged(e);
    case 'verdict':         return onVerdict(e);
    case 'finding_settled': return onSettled(e);
    case 'run_failed':      return alarm(e.reason);
    case 'run_finished':    return onFinish(e);
  }
}

function alarm(text) {
  const bar = el('div', 'alarm', text);
  $('fire').prepend(bar);
}

function spine(cls) {
  const seg = el('i', cls);
  $('spine').append(seg);
  const host = $('spine');
  // The spine is a fixed-height column; once it fills, the oldest segment goes
  // so the newest activity is always the visible part.
  if (host.children.length > Math.floor(host.clientHeight / 2)) {
    host.removeChild(host.firstChild);
  }
}

/* ------------------------------------------------------------------ render */

function onStart(e) {
  $('runid').textContent = 'run ' + e.run_id.slice(0, 8);
  $('task').textContent = e.task || '';
  const rules = $('rules');
  rules.innerHTML = '';
  const tools = (e.policy && e.policy.tools) || {};
  Object.keys(tools).forEach(name => {
    const rule = tools[name];
    const row = el('div', 'r');
    row.append(el('span', 't', name));
    let scope = '';
    if (rule.commands && rule.commands.length) scope = rule.commands.join(' ');
    else if (rule.path_scopes && rule.path_scopes.length) scope = shortPath(rule.path_scopes[0]) + '/';
    row.append(el('span', 's', scope));
    rules.append(row);
  });
  const no = el('div', 'r no');
  no.append(el('span', 't', 'network'), el('span', 's', 'refused'));
  rules.append(no);
}

function onPhase(e) {
  const words = { plan: 'planning', hunt: 'hunting', verify: 'verifying' };
  $('phase').textContent = words[e.phase] || e.phase;
  if (e.lanes) $('n-lanes').textContent = e.lanes;
}

function onAgent(e) {
  if (S.agents.has(e.agent)) return;
  if (e.role === 'verifier') { bumpAgents(); return; }  // verifiers show as pips
  const lane = el('div', 'lane running');
  const hd = el('div', 'hd');
  hd.append(el('span', 'glyph running'), el('span', 'id', e.agent));
  const n = el('span', 'n', '0');
  hd.append(n);
  const sub = el('div', 'sub', e.lane || '');
  const tape = el('div', 'tape');
  lane.append(hd, sub, tape);
  $('lanes').append(lane);
  S.agents.set(e.agent, { lane, tape, n, steps: 0 });
  bumpAgents();
}

function bumpAgents() {
  const total = (Number($('c-agents').textContent) || 0) + 1;
  $('c-agents').textContent = total;
}

function onThought(e) {
  S.calls += 1; S.spend += e.cost || 0;
  $('c-calls').textContent = S.calls;
  $('c-spend').textContent = money(S.spend);
  const a = S.agents.get(e.agent);
  if (!a) return;
  a.steps = (e.step || 0) + 1;
  a.n.textContent = a.steps;
}

function onTool(e) {
  if (e.refused) {
    S.refused += 1;
    $('c-refused').textContent = S.refused;
    $('c-refused').classList.add('warn');
    $('n-blocked').textContent = S.refused;
    const host = $('blocked');
    if (host.querySelector('.nil')) host.innerHTML = '';
    const row = el('div', 'blockrow');
    row.append(el('span', 'glyph refused'), el('span', 'r', e.reason || ''));
    host.prepend(row);
    spine('refused');
  } else {
    S.tools += 1;
    $('c-tools').textContent = S.tools;
    spine('call');
  }
  const a = S.agents.get(e.agent);
  if (!a) return;
  const line = el('div', 'l' + (e.refused ? ' no' : ''));
  line.append(el('span', 'v', e.refused ? 'refused' : e.tool));
  const detail = e.refused ? (e.reason || '')
    : (e.args && (shortPath(e.args.path) || e.args.pattern || e.args.command) || '');
  line.append(el('span', 'a', String(detail)));
  a.tape.append(line);
  a.tape.scrollTop = a.tape.scrollHeight;
}

function onAgentEnd(e) {
  const a = S.agents.get(e.agent);
  if (!a) return;
  a.lane.classList.remove('running');
  a.lane.classList.add('survived');
  a.lane.querySelector('.glyph').className = 'glyph survived';
}

function onAgentStopped(e) {
  const a = S.agents.get(e.agent);
  if (!a) return;
  a.lane.classList.remove('running');
  a.lane.classList.add('pending');
  a.lane.querySelector('.glyph').className = 'glyph refused';
  const line = el('div', 'l no');
  line.append(el('span', 'v', 'stopped'),
              el('span', 'a', e.reason || ('step limit ' + e.steps)));
  a.tape.append(line);
}

function onRaised(e) {
  S.raised += 1;
  $('r-all').textContent = pad2(S.raised);
  spine('raise');

  const host = $('fire');
  if (host.querySelector('.nil')) host.innerHTML = '';
  const card = el('div', 'trial');
  card.append(el('div', 't', e.title));
  card.append(el('div', 'w', shortPath(e.file) + (e.line ? ':' + e.line : '') +
                             '   ' + (e.severity || 'medium')));
  const pips = el('div', 'pips');
  for (let i = 0; i < 3; i++) pips.append(el('span', 'p'));
  const lbl = el('span', 'lbl', 'under attack');
  pips.append(lbl);
  card.append(pips);
  host.append(card);
  S.trials.set(e.id, { card, pips, lbl, filled: 0, data: e });
}

function onMerged(e) {
  // Two hunters reached the same defect independently. Shown as corroboration
  // on the surviving card rather than quietly dropped, because a reader should
  // know the claim arrived twice by separate routes.
  const t = S.trials.get(e.into);
  const dropped = S.trials.get(e.dropped);
  // The denominator must count what actually went on trial. Raised is
  // incremented per hunter report, and merges happen after, so without this
  // the ratio reads 08/12 while ten cards exist.
  if (S.raised > 0) { S.raised -= 1; $('r-all').textContent = pad2(S.raised); }
  if (dropped) { dropped.card.remove(); S.trials.delete(e.dropped); }
  if (!t) return;
  t.corroborated = (t.corroborated || 0) + 1;
  const badge = t.card.querySelector('.w');
  if (badge) badge.textContent += `   found by ${t.corroborated + 1} lanes`;
}

function onVerdict(e) {
  const t = S.trials.get(e.finding);
  if (!t) return;
  const pip = t.pips.children[t.filled];
  if (pip) pip.classList.add(e.refuted ? 'killed' : 'kept');
  t.filled += 1;
  spine(e.refuted ? 'killed' : 'kept');
}

function onSettled(e) {
  const t = S.trials.get(e.id);
  if (!t) return;
  const d = t.data;

  if (e.survived) {
    S.kept += 1;
    $('r-kept').textContent = pad2(S.kept);
    $('n-kept').textContent = S.kept;
    const host = $('kept');
    if (host.querySelector('.nil')) host.innerHTML = '';
    const card = el('div', 'won');
    card.append(el('span', 'sev', d.severity || 'medium'));
    card.append(el('div', 't', d.title));
    card.append(el('div', 'w', shortPath(d.file) + (d.line ? ':' + d.line : '')));
    card.append(el('div', 's', d.summary || ''));
    // Kept as a reference rather than found later by an attribute selector
    // built from a server-supplied id. The id is generated here and is safe
    // today, but interpolating any remote value into a selector is a habit
    // that eventually meets a value that breaks the parse.
    t.won = card;
    host.append(card);
  } else {
    S.killed += 1;
    $('n-killed').textContent = S.killed;
    const host = $('killed');
    if (host.querySelector('.nil')) host.innerHTML = '';
    const row = el('div', 'lost');
    row.append(el('span', 'glyph refuted'));
    row.append(el('span', 't', d.title));
    row.append(el('span', 'v', e.refuted_by + '/' + (e.refuted_by + e.survived_by)));
    host.append(row);
  }

  // Leave the verdict visible for a beat, then clear it out of the crucible.
  t.lbl.textContent = e.survived ? 'survived' : 'refuted';
  setTimeout(() => {
    t.card.classList.add('gone');
    setTimeout(() => {
      t.card.remove();
      $('n-trial').textContent = Math.max(0, S.raised - S.kept - S.killed);
      if (!$('fire').children.length) {
        $('fire').append(el('p', 'nil', 'All findings have been through the fire.'));
      }
    }, 360);
  }, 900);
  $('n-trial').textContent = Math.max(0, S.raised - S.kept - S.killed);
}

async function onFinish(e) {
  document.body.classList.remove('hot');
  $('phase').textContent = 'complete';
  clearInterval(S.timer);
  clock();
  if (S.source) S.source.close();
  $('link').textContent = 'run complete';

  // The full failure scenario only travels with the final report, and it is
  // the part a reader actually checks, so it is attached now.
  (e.findings || []).forEach(f => {
    const t = S.trials.get(f.id);
    if (t && t.won && f.failure_scenario && !t.won.querySelector('.f')) {
      t.won.append(el('div', 'f', f.failure_scenario));
    }
  });

  $('models').textContent = e.calls + ' model calls';
  const root = $('root');
  root.textContent = 'ledger ' + groups((e.ledger_head || '').slice(0, 16));
  root.onclick = () => navigator.clipboard && navigator.clipboard.writeText(e.ledger_head || '');

  if (e.halted) alarm('The run stopped early: ' + e.halted);
  if (!S.kept && !S.killed) {
    $('kept').innerHTML = '';
    $('kept').append(el('p', 'nil', 'No findings were raised in this run.'));
  }
  await loadLedger();
}

const groups = (hex) => (hex.match(/.{1,4}/g) || []).join(' ');

/* ------------------------------------------------------------- the ledger */

$('spine').addEventListener('click', () => $('drawer').classList.add('open'));
$('close').addEventListener('click', () => $('drawer').classList.remove('open'));
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') $('drawer').classList.remove('open');
});

async function loadLedger() {
  if (!S.runId) return;
  try {
    const res = await fetch('/api/ledger/' + S.runId);
    if (!res.ok) return;
    const text = await res.text();
    S.ledger = text.trim().split('\n').filter(Boolean).map(JSON.parse);
    const host = $('entries');
    host.innerHTML = '';
    S.ledger.slice(-400).forEach(entry => {
      const row = el('div', 'e');
      row.append(el('span', 'q', String(entry.seq)));
      row.append(el('span', 'k', entry.event));
      row.append(el('span', 'p', entry.hash.slice(0, 12) + '  ' +
                                 JSON.stringify(entry.payload).slice(0, 90)));
      host.append(row);
    });
  } catch { /* the drawer simply stays empty */ }
}

/* Recompute the whole chain in the browser.
 *
 * The canonical form has to match the writer exactly or every entry fails, so
 * this mirrors Python's json.dumps(sort_keys=True, separators=(',', ':')):
 * keys sorted at every depth, no whitespace, and non-ASCII escaped as \uXXXX
 * the way ensure_ascii does. Verifying here rather than server-side is the
 * point: the claim is checkable by the person who doubts it, on their machine,
 * without trusting anything this server says.
 */
function canonical(value) {
  const body = stable(value);
  return body.replace(/[-￿]/g,
    (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'));
}

function stable(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(stable).join(',') + ']';
  return '{' + Object.keys(value).sort()
    .map(k => JSON.stringify(k) + ':' + stable(value[k])).join(',') + '}';
}

async function sha256(text) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf))
    .map(b => b.toString(16).padStart(2, '0')).join('');
}

$('verify').addEventListener('click', async () => {
  const bar = $('verdictbar');
  if (!S.ledger.length) {
    bar.className = 'verdictbar bad';
    bar.textContent = 'No ledger loaded yet.';
    return;
  }
  if (!crypto.subtle) {
    bar.className = 'verdictbar bad';
    bar.textContent = 'This browser exposes no SubtleCrypto over an insecure origin.';
    return;
  }
  bar.className = 'verdictbar';
  bar.textContent = 'Recomputing ' + S.ledger.length + ' entries…';
  const t0 = performance.now();
  let prev = '0'.repeat(64);
  for (let i = 0; i < S.ledger.length; i++) {
    const e = S.ledger[i];
    const digest = await sha256(canonical({
      seq: e.seq, ts: e.ts, event: e.event, payload: e.payload, prev: e.prev,
    }));
    if (e.seq !== i || digest !== e.hash || e.prev !== prev) {
      bar.className = 'verdictbar bad';
      bar.textContent = `Chain broken at entry ${e.seq}.`;
      return;
    }
    prev = e.hash;
  }
  const ms = (performance.now() - t0).toFixed(0);
  bar.className = 'verdictbar';
  bar.textContent = `Chain intact. ${S.ledger.length} entries recomputed in ` +
                    `${ms}ms. Root ${groups(prev.slice(0, 16))}`;
});

boot();
