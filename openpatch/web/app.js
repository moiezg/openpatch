'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const TOKEN_KEY = 'openpatch.token';
let meta = null;
let lastStatus = null;
let pollTimer = null;

// ---------- API ----------

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { 'Authorization': 'Bearer ' + (localStorage.getItem(TOKEN_KEY) || ''), 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { showLogin('Access token rejected.'); throw new Error('unauthorized'); }
  if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

// ---------- helpers ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

function clock(epochMin) {
  return new Date(epochMin * 60000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function simClock(m) {
  const d = Math.floor(m / 1440) + 1, h = Math.floor((m % 1440) / 60), mm = Math.round(m % 60);
  return `D${d} ${String(h).padStart(2, '0')}:${String(mm).padStart(2, '0')}`;
}

function arrow(delta) {
  if (delta === null || delta === undefined) return '';
  if (delta < -15) return '⇊'; if (delta < -10) return '↓'; if (delta < -5) return '↘';
  if (delta < 5) return '→'; if (delta < 10) return '↗'; if (delta < 15) return '↑'; return '⇈';
}

function rangeClass(v) { return v < 70 ? 'low' : v > 180 ? 'high' : 'inrange'; }

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function toast(msg) { alert(msg); }

function promptDialog(title, text, fields) {
  return new Promise((resolve) => {
    const dlg = $('#prompt');
    $('#prompt-title').textContent = title;
    $('#prompt-text').textContent = text || '';
    const box = $('#prompt-fields');
    box.replaceChildren(...fields.map((f) => el('label', {}, f.label,
      el('input', { id: 'pf-' + f.id, type: f.type || 'number', inputmode: 'decimal', step: f.step || 'any', value: f.value ?? '' }))));
    dlg.returnValue = '';
    dlg.onclose = () => {
      if (dlg.returnValue !== 'ok') return resolve(null);
      const out = {};
      for (const f of fields) out[f.id] = $('#pf-' + f.id).value;
      resolve(out);
    };
    dlg.showModal();
  });
}

// ---------- charts ----------

function drawChart(canvas, o) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  const pad = { l: 34, r: 8, t: 8, b: 20 };
  const X = (t) => pad.l + (t - o.x0) / (o.x1 - o.x0) * (w - pad.l - pad.r);
  const Y = (v) => h - pad.b - (Math.min(Math.max(v, o.y0), o.y1) - o.y0) / (o.y1 - o.y0) * (h - pad.t - pad.b);
  const muted = cssVar('--muted'), line = cssVar('--line');

  if (o.band) {
    ctx.fillStyle = cssVar('--band');
    ctx.fillRect(pad.l, Y(o.band[1]), w - pad.l - pad.r, Y(o.band[0]) - Y(o.band[1]));
  }
  ctx.font = '11px -apple-system, sans-serif';
  ctx.fillStyle = muted; ctx.strokeStyle = line; ctx.lineWidth = 1;
  for (const v of o.yTicks || []) {
    ctx.beginPath(); ctx.moveTo(pad.l, Y(v)); ctx.lineTo(w - pad.r, Y(v)); ctx.stroke();
    ctx.fillText(String(v), 2, Y(v) + 4);
  }
  if (o.xTickEvery) {
    const first = Math.ceil(o.x0 / o.xTickEvery) * o.xTickEvery;
    let lastLabelX = -1e9;
    for (let t = first; t <= o.x1; t += o.xTickEvery) {
      const x = X(t);
      ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, h - pad.b); ctx.stroke();
      if (x - lastLabelX > 52) { ctx.fillText(o.xFmt(t), x - 16, h - 5); lastLabelX = x; }
    }
  }
  for (const b of o.bars || []) {
    ctx.fillStyle = b.color;
    for (const [t, v, width] of b.points) {
      const x0 = X(t), x1 = X(t + width);
      ctx.fillRect(x0, Y(v), Math.max(1, x1 - x0), Y(o.y0) - Y(v));
    }
  }
  for (const s of o.lines || []) {
    ctx.strokeStyle = s.color; ctx.lineWidth = s.width || 2; ctx.setLineDash(s.dash || []);
    ctx.beginPath();
    let started = false;
    for (const [t, v] of s.points) {
      if (v === null || v === undefined) { started = false; continue; }
      if (!started) { ctx.moveTo(X(t), Y(v)); started = true; } else ctx.lineTo(X(t), Y(v));
    }
    ctx.stroke(); ctx.setLineDash([]);
  }
  for (const d of o.dots || []) {
    for (const [t, v] of d.points) {
      ctx.fillStyle = d.color ? d.color : cssVar('--' + rangeClass(v));
      ctx.beginPath(); ctx.arc(X(t), Y(v), d.r || 2.6, 0, Math.PI * 2); ctx.fill();
    }
  }
  for (const m of o.markers || []) {
    ctx.fillStyle = m.color;
    for (const [t, label] of m.points) {
      const x = X(t), y = m.top ? pad.t + 2 : h - pad.b - 2;
      ctx.beginPath(); ctx.moveTo(x, y + (m.top ? 8 : -8)); ctx.lineTo(x - 4, y); ctx.lineTo(x + 4, y); ctx.fill();
      if (label) ctx.fillText(label, x + 5, m.top ? y + 9 : y - 2);
    }
  }
  if (o.nowX !== undefined) {
    ctx.strokeStyle = muted; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(X(o.nowX), pad.t); ctx.lineTo(X(o.nowX), h - pad.b); ctx.stroke(); ctx.setLineDash([]);
  }
}

// ---------- home ----------

async function refresh() {
  try {
    const s = await api('GET', '/api/status');
    lastStatus = s;
    renderHome(s);
  } catch (e) {
    if (e.message !== 'unauthorized') renderWarnings(['Cannot reach OpenPatch server: ' + e.message]);
  }
}

function renderWarnings(list) {
  $('#warnings').replaceChildren(...list.map((w) => el('div', { class: 'warning' }, w)));
}

function renderHome(s) {
  renderWarnings(s.warnings || []);
  renderPump(s);
  const badge = $('#mode-badge');
  badge.textContent = s.mode; badge.className = 'badge ' + s.mode;

  const bgEl = $('#bg-value');
  if (s.bg === null) {
    bgEl.textContent = '--'; bgEl.className = 'bg-value';
    $('#bg-sub').textContent = 'no CGM data'; $('#bg-arrow').textContent = '';
  } else {
    bgEl.textContent = Math.round(s.bg); bgEl.className = 'bg-value ' + rangeClass(s.bg);
    $('#bg-arrow').textContent = arrow(s.delta);
    const age = Math.max(0, Math.round(s.now - s.bg_time));
    $('#bg-sub').textContent = `mg/dL · ${age} min ago`;
  }
  const d = s.decision || {};
  $('#iob').textContent = d.iob !== undefined ? d.iob.toFixed(2) + ' U' : '--';
  $('#cob').textContent = d.cob !== undefined ? Math.round(d.cob) + ' g' : '--';
  $('#target').textContent = Math.round(s.target) + (s.temp_target ? ' *' : '');
  $('#delta').textContent = s.delta === null ? '--' : (s.delta > 0 ? '+' : '') + s.delta;

  const summary = $('#loop-summary');
  if (!s.decision) summary.textContent = 'No decision yet.';
  else {
    const rate = d.rate === null ? `scheduled basal ${d.scheduled_basal} U/h` : `temp basal ${d.rate} U/h`;
    const verb = s.mode === 'closed' ? 'Running' : 'Would run';
    const label = s.mode === 'closed' ? d.mode : (d.mode === 'CLOSED' ? 'ADVICE' : d.mode);
    summary.textContent = d.mode === 'BLOCKED'
      ? 'BLOCKED · Loop paused: no insulin commands until the pump is running again'
      : `${label} · ${verb} ${rate}` + (s.pump_mode === 'none' ? ' (advisory only: no pump)' : '');
  }
  $('#loop-reasons').replaceChildren(...(d.reasons || []).map((r) => el('li', {}, r)));

  const now = s.now;
  const pred = (d.prediction || []).map((v, i) => [d.time + (i + 1) * 5, v]);
  const values = s.readings.map((r) => r[1]).concat(pred.map((p) => p[1]));
  drawChart($('#home-chart'), {
    x0: now - 360, x1: now + 240, y0: 40, y1: Math.max(300, ...values) + 10,
    band: [70, 180], yTicks: [70, 180, 250], xTickEvery: 60, xFmt: clock, nowX: now,
    dots: [{ points: s.readings }],
    lines: pred.length ? [{ points: [[d.time, d.bg], ...pred], color: cssVar('--muted'), dash: [5, 4], width: 1.6 }] : [],
    markers: [
      { color: '#d97706', points: s.treatments.filter((t) => t.type === 'carbs').map((t) => [t.time, t.amount + 'g']) },
      { color: '#2563eb', top: true, points: s.treatments.filter((t) => t.type.startsWith('bolus')).map((t) => [t.time, t.amount + 'U']) },
    ],
  });

  const names = { carbs: 'Carbs', bolus: 'Pen bolus', bolus_request: 'Pump bolus', temp_target: 'Temp target', temp_target_cancel: 'Temp target ended', note: 'Note' };
  const units = { carbs: ' g', bolus: ' U', bolus_request: ' U', temp_target: ' mg/dL', temp_target_cancel: '', note: '' };
  const label = (t) => t.type === 'pump_event' ? 'Pump: ' + (PUMP_EVENT_NAMES[t.label] || t.label) : (names[t.type] || t.type);
  const amount = (t) => t.type === 'temp_target_cancel' || (t.type === 'pump_event' && !t.amount) ? ''
    : `${t.amount}${t.type === 'pump_event' ? ' U' : (units[t.type] || '')}`;
  $('#treatments').replaceChildren(...(s.treatments.length ? s.treatments.map((t) => el('li', {},
    el('span', {}, `${clock(t.time)}  ${label(t)}`),
    el('span', {}, amount(t),
      t.type === 'pump_event' ? '' :   // pump events are a record of what the pump did: not deletable
        el('button', { class: 'del', title: 'Delete', onclick: () => deleteTreatment(t.id) }, '✕')))) : [el('li', { class: 'muted' }, 'Nothing logged.')]));
}

async function deleteTreatment(id) {
  if (!confirm('Delete this entry?')) return;
  try { await api('DELETE', '/api/treatments/' + id); refresh(); } catch (e) { toast(e.message); }
}

async function quickAction(kind) {
  try {
    if (kind === 'carbs') {
      const v = await promptDialog('Log carbs', 'Carbs eaten without a calculator bolus.', [
        { id: 'amount', label: 'Grams', value: 30 }, { id: 'ago', label: 'Minutes ago', value: 0 }]);
      if (!v) return;
      await api('POST', '/api/treatments', { type: 'carbs', amount: +v.amount, minutes_ago: +v.ago });
    } else if (kind === 'pen') {
      const v = await promptDialog('Log pen dose', 'Rapid-acting insulin injected with a pen.', [
        { id: 'amount', label: 'Units', value: '', step: '0.5' }, { id: 'ago', label: 'Minutes ago', value: 0 }]);
      if (!v) return;
      await api('POST', '/api/treatments', { type: 'bolus', amount: +v.amount, minutes_ago: +v.ago });
    } else if (kind === 'activity') {
      const v = await promptDialog('Activity mode', 'Raises the target before exercise so less insulin is on board.', [
        { id: 'target', label: 'Target mg/dL', value: 150 }, { id: 'duration', label: 'Duration (min)', value: 120 }]);
      if (!v) return;
      await api('POST', '/api/treatments', { type: 'temp_target', amount: +v.target, duration_min: +v.duration, label: 'activity' });
    } else if (kind === 'cancel-target') {
      await api('POST', '/api/treatments', { type: 'temp_target_cancel', amount: 0 });
    }
    refresh();
  } catch (e) { toast(e.message); }
}

// ---------- bolus ----------

async function calcBolus() {
  const bg = $('#b-bg').value;
  try {
    const r = await api('POST', '/api/bolus/calc', { carbs: +$('#b-carbs').value || 0, bg: bg === '' ? null : +bg });
    $('#b-result').classList.remove('hidden');
    $('#b-units').textContent = r.units;
    $('#b-final').value = r.units;
    $('#b-breakdown').textContent = `carbs ${r.carb_units} U + correction ${r.correction_units} U · IOB ${r.iob} U`;
    $('#b-warnings').replaceChildren(...r.warnings.map((w) => el('li', {}, w)));
  } catch (e) { toast(e.message); }
}

async function deliverBolus() {
  const units = +$('#b-final').value;
  const carbs = +$('#b-carbs').value || 0;
  if (!(units > 0) && carbs > 0) {
    await api('POST', '/api/treatments', { type: 'carbs', amount: carbs });
    toast('Carbs logged (no insulin).'); return refresh();
  }
  if (!(units > 0)) return;
  const pump = lastStatus && lastStatus.pump_mode !== 'none';
  const msg = pump ? `Deliver ${units} U from the pump now?` : `Log a pen dose of ${units} U (and ${carbs} g carbs)?`;
  if (!confirm(msg)) return;
  try {
    await api('POST', '/api/bolus', { units, carbs, confirm: true });
    $('#b-result').classList.add('hidden');
    $('#b-carbs').value = 0;
    switchTab('home'); refresh();
  } catch (e) { toast(e.message); }
}

// ---------- simulator ----------

function fillSimSelectors() {
  const sc = $('#s-scenario'), pt = $('#s-patient');
  sc.replaceChildren(...Object.keys(meta.scenarios).map((k) => el('option', { value: k }, k.replaceAll('_', ' '))));
  pt.replaceChildren(...Object.entries(meta.patients).map(([k, v]) => el('option', { value: k }, v)));
  const desc = () => { $('#s-desc').textContent = meta.scenarios[sc.value]; };
  sc.onchange = desc; desc();
}

async function runSim() {
  const btn = $('#s-run');
  btn.disabled = true; btn.textContent = 'Running…';
  try {
    const r = await api('POST', '/api/simulate', {
      scenario: $('#s-scenario').value, patient: $('#s-patient').value,
      days: +$('#s-days').value, seed: +$('#s-seed').value, mode: 'compare' });
    renderSim(r);
  } catch (e) { toast(e.message); }
  finally { btn.disabled = false; btn.textContent = 'Run open vs closed loop'; }
}

function renderSim(r) {
  $('#s-result').classList.remove('hidden');
  const rows = [
    ['Time in range 70–180 %', 'tir_70_180', 1], ['Time 70–140 %', 'titr_70_140', 1],
    ['Below 70 %', 'tbr_below_70', -1], ['Below 54 %', 'tbr_below_54', -1],
    ['Above 180 %', 'tar_above_180', -1], ['Above 250 %', 'tar_above_250', -1],
    ['Mean mg/dL', 'mean', 0], ['CV %', 'cv', -1], ['GMI (≈HbA1c) %', 'gmi', 0], ['Hypo events', 'hypo_events', -1]];
  const tbl = $('#s-table');
  const head = el('tr', {}, el('th', {}, ''), el('th', {}, 'Open'), el('th', {}, 'Closed'));
  const body = rows.map(([label, k, dir]) => {
    const o = r.open.metrics[k], c = r.closed.metrics[k];
    const cls = dir === 0 || o === c ? '' : ((c - o) * dir > 0 ? 'better' : 'worse');
    return el('tr', {}, el('td', {}, label), el('td', {}, o), el('td', { class: cls }, c));
  });
  body.push(el('tr', {}, el('td', {}, 'Insulin U/day'), el('td', {}, r.open.insulin.per_day_u), el('td', {}, r.closed.insulin.per_day_u)));
  const rc = r.closed.rescue_count, ro = r.open.rescue_count;
  body.push(el('tr', {}, el('td', {}, 'Hypo treatments (15 g)'), el('td', {}, ro), el('td', { class: rc < ro ? 'better' : rc > ro ? 'worse' : '' }, rc)));
  tbl.replaceChildren(head, ...body);

  const days = r.closed.days, x1 = days * 1440;
  const ys = r.open.series.map((s) => s.bg).concat(r.closed.series.map((s) => s.bg));
  drawChart($('#s-chart'), {
    x0: 0, x1, y0: 40, y1: Math.max(300, ...ys) + 10, band: [70, 180], yTicks: [70, 180, 250],
    xTickEvery: days > 3 ? 720 : 360, xFmt: simClock,
    lines: [{ points: r.open.series.map((s) => [s.t, s.bg]), color: cssVar('--open'), width: 1.4 },
            { points: r.closed.series.map((s) => [s.t, s.bg]), color: cssVar('--closed'), width: 2 }],
    markers: [{ color: '#d97706', points: r.closed.meals.map((m) => [m.time, '']) },
              { color: '#dc2626', top: true, points: r.closed.rescue_carbs.map((m) => [m.t, '']) }],
  });
  const maxRate = Math.max(1, ...r.closed.series.map((s) => s.rate));
  drawChart($('#s-ins'), {
    x0: 0, x1, y0: 0, y1: maxRate * 1.1, yTicks: [0, Math.round(maxRate * 10) / 10],
    xTickEvery: days > 3 ? 720 : 360, xFmt: simClock,
    bars: [{ color: cssVar('--closed'), points: r.closed.series.map((s) => [s.t, s.rate, 5]) }],
    lines: [{ points: r.closed.series.map((s) => [s.t, s.sched]), color: cssVar('--muted'), width: 1, dash: [4, 3] }],
    markers: [{ color: '#2563eb', top: true, points: r.closed.boluses.map((b) => [b.t, days === 1 ? b.units + '' : '']) }],
  });

  const ev = [];
  for (const m of r.closed.meals) ev.push([m.time, `${m.label}: ate ${m.carbs} g, entered ${m.announced} g${m.bolus ? '' : ', NO bolus'}`]);
  for (const e of r.closed.events) ev.push([e.t, `closed loop: ${e.type}: ${e.detail}`]);
  for (const e of r.open.events) ev.push([e.t, `open loop: ${e.type}: ${e.detail}`]);
  ev.sort((a, b) => a[0] - b[0]);
  $('#s-events').replaceChildren(...ev.map(([t, txt]) => el('li', {}, el('span', {}, simClock(t)), el('span', {}, txt))));
}

// ---------- pump: card, alarms, suspend/resume ----------

const PUMP_EVENT_NAMES = { suspend: 'suspended', resume: 'resumed', clear_alarms: 'alarm cleared', rewind: 'rewound',
  set_reservoir: 'new reservoir', prime: 'primed', site_change: 'site changed' };
const STATE_NAMES = { running: 'running', suspended: 'suspended', no_reservoir: 'no reservoir', priming: 'priming' };
const ALARM_TEXT = {
  OCCLUSION: ['Blocked line: insulin is not reaching you',
    'Check your BG now and be ready to use a pen. Replace the infusion set (Change site), then clear the alarm.', true],
  RESERVOIR_EMPTY: ['Reservoir empty: delivery stopped', 'Change the reservoir and infusion set now. Use a pen if BG is rising.', true],
  HOURLY_CAP: ['Hourly safety cap reached: delivery stopped',
    'The pump delivered as much as its hourly limit allows. Check for a fault or a wrong command before clearing.', false],
  MOTOR_FAULT: ['Motor fault: delivery stopped', 'The pump could not move the plunger. Use pens and inspect the pump.', true],
  RESERVOIR_LOW: ['Reservoir low', 'Plan a site change soon.', true],
  BATTERY_LOW: ['Pump battery low', 'Charge the pump at the next reservoir change.', false],
  RESTARTED: ['Pump restarted', 'It resumed your scheduled basal. Any bolus in progress was stopped: check your IOB before dosing again.', false],
};

function renderPump(s) {
  const card = $('#pump-card'), alarmsBox = $('#pump-alarms');
  if (s.pump_mode === 'none' || !s.pump) { card.classList.add('hidden'); alarmsBox.replaceChildren(); return; }
  card.classList.remove('hidden');
  const p = s.pump;
  if (p.error) {
    alarmsBox.replaceChildren(el('div', { class: 'alarm' }, el('strong', {}, 'Cannot reach the pump'),
      'Basal continues on its own schedule. ' + p.error));
    return;
  }
  const pill = $('#pump-state');
  const stopped = p.alarms.length > 0;
  pill.textContent = stopped ? 'stopped' : (STATE_NAMES[p.state] || p.state);
  pill.className = 'pill ' + (stopped ? 'stopped' : p.state);
  $('#p-res').textContent = p.reservoir_u.toFixed(1) + ' U';
  $('#p-batt').textContent = p.battery_pct + '%';
  $('#p-rate').textContent = p.state !== 'running' || p.alarms.length ? '0 U/h'
    : (p.temp_rate !== null ? p.temp_rate.toFixed(2) + ' U/h' : 'basal');
  $('#p-hour').textContent = p.delivered_last_hour.toFixed(2) + ' U';
  const extra = [];
  if (p.temp_rate !== null) extra.push(`Temp basal ${p.temp_rate} U/h, ${p.temp_remaining_min} min left`);
  if (p.bolus_remaining > 0) extra.push(`Bolus in progress: ${p.bolus_remaining} U to go`);
  $('#p-extra').textContent = extra.join(' · ');
  const sus = $('#p-suspend');
  sus.textContent = p.state === 'running' ? 'Suspend' : 'Resume';
  sus.disabled = p.state === 'no_reservoir' || p.state === 'priming';

  const items = [...p.alarms, ...p.warnings].map((code) => {
    const [title, text, siteChange] = ALARM_TEXT[code] || [code, '', false];
    const stop = p.alarms.includes(code);
    const actions = [];
    if (siteChange) actions.push(el('button', { onclick: () => openSiteChange() }, 'Change site'));
    // low reservoir / battery clear themselves once fixed; only stop alarms and RESTARTED need a tap
    if (stop || code === 'RESTARTED') {
      actions.push(el('button', { onclick: () => pumpAction('clear-alarms') }, stop ? 'Clear alarm' : 'Acknowledge'));
    }
    return el('div', { class: 'alarm' + (stop ? '' : ' warn') }, el('strong', {}, title), text,
      el('div', { class: 'alarm-actions' }, ...actions));
  });
  alarmsBox.replaceChildren(...items);
}

async function pumpAction(action, body) {
  try {
    const p = await api('POST', '/api/pump/' + action, body || {});
    await refresh();
    return p;
  } catch (e) {
    const blocked = action === 'clear-alarms' && /current state/.test(e.message);
    toast(blocked ? 'The line is still blocked. Replace the infusion set (Change site), then clear the alarm.' : e.message);
    return null;
  }
}

async function toggleSuspend() {
  const p = lastStatus && lastStatus.pump;
  if (!p) return;
  if (p.state === 'running') {
    if (!confirm('Suspend stops ALL insulin, including basal. Resume within 1-2 hours or use pens. Suspend now?')) return;
    await pumpAction('suspend');
  } else {
    await pumpAction('resume');
  }
}

// ---------- site change walkthrough ----------

const site = { step: 0, timer: null, pump: null };
const SITE_STEPS = [
  { title: 'Change site and reservoir', body: () => [
      el('p', {}, 'Every 2–3 days. Have ready:'),
      el('ol', {}, el('li', {}, 'a new reservoir and infusion set'), el('li', {}, 'your insulin vial at room temperature'),
        el('li', {}, 'an alcohol swab and a tissue')),
      el('p', { class: 'muted' }, 'Insulin delivery stops until the last step: keep it under 30 minutes.')],
    actions: () => [btn('Start: suspend insulin', 'primary', async () => {
      if (site.pump.state === 'running' && !(await pumpAction('suspend'))) return;
      next();
    })] },
  { title: 'Disconnect and rewind', body: () => [
      el('p', {}, 'Disconnect the infusion set from your body and remove the old set.'),
      el('p', {}, 'Open the reservoir door and take out the old reservoir, then rewind the plunger.')],
    actions: () => [btn('Rewind', 'primary', async () => { if (await pumpAction('rewind')) next(); })] },
  { title: 'Fill and insert the new reservoir', body: () => [
      el('ol', {}, el('li', {}, 'Wipe the vial top. Fill the reservoir slowly through the transfer guard.'),
        el('li', {}, 'Hold it needle-up and tap out every bubble: bubbles mean missed insulin.'),
        el('li', {}, 'Attach the new set’s tubing, put the reservoir in and close the door.')),
      el('label', {}, 'Units in the reservoir', el('input', { id: 'site-units', type: 'number', inputmode: 'decimal', min: '10', max: '300', value: '180' }))],
    actions: () => [btn('Set reservoir', 'primary', async () => {
      const units = +$('#site-units').value;
      if (await pumpAction('set-reservoir', { units })) next();
    })] },
  { title: 'Prime the tubing', body: () => [
      el('p', {}, 'Point the needle end up over a tissue, NOT into your body. Prime until drops appear at the needle tip.'),
      el('div', { class: 'big-number', id: 'site-primed' }, (site.pump.primed_u || 0).toFixed(2) + ' U primed'),
      el('p', { class: 'muted small' }, 'Each step takes up to a minute. Long tubing can need 10 U or more.')],
    actions: () => [el('div', { class: 'row' },
        btn('Prime 1 U', '', () => pumpAction('prime', { units: 1 })),
        btn('Prime 3 U', '', () => pumpAction('prime', { units: 3 }))),
      btn('I see drops at the needle', 'primary', () => {
        if (!(site.pump.primed_u > 0)) return toast('Prime at least once first.');
        next();
      })] },
  { title: 'Insert the new set', body: () => [
      el('p', {}, 'Insert the set at a fresh site:'),
      el('ol', {}, el('li', {}, 'at least 2.5 cm from the last site'),
        el('li', {}, '7–8 cm away from your CGM sensor'),
        el('li', {}, 'rotate: abdomen, thighs, back of arms, upper buttocks'),
        el('li', {}, 'avoid lumpy or scarred skin'))],
    actions: () => [btn('Set inserted and connected', 'primary', next)] },
  { title: 'Resume insulin', body: () => [
      el('p', {}, 'Resume delivery. A soft cannula then needs a small fill (see your set’s leaflet, usually 0.3–0.5 U). That fill counts as insulin you received.')],
    actions: () => [btn('Resume insulin', 'primary', async () => {
      if (site.pump.state !== 'running' && !(await pumpAction('resume'))) return;
      next();
    })] },
  { title: 'Fill the cannula', body: () => [
      el('p', {}, 'Choose your set’s cannula fill, or skip for a steel-needle set.'),
      el('p', { class: 'muted small' }, 'Check your BG in 1–2 hours to confirm the new site works.')],
    actions: () => [el('div', { class: 'row' },
        btn('Fill 0.3 U', '', () => cannulaFill(0.3)), btn('Fill 0.5 U', '', () => cannulaFill(0.5))),
      btn('Done', 'primary', finishSiteChange)] },
];

function btn(text, cls, onclick) {
  return el('button', { class: cls, onclick: async (ev) => {
    ev.target.disabled = true;
    try { await onclick(); } finally { ev.target.disabled = false; }
  } }, text);
}

async function cannulaFill(units) {
  if (!confirm(`Deliver ${units} U to fill the cannula?`)) return;
  try { await api('POST', '/api/bolus', { units, carbs: 0, confirm: true }); $('#site-msg').textContent = `${units} U cannula fill sent.`; }
  catch (e) { toast(e.message); }
}

async function finishSiteChange() {
  await pumpAction('site-change-done');
  closeSiteChange();
}

function next() { site.step = Math.min(site.step + 1, SITE_STEPS.length - 1); renderSite(); }

function renderSite() {
  const st = SITE_STEPS[site.step];
  $('#site-steps').replaceChildren(...SITE_STEPS.map((_, i) => el('span', { class: i <= site.step ? 'done' : '' })));
  $('#site-title').textContent = st.title;
  $('#site-body').replaceChildren(...st.body());
  $('#site-actions').replaceChildren(...st.actions());
  $('#site-msg').textContent = '';
}

async function pollSite() {
  try {
    site.pump = await api('GET', '/api/pump');
    const primed = $('#site-primed');
    if (primed) primed.textContent = (site.pump.primed_u || 0).toFixed(2) + ' U primed';
  } catch (e) { /* shown on the next action */ }
}

async function openSiteChange() {
  try { site.pump = await api('GET', '/api/pump'); } catch (e) { return toast(e.message); }
  // pick up where a previous, unfinished change left off
  site.step = { running: 0, suspended: 1, no_reservoir: 2, priming: 3 }[site.pump.state] ?? 0;
  if (site.pump.state === 'priming' && site.pump.primed_u > 0) site.step = 3;
  switchTab('site');
  renderSite();
  clearInterval(site.timer);
  site.timer = setInterval(pollSite, 5000);
}

function closeSiteChange() {
  clearInterval(site.timer);
  site.timer = null;
  switchTab('home');
  refresh();
}

function leaveSiteChange() {
  const st = site.pump && site.pump.state;
  if (st && st !== 'running' &&
      !confirm('Insulin delivery is still stopped. Leave anyway? You can come back with "Change site".')) return;
  closeSiteChange();
}

// ---------- settings ----------

const EDITABLE = ['profile', 'safety', 'cgm'];
const GATE_TEXT = {
  G0_simulation: 'Simulated all scenarios with your profile shape: below 70 < 4%, below 54 < 1%',
  G1_bench_accuracy: 'Saline on a 0.001 g scale: every hour within ±5%, boluses within ±5%',
  G2_fault_injection: 'Blocked line, power pull, BLE loss, duplicate frames, runaway motor all alarm or refuse',
  G3_saline_wear: '14 days wearing the pump with saline: adhesive, water, night-time alarms',
  G4_advisory_2_weeks: '2 weeks of dry run against your real CGM, every decision reviewed',
  G5_clinician_informed: 'Your diabetes team has seen the plan, limits and data',
};
let settingsCache = null;

async function loadSettings() {
  try {
    const s = await api('GET', '/api/settings');
    settingsCache = s;
    const editable = {};
    for (const k of EDITABLE) editable[k] = s[k];
    $('#cfg-json').value = JSON.stringify(editable, null, 2);
    $('#cfg-msg').textContent = s.configured === false ? 'Profile is still the placeholder.' : '';

    const pump = s.pump || { type: 'none' };
    $('#pm-type').value = pump.type;
    $('#pm-address').value = pump.address || '';
    $('#pm-key').value = '';
    $('#pm-key-state').textContent = pump.key_set ? '(a key is stored)' : '(no key stored)';
    $('#pm-address-row').classList.toggle('hidden', pump.type !== 'ble');
    $('#pm-generated').classList.add('hidden');
    $('#pm-msg').textContent = '';

    const done = new Set(s.gates_completed || []);
    $('#gates').replaceChildren(...s.required_gates.map((g) => el('li', {},
      el('label', {}, Object.assign(el('input', { type: 'checkbox', 'data-gate': g }), { checked: done.has(g) }),
        el('span', {}, g.replaceAll('_', ' '), el('span', { class: 'gate-desc' }, GATE_TEXT[g] || ''))))));
    $('#cl-enabled').checked = !!s.closed_loop_enabled;
    $('#gates-msg').textContent = s.closed_loop_active ? 'Closed loop is ACTIVE.'
      : (s.pump.type === 'ble' && s.gates_missing.length ? `Advice mode: ${s.gates_missing.length} gate(s) not ticked.` : '');
  } catch (e) { $('#cfg-msg').textContent = e.message; }
}

async function saveSettings() {
  const msg = $('#cfg-msg');
  let body;
  try { body = JSON.parse($('#cfg-json').value); } catch (e) { msg.textContent = 'Invalid JSON: ' + e.message; return; }
  for (const k of Object.keys(body)) if (!EDITABLE.includes(k)) delete body[k];
  try {
    await api('PUT', '/api/settings', body);
    msg.textContent = 'Saved.'; msg.className = 'small';
    loadSettings(); refresh();
  } catch (e) { msg.textContent = e.message; msg.className = 'small error'; }
}

function generateKey() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  $('#pm-key').value = hex;
  $('#pm-key').type = 'text';
  const out = $('#pm-generated');
  out.classList.remove('hidden');
  out.replaceChildren('Copy this key into the pump over USB with ', el('code', {}, 'op key ' + hex),
    '. It is shown only now; after saving, the app never displays it again.');
}

async function savePump() {
  const msg = $('#pm-msg');
  const type = $('#pm-type').value;
  if (type === 'ble' && !confirm('Connect to a real pump? Until every safety gate is ticked it stays in advice mode.')) return;
  try {
    await api('PUT', '/api/settings', { pump: { type, address: $('#pm-address').value.trim(), key: $('#pm-key').value.trim() } });
    $('#pm-key').type = 'password';
    await loadSettings();
    msg.textContent = 'Pump settings saved.'; msg.className = 'small';
    refresh();
  } catch (e) { msg.textContent = e.message; msg.className = 'small error'; }
}

async function saveGates() {
  const gates = $$('[data-gate]').filter((c) => c.checked).map((c) => c.dataset.gate);
  const enabled = $('#cl-enabled').checked;
  if (enabled && !confirm('Switch on automatic insulin adjustment? Keep pens, a meter and glucagon with you.')) return;
  try {
    await api('PUT', '/api/settings', { gates_completed: gates, closed_loop_enabled: enabled });
    await loadSettings();
    refresh();
  } catch (e) { $('#gates-msg').textContent = e.message; }
}

// ---------- shell ----------

function switchTab(name) {
  $$('.tab').forEach((t) => t.classList.toggle('hidden', t.id !== 'tab-' + name));
  $$('.tabbar button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  if (name !== 'site' && site.timer) { clearInterval(site.timer); site.timer = null; }
  if (name === 'settings') loadSettings();
  if (name === 'home' && lastStatus) renderHome(lastStatus);
  window.scrollTo(0, 0);
}

function showLogin(err) {
  clearInterval(pollTimer);
  $('#app').classList.add('hidden');
  $('#login').classList.remove('hidden');
  $('#login-error').textContent = err || '';
}

async function start() {
  try {
    meta = await api('GET', '/api/meta');
  } catch (e) {
    if (e.message !== 'unauthorized') showLogin('Cannot reach server: ' + e.message);
    return;
  }
  $('#login').classList.add('hidden');
  $('#app').classList.remove('hidden');
  fillSimSelectors();
  refresh();
  clearInterval(pollTimer);
  pollTimer = setInterval(refresh, 60000);
}

document.addEventListener('DOMContentLoaded', () => {
  $('#login-form').addEventListener('submit', (ev) => {
    ev.preventDefault();
    localStorage.setItem(TOKEN_KEY, $('#token-input').value.trim());
    start();
  });
  $$('.tabbar button').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));
  $$('[data-action]').forEach((b) => b.addEventListener('click', () => quickAction(b.dataset.action)));
  $$('[data-carbs]').forEach((b) => b.addEventListener('click', () => { $('#b-carbs').value = b.dataset.carbs; }));
  $('#b-calc').addEventListener('click', calcBolus);
  $('#b-deliver').addEventListener('click', deliverBolus);
  $('#s-run').addEventListener('click', runSim);
  $('#cfg-save').addEventListener('click', saveSettings);
  $('#p-suspend').addEventListener('click', toggleSuspend);
  $('#p-site').addEventListener('click', openSiteChange);
  $('#site-cancel').addEventListener('click', leaveSiteChange);
  $('#pm-type').addEventListener('change', () => $('#pm-address-row').classList.toggle('hidden', $('#pm-type').value !== 'ble'));
  $('#pm-generate').addEventListener('click', generateKey);
  $('#pm-save').addEventListener('click', savePump);
  $('#gates-save').addEventListener('click', saveGates);
  $('#logout').addEventListener('click', () => { localStorage.removeItem(TOKEN_KEY); showLogin(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && meta) refresh(); });
  window.addEventListener('resize', () => { if (lastStatus && !$('#tab-home').classList.contains('hidden')) renderHome(lastStatus); });
  if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/sw.js').catch(() => {});
  // one-tap login link printed by `openpatch serve`: /#token=...; the fragment never reaches the server
  if (location.hash.startsWith('#token=')) {
    localStorage.setItem(TOKEN_KEY, decodeURIComponent(location.hash.slice(7)));
    history.replaceState(null, '', location.pathname);
  }
  if (localStorage.getItem(TOKEN_KEY)) start(); else showLogin();
});
