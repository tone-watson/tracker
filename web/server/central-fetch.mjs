/** BFF-only transport: one configured authority, no redirects/cookies or token URLs. */
import { createAuthorityFetch, FARM_ORIGIN } from './authority-fetch.mjs';

export function createCentralFetch({ credential = process.env.TRACKER_BFF_CLIENT_CREDENTIAL ?? '', transportOrigin } = {}) {
  const authority = createAuthorityFetch({ transportOrigin });
  return async (input, init = {}) => {
    const logical = new URL(String(input));
    if (logical.origin !== FARM_ORIGIN ||
        !/^\/v1\/auth\/(?:login|logout|me|refresh|service-token|services|capabilities|passkeys\/exchange|app-session\/(?:authorize|exchange))$/.test(logical.pathname) ||
        logical.search || logical.hash || logical.username || logical.password)
      throw new Error('Untrusted BFF authority request');
    if (!/^fbff_[A-Za-z0-9_-]{32,}$/.test(credential))
      throw new Error('BFF client credential is unavailable');
    const headers = new Headers(init.headers);
    headers.delete('cookie');
    headers.delete('x-api-key');
    headers.set('X-Farm-Client-Credential', credential);
    return authority(logical.pathname, { ...init, headers });
  };
}
