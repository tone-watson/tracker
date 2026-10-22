# Tracker

Tracker owns private task lists, items, details, history and recurrence. The live
SQLite database is `/data/databases/personal/tracker/tracker.db`. Farm API owns
identity and grants; `/srv/farm/sys/mcp` exposes the tools and delegates scoped
Farm service credentials. Manager episode todos are independent.

- Follow root `farm.json` release workflow. Work and commit on `develop`; releases,
  tags, pushes, privileged installation and production restarts belong to the user.
- Python 3.11+, isolated uv environment. `sh scripts/runtime.sh sync`, then
  `sh scripts/runtime.sh check`. Do not install into other services' environments.
- Changes must preserve append-only history, transaction-safe revisions,
  idempotent creates and all missed valid calendar occurrences.
- Auth requires Farm Tracker audience tokens and live session entitlement.
  There is no production authentication bypass, shared user session or per-list owner.
- Do not open the production database for tests. Use pytest temporary databases.
- Keep docs in `docs/`, credentials in ignored `secrets/`, and never log task
  contents or credentials. Details support text and JSON; no arbitrary file paths.
