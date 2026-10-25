# Install and activate Tracker

## Local Farm authorization transport

Tracker's API and browser backend send Farm authorization requests through the
existing local nginx route by default: `http://127.0.0.1`, with the fixed HTTP
Host `noiseflood.farm`. This covers API JWKS and live entitlement checks, browser
login/session/service-token requests, and browser JWKS. Public JWT issuer
`https://noiseflood.farm`, audience `urn:farm:service:tracker`, browser URLs,
credentials and permission checks are unchanged. Every authenticated API request
still checks the current Farm session and entitlement; local transport does not
cache or bypass that decision.

`TRACKER_FARM_TRANSPORT_ORIGIN` overrides the physical destination for both
processes. It accepts an HTTP(S) origin on literal `127.0.0.1` or `::1`, or the
canonical `https://noiseflood.farm` origin for an explicit public-route rollback.
Paths, query strings, fragments, userinfo, DNS aliases and other hosts are rejected.
The configured destination never comes from a request or token. Redirects are
rejected, environment HTTP proxies are ignored, and local failure does not retry
over the public route. Response limits and request timeouts remain enforced.

No dependency, schema, nginx or systemd unit changes are needed for this upgrade.
Run the normal Tracker release, then restart both processes:

```bash
cd /srv/farm/private/tracker
farm release patch
sudo systemctl restart farm-tracker.service farm-tracker-web.service
```

MCP has the matching local transport change in its own release. Release and
restart Tracker first, then release MCP (which builds its server) and run
`sudo systemctl restart farm-mcp.service`. Either version remains protocol
compatible during this sequence. There is no new OAuth grant, credential
provisioning, forced sign-in or tool-catalog refresh.

For an explicit public-route rollback, set
`Environment=TRACKER_FARM_TRANSPORT_ORIGIN=https://noiseflood.farm` in a systemd
service override for **both** `farm-tracker.service` and
`farm-tracker-web.service`, run `sudo systemctl daemon-reload`, then restart both
services. Removing those overrides restores the local default after the same
reload/restart sequence. No override is needed for ordinary activation.

Isolated tests use temporary HTTP servers and signed fixture tokens to check the
actual Host header, credential separation, public issuer/audience, rejected
redirects and revoked sessions. Existing local nginx routes were also checked
without credentials. These checks do not establish live browser login or a
delegated production tool call after the operator restarts the services.

## Upgrade to the web workspace and repository sync

This change modifies Tracker, Farm CLI and Farm MCP. Release CLI first (for
`farm init`), Tracker second (new API/schema), and MCP last (new tool metadata).
No API service change or restart is required. Install Tracker's locked Python
and Node dependencies before running its release checks:

```bash
cd /srv/farm/private/tracker
sh scripts/runtime.sh sync
sh scripts/runtime.sh check
```

The browser uses the installed Node runtime
`/home/gradywoodruff/.nvm/versions/node/v22.23.4/bin/node`. The release checks run
Python tests, Ruff, compilation, Node auth/proxy tests and frontend syntax checks.
Optional browser acceptance is `npm run test:browser` with Playwright Chromium
installed; it uses only fixture routes and never logs in to the live service.

After the normal releases, provision the separate Tracker browser client for the
existing selected Farm account. Preview first; apply adds only the browser client
and that account's grant, preserving Tracker service and MCP policy. Credentials
are reused on later runs and never printed:

```bash
cd /srv/farm/private/tracker
.venv/bin/python scripts/setup_web.py --user-id 1ea80ff0-605d-4c1c-a4c9-5e76442d3c31
.venv/bin/python scripts/setup_web.py --user-id 1ea80ff0-605d-4c1c-a4c9-5e76442d3c31 --apply
```

The account must already have Tracker read/write entitlement. Browser credentials
are in ignored `secrets/tracker-web.token` and `secrets/tracker-web-vault.key`;
encrypted browser sessions live in `/var/lib/farm-tracker-web/sessions`, created
by systemd. The initial UI uses Farm email/password sign-in; passkeys are not
registered for this new client.

Prepare all discovered repositories with a root TODO file and import them:

```bash
.venv/bin/python -m tracker.sync --initialize
```

This runs `farm init` inside each qualifying repository, preserving its manifest
and ensuring `.farm/` is in the account's global Git excludes file. It then creates
the Farm list and imports existing tasks. It **does write** the live Tracker database and
per-repository sync state; source checkboxes are preserved on the initial import.
It has not been run by implementation tests. Existing repository ignore rules are
preserved. To initialize just one repository, run `farm init` there, then request
sync. See [sync semantics and recovery](repository-sync.md).

Install the new browser and sync services and replace the nginx route:

```bash
sudo install -m 0644 /srv/farm/private/tracker/deploy/farm-tracker-web.service /etc/systemd/system/farm-tracker-web.service
sudo install -m 0644 /srv/farm/private/tracker/deploy/farm-tracker-sync.service /etc/systemd/system/farm-tracker-sync.service
sudo install -m 0644 /srv/farm/private/tracker/deploy/farm-tracker-sync.timer /etc/systemd/system/farm-tracker-sync.timer
sudo install -m 0644 /srv/farm/private/tracker/deploy/farm-tracker-sync.path /etc/systemd/system/farm-tracker-sync.path
sudo install -m 0644 /srv/farm/private/tracker/deploy/tracker.noiseflood.farm /etc/nginx/sites-available/tracker.noiseflood.farm
sudo systemctl daemon-reload
sudo systemctl restart farm-tracker.service
sudo systemctl enable --now farm-tracker-web.service farm-tracker-sync.timer farm-tracker-sync.path
sudo nginx -t
sudo systemctl reload nginx.service
sudo systemctl restart farm-mcp.service
```

The API automatically upgrades SQLite schema 1 to 2 transactionally, preserving
existing tasks, history and receipts. The same migration runs if initial sync
is invoked first. A previously running API process can remain up while initial
import is prepared; restart it before enabling web/MCP calls to the new routes.
No backup configuration is changed.

Tracker API remains on loopback port 8792, browser backend uses 8793, and existing
public DNS/tunnel configuration stays the same. Raw `/api/` stays on the API;
frontend assets, `/auth/` and `/browser/api/` use the browser backend. No further
Cloudflare tunnel restart is needed.

Open `https://tracker.noiseflood.farm/` and sign in. Confirm ordinary lists and
Farm repository groups, edit a disposable source task, and verify both its
Markdown checkbox and Tracker history. The sync status should settle to synced.
The two new MCP tools need tool-catalog refresh; existing Tracker OAuth scopes
are unchanged. A health response or isolated browser fixture does not establish
live Farm browser login or successful production file synchronization.

For future releases, restart `farm-tracker.service` for Python changes and
`farm-tracker-web.service` for browser-server changes. Static assets are served
from disk; refresh the browser after frontend changes. Sync is a one-shot worker
and uses new source on its next run. Reinstall changed unit/config files and run
`sudo systemctl daemon-reload` before restarting their services.

## Original service installation

Source and isolated tests do not activate the live app. The deployment has three
repositories: API provisioning support, Tracker, and MCP. Release them in that
order using the normal `farm release` workflow from each repository. API changes
are CLI provisioning support only and need no API restart. Tracker initializes schema version 2 on first start and upgrades existing version 1
databases transactionally, preserving records and history.
No backup configuration changes are required.

## Prepare runtime and Farm permissions

In `/srv/farm/private/tracker`, `sh scripts/runtime.sh sync` installs the exact
`uv.lock` into the isolated `.venv`, and `sh scripts/runtime.sh check` runs tests,
lint and compilation. Versions and releases belong to the normal Farm workflow.

Before starting the service, run these from `/srv/farm/private/tracker`:

```bash
.venv/bin/python -m tracker.setup_auth
.venv/bin/python -m tracker.setup_auth --apply
```

The first command previews the missing prerequisites without changing files or
Farm policy. The second creates a private credential only when none exists and
registers Tracker through Farm's existing provisioning helper. Repeated runs
reuse the same credential; conflicting existing registration is rejected.
Neither command changes user, client or agent grants. Credentials are never printed.

Then follow the API's [Tracker access runbook](/srv/farm/sys/api/docs/security/tracker-service-access.md)
for the selected-account MCP grant extension. The reviewed service policy is
`/srv/farm/sys/api/docs/security/fixtures/tracker-service-policy.json`. It does not
authorize arbitrary new users. Scope names are `todos.read`/`todos.write` upstream
and `tracker.todos.read`/`tracker.todos.write` in MCP. All authorized callers share
all Tracker lists.

The credential file is `/srv/farm/private/tracker/secrets/tracker-entitlement.token`.
Keep its directory mode 0700 and file mode 0600. It is ignored by Git. The service
fails startup with an actionable message and exit status 78 if the file is absent,
unreadable or malformed. The unit does not retry configuration failures. Fix the
prerequisite and explicitly restart. It never prints the credential.

## Install the service and HTTP route

Run as the normal Farm operator after the release, runtime sync and registration.
These are the exact privileged installation commands; they have not been run by
the implementation task:

```bash
sudo install -d -o gradywoodruff -g gradywoodruff -m 0700 /data/databases/personal/tracker
sudo install -m 0644 /srv/farm/private/tracker/deploy/farm-tracker.service /etc/systemd/system/farm-tracker.service
sudo install -m 0644 /srv/farm/private/tracker/deploy/tracker.noiseflood.farm /etc/nginx/sites-available/tracker.noiseflood.farm
sudo ln -s /etc/nginx/sites-available/tracker.noiseflood.farm /etc/nginx/sites-enabled/tracker.noiseflood.farm
sudo systemctl daemon-reload
sudo systemctl enable --now farm-tracker.service
sudo nginx -t
sudo systemctl reload nginx.service
```

The symlink command is first-install only; if that path exists, inspect its target
before reusing it. The service listens only on 127.0.0.1:8792. Port 8791 belongs to the existing DP
service and must not be reused for Tracker. Nginx follows the
existing Farm Cloudflare Tunnel pattern: public TLS terminates at Cloudflare;
loopback HTTP reaches nginx. Request/task content is not access-logged.

## Repair an installation made with the original 8791 configuration

If Tracker was enabled before its credential existed, stop the repeated failures
before setup. After releasing the source fix, run:

```bash
sudo systemctl stop farm-tracker.service
cd /srv/farm/private/tracker
.venv/bin/python -m tracker.setup_auth
.venv/bin/python -m tracker.setup_auth --apply
sudo install -m 0644 deploy/farm-tracker.service /etc/systemd/system/farm-tracker.service
sudo install -m 0644 deploy/tracker.noiseflood.farm /etc/nginx/sites-available/tracker.noiseflood.farm
sudo systemctl daemon-reload
sudo systemctl restart farm-tracker.service
sudo nginx -t
sudo systemctl reload nginx.service
.venv/bin/python -m tracker.health
```

The existing nginx symlink can stay. These steps repair startup and port routing;
they do not add the tunnel hostname, DNS, Farm MCP user grants or external client
ceilings. Complete those steps below before expecting agent tools to work. A
successful `cloudflared ... ingress validate` does not prove the Tracker ingress
entry exists. Do not restart or change DP.

## Add the public hostname to the existing tunnel

The installed tunnel is `97709653-ba23-4bba-b83b-fa028bb8a3a2`, managed by
`cloudflared.service` using `/etc/cloudflared/config.yml`. Add this ingress entry
before its final `http_status:404` entry, preserving every existing route:

```yaml
  - hostname: tracker.noiseflood.farm
    service: http://localhost:80
```

Edit with `sudoedit /etc/cloudflared/config.yml`, then validate and restart:

```bash
sudo cloudflared --config /etc/cloudflared/config.yml tunnel ingress validate
sudo systemctl restart cloudflared.service
```

Create the corresponding public-hostname DNS route in the existing Cloudflare
account. If the operator's cloudflared login has DNS-management authority, the
exact command for the verified tunnel is:

```bash
cloudflared tunnel route dns 97709653-ba23-4bba-b83b-fa028bb8a3a2 tracker.noiseflood.farm
```

The hostname/DNS operation is not performed by this repository. A working local
health response does not prove the public route or OAuth delegation is active.

## Activate MCP

After MCP's release/build, use its [Tracker activation guide](/srv/farm/sys/mcp/docs/tracker.md)
to preview and apply the selected external OAuth-client scope additions. Preserve
all existing scopes. The Farm BFF/user grant extension above and these external
client ceilings are both necessary. Then run:

```bash
sudo systemctl restart farm-mcp.service
```

Reconnect the intended agent, sign in through Farm and approve Tracker read/write
access. Refreshing old tokens cannot expand their bound Farm session scopes.
New tools may require refreshing the agent connection's tool catalog.

## Acceptance

Verify `.venv/bin/python -m tracker.health` (also the manifest's health command)
and the equivalent public health route. Require `data.contract: farm.tracker/1`
and `data.status: ok`; HTTP 200 alone can come from another service. Use the newly authorized agent to create an explicitly temporary list,
create an overdue item with phone/JSON details, verify due ordering, complete and
reopen it, read its history, create/skip/cancel a recurring series and finally
cancel the temporary list. Never use real personal tasks as a destructive test.

Verify service logs and status with:

```bash
systemctl status farm-tracker.service --no-pager
journalctl -u farm-tracker.service -n 50 --no-pager
```

The background worker catches up persisted calendar occurrences after startup and
checks every ten seconds when idle. A failed catch-up transaction is retried without
advancing its cursor. Keep one managed Tracker process. The recurrence cache is
bounded and disposable; SQLite is the authoritative cursor and history.

For later code releases, sync the locked runtime, run checks, release, then
`sudo systemctl restart farm-tracker.service`. Unit changes additionally require
reinstallation and `sudo systemctl daemon-reload` before restart. Nginx and tunnel
changes are first-install only unless their configuration changes.

The cross-repository contract check uses the actual Flask app over a test-only
stdio bridge and temporary databases. From MCP, run its documented
`test:tracker-contract` command with `TRACKER_CONTRACT_ROOT=/srv/farm/private/tracker`.
It proves local tool/backend compatibility, not a live OAuth grant or deployed
agent connection.
