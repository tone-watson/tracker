# Tracker

Private task lists managed by agents through Farm MCP. Tracker stores independent
lists, items, labeled text/JSON details, precise or partial due dates, dependencies,
recurring schedules, and immutable change history in SQLite.

- [Behavior and API](docs/design.md)
- [Installation and activation](docs/deployment.md)

The database defaults to `/data/databases/personal/tracker/tracker.db`. Farm owns
identity and permissions; any authorized agent can manage the shared lists.
Manager episode todos remain separate. There is no user interface or reminder
notification service.

Prepare the isolated runtime with `sh scripts/runtime.sh sync`; run checks with
`sh scripts/runtime.sh check`. The Farm manifest exposes the same build/check
operations. Tests use temporary databases and injected authentication.
