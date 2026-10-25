import { test } from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import http from 'node:http';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { authorityOrigin, createAuthorityFetch } from '../server/authority-fetch.mjs';
import { createCentralFetch } from '../server/central-fetch.mjs';
import { createJwksFetch, createVerifier } from '../server/service-token.mjs';

const credential = 'fbff_' + 'x'.repeat(43);
async function fixture(t, handler) {
  const seen = [], server = http.createServer((req, res) => {
    seen.push({ path: req.url, headers: req.headers });
    handler(req, res);
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  t.after(() => { server.closeAllConnections(); server.close(); });
  return { seen, transportOrigin: `http://127.0.0.1:${server.address().port}` };
}
function environment(values) {
  const before = Object.fromEntries(Object.keys(values).map(key => [key, process.env[key]]));
  Object.assign(process.env, values);
  return () => {
    for (const [key, value] of Object.entries(before)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  };
}

test('authority transport accepts only explicit loopback or the public authority origin', () => {
  for (const origin of ['http://127.0.0.1', 'http://127.0.0.1:8780/', 'https://127.0.0.1',
    'http://[::1]', 'https://[::1]:8780', 'https://noiseflood.farm', 'https://noiseflood.farm:443/'])
    assert.equal(authorityOrigin(origin).origin, new URL(origin).origin);
  for (const origin of ['', 'http://localhost', 'http://127.0.0.2', 'http://127.1', 'http://2130706433',
    'http://0x7f000001', 'http://0.0.0.0', 'http://[::ffff:127.0.0.1]', 'http://192.168.1.1',
    'https://attacker.invalid', 'http://noiseflood.farm', 'https://noiseflood.farm:444',
    'http://127.0.0.1/v1', 'http://127.0.0.1/?', 'http://127.0.0.1/#',
    'http://127.0.0.1?token=secret', 'http://127.0.0.1#fragment', 'http://user:secret@127.0.0.1',
    ' http://127.0.0.1', 'http://127.0.0.1\n', 'http://127.0.0.1\\', 'ftp://127.0.0.1',
    'http://127.0.0.1:0', 'http://127.0.0.1:65536']) {
    assert.throws(() => createCentralFetch({ credential, transportOrigin: origin }), /TRACKER_FARM_TRANSPORT_ORIGIN/);
    assert.throws(() => createJwksFetch({ transportOrigin: origin }), /TRACKER_FARM_TRANSPORT_ORIGIN/);
  }
});

test('bad transport configuration fails startup before loading private credentials', () => {
  const result = spawnSync(process.execPath, [fileURLToPath(new URL('../server/main.mjs', import.meta.url))], {
    env: { ...process.env, TRACKER_FARM_TRANSPORT_ORIGIN: 'https://user:secret@attacker.invalid' }, encoding: 'utf8',
  });
  assert.equal(result.status, 78);
  assert.match(result.stderr, /TRACKER_FARM_TRANSPORT_ORIGIN/);
  assert.ok(!result.stderr.includes('secret') && !result.stderr.includes('setup_web.py'));
});

test('shared configured transport pins actual Host and sends BFF credentials only to central auth', async t => {
  const f = await fixture(t, (req, res) => { res.setHeader('Content-Type', 'application/json'); res.end('{"keys":[]}'); });
  const proxy = await fixture(t, (req, res) => { res.writeHead(500); res.end(); });
  const restore = environment({ TRACKER_FARM_TRANSPORT_ORIGIN: f.transportOrigin,
    TRACKER_BFF_CLIENT_CREDENTIAL: credential, HTTP_PROXY: proxy.transportOrigin,
    HTTPS_PROXY: proxy.transportOrigin, ALL_PROXY: proxy.transportOrigin, NO_PROXY: '', NODE_USE_ENV_PROXY: '1' });
  t.after(restore);
  const central = createCentralFetch(), jwks = createJwksFetch();
  await central('https://noiseflood.farm/v1/auth/me', { headers: {
    Host: 'attacker.invalid', Authorization: 'Bearer session-token', Cookie: 'private=browser',
    'X-API-Key': 'wrong', 'Proxy-Authorization': 'secret-proxy',
  } });
  await jwks();
  assert.equal(proxy.seen.length, 0);
  assert.equal(f.seen.length, 2);
  for (const request of f.seen) {
    assert.equal(request.headers.host, 'noiseflood.farm');
    assert.equal(request.headers['user-agent'], 'farm-tracker-web/1');
    assert.equal(request.headers.cookie, undefined);
    assert.equal(request.headers['x-api-key'], undefined);
    assert.equal(request.headers['proxy-authorization'], undefined);
  }
  assert.equal(f.seen[0].path, '/v1/auth/me');
  assert.equal(f.seen[0].headers.authorization, 'Bearer session-token');
  assert.equal(f.seen[0].headers['x-farm-client-credential'], credential);
  assert.equal(f.seen[1].path, '/v1/auth/jwks');
  assert.equal(f.seen[1].headers.authorization, undefined);
  assert.equal(f.seen[1].headers['x-farm-client-credential'], undefined);
});

test('default transport is loopback and explicit public rollback retains the canonical authority', () => {
  const previous = process.env.TRACKER_FARM_TRANSPORT_ORIGIN;
  delete process.env.TRACKER_FARM_TRANSPORT_ORIGIN;
  try { assert.equal(authorityOrigin().origin, 'http://127.0.0.1'); }
  finally {
    if (previous !== undefined) process.env.TRACKER_FARM_TRANSPORT_ORIGIN = previous;
  }
  assert.equal(authorityOrigin('https://noiseflood.farm').origin, 'https://noiseflood.farm');
});

test('central logical destination and credential validation still happen before network access', async t => {
  const f = await fixture(t, (req, res) => res.end('{}'));
  const central = createCentralFetch({ credential, transportOrigin: f.transportOrigin });
  for (const url of ['http://127.0.0.1/v1/auth/me', 'https://attacker.invalid/v1/auth/me',
    'https://noiseflood.farm/v1/auth/jwks', 'https://noiseflood.farm/v1/process/render',
    'https://noiseflood.farm/v1/auth/me?token=secret', 'https://noiseflood.farm/v1/auth/me#fragment',
    'https://user:secret@noiseflood.farm/v1/auth/me']) await assert.rejects(central(url));
  await assert.rejects(createCentralFetch({ credential: '', transportOrigin: f.transportOrigin })('https://noiseflood.farm/v1/auth/me'));
  assert.equal(f.seen.length, 0);
});

test('central and JWKS reject redirects without contacting the destination', async t => {
  const destination = await fixture(t, (req, res) => res.end('{}'));
  const f = await fixture(t, (req, res) => { res.writeHead(302, { Location: destination.transportOrigin + '/escaped' }); res.end(); });
  await assert.rejects(createCentralFetch({ credential, transportOrigin: f.transportOrigin })('https://noiseflood.farm/v1/auth/me'), /redirect rejected/);
  await assert.rejects(createJwksFetch({ transportOrigin: f.transportOrigin })(), /redirect rejected/);
  assert.equal(destination.seen.length, 0);
});

test('central and JWKS bound response bytes while receiving data', async t => {
  const f = await fixture(t, (req, res) => {
    res.setHeader('Content-Type', 'application/json');
    res.end('x'.repeat((req.url.endsWith('/jwks') ? 128 : 256) * 1024 + 1));
  });
  await assert.rejects(createCentralFetch({ credential, transportOrigin: f.transportOrigin })('https://noiseflood.farm/v1/auth/me'));
  await assert.rejects(createJwksFetch({ transportOrigin: f.transportOrigin })());
});

test('absolute deadline stops a trickling response and cancellation prevents an outgoing request', async t => {
  const f = await fixture(t, (req, res) => {
    res.writeHead(200); res.write(' ');
    const timer = setInterval(() => res.write(' '), 5);
    res.once('close', () => clearInterval(timer));
  });
  const authority = createAuthorityFetch({ transportOrigin: f.transportOrigin, timeoutMs: 50 });
  await assert.rejects(authority('/v1/auth/jwks'), /deadline exceeded|interrupted/);
  assert.equal(f.seen.length, 1);
  const controller = new AbortController(); controller.abort();
  await assert.rejects(authority('/v1/auth/jwks', { signal: controller.signal }), /cancelled/);
  assert.equal(f.seen.length, 1);
});

test('JWTs fetched over loopback retain public issuer, Tracker audience, expiry and scoped authorization checks', async t => {
  const { publicKey, privateKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
  const jwk = { ...publicKey.export({ format: 'jwk' }), kid: 'loopback-test', alg: 'RS256', use: 'sig' };
  const f = await fixture(t, (req, res) => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ keys: [jwk] })); });
  const verify = createVerifier(createJwksFetch({ transportOrigin: f.transportOrigin }));
  const now = Math.floor(Date.now() / 1000);
  function signed(overrides = {}) {
    const claims = { iss: 'https://noiseflood.farm', aud: 'urn:farm:service:tracker', sub: 'user-1',
      sid: 'family-1', jti: 'request-1', client_id: 'tracker-web', scope: 'todos.read', iat: now, exp: now + 300, ...overrides };
    const data = [JSON.stringify({ alg: 'RS256', typ: 'at+jwt', kid: jwk.kid }), JSON.stringify(claims)]
      .map(value => Buffer.from(value).toString('base64url')).join('.');
    return data + '.' + crypto.sign('RSA-SHA256', Buffer.from(data), privateKey).toString('base64url');
  }
  assert.equal((await verify(signed(), 'todos.read')).sub, 'user-1');
  for (const claims of [{ iss: f.transportOrigin }, { aud: f.transportOrigin }, { aud: 'urn:farm:service:manager' },
    { exp: now }, { scope: 'todos.write' }, { client_id: 'manager-web' }])
    await assert.rejects(verify(signed(claims), 'todos.read'), { status: 401 });
  assert.equal(f.seen.length, 1);
  assert.equal(f.seen[0].headers.host, 'noiseflood.farm');
});
