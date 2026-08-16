/* Crucible, client side.
 *
 * The page is a certificate that fills itself in. Every slot exists before the
 * run does, holding a rule, and events replace rules with text. Nothing is
 * created and destroyed as the run proceeds, which is why the document does
 * not jump about while it is being written.
 *
 * All state is derived from the event stream. Each event carries its index, so
 * a reconnect that replays the run from the beginning lands in the same place
 * rather than counting everything twice.
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
  hunters: new Map(), specimens: new Map(),
  raised: 0, stood: 0, struck: 0, refused: 0,
  calls: 0, tools: 0, spend: 0,
};

const money = (n) => '$' + (n || 0).toFixed(4);
const short = (p) => !p ? '' : String(p).replace(/\\/g, '/').split('/').slice(-2).join('/');
const ROMAN = ['i', 'ii', 'iii', 'iv', 'v'];

// Quantities read as words in prose and as figures in anything that mutates.
const WORDS = ['no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven',
               'eight', 'nine', 'ten', 'eleven', 'twelve', 'thirteen',
               'fourteen', 'fifteen', 'sixteen', 'seventeen', 'eighteen',
               'nineteen', 'twenty'];
const say = (n) => (n < WORDS.length ? WORDS[n] : String(n));

function clock() {
  if (!S.started) return;
  const s = Math.floor((Date.now() - S.started) / 1000);
  $('clock').textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function stamp() {
  const d = new Date();
  const months = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                  'August', 'September', 'October', 'November', 'December'];
  return `${d.getDate()} ${months[d.getMonth()]} ${d.getFullYear()}, ` +
         `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/* ------------------------------------------------------------ nomination */

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
      const wrap = el('span', 't');
      wrap.append(el('span', null, names[task.key] || task.key),
                  el('span', 'd', ' ' + task.label));
      label.append(input, el('span', 'mark', ROMAN[i] + '.'), wrap);
      $('choose').append(label);
    });
    $('subject').textContent = names[data.tasks[0].key] || 'A codebase';
    $('state').textContent =
      `Awaiting nomination · bounded at ${data.max_concurrent} concurrent ` +
      `examinations and $${data.ceiling_usd.toFixed(2)} per run`;
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
  $('begin').disabled = true;
  $('begin').textContent = 'Beginning';
  $('notice').innerHTML = '';
  try {
    const res = await fetch('/api/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task: picked ? picked.value : 'full' }),
    });
    if (res.status === 401) { location.href = '/login'; return; }
    const data = await res.json();
    if (!res.ok) {
      notice(data.error || 'The examination was refused.');
      $('begin').disabled = false; $('begin').textContent = 'Begin examination';
      return;
    }
    $('nominate').style.display = 'none';
    document.querySelector('.marg.sec').style.display = 'none';
    $('issued').textContent = 'Examined ' + stamp();
    S.started = Date.now();
    S.timer = setInterval(clock, 1000);
    listen(data.run_id);
  } catch (err) {
    notice('The examination could not be started. ' + err.message);
    $('begin').disabled = false; $('begin').textContent = 'Begin examination';
  }
});

/* ---------------------------------------------------------------- stream */

function listen(runId) {
  S.runId = runId;
  const source = new EventSource('/api/stream/' + runId);
  S.source = source;
  source.onmessage = (m) => {
    let e; try { e = JSON.parse(m.data); } catch { return; }
    if (typeof e.n === 'number') {
      if (e.n <= S.seen) return;
      S.seen = e.n;
    }
    handle(e);
  };
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED && !document.body.classList.contains('settled')) {
      $('state').textContent = 'Connection closed. Reload to reattach.';
    }
  };
}

function handle(e) {
  switch (e.kind) {
    case 'run_started':     return onStart(e);
    case 'phase':           return onPhase(e);
    case 'lane':            return;
    case 'agent_started':   return onHunter(e);
    case 'agent_thought':   return onThought(e);
    case 'tool':            return onTool(e);
    case 'agent_done':
    case 'agent_finished':  return onHunterDone(e);
    case 'agent_exhausted':
    case 'agent_halted':
    case 'agent_error':     return onHunterStopped(e);
    case 'finding_raised':  return onRaised(e);
    case 'finding_merged':  return onMerged(e);
    case 'verdict':         return onVerdict(e);
    case 'finding_settled': return onSettled(e);
    case 'run_failed':      return notice(e.reason);
    case 'run_finished':    return onFinished(e);
  }
}

/* ---------------------------------------------------------------- filling */

function onStart(e) {
  $('report').textContent = 'CRUCIBLE / EX / ' + e.run_id.slice(0, 8).toUpperCase();
  updateTally();

  const auth = $('authority');
  auth.innerHTML = '';
  const tools = (e.policy && e.policy.tools) || {};
  Object.keys(tools).forEach(name => {
    const rule = tools[name];
    let scope = '';
    if (rule.commands && rule.commands.length) scope = 'may run ' + rule.commands.join(', ');
    else if (rule.path_scopes && rule.path_scopes.length) scope = 'within the specimen directory';
    const li = el('li', 'arrives');
    li.append(el('span', 'n', ''), el('span', null, name + ' — ' + scope));
    auth.append(li);
  });
  const deny = el('li', 'arrives');
  const d = el('span', null, 'network — refused entirely');
  d.style.color = 'var(--rubric)';
  deny.append(el('span', 'n', ''), d);
  auth.append(deny);
}

function onPhase(e) {
  const words = {
    plan: 'Apportioning the examination',
    hunt: 'Under examination',
    verify: 'Specimens put to the examiners',
  };
  $('state').textContent = words[e.phase] || e.phase;
  if (e.phase === 'plan') $('lanes').innerHTML = '';
}

function onHunter(e) {
  if (e.role === 'verifier') return;
  if (S.hunters.has(e.agent)) return;

  const li = el('li', 'arrives');
  li.append(el('span', 'n', S.hunters.size + 1 + '.'));
  li.append(el('span', null, e.lane || ''));
  li.append(el('span', 'who', e.agent.replace('hunter-', 'examiner ')));
  $('lanes').append(li);

  const col = el('div', 'hunter');
  const head = el('div', 'h', e.agent.replace('hunter-', 'examiner '));
  const tape = el('div', 'tape');
  col.append(head, tape);
  $('hunters').append(col);
  S.hunters.set(e.agent, { head, tape });
}

function onThought(e) {
  S.calls += 1; S.spend += e.cost || 0;
  $('calls').textContent = S.calls + ' calls';
  $('spend').textContent = money(S.spend);
}

function onTool(e) {
  if (e.refused) {
    S.refused += 1;
    $('daggers').textContent = '† ' + S.refused + ' refused for want of authority';
  } else {
    S.tools += 1;
    $('tools').textContent = S.tools + ' reads';
  }
  const h = S.hunters.get(e.agent);
  if (!h) return;
  const line = el('div', e.refused ? 'no' : '');
  line.textContent = e.refused
    ? '† ' + (e.reason || '')
    : e.tool + '  ' + (e.args && (short(e.args.path) || e.args.pattern || e.args.command) || '');
  h.tape.append(line);
  while (h.tape.children.length > 12) h.tape.removeChild(h.tape.firstChild);
}

function onHunterDone(e) {
  const h = S.hunters.get(e.agent);
  if (h) h.head.classList.add('done');
}

function onHunterStopped(e) {
  const h = S.hunters.get(e.agent);
  if (!h) return;
  const line = el('div', 'no');
  line.textContent = '† ' + (e.reason || 'reached its step limit');
  h.tape.append(line);
}

/* -------------------------------------------------------------- specimens */

function onRaised(e) {
  S.raised += 1;
  const host = $('findings');
  // Remove the placeholder by its own id. Matching on a class the specimens
  // themselves also carry meant every new specimen wiped the ones before it.
  const placeholder = $('nospecimens');
  if (placeholder) placeholder.remove();

  const wrap = el('div', 'spec');
  const disp = el('div', 'marg disposition');
  disp.append(el('span', 'word', 'under examination'));
  disp.append(el('span', 'count', ''));

  const body = el('div', 'meas finding trying arrives');
  body.append(el('span', 'num', '[' + S.raised + ']'));
  body.append(el('h2', null, e.title));
  const cite = el('div', 'cite');
  cite.append(el('span', 'mono', short(e.file) + (e.line ? ' at line ' + e.line : '')));
  cite.append(el('span', null, ' · '));
  cite.append(el('span', 'sev', e.severity || 'material'));
  body.append(cite);
  body.append(el('p', 'sum', e.summary || ''));
  const verdicts = el('div', 'verdicts');
  body.append(verdicts);

  wrap.append(disp, body);
  host.append(wrap);
  S.specimens.set(e.id, { wrap, body, disp, verdicts, n: S.raised, data: e, seen: 0 });
  updateTally();
}

function onMerged(e) {
  const keep = S.specimens.get(e.into);
  const drop = S.specimens.get(e.dropped);
  if (drop) { drop.wrap.remove(); S.specimens.delete(e.dropped); }
  if (S.raised > 0) S.raised -= 1;
  renumber();
  if (keep) {
    keep.corroborated = (keep.corroborated || 0) + 1;
    const cite = keep.body.querySelector('.cite');
    if (cite) cite.append(el('span', null,
      ' · raised independently by ' + say(keep.corroborated + 1) + ' examiners'));
  }
  updateTally();
}

function renumber() {
  let n = 0;
  S.specimens.forEach(s => { n += 1; s.n = n; s.body.querySelector('.num').textContent = '[' + n + ']'; });
}

function onVerdict(e) {
  const s = S.specimens.get(e.finding);
  if (!s) return;
  s.seen += 1;
  const line = el('div', 'v arrives' + (e.refuted ? ' killer' : ''));
  const r = el('span', 'r', e.reasoning || (e.refuted ? 'refutes.' : 'does not refute.'));
  line.append(el('span', null, '(' + ROMAN[s.seen - 1] + ') Examiner ' +
                (e.refuted ? 'refutes: ' : 'declines to refute: ')), r);
  s.verdicts.append(line);
  s.disp.querySelector('.count').textContent = s.seen + ' of 3 returned';
}

function onSettled(e) {
  const s = S.specimens.get(e.id);
  if (!s) return;
  s.body.classList.remove('trying');
  const word = s.disp.querySelector('.word');
  const count = s.disp.querySelector('.count');

  if (e.survived) {
    S.stood += 1;
    word.textContent = 'stands';
    count.textContent = e.survived_by + ' of ' + (e.survived_by + e.refuted_by) +
                        ' declined to refute';
  } else {
    S.struck += 1;
    s.body.classList.add('struck');
    s.disp.classList.add('struck');
    word.textContent = 'struck out';
    count.textContent = 'refuted by ' + e.refuted_by + ' of ' +
                        (e.survived_by + e.refuted_by);
  }
  updateTally();
}

function updateTally() {
  const t = $('tally');
  t.innerHTML = '';
  if (!S.raised) { t.textContent = 'examination in progress'; return; }
  const pending = S.raised - S.struck - S.stood;
  t.append(el('span', null, say(S.raised) + ' raised · '));
  if (S.struck) {
    t.append(el('b', 'struck', say(S.struck)), el('span', null, ' struck out · '));
  }
  // "none standing" rather than "no standing", which reads as a missing noun.
  t.append(el('b', null, S.stood ? say(S.stood) : 'none'),
           el('span', null, ' standing'));
  if (pending > 0) t.append(el('span', null, ' · ' + say(pending) + ' under examination'));
}

/* --------------------------------------------------------------- issuing */

function onFinished(e) {
  clearInterval(S.timer);
  clock();
  if (S.source) S.source.close();

  (e.findings || []).forEach(f => {
    const s = S.specimens.get(f.id);
    if (s && f.failure_scenario && !s.body.querySelector('.fail')) {
      s.body.append(el('p', 'fail arrives', f.failure_scenario));
    }
  });

  const attrition = e.raised ? Math.round((e.raised - e.survived) / e.raised * 100) : 0;
  const head = $('headnote');
  head.innerHTML = '';
  head.append(el('span', null,
    `${say(e.lanes ? e.lanes.length : 0).replace(/^n/, 'N')} lines of examination were opened. ` +
    `${say(e.raised).replace(/^n/, 'N')} specimen${e.raised === 1 ? '' : 's'} ` +
    `${e.raised === 1 ? 'was' : 'were'} raised. `));
  const kill = el('em', null,
    `${say(e.raised - e.survived).replace(/^n/, 'N')} ` +
    `${e.raised - e.survived === 1 ? 'was' : 'were'} struck out on refutation, ` +
    `an attrition of ${attrition} per cent. `);
  head.append(kill);
  head.append(el('span', null,
    `${say(e.survived).replace(/^n/, 'N')} stand${e.survived === 1 ? 's' : ''}.`));

  $('state').textContent = 'Examination closed';
  $('root').textContent = (e.ledger_head || '').slice(0, 32).replace(/(.{4})/g, '$1 ').trim();
  $('entries').textContent = (e.tool_calls || 0) + ' actions, ' + (e.refusals || 0) + ' refused';
  $('issuedfoot').textContent = stamp();

  const link = el('div', 'row');
  const a = el('a', null, 'Download the ledger and verify the chain');
  a.href = '/api/ledger/' + e.run_id;
  link.append(a);
  $('colophon').append(link);

  if (e.halted) notice('The examination stopped early: ' + e.halted);

  // The apparatus collapses and the certificate is left alone. This is the
  // state that gets screenshotted, so nothing operational stays in frame.
  document.body.classList.add('settled');
}

boot();
