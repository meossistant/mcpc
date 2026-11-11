"""On-disk catalog cache: one JSON file per (server name, spec hash) under `paths.cache_dir()`.

Files are written atomically (temp file in the cache dir, then `os.replace`), so concurrent
mcpc processes never observe or leave a truncated snapshot. Snapshots never contain resolved
secrets because the cache key and contents derive from the uninterpolated spec.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from mcpc import paths
from mcpc.catalog.models import ServerSnapshot
from mcpc.config.models import ServerConfig

_HASH_LEN = 12
_REPLACE_ATTEMPTS = 5
_REPLACE_DELAY = 0.05
_TMP_SUFFIX = ".tmp"


def snapshot_path(cfg: ServerConfig) -> Path:
    """Cache file for this server config: `<name>-<spec_hash[:12]>.json`."""
    return paths.cache_dir() / f"{cfg.name}-{cfg.spec_hash()[:_HASH_LEN]}.json"


def load(cfg: ServerConfig) -> ServerSnapshot | None:
    """Return the cached snapshot, or None if missing, unreadable, invalid or for a different spec."""
    try:
        raw = snapshot_path(cfg).read_text(encoding="utf-8")
        snap = ServerSnapshot.model_validate_json(raw)
    except (OSError, ValueError, ValidationError):
        return None
    if snap.spec_hash != cfg.spec_hash():
        return None
    return snap


def save(snap: ServerSnapshot, cfg: ServerConfig) -> Path:
    """Atomically write the snapshot and remove cache files for older specs of the same server."""
    target = snapshot_path(cfg)
    data = snap.model_dump_json(by_alias=True, indent=None)
    _atomic_write(target, data)
    _remove_stale_files(cfg.name, keep=target)
    return target


def record_error(cfg: ServerConfig, message: str) -> ServerSnapshot:
    """Store `message` as `last_error`, keeping the last good data (and its age) if there is any."""
    existing = load(cfg)
    if existing is None:
        snap = ServerSnapshot(server=cfg.name, spec_hash=cfg.spec_hash(), last_error=message)
    else:
        snap = existing.model_copy(update={"last_error": message})
    save(snap, cfg)
    return snap


def is_fresh(snap: ServerSnapshot | None, ttl_hours: float, now: datetime | None = None) -> bool:
    """True if the snapshot exists, its last refresh succeeded, and it is younger than the TTL."""
    if snap is None or snap.last_error is not None:
        return False
    return snap.age_hours(now or datetime.now(UTC)) < ttl_hours


def clear(name: str | None = None) -> int:
    """Delete cached snapshots for one server (or all when `name` is None). Returns the count removed."""
    removed = 0
    for path in _cache_files(name):
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
    return removed


def _cache_files(name: str | None) -> list[Path]:
    """Snapshot files in the cache dir, optionally only those belonging to server `name`."""
    pattern = _file_pattern(name)
    try:
        entries = list(paths.cache_dir().iterdir())
    except OSError:
        return []
    return [p for p in entries if pattern.match(p.name) and p.is_file()]


def _file_pattern(name: str | None) -> re.Pattern[str]:
    # Match the exact `<name>-<12 hex>.json` shape so server "a" never claims files of server "a-b".
    prefix = re.escape(name) if name is not None else r".+"
    return re.compile(rf"^{prefix}-[0-9a-f]{{{_HASH_LEN}}}\.json$")


def _remove_stale_files(name: str, keep: Path) -> None:
    for path in _cache_files(name):
        if path != keep:
            with contextlib.suppress(OSError):
                path.unlink()


def _atomic_write(target: Path, data: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.stem}-", suffix=_TMP_SUFFIX)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        _replace_with_retry(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _replace_with_retry(src: Path, dst: Path) -> None:
    """`os.replace`, retrying on Windows PermissionError when another process holds the target open."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY)
