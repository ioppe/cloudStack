/* ==========================================================================
   云栈 · 虚拟机平台 — noVNC 控制台窗口（ES module）

   约定（docs/IMPLEMENTATION.md）：
   - 页面地址 /console?host={id}&uuid={uuid}，登录态校验失败时提示回主界面登录
   - 身份信息（主机名、虚拟机名、状态）全部来自 API，不在前端拼接猜测
   - 画面通过 ES module 从同源 /static/novnc/core/rfb.js 载入，无 CDN
   - WebSocket /api/hosts/{host}/vms/{uuid}/vnc 使用同源 Cookie 认证，
     浏览器握手自动携带 Cookie，前端无法也不应附加 CSRF 头
   - 所有动态文本走 textContent，避免注入
   ========================================================================== */

const screenEl = document.getElementById('screen');
const overlayEl = document.getElementById('overlay');
const overlayTitle = document.getElementById('overlay-title');
const overlayMsg = document.getElementById('overlay-msg');
const overlayIcon = document.getElementById('overlay-icon');
const overlayActions = document.getElementById('overlay-actions');
const statusPill = document.getElementById('console-status');
const statusText = document.getElementById('console-status-text');
const vmNameEl = document.getElementById('vm-name');
const vmMetaEl = document.getElementById('vm-meta');
const footSource = document.getElementById('foot-source');
const btnCad = document.getElementById('btn-cad');
const btnScale = document.getElementById('btn-scale');
const btnReconnect = document.getElementById('btn-reconnect');
const btnClose = document.getElementById('btn-close');
const pwForm = document.getElementById('vnc-password-form');
const pwInput = document.getElementById('vnc-password');

const params = new URLSearchParams(window.location.search);
const hostId = params.get('host') || '';
const uuid = params.get('uuid') || '';

/** 只允许安全字符，避免把奇怪内容拼进 URL 路径 */
const HOST_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

let rfb = null;
let connecting = false;
let scaleViewport = true;

/* --------------------------------------------------------------- UI 片段 */

function setStatus(kind, text) {
  statusPill.className = 'pill' + (kind ? ' pill--' + kind : '');
  statusText.textContent = text;
  statusPill.title = text;
}

function clearOverlayActions() {
  overlayActions.replaceChildren();
}

function actionButton(label, onClick, testid, primary) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'btn btn--sm' + (primary ? ' btn--primary' : '');
  if (testid) b.setAttribute('data-testid', testid);
  b.textContent = label;
  b.addEventListener('click', onClick);
  return b;
}

/**
 * @param {{title:string, message:string, kind?:'busy'|'error'|'info', actions?:HTMLElement[]}} o
 */
function showOverlay(o) {
  overlayEl.hidden = false;
  overlayTitle.textContent = o.title;
  overlayMsg.textContent = o.message || '';
  overlayIcon.replaceChildren();
  overlayIcon.style.color = o.kind === 'error' ? 'var(--c-warn)' : 'var(--c-ink-mute)';
  if (o.actions && o.actions.length) {
    overlayActions.replaceChildren(...o.actions);
    overlayActions.hidden = false;
  } else {
    clearOverlayActions();
    overlayActions.hidden = true;
  }
  if (o.kind === 'error') setStatus('off', '连接失败');
  else if (o.kind === 'busy') setStatus('busy', o.title);
}

function hideOverlay() {
  overlayEl.hidden = true;
  pwForm.hidden = true;
}

/* --------------------------------------------------------------- API */

async function getJson(path) {
  const res = await fetch(path, { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
  let data = null;
  const raw = await res.text();
  if (raw) {
    try { data = JSON.parse(raw); } catch (e) { data = null; }
  }
  if (!res.ok) {
    const err = new Error((data && data.error) || ('HTTP ' + res.status));
    err.status = res.status;
    throw err;
  }
  return data || {};
}

function wsUrl() {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return proto + '//' + window.location.host +
    '/api/hosts/' + encodeURIComponent(hostId) +
    '/vms/' + encodeURIComponent(uuid) + '/vnc';
}

/* --------------------------------------------------------------- 身份 */

async function loadIdentity() {
  const [hosts, vms] = await Promise.all([
    getJson('/api/hosts'),
    getJson('/api/hosts/' + encodeURIComponent(hostId) + '/vms')
  ]);
  const host = (hosts.hosts || []).filter(function (x) { return x.id === hostId; })[0];
  const vm = (vms.vms || []).filter(function (x) { return x.uuid === uuid; })[0];

  if (host) {
    document.title = (vm ? vm.name : uuid) + ' · 控制台 · 云栈';
    vmNameEl.textContent = vm ? vm.name : uuid;
  }
  if (!vm) {
    return { host: host, vm: null };
  }
  vmNameEl.textContent = vm.name || uuid;
  const bits = [];
  if (host) bits.push(host.name + '（' + host.id + '）');
  else bits.push(hostId);
  bits.push(uuid);
  if (vm.state) bits.push(vm.state);
  vmMetaEl.textContent = bits.join(' · ');
  footSource.textContent = '已绑定目标主机与虚拟机，连接通过平台验证。';
  return { host: host, vm: vm };
}

/* --------------------------------------------------------------- 连接 */

async function connect() {
  if (connecting) return;
  if (rfb) {
    try { rfb.disconnect(); } catch (e) { /* 已断开 */ }
    rfb = null;
  }
  connecting = true;
  btnCad.disabled = true;
  setStatus('busy', '连接中…');
  showOverlay({ kind: 'busy', title: '正在连接控制台', message: '校验会话与虚拟机身份…' });

  // 1. 会话
  try {
    await getJson('/api/session');
  } catch (e) {
    connecting = false;
    if (e.status === 401) {
      setStatus('off', '未登录');
      showOverlay({
        kind: 'error',
        title: '需要先登录',
        message: '会话已过期或未登录。请回到平台主界面登录，再重新打开控制台。',
        actions: [
          actionButton('打开登录页', function () { window.open('/', '_blank', 'noopener'); }, 'console-open-login', true),
          actionButton('重新连接', connect, 'console-retry-login')
        ]
      });
    } else {
      showOverlay({
        kind: 'error',
        title: '无法连接后端',
        message: e.message || '登录状态验证失败，请重新登录。',
        actions: [actionButton('重试', connect, 'console-retry-session', true)]
      });
    }
    return;
  }

  // 2. 身份（host + uuid 必须匹配到真实虚拟机）
  try {
    const ident = await loadIdentity();
    if (!ident.vm) {
      connecting = false;
      vmNameEl.textContent = uuid || '（缺少 uuid）';
      vmMetaEl.textContent = hostId ? hostId + ' · ' + uuid : '—';
      setStatus('off', '未找到虚拟机');
      showOverlay({
        kind: 'error',
        title: '未找到该虚拟机',
        message: '主机 ' + hostId + ' 下没有 UUID 为 ' + uuid + ' 的虚拟机，可能已被删除或主机不匹配。',
        actions: [actionButton('重新检查', connect, 'console-retry-identity', true)]
      });
      return;
    }
    if (ident.vm.state !== 'running') {
      connecting = false;
      setStatus('note', '虚拟机未运行');
      showOverlay({
        kind: 'error',
        title: '虚拟机未运行',
        message: '当前状态为「' + (ident.vm.state || '未知') + '」。控制台只能在运行中连接，请先在虚拟机页启动。',
        actions: [actionButton('重新检查', connect, 'console-retry-state', true)]
      });
      return;
    }
  } catch (e) {
    connecting = false;
    setStatus('off', e.status === 401 ? '未登录' : '读取失败');
    showOverlay({
      kind: 'error',
      title: e.status === 401 ? '需要先登录' : '读取虚拟机信息失败',
      message: e.message || '',
      actions: [actionButton('重试', connect, 'console-retry-meta', true)]
    });
    return;
  }

  // 3. noVNC 库（同源 ES module）
  let RFB;
  try {
    const mod = await import('/static/novnc/core/rfb.js');
    RFB = mod.default || mod.RFB;
    if (typeof RFB !== 'function') throw new Error('模块未导出 RFB 构造函数');
  } catch (e) {
    connecting = false;
    setStatus('off', '控制台库未就绪');
    showOverlay({
      kind: 'error',
      title: 'noVNC 库未就绪',
      message: '无法载入 /static/novnc/core/rfb.js：' + (e && e.message ? e.message : '加载失败') +
        '。请确认 noVNC 已复制到 static/novnc。',
      actions: [actionButton('重试', connect, 'console-retry-lib', true)]
    });
    return;
  }

  // 4. 建连
  try {
    rfb = new RFB(screenEl, wsUrl(), { shared: true, credentials: { password: '' } });
    rfb.scaleViewport = scaleViewport;
    rfb.resizeSession = false;
    rfb.focusOnClick = true;

    rfb.addEventListener('connect', function () {
      connecting = false;
      hideOverlay();
      setStatus('on', '已连接');
      btnCad.disabled = false;
      btnScale.setAttribute('aria-pressed', String(scaleViewport));
      if (rfb.focusOnClick !== false) {
        try { rfb.focus(); } catch (e) { /* 部分版本无 focus */ }
      }
    });

    rfb.addEventListener('disconnect', function (ev) {
      connecting = false;
      btnCad.disabled = true;
      if (ev && ev.detail && ev.detail.clean) {
        setStatus('idle', '已断开');
        showOverlay({
          kind: 'info', title: '连接已结束', message: '服务端正常关闭了控制台会话。',
          actions: [actionButton('重新连接', connect, 'console-reconnect-after-close', true)]
        });
      } else {
        setStatus('off', '连接中断');
        showOverlay({
          kind: 'error',
          title: '控制台连接中断',
          message: '可能原因：虚拟机已关机、网络中断或 VNC 会话超时。',
          actions: [actionButton('重新连接', connect, 'console-reconnect-after-drop', true)]
        });
      }
    });

    rfb.addEventListener('credentialsrequired', function () {
      showOverlay({
        kind: 'busy',
        title: '需要 VNC 密码',
        message: '服务端要求凭据，请输入该虚拟机的 VNC 密码。'
      });
      pwForm.hidden = false;
      pwInput.focus();
    });

    rfb.addEventListener('securityfailure', function (ev) {
      const reason = ev && ev.detail && ev.detail.reason ? ev.detail.reason : '认证失败';
      setStatus('off', '认证失败');
      showOverlay({
        kind: 'error', title: 'VNC 认证失败', message: String(reason),
        actions: [actionButton('重试', connect, 'console-retry-auth', true)]
      });
    });
  } catch (e) {
    connecting = false;
    setStatus('off', '连接失败');
    showOverlay({
      kind: 'error',
      title: '无法建立控制台连接',
      message: (e && e.message) || 'WebSocket 创建失败。',
      actions: [actionButton('重试', connect, 'console-retry-ws', true)]
    });
  }
}

/* --------------------------------------------------------------- 交互 */

pwForm.addEventListener('submit', function (ev) {
  ev.preventDefault();
  if (!rfb) return;
  const pwd = pwInput.value;
  pwInput.value = '';
  pwForm.hidden = true;
  if (typeof rfb.sendCredentials === 'function') rfb.sendCredentials({ password: pwd });
  showOverlay({ kind: 'busy', title: '正在认证', message: '已发送凭据，等待服务端响应…' });
});

btnCad.addEventListener('click', function () {
  if (!rfb) return;
  if (typeof rfb.sendCtrlAltDel === 'function') rfb.sendCtrlAltDel();
  btnCad.blur();
  screenEl.focus();
});

btnScale.addEventListener('click', function () {
  scaleViewport = !scaleViewport;
  btnScale.setAttribute('aria-pressed', String(scaleViewport));
  btnScale.textContent = scaleViewport ? '适应窗口' : '原始尺寸';
  if (rfb) {
    rfb.scaleViewport = scaleViewport;
  }
});

btnReconnect.addEventListener('click', function () {
  connect();
});

btnClose.addEventListener('click', function () {
  if (rfb) { try { rfb.disconnect(); } catch (e) { /* ignore */ } }
  window.close();
  // window.close() 对非脚本打开的窗口无效：退回上一页
  window.setTimeout(function () {
    if (!window.closed) window.location.href = '/';
  }, 120);
});

window.addEventListener('beforeunload', function () {
  if (rfb) { try { rfb.disconnect(); } catch (e) { /* ignore */ } }
});

/* --------------------------------------------------------------- 启动 */

(function boot() {
  vmNameEl.textContent = uuid || '（缺少 uuid）';
  vmMetaEl.textContent = hostId ? hostId : '—';
  btnScale.textContent = scaleViewport ? '适应窗口' : '原始尺寸';

  if (!hostId || !uuid) {
    setStatus('off', '参数缺失');
    showOverlay({
      kind: 'error',
      title: '控制台地址缺少参数',
      message: '需要 /console?host=<主机ID>&uuid=<虚拟机UUID>。请从虚拟机列表的「控制台」按钮打开。',
      actions: [actionButton('回到平台', function () { window.location.href = '/'; }, 'console-back', true)]
    });
    return;
  }
  if (!HOST_RE.test(hostId) || !UUID_RE.test(uuid)) {
    setStatus('off', '参数无效');
    showOverlay({
      kind: 'error',
      title: '控制台参数无效',
      message: '主机 ID 或 UUID 格式不正确，无法连接。',
      actions: [actionButton('回到平台', function () { window.location.href = '/'; }, 'console-back-invalid', true)]
    });
    return;
  }

  connect();
})();
