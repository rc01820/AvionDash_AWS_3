'use strict';

const API = '/api';
const CFG = window.AVIONPULSE_CONFIG || {};
let token = localStorage.getItem('avp_token') || null;
let currentUser = null;
let faultState = {};
let faultDetails = {};
let catalog = null;
let classicState = null;
let refreshTimer = null;
let clockTimer = null;

// ═══════════════════════════════════════════════════════════
//  HELPERS
// ═══════════════════════════════════════════════════════════
const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = n => (n == null ? '—' : Number(n).toLocaleString());
function setText(id, val) { const el = $(id); if (el) el.textContent = (val ?? '—'); }
function utc(iso) { return iso ? iso.slice(0, 16).replace('T', ' ') : '—'; }

class ApiError extends Error {
  constructor(status, detail, code) { super(detail); this.status = status; this.code = code; }
}

async function api(path, opts = {}) {
  const headers = {};
  if (token) headers['X-Avion-Token'] = token;
  if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.json);
    delete opts.json;
  }
  let res;
  for (let attempt = 0; ; attempt++) {
    res = await fetch(`${API}${path}`, { ...opts, headers });
    // Writes (fault toggles, events) retry through a stage throttle instead of failing outright.
    if (res.status !== 429 || !opts.method || opts.method === 'GET' || attempt >= 3) break;
    await new Promise(r => setTimeout(r, 1200 * (attempt + 1)));
  }
  noteLatency(res);
  const txt = await res.text();
  let data = null;
  try { data = txt ? JSON.parse(txt) : null; } catch { /* API Gateway / CloudFront error pages */ }
  if (res.status === 401 && path !== '/auth/token') { doLogout(); throw new ApiError(401, 'Session expired'); }
  if (!res.ok) {
    // API Gateway answers {"message": "..."} for 429 throttles, Lambda timeouts (503) and unhandled errors (500).
    const detail = data?.detail || data?.message || `HTTP ${res.status}`;
    const code = data?.error_code || (res.status === 429 ? 'ApiGatewayThrottled' : data?.message ? 'ApiGateway' : null);
    throw new ApiError(res.status, detail, code);
  }
  return data;
}

function noteLatency(res) {
  const el = $('cold-ind');
  if (!el) return;
  const ms = res.headers.get('x-avion-duration-ms');
  const cold = res.headers.get('x-avion-cold-start') === 'true';
  if (ms) {
    el.textContent = `λ ${Math.round(ms)} ms${cold ? ' · COLD START' : ''}`;
    el.classList.toggle('cold', cold);
  } else if (!res.ok) {
    el.textContent = `HTTP ${res.status} from API Gateway`;
    el.classList.remove('cold');
  }
}

function errText(e) { return `${e.status ? 'HTTP ' + e.status + ' · ' : ''}${e.code ? e.code + ' · ' : ''}${e.message}`; }

// ═══════════════════════════════════════════════════════════
//  DATADOG RUM (optional — configured by scripts/deploy via config.js)
// ═══════════════════════════════════════════════════════════
(function initRum() {
  const rum = CFG.rum || {};
  if (!rum.applicationId || !rum.clientToken) return;
  const s = document.createElement('script');
  s.src = rum.scriptUrl || 'https://www.datadoghq-browser-agent.com/us1/v6/datadog-rum.js';
  s.async = true;
  s.onload = () => {
    window.DD_RUM && window.DD_RUM.init({
      applicationId: rum.applicationId, clientToken: rum.clientToken, site: rum.site || 'datadoghq.com',
      service: 'avionpulse-web', env: CFG.env || 'demo', version: CFG.version || '1.0.0',
      sessionSampleRate: 100, sessionReplaySampleRate: rum.replaySampleRate ?? 20,
      trackUserInteractions: true, trackResources: true, trackLongTasks: true,
      defaultPrivacyLevel: 'mask-user-input',
      allowedTracingUrls: [window.location.origin + '/api'],
    });
  };
  document.head.appendChild(s);
})();

// ═══════════════════════════════════════════════════════════
//  LOGIN
// ═══════════════════════════════════════════════════════════
async function doLogin(ev) {
  ev?.preventDefault();
  const username = $('inp-user').value.trim();
  const password = $('inp-pass').value;
  if (!username || !password) return;
  $('btn-login').disabled = true;
  $('login-error').hidden = true;
  try {
    const data = await api('/auth/token', { method: 'POST', json: { username, password } });
    token = data.access_token;
    localStorage.setItem('avp_token', token);
    currentUser = { username: data.username, role: data.role };
    window.DD_RUM?.setUser?.({ id: data.username, name: data.full_name, role: data.role });
    enterApp();
  } catch (e) {
    $('login-error').textContent = '✗ ' + (e.status === 401 ? e.message : errText(e));
    $('login-error').hidden = false;
  } finally {
    $('btn-login').disabled = false;
  }
}

function doLogout() {
  token = null;
  currentUser = null;
  localStorage.removeItem('avp_token');
  clearInterval(refreshTimer);
  if (document.body.classList.contains('role-viewer')) { location.replace('/'); return; }  // restore the full shell
  $('app-page').classList.remove('active');
  $('login-page').classList.add('active');
  loadLoginStats();
}

async function loadLoginStats() {
  try {
    const d = await api('/dashboard/public-stats');
    setText('ls-flights', d?.flights?.total);
    setText('ls-airborne', d?.flights?.airborne);
    setText('ls-aircraft', d?.aircraft?.total);
    setText('ls-delayed', d?.flights?.delayed);
  } catch (e) { console.warn('public stats', e); }
}

window.addEventListener('load', async () => {
  initRadar();
  if (token) {
    try {
      const me = await api('/auth/me');
      currentUser = { username: me.username, role: me.role };
      enterApp();
      return;
    } catch { token = null; localStorage.removeItem('avp_token'); }
  }
  loadLoginStats();
});

// ═══════════════════════════════════════════════════════════
//  SHELL + ROUTING
// ═══════════════════════════════════════════════════════════
const VIEWS = {
  dashboard: ['Dashboard', loadDashboard],
  flights: ['Flights', loadFlights],
  fleet: ['Fleet', loadFleet],
  airports: ['Airports', loadAirports],
  events: ['Ops Pipeline', loadEvents],
  notams: ['NOTAMs', loadNotams],
  integrations: ['Integrations', loadIntegrations],
  gates: ['Gates — Classic Tier', loadGates],
  aws: ['AWS Resources', loadAws],
  architecture: ['Architecture & Cost', loadArchitecture],
  chaos: ['Chaos Control', loadChaos],
};

function enterApp() {
  $('login-page').classList.remove('active');
  $('app-page').classList.add('active');
  document.body.classList.toggle('role-viewer', isViewer());
  // Viewers: take the platform links out of the page entirely (not just hidden), so nothing about
  // AWS resources, cost or chaos is in the DOM. Sign-out reloads the page, which restores them.
  if (isViewer()) document.querySelectorAll('.nav-platform, .platform-only, #fault-ind').forEach(el => el.remove());
  if (isViewer() && PLATFORM_VIEWS.includes(location.hash.replace('#', ''))) history.replaceState(null, '', '#dashboard');
  setText('sb-user', currentUser.username);
  setText('sb-role', currentUser.role.toUpperCase());
  setText('sb-env', `env:${CFG.env || 'demo'} · v${CFG.version || '1.0.0'}`);
  startClock();
  route();
  clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshView, 30000);
}

// Viewers see the operations pages only; the platform pages (and their API routes) need operator or admin.
const PLATFORM_VIEWS = ['aws', 'architecture', 'chaos'];
const isViewer = () => currentUser?.role === 'viewer';

function currentView() {
  const v = location.hash.replace('#', '');
  if (!VIEWS[v] || (isViewer() && PLATFORM_VIEWS.includes(v))) return 'dashboard';
  return v;
}

function route() {
  if (!currentUser) return;
  const name = currentView();
  if (location.hash && location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === `view-${name}`));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.view === name));
  setText('view-title', VIEWS[name][0]);
  toggleNav(false);
  VIEWS[name][1]();
  if (name !== 'chaos') refreshFaultIndicator();
}
window.addEventListener('hashchange', route);

function refreshView() {
  const name = currentView();
  if (!['chaos', 'events', 'aws'].includes(name)) VIEWS[name][1]();
  refreshFaultIndicator();
}

function toggleNav(open) {
  $('sidebar').classList.toggle('open', open);
  $('nav-scrim').classList.toggle('open', open);
}

function startClock() {
  clearInterval(clockTimer);
  const tick = () => setText('clock', new Date().toUTCString().replace(' GMT', ' UTC'));
  tick();
  clockTimer = setInterval(tick, 1000);
}

// ═══════════════════════════════════════════════════════════
//  DASHBOARD
// ═══════════════════════════════════════════════════════════
async function loadDashboard() {
  const banner = $('dash-error');
  try {
    const d = await api('/dashboard/summary');
    banner.hidden = true;
    setText('k-total', d.flights.total);
    setText('k-airborne', d.flights.airborne);
    setText('k-pax', fmt(d.flights.pax_airborne));
    setText('k-delayed', d.flights.delayed);
    setText('k-canc', d.flights.cancelled);
    setText('k-ontime', d.flights.on_time_pct + '%');
    const sh = d.system_health || {};
    $('h-api').innerHTML = '<span class="ok">HTTP API ✓</span>';
    $('h-cold').innerHTML = sh.cold_start ? '<span class="cold">COLD START</span>' : `<span class="ok">warm · ${sh.container_invocations} inv</span>`;
    setText('h-ddb', `${sh.dynamodb_ms} ms`);
    const age = d.data_age_s;
    $('h-age').innerHTML = age == null ? '<span class="bad">no data</span>'
      : `<span class="${age > 900 ? 'bad' : age > 420 ? 'warn' : 'ok'}">${Math.round(age / 60)} min old</span>`;
    setText('h-util', d.aircraft.utilization_pct + '%');
    $('h-faults').innerHTML = sh.active_faults ? `<span class="bad">${sh.active_faults} active</span>` : '<span class="ok">none</span>';
    setText('map-age', age == null ? '' : `· simulator tick ${Math.round(age / 60)} min ago`);
    renderBusiest(d.busiest_origins || []);
  } catch (e) {
    banner.textContent = '✗ Dashboard summary failed — ' + errText(e);
    banner.hidden = false;
    $('h-api').innerHTML = `<span class="bad">${esc(e.status || 'ERR')}</span>`;
  }
  renderMap();
  loadWeather();
}

function renderBusiest(rows) {
  const el = $('busiest-panel');
  if (!rows.length) { el.innerHTML = '<div class="muted">No departures (is the flights query returning data?)</div>'; return; }
  const max = Math.max(...rows.map(r => r.departures), 1);
  el.innerHTML = rows.map(r => `
    <div class="b-row"><span class="b-iata">${esc(r.iata)}</span>
      <div class="b-track"><div class="b-fill" style="width:${Math.round(r.departures / max * 100)}%"></div></div>
      <span class="b-cnt">${r.departures}</span></div>`).join('');
}

async function loadWeather() {
  const el = $('weather-panel');
  try {
    const d = await api('/weather');
    el.innerHTML = `<div class="wx-grid">${d.observations.map(o => `
      <div class="wx"><div><b>${esc(o.iata)}</b><span class="cat cat-${esc(o.category)}">${esc(o.category)}</span></div>
        <span>${esc(o.wind)} · ${o.visibility_sm}SM · ${o.temp_c}°C</span>
        <span class="muted">${esc(o.metar)}</span></div>`).join('')}</div>`;
  } catch (e) {
    el.innerHTML = `<div class="bad" style="font-family:var(--font-mono);font-size:.78rem">⛈ Weather provider unavailable — ${esc(errText(e))}</div>`;
  }
}

// Robinson projection matched to the shared world.svg (900×440). Both tables are
// read off that file's graticule at 5° latitude steps (0°…90°): ROB_Y is the
// distance above the equator in px; ROB_X is the 150°W meridian's x, which gives
// the parallel's length relative to the equator.
const ROB_Y = [0, 12.73, 25.46, 38.2, 50.93, 63.66, 76.39, 89.12, 101.82, 114.4, 126.83, 139.01, 150.86, 162.29, 173.22, 183.51, 192.91, 200.45, 205.36];
const ROB_X = [112.59, 113.06, 114.14, 115.96, 118.59, 121.7, 126.08, 131.92, 139.04, 147.61, 157.16, 168.26, 180.54, 193.67, 207.53, 222.85, 240.36, 256.93, 275.48];
function proj(lat, lon) {
  const a = Math.min(Math.abs(lat), 89.999) / 5, i = Math.floor(a), t = a - i;
  const lerp = tbl => tbl[i] + (tbl[i + 1] - tbl[i]) * t;
  const widthFactor = (450 - lerp(ROB_X)) / (450 - ROB_X[0]);
  return [450 + lon * (404.9 / 180) * widthFactor, 216.64 - Math.sign(lat) * lerp(ROB_Y)];
}

async function renderMap() {
  const svg = $('flight-map');
  let html = `<rect width="900" height="440" fill="#061525"/>
    <image href="/static/img/world.svg" x="0" y="0" width="900" height="440" preserveAspectRatio="none" style="pointer-events:none"/>`;
  try {
    const [airports, flights] = await Promise.all([api('/airports'), api('/flights?limit=200')]);
    const byIata = Object.fromEntries(airports.map(a => [a.iata_code, a]));
    html += airports.map(a => {
      const [x, y] = proj(a.lat, a.lon);
      return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3" fill="rgba(0,198,255,.18)" stroke="rgba(0,198,255,.8)" stroke-width="1"><title>${esc(a.iata_code)} – ${esc(a.name)}</title></circle>`;
    }).join('');
    const air = flights.filter(f => (f.status === 'en_route' || f.status === 'departed') && f.lat != null);
    html += air.map(f => {
      const o = byIata[f.origin_iata], d = byIata[f.destination_iata];
      const [x, y] = proj(f.lat, f.lon);
      let trail = '';
      if (o && d && Math.abs(o.lon - d.lon) < 180) {
        const [ox, oy] = proj(o.lat, o.lon), [dx, dy] = proj(d.lat, d.lon);
        trail = `<path d="M${ox.toFixed(1)},${oy.toFixed(1)} L${x.toFixed(1)},${y.toFixed(1)}" stroke="rgba(0,230,118,.45)" stroke-width="1" fill="none"/>
                 <path d="M${x.toFixed(1)},${y.toFixed(1)} L${dx.toFixed(1)},${dy.toFixed(1)}" stroke="rgba(255,43,214,.3)" stroke-width="1" stroke-dasharray="3 3" fill="none"/>`;
      }
      return `${trail}<g transform="translate(${x.toFixed(1)},${y.toFixed(1)})">
        <circle r="7" fill="rgba(0,230,118,.12)" stroke="rgba(0,230,118,.6)" stroke-width="1"/>
        <text x="0" y="1" text-anchor="middle" dominant-baseline="central" font-size="8" fill="#00e676">✈</text>
        <text x="9" y="-7" font-size="7" font-family="monospace" fill="rgba(0,230,118,.85)">${esc(f.flight_number)}</text>
        <title>${esc(f.flight_number)} ${esc(f.origin_iata)}→${esc(f.destination_iata)} · FL${Math.round((f.altitude_ft || 0) / 100)} · ${f.progress_pct}%</title></g>`;
    }).join('');
    if (!air.length) html += `<text x="450" y="220" text-anchor="middle" fill="#4a6a85" font-family="monospace" font-size="13">NO AIRBORNE TRAFFIC REPORTED</text>`;
  } catch (e) {
    html += `<text x="450" y="220" text-anchor="middle" fill="#ff3d5a" font-family="monospace" font-size="13">MAP FEED ERROR — ${esc(errText(e))}</text>`;
  }
  svg.innerHTML = html;
}

// ═══════════════════════════════════════════════════════════
//  TABLES
// ═══════════════════════════════════════════════════════════
function tableError(tbody, cols, e) {
  tbody.innerHTML = `<tr><td colspan="${cols}" class="loading err">✗ ${esc(errText(e))}</td></tr>`;
}
const badge = s => `<span class="sb s-${esc(s)}">${esc(String(s).replace(/_/g, ' ').toUpperCase())}</span>`;

async function loadFlights() {
  const tbody = $('flights-body');
  const status = $('filter-status').value;
  tbody.innerHTML = '<tr><td colspan="10" class="loading">Loading…</td></tr>';
  const t0 = performance.now();
  try {
    const rows = await api(`/flights?limit=100${status ? '&status=' + encodeURIComponent(status) : ''}`);
    setText('flights-note', `${rows.length} rows · ${Math.round(performance.now() - t0)} ms round trip`);
    if (!rows.length) { tbody.innerHTML = '<tr><td colspan="10" class="loading">No flights found.</td></tr>'; return; }
    tbody.innerHTML = rows.map(f => `<tr>
      <td class="fn">${esc(f.flight_number)}</td>
      <td>${esc(f.origin_iata)} → ${esc(f.destination_iata)}</td>
      <td>${esc(f.tail_number)} <span class="muted">${esc(f.aircraft_model)}</span></td>
      <td>${badge(f.status)}</td>
      <td>${utc(f.departure_time)}</td>
      <td>${esc(f.gate || '—')}</td>
      <td>${f.altitude_ft ? fmt(f.altitude_ft) + ' ft' : '—'}</td>
      <td><span class="prog"><i style="width:${f.progress_pct || 0}%"></i></span>${Math.round(f.progress_pct || 0)}%</td>
      <td>${fmt(f.pax)}</td>
      <td style="color:${f.delay_minutes > 0 ? 'var(--orange)' : 'var(--text-d)'}" title="${esc(f.notes || '')}">${f.delay_minutes > 0 ? '+' + f.delay_minutes + 'm' : (f.status === 'cancelled' ? esc(f.notes) : '—')}</td>
    </tr>`).join('');
  } catch (e) { setText('flights-note', ''); tableError(tbody, 10, e); }
}

async function loadFleet() {
  const tbody = $('fleet-body');
  const status = $('filter-ac').value;
  tbody.innerHTML = '<tr><td colspan="9" class="loading">Loading…</td></tr>';
  try {
    const rows = await api(`/aircraft${status ? '?status=' + encodeURIComponent(status) : ''}`);
    tbody.innerHTML = rows.map(a => `<tr>
      <td class="fn">${esc(a.tail_number)}</td><td>${esc(a.model)}</td><td>${esc(a.manufacturer)}</td>
      <td>${a.capacity}</td><td>${fmt(a.range_nm)}</td><td>${esc(a.engine_type)}</td><td>${a.year_manufactured}</td>
      <td>${badge(a.status)}</td><td>${fmt(a.flight_hours)}</td></tr>`).join('')
      || '<tr><td colspan="9" class="loading">No aircraft.</td></tr>';
  } catch (e) { tableError(tbody, 9, e); }
}

async function loadAirports() {
  const tbody = $('airports-body');
  tbody.innerHTML = '<tr><td colspan="9" class="loading">Loading…</td></tr>';
  try {
    const rows = await api('/airports');
    tbody.innerHTML = rows.map(a => `<tr>
      <td class="fn">${esc(a.iata_code)}</td><td class="muted">${esc(a.icao_code)}</td><td>${esc(a.name)}</td>
      <td>${esc(a.city)}</td><td>${esc(a.country)}</td><td class="muted">${Number(a.lat).toFixed(3)}</td>
      <td class="muted">${Number(a.lon).toFixed(3)}</td><td>${fmt(a.elevation_ft)} ft</td><td>${a.runways}</td></tr>`).join('');
  } catch (e) { tableError(tbody, 9, e); }
}

// ═══════════════════════════════════════════════════════════
//  OPS PIPELINE (SQS)
// ═══════════════════════════════════════════════════════════
async function loadEvents() {
  const tbody = $('events-body');
  const sel = $('ev-tail');
  if (!sel.options.length) {
    api('/aircraft').then(rows => {
      sel.innerHTML = rows.map(a => `<option value="${esc(a.tail_number)}">${esc(a.tail_number)} · ${esc(a.model)}</option>`).join('');
    }).catch(() => {});
  }
  api('/events/queue').then(q => {
    setText('pq-depth', q.error ? q.error : `${q.queue_visible ?? '—'} visible · ${q.queue_in_flight ?? 0} in flight`);
    setText('pq-dlq', q.dlq_visible ?? '—');
    document.querySelector('.pipe-dlq').classList.toggle('has', (q.dlq_visible || 0) > 0);
  }).catch(() => {});
  try {
    const rows = await api('/events');
    tbody.innerHTML = rows.map(e => `<tr>
      <td>${esc(utc(e.processed_at))}</td><td>${badge(e.severity)}</td><td>${esc(e.category)}</td>
      <td class="fn">${esc(e.tail_number || '—')}</td><td class="wrap">${esc(e.message)}</td>
      <td>${e.queue_lag_s != null ? e.queue_lag_s + ' s' : '—'}</td><td class="muted">${esc(e.submitted_by || e.source)}</td></tr>`).join('')
      || '<tr><td colspan="7" class="loading">No events yet — the simulator adds a few every 5 minutes.</td></tr>';
  } catch (e) { tableError(tbody, 7, e); }
}

async function submitEvent(ev) {
  ev.preventDefault();
  const out = $('ev-result');
  out.className = 'form-note';
  try {
    await api('/events', { method: 'POST', json: {
      tail_number: $('ev-tail').value, category: $('ev-cat').value,
      severity: $('ev-sev').value, message: $('ev-msg').value } });
    out.textContent = '✓ 202 Accepted — message on the SQS queue. Refreshing in 6 s…';
    out.classList.add('ok');
    $('ev-msg').value = '';
    setTimeout(loadEvents, 6000);
  } catch (e) {
    out.textContent = '✗ ' + errText(e);
    out.classList.add('bad');
  }
}

// ═══════════════════════════════════════════════════════════
//  NOTAMS (S3)
// ═══════════════════════════════════════════════════════════
async function loadNotams() {
  const grid = $('notam-grid');
  grid.innerHTML = '<div class="muted">Loading…</div>';
  try {
    const d = await api('/notams');
    setText('notam-src', `source: ${d.source}`);
    grid.innerHTML = d.notams.map(n => `<div class="notam n-${esc(n.severity)}">
      <div class="notam-top"><span class="notam-id">${esc(n.id)} · ${esc(n.location)}</span>${badge(n.severity)}</div>
      <div class="notam-text">${esc(n.text)}</div>
      <div class="notam-meta">${esc(n.category)} · effective ${esc(utc(n.effective))} → ${esc(n.expires ? utc(n.expires) : 'PERM')}</div></div>`).join('');
  } catch (e) {
    setText('notam-src', '');
    grid.innerHTML = `<div class="err-card">✗ NOTAM feed unavailable<br>${esc(errText(e))}<br><span class="muted">The API reads this board from S3 on every request.</span></div>`;
  }
}

// ═══════════════════════════════════════════════════════════
//  ARCHITECTURE
// ═══════════════════════════════════════════════════════════
async function loadArchitecture() {
  const el = $('sys-panel');
  try {
    const s = await api('/system');
    const q = s.queues || {};
    const rows = [
      ['Function', s.function_name], ['Region', s.region], ['Runtime', `${s.runtime} · arm64 · ${s.memory_mb} MB`],
      ['Request id', s.request_id], ['This request', s.cold_start ? 'COLD START' : 'warm'],
      ['Container invocations', s.container_invocations], ['Container age', `${s.container_age_s} s`],
      ['Leaked memory held', `${s.leak_mb} MB`], ['Slow init (chaos)', s.slow_init_ms ? s.slow_init_ms + ' ms' : 'off'],
      ['SQS visible / DLQ', q.error ? q.error : `${q.queue_visible} / ${q.dlq_visible}`],
      ['DynamoDB table', s.table], ['Datadog layer', s.datadog_enabled ? 'attached' : (s.local_mode ? 'local mode' : 'not attached')],
      ['Active faults', s.active_faults],
    ];
    el.innerHTML = `<dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`;
  } catch (e) { el.innerHTML = `<div class="bad">✗ ${esc(errText(e))}</div>`; }
}

// ═══════════════════════════════════════════════════════════
//  CHAOS
// ═══════════════════════════════════════════════════════════
async function refreshFaultIndicator() {
  if (isViewer()) return;  // chaos and Classic state are platform information
  try {
    const st = await api('/chaos/status');
    faultState = st.faults || {};
    faultDetails = st.details || {};
    updateFaultInd();
  } catch { /* indicator is best-effort */ }
  try { classicState = await api('/classic/status'); updateClassicTag(); } catch { /* best effort */ }
}

function updateClassicTag() {
  const el = $('nav-classic-tag');
  if (!el || !classicState) return;
  el.className = 'nav-tag ' + (classicState.phase === 'online' ? 'on' : classicState.phase === 'offline' ? '' : 'busy');
  el.textContent = classicState.phase === 'online' ? 'ONLINE' : classicState.phase === 'offline' ? 'CLASSIC' : classicState.phase.toUpperCase();
}

function updateFaultInd() {
  const el = $('fault-ind');
  const n = Object.values(faultState).filter(Boolean).length;
  el.textContent = n ? `⚡ ${n} FAULT${n > 1 ? 'S' : ''} ACTIVE` : 'ALL SYSTEMS NOMINAL';
  el.className = 'fault-ind ' + (n ? 'fault-on' : 'fault-off');
}

async function loadChaos() {
  $('chaos-readonly').hidden = currentUser?.role === 'admin';
  try {
    const [st, cat, cl] = await Promise.all([api('/chaos/status'), catalog ? Promise.resolve(catalog) : api('/chaos/catalog'),
                                             api('/classic/status').catch(() => null)]);
    catalog = cat;
    if (cl) { classicState = cl; updateClassicTag(); }
    renderClassicCard();
    faultState = st.faults || {};
    faultDetails = st.details || {};
    const ttlSel = $('chaos-ttl');
    if (!ttlSel.dataset.init) { ttlSel.value = String(st.ttl_default_min || 30); ttlSel.dataset.init = '1'; }
    buildChaosCards();
    updateFaultInd();
  } catch (e) { chaosAlert('✗ ' + errText(e), 'error'); }
}

function remaining(name) {
  const until = faultDetails[name]?.until;
  if (!until) return '';
  const s = Math.max(0, Math.round(until - Date.now() / 1000));
  return ` · auto-revert in ${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
}

function buildChaosCards() {
  const isAdmin = currentUser?.role === 'admin';
  const byTier = {};
  for (const [key, f] of Object.entries(catalog.faults)) (byTier[f.tier] ||= []).push([key, f]);
  $('chaos-tiers').innerHTML = Object.entries(catalog.tiers).map(([tier, title]) => `
    <div class="tier-section t-${tier}">
      <div class="tier-hdr"><span class="tier-dot"></span>${esc(title)}</div>
      ${tier === 'fis' || tier === 'classic' ? classicTierNote() : ''}
      <div class="fault-grid">${(byTier[tier] || []).map(([key, f]) => {
        const on = !!faultState[key];
        const blocked = !on && classicBlocker(f);
        const cls = (on ? (f.severity === 'critical' ? 'f-critical' : 'f-active') : '') + (blocked ? ' f-blocked' : '');
        return `<div class="fault-card ${cls}" id="card-${key}">
          <div class="f-top">
            <div>
              <div class="f-name"><span>${f.icon}</span>${esc(f.label)}</div>
              <div class="f-tags"><span class="sev sev-${f.severity}">${f.severity.toUpperCase()}</span>
                <span class="chip chip-aws">${esc(f.aws)}</span>${f.infra ? '<span class="chip chip-infra">MODIFIES AWS</span>' : ''}${f.classic ? '<span class="chip chip-classic">CLASSIC</span>' : ''}${f.full_only ? '<span class="chip chip-classic">FULL SIZE</span>' : ''}</div>
            </div>
            <label class="toggle" title="${blocked ? esc(blocked) : isAdmin ? 'Toggle fault' : 'Admin only'}">
              <input type="checkbox" ${on ? 'checked' : ''} ${isAdmin && !blocked ? '' : 'disabled'} aria-label="${esc(f.label)}"
                     onchange="toggleFault('${key}', this.checked, this)"/>
              <span class="tslider"></span>
            </label>
          </div>
          <div class="f-desc">${esc(f.description)}</div>
          <div class="f-signal">📡 ${esc(f.datadog_signal)}</div>
          <div class="f-status ${on ? 'on' : ''}" id="flbl-${key}">${on ? '⚡ ACTIVE' + remaining(key) : blocked ? esc(blocked) : 'INACTIVE'}</div>
        </div>`;
      }).join('')}</div>
    </div>`).join('');
}

async function toggleFault(name, enabled, cb) {
  cb.disabled = true;
  try {
    const ttl = Number($('chaos-ttl').value);
    const r = await api(`/chaos/${name}/toggle`, { method: 'POST', json: { enabled, ttl_minutes: ttl } });
    const note = r.notes?.length ? ` — ⚠ ${r.notes.join('; ')}` : '';
    chaosAlert(`${enabled ? '⚡ ACTIVATED' : '✓ DEACTIVATED'}: ${r.members.join(', ').toUpperCase()}${enabled ? ` (auto-revert ${r.ttl_min} min)` : ''}${note}`,
               r.notes?.length ? 'error' : enabled ? 'warn' : 'ok');
    await loadChaos();
  } catch (e) {
    cb.checked = !enabled;
    cb.disabled = false;
    chaosAlert('✗ ' + errText(e), 'error');
  }
}

async function resetAllFaults() {
  if (!confirm('Reset ALL active faults and restore every AWS resource?')) return;
  try {
    const r = await api('/chaos/reset-all', { method: 'POST' });
    chaosAlert('✓ All faults cleared' + (r.notes?.length ? ' — ⚠ ' + r.notes.join('; ') : ''), 'ok');
    await loadChaos();
  } catch (e) { chaosAlert('✗ ' + errText(e), 'error'); }
}

let chaosAlertTimer;
function chaosAlert(msg, type) {
  const el = $('chaos-alert');
  const s = { error: ['var(--red)', 'rgba(255,61,90,.3)', 'rgba(255,61,90,.08)'],
              ok: ['var(--green)', 'rgba(0,230,118,.3)', 'rgba(0,230,118,.08)'],
              warn: ['var(--orange)', 'rgba(255,112,67,.3)', 'rgba(255,112,67,.08)'] }[type] || [];
  Object.assign(el.style, { color: s[0], borderColor: s[1], background: s[2] });
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(chaosAlertTimer);
  chaosAlertTimer = setTimeout(() => { el.hidden = true; }, type === 'error' ? 9000 : 5000);
}

let classicPollAt = 0;
setInterval(() => {
  if (currentView() !== 'chaos' || !catalog) return;
  for (const key of Object.keys(faultState)) {
    if (!faultState[key]) continue;
    const el = $(`flbl-${key}`);
    if (el) el.textContent = '⚡ ACTIVE' + remaining(key);
  }
  updateClassicClock();
  const busy = classicState && ['launching', 'deleting', 'failed'].includes(classicState.phase);
  if (Date.now() - classicPollAt > (busy ? 5000 : 30000)) { classicPollAt = Date.now(); pollClassic(); }
}, 1000);

// ═══════════════════════════════════════════════════════════
//  CLASSIC TIER (on-demand EC2 / ALB / RDS stack)
// ═══════════════════════════════════════════════════════════
const CLASSIC_SIZES = {
  minimal: 'Minimal — ALB · 1× t4g.micro · single-AZ RDS',
  full: 'Full — ALB · 2× t4g.micro across 2 AZs · Multi-AZ RDS',
};

function classicBlocker(f) {
  if (!f.classic) return '';
  if (!classicState || classicState.phase !== 'online') return 'Launch the Classic tier first';
  if (f.full_only && classicState.size !== 'full') return 'Needs the Full (Multi-AZ) size';
  return '';
}

function classicTierNote() {
  const on = classicState?.phase === 'online';
  return `<div class="tier-note ${on ? 'ok' : ''}">${on
    ? `Classic tier online (${esc(classicState.size)}). These faults act on the live EC2 / ALB / RDS resources.`
    : 'These faults need the Classic tier. Launch it above — it takes about 12–15 minutes, mostly RDS.'}</div>`;
}

function hhmmss(s) {
  s = Math.max(0, Math.round(s));
  return `${Math.floor(s / 3600)}h ${String(Math.floor(s / 60) % 60).padStart(2, '0')}m ${String(s % 60).padStart(2, '0')}s`;
}

function renderClassicCard() {
  const el = $('classic-card');
  const c = classicState;
  if (!el) return;
  if (!c) { el.innerHTML = ''; return; }
  const isAdmin = currentUser?.role === 'admin';
  const rate = c.hourly_usd || { minimal: 0.074, full: 0.107 };
  const dot = { offline: 'off', launching: 'busy', deleting: 'busy', online: 'on', failed: 'bad' }[c.phase] || 'off';
  let body = '';
  if (c.phase === 'offline') {
    const last = c.last_session ? `<div class="muted cc-last">Last session: ${esc(c.last_session.size)} · ${c.last_session.hours} h · ≈ $${c.last_session.cost_usd} (${esc(c.last_session.reason)})</div>` : '';
    body = !c.configured ? '<div class="banner banner-warn">This deployment has no Classic tier support (ClassicCfnRole missing).</div>' : `
      <div class="cc-launch">
        <div class="cc-sizes">${Object.entries(CLASSIC_SIZES).map(([k, v], i) => `
          <label class="cc-size"><input type="radio" name="cc-size" value="${k}" ${i === 0 ? 'checked' : ''} onchange="classicEstimate()"/>
            <span>${esc(v)}<br><b>≈ $${rate[k].toFixed(3)} / hour</b></span></label>`).join('')}
        </div>
        <label class="ttl-pick">Auto-shutdown after
          <select id="cc-hours" onchange="classicEstimate()">
            <option value="1">1 hour</option><option value="2" selected>2 hours</option>
            <option value="4">4 hours</option><option value="8">8 hours</option>
          </select>
        </label>
        <div class="cc-est" id="cc-est"></div>
        <button class="btn-launch" ${isAdmin ? '' : 'disabled title="Admin only"'} onclick="launchClassic()">▲ LAUNCH CLASSIC TIER</button>
      </div>${last}`;
  } else if (c.phase === 'launching' || c.phase === 'deleting') {
    const pct = c.phase === 'launching' ? c.progress_pct : 50;
    body = `
      <div class="cc-progress"><div class="cc-bar ${c.phase === 'deleting' ? 'indet' : ''}" style="width:${pct}%"></div></div>
      <div class="muted">${c.phase === 'launching'
        ? `Creating ${esc(c.stack)} — about 12–15 minutes (RDS is the slow part). Stack status ${esc(c.stack_status)}.`
        : `Deleting ${esc(c.stack)} — about 10 minutes.`}</div>
      ${classicEvents(c)}
      ${c.phase === 'launching' && isAdmin ? '<button class="btn-reset" onclick="teardownClassic()">✕ CANCEL &amp; DELETE</button>' : ''}`;
  } else if (c.phase === 'online') {
    body = `
      <div class="cc-online">
        <div class="cc-stat"><span>Size</span><b>${esc(c.size)}</b></div>
        <div class="cc-stat"><span>Auto-shutdown in</span><b id="cc-left">—</b></div>
        <div class="cc-stat"><span>Cost so far</span><b>$${c.cost_so_far_usd.toFixed(2)}</b></div>
        <div class="cc-stat"><span>Launched by</span><b>${esc(c.launched_by || '—')}</b></div>
        <div class="cc-actions">
          <a class="btn-sec" href="#gates">▦ Gates page</a>
          ${isAdmin ? '<button class="btn-sec" onclick="extendClassic()">＋1 h</button><button class="btn-reset" onclick="teardownClassic()">⬛ TEAR DOWN</button>' : ''}
        </div>
      </div>
      <div class="muted cc-dns">ALB ${esc(c.alb_dns || '')} · max lifetime ends ${c.max_expires_at ? new Date(c.max_expires_at * 1000).toISOString().slice(11, 16) + ' UTC' : '—'}</div>`;
  } else {
    body = `<div class="banner banner-err">Stack ${esc(c.stack)} is ${esc(c.stack_status)}. The janitor retries a failed delete every 2 minutes.</div>
      ${classicEvents(c)}
      ${isAdmin ? '<button class="btn-reset" onclick="teardownClassic()">↻ RETRY TEARDOWN</button>' : ''}`;
  }
  el.innerHTML = `
    <div class="cc-hdr"><span class="tier-dot"></span>CLASSIC TIER — EC2 / ALB / RDS
      <span class="cc-phase ${dot}">● ${esc(c.phase.toUpperCase())}</span></div>
    <div class="cc-body">${body}</div>`;
  classicEstimate();
  updateClassicClock();
}

function classicEvents(c) {
  if (!c.events?.length) return '';
  return `<div class="cc-events">${c.events.slice(0, 6).map(e => `<div><span class="${/FAILED/.test(e.status) ? 'bad' : /COMPLETE/.test(e.status) ? 'ok' : 'warn'}">${esc(e.status)}</span> ${esc(e.resource)}${e.reason ? ` <span class="muted">${esc(e.reason)}</span>` : ''}</div>`).join('')}</div>`;
}

function classicEstimate() {
  const est = $('cc-est');
  if (!est || !classicState) return;
  const size = document.querySelector('input[name="cc-size"]:checked')?.value || 'minimal';
  const hours = Number($('cc-hours')?.value || 2);
  est.textContent = `Estimated ≈ $${(classicState.hourly_usd[size] * hours).toFixed(2)} for ${hours} h, then it deletes itself.`;
}

function updateClassicClock() {
  const el = $('cc-left');
  if (el && classicState?.expires_at) el.textContent = hhmmss(classicState.expires_at - Date.now() / 1000);
}

async function pollClassic() {
  try {
    const before = classicState?.phase;
    classicState = await api('/classic/status');
    updateClassicTag();
    if (currentView() !== 'chaos') return;
    if (before !== classicState.phase) { renderClassicCard(); buildChaosCards(); }
    else if (['launching', 'deleting', 'failed'].includes(classicState.phase)) renderClassicCard();
  } catch { /* best effort */ }
}

async function launchClassic() {
  const size = document.querySelector('input[name="cc-size"]:checked')?.value || 'minimal';
  const hours = Number($('cc-hours').value);
  const cost = (classicState.hourly_usd[size] * hours).toFixed(2);
  if (!confirm(`Launch the ${size} Classic tier for ${hours} h?\n\nThis creates billable EC2, ALB and RDS resources (≈ $${cost}). It deletes itself when the time is up.`)) return;
  try {
    classicState = await api('/classic/launch', { method: 'POST', json: { size, hours } });
    chaosAlert(`▲ Classic tier launching (${size}, ${hours} h) — follow progress below`, 'warn');
    renderClassicCard(); buildChaosCards(); updateClassicTag();
  } catch (e) { chaosAlert('✗ ' + errText(e), 'error'); }
}

async function extendClassic() {
  try {
    classicState = await api('/classic/extend', { method: 'POST', json: { hours: 1 } });
    chaosAlert('✓ Classic tier auto-shutdown extended by 1 hour', 'ok');
    renderClassicCard();
  } catch (e) { chaosAlert('✗ ' + errText(e), 'error'); }
}

async function teardownClassic() {
  if (!confirm('Delete the Classic tier stack now? Running Classic faults and FIS experiments are stopped.')) return;
  try {
    classicState = await api('/classic/teardown', { method: 'POST' });
    chaosAlert('⬛ Classic tier teardown started', 'ok');
    await loadChaos();
  } catch (e) { chaosAlert('✗ ' + errText(e), 'error'); }
}

// ═══════════════════════════════════════════════════════════
//  GATES (Classic tier: λ → ALB → EC2 → RDS)
// ═══════════════════════════════════════════════════════════
async function loadGates() {
  const grid = $('gates-grid');
  try {
    const d = await api('/gates');
    setText('gates-inst', `${d.instance_id} · ${d.az}`);
    setText('gates-db', `${d.db_ms} ms`);
    setText('gates-note', `served by ${d.served_by} · end-to-end ${d.proxy_ms} ms`);
    const occ = d.gates.filter(g => g.status === 'occupied').length;
    grid.innerHTML = `<div class="gate-sum">${occ} / ${d.gates.length} gates occupied</div>` + d.gates.map(g => `
      <div class="gate ${g.status === 'occupied' ? 'occ' : ''}">
        <div class="gate-id">${esc(g.gate)}</div>
        <div class="gate-fl">${esc(g.flight || 'OPEN')}</div>
        <div class="gate-t muted">${esc(utc(g.updated_at))}</div>
      </div>`).join('');
  } catch (e) {
    setText('gates-inst', '—'); setText('gates-db', '—'); setText('gates-note', '');
    grid.innerHTML = e.code === 'ClassicOffline'
      ? `<div class="err-card info">The gate-assignment service (Classic tier) is offline.<br><span class="muted">${isViewer()
          ? 'It runs on demand. Ask an operator or admin to launch it.'
          : 'Launch it from <a href="#chaos">Chaos Control</a> — about 12–15 minutes, ≈ $0.07–0.11 / hour, deletes itself when its time is up.'}</span></div>`
      : `<div class="err-card">✗ Gate service unavailable<br>${esc(errText(e))}<br><span class="muted">Path: λ api → ALB → EC2 → RDS. Check the Classic and FIS faults on the Chaos page.</span></div>`;
  }
}

// ═══════════════════════════════════════════════════════════
//  INTEGRATIONS (Streams · EventBridge · SNS · Step Functions · S3)
// ═══════════════════════════════════════════════════════════
function ago(s) { return s == null ? 'never' : s < 90 ? `${s} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`; }

async function loadIntegrations() {
  const grid = $('integ-grid');
  try {
    const d = await api('/integrations');
    grid.innerHTML = d.channels.map(c => `
      <div class="integ ${c.healthy ? 'ok' : 'stale'}">
        <div class="integ-top"><span class="integ-name">${esc(c.label)}</span><span class="integ-dot">${c.healthy ? '● FLOWING' : '● STALE'}</span></div>
        <div class="integ-aws">${esc(c.aws)}</div>
        <div class="integ-stats"><span><b>${fmt(c.count)}</b> received</span><span>last ${esc(ago(c.age_s))}</span></div>
        <pre class="integ-detail">${esc(c.last_detail ? JSON.stringify(c.last_detail, null, 1).slice(0, 260) : '—')}</pre>
      </div>`).join('');
    const f = d.turnaround_last_failure;
    $('integ-fail').hidden = !f;
    if (f) $('integ-fail').textContent = `Last turnaround failure (local mode): ${f.flight} — ${f.error}`;
  } catch (e) { grid.innerHTML = `<div class="err-card">✗ ${esc(errText(e))}</div>`; }
}

// ═══════════════════════════════════════════════════════════
//  AWS RESOURCES (configuration drift)
// ═══════════════════════════════════════════════════════════
async function loadAws() {
  const tbody = $('drift-body');
  tbody.innerHTML = '<tr><td colspan="6" class="loading">Reading AWS configuration…</td></tr>';
  try {
    const [d, cat] = await Promise.all([api('/aws/drift'), catalog ? Promise.resolve(catalog) : api('/chaos/catalog')]);
    catalog = cat;
    setText('drift-note', `${d.drifted ? d.drifted + ' setting(s) differ from the template' : 'Everything matches the template'} · checked ${new Date(d.checked_at * 1000).toISOString().slice(11, 19)} UTC`);
    tbody.innerHTML = d.rows.map(r => `<tr class="drift-${r.status}">
      <td><span class="dstat ${r.status}">${r.status === 'ok' ? '✓ OK' : r.status === 'drift' ? '⚠ DRIFT' : '✗ ERROR'}</span></td>
      <td>${esc(r.resource)}</td><td>${esc(r.setting)}</td><td class="muted">${esc(r.baseline)}</td>
      <td class="${r.status === 'ok' ? '' : 'bad'}">${esc(r.current)}</td>
      <td>${esc(catalog.faults[r.fault]?.label || r.fault)}</td></tr>`).join('');
  } catch (e) { tableError(tbody, 6, e); }
}

// ═══════════════════════════════════════════════════════════
//  RADAR (login background)
// ═══════════════════════════════════════════════════════════
function initRadar() {
  const canvas = $('radar-canvas');
  if (!canvas || canvas.dataset.running) return;
  canvas.dataset.running = '1';
  const ctx = canvas.getContext('2d');
  let angle = 0;
  const blips = Array.from({ length: 9 }, () => ({ r: Math.random() * 0.38 + 0.08, a: Math.random() * Math.PI * 2, sz: Math.random() * 2 + 1 }));
  function draw() {
    if (!$('login-page').classList.contains('active')) { requestAnimationFrame(draw); return; }
    canvas.width = canvas.offsetWidth; canvas.height = canvas.offsetHeight;
    const cx = canvas.width / 2, cy = canvas.height / 2, R = Math.min(cx, cy) * 0.82;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = 'rgba(0,198,255,0.15)'; ctx.lineWidth = 1;
    [0.25, 0.5, 0.75, 1].forEach(f => { ctx.beginPath(); ctx.arc(cx, cy, R * f, 0, Math.PI * 2); ctx.stroke(); });
    ctx.beginPath(); ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy); ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R); ctx.stroke();
    ctx.save(); ctx.translate(cx, cy); ctx.rotate(angle);
    const g = ctx.createLinearGradient(0, 0, R, 0);
    g.addColorStop(0, 'rgba(255,43,214,0.35)'); g.addColorStop(1, 'rgba(0,198,255,0)');
    ctx.fillStyle = g; ctx.beginPath(); ctx.moveTo(0, 0); ctx.arc(0, 0, R, -0.28, 0.28); ctx.closePath(); ctx.fill();
    ctx.restore();
    blips.forEach(b => {
      const bx = cx + Math.cos(b.a) * R * b.r * 2.5, by = cy + Math.sin(b.a) * R * b.r * 2.5;
      const diff = ((angle - b.a) % (Math.PI * 2) + Math.PI * 2) % (Math.PI * 2);
      if (diff < 0.45) { ctx.beginPath(); ctx.arc(bx, by, b.sz, 0, Math.PI * 2); ctx.fillStyle = `rgba(0,230,118,${(1 - diff / 0.45).toFixed(2)})`; ctx.fill(); }
    });
    angle = (angle + 0.012) % (Math.PI * 2);
    requestAnimationFrame(draw);
  }
  draw();
}
