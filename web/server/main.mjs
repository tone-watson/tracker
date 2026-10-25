import fs from 'node:fs';
import { createSessions } from './sessions.mjs';
import { createApp } from './app.mjs';
import { createCentralFetch } from './central-fetch.mjs';
import { authorityOrigin } from './authority-fetch.mjs';

try { authorityOrigin(); }
catch (error) {
  console.error(error.message);
  process.exit(78);
}

let sessions;
try {
  const credential = fs.readFileSync('/srv/farm/private/tracker/secrets/tracker-web.token', 'utf8').trim();
  const key = fs.readFileSync('/srv/farm/private/tracker/secrets/tracker-web-vault.key', 'utf8').trim();
  if (!/^fbff_[A-Za-z0-9_-]{43,128}$/.test(credential)) throw new Error('Invalid client credential');
  sessions = createSessions({
    directory: '/var/lib/farm-tracker-web/sessions', key, central: createCentralFetch({ credential }),
  });
} catch {
  console.error('Tracker web configuration is unavailable. Complete scripts/setup_web.py for the selected Farm user and check private file permissions before restarting farm-tracker-web.service.');
  process.exit(78);
}
const server = createApp(sessions);
sessions.sweep();
const sweep = setInterval(() => sessions.sweep(), 12 * 3600000);
sweep.unref();
server.listen(8793, '127.0.0.1');
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => {
  clearInterval(sweep);
  server.close(() => { sessions.dispose(); process.exit(0); });
  setTimeout(() => process.exit(1), 20000).unref();
});
