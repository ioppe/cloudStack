/* ==========================================================================
   云栈 · 虚拟机平台 — 前端应用（vanilla ES2018，无构建、无外部依赖）

   约束（对应 docs/IMPLEMENTATION.md）：
   - 同源 fetch，credentials: 'same-origin'，写请求带 X-CSRF-Token
   - 前端不存任何密码常量；登录走 POST /api/login
   - 所有动态文本只用 textContent / createTextNode，绝不拼 HTML
   - 切换主机时递增 generation，迟到响应直接丢弃
   - 所有写操作在发起瞬间捕获 host + uuid，弹窗与忙状态同源绑定
   - 所有 POST 创建/动作/配置返回 202 {task}，任务由轮询收敛
   ========================================================================== */
'use strict';

/* --------------------------------------------------------------- 工具 */

const SVG_NS = 'http://www.w3.org/2000/svg';

const ICONS = {
  hosts: ['M4 5.5h16v5H4zM4 13.5h16v5H4z', 'M7.5 8h.01M7.5 16h.01'],
  vms: ['M3.5 5h17v10.5h-17z', 'M9 19.5h6M12 15.5v4'],
  images: ['M4 4.5h16v15H4z', 'M4 16.5l4.5-4.5 4.5 4.5 2.5-2.5 4.5 4.5', 'M14.5 8.5h.01'],
  tasks: ['M4.5 6.5h15M4.5 12h15M4.5 17.5h9'],
  refresh: ['M20 12a8 8 0 1 1-2.4-5.7', 'M20 4.5v5h-5'],
  plus: ['M12 5.5v13M5.5 12h13'],
  logout: ['M14.5 4.5H18A1.5 1.5 0 0 1 19.5 6v12A1.5 1.5 0 0 1 18 19.5h-3.5', 'M9.5 8.5L6 12l3.5 3.5', 'M6 12h8.5'],
  close: ['M6 6l12 12M18 6L6 18'],
  check: ['M4.5 12.5l5 5L20 6.5'],
  warn: ['M12 4.5l8.5 15H3.5z', 'M12 10v4.5M12 17.2v.05'],
  power: ['M12 4v7.5', 'M7.6 6.8a6.5 6.5 0 1 0 8.8 0'],
  terminal: ['M3.5 5h17v11h-17z', 'M7 9.5l2.5 2.5L7 14.5', 'M12.5 14.5h4'],
  disk: ['M12 4.5c4.1 0 7 1.2 7 2.5s-2.9 2.5-7 2.5-7-1.2-7-2.5 2.9-2.5 7-2.5z', 'M5 7v10c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5V7'],
  network: ['M12 4.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5z', 'M5.5 15a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5z', 'M18.5 15a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5z', 'M10.4 8.9L7 14.7M13.6 8.9l3.4 5.8M8 17.5h8'],
  dot: ['M12 12h.01'],
  info: ['M12 4.5a7.5 7.5 0 1 1 0 15 7.5 7.5 0 0 1 0-15z', 'M12 11v5M12 8.2v.05'],
  key: ['M15.2 4.5a4.3 4.3 0 1 0-3.1 7.3L4.5 19.4v.1h2l1.5-1.5h1.5v-1.5H11l1.1-1.1a4.3 4.3 0 0 0 3.1-7.4z', 'M16.4 8.1h.01']
};

function icon(name, size) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size || 16));
  svg.setAttribute('height', String(size || 16));
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.7');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const paths = ICONS[name] || ICONS.dot;
  for (const d of paths) {
    const p = document.createElementNS(SVG_NS, 'path');
    p.setAttribute('d', d);
    svg.appendChild(p);
  }
  return svg;
}

const BOOL_PROPS = new Set(['value', 'checked', 'disabled', 'readOnly', 'selected', 'multiple', 'indeterminate']);

function addKids(el, kids) {
  for (const k of kids) {
    if (k === null || k === undefined || k === false || k === true) continue;
    if (Array.isArray(k)) {
      addKids(el, k);
      continue;
    }
    el.appendChild(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}

/** 建 DOM 的唯一入口。所有动态文本走 textContent，杜绝 HTML 注入。 */
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const key in props) {
      const v = props[key];
      if (v === null || v === undefined || v === false) continue;
      if (key === 'class') el.className = v;
      else if (key === 'text') el.textContent = String(v);
      else if (key === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (key.slice(0, 2) === 'on' && typeof v === 'function') el.addEventListener(key.slice(2).toLowerCase(), v);
      else if (BOOL_PROPS.has(key)) el[key] = v;
      else el.setAttribute(key, v === true ? '' : String(v));
    }
  }
  addKids(el, kids);
  return el;
}

const enc = encodeURIComponent;

const GiB = 1024 * 1024 * 1024;

function fmtBytes(n) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  const g = n / GiB;
  if (g >= 1) return (Math.round(g * 10) / 10) + ' GiB';
  const m = n / 1048576;
  if (m >= 1) return Math.round(m) + ' MiB';
  return Math.round(n / 1024) + ' KiB';
}

function usedPct(used, total) {
  if (!total) return 0;
  const p = (used / total) * 100;
  return Math.max(0, Math.min(100, Math.round(p)));
}

function levelFor(pct) {
  if (pct >= 90) return 'high';
  if (pct >= 75) return 'warn';
  return 'normal';
}

function pad2(n) {
  return n < 10 ? '0' + n : String(n);
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  if (isNaN(d.getTime())) return '—';
  return pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' +
    pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
}

function fmtClock(ms) {
  const d = new Date(ms);
  return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
}

function num(v, fallback) {
  const n = Number(v);
  return isFinite(n) ? n : fallback;
}

function safeGet(key) {
  try { return window.sessionStorage.getItem(key); } catch (e) { return null; }
}

function safeSet(key, val) {
  try { window.sessionStorage.setItem(key, val); } catch (e) { /* 隐私模式下忽略 */ }
}

/* --------------------------------------------------------------- 状态 */

const state = {
  user: null,
  csrf: '',
  version: '',

  view: 'hosts',

  hosts: [],
  hostsLoading: false,
  hostsError: null,

  hostId: null,
  hostStatus: {},        // hostId -> {online, status, error, at}
  checking: new Set(),   // 正在检测的主机 id

  status: null,
  statusLoading: false,
  statusError: null,

  vms: [],
  vmsLoading: false,
  vmsError: null,

  images: null,
  imageDirs: [],
  diskDirs: [],
  imagesLoading: false,
  imagesError: null,

  tasks: [],
  tasksLoading: false,
  tasksError: null,
  tasksAt: 0,
  highlight: null,

  busy: new Set()        // `${host}:${uuid}` 正在执行动作
};

const gen = { hosts: 0, vms: 0, status: 0, images: 0 };

/* --------------------------------------------------------------- DOM */

const el = {};

function cacheEls() {
  el.loginLayer = document.getElementById('login-layer');
  el.loginForm = document.getElementById('login-form');
  el.loginUser = document.getElementById('login-username');
  el.loginPass = document.getElementById('login-password');
  el.loginSubmit = document.getElementById('login-submit');
  el.loginError = document.getElementById('login-error');
  el.loginVersion = document.getElementById('login-version');

  el.app = document.getElementById('app');
  el.nav = document.getElementById('nav');
  el.hostSelect = document.getElementById('host-select');
  el.topbarStatus = document.getElementById('topbar-status');
  el.topbarStatusText = document.getElementById('topbar-status-text');
  el.topbarUser = document.getElementById('topbar-user');
  el.sidebarUser = document.getElementById('sidebar-user');
  el.viewRoot = document.getElementById('view-root');
  el.modalRoot = document.getElementById('modal-root');
  el.toastRoot = document.getElementById('toast-root');
  el.demoBanner = document.getElementById('demo-banner');
}

/* --------------------------------------------------------------- API */

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status || 0;
  }
}

function handleUnauthorized(serverMessage) {
  if (!state.user) return;      // 登录页自身的 401 由登录流程处理
  state.user = null;
  state.csrf = '';
  resetHostState();
  const detail = serverMessage && serverMessage !== '未登录' ? '（' + serverMessage + '）' : '';
  showLogin('登录状态已失效，请重新登录。' + detail);
}

async function api(method, path, body) {
  if (DEMO) return demoApi(method, path, body);

  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (state.csrf) headers['X-CSRF-Token'] = state.csrf;

  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      credentials: 'same-origin',
      cache: 'no-store',
      body: body === undefined ? undefined : JSON.stringify(body)
    });
  } catch (e) {
    throw new ApiError('无法连接后端服务，请检查网络或服务状态。', 0);
  }

  let data = null;
  const raw = await res.text();
  if (raw) {
    try { data = JSON.parse(raw); } catch (e) { data = null; }
  }

  if (res.status === 401) {
    const msg = (data && data.error) || '登录已过期，请重新登录。';
    handleUnauthorized(msg);
    throw new ApiError(msg, 401);
  }
  if (!res.ok) {
    const msg = (data && data.error) || ('请求失败（HTTP ' + res.status + '）');
    throw new ApiError(msg, res.status);
  }
  return data === null ? {} : data;
}

/* --------------------------------------------------------------- 登录 */

function setLoginError(msg, kind) {
  if (!el.loginError) return;
  if (!msg) {
    el.loginError.hidden = true;
    el.loginError.textContent = '';
    return;
  }
  el.loginError.hidden = false;
  el.loginError.textContent = msg;
  el.loginError.className = kind === 'info' ? 'field__error muted' : 'field__error';
}

function showLogin(message) {
  state.user = null;
  state.csrf = '';
  if (el.app) el.app.hidden = true;
  if (el.loginLayer) el.loginLayer.hidden = false;
  setLoginError(message || '', message ? 'error' : 'none');
  if (el.loginPass) el.loginPass.value = '';
  if (el.loginUser) {
    // 只有用户名留空时才自动聚焦，避免 401 时抢焦点
    if (!el.loginUser.value) el.loginUser.focus();
  }
}

function showApp() {
  if (el.loginLayer) el.loginLayer.hidden = true;
  if (el.app) el.app.hidden = false;
  if (el.topbarUser) el.topbarUser.textContent = state.user || '—';
  if (el.sidebarUser) el.sidebarUser.textContent = state.user || '—';
  render();
}

function bindLogin() {
  el.loginForm.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    const username = el.loginUser.value.trim();
    const password = el.loginPass.value;
    if (!username || !password) {
      setLoginError('请输入用户名和密码');
      (username ? el.loginPass : el.loginUser).focus();
      return;
    }
    setLoginError('');
    setLoginBusy(true);
    try {
      const data = await api('POST', '/api/login', { username: username, password: password });
      state.csrf = data.csrf_token || '';
      state.user = data.username || username;
      el.loginPass.value = '';
      showApp();
      loadHosts();
      loadTasks({ quiet: true });
    } catch (e) {
      setLoginError(e.message || '登录失败');
      el.loginPass.value = '';
      el.loginPass.focus();
    } finally {
      setLoginBusy(false);
    }
  });
}

function setLoginBusy(busy) {
  el.loginSubmit.disabled = busy;
  el.loginSubmit.replaceChildren(...[
    busy ? h('span', { class: 'btn__spinner', 'aria-hidden': 'true' }) : null,
    h('span', null, busy ? '登录中…' : '登录')
  ].filter(Boolean));
}

async function logout() {
  try { await api('POST', '/api/logout', {}); } catch (e) { /* 忽略：本地状态照常清理 */ }
  resetHostState();
  state.view = 'hosts';
  showLogin('已退出登录。');
}

function resetHostState() {
  gen.hosts++; gen.vms++; gen.status++; gen.images++;
  cancelVmsPoll();
  vmsInflight = null;
  vmsRefreshQueued = false;
  state.hosts = [];
  state.hostsError = null;
  state.hostId = null;
  state.hostStatus = {};
  state.checking = new Set();
  state.status = null;
  state.statusError = null;
  state.vms = [];
  state.vmsError = null;
  state.images = null;
  state.imageDirs = [];
  state.diskDirs = [];
  state.tasks = [];
  state.busy = new Set();
  lastViewKey = '';
  topbarSig = '';
}

/* --------------------------------------------------------------- 渲染 */

let lastViewKey = '';
let topbarSig = '';

function render() {
  const authed = !!state.user;
  if (el.loginLayer) el.loginLayer.hidden = authed;
  if (el.app) el.app.hidden = !authed;
  if (!authed) return;
  if (el.topbarUser) el.topbarUser.textContent = state.user;
  if (el.sidebarUser) el.sidebarUser.textContent = state.user;
  renderNav();
  renderTopbar();
  renderView();
}

function renderNav() {
  const counts = { hosts: state.hosts.length, vms: state.vms.length, images: (state.images || []).length, tasks: state.tasks.filter(t => t.status === 'running').length };
  const items = el.nav.querySelectorAll('[data-view]');
  for (const item of items) {
    const view = item.getAttribute('data-view');
    if (view === state.view) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
    const badge = item.querySelector('[data-count]');
    if (badge) {
      const n = counts[view];
      badge.textContent = n ? String(n) : '';
    }
  }
}

function renderTopbar() {
  const sig = JSON.stringify(state.hosts.map(hh => [hh.id, hh.name])) + '|' + (state.hostId || '');
  if (sig !== topbarSig) {
    topbarSig = sig;
    const opts = state.hosts.map(hh => h('option', { value: hh.id }, hh.name + ' · ' + hh.id));
    if (!opts.length) opts.push(h('option', { value: '' }, '（暂无纳管主机）'));
    el.hostSelect.replaceChildren(...opts);
    el.hostSelect.value = state.hostId || '';
    el.hostSelect.disabled = !state.hosts.length;
  } else if (el.hostSelect.value !== (state.hostId || '')) {
    el.hostSelect.value = state.hostId || '';
  }

  let cls = 'pill';
  let text = '未选择主机';
  if (!state.hostId) {
    text = state.hosts.length ? '请选择主机' : '暂无主机';
  } else if (state.checking.has(state.hostId) || state.statusLoading) {
    cls = 'pill pill--busy';
    text = '检测中…';
  } else {
    const st = state.hostStatus[state.hostId];
    if (!st) {
      text = '未检测';
    } else if (st.online) {
      cls = 'pill pill--on';
      text = '在线';
    } else {
      cls = 'pill pill--off';
      text = '离线';
    }
  }
  el.topbarStatus.className = cls;
  el.topbarStatusText.textContent = text;
  el.topbarStatus.title = text;
}

function viewSignature() {
  const s = state;
  switch (s.view) {
    case 'hosts':
      return JSON.stringify([s.hosts, s.hostsError, s.hostsLoading, s.hostStatus, s.hostId, Array.from(s.checking)]);
    case 'vms':
      return JSON.stringify([s.hostId, s.vms, s.vmsError, s.vmsLoading, s.status, s.statusError, s.statusLoading, Array.from(s.busy)]);
    case 'images':
      return JSON.stringify([s.hostId, s.images, s.imageDirs, s.diskDirs, s.imagesError, s.imagesLoading, s.hostStatus[s.hostId] || null]);
    case 'tasks':
      return JSON.stringify([s.tasks, s.tasksError, s.tasksLoading, s.tasksAt, s.highlight]);
    default:
      return s.view;
  }
}

function renderView() {
  const key = state.view + '|' + viewSignature();
  if (key === lastViewKey) return;
  lastViewKey = key;

  const active = document.activeElement;
  const focusId = active && el.viewRoot.contains(active) ? active.getAttribute('data-testid') : null;

  const frag = document.createDocumentFragment();
  if (state.view === 'hosts') frag.appendChild(buildHostsView());
  else if (state.view === 'vms') frag.appendChild(buildVmsView());
  else if (state.view === 'images') frag.appendChild(buildImagesView());
  else frag.appendChild(buildTasksView());
  el.viewRoot.replaceChildren(frag);

  if (focusId) {
    const nodes = el.viewRoot.querySelectorAll('[data-testid]');
    for (const n of nodes) {
      if (n.getAttribute('data-testid') === focusId) { n.focus({ preventScroll: true }); break; }
    }
  }
}

/* --------------------------------------------------------------- 通用片段 */

function btn(o) {
  return h('button', {
    class: 'btn' + (o.variant ? ' btn--' + o.variant : '') + (o.size === 'sm' ? ' btn--sm' : ''),
    type: o.type || 'button',
    'data-testid': o.testid || null,
    disabled: !!o.disabled,
    title: o.title || null,
    onClick: o.onClick || null
  },
    o.icon ? icon(o.icon, o.size === 'sm' ? 13 : 14) : null,
    o.loading ? h('span', { class: 'btn__spinner', 'aria-hidden': 'true' }) : null,
    o.label ? h('span', null, o.label) : null
  );
}

function badge(label, variant, dot) {
  return h('span', { class: 'badge' + (variant ? ' badge--' + variant : '') },
    dot ? h('span', { class: 'badge__dot', 'aria-hidden': 'true' }) : null,
    h('span', null, label)
  );
}

function notice(o) {
  return h('div', { class: 'notice notice--' + (o.kind || 'error'), role: o.kind === 'error' ? 'alert' : null, 'data-testid': o.testid || null },
    h('span', { class: 'notice__icon' }, icon(o.kind === 'error' ? 'warn' : 'info', 15)),
    h('div', { class: 'notice__body' },
      o.title ? h('span', { class: 'notice__title' }, o.title) : null,
      h('span', { class: 'notice__msg' }, o.message || '')
    ),
    o.action ? h('span', { class: 'notice__action' }, o.action) : null
  );
}

function emptyState(o) {
  return h('div', { class: 'empty', 'data-testid': o.testid || null },
    h('span', { class: 'empty__icon' }, icon(o.icon || 'info', 18)),
    h('span', { class: 'empty__title' }, o.title),
    o.hint ? h('span', { class: 'empty__hint' }, o.hint) : null,
    o.actions && o.actions.length ? h('div', { class: 'empty__actions' }, o.actions) : null
  );
}

function capBand(label, pct, valueText, testid) {
  return h('div', { class: 'cap', 'data-testid': testid || null },
    h('span', { class: 'cap__label' }, label),
    pct === null || pct === undefined ? null :
      h('span', { class: 'cap__track' },
        h('span', { class: 'cap__fill', 'data-level': levelFor(pct), style: { width: Math.max(2, pct) + '%' } })),
    h('span', { class: 'cap__value num' }, valueText)
  );
}

function viewHead(title, desc, actions, count) {
  return h('div', { class: 'view__head' },
    h('div', { class: 'view__titles' },
      h('h1', { class: 'view__title' },
        h('span', null, title),
        count === null || count === undefined ? null : h('span', { class: 'count num' }, String(count))
      ),
      desc ? h('p', { class: 'view__desc' }, desc) : null
    ),
    actions && actions.length ? h('div', { class: 'view__actions' }, actions) : null
  );
}

function dataTable(testid, cols, rowEls, minWidth, extraClass) {
  const thead = h('thead', null, h('tr', null, cols.map(c =>
    h('th', { scope: 'col', style: c.width ? { width: c.width } : null }, c.label))));
  const tbody = h('tbody', { class: 'stagger' }, rowEls);
  return h('div', { class: 'table-scroll' },
    h('table', { class: 'table' + (extraClass ? ' ' + extraClass : ''), 'data-testid': testid, style: minWidth ? { minWidth: minWidth } : null }, thead, tbody));
}

function skeletonRows(cols, n) {
  const rows = [];
  for (let i = 0; i < n; i++) {
    const cells = [];
    for (let c = 0; c < cols; c++) {
      cells.push(h('td', null, h('span', { class: 'skeleton skeleton--row', style: { width: (45 + ((i * 7 + c * 13) % 40)) + '%' } })));
    }
    rows.push(h('tr', { 'aria-hidden': 'true' }, cells));
  }
  return rows;
}

function card(title, sub, actions, body, testid) {
  return h('section', { class: 'card', 'data-testid': testid || null },
    title ? h('div', { class: 'card__head' },
      h('h2', { class: 'card__title' }, title),
      sub ? h('span', { class: 'card__sub' }, sub) : null,
      actions && actions.length ? h('div', { class: 'card__actions' }, actions) : null
    ) : null,
    body
  );
}

/* --------------------------------------------------------------- 主机视图 */

function hostById(id) {
  for (const hh of state.hosts) if (hh.id === id) return hh;
  return null;
}

function hostLabel(id) {
  const hh = hostById(id);
  return hh ? hh.name : (id || '—');
}

function transportLabel(hh) {
  if (!hh) return '—';
  if (hh.transport === 'local') return 'local · 本机';
  return 'ssh ' + (hh.ssh_user || 'root') + '@' + (hh.address || '—') + ':' + (hh.ssh_port || 22);
}

function buildHostsView() {
  const frag = document.createDocumentFragment();

  frag.appendChild(viewHead('主机', '纳管登记、连接方式与实时资源。解除纳管只删除本平台登记，不会改动被管主机。', [
    btn({
      label: '检测当前主机', icon: 'check', testid: 'btn-host-check-current',
      disabled: !state.hostId || state.checking.has(state.hostId),
      loading: !!state.hostId && state.checking.has(state.hostId),
      onClick: () => state.hostId && checkHost(state.hostId)
    }),
    btn({ label: '新增主机', icon: 'plus', variant: 'primary', testid: 'btn-host-new', onClick: () => openHostDialog(null) })
  ], state.hosts.length || null));

  if (state.hostsError) {
    frag.appendChild(notice({
      kind: 'error', title: '主机列表加载失败', message: state.hostsError, testid: 'hosts-error',
      action: btn({ label: '重试', size: 'sm', onClick: () => loadHosts() })
    }));
  }

  const cols = [
    { label: '主机', width: '22%' },
    { label: '地址 / 连接', width: '20%' },
    { label: '状态', width: '12%' },
    { label: '资源', width: '28%' },
    { label: '操作', width: '18%' }
  ];

  let body;
  if (state.hostsLoading && !state.hosts.length) {
    body = dataTable('hosts-table', cols, skeletonRows(5, 3), 880);
  } else if (!state.hosts.length) {
    body = emptyState({
      icon: 'hosts', testid: 'hosts-empty',
      title: '还没有纳管任何主机',
      hint: '纳管只写入登记信息，不会在远端安装任何组件。local 主机即控制端本机。',
      actions: [btn({ label: '新增主机', icon: 'plus', variant: 'primary', testid: 'hosts-empty-new', onClick: () => openHostDialog(null) })]
    });
  } else {
    const rows = state.hosts.map((hh, i) => {
      const st = state.hostStatus[hh.id];
      const checking = state.checking.has(hh.id);
      const isCurrent = hh.id === state.hostId;

      let statusCell;
      if (checking) statusCell = badge('检测中', 'busy', true);
      else if (!st) statusCell = badge('未检测', 'idle', true);
      else if (st.online) statusCell = badge('在线', 'on', true);
      else statusCell = badge('离线', 'off', true);

      const meta = st && st.online && st.status
        ? (st.status.hostname || '') + (st.status.os ? ' · ' + st.status.os : '')
        : (st && st.error ? st.error : '');

      let resources;
      if (checking) {
        resources = h('span', { class: 'skeleton skeleton--row', style: { width: '160px' } });
      } else if (st && st.online && st.status) {
        const s = st.status;
        const memTotal = num(s.memory_total, 0);
        const memAvail = num(s.memory_available, 0);
        const diskTotal = num(s.disk_total, 0);
        const diskFree = num(s.disk_free, 0);
        resources = h('div', { class: 'cap-row' },
          capBand('CPU', null, (s.cpu_count ? s.cpu_count + ' 核' : '—') + (s.kvm ? ' · KVM' : '')),
          capBand('内存', usedPct(memTotal - memAvail, memTotal), fmtBytes(memTotal - memAvail) + ' / ' + fmtBytes(memTotal), 'host-cap-mem-' + hh.id),
          capBand('磁盘', usedPct(diskTotal - diskFree, diskTotal), fmtBytes(diskTotal - diskFree) + ' / ' + fmtBytes(diskTotal), 'host-cap-disk-' + hh.id)
        );
      } else {
        resources = h('span', { class: 'muted' }, st ? '上次检测失败' : '点击“检测”读取真实资源');
      }

      return h('tr', {
        'data-testid': 'host-row-' + hh.id,
        'data-host': hh.id,
        style: { '--i': String(Math.min(i, 8)) }
      },
        h('td', null,
          h('div', { class: 'cell-name' },
            h('span', { class: 'cell-name__title' }, hh.name || hh.id,
              isCurrent ? badge('当前', 'teal') : null),
            h('span', { class: 'mono' }, hh.id)
          )
        ),
        h('td', null,
          h('div', { class: 'cell-name' },
            h('span', null, transportLabel(hh)),
            h('span', { class: 'mono' }, hh.address || '—')
          )
        ),
        h('td', null,
          h('div', { class: 'cell-name' },
            h('span', { 'data-testid': 'host-status-' + hh.id }, statusCell),
            meta ? h('span', { class: 'mono', title: meta }, meta.length > 34 ? meta.slice(0, 32) + '…' : meta) : null
          )
        ),
        h('td', null, resources, st && st.online && st.status
          ? h('span', { class: 'mono' }, 'VM ' + num(st.status.vm_count, 0) + ' · 运行 ' + num(st.status.running_count, 0))
          : null),
        h('td', null,
          h('div', { class: 'cell-actions' },
            btn({
              label: '检测', size: 'sm', testid: 'host-check-' + hh.id,
              disabled: checking, loading: checking,
              onClick: () => checkHost(hh.id)
            }),
            btn({ label: '编辑', size: 'sm', testid: 'host-edit-' + hh.id, onClick: () => openHostDialog(hh) }),
            btn({
              label: '解除纳管', size: 'sm', variant: 'danger-quiet', testid: 'host-delete-' + hh.id,
              onClick: () => openDeleteHostDialog(hh)
            })
          )
        )
      );
    });
    body = dataTable('hosts-table', cols, rows, 880);
  }

  frag.appendChild(card(null, null, null, body, 'hosts-card'));
  return frag;
}

/* --------------------------------------------------------------- 虚拟机 */

function vmStateMeta(vm) {
  const raw = String((vm && vm.state) || '').toLowerCase();
  if (raw === 'running') return { label: '运行中', variant: 'on' };
  if (raw === 'paused') return { label: '已暂停', variant: 'note' };
  if (raw === 'shut off' || raw === 'shutoff' || raw === 'stopped') return { label: '已关机', variant: 'idle' };
  if (raw === 'saved') return { label: '已保存', variant: 'note' };
  if (raw === 'crashed') return { label: '已崩溃', variant: 'off' };
  return { label: (vm && vm.state) || '未知', variant: 'idle' };
}

function vmFlags(vm) {
  const raw = String((vm && vm.state) || '').toLowerCase();
  const off = raw === 'shut off' || raw === 'shutoff' || raw === 'stopped';
  const saved = raw === 'saved';
  const crashed = raw === 'crashed';
  return {
    raw,
    off,
    saved,
    crashed,
    running: raw === 'running',
    paused: raw === 'paused',
    canStart: off || saved || crashed,
    canShutdown: raw === 'running',
    canStop: raw === 'running' || raw === 'paused',
    canReboot: raw === 'running',
    canSuspend: raw === 'running',
    canResume: raw === 'paused',
    canConfig: off,
    canDelete: off || saved || crashed
  };
}

function isVmBusy(hostId, uuid) {
  return state.busy.has(hostId + ':' + uuid);
}

function viewIps(vm) {
  if (Array.isArray(vm.ip_addresses) && vm.ip_addresses.length) return vm.ip_addresses;
  const out = [];
  if (Array.isArray(vm.interfaces)) {
    for (const itf of vm.interfaces) {
      if (itf && Array.isArray(itf.ips)) for (const ip of itf.ips) if (out.indexOf(ip) < 0) out.push(ip);
    }
  }
  return out;
}

// guest-agent 未连接时，执行端会从宿主机网桥本机网段做 ARP 探测；两者都没有就只能控制台确认。
const IP_TITLE_HINT = 'guest-agent 未连接时由执行端从局域网 ARP 探测，仍未知可在控制台确认。';

function ipCell(vm, flags, ips) {
  const testid = 'vm-ip-' + (vm && vm.uuid);
  const status = (vm && vm.ip_status) || '';
  const reason = (vm && vm.ip_status_reason) || '';
  if (status === 'not_running' || !flags.running) {
    return h('span', { class: 'muted', title: '虚拟机未运行', 'data-testid': testid }, '—');
  }
  if (ips.length) {
    const text = ips.slice(0, 2).join(', ') + (ips.length > 2 ? ' +' + (ips.length - 2) : '');
    return h('span', { class: 'num', title: ips.join(', '), 'data-testid': testid }, text);
  }
  if (status === 'discovering') {
    return h('span', { class: 'muted', title: (reason || '正在从局域网 ARP 探测地址') + '。' + IP_TITLE_HINT, 'data-testid': testid }, '探测中…');
  }
  // unavailable，或旧数据没有 ip_status：明确“未探测到”，不再一直显示获取中。
  return h('span', { class: 'muted', title: (reason ? reason + '。' : '未探测到地址。') + IP_TITLE_HINT, 'data-testid': testid }, '未探测到');
}

function buildVmsView() {
  const frag = document.createDocumentFragment();
  const hostId = state.hostId;

  frag.appendChild(viewHead('虚拟机', '列表与操作都绑定在顶栏选定的目标主机上；所有动作提交后返回任务，结果在任务页收敛。', [
    btn({
      label: '刷新列表', icon: 'refresh', testid: 'btn-vms-refresh', size: 'sm',
      disabled: !hostId,
      onClick: () => { if (hostId) { loadVms(hostId); loadStatus(hostId); } }
    }),
    btn({
      label: '创建虚拟机', icon: 'plus', variant: 'primary', testid: 'btn-vm-create',
      disabled: !hostId, onClick: () => openCreateDialog()
    })
  ], state.vms.length || null));

  // 目标主机工具栏（第二处主机选择，与顶栏同步）
  if (!state.hosts.length) {
    frag.appendChild(emptyState({
      icon: 'hosts', testid: 'vms-no-hosts',
      title: '还没有可用主机',
      hint: '先到「主机」页纳管一台主机，才能查看与创建虚拟机。',
      actions: [btn({ label: '前往主机页', variant: 'primary', onClick: () => setView('hosts') })]
    }));
    return frag;
  }

  const st = state.hostStatus[hostId];
  const sel = h('select', {
    class: 'select', style: { maxWidth: '260px' }, 'data-testid': 'vms-host-select',
    'aria-label': '目标主机',
    onChange: (ev) => selectHost(ev.target.value)
  }, state.hosts.map(hh => h('option', { value: hh.id }, hh.name + ' · ' + hh.id)));
  sel.value = hostId || '';

  const toolbar = h('div', { class: 'toolbar' },
    h('div', { class: 'toolbar__group' },
      h('span', { class: 'topbar__label' }, '目标主机'),
      sel,
      h('span', { class: 'pill' + (st && st.online ? ' pill--on' : st ? ' pill--off' : '') },
        h('span', { class: 'pill__dot', 'aria-hidden': 'true' }),
        h('span', { 'data-testid': 'vms-host-online' },
          st ? (st.online ? '在线' : '离线') : (state.statusLoading ? '检测中…' : '未检测')))
    ),
    h('span', { class: 'toolbar__spacer' }),
    st && st.online && st.status
      ? h('div', { class: 'toolbar__meta', 'data-testid': 'vms-host-resources' },
        h('span', null, (st.status.hostname || hostId)),
        h('span', null, '·'),
        capBand('CPU', null, st.status.cpu_count ? st.status.cpu_count + ' 核' : '—'),
        capBand('内存', usedPct(num(st.status.memory_total, 0) - num(st.status.memory_available, 0), num(st.status.memory_total, 0)),
          fmtBytes(num(st.status.memory_total, 0) - num(st.status.memory_available, 0)) + ' / ' + fmtBytes(num(st.status.memory_total, 0)))
      )
      : null
  );
  frag.appendChild(toolbar);

  if (state.statusError && st && !st.online) {
    frag.appendChild(notice({
      kind: 'error', title: '主机检测失败（离线）', message: state.statusError, testid: 'vms-host-offline',
      action: btn({ label: '重新检测', size: 'sm', onClick: () => loadStatus(hostId) })
    }));
  }
  if (state.vmsError) {
    frag.appendChild(notice({
      kind: 'error', title: '虚拟机列表加载失败', message: state.vmsError, testid: 'vms-error',
      action: btn({ label: '重试', size: 'sm', onClick: () => loadVms(hostId) })
    }));
  }

  const cols = [
    { label: '虚拟机', width: '24%' },
    { label: '状态', width: '14%' },
    { label: '规格', width: '15%' },
    { label: 'IP', width: '15%' },
    { label: '操作', width: '32%' }
  ];

  let body;
  if (state.vmsLoading && !state.vms.length) {
    body = dataTable('vms-table', cols, skeletonRows(5, 3), 880);
  } else if (!state.vms.length) {
    body = emptyState({
      icon: 'vms', testid: 'vms-empty',
      title: '该主机上没有虚拟机',
      hint: '可以选择 cloud image 或 ISO 创建一台临时虚拟机；创建任务完成后列表自动刷新。',
      actions: [
        btn({ label: '创建虚拟机', icon: 'plus', variant: 'primary', testid: 'vms-empty-create', onClick: () => openCreateDialog() })
      ]
    });
  } else {
    const rows = state.vms.map((vm, i) => {
      const meta = vmStateMeta(vm);
      const f = vmFlags(vm);
      const busy = isVmBusy(hostId, vm.uuid);
      const cfg = vm.config || {};
      const vcpus = cfg.vcpus !== undefined ? cfg.vcpus : (vm.cpu ? vm.cpu : '—');
      const memGb = cfg.memory_gb !== undefined ? cfg.memory_gb : (vm.max_memory ? Math.round(vm.max_memory / 1024 / 1024) : null);
      const diskGb = cfg.primary_disk_gb !== undefined ? cfg.primary_disk_gb : (Array.isArray(vm.disks) ? vm.disks.length : null);
      const metrics = vm.metrics || {};
      const cpuText = metrics.cpu && metrics.cpu.percent !== undefined ? metrics.cpu.percent + '%' : null;
      const memText = metrics.memory && metrics.memory.percent !== undefined ? metrics.memory.percent + '%' : null;
      const ips = viewIps(vm);

      const actionBtn = (action, label, extra) => btn(Object.assign({
        label, size: 'sm', testid: 'vm-action-' + vm.uuid + '-' + action,
        disabled: busy || (extra && extra.disabled),
        title: (extra && extra.title) || label,
        onClick: () => runVmAction(hostId, vm, action)
      }, extra && extra.loading ? { loading: true } : {}));

      return h('tr', {
        'data-testid': 'vm-row-' + vm.uuid,
        'data-host': hostId,
        'data-uuid': vm.uuid,
        class: busy ? 'is-pending' : null,
        style: { '--i': String(Math.min(i, 8)) }
      },
        h('td', null,
          h('div', { class: 'cell-name' },
            h('span', { class: 'cell-name__title' }, vm.name || vm.uuid),
            h('span', { class: 'mono', title: vm.uuid }, vm.uuid)
          )
        ),
        h('td', null,
          h('div', { class: 'cell-name' },
            h('span', { 'data-testid': 'vm-state-' + vm.uuid },
              busy ? badge('执行中', 'busy', true) : badge(meta.label, meta.variant, true)),
            (cpuText || memText) && f.running
              ? h('span', { class: 'mono' }, 'CPU ' + (cpuText || '—') + ' · 内存 ' + (memText || '—'))
              : null
          )
        ),
        h('td', null,
          h('div', { class: 'cell-name' },
            h('span', { class: 'num' }, vcpus + ' vCPU · ' + (memGb === null || memGb === undefined ? '—' : memGb + ' GiB')),
            h('span', { class: 'mono' }, '磁盘 ' + (diskGb === null || diskGb === undefined ? '—' : diskGb + ' GiB'))
          )
        ),
        h('td', null, ipCell(vm, f, ips)),
        h('td', null,
          h('div', { class: 'cell-actions' },
            actionBtn('start', '启动', { disabled: !f.canStart, title: f.canStart ? '启动虚拟机' : '当前状态不可启动' }),
            actionBtn('shutdown', '关机', { disabled: !f.canShutdown, title: f.canShutdown ? '优雅关机（ACPI）' : '仅在运行中可用' }),
            actionBtn('stop', '强制停止', { disabled: !f.canStop, title: f.canStop ? '强制断电，未落盘数据可能丢失' : '仅在运行/暂停时可用' }),
            actionBtn('reboot', '重启', { disabled: !f.canReboot, title: f.canReboot ? '重启' : '仅在运行中可用' }),
            actionBtn('suspend', '挂起', { disabled: !f.canSuspend, title: f.canSuspend ? '挂起到内存' : '仅在运行中可用' }),
            actionBtn('resume', '恢复', { disabled: !f.canResume, title: f.canResume ? '从暂停恢复' : '仅在暂停时可用' }),
            btn({
              label: '配置', size: 'sm', testid: 'vm-config-' + vm.uuid,
              disabled: busy || !f.canConfig,
              title: f.canConfig ? '调整 vCPU / 内存 / 磁盘（关机状态）' : '需先关机才能调整配置',
              onClick: () => openConfigDialog(hostId, vm)
            }),
            btn({
              label: '控制台', size: 'sm', icon: 'terminal', testid: 'vm-console-' + vm.uuid,
              disabled: busy || !f.running || vm.has_console === false || DEMO_CONSOLE_DISABLED,
              title: DEMO_CONSOLE_DISABLED ? '演示数据下不打开控制台' : (!f.running ? '仅在运行中可用' : '打开 noVNC 控制台'),
              onClick: () => openConsole(hostId, vm)
            }),
            btn({
              label: '删除', size: 'sm', variant: 'danger-quiet', testid: 'vm-delete-' + vm.uuid,
              disabled: busy || !f.canDelete,
              title: f.canDelete ? '删除虚拟机（可选删除磁盘）' : '需先关机才能删除',
              onClick: () => openDeleteVmDialog(hostId, vm)
            })
          )
        )
      );
    });
    body = dataTable('vms-table', cols, rows, 1040);
  }

  frag.appendChild(card(null, null, null, body, 'vms-card'));
  return frag;
}

/* --------------------------------------------------------------- 镜像/存储 */

function buildImagesView() {
  const frag = document.createDocumentFragment();
  const hostId = state.hostId;

  frag.appendChild(viewHead('镜像与存储', '只读列出执行端可用的 ISO / 磁盘模板与磁盘目录剩余空间；模板不被修改。', [
    btn({
      label: '刷新', icon: 'refresh', size: 'sm', testid: 'btn-images-refresh',
      disabled: !hostId, loading: state.imagesLoading,
      onClick: () => hostId && loadImages(hostId)
    })
  ], state.images ? state.images.length : null));

  if (!state.hosts.length) {
    frag.appendChild(emptyState({
      icon: 'hosts', testid: 'images-no-hosts',
      title: '还没有可查询的主机', hint: '镜像与存储来自执行端本地目录，需要先纳管主机。',
      actions: [btn({ label: '前往主机页', variant: 'primary', onClick: () => setView('hosts') })]
    }));
    return frag;
  }

  if (!hostId) {
    frag.appendChild(emptyState({
      icon: 'images', testid: 'images-no-host',
      title: '请选择目标主机', hint: '使用顶栏的主机选择器切换后自动加载。'
    }));
    return frag;
  }

  if (state.imagesError) {
    frag.appendChild(notice({
      kind: 'error', title: '镜像列表加载失败', message: state.imagesError, testid: 'images-error',
      action: btn({ label: '重试', size: 'sm', onClick: () => loadImages(hostId) })
    }));
  }

  const cols = [
    { label: '镜像', width: '38%' },
    { label: '类型', width: '10%' },
    { label: '大小', width: '12%' },
    { label: '格式', width: '12%' },
    { label: '系统', width: '28%' }
  ];

  let imgBody;
  if (state.imagesLoading && !state.images) {
    imgBody = dataTable('images-table', cols, skeletonRows(5, 3), 760);
  } else if (!state.images || !state.images.length) {
    imgBody = emptyState({
      icon: 'disk', testid: 'images-empty',
      title: '该主机没有可用镜像',
      hint: '把 ISO 或 cloud image 放入执行端允许的镜像目录后刷新；平台不会自动下载。'
    });
  } else {
    const rows = state.images.map((img, i) => {
      const type = String(img.type || '').toLowerCase();
      const md = img.metadata || {};
      const osText = [md.alias, md.os_family, md.os_version].filter(Boolean).join(' · ');
      return h('tr', {
        'data-testid': 'image-row-' + (img.name || i),
        style: { '--i': String(Math.min(i, 8)) }
      },
        h('td', null, h('div', { class: 'cell-name' },
          h('span', { class: 'cell-name__title' }, img.name || '—'),
          img.path ? h('span', { class: 'mono', title: img.path }, img.path) : null
        )),
        h('td', null, badge(type === 'iso' ? 'ISO' : '磁盘', type === 'iso' ? 'note' : 'teal')),
        h('td', { class: 'num' }, img.size_human || (img.virtual_size ? fmtBytes(img.virtual_size) : '—')),
        h('td', { class: 'mono' }, img.format || '—'),
        h('td', { class: 'soft' }, osText || '—')
      );
    });
    imgBody = dataTable('images-table', cols, rows, 760);
  }

  frag.appendChild(card('镜像', 'ISO 与磁盘模板只读', null, imgBody, 'images-card'));

  // 存储目录
  let storageBody;
  if (state.imagesLoading && !state.images) {
    storageBody = h('div', { class: 'table-scroll' }, h('table', { class: 'table' },
      h('tbody', null, skeletonRows(3, 3))));
  } else if (!state.diskDirs.length) {
    storageBody = emptyState({
      icon: 'disk', testid: 'storage-empty',
      title: '没有磁盘目录信息',
      hint: '执行端未返回 disk_dirs，或尚未配置允许写入的磁盘目录。'
    });
  } else {
    const rows = state.diskDirs.map((d, i) => {
      const free = num(d.free, 0);
      const total = num(d.total, 0);
      const pct = total ? usedPct(total - free, total) : 0;
      return h('tr', { 'data-testid': 'storage-row-' + i, style: { '--i': String(Math.min(i, 8)) } },
        h('td', null, h('div', { class: 'cell-name' },
          h('span', { class: 'cell-name__title mono', style: { fontFamily: 'var(--font-mono)', fontSize: '12px' } }, d.path || '—')
        )),
        h('td', null, capBand('已用', pct, (d.free_human || fmtBytes(free)) + ' 可用', 'storage-cap-' + i)),
        h('td', { class: 'num soft' }, total ? fmtBytes(total) : '—')
      );
    });
    storageBody = dataTable('storage-table', [
      { label: '磁盘目录', width: '46%' }, { label: '剩余', width: '34%' }, { label: '容量', width: '20%' }
    ], rows, 640);
  }
  frag.appendChild(card('存储目录', hostLabel(hostId), null, storageBody, 'storage-card'));

  if (state.imageDirs.length) {
    frag.appendChild(h('p', { class: 'legend' },
      h('span', null, '镜像目录：' + state.imageDirs.join(' · '))
    ));
  }
  return frag;
}

/* --------------------------------------------------------------- 任务 */

function taskBadge(t) {
  if (t.status === 'success') return badge('成功', 'on', true);
  if (t.status === 'failed') return badge('失败', 'off', true);
  if (t.status === 'running') return h('span', { class: 'badge badge--busy' }, h('span', { class: 'btn__spinner', 'aria-hidden': 'true' }), h('span', null, '执行中'));
  return badge(t.status || '未知', 'idle', true);
}

function buildTasksView() {
  const frag = document.createDocumentFragment();

  frag.appendChild(viewHead('任务', '创建、生命周期与配置都以后台任务执行；页面打开时每 3 秒自动轮询。', [
    btn({
      label: '立即刷新', icon: 'refresh', size: 'sm', testid: 'btn-tasks-refresh',
      loading: state.tasksLoading,
      onClick: () => loadTasks()
    })
  ], state.tasks.length || null));

  frag.appendChild(h('div', { class: 'toolbar' },
    h('div', { class: 'toolbar__group' },
      h('span', { class: 'pill pill--busy' },
        h('span', { class: 'pill__dot', 'aria-hidden': 'true' }),
        h('span', null, '自动轮询 3s')
      ),
      h('span', { class: 'toolbar__meta', 'data-testid': 'tasks-updated' },
        state.tasksAt ? '最近更新 ' + fmtClock(state.tasksAt) : '等待首次刷新…')
    ),
    h('span', { class: 'toolbar__spacer' }),
    h('span', { class: 'toolbar__meta' },
      state.tasks.filter(t => t.status === 'running').length + ' 个执行中')
  ));

  if (state.tasksError) {
    frag.appendChild(notice({
      kind: 'error', title: '任务列表加载失败', message: state.tasksError, testid: 'tasks-error',
      action: btn({ label: '重试', size: 'sm', onClick: () => loadTasks() })
    }));
  }

  const cols = [
    { label: '状态', width: '10%' },
    { label: '任务', width: '26%' },
    { label: '主机', width: '14%' },
    { label: '虚拟机', width: '18%' },
    { label: '时间', width: '14%' },
    { label: '错误', width: '18%' }
  ];

  let body;
  if (state.tasksLoading && !state.tasks.length) {
    body = dataTable('tasks-table', cols, skeletonRows(6, 3), 900);
  } else if (!state.tasks.length) {
    body = emptyState({
      icon: 'tasks', testid: 'tasks-empty',
      title: '还没有任务',
      hint: '创建虚拟机、启停或改配置后会在这里出现，可查看所属主机、虚拟机与失败原因。',
      actions: [btn({ label: '去虚拟机页', variant: 'primary', onClick: () => setView('vms') })]
    });
  } else {
    const rows = state.tasks.map((t, i) => h('tr', {
      'data-testid': 'task-row-' + t.id,
      class: state.highlight === t.id ? 'is-highlight' : null,
      style: { '--i': String(Math.min(i, 8)) }
    },
      h('td', null, h('span', { 'data-testid': 'task-status-' + t.id }, taskBadge(t))),
      h('td', null, h('div', { class: 'cell-name' },
        h('span', { class: 'cell-name__title' }, t.title || '（无标题）'),
        h('span', { class: 'mono' }, t.id)
      )),
      h('td', null, h('div', { class: 'cell-name' },
        h('span', null, t.host_name || hostLabel(t.host_id)),
        h('span', { class: 'mono' }, t.host_id || '—')
      )),
      h('td', null, h('div', { class: 'cell-name' },
        h('span', null, t.vm_name || '—'),
        h('span', { class: 'mono' }, t.vm_uuid || '')
      )),
      h('td', { class: 'num soft' },
        h('span', { class: 'nowrap' }, fmtTime(t.created_at)),
        t.finished_at ? h('span', { class: 'mono' }, '完成 ' + fmtTime(t.finished_at)) : null
      ),
      h('td', { 'data-testid': 'task-error-' + t.id, class: 'soft' },
        t.error ? h('span', { class: 'notice__msg', style: { color: 'var(--c-warn)' } }, t.error) : h('span', { class: 'muted' }, '—')
      )
    ));
    body = dataTable('tasks-table', cols, rows, 900);
  }

  frag.appendChild(card(null, null, null, body, 'tasks-card'));
  return frag;
}

/* --------------------------------------------------------------- 主机数据加载 */

async function loadHosts() {
  const g = ++gen.hosts;
  state.hostsLoading = true;
  render();
  try {
    const data = await api('GET', '/api/hosts');
    if (g !== gen.hosts) return;
    state.hosts = Array.isArray(data.hosts) ? data.hosts : [];
    state.hostsError = null;
    const exists = state.hostId && state.hosts.some(hh => hh.id === state.hostId);
    if (!exists) {
      const stored = safeGet('vmplatform.host');
      const pick = state.hosts.some(hh => hh.id === stored) ? stored : (state.hosts[0] ? state.hosts[0].id : null);
      if (pick) {
        selectHost(pick, { force: true });
      } else {
        state.hostId = null;
        lastViewKey = '';
        topbarSig = '';
      }
    }
  } catch (e) {
    if (g !== gen.hosts) return;
    state.hosts = [];
    state.hostsError = e.status === 401 ? '登录已过期' : e.message;
  } finally {
    if (g === gen.hosts) {
      state.hostsLoading = false;
      render();
    }
  }
}

function selectHost(hostId, opts) {
  if (!hostId) return;
  if (hostId === state.hostId && !(opts && opts.force)) return;

  // 切主机：丢弃所有旧状态与在途响应，并停掉旧主机的轮询
  cancelVmsPoll();
  vmsInflight = null;
  vmsRefreshQueued = false;
  state.hostId = hostId;
  gen.vms++;
  gen.status++;
  gen.images++;
  state.vms = [];
  state.vmsError = null;
  state.vmsLoading = true;
  state.status = null;
  state.statusError = null;
  state.statusLoading = true;
  state.images = null;
  state.imageDirs = [];
  state.diskDirs = [];
  state.imagesError = null;
  state.highlight = null;
  safeSet('vmplatform.host', hostId);
  lastViewKey = '';
  render();

  loadVms(hostId);
  loadStatus(hostId);
  if (state.view === 'images') loadImages(hostId);
}

/* VM 列表轻量轮询：只在“虚拟机页 + 已登录 + 主机已选 + 页面可见”时每 5 秒静默刷新。 */
const VMS_POLL_MS = 5000;
let vmsPollTimer = null;
let vmsInflight = null;      // {hostId, gen}：每主机最多一个在途请求
let vmsRefreshQueued = false;

function vmsPollAllowed() {
  return !!(state.user && state.view === 'vms' && state.hostId && !document.hidden);
}

function cancelVmsPoll() {
  if (vmsPollTimer) {
    window.clearTimeout(vmsPollTimer);
    vmsPollTimer = null;
  }
}

function scheduleVmsPoll(delay) {
  if (!vmsPollAllowed()) { cancelVmsPoll(); return; }
  if (vmsPollTimer) return;   // 只保留一个定时器，完成后才排下一次
  const wait = typeof delay === 'number' ? delay : VMS_POLL_MS;
  vmsPollTimer = window.setTimeout(function () {
    vmsPollTimer = null;
    if (!vmsPollAllowed()) return;
    loadVms(state.hostId, { quiet: true, poll: true });
  }, wait);
}

async function loadVms(hostId, opts) {
  const options = opts || {};
  if (!hostId || hostId !== state.hostId) return;
  if (vmsInflight && vmsInflight.hostId === hostId) {
    // 轮询遇到在途请求直接跳过；手动刷新排队一次，绝不把在途响应作废。
    if (!options.poll) vmsRefreshQueued = true;
    return;
  }
  const g = ++gen.vms;
  vmsInflight = { hostId: hostId, gen: g };
  if (!options.quiet) {
    state.vmsLoading = true;
    state.vmsError = null;
    render();
  }
  try {
    const data = await api('GET', '/api/hosts/' + enc(hostId) + '/vms');
    if (g !== gen.vms || hostId !== state.hostId) return;   // 迟到响应丢弃
    state.vms = Array.isArray(data.vms) ? data.vms : [];
    state.vmsError = null;
  } catch (e) {
    if (g !== gen.vms || hostId !== state.hostId) return;
    if (e.status === 401) cancelVmsPoll();
    // 静默刷新失败：保留已有列表只显示错误，不覆盖其它主机的数据
    state.vmsError = e.status === 401 ? '登录已过期' : e.message;
    if (!options.quiet) state.vms = [];
  } finally {
    if (vmsInflight && vmsInflight.gen === g) vmsInflight = null;
    if (g === gen.vms && hostId === state.hostId) {
      state.vmsLoading = false;
      render();
    }
    const again = vmsRefreshQueued;
    vmsRefreshQueued = false;
    if (again) loadVms(hostId);
    scheduleVmsPoll();
  }
}

async function loadStatus(hostId) {
  if (hostId !== state.hostId) return;
  state.statusLoading = true;
  state.statusError = null;
  render();
  await checkHost(hostId, { silent: true });
  if (hostId === state.hostId) {
    state.statusLoading = false;
    render();
  }
}

async function checkHost(hostId, opts) {
  if (state.checking.has(hostId)) return;
  state.checking.add(hostId);
  render();
  try {
    const data = await api('GET', '/api/hosts/' + enc(hostId) + '/status');
    state.hostStatus[hostId] = { online: true, status: data.status || null, error: null, at: Date.now() };
    if (hostId === state.hostId) {
      state.status = data.status || null;
      state.statusError = null;
    }
  } catch (e) {
    if (e.status === 401) return;
    state.hostStatus[hostId] = { online: false, status: null, error: e.message, at: Date.now() };
    if (hostId === state.hostId) {
      state.status = null;
      state.statusError = e.message;
    }
  } finally {
    state.checking.delete(hostId);
    render();
  }
}

async function loadImages(hostId) {
  const g = ++gen.images;
  state.imagesLoading = true;
  state.imagesError = null;
  render();
  try {
    const data = await api('GET', '/api/hosts/' + enc(hostId) + '/images');
    if (g !== gen.images || hostId !== state.hostId) return;
    state.images = Array.isArray(data.images) ? data.images : [];
    state.imageDirs = Array.isArray(data.dirs) ? data.dirs : [];
    state.diskDirs = Array.isArray(data.disk_dirs) ? data.disk_dirs : [];
  } catch (e) {
    if (g !== gen.images || hostId !== state.hostId) return;
    state.images = [];
    state.imageDirs = [];
    state.diskDirs = [];
    state.imagesError = e.status === 401 ? '登录已过期' : e.message;
  } finally {
    if (g === gen.images && hostId === state.hostId) {
      state.imagesLoading = false;
      render();
    }
  }
}

/* --------------------------------------------------------------- 任务轮询 */

let pollTimer = null;

function scheduleTaskPoll(delay) {
  if (pollTimer) return;
  const wait = typeof delay === 'number' ? delay : (state.view === 'tasks' ? 3000 : 2500);
  pollTimer = window.setTimeout(function () {
    pollTimer = null;
    pollTasks();
  }, wait);
}

function needsPolling() {
  return state.view === 'tasks' || state.tasks.some(t => t.status === 'running');
}

function ingestTask(task) {
  if (!task || !task.id) return;
  const i = state.tasks.findIndex(t => t.id === task.id);
  if (i >= 0) state.tasks[i] = task;
  else state.tasks.unshift(task);
  scheduleTaskPoll(1200);
}

async function loadTasks(opts) {
  if (!state.user) return;
  state.tasksLoading = true;
  if (!opts || !opts.quiet) render();
  try {
    const data = await api('GET', '/api/tasks');
    const prev = new Map(state.tasks.map(t => [t.id, t.status]));
    state.tasks = Array.isArray(data.tasks) ? data.tasks : [];
    state.tasksError = null;
    state.tasksAt = Date.now();
    for (const t of state.tasks) {
      if (prev.get(t.id) === 'running' && t.status !== 'running') onTaskFinished(t);
    }
  } catch (e) {
    if (e.status !== 401) state.tasksError = e.message;
  } finally {
    state.tasksLoading = false;
    render();
    if (needsPolling()) scheduleTaskPoll();
  }
}

function pollTasks() {
  if (!state.user) return;
  loadTasks({ quiet: true });
}

function onTaskFinished(t) {
  if (t.status === 'success') {
    toast('任务完成：' + (t.title || t.id), { kind: 'success', taskId: t.id });
    refreshHostData(t.host_id);
  } else {
    toast('任务失败：' + (t.title || t.id) + (t.error ? '——' + t.error : ''), { kind: 'error', taskId: t.id });
    refreshHostData(t.host_id);
  }
}

function refreshHostData(hostId) {
  if (!hostId) {
    loadHosts();
    return;
  }
  if (hostId === state.hostId) {
    loadVms(hostId);
    loadStatus(hostId);
    if (state.view === 'images') loadImages(hostId);
  } else {
    checkHost(hostId, { silent: true });
  }
}

function goTasks(taskId) {
  state.view = 'tasks';
  state.highlight = taskId || null;
  lastViewKey = '';
  render();
  scheduleTaskPoll(0);
  if (taskId) {
    window.setTimeout(function () {
      const nodes = el.viewRoot.querySelectorAll('[data-testid]');
      for (const n of nodes) {
        if (n.getAttribute('data-testid') === 'task-row-' + taskId) {
          n.scrollIntoView({ block: 'center', behavior: 'smooth' });
          break;
        }
      }
    }, 60);
  }
}

/* --------------------------------------------------------------- Toast */

function toast(message, opts) {
  const o = opts || {};
  const kind = o.kind || 'info';
  let timer = null;
  const node = h('div', { class: 'toast toast--' + kind, 'data-testid': 'toast' },
    h('div', { class: 'toast__body' },
      h('span', { class: 'toast__msg' }, message),
      o.taskId ? h('div', { class: 'toast__actions' },
        btn({
          label: '查看任务', size: 'sm', variant: 'quiet', testid: 'toast-task-link',
          onClick: () => { goTasks(o.taskId); dismiss(); }
        })
      ) : null
    ),
    h('button', {
      class: 'toast__close', type: 'button', 'aria-label': '关闭通知',
      onClick: () => dismiss()
    }, icon('close', 13))
  );

  function dismiss() {
    if (!node.isConnected) return;
    if (timer) { clearTimeout(timer); timer = null; }
    node.classList.add('is-leaving');
    window.setTimeout(() => node.remove(), 220);
  }

  node.addEventListener('mouseenter', () => { if (timer) { clearTimeout(timer); timer = null; } });
  node.addEventListener('focusin', () => { if (timer) { clearTimeout(timer); timer = null; } });

  el.toastRoot.appendChild(node);
  // 最多同时 4 条
  while (el.toastRoot.children.length > 4) el.toastRoot.firstChild.remove();

  const ttl = kind === 'error' ? 10000 : 6000;
  timer = window.setTimeout(dismiss, ttl);
}

/* --------------------------------------------------------------- 弹窗 */

let activeModal = null;
let modalSeq = 0;

function openModal(opts) {
  if (activeModal) activeModal.close('replaced');

  const prevFocus = document.activeElement;
  const id = ++modalSeq;
  const testid = opts.testid || ('modal-' + id);

  const card = h('div', {
    class: 'modal__card' + (opts.wide ? ' modal__card--wide' : ''),
    role: 'dialog',
    'aria-modal': 'true',
    'aria-labelledby': testid + '-title',
    tabindex: '-1',
    'data-testid': testid
  });

  const titleEl = h('h2', { class: 'modal__title', id: testid + '-title' }, opts.title);
  const closeBtn = h('button', {
    class: 'modal__close', type: 'button', 'aria-label': '关闭', 'data-testid': testid + '-close',
    onClick: () => dlg.close('button')
  }, icon('close', 16));

  const head = h('div', { class: 'modal__head' },
    h('div', { class: 'modal__titles' },
      titleEl,
      opts.desc ? h('p', { class: 'modal__desc' }, opts.desc) : null
    ),
    closeBtn
  );

  const body = h(opts.bodyTag || 'div',
    Object.assign({ class: 'modal__body' }, opts.bodyAttrs || {}),
    opts.body || []);

  const foot = opts.footer ? h('div', { class: 'modal__foot' }, opts.footer) : null;

  card.appendChild(head);
  card.appendChild(body);
  if (foot) card.appendChild(foot);

  const wrap = h('div', { class: 'modal' },
    h('div', { class: 'modal__backdrop' }),
    card
  );

  const dlg = {
    el: wrap,
    card: card,
    body: body,
    alive: true,
    busy: false,
    host: opts.host || null,
    uuid: opts.uuid || null,
    close(reason) {
      if (!dlg.alive) return;
      dlg.alive = false;
      document.removeEventListener('keydown', onKey, true);
      wrap.remove();
      if (activeModal === dlg) activeModal = null;
      document.body.style.overflow = '';
      if (prevFocus && typeof prevFocus.focus === 'function' && prevFocus.isConnected) {
        prevFocus.focus({ preventScroll: true });
      }
      if (opts.onClose) opts.onClose(reason);
    }
  };

  function focusables() {
    const nodes = card.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
    return Array.prototype.filter.call(nodes, n => !n.disabled && n.offsetParent !== null);
  }

  function onKey(ev) {
    if (!dlg.alive) return;
    if (ev.key === 'Escape') {
      ev.stopPropagation();
      ev.preventDefault();
      dlg.close('escape');
      return;
    }
    if (ev.key !== 'Tab') return;
    const list = focusables();
    if (!list.length) { ev.preventDefault(); card.focus(); return; }
    const first = list[0];
    const last = list[list.length - 1];
    const active = document.activeElement;
    if (!card.contains(active)) { ev.preventDefault(); first.focus(); return; }
    if (!ev.shiftKey && active === last) { ev.preventDefault(); first.focus(); }
    else if (ev.shiftKey && active === first) { ev.preventDefault(); last.focus(); }
  }

  wrap.querySelector('.modal__backdrop').addEventListener('click', () => dlg.close('backdrop'));
  document.addEventListener('keydown', onKey, true);
  document.body.style.overflow = 'hidden';

  el.modalRoot.appendChild(wrap);
  activeModal = dlg;

  const autofocus = card.querySelector('[data-autofocus]') || focusables()[0] || card;
  window.setTimeout(() => { if (dlg.alive) autofocus.focus({ preventScroll: true }); }, 0);

  return dlg;
}

function fieldRow(o) {
  return h('div', { class: 'field' + (o.span2 ? ' span-2' : '') },
    h('label', { class: 'field__label', for: o.id },
      h('span', null, o.label),
      o.required ? h('span', { class: 'req', 'aria-hidden': 'true' }, '*') : null,
      o.hint ? h('span', { class: 'field__hint' }, o.hint) : null
    ),
    o.control,
    o.error ? h('span', { class: 'field__error' }, o.error) : null
  );
}

/* --------------------------------------------------------------- 主机弹窗 */

function openHostDialog(host) {
  const editing = !!host;
  const isLocal = editing && host.transport === 'local';

  const idInput = h('input', {
    class: 'input', id: 'host-id', type: 'text', 'data-autofocus': editing ? null : '1',
    'data-testid': 'host-field-id', maxlength: '32', spellcheck: 'false',
    value: editing ? host.id : '', readOnly: editing,
    placeholder: '例如 node150（小写字母开头，可含数字 _ -）'
  });
  const nameInput = h('input', {
    class: 'input', id: 'host-name', type: 'text', maxlength: '64',
    'data-testid': 'host-field-name', 'data-autofocus': editing ? '1' : null,
    value: editing ? (host.name || '') : '', placeholder: '例如 执行端 .150'
  });
  const transportSel = h('select', { class: 'select', id: 'host-transport', 'data-testid': 'host-field-transport' },
    h('option', { value: 'ssh' }, 'SSH（远程执行端）'),
    isLocal ? h('option', { value: 'local' }, '本机执行端') : null
  );
  transportSel.value = editing ? (host.transport || 'ssh') : 'ssh';
  transportSel.disabled = isLocal;

  const addressInput = h('input', {
    class: 'input', id: 'host-address', type: 'text', maxlength: '128',
    'data-testid': 'host-field-address', spellcheck: 'false',
    value: editing ? (host.address || '') : '', placeholder: '192.168.0.150'
  });
  const userInput = h('input', {
    class: 'input', id: 'host-ssh-user', type: 'text', maxlength: '32',
    'data-testid': 'host-field-ssh-user', spellcheck: 'false',
    value: editing ? (host.ssh_user || '') : 'root'
  });
  const portInput = h('input', {
    class: 'input', id: 'host-ssh-port', type: 'number', min: '1', max: '65535',
    'data-testid': 'host-field-ssh-port',
    value: editing ? String(host.ssh_port || 22) : '22'
  });

  const errorBox = h('p', { class: 'field__error', role: 'alert', 'data-testid': 'host-dialog-error', hidden: true });

  function applyLocalLock() {
    const local = transportSel.value === 'local';
    addressInput.disabled = local;
    userInput.disabled = local;
    portInput.disabled = local;
    if (local) {
      addressInput.value = host.address;
      userInput.value = host.ssh_user;
      portInput.value = String(host.ssh_port);
    }
  }
  transportSel.addEventListener('change', applyLocalLock);
  applyLocalLock();

  const form = h('form', {
    id: 'host-form', novalidate: true,
    onSubmit: (ev) => { ev.preventDefault(); submit(); }
  },
    h('div', { class: 'form-grid' },
      fieldRow({
        id: 'host-id', label: '主机 ID', required: true, span2: false,
        hint: editing ? '（不可修改）' : null, control: idInput
      }),
      fieldRow({ id: 'host-name', label: '显示名称', required: true, control: nameInput }),
      fieldRow({ id: 'host-transport', label: '连接方式', required: true, control: transportSel }),
      fieldRow({
        id: 'host-address', label: '地址', required: true,
        hint: isLocal ? '' : null, control: addressInput
      }),
      fieldRow({ id: 'host-ssh-user', label: 'SSH 用户', control: userInput }),
      fieldRow({ id: 'host-ssh-port', label: 'SSH 端口', control: portInput })
    ),
    h('p', { class: 'field__hint' },
      isLocal
        ? '本机连接信息保持不变，可修改显示名称。'
        : '保存只写入登记信息，不校验连通性；保存后请用「检测」确认在线状态。'),
    errorBox
  );

  const submitBtn = btn({
    label: editing ? '保存修改' : '登记主机', type: 'submit', variant: 'primary',
    testid: 'host-dialog-submit'
  });
  submitBtn.setAttribute('form', 'host-form');

  const dlg = openModal({
    title: editing ? '编辑主机' : '新增主机',
    desc: editing ? ('修改 ' + (host.name || host.id) + ' 的登记信息') : '登记一台执行端；不改动远端任何配置。',
    testid: 'host-dialog',
    body: [form],
    footer: [
      h('span', { class: 'foot-note' }, '保存后可检测主机连接'),
      h('span', { class: 'spacer' }),
      btn({ label: '取消', onClick: () => dlg.close('cancel') }),
      submitBtn
    ]
  });

  async function submit() {
    if (dlg.busy) return;
    const id = (editing ? host.id : idInput.value.trim());
    const name = nameInput.value.trim();
    const transport = transportSel.value;
    const address = addressInput.value.trim();
    const sshUser = userInput.value.trim();
    const sshPort = Number(portInput.value);

    const problems = [];
    if (!id) problems.push('主机 ID 必填');
    else if (!editing && !/^[a-z][a-z0-9_-]{0,31}$/.test(id)) problems.push('主机 ID 须以小写字母开头，可含数字、下划线、连字符');
    if (!name) problems.push('显示名称必填');
    if (transport === 'ssh') {
      if (!address) problems.push('地址必填');
      if (!sshUser) problems.push('SSH 用户必填');
      if (!(sshPort >= 1 && sshPort <= 65535)) problems.push('SSH 端口需在 1-65535 之间');
    }
    if (problems.length) {
      errorBox.hidden = false;
      errorBox.textContent = problems.join('；');
      return;
    }

    const payload = {
      id,
      name,
      address: isLocal ? host.address : address,
      transport,
      ssh_user: isLocal ? host.ssh_user : sshUser,
      ssh_port: isLocal ? host.ssh_port : sshPort
    };

    dlg.busy = true;
    submitBtn.disabled = true;
    submitBtn.replaceChildren(h('span', { class: 'btn__spinner', 'aria-hidden': 'true' }), h('span', null, '保存中…'));
    errorBox.hidden = true;

    try {
      if (editing) await api('PUT', '/api/hosts/' + enc(host.id), payload);
      else await api('POST', '/api/hosts', payload);
      if (!dlg.alive) return;
      dlg.close('saved');
      toast(editing ? '已保存主机登记' : '已登记主机 ' + id, { kind: 'success' });
      loadHosts();
      if (!editing) {
        state.hostId = null;   // 让 loadHosts 重新挑选
        selectHost(id, { force: true });
      } else if (state.hostId === id) {
        loadStatus(id);
      }
    } catch (e) {
      if (!dlg.alive) return;
      errorBox.hidden = false;
      errorBox.textContent = e.message;
    } finally {
      dlg.busy = false;
      if (dlg.alive) {
        submitBtn.disabled = false;
        submitBtn.replaceChildren(h('span', null, editing ? '保存修改' : '登记主机'));
      }
    }
  }
}

function openDeleteHostDialog(host) {
  const dlg = openModal({
    title: '解除纳管',
    desc: '只删除本平台的主机登记，不会停止、删除或修改该主机上的任何虚拟机。',
    testid: 'host-delete-dialog',
    body: [
      h('dl', { class: 'target-box' },
        h('dt', null, '主机'), h('dd', null, (host.name || host.id) + '（' + host.id + '）'),
        h('dt', null, '连接'), h('dd', null, transportLabel(host)),
        h('dt', null, '影响'), h('dd', null, '仅移除登记记录；远端虚机与磁盘保持不变')
      ),
      h('p', { class: 'field__error', 'data-testid': 'host-delete-error', hidden: true })
    ],
    footer: []
  });
  const errorEl = dlg.card.querySelector('[data-testid="host-delete-error"]');
  const confirmBtn = btn({
    label: '解除纳管', variant: 'danger', testid: 'host-delete-confirm',
    onClick: async () => {
      confirmBtn.disabled = true;
      confirmBtn.replaceChildren(h('span', { class: 'btn__spinner', 'aria-hidden': 'true' }), h('span', null, '处理中…'));
      try {
        await api('DELETE', '/api/hosts/' + enc(host.id));
        dlg.close('done');
        toast('已解除纳管：' + (host.name || host.id), { kind: 'success' });
        if (state.hostId === host.id) {
          state.hostId = null;
          state.vms = [];
          state.status = null;
          state.hostStatus[host.id] = undefined;
        }
        loadHosts();
      } catch (e) {
        if (!dlg.alive) return;
        errorEl.hidden = false;
        errorEl.textContent = e.message;
        confirmBtn.disabled = false;
        confirmBtn.replaceChildren(h('span', null, '解除纳管'));
      }
    }
  });
  dlg.card.querySelector('.modal__foot').append(
    h('span', { class: 'foot-note' }, '有任务执行中的主机会被服务端拒绝'),
    h('span', { class: 'spacer' }),
    btn({ label: '取消', onClick: () => dlg.close('cancel') }),
    confirmBtn
  );
}

/* --------------------------------------------------------------- 虚拟机动作 */

function captureVmRequest(hostId, vm) {
  return { host: hostId, hostName: hostLabel(hostId), uuid: vm.uuid, name: vm.name || vm.uuid };
}

async function postVmAction(target, action, extra) {
  const body = Object.assign({ action: action, remove_disks: false, force: false }, extra || {});
  return api('POST', '/api/hosts/' + enc(target.host) + '/vms/' + enc(target.uuid) + '/action', body);
}

function runVmAction(hostId, vm, action) {
  const target = captureVmRequest(hostId, vm);
  if (action === 'delete') return openDeleteVmDialog(hostId, vm);
  if (action === 'stop') return openStopVmDialog(hostId, vm);
  fireVmAction(target, action);
}

async function fireVmAction(target, action, extra) {
  const key = target.host + ':' + target.uuid;
  if (state.busy.has(key)) return;
  state.busy.add(key);
  lastViewKey = '';
  render();
  try {
    const data = await postVmAction(target, action, extra);
    if (data && data.task) {
      ingestTask(data.task);
      toast('已提交：' + (data.task.title || action), { kind: 'info', taskId: data.task.id });
    } else {
      toast('已提交动作：' + action, { kind: 'info' });
    }
  } catch (e) {
    if (e.status !== 401) toast('动作失败：' + e.message, { kind: 'error' });
  } finally {
    state.busy.delete(key);
    lastViewKey = '';
    render();
  }
}

function openStopVmDialog(hostId, vm) {
  const target = captureVmRequest(hostId, vm);
  const dlg = openModal({
    title: '强制停止虚拟机',
    desc: '相当于直接断电，guest 内未落盘的数据可能丢失。',
    testid: 'vm-stop-dialog',
    body: [
      h('dl', { class: 'target-box' },
        h('dt', null, '主机'), h('dd', null, target.hostName + '（' + target.host + '）'),
        h('dt', null, '虚拟机'), h('dd', null, target.name),
        h('dt', null, 'UUID'), h('dd', { class: 'mono' }, target.uuid)
      ),
      notice({ kind: 'note', title: '建议先关机', message: '优先使用「关机」发送 ACPI 请求，确认无响应后再强制停止。' })
    ],
    footer: [
      h('span', { class: 'spacer' }),
      btn({ label: '取消', onClick: () => dlg.close('cancel') }),
      btn({
        label: '强制停止', variant: 'danger', testid: 'vm-stop-confirm',
        onClick: () => { dlg.close('confirm'); fireVmAction(target, 'stop', { force: true }); }
      })
    ]
  });
}

function openDeleteVmDialog(hostId, vm) {
  const target = captureVmRequest(hostId, vm);
  const diskCheck = h('input', { type: 'checkbox', id: 'remove-disks', 'data-testid': 'vm-delete-remove-disks' });
  const errorBox = h('p', { class: 'field__error', role: 'alert', 'data-testid': 'vm-delete-error', hidden: true });

  const confirmBtn = btn({
    label: '删除虚拟机', variant: 'danger', testid: 'vm-delete-confirm',
    onClick: async () => {
      if (confirmBtn.disabled) return;
      confirmBtn.disabled = true;
      confirmBtn.replaceChildren(h('span', { class: 'btn__spinner', 'aria-hidden': 'true' }), h('span', null, '提交中…'));
      errorBox.hidden = true;
      try {
        const data = await postVmAction(target, 'delete', { remove_disks: !!diskCheck.checked });
        if (data && data.task) {
          ingestTask(data.task);
          toast('已提交删除：' + target.name, { kind: 'info', taskId: data.task.id });
        }
        dlg.close('done');
      } catch (e) {
        if (!dlg.alive) return;
        errorBox.hidden = false;
        errorBox.textContent = e.message;
        confirmBtn.disabled = false;
        confirmBtn.replaceChildren(h('span', null, '删除虚拟机'));
      }
    }
  });

  const dlg = openModal({
    title: '删除虚拟机',
    desc: '删除前请确认目标主机与 UUID；默认保留磁盘。',
    testid: 'vm-delete-dialog',
    host: hostId,
    uuid: vm.uuid,
    body: [
      h('dl', { class: 'target-box' },
        h('dt', null, '主机'), h('dd', null, target.hostName + '（' + target.host + '）'),
        h('dt', null, '虚拟机'), h('dd', null, target.name),
        h('dt', null, 'UUID'), h('dd', { class: 'mono' }, target.uuid),
        h('dt', null, '状态'), h('dd', null, vmStateMeta(vm).label)
      ),
      h('label', { class: 'check', for: 'remove-disks' },
        diskCheck,
        h('span', { class: 'check__text' },
          h('span', null, '同时删除磁盘'),
          h('span', { class: 'check__hint' }, '勾选后虚拟机磁盘文件一并删除，不可恢复；不勾选仅移除定义。')
        )
      ),
      errorBox
    ],
    footer: [
      h('span', { class: 'foot-note' }, '需要 202 任务成功后才真正移除'),
      h('span', { class: 'spacer' }),
      btn({ label: '取消', onClick: () => dlg.close('cancel') }),
      confirmBtn
    ]
  });
}

/* --------------------------------------------------------------- 配置弹窗 */

function openConfigDialog(hostId, vm) {
  const cfg = vm.config || {};
  const curCpus = num(cfg.vcpus, num(vm.cpu, 1));
  const curMem = num(cfg.memory_gb, vm.max_memory ? Math.round(vm.max_memory / 1024 / 1024) : 1);
  const curDisk = num(cfg.primary_disk_gb, Array.isArray(vm.disks) && vm.disks.length ? 1 : 0);
  const f = vmFlags(vm);
  const target = captureVmRequest(hostId, vm);

  const cpusInput = h('input', {
    class: 'input', id: 'cfg-vcpus', type: 'number', min: String(curCpus), max: '128', step: '1',
    'data-testid': 'vm-config-vcpus', value: String(curCpus), 'data-autofocus': '1'
  });
  const memInput = h('input', {
    class: 'input', id: 'cfg-memory', type: 'number', min: String(curMem), max: '2048', step: '1',
    'data-testid': 'vm-config-memory', value: String(curMem)
  });
  const diskInput = h('input', {
    class: 'input', id: 'cfg-disk', type: 'number', min: String(curDisk), max: '8192', step: '1',
    'data-testid': 'vm-config-disk', value: String(curDisk)
  });
  const errorBox = h('p', { class: 'field__error', role: 'alert', 'data-testid': 'vm-config-error', hidden: true });

  const form = h('form', { id: 'config-form', novalidate: true, onSubmit: (ev) => { ev.preventDefault(); submit(); } },
    h('dl', { class: 'target-box' },
      h('dt', null, '主机'), h('dd', null, target.hostName + '（' + target.host + '）'),
      h('dt', null, '虚拟机'), h('dd', null, target.name),
      h('dt', null, 'UUID'), h('dd', { class: 'mono' }, target.uuid)
    ),
    f.canConfig ? null : notice({
      kind: 'note', title: '需要关机后修改',
      message: '当前状态为「' + vmStateMeta(vm).label + '」。配置变更只能在关机状态提交，请先关机。'
    }),
    h('div', { class: 'form-grid' },
      fieldRow({
        id: 'cfg-vcpus', label: 'vCPU', required: true,
        hint: '当前 ' + curCpus, control: cpusInput
      }),
      fieldRow({
        id: 'cfg-memory', label: '内存（GiB）', required: true,
        hint: '当前 ' + curMem + ' GiB', control: memInput
      }),
      fieldRow({
        id: 'cfg-disk', label: '主磁盘（GiB）', required: true, span2: true,
        hint: '当前 ' + (curDisk || '—') + ' GiB · 只能扩大，禁止缩小',
        control: diskInput
      })
    ),
    h('p', { class: 'field__hint' }, '缩小 vCPU / 内存 / 磁盘会被前端拦截，服务端同样拒绝；磁盘扩容默认不改动 guest 内文件系统。'),
    errorBox
  );

  const submitBtn = btn({
    label: '提交配置', type: 'submit', variant: 'primary', testid: 'vm-config-submit',
    disabled: !f.canConfig
  });
  submitBtn.setAttribute('form', 'config-form');

  const dlg = openModal({
    title: '调整配置',
    desc: '仅关机状态可提交，返回 202 任务后由任务页收敛。',
    testid: 'vm-config-dialog',
    host: hostId,
    uuid: vm.uuid,
    body: [form],
    footer: [
      h('span', { class: 'foot-note' }, '禁止缩小'),
      h('span', { class: 'spacer' }),
      btn({ label: '取消', onClick: () => dlg.close('cancel') }),
      submitBtn
    ]
  });

  function fail(msg, field) {
    errorBox.hidden = false;
    errorBox.textContent = msg;
    [cpusInput, memInput, diskInput].forEach(function (inp) { inp.removeAttribute('aria-invalid'); });
    if (field) {
      field.setAttribute('aria-invalid', 'true');
      field.focus({ preventScroll: true });
    }
    return false;
  }

  async function submit() {
    if (dlg.busy || !f.canConfig) return;
    errorBox.hidden = true;

    const cpus = Number(cpusInput.value);
    const mem = Number(memInput.value);
    const disk = Number(diskInput.value);

    if (!Number.isInteger(cpus) || cpus < 1 || cpus > 128) return fail('vCPU 需为 1-128 的整数', cpusInput);
    if (!Number.isInteger(mem) || mem < 1 || mem > 2048) return fail('内存需为 1-2048 的整数（GiB）', memInput);
    if (!Number.isInteger(disk) || disk < 1) return fail('磁盘需为大于 0 的整数（GiB）', diskInput);
    if (cpus < curCpus) return fail('vCPU 不能小于当前值 ' + curCpus, cpusInput);
    if (mem < curMem) return fail('内存不能小于当前值 ' + curMem + ' GiB', memInput);
    if (curDisk && disk < curDisk) return fail('磁盘不能小于当前值 ' + curDisk + ' GiB', diskInput);

    dlg.busy = true;
    submitBtn.disabled = true;
    submitBtn.replaceChildren(h('span', { class: 'btn__spinner', 'aria-hidden': 'true' }), h('span', null, '提交中…'));

    try {
      const data = await api('POST', '/api/hosts/' + enc(hostId) + '/vms/' + enc(vm.uuid) + '/config', {
        vcpus: cpus, memory_gb: mem, disk_gb: disk, resize_guest: false
      });
      if (!dlg.alive) return;
      if (data && data.task) {
        ingestTask(data.task);
        toast('已提交配置任务：' + target.name, { kind: 'info', taskId: data.task.id });
      } else {
        toast('已提交配置变更', { kind: 'info' });
      }
      dlg.close('done');
    } catch (e) {
      if (!dlg.alive) return;
      fail(e.message);
    } finally {
      dlg.busy = false;
      if (dlg.alive) {
        submitBtn.disabled = !f.canConfig;
        submitBtn.replaceChildren(h('span', null, '提交配置'));
      }
    }
  }
}

/* --------------------------------------------------------------- 创建弹窗 */

/* --------------------------------------------------------------- 修改密码 */

function openPasswordDialog() {
  const currentInput = h('input', {
    class: 'input', id: 'password-current', type: 'password', autocomplete: 'current-password',
    'data-testid': 'password-current', 'data-autofocus': '1', spellcheck: 'false'
  });
  const newInput = h('input', {
    class: 'input', id: 'password-new', type: 'password', autocomplete: 'new-password',
    'data-testid': 'password-new', spellcheck: 'false'
  });
  const confirmInput = h('input', {
    class: 'input', id: 'password-confirm', type: 'password', autocomplete: 'new-password',
    'data-testid': 'password-confirm', spellcheck: 'false'
  });
  const errorBox = h('p', { class: 'field__error', role: 'alert', 'data-testid': 'password-error', hidden: true });

  const form = h('form', { id: 'password-form', novalidate: true, onSubmit: (ev) => { ev.preventDefault(); submit(); } },
    h('div', { class: 'form-grid' },
      fieldRow({ id: 'password-current', label: '当前密码', required: true, control: currentInput }),
      fieldRow({ id: 'password-new', label: '新密码', required: true, hint: '12 到 200 字符', control: newInput }),
      fieldRow({ id: 'password-confirm', label: '确认新密码', required: true, control: confirmInput })
    ),
    errorBox
  );

  const submitBtn = btn({ label: '修改密码', type: 'submit', variant: 'primary', testid: 'password-submit' });
  submitBtn.setAttribute('form', 'password-form');

  const dlg = openModal({
    title: '修改管理员密码',
    desc: '修改成功后所有已登录会话都会失效，需要使用新密码重新登录。',
    testid: 'password-dialog',
    body: [form],
    footer: [
      h('span', { class: 'foot-note' }, '密码只提交到当前控制器，不写入浏览器存储或任务记录'),
      h('span', { class: 'spacer' }),
      btn({ label: '取消', onClick: () => dlg.close('cancel') }),
      submitBtn
    ],
    onClose: () => {
      // 关闭即清空，避免密码残留在 DOM。
      currentInput.value = '';
      newInput.value = '';
      confirmInput.value = '';
    }
  });

  function fail(msg, field) {
    errorBox.hidden = false;
    errorBox.textContent = msg;
    [currentInput, newInput, confirmInput].forEach(function (inp) {
      inp.removeAttribute('aria-invalid');
    });
    if (field) {
      field.setAttribute('aria-invalid', 'true');
      field.focus({ preventScroll: true });
    } else {
      errorBox.scrollIntoView({ block: 'nearest' });
    }
  }

  async function submit() {
    if (dlg.busy) return;
    errorBox.hidden = true;

    const current = currentInput.value;
    const next = newInput.value;
    const confirm = confirmInput.value;
    if (!current) return fail('请输入当前密码', currentInput);
    if (next.length < 12 || next.length > 200) return fail('新密码长度须为 12 到 200 字符', newInput);
    if (next !== confirm) return fail('两次输入的新密码不一致', confirmInput);

    dlg.busy = true;
    submitBtn.disabled = true;
    submitBtn.replaceChildren(h('span', { class: 'btn__spinner', 'aria-hidden': 'true' }), h('span', null, '提交中…'));

    try {
      await api('POST', '/api/password', { current_password: current, password: next });
      currentInput.value = '';
      newInput.value = '';
      confirmInput.value = '';
      dlg.busy = false;
      submitBtn.disabled = false;
      dlg.close('done');
      resetHostState();
      state.view = 'hosts';
      showLogin('密码已修改，请使用新密码重新登录；其他会话已全部失效。');
    } catch (e) {
      dlg.busy = false;
      submitBtn.disabled = false;
      submitBtn.replaceChildren(h('span', null, '修改密码'));
      if (!dlg.alive) return;
      fail(e.message || '修改密码失败');
    }
  }
}

function openCreateDialog() {
  const hostId = state.hostId;
  if (!hostId) return;
  const hostName = hostLabel(hostId);

  const errorBox = h('p', { class: 'field__error', role: 'alert', 'data-testid': 'vm-create-error', hidden: true });
  const loadBox = h('div', { 'data-testid': 'vm-create-loading' },
    notice({ kind: 'info', title: '正在读取镜像、存储与网络', message: '目标主机：' + hostName })
  );

  const nameInput = h('input', {
    class: 'input', id: 'new-name', type: 'text', maxlength: '63', 'data-autofocus': '1',
    'data-testid': 'vm-create-name', spellcheck: 'false', placeholder: '例如 temp-ubuntu-01'
  });

  const sourceSel = h('select', { class: 'select', id: 'new-source', 'data-testid': 'vm-create-source' },
    h('option', { value: '' }, '加载中…'));
  const sourceHint = h('span', { class: 'field__hint', 'data-testid': 'vm-create-source-hint' }, '');

  const diskDirSel = h('select', { class: 'select', id: 'new-disk-dir', 'data-testid': 'vm-create-disk-dir' },
    h('option', { value: '' }, '加载中…'));

  const cpusInput = h('input', {
    class: 'input', id: 'new-vcpus', type: 'number', min: '1', max: '128', step: '1', value: '2',
    'data-testid': 'vm-create-vcpus'
  });
  const memInput = h('input', {
    class: 'input', id: 'new-memory', type: 'number', min: '1', max: '2048', step: '1', value: '2',
    'data-testid': 'vm-create-memory'
  });
  const diskInput = h('input', {
    class: 'input', id: 'new-disk', type: 'number', min: '1', max: '8192', step: '1', value: '20',
    'data-testid': 'vm-create-disk'
  });

  const netTypeSel = h('select', { class: 'select', id: 'new-network-type', 'data-testid': 'vm-create-network-type' },
    h('option', { value: 'network' }, 'NAT / libvirt 网络'),
    h('option', { value: 'bridge' }, '桥接网卡'));
  const netSourceSel = h('select', { class: 'select', id: 'new-network-source', 'data-testid': 'vm-create-network-source' });
  const netHint = h('span', { class: 'field__hint', 'data-testid': 'vm-create-network-hint' }, '');

  const ciUser = h('input', { class: 'input', id: 'new-ci-user', type: 'text', value: 'ubuntu', maxlength: '32', 'data-testid': 'vm-create-ci-user' });
  const ciPass = h('input', { class: 'input', id: 'new-ci-pass', type: 'password', value: '', autocomplete: 'new-password', 'data-testid': 'vm-create-ci-password' });
  const ciKey = h('textarea', { class: 'textarea', id: 'new-ci-key', 'data-testid': 'vm-create-ci-key', placeholder: 'ssh-ed25519 AAAA… 可选' });
  const ciDhcp = h('input', { type: 'checkbox', checked: true, id: 'new-ci-dhcp', 'data-testid': 'vm-create-ci-dhcp' });
  const ciEnabled = h('input', { type: 'checkbox', checked: true, id: 'new-ci-enabled', 'data-testid': 'vm-create-ci-enabled' });
  const ciIsolated = h('input', { type: 'checkbox', checked: false, id: 'new-ci-isolated', 'data-testid': 'vm-create-ci-isolated' });
  const ciIp = h('input', {
    class: 'input', id: 'new-ci-ip', type: 'text', spellcheck: 'false',
    'data-testid': 'vm-create-ci-ip', placeholder: '192.168.0.201/24'
  });
  const ciGateway = h('input', {
    class: 'input', id: 'new-ci-gateway', type: 'text', spellcheck: 'false',
    'data-testid': 'vm-create-ci-gateway', placeholder: '192.168.0.1'
  });
  const ciDns = h('input', {
    class: 'input', id: 'new-ci-dns', type: 'text', spellcheck: 'false',
    'data-testid': 'vm-create-ci-dns', placeholder: '192.168.0.1, 8.8.8.8'
  });
  const ciStaticHint = h('span', { class: 'field__hint', 'data-testid': 'vm-create-ci-static-hint' }, '');
  const ciStaticBox = h('div', { class: 'form-grid span-2', 'data-testid': 'vm-create-ci-static', style: { display: 'none' } },
    fieldRow({ id: 'new-ci-ip', label: 'IP 地址／前缀', required: true, hint: '虚拟机地址，勿填宿主机 IP', control: ciIp }),
    h('div', { class: 'field span-2' },
      h('label', { class: 'check', for: 'new-ci-isolated' }, ciIsolated,
        h('span', { class: 'check__text' },
          h('span', null, '仅同网段内网（不配置网关和 DNS）'),
          h('span', { class: 'check__hint' }, '勾选后不写入默认路由和 DNS，虚拟机只能访问同网段，无法访问其他网段或互联网。')
        ))),
    fieldRow({ id: 'new-ci-gateway', label: '网关（必填）', required: true, control: ciGateway }),
    fieldRow({ id: 'new-ci-dns', label: 'DNS（必填）', span2: true, required: true, hint: 'IPv4，多个用逗号或空格分隔', control: ciDns }),
    h('div', { class: 'field span-2' }, ciStaticHint)
  );

  const ciBlock = h('div', { class: 'form-section' },
    h('span', { class: 'form-section__title' }, 'Cloud-init', h('span', { class: 'hint' }, '仅 disk 类型生效')),
    h('label', { class: 'check', for: 'new-ci-enabled' }, ciEnabled,
      h('span', { class: 'check__text' },
        h('span', null, '启用 cloud-init 初始化'),
        h('span', { class: 'check__hint' }, '注入用户名、密码或 SSH 公钥；不启用则使用镜像内既有账户。')
      )),
    h('div', { class: 'ci-fields form-grid' },
      fieldRow({ id: 'new-ci-user', label: '用户名', control: ciUser }),
      fieldRow({ id: 'new-ci-pass', label: '密码', control: ciPass }),
      fieldRow({ id: 'new-ci-key', label: 'SSH 公钥', span2: true, control: ciKey }),
      h('div', { class: 'field span-2' },
        h('label', { class: 'check', for: 'new-ci-dhcp' }, ciDhcp,
          h('span', { class: 'check__text' },
            h('span', null, '使用 DHCP 自动获取地址'),
            h('span', { class: 'check__hint' }, '仅桥接网络可关闭 DHCP 并手工指定静态 IPv4；NAT / libvirt 网络固定使用 DHCP。')
          ))
      ),
      ciStaticBox
    )
  );

  const kindState = { kind: 'disk' };
  const kindDiskBtn = h('button', { class: 'seg__btn', type: 'button', 'aria-pressed': 'true', 'data-testid': 'vm-create-kind-disk' }, '磁盘镜像');
  const kindIsoBtn = h('button', { class: 'seg__btn', type: 'button', 'aria-pressed': 'false', 'data-testid': 'vm-create-kind-iso' }, 'ISO 安装');

  const form = h('form', { id: 'create-form', novalidate: true, onSubmit: (ev) => { ev.preventDefault(); submit(); } },
    loadBox,
    h('div', { class: 'form-section' },
      h('span', { class: 'form-section__title' }, '来源'),
      h('div', { class: 'seg', role: 'group', 'aria-label': '虚拟机来源类型' }, kindDiskBtn, kindIsoBtn),
      h('div', { class: 'form-grid' },
        fieldRow({ id: 'new-name', label: '名称', required: true, control: nameInput }),
        fieldRow({ id: 'new-source', label: '镜像', required: true, control: sourceSel }),
        h('div', { class: 'field span-2' }, sourceHint)
      )
    ),
    h('div', { class: 'form-section' },
      h('span', { class: 'form-section__title' }, '资源'),
      h('div', { class: 'form-grid' },
        fieldRow({ id: 'new-vcpus', label: 'vCPU', required: true, control: cpusInput }),
        fieldRow({ id: 'new-memory', label: '内存（GiB）', required: true, control: memInput }),
        fieldRow({ id: 'new-disk', label: '磁盘（GiB）', required: true, control: diskInput }),
        fieldRow({ id: 'new-disk-dir', label: '磁盘目录', required: true, control: diskDirSel })
      )
    ),
    h('div', { class: 'form-section' },
      h('span', { class: 'form-section__title' }, '网络'),
      h('div', { class: 'form-grid' },
        fieldRow({ id: 'new-network-type', label: '网络模式', required: true, control: netTypeSel }),
        fieldRow({ id: 'new-network-source', label: '网络来源', required: true, control: netSourceSel }),
        h('div', { class: 'field span-2' }, netHint)
      )
    ),
    ciBlock,
    errorBox
  );

  const submitBtn = btn({
    label: '创建并提交任务', type: 'submit', variant: 'primary', testid: 'vm-create-submit'
  });
  submitBtn.setAttribute('form', 'create-form');

  const dlg = openModal({
    title: '创建虚拟机',
    desc: '目标主机：' + hostName + '（' + hostId + '）；创建进度将在任务页显示。',
    testid: 'vm-create-dialog',
    wide: true,
    host: hostId,
    body: [form],
    footer: [
      h('span', { class: 'foot-note' }, '磁盘与 ISO 只允许执行端白名单目录'),
      h('span', { class: 'spacer' }),
      btn({ label: '取消', onClick: () => dlg.close('cancel') }),
      submitBtn
    ],
    onClose: (reason) => {
      if (reason === 'cancel' || reason === 'escape' || reason === 'backdrop') { /* 输入不持久化 */ }
    }
  });

  // 该弹窗固定捕获 hostId，后续切主机不影响提交目标
  let netData = null;
  let imagesData = [];
  let diskDirsData = [];
  let autoGateway = '';
  let autoDns = '';

  function syncKind() {
    kindDiskBtn.setAttribute('aria-pressed', String(kindState.kind === 'disk'));
    kindIsoBtn.setAttribute('aria-pressed', String(kindState.kind === 'iso'));
    fillSources();
    ciBlock.style.display = kindState.kind === 'iso' ? 'none' : '';
    syncCiNetwork();
  }
  kindDiskBtn.addEventListener('click', () => { kindState.kind = 'disk'; syncKind(); });
  kindIsoBtn.addEventListener('click', () => { kindState.kind = 'iso'; syncKind(); });
  ciEnabled.addEventListener('change', () => syncKind());
  ciDhcp.addEventListener('change', () => syncCiNetwork());
  ciIsolated.addEventListener('change', () => syncCiNetwork());
  ciIp.addEventListener('input', () => fillStaticDefaults());
  netSourceSel.addEventListener('change', () => syncCiNetwork());
  netTypeSel.addEventListener('change', () => renderNetworkSources(netTypeSel.value));
  syncCiNetwork();

  function fillSources() {
    if (!sourceSel.isConnected) return;
    const previousSource = sourceSel.value;
    const want = kindState.kind;
    const list = imagesData.filter(img => String(img.type || '').toLowerCase() === want);
    const opts = [];
    if (!list.length) {
      opts.push(h('option', { value: '' }, want === 'iso' ? '（没有 ISO 镜像）' : '（没有磁盘镜像）'));
    } else {
      for (const img of list) {
        const meta = img.metadata || {};
        const suffix = [meta.os_family, meta.os_version].filter(Boolean).join(' ');
        opts.push(h('option', { value: img.path || img.name },
          img.name + (suffix ? ' · ' + suffix : '') + (img.size_human ? ' · ' + img.size_human : '')));
      }
    }
    sourceSel.replaceChildren(...opts);
    sourceSel.value = Array.prototype.some.call(sourceSel.options, option => option.value === previousSource)
      ? previousSource : (opts.length ? opts[0].value : '');
    sourceHint.textContent = want === 'iso'
      ? 'ISO 安装：挂载 ISO 并从光驱启动，系统由安装器接管。'
      : '磁盘镜像：复制模板为磁盘并可直接启动；可配合 cloud-init 初始化账户。';
  }

  function renderNetworkSources(type) {
    if (!netSourceSel.isConnected) return;
    const nets = netData || { bridges: [], libvirt_networks: [], default_bridge: '', default_type: 'network', default_source: '', static_network_defaults: {} };
    const mode = type === 'bridge' ? 'bridge' : 'network';
    const opts = [];
    if (mode === 'bridge') {
      for (const b of nets.bridges || []) opts.push(h('option', { value: b }, '网桥 ' + b));
    } else {
      for (const n of nets.libvirt_networks || []) {
        opts.push(h('option', { value: n.name }, 'libvirt ' + n.name + (n.state ? '（' + n.state + '）' : '')));
      }
    }
    const hasSource = !!opts.length;
    if (!hasSource) {
      opts.push(h('option', { value: '' },
        mode === 'bridge' ? '（该主机没有可用网桥）' : '（该主机没有可用 libvirt 网络）'));
    }
    netSourceSel.replaceChildren(...opts);
    const want = mode === 'bridge'
      ? (nets.default_bridge || nets.default_source || '')
      : (nets.default_network || (nets.default_type === 'network' ? nets.default_source : '') || '');
    const has = Array.prototype.some.call(netSourceSel.options, o => o.value === want && o.value !== '');
    netSourceSel.value = has ? want : opts[0].value;
    netSourceSel.disabled = !hasSource;
    netHint.textContent = mode === 'bridge'
      ? (hasSource
        ? '桥接：虚拟机直接接入物理网桥；可关闭 DHCP 并指定静态 IPv4。'
        : '该主机没有可用网桥，无法使用桥接；请改用 NAT / libvirt 网络。')
      : (hasSource
        ? 'NAT / libvirt 网络：地址由 libvirt 的 DHCP 分配。'
        : '该主机没有可用 libvirt 网络，无法创建虚拟机。');
    syncCiNetwork();
  }

  function fillNetworks() {
    if (!netSourceSel.isConnected) return;
    const nets = netData || { bridges: [], libvirt_networks: [], default_bridge: '', default_type: 'network', default_source: '', static_network_defaults: {} };
    const defaultType = nets.default_type === 'bridge' ? 'bridge' : 'network';
    netTypeSel.value = defaultType;
    renderNetworkSources(defaultType);
  }

  // 自动默认值只在「当前网桥 profile 覆盖该 IP，且 profile 网关落在 guest 网段」时带入；
  // 记住上次自动填入的内容，从而不覆盖用户手填的值。

  function ipv4ToInt(text) {
    let value = 0;
    for (const part of String(text || '').split('.')) value = ((value << 8) + Number(part)) >>> 0;
    return value;
  }

  function parseIpv4Cidr(text) {
    const parts = String(text || '').trim().split('/');
    if (parts.length !== 2 || !isIpv4(parts[0]) || !/^\d{1,2}$/.test(parts[1])) return null;
    const prefix = Number(parts[1]);
    if (prefix < 1 || prefix > 30) return null;
    const mask = (0xFFFFFFFF << (32 - prefix)) >>> 0;
    return { base: (ipv4ToInt(parts[0]) & mask) >>> 0, mask: mask };
  }

  function ipInCidr(cidr, ip) {
    return !!cidr && ((ip & cidr.mask) >>> 0) === cidr.base;
  }

  function currentProfile() {
    if (netTypeSel.value !== 'bridge') return null;
    const map = (netData && netData.static_network_defaults) || {};
    const name = netSourceSel.value;
    return (name && Object.prototype.hasOwnProperty.call(map, name)) ? map[name] : null;
  }

  function fillStaticDefaults() {
    const guest = parseIpv4Cidr(ciIp.value);
    if (!guest) return;   // IP 未填成合法 CIDR：保持现状，不做任何猜测
    const profile = currentProfile();
    const profileNet = profile ? parseIpv4Cidr(profile.cidr) : null;
    const gatewayIp = profile && isIpv4(profile.gateway) ? ipv4ToInt(profile.gateway) : -1;
    const matched = !!profileNet && ipInCidr(profileNet, ipv4ToInt(String(ciIp.value).split('/')[0]))
      && gatewayIp >= 0 && ipInCidr(guest, gatewayIp);
    const gatewayValue = ciGateway.value.trim();
    const dnsValue = ciDns.value.trim();
    if (!matched) {
      // 切换 IP / 网桥后不再匹配：只清掉上次自动填的值，手填值原样保留
      if (autoGateway && gatewayValue === autoGateway) ciGateway.value = '';
      if (autoDns && dnsValue === autoDns) ciDns.value = '';
      autoGateway = '';
      autoDns = '';
      return;
    }
    if (!gatewayValue || gatewayValue === autoGateway) {
      ciGateway.value = profile.gateway;
      autoGateway = profile.gateway;
    }
    const dnsText = Array.isArray(profile.dns) ? profile.dns.join(', ') : '';
    if (dnsText && (!dnsValue || dnsValue === autoDns)) {
      ciDns.value = dnsText;
      autoDns = dnsText;
    }
  }

  function syncCiNetwork() {
    // 静态 IP 仅在 disk + cloud-init + 桥接时可用；其余组合固定 DHCP，且不发送静态 flag。
    const allowed = kindState.kind === 'disk' && ciEnabled.checked && netTypeSel.value === 'bridge';
    ciDhcp.disabled = !allowed;
    if (!allowed) ciDhcp.checked = true;
    const staticMode = allowed && !ciDhcp.checked;
    if (!staticMode) ciIsolated.checked = false;
    ciStaticBox.style.display = staticMode ? '' : 'none';
    ciIsolated.disabled = !staticMode;
    const isolated = staticMode && ciIsolated.checked;
    ciIp.disabled = !staticMode;
    ciGateway.disabled = !staticMode || isolated;
    ciDns.disabled = !staticMode || isolated;
    ciStaticHint.textContent = isolated
      ? '仅同网段内网：只写入 IP 与掩码，不生成默认路由和 DNS，无法访问其他网段或互联网。'
      : '默认联网：网关与 DNS 必填；选择网桥并填入同网段 IP 后会自动带入该网桥的默认值。';
    if (!staticMode) {
      autoGateway = '';
      autoDns = '';
      return;
    }
    fillStaticDefaults();
  }

  function fillDiskDirs() {
    if (!diskDirSel.isConnected) return;
    const dirs = diskDirsData;
    if (!dirs.length) {
      diskDirSel.replaceChildren(h('option', { value: '' }, '（无可用磁盘目录）'));
      return;
    }
    diskDirSel.replaceChildren(...dirs.map(d => {
      const label = d.path + (d.free_human ? '（剩余 ' + d.free_human + '）' : '');
      return h('option', { value: d.path }, label);
    }));
    diskDirSel.value = dirs[0].path;
  }

  (async function load() {
    try {
      const [imgRes, netRes] = await Promise.all([
        api('GET', '/api/hosts/' + enc(hostId) + '/images'),
        api('GET', '/api/hosts/' + enc(hostId) + '/networks')
      ]);
      if (!dlg.alive) return;   // 弹窗已关闭：丢弃结果，不影响其他主机
      imagesData = Array.isArray(imgRes.images) ? imgRes.images : [];
      diskDirsData = Array.isArray(imgRes.disk_dirs) ? imgRes.disk_dirs : [];
      netData = netRes;
      loadBox.remove();
      fillDiskDirs();
      fillSources();
      fillNetworks();
      lastViewKey = '';
    } catch (e) {
      if (!dlg.alive) return;
      loadBox.replaceChildren(notice({
        kind: 'error', title: '读取镜像/网络失败', message: e.message, testid: 'vm-create-load-error'
      }));
      sourceSel.replaceChildren(h('option', { value: '' }, '（加载失败）'));
      dlg.card.querySelector('[data-testid="vm-create-submit"]').disabled = true;
    }
  })();

  function fail(msg, field) {
    errorBox.hidden = false;
    errorBox.textContent = msg;
    [nameInput, sourceSel, cpusInput, memInput, diskInput, netTypeSel, netSourceSel, ciUser, ciPass, ciKey, ciIp, ciGateway, ciDns].forEach(function (inp) {
      inp.removeAttribute('aria-invalid');
    });
    if (field) {
      field.setAttribute('aria-invalid', 'true');
      field.focus({ preventScroll: true });
    } else {
      errorBox.scrollIntoView({ block: 'nearest' });
    }
    return false;
  }

  const IPV4_RE = /^(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$/;

  function isIpv4(text) {
    return IPV4_RE.test(String(text || '').trim());
  }

  function isIpv4Cidr(text) {
    const parts = String(text || '').trim().split('/');
    if (parts.length !== 2 || !isIpv4(parts[0]) || !/^\d{1,2}$/.test(parts[1])) return false;
    const prefix = Number(parts[1]);
    return prefix >= 1 && prefix <= 30;
  }

  async function submit() {
    if (dlg.busy) return;
    errorBox.hidden = true;

    const name = nameInput.value.trim();
    const vcpus = Number(cpusInput.value);
    const mem = Number(memInput.value);
    const disk = Number(diskInput.value);
    const source = sourceSel.value;
    const ciOn = ciEnabled.checked && kindState.kind !== 'iso';
    const netType = netTypeSel.value === 'bridge' ? 'bridge' : 'network';

    if (!name) return fail('名称必填', nameInput);
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/.test(name)) return fail('名称须以字母或数字开头，可含点、下划线、连字符，最长 63 字符', nameInput);
    if (!source) return fail('请选择镜像来源', sourceSel);
    if (!Number.isInteger(vcpus) || vcpus < 1 || vcpus > 128) return fail('vCPU 需为 1-128 的整数', cpusInput);
    if (!Number.isInteger(mem) || mem < 1 || mem > 2048) return fail('内存需为 1-2048 的整数（GiB）', memInput);
    if (!Number.isInteger(disk) || disk < 1 || disk > 8192) return fail('磁盘需为 1-8192 的整数（GiB）', diskInput);
    if (!netSourceSel.value) {
      return fail(netType === 'bridge'
        ? '该主机没有可用网桥，请改用 NAT / libvirt 网络，或先在宿主机创建网桥'
        : '请选择网络来源', netSourceSel);
    }
    if (ciOn) {
      if (!ciUser.value.trim()) return fail('cloud-init 需要用户名', ciUser);
      if (!ciPass.value && !ciKey.value.trim()) return fail('cloud-init 至少填写密码或 SSH 公钥，否则无法登录', ciPass);
    }

    const staticMode = netType === 'bridge' && ciOn && kindState.kind === 'disk' && !ciDhcp.checked;
    const isolated = staticMode && ciIsolated.checked;
    if (staticMode) {
      if (!ciIp.value.trim()) return fail('请填写虚拟机静态 IP（IPv4/前缀）', ciIp);
      if (!isIpv4Cidr(ciIp.value)) return fail('静态 IP 须为 IPv4/前缀 且前缀 1-30，例如 192.168.0.201/24', ciIp);
      if (!isolated) {
        const gateway = ciGateway.value.trim();
        if (!gateway) return fail('联网的静态 IP 必须填写网关；仅同网段内网请勾选「仅同网段内网」', ciGateway);
        if (!isIpv4(gateway)) return fail('网关必须是合法的 IPv4 地址', ciGateway);
        const dnsText = ciDns.value.trim();
        if (!dnsText) return fail('联网的静态 IP 必须填写 DNS；仅同网段内网请勾选「仅同网段内网」', ciDns);
        const badDns = dnsText.split(/[\s,]+/).filter(Boolean).filter(item => !isIpv4(item));
        if (badDns.length) return fail('DNS 必须是合法的 IPv4 地址，多个地址用逗号或空格分隔', ciDns);
      }
    }

    const payload = {
      kind: kindState.kind,
      name: name,
      source: source,
      disk_dir: diskDirSel.value || '',
      memory_gb: mem,
      vcpus: vcpus,
      disk_gb: disk,
      network_type: netType,
      network_source: netSourceSel.value,
      cloud_init_enabled: ciOn,
      ci_username: ciOn ? ciUser.value.trim() : '',
      ci_password: ciOn ? ciPass.value : '',
      ci_ssh_key: ciOn ? ciKey.value.trim() : '',
      ci_dhcp: !staticMode,
      ci_isolated: isolated,
      ci_ip: staticMode ? ciIp.value.trim() : '',
      ci_gateway: staticMode && !isolated ? ciGateway.value.trim() : '',
      ci_dns: staticMode && !isolated ? ciDns.value.trim() : ''
    };

    dlg.busy = true;
    submitBtn.disabled = true;
    submitBtn.replaceChildren(h('span', { class: 'btn__spinner', 'aria-hidden': 'true' }), h('span', null, '提交中…'));

    try {
      const data = await api('POST', '/api/hosts/' + enc(hostId) + '/vms', payload);
      dlg.busy = false;
      if (!dlg.alive) {
        if (data && data.task) ingestTask(data.task);
        return;
      }
      if (data && data.task) {
        ingestTask(data.task);
        toast('已提交创建任务：' + name, { kind: 'info', taskId: data.task.id });
      } else {
        toast('已提交创建请求：' + name, { kind: 'info' });
      }
      dlg.close('done');
    } catch (e) {
      dlg.busy = false;
      if (!dlg.alive) return;
      fail(e.message);
      submitBtn.disabled = false;
      submitBtn.replaceChildren(h('span', null, '创建并提交任务'));
    }
  }
}

/* --------------------------------------------------------------- 控制台 */

function openConsole(hostId, vm) {
  const url = '/console?host=' + enc(hostId) + '&uuid=' + enc(vm.uuid);
  const win = window.open(url, 'vmconsole-' + vm.uuid, 'width=1280,height=820,noopener');
  if (!win) {
    toast('浏览器拦截了弹出窗口，请允许弹窗后重试，或直接访问 ' + url, { kind: 'error' });
  }
}

/* --------------------------------------------------------------- 视图切换 */

function setView(view) {
  if (view !== 'hosts' && view !== 'vms' && view !== 'images' && view !== 'tasks') return;
  state.view = view;
  state.highlight = null;
  lastViewKey = '';
  if (view !== 'vms') cancelVmsPoll();
  render();
  if (view === 'images' && state.hostId) loadImages(state.hostId);
  if (view === 'tasks') scheduleTaskPoll(0);
  if (view === 'vms') scheduleVmsPoll(0);
}

/* --------------------------------------------------------------- 绑定 */

function bindChrome() {
  el.nav.addEventListener('click', (ev) => {
    const target = ev.target.closest('[data-view]');
    if (target) setView(target.getAttribute('data-view'));
  });

  el.hostSelect.addEventListener('change', (ev) => {
    selectHost(ev.target.value);
  });

  document.getElementById('btn-refresh').addEventListener('click', () => {
    loadHosts();
    if (state.hostId) {
      loadStatus(state.hostId);
      if (state.view === 'images') loadImages(state.hostId);
      else loadVms(state.hostId);
    }
    if (state.view === 'tasks') loadTasks();
  });

  document.getElementById('btn-password').addEventListener('click', () => openPasswordDialog());
  document.getElementById('btn-logout').addEventListener('click', () => logout());
}
function hydrateIcons(root) {
  const nodes = (root || document).querySelectorAll('[data-icon]');
  for (const n of nodes) {
    const name = n.getAttribute('data-icon');
    n.replaceChildren(icon(name, 16));
  }
}

/* --------------------------------------------------------------- 启动 */

async function boot() {
  cacheEls();
  hydrateIcons(document);
  bindLogin();
  bindChrome();

  if (DEMO) {
    el.demoBanner.hidden = false;
    el.demoBanner.replaceChildren(
      icon('warn', 14),
      h('span', null, '演示数据：未连接任何后端，所有主机、虚机与任务均为前端固定数据，操作只在浏览器内模拟，不会影响真实环境。')
    );
  }

  // 启动时先露出登录门，避免空白页；会话有效则立即切换
  el.loginLayer.hidden = false;
  el.app.hidden = true;
  setLoginError('正在检查登录状态…', 'info');
  setLoginBusy(true);

  try {
    const session = await api('GET', '/api/session');
    state.user = session.username || '已登录';
    state.csrf = session.csrf_token || '';
    setLoginBusy(false);
    setLoginError('');
    showApp();
    loadHosts();
    loadTasks({ quiet: true });
  } catch (e) {
    setLoginBusy(false);
    if (e.status === 401) {
      setLoginError('');
      el.loginUser.focus();
    } else {
      setLoginError((e.message || '无法连接后端') + '——后端未启动时可访问 ?demo=1 查看演示数据。');
    }
  }

  try {
    const health = await api('GET', '/health');
    if (health && health.version) {
      state.version = health.version;
      if (el.loginVersion) el.loginVersion.textContent = 'v' + health.version;
    }
  } catch (e) { /* 版本号非关键路径 */ }

  // 页面重新可见时补一次静默刷新并重新排 VM 轮询；隐藏时停掉 timer。
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { cancelVmsPoll(); return; }
    if (!state.user) return;
    if (state.hostId) loadVms(state.hostId, { quiet: true });
    scheduleTaskPoll(0);
    scheduleVmsPoll();
  });
}

/* ==========================================================================
   演示数据（?demo=1）：仅用于无后端时的界面走查，界面上有常驻横幅标注。
   不参与生产路径；所有写操作只改变内存中的 fixtures。
   ========================================================================== */

const DEMO = new URLSearchParams(location.search).get('demo') === '1';
const DEMO_CONSOLE_DISABLED = DEMO;

const DEMO_HOSTS = [
  { id: 'local', name: '控制端本机 .30', address: '127.0.0.1', transport: 'local', ssh_user: 'root', ssh_port: 22 },
  { id: 'vm150', name: '执行端 .150', address: '192.168.0.150', transport: 'ssh', ssh_user: 'root', ssh_port: 22 },
  { id: 'edge151', name: '边缘节点 .151', address: '192.168.0.151', transport: 'ssh', ssh_user: 'ops', ssh_port: 2222 }
];

const DEMO_STATUS = {
  local: { hostname: 'control-30', os: 'CentOS 7', cpu_count: 8, memory_total: 16 * GiB, memory_available: 7.4 * GiB, disk_total: 480 * GiB, disk_free: 126 * GiB, vm_count: 3, running_count: 1, kvm: true },
  vm150: { hostname: 'exec-150', os: 'Ubuntu 22.04', cpu_count: 16, memory_total: 32 * GiB, memory_available: 11 * GiB, disk_total: 960 * GiB, disk_free: 302 * GiB, vm_count: 2, running_count: 2, kvm: true }
};

const DEMO_VMS = {
  local: [
    {
      uuid: '5f1c2b40-9a77-4a1e-8f0d-0d6b2f3a8c11', name: 'web-temp-01', host_id: 'local', state: 'running',
      cpu: 4, max_memory: 8 * 1024 * 1024, ip_addresses: ['192.168.122.41'],
      interfaces: [{ source: 'default', mac: '52:54:00:aa:11:01', ips: ['192.168.122.41'] }],
      disks: ['/var/lib/libvirt/images/web-temp-01.qcow2'],
      metrics: { cpu: { percent: 12, text: '12%' }, memory: { percent: 38, text: '38%' }, storage: {} },
      config: { vcpus: 4, memory_gb: 8, primary_disk_gb: 40 }, has_console: true
    },
    {
      uuid: '2a7d9c66-31b8-4d5f-9d1a-7c4f9e2b6a02', name: 'build-temp-02', host_id: 'local', state: 'shut off',
      cpu: 2, max_memory: 4 * 1024 * 1024, ip_addresses: [], interfaces: [{ source: 'default', mac: '52:54:00:aa:11:02', ips: [] }],
      disks: ['/var/lib/libvirt/images/build-temp-02.qcow2'], metrics: {},
      config: { vcpus: 2, memory_gb: 4, primary_disk_gb: 20 }, has_console: true
    },
    {
      uuid: '9e3b4f12-c7d2-4a63-b1e5-1f8a2c4d7e03', name: 'test-temp-03', host_id: 'local', state: 'paused',
      cpu: 2, max_memory: 2 * 1024 * 1024, ip_addresses: ['192.168.122.55'],
      interfaces: [{ source: 'default', mac: '52:54:00:aa:11:03', ips: ['192.168.122.55'] }],
      disks: ['/var/lib/libvirt/images/test-temp-03.qcow2'],
      metrics: { cpu: { percent: 0, text: '0%' }, memory: { percent: 64, text: '64%' }, storage: {} },
      config: { vcpus: 2, memory_gb: 2, primary_disk_gb: 16 }, has_console: true
    }
  ],
  vm150: [
    {
      uuid: 'c3a41d88-5b60-4e0a-8c77-2b0d1e6f9a04', name: 'ubuntu-temp-04', host_id: 'vm150', state: 'running',
      cpu: 4, max_memory: 8 * 1024 * 1024, ip_addresses: ['192.168.0.201'],
      interfaces: [{ source: 'br0', mac: '52:54:00:bb:22:04', ips: ['192.168.0.201'] }],
      disks: ['/var/lib/libvirt/images/ubuntu-temp-04.qcow2'],
      metrics: { cpu: { percent: 96, text: '96%' }, memory: { percent: 81, text: '81%' }, storage: {} },
      config: { vcpus: 4, memory_gb: 8, primary_disk_gb: 60 }, has_console: true
    },
    {
      uuid: 'd8f02e5a-1c93-4b7d-a2f6-3e5c8b0d1a05', name: 'win-temp-05', host_id: 'vm150', state: 'shut off',
      cpu: 8, max_memory: 16 * 1024 * 1024, ip_addresses: [], interfaces: [{ source: 'br0', mac: '52:54:00:bb:22:05', ips: [] }],
      disks: ['/var/lib/libvirt/images/win-temp-05.qcow2'], metrics: {},
      config: { vcpus: 8, memory_gb: 16, primary_disk_gb: 80 }, has_console: false
    }
  ],
  edge151: []
};

const DEMO_IMAGES = [
  { name: 'ubuntu-24.04-server-cloudimg-amd64.img', path: '/opt/vm-platform/images/ubuntu-24.04-server-cloudimg-amd64.img', type: 'disk', size_human: '590 MiB', format: 'qcow2', virtual_size: 3.5 * GiB, metadata: { alias: 'ubuntu2404', os_family: 'Ubuntu', os_version: '24.04 LTS' } },
  { name: 'debian-12-genericcloud-amd64.qcow2', path: '/opt/vm-platform/images/debian-12-genericcloud-amd64.qcow2', type: 'disk', size_human: '420 MiB', format: 'qcow2', virtual_size: 2 * GiB, metadata: { alias: 'debian12', os_family: 'Debian', os_version: '12' } },
  { name: 'rocky-9.4-x86_64-dvd.iso', path: '/opt/vm-platform/images/rocky-9.4-x86_64-dvd.iso', type: 'iso', size_human: '10.4 GiB', format: 'iso', virtual_size: 10.4 * GiB, metadata: { os_family: 'Rocky Linux', os_version: '9.4' } }
];

const DEMO_DISK_DIRS = [
  { path: '/var/lib/libvirt/images', free_human: '302 GiB', total: 960 * GiB, used: 658 * GiB, free: 302 * GiB },
  { path: '/opt/vm-platform/disks', free_human: '88.5 GiB', total: 200 * GiB, used: 111.5 * GiB, free: 88.5 * GiB }
];

const DEMO_NETWORKS = {
  local: {
    bridges: ['br0'], libvirt_networks: [{ name: 'default', state: 'active' }],
    default_bridge: 'br0', default_type: 'network', default_source: 'default',
    static_network_defaults: {
      br0: { cidr: '192.168.0.0/24', gateway: '192.168.0.1', dns: ['192.168.0.1', '8.8.8.8'] }
    }
  },
  vm150: {
    bridges: ['br0', 'br1'], libvirt_networks: [{ name: 'default', state: 'active' }, { name: 'lab', state: 'inactive' }],
    default_bridge: 'br0', default_type: 'bridge', default_source: 'br0',
    static_network_defaults: {
      br0: { cidr: '192.168.0.0/24', gateway: '192.168.0.1', dns: ['192.168.0.1', '8.8.8.8'] },
      br1: { cidr: '192.168.10.0/24', gateway: '192.168.10.1', dns: ['192.168.10.1', '8.8.8.8'] }
    }
  },
  edge151: {
    bridges: [], libvirt_networks: [], default_bridge: '', default_type: 'network', default_source: '',
    static_network_defaults: {}
  }
};

const DEMO_TASKS = [
  { id: 't-1041', host_id: 'vm150', host_name: '执行端 .150', vm_uuid: 'c3a41d88-5b60-4e0a-8c77-2b0d1e6f9a04', vm_name: 'ubuntu-temp-04', title: '启动 ubuntu-temp-04', status: 'success', created_at: Math.floor(Date.now() / 1000) - 320, finished_at: Math.floor(Date.now() / 1000) - 312, error: null, result: '域已启动' },
  { id: 't-1042', host_id: 'local', host_name: '控制端本机 .30', vm_uuid: '2a7d9c66-31b8-4d5f-9d1a-7c4f9e2b6a02', vm_name: 'build-temp-02', title: '调整 build-temp-02 配置', status: 'failed', created_at: Math.floor(Date.now() / 1000) - 260, finished_at: Math.floor(Date.now() / 1000) - 255, error: '虚拟机未关机，拒绝修改配置', result: null },
  { id: 't-1043', host_id: 'vm150', host_name: '执行端 .150', vm_uuid: null, vm_name: null, title: '检测主机 vm150', status: 'failed', created_at: Math.floor(Date.now() / 1000) - 60, finished_at: Math.floor(Date.now() / 1000) - 58, error: 'SSH 连接超时：192.168.0.150:22 无响应', result: null }
];

let demoTaskSeq = 1044;
let demoTaskList = DEMO_TASKS.slice();
let demoVms = JSON.parse(JSON.stringify(DEMO_VMS));
let demoHosts = DEMO_HOSTS.slice();

function demoDelay() {
  return new Promise(r => setTimeout(r, 220 + Math.random() * 180));
}

function demoNewTask(task) {
  const t = Object.assign({
    id: 't-' + (demoTaskSeq++),
    host_id: 'local', host_name: '控制端本机 .30',
    vm_uuid: null, vm_name: null,
    status: 'running', created_at: Math.floor(Date.now() / 1000),
    finished_at: null, error: null, result: null
  }, task);
  demoTaskList.unshift(t);
  setTimeout(() => {
    t.status = 'success';
    t.finished_at = Math.floor(Date.now() / 1000);
    t.result = '演示：任务完成';
  }, 1800);
  return t;
}

async function demoApi(method, path, body) {
  await demoDelay();
  const url = path.split('?')[0];

  if (url === '/api/session') return { username: 'demo', csrf_token: 'demo-csrf-token' };
  if (url === '/api/login') return { username: 'demo', csrf_token: 'demo-csrf-token' };
  if (url === '/api/logout') return { ok: true };
  if (url === '/api/password' && method === 'POST') return { ok: true };
  if (url === '/health') return { ok: true, version: '1.1.2' };
  if (url === '/api/hosts' && method === 'GET') return { hosts: demoHosts };
  if (url === '/api/tasks') return { tasks: demoTaskList };

  let m = url.match(/^\/api\/hosts\/([^/]+)\/status$/);
  if (m) {
    const id = decodeURIComponent(m[1]);
    const st = DEMO_STATUS[id];
    if (!st) throw new ApiError('无法连接主机（演示数据中该主机离线）', 502);
    return { host: demoHosts.filter(hh => hh.id === id)[0], online: true, status: st };
  }

  m = url.match(/^\/api\/hosts\/([^/]+)\/vms$/);
  if (m && method === 'GET') {
    const id = decodeURIComponent(m[1]);
    return { vms: demoVms[id] || [] };
  }
  if (m && method === 'POST') {
    const id = decodeURIComponent(m[1]);
    const name = (body && body.name) || 'new-vm';
    const list = demoVms[id] || (demoVms[id] = []);
    const task = demoNewTask({
      host_id: id, host_name: (demoHosts.filter(hh => hh.id === id)[0] || {}).name || id,
      title: '创建虚拟机 ' + name
    });
    setTimeout(() => {
      list.unshift({
        uuid: 'demo-' + Math.random().toString(16).slice(2, 10) + '-4a1e-8f0d-0d6b2f3a8c11',
        name: name, host_id: id, state: 'shut off', cpu: (body && body.vcpus) || 2,
        max_memory: ((body && body.memory_gb) || 2) * 1024 * 1024, ip_addresses: [],
        interfaces: [{ source: (body && body.network_source) || 'default', mac: '52:54:00:de:mo:01', ips: [] }],
        disks: [], metrics: {},
        config: { vcpus: (body && body.vcpus) || 2, memory_gb: (body && body.memory_gb) || 2, primary_disk_gb: (body && body.disk_gb) || 20 },
        has_console: true
      });
    }, 1800);
    return { task: task };
  }

  m = url.match(/^\/api\/hosts\/([^/]+)\/vms\/([^/]+)\/action$/);
  if (m) {
    const id = decodeURIComponent(m[1]);
    const uuid = decodeURIComponent(m[2]);
    const list = demoVms[id] || [];
    const vm = list.filter(v => v.uuid === uuid)[0];
    const action = (body && body.action) || 'start';
    const task = demoNewTask({
      host_id: id, host_name: (demoHosts.filter(hh => hh.id === id)[0] || {}).name || id,
      vm_uuid: uuid, vm_name: vm ? vm.name : null,
      title: ({ start: '启动', shutdown: '关机', stop: '强制停止', reboot: '重启', suspend: '挂起', resume: '恢复', delete: '删除' }[action] || action) + ' ' + (vm ? vm.name : uuid)
    });
    setTimeout(() => {
      if (!vm) return;
      if (action === 'delete') {
        const i = list.indexOf(vm);
        if (i >= 0) list.splice(i, 1);
        return;
      }
      if (action === 'start' || action === 'resume') vm.state = 'running';
      else if (action === 'suspend' || action === 'shutdown') vm.state = action === 'suspend' ? 'paused' : 'shut off';
      else if (action === 'stop') vm.state = 'shut off';
      else if (action === 'reboot') vm.state = 'running';
    }, 1800);
    return { task: task };
  }

  m = url.match(/^\/api\/hosts\/([^/]+)\/vms\/([^/]+)\/config$/);
  if (m) {
    const id = decodeURIComponent(m[1]);
    const uuid = decodeURIComponent(m[2]);
    const vm = (demoVms[id] || []).filter(v => v.uuid === uuid)[0];
    const task = demoNewTask({
      host_id: id, host_name: (demoHosts.filter(hh => hh.id === id)[0] || {}).name || id,
      vm_uuid: uuid, vm_name: vm ? vm.name : null,
      title: '调整配置 ' + (vm ? vm.name : uuid)
    });
    setTimeout(() => {
      if (!vm) return;
      vm.config = { vcpus: body.vcpus, memory_gb: body.memory_gb, primary_disk_gb: body.disk_gb };
    }, 1800);
    return { task: task };
  }

  m = url.match(/^\/api\/hosts\/([^/]+)\/images$/);
  if (m) return { images: DEMO_IMAGES, dirs: ['/opt/vm-platform/images'], disk_dirs: DEMO_DISK_DIRS };

  m = url.match(/^\/api\/hosts\/([^/]+)\/networks$/);
  if (m) {
    const id = decodeURIComponent(m[1]);
    return DEMO_NETWORKS[id] || { bridges: [], libvirt_networks: [], default_bridge: '', default_type: 'network', default_source: '', static_network_defaults: {} };
  }

  if (url === '/api/hosts' && method === 'POST') {
    const rec = { id: body.id, name: body.name, address: body.address, transport: body.transport, ssh_user: body.ssh_user, ssh_port: body.ssh_port };
    demoHosts.push(rec);
    return { host: rec };
  }
  m = url.match(/^\/api\/hosts\/([^/]+)$/);
  if (m && method === 'PUT') {
    const id = decodeURIComponent(m[1]);
    for (const hh of demoHosts) if (hh.id === id) Object.assign(hh, body, { id: id });
    return { host: demoHosts.filter(hh => hh.id === id)[0] };
  }
  if (m && method === 'DELETE') {
    const id = decodeURIComponent(m[1]);
    demoHosts = demoHosts.filter(hh => hh.id !== id);
    return { ok: true };
  }

  throw new ApiError('演示数据未覆盖该接口：' + method + ' ' + url, 404);
}

/* --------------------------------------------------------------- go */

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
