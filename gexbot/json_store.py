"""Atomic JSON read/write helpers for producer state.

The naive ``path.write_text(json.dumps(...))`` pattern is NOT atomic —
``write()`` on Linux isn't atomic for arbitrarily-sized buffers, so a
process killed mid-write (SIGKILL, OOM, power loss, a pre-commit hook
interrupting a running script) leaves a truncated/empty file. The next
reader hits ``ValueError: Expecting value`` and silently falls back to
``{}`` / ``[]`` via the usual ``except Exception: return default``
pattern, with ``.bak`` recovery artifacts left next to the originals.

This module centralizes the safe pattern:

  1. Write to ``<path>.tmp``.
  2. ``flush()`` + ``fsync()`` to get bytes to disk (not just the page
     cache).
  3. ``os.replace(tmp, path)`` — atomic on POSIX.

Plus a few helpers for the common shapes in this codebase:

  * :func:`atomic_write_json` — full-document write.
  * :func:`read_or_default`   — load + tolerate missing/malformed files.
  * :func:`append_jsonl`      — append a single row, durably.
  * :func:`truncate_jsonl_by_age` — prune old rows by a timestamp field.

All functions never raise on the happy path; disk errors propagate up
since those are genuine environment failures the caller should see.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Full-document atomic write
# ---------------------------------------------------------------------------


def atomic_write_json(
    path: Path | str,
    data: Any,
    *,
    indent: int | None = 2,
    default: Callable | None = str,
    mode: int = 0o644,
) -> None:
    """Atomically write ``data`` as JSON to ``path``.

    Uses the tempfile-in-same-dir + ``os.replace`` pattern so concurrent
    readers see either the previous version or the new version — never a
    torn half-write.

    Args:
        path: Destination file.
        data: JSON-serializable object (or anything the default-fn can
            stringify).
        indent: Passed to ``json.dumps``. ``None`` for compact output.
        default: Fallback stringifier for non-serializable values.
            Defaults to ``str`` to match the existing codebase pattern.
        mode: POSIX file mode for the final file.

    Raises:
        OSError: disk full, permission denied, etc. Let these propagate
            — they're genuine environment failures the caller should
            see, not swallow.
        TypeError: non-serializable payload that even ``default`` can't
            handle. Same principle — caller fix.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # mkstemp gives us a file handle in the same directory as the target
    # so os.replace works without crossing a filesystem boundary.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, default=default)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup of the temp file so we don't leak them on
        # repeated failures.
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


# ---------------------------------------------------------------------------
# Fault-tolerant read
# ---------------------------------------------------------------------------


def read_or_default(path: Path | str, default: Any = None) -> Any:
    """Load JSON at ``path`` or return ``default`` on ANY failure.

    Matches the existing "best-effort read" pattern used throughout the
    codebase. Missing file, unreadable file, malformed JSON — all yield
    ``default``. The caller that wants to know about a malformed file
    should check the path's existence before calling this.

    Returning a shared mutable default (e.g. ``{}`` passed as a literal)
    is a foot-gun. Callers either pass a fresh default per-call or use
    :func:`read_or_default_factory`.
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def read_or_default_factory(
    path: Path | str, factory: Callable[[], Any]
) -> Any:
    """Like :func:`read_or_default` but calls ``factory()`` on failure so
    each caller gets a fresh default instance."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return factory()


# ---------------------------------------------------------------------------
# JSONL — line-oriented append + age-based truncation
# ---------------------------------------------------------------------------


def append_jsonl(path: Path | str, row: dict[str, Any]) -> None:
    """Atomically append a single row to a JSONL file.

    ``open(path, "a")`` + ``write()`` for a newline-terminated JSON row
    under Linux's default glibc is effectively atomic on small writes
    (<= PIPE_BUF == 4096 bytes) because the kernel serializes them
    per-inode. Most JSONL rows here are well under 4KB — an intraday
    gap observation is ~200 bytes, a flow-stack row is ~150 bytes. For
    rows that size, a raw append is safe.

    This helper wraps that pattern with explicit ``flush()`` + ``fsync()``
    so a crash immediately after the write doesn't lose the most-recent
    row to the page cache. That's the difference between "I have the
    last observation" and "I silently lost it" after a power cycle.

    For rows > 4096 bytes (rare — large JSON payloads), this is still
    line-at-a-time safe because we write the whole row in one call; the
    kernel buffers it and flushes to disk on fsync.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, default=str) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def append_alert_with_archive(
    rolling_path: Path | str,
    archive_dir: Path | str,
    alert: dict[str, Any],
    archive_date: str | None = None,
) -> None:
    """Append a strategy alert to BOTH the rolling JSONL and a per-day archive.

    The rolling file (e.g., ``stage2_alerts.jsonl``) is the consumer-facing
    feed and is subject to retention/rotation. The per-day archive at
    ``<archive_dir>/YYYY-MM-DD.jsonl`` is the immutable audit record — it
    is never read by the consumer and never rotated, so even if the rolling
    file gets clobbered by an external process, every fire remains
    recoverable from the archive.

    Why both: 2026-04-20 stage2_alerts.jsonl was found at 0 bytes despite
    the consumer having processed 2 fires (RTX, ADI) — the rolling file
    was truncated by an unknown external process, leaving no main-side
    record of the fires. The per-day archive makes that asymmetry
    impossible to hide.

    ``archive_date`` defaults to today (UTC). Pass an explicit YYYY-MM-DD
    string when backfilling or testing.
    """
    rolling_path = Path(rolling_path)
    archive_dir = Path(archive_dir)
    if archive_date is None:
        archive_date = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    archive_path = archive_dir / f"{archive_date}.jsonl"
    append_jsonl(rolling_path, alert)
    append_jsonl(archive_path, alert)


def atomic_write_jsonl(
    path: Path | str,
    rows: Iterable[dict[str, Any]],
    *,
    mode: int = 0o644,
) -> int:
    """Atomically rewrite ``path`` with a JSONL document of ``rows``.

    Used by the measure_* scripts that rebuild their history files from
    scratch each run (as opposed to :func:`append_jsonl`, which grows a
    file one row at a time). Same tempfile-then-replace pattern as
    :func:`atomic_write_json` so a crash mid-rewrite can't truncate the
    file — readers see either the old version or the new version.

    Returns the number of rows written.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    n = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, default=str) + "\n")
                n += 1
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    return n


def truncate_jsonl_by_age(
    path: Path | str,
    max_age_days: float,
    timestamp_key: str = "ts",
    now: datetime | None = None,
) -> int:
    """Rewrite a JSONL file in-place, keeping only rows newer than
    ``max_age_days``. Returns the number of rows kept.

    Rows without a parseable ``timestamp_key`` are KEPT (fail-safe — a
    malformed timestamp shouldn't delete potentially valuable history).
    Rows whose timestamp is in the future are also kept — clock skew
    is a real thing and we'd rather over-keep than silently drop.

    Rewrite goes through :func:`atomic_write_json`-style tempfile-then-
    replace, so the file is never in a partial state during truncation.
    """
    path = Path(path)
    if not path.is_file():
        return 0
    if now is None:
        now = datetime.now(tz=UTC)
    cutoff = now.timestamp() - max_age_days * 86400

    kept: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                raw = line.rstrip("\n")
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    # Corrupted row: keep it, so a human can investigate.
                    kept.append(raw)
                    continue

                ts_raw = row.get(timestamp_key) if isinstance(row, dict) else None
                if not isinstance(ts_raw, str):
                    # No parseable timestamp — keep.
                    kept.append(raw)
                    continue

                parsed = _parse_iso_to_timestamp(ts_raw)
                if parsed is None:
                    kept.append(raw)
                    continue
                if parsed >= cutoff:
                    kept.append(raw)
    except OSError:
        # Unreadable file — don't silently zero it out. Surface to
        # caller.
        raise

    # Atomic rewrite via tempfile-then-replace, same pattern as
    # atomic_write_json. We don't call that function directly because
    # we're writing pre-serialized strings, not going through json.dumps
    # (which would double-encode).
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for line in kept:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise

    return len(kept)


def _parse_iso_to_timestamp(raw: str) -> float | None:
    """Best-effort ISO-8601 -> epoch seconds. Returns None on failure."""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


# ---------------------------------------------------------------------------
# Convenience iter helpers
# ---------------------------------------------------------------------------


def iter_jsonl(path: Path | str) -> Iterable[dict[str, Any]]:
    """Yield each parseable JSON object in the file. Skips malformed
    rows silently (logged at DEBUG) so a single corrupted line doesn't
    bring down an analysis pass."""
    path = Path(path)
    if not path.is_file():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                logger.debug("iter_jsonl: skipping malformed line in %s", path)
                continue
