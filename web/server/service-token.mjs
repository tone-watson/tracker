import crypto from 'node:crypto';

export class HttpError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}
const invalid = () => new HttpError(401, 'invalid_token', 'Sign in to use Tracker.');
const identifier = value => typeof value === 'string' && /^[\w:@.-]{1,256}$/.test(value);


export function createVerifier(fetchKeys = async () => {
  const res = await fetch('https://noiseflood.farm/v1/auth/jwks', {
    headers: { 'User-Agent': 'farm-tracker-web/1' }, redirect: 'error', signal: AbortSignal.timeout(10000), credentials: 'omit',
  });
  if (!res.ok) throw new HttpError(503, 'auth_unavailable', 'Farm sign-in is unavailable.');
  const text = await res.text();
  if (text.length > 128 * 1024) throw invalid();
  return JSON.parse(text);
}) {
  let keys = new Map(), updated = 0, pending;
  async function refresh() {
    pending ??= (async () => {
      const data = await fetchKeys();
      if (!Array.isArray(data.keys) || !data.keys.length || data.keys.length > 32) throw invalid();
      const next = new Map();
      for (const jwk of data.keys) {
        if (jwk.kty !== 'RSA' || jwk.alg !== 'RS256' || jwk.use !== 'sig' ||
            !identifier(jwk.kid) || ['d', 'p', 'q', 'dp', 'dq', 'qi', 'oth'].some(field => Object.hasOwn(jwk, field)) ||
            (jwk.key_ops !== undefined && JSON.stringify(jwk.key_ops) !== '["verify"]') || next.has(jwk.kid)) throw invalid();
        const key = crypto.createPublicKey({ key: jwk, format: 'jwk' });
        if (key.asymmetricKeyDetails.modulusLength < 2048) throw invalid();
        next.set(jwk.kid, key);
      }
      keys = next; updated = Date.now();
    })().finally(() => { pending = null; });
    return pending;
  }
  return async (token, expectedScope) => {
    if (!['todos.read', 'todos.write'].includes(expectedScope)) throw invalid();
    if (typeof token !== 'string' || token.length > 16384) throw invalid();
    const parts = token.split('.');
    if (parts.length !== 3 || parts.some(p => !/^[A-Za-z0-9_-]+$/.test(p))) throw invalid();
    let header, claims;
    try {
      header = JSON.parse(Buffer.from(parts[0], 'base64url'));
      claims = JSON.parse(Buffer.from(parts[1], 'base64url'));
    } catch { throw invalid(); }
    if (!header || !claims || header.alg !== 'RS256' || header.typ !== 'at+jwt' ||
        !identifier(header.kid) || ['jku', 'x5u', 'crit'].some(field => Object.hasOwn(header, field))) throw invalid();
    if (!keys.has(header.kid) || Date.now() - updated > 300000) await refresh();
    const key = keys.get(header.kid);
    if (!key || !crypto.verify('RSA-SHA256', Buffer.from(parts.slice(0, 2).join('.')),
        key, Buffer.from(parts[2], 'base64url'))) throw invalid();
    const now = Math.floor(Date.now() / 1000);
    if (claims.iss !== 'https://noiseflood.farm' || claims.aud !== 'urn:farm:service:tracker' ||
        claims.client_id !== 'tracker-web' || claims.scope !== expectedScope ||
        !identifier(claims.sub) || claims.sub.length > 128 || claims.sub.startsWith('machine:') || !identifier(claims.sid) || !identifier(claims.jti) ||
        !Number.isSafeInteger(claims.iat) || !Number.isSafeInteger(claims.exp) ||
        claims.iat > now + 30 || claims.exp <= now || claims.exp <= claims.iat ||
        claims.exp - claims.iat > 300 || (claims.nbf !== undefined &&
          (!Number.isSafeInteger(claims.nbf) || claims.nbf > now + 30))) throw invalid();
    return claims;
  };
}
