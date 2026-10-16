// Foley browser UI. Vanilla ES module, no dependencies, works offline.
// Talks only to the local `foley browse` server (see the HTTP API in the browse contract).

// ------------------------------------------------------------------ helpers

const $ = (id) => document.getElementById(id);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const SVG_NS = 'http://www.w3.org/2000/svg';
function icon(d, cls) {
  const s = document.createElementNS(SVG_NS, 'svg');
  s.setAttribute('viewBox', '0 0 24 24');
  s.setAttribute('aria-hidden', 'true');
  if (cls) s.setAttribute('class', cls);
  const p = document.createElementNS(SVG_NS, 'path');
  p.setAttribute('d', d);
  s.append(p);
  return s;
}
const STAR_D = 'm12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z';
const CHEVRON_D = 'm9 6 6 6-6 6';
const BARS_D = 'M5 18V10M10 18V5M15 18v-9M20 18v-5';

const MINUS = '−';
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const signed = (v, d = 1) => (v < 0 ? MINUS : v > 0 ? '+' : '') + Math.abs(v).toFixed(d);
const neg = (v, d = 1) => (v < 0 ? MINUS : '') + Math.abs(v).toFixed(d);

function fmtDur(s) {
  if (!isNum(s)) return '—';
  if (s < 10) return s.toFixed(2) + ' s';
  if (s < 60) return s.toFixed(1) + ' s';
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  if (m < 60) return `${m}:${String(sec).padStart(2, '0')}`;
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
}
function fmtClock(s) {
  if (!isNum(s) || s < 0) s = 0;
  const m = Math.floor(s / 60);
  const rest = s - m * 60;
  return `${m}:${rest.toFixed(2).padStart(5, '0')}`;
}
function fmtSec(s) {
  if (!isNum(s)) return null;
  if (s < 1) return [(s * 1000).toFixed(s < 0.01 ? 1 : 0), 'ms'];
  return [s.toFixed(s < 10 ? 2 : 1), 's'];
}
function fmtHz(v) {
  if (!isNum(v)) return null;
  return v >= 1000 ? [(v / 1000).toFixed(v >= 10000 ? 1 : 2), 'kHz'] : [v.toFixed(0), 'Hz'];
}
function fmtBytes(n) {
  if (!isNum(n)) return null;
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return [n.toFixed(i === 0 ? 0 : n < 10 ? 2 : 1), u[i]];
}
function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
const fmtInt = (n) => (isNum(n) ? n.toLocaleString('en-US') : '—');
const basename = (p) => (p || '').split('/').pop();
const dirname = (p) => { const i = (p || '').lastIndexOf('/'); return i < 0 ? '' : p.slice(0, i); };
const humanKey = (k) => String(k).replace(/_/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/^./, (c) => c.toUpperCase());

function debounce(fn, ms) {
  let t = 0;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  d.flush = (...a) => { clearTimeout(t); fn(...a); };
  d.cancel = () => clearTimeout(t);
  return d;
}

class HttpError extends Error {
  constructor(msg, status) { super(msg); this.status = status; }
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { Accept: 'application/json', ...(opts.body ? { 'Content-Type': 'application/json' } : {}), ...(opts.headers || {}) },
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* not JSON */ }
  if (!res.ok) throw new HttpError((data && data.error) || `${res.status} ${res.statusText || 'error'}`, res.status);
  return data;
}
const isAbort = (e) => e && e.name === 'AbortError';
const withQuery = (url, q) => url + (url.includes('?') ? '&' : '?') + q;

// ------------------------------------------------------------------ prefs (localStorage, never trusted)

const PREF_KEY = 'foley.browse.prefs.v1';
const prefs = {
  autoplay: true, loop: false, match: true, volume: 80, folder: '', view: 'folder', fit: false,
  treeW: null, detailW: null,
};
try {
  const saved = JSON.parse(localStorage.getItem(PREF_KEY) || 'null');
  if (saved && typeof saved === 'object') {
    for (const k of Object.keys(prefs)) if (k in saved && typeof saved[k] === typeof prefs[k] || (k in saved && prefs[k] === null)) prefs[k] = saved[k];
  }
} catch { /* storage unavailable */ }
prefs.volume = clamp(Number(prefs.volume) || 0, 0, 100);
const savePrefs = debounce(() => {
  try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch { /* ignore */ }
}, 250);

// ------------------------------------------------------------------ toasts / copy

function toast(msg, kind) {
  const t = h('div', { class: 'toast' + (kind ? ' ' + kind : ''), role: kind === 'error' ? 'alert' : 'status', text: msg });
  $('toasts').append(t);
  setTimeout(() => t.remove(), kind === 'error' ? 4500 : 2200);
}

async function copyText(text, label = 'Copied') {
  try {
    await navigator.clipboard.writeText(text);
    toast(label);
  } catch {
    const ta = h('textarea', { style: 'position:fixed;opacity:0' });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { /* ignore */ }
    ta.remove();
    toast(ok ? label : 'Copy failed', ok ? '' : 'error');
  }
}

// ------------------------------------------------------------------ global state

const state = {
  status: null,
  view: prefs.view === 'shortlist' ? 'shortlist' : 'folder',
  folder: typeof prefs.folder === 'string' ? prefs.folder : '',
  q: '',
  sort: '',
  filters: { min_duration: '', max_duration: '', min_lufs: '', max_lufs: '', max_attack: '', is_music: '', ext: '', starred: false },
  shortlistNotes: new Map(), // id -> note
};
const targetLufs = () => (state.status && isNum(state.status.loudness_target_lufs) ? state.status.loudness_target_lufs : -20);

// ------------------------------------------------------------------ status

async function loadStatus() {
  try {
    const s = await api('/api/status');
    state.status = s;
    $('banner').classList.add('hidden');
    renderStatus();
    return s;
  } catch (e) {
    showBanner(`Can't reach the Foley server (${e.message}). Is \`foley browse\` still running?`);
    $('status-line').textContent = 'Offline';
    return null;
  }
}
function showBanner(msg) {
  const b = $('banner');
  b.replaceChildren(h('span', { text: msg + ' ' }), h('button', { type: 'button', class: 'btn small', text: 'Retry', onclick: () => init() }));
  b.classList.remove('hidden');
}
function renderStatus() {
  const s = state.status;
  if (!s) return;
  const a = s.assets || {};
  const parts = [h('span', null, h('b', { text: fmtInt(a.active) }), ' sounds')];
  if (a.error) parts.push(' · ', h('span', { class: 'warn', text: `${fmtInt(a.error)} undecodable` }));
  if (a.missing) parts.push(' · ', h('span', { class: 'warn', text: `${fmtInt(a.missing)} missing` }));
  if (a.pending) parts.push(' · ', `${fmtInt(a.pending)} pending`);
  parts.push(' · target ', h('b', { text: `${neg(targetLufs())} LUFS` }));
  if (s.library_root) parts.push(' · ', h('span', { title: 'Library root', text: s.library_root }));
  if (s.note) parts.push(' · ', h('span', { class: 'warn', text: s.note }));
  $('status-line').replaceChildren(...parts);
  $('root-count').textContent = fmtInt(a.active);
  setShortlistCount(s.shortlist_count);
}
function setShortlistCount(n) {
  if (isNum(n)) $('shortlist-count').textContent = fmtInt(n);
}
async function refreshShortlist() {
  try {
    const r = await api('/api/shortlist');
    const items = (r && r.items) || [];
    state.shortlistNotes = new Map(items.map((it) => [it.id, it.note || '']));
    setShortlistCount(items.length);
    list.render();
  } catch { /* non-fatal */ }
}

// ------------------------------------------------------------------ folder tree

const tree = {
  nodes: new Map(), // path -> {li, toggle, row, ul, loaded, expanded, loading}
  root: $('tree'),

  async fetch(path) {
    return api('/api/tree?path=' + encodeURIComponent(path));
  },

  makeNode(f) {
    const toggle = h('button', { type: 'button', class: 'tree-toggle', 'aria-expanded': 'false', 'aria-label': `Expand ${f.name}` }, icon(CHEVRON_D));
    const row = h('button', { type: 'button', class: 'tree-row', title: f.path, dataset: { path: f.path } },
      h('span', { class: 'tree-label', text: f.name }), h('span', { class: 'tree-count', text: fmtInt(f.count) }));
    const ul = h('ul', { class: 'hidden', role: 'group' });
    const li = h('li', { class: 'tree-node' }, h('div', { class: 'tree-line' }, toggle, row), ul);
    const node = { li, toggle, row, ul, loaded: false, expanded: false, loading: null, path: f.path };
    toggle.addEventListener('click', () => this.toggle(node));
    row.addEventListener('click', () => { setFolder(f.path); this.expand(node); });
    this.nodes.set(f.path, node);
    return li;
  },

  fill(ul, folders, parentPath) {
    ul.replaceChildren(...folders.map((f) => this.makeNode(f)));
    if (!folders.length && parentPath === '') ul.append(h('li', { class: 'tree-loading', text: 'No folders' }));
  },

  async loadInto(node) {
    if (node.loaded) return;
    if (node.loading) return node.loading;
    node.ul.replaceChildren(h('li', { class: 'tree-loading', text: 'Loading…' }));
    node.ul.classList.remove('hidden');
    node.loading = (async () => {
      try {
        const r = await this.fetch(node.path);
        const folders = (r && r.folders) || [];
        this.fill(node.ul, folders, node.path);
        node.loaded = true;
        if (!folders.length) {
          node.toggle.classList.add('leaf');
          node.toggle.setAttribute('aria-hidden', 'true');
          node.toggle.tabIndex = -1;
        }
        this.markCurrent();
      } catch (e) {
        node.ul.replaceChildren(h('li', { class: 'tree-error' }, `Couldn't load: ${e.message}`,
          h('button', { type: 'button', class: 'btn small', text: 'Retry', onclick: () => { node.loading = null; this.loadInto(node); } })));
      } finally {
        node.loading = null;
      }
    })();
    return node.loading;
  },

  async expand(node) {
    node.expanded = true;
    node.toggle.setAttribute('aria-expanded', 'true');
    node.toggle.setAttribute('aria-label', `Collapse ${basename(node.path)}`);
    node.ul.classList.remove('hidden');
    await this.loadInto(node);
  },

  collapse(node) {
    node.expanded = false;
    node.toggle.setAttribute('aria-expanded', 'false');
    node.toggle.setAttribute('aria-label', `Expand ${basename(node.path)}`);
    node.ul.classList.add('hidden');
  },

  toggle(node) { node.expanded ? this.collapse(node) : this.expand(node); },

  async init() {
    this.nodes.clear();
    this.root.replaceChildren(h('li', { class: 'tree-loading', text: 'Loading folders…' }));
    try {
      const r = await this.fetch('');
      this.fill(this.root, (r && r.folders) || [], '');
      this.markCurrent();
    } catch (e) {
      this.root.replaceChildren(h('li', { class: 'tree-error' }, `Couldn't load folders: ${e.message}`,
        h('button', { type: 'button', class: 'btn small', text: 'Retry', onclick: () => this.init() })));
    }
  },

  async revealPath(path) {
    if (!path) return;
    const segs = path.split('/');
    for (let i = 1; i < segs.length; i++) {
      const node = this.nodes.get(segs.slice(0, i).join('/'));
      if (!node) return;
      await this.expand(node);
    }
    this.markCurrent();
    const cur = this.nodes.get(path);
    if (cur) cur.row.scrollIntoView({ block: 'nearest' });
  },

  markCurrent() {
    const cur = state.view === 'folder' ? state.folder : null;
    for (const [p, n] of this.nodes) n.row.setAttribute('aria-current', p === cur ? 'true' : 'false');
    $('root-btn').setAttribute('aria-current', state.view === 'folder' && state.folder === '' ? 'true' : 'false');
    $('shortlist-btn').setAttribute('aria-current', state.view === 'shortlist' ? 'true' : 'false');
  },
};

function setFolder(path) {
  state.view = 'folder';
  state.folder = path;
  prefs.folder = path;
  prefs.view = 'folder';
  savePrefs();
  tree.markCurrent();
  renderCrumbs();
  list.reload();
}
function setShortlistView() {
  state.view = 'shortlist';
  prefs.view = 'shortlist';
  savePrefs();
  tree.markCurrent();
  renderCrumbs();
  list.reload();
}

function crumbButtons(path, onPick, { currentLast = true } = {}) {
  const out = [h('button', { type: 'button', text: 'Library', onclick: () => onPick('') })];
  if (!path) {
    if (currentLast) out[0] = h('span', { class: 'crumb-current', text: 'Library' });
    return out;
  }
  const segs = path.split('/');
  segs.forEach((s, i) => {
    out.push(h('span', { class: 'crumb-sep', 'aria-hidden': 'true', text: '›' }));
    const p = segs.slice(0, i + 1).join('/');
    if (i === segs.length - 1 && currentLast) out.push(h('span', { class: 'crumb-current', 'aria-current': 'page', text: s }));
    else out.push(h('button', { type: 'button', text: s, title: p, onclick: () => onPick(p) }));
  });
  return out;
}
function renderCrumbs() {
  const c = $('crumbs');
  if (state.view === 'shortlist') {
    c.replaceChildren(h('button', { type: 'button', text: 'Library', onclick: () => setFolder('') }),
      h('span', { class: 'crumb-sep', 'aria-hidden': 'true', text: '›' }),
      h('span', { class: 'crumb-current', text: 'Shortlist' }));
  } else {
    c.replaceChildren(...crumbButtons(state.folder, (p) => { setFolder(p); tree.revealPath(p); }));
  }
}

// ------------------------------------------------------------------ filters / search / sort

const qInput = $('q');
const filterInputs = {
  min_duration: $('f-min-dur'), max_duration: $('f-max-dur'),
  min_lufs: $('f-min-lufs'), max_lufs: $('f-max-lufs'), max_attack: $('f-max-attack'),
};

function effectiveSort() {
  if (state.sort) return state.sort === 'relevance' && !state.q ? 'name' : state.sort;
  return state.q ? 'relevance' : 'name';
}

function listParams() {
  const p = new URLSearchParams();
  p.set('folder', state.view === 'shortlist' ? '' : state.folder);
  if (state.q) p.set('q', state.q);
  const f = state.filters;
  for (const k of ['min_duration', 'max_duration', 'min_lufs', 'max_lufs', 'max_attack']) {
    if (f[k] !== '' && Number.isFinite(Number(f[k]))) p.set(k, String(Number(f[k])));
  }
  if (f.is_music !== '') p.set('is_music', f.is_music);
  if (f.ext) p.set('ext', f.ext);
  if (f.starred || state.view === 'shortlist') p.set('starred', '1');
  p.set('sort', effectiveSort());
  return p;
}

function filtersActive() {
  const f = state.filters;
  return Object.keys(filterInputs).some((k) => f[k] !== '') || f.is_music !== '' || !!f.ext || f.starred;
}
function syncFilterUI() {
  for (const [k, el] of Object.entries(filterInputs)) el.classList.toggle('active', state.filters[k] !== '');
  $('f-ext').classList.toggle('active', !!state.filters.ext);
  $('f-reset').classList.toggle('hidden', !filtersActive());
  for (const b of $('f-kind').querySelectorAll('button')) b.setAttribute('aria-checked', b.dataset.v === state.filters.is_music ? 'true' : 'false');
  $('f-starred').setAttribute('aria-pressed', state.filters.starred ? 'true' : 'false');
  const eff = effectiveSort();
  for (const b of document.querySelectorAll('.sorter')) {
    const k = b.dataset.sort;
    b.classList.toggle('asc', eff === k);
    b.classList.toggle('desc', eff === '-' + k);
    b.title = `Sort by ${b.textContent.toLowerCase()}`;
  }
}

const reloadSoon = debounce(() => list.reload(), 350);
const searchSoon = debounce(() => {
  const v = qInput.value.trim();
  if (v === state.q) return;
  state.q = v;
  syncFilterUI();
  list.reload();
}, 220);

function wireFilters() {
  qInput.addEventListener('input', () => searchSoon());
  qInput.addEventListener('search', () => searchSoon.flush()); // clear (×) button
  for (const [k, el] of Object.entries(filterInputs)) {
    el.addEventListener('input', () => {
      const v = el.value.trim();
      state.filters[k] = v === '' || !Number.isFinite(Number(v)) ? '' : v;
      syncFilterUI();
      reloadSoon();
    });
  }
  $('f-kind').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    state.filters.is_music = b.dataset.v;
    syncFilterUI();
    list.reload();
  });
  $('f-kind').addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    const btns = [...$('f-kind').querySelectorAll('button')];
    const i = btns.indexOf(document.activeElement);
    if (i < 0) return;
    e.preventDefault();
    e.stopPropagation();
    const n = btns[(i + (e.key === 'ArrowRight' ? 1 : btns.length - 1)) % btns.length];
    n.focus();
    n.click();
  });
  $('f-ext').addEventListener('change', (e) => { state.filters.ext = e.target.value; syncFilterUI(); list.reload(); });
  $('f-starred').addEventListener('click', () => { state.filters.starred = !state.filters.starred; syncFilterUI(); list.reload(); });
  $('f-reset').addEventListener('click', () => {
    for (const [k, el] of Object.entries(filterInputs)) { el.value = ''; state.filters[k] = ''; }
    Object.assign(state.filters, { is_music: '', ext: '', starred: false });
    $('f-ext').value = '';
    syncFilterUI();
    list.reload();
  });
  $('sort').addEventListener('change', (e) => { state.sort = e.target.value; syncFilterUI(); list.reload(); });
  for (const b of document.querySelectorAll('.sorter')) {
    b.addEventListener('click', () => {
      const k = b.dataset.sort;
      state.sort = effectiveSort() === k ? '-' + k : k;
      $('sort').value = state.sort;
      syncFilterUI();
      list.reload();
    });
  }
}

// ------------------------------------------------------------------ results list (virtualized, paged)

const PAGE = 200;
const OVERSCAN = 10;

const list = {
  vp: $('list'),
  spacer: $('list-spacer'),
  rowsEl: $('list-rows'),
  stateEl: $('list-state'),
  rowH: 46,
  key: null,
  total: null,
  note: null,
  pages: new Map(), // page index -> array of rows
  inflight: new Map(), // page index -> promise
  ctrl: null,
  error: null,
  nodes: new Map(), // row index -> element
  sel: -1,
  pendingSel: null, // {index, play}
  rafPending: false,
  fetchTimer: 0,

  init() {
    const rh = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--row-h'));
    if (rh > 0) this.rowH = rh;
    this.showState('loading');
    this.vp.addEventListener('scroll', () => this.schedule(), { passive: true });
    new ResizeObserver(() => this.schedule()).observe(this.vp);
    this.rowsEl.addEventListener('click', (e) => {
      const row = e.target.closest('.row');
      if (!row) return;
      const i = Number(row.dataset.i);
      const item = this.item(i);
      if (e.target.closest('.star-btn')) {
        if (item) toggleStar(item.id);
        return;
      }
      if (item) this.select(i, { play: prefs.autoplay });
      this.vp.focus({ preventScroll: true });
    });
    this.rowsEl.addEventListener('dblclick', (e) => {
      const row = e.target.closest('.row');
      if (!row || e.target.closest('.star-btn')) return;
      const i = Number(row.dataset.i);
      if (this.item(i)) {
        if (this.sel !== i) this.select(i, { play: false });
        player.playFromStart();
      }
    });
  },

  item(i) {
    const page = this.pages.get(Math.floor(i / PAGE));
    return page ? page[i % PAGE] || null : null;
  },
  get selectedItem() { return this.sel >= 0 ? this.item(this.sel) : null; },

  reload() {
    const key = listParams().toString();
    if (this.ctrl) this.ctrl.abort();
    this.ctrl = new AbortController();
    this.key = key;
    this.inflight.clear();
    this.pendingSel = null;
    const hadError = !!this.error;
    this.error = null;
    this.vp.classList.add('stale');
    if (this.total === null || hadError) this.showState('loading');
    this.fetchPage(0, true);
  },

  async fetchPage(p, fresh = false) {
    if (!fresh && (this.pages.has(p) || this.inflight.has(p))) return;
    const key = this.key;
    const signal = this.ctrl.signal;
    const params = new URLSearchParams(key);
    params.set('offset', String(p * PAGE));
    params.set('limit', String(PAGE));
    const prom = api('/api/list?' + params.toString(), { signal });
    this.inflight.set(p, prom);
    try {
      const r = await prom;
      if (key !== this.key) return;
      if (fresh) {
        this.pages = new Map();
        this.clearNodes();
        this.vp.scrollTop = 0;
        this.sel = -1;
      }
      this.pages.set(p, r.results || []);
      this.total = isNum(r.total) ? r.total : (r.results || []).length;
      this.note = r.note || null;
      this.vp.classList.remove('stale');
      if (fresh) {
        const sid = detail.id;
        if (sid != null) {
          const idx = (r.results || []).findIndex((x) => x.id === sid);
          if (idx >= 0) this.sel = idx;
        }
      }
      this.spacer.style.height = this.total * this.rowH + 'px';
      this.updateState();
      this.render();
      this.renderFoot();
      if (this.pendingSel && Math.floor(this.pendingSel.index / PAGE) === p) {
        const ps = this.pendingSel;
        this.pendingSel = null;
        if (this.item(ps.index)) this.select(ps.index, ps);
      }
    } catch (e) {
      if (isAbort(e) || key !== this.key) return;
      if (fresh || this.total === null) {
        this.error = e;
        this.vp.classList.remove('stale');
        this.pages = new Map();
        this.total = 0;
        this.clearNodes();
        this.spacer.style.height = '0px';
        this.showState('error');
        this.renderFoot();
      } else {
        toast(`Couldn't load more results: ${e.message}`, 'error');
      }
    } finally {
      if (this.inflight.get(p) === prom) this.inflight.delete(p);
    }
  },

  clearNodes() {
    for (const n of this.nodes.values()) n.remove();
    this.nodes.clear();
  },

  schedule() {
    if (this.rafPending) return;
    this.rafPending = true;
    requestAnimationFrame(() => { this.rafPending = false; this.render(); });
  },

  render() {
    const total = this.total || 0;
    const top = this.vp.scrollTop;
    const vh = this.vp.clientHeight || 600;
    const first = Math.max(0, Math.floor(top / this.rowH) - OVERSCAN);
    const last = Math.min(total - 1, Math.ceil((top + vh) / this.rowH) + OVERSCAN);
    if (total > 0 && this.key) {
      // Fetch pages for what is on screen once scrolling pauses briefly, so a fast flick
      // through 40k rows doesn't queue a request for every page it passes.
      clearTimeout(this.fetchTimer);
      this.fetchTimer = setTimeout(() => {
        const t2 = this.vp.scrollTop;
        const f2 = Math.max(0, Math.floor(t2 / this.rowH) - OVERSCAN);
        const l2 = Math.min((this.total || 0) - 1, Math.ceil((t2 + vh) / this.rowH) + OVERSCAN);
        if (l2 < 0) return;
        for (let p = Math.floor(f2 / PAGE); p <= Math.floor(l2 / PAGE); p++) this.fetchPage(p);
        // Prefetch the next page as the viewport nears the end of what is loaded.
        const nextP = Math.floor((l2 + PAGE / 2) / PAGE);
        if (nextP * PAGE < this.total) this.fetchPage(nextP);
      }, this.pages.has(Math.floor(first / PAGE)) ? 0 : 70);
    }
    for (const [i, n] of this.nodes) {
      if (i < first || i > last) { n.remove(); this.nodes.delete(i); }
    }
    const playingId = player.isPlaying() ? player.id : null;
    for (let i = first; i <= last; i++) {
      const it = this.item(i);
      let n = this.nodes.get(i);
      const sig = it ? `${it.id}|${it.starred ? 1 : 0}|${state.shortlistNotes.get(it.id) || ''}` : 'skeleton';
      if (!n) {
        n = h('div', { class: 'row', role: 'option', id: 'row-' + i, dataset: { i: String(i) } });
        n.style.transform = `translateY(${i * this.rowH}px)`;
        this.nodes.set(i, n);
        this.rowsEl.append(n);
      }
      if (n._sig !== sig) { this.fill(n, it); n._sig = sig; }
      const selected = i === this.sel;
      n.classList.toggle('selected', selected);
      n.setAttribute('aria-selected', selected ? 'true' : 'false');
      n.classList.toggle('playing', !!it && it.id === playingId);
    }
    if (this.sel >= 0 && this.nodes.has(this.sel)) this.vp.setAttribute('aria-activedescendant', 'row-' + this.sel);
    else this.vp.removeAttribute('aria-activedescendant');
  },

  fill(n, it) {
    n.classList.toggle('skeleton', !it);
    if (!it) {
      n.replaceChildren(h('span'), h('div', { class: 'row-main' }, h('span', { class: 'bar', style: `width:${40 + ((Number(n.dataset.i) * 37) % 45)}%` }), h('span', { class: 'bar short' })), h('span'), h('span'), h('span'));
      n.setAttribute('aria-label', 'Loading');
      n.title = '';
      return;
    }
    n.removeAttribute('aria-label');
    const note = state.shortlistNotes.get(it.id);
    const tags = Array.isArray(it.tags) ? it.tags.slice(0, 3).map((t) => (typeof t === 'string' ? t : t && t.label)).filter(Boolean) : [];
    const sub = h('div', { class: 'row-sub' });
    if (note) sub.append(h('span', { class: 'note', text: '“' + note + '”  ' }));
    for (const t of tags) sub.append(h('span', { class: 'rtag', text: t }));
    sub.append(it.caption || it.folder || '');
    const nameLine = h('div', { class: 'row-name' }, icon(BARS_D, 'playing-ind'), h('span', { class: 'nm', text: it.name || basename(it.path) }));
    // Packs often ship one sound at several rates (16/44 and 24/96), so same-named rows need this to tell apart.
    const fmt = [isNum(it.sample_rate) ? `${+(it.sample_rate / 1000).toFixed(1)}k` : '', it.channels === 1 ? 'mono' : it.channels === 2 ? 'stereo' : isNum(it.channels) ? `${it.channels}ch` : ''].filter(Boolean).join(' · ');
    if (fmt) nameLine.append(h('span', { class: 'badge fmt', title: 'Sample rate and channels', text: fmt }));
    if (it.duplicates > 0) nameLine.append(h('span', { class: 'badge', title: `${it.duplicates} other identical cop${it.duplicates === 1 ? 'y' : 'ies'} in the library`, text: `×${it.duplicates + 1}` }));
    const star = h('button', { type: 'button', class: 'star-btn', tabindex: '-1', 'aria-pressed': it.starred ? 'true' : 'false', 'aria-label': `${it.starred ? 'Unstar' : 'Star'} ${it.name}`, title: it.starred ? 'Unstar (S)' : 'Star (S)' }, icon(STAR_D));
    const lufs = isNum(it.lufs) ? neg(it.lufs) : '—';
    n.title = it.path;
    n.replaceChildren(
      star,
      h('div', { class: 'row-main' }, nameLine, sub),
      h('div', { class: 'cell-num', text: fmtDur(it.duration_s) }),
      h('div', { class: 'cell-num' + (isNum(it.lufs) ? '' : ' dim'), title: isNum(it.lufs) ? 'Integrated loudness' : 'Too short for integrated loudness', text: lufs }),
      h('div', { class: 'cell-num' + (isNum(it.true_peak_db) ? '' : ' dim'), title: 'True peak, dBTP', text: isNum(it.true_peak_db) ? neg(it.true_peak_db) : '—' }),
    );
  },

  showState(kind) {
    const s = this.stateEl;
    s.className = 'list-state ' + kind;
    if (kind === 'loading') {
      s.replaceChildren(h('div', { class: 'spinner', role: 'progressbar', 'aria-label': 'Loading' }), h('p', { text: 'Loading sounds…' }));
    } else if (kind === 'error') {
      s.replaceChildren(h('p', { class: 'state-title', text: "Couldn't load results" }), h('p', { text: this.error ? this.error.message : '' }),
        h('button', { type: 'button', class: 'btn', text: 'Retry', onclick: () => this.reload() }));
    } else if (kind === 'empty') {
      const kids = [];
      if (state.view === 'shortlist' && !state.q && !filtersActive()) {
        kids.push(h('p', { class: 'state-title', text: 'Your shortlist is empty' }), h('p', { text: 'Press S or click the star on any sound to keep it here.' }));
      } else {
        kids.push(h('p', { class: 'state-title', text: 'No sounds match' }));
        const where = state.view === 'shortlist' ? 'your shortlist' : state.folder ? `“${basename(state.folder)}”` : 'the library';
        kids.push(h('p', { text: state.q ? `Nothing for “${state.q}” in ${where}${filtersActive() ? ' with these filters' : ''}.` : `Nothing in ${where}${filtersActive() ? ' with these filters' : ''}.` }));
        const btns = h('div', { style: 'display:flex;gap:8px' });
        if (state.view === 'folder' && state.folder && state.q) btns.append(h('button', { type: 'button', class: 'btn', text: 'Search the whole library', onclick: () => { setFolder(''); } }));
        if (filtersActive()) btns.append(h('button', { type: 'button', class: 'btn', text: 'Clear filters', onclick: () => $('f-reset').click() }));
        if (btns.childElementCount) kids.push(btns);
      }
      s.replaceChildren(...kids);
    }
    s.classList.remove('hidden');
  },
  updateState() {
    if (this.error) this.showState('error');
    else if (this.total === 0) this.showState('empty');
    else this.stateEl.classList.add('hidden');
  },

  renderFoot() {
    const f = $('results-foot');
    const kids = [];
    if (this.error) kids.push(h('span', { text: 'Error' }));
    else if (this.total != null) {
      kids.push(h('span', null, h('b', { text: fmtInt(this.total) }), this.total === 1 ? ' sound' : ' sounds'));
      if (this.sel >= 0) kids.push(h('span', { text: `#${fmtInt(this.sel + 1)}` }));
    }
    if (this.note) kids.push(h('span', { class: 'note', title: this.note, text: this.note }));
    f.replaceChildren(...kids);
  },

  ensureVisible(i) {
    const top = i * this.rowH;
    const vh = this.vp.clientHeight;
    if (top < this.vp.scrollTop) this.vp.scrollTop = top;
    else if (top + this.rowH > this.vp.scrollTop + vh) this.vp.scrollTop = top + this.rowH - vh;
  },

  select(i, opts = {}) {
    if (!this.total) return;
    i = clamp(i, 0, this.total - 1);
    const it = this.item(i);
    this.ensureVisible(i);
    if (!it) {
      this.pendingSel = { index: i, ...opts };
      this.fetchPage(Math.floor(i / PAGE));
      this.sel = i;
      this.render();
      return;
    }
    this.pendingSel = null;
    this.sel = i;
    this.render();
    this.renderFoot();
    openAsset(it.id, it, opts);
  },

  move(delta, opts = {}) {
    if (!this.total) return;
    let i;
    if (this.sel < 0) i = delta > 0 ? 0 : this.total - 1;
    else i = this.sel + delta;
    i = clamp(i, 0, this.total - 1);
    if (i === this.sel && this.item(i) && detail.id === this.item(i).id) return;
    this.select(i, { play: prefs.autoplay, ...opts });
  },

  pageRows() { return Math.max(1, Math.floor(this.vp.clientHeight / this.rowH) - 1); },

  updateItem(id, patch) {
    for (const page of this.pages.values()) for (const it of page) if (it.id === id) Object.assign(it, patch);
    this.render();
  },
};

// ------------------------------------------------------------------ player (audio + Web Audio gain)

const audio = $('audio');
const player = {
  id: null,
  baseUrl: null,
  transcoded: false,
  failed: false,
  wantPlay: false,
  pendingSeek: null,
  info: { lufs: null, peak: null, duration: null, name: '' },
  ctx: null,
  matchNode: null,
  masterNode: null,
  gainDb: null,

  isPlaying() { return !audio.paused && !audio.ended; },

  ensureContext() {
    if (this.ctx === false) return;
    if (!this.ctx) {
      try {
        const AC = window.AudioContext || window.webkitAudioContext;
        const ctx = new AC();
        const src = ctx.createMediaElementSource(audio);
        this.matchNode = ctx.createGain();
        this.masterNode = ctx.createGain();
        src.connect(this.matchNode).connect(this.masterNode).connect(ctx.destination);
        this.ctx = ctx;
        audio.volume = 1;
        this.applyGain(true);
      } catch {
        this.ctx = false; // fall back to element volume (no boost possible)
        this.applyGain(true);
        return;
      }
    }
    if (this.ctx.state === 'suspended') this.ctx.resume().catch(() => {});
  },

  load(id, info, { play = false } = {}) {
    this.wantPlay = play;
    this.id = id;
    this.transcoded = false;
    this.failed = false;
    this.pendingSeek = null;
    this.info = { ...this.info, ...info };
    this.baseUrl = `/api/audio/${id}`;
    audio.loop = prefs.loop;
    audio.preload = play ? 'auto' : 'metadata';
    audio.src = this.baseUrl;
    this.applyGain(true);
    renderNow();
    wave.redraw();
    if (play) this.play();
  },

  unload() {
    this.id = null;
    this.wantPlay = false;
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    renderNow();
  },

  setInfo(info) {
    this.info = { ...this.info, ...info };
    this.applyGain(false);
    renderNow();
  },

  play() {
    if (this.id == null) return;
    if (this.failed) { toast("This file can't be played", 'error'); return; }
    this.ensureContext();
    this.wantPlay = true;
    const p = audio.play();
    if (p && p.catch) {
      p.catch((e) => {
        if (e.name === 'NotAllowedError') toast('Click anywhere or press Space to enable audio');
      });
    }
  },
  pause() { this.wantPlay = false; audio.pause(); },
  toggle() {
    if (this.id == null) {
      if (list.total) list.select(Math.max(0, list.sel), { play: true });
      return;
    }
    this.isPlaying() ? this.pause() : this.play();
  },
  stop() {
    this.wantPlay = false;
    audio.pause();
    this.seek(0);
  },
  playFromStart() {
    if (this.id == null) { this.toggle(); return; }
    this.seek(0);
    this.play();
  },

  duration() {
    if (isNum(wave.peaks?.duration_s) && wave.peaksId === this.id) return wave.peaks.duration_s;
    if (isNum(this.info.duration) && this.info.duration > 0) return this.info.duration;
    return isNum(audio.duration) ? audio.duration : 0;
  },
  time() { return this.pendingSeek != null ? this.pendingSeek : audio.currentTime || 0; },

  seek(t) {
    const d = this.duration();
    t = clamp(t, 0, d > 0 ? d : Infinity);
    if (audio.readyState >= 1) {
      audio.currentTime = t;
      this.pendingSeek = null;
    } else {
      this.pendingSeek = t;
    }
    frames.kick();
  },
  nudge(dt) { if (this.id != null) this.seek(this.time() + dt); },

  computeGain() {
    const { lufs, peak } = this.info;
    if (isNum(lufs)) return clamp(targetLufs() - lufs, -24, 12);
    if (isNum(peak)) return clamp(-6 - peak, -24, 12);
    return null;
  },

  applyGain(immediate) {
    const g = prefs.match ? this.computeGain() : null;
    this.gainDb = g;
    const lin = g == null ? 1 : Math.pow(10, g / 20);
    const master = Math.pow(prefs.volume / 100, 2);
    if (this.ctx) {
      const t = this.ctx.currentTime;
      for (const [node, v] of [[this.matchNode, lin], [this.masterNode, master]]) {
        node.gain.cancelScheduledValues(t);
        if (immediate) node.gain.setValueAtTime(v, t);
        else node.gain.setTargetAtTime(v, t, 0.02);
      }
    } else {
      audio.volume = clamp(lin * master, 0, 1);
    }
    renderGain();
  },
};

function renderGain() {
  const el = $('t-gain');
  const g = player.gainDb;
  el.classList.remove('off', 'hot');
  if (!prefs.match) {
    el.textContent = 'off';
    el.classList.add('off');
    el.title = 'Loudness match is off';
    return;
  }
  if (player.id == null) { el.textContent = '—'; el.classList.add('off'); el.title = 'Gain applied by loudness match'; return; }
  if (g == null) {
    el.textContent = '0.0 dB';
    el.classList.add('off');
    el.title = 'No loudness or peak data for this sound; played at unity gain';
    return;
  }
  const { lufs, peak } = player.info;
  el.textContent = `${signed(g)} dB`;
  const basis = isNum(lufs) ? `${neg(lufs)} LUFS → ${neg(targetLufs())} LUFS target` : `true peak ${neg(peak)} dBTP → −6 dBTP (too short for LUFS)`;
  let title = `Loudness match: ${signed(g)} dB (${basis})`;
  if (!player.ctx && g > 0) title += '. Boost needs Web Audio; press Space or click once to enable it.';
  if (isNum(peak) && peak + g > 0) {
    el.classList.add('hot');
    title += `. Peaks reach ${signed(peak + g)} dBTP after gain and may clip.`;
  }
  el.title = title;
}

function renderNow() {
  const el = $('t-now');
  if (player.id == null) { el.replaceChildren(h('span', { class: 'muted', text: 'Nothing loaded' })); el.title = ''; return; }
  const name = player.info.name || '';
  if (player.failed) {
    el.replaceChildren(h('span', { class: 'err', text: `Can't play ${name}` }));
  } else {
    el.replaceChildren(h('span', { text: name }), player.transcoded ? h('span', { class: 'muted', text: '  (transcoded)' }) : '');
  }
  el.title = player.info.path || name;
  $('t-dur').textContent = fmtClock(player.duration());
}

function wireAudio() {
  audio.addEventListener('play', () => { document.body.classList.add('is-playing'); $('t-play').setAttribute('aria-label', 'Pause (Space)'); frames.kick(); list.render(); });
  audio.addEventListener('pause', () => { document.body.classList.remove('is-playing'); $('t-play').setAttribute('aria-label', 'Play (Space)'); frames.kick(); list.render(); });
  audio.addEventListener('ended', () => {
    if (audio.loop) return;
    player.wantPlay = false;
    document.body.classList.remove('is-playing');
    audio.currentTime = 0;
    frames.kick();
    list.render();
  });
  audio.addEventListener('timeupdate', () => frames.kick());
  audio.addEventListener('seeked', () => frames.kick());
  audio.addEventListener('loadedmetadata', () => {
    if (player.pendingSeek != null) {
      const t = player.pendingSeek;
      player.pendingSeek = null;
      try { audio.currentTime = t; } catch { /* ignore */ }
    }
    renderNow();
    frames.kick();
  });
  audio.addEventListener('error', () => {
    if (player.id == null || !audio.getAttribute('src')) return;
    if (!player.transcoded) {
      // The browser can't decode this file natively: ask the server for 16-bit PCM WAV.
      player.transcoded = true;
      const t = player.time();
      audio.src = withQuery(player.baseUrl, 'transcode=1');
      if (t > 0) player.pendingSeek = t;
      renderNow();
      if (player.wantPlay) player.play();
      return;
    }
    player.failed = true;
    player.wantPlay = false;
    renderNow();
    detail.setAudioError();
  });
}

// ------------------------------------------------------------------ waveform canvas

const wave = {
  wrap: $('wave-wrap'),
  canvas: $('wave'),
  msg: $('wave-msg'),
  hoverEl: $('wave-hover'),
  peaks: null,
  peaksId: null,
  base: null,
  played: null,
  W: 0, H: 0, dpr: 1,
  hoverX: null,
  dragging: false,
  wasPlaying: false,

  init() {
    new ResizeObserver(() => this.rebuild()).observe(this.wrap);
    this.wrap.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 || e.target.closest('.wave-fit') || player.id == null) return;
      this.dragging = true;
      this.wasPlaying = player.isPlaying() || player.wantPlay;
      this.wrap.setPointerCapture(e.pointerId);
      this.seekAt(e);
    });
    this.wrap.addEventListener('pointermove', (e) => {
      this.hoverX = e.clientX - this.wrap.getBoundingClientRect().left;
      if (this.dragging) this.seekAt(e);
      this.showHover();
      frames.kick();
    });
    const end = (e) => {
      if (!this.dragging) return;
      this.dragging = false;
      try { this.wrap.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
      if (!player.isPlaying()) player.play();
    };
    this.wrap.addEventListener('pointerup', end);
    this.wrap.addEventListener('pointercancel', end);
    this.wrap.addEventListener('pointerleave', () => { this.hoverX = null; this.hoverEl.classList.add('hidden'); frames.kick(); });
    const fitBtn = $('wave-fit');
    fitBtn.setAttribute('aria-pressed', prefs.fit ? 'true' : 'false');
    fitBtn.addEventListener('click', () => {
      prefs.fit = !prefs.fit;
      savePrefs();
      fitBtn.setAttribute('aria-pressed', prefs.fit ? 'true' : 'false');
      this.rebuild();
    });
  },

  seekAt(e) {
    const r = this.wrap.getBoundingClientRect();
    const f = clamp((e.clientX - r.left) / r.width, 0, 1);
    player.seek(f * player.duration());
  },

  showHover() {
    const d = player.duration();
    if (this.hoverX == null || !(d > 0)) { this.hoverEl.classList.add('hidden'); return; }
    const w = this.wrap.clientWidth;
    const t = clamp(this.hoverX / w, 0, 1) * d;
    this.hoverEl.textContent = fmtClock(t);
    this.hoverEl.style.left = clamp(this.hoverX, 28, w - 28) + 'px';
    this.hoverEl.classList.remove('hidden');
  },

  setMessage(text, kind) {
    this.msg.className = 'wave-msg' + (kind ? ' ' + kind : '');
    this.msg.replaceChildren();
    if (!text) return;
    if (kind === 'loading') this.msg.append(h('div', { class: 'spinner' }));
    this.msg.append(h('span', { text }));
  },

  clear() {
    this.peaks = null;
    this.peaksId = null;
    this.setMessage('');
    this.rebuild();
  },

  setPeaks(id, p) {
    this.peaks = p;
    this.peaksId = id;
    this.setMessage('');
    this.rebuild();
  },

  rebuild() {
    const r = this.wrap.getBoundingClientRect();
    this.dpr = Math.max(1, window.devicePixelRatio || 1);
    this.W = Math.max(1, Math.round(r.width * this.dpr));
    this.H = Math.max(1, Math.round(r.height * this.dpr));
    this.canvas.width = this.W;
    this.canvas.height = this.H;
    const css = getComputedStyle(document.documentElement);
    const col = (n, fb) => css.getPropertyValue(n).trim() || fb;
    this.colors = {
      wave: col('--wave', '#6d7886'), played: col('--wave-played', '#4fc1b0'),
      grid: 'rgba(201,209,217,0.10)', label: 'rgba(170,178,189,0.55)', head: '#ffffff', hover: 'rgba(255,255,255,0.35)',
      tint: 'rgba(79,193,176,0.07)',
    };
    this.base = this.offscreen(true);
    this.played = this.offscreen(false);
    this.redraw();
  },

  lanes() {
    const p = this.peaks;
    const n = p && Array.isArray(p.max) ? Math.min(2, p.max.length) : 1;
    const gap = n > 1 ? Math.round(4 * this.dpr) : 0;
    const pad = Math.round(4 * this.dpr);
    const laneH = (this.H - pad * 2 - gap * (n - 1)) / n;
    return Array.from({ length: n }, (_, i) => ({ top: pad + i * (laneH + gap), h: laneH }));
  },

  offscreen(withGrid) {
    const c = document.createElement('canvas');
    c.width = this.W;
    c.height = this.H;
    const g = c.getContext('2d');
    const lanes = this.lanes();
    const p = this.peaks && this.peaksId === player.id ? this.peaks : null;
    if (withGrid) {
      g.fillStyle = this.colors.grid;
      for (const L of lanes) {
        const mid = Math.round(L.top + L.h / 2);
        g.fillRect(0, mid, this.W, Math.max(1, Math.round(this.dpr * 0.75)));
        // -6 dBFS guides (amplitude 0.5) on the fixed scale, as in the overview image.
        if (!prefs.fit) {
          g.globalAlpha = 0.5;
          for (const s of [-1, 1]) {
            const y = Math.round(mid - s * 0.5 * (L.h / 2) * 0.94);
            for (let x = 0; x < this.W; x += 6 * this.dpr) g.fillRect(x, y, 3 * this.dpr, Math.max(1, Math.round(this.dpr * 0.75)));
          }
          g.globalAlpha = 1;
        }
      }
      if (lanes.length > 1) {
        g.fillStyle = this.colors.label;
        g.font = `${Math.round(10 * this.dpr)}px ${getComputedStyle(document.body).fontFamily}`;
        g.textBaseline = 'top';
        ['L', 'R'].forEach((t, i) => g.fillText(t, Math.round(5 * this.dpr), Math.round(lanes[i].top + 2 * this.dpr)));
      }
    }
    if (!p) return c;
    const n = p.n || (p.max[0] || []).length;
    if (!n) return c;
    const peak = isNum(p.peak) && p.peak > 0 ? p.peak : 1;
    const scale = prefs.fit ? 1 / Math.max(peak, 0.0005) : 1;
    g.fillStyle = withGrid ? this.colors.wave : this.colors.played;
    const minW = Math.max(1, Math.round(this.dpr));
    lanes.forEach((L, ch) => {
      const mx = p.max[ch] || p.max[0];
      const mn = (p.min && (p.min[ch] || p.min[0])) || mx.map((v) => -v);
      const mid = L.top + L.h / 2;
      const amp = (L.h / 2) * 0.94;
      for (let x = 0; x < this.W; x++) {
        const i0 = Math.floor((x * n) / this.W);
        const i1 = Math.max(i0 + 1, Math.floor(((x + 1) * n) / this.W));
        let hi = -Infinity;
        let lo = Infinity;
        for (let i = i0; i < i1 && i < n; i++) {
          if (mx[i] > hi) hi = mx[i];
          if (mn[i] < lo) lo = mn[i];
        }
        if (hi === -Infinity) continue;
        hi = clamp(hi * scale, -1, 1);
        lo = clamp(lo * scale, -1, 1);
        const y1 = mid - hi * amp;
        const y2 = mid - lo * amp;
        g.fillRect(x, y1, 1, Math.max(minW, y2 - y1));
      }
    });
    return c;
  },

  redraw() {
    const g = this.canvas.getContext('2d');
    g.clearRect(0, 0, this.W, this.H);
    if (!this.base) return;
    g.drawImage(this.base, 0, 0);
    const d = player.duration();
    if (player.id == null || !(d > 0)) return;
    const t = player.time();
    const px = clamp(t / d, 0, 1) * this.W;
    if (px > 0) {
      g.fillStyle = this.colors.tint;
      g.fillRect(0, 0, px, this.H);
      g.save();
      g.beginPath();
      g.rect(0, 0, px, this.H);
      g.clip();
      g.drawImage(this.played, 0, 0);
      g.restore();
    }
    if (this.hoverX != null) {
      g.fillStyle = this.colors.hover;
      g.fillRect(Math.round(this.hoverX * this.dpr), 0, Math.max(1, Math.round(this.dpr)), this.H);
    }
    g.fillStyle = this.colors.head;
    const lw = Math.max(1.5, 1.5 * this.dpr);
    g.fillRect(Math.round(px - lw / 2), 0, lw, this.H);
  },
};

// Animation loop for playhead, overview overlay and time readout.
const frames = {
  raf: 0,
  kick() { if (!this.raf) this.raf = requestAnimationFrame(() => this.tick()); },
  tick() {
    this.raf = 0;
    wave.redraw();
    overview.drawPlayhead();
    $('t-cur').textContent = fmtClock(player.time());
    if (wave.hoverX != null) wave.showHover();
    if (player.isPlaying()) this.kick();
  },
};

// ------------------------------------------------------------------ overview PNG

const DEFAULT_PLOT_X = [0.0605, 0.9395];
const overview = {
  box: $('overview'),
  img: $('ov-img'),
  head: $('ov-playhead'),
  msg: $('ov-msg'),
  url: null,
  plotX: DEFAULT_PLOT_X,
  ready: false,
  ctrl: null,
  timer: 0,
  spinTimer: 0,
  dragging: false,

  init() {
    this.box.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 || !this.ready || player.id == null) return;
      this.dragging = true;
      this.box.setPointerCapture(e.pointerId);
      this.seekAt(e);
    });
    this.box.addEventListener('pointermove', (e) => { if (this.dragging) this.seekAt(e); });
    const end = (e) => {
      if (!this.dragging) return;
      this.dragging = false;
      try { this.box.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
      if (!player.isPlaying()) player.play();
    };
    this.box.addEventListener('pointerup', end);
    this.box.addEventListener('pointercancel', end);
    this.img.addEventListener('load', () => { this.img.classList.add('loaded'); this.ready = true; this.drawPlayhead(); });
  },

  seekAt(e) {
    const r = this.box.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width;
    const [x0, x1] = this.plotX;
    const f = clamp((x - x0) / (x1 - x0), 0, 1);
    player.seek(f * player.duration());
  },

  reset() {
    if (this.ctrl) this.ctrl.abort();
    clearTimeout(this.timer);
    clearTimeout(this.spinTimer);
    this.ready = false;
    this.img.classList.remove('loaded');
    this.img.removeAttribute('src');
    if (this.url) URL.revokeObjectURL(this.url);
    this.url = null;
    this.head.classList.add('hidden');
    this.setMsg('');
    this.box.style.cursor = 'default';
  },

  setMsg(text, kind) {
    this.msg.className = 'ov-msg' + (kind ? ' ' + kind : '');
    this.msg.replaceChildren();
    if (!text) return;
    if (kind === 'loading') this.msg.append(h('div', { class: 'spinner' }));
    this.msg.append(h('span', { text }));
  },

  // Rendering can take a second or more on first view, so wait for the selection to settle.
  load(id, url, delay) {
    this.reset();
    const ctrl = new AbortController();
    this.ctrl = ctrl;
    this.spinTimer = setTimeout(() => this.setMsg('Rendering overview…', 'loading'), Math.max(120, delay));
    this.timer = setTimeout(async () => {
      try {
        const res = await fetch(url, { signal: ctrl.signal });
        if (!res.ok) {
          let msg = `${res.status}`;
          try { const j = await res.json(); if (j && j.error) msg = j.error; } catch { /* not JSON */ }
          throw new HttpError(msg, res.status);
        }
        const blob = await res.blob();
        if (ctrl.signal.aborted) return;
        clearTimeout(this.spinTimer);
        this.url = URL.createObjectURL(blob);
        this.img.src = this.url;
        this.setMsg('');
        this.box.style.cursor = '';
      } catch (e) {
        if (isAbort(e)) return;
        clearTimeout(this.spinTimer);
        const what = e.status === 422 ? 'Foley could not decode this file to draw its overview.' : e.status === 404 ? 'No overview available for this file.' : "Couldn't load the overview.";
        this.setMsg(`${what} ${e.message && e.status !== 404 ? '(' + e.message + ')' : ''}`.trim(), 'error');
      }
    }, delay);
  },

  drawPlayhead() {
    const d = player.duration();
    if (!this.ready || player.id == null || player.id !== detail.id || !(d > 0)) { this.head.classList.add('hidden'); return; }
    const [x0, x1] = this.plotX;
    const f = clamp(player.time() / d, 0, 1);
    this.head.style.left = ((x0 + f * (x1 - x0)) * 100).toFixed(3) + '%';
    this.head.classList.remove('hidden');
  },
};

// ------------------------------------------------------------------ detail pane

const BAND_INFO = [
  ['sub', 'Sub', '<60'], ['low', 'Low', '60–250'], ['low_mid', 'Lo-mid', '250–500'], ['mid', 'Mid', '0.5–2k'],
  ['upper_mid', 'Hi-mid', '2–4k'], ['presence', 'Pres', '4–6k'], ['air', 'Air', '6k+'],
];

const detail = {
  id: null,
  data: null,
  ctrl: null,
  peaksCtrl: null,
  token: 0,
  loadTimer: 0,
  noteSaved: '',

  init() {
    $('d-star').addEventListener('click', () => { if (this.id != null) toggleStar(this.id); });
    $('d-copy').addEventListener('click', () => { if (this.data || this.row) copyText(fullPath((this.data || this.row).path), 'Path copied'); });
    const note = $('d-note');
    const saveNote = debounce(() => this.saveNote(), 900);
    note.addEventListener('input', () => { $('d-note-status').textContent = 'Unsaved'; $('d-note-status').className = 'note-status muted'; saveNote(); });
    note.addEventListener('blur', () => saveNote.flush());
    this.saveNoteDebounced = saveNote;
  },

  // Called on every selection. Shows what the row already knows instantly, then fills in.
  open(id, row, { play = false, repeat = false } = {}) {
    const changed = id !== this.id;
    if (this.saveNoteDebounced && changed) this.saveNoteDebounced.flush();
    this.id = id;
    this.row = row || null;
    const token = ++this.token;
    if (this.ctrl) this.ctrl.abort();
    if (this.peaksCtrl) this.peaksCtrl.abort();
    clearTimeout(this.loadTimer);
    $('detail-empty').classList.add('hidden');
    $('detail-body').classList.remove('hidden');
    if (changed) {
      this.data = null;
      this.renderSkeleton(row, id);
      wave.clear();
      overview.reset();
      player.load(id, {
        lufs: row ? row.lufs : null, peak: row ? row.true_peak_db : null,
        duration: row ? row.duration_s : null, name: row ? row.name : `#${id}`, path: row ? row.path : '',
      }, { play });
    } else if (play) {
      player.playFromStart();
    }
    if (!changed && this.data) return;
    document.title = `${row ? row.name : '#' + id} · Foley`;
    // Key-repeat scrolling: let the selection settle before hitting the server for details.
    const delay = repeat ? 140 : 0;
    this.loadTimer = setTimeout(() => this.fetchAll(id, token, repeat), delay);
  },

  fetchAll(id, token, repeat) {
    const ctrl = new AbortController();
    this.ctrl = ctrl;
    const peaksCtrl = new AbortController();
    this.peaksCtrl = peaksCtrl;
    const wmTimer = setTimeout(() => { if (wave.peaksId !== id) wave.setMessage('Loading waveform…', 'loading'); }, 180);
    api(`/api/peaks/${id}?n=1600`, { signal: peaksCtrl.signal })
      .then((p) => { if (token === this.token) { clearTimeout(wmTimer); wave.setPeaks(id, p); renderNow(); frames.kick(); } })
      .catch((e) => {
        if (isAbort(e) || token !== this.token) return;
        clearTimeout(wmTimer);
        wave.setMessage(e.status === 422 ? `Waveform unavailable: ${e.message}` : `Couldn't load waveform: ${e.message}`, 'error');
      });
    overview.load(id, `/api/image/${id}`, repeat ? 300 : 220);
    api(`/api/asset/${id}`, { signal: ctrl.signal })
      .then((d) => { if (token === this.token) this.render(d); })
      .catch((e) => {
        if (isAbort(e) || token !== this.token) return;
        this.alert(`Couldn't load details: ${e.message}`);
      });
  },

  alert(text, kind) {
    const a = $('d-alert');
    if (!text) { a.classList.add('hidden'); return; }
    a.className = 'd-alert' + (kind ? ' ' + kind : '');
    a.textContent = text;
  },

  setAudioError() {
    const cur = $('d-alert');
    const msg = "The browser couldn't play this file, even transcoded by the server.";
    if (cur.classList.contains('hidden') || !cur.textContent.includes(msg)) {
      this.alert(cur.classList.contains('hidden') ? msg : `${cur.textContent}\n${msg}`);
    }
  },

  renderSkeleton(row, id) {
    const path = row ? row.path : '';
    $('d-name').textContent = row ? row.name : `Sound #${id}`;
    $('d-crumbs').replaceChildren(...(path ? crumbButtons(dirname(path), (p) => { setFolder(p); tree.revealPath(p); }, { currentLast: false }) : []));
    $('d-path').replaceChildren(h('bdi', { text: path || '' }));
    $('d-path').title = path;
    this.alert('');
    this.renderStar(row ? !!row.starred : false);
    const cap = $('d-caption');
    cap.textContent = row && row.caption ? row.caption : 'Loading…';
    cap.classList.toggle('empty', !(row && row.caption));
    this.renderTags(row ? row.tags : []);
    $('d-note').value = state.shortlistNotes.get(id) || '';
    this.noteSaved = $('d-note').value;
    $('d-note-status').textContent = '';
    const pend = () => h('span', { class: 'empty-line', text: '…' });
    $('d-nums').replaceChildren(...this.numCells(null, row));
    $('d-bands').replaceChildren(pend());
    $('d-license').replaceChildren(pend());
    $('d-dupes').replaceChildren(pend());
    $('d-dupes-count').textContent = row && row.duplicates ? String(row.duplicates) : '';
    $('d-ledger').replaceChildren(pend());
    $('d-meta').replaceChildren(pend());
    $('d-record').replaceChildren();
  },

  render(d) {
    this.data = d;
    const f = d.features || {};
    const name = basename(d.path);
    if (d.image_plot_x && d.image_plot_x.length === 2 && d.image_plot_x.every(isNum)) overview.plotX = d.image_plot_x;
    $('d-name').textContent = name;
    document.title = `${name} · Foley`;
    $('d-crumbs').replaceChildren(...crumbButtons(dirname(d.path), (p) => { setFolder(p); tree.revealPath(p); }, { currentLast: false }));
    $('d-path').replaceChildren(h('bdi', { text: d.path }));
    $('d-path').title = fullPath(d.path);
    this.renderStar(!!d.starred);
    if (typeof d.star_note === 'string' || d.star_note === null) {
      if (document.activeElement !== $('d-note')) {
        $('d-note').value = d.star_note || '';
        this.noteSaved = $('d-note').value;
      }
      if (d.starred) state.shortlistNotes.set(d.id, d.star_note || '');
    }
    if (d.status === 'error') this.alert(`Foley couldn't analyse this file${d.error ? ': ' + d.error : '.'}`);
    else if (d.status === 'missing') this.alert(`This file is missing from the library${d.missing_since ? ' since ' + fmtDate(d.missing_since) : ''}.`, 'warn');
    else if (d.status === 'pending') this.alert('Not analysed yet; run `foley index` to fill in the numbers.', 'warn');
    else if (!player.failed) this.alert('');
    if (player.failed) this.setAudioError();

    const cap = $('d-caption');
    cap.textContent = d.caption || 'No caption yet.';
    cap.classList.toggle('empty', !d.caption);
    this.renderTags(d.tags);

    player.setInfo({
      lufs: isNum(f.lufs_integrated) ? f.lufs_integrated : null,
      peak: isNum(f.true_peak_db) ? f.true_peak_db : null,
      duration: isNum(f.duration_s) ? f.duration_s : player.info.duration,
      name, path: d.path,
    });

    $('d-nums').replaceChildren(...this.numCells(d, null));
    this.renderBands(f.band_energy);
    this.renderLicense(d.license);
    this.renderDupes(d.duplicates || []);
    this.renderLedger(d.decisions || [], d.usages || []);
    this.renderMeta(d.metadata);
    this.renderRecord(d);
  },

  renderStar(on) {
    const b = $('d-star');
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.setAttribute('aria-label', on ? 'Unstar (S)' : 'Star (S)');
    b.title = on ? 'Unstar (S)' : 'Star (S)';
  },

  renderTags(tags) {
    const box = $('d-tags');
    const list = (tags || []).map((t) => (typeof t === 'string' ? { label: t } : t)).filter((t) => t && t.label);
    if (!list.length) {
      const hasTags = state.status && state.status.has_tags;
      box.replaceChildren(h('span', { class: 'tags-empty', text: hasTags ? 'No tags for this sound.' : 'Tags arrive with Phase 1b.' }));
      return;
    }
    box.replaceChildren(...list.map((t) => h('span', { class: 'tag', title: t.model ? `${t.label} · ${t.model}` : t.label },
      t.label, isNum(t.score) ? h('span', { class: 'score', text: t.score.toFixed(2) }) : null)));
  },

  numCells(d, row) {
    const f = (d && d.features) || {};
    const cell = (label, val, title) => {
      const dd = h('dd');
      if (val == null) { dd.className = 'na'; dd.textContent = '—'; } else if (Array.isArray(val)) dd.append(val[0], h('span', { class: 'u', text: val[1] }));
      else dd.textContent = val;
      return h('div', { title: title || null }, h('dt', { text: label }), dd);
    };
    const db = (v, u) => (isNum(v) ? [neg(v), u] : null);
    if (!d) {
      return [
        cell('Duration', row && isNum(row.duration_s) ? fmtDur(row.duration_s) : null),
        cell('Integrated', row && isNum(row.lufs) ? [neg(row.lufs), 'LUFS'] : null),
        cell('True peak', row && isNum(row.true_peak_db) ? [neg(row.true_peak_db), 'dBTP'] : null),
        cell('Attack', row && fmtSec(row.attack_s)),
        cell('Sample rate', row && isNum(row.sample_rate) ? fmtHz(row.sample_rate) : null),
        cell('Channels', row && isNum(row.channels) ? String(row.channels) : null),
      ];
    }
    const out = [
      cell('Duration', isNum(f.duration_s) ? fmtDur(f.duration_s) : null),
      isNum(f.lufs_integrated) ? cell('Integrated', db(f.lufs_integrated, 'LUFS'), 'Integrated loudness')
        : cell('Integrated', d.features ? 'short' : null, 'Too short for integrated loudness (needs 0.4 s)'),
      cell('Short-term max', db(f.lufs_short_max, 'LUFS'), 'Maximum short-term (3 s) loudness'),
      cell('True peak', db(f.true_peak_db, 'dBTP')),
      cell('Attack', fmtSec(f.attack_s)),
      cell('Decay', fmtSec(f.decay_s)),
      cell('Lead silence', fmtSec(f.lead_silence_s)),
      cell('Tail silence', fmtSec(f.tail_silence_s)),
      cell('Centroid', fmtHz(f.centroid_hz_mean), 'Mean spectral centroid: brightness'),
      cell('Rolloff', fmtHz(f.rolloff_hz_mean), 'Mean spectral rolloff'),
      cell('Onsets', isNum(f.onset_count) ? String(f.onset_count) : null),
      cell('Sample rate', fmtHz(f.sample_rate)),
      cell('Channels', isNum(f.channels) ? (f.channels === 1 ? '1 (mono)' : f.channels === 2 ? '2 (stereo)' : String(f.channels)) : null),
      cell('Size', fmtBytes(d.size_bytes)),
    ];
    if (isNum(f.tempo_bpm)) out.push(cell('Tempo', [f.tempo_bpm.toFixed(1), 'BPM']));
    if (isNum(f.loop_seam_score)) out.push(cell('Loop seam', f.loop_seam_score.toFixed(2), 'How cleanly the end joins the start (lower is smoother)'));
    if (typeof f.is_music === 'boolean') out.push(cell('Kind', f.is_music ? 'Music' : 'SFX'));
    if (isNum(f.flatness_mean)) out.push(cell('Flatness', f.flatness_mean.toFixed(3), 'Spectral flatness: 0 tonal, 1 noisy'));
    if (!d.features) {
      return [
        h('div', { class: 'wide' }, h('dt', { text: 'Not analysed' }), h('dd', { class: 'na', text: 'Foley has no features for this file.' })),
        cell('Size', fmtBytes(d.size_bytes)),
        cell('Format', d.ext ? d.ext.replace('.', '').toUpperCase() : null),
      ];
    }
    return out;
  },

  renderBands(be) {
    const box = $('d-bands');
    if (!be || typeof be !== 'object') { box.replaceChildren(h('span', { class: 'empty-line', text: 'No band energy recorded.' })); box.style.display = 'block'; return; }
    box.style.display = '';
    const vals = BAND_INFO.map(([k]) => (isNum(be[k]) ? be[k] : 0));
    const max = Math.max(...vals, 0.0001);
    box.setAttribute('role', 'img');
    box.setAttribute('aria-label', 'Band energy: ' + BAND_INFO.map(([, label], i) => `${label} ${(vals[i] * 100).toFixed(0)}%`).join(', '));
    box.replaceChildren(...BAND_INFO.map(([k, label, hz], i) => {
      const pct = vals[i] * 100;
      const bar = h('div', { class: 'band-bar' });
      bar.style.height = `${(vals[i] / max) * 100}%`;
      return h('div', { class: 'band', title: `${label} (${hz} Hz): ${pct.toFixed(1)}% of energy` },
        h('div', { class: 'band-val', text: (pct >= 10 ? pct.toFixed(0) : pct >= 1 ? pct.toFixed(1) : pct > 0 ? '<1' : '0') + '%' }),
        h('div', { class: 'band-track' }, bar),
        h('div', { class: 'band-name', text: label }),
        h('div', { class: 'band-hz', text: hz }));
    }));
  },

  renderLicense(lic) {
    const box = $('d-license');
    if (!lic) {
      box.replaceChildren(h('div', { class: 'license none', text: 'No licence file found in this folder or above it. Check the source before shipping.' }));
      return;
    }
    box.replaceChildren(h('div', { class: 'license' }, h('code', { text: lic }),
      h('button', { type: 'button', class: 'btn ghost small', text: 'Copy', title: 'Copy full path of the licence file', onclick: () => copyText(fullPath(lic), 'Licence path copied') })));
  },

  renderDupes(dupes) {
    const box = $('d-dupes');
    $('d-dupes-count').textContent = dupes.length ? String(dupes.length) : '';
    if (!dupes.length) { box.replaceChildren(h('li', { class: 'empty-line', text: 'No identical copies.' })); return; }
    box.replaceChildren(...dupes.map((x) => h('li', null, h('button', {
      type: 'button', title: `Open ${x.path}`,
      onclick: () => openAsset(x.id, null, { play: prefs.autoplay, fromDupe: true }),
    }, h('span', { class: 'p', text: x.path }), x.status && x.status !== 'active' ? h('span', { class: 'badge' + (x.status === 'error' ? ' err' : ''), text: x.status }) : null))));
  },

  renderLedger(decisions, usages) {
    const box = $('d-ledger');
    if (!decisions.length && !usages.length) {
      box.replaceChildren(h('span', { class: 'empty-line', text: 'No decisions or usages recorded for this sound yet.' }));
      return;
    }
    const kids = [];
    const dash = (v) => (v == null || v === '' ? '—' : String(v));
    const gain = (v) => (isNum(v) ? `${signed(v)} dB` : '—');
    if (decisions.length) {
      kids.push(h('h3', { class: 'ledger-sub', text: `Decisions (${decisions.length})` }));
      kids.push(h('table', { class: 'ledger-table' },
        h('thead', null, h('tr', null, ['Verdict', 'Project', 'Usage', 'Gain', 'Note', 'When'].map((t) => h('th', { text: t })))),
        h('tbody', null, decisions.map((r) => {
          const v = String(r.verdict || '');
          const cls = /accept|approv|keep|use|yes|pick|good/i.test(v) ? 'good' : /reject|no|drop|bad|veto/i.test(v) ? 'bad' : '';
          return h('tr', null,
            h('td', null, h('span', { class: 'verdict ' + cls, text: dash(v) })),
            h('td', { text: dash(r.project) }), h('td', { text: dash(r.usage_key) }),
            h('td', { class: 'tnum', text: gain(r.gain_db) }), h('td', { text: dash(r.note) }),
            h('td', { class: 'tnum', title: r.decided_by ? `by ${r.decided_by}` : null, text: fmtDate(r.decided_at) }));
        }))));
    }
    if (usages.length) {
      kids.push(h('h3', { class: 'ledger-sub', text: `Used in projects (${usages.length})` }));
      kids.push(h('table', { class: 'ledger-table' },
        h('thead', null, h('tr', null, ['Project', 'Usage', 'Imported as', 'Gain', 'When'].map((t) => h('th', { text: t })))),
        h('tbody', null, usages.map((r) => h('tr', null,
          h('td', { text: dash(r.project) }), h('td', { text: dash(r.usage_key) }),
          h('td', { title: r.license ? `Licence: ${r.license}` : null, text: dash(r.project_path) }),
          h('td', { class: 'tnum', title: isNum(r.normalized_to_lufs) ? `normalised to ${neg(r.normalized_to_lufs)} LUFS${r.gain_applied ? ', gain baked in' : ''}` : null, text: gain(r.gain_db) }),
          h('td', { class: 'tnum', text: fmtDate(r.imported_at) }))))));
    }
    box.replaceChildren(...kids);
  },

  renderMeta(meta) {
    const box = $('d-meta');
    if (!meta || typeof meta !== 'object' || !Object.keys(meta).length) {
      box.replaceChildren(h('span', { class: 'empty-line', text: 'No embedded metadata.' }));
      return;
    }
    const groupNames = { ucs: ['UCS', 'parsed from filename'], bext: ['Broadcast WAV', 'bext'], ixml: ['iXML', ''], info: ['RIFF INFO', ''], tags: ['Tags', 'ID3 / Vorbis / FLAC'] };
    const order = ['ucs', 'bext', 'ixml', 'info', 'tags'];
    const keys = [...order.filter((k) => k in meta), ...Object.keys(meta).filter((k) => !order.includes(k) && k !== 'warnings'), ...('warnings' in meta ? ['warnings'] : [])];
    const fmtVal = (v) => (v == null ? '—' : Array.isArray(v) ? v.map((x) => (typeof x === 'object' ? JSON.stringify(x) : String(x))).join(', ') : typeof v === 'object' ? JSON.stringify(v, null, 1) : String(v));
    const groups = keys.map((k) => {
      const v = meta[k];
      const [title, src] = groupNames[k] || [k === 'warnings' ? 'Parser warnings' : humanKey(k), ''];
      const head = h('h3', null, title, src ? h('span', { class: 'src', text: src }) : null);
      let body;
      if (v && typeof v === 'object' && !Array.isArray(v)) {
        const entries = Object.entries(v).filter(([, x]) => x !== '' && x != null);
        body = entries.length ? h('dl', { class: 'kv' }, entries.flatMap(([kk, vv]) => [h('dt', { text: humanKey(kk) }), h('dd', { text: fmtVal(vv) })])) : h('span', { class: 'empty-line', text: 'Empty' });
      } else {
        body = h('dl', { class: 'kv' }, h('dt', { text: k === 'warnings' ? '' : humanKey(k) }), h('dd', { text: fmtVal(v) }));
      }
      return h('div', { class: 'meta-group' }, head, body);
    });
    box.replaceChildren(...groups);
  },

  renderRecord(d) {
    const rows = [
      ['Asset id', String(d.id)], ['Status', d.status], ['Format', d.ext], ['Pack', d.pack], ['Category', d.category_path],
      ['Content hash', d.content_hash, true], ['Audio hash', d.audio_hash, true], ['First seen', fmtDate(d.first_seen)],
      ['Last seen', fmtDate(d.last_seen)], ['Indexed', d.indexed_at ? fmtDate(d.indexed_at) : 'not yet'],
    ];
    if (d.missing_since) rows.push(['Missing since', fmtDate(d.missing_since)]);
    if (d.error) rows.push(['Error', d.error]);
    if (d.image && d.image.version) rows.push(['Overview', `v${d.image.version}${d.image.exists === false ? ' (not cached)' : ''}`]);
    $('d-record').replaceChildren(...rows.filter(([, v]) => v != null && v !== '').flatMap(([k, v, mono]) => [h('dt', { text: k }), h('dd', { class: mono ? 'mono' : null, text: v })]));
  },

  async saveNote() {
    if (this.id == null) return;
    const ta = $('d-note');
    const text = ta.value.trim();
    if (text === (this.noteSaved || '').trim()) { if ($('d-note-status').textContent === 'Unsaved') $('d-note-status').textContent = ''; return; }
    const id = this.id;
    const status = $('d-note-status');
    const wasStarred = $('d-star').getAttribute('aria-pressed') === 'true';
    if (!text && !wasStarred) { this.noteSaved = ''; status.textContent = ''; return; }
    status.className = 'note-status muted';
    status.textContent = 'Saving…';
    try {
      const r = await api('/api/shortlist', { method: 'POST', body: JSON.stringify({ id, starred: true, note: text }) });
      this.noteSaved = text;
      const note = r && typeof r.note === 'string' ? r.note : text;
      state.shortlistNotes.set(id, note);
      applyStar(id, true);
      if (this.id === id) { status.textContent = wasStarred ? 'Note saved' : 'Note saved · starred'; }
      refreshShortlist();
    } catch (e) {
      if (this.id === id) { status.className = 'note-status error'; status.textContent = `Couldn't save note: ${e.message}`; }
    }
  },
};

function fullPath(rel) {
  const root = state.status && state.status.library_root;
  if (!root || !rel) return rel || '';
  const win = root.includes('\\') && !root.includes('/');
  const sep = win ? '\\' : '/';
  return root.replace(/[\\/]+$/, '') + sep + (win ? rel.replace(/\//g, '\\') : rel);
}

function openAsset(id, row, opts = {}) {
  if (!row) {
    // e.g. a duplicate: use the list row if it is loaded, otherwise detail fills in later.
    for (const page of list.pages.values()) { const it = page.find((x) => x.id === id); if (it) { row = it; break; } }
    if (opts.fromDupe) {
      let idx = -1;
      for (const [p, page] of list.pages) { const j = page.findIndex((x) => x.id === id); if (j >= 0) { idx = p * PAGE + j; break; } }
      list.sel = idx;
      if (idx >= 0) list.ensureVisible(idx);
      list.render();
      list.renderFoot();
    }
  }
  detail.open(id, row, opts);
}

// ------------------------------------------------------------------ star / shortlist

function applyStar(id, starred) {
  list.updateItem(id, { starred });
  if (detail.id === id) detail.renderStar(starred);
  if (detail.data && detail.data.id === id) detail.data.starred = starred;
  if (!starred) state.shortlistNotes.delete(id);
  else if (!state.shortlistNotes.has(id)) state.shortlistNotes.set(id, '');
}

async function toggleStar(id) {
  let cur = false;
  if (detail.id === id) cur = $('d-star').getAttribute('aria-pressed') === 'true';
  else for (const page of list.pages.values()) { const it = page.find((x) => x.id === id); if (it) { cur = !!it.starred; break; } }
  const next = !cur;
  const prevNote = state.shortlistNotes.get(id);
  applyStar(id, next);
  const body = { id, starred: next };
  if (next && detail.id === id && $('d-note').value.trim()) body.note = $('d-note').value.trim();
  try {
    const r = await api('/api/shortlist', { method: 'POST', body: JSON.stringify(body) });
    const ok = r && typeof r.starred === 'boolean' ? r.starred : next;
    applyStar(id, ok);
    if (ok && r && typeof r.note === 'string') state.shortlistNotes.set(id, r.note);
    if (detail.id === id) {
      if (!ok) { $('d-note').value = ''; detail.noteSaved = ''; $('d-note-status').textContent = ''; }
      else detail.noteSaved = (r && r.note) || body.note || '';
    }
    toast(ok ? 'Starred' : 'Removed from shortlist');
    refreshShortlist();
  } catch (e) {
    applyStar(id, cur);
    if (cur && prevNote != null) state.shortlistNotes.set(id, prevNote);
    toast(`Couldn't update the shortlist: ${e.message}`, 'error');
  }
}

// ------------------------------------------------------------------ transport controls

function syncToggles() {
  $('t-loop').setAttribute('aria-pressed', prefs.loop ? 'true' : 'false');
  $('t-autoplay').setAttribute('aria-pressed', prefs.autoplay ? 'true' : 'false');
  $('t-match').setAttribute('aria-pressed', prefs.match ? 'true' : 'false');
  $('t-volume').value = String(prefs.volume);
  $('t-volume-val').textContent = String(Math.round(prefs.volume));
  const lin = Math.pow(prefs.volume / 100, 2);
  $('t-volume').title = `Master volume ${lin > 0 ? neg(20 * Math.log10(lin)) + ' dB' : 'muted'}`;
  audio.loop = prefs.loop;
}

function wireTransport() {
  $('t-play').addEventListener('click', () => player.toggle());
  $('t-stop').addEventListener('click', () => player.stop());
  $('t-restart').addEventListener('click', () => player.playFromStart());
  $('t-loop').addEventListener('click', () => toggleLoop());
  $('t-autoplay').addEventListener('click', () => { prefs.autoplay = !prefs.autoplay; savePrefs(); syncToggles(); });
  $('t-match').addEventListener('click', () => { prefs.match = !prefs.match; savePrefs(); syncToggles(); player.applyGain(false); });
  $('t-volume').addEventListener('input', (e) => {
    prefs.volume = clamp(Number(e.target.value) || 0, 0, 100);
    savePrefs();
    syncToggles();
    player.applyGain(false);
  });
  $('shortlist-btn').addEventListener('click', () => setShortlistView());
  $('root-btn').addEventListener('click', () => setFolder(''));
  $('help-btn').addEventListener('click', () => openHelp());
  $('help-close').addEventListener('click', () => $('help').close());
  $('help').addEventListener('click', (e) => { if (e.target === $('help')) $('help').close(); });
}
function toggleLoop() {
  prefs.loop = !prefs.loop;
  savePrefs();
  syncToggles();
  toast(prefs.loop ? 'Loop on' : 'Loop off');
}
function openHelp() {
  const d = $('help');
  if (d.open) d.close();
  else d.showModal();
}

// ------------------------------------------------------------------ keyboard

function isTyping(el) {
  if (!el || el === document.body) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') return !['range', 'checkbox', 'radio', 'button', 'submit', 'reset'].includes(el.type);
  return false;
}

function onKeyDown(e) {
  if (e.defaultPrevented || e.isComposing) return;
  if ($('help').open) {
    if (e.key === '?') { e.preventDefault(); $('help').close(); }
    return; // <dialog> handles Esc itself
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (isTyping(t)) {
    if (e.key === 'Escape') {
      e.preventDefault();
      t.blur();
      list.vp.focus({ preventScroll: true });
    } else if (t === qInput && (e.key === 'ArrowDown' || e.key === 'Enter')) {
      // Leave the search box for the results, selecting the first hit once it is there.
      e.preventDefault();
      const changed = qInput.value.trim() !== state.q;
      searchSoon.flush();
      list.vp.focus({ preventScroll: true });
      if (changed) list.pendingSel = { index: 0, play: prefs.autoplay };
      else if (list.total && list.sel < 0) list.select(0, { play: prefs.autoplay });
    }
    return;
  }
  const onButton = t && (t.tagName === 'BUTTON' || t.getAttribute?.('role') === 'radio');
  const onRange = t && t.tagName === 'INPUT' && t.type === 'range';
  switch (e.key) {
    case ' ':
    case 'Spacebar':
      e.preventDefault();
      player.toggle();
      break;
    case 'Enter':
      if (onButton) return; // let the focused button activate
      e.preventDefault();
      if (list.sel >= 0 && list.selectedItem && detail.id !== list.selectedItem.id) list.select(list.sel, { play: true });
      else player.playFromStart();
      break;
    case 'ArrowDown':
    case 'ArrowUp':
      if (onRange) return;
      e.preventDefault();
      list.move(e.key === 'ArrowDown' ? 1 : -1, { repeat: e.repeat });
      if (document.activeElement !== list.vp && !onButton) list.vp.focus({ preventScroll: true });
      break;
    case 'PageDown':
    case 'PageUp':
      e.preventDefault();
      list.move((e.key === 'PageDown' ? 1 : -1) * list.pageRows(), { repeat: e.repeat });
      break;
    case 'Home':
    case 'End':
      if (onRange) return;
      e.preventDefault();
      if (list.total) list.select(e.key === 'Home' ? 0 : list.total - 1, { play: prefs.autoplay });
      break;
    case 'ArrowLeft':
    case 'ArrowRight':
      if (onRange) return;
      e.preventDefault();
      player.nudge((e.key === 'ArrowRight' ? 1 : -1) * (e.shiftKey ? 5 : 1));
      break;
    case 'l':
    case 'L':
      e.preventDefault();
      toggleLoop();
      break;
    case 's':
    case 'S':
      e.preventDefault();
      if (detail.id != null) toggleStar(detail.id);
      break;
    case '/':
      e.preventDefault();
      qInput.focus();
      qInput.select();
      break;
    case '?':
      e.preventDefault();
      openHelp();
      break;
    case 'Escape':
      player.stop();
      break;
    default:
  }
}

function wireKeys() {
  window.addEventListener('keydown', onKeyDown);
  // Space on a focused button must not also "click" it (keyup activates buttons).
  window.addEventListener('keyup', (e) => {
    if ((e.key === ' ' || e.key === 'Spacebar') && !isTyping(e.target) && e.target.tagName === 'BUTTON') e.preventDefault();
  });
  // Audio contexts need a user gesture; create ours on the first one.
  const unlock = () => player.ensureContext();
  window.addEventListener('pointerdown', unlock, { capture: true });
  window.addEventListener('keydown', unlock, { capture: true });
}

// ------------------------------------------------------------------ pane resizing

function wireResizers() {
  const panes = $('panes');
  if (isNum(prefs.treeW)) panes.style.setProperty('--tree-w', prefs.treeW + 'px');
  if (isNum(prefs.detailW)) panes.style.setProperty('--detail-w', prefs.detailW + 'px');
  for (const r of document.querySelectorAll('.resizer')) {
    const which = r.dataset.resize;
    r.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      r.setPointerCapture(e.pointerId);
      r.classList.add('dragging');
      const move = (ev) => {
        if (which === 'tree') {
          prefs.treeW = Math.round(clamp(ev.clientX, 170, 460));
          panes.style.setProperty('--tree-w', prefs.treeW + 'px');
        } else {
          prefs.detailW = Math.round(clamp(window.innerWidth - ev.clientX, 340, Math.max(360, window.innerWidth - 700)));
          panes.style.setProperty('--detail-w', prefs.detailW + 'px');
        }
      };
      const up = () => {
        r.classList.remove('dragging');
        r.removeEventListener('pointermove', move);
        r.removeEventListener('pointerup', up);
        r.removeEventListener('pointercancel', up);
        savePrefs();
      };
      r.addEventListener('pointermove', move);
      r.addEventListener('pointerup', up);
      r.addEventListener('pointercancel', up);
    });
    r.addEventListener('dblclick', () => {
      if (which === 'tree') { prefs.treeW = null; panes.style.removeProperty('--tree-w'); } else { prefs.detailW = null; panes.style.removeProperty('--detail-w'); }
      savePrefs();
    });
  }
}

// ------------------------------------------------------------------ boot

let wired = false;
async function init() {
  if (!wired) {
    wired = true;
    wireFilters();
    wireTransport();
    wireKeys();
    wireAudio();
    wireResizers();
    list.init();
    wave.init();
    overview.init();
    detail.init();
    syncToggles();
    syncFilterUI();
    renderGain();
  }
  renderCrumbs();
  tree.markCurrent();
  const s = await loadStatus();
  if (!s) {
    list.error = new Error("The Foley server isn't answering.");
    list.showState('error');
    return;
  }
  refreshShortlist();
  await tree.init();
  list.reload();
  if (state.view === 'folder' && state.folder) tree.revealPath(state.folder).catch(() => {});
  list.vp.focus({ preventScroll: true });
}

init();
