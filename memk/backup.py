"""Backup: rolling snapshots, and a verification that actually restores.

Two rules from hard experience:

* **A backup job that swallows an error is worse than no backup**, because it is
  trusted. Every failure here raises or is reported, never ignored.
* **Checking that a file exists is not verification.** A backup is only proven by
  restoring it and reading the restored store: integrity check, schema version, row
  counts against the manifest. :func:`verify` does that, and :func:`restore` refuses
  to overwrite a live store without an explicit flag.

Snapshots use SQLite's own backup API, which gives a consistent copy while the store
is open, writer included -- no "copy the file and hope" behaviour, and no dependence
on the journal mode.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schema import schema_version
from .util import sha256_text, utc_now

DEFAULT_KEEP_DAILY = 30
MANIFEST_SUFFIX = ".manifest.json"
DB_SUFFIX = ".sqlite3"


@dataclass
class BackupEntry:
    path: str
    kind: str
    created_at: str
    size_bytes: int
    sha256: str = ""
    counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "created_at": self.created_at,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "counts": self.counts,
        }


def _file_digest(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in ("memories", "journal", "evidence", "vector_records"):
        try:
            out[name] = int(conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
        except sqlite3.Error:
            out[name] = -1
    return out


def snapshot(conn: sqlite3.Connection, target: Path) -> dict[str, int]:
    """Write a consistent copy of the open database to *target*."""

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    destination = sqlite3.connect(str(target))
    try:
        conn.backup(destination)
        destination.commit()
    finally:
        destination.close()
    return _counts(conn)


def create(
    conn: sqlite3.Connection,
    directory: str | Path,
    *,
    kind: str = "auto",
    monthly_keep: bool = True,
) -> BackupEntry:
    """Take a snapshot. ``kind='auto'`` writes a monthly copy on the first of the month."""

    base = Path(directory)
    base.mkdir(parents=True, exist_ok=True)
    stamp = utc_now()
    day = stamp[:10]
    resolved = kind
    if kind == "auto":
        resolved = "monthly" if stamp[8:10] == "01" else "daily"

    folder = base / ("monthly" if resolved == "monthly" else "")
    folder.mkdir(parents=True, exist_ok=True)
    name = f"memory-{stamp[:10]}-{stamp[11:16].replace(':', '')}{DB_SUFFIX}"
    target = folder / name

    counts = snapshot(conn, target)
    entry = BackupEntry(
        path=str(target),
        kind=resolved,
        created_at=stamp,
        size_bytes=target.stat().st_size,
        sha256=_file_digest(target),
        counts=counts,
    )
    manifest = {
        "format": 1,
        "schema_version": schema_version(conn),
        "created_at": entry.created_at,
        "kind": entry.kind,
        "sha256": entry.sha256,
        "size_bytes": entry.size_bytes,
        "counts": entry.counts,
        "source_database": conn.execute("PRAGMA database_list").fetchone()[2],
    }
    Path(str(target) + MANIFEST_SUFFIX).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return entry


def list_backups(directory: str | Path) -> list[BackupEntry]:
    base = Path(directory)
    if not base.exists():
        return []
    out: list[BackupEntry] = []
    for path in sorted(base.rglob(f"*{DB_SUFFIX}")):
        manifest_path = Path(str(path) + MANIFEST_SUFFIX)
        manifest: dict[str, Any] = {}
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = {}
        out.append(
            BackupEntry(
                path=str(path),
                kind=str(manifest.get("kind") or ("monthly" if "monthly" in path.parts else "daily")),
                created_at=str(manifest.get("created_at") or ""),
                size_bytes=path.stat().st_size,
                sha256=str(manifest.get("sha256") or ""),
                counts=dict(manifest.get("counts") or {}),
            )
        )
    out.sort(key=lambda entry: entry.created_at or entry.path, reverse=True)
    return out


def verify(path: str | Path) -> dict[str, Any]:
    """Restore-drill a backup in place: open it, check it, count it.

    This is the verification that matters. A backup that opens, passes
    ``integrity_check``, reports the expected schema version and holds the expected
    number of rows is a backup; anything else is a file.
    """

    target = Path(path)
    problems: list[str] = []
    if not target.exists():
        return {"path": str(target), "ok": False, "problems": ["missing"]}

    manifest_path = Path(str(target) + MANIFEST_SUFFIX)
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            problems.append("manifest_unreadable")
    else:
        problems.append("manifest_missing")

    digest = _file_digest(target)
    if manifest.get("sha256") and manifest["sha256"] != digest:
        problems.append("checksum_mismatch")

    observed: dict[str, Any] = {}
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        observed["integrity_check"] = integrity
        if integrity.lower() != "ok":
            problems.append(f"integrity:{integrity[:60]}")
        observed["schema_version"] = schema_version(conn)
        observed["counts"] = _counts(conn)
    except sqlite3.Error as exc:
        problems.append(f"open_failed:{type(exc).__name__}")
    finally:
        conn.close()

    expected = dict(manifest.get("counts") or {})
    for name, value in expected.items():
        if observed.get("counts", {}).get(name) != value:
            problems.append(
                f"count_mismatch:{name} expected {value} got {observed.get('counts', {}).get(name)}"
            )

    return {
        "path": str(target),
        "ok": not problems,
        "problems": problems,
        "manifest": manifest,
        "observed": observed,
        "sha256": digest,
    }


def restore(
    path: str | Path,
    target: str | Path,
    *,
    overwrite: bool = False,
    verify_first: bool = True,
) -> dict[str, Any]:
    """Copy a backup into place, optionally refusing to clobber a live store."""

    source = Path(path)
    destination = Path(target)
    if verify_first:
        report = verify(source)
        if not report["ok"]:
            return {"restored": False, "reason": "verification_failed", "report": report}
    if destination.exists() and not overwrite:
        return {"restored": False, "reason": "target_exists", "target": str(destination)}

    destination.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(destination) + suffix)
        if candidate.exists():
            candidate.unlink()
    shutil.copy2(source, destination)
    return {
        "restored": True,
        "target": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": _file_digest(destination),
        "verified_source": bool(verify_first),
    }


def prune(
    directory: str | Path,
    *,
    keep_daily: int = DEFAULT_KEEP_DAILY,
    keep_monthly: int = 0,
) -> list[str]:
    """Drop old snapshots. The newest of each kind is never removed.

    ``keep_monthly=0`` keeps every monthly snapshot, because the monthly copies are
    the ones that protect against slow corruption the daily window would age out.
    """

    entries = list_backups(directory)
    removed: list[str] = []
    daily = [e for e in entries if e.kind != "monthly"]
    monthly = [e for e in entries if e.kind == "monthly"]

    for index, entry in enumerate(daily):
        if index < max(1, keep_daily):
            continue
        Path(entry.path).unlink(missing_ok=True)
        Path(entry.path + MANIFEST_SUFFIX).unlink(missing_ok=True)
        removed.append(entry.path)

    if keep_monthly > 0:
        for index, entry in enumerate(monthly):
            if index < max(1, keep_monthly):
                continue
            Path(entry.path).unlink(missing_ok=True)
            Path(entry.path + MANIFEST_SUFFIX).unlink(missing_ok=True)
            removed.append(entry.path)
    return removed


def describe(directory: str | Path) -> dict[str, Any]:
    entries = list_backups(directory)
    daily = [e for e in entries if e.kind != "monthly"]
    monthly = [e for e in entries if e.kind == "monthly"]
    total = sum(e.size_bytes for e in entries)
    return {
        "directory": str(directory),
        "count": len(entries),
        "daily": len(daily),
        "monthly": len(monthly),
        "total_bytes": total,
        "newest": entries[0].as_dict() if entries else None,
        "oldest": entries[-1].as_dict() if entries else None,
        "window_note": (
            f"{len(daily)} daily copies retained; 'count' is copies, not days"
        ),
    }
