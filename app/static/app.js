/* SpaceMatch Reels — agent accounts (brokers by invite), client-side routing, Playwright counters. */
'use strict';

const state = { csrf: null, me: null, user: null };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = text => String(text ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
const isNum = v => v !== null && v !== undefined && Number.isFinite(Number(v));
const num = v => isNum(v) ? Number(v).toLocaleString('en-US') : 'N/A';
const compact = v => isNum(v) ? Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(v) : '—';
const enc = encodeURIComponent;
const ACCESS = { agent: ['Agent', 'tag-green'], full: ['Full access', 'tag-blue'], view: ['View only', 'tag-gray'] };
const accessTag = a => `<span class="tag ${ACCESS[a]?.[1] || 'tag-gray'}">${esc(ACCESS[a]?.[0] || a)}</span>`;
const canEdit = access => access === 'agent' || access === 'full';

let toastTimer;
function toast(message, error = false) {
  const el = $('#toast');
  el.textContent = message;
  el.className = 'toast visible' + (error ? ' error' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = 'toast'), 5100);
}

async function api(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body) headers['Content-Type'] = 'application/json';
  if (options.method && options.method !== 'GET' && state.csrf) headers['X-CSRF-Token'] = state.csrf;
  const response = await fetch(url, { credentials: 'same-origin', ...options, headers });
  const json = await response.json().catch(() => ({}));
  if (response.status === 401 && state.user) { // session ended elsewhere
    state.user = null;
    navigate('/login?next=' + enc(location.pathname), true);
  }
  if (!response.ok) {
    let detail = json.detail || `HTTP ${response.status}`;
    if (Array.isArray(detail)) detail = detail.map(d => `${d.loc?.slice(-1)[0] || 'field'}: ${d.msg}`).join('; ');
    const error = new Error(String(detail));
    error.status = response.status;
    error.failedAttempts = Number(response.headers.get('X-Failed-Attempts')) || 0;  // login only
    throw error;
  }
  return json;
}
const post = (url, body, method = 'POST') => api(url, { method, body: JSON.stringify(body || {}) });

async function busy(button, fn) {
  if (button) { button.disabled = true; button.dataset.label = button.textContent; button.textContent = 'Working…'; }
  try { await fn(); } catch (e) { toast(e.message, true); }
  finally { if (button && button.isConnected) { button.disabled = false; button.textContent = button.dataset.label; } }
}

function formData(form) { return Object.fromEntries(new FormData(form).entries()); }

/* ------------------------------------------------------------------ router -- */

const routes = [
  { pattern: /^\/$/, redirect: '/properties' },
  { pattern: /^\/login$/, view: viewLogin, guest: true, title: 'Log in' },
  { pattern: /^\/signup$/, view: viewSignup, guest: true, title: 'Create account' },
  { pattern: /^\/forgot-password$/, view: viewForgotPassword, guest: true, title: 'Forgot password' },
  { pattern: /^\/recover$/, view: viewRecover, guest: true, title: 'Recover account' },
  { pattern: /^\/reset-password\/([\w-]+)$/, view: viewResetPassword, guest: true, title: 'Reset password' },
  { pattern: /^\/invite\/([\w-]+)$/, view: viewInvite, guest: true, title: 'Accept invite' },
  { pattern: /^\/properties$/, view: viewProperties, auth: true, title: 'Properties', nav: 'properties' },
  { pattern: /^\/properties\/([\w-]+)$/, view: viewProperty, auth: true, title: 'Property', nav: 'properties' },
  { pattern: /^\/brokers$/, view: viewBrokers, auth: true, agent: true, title: 'Brokers', nav: 'brokers' },
  { pattern: /^\/api-keys$/, view: viewApiKeys, auth: true, agent: true, title: 'API keys', nav: 'api-keys' },
  { pattern: /^\/account$/, view: viewAccount, auth: true, title: 'Account', nav: 'account' },
];
let renderToken = 0;

function navigate(path, replace = false) {
  history[replace ? 'replaceState' : 'pushState']({}, '', path);
  render();
}

async function render() {
  const token = ++renderToken;
  const path = location.pathname.replace(/\/+$/, '') || '/';
  const route = routes.find(r => r.pattern.test(path));
  const params = route ? path.match(route.pattern).slice(1) : [];
  if (route?.redirect) return navigate(route.redirect, true);
  if (route?.auth && !state.user) return navigate('/login?next=' + enc(path), true);
  if (route?.guest && state.user && !path.startsWith('/invite/') && !path.startsWith('/reset-password/')) return navigate(nextPath(), true);
  if (route?.agent && state.user?.role !== 'agent') return navigate('/properties', true);

  const authLayout = route ? !!route.guest : !state.user;
  $('#auth-layout').classList.toggle('hidden', !authLayout);
  $('#app-layout').classList.toggle('hidden', authLayout);
  document.title = `${route?.title || 'Not found'} · SpaceMatch`;
  $$('[data-nav]').forEach(a => a.classList.toggle('active', a.dataset.nav === route?.nav));
  const root = authLayout ? $('#auth-root') : $('#view');
  if (!route) { root.innerHTML = notFound(); setCrumbs([['Not found']]); return; }
  root.innerHTML = '<div class="empty">Loading…</div>';
  try {
    await route.view(root, ...params, () => token === renderToken);
    if (authLayout && token === renderToken) addPasswordToggles(root);
  } catch (e) {
    if (token === renderToken) root.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
  window.scrollTo(0, 0);
}

function nextPath() {
  const next = new URLSearchParams(location.search).get('next');
  return next && next.startsWith('/') && !next.startsWith('//') ? next : '/properties';
}

function setCrumbs(items) {
  $('#breadcrumb').innerHTML = ['Hub', ...items.map(i => Array.isArray(i) ? i : [i])].map((item, i, all) => {
    const [label, href] = Array.isArray(item) ? item : [item];
    if (i === all.length - 1) return `<strong>${esc(label)}</strong>`;
    return href ? `<a href="${esc(href)}" data-link>${esc(label)}</a>` : esc(label);
  }).join(' <span>/</span> ');
}

const notFound = () => `<div class="empty"><h2>Page not found</h2><p><a href="/properties" data-link>Go to properties</a></p></div>`;

document.addEventListener('click', e => {
  const link = e.target.closest('a[data-link]');
  if (!link || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
  e.preventDefault();
  if (link.getAttribute('href') !== location.pathname + location.search) navigate(link.getAttribute('href'));
});
window.addEventListener('popstate', render);

function setUser(user, csrf) {
  state.user = user;
  if (csrf) state.csrf = csrf;
  if (!user) return;
  $('#user-name').textContent = user.name;
  $('#user-email').textContent = user.email;
  $('#user-role').textContent = user.role === 'agent' ? 'AGENT' : 'BROKER';
  $$('[data-agent-only]').forEach(el => el.classList.toggle('hidden', user.role !== 'agent'));
}

async function logout() {
  try { const r = await post('/api/auth/logout'); state.csrf = r.csrf; } catch (e) { /* already logged out */ }
  state.user = null;
  navigate('/login');
}
$('#logout').addEventListener('click', logout);
$('#logout-mobile').addEventListener('click', logout);

/* ---------------------------------------------------------------- auth views -- */

/** Adds a Show/Hide button inside every password field in `root`. */
function addPasswordToggles(root) {
  $$('input[type="password"]', root).forEach(input => {
    if (input.parentElement.classList.contains('pw-field')) return;
    const wrap = document.createElement('div');
    wrap.className = 'pw-field';
    input.replaceWith(wrap);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pw-toggle';
    btn.textContent = 'Show';
    btn.setAttribute('aria-label', 'Show password');
    btn.setAttribute('aria-pressed', 'false');
    btn.addEventListener('click', () => {
      const show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      btn.textContent = show ? 'Hide' : 'Show';
      btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
      btn.setAttribute('aria-pressed', String(show));
      input.focus();
    });
    wrap.append(input, btn);
  });
}

const brand = `<div class="brand-large"><span class="mark">S</span><div><strong>SPACE<span>MATCH</span></strong><small>HUB</small></div></div>`;

function viewLogin(root) {
  root.innerHTML = `${brand}<h1>Log in</h1><p class="muted">Agents and invited brokers.</p>
    <form id="login-form">
      <label for="login-email">Email</label><input id="login-email" name="email" type="email" autocomplete="email" required>
      <label for="login-password">Password</label><input id="login-password" name="password" type="password" autocomplete="current-password" required>
      <a class="forgot-link" href="/forgot-password" data-link>Forgot password?</a>
      <button class="primary" type="submit">Log in</button>
    </form>
    <div id="recover-help" class="notice hidden" role="alert">Having trouble logging in? If you can't get into your email, <a id="recover-help-link" href="/recover" data-link>recover your account</a> with your recovery email. Or <a href="/forgot-password" data-link>reset your password</a>.</div>
    <small class="muted">New agent? <a href="/signup" data-link>Create an account</a>.<br></small>`;
  $('#login-form').addEventListener('submit', e => {
    e.preventDefault();
    busy(e.submitter, async () => {
      try {
        const r = await post('/api/auth/login', formData(e.target));
        setUser(r.user, r.csrf);
        navigate(nextPath(), true);
      } catch (err) {
        if (err.failedAttempts >= 5) {  // offer recovery after 5 failed attempts for this email
          $('#recover-help-link').setAttribute('href', '/recover?email=' + enc($('#login-email').value.trim()));
          $('#recover-help').classList.remove('hidden');
        }
        throw err;
      }
    });
  });
}

function viewSignup(root) {
  root.innerHTML = `${brand}<h1>Create account</h1><p class="muted">Create your agent account.</p>
    <form id="signup-form">
      <label for="su-name">Full name</label><input id="su-name" name="name" autocomplete="name" required minlength="2" maxlength="120">
      <label for="su-email">Email</label><input id="su-email" name="email" type="email" autocomplete="email" required>
      <label for="su-password">Password</label><input id="su-password" name="password" type="password" autocomplete="new-password" required minlength="8" maxlength="128">
      <label for="su-confirm">Confirm password</label><input id="su-confirm" name="confirm" type="password" autocomplete="new-password" required minlength="8">
      <button class="primary" type="submit">Create account</button>
    </form>
    <small class="muted">Already have an account? <a href="/login" data-link>Log in</a>.</small>`;
  $('#signup-form').addEventListener('submit', e => {
    e.preventDefault();
    const data = formData(e.target);
    if (data.password !== data.confirm) return toast('Passwords do not match', true);
    busy(e.submitter, async () => {
      const r = await post('/api/auth/signup', { name: data.name, email: data.email, password: data.password });
      setUser(r.user, r.csrf);
      toast('Welcome to SpaceMatch!');
      navigate('/properties', true);
    });
  });
}

function viewForgotPassword(root) {
  root.innerHTML = `${brand}<h1>Forgot password</h1><p class="muted">Enter your account email and we'll send you a link to choose a new password.</p>
    <form id="forgot-form">
      <label for="fp-email">Email</label><input id="fp-email" name="email" type="email" autocomplete="email" required>
      <button class="primary" type="submit">Send reset link</button>
    </form>
    <small class="muted">Can't access this email? <a href="/recover" data-link>Recover your account</a> with your recovery email.<br><a href="/login" data-link>Back to log in</a></small>`;
  $('#forgot-form').addEventListener('submit', e => {
    e.preventDefault();
    busy(e.submitter, async () => {
      const r = await post('/api/auth/forgot-password', formData(e.target));
      root.innerHTML = `${brand}<h1>Check your email</h1><p class="muted">${esc(r.message)} The link works once, for 1 hour.</p>
        <a class="secondary link-button" href="/login" data-link>Back to log in</a>`;
    });
  });
}

async function viewResetPassword(root, token, current) {
  const reset = await api(`/api/password-resets/${enc(token)}`).catch(e => ({ error: e.message }));
  if (!current()) return;
  if (reset.error) {
    root.innerHTML = `${brand}<h1>Link unavailable</h1><p class="muted">${esc(reset.error)}</p>
      <a class="primary link-button" href="/forgot-password" data-link>Request a new link</a>`;
    return;
  }
  root.innerHTML = `${brand}<h1>Choose a new password</h1><p class="muted">It must be different from your current password.</p>
    <form id="reset-form">
      <label>Email</label><input value="${esc(reset.email)}" disabled>
      <label for="rp-password">New password</label><input id="rp-password" name="password" type="password" autocomplete="new-password" required minlength="8" maxlength="128">
      <label for="rp-confirm">Confirm new password</label><input id="rp-confirm" name="confirm" type="password" autocomplete="new-password" required minlength="8">
      <button class="primary" type="submit">Set new password</button>
    </form>`;
  $('#reset-form').addEventListener('submit', e => {
    e.preventDefault();
    const data = formData(e.target);
    if (data.password.length < 8) return toast('Use at least 8 characters', true);
    if (data.password !== data.confirm) return toast('Passwords do not match', true);
    busy(e.submitter, async () => {
      const r = await post(`/api/password-resets/${enc(token)}`, { password: data.password });
      setUser(r.user, r.csrf);
      toast('Password updated - you are logged in');
      navigate('/properties', true);
    });
  });
}

/* ------------------------------------------------------- account recovery -- */

const codeInput = (id, label = '6-digit code') =>
  `<input id="${id}" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="\\d{6}" maxlength="6" required placeholder="123456" aria-label="${esc(label)}">`;

/** Disable `button` for `seconds`, showing a countdown, so codes can't be re-requested too fast. */
let codeSentAt = 0;
const sinceCode = () => Math.max(0, 60 - Math.floor((Date.now() - codeSentAt) / 1000));

function cooldown(button, seconds = 60) {
  const label = button.dataset.idle || (button.dataset.idle = button.textContent);
  let left = seconds;
  button.disabled = true;
  const tick = () => {
    if (!button.isConnected) return;
    if (left <= 0) { button.disabled = false; button.textContent = label; return; }
    button.textContent = `${label} (${left--}s)`;
    setTimeout(tick, 1000);
  };
  tick();
}

/** Logged out: login email + recovery email -> code -> new password -> (optionally) a new login email. */
function viewRecover(root) {
  let request = { email: new URLSearchParams(location.search).get('email') || '', recovery_email: '' };

  const start = () => {
    root.innerHTML = `${brand}<h1>Recover account</h1>
      <p class="muted">Can't get into your login email? Enter it together with the recovery email you verified on your Account page. We'll send a 6-digit code to the recovery email.</p>
      <form id="rc-form">
        <label for="rc-email">Login email</label><input id="rc-email" name="email" type="email" autocomplete="username" required value="${esc(request.email)}">
        <label for="rc-backup">Recovery email</label><input id="rc-backup" name="recovery_email" type="email" autocomplete="email" required value="${esc(request.recovery_email)}">
        <button class="primary" type="submit">Send code</button>
      </form>
      <small class="muted">No recovery email set up? <a href="/forgot-password" data-link>Reset your password by email</a> instead.<br><a href="/login" data-link>Back to log in</a></small>`;
    $(request.email ? '#rc-backup' : '#rc-email').focus();
    $('#rc-form').addEventListener('submit', e => {
      e.preventDefault();
      request = { email: $('#rc-email').value.trim(), recovery_email: $('#rc-backup').value.trim() };
      busy(e.submitter, async () => { const r = await post('/api/auth/recover', request); codeSentAt = Date.now(); enterCode(r.message); });
    });
  };

  const enterCode = message => {
    root.innerHTML = `${brand}<h1>Enter your code</h1><p class="muted">${esc(message)}</p>
      <form id="rc-code-form">
        <label for="rc-code">6-digit code</label>${codeInput('rc-code')}
        <button class="primary" type="submit">Continue</button>
      </form>
      <div class="report-actions"><button type="button" class="secondary small" id="rc-resend">Send a new code</button><button type="button" class="secondary small" id="rc-restart">Start again</button></div>`;
    $('#rc-code').focus();
    cooldown($('#rc-resend'), sinceCode());
    $('#rc-resend').addEventListener('click', e => {
      const btn = e.currentTarget;
      busy(btn, async () => { await post('/api/auth/recover', request); codeSentAt = Date.now(); toast('If the details match, a new code is on its way'); }).then(() => cooldown(btn, sinceCode()));
    });
    $('#rc-restart').addEventListener('click', start);
    $('#rc-code-form').addEventListener('submit', e => {
      e.preventDefault();
      busy(e.submitter, async () => { await post('/api/auth/recover/verify', { email: request.email, code: $('#rc-code').value.trim() }); newPassword(); });
    });
  };

  const newPassword = () => {
    root.innerHTML = `${brand}<h1>Choose a new password</h1><p class="muted">Code accepted. Set a new password for <b>${esc(request.email)}</b> - you have 15 minutes. Other devices will be logged out.</p>
      <form id="rc-pw-form">
        <label for="rc-password">New password</label><input id="rc-password" name="password" type="password" autocomplete="new-password" required minlength="8" maxlength="128">
        <label for="rc-confirm">Confirm new password</label><input id="rc-confirm" name="confirm" type="password" autocomplete="new-password" required minlength="8">
        <button class="primary" type="submit">Set password &amp; log in</button>
      </form>`;
    addPasswordToggles(root);
    $('#rc-password').focus();
    $('#rc-pw-form').addEventListener('submit', e => {
      e.preventDefault();
      const data = formData(e.target);
      if (data.password !== data.confirm) return toast('Passwords do not match', true);
      busy(e.submitter, async () => {
        const r = await post('/api/auth/recover/password', { password: data.password });
        setUser(r.user, r.csrf);
        loginEmailQuestion(data.password);
      });
    });
  };

  // Logged in now. Most people recover because the login mailbox is gone, so offer to replace it.
  const loginEmailQuestion = password => {
    root.innerHTML = `${brand}<h1>You're back in</h1>
      <p class="muted">Your password was changed and other devices were logged out. Can you still get into <b>${esc(state.user.email)}</b>? If not, switch your login email to one you can use.</p>
      <div class="report-actions"><button class="primary" id="rc-done">Yes, go to dashboard</button><button class="secondary" id="rc-change">No, change my login email</button></div>`;
    $('#rc-done').addEventListener('click', () => navigate('/properties', true));
    $('#rc-change').addEventListener('click', () => newLoginEmail(password));
  };

  const newLoginEmail = password => {
    root.innerHTML = `${brand}<h1>New login email</h1><p class="muted">We'll send a code to the new address to confirm it.</p>
      <form id="rc-new-email">
        <label for="rc-new">New login email</label><input id="rc-new" type="email" autocomplete="email" required>
        <button class="primary" type="submit">Send code</button>
      </form>
      <small class="muted"><a href="/properties" data-link>Skip for now</a> - you can do this later on the Account page.</small>`;
    $('#rc-new').focus();
    $('#rc-new-email').addEventListener('submit', e => {
      e.preventDefault();
      const email = $('#rc-new').value.trim();
      busy(e.submitter, async () => { await post('/api/account/email', { email, password }); codeSentAt = Date.now(); confirmLoginEmail(email); });
    });
  };

  const confirmLoginEmail = email => {
    root.innerHTML = `${brand}<h1>Confirm new email</h1><p class="muted">We sent a 6-digit code to <b>${esc(email)}</b>. It expires in 10 minutes.</p>
      <form id="rc-new-code">
        <label for="rc-new-code-input">6-digit code</label>${codeInput('rc-new-code-input')}
        <button class="primary" type="submit">Confirm</button>
      </form>
      <small class="muted"><a href="/account" data-link>Finish later on the Account page</a></small>`;
    $('#rc-new-code-input').focus();
    $('#rc-new-code').addEventListener('submit', e => {
      e.preventDefault();
      busy(e.submitter, async () => {
        const r = await post('/api/account/email/verify', { code: $('#rc-new-code-input').value.trim() });
        setUser(r.user);
        toast('Login email updated');
        navigate('/properties', true);
      });
    });
  };

  start();
}

async function viewInvite(root, token, current) {
  if (state.user) {
    root.innerHTML = `${brand}<h1>Invite</h1><p class="muted">You're logged in as ${esc(state.user.email)}. Log out first to accept an invite for another account.</p><button class="secondary" id="inv-logout">Log out</button>`;
    $('#inv-logout').addEventListener('click', async () => { await logout(); navigate(`/invite/${token}`, true); });
    return;
  }
  const invite = await api(`/api/invites/${enc(token)}`).catch(e => ({ error: e.message }));
  if (!current()) return;
  if (invite.error) {
    root.innerHTML = `${brand}<h1>Invite unavailable</h1><p class="muted">${esc(invite.error)}</p><a class="secondary link-button" href="/login" data-link>Go to login</a>`;
    return;
  }
  root.innerHTML = `${brand}<h1>Welcome, ${esc(invite.name)}</h1>
    <p class="muted">${esc(invite.invited_by || 'An agent')} invited you to SpaceMatch as a broker. Set a password to finish your account.</p>
    <form id="invite-form">
      <label>Email</label><input value="${esc(invite.email)}" disabled>
      <label for="inv-password">Password</label><input id="inv-password" name="password" type="password" autocomplete="new-password" required minlength="8" maxlength="128">
      <label for="inv-confirm">Confirm password</label><input id="inv-confirm" name="confirm" type="password" autocomplete="new-password" required minlength="8">
      <button class="primary" type="submit">Set password &amp; continue</button>
    </form>`;
  $('#invite-form').addEventListener('submit', e => {
    e.preventDefault();
    const data = formData(e.target);
    if (data.password !== data.confirm) return toast('Passwords do not match', true);
    busy(e.submitter, async () => {
      const r = await post(`/api/invites/${enc(token)}/accept`, { password: data.password });
      setUser(r.user, r.csrf);
      toast('Your account is ready');
      navigate('/properties', true);
    });
  });
}

/* ----------------------------------------------------------- properties list -- */

async function viewProperties(root, current) {
  setCrumbs(['Properties']);
  const owner = state.user.role === 'agent';
  const props = await api('/api/properties');
  if (!current()) return;
  root.innerHTML = `
    <section class="section page-head">
      <div><h2>${owner ? 'Your properties' : 'Properties shared with you'}</h2>
      <p class="subtle">${owner ? 'Add a property, then attach its social links (reels).' : 'Agents share properties with you by invite.'}</p></div>
      ${owner ? `<div><button class="primary" id="open-add-property">+ Add property</button></div>` : ''}
    </section>
    <section class="section bottom-section">
      ${props.length ? `<div class="property-grid">${props.map(propertyCard).join('')}</div>`
        : `<div class="empty">${owner ? 'No properties yet. Add your first one above.' : 'No properties have been shared with you yet.'}</div>`}
    </section>`;
  $('#open-add-property')?.addEventListener('click', openAddPropertyModal);
}

function propertyCard(p) {
  return `<a class="property-card" href="/properties/${esc(p.id)}" data-link>
    <div class="property-card-top"><strong>${esc(p.address)}</strong><span class="tag-row">${p.active ? '' : '<span class="tag tag-amber">ARCHIVED</span>'}${p.access === 'agent' ? '' : accessTag(p.access)}</span></div>
    ${p.access !== 'agent' ? `<small class="muted">Agent: ${esc(p.agent_name || '—')}</small>` : ''}
    <div class="property-card-stats"><div><small>Social links</small><b>${num(p.reel_count)}</b></div><div><small>Views</small><b>${compact(p.total_views)}</b></div></div>
  </a>`;
}

/* ---------------------------------------------------------- property detail -- */

const platformLabels = { instagram: '◎ Instagram', facebook: 'f Facebook', youtube: '▶ YouTube', tiktok: '♪ TikTok' };
const metricDefs = [['views', 'Views'], ['likes', 'Likes'], ['comments', 'Comments'], ['shares', 'Shares']];
const sumKnown = values => { const v = values.filter(isNum); return v.length ? v.reduce((a, b) => a + Number(b), 0) : null; };

async function viewProperty(root, id, current) {
  const [prop, data] = await Promise.all([api(`/api/properties/${enc(id)}`), api(`/api/properties/${enc(id)}/reels`)]);
  if (!current()) return;
  setCrumbs([['Properties', '/properties'], prop.address]);
  const access = prop.access, owner = access === 'agent', archived = !prop.active;
  const editable = canEdit(access), canFetch = editable && !archived;
  const reels = data.reels;
  root.innerHTML = `
    <section class="section property-head">
      <div class="property-title">
        <div><h2 id="prop-address">${esc(prop.address)}</h2>
        ${archived ? `<p class="subtle"><span class="tag tag-amber">ARCHIVED</span>&nbsp; Daily refresh is paused. Unarchive to fetch again.</p>` : ''}
        ${owner ? '' : `<p class="subtle">${accessTag(access)}&nbsp; Agent: ${esc(prop.agent_name || '—')}</p>`}</div>
        ${owner ? `<div class="report-actions"><button class="secondary" id="edit-property">Edit</button><button class="secondary" id="toggle-archive">${archived ? 'Unarchive' : 'Archive'}</button><button class="secondary danger" id="delete-property">Delete</button></div>` : ''}
      </div>
      ${owner ? `<form id="edit-property-form" class="inline-form hidden"><input name="address" required minlength="4" maxlength="240" value="${esc(prop.address)}"><button class="primary" type="submit">Save</button><button class="secondary" type="button" id="cancel-edit">Cancel</button></form>` : ''}
    </section>
    <section class="stats" aria-label="Summary">
      <article class="stat"><div class="stat-title">Views</div><div class="stat-number">${compact(sumKnown(reels.map(r => r.metrics?.views)))}</div></article>
      <article class="stat"><div class="stat-title">Interactions</div><div class="stat-number">${compact(sumKnown(reels.flatMap(r => [r.metrics?.likes, r.metrics?.comments, r.metrics?.shares])))}</div></article>
      <article class="stat"><div class="stat-title">Social links</div><div class="stat-number">${num(reels.length)}</div></article>
      <article class="stat"><div class="stat-title">Fetched</div><div class="stat-number">${num(reels.filter(r => r.metrics_updated_at).length)}</div></article>
    </section>
    ${editable ? `<section class="section"><form id="attach-url-form" class="attach-row"><input name="url" type="url" required placeholder="Paste an Instagram, Facebook, YouTube or TikTok reel link" autocomplete="url" aria-label="Reel link"><button class="primary" type="submit">Add social link</button></form></section>` : ''}
    ${owner ? brokerStrip(prop) : ''}
    <section class="section bottom-section">
      <div class="section-heading"><h2>Social links</h2><div class="report-actions">
        ${canFetch ? `<label class="toggle"><input type="checkbox" id="show-window" ${state.me?.can_show_window === false ? 'disabled title="No screen on the server machine"' : ''}> Show browser window</label><button class="primary" id="fetch-all" ${reels.length ? '' : 'disabled'}>▶ Fetch all</button>` : ''}
        <a class="secondary link-button" href="/api/properties/${esc(id)}/export.csv">↓ CSV</a>
      </div></div>
      <div class="linked-list">${reels.length ? reels.map(r => reelCard(r, editable, canFetch, owner)).join('') : `<div class="empty">No social links yet.${editable ? ' Paste a reel link above.' : ''}</div>`}</div>
    </section>`;

  const reload = () => current() && render();
  const showWindow = () => !!$('#show-window')?.checked;

  if (owner) {
    const form = $('#edit-property-form');
    $('#edit-property').addEventListener('click', () => form.classList.toggle('hidden'));
    $('#cancel-edit').addEventListener('click', () => form.classList.add('hidden'));
    form.addEventListener('submit', e => { e.preventDefault(); busy(e.submitter, async () => { await post(`/api/properties/${enc(id)}`, formData(form), 'PATCH'); toast('Property updated'); reload(); }); });
    $('#toggle-archive').addEventListener('click', e => archived
      ? busy(e.currentTarget, async () => { await post(`/api/properties/${enc(id)}/unarchive`); toast('Property unarchived'); reload(); })
      : confirmModal('Archive property?', `<b>${esc(prop.address)}</b> stops being refreshed every day. Its links and history are kept, and you can unarchive it any time.`, 'Archive',
          () => busy(e.target, async () => { await post(`/api/properties/${enc(id)}/archive`); toast('Property archived'); reload(); })));
    $('#delete-property').addEventListener('click', e => confirmModal('Delete property?',
      `This removes <b>${esc(prop.address)}</b>, its social links, metric history and broker access. This can't be undone.`, 'Delete property',
      () => busy(e.target, async () => { await api(`/api/properties/${enc(id)}`, { method: 'DELETE' }); toast('Property deleted'); navigate('/properties'); })));
  }
  $('#attach-url-form')?.addEventListener('submit', e => {
    e.preventDefault();
    busy(e.submitter, async () => { await post(`/api/properties/${enc(id)}/reels`, formData(e.target)); toast('Social link added'); reload(); });
  });
  $('#fetch-all')?.addEventListener('click', e => busy(e.currentTarget, async () => {
    const r = await post(`/api/properties/${enc(id)}/fetch-public`, { show_window: showWindow() });
    watchJob(r.job_id, reload);
  }));
  $$('button[data-action]', root).forEach(btn => btn.addEventListener('click', () => {
    const reelId = btn.dataset.id;
    if (btn.dataset.action === 'fetch') busy(btn, async () => { const r = await post(`/api/properties/${enc(id)}/fetch-public`, { show_window: showWindow(), reel_ids: [reelId] }); watchJob(r.job_id, reload); });
    if (btn.dataset.action === 'history') busy(btn, () => showHistory(reelId));
    if (btn.dataset.action === 'scan') openScanModal(btn.dataset.url);
    if (btn.dataset.action === 'detach') busy(btn, async () => { await api(`/api/properties/${enc(id)}/reels/${enc(reelId)}`, { method: 'DELETE' }); toast('Social link removed'); reload(); });
  }));
}

function brokerStrip(prop) {
  const list = prop.brokers || [];
  return `<section class="section"><div class="section-heading"><h2>Brokers</h2><a class="secondary link-button" href="/brokers?property=${esc(prop.id)}" data-link>+ Invite broker</a></div>
    ${list.length ? `<div class="chip-row">${list.map(b => `<span class="chip">${esc(b.name)} ${accessTag(b.access)}${b.active ? '' : ' <span class="tag tag-amber">INVITED</span>'}</span>`).join('')}</div>`
      : '<p class="subtle">No brokers on this property yet.</p>'}</section>`;
}

function reelCard(r, editable, canFetch = editable, canScan = false) {
  const caption = esc((r.caption || r.permalink || 'Reel').slice(0, 180));
  const open = r.permalink ? `<a href="${esc(r.permalink)}" target="_blank" rel="noopener noreferrer">Open ↗</a>` : '';
  const iframe = r.embed_url ? `<iframe src="${esc(r.embed_url)}" title="${esc(r.platform)} reel" loading="lazy" referrerpolicy="strict-origin-when-cross-origin" sandbox="allow-scripts allow-same-origin allow-popups allow-forms" allowfullscreen></iframe>` : '<p>No preview</p>';
  const metrics = metricDefs.map(([k, label]) => { const f = num(r.metrics?.[k]); return `<div class="metric"><small>${label}</small><strong class="${f === 'N/A' ? 'na' : ''}">${esc(f)}</strong></div>`; }).join('');
  const actions = (canFetch ? `<button class="secondary" data-action="fetch" data-id="${esc(r.id)}">↻ Fetch</button>` : '')
    + `<button class="secondary" data-action="history" data-id="${esc(r.id)}">History</button>`
    // Profile JSON (Instagram profile scan) is disabled for now - single-video Fetch reads Instagram via Apify.
    // + (canScan && r.platform === 'instagram' && r.permalink ? `<button class="secondary" data-action="scan" data-url="${esc(r.permalink)}" title="Scan the account's Reels tab and show every reel as JSON">Profile JSON</button>` : '')
    + (editable ? `<button class="secondary" data-action="detach" data-id="${esc(r.id)}" title="Remove from property">×</button>` : '');
  return `<article class="reel-card"><div class="reel-top"><div class="details"><strong>${caption}</strong><small>${esc(platformLabels[r.platform] || r.platform)}</small></div><div class="actions">${actions}</div></div>
    <div class="reel-content"><div class="reel-embed">${iframe}${open}</div><div class="metrics-grid">${metrics}</div></div>
    ${r.metrics_updated_at ? `<div class="reel-footer">Updated ${esc(new Date(r.metrics_updated_at).toLocaleString())}</div>` : ''}</article>`;
}

async function showHistory(reelId) {
  const history = await api(`/api/reels/${enc(reelId)}/history`);
  openModal('History', history.length ? `<table class="table"><thead><tr><th>Fetched</th><th>Views</th><th>Likes</th><th>Comments</th><th>Shares</th></tr></thead><tbody>${history.map(h => {
    const m = JSON.parse(h.metrics_json || '{}');
    return `<tr><td>${esc(new Date(h.fetched_at).toLocaleString())}</td><td>${num(m.views)}</td><td>${num(m.likes)}</td><td>${num(m.comments)}</td><td>${num(m.shares)}</td></tr>`;
  }).join('')}</tbody></table>` : '<div class="empty">No snapshots yet.</div>');
}

/* ------------------------------------------------------ add property modal -- */

const PLATFORM_HOSTS = [[/(^|\.)instagram\.com$/, 'instagram'], [/(^|\.)facebook\.com$/, 'facebook'],
  [/(^|\.)(youtube\.com|youtu\.be)$/, 'youtube'], [/(^|\.)tiktok\.com$/, 'tiktok']];
function detectPlatform(url) {
  try { const host = new URL(url.trim()).hostname.toLowerCase(); return (PLATFORM_HOSTS.find(([re]) => re.test(host)) || [])[1] || null; }
  catch { return null; }
}

function linkRow() {
  const row = document.createElement('div');
  row.className = 'link-row';
  row.innerHTML = `<input type="url" name="link" placeholder="Paste an Instagram, Facebook, TikTok or YouTube reel link" aria-label="Reel link">
    <span class="platform-tag muted"></span><button type="button" class="secondary small" data-remove-row aria-label="Remove link">×</button>
    <small class="field-error" role="alert"></small>`;
  const input = $('input', row), tag = $('.platform-tag', row);
  input.addEventListener('input', () => {
    const p = detectPlatform(input.value);
    tag.textContent = input.value.trim() ? (platformLabels[p] || 'Unknown') : '';
    tag.classList.toggle('bad', !!input.value.trim() && !p);
    setFieldError(row, '');
  });
  return row;
}

/** Show (or clear, with '') an error message inside a form field's container. */
function setFieldError(container, message) {
  const el = $('.field-error', container);
  if (el) el.textContent = message;
  container.classList.toggle('has-error', !!message);
  $$('input', container).forEach(i => i.setAttribute('aria-invalid', message ? 'true' : 'false'));
}

function openAddPropertyModal() {
  openModal('Add property', `<form id="new-property-form" class="modal-form" novalidate>
      <div class="field" id="np-address"><label for="np-address-input">Property address</label>
        <input id="np-address-input" name="address" maxlength="240" placeholder="e.g. 1620 N Hoyne Ave, Chicago, IL">
        <small class="field-error" role="alert"></small></div>
      <div class="field" id="np-links"><div class="links-head"><b>Reel links</b><small class="muted">At least one. Add as many as you like, from any platform.</small></div>
        <small class="field-error" role="alert"></small></div>
      <div id="link-rows"></div>
      <button type="button" class="secondary small" id="add-link-row">+ Add another link</button>
      <p class="form-error" id="np-error" role="alert"></p>
      <div class="report-actions modal-actions"><button type="button" class="secondary" data-close-modal>Cancel</button><button class="primary" type="submit">Create property</button></div>
    </form>`);
  const form = $('#new-property-form'), rows = $('#link-rows');
  const refreshRemoveButtons = () => $$('[data-remove-row]', rows).forEach(b => { b.disabled = rows.children.length === 1; });
  const addRow = () => { const r = linkRow(); rows.appendChild(r); refreshRemoveButtons(); $('input', r).focus(); setFieldError($('#np-links'), ''); };
  rows.appendChild(linkRow()); refreshRemoveButtons();
  $('#add-link-row').addEventListener('click', addRow);
  rows.addEventListener('click', e => { const b = e.target.closest('[data-remove-row]'); if (b && rows.children.length > 1) { b.parentElement.remove(); refreshRemoveButtons(); } });
  $('#np-address-input').addEventListener('input', () => setFieldError($('#np-address'), ''));
  $('#np-address-input').focus();

  form.addEventListener('submit', async e => {
    e.preventDefault();
    const clear = () => { [$('#np-address'), $('#np-links'), ...rows.children].forEach(c => setFieldError(c, '')); $('#np-error').textContent = ''; };
    clear();
    const address = $('#np-address-input').value.trim();
    const filled = [...rows.children].filter(r => $('input', r).value.trim());
    let first = null;
    const fail = (container, message, focus) => { setFieldError(container, message); first = first || focus; };
    if (address.length < 4) fail($('#np-address'), 'Enter the property address (at least 4 characters).', $('#np-address-input'));
    if (!filled.length) fail($('#np-links'), 'Add at least one reel link.', $('input', rows.children[0]));
    for (const r of filled) {
      if (!detectPlatform($('input', r).value)) fail(r, 'Not an Instagram, Facebook, TikTok or YouTube link.', $('input', r));
    }
    if (first) { first.focus(); return; }

    const button = e.submitter, label = button.textContent;
    button.disabled = true; button.textContent = 'Creating…';
    try {
      const p = await post('/api/properties', { address, links: filled.map(r => $('input', r).value.trim()) });
      closeModal();
      toast('Property added');
      navigate(`/properties/${p.id}`);
    } catch (err) {
      const m = /^Link (\d+): (.*)$/.exec(err.message);  // server names the bad link by position
      const row = m && filled[Number(m[1]) - 1];
      if (row) { setFieldError(row, m[2]); $('input', row).focus(); } else { $('#np-error').textContent = err.message; }
    } finally {
      if (button.isConnected) { button.disabled = false; button.textContent = label; }
    }
  });
}

/* ------------------------------------------------------------------ brokers -- */

async function viewBrokers(root, current) {
  setCrumbs(['Brokers']);
  const [brokers, props] = await Promise.all([api('/api/brokers'), api('/api/properties')]);
  if (!current()) return;
  const preselect = new URLSearchParams(location.search).get('property');
  const options = props.map(p => `<option value="${esc(p.id)}" ${p.id === preselect ? 'selected' : ''}>${esc(p.address)}</option>`).join('');
  root.innerHTML = `
    <section class="section">
      <div class="section-heading"><div><h2>Invite a broker</h2><p class="subtle">They get an email to set their password. Existing brokers are simply given access.</p></div></div>
      ${props.length ? `<form id="invite-form" class="invite-grid">
        <label>Broker email<input name="email" type="email" required autocomplete="off"></label>
        <label>Name<input name="name" maxlength="120" placeholder="Needed for new brokers"></label>
        <label>Brokerage<input name="brokerage" maxlength="160" placeholder="Optional"></label>
        <label>Property<select name="property_id" required>${options}</select></label>
        <label>Access<select name="access"><option value="view">View only</option><option value="full">Full access</option></select></label>
        <button class="primary" type="submit">Send invite</button>
      </form><div id="invite-result"></div>` : `<div class="empty">Add a property first, then invite brokers to it. <a href="/properties" data-link>Go to properties</a></div>`}
    </section>
    <section class="section bottom-section">
      <div class="section-heading"><h2>Your brokers</h2></div>
      ${brokers.length ? brokers.map(brokerRow).join('') : '<div class="empty">No brokers yet.</div>'}
    </section>`;

  $('#invite-form')?.addEventListener('submit', e => {
    e.preventDefault();
    busy(e.submitter, async () => {
      const r = await post('/api/brokers/invite', formData(e.target));
      e.target.reset();
      await viewBrokers(root, current);
      showInviteResult(r, r.pending ? 'Invite created' : 'Access granted');
    });
  });
  $$('select[data-access]', root).forEach(sel => sel.addEventListener('change', async () => {
    sel.disabled = true;
    try {
      await post(`/api/properties/${enc(sel.dataset.property)}/brokers/${enc(sel.dataset.broker)}`, { access: sel.value }, 'PATCH');
      toast('Access updated');
    } catch (e) { toast(e.message, true); viewBrokers(root, current); }
    finally { sel.disabled = false; }
  }));
  $$('button[data-remove]', root).forEach(btn => btn.addEventListener('click', () => confirmModal('Remove broker?',
    `They will lose access to <b>${esc(btn.dataset.address)}</b>.`, 'Remove',
    () => busy(btn, async () => { await api(`/api/properties/${enc(btn.dataset.property)}/brokers/${enc(btn.dataset.remove)}`, { method: 'DELETE' }); toast('Broker removed'); viewBrokers(root, current); }))));
  $$('button[data-resend]', root).forEach(btn => btn.addEventListener('click', () => busy(btn, async () => {
    const r = await post(`/api/brokers/${enc(btn.dataset.resend)}/resend`);
    showInviteResult(r, 'Invite re-sent');
  })));
}

function showInviteResult(r, title) {
  if (r.invite_link) {
    openModal(title, `<p class="subtle">Email isn't configured on this server (SMTP settings in <code>.env</code>), so send this link to the broker yourself:</p>
      <div class="copy-row"><input id="invite-link" readonly value="${esc(r.invite_link)}"><button class="primary" id="copy-link">Copy</button></div>`);
    $('#copy-link').addEventListener('click', () => { $('#invite-link').select(); navigator.clipboard?.writeText(r.invite_link).then(() => toast('Link copied')); });
  } else {
    toast(r.emailed ? `${title} - email sent` : `${title}, but the email could not be sent`, !r.emailed);
  }
}

function brokerRow(b) {
  return `<article class="broker-card">
    <div class="broker-head"><div><strong>${esc(b.name)}</strong> ${b.active ? '<span class="tag tag-green">ACTIVE</span>' : '<span class="tag tag-amber">INVITE PENDING</span>'}
      <small class="muted">${esc(b.email)}${b.brokerage ? ' · ' + esc(b.brokerage) : ''}</small></div>
      ${b.active ? '' : `<button class="secondary small" data-resend="${esc(b.id)}">Resend invite</button>`}</div>
    <table class="table"><tbody>${b.properties.map(p => `<tr>
      <td><a href="/properties/${esc(p.property_id)}" data-link>${esc(p.address)}</a></td>
      <td class="narrow"><select data-access data-broker="${esc(b.id)}" data-property="${esc(p.property_id)}" aria-label="Access level">
        <option value="view" ${p.access === 'view' ? 'selected' : ''}>View only</option><option value="full" ${p.access === 'full' ? 'selected' : ''}>Full access</option></select></td>
      <td class="narrow"><button class="secondary small danger" data-remove="${esc(b.id)}" data-property="${esc(p.property_id)}" data-address="${esc(p.address)}">Remove</button></td>
    </tr>`).join('')}</tbody></table>
  </article>`;
}

/* ------------------------------------------------------------------ account -- */

async function viewAccount(root, current) {
  setCrumbs(['Account']);
  const account = await api('/api/account');
  if (!current()) return;
  renderAccount(root, account);
}

/** A sent-code box: enter the code, resend it, or cancel. `kind` is 'recovery-email' or 'email'. */
const pendingCode = (kind, email) => `<div class="pending-box">
    <p>We sent a 6-digit code to <b>${esc(email)}</b>. It expires in 10 minutes.</p>
    <form class="inline-form" data-verify="${kind}">${codeInput(`code-${kind}`)}<button class="primary" type="submit">Verify</button></form>
    <div class="report-actions"><button class="secondary small" data-code-resend="${kind}">Resend code</button><button class="secondary small" data-code-cancel="${kind}">Cancel</button></div>
  </div>`;

const emailPasswordForm = (id, emailLabel, button, placeholder = '') => `<form class="account-form" id="${id}">
    <label>${esc(emailLabel)}<input name="email" type="email" required autocomplete="off" placeholder="${esc(placeholder)}"></label>
    <label>Current password<input name="password" type="password" required autocomplete="current-password"></label>
    <button class="primary" type="submit">${esc(button)}</button>
  </form>`;

function renderAccount(root, a) {
  const rerender = next => renderAccount(root, next);
  const verified = !!a.recovery_email;
  root.innerHTML = `
    <section class="section page-head"><div><h2>Account</h2><p class="subtle">Your login details, and how to get back in if you lose access to your email.</p></div></section>
    <section class="section">
      <div class="account-card">
        <div class="account-card-head"><h3>Login</h3>${a.pending_login_email ? '' : '<button class="secondary small" id="show-change-email">Change login email</button>'}</div>
        <dl class="account-facts"><dt>Name</dt><dd>${esc(a.name)}</dd><dt>Role</dt><dd>${a.role === 'agent' ? 'Agent' : 'Broker'}</dd><dt>Login email</dt><dd>${esc(a.email)}</dd></dl>
        ${a.pending_login_email ? pendingCode('email', a.pending_login_email)
          : `<div id="change-email-box" class="hidden">${emailPasswordForm('change-email-form', 'New login email', 'Send code')}</div>`}
      </div>
    </section>
    <section class="section bottom-section">
      <div class="account-card">
        <div class="account-card-head"><h3>Recovery email</h3>${verified ? '<span class="tag tag-green">VERIFIED</span>' : '<span class="tag tag-amber">NOT SET</span>'}</div>
        <p class="subtle">If you lose access to <b>${esc(a.email)}</b>, you can recover your account through this address. Use one you'll keep, like a personal email.</p>
        ${verified ? `<dl class="account-facts"><dt>Recovery email</dt><dd>${esc(a.recovery_email)}</dd></dl>
          <div class="report-actions"><button class="secondary small" id="show-change-recovery">Change</button><button class="secondary small danger" id="show-remove-recovery">Remove</button></div>
          <form class="account-form hidden" id="remove-recovery-form"><label>Current password<input name="password" type="password" required autocomplete="current-password"></label><button class="primary danger-fill" type="submit">Remove recovery email</button></form>` : ''}
        ${a.pending_recovery_email ? pendingCode('recovery-email', a.pending_recovery_email)
          : `<div id="recovery-form-box" class="${verified ? 'hidden' : ''}">${emailPasswordForm('recovery-form', verified ? 'New recovery email' : 'Recovery email', 'Send code', 'e.g. your personal email')}</div>`}
      </div>
    </section>`;
  addPasswordToggles(root);

  const toggle = (button, box) => $(button, root)?.addEventListener('click', () => { $(box, root).classList.toggle('hidden'); $(`${box} input`, root)?.focus(); });
  toggle('#show-change-email', '#change-email-box');
  toggle('#show-change-recovery', '#recovery-form-box');
  toggle('#show-remove-recovery', '#remove-recovery-form');

  $('#recovery-form', root)?.addEventListener('submit', e => {
    e.preventDefault();
    busy(e.submitter, async () => {
      const r = await post('/api/account/recovery-email', formData(e.target));
      codeSentAt = Date.now();
      rerender(r);
      toast(r.same_domain ? 'Code sent. Tip: an address at a different domain is safer if you leave your company.' : 'Code sent - check that inbox');
    });
  });
  $('#change-email-form', root)?.addEventListener('submit', e => {
    e.preventDefault();
    busy(e.submitter, async () => { const r = await post('/api/account/email', formData(e.target)); codeSentAt = Date.now(); rerender(r); toast('Code sent to your new email'); });
  });
  $('#remove-recovery-form', root)?.addEventListener('submit', e => {
    e.preventDefault();
    busy(e.submitter, async () => { rerender(await post('/api/account/recovery-email/remove', formData(e.target))); toast('Recovery email removed'); });
  });
  $$('form[data-verify]', root).forEach(form => form.addEventListener('submit', e => {
    e.preventDefault();
    busy(e.submitter, async () => {
      const r = await post(`/api/account/${form.dataset.verify}/verify`, { code: $('input', form).value.trim() });
      if (r.user) setUser(r.user);
      rerender(r);
      toast(form.dataset.verify === 'email' ? 'Login email updated' : 'Recovery email verified');
    });
  }));
  $$('[data-code-resend]', root).forEach(btn => {
    cooldown(btn, sinceCode());
    btn.addEventListener('click', () => busy(btn, async () => { await post(`/api/account/${btn.dataset.codeResend}/resend`); codeSentAt = Date.now(); toast('New code sent'); }).then(() => cooldown(btn, sinceCode())));
  });
  $$('[data-code-cancel]', root).forEach(btn => btn.addEventListener('click', () =>
    busy(btn, async () => rerender(await post(`/api/account/${btn.dataset.codeCancel}/cancel`)))));
}

/* ----------------------------------------------------------------- API keys -- */

const SCOPES = [
  ['properties:read', 'Read properties'], ['properties:write', 'Create and edit properties'],
  ['socials:read', 'Read links and analytics'], ['socials:write', 'Set links'],
  ['refresh:write', 'Start refreshes'], ['logs:read', 'Read logs'],
];
const when = ts => ts ? new Date(ts).toLocaleString() : '—';

async function viewApiKeys(root, current) {
  setCrumbs(['API keys']);
  const keys = await api('/api/api-keys');
  if (!current()) return;
  const statusTag = k => ({ active: '<span class="tag tag-green">ACTIVE</span>', expired: '<span class="tag tag-amber">EXPIRED</span>', revoked: '<span class="tag tag-gray">REVOKED</span>' }[k.status]);
  root.innerHTML = `
    <section class="section page-head">
      <div><h2>API keys</h2>
      <p class="subtle">The Hub uses an API key to manage your properties and links and to read their analytics through the <code>/v1</code> API. Send it as <code>Authorization: Bearer sk_…</code>. A key can only reach your own properties. <a href="/api/docs#/Hub%20API%20v1" target="_blank" rel="noopener">API reference ↗</a></p></div>
      <form id="key-form" class="key-form">
        <label>Name<input name="name" required minlength="2" maxlength="80" placeholder="e.g. Hub production"></label>
        <label>Environment<select name="environment"><option value="production">Production (sk_live_)</option><option value="preview">Preview (sk_test_)</option></select></label>
        <label>Expires<select name="expires_in_days"><option value="">Never</option><option value="30">In 30 days</option><option value="90">In 90 days</option><option value="365">In 1 year</option></select></label>
        <fieldset class="scope-grid"><legend>Scopes</legend>${SCOPES.map(([s, label]) => `<label class="toggle"><input type="checkbox" name="scopes" value="${s}" checked> <span><code>${s}</code> <small class="muted">${label}</small></span></label>`).join('')}</fieldset>
        <button class="primary" type="submit">Create key</button>
      </form>
    </section>
    <section class="section bottom-section">
      ${keys.length ? `<table class="table keys-table"><thead><tr><th>Name</th><th>Key</th><th>Status</th><th>Last used</th><th>Expires</th><th></th></tr></thead><tbody>${keys.map(k => `<tr>
        <td><strong>${esc(k.name)}</strong><br><small class="muted">${esc(k.scopes.join(', '))}</small></td>
        <td><code>${esc(k.prefix)}</code></td><td>${statusTag(k)}</td><td>${esc(when(k.last_used_at))}</td><td>${esc(when(k.expires_at))}</td>
        <td class="narrow">${k.status === 'revoked' ? '' : `<button class="secondary small" data-rotate="${esc(k.id)}">Rotate</button> <button class="secondary small danger" data-revoke="${esc(k.id)}" data-name="${esc(k.name)}">Revoke</button>`}</td>
      </tr>`).join('')}</tbody></table>` : '<div class="empty">No API keys yet. Create one for the Hub above.</div>'}
    </section>`;

  const showSecret = (title, k, note = '') => {
    openModal(title, `<p class="subtle">Copy this key now and store it in the Hub's secret settings. <b>It won't be shown again.</b>${note}</p>
      <div class="copy-row"><input id="key-secret" readonly value="${esc(k.secret)}"><button class="primary" id="copy-key">Copy</button></div>`);
    $('#copy-key').addEventListener('click', () => { $('#key-secret').select(); navigator.clipboard?.writeText(k.secret).then(() => toast('Key copied')); });
  };
  $('#key-form').addEventListener('submit', e => {
    e.preventDefault();
    const form = new FormData(e.target);
    const scopes = form.getAll('scopes');
    if (!scopes.length) return toast('Pick at least one scope', true);
    busy(e.submitter, async () => {
      const k = await post('/api/api-keys', { name: form.get('name'), environment: form.get('environment'), scopes,
        expires_in_days: form.get('expires_in_days') ? Number(form.get('expires_in_days')) : null });
      await viewApiKeys(root, current);
      showSecret('API key created', k);
    });
  });
  $$('button[data-rotate]', root).forEach(btn => btn.addEventListener('click', () => confirmModal('Rotate key?',
    'A new secret is created. The old one keeps working for <b>24 hours</b> so the Hub can switch over.', 'Rotate',
    () => busy(btn, async () => {
      const k = await post(`/api/api-keys/${enc(btn.dataset.rotate)}/rotate`, { grace_hours: 24 });
      await viewApiKeys(root, current);
      showSecret('Key rotated', k, ' The previous key stops working in 24 hours.');
    }))));
  $$('button[data-revoke]', root).forEach(btn => btn.addEventListener('click', () => confirmModal('Revoke key?',
    `<b>${esc(btn.dataset.name)}</b> stops working immediately. Anything using it (like the Hub) will get 401 errors.`, 'Revoke',
    () => busy(btn, async () => { await api(`/api/api-keys/${enc(btn.dataset.revoke)}`, { method: 'DELETE' }); toast('Key revoked'); viewApiKeys(root, current); }))));
}

/* ---------------------------------------------- Instagram profile scan modal -- */
// Go to the reel owner's Reels tab, scroll, collect every reel's JSON, show it as a table + JSON tree.

let scanToken = 0;  // bumps when the modal closes or a new scan starts, so old polls stop

function jsonTree(value, key = null, depth = 0) {
  const label = key === null ? '' : `<span class="jt-key">${esc(key)}</span>: `;
  if (value === null) return `<div class="jt-row">${label}<span class="jt-null">null</span></div>`;
  if (Array.isArray(value) || typeof value === 'object') {
    const entries = Array.isArray(value) ? value.map((v, i) => [i, v]) : Object.entries(value);
    const summary = Array.isArray(value) ? `[${entries.length}]` : `{${entries.length}}`;
    return `<details class="jt-node" ${depth < 2 ? 'open' : ''}><summary>${label}<span class="jt-meta">${summary}</span></summary>
      ${entries.map(([k, v]) => jsonTree(v, k, depth + 1)).join('')}</details>`;
  }
  const cls = typeof value === 'number' ? 'jt-num' : typeof value === 'boolean' ? 'jt-bool' : 'jt-str';
  const shown = typeof value === 'string' ? `"${esc(value)}"` : esc(String(value));
  return `<div class="jt-row">${label}<span class="${cls}">${shown}</span></div>`;
}

function scanTable(data) {
  const target = data.target?.shortcode;
  if (!data.reels.length) return '<div class="empty">No reels were collected. Instagram may be hiding the Reels tab from this server.</div>';
  return `<div class="scan-table-wrap"><table class="table scan-table"><thead><tr><th>#</th><th>Reel</th><th>Plays</th><th>Likes</th><th>Comments</th></tr></thead>
    <tbody>${data.reels.map((r, i) => `<tr class="${r.shortcode === target ? 'is-target' : ''}">
      <td>${i + 1}</td><td><a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.shortcode)}</a>${r.shortcode === target ? ' <span class="tag tag-green">PASTED</span>' : ''}</td>
      <td>${num(r.play_count)}</td><td>${num(r.like_count)}</td><td>${num(r.comment_count)}</td></tr>`).join('')}</tbody></table></div>`;
}

function renderScanResult(box, scan) {
  const d = scan.data;
  const t = d.target;
  const jsonUrl = `/api/profile-scans/${enc(scan.id)}/json`;
  box.innerHTML = `
    <div class="scan-summary">
      <span><b>@${esc(d.username)}</b></span><span>${num(d.reel_count)} reels</span>${d.scrolls != null ? `<span>${num(d.scrolls)} scrolls</span>` : ''}
      <span class="muted">via ${d.provider === 'apify' ? 'Apify' : 'browser'}</span>
      <span class="muted">collected ${esc(new Date(d.collected_at).toLocaleString())}</span>
      ${t ? `<span class="tag ${t.found ? 'tag-green' : 'tag-amber'}">PASTED REEL ${t.found ? 'FOUND' : 'NOT IN THIS BATCH'}</span>` : ''}
    </div>
    ${t && t.found ? `<p class="scan-target">Pasted reel <b>${esc(t.shortcode)}</b>: ${num(t.reel.play_count)} plays · ${num(t.reel.like_count)} likes · ${num(t.reel.comment_count)} comments</p>`
      : t ? `<p class="scan-target muted">The pasted reel <b>${esc(t.shortcode)}</b> isn't among the reels collected - collect more to include it.${t.reel ? ` From its own link: ${num(t.reel.play_count)} plays · ${num(t.reel.like_count)} likes · ${num(t.reel.comment_count)} comments.` : ''}</p>` : ''}
    <div class="scan-actions">
      <div class="tabs" role="tablist"><button class="tab active" data-tab="table" role="tab">Table</button><button class="tab" data-tab="json" role="tab">JSON</button></div>
      <div class="report-actions"><a class="secondary link-button small" href="${jsonUrl}" target="_blank" rel="noopener">Open JSON ↗</a>
        <button class="secondary small" id="copy-scan-json">Copy JSON</button></div>
    </div>
    <div data-pane="table">${scanTable(d)}</div>
    <div data-pane="json" class="hidden"><div class="json-tree">${jsonTree(d)}</div></div>`;
  $$('.tab', box).forEach(tab => tab.addEventListener('click', () => {
    $$('.tab', box).forEach(x => x.classList.toggle('active', x === tab));
    $$('[data-pane]', box).forEach(p => p.classList.toggle('hidden', p.dataset.pane !== tab.dataset.tab));
  }));
  $('#copy-scan-json').addEventListener('click', () =>
    navigator.clipboard?.writeText(JSON.stringify(d, null, 2)).then(() => toast('JSON copied'), () => toast('Copy failed', true)));
}

function openScanModal(url) {
  scanToken++;
  openModal('Instagram profile JSON', `
    <form id="scan-form" class="modal-form scan-form" novalidate>
      <div class="field"><label for="scan-url">Reel or profile link</label>
        <input id="scan-url" value="${esc(url || '')}" placeholder="https://www.instagram.com/reel/… or https://www.instagram.com/username/"></div>
      <div class="field scan-scrolls"><label for="scan-reels">Reels to collect</label>
        <input id="scan-reels" type="number" min="1" max="300" value="30"></div>
      <button class="primary" type="submit">Scan profile</button>
    </form>
    <p class="subtle scan-help">Collects the account's latest reels - plays, likes and comments - as JSON.</p>
    <div id="scan-box"></div>`);
  $('#modal .modal-box').classList.add('modal-wide');
  $('#scan-form').addEventListener('submit', e => { e.preventDefault(); runScan(); });
}

async function runScan() {
  const token = ++scanToken;
  const box = $('#scan-box'), button = $('#scan-form button');
  const url = $('#scan-url').value.trim(), maxReels = Number($('#scan-reels').value) || 30;
  box.innerHTML = '';
  if (!url) { box.innerHTML = '<p class="form-error">Paste an Instagram reel or profile link.</p>'; return; }
  button.disabled = true;
  let started;
  try {
    started = await post('/api/profile-scans', { url, max_reels: maxReels });
  } catch (err) {
    box.innerHTML = `<p class="form-error">${esc(err.message)}</p>`; button.disabled = false; return;
  }
  box.innerHTML = `<div class="scan-progress"><div class="scan-shot"><img alt="Live view of the automated browser"><span>Starting browser…</span></div>
    <div><p class="scan-status"><span class="spinner"></span> Scanning…</p><ol class="viewer-log scan-log"></ol></div></div>`;
  let seen = 0;
  while (token === scanToken) {
    let job;
    try { job = await api(`/api/scrape-jobs/${enc(started.job_id)}`); } catch (err) { break; }
    if (token !== scanToken) return;
    if (job.screenshot) { $('.scan-shot img', box).src = job.screenshot; $('.scan-shot span', box)?.remove(); }
    const log = $('.scan-log', box);
    for (const line of job.log.slice(seen)) {
      const li = document.createElement('li');
      li.innerHTML = `<span>${esc(line.t.toFixed(1))}s</span> ${esc(line.msg)}`;
      log.appendChild(li);
    }
    seen = job.log.length; log.scrollTop = log.scrollHeight;
    if (job.status !== 'running') break;
    await new Promise(r => setTimeout(r, 1000));
  }
  if (token !== scanToken) return;
  try {
    const scan = await api(`/api/profile-scans/${enc(started.scan_id)}`);
    if (scan.status === 'done') renderScanResult(box, scan);
    else box.insertAdjacentHTML('afterbegin', `<p class="form-error">Scan ${esc(scan.status)}: ${esc(scan.error || 'unknown error')}</p>`);
  } catch (err) {
    box.insertAdjacentHTML('afterbegin', `<p class="form-error">${esc(err.message)}</p>`);
  } finally {
    if ($('#scan-form button')) $('#scan-form button').disabled = false;
  }
}

/* ------------------------------------------------------------------- modals -- */

function openModal(title, html) {
  $('#modal .modal-box').classList.remove('modal-wide');
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = html;
  $('#modal').classList.remove('hidden');
}
function closeModal() { $('#modal').classList.add('hidden'); $('#modal .modal-box').classList.remove('modal-wide'); scanToken++; }
function confirmModal(title, html, label, onConfirm) {
  openModal(title, `<p>${html}</p><div class="report-actions modal-actions"><button class="secondary" data-close-modal>Cancel</button><button class="primary danger-fill" id="modal-confirm">${esc(label)}</button></div>`);
  $('#modal-confirm').addEventListener('click', () => { closeModal(); onConfirm(); });
}
$('#modal').addEventListener('click', e => { if (e.target.id === 'modal' || e.target.closest('[data-close-modal]')) closeModal(); });

/* ------------------------------------------------- Playwright live viewer -- */

let jobTimer = null, jobLogCount = 0;
const openViewer = () => $('#viewer').classList.remove('hidden');
const closeViewer = () => $('#viewer').classList.add('hidden');
$('#viewer-close').addEventListener('click', closeViewer);
$('#viewer').addEventListener('click', e => { if (e.target.id === 'viewer') closeViewer(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') { closeViewer(); closeModal(); } });

function watchJob(jobId, onDone) {
  clearTimeout(jobTimer); jobLogCount = 0;
  $('#viewer-log').innerHTML = ''; $('#viewer-img').removeAttribute('src'); $('#viewer-wait').classList.remove('hidden');
  $('#viewer-pill').className = 'tag tag-amber'; $('#viewer-pill').textContent = 'RUNNING'; $('#viewer-current').textContent = '';
  openViewer();
  const poll = async () => {
    let job;
    try { job = await api(`/api/scrape-jobs/${enc(jobId)}`); } catch (e) { toast(e.message, true); return; }
    if (job.screenshot) { $('#viewer-img').src = job.screenshot; $('#viewer-wait').classList.add('hidden'); }
    $('#viewer-current').textContent = job.current || '';
    $('#viewer-elapsed').textContent = job.elapsed + 's';
    const log = $('#viewer-log');
    for (const line of job.log.slice(jobLogCount)) {
      const li = document.createElement('li');
      li.innerHTML = `<span>${esc(line.t.toFixed(1))}s</span> ${esc(line.msg)}`;
      if (/^(Failed|Browser error)/.test(line.msg)) li.className = 'bad';
      if (/^Saved/.test(line.msg)) li.className = 'good';
      log.appendChild(li);
    }
    jobLogCount = job.log.length; log.scrollTop = log.scrollHeight;
    if (job.status === 'running') { jobTimer = setTimeout(poll, 700); return; }
    const ok = job.results.filter(r => r.ok).length, bad = job.results.length - ok;
    $('#viewer-pill').className = 'tag ' + (bad && !ok ? 'tag-gray' : 'tag-green');
    $('#viewer-pill').textContent = bad && !ok ? 'FAILED' : 'DONE';
    toast(`Public fetch finished: ${ok} updated${bad ? ', ' + bad + ' failed' : ''}`, bad > 0 && !ok);
    onDone?.();
    if (ok && !bad) jobTimer = setTimeout(closeViewer, 1500); // keep it open on failures so the log can be read
  };
  poll();
}

/* -------------------------------------------------------------------- start -- */

(async function start() {
  window.__appStarted = true;
  try {
    state.me = await api('/api/me');
    setUser(state.me.user, state.me.csrf);
  } catch (e) { toast(e.message, true); }
  render();
})();
