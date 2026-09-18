# Restore the SQLite database from a pre-migration backup (spec §7.0.1; runbook
# entry "Restore the database").
#
# Why this exists: every schema migration first copies the live file to
# data/backups/<db>.<from>-to-<to>.<timestamp>.db (src/noc_agents/db/migrate.py).
# Because migrations are additive, the FAST rollback is a code checkout -- older
# code runs fine against the newer file. Reach for this script only when the data
# itself was damaged and you need the file as it was before the migration.
#
# Usage (STOP THE SERVER FIRST -- SQLite cannot swap a file under a live process):
#     C:\Python313\python.exe scripts\restore_db.py --from data\backups\noc_agents.1-to-2.20260916T120000Z.db
#     C:\Python313\python.exe scripts\restore_db.py --from <backup> --to data\demo_safaricom.db
#
# The target defaults to DATABASE_URL when it is a sqlite:/// file URL, otherwise
# to data/noc_agents.db under the project root (the same default as config.py).
#
# What it does, in order:
#   1. checks the backup is a readable SQLite file that passes integrity_check;
#   2. reads the schema_version it carries (no schema_version table == version 1);
#   3. folds the live file's write-ahead log into it and moves the live file aside
#      to <name>.pre-restore.<timestamp>.db, so a mistaken restore is itself reversible;
#   4. copies the backup into place and prints the version.
#
# Exit codes:  0 = restored   1 = refused (bad arguments or unreadable backup)   2 = swap failed

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQLITE_HEADER = b"SQLite format 3\x00"


def default_target() -> Path:
    """Where the running app keeps its database, without importing the package."""
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("sqlite:///./"):
        return (ROOT / url.removeprefix("sqlite:///./")).resolve()
    if url.startswith("sqlite:///") and not url.endswith(":memory:"):
        return Path(url.removeprefix("sqlite:///")).resolve()
    return ROOT / "data" / "noc_agents.db"


def check_backup(path: Path) -> str | None:
    """Return a refusal reason, or None when the file is a healthy SQLite database."""
    if not path.is_file():
        return f"no such file: {path}"
    with path.open("rb") as fh:
        if fh.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            return f"not a SQLite database: {path}"
    con = sqlite3.connect(path)
    try:
        verdict = con.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        return f"backup is unreadable: {exc}"
    finally:
        con.close()
    return None if verdict == "ok" else f"backup fails integrity_check: {verdict}"


def schema_version_of(path: Path) -> int:
    """The version the file carries; a pre-Phase-1 file has no table and is version 1."""
    con = sqlite3.connect(path)
    try:
        has_table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if not has_table:
            return 1
        stored = con.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        return int(stored) if stored is not None else 1
    finally:
        con.close()


def set_aside(live: Path) -> Path | None:
    """Move the current file (and its WAL sidecars) out of the way; return the new name."""
    if not live.exists():
        return None
    try:  # fold the write-ahead log into the main file so the copy we keep is complete
        con = sqlite3.connect(live)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
    except sqlite3.DatabaseError:
        pass  # a damaged file is exactly why we are here; keep whatever is on disk
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    aside = live.with_name(f"{live.stem}.pre-restore.{ts}.db")
    live.rename(aside)
    for suffix in ("-wal", "-shm"):
        sidecar = live.with_name(live.name + suffix)
        if sidecar.exists():
            sidecar.rename(aside.with_name(aside.name + suffix))
    return aside


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy a pre-migration backup back into place.")
    parser.add_argument("--from", dest="backup", required=True, metavar="BACKUP_DB", help="backup file to restore")
    parser.add_argument("--to", dest="target", metavar="LIVE_DB", help="database file to replace (default: the app's)")
    args = parser.parse_args(argv)

    backup = Path(args.backup).resolve()
    target = Path(args.target).resolve() if args.target else default_target()

    reason = check_backup(backup)
    if reason:
        print(f"REFUSED: {reason}")
        return 1
    if backup == target:
        print("REFUSED: --from and --to are the same file")
        return 1
    version = schema_version_of(backup)

    try:
        aside = set_aside(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, target)
    except PermissionError as exc:
        print(f"FAILED: {exc}\nIs the server still running? Stop it and retry.")
        return 2
    except OSError as exc:
        print(f"FAILED: {exc}")
        return 2

    print(f"restored {target}")
    print(f"   from  {backup}")
    if aside:
        print(f"   previous file kept as {aside}")
    print(f"schema_version carried by the restored file: {version}")
    print("Start a code version whose SCHEMA_VERSION is >= this; the next start migrates forward if needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
