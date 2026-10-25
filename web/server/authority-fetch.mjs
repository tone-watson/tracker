import http from 'node:http';
import https from 'node:https';

export const FARM_ORIGIN = 'https://noiseflood.farm';
const agents = { 'http:': new http.Agent({ keepAlive: true }), 'https:': new https.Agent({ keepAlive: true }) };

export function authorityOrigin(value = process.env.TRACKER_FARM_TRANSPORT_ORIGIN ?? 'http://127.0.0.1') {
  const match = typeof value === 'string' && /^(https?):\/\/(\[[^\]]+\]|[^:/]+)(?::([0-9]+))?\/?$/.exec(value);
  const invalid = () => new Error('Invalid TRACKER_FARM_TRANSPORT_ORIGIN: use a 127.0.0.1 or [::1] HTTP(S) origin, or https://noiseflood.farm');
  if (!match || /[\s\\?#@]/.test(value)) throw invalid();
  let origin;
  try { origin = new URL(value); } catch { throw invalid(); }
  if (origin.port === '0') throw invalid();
  const loopback = ['127.0.0.1', '[::1]'].includes(match[2]);
  const publicOrigin = match[1] === 'https' && match[2] === 'noiseflood.farm' && origin.origin === FARM_ORIGIN;
  if (!loopback && !publicOrigin) throw invalid();
  return origin;
}

export function createAuthorityFetch({ transportOrigin, maximumBytes = 256 * 1024, timeoutMs = 15000 } = {}) {
  const base = authorityOrigin(transportOrigin);
  return async (route, init = {}) => {
    if (!/^\/v1\/auth\/[a-z/-]+$/.test(route) || route.includes('//'))
      throw new Error('Invalid Farm authority path');
    if (init.signal?.aborted) throw new Error('Authority request cancelled');
    const headers = new Headers(init.headers);
    headers.set('Host', 'noiseflood.farm');
    headers.set('User-Agent', 'farm-tracker-web/1');
    headers.delete('proxy-authorization');
    const body = init.body == null ? undefined : String(init.body);
    if (body !== undefined) headers.set('Content-Length', String(Buffer.byteLength(body)));
    const target = new URL(route, base);
    return new Promise((resolve, reject) => {
      const request = (target.protocol === 'https:' ? https : http).request(target, {
        method: init.method ?? 'GET', headers: Object.fromEntries(headers), agent: agents[target.protocol],
      }, response => {
        const status = response.statusCode ?? 502;
        if (status >= 300 && status < 400) {
          reject(new Error('Authority redirect rejected'));
          response.destroy();
          request.destroy();
          return;
        }
        const chunks = []; let size = 0;
        response.on('data', chunk => {
          size += chunk.length;
          if (size > maximumBytes) request.destroy(new Error('Authority response too large'));
          else chunks.push(chunk);
        });
        response.on('error', reject);
        response.on('aborted', () => reject(new Error('Authority response interrupted')));
        response.on('end', () => {
          const resultHeaders = new Headers();
          for (const name of ['content-type', 'retry-after']) {
            const value = response.headers[name];
            if (typeof value === 'string') resultHeaders.set(name, value);
          }
          resolve(new Response([204, 205, 304].includes(status) ? null : Buffer.concat(chunks), {
            status, headers: resultHeaders,
          }));
        });
      });
      const abort = () => request.destroy(new Error('Authority request cancelled'));
      const timer = setTimeout(() => request.destroy(new Error('Authority deadline exceeded')), timeoutMs);
      init.signal?.addEventListener('abort', abort, { once: true });
      request.once('close', () => {
        clearTimeout(timer);
        init.signal?.removeEventListener('abort', abort);
      });
      request.on('error', reject);
      request.end(body);
    });
  };
}
