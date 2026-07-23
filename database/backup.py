"""SQLite backup and restore for Discord Traders (Milestone 2D.3).

Backup creation (create_backup()) is safe to call from the live Streamlit
application: it uses sqlite3.Connection.backup(), which produces a
consistent, standalone copy of the source database regardless of whether
its journal mode is the default rollback journal or WAL, and regardless of
whether a connection is currently open on the source.

Restore (restore_backup()) is CLI-only and is never called from
app/streamlit_app.py. It requires the Streamlit application and every
other user of the production database to be fully stopped first - see
main()'s usage message. Restore is invoked as:

    python -m database.backup restore <backup_file_path>
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.logging_config import configure_console_logging, log_operation_failure
from database.config import resolve_database_path

logger = logging.getLogger("discord_traders.backup")

_FILENAME_PREFIX = "discord_traders_"
_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S_%f"
_TEMP_SUFFIX = ".tmp"
_MAX_RETAINED_BACKUPS = 10
_SAFETY_SUBDIR = "prerestore_safety"
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

_SQLITE_HEADER = b"SQLite format 3\x00"

# The frozen schema (database/schema.sql) is the source of truth; this is a
# presence check only, never a migration - restore rejects anything that
# doesn't already match it exactly.
_REQUIRED_TABLES: dict[str, set[str]] = {
    "sources": {"id", "name"},
    "traders": {"id", "source_id", "name", "external_trader_id", "created_at"},
    "raw_messages": {
        "id",
        "source_id",
        "external_id",
        "raw_text",
        "content_hash",
        "metadata",
        "received_at",
        "ingested_at",
    },
    "trade_signals": {
        "id",
        "raw_message_id",
        "trader_id",
        "symbol",
        "action",
        "option_type",
        "price",
        "expiration",
        "position_size",
        "created_at",
        "updated_at",
    },
    "trade_signal_edits": {
        "id",
        "trade_signal_id",
        "previous_values",
        "edited_at",
    },
}


class BackupIntegrityError(Exception):
    """Raised when a backup or restore candidate fails PRAGMA integrity_check."""


class SchemaCompatibilityError(Exception):
    """Raised when a restore candidate is missing required frozen-schema tables/columns."""


class SidecarQuarantineError(Exception):
    """Raised when an existing SQLite sidecar file cannot be safely quarantined."""


class ExclusiveAccessError(Exception):
    """Raised when exclusive access to the production database cannot be confirmed."""


def resolve_backup_dir() -> str:
    """Resolve the runtime backup directory from the environment.

    Precedence, mirroring database.config.resolve_database_path():
        1. DISCORD_TRADERS_BACKUP_PATH, if non-empty after stripping
           whitespace, normalized via Path(value).expanduser().resolve().
        2. Path(LOCALAPPDATA) / "DiscordTraders" / "backups", if
           LOCALAPPDATA is non-empty after stripping whitespace.
        3. Path.home() / "AppData" / "Local" / "DiscordTraders" / "backups",
           used only when LOCALAPPDATA is missing or empty/whitespace-only.

    Returns:
        The resolved backup directory path as a string.
    """
    override = os.environ.get("DISCORD_TRADERS_BACKUP_PATH", "").strip()
    if override:
        return str(Path(override).expanduser().resolve())

    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        return str(Path(local_appdata) / "DiscordTraders" / "backups")

    return str(Path.home() / "AppData" / "Local" / "DiscordTraders" / "backups")


def _generate_backup_filename(reference_time: datetime | None = None) -> str:
    moment = reference_time or datetime.now(timezone.utc)
    return f"{_FILENAME_PREFIX}{moment.strftime(_TIMESTAMP_FORMAT)}.db"


def _parse_backup_timestamp(filename: str) -> datetime | None:
    if not filename.startswith(_FILENAME_PREFIX) or not filename.endswith(".db"):
        return None
    stem = filename[len(_FILENAME_PREFIX) : -len(".db")]
    try:
        return datetime.strptime(stem, _TIMESTAMP_FORMAT)
    except ValueError:
        return None


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _verify_integrity(db_path: Path) -> None:
    connection = _connect_readonly(db_path)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if result is None or result[0] != "ok":
        raise BackupIntegrityError("Database failed integrity verification.")


def _is_sqlite_file(db_path: Path) -> bool:
    try:
        with open(db_path, "rb") as handle:
            header = handle.read(len(_SQLITE_HEADER))
    except OSError:
        return False
    return header == _SQLITE_HEADER


def _verify_schema_compatibility(db_path: Path) -> None:
    connection = _connect_readonly(db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing_tables = set(_REQUIRED_TABLES) - tables
        if missing_tables:
            raise SchemaCompatibilityError(
                "Restore candidate is missing one or more required tables."
            )

        for table, required_columns in _REQUIRED_TABLES.items():
            columns = {
                row[1]
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            missing_columns = required_columns - columns
            if missing_columns:
                raise SchemaCompatibilityError(
                    "Restore candidate is missing one or more required columns."
                )
    finally:
        connection.close()


def _list_verified_backup_files(backup_dir: Path) -> list[Path]:
    """Return published (non-temp, correctly named) backups, oldest first."""
    candidates = [
        path
        for path in backup_dir.glob(f"{_FILENAME_PREFIX}*.db")
        if not path.name.endswith(_TEMP_SUFFIX)
        and _parse_backup_timestamp(path.name) is not None
    ]
    return sorted(candidates, key=lambda path: path.name)


def list_backups(backup_dir: str | None = None) -> list[str]:
    """List published, correctly named backups in backup_dir, oldest first.

    Args:
        backup_dir: Directory to scan; defaults to resolve_backup_dir().
            Temporary and invalid/unrecognized files are excluded.

    Returns:
        Full paths of verified backup files, oldest first. Empty if the
        directory does not exist or contains no matching backups.
    """
    directory = Path(backup_dir) if backup_dir is not None else Path(resolve_backup_dir())
    if not directory.exists():
        return []
    return [str(path) for path in _list_verified_backup_files(directory)]


def _apply_retention(backup_dir: Path, keep: int = _MAX_RETAINED_BACKUPS) -> None:
    backups = _list_verified_backup_files(backup_dir)
    excess = backups[: max(0, len(backups) - keep)]
    for old_backup in excess:
        try:
            old_backup.unlink()
        except OSError:
            logger.warning("Failed to delete an old backup during retention cleanup")


def create_backup(
    db_path: str,
    backup_dir: str | None = None,
    *,
    target_subdir: str | None = None,
    apply_retention: bool = True,
) -> str:
    """Create a verified, standalone backup of the SQLite database at db_path.

    Uses sqlite3.Connection.backup(), so the result is correct regardless of
    the source database's journal mode and safe even if a connection is
    currently open on the source. The backup is written to a temporary
    file, verified with PRAGMA integrity_check, then atomically published
    under its final timestamped name. Retention (keep the most recent
    _MAX_RETAINED_BACKUPS verified backups) is applied only after a
    successful, verified backup, and only against backup_dir's top-level
    contents - never target_subdir.

    Args:
        db_path: Path to the production SQLite database to back up.
        backup_dir: Destination root directory; defaults to
            resolve_backup_dir().
        target_subdir: If given, publish under backup_dir/target_subdir
            instead of backup_dir directly (used for the pre-restore safety
            backup, so it is excluded from normal retention scanning).
        apply_retention: If False, skip retention entirely after publishing
            (used for the pre-restore safety backup).

    Returns:
        The full path to the published backup file.

    Raises:
        FileNotFoundError: If db_path does not exist.
        FileExistsError: If the generated final filename already exists.
        BackupIntegrityError: If the backup fails PRAGMA integrity_check.
        sqlite3.Error: If the source or backup connection cannot be used.
        OSError: If filesystem operations fail.
    """
    source_path = Path(db_path)
    if not source_path.exists():
        raise FileNotFoundError("No database exists at the configured path.")

    root_dir = Path(backup_dir) if backup_dir is not None else Path(resolve_backup_dir())
    target_dir = root_dir / target_subdir if target_subdir else root_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    filename = _generate_backup_filename()
    final_path = target_dir / filename
    if final_path.exists():
        raise FileExistsError("A backup with this generated filename already exists.")

    temp_path = target_dir / f"{filename}{_TEMP_SUFFIX}"
    if temp_path.exists():
        temp_path.unlink()

    source_conn = sqlite3.connect(str(source_path))
    try:
        dest_conn = sqlite3.connect(str(temp_path))
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()

    try:
        _verify_integrity(temp_path)
    except BackupIntegrityError:
        temp_path.unlink(missing_ok=True)
        raise

    os.replace(str(temp_path), str(final_path))
    logger.info("Database backup created")

    if apply_retention:
        try:
            _apply_retention(root_dir)
        except Exception:
            logger.warning("Backup retention cleanup failed; new backup was retained")

    return str(final_path)


def _sidecar_paths(db_path: Path) -> list[Path]:
    return [Path(str(db_path) + suffix) for suffix in _SIDECAR_SUFFIXES]


def _quarantine_sidecars(db_path: Path, quarantine_dir: Path) -> list[Path]:
    """Move any existing sidecar files for db_path into quarantine_dir.

    If any sidecar cannot be safely moved, any sidecars already moved by
    this call are moved back to their original location before
    SidecarQuarantineError is raised, so the database's sidecar state is
    left exactly as found.

    Returns:
        The list of quarantined destination paths (empty if none existed).
    """
    existing = [path for path in _sidecar_paths(db_path) if path.exists()]
    if not existing:
        return []

    quarantine_dir.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []
    try:
        for sidecar in existing:
            destination = quarantine_dir / sidecar.name
            shutil.move(str(sidecar), str(destination))
            moved.append((sidecar, destination))
    except OSError as exc:
        for original, destination in reversed(moved):
            try:
                shutil.move(str(destination), str(original))
            except OSError:
                pass
        raise SidecarQuarantineError(
            "An existing database sidecar file could not be safely quarantined."
        ) from exc

    return [destination for _, destination in moved]


def _restore_quarantined_sidecars(db_path: Path, quarantine_dir: Path) -> None:
    for suffix in _SIDECAR_SUFFIXES:
        quarantined = quarantine_dir / (db_path.name + suffix)
        if quarantined.exists():
            shutil.move(str(quarantined), str(Path(str(db_path) + suffix)))


def _discard_quarantined_sidecars(quarantine_dir: Path) -> None:
    if quarantine_dir.exists():
        shutil.rmtree(quarantine_dir, ignore_errors=True)


def _probe_exclusive_access(db_path: Path) -> None:
    """Best-effort check that no other connection currently holds db_path.

    Not a guarantee across every external process or tool - Windows
    file-locking cannot be verified with certainty from a single process -
    but attempts an exclusive SQLite transaction with a short busy timeout,
    so an active writer elsewhere is likely to be detected rather than
    silently proceeded past.
    """
    connection = sqlite3.connect(str(db_path), timeout=1.0)
    try:
        try:
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            raise ExclusiveAccessError(
                "Could not confirm exclusive access to the production database."
            ) from exc
    finally:
        connection.close()


def _validate_restore_candidate(candidate_path: Path) -> None:
    if not candidate_path.exists():
        raise FileNotFoundError("The selected backup file does not exist.")
    if not _is_sqlite_file(candidate_path):
        raise ValueError("The selected file is not a valid SQLite database.")

    try:
        connection = _connect_readonly(candidate_path)
        connection.close()
    except sqlite3.Error as exc:
        raise ValueError(
            "The selected file could not be opened as a SQLite database."
        ) from exc

    _verify_integrity(candidate_path)
    _verify_schema_compatibility(candidate_path)


def restore_backup(
    db_path: str, candidate_path: str, backup_dir: str | None = None
) -> str:
    """Restore the production database at db_path from candidate_path.

    CLI-only; never called from app/streamlit_app.py. Requires the
    Streamlit application and every other user of the production database
    to already be fully stopped before this is invoked.

    Order of operations:
        1. Validate the candidate (header, read-only open, integrity,
           required-tables-and-columns schema compatibility) before any
           modification.
        2. Best-effort exclusive-access probe against db_path, when it
           exists.
        3. Create and verify a pre-restore safety backup of db_path, when
           it exists, excluded from normal retention.
        4. Quarantine any existing sidecar files (-wal, -shm, -journal)
           for db_path. If any cannot be safely quarantined, abort here -
           db_path and its sidecars are left completely untouched, and the
           unused safety backup (if any) is removed.
        5. Atomically replace the main database file.
        6. Run post-restore integrity and schema validation. Stale
           quarantined sidecars are discarded only once this validation
           succeeds - never before - so a failed restore can still recover
           them (see step 7).
        7. If post-restore validation fails, restore the original database
           from the protected safety backup and restore its quarantined
           sidecars, then re-raise - the newly restored file is never
           relied upon once it fails validation.
        8. On success, the safety backup is promoted into normal retention
           accounting.

    Returns:
        The path to the restored production database on success.

    Raises:
        FileNotFoundError, ValueError, BackupIntegrityError,
        SchemaCompatibilityError: If the candidate fails validation; the
            production database is left completely untouched.
        ExclusiveAccessError: If exclusive access cannot be confirmed; the
            production database is left completely untouched.
        SidecarQuarantineError: If an existing sidecar cannot be safely
            quarantined; the production database and all sidecars are left
            completely untouched.
    """
    production_path = Path(db_path)
    candidate = Path(candidate_path)
    root_dir = Path(backup_dir) if backup_dir is not None else Path(resolve_backup_dir())
    safety_dir = root_dir / _SAFETY_SUBDIR
    quarantine_dir = safety_dir / "sidecars"

    _validate_restore_candidate(candidate)

    production_exists = production_path.exists()
    if production_exists:
        _probe_exclusive_access(production_path)

    safety_backup_path: Path | None = None
    if production_exists:
        safety_backup_path = Path(
            create_backup(
                str(production_path),
                backup_dir=str(root_dir),
                target_subdir=_SAFETY_SUBDIR,
                apply_retention=False,
            )
        )

    try:
        _quarantine_sidecars(production_path, quarantine_dir)
    except SidecarQuarantineError:
        if safety_backup_path is not None:
            try:
                safety_backup_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    temp_restore_path = production_path.parent / f"{production_path.name}{_TEMP_SUFFIX}"
    shutil.copyfile(str(candidate), str(temp_restore_path))
    os.replace(str(temp_restore_path), str(production_path))
    logger.info("Database file replaced")

    try:
        _verify_integrity(production_path)
        _verify_schema_compatibility(production_path)
    except Exception:
        logger.error("Post-restore validation failed; recovering original database")
        if safety_backup_path is not None:
            os.replace(str(safety_backup_path), str(production_path))
            _restore_quarantined_sidecars(production_path, quarantine_dir)
        raise

    # Stale sidecars belonged to the pre-restore database. They are
    # discarded only now that post-restore validation has confirmed the
    # replacement succeeded - never before - so the except block above can
    # still recover them if validation fails.
    _discard_quarantined_sidecars(quarantine_dir)
    logger.info("Database restore completed")

    if safety_backup_path is not None:
        promoted_path = root_dir / safety_backup_path.name
        os.replace(str(safety_backup_path), str(promoted_path))
        try:
            _apply_retention(root_dir)
        except Exception:
            logger.warning("Backup retention cleanup failed after restore")

    return str(production_path)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: `python -m database.backup restore <backup_file_path>`."""
    configure_console_logging()
    argv = sys.argv[1:] if argv is None else argv

    if len(argv) != 2 or argv[0] != "restore":
        print("Usage: python -m database.backup restore <backup_file_path>")
        return 2

    print(
        "This will replace the production database. Ensure the Discord "
        "Traders application and any other program with this database open "
        "are fully closed before continuing."
    )

    try:
        restore_backup(resolve_database_path(), argv[1])
    except Exception as exc:
        log_operation_failure(logger, "database restore", exc)
        print("Restore failed. See logs for details.")
        return 1

    print("Restore completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
