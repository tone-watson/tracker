const $ = selector => document.querySelector(selector);
const state = { lists: [], repositories: [], items: [], list: '', repository: '', status: 'active', cursor: null, selected: null, generation: 0 };
const attempts = new WeakMap();
let syncTimer, searchTimer, editorGeneration = 0;
const labels = { open: 'To do', in_progress: 'In progress', blocked: 'Blocked', completed: 'Completed', cancelled: 'Cancelled' };
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function message(text = '', error = false) {
  $('#message').textContent = text;
  $('#message').hidden = !text;
  $('#message').classList.toggle('error', error);
}
function invalidateEditor() {
  editorGeneration++; state.selected = null;
}
function closeEditor() {
  invalidateEditor(); $('#editor').close();
}
function signedOut() {
  invalidateEditor();
  clearTimeout(syncTimer);
  document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
  $('#workspace').hidden = true; $('#login').hidden = false; $('#loading').hidden = true;
}
async function request(path, method = 'GET', value) {
  const response = await fetch(path, { method, credentials: 'same-origin', cache: 'no-store',
    headers: value === undefined ? {} : { 'Content-Type': 'application/json' },
    body: value === undefined ? undefined : JSON.stringify(value) });
  let data;
  try { data = await response.json(); } catch { throw new Error('The server returned an unreadable response. Refresh before trying again.'); }
  if (!response.ok) {
    if (response.status === 401 && path !== '/auth/login') signedOut();
    const error = new Error(data.error?.message || 'The request could not be completed.');
    error.code = data.error?.code;
    if (data.error?.canSignOut || (path === '/auth/session' && response.status === 403)) $('#reset-login').hidden = false;
    if (error.code === 'revision_conflict') error.message = 'This task changed elsewhere. Close and reopen it to review the latest version before saving.';
    if (error.code === 'cursor_conflict') error.message = 'The list changed while loading. Refresh to load its current tasks.';
    throw error;
  }
  return data;
}
async function api(path, method = 'GET', value) {
  return (await request('/browser/api' + path, method, value)).data;
}
async function pages(path) {
  const values = []; let cursor;
  do {
    const separator = path.includes('?') ? '&' : '?';
    const data = await api(path + separator + new URLSearchParams({ limit: '50', ...(cursor ? { cursor } : {}) }));
    values.push(...data.items); cursor = data.next_cursor;
  } while (cursor);
  return values;
}
async function signedIn(user) {
  $('#account').textContent = user?.name || user?.email || 'Farm account';
  $('#login').hidden = true; $('#workspace').hidden = false; $('#loading').hidden = true;
  await refresh();
}
function navigation() {
  const nav = $('#navigation'); nav.replaceChildren();
  const button = (name, list, repository = '', extra = '') => {
    const node = element('button', extra + (state.list === list && state.repository === repository ? ' selected' : ''));
    const farm = state.lists.find(item => item.id === list)?.type === 'farm';
    node.append(element('span', 'nav-dot' + (farm ? ' farm' : '')), element('span', 'nav-label', name));
    node.title = repository ? state.repositories.find(repo => repo.id === repository)?.path || name : name;
    node.addEventListener('click', () => { state.list = list; state.repository = repository; navigation(); loadItems().catch(error => message(error.message, true)); });
    nav.append(node);
  };
  button('All tasks', '');
  for (const list of state.lists.filter(item => item.status === 'active')) {
    button(list.name, list.id);
    if (list.type === 'farm' && state.list === list.id) for (const repo of state.repositories) button(repo.name, list.id, repo.id, 'repo-nav');
  }
  const list = state.lists.find(item => item.id === state.list);
  const repo = state.repositories.find(item => item.id === state.repository);
  $('#page-title').textContent = repo?.name || list?.name || 'Your tasks';
  $('#breadcrumb').textContent = repo ? 'FARM / ' + repo.path : list ? 'YOUR LISTS' : 'ALL LISTS';
  $('#page-description').textContent = repo ? repo.path + '/TODO.md' : list?.type === 'farm' ? 'One place for every repository’s next step.' : list?.description || 'Make room for what matters.';
  syncStatus();
}
function syncStatus() {
  const node = $('#sync-status'); node.replaceChildren();
  const isFarm = state.lists.find(item => item.id === state.list)?.type === 'farm';
  node.hidden = !isFarm;
  const repos = state.repository ? state.repositories.filter(repo => repo.id === state.repository) : state.repositories;
  for (const repo of repos) {
    const status = repo.pending ? 'Changes waiting to sync' : repo.state === 'attention' ? repo.error : repo.last_synced_at ? 'Synced ' + new Date(repo.last_synced_at).toLocaleString() : 'Waiting for first sync';
    node.append(element('div', repo.state === 'attention' ? 'attention' : '', repo.name + ' · ' + status));
  }
  if (isFarm && !repos.length) node.append(element('div', '', 'No repositories imported yet. Run repository setup, then sync.'));
  clearTimeout(syncTimer);
  if (state.repositories.some(repo => repo.pending)) syncTimer = setTimeout(async () => {
    try { state.repositories = (await api('/v1/repositories')).items; syncStatus(); if (!state.repositories.some(repo => repo.pending)) await loadItems(); }
    catch (error) { message(error.message, true); }
  }, 4000);
}
async function refresh() {
  const [lists, repositories] = await Promise.all([pages('/v1/lists?status=all'), api('/v1/repositories')]);
  state.lists = lists; state.repositories = repositories.items;
  navigation(); await loadItems();
}
async function loadItems(append = false) {
  const generation = ++state.generation;
  const query = new URLSearchParams({ status: state.status, limit: '50' });
  if (state.list) query.set('list_id', state.list);
  if (state.repository) query.set('repository_id', state.repository);
  if ($('#search').value.trim()) query.set('q', $('#search').value.trim());
  if ($('#overdue').checked) query.set('overdue_only', 'true');
  if (append && state.cursor) query.set('cursor', state.cursor);
  $('#load-more').disabled = true;
  try {
    const data = await api('/v1/items?' + query);
    if (generation !== state.generation) return;
    state.items = append ? [...state.items, ...data.items] : data.items;
    state.cursor = data.next_cursor; renderItems();
  } finally { if (generation === state.generation) $('#load-more').disabled = false; }
}
function renderItems() {
  const target = $('#tasks'); target.replaceChildren();
  $('#load-more').hidden = !state.cursor;
  $('#loaded-count').textContent = `${state.items.length} ${state.cursor ? 'loaded ' : ''}task${state.items.length === 1 ? '' : 's'}`;
  if (!state.items.length) {
    const empty = element('div', 'empty');
    empty.append(element('strong', '', 'A little breathing room.'), element('span', '', 'No tasks match this view. Add a task or change the filters.'));
    target.append(empty); return;
  }
  const groups = new Map();
  for (const item of state.items) {
    const key = item.repository_id || item.list_id;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  for (const [key, items] of groups) {
    const repo = state.repositories.find(value => value.id === key), list = state.lists.find(value => value.id === items[0].list_id);
    const group = element('div', 'task-group');
    const heading = element('div', 'group-header', repo?.name || list?.name || 'Tasks');
    heading.append(element('span', '', repo?.path || `${items.length} in this view`)); group.append(heading);
    for (const item of items) {
      const row = element('div', 'task-row ' + item.status);
      const check = document.createElement('input'); check.type = 'checkbox'; check.checked = item.status === 'completed';
      check.setAttribute('aria-label', `${check.checked ? 'Reopen' : 'Complete'} ${item.title}`);
      check.addEventListener('change', async () => {
        check.disabled = true;
        try {
          await api('/v1/items/' + item.id, 'PUT', { expected_revision: item.revision, status: check.checked ? 'completed' : 'open' });
          await refresh();
        } catch (error) { check.checked = item.status === 'completed'; message(error.message, true); }
        finally { check.disabled = false; }
      });
      const main = element('button', 'task-main'); main.append(element('span', 'task-title', item.title));
      const meta = element('span', 'task-meta');
      if (!['open', 'completed'].includes(item.status)) meta.append(element('span', 'task-status ' + item.status, labels[item.status]));
      if (item.source?.headings?.length) meta.append(element('span', '', item.source.headings.join(' / ')));
      if (item.description) meta.append(element('span', '', item.description.slice(0, 90) + (item.description.length > 90 ? '…' : '')));
      if (item.recurrence_id) meta.append(element('span', '', '↻ Repeating'));
      if (meta.childNodes.length) main.append(meta);
      main.addEventListener('click', () => openTask(item).catch(error => message(error.message, true)));
      row.append(check, main);
      if (item.due) {
        const late = ['open', 'blocked', 'in_progress'].includes(item.status) && new Date(item.due.due_at) <= new Date();
        row.append(element('span', 'due' + (late ? ' late' : ''), (late ? 'Overdue · ' : '') + item.due.value));
      }
      group.append(row);
    }
    target.append(group);
  }
}
function option(select, value, label) { const node = element('option', '', label); node.value = value; select.append(node); }
function repositoryField() {
  const form = $('#task-form'); const farm = state.lists.find(list => list.id === form.elements.list_id.value)?.type === 'farm';
  $('#repository-field').hidden = !farm; form.elements.repository_id.required = farm;
}
async function openTask(item = null) {
  const generation = ++editorGeneration;
  try { item = item ? await api('/v1/items/' + item.id) : null; }
  catch (error) { if (generation !== editorGeneration) return; throw error; }
  if (generation !== editorGeneration || $('#workspace').hidden) return;
  const form = $('#task-form'); form.reset(); attempts.delete(form); state.selected = item;
  const listField = form.elements.list_id, repoField = form.elements.repository_id;
  listField.replaceChildren(); repoField.replaceChildren(); option(repoField, '', 'Choose a repository');
  for (const list of state.lists.filter(list => list.status === 'active')) option(listField, list.id, list.name);
  for (const repo of state.repositories) option(repoField, repo.id, repo.path);
  listField.value = item?.list_id || state.list || state.lists.find(list => list.status === 'active' && list.type !== 'farm')?.id || state.lists[0]?.id || '';
  repoField.value = item?.repository_id || state.repository || '';
  listField.disabled = !!item?.repository_id; repoField.disabled = !!item;
  for (const name of ['title', 'description', 'status']) if (item?.[name]) form.elements[name].value = item[name];
  form.elements.due.value = item?.due?.value || ''; form.elements.timezone.value = item?.due?.timezone || 'America/Chicago';
  for (const opt of form.elements.status.options) opt.disabled = !item && ['completed', 'cancelled'].includes(opt.value);
  $('#editor-label').textContent = item ? 'TASK DETAILS' : 'NEW TASK';
  $('#editor-error').textContent = ''; $('#detail-error').textContent = '';
  $('#task-context').textContent = item ? [item.source?.path, item.repository_id ? 'Title and completion sync with TODO.md. Cancelling keeps its text as an ordinary Markdown bullet.' : '', item.blocked_by?.length ? `Blocked by ${item.blocked_by.length} other task(s). Complete or clear those dependencies through your agent before marking this task done.` : '', item.recurrence_id ? 'Repeating task. Cancelling stops its series. Your agent can edit or skip occurrences.' : '', 'Created ' + new Date(item.created_at).toLocaleString(), item.completed_at ? 'Completed ' + new Date(item.completed_at).toLocaleString() : ''].filter(Boolean).join('\n') : '';
  $('#details-panel').hidden = !item; $('#details').replaceChildren(); $('#history').replaceChildren();
  repositoryField(); $('#editor').showModal(); form.elements.title.focus();
  if (item) await loadDetails(item.id, generation);
}
async function loadDetails(id, generation = editorGeneration) {
  let details, history;
  try { [details, history] = await Promise.all([pages('/v1/items/' + id + '/details'), pages('/v1/items/' + id + '/history')]); }
  catch (error) { if (generation !== editorGeneration) return; throw error; }
  if (generation !== editorGeneration || state.selected?.id !== id || !$('#editor').open) return;
  $('#details').replaceChildren();
  for (const detail of details.filter(value => !value.cancelled)) {
    const node = element('div', 'detail-entry'); node.append(element('h3', '', detail.label), element('pre', '', detail.kind === 'json' ? JSON.stringify(detail.value, null, 2) : detail.value)); $('#details').append(node);
  }
  if (!details.length) $('#details').append(element('p', 'muted', 'No extra details yet.'));
  $('#history').replaceChildren(...history.slice().reverse().map(event => element('p', '', new Date(event.at).toLocaleString() + ' · ' + event.action.replaceAll('_', ' '))));
}
function createPayload(form, payload) {
  const fingerprint = JSON.stringify(payload), prior = attempts.get(form);
  const key = prior?.fingerprint === fingerprint ? prior.key : crypto.randomUUID();
  attempts.set(form, { fingerprint, key }); return { ...payload, idempotency_key: key };
}
async function submit(form, errorNode, work) {
  const button = form.querySelector('[type=submit]'); button.disabled = true; errorNode.textContent = '';
  try { await work(); } catch (error) { errorNode.textContent = error.message; }
  finally { button.disabled = false; }
}
$('#login-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  submit(form, $('#login-error'), async () => {
    const response = await request('/auth/login', 'POST', { email: form.elements.email.value, password: form.elements.password.value });
    form.elements.password.value = ''; await signedIn(response.user);
  });
});
$('#reset-login').addEventListener('click', async () => {
  try { await request('/auth/logout', 'POST', {}); $('#reset-login').hidden = true; $('#login-error').textContent = ''; }
  catch (error) { $('#login-error').textContent = error.message; }
});
$('#logout').addEventListener('click', async () => { try { await request('/auth/logout', 'POST', {}); signedOut(); } catch (error) { message(error.message, true); } });
$('#refresh').addEventListener('click', () => refresh().then(() => message()).catch(error => message(error.message, true)));
$('#sync').addEventListener('click', async () => {
  $('#sync').disabled = true;
  try { await api('/v1/sync', 'POST', {}); message('Repository sync requested. It will run in the background; refresh in a moment for the result.'); }
  catch (error) { message(error.message, true); }
  finally { $('#sync').disabled = false; }
});
$('#new-task').addEventListener('click', () => {
  if (!state.lists.some(list => list.status === 'active')) { message('Create a list first.'); return; }
  openTask().catch(error => message(error.message, true));
});
$('#new-list').addEventListener('click', () => { $('#list-form').reset(); attempts.delete($('#list-form')); $('#list-error').textContent = ''; $('#list-editor').showModal(); });
$('#list-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  submit(form, $('#list-error'), async () => {
    const list = await api('/v1/lists', 'POST', createPayload(form, { name: form.elements.name.value, description: form.elements.description.value }));
    state.list = list.id; state.repository = ''; $('#list-editor').close(); await refresh();
  });
});
$('#task-form').elements.list_id.addEventListener('change', repositoryField);
$('#task-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget, fields = form.elements;
  submit(form, $('#editor-error'), async () => {
    const due = fields.due.value.trim() ? { value: fields.due.value.trim(), timezone: fields.timezone.value.trim() } : null;
    let payload = { title: fields.title.value, description: fields.description.value, status: fields.status.value, due, list_id: fields.list_id.value };
    if (state.selected) {
      payload.expected_revision = state.selected.revision;
      await api('/v1/items/' + state.selected.id, 'PUT', payload);
    } else {
      if (fields.repository_id.value && !$('#repository-field').hidden) payload.repository_id = fields.repository_id.value;
      await api('/v1/items', 'POST', createPayload(form, payload));
    }
    closeEditor(); await refresh();
  });
});
$('#detail-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  submit(form, $('#detail-error'), async () => {
    let value = form.elements.value.value;
    if (form.elements.kind.value === 'json') { try { value = JSON.parse(value); } catch { throw new Error('Enter valid JSON for structured data.'); } }
    await api('/v1/items/' + state.selected.id + '/details', 'POST', createPayload(form, { label: form.elements.label.value, kind: form.elements.kind.value, value }));
    form.reset(); attempts.delete(form); await loadDetails(state.selected.id);
  });
});
for (const button of document.querySelectorAll('[data-close]')) button.addEventListener('click', () => {
  const dialog = button.closest('dialog');
  if (dialog.id === 'editor') closeEditor(); else dialog.close();
});
$('#editor').addEventListener('cancel', invalidateEditor);
$('#editor').addEventListener('close', () => { if (!$('#editor').open) invalidateEditor(); });
for (const button of document.querySelectorAll('[data-status]')) button.addEventListener('click', () => {
  state.status = button.dataset.status;
  document.querySelectorAll('[data-status]').forEach(node => node.setAttribute('aria-pressed', String(node === button)));
  loadItems().catch(error => message(error.message, true));
});
$('#search').maxLength = 200;
$('#search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadItems().catch(error => message(error.message, true)), 250); });
$('#overdue').addEventListener('change', () => loadItems().catch(error => message(error.message, true)));
$('#load-more').addEventListener('click', () => loadItems(true).catch(error => message(error.message, true)));
try { const response = await request('/auth/session'); await signedIn(response.user); }
catch (error) { if ($('#workspace').hidden) signedOut(); else message(error.message, true); }
