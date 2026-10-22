# Install and activate Tracker

Source and isolated tests do not activate the live app. The deployment has three
repositories: API provisioning support, Tracker, and MCP. Release them in that
order using the normal `farm release` workflow from each repository. API changes
are CLI provisioning support only and need no API restart. Tracker initializes
schema version 1 transactionally on its first start; no existing data is migrated.
No backup configuration changes are required.

## Prepare runtime and Farm permissions

In `/srv/farm/private/tracker`, `sh scripts/runtime.sh sync` installs the exact
`uv.lock` into the isolated `.venv`, and `sh scripts/runtime.sh check` runs tests,
lint and compilation. The initial project version is 0.1.0; subsequent versions
and releases belong to the normal Farm workflow.

Follow the API's full [Tracker access runbook](/srv/farm/sys/api/docs/security/tracker-service-access.md)
for the private entitlement credential, service registration preview/apply and
selected-account MCP grant extension. The reviewed service policy is
`/srv/farm/sys/api/docs/security/fixtures/tracker-service-policy.json`. It does not
authorize arbitrary new users. Scope names are `todos.read`/`todos.write` upstream
and `tracker.todos.read`/`tracker.todos.write` in MCP. All authorized callers share
all Tracker lists.

The credential file is `/srv/farm/private/tracker/secrets/tracker-entitlement.token`.
Keep its directory mode 0700 and file mode 0600. It is ignored by Git. The service
fails startup if the file is absent or malformed. It never prints the credential.

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
before reusing it. The service listens only on 127.0.0.1:8791. Nginx follows the
existing Farm Cloudflare Tunnel pattern: public TLS terminates at Cloudflare;
loopback HTTP reaches nginx. Request/task content is not access-logged.

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

Verify `curl --fail http://127.0.0.1:8791/healthz` and the equivalent public health
route. Use the newly authorized agent to create an explicitly temporary list,
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
