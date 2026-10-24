import http from 'node:http';
import fs from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { COOKIE, LIFETIME } from './sessions.mjs';
import { HttpError } from './service-token.mjs';
import { createTrackerFetch } from './tracker-fetch.mjs';

const ORIGIN = 'https://tracker.noiseflood.farm';
const UUID = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
const ROUTES = [
  [/^\/api\/capabilities$/, ['GET', 'HEAD']],
  [/^\/api\/v1\/(?:lists|items)$/, ['GET', 'HEAD', 'POST']],
  [new RegExp(`^/api/v1/(?:lists|items)/${UUID}$`), ['GET', 'HEAD', 'PUT']],
  [new RegExp(`^/api/v1/items/${UUID}/details$`), ['GET', 'HEAD', 'POST']],
  [new RegExp(`^/api/v1/items/${UUID}/details/${UUID}$`), ['PUT']],
  [new RegExp(`^/api/v1/items/${UUID}/history$`), ['GET', 'HEAD']],
  [new RegExp(`^/api/v1/items/${UUID}/skip$`), ['POST']],
  [/^\/api\/v1\/(?:recurrences|repositories)$/, ['GET', 'HEAD']],
  [new RegExp(`^/api/v1/recurrences/${UUID}$`), ['GET', 'HEAD', 'PUT']],
  [/^\/api\/v1\/sync$/, ['POST']],
];
const ASSETS = new Map([
  ['/', ['index.html', 'text/html; charset=utf-8']],
  ['/login', ['index.html', 'text/html; charset=utf-8']],
  ['/app.js', ['app.js', 'application/javascript; charset=utf-8']],
  ['/app.css', ['app.css', 'text/css; charset=utf-8']],
]);
const cookie = (id, age = LIFETIME / 1000) =>
  `${COOKIE}=${id}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=${age}`;
function handle(req) {
  const values = (req.headers.cookie || '').split(';').map(s => s.trim())
    .filter(s => s.startsWith(COOKIE + '='));
  return values.length === 1 ? values[0].slice(COOKIE.length + 1) : '';
}
async function body(req, maximum = 256 * 1024) {
  if (!/^application\/json(?:;|$)/i.test(req.headers['content-type'] || '') || req.headers['content-encoding'])
    throw new HttpError(415, 'invalid_request', 'Uncompressed JSON is required.');
  let size = 0; const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > maximum) throw new HttpError(413, 'invalid_request', 'Request is too large.');
    chunks.push(chunk);
  }
  let data;
  try { data = JSON.parse(Buffer.concat(chunks)); }
  catch { throw new HttpError(400, 'invalid_request', 'Invalid JSON request.'); }
  if (!data || Array.isArray(data) || typeof data !== 'object')
    throw new HttpError(400, 'invalid_request', 'A JSON object is required.');
  return data;
}

export function createApp(sessions, {
  upstream = createTrackerFetch(),
  publicDirectory = fileURLToPath(new URL('../public/', import.meta.url)),
} = {}) {
  const attempts = new Map();
  function limit(req) {
    const ip = req.headers['x-real-ip'] || req.socket.remoteAddress, now = Date.now();
    for (const [key, value] of attempts) if (value.until <= now) attempts.delete(key);
    const state = attempts.get(ip) || { count: 0, until: now + 60000 };
    if (state.count >= 10 || (!attempts.has(ip) && attempts.size >= 10000))
      throw new HttpError(429, 'rate_limited', 'Please wait a minute before trying again.');
    state.count++; attempts.set(ip, state);
  }
  async function authenticate(id) {
    const authorized = await sessions.authorize(id);
    const reply = await upstream('/api/capabilities', { token: authorized.token });
    if (reply.status !== 200) {
      if ([401, 403].includes(reply.status)) sessions.invalidate(id);
      throw new HttpError(reply.status, 'service_access_denied', 'Tracker access is unavailable.');
    }
    let data;
    try { data = JSON.parse(reply.body).data; } catch { /* Invalid upstream envelope. */ }
    if (data?.contract !== 'farm.tracker/1' || data.service_id !== 'tracker')
      throw new HttpError(502, 'invalid_response', 'Invalid Tracker response.');
    return authorized.user;
  }
  const server = http.createServer(async (req, res) => {
    const json = (status, data) => {
      res.setHeader('Content-Type', 'application/json'); res.writeHead(status); res.end(JSON.stringify(data));
    };
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'");
    try {
      const raw = req.url ?? '';
      const rawPath = raw.split('?')[0];
      if (!raw.startsWith('/') || raw.startsWith('//') || raw.length > 8192 ||
          /[\\#\x00-\x20]/.test(raw) || rawPath.includes('%') || /(?:^|\/)\.{1,2}(?:\/|$)/.test(rawPath))
        throw new HttpError(400, 'invalid_request', 'Invalid request path.');
      const url = new URL(raw, ORIGIN), pathname = url.pathname;
      if (pathname === '/healthz' && req.method === 'GET')
        return json(200, { data: { status: 'ok', contract: 'farm.tracker-web/1' } });
      if (ASSETS.has(pathname)) {
        if (!['GET', 'HEAD'].includes(req.method)) throw new HttpError(405, 'method_not_allowed', 'Method not allowed.');
        const [filename, type] = ASSETS.get(pathname);
        const contents = await fs.readFile(path.join(publicDirectory, filename));
        res.setHeader('Content-Type', type); res.writeHead(200);
        return res.end(req.method === 'HEAD' ? undefined : contents);
      }
      const apiPath = pathname.startsWith('/browser/api/') ? pathname.slice('/browser'.length) : null;
      const methods = apiPath ? ROUTES.find(([pattern]) => pattern.test(apiPath))?.[1] :
        pathname === '/auth/session' ? ['GET'] : ['/auth/login', '/auth/logout'].includes(pathname) ? ['POST'] : null;
      if (!methods) throw new HttpError(404, 'not_found', 'Unknown Tracker route.');
      if (!methods.includes(req.method)) {
        res.setHeader('Allow', methods.join(', '));
        throw new HttpError(405, 'method_not_allowed', 'Method not allowed.');
      }
      if (req.headers.authorization !== undefined)
        throw new HttpError(401, 'invalid_token', 'Use Farm sign-in.');
      const mutation = !['GET', 'HEAD'].includes(req.method);
      if (mutation && (req.headers.origin !== ORIGIN ||
          ![undefined, 'same-origin', 'none'].includes(req.headers['sec-fetch-site'])))
        throw new HttpError(403, 'invalid_origin', 'Reload this page before trying again.');
      const id = handle(req);
      if (pathname === '/auth/login') {
        limit(req);
        const data = await body(req, 8192);
        if (typeof data.email !== 'string' || !data.email.trim() || data.email.length > 320 ||
            typeof data.password !== 'string' || !data.password || data.password.length > 4096)
          throw new HttpError(400, 'invalid_request', 'Enter your email and password.');
        const next = await sessions.login(data.email.trim(), data.password, id);
        res.setHeader('Set-Cookie', cookie(next));
        try { return json(200, { user: await authenticate(next) }); }
        catch (error) { error.sessionId = next; throw error; }
      }
      if (pathname === '/auth/logout') {
        await body(req, 8192);
        if (id) await sessions.logout(id);
        res.setHeader('Set-Cookie', cookie('', 0)); return json(200, { ok: true });
      }
      if (pathname === '/auth/session') return json(200, { user: await authenticate(id) });
      const authorized = await sessions.authorize(id, mutation ? 'todos.write' : 'todos.read');
      const payload = mutation ? JSON.stringify(await body(req)) : undefined;
      const reply = await upstream(apiPath + url.search, { method: req.method, token: authorized.token, body: payload });
      if ([401, 403].includes(reply.status)) sessions.invalidate(id);
      res.setHeader('Content-Type', 'application/json');
      if (typeof reply.requestId === 'string' && /^[A-Za-z0-9-]{1,64}$/.test(reply.requestId))
        res.setHeader('X-Request-ID', reply.requestId);
      res.writeHead(reply.status); return res.end(req.method === 'HEAD' ? undefined : reply.body);
    } catch (error) {
      if (res.headersSent) { res.destroy(); return; }
      if (error.sessionId) res.setHeader('Set-Cookie', cookie(error.sessionId));
      const status = error.code === 'SESSION_REAUTHENTICATION_REQUIRED' ? 401 :
        [400, 401, 403, 404, 405, 409, 413, 415, 429, 500, 502, 503].includes(error.status) ? error.status : 503;
      if (status === 429) res.setHeader('Retry-After', '60');
      json(status, { error: { code: typeof error.code === 'string' ? error.code : 'authorization_unavailable',
        message: status === 401 ? 'Sign in with your Farm account.' :
          status === 403 ? 'Your request or Farm access was denied.' :
          status >= 500 ? 'Tracker or Farm sign-in is unavailable. Please try again.' : error.message,
        ...(error.sessionId || error.code === 'sign_out_first' ? { canSignOut: true } : {}) } });
    }
  });
  server.requestTimeout = 30000; server.headersTimeout = 10000; server.maxHeadersCount = 40;
  return server;
}
