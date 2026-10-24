import { chromium } from 'playwright';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';

const root = fileURLToPath(new URL('../public', import.meta.url));
const origin = 'https://tracker.noiseflood.farm';
const stamp = '2026-10-24T12:00:00.000000Z';
const id = n => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const listPersonal = id(1), listFarm = id(2), repoTracker = id(3), repoNoise = id(4);
const state = { auth: true, denySession: false, denyLogin: false, conflict: false, requests: [], updates: [], creates: [], detailCreates: [], logoutCount: 0 };
const deferredGets = new Map();
function holdGet(path) {
  let started, release;
  const requested = new Promise(resolve => { started = resolve; });
  const released = new Promise(resolve => { release = resolve; });
  const gate = { path, requested, released, started, release };
  deferredGets.set(path, gate); return gate;
}
async function deferGet(path) {
  const gate = deferredGets.get(path);
  if (gate) { deferredGets.delete(path); gate.started(); await gate.released; }
}
const lists = [
  { id: listPersonal, name: 'Personal', description: 'The things outside the code.', status: 'active', revision: 1 },
  { id: listFarm, name: 'Farm', type: 'farm', description: 'Repository tasks', status: 'active', revision: 1 },
];
const repos = [
  { id: repoTracker, name: 'tracker', path: 'private/tracker', state: 'synced', pending: false, last_synced_at: stamp, task_count: 2 },
  { id: repoNoise, name: 'noise-sim', path: 'extensions/noise-sim', state: 'attention', error: 'A source file needs attention before syncing.', pending: false, last_synced_at: stamp, task_count: 1 },
];
const task = (n, title, values = {}) => ({ id: id(n), title, status: 'open', list_id: listPersonal, revision: 7, description: '', created_at: stamp, updated_at: stamp, completed_at: null, cancelled_at: null, blocked_by: [], due: null, ...values });
let tasks = [
  task(10, 'Review the studio budget', { description: 'Keep a little room for the next project.', due: { value: '2026-10-20', timezone: 'America/Chicago', due_at: '2026-10-21T05:00:00Z' } }),
  task(11, 'Call the instrument repair shop', { status: 'in_progress' }),
  task(12, 'Add repository sync acceptance tests', { list_id: listFarm, repository_id: repoTracker, source: { headings: ['Frontend', 'Next steps'], path: 'private/tracker/TODO.md', present: true } }),
  task(13, 'Check motion cache consistency', { list_id: listFarm, repository_id: repoNoise, status: 'blocked' }),
  task(14, '<img src=x onerror="window.__taskXss=1"> stays plain text', { list_id: listFarm, repository_id: repoTracker }),
  task(15, 'Finished source audit', { list_id: listFarm, repository_id: repoTracker, status: 'completed', completed_at: stamp }),
];
const malicious = '<img src=x onerror="window.__detailXss=1">';
let details = [{ id: id(50), label: 'Reference note', kind: 'text', value: malicious, revision: 1, cancelled: false },
  { id: id(51), label: 'Structured reference', kind: 'json', value: { reference: '<script>window.__jsonXss=1</script>', ready: true }, revision: 1, cancelled: false }];
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
const errors = [], results = [];
await context.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url());
  if (url.origin !== origin) return route.abort();
  const method = request.method(), pathname = url.pathname;
  const body = method === 'GET' ? undefined : request.postDataJSON();
  state.requests.push({ method, path: pathname, query: Object.fromEntries(url.searchParams) });
  const reply = (status, data) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
  if (['/', '/app.js', '/app.css'].includes(pathname)) {
    const file = pathname === '/' ? 'index.html' : pathname.slice(1);
    return route.fulfill({ status: 200, contentType: file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html', body: await fs.readFile(root + '/' + file) });
  }
  if (pathname === '/auth/session') {
    if (state.denySession) return reply(403, { error: { code: 'service_access_denied', message: 'Tracker access denied for this session' } });
    return state.auth ? reply(200, { user: { id: 'fixture-user', email: 'fixture@example.invalid' } }) : reply(401, { error: { code: 'invalid_token', message: 'Sign in' } });
  }
  if (pathname === '/auth/login') {
    if (state.denyLogin) return reply(403, { error: { code: 'service_access_denied', message: 'Access denied', canSignOut: true } });
    state.auth = true; return reply(200, { user: { id: 'fixture-user', email: 'fixture@example.invalid' } });
  }
  if (pathname === '/auth/logout') { assert.deepEqual(body, {}); state.logoutCount++; state.auth = false; state.denySession = false; return reply(200, { ok: true }); }
  if (!state.auth) return reply(401, { error: { code: 'invalid_token', message: 'Sign in' } });
  if (pathname === '/browser/api/v1/lists' && method === 'GET') return reply(200, { data: { items: lists, next_cursor: null } });
  if (pathname === '/browser/api/v1/repositories') return reply(200, { data: { items: repos, interval_hours: 4 } });
  if (pathname === '/browser/api/v1/sync') { assert.equal(method, 'POST'); assert.deepEqual(body, {}); return reply(202, { data: { queued: true } }); }
  if (pathname === '/browser/api/v1/items' && method === 'GET') {
    let selected = tasks.filter(item => {
      const status = url.searchParams.get('status');
      return (status === 'all' || status === 'active' && ['open', 'in_progress', 'blocked'].includes(item.status) || status === item.status)
        && (!url.searchParams.has('list_id') || item.list_id === url.searchParams.get('list_id'))
        && (!url.searchParams.has('repository_id') || item.repository_id === url.searchParams.get('repository_id'))
        && (!url.searchParams.has('q') || item.title.toLowerCase().includes(url.searchParams.get('q').toLowerCase()))
        && (!url.searchParams.has('overdue_only') || item.due && new Date(item.due.due_at) < new Date());
    });
    const offset = Number(url.searchParams.get('cursor') || 0), more = selected.length > offset + 2;
    return reply(200, { data: { items: selected.slice(offset, offset + 2), next_cursor: more ? String(offset + 2) : null } });
  }
  if (pathname === '/browser/api/v1/items' && method === 'POST') {
    assert.ok(body.idempotency_key);
    if (body.list_id === listFarm) assert.ok([repoTracker, repoNoise].includes(body.repository_id));
    const created = task(100 + state.creates.length, body.title, body);
    tasks.push(created); state.creates.push(body); return reply(200, { data: created });
  }
  const itemMatch = /^\/browser\/api\/v1\/items\/([^/]+)$/.exec(pathname);
  if (itemMatch) {
    const index = tasks.findIndex(item => item.id === itemMatch[1]);
    assert.ok(index >= 0);
    if (method === 'GET') {
      const snapshot = structuredClone(tasks[index]);
      await deferGet(pathname); return reply(200, { data: snapshot });
    }
    assert.equal(method, 'PUT'); state.updates.push(body);
    assert.equal(body.expected_revision, tasks[index].revision);
    if (state.conflict) { state.conflict = false; tasks[index] = { ...tasks[index], revision: tasks[index].revision + 1 }; return reply(409, { error: { code: 'revision_conflict', message: 'Changed elsewhere' } }); }
    tasks[index] = { ...tasks[index], ...body, revision: tasks[index].revision + 1 };
    return reply(200, { data: tasks[index] });
  }
  if (/\/items\/[^/]+\/details$/.test(pathname)) {
    if (method === 'POST') { assert.ok(body.idempotency_key); state.detailCreates.push(body); details.push({ id: id(60), ...body }); return reply(200, { data: details.at(-1) }); }
    const snapshot = structuredClone(details);
    await deferGet(pathname); return reply(200, { data: { items: snapshot, next_cursor: null } });
  }
  if (/\/items\/[^/]+\/history$/.test(pathname)) return reply(200, { data: { items: [{ action: 'source_updated', at: stamp }], next_cursor: null } });
  throw new Error(`Unexpected fixture request ${method} ${pathname}`);
});
const page = await context.newPage();
page.on('pageerror', error => errors.push(error.message));
async function check(name, work) { await work(); results.push(name); console.log('PASS ' + name); }
async function releaseGet(gate) {
  const response = page.waitForResponse(value => new URL(value.url()).pathname === gate.path && value.request().method() === 'GET');
  gate.release(); await (await response).finished();
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}
try {
  await page.goto(origin);
  await page.locator('#workspace').waitFor({ state: 'visible' });
  await check('initial load and cursor pagination', async () => {
    await page.waitForFunction(() => document.querySelectorAll('.task-row').length === 2);
    await page.locator('#load-more').click();
    await page.waitForFunction(() => document.querySelectorAll('.task-row').length === 4);
    await page.locator('#load-more').click();
    await page.waitForFunction(() => document.querySelectorAll('.task-row').length === 5);
    assert.equal(await page.locator('#load-more').isVisible(), false);
    assert.equal(await page.locator('#tasks img').count(), 0);
    assert.equal(await page.evaluate(() => window.__taskXss), undefined);
  });
  await page.screenshot({ path: '/tmp/tracker-ui-desktop.png', fullPage: true });
  await check('late task reads cannot replace a newer task or draft, reopen a closed editor, or survive sign-out', async () => {
    const firstPath = '/browser/api/v1/items/' + id(10);
    const first = () => page.locator('.task-main').filter({ hasText: 'Review the studio budget' });
    const second = () => page.locator('.task-main').filter({ hasText: 'Call the instrument repair shop' });
    let held = holdGet(firstPath);
    await first().click(); await held.requested;
    await second().click(); await page.locator('#editor').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#task-form [name=title]').inputValue(), 'Call the instrument repair shop');
    await releaseGet(held);
    assert.equal(await page.locator('#task-form [name=title]').inputValue(), 'Call the instrument repair shop');
    await page.getByRole('button', { name: 'Close task', exact: true }).click();

    held = holdGet(firstPath);
    await first().click(); await held.requested;
    await page.locator('#new-task').click();
    await page.locator('#task-form [name=title]').fill('Preserve this new draft');
    await releaseGet(held);
    assert.equal(await page.locator('#editor-label').textContent(), 'NEW TASK');
    assert.equal(await page.locator('#task-form [name=title]').inputValue(), 'Preserve this new draft');
    await page.getByRole('button', { name: 'Close task', exact: true }).click();

    held = holdGet(firstPath);
    await first().click(); await held.requested;
    await second().click(); await page.locator('#editor').waitFor({ state: 'visible' });
    await page.keyboard.press('Escape');
    await page.locator('#editor').waitFor({ state: 'hidden' });
    await releaseGet(held);
    assert.equal(await page.locator('#editor').isVisible(), false);

    held = holdGet(firstPath);
    await first().click(); await held.requested;
    await page.locator('#logout').click();
    await page.locator('#login').waitFor({ state: 'visible' });
    await releaseGet(held);
    assert.equal(await page.locator('#editor').isVisible(), false);
    assert.equal(await page.locator('#workspace').isVisible(), false);
    state.auth = true; await page.reload();
    await page.locator('#workspace').waitFor({ state: 'visible' });
    await page.waitForFunction(() => document.querySelectorAll('.task-row').length === 2);
  });
  await check('late details cannot overwrite a reopened copy of the same task', async () => {
    const held = holdGet('/browser/api/v1/items/' + id(10) + '/details');
    details[0].label = 'Earlier details snapshot';
    const first = () => page.locator('.task-main').filter({ hasText: 'Review the studio budget' });
    await first().click(); await held.requested;
    await page.getByRole('button', { name: 'Close task', exact: true }).click();
    details[0].label = 'Current details snapshot';
    await first().click();
    await page.locator('#details').getByRole('heading', { name: 'Current details snapshot' }).waitFor();
    await releaseGet(held);
    assert.equal(await page.locator('#details').getByRole('heading', { name: 'Current details snapshot' }).count(), 1);
    assert.equal(await page.locator('#details').getByRole('heading', { name: 'Earlier details snapshot' }).count(), 0);
    await page.getByRole('button', { name: 'Close task', exact: true }).click();
  });
  await check('task editor details are safe text and updates retain expected_revision', async () => {
    await page.locator('.task-main').filter({ hasText: 'Review the studio budget' }).click();
    await page.locator('#editor').waitFor({ state: 'visible' });
    await page.locator('#details').getByText(malicious, { exact: true }).waitFor();
    assert.equal(await page.locator('#details img, #details script').count(), 0);
    assert.equal(await page.evaluate(() => window.__detailXss || window.__jsonXss), undefined);
    await page.locator('#task-form [name=title]').fill('Review the studio budget — revised');
    await page.locator('#save-task').click();
    await page.locator('#editor').waitFor({ state: 'hidden' });
    await page.getByText('Review the studio budget — revised', { exact: true }).waitFor();
    assert.equal(state.updates.at(-1).expected_revision, 7);
  });
  await check('revision conflict keeps unsaved editor open and directs reread', async () => {
    await page.locator('.task-main').filter({ hasText: 'Review the studio budget — revised' }).click();
    await page.locator('#task-form [name=title]').fill('Unsaved competing edit'); state.conflict = true;
    await page.locator('#save-task').click();
    await page.getByText('This task changed elsewhere. Close and reopen it to review the latest version before saving.', { exact: true }).waitFor();
    assert.equal(await page.locator('#editor').isVisible(), true);
    assert.equal(await page.locator('#task-form [name=title]').inputValue(), 'Unsaved competing edit');
    await page.getByRole('button', { name: 'Close task', exact: true }).click();
  });
  await check('search and status filters send correct queries', async () => {
    await page.locator('#search').fill('repair');
    await page.waitForFunction(() => document.querySelectorAll('.task-row').length === 1 && document.querySelector('.task-title')?.textContent.includes('repair'));
    assert.equal(state.requests.filter(request => request.path === '/browser/api/v1/items').at(-1).query.q, 'repair');
    await page.locator('#search').fill('');
    await page.locator('[data-status=completed]').click();
    await page.getByText('Finished source audit', { exact: true }).waitFor();
    await page.locator('[data-status=active]').click();
  });
  await check('Farm repository navigation and create task bind repository_id', async () => {
    await page.locator('#navigation button').filter({ hasText: /^Farm$/ }).click();
    await page.locator('#navigation button').filter({ hasText: /^tracker$/ }).click();
    await page.getByRole('heading', { name: 'tracker', exact: true }).waitFor();
    await page.locator('#new-task').click();
    assert.equal(await page.locator('#task-form [name=list_id]').inputValue(), listFarm);
    assert.equal(await page.locator('#task-form [name=repository_id]').inputValue(), repoTracker);
    await page.locator('#task-form [name=title]').fill('Created in the browser for TODO.md');
    await page.locator('#save-task').click();
    await page.locator('#editor').waitFor({ state: 'hidden' });
    assert.equal(state.creates.at(-1).repository_id, repoTracker);
    assert.equal(state.creates.at(-1).list_id, listFarm);
  });
  await check('completion checkbox uses latest revision and changes status', async () => {
    const target = tasks.find(item => item.id === id(12));
    await page.getByRole('checkbox', { name: 'Complete Add repository sync acceptance tests', exact: true }).check();
    await page.waitForFunction(() => !document.querySelector('#tasks')?.textContent.includes('Add repository sync acceptance tests'));
    assert.equal(state.updates.at(-1).expected_revision, target.revision);
    assert.equal(state.updates.at(-1).status, 'completed');
  });
  await check('structured JSON details validate locally and add through API', async () => {
    await page.locator('.task-main').filter({ hasText: 'stays plain text' }).click();
    await page.locator('#detail-form [name=label]').fill('Build data');
    await page.locator('#detail-form [name=kind]').selectOption('json');
    await page.locator('#detail-form [name=value]').fill('{broken');
    await page.locator('#detail-form button[type=submit]').click();
    await page.getByText('Enter valid JSON for structured data.', { exact: true }).waitFor();
    assert.equal(state.detailCreates.length, 0);
    await page.locator('#detail-form [name=value]').fill('{"ready":true}');
    await page.locator('#detail-form button[type=submit]').click();
    await page.locator('#details').getByRole('heading', { name: 'Build data' }).waitFor();
    assert.deepEqual(state.detailCreates.at(-1).value, { ready: true });
    await page.getByRole('button', { name: 'Close task', exact: true }).click();
  });
  await check('sync button queues explicit request', async () => {
    await page.locator('#sync').click();
    await page.getByText('Repository sync requested. It will run in the background; refresh in a moment for the result.', { exact: true }).waitFor();
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: '/tmp/tracker-ui-mobile.png', fullPage: true });
  const mobile = await page.evaluate(() => ({ width: innerWidth, documentWidth: document.documentElement.scrollWidth, logoutVisible: Boolean(document.querySelector('#logout')?.getClientRects().length) }));
  console.log('MOBILE ' + JSON.stringify(mobile));
  await check('mobile task editor fits viewport and has usable controls', async () => {
    assert.ok(mobile.documentWidth <= mobile.width, 'Page overflows mobile viewport');
    assert.equal(mobile.logoutVisible, true, 'Mobile users need a visible sign-out control');
    await page.locator('#new-task').click();
    const box = await page.locator('#editor').boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= 390);
    await page.locator('#task-form [name=title]').fill('Mobile draft');
    await page.getByRole('button', { name: 'Close task', exact: true }).click();
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await check('signout and denied-session recovery remain usable', async () => {
    await page.locator('#logout').click();
    await page.locator('#login').waitFor({ state: 'visible' });
    state.auth = true; state.denySession = true;
    await page.reload();
    await page.locator('#login').waitFor({ state: 'visible' });
    await page.locator('#reset-login').waitFor({ state: 'visible' });
    const before = state.logoutCount;
    await page.locator('#reset-login').click();
    await page.locator('#reset-login').waitFor({ state: 'hidden' });
    assert.equal(state.logoutCount, before + 1);
    state.denyLogin = true;
    await page.locator('#login-form [name=email]').fill('fixture@example.invalid');
    await page.locator('#login-form [name=password]').fill('test-only-password');
    await page.locator('#login-form button[type=submit]').click();
    await page.locator('#reset-login').waitFor({ state: 'visible' });
    await page.getByText('Access denied', { exact: true }).waitFor();
    await page.locator('#reset-login').click();
    await page.locator('#reset-login').waitFor({ state: 'hidden' });
  });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: results.length, results, mobile, browserErrors: errors }, null, 2));
} finally { await browser.close(); }
