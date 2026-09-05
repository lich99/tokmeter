let CHART_SPLIT;  // donut still uses Chart.js (small, 5 slices)
let curStart, curEnd, curTz='ET', curBucket='auto', curGaps='fill', curSource='cc';
let curCostView = 'role';   // 'role' (Main/Sub) or 'token' (cost-component breakdown)
let curPreset   = '5';      // active preset: '1','5','24','168','720','all','custom'

// ───── Cross-session state persistence ─────
const STATE_KEY = 'cc-usage-state-v2';
function saveState(){
  try {
    localStorage.setItem(STATE_KEY, JSON.stringify({
      source: curSource, bucket: curBucket, gaps: curGaps, tz: curTz,
      costView: curCostView, preset: curPreset,
      start: curStart, end: curEnd,
    }));
  } catch (e) {}
}
function loadState(){
  try { return JSON.parse(localStorage.getItem(STATE_KEY)); }
  catch (e) { return null; }
}
let firstRecordMs = null;
// Render-cache keys: skip recomputing/redrawing pieces of the dashboard whose
// underlying data didn't actually change (e.g. SKIP/FILL toggle only changes
// buckets — totals, project / model breakdown, and cost-split donut stay put).
let _lastSplitKey = null;
let _lastProjKey  = null;
let _lastModelKey = null;

// Pick a sensible bucket size for the given window duration.
// Goal: keep bucket count between ~12 and ~600.
function autoBucket(durMs){
  const h = durMs / 3600000;
  if (h <= 1.5)  return '1m';   // ≤ 90m   → up to 90 buckets
  if (h <= 8)    return '5m';   // ≤ 8h    → up to 96 buckets
  if (h <= 24)   return '15m';  // ≤ 24h   → up to 96 buckets
  if (h <= 72)   return '30m';  // ≤ 3d    → up to 144 buckets
  if (h <= 24*14) return '1h';  // ≤ 14d   → up to 336 buckets
  if (h <= 24*90) return '6h';  // ≤ 90d   → up to 360 buckets
  return '1d';
}
function resolveBucket(){
  return curBucket === 'auto' ? autoBucket(curEnd - curStart) : curBucket;
}

const fmt = {
  // Cost: 2 decimals normally; if |v| < 0.01 keep up to 4 decimals so micro-amounts
  // stay visible during animation (e.g. $0.0042 instead of rounded-to-zero $0.00).
  $: v => {
    const a = Math.abs(v);
    if (a === 0) return '$0.00';
    if (a >= 0.01) return '$' + v.toLocaleString(undefined,{minimumFractionDigits:2, maximumFractionDigits:2});
    return '$' + v.toLocaleString(undefined,{minimumFractionDigits:2, maximumFractionDigits:4});
  },
  // Counts: integer-only — `Math.round` first so animation interpolation never
  // shows fractional values like "25,056.706".
  n: v => Math.round(v).toLocaleString(),
  // Token shorthand: 1.50M, 12.3K, 245
  short: v => {
    const a = Math.abs(v);
    if (a >= 1e9) return (v/1e9).toFixed(2)+'B';
    if (a >= 1e6) return (v/1e6).toFixed(2)+'M';
    if (a >= 1e3) return (v/1e3).toFixed(1)+'K';
    return String(Math.round(v));
  },
};

const dateFormatters = new Map();
function resolvedTz(tz){
  return tz === 'ET' ? 'America/New_York' : tz === 'LOCAL' ? Intl.DateTimeFormat().resolvedOptions().timeZone : tz;
}
function dateParts(ms, tz){
  const zone = resolvedTz(tz);
  if (!dateFormatters.has(zone)) dateFormatters.set(zone, new Intl.DateTimeFormat('en-CA', {
    timeZone: zone, year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23', timeZoneName:'short'
  }));
  return Object.fromEntries(dateFormatters.get(zone).formatToParts(ms).map(p => [p.type,p.value]));
}
function tzOffsetMs(tz, ms=Date.now()){
  const p = dateParts(ms,tz);
  return Date.UTC(+p.year,+p.month-1,+p.day,+p.hour,+p.minute,+p.second) - Math.floor(ms/1000)*1000;
}
function escapeHtml(value){
  return String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function money(value, calls, unknown=0){
  return unknown && unknown === calls ? 'Unpriced' : (unknown ? '≥ ' : '') + fmt.$(value);
}

// ───── Custom dropdown component ─────
class Dropdown {
  constructor(root, onChange){
    this.root = root;
    this.value = root.dataset.value;
    this.options = JSON.parse(root.dataset.options);
    this.onChange = onChange;
    this.trigger = root.querySelector('.dropdown-trigger');
    this.valueEl = root.querySelector('.dropdown-value');
    this._buildMenu();
    this.trigger.addEventListener('click', e => { e.stopPropagation(); this.toggle(); });
    document.addEventListener('click', e => { if (!root.contains(e.target)) this.close(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') this.close(); });
  }
  _buildMenu(){
    const ul = document.createElement('ul');
    ul.className = 'dropdown-menu';
    ul.setAttribute('role', 'listbox');
    ul.innerHTML = this.options.map(o => `
      <li class="dropdown-item${o.v===this.value?' selected':''}" data-value="${o.v}" role="option">
        <span class="lbl">${o.l}</span><span class="check"></span>
      </li>
    `).join('');
    ul.addEventListener('click', e => {
      e.stopPropagation();
      const li = e.target.closest('.dropdown-item');
      if (!li) return;
      this.close();          // close FIRST so the menu starts disappearing immediately
      this.set(li.dataset.value);
    });
    this.root.appendChild(ul);
    this.menu = ul;
  }
  open(){ this.root.classList.add('open'); }
  close(){ this.root.classList.remove('open'); }
  toggle(){ this.root.classList.toggle('open'); }
  set(v){
    if (v === this.value) return;
    this.value = v;
    const opt = this.options.find(o => o.v === v);
    this.valueEl.textContent = opt ? opt.l : v;
    this.menu.querySelectorAll('.dropdown-item').forEach(li => {
      li.classList.toggle('selected', li.dataset.value === v);
    });
    if (this.onChange) this.onChange(v);
  }
}
// Segmented datetime input. 5 small inputs (year, month, day, hour, minute)
// inside a wrapper; tab/arrow navigates between them, digits auto-pad and
// auto-advance, up/down inc/dec the active segment. Wrapper exposes a
// virtual `value` getter/setter and dispatches `change` so existing call
// sites (`getElementById('from').value`, `addEventListener('change', …)`)
// keep working.
class DateTimeField {
  constructor(wrapper) {
    this.el = wrapper;
    wrapper.classList.add('dt-field');
    wrapper.innerHTML = `
      <input class="dt-seg dt-y"  maxlength="4" inputmode="numeric" autocomplete="off" spellcheck="false" placeholder="YYYY">
      <span class="dt-sep">-</span>
      <input class="dt-seg dt-m"  maxlength="2" inputmode="numeric" autocomplete="off" spellcheck="false" placeholder="MM">
      <span class="dt-sep">-</span>
      <input class="dt-seg dt-d"  maxlength="2" inputmode="numeric" autocomplete="off" spellcheck="false" placeholder="DD">
      <span class="dt-sep dt-gap"></span>
      <input class="dt-seg dt-h"  maxlength="2" inputmode="numeric" autocomplete="off" spellcheck="false" placeholder="hh">
      <span class="dt-sep">:</span>
      <input class="dt-seg dt-mi" maxlength="2" inputmode="numeric" autocomplete="off" spellcheck="false" placeholder="mm">
    `;
    this.parts = [
      ['Y',  wrapper.querySelector('.dt-y'),  4, 1970, 2099],
      ['M',  wrapper.querySelector('.dt-m'),  2, 1, 12],
      ['D',  wrapper.querySelector('.dt-d'),  2, 1, 31],
      ['h',  wrapper.querySelector('.dt-h'),  2, 0, 23],
      ['mi', wrapper.querySelector('.dt-mi'), 2, 0, 59],
    ];
    const label = {from:'Start',to:'End','bucket-jump':'Jump to bucket'}[wrapper.id];
    this.parts.forEach((part,i)=>part[1].setAttribute('aria-label',`${label} ${['year','month','day','hour','minute'][i]}`));
    this._lastFired = '';
    this._wire();
    Object.defineProperty(wrapper, 'value', {
      get: () => this._read(),
      set: (v) => this._write(v),
      configurable: true,
    });
    wrapper._dtfield = this;
  }
  _wire() {
    this.parts.forEach((p, i) => {
      const el = p[1];
      el.addEventListener('focus', () => el.select());
      // Always select-all on click — re-clicking a segment treats subsequent
      // typing as overtype, no Backspace required.
      el.addEventListener('click', () => el.select());
      el.addEventListener('keydown', e => this._key(e, i));
      el.addEventListener('input',   () => this._input(i));
      el.addEventListener('blur',    () => this._padSeg(i));
    });
    this.el.addEventListener('mousedown', e => {
      if (e.target === this.el || e.target.classList.contains('dt-sep')) {
        e.preventDefault();
        const empty = this.parts.find(p => !p[1].value) || this.parts[0];
        empty[1].focus();
      }
    });
    // Commit on focus leaving the entire field (tab-out, click-outside).
    // Edits within the same field don't trigger commits.
    this.el.addEventListener('focusout', e => {
      if (!this.el.contains(e.relatedTarget)) this._fire();
    });
  }
  _focus(i) {
    const el = this.parts[i][1];
    el.focus(); el.select();
  }
  _clamp(i, v) {
    const [, , , min, max] = this.parts[i];
    if (isNaN(v)) v = min;
    return Math.max(min, Math.min(max, v));
  }
  _pad(i, n) {
    return String(n).padStart(this.parts[i][2], '0');
  }
  _key(e, i) {
    const [, el, max] = this.parts[i];
    if (e.key === 'Enter') {
      // Commit current edit and exit. Pad active segment first so the
      // value is normalized before the change event fires.
      e.preventDefault();
      this._padSeg(i);
      this._fire();
      el.blur();
      return;
    }
    if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
      e.preventDefault();
      const step = e.key === 'ArrowUp' ? 1 : -1;
      const n = this._clamp(i, (parseInt(el.value, 10) || 0) + step);
      el.value = this._pad(i, n);
      el.select();
      return;
    }
    if (e.key === 'ArrowRight' && el.selectionStart === el.value.length && i < this.parts.length - 1) {
      e.preventDefault(); this._focus(i + 1); return;
    }
    if (e.key === 'ArrowLeft' && el.selectionStart === 0 && i > 0) {
      e.preventDefault();
      const prev = this.parts[i - 1][1];
      prev.focus(); prev.setSelectionRange(prev.value.length, prev.value.length);
      return;
    }
    if (e.key === 'Backspace' && el.value === '' && i > 0) {
      e.preventDefault(); this._focus(i - 1); return;
    }
    const ctrl = e.ctrlKey || e.metaKey;
    const allowed = ['Tab','Escape','Backspace','Delete','Home','End'];
    if (!ctrl && e.key.length === 1 && !/[0-9]/.test(e.key) && !allowed.includes(e.key)) {
      e.preventDefault();
    }
  }
  _input(i) {
    const [, el, max, min, hi] = this.parts[i];
    el.value = el.value.replace(/\D/g, '');
    if (el.value.length >= max) {
      const n = this._clamp(i, parseInt(el.value, 10));
      el.value = this._pad(i, n);
      if (i < this.parts.length - 1) this._focus(i + 1);
      return;
    }
    // Smart advance: if the first digit alone can't be the start of any
    // valid 2-digit value (e.g. month '5' has no 5X in 1–12), pad with 0
    // and jump to the next segment.
    if (max === 2 && el.value.length === 1) {
      const d1 = parseInt(el.value, 10);
      let any = false;
      for (let d2 = 0; d2 <= 9 && !any; d2++) {
        const v = d1 * 10 + d2;
        if (v >= min && v <= hi) any = true;
      }
      if (!any) {
        el.value = this._pad(i, this._clamp(i, d1));
        if (i < this.parts.length - 1) this._focus(i + 1);
      }
    }
  }
  // Pad-only on segment blur — visual normalization, NOT a commit.
  // (Tabbing between segments shouldn't trigger backend reloads.)
  _padSeg(i) {
    const [, el] = this.parts[i];
    if (el.value === '') return;
    const n = this._clamp(i, parseInt(el.value, 10));
    el.value = this._pad(i, n);
  }
  _fire() {
    const v = this._read();
    if (v === this._lastFired) return;
    this._lastFired = v;
    this.el.dispatchEvent(new Event('change', { bubbles: true }));
  }
  _read() {
    const v = this.parts.map(p => p[1].value);
    if (v.some(x => !x)) return '';
    return `${v[0]}-${v[1]}-${v[2]} ${v[3]}:${v[4]}`;
  }
  _write(s) {
    if (!s) {
      this.parts.forEach(p => p[1].value = '');
      const label = {from:'Start',to:'End','bucket-jump':'Jump to bucket'}[wrapper.id];
    this.parts.forEach((part,i)=>part[1].setAttribute('aria-label',`${label} ${['year','month','day','hour','minute'][i]}`));
    this._lastFired = '';
      return;
    }
    const m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})$/);
    if (!m) return;
    for (let k = 0; k < 5; k++) this.parts[k][1].value = m[k+1];
    this._lastFired = this._read();
  }
}

// Plain-text datetime field: parse `YYYY-MM-DD HH:MM` (also tolerates `T`
// separator for legacy state and trims spaces). Returns null on bad input
// so callers can fall back to the prior value rather than NaN-propagating.
function inputToUtcMs(s, tz){
  const m = String(s || '').trim().match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})$/);
  if (!m) return null;
  const wall = Date.UTC(+m[1],+m[2]-1,+m[3],+m[4],+m[5]);
  const canonical = `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
  const candidates = [-43200000,0,43200000].map(delta => wall-tzOffsetMs(tz,wall+delta));
  const valid = candidates.filter(ms => utcMsToInput(ms,tz) === canonical);
  return valid.length ? Math.min(...valid) : null; // Earlier fold; nonexistent local times are rejected.
}
function utcMsToInput(ms,tz){
  const p = dateParts(ms,tz);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
}

function toast(msg, kind=''){
  const el = document.getElementById('toast');
  el.querySelector('.msg').textContent = msg;
  el.className = 'toast show ' + kind;
  clearTimeout(el._to);
  el._to = setTimeout(()=>el.classList.remove('show'), 2400);
}

// Live accounting values update immediately; polling must never replay entry animations.
function setNumber(el, value, formatter){
  const text = formatter(value);
  if (el.textContent !== text) el.textContent = text;
}

async function api(path){
  const r = await fetch(path);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}


let requestVersion = 0, requestController = null, loadingRetry = 0;
async function loadAggregate(quiet=false){
  if (!curStart || !curEnd || (quiet && requestController)) return;
  if (curPreset !== 'custom') {
    curEnd = Date.now();
    curStart = curPreset === 'all' ? (firstRecordMs || curEnd-30*86400000) : curEnd-Number(curPreset)*3600000;
    if (!document.activeElement?.closest('.dt-field')) {
      document.getElementById('from').value = utcMsToInput(curStart,curTz);
      document.getElementById('to').value = utcMsToInput(curEnd,curTz);
    }
  }
  const version = ++requestVersion;
  requestController?.abort(); requestController = new AbortController();
  clearTimeout(loadingRetry);
  const qs = new URLSearchParams({from:String(curStart),to:String(curEnd),granularity:resolveBucket(),tz:resolvedTz(curTz),gaps:curGaps,source:curSource});
  try {
    const response = await fetch('/api/aggregate?'+qs,{signal:requestController.signal});
    const data = await response.json();
    if (version !== requestVersion) return;
    if (!response.ok) throw new Error(data.error || 'Request failed');
    if (!data.ready) {
      document.getElementById('data-status').textContent = data.error || 'Reading local usage…';
      loadingRetry = setTimeout(()=>loadAggregate(),500);
      return;
    }
    firstRecordMs = data.firstRecordMs;
    if (curPreset === 'all' && firstRecordMs != null && curStart !== firstRecordMs) {
      curStart = firstRecordMs;
      loadingRetry = setTimeout(()=>loadAggregate(),0); return;
    }
    window._lastAggregateData = data;
    render(data);
    const issues = [];
    if (data.totals.unpricedCalls) issues.push(`${fmt.n(data.totals.unpricedCalls)} unpriced calls excluded`);
    if (data.totals.unknownTierCalls) issues.push(`${fmt.n(data.totals.unknownTierCalls)} calls have no recorded tier; standard-rate reference only`);
    if (data.totals.unsupportedTierCalls) issues.push(`${fmt.n(data.totals.unsupportedTierCalls)} calls have no supported tier price`);
    if (data.scan?.unresolved_forks) issues.push(`${data.scan.unresolved_forks} fork histories could not be verified`);
    if (data.scan?.malformed || data.scan?.invalid_usage) issues.push(`${(data.scan.malformed||0)+(data.scan.invalid_usage||0)} invalid records skipped`);
    if (data.scan?.errors?.length) issues.push(`${data.scan.errors.length} files could not be read`);
    if (data.error) issues.push(data.error);
    document.getElementById('data-status').textContent = `API-equivalent estimate · prices ${data.pricingVersion}` + (issues.length ? ' · '+issues.join(' · ') : '');
    document.getElementById('tier-status').textContent = 'Recorded processing tier · ' + (data.byTier || []).map(row=>`${row.name}: ${fmt.n(row.calls)}`).join(' · ');
    document.getElementById('gen-info').textContent = `${fmt.n(data.totalRecords)} REC · ${fmt.n(data.buckets.ts.length)} BUCKETS · UPDATED ${new Date(data.lastRefresh*1000).toLocaleTimeString()}`;
    saveState();
  } catch (error) {
    if (version === requestVersion && error.name !== 'AbortError') {
      document.getElementById('data-status').textContent = error.message;
      if (!quiet) toast('Unable to load usage','err');
    }
  } finally {
    if (version === requestVersion) requestController = null;
  }
}

function timeAgo(ms){
  const d = (Date.now() - ms)/1000;
  if (d < 60) return Math.floor(d) + 'S AGO';
  if (d < 3600) return Math.floor(d/60) + 'M AGO';
  if (d < 86400) return Math.floor(d/3600) + 'H AGO';
  return Math.floor(d/86400) + 'D AGO';
}

// ───── uPlot time chart (canvas, handles 100k+ points) ─────
let UPLOT_TS = null, timeChartState = null;

// Full bucket label — used in tooltips and table where horizontal room is plenty.
function formatBucketKey(utcMs, _offMs, granularity){
  const p = dateParts(utcMs,curTz);
  const day = `${p.year}-${p.month}-${p.day}`;
  return granularity === '1d' ? day : `${day} ${p.hour}:${p.minute} ${p.timeZoneName}`;
}
function formatBucketKeyAxis(utcMs,_offMs,granularity,rangeMs){
  const p = dateParts(utcMs,curTz);
  if (granularity === '1d' || rangeMs > 7*86400000) return `${p.month}/${p.day}`;
  return rangeMs > 86400000 ? `${p.month}/${p.day} ${p.hour}h` : `${p.hour}:${p.minute}`;
}

function ensureUplotTooltip(parent){
  let tip = parent.querySelector('.u-tooltip');
  if (!tip) {
    tip = document.createElement('div');
    tip.className = 'u-tooltip';
    tip.style.opacity = '0';
    parent.appendChild(tip);
  }
  return tip;
}

function renderTimeChartUplot(b, granularity, source){
  const wrap = document.getElementById('chart-ts-wrap');
  if (!wrap || !window.uPlot) return;
  const empty = document.getElementById('chart-ts-empty');
  const legendEl = document.getElementById('ts-legend');
  const N = b.ts.length;
  if (N === 0) {
    if (UPLOT_TS) { UPLOT_TS.destroy(); UPLOT_TS = null; timeChartState = null; }
    if (empty) empty.hidden = false;
    if (legendEl) legendEl.innerHTML = '';
    return;
  }
  if (empty) empty.hidden = true;

  const offMs = tzOffsetMs(curTz);
  const isSkip = curGaps === 'skip';

  // X axis — wall-clock seconds (FILL) or ordinal index 0..N-1 (SKIP).
  const xs = new Float64Array(N);
  for (let i = 0; i < N; i++) xs[i] = isSkip ? i : b.ts[i] / 1000;

  // Cumulative line (always shown on the right Y axis).
  const cumArr = new Float64Array(N);
  { let s = 0; for (let i = 0; i < N; i++) { s += b.cost[i]; cumArr[i] = s; } }

  // Layer definitions per view mode + source.
  let layers;
  if (curCostView === 'token') {
    layers = (source === 'codex') ? [
      {label:'Cached',         arr: b.cCached, color:'#0A0A0A'},
      {label:'Output (text)',  arr: b.cOut,    color:'#FFD81F'},
      {label:'Reasoning',      arr: b.cReason, color:'#FF2BA0'},
      {label:'Input',          arr: b.cIn,     color:'#FF6A1A'},
    ] : [
      {label:'Cache read',     arr: b.cCr,     color:'#0A0A0A'},
      {label:'Cache write 1h', arr: b.cCw1h,   color:'#FF6A1A'},
      {label:'Output',         arr: b.cOut,    color:'#FFD81F'},
      {label:'Cache write 5m', arr: b.cCw5m,   color:'#FF2BA0'},
      {label:'Input',          arr: b.cIn,     color:'#7C3AED'},
    ];
    if (source === 'all') layers.push({label:'Cached input',arr:b.cCached,color:'#4D5564'},{label:'Reasoning',arr:b.cReason,color:'#7C3AED'});
  } else {
    layers = [
      {label:'Main',     arr: b.mainCost, color:'#0A0A0A'},
      {label:'Subagent', arr: b.subCost,  color:'#FF6A1A'},
    ];
  }

  // Cumulative-stacked data per layer: each series's data is the SUM of
  // its layer + all layers below. Drawn in REVERSE (top-most/largest first)
  // so each layer's color shows in its strip after the next-smaller layer
  // covers the lower portion.
  const stacked = layers.map(() => new Float64Array(N));
  for (let i = 0; i < N; i++) {
    let s = 0;
    for (let l = 0; l < layers.length; l++) {
      s += (layers[l].arr[i] || 0);
      stacked[l][i] = s;
    }
  }

  const data = [xs, ...stacked.slice().reverse(), cumArr];
  const key = JSON.stringify([granularity, source, curTz, curGaps, curCostView]);
  const signature = JSON.stringify(b);
  const view = {b, N, layers, cumArr};
  if (UPLOT_TS && timeChartState?.key === key) {
    Object.assign(timeChartState.view, view);
    if (timeChartState.signature !== signature) {
      UPLOT_TS.setData(data);
      timeChartState.signature = signature;
    } else if (!isSkip && (UPLOT_TS.scales.x.min !== curStart/1000 || UPLOT_TS.scales.x.max !== curEnd/1000)) {
      UPLOT_TS.setScale('x', {min:curStart/1000, max:curEnd/1000});
    }
    return;
  }
  if (UPLOT_TS) UPLOT_TS.destroy();
  timeChartState = {key, signature, view};

  // Update legend (HTML, not Chart.js)
  if (legendEl) {
    legendEl.innerHTML = layers.map(l =>
      `<span><span class="swatch" style="background:${l.color}"></span>${l.label}</span>`
    ).join('') +
    `<span><span class="swatch" style="background:#0042FF"></span>Cumulative</span>`;
  }

  const tip = ensureUplotTooltip(wrap);
  const fmtKey = ts => formatBucketKey(ts*1000,0,granularity);

  // Stacked: draw "Sub-Total" (full height, orange) FIRST, then "Main" (black) on top.
  // The visible bottom portion = main (black), the visible top = sub (orange).
  //
  // CUSTOM PATHS: two-stage aggregation that gives uniform bar widths AND
  // visible gaps regardless of data density.
  //   1) Project every point to its integer X pixel, max-aggregate per column.
  //   2) Inspect the smallest spacing between active columns:
  //      - if spacing < 3px → DENSE: re-aggregate columns into 3-pixel slots
  //        (2px bar + 1px gap), drawing on a fixed grid.
  //      - else → SPARSE: draw at the column's true X with uniform width
  //        = 70% of the min inter-column spacing (so bars never touch).
  // Result: same width across the whole chart, always with a gap, and no
  // 86k-segment Path2D blowing up the GPU.
  const fastBars = () => (u, seriesIdx, idx0, idx1) => {
    const fill = new Path2D();
    const xs = u.data[0];
    const ys = u.data[seriesIdx];
    const yScaleKey = u.series[seriesIdx].scale;
    const yBaseline = u.valToPos(0, yScaleKey, true);
    const xMinPx = u.bbox.left;

    // Stage 1: max-aggregate to integer X pixel columns
    const cols = new Map();
    for (let i = idx0; i <= idx1; i++) {
      const v = ys[i];
      if (v == null || v <= 0) continue;
      const px = Math.floor(u.valToPos(xs[i], 'x', true));
      const py = u.valToPos(v, yScaleKey, true);
      const cur = cols.get(px);
      if (cur === undefined || py < cur) cols.set(px, py);
    }
    if (cols.size === 0) return { fill, stroke: null };

    const colKeys = [...cols.keys()].sort((a, b) => a - b);
    let minSpacing = Infinity;
    for (let i = 1; i < colKeys.length; i++) {
      const d = colKeys[i] - colKeys[i - 1];
      if (d < minSpacing) minSpacing = d;
    }
    if (colKeys.length === 1) minSpacing = 24;

    if (minSpacing < 3) {
      // DENSE: re-aggregate into 3-pixel slots so bars get visible gaps
      const BAR_W = 2, GAP = 1;
      const SLOT = BAR_W + GAP;
      const slots = new Map();
      cols.forEach((top, px) => {
        const idx = Math.floor((px - xMinPx) / SLOT);
        const cur = slots.get(idx);
        if (cur === undefined || top < cur) slots.set(idx, top);
      });
      slots.forEach((top, idx) => {
        const x = xMinPx + idx * SLOT;
        fill.rect(x, top, BAR_W, yBaseline - top);
      });
    } else {
      // SPARSE: uniform width across all bars, drawn at real X positions
      const w = Math.max(2, Math.min(28, Math.floor(minSpacing * 0.70)));
      const halfW = w >> 1;
      cols.forEach((top, px) => {
        fill.rect(px - halfW, top, w, yBaseline - top);
      });
    }
    return { fill, stroke: null };
  };

  const opts = {
    width: wrap.clientWidth,
    height: wrap.clientHeight,
    // Tight padding — bars sit right after the y-axis labels.
    padding: [6, 2, 0, 2],
    cursor: {
      drag: { x: false, y: false },
      points: { show: false },
    },
    legend: { show: false },
    scales: {
      x: isSkip
        ? { time: false, range: () => [-0.5, Math.max(0.5, view.N - 0.5)] }
        : { time: true, range: () => [
            curStart / 1000,
            curEnd / 1000,
          ]},
      y:  { range: (u, dmin, dmax) => [0, (dmax || 1) * 1.06] },
      y2: { range: (u, dmin, dmax) => [0, (dmax || 1) * 1.06] },
    },
    axes: (() => {
      // System mono ensures labels render at correct size even if IBM Plex Mono
      // hasn't finished loading — webfonts on canvas are flaky.
      const AXIS_FONT  = '500 14px ui-monospace, "SF Mono", "JetBrains Mono", "IBM Plex Mono", Menlo, monospace';
      const AXIS_INK   = '#1A1A19';
      const GRID_LINE  = '#E8E5DC';

      const xTime = {
        values: (u, splits) => splits.map(v => formatBucketKeyAxis(v*1000,0,granularity,curEnd-curStart)),
        stroke: AXIS_INK, font: AXIS_FONT,
        grid: { stroke: 'transparent' },
        ticks: { stroke: '#0A0A0A', size: 6, width: 1 },
        // 50px gives room for uPlot's 2-row time format (e.g. "9pm" + "5/8")
        space: 130, size: 50, gap: 4,
      };
      // Compute the actual time span of visible data for adaptive label format.
      const xOrdinal = {
        ...xTime,
        space: 160,
        splits: (u, axisIdx, sMin, sMax) => {
          // 160px per label gives "M/D HH" room to breathe without overlap
          const target = Math.max(2, Math.floor(u.bbox.width / 160));
          const stride = Math.max(1, Math.ceil((sMax - sMin) / target));
          const out = [];
          let i = Math.max(0, Math.ceil(sMin / stride) * stride);
          while (i <= sMax) { if (i < view.N) out.push(i); i += stride; }
          return out;
        },
        values: (u, splits) => splits.map(v => {
          const i = Math.round(v);
          if (i < 0 || i >= view.N) return '';
          return formatBucketKeyAxis(view.b.ts[i], offMs, granularity, view.N >= 2 ? view.b.ts[view.N-1]-view.b.ts[0] : 86400000);
        }),
      };
      const yLeft = {
        stroke: AXIS_INK, font: AXIS_FONT,
        grid: { stroke: GRID_LINE, width: 1 },
        ticks: { show: false },
        size: 64, gap: 4,
        values: (u, vals) => vals.map(v => Math.abs(v)>=1000 ? '$'+fmt.short(v) : fmt.$(v)),
      };
      const yRight = {
        side: 1, stroke: AXIS_INK, scale: 'y2', font: AXIS_FONT,
        grid: { stroke: 'transparent' }, ticks: { show: false },
        size: 64, gap: 4,
        values: (u, vals) => vals.map(v => Math.abs(v)>=1000 ? '$'+fmt.short(v) : fmt.$(v)),
      };
      return [isSkip ? xOrdinal : xTime, yLeft, yRight];
    })(),
    series: (() => {
      // x  | view.layers REVERSED (largest first) | cumulative line
      const arr = [{ value: (u, v) => v == null ? '—' : fmtKey(v) }];
      for (let l = view.layers.length - 1; l >= 0; l--) {
        arr.push({
          label: view.layers[l].label,
          stroke: view.layers[l].color, fill: view.layers[l].color, width: 0,
          paths: fastBars(), points: { show: false },
        });
      }
      arr.push({
        label: 'Cumulative', stroke: '#0042FF', width: 1.6,
        fill: 'rgba(0,66,255,0.06)', scale: 'y2', points: { show: false },
      });
      return arr;
    })(),
    hooks: {
      setCursor: [
        u => {
          const idx = u.cursor.idx;
          if (idx == null || idx < 0 || idx >= view.N) { tip.style.opacity = '0'; return; }
          const key = formatBucketKey(view.b.ts[idx], offMs, granularity);
          const layerLines = view.layers.map(l =>
            `<div class="t-row"><span class="lbl">${l.label}</span><span class="v">$${(l.arr[idx]||0).toFixed(2)}</span></div>`
          ).join('');
          tip.innerHTML = `
            <div class="t-key">${key}</div>
            ${layerLines}
            <div class="t-row"><span class="lbl">Cumulative</span><span class="v">$${view.cumArr[idx].toFixed(2)}</span></div>
            <div class="t-row"><span class="lbl">Calls</span><span class="v">${view.b.calls[idx].toLocaleString()}</span></div>`;
          const left = u.cursor.left;
          const top = u.cursor.top;
          const tw = tip.offsetWidth, th = tip.offsetHeight;
          const x = Math.min(Math.max(left + 14, 0), wrap.clientWidth - tw - 4);
          const y = Math.min(Math.max(top - th - 12, 4), wrap.clientHeight - th - 4);
          tip.style.transform = `translate(${x}px, ${y}px)`;
          tip.style.opacity = '1';
        }
      ],
    },
  };

  UPLOT_TS = new uPlot(opts, data, wrap);
}

function render(d){
  const t = d.totals;
  setNumber(document.getElementById('s-cost'), t.cost, v => money(v,t.calls,t.unpricedCalls));
  const dur = (curEnd - curStart)/3600000;
  document.getElementById('s-cost-sub').textContent =
    `${fmt.$(t.cost / Math.max(dur,0.01))}/H · ${dur.toFixed(1)}H WINDOW`;
  setNumber(document.getElementById('s-calls'), t.calls, fmt.n);
  document.getElementById('s-calls-sub').textContent =
    `${t.main.calls.toLocaleString()} MAIN · ${t.sub.calls.toLocaleString()} SUB`;
  setNumber(document.getElementById('s-out'), t.out, fmt.short);
  document.getElementById('s-out-sub').textContent = `${fmt.n(t.out)} TOKENS`;
  // Cache stat: for Claude Code → cache_read; for Codex → cached_input
  const isCodex = (d.source === 'codex');
  const cacheTok = (t.cached || 0) + t.cr;
  const cacheLbl = isCodex ? '04 — CACHED INPUT' : '04 — CACHE R';
  document.getElementById('s-cache-lbl').textContent = cacheLbl;
  setNumber(document.getElementById('s-cr'), cacheTok, fmt.short);
  if (isCodex) {
    // Codex: hit ratio = cached / total_input
    const totalIn = t.in;
    document.getElementById('s-cr-sub').textContent = totalIn > 0
      ? `${(cacheTok / totalIn * 100).toFixed(1)}% HIT RATIO` : '—';
  } else {
    const totalIn = t.in + t.cw5m + t.cw1h + t.cr;
    document.getElementById('s-cr-sub').textContent = totalIn > 0
      ? `${(cacheTok / totalIn * 100).toFixed(1)}% HIT RATIO` : '—';
  }
  // Total tokens touched. CC: input is the new (uncached) input, so add all cache buckets + output.
  // Codex: `in_t` already includes cached input, so just in + out (avoid double-counting).
  const qp = isCodex
    ? (t.in + t.out)
    : (t.in + t.cw5m + t.cw1h + t.cr + t.out);
  setNumber(document.getElementById('s-qp'), qp, fmt.short);
  document.getElementById('s-qp-sub').textContent =
    isCodex ? `${fmt.n(qp)} TOKENS · IN+OUT`
            : `${fmt.n(qp)} TOKENS · IN+CACHE+OUT`;

  // Update the resolved-bucket badge in the chart title
  const badge = document.getElementById('resolved-bucket');
  if (badge) badge.textContent = (d.granularity || '').toUpperCase();

  // Time chart — uPlot canvas (renders 40k+ bars at 60fps; Chart.js DOM can't keep up).
  renderTimeChartUplot(d.buckets, d.granularity, d.source);

  // Cost split donut — only redraw when split values actually change.
  // Avoid re-creating the chart when only the bucket layout changes.
  const splitKey = JSON.stringify(d.costSplit);
  if (splitKey !== _lastSplitKey) {
    _lastSplitKey = splitKey;
    const ctx2 = document.getElementById('chart-split');
    const splitEmpty = document.getElementById('chart-split-empty');
    // Backend now sends an array [{key,label,val,color}] sized to the source
    // (5 slices for Claude Code, 4 for Codex including a Reasoning slice).
    const cats = (d.costSplit || []).filter(c => c.val > 0)
      .map(c => ({ label: c.label, val: c.val, c: c.color }));
    const splitLegend = document.getElementById('split-legend');
    if (cats.length === 0) {
      if (CHART_SPLIT) { CHART_SPLIT.destroy(); CHART_SPLIT = null; }
      if (splitEmpty) splitEmpty.hidden = false;
      if (splitLegend) splitLegend.innerHTML = '';
      // Skip Chart.js construction when there's nothing to show
      _lastSplitKey = splitKey;
    } else {
      if (splitEmpty) splitEmpty.hidden = true;
    if (CHART_SPLIT) {
      CHART_SPLIT.data.labels = cats.map(c=>c.label);
      CHART_SPLIT.data.datasets[0].data = cats.map(c=>c.val);
      CHART_SPLIT.data.datasets[0].backgroundColor = cats.map(c=>c.c);
      CHART_SPLIT.update('none');
    } else CHART_SPLIT = new Chart(ctx2, {
      type:'doughnut',
      data:{labels:cats.map(c=>c.label), datasets:[{data:cats.map(c=>c.val),
        backgroundColor:cats.map(c=>c.c), borderColor:'#FFFFFF', borderWidth:4, hoverOffset: 8}]},
      options:{
        responsive:true, maintainAspectRatio:false, cutout:'62%',
        animation: false,
        plugins:{
          legend:{display:false},
          tooltip:{backgroundColor:'#0A0A0A', titleColor:'#FFFFFF', bodyColor:'#D4D4D4',
            borderColor:'#FFD81F', borderWidth:0, padding:14, cornerRadius:0,
            titleFont: {family: 'IBM Plex Mono', size: 11, weight: '500'},
            bodyFont: {family: 'IBM Plex Mono', size: 11.5, weight: '400'},
            boxPadding: 10, boxWidth: 10, boxHeight: 10,
            callbacks:{label:c=>`  ${c.label}    $${c.parsed.toFixed(2)} · ${(c.parsed/c.dataset.data.reduce((sum,v)=>sum+v,0)*100).toFixed(1)}%`}}
        }
      }
    });
    // Custom HTML legend (responsive, never wraps awkwardly)
    if (splitLegend) {
      splitLegend.innerHTML = cats.map(c => {
        const pct = t.cost > 0 ? (c.val/t.cost*100).toFixed(1) : '0';
        return `<li>
          <span class="swatch" style="background:${c.c}"></span>
          <span class="label">${c.label}</span>
          <span class="pct">${pct}%</span>
        </li>`;
      }).join('');
    }
    }  // end of `cats.length > 0` branch
  }

  // Bar lists — same idea: only re-render when the underlying breakdown changed.
  const projKey = JSON.stringify(d.byProject);
  if (projKey !== _lastProjKey) { _lastProjKey = projKey; renderBars('bar-projects', d.byProject); }
  const modelKey = JSON.stringify(d.byModel);
  if (modelKey !== _lastModelKey) { _lastModelKey = modelKey; renderBars('bar-models', d.byModel); }
  renderBucketTable(d.buckets, d.granularity, t, d.source);
}

function renderBars(elId, items){
  const el = document.getElementById(elId);
  if (!items.length){ el.innerHTML = '<div class="empty">No data in this window</div>'; return; }
  const sorted = [...items].sort((a,b)=>b.cost-a.cost).slice(0,10);
  const max = Math.max(...sorted.map(x=>x.cost));
  el.innerHTML = sorted.map(x=>`
    <div class="bar-item">
      <div class="name" title="${escapeHtml(x.name)}">${escapeHtml(x.name)}</div>
      <div class="val">${money(x.cost,x.calls,x.unpricedCalls)}<span class="pill">${x.calls}</span></div>
      <div class="bar-track"><div class="bar-fill" style="width:${max>0?(x.cost/max*100):0}%"></div></div>
    </div>`).join('');
}

// ───── Virtual-scrolled bucket table — only paints visible rows ─────
const TABLE_ROW_H = 38;
const TABLE_BUFFER = 8;
const _vt = { buckets: null, granularity: '1h', tz: 'ET', source: 'cc', lastStart: -1, lastEnd: -1 };

// Column 4-6 differ by source. CC: Output / Cache W / Cache R. Codex: Cached / Output / Reasoning.
function _bucketCol(b, i, key) {
  if (key === 'in')        return b.in[i];
  if (key === 'cached')    return b.cached[i];
  if (key === 'out')       return b.out[i];
  if (key === 'reason')    return b.reason[i];
  if (key === 'cw_total')  return b.cw5m[i] + b.cw1h[i];
  if (key === 'cr')        return b.cr[i];
  if (key === 'cache_read') return b.cr[i] + b.cached[i];
  return 0;
}

function paintVisibleRows(force){
  const wrap = document.getElementById('bucket-scroll');
  const tb = document.querySelector('#bucket-table tbody');
  const b = _vt.buckets;
  if (!b) return;
  const n = b.ts.length;
  const wrapH = wrap.clientHeight;
  const scrollTop = wrap.scrollTop;
  const startI = Math.min(n, Math.max(0, Math.floor(scrollTop / TABLE_ROW_H) - TABLE_BUFFER));
  const endI   = Math.min(n, Math.ceil((scrollTop + wrapH) / TABLE_ROW_H) + TABLE_BUFFER);
  if (!force && startI === _vt.lastStart && endI === _vt.lastEnd) return;
  _vt.lastStart = startI; _vt.lastEnd = endI;

  const offMs = tzOffsetMs(_vt.tz);
  const g = _vt.granularity;
  const isCx = _vt.source === 'codex';
  // Per-source column ordering for the 3 middle columns:
  //   CC:    [Output, Cache W (5m+1h), Cache R]
  //   Codex: [Cached, Output, Reasoning]
  const c1 = isCx ? 'cached' : 'out';
  const c2 = isCx ? 'out'    : 'cw_total';
  const c3 = isCx ? 'reason' : (_vt.source === 'all' ? 'cache_read' : 'cr');

  const padTop = startI * TABLE_ROW_H;
  const padBot = (n - endI) * TABLE_ROW_H;
  const rows = new Array(endI - startI + 2);
  rows[0] = `<tr class="vspacer" style="height:${padTop}px"><td colspan="7"></td></tr>`;
  for (let i = startI; i < endI; i++) {
    rows[i - startI + 1] =
      '<tr><td>' + formatBucketKey(b.ts[i], offMs, g) +
      '</td><td>' + fmt.n(b.calls[i]) +
      '</td><td>' + fmt.short(b.in[i]) +
      '</td><td>' + fmt.short(_bucketCol(b, i, c1)) +
      '</td><td>' + fmt.short(_bucketCol(b, i, c2)) +
      '</td><td>' + fmt.short(_bucketCol(b, i, c3)) +
      '</td><td>' + money(b.cost[i],b.calls[i],b.unpricedCalls?.[i]) +
      '</td></tr>';
  }
  rows[rows.length - 1] = `<tr class="vspacer" style="height:${padBot}px"><td colspan="7"></td></tr>`;
  tb.innerHTML = rows.join('');
}

function renderBucketTable(b, granularity, totals, source){
  _vt.buckets = b;
  _vt.granularity = granularity;
  _vt.tz = curTz;
  _vt.source = source || 'cc';
  _vt.lastStart = _vt.lastEnd = -1;
  const wrap = document.getElementById('bucket-scroll');
  wrap.scrollTop = Math.min(wrap.scrollTop,Math.max(0,b.ts.length*TABLE_ROW_H-wrap.clientHeight));
  // Set per-source column headers + totals row
  const isCx = _vt.source === 'codex';
  document.getElementById('th-c1').textContent = isCx ? 'Cached' : 'Output';
  document.getElementById('th-c2').textContent = isCx ? 'Output' : 'Cache W';
  document.getElementById('th-c3').textContent = isCx ? 'Reasoning' : 'Cache R';
  paintVisibleRows(true);
  document.getElementById('bucket-counter').textContent =
    `${b.ts.length.toLocaleString()} ROWS`;
  document.getElementById('t-calls').textContent = fmt.n(totals.calls);
  document.getElementById('t-in').textContent    = fmt.short(totals.in);
  if (isCx) {
    document.getElementById('t-c1').textContent = fmt.short(totals.cached || 0);
    document.getElementById('t-c2').textContent = fmt.short(totals.out);
    document.getElementById('t-c3').textContent = fmt.short(totals.reason || 0);
  } else {
    document.getElementById('t-c1').textContent = fmt.short(totals.out);
    document.getElementById('t-c2').textContent = fmt.short(totals.cw5m + totals.cw1h);
    document.getElementById('t-c3').textContent = fmt.short(totals.cr + (totals.cached || 0));
  }
  document.getElementById('t-cost').textContent  = money(totals.cost,totals.calls,totals.unpricedCalls);
}

(function attachTableScroll(){
  const wrap = document.getElementById('bucket-scroll');
  if (!wrap) return;
  let raf = 0;
  wrap.addEventListener('scroll', () => {
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; paintVisibleRows(false); });
  }, { passive: true });
})();

// Jump to a specific bucket by datetime — binary search the ts array
document.getElementById('bucket-jump').addEventListener('change', (e) => {
  const v = e.target.value;
  if (!v) return;
  const b = _vt.buckets;
  if (!b || !b.ts.length) return;
  const target = inputToUtcMs(v, _vt.tz);
  // bisect_left
  let lo = 0, hi = b.ts.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (b.ts[mid] < target) lo = mid + 1; else hi = mid;
  }
  const idx = Math.min(b.ts.length - 1, lo);
  const wrap = document.getElementById('bucket-scroll');
  // Center the target row in the viewport
  const target_top = idx * TABLE_ROW_H - wrap.clientHeight / 2 + TABLE_ROW_H;
  wrap.scrollTo({ top: Math.max(0, target_top), behavior: 'smooth' });
});

function setRangePreset(hOrAll){
  curPreset = String(hOrAll);
  const tz = curTz;
  const end = Date.now();
  let start;
  if (hOrAll === 'all') {
    start = firstRecordMs || (end - 30*86400000);
  } else {
    start = end - hOrAll*3600000;
  }
  curStart = start; curEnd = end;
  document.getElementById('from').value = utcMsToInput(start, tz);
  document.getElementById('to').value   = utcMsToInput(end, tz);
  loadAggregate();
}

function setRangeFromInputs(){
  const tz = curTz;
  const fromEl = document.getElementById('from');
  const toEl = document.getElementById('to');
  const ns = inputToUtcMs(fromEl.value, tz);
  const ne = inputToUtcMs(toEl.value, tz);
  // Don't auto-rewrite the user's typed value — they may still be
  // editing the OTHER field. Just skip applying until the range is valid.
  if (ns == null || ne == null) return;
  if (ns >= ne) { toast('Start must be before End', 'err'); return; }
  curPreset = 'custom';
  curStart = ns; curEnd = ne;
  document.querySelectorAll('.preset').forEach(b=>b.classList.remove('active'));
  loadAggregate();
}

document.querySelectorAll('#presets .preset').forEach(btn=>{
  btn.addEventListener('click', ()=>{
    document.querySelectorAll('#presets .preset').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    const h = btn.dataset.h;
    setRangePreset(h==='all' ? 'all' : Number(h));
  });
});

// Cost view segmented toggle (By role / By token) — re-renders ONLY the chart,
// no need to re-fetch since the buckets payload already includes both views.
document.querySelectorAll('#seg-costview .seg-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    if (btn.classList.contains('active')) return;
    document.querySelectorAll('#seg-costview .seg-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    curCostView = btn.dataset.v;
    if (window._lastAggregateData) {
      renderTimeChartUplot(window._lastAggregateData.buckets,
                           window._lastAggregateData.granularity,
                           window._lastAggregateData.source);
    }
    saveState();
  });
});

// Source tabs — Claude Code / Codex (mutually exclusive)
document.querySelectorAll('#source-tabs .src-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    if (btn.classList.contains('active')) return;
    document.querySelectorAll('#source-tabs .src-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    curSource = btn.dataset.src; firstRecordMs = null;
    // Force-invalidate split cache so donut redraws with new (different) slices.
    _lastSplitKey = null; _lastProjKey = null; _lastModelKey = null;
    loadAggregate();
  });
});

// Skip-gaps single-toggle — pressed = SKIP, released = FILL (default)
(function(){
  const btn = document.getElementById('toggle-gaps');
  const span = btn.querySelector('span');
  btn.addEventListener('click', () => {
    const pressed = !btn.classList.contains('active');
    btn.classList.toggle('active', pressed);
    btn.setAttribute('aria-pressed', String(pressed));
    span.textContent = pressed ? 'On' : 'Off';
    curGaps = pressed ? 'skip' : 'fill';
    loadAggregate();
  });
})();
// Build segmented datetime fields BEFORE wiring change listeners; the
// constructor swaps in the segment <input>s and installs the virtual
// `value` property on the wrapper.
new DateTimeField(document.getElementById('from'));
new DateTimeField(document.getElementById('to'));
new DateTimeField(document.getElementById('bucket-jump'));
document.getElementById('from').addEventListener('change', setRangeFromInputs);
document.getElementById('to').addEventListener('change', setRangeFromInputs);

const ddTz = new Dropdown(document.getElementById('dd-tz'), v => {
  curTz = v;
  document.getElementById('from').value = utcMsToInput(curStart, v);
  document.getElementById('to').value   = utcMsToInput(curEnd, v);
  loadAggregate();
});
const ddBucket = new Dropdown(document.getElementById('dd-bucket'), v => {
  curBucket = v; loadAggregate();
});
document.getElementById('refresh-btn').addEventListener('click', async (e)=>{
  const b = e.currentTarget; b.classList.add('spin');
  try {
    await fetch('/api/refresh',{method:'POST'});
    toast('Refresh requested');
    await loadAggregate();
  } catch (err) { toast('Refresh failed', 'err'); }
  finally { setTimeout(()=>b.classList.remove('spin'), 350); }
});


// Force chart redraw on viewport resize — uPlot needs explicit setSize().
let _resizeRaf = 0;
window.addEventListener('resize', () => {
  cancelAnimationFrame(_resizeRaf);
  _resizeRaf = requestAnimationFrame(() => {
    if (UPLOT_TS) {
      const wrap = document.getElementById('chart-ts-wrap');
      if (wrap) UPLOT_TS.setSize({ width: wrap.clientWidth, height: wrap.clientHeight });
    }
    if (CHART_SPLIT) CHART_SPLIT.resize();
  });
});

// Refresh visible pages every two seconds.
setInterval(async ()=>{
  if (!document.hidden) await loadAggregate(true);
}, 2000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)loadAggregate(true);});

// boot — restore previous session state (source, tz, bucket, gaps, view, range)
(async ()=>{
  const saved = loadState();
  if (saved) {
    curSource   = saved.source   || 'cc';
    curBucket   = saved.bucket   || 'auto';
    curGaps     = saved.gaps     || 'fill';
    curTz       = saved.tz       || 'ET';
    curCostView = saved.costView || 'role';
    curPreset   = saved.preset   || '5';

    // Reflect non-range state onto UI controls
    document.querySelectorAll('#source-tabs .src-tab').forEach(b =>
      b.classList.toggle('active', b.dataset.src === curSource));
    document.querySelectorAll('#seg-costview .seg-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.v === curCostView));
    const tg = document.getElementById('toggle-gaps');
    if (tg) {
      tg.classList.toggle('active', curGaps === 'skip');
      tg.setAttribute('aria-pressed', String(curGaps === 'skip'));
      tg.querySelector('span').textContent = curGaps === 'skip' ? 'On' : 'Off';
    }
    if (typeof ddTz     !== 'undefined' && ddTz)     ddTz.set(curTz);
    if (typeof ddBucket !== 'undefined' && ddBucket) ddBucket.set(curBucket);

    // Range: rolling presets re-anchor to "now"; only 'custom' uses literal saved start/end.
    if (curPreset === 'custom' && saved.start && saved.end) {
      curStart = saved.start; curEnd = saved.end;
      document.getElementById('from').value = utcMsToInput(curStart, curTz);
      document.getElementById('to').value   = utcMsToInput(curEnd,   curTz);
      document.querySelectorAll('.preset').forEach(b => b.classList.remove('active'));
      loadAggregate();
    } else {
      const target = (curPreset === 'all') ? 'all' : Number(curPreset);
      document.querySelectorAll('.preset').forEach(b =>
        b.classList.toggle('active', b.dataset.h === curPreset));
      setRangePreset(target);
    }
  } else {
    document.querySelector('.preset[data-h="5"]').classList.add('active');
    setRangePreset(5);
  }
})();
