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
  else if (!im.has_owner_account) line = 'Create your Photos account to begin';
  else if (s.library) line = n(s.library.photos) + ' photos · ' + n(s.library.videos) + ' videos · ' + (s.library.size_gb || 0) + ' GB';
  else line = 'Immich ' + (im.version || '');
  const busy = s.background_work && Object.values(s.background_work).reduce((a, q) => a + (q.waiting || 0) + (q.active || 0), 0);
  if (busy) line += ' · organising ' + n(busy);
  if (s.import && s.import.state === 'running') line += ' · importing';
  el('line').textContent = line;

  el('setup').hidden = true;
  if (!own) {
    setupCard('Open Photos from your Wizard', 'This page needs your Wizard\u2019s private address to show your library.');
  } else if (!im.reachable) {
    setupCard('Photos is starting', 'The first start takes a minute or two. This page refreshes on its own.');
  } else if (!im.has_owner_account) {
    setupCard('Create your Photos account', 'Below, tap Getting started and enter your email, your name and a password you choose. This is your own account on your own box; Google is not involved.');
  }
  el('open-full').hidden = !own;
  if (own) el('open-full').href = own;
  const frame = el('immich');
  if (own && im.reachable) {
    el('frame-wrap').hidden = false;
    if (!frame.getAttribute('src')) frame.src = own;
  }
  const connected = !!s.agent_connected;
  el('agent-line').textContent = connected
    ? 'Your agent is connected' + (s.account && s.account.email ? ' (as ' + s.account.email + ').' : '.')
    : (im.has_owner_account ? 'Sign in to Photos below first, then tap Let my agent help.' : 'Create your Photos account first.');
  el('agent-go').hidden = connected;
  el('agent-off').hidden = !connected;
  el('agent-go').disabled = !im.has_owner_account;
}

function renderImport(d) {
  const st = d.state;
  const words = { idle: 'No import yet.', running: 'Importing… you can close this; it keeps going.', done: 'Last import finished.',
    failed: 'The last import stopped: ' + (d.message || 'see the log below.'), interrupted: 'The last import was interrupted. Import again to continue; nothing is duplicated.' };
  el('import-state').textContent = (words[st] || st) + ' ' + (d.waiting_files ? d.waiting_files + ' file(s), ' + d.waiting_gb + ' GB in Files › Photos › Import.' : 'No Takeout files in Files › Photos › Import yet.');
  el('import-go').disabled = st === 'running' || !d.waiting_files || !(state && state.agent_connected);
  if (!(state && state.agent_connected) && d.waiting_files) el('import-state').textContent += ' Connect the agent first (Agent button): the import uses the same key.';
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
el('import-go').onclick = async () => {
  el('import-go').disabled = true;
  try { const r = await request('api/import/start', {}); say(r.started ? 'Import started.' : (r.message || 'Not started.'), 'note'); }
  catch (e) { say(e.message, 'error'); }
  try { renderImport(await request('api/import')); } catch (e) {}
};
el('agent-go').onclick = async () => {
  el('agent-go').disabled = true;
  try { await request('api/connect', {}); say('Your agent is connected to Photos.', 'note'); }
  catch (e) { say(e.message, 'error'); }
  await refresh();
};
el('agent-off').onclick = async () => {
  try { const r = await request('api/disconnect', {}); say(r.key_revoked_in_immich ? 'Agent access removed.' : 'Agent access removed here. Also delete the key in Immich › Account settings › API keys.', 'note'); }
  catch (e) { say(e.message, 'error'); }
  await refresh();
};

if (!window.WizardApp || WizardApp.version !== 1) {
  say('Wizard system functions are unavailable. Open Photos through Wizard.', 'error');
} else {
  WizardApp.ready({ name: 'Photos', version: '0.1.0' });
  WizardApp.back.register({ canGoBack: () => false, back: () => false });
}

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
