"""Bounded repository discovery and journalled, source-preserving TODO file writes."""

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .markdown_tasks import MAX_DOCUMENT_BYTES


MAX_DIRECTORIES = 10000
MAX_DEPTH = 8
MAX_STATE_BYTES = 8 * 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_PRUNED = {
    "archive", "archives", "archived", "backup", "backups", "build", "cache", "caches",
    "dist", "env", "logs", "lost+found", "miniconda3", "models", "node_modules",
    "production", "recovery", "recovered", "tmp", "training", "vendor", "venv",
    "__pycache__",
}
_OPAQUE_DIRECTORIES = {
    "assets", "audio", "blender", "downloads", "generated", "media", "output", "outputs",
    "renders", "rollback", "samples", "share", "staging",
}
_PROJECT_FILES = {"Cargo.toml", "CMakeLists.txt", "package.json", "pyproject.toml", "setup.py"}
_SOURCE_NAMESPACES = {"apps", "bots", "code", "extensions", "plugins", "private", "sys", "tools"}


class FileSafetyError(ValueError):
    """A source or sidecar violates the synchronizer's bounded filesystem contract."""


class FileConflict(FileSafetyError):
    """A file changed concurrently or an interrupted write needs human resolution."""


class NeedsInitialization(FileSafetyError):
    """Farm init must prepare the repository before synchronization may write sidecars."""


@dataclass(frozen=True)
class Repository:
    path: str
    name: str
    root: Path
    needs_init: bool
    has_todo: bool


def _pruned(name):
    lower = name.lower()
    return lower.startswith(".") or lower in _PRUNED or any(
        lower.startswith(prefix + separator)
        for prefix in ("archive", "archived", "backup", "recovery", "recovered")
        for separator in ("-", "_", ".")
    )


def _registered_backup_source(root, parent, name):
    """Distinguish a direct backup-service source checkout from protected backup data."""
    if (
        name not in {"backup", "backups"}
        or parent.parent != root
        or parent.name not in _SOURCE_NAMESPACES
    ):
        return False
    repo = parent / name
    try:
        manifest, git = (repo / "farm.json").lstat(), (repo / ".git").lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(manifest.st_mode) and stat.S_ISDIR(git.st_mode)


def _directory_fd(path):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise FileSafetyError("Directory paths must not contain parent traversal")
    fd = os.open(path.anchor, _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _regular(info, maximum):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise FileSafetyError("Sync files must be regular files with no hard links")
    if info.st_size > maximum:
        raise FileSafetyError("Sync file exceeds its size limit")


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_at(directory, name, maximum):
    fd = os.open(name, _FILE_FLAGS, dir_fd=directory)
    try:
        before = os.fstat(fd)
        _regular(before, maximum)
        chunks, size = [], 0
        while True:
            block = os.read(fd, min(65536, maximum + 1 - size))
            if not block:
                break
            chunks.append(block)
            size += len(block)
            if size > maximum:
                raise FileSafetyError("Sync file exceeds its size limit")
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if _signature(before) != _signature(after) or _signature(after) != _signature(current):
            raise FileConflict("Sync file changed while it was being read")
        return b"".join(chunks), after
    finally:
        os.close(fd)


def _json(data):
    try:
        result = json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError, RecursionError):
        raise FileSafetyError("Invalid sync state JSON") from None
    if not isinstance(result, dict):
        raise FileSafetyError("Sync state must be a JSON object")
    return result


def _encoded(value):
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()
    except (TypeError, ValueError, RecursionError):
        raise FileSafetyError("Invalid sync state JSON") from None
    if len(data) + 1 > MAX_STATE_BYTES:
        raise FileSafetyError("Sync state exceeds its size limit")
    return data + b"\n"


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _document(data):
    if not isinstance(data, bytes) or len(data) > MAX_DOCUMENT_BYTES:
        raise FileSafetyError("TODO.md exceeds its 1 MiB size limit")
    try:
        value = data.decode("utf-8")
    except UnicodeError:
        raise FileSafetyError("TODO.md must use UTF-8") from None
    if "\x00" in value:
        raise FileSafetyError("TODO.md must not contain NUL bytes")


def _write_all(fd, data):
    remaining = memoryview(data)
    while remaining:
        count = os.write(fd, remaining)
        if count <= 0:
            raise OSError("Short write while saving sync state")
        remaining = remaining[count:]


def _atomic_write(directory, name, data, mode=0o600, before_replace=None):
    temporary = "." + name + "." + uuid4().hex
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode, dir_fd=directory,
    )
    try:
        _write_all(fd, data)
        os.fchmod(fd, mode)
        os.fsync(fd)
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _regular(info, max(MAX_STATE_BYTES, MAX_DOCUMENT_BYTES))
        if before_replace is not None:
            before_replace()
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def discover_repositories(root=Path("/srv/farm")):
    """Find primary registered checkouts without entering protected or dependency trees."""
    root = Path(root).absolute()
    for protected in (Path("/srv/farm/production"), Path("/srv/farm/tmp"), Path("/srv/farm/.uv")):
        if root == protected or protected in root.parents:
            raise FileSafetyError("Repository discovery root is protected")
    fd = _directory_fd(root)
    os.close(fd)
    result, visited = [], 0

    def fail(error):
        raise FileSafetyError("Repository discovery could not read every source directory") from error

    for directory, directories, files in os.walk(root, topdown=True, followlinks=False, onerror=fail):
        repo = Path(directory)
        visited += 1
        if visited > MAX_DIRECTORIES:
            raise FileSafetyError("Repository discovery exceeds the directory limit")
        directories[:] = sorted(
            name for name in directories
            if not (repo / name).is_symlink()
            and (not _pruned(name) or _registered_backup_source(root, repo, name))
        )
        git_path = repo / ".git"
        if git_path.is_symlink() or git_path.is_file():
            directories[:] = []
            continue
        if git_path.is_dir():
            checked = subprocess.run(
                ["git", "--no-optional-locks", "-C", str(repo), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if checked.returncode == 0:
                directories[:] = []
                if Path(checked.stdout.strip()).absolute() != repo or "farm.json" not in files:
                    continue
                repo_fd = _directory_fd(repo)
                try:
                    data, _ = _read_at(repo_fd, "farm.json", 1024 * 1024)
                    manifest = _json(data)
                    if manifest.get("workflow", "release") not in {"release", "autocommit"}:
                        raise FileSafetyError("Registered repository has an invalid workflow")
                    name = manifest.get("name", repo.name)
                    if not isinstance(name, str) or not name.strip() or len(name) > 200:
                        raise FileSafetyError("Registered repository has an invalid name")
                    farm = repo / ".farm"
                    result.append(Repository(
                        repo.relative_to(root).as_posix(), name, repo,
                        not farm.is_dir() or farm.is_symlink(), "TODO.md" in files,
                    ))
                finally:
                    os.close(repo_fd)
                continue
        if repo.name.lower() in _OPAQUE_DIRECTORIES or _PROJECT_FILES.intersection(files):
            directories[:] = []
            continue
        if len(repo.relative_to(root).parts) >= MAX_DEPTH and directories:
            raise FileSafetyError(
                "Repository discovery exceeds the depth limit at " + repo.relative_to(root).as_posix()
            )
    return sorted(result, key=lambda item: item.path)


class RepositoryFile:
    """Serialize cooperating writers and recover interrupted file/database transactions.

    File replacement rechecks exact bytes and identity immediately before rename.
    Editors that do not honor the sidecar lock can still race that final check;
    durable source snapshots retain recovery evidence without claiming filesystem CAS.
    """

    def __init__(self, repo):
        self.repo = Path(repo).absolute()
        self._fds = []
        self._last_read = None

    def _keep(self, fd):
        self._fds.append(fd)
        return fd

    def __enter__(self):
        try:
            self._repo = self._keep(_directory_fd(self.repo))
            try:
                self._farm = self._keep(os.open(".farm", _DIR_FLAGS, dir_fd=self._repo))
            except FileNotFoundError:
                raise NeedsInitialization("Run farm init before synchronizing this repository") from None
            for name, parent in (("tracker", self._farm),):
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
            self._tracker = self._keep(os.open("tracker", _DIR_FLAGS, dir_fd=self._farm))
            if stat.S_IMODE(os.fstat(self._tracker).st_mode) & 0o077:
                raise FileSafetyError("The .farm/tracker directory must be private (mode 0700)")
            self._lock = self._keep(os.open(
                "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=self._tracker,
            ))
            _regular(os.fstat(self._lock), MAX_STATE_BYTES)
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise FileConflict("Repository synchronization is already running") from None
            try:
                os.mkdir("objects", mode=0o700, dir_fd=self._tracker)
                os.fsync(self._tracker)
            except FileExistsError:
                pass
            self._objects = self._keep(os.open("objects", _DIR_FLAGS, dir_fd=self._tracker))
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        for fd in reversed(self._fds):
            os.close(fd)
        self._fds.clear()

    def _assert_root(self):
        if not self._fds:
            raise FileSafetyError("RepositoryFile must be used as a context manager")
        current = _directory_fd(self.repo)
        try:
            if _signature(os.fstat(current))[:2] != _signature(os.fstat(self._repo))[:2]:
                raise FileConflict("Repository directory changed during synchronization")
            for parent, name, expected in (
                (self._repo, ".farm", self._farm),
                (self._farm, "tracker", self._tracker),
                (self._tracker, "objects", self._objects),
            ):
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode) or (
                    info.st_dev, info.st_ino
                ) != (os.fstat(expected).st_dev, os.fstat(expected).st_ino):
                    raise FileConflict("Repository sync directories changed during synchronization")
            lock = os.stat("lock", dir_fd=self._tracker, follow_symlinks=False)
            if (lock.st_dev, lock.st_ino) != (
                os.fstat(self._lock).st_dev, os.fstat(self._lock).st_ino
            ):
                raise FileConflict("Repository synchronization lock changed")
        finally:
            os.close(current)

    def read(self):
        """Read one stable, bounded UTF-8 source and retain its identity for publication."""
        self._assert_root()
        data, info = _read_at(self._repo, "TODO.md", MAX_DOCUMENT_BYTES)
        _document(data)
        self._last_read = (_digest(data), _signature(info))
        return data

    def _read_json(self, name):
        self._assert_root()
        try:
            data, _ = _read_at(self._tracker, name, MAX_STATE_BYTES)
        except FileNotFoundError:
            return None
        return _json(data)

    def state(self):
        return self._read_json("state.json")

    def pending(self):
        value = self._read_json("pending.json")
        if value is None:
            return None
        if (
            value.get("schema") != 1
            or not isinstance(value.get("token"), str)
            or not _TOKEN.fullmatch(value["token"])
            or not isinstance(value.get("new_state"), dict)
            or not (value.get("old_state") is None or isinstance(value["old_state"], dict))
            or type(value.get("mode")) is not int
            or not 0 <= value["mode"] <= 0o7777
            or any(not isinstance(value.get(key), str) or not _DIGEST.fullmatch(value[key])
                   for key in ("before_hash", "after_hash"))
        ):
            raise FileSafetyError("Invalid pending synchronization journal")
        if value["new_state"].get("snapshot") != value["after_hash"]:
            raise FileSafetyError("Pending synchronization state has the wrong snapshot")
        return value

    def snapshot(self, digest):
        self._assert_root()
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise FileSafetyError("Invalid snapshot digest")
        data, _ = _read_at(self._objects, digest, MAX_DOCUMENT_BYTES)
        if _digest(data) != digest:
            raise FileSafetyError("Stored Markdown snapshot failed its content hash")
        _document(data)
        return data

    def _snapshot(self, data):
        digest = _digest(data)
        try:
            self.snapshot(digest)
        except FileNotFoundError:
            _atomic_write(self._objects, digest, data)
        return digest

    def _replace_todo(self, candidate, expected, mode, expected_signature=None):
        def check():
            self._assert_root()
            data, info = _read_at(self._repo, "TODO.md", MAX_DOCUMENT_BYTES)
            if data != expected or (
                expected_signature is not None and _signature(info) != expected_signature
            ):
                raise FileConflict("TODO.md changed before synchronization could publish")
        if candidate == expected:
            check()
        else:
            _atomic_write(self._repo, "TODO.md", candidate, mode, before_replace=check)

    def save(self, candidate, state, expected, token):
        """Persist recovery intent, then publish Markdown; finish only after the DB commit."""
        self._assert_root()
        _document(expected)
        _document(candidate)
        if not isinstance(state, dict) or not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise FileSafetyError("Invalid synchronization state or transaction token")
        if self.pending() is not None:
            raise FileConflict("Recover the pending synchronization before starting another")
        data, info = _read_at(self._repo, "TODO.md", MAX_DOCUMENT_BYTES)
        if data != expected or (
            self._last_read is not None and self._last_read[0] == _digest(expected)
            and self._last_read[1] != _signature(info)
        ):
            raise FileConflict("TODO.md changed after it was read")
        before_hash, after_hash = self._snapshot(expected), self._snapshot(candidate)
        new_state = {**state, "snapshot": after_hash}
        pending = {
            "schema": 1, "token": token, "before_hash": before_hash, "after_hash": after_hash,
            "old_state": self.state(), "new_state": new_state, "mode": stat.S_IMODE(info.st_mode),
        }
        _atomic_write(self._tracker, "pending.json", _encoded(pending))
        self._replace_todo(candidate, expected, pending["mode"], _signature(info))

    def finish(self, token):
        """Finalize a receipt-confirmed transaction without touching subsequent source edits."""
        pending = self.pending()
        if pending is None or pending["token"] != token:
            raise FileConflict("Pending synchronization token does not match")
        self.snapshot(pending["after_hash"])
        _atomic_write(self._tracker, "state.json", _encoded(pending["new_state"]))
        os.unlink("pending.json", dir_fd=self._tracker)
        os.fsync(self._tracker)

    def recover(self, committed):
        """Consult a database receipt and finish or conditionally undo an interrupted write."""
        pending = self.pending()
        if pending is None:
            return "none"
        if committed(pending["token"]):
            self.finish(pending["token"])
            return "committed"
        original = self.snapshot(pending["before_hash"])
        candidate = self.snapshot(pending["after_hash"])
        current, info = _read_at(self._repo, "TODO.md", MAX_DOCUMENT_BYTES)
        if original != candidate and current not in (original, candidate):
            raise FileConflict("TODO.md changed after an interrupted sync; snapshots are retained")
        if original != candidate and current == candidate:
            self._replace_todo(original, candidate, pending["mode"], _signature(info))
        if pending["old_state"] is None:
            try:
                os.unlink("state.json", dir_fd=self._tracker)
            except FileNotFoundError:
                pass
        else:
            _atomic_write(self._tracker, "state.json", _encoded(pending["old_state"]))
        os.unlink("pending.json", dir_fd=self._tracker)
        os.fsync(self._tracker)
        self._last_read = None
        return "rolled_back"
