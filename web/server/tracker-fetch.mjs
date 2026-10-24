import http from 'node:http';
import { HttpError } from './service-token.mjs';

export function createTrackerFetch({ port = 8792 } = {}) {
  return (route, { method = 'GET', token, body } = {}) => new Promise((resolve, reject) => {
    if (!route.startsWith('/api/') || route.startsWith('//') || /[\r\n\\#]/.test(route))
      return reject(new HttpError(400, 'invalid_route', 'Invalid Tracker route.'));
    const headers = {
      Accept: 'application/json', Authorization: `Bearer ${token}`, 'User-Agent': 'farm-tracker-web/1',
    };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      headers['Content-Length'] = Buffer.byteLength(body);
    }
    const req = http.request({ hostname: '127.0.0.1', port, path: route, method, headers }, res => {
      const chunks = []; let size = 0;
      res.on('data', chunk => {
        size += chunk.length;
        if (size > 4 * 1024 * 1024) req.destroy(new Error('Tracker response exceeds limit'));
        else chunks.push(chunk);
      });
      res.on('error', reject);
      res.on('aborted', () => reject(new Error('Tracker response interrupted')));
      res.on('end', () => {
        const status = res.statusCode ?? 502;
        if (status >= 300 && status < 400) return reject(new Error('Tracker redirect rejected'));
        if (!String(res.headers['content-type'] ?? '').startsWith('application/json'))
          return reject(new Error('Tracker response is not JSON'));
        resolve({ status, body: Buffer.concat(chunks), requestId: res.headers['x-request-id'] });
      });
    });
    const timeout = setTimeout(() => req.destroy(new Error('Tracker deadline exceeded')), 20000);
    req.once('close', () => clearTimeout(timeout));
    req.on('error', reject);
    req.end(body);
  });
}
