import { test } from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createSessions, COOKIE, LIFETIME } from '../server/sessions.mjs';
import { createVerifier } from '../server/service-token.mjs';
import { createApp } from '../server/app.mjs';
import { createBffVault } from '../server/vault.mjs';
import { createCentralFetch } from '../server/central-fetch.mjs';
import { createTrackerFetch } from '../server/tracker-fetch.mjs';
import http from 'node:http';

const { publicKey, privateKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
const jwk = { ...publicKey.export({ format: 'jwk' }), kid: 'test-key', alg: 'RS256', use: 'sig' };
function token(overrides = {}, header = {}) {
  const now = Math.floor(Date.now() / 1000);
  const claims = { iss: 'https://noiseflood.farm', aud: 'urn:farm:service:tracker', sub: 'user-1',
    client_id: 'tracker-web', sid: 'family-1', jti: crypto.randomUUID(), iat: now, exp: now + 300,
    scope: 'todos.read', ...overrides };
  const data = [JSON.stringify({ alg: 'RS256', typ: 'at+jwt', kid: jwk.kid, ...header }), JSON.stringify(claims)]
    .map(value => Buffer.from(value).toString('base64url')).join('.');
  return data + '.' + crypto.sign('RSA-SHA256', Buffer.from(data), privateKey).toString('base64url');
}
const verifier = () => createVerifier(async () => ({ keys: [jwk] }));
function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'tracker-auth-test-'));
  const key = crypto.randomBytes(32).toString('base64');
  const state = { deny: false, logoutFails: false, services: 0, me: 0, refresh: false, lostRefresh: false, refreshCalls: 0 };
  const pair = { token: 'central-access-secret', refresh_token: 'central-refresh-secret',
    user_id: 'user-1', email: 'user@example.invalid', app_session: { id: 'family-1', client_id: 'tracker-web' } };
  const central = async (input, init) => {
    const route = new URL(input).pathname;
    const reply = (status, data) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
    if (route.endsWith('/login')) {
      assert.equal(JSON.parse(init.body).client_id, 'tracker-web');
      return reply(200, { success: true, data: pair });
    }
    if (route.endsWith('/me')) { state.me++; return reply(200, { success: true, data: pair }); }
    if (route.endsWith('/service-token')) {
      state.services++;
      if (state.refresh) return reply(401, { error: 'SESSION_EXPIRED', message: 'Expired' });
      if (state.deny) return reply(403, { error: 'insufficient_scope', message: 'Denied' });
      return reply(200, { success: true, data: { contract: 'farm.service-access/1', service_id: 'tracker',
        audience: 'urn:farm:service:tracker', token_type: 'Bearer', scope: JSON.parse(init.body).scopes[0], expires_in: 300, access_token: token({ scope: JSON.parse(init.body).scopes[0] }) } });
    }
    if (route.endsWith('/refresh')) {
      state.refreshCalls++;
      if (state.lostRefresh) throw new Error('Lost rotating response');
      state.refresh = false;
      return reply(200, { success: true, data: { ...pair, token: 'rotated-access', refresh_token: 'rotated-refresh' } });
    }
    if (route.endsWith('/logout')) return reply(state.logoutFails ? 503 : 200, state.logoutFails ? { error: 'Unavailable' } : { success: true });
    throw new Error('Unexpected authority route: ' + route);
  };
  const options = { directory, key, central, verify: verifier() };
  let sessions = createSessions(options);
  t.after(() => { sessions.dispose(); fs.rmSync(directory, { recursive: true, force: true }); });
  return { options, state, get sessions() { return sessions; }, restart() { sessions.dispose(); sessions = createSessions(options); } };
}

test('service tokens reject wrong audience, scope, client, type, lifetime and signature', async () => {
  const verify = verifier();
  assert.equal((await verify(token(), 'todos.read')).sub, 'user-1');
  for (const claims of [{ aud: 'urn:farm:service:manager' }, { scope: 'admin' }, { client_id: 'manager-web' },
    { exp: 1 }, { exp: Math.floor(Date.now() / 1000) + 301 }, { sid: '' }, { sub: 'machine:task' }, { iss: 'https://attacker.invalid' }])
    await assert.rejects(verify(token(claims), 'todos.read'));
  await assert.rejects(verify(token({}, { typ: 'farm-machine+jwt' }), 'todos.read'));
  await assert.rejects(verify(token({}, { jku: 'https://attacker.invalid' }), 'todos.read'));
  await assert.rejects(verify(token({}, { jku: '' }), 'todos.read'));
  const parts = token().split('.'); parts[2] = Buffer.alloc(256).toString('base64url');
  await assert.rejects(verify(parts.join('.'), 'todos.read'));
});

test('real shared client persists encrypted sessions, restores after restart and coalesces authorization', async t => {
  const f = fixture(t), id = await f.sessions.login('user@example.invalid', 'password');
  await Promise.all(Array.from({ length: 12 }, () => f.sessions.authorize(id)));
  assert.equal(f.state.services, 1);
  const disk = fs.readFileSync(path.join(f.options.directory, fs.readdirSync(f.options.directory)[0]), 'utf8');
  assert.ok(!disk.includes('central-access-secret') && !disk.includes('central-refresh-secret'));
  f.restart();
  assert.equal((await f.sessions.authorize(id)).user.id, 'user-1');
  assert.equal(f.state.me, 1);
  await assert.rejects(f.sessions.login('other@example.invalid', 'password', id), { status: 409 });
});

test('denial preserves logout, failed logout retains authority, successful logout invalidates cookie', async t => {
  const f = fixture(t), id = await f.sessions.login('user@example.invalid', 'password');
  f.state.deny = true;
  await assert.rejects(f.sessions.authorize(id), { status: 403 });
  f.state.logoutFails = true;
  await assert.rejects(f.sessions.logout(id));
  assert.equal(fs.readdirSync(f.options.directory).length, 1);
  f.state.logoutFails = false;
  await f.sessions.logout(id);
  await assert.rejects(f.sessions.authorize(id), { status: 401 });
  assert.equal(fs.readdirSync(f.options.directory).length, 0);
});

test('renewal persists replacement credentials and an interrupted renewal cannot replay after restart', async t => {
  const f = fixture(t), id = await f.sessions.login('user@example.invalid', 'password');
  f.state.refresh = true;
  await f.sessions.authorize(id);
  assert.equal(f.state.refreshCalls, 1);
  const vault = createBffVault(f.options.directory, f.options.key);
  assert.equal(vault.load(id).tokens.refresh_token, 'rotated-refresh');
  f.restart(); f.state.refresh = true; f.state.lostRefresh = true;
  await assert.rejects(f.sessions.authorize(id));
  assert.equal(vault.load(id).tokens.refresh_pending, true);
  f.restart();
  await assert.rejects(f.sessions.authorize(id), { status: 401 });
  assert.equal(f.state.refreshCalls, 2);
});

test('HTTP login uses opaque secure cookies, rejects CSRF/bearer fallback, and never exposes central tokens', async t => {
  const f = fixture(t), server = createApp(f.sessions, { upstream: async () => ({ status: 200, body: JSON.stringify({ data: { contract: 'farm.tracker/1', service_id: 'tracker' } }) }) });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const headers = { 'Content-Type': 'application/json', Origin: 'https://tracker.noiseflood.farm' };
  const body = JSON.stringify({ email: 'user@example.invalid', password: 'password' });
  assert.equal((await fetch(base + '/auth/session')).status, 401);
  assert.equal((await fetch(base + '/auth/login', { method: 'POST', headers: { ...headers, Origin: 'https://attacker.invalid' }, body })).status, 403);
  const result = await fetch(base + '/auth/login', { method: 'POST', headers, body });
  assert.equal(result.status, 200);
  const setCookie = result.headers.get('set-cookie');
  assert.match(setCookie, /^__Host-tracker_session=[A-Za-z0-9_-]{43};/);
  assert.match(setCookie, /Secure; HttpOnly; SameSite=Lax/);
  assert.ok(!setCookie.includes('Domain='));
  const text = await result.text(); assert.ok(!text.includes('secret') && !text.includes('token'));
  const Cookie = setCookie.split(';')[0];
  assert.equal((await fetch(base + '/auth/session', { headers: { Cookie } })).status, 200);
  assert.equal((await fetch(base + '/auth/session', { headers: { Cookie, Authorization: 'Bearer wrong' } })).status, 401);
  assert.equal((await fetch(base + '/auth/session', { headers: { Cookie: Cookie + '; ' + Cookie } })).status, 401);
  assert.equal((await fetch(base + '/server/main.mjs')).status, 404);
  assert.equal((await fetch(base + '/auth/logout', { method: 'POST', headers: { ...headers, Cookie }, body: '{}' })).status, 200);
  assert.equal((await fetch(base + '/auth/session', { headers: { Cookie } })).status, 401);
  f.state.deny = true;
  const denied = await fetch(base + '/auth/login', { method: 'POST', headers, body });
  assert.equal(denied.status, 403);
  assert.equal((await denied.json()).error.canSignOut, true);
  const deniedCookie = denied.headers.get('set-cookie').split(';')[0];
  assert.equal((await fetch(base + '/auth/logout', { method: 'POST', headers: { ...headers, Cookie: deniedCookie }, body: '{}' })).status, 200);
});

test('central transport refuses arbitrary destinations before sending its private credential', async () => {
  const central = createCentralFetch({ credential: 'fbff_' + 'x'.repeat(43) });
  for (const url of ['https://attacker.invalid/v1/auth/login', 'https://noiseflood.farm/v1/process/render',
    'https://noiseflood.farm/v1/auth/login?token=oops']) await assert.rejects(central(url));
});

test('vault sweep removes only expired tokenless records and preserves credentials and in-use records', t => {
  const f = fixture(t), vault = createBffVault(f.options.directory, f.options.key);
  const old = { createdAt: Date.now() - LIFETIME - 1000, tokens: null };
  const expired = vault.create(), active = vault.create(), credentials = vault.create(), journal = vault.create(), recent = vault.create();
  vault.save(expired, old); vault.save(active, old);
  vault.save(credentials, { ...old, tokens: { refresh_token: 'keep-for-logout' } });
  vault.save(journal, { ...old, tokens: { refresh_pending: true } });
  assert.equal(vault.sweep({ maxAge: LIFETIME, excludeIds: [active] }), 1);
  assert.equal(vault.load(expired), null);
  for (const id of [active, credentials, journal, recent]) assert.ok(vault.load(id));
});

test('browser proxy preserves revisions and conflicts, chooses per-operation scopes and rechecks live entitlement', async t => {
  const f = fixture(t), calls = [], state = { denied: false };
  const server = createApp(f.sessions, { upstream: async (route, options) => {
    calls.push({ route, options });
    const claims = JSON.parse(Buffer.from(options.token.split('.')[1], 'base64url'));
    if (state.denied) return { status: 403, body: JSON.stringify({ error: { code: 'service_access_denied' } }) };
    if (route === '/api/capabilities') return { status: 200, body: JSON.stringify({ data: { contract: 'farm.tracker/1', service_id: 'tracker' } }) };
    assert.equal(claims.scope, options.method === 'GET' ? 'todos.read' : 'todos.write');
    if (options.method === 'PUT') {
      assert.deepEqual(JSON.parse(options.body), { expected_revision: 2, title: 'changed' });
      return { status: 409, body: JSON.stringify({ error: { code: 'revision_conflict' } }), requestId: 'request-1' };
    }
    return { status: 200, body: JSON.stringify({ data: { items: [] } }) };
  } });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const headers = { 'Content-Type': 'application/json', Origin: 'https://tracker.noiseflood.farm' };
  const login = await fetch(base + '/auth/login', { method: 'POST', headers, body: JSON.stringify({ email: 'user@example.invalid', password: 'password' }) });
  const Cookie = login.headers.get('set-cookie').split(';')[0];
  headers.Cookie = Cookie;
  const get = () => fetch(base + '/browser/api/v1/items?limit=50&status=open', { headers: { Cookie } });
  assert.equal((await get()).status, 200);
  assert.equal(calls.at(-1).route, '/api/v1/items?limit=50&status=open');
  const before = calls.length;
  for (const route of ['/browser/api/v1/unregistered', '/browser/api/v1/items/../../auth/login', '/browser/api/v1/items/%2fsecrets', '/server/main.mjs', '/api/v1/items'])
    assert.ok([400, 404].includes((await fetch(base + route, { headers })).status));
  assert.equal(calls.length, before);
  const item = '/browser/api/v1/items/12345678-1234-1234-1234-123456789abc';
  const payload = JSON.stringify({ expected_revision: 2, title: 'changed' });
  for (const extra of [{ Origin: 'https://evil.invalid' }, { 'Sec-Fetch-Site': 'cross-site' }])
    assert.equal((await fetch(base + item, { method: 'PUT', headers: { ...headers, ...extra }, body: payload })).status, 403);
  assert.equal(calls.length, before);
  const conflict = await fetch(base + item, { method: 'PUT', headers, body: payload });
  assert.equal(conflict.status, 409);
  assert.equal((await conflict.json()).error.code, 'revision_conflict');
  assert.equal(conflict.headers.get('x-request-id'), 'request-1');
  for (const [route, method] of [['/repositories', 'GET'], ['/sync', 'POST']])
    assert.equal((await fetch(base + '/browser/api/v1' + route, { method, headers, ...(method === 'POST' ? { body: '{}' } : {}) })).status, 200);
  assert.equal((await fetch(base + '/browser/api/v1/sync', { method: 'GET', headers })).status, 405);
  state.denied = true;
  assert.equal((await get()).status, 403);
  assert.equal((await fetch(base + '/auth/session', { headers: { Cookie } })).status, 403);
});

test('unauthenticated requests, unsupported bodies and oversized mutations never reach Tracker', async t => {
  const f = fixture(t), calls = [];
  const server = createApp(f.sessions, { upstream: async (...args) => {
    calls.push(args); return { status: 200, body: JSON.stringify({ data: { contract: 'farm.tracker/1', service_id: 'tracker' } }) };
  } });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const headers = { 'Content-Type': 'application/json', Origin: 'https://tracker.noiseflood.farm' };
  assert.equal((await fetch(base + '/browser/api/v1/lists', { method: 'POST', headers, body: '{}' })).status, 401);
  assert.equal(calls.length, 0);
  const id = await f.sessions.login('user@example.invalid', 'password'); headers.Cookie = `${COOKIE}=${id}`;
  for (const [overrides, body, expected] of [
    [{ 'Content-Type': 'text/plain' }, '{}', 415],
    [{ 'Content-Encoding': 'gzip' }, '{}', 415],
    [{}, '[]', 400], [{}, '{', 400], [{}, JSON.stringify({ text: 'x'.repeat(262144) }), 413],
  ]) assert.equal((await fetch(base + '/browser/api/v1/lists', { method: 'POST', headers: { ...headers, ...overrides }, body })).status, expected);
  assert.equal(calls.length, 0);
});

test('fixed upstream sends only service credentials and refuses redirects and oversized responses', async t => {
  const seen = [], server = http.createServer((req, res) => {
    seen.push(req);
    if (req.url === '/api/redirect') { res.writeHead(302, { Location: 'http://attacker.invalid' }); return res.end(); }
    res.setHeader('Content-Type', 'application/json');
    res.end(req.url === '/api/large' ? 'x'.repeat(4 * 1024 * 1024 + 1) : '{"data":{}}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const upstream = createTrackerFetch({ port: server.address().port });
  assert.equal((await upstream('/api/v1/lists', { token: 'service-token' })).status, 200);
  assert.equal(seen[0].headers.authorization, 'Bearer service-token');
  assert.equal(seen[0].headers.cookie, undefined);
  assert.equal(seen[0].headers['x-farm-client-credential'], undefined);
  await assert.rejects(upstream('/api/redirect', { token: 'service-token' }));
  await assert.rejects(upstream('/api/large', { token: 'service-token' }));
  await assert.rejects(upstream('http://attacker.invalid/api/lists', { token: 'service-token' }));
});

test('confidential central transport identifies itself and never forwards browser cookies', async t => {
  const seen = [], server = http.createServer((req, res) => {
    seen.push(req); res.setHeader('Content-Type', 'application/json'); res.end('{}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const credential = 'fbff_' + 'x'.repeat(43);
  const central = createCentralFetch({ credential, transportOrigin: `http://127.0.0.1:${server.address().port}` });
  await central('https://noiseflood.farm/v1/auth/me', { headers: { Cookie: 'private=browser', 'X-API-Key': 'wrong' } });
  assert.equal(seen[0].headers['user-agent'], 'farm-tracker-web/1');
  assert.equal(seen[0].headers['x-farm-client-credential'], credential);
  assert.equal(seen[0].headers.cookie, undefined);
  assert.equal(seen[0].headers['x-api-key'], undefined);
});
