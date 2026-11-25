'use strict';
const el = id => document.getElementById(id);
let state = null;

function say(text, kind = '') { // kind: '' (screen readers only), 'error', 'note'
  const s = el('status');
  s.classList.toggle('wz-sr', !kind);
  s.classList.toggle('note', kind === 'note');
  s.textContent = text;
}

async function request(path, data) {
  const init = data === undefined ? { credentials: 'same-origin' }
    : { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) };
  const response = await fetch(path, init);
  let body = {};
  try { body = await response.json(); } catch (e) {}
  if (!response.ok) { const e = new Error(body.error || 'Photos could not do that. Try again.'); e.code = body.code; throw e; }
  return body;
}

function n(x) { return typeof x === 'number' ? x.toLocaleString() : '0'; }

function setupCard(title, text) {
  const card = el('setup');
  card.replaceChildren();
  const h = document.createElement('h2'); h.textContent = title;
  const p = document.createElement('p'); p.className = 'ph-help'; p.textContent = text;
  card.append(h, p);
  card.hidden = false;
}

function render(s) {
  state = s;
  const own = s.links && s.links.immich;
  const im = s.immich || {};
  let line = '';
  if (!im.reachable) line = 'Starting Immich…';
  else if (!im.has_owner_account || !s.agent_connected) line = 'Setting up…';
  else if (s.library) line = n(s.library.photos) + ' photos · ' + n(s.library.videos) + ' videos · ' + (s.library.size_gb || 0) + ' GB';
  else line = 'Immich ' + (im.version || '');
  const busy = s.background_work && Object.values(s.background_work).reduce((a, q) => a + (q.waiting || 0) + (q.active || 0), 0);
  if (busy) line += ' · organising ' + n(busy);
  if (s.import && s.import.state === 'running') line += ' · importing';
  el('line').textContent = line;
  el('menu-line').textContent = line;

  el('setup').hidden = true;
  if (!own) {
    setupCard('Open Photos from your Wizard', 'This page needs your Wizard\u2019s private address to show your library.');
  } else if (!im.reachable) {
    setupCard('Photos is starting', 'The first start takes a minute or two. This page refreshes on its own.');
  } else if (!(s.sign_in && s.sign_in.ready)) {
    setupCard('Photos is setting up', (s.sign_in && s.sign_in.problem) || 'Photos makes its own library for you; there is nothing to sign in to. This page refreshes on its own.');
  }
  el('open-prints').hidden = !s.print_review;
  el('open-full').hidden = !own;
  if (own) el('open-full').href = own;
  const frame = el('immich');
  if (own && im.reachable && s.sign_in && s.sign_in.ready) {
    el('frame-wrap').hidden = false;
    if (!frame.getAttribute('src')) frame.src = own;
  }
  const phone = s.links && s.links.mobile_server_url;
  el('phone-line').textContent = phone ? 'Phone backup: install the Immich app, type ' + phone + ' as the server, tap Next, then Continue. Keep Tailscale on.' : '';
  if (phone) backup.server = phone;
  el('agent-line').textContent = s.agent_connected ? 'Your agent is connected to Photos.' : 'Your agent connects by itself once Photos has finished setting up.';
}

function renderImport(d) {
  const st = d.state;
  const words = { idle: 'No import yet.', running: 'Importing… you can close this; it keeps going.', done: 'Last import finished.',
    failed: 'The last import stopped: ' + (d.message || 'see the log below.'), interrupted: 'The last import was interrupted. Import again to continue; nothing is duplicated.' };
  el('import-state').textContent = (words[st] || st) + ' ' + (d.waiting_files ? d.waiting_files + ' file(s), ' + d.waiting_gb + ' GB in Files › Photos › Import.' : 'No Takeout files in Files › Photos › Import yet.');
  el('import-go').disabled = st === 'running' || !d.waiting_files || !(state && state.agent_connected);
  if (!(state && state.agent_connected) && d.waiting_files) el('import-state').textContent += ' Photos is still setting up; Import now works in a minute.';
  const list = el('import-options'); list.replaceChildren();
  const labels = { include_archived: 'Include archived photos', include_partner: 'Include partner-shared photos', sync_albums: 'Recreate my Google albums', include_trashed: 'Include photos in Google\u2019s trash' };
  for (const [k, label] of Object.entries(labels)) {
    const li = document.createElement('li');
    const row = document.createElement('button'); row.type = 'button'; row.className = 'wz-row';
    const on = !!(d.options || {})[k];
    row.setAttribute('aria-pressed', String(on));
    const box = document.createElement('span'); box.className = 'wz-check'; box.setAttribute('aria-hidden', 'true');
    box.innerHTML = '<i><svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg></i>';
    const main = document.createElement('span'); main.className = 'wz-main';
    const b = document.createElement('b'); b.textContent = label; main.append(b);
    row.append(box, main);
    row.onclick = async () => {
      row.disabled = true;
      try { await request('api/import/options', { [k]: !on }); renderImport(await request('api/import')); }
      catch (e) { say(e.message, 'error'); row.disabled = false; }
    };
    li.append(row); list.append(li);
  }
  const log = el('import-log');
  log.hidden = !(d.log_tail && d.log_tail.length);
  log.textContent = (d.log_tail || []).join('\n');
}

async function refresh() {
  try { render(await request('api/status')); }
  catch (e) { say(e.message, 'error'); }
}

el('open-import').onclick = async () => {
  WzSheet.open(el('import-sheet'));
  try { renderImport(await request('api/import')); } catch (e) { say(e.message, 'error'); }
};
el('open-agent').onclick = () => WzSheet.open(el('agent-sheet'));

// The ⋯ menu (narrow widths, where the header is hidden): the same actions as the header chips, in one sheet.
function syncMenu() {
  for (const row of el('menu-sheet').querySelectorAll('[data-menu]')) {
    const chip = el(row.dataset.menu);
    row.parentElement.hidden = chip.hidden;
    row.querySelector('b').textContent = chip.textContent;
    if (chip.href) row.href = chip.href;
  }
}
el('open-menu').onclick = () => { syncMenu(); WzSheet.open(el('menu-sheet')); };
el('menu-sheet').addEventListener('click', e => {
  const row = e.target.closest('[data-menu]'); if (!row) return;
  WzSheet.close(el('menu-sheet'));
  if (row.dataset.menu !== 'open-full') el(row.dataset.menu).click(); // the link opens by itself
});
el('import-go').onclick = async () => {
  el('import-go').disabled = true;
  try { const r = await request('api/import/start', {}); say(r.started ? 'Import started.' : (r.message || 'Not started.'), 'note'); }
  catch (e) { say(e.message, 'error'); }
  try { renderImport(await request('api/import')); } catch (e) {}
};
// ---- Monthly print review (app switch print-review, humanitylabs-org/photos#2) ----
// Photos suggests a few photos of last month; the owner takes photos out or puts others in, writes a note and saves
// the approved set on the box. Nothing here orders, mails or pays for prints.
const prints = { month: '', doc: null, picked: new Set(), busy: false };
function monthShift(month, by) {
  const [y, m] = month.split('-').map(Number);
  const d = new Date(Date.UTC(y, m - 1 + by, 1));
  return d.getUTCFullYear() + '-' + String(d.getUTCMonth() + 1).padStart(2, '0');
}
function renderPrints() {
  const d = prints.doc; if (!d) return;
  el('print-month').textContent = d.label;
  el('print-next').disabled = prints.busy || d.month >= monthShift(d.previous_month, 1);
  el('print-prev').disabled = prints.busy;
  el('print-line').textContent = d.next || '';
  el('print-never').textContent = d.never_orders || '';
  const grid = el('print-grid'); grid.replaceChildren();
  for (const c of d.candidates || []) {
    const li = document.createElement('li');
    const b = document.createElement('button'); b.type = 'button'; b.className = 'ph-print-pick';
    const on = prints.picked.has(c.id);
    b.setAttribute('aria-pressed', String(on));
    const when = c.taken ? new Date(c.taken.slice(0, 10) + 'T12:00:00Z').toLocaleDateString(undefined, { day: 'numeric', month: 'short', timeZone: 'UTC' }) : '';
    b.setAttribute('aria-label', (on ? 'Picked: ' : 'Not picked: ') + [when, (c.people || []).join(', ')].filter(Boolean).join(', '));
    const img = document.createElement('img'); img.alt = ''; img.loading = 'lazy'; img.decoding = 'async';
    img.src = 'api/print-review/thumb?id=' + encodeURIComponent(c.id);
    const mark = document.createElement('span'); mark.className = 'ph-print-mark'; mark.setAttribute('aria-hidden', 'true');
    mark.innerHTML = '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
    const cap = document.createElement('span'); cap.className = 'ph-print-cap'; cap.textContent = when;
    b.append(img, mark, cap);
    b.onclick = () => {
      if (prints.picked.has(c.id)) prints.picked.delete(c.id);
      else if (prints.picked.size < (d.max || 30)) prints.picked.add(c.id);
      renderPrints();
    };
    li.append(b); grid.append(li);
  }
  const any = (d.candidates || []).length > 0;
  el('print-form').hidden = !any;
  el('print-count').textContent = prints.picked.size + ' of ' + d.candidates.length + ' picked' + (d.saved ? ' · saved set' : ' · not saved yet');
  el('print-save').disabled = prints.busy || !prints.picked.size;
  el('print-save').textContent = d.saved ? 'Save changes' : 'Save approved set';
  const saved = el('print-saved'); saved.replaceChildren();
  for (const s of d.saved_months || []) {
    const li = document.createElement('li');
    const row = document.createElement('button'); row.type = 'button'; row.className = 'wz-row';
    const main = document.createElement('span'); main.className = 'wz-main';
    const b = document.createElement('b'); b.textContent = s.label + ' · ' + s.count + ' photo' + (s.count === 1 ? '' : 's');
    main.append(b);
    if (s.note) { const sm = document.createElement('small'); sm.textContent = s.note; main.append(sm); }
    row.append(main);
    row.onclick = () => loadPrints(s.month);
    li.append(row); saved.append(li);
  }
}
async function loadPrints(month) {
  prints.busy = true; if (prints.doc) renderPrints();
  try {
    const d = await request('api/print-review' + (month ? '?month=' + encodeURIComponent(month) : ''));
    prints.doc = d; prints.month = d.month;
    prints.picked = new Set((d.candidates || []).filter(c => c.selected).map(c => c.id));
    el('print-note').value = (d.saved && d.saved.note) || '';
  } catch (e) { say(e.message, 'error'); el('print-line').textContent = e.message; }
  prints.busy = false; renderPrints();
}
el('open-prints').onclick = () => { WzSheet.open(el('print-sheet')); loadPrints(prints.month); };
el('print-prev').onclick = () => prints.doc && loadPrints(monthShift(prints.doc.month, -1));
el('print-next').onclick = () => prints.doc && loadPrints(monthShift(prints.doc.month, 1));
el('print-save').onclick = async () => {
  const d = prints.doc; if (!d || !prints.picked.size) return;
  prints.busy = true; renderPrints();
  const order = (d.candidates || []).map(c => c.id).filter(id => prints.picked.has(id));
  try {
    const r = await request('api/print-review', { month: d.month, assets: order, note: el('print-note').value });
    prints.doc = r; prints.picked = new Set((r.candidates || []).filter(c => c.selected).map(c => c.id));
    say('Saved ' + order.length + ' photo' + (order.length === 1 ? '' : 's') + ' for ' + r.label + '.', 'note');
  } catch (e) { say(e.message, 'error'); el('print-line').textContent = e.message; }
  prints.busy = false; renderPrints();
};

if (!window.WizardApp || WizardApp.version !== 1) {
  say('Wizard system functions are unavailable. Open Photos through Wizard.', 'error');
} else {
  WizardApp.ready({ name: 'Photos', version: '0.5.0' });
  WizardApp.back.register({ canGoBack: () => false, back: () => false });
}

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 15000);

// ---- Phone backup (from the Wizard OS Android app) ----
// The backup worker lives in Wizard OS for Android; this page drives it through the shell's Photos relay
// (photos.js, unchanged from the former view-only Photos app). The photo server is this app's own Immich, so the
// owner never types an address. In a browser or the desktop shell there is no relay and the button stays hidden.
const backup = { mode: '', status: null, error: '', busy: false, server: '', albumsOpen: false, albums: null, albumsError: '', confirmDisconnect: false };
const P = window.PhotosPage;
const framed = window.parent && window.parent !== window;
const bapi = P.client(msg => {
  if (!framed) throw Object.assign(new Error('no-bridge'), { code: 'no-bridge' });
  window.parent.postMessage(msg, location.origin);
}, {
  onStatus: s => { backup.status = s; if (backup.mode !== 'v1') { backup.mode = 'v1'; el('open-backup').hidden = false; } renderBackup(); },
  onResume: () => { if (backup.mode === 'v1') bapi.status().catch(() => {}); },
});
window.addEventListener('message', ev => { if (ev.origin === location.origin && ev.source === window.parent) bapi.onMessage(ev.data); });

function renderBackup() {
  const root = el('backup-root');
  if (document.activeElement && document.activeElement.matches && document.activeElement.matches('[data-ph-server]')) {
    root.querySelectorAll('[data-ph-status]').forEach(x => { x.textContent = backup.error || P.summary(backup.status); }); return;
  }
  root.innerHTML = backup.mode === 'v1' ? P.html(backup) : '<p class="ph-status">Checking…</p>';
  if (backup.status) el('open-backup').textContent = backup.status.enabled ? 'Phone backup · on' : 'Phone backup';
}
function backupTap(what, extra = {}) {
  backup.error = '';
  const pending = P.act(backup, bapi, what, extra);
  if (!pending) { renderBackup(); return; }
  if (what !== 'albums' && what !== 'album') backup.busy = what === 'run' ? 'run' : true;
  renderBackup();
  pending.catch(e => { backup.error = e.timeout ? 'The Wizard OS app did not answer · try again' : e.message; })
    .finally(() => { backup.busy = false; renderBackup(); });
}
el('backup-root').addEventListener('click', e => {
  const b = e.target.closest('[data-ph], [data-ph-album]'); if (!b || b.disabled) return;
  if (b.dataset.phAlbum != null) { backupTap('album', { id: b.dataset.phAlbum }); return; }
  const field = el('backup-root').querySelector('[data-ph-server]');
  backupTap(b.dataset.ph, field && field.value ? { server: field.value } : {});
});
el('backup-root').addEventListener('input', e => { if (e.target.matches('[data-ph-server]')) backup.server = e.target.value; });
el('open-backup').onclick = () => { WzSheet.open(el('backup-sheet')); renderBackup(); bapi.status().catch(() => {}); };
el('backup-sheet').addEventListener('close', () => { backup.albumsOpen = false; backup.confirmDisconnect = false; });
P.detect(bapi).then(mode => {
  if (mode !== 'v1' && mode !== 'v1-error') return; // browser, desktop or an old phone app: Immich phone app instructions stay in Agent
  backup.mode = 'v1';
  if (mode === 'v1-error') backup.error = 'Could not read backup status · try again';
  el('open-backup').hidden = false;
  renderBackup();
});
