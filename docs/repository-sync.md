# Repository tasks and local Farm state

Tracker's managed Farm list groups tasks by registered repository. A repository
is a primary Git checkout with a root `farm.json` and root `TODO.md`. Discovery
prunes production, backups, archives, dependencies and media/generated trees;
linked worktrees and symlinked repositories are excluded to avoid duplicate or
redirected writes. It does not search arbitrary Markdown files.

Run `farm init` inside each participating repository. The command locates the
Git root, creates missing `farm.json` and `.farm/`, and adds `.farm/` to the root
`.gitignore`, creating that file if needed. Existing manifests and local state
are preserved. No Git repository, branch, commit, release or TODO file is created
by initialization. `.gitignore` is a normal source change for that repository's
usual workflow; `.farm/` is local operational state.

`python -m tracker.sync --initialize` runs that command in each discovered
repository that already has TODO.md, then performs the first import. Ordinary
sync never edits root `.gitignore`; uninitialized repositories need attention.
This command is an explicit operator action and is not run automatically at
startup. Imported tasks can include completed tasks and titles up to 4,000
characters. Their timestamps record when Tracker observed them; Markdown cannot
supply the original creation/completion time.

## What is synchronized

The checkbox line's title and completion state synchronize in both directions.
Headings appear as source context. Notes, formatting, indentation, line endings,
code fences and non-task Markdown are retained, not re-rendered. Task notes,
details, due dates, dependencies and richer status remain Tracker data.

An unchecked box retains an unchanged in-progress or blocked Tracker status.
Changing the box to checked requests completion and still respects unresolved
Tracker blockers. Clearing a previously checked box reopens the task. Cancelling
in Tracker removes the checkbox token and retains an ordinary bullet, its notes
and nested children. Reopening appends a new checkbox row; the archived ordinary
bullet remains as prose. Removing a task in Markdown cancels its active Tracker
record while retaining history; a removed completed task stays completed.
A missing/unreadable entire TODO file is an error, never a request to cancel all
its tasks.
Removing a repository's Farm registration pauses its synchronization and retains
its Tracker records; registration is checked again before each scan's writes.

Farm tasks require a repository when created, cannot move into ordinary lists,
and cannot recur automatically. The Farm list can be renamed but cannot be
cancelled. Ordinary lists retain their previous behavior.

## Three-way reconciliation

Every sync compares the previous synchronized snapshot, current Markdown, and
current Tracker values. Independent field edits merge. If both sides change the
same field differently, Tracker wins. The overwritten Markdown is retained in a
content-addressed recovery snapshot.

Task identity is stored outside Markdown. Unique unchanged titles, unchanged
source ranges and unambiguous single-row edits preserve identity. Some duplicate
titles or simultaneous renames/reordering are intrinsically ambiguous without
inline IDs. Such a repository reports `attention` and is left unchanged; its
unmatched rows are not silently duplicated or cancelled. Restore the last synced
ordering or make the correspondence unambiguous before retrying.

Each participating repository has:

```text
.farm/tracker/
  lock
  state.json
  pending.json       # only while a write is pending/recovering
  objects/<sha256>   # exact Markdown snapshots
```

The mapping includes stable Tracker item IDs, previous source lines, title,
completion, status and revision. A private journal is durably saved before file
replacement. The database transaction records a matching receipt before the
baseline advances. After interruption, the next sync either finalizes a committed
write or conditionally restores an uncommitted file. If an external editor has
changed the file during recovery, both snapshots are retained and the worker
stops rather than overwriting that edit.

A lock serializes sync workers. File identity and contents are checked again
immediately before atomic replacement. Editors that ignore this lock can still
write in the narrow final check/replace window; ordinary filesystems do not offer
an atomic compare-and-swap across unrelated editors. Snapshots provide recovery,
not a guarantee that every intermediate external edit was observed. Four-hour
polling sees the state at each poll.

## Scheduling and permissions

`farm-tracker-sync.timer` checks every four hours with a small scheduling jitter
and catches a missed run after downtime. `farm-tracker-sync.path` wakes the same
short-lived worker when Tracker writes its durable `sync.request` notification.
There is no permanent recursive filesystem watcher. The worker rereads the files
before applying queued Tracker edits, so it sees edits made since the last poll.

The web/API process retains its database-only write sandbox. The separate local
sync worker is granted write access to `/srv/farm` and Tracker's database directory;
its code limits writes to discovered root TODO files and `.farm/tracker` state,
rejects links/non-regular files, and has no network access. One-shot runs are
serialized by systemd and repository locks. A failed notification is retried by
the periodic timer. Failed repositories show their last error and are retried on
the next scan; other repositories can still synchronize.

The UI shows pending changes, last sync and repositories requiring attention.
MCP exposes repository listing and an explicit sync request, plus a repository
filter and repository assignment on task creation. Existing `todos.read` and
`todos.write` permissions apply. Reads never trigger synchronization.

Do not delete `.farm/tracker` while retaining linked Tracker records. Restore its
mapping and snapshots together. If the database is restored, retain the sidecar
journals/receipts for reconciliation; mismatched mappings are reported rather
than guessed. No backup schedule has been changed by this implementation.
