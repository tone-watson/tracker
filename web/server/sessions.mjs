import { createAuthClient } from '@noiseflood/auth';
import { createBffVault } from './vault.mjs';
import { createCentralFetch } from './central-fetch.mjs';
import { createVerifier, HttpError } from './service-token.mjs';

export const COOKIE = '__Host-tracker_session';
export const LIFETIME = 30 * 86400000;
const expired = () => new HttpError(401, 'invalid_token', 'Sign in to use Tracker.');

export function createSessions({ directory, key, central = createCentralFetch(), verify = createVerifier() }) {
  const vault = createBffVault(directory, key), runtime = new Map();
  function record(id) {
    if (typeof id !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(id)) throw expired();
    const data = vault.load(id);
    if (!data) throw expired();
    return data;
  }
  function entry(id) {
    record(id);
    if (runtime.has(id)) return runtime.get(id);
    for (const [other, item] of runtime) {
      if (!item.pending.size && !item.busy && Date.now() - item.used > 600000) {
        item.auth.dispose(); runtime.delete(other);
      }
    }
    if (runtime.size >= 1000) throw new HttpError(503, 'busy', 'Please try again shortly.');
    const auth = createAuthClient({
      baseURL: 'https://noiseflood.farm/v1', mode: 'bearer', clientId: 'tracker-web', fetch: central,
      tokenStore: {
        supportsSessionJournal: true,
        load: async () => record(id).tokens,
        save: async tokens => vault.save(id, { ...record(id), tokens }),
        clear: async () => vault.save(id, { ...record(id), tokens: null }),
      },
    });
    const item = { auth, cache: new Map(), pending: new Map(), closed: false, busy: false, used: Date.now() };
    auth.subscribe(() => item.cache.clear());
    runtime.set(id, item);
    return item;
  }
  function remove(id) {
    runtime.get(id)?.auth.dispose(); runtime.delete(id); vault.remove(id);
  }
  async function authorize(id, scope = 'todos.read') {
    if (!['todos.read', 'todos.write'].includes(scope)) throw expired();
    const data = record(id), item = entry(id);
    item.used = Date.now();
    if (item.closed || data.createdAt + LIFETIME <= Date.now() ||
        (data.tokens?.refresh_pending && !item.pending.size) ||
        data.tokens?.app_session?.client_id !== 'tracker-web') throw expired();
    const cached = item.cache.get(scope);
    if (cached && cached.until > Date.now()) return { user: data.user, token: cached.token };
    if (!item.pending.has(scope)) {
      const pending = (async () => {
        const reply = await item.auth.request('auth/service-token', {
          method: 'POST', body: { service_id: 'tracker', scopes: [scope] },
        });
        const issued = reply?.data;
        if (issued?.contract !== 'farm.service-access/1' || issued.service_id !== 'tracker' ||
            issued.audience !== 'urn:farm:service:tracker' || issued.token_type !== 'Bearer' ||
            issued.scope !== scope || !Number.isInteger(issued.expires_in) ||
            issued.expires_in < 1 || issued.expires_in > 300)
          throw new HttpError(502, 'invalid_response', 'Invalid Farm response.');
        const claims = await verify(issued.access_token, scope), current = record(id);
        if (item.closed || current.user?.id !== claims.sub ||
            current.tokens?.app_session?.id !== claims.sid) throw expired();
        item.cache.set(scope, {
          until: Math.min(Date.now() + issued.expires_in * 1000, claims.exp * 1000) - 30000,
          token: issued.access_token,
        });
        return { user: current.user, token: issued.access_token };
      })().finally(() => item.pending.delete(scope));
      item.pending.set(scope, pending);
    }
    return item.pending.get(scope);
  }
  return {
    async login(email, password, previous) {
      if (previous) {
        try {
          if (record(previous).tokens) throw new HttpError(409, 'sign_out_first', 'Sign out before changing accounts.');
        } catch (error) { if (error.status !== 401) throw error; }
      }
      const id = vault.create(), item = entry(id);
      item.busy = true;
      try {
        await item.auth.login(email, password);
        const user = item.auth.getState().user, saved = record(id);
        if (!(user?.id ?? user?.user_id) || saved.tokens?.app_session?.client_id !== 'tracker-web')
          throw new HttpError(502, 'invalid_response', 'Farm did not confirm the Tracker session.');
        vault.save(id, { ...saved, user: { id: String(user.id ?? user.user_id), email: String(user.email) } });
        return id;
      } catch (error) {
        if (record(id).tokens) error.sessionId = id;
        else remove(id);
        throw error;
      } finally { item.busy = false; }
    },
    authorize,
    invalidate(id) { runtime.get(id)?.cache.clear(); },
    async logout(id) {
      let item;
      try { item = entry(id); } catch (error) { if (error.status === 401) return; throw error; }
      if (item.closed) throw new HttpError(409, 'busy', 'Sign-out is already in progress.');
      item.closed = true; item.cache.clear(); item.busy = true;
      try {
        await Promise.allSettled([...item.pending.values()]);
        await item.auth.logout();
        remove(id);
      } finally { item.closed = false; item.busy = false; }
    },
    sweep() { return vault.sweep({ maxAge: LIFETIME, excludeIds: [...runtime.keys()] }); },
    dispose() { for (const item of runtime.values()) item.auth.dispose(); runtime.clear(); },
  };
}
