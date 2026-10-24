# Tracker

Private task lists managed through Farm MCP and an authenticated web workspace. Tracker stores independent
lists, items, labeled text/JSON details, precise or partial due dates, dependencies,
recurring schedules, and immutable change history in SQLite.

- [Behavior and API](docs/design.md)
- [Installation and activation](docs/deployment.md)

The database defaults to `/data/databases/personal/tracker/tracker.db`. Farm owns
identity and permissions; any authorized agent can manage the shared lists.
Manager episode todos remain separate. There is no reminder notification service.

Prepare the isolated runtime with `sh scripts/runtime.sh sync`; run checks with
`sh scripts/runtime.sh check`. The Farm manifest exposes the same build/check
operations. Tests use temporary databases and injected authentication.

## Web workspace and repository TODOs

Tracker now has an authenticated Farm web workspace at `https://tracker.noiseflood.farm/`.
The Farm list groups root repository TODO.md tasks and synchronizes titles and
checkboxes using private `.farm/tracker` snapshots. Run `farm init` to prepare a
repository; its Markdown remains free of sync metadata. See
[repository sync](docs/repository-sync.md) and [deployment](docs/deployment.md)
for activation, conflict behavior and recovery.
