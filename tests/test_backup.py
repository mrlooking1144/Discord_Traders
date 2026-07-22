"""Tests for database/backup.py.

Covers Milestone 2D.3: backup creation via sqlite3.Connection.backup(),
retention, restore-candidate validation, the CLI-only restore workflow
(exclusive-access probing, sidecar quarantine/cleanup/recovery, and
post-restore validation with full recovery on failure), and logging
sanitization. Uses tempfile.TemporaryDirectory exclusively - no real
LOCALAPPDATA, home directory, or the real application database/log is ever
read or written.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.logging_config import log_operation_failure
from database.backup import (
    BackupIntegrityError,
    ExclusiveAccessError,
    SchemaCompatibilityError,
    SidecarQuarantineError,
    create_backup,
    list_backups,
    resolve_backup_dir,
    restore_backup,
)
from database.config import DatabaseConfig
from database.db import initialize_database


class ResolveBackupDirTests(unittest.TestCase):
    """DISCORD_TRADERS_BACKUP_PATH precedence and normalization."""

    def test_override_is_used_and_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            override = str(Path(tmp) / "custom_backups")
            with patch.dict(
                "os.environ", {"DISCORD_TRADERS_BACKUP_PATH": override}, clear=False
            ):
                result = resolve_backup_dir()

        self.assertEqual(result, str(Path(override).resolve()))

    def test_empty_override_falls_back_to_localappdata_default(self):
        with patch.dict(
            "os.environ",
            {
                "DISCORD_TRADERS_BACKUP_PATH": "",
                "LOCALAPPDATA": "C:/Users/tester/AppData/Local",
            },
            clear=False,
        ):
            result = resolve_backup_dir()

        self.assertEqual(
            result,
            str(Path("C:/Users/tester/AppData/Local") / "DiscordTraders" / "backups"),
        )

    def test_missing_localappdata_uses_home_fallback(self):
        env = dict(os.environ)
        env.pop("DISCORD_TRADERS_BACKUP_PATH", None)
        env.pop("LOCALAPPDATA", None)
        fake_home = Path("C:/Users/tester")
        with patch.dict("os.environ", env, clear=True):
            with patch("database.backup.Path.home", return_value=fake_home):
                result = resolve_backup_dir()

        self.assertEqual(
            result, str(fake_home / "AppData" / "Local" / "DiscordTraders" / "backups")
        )

    def test_whitespace_only_localappdata_uses_home_fallback(self):
        env = dict(os.environ)
        env.pop("DISCORD_TRADERS_BACKUP_PATH", None)
        env["LOCALAPPDATA"] = "   "
        fake_home = Path("C:/Users/tester")
        with patch.dict("os.environ", env, clear=True):
            with patch("database.backup.Path.home", return_value=fake_home):
                result = resolve_backup_dir()

        self.assertEqual(
            result, str(fake_home / "AppData" / "Local" / "DiscordTraders" / "backups")
        )


class _TempDatabaseTestCase(unittest.TestCase):
    """Shared temp-directory and initialized-database helpers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.db_path = self.tmp_dir / "discord_traders.db"
        self.backup_dir = self.tmp_dir / "backups"

    def _init_db(self, path: Path | None = None) -> Path:
        path = path or self.db_path
        initialize_database(DatabaseConfig(db_path=str(path)))
        return path

    def _insert_source(self, path: Path | None = None, name: str = "discord") -> None:
        path = path or self.db_path
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("INSERT INTO sources (name) VALUES (?)", (name,))
            conn.commit()
        finally:
            conn.close()

    def _source_names(self, path: Path) -> set[str]:
        conn = sqlite3.connect(str(path))
        try:
            return {row[0] for row in conn.execute("SELECT name FROM sources").fetchall()}
        finally:
            conn.close()


class CreateBackupTests(_TempDatabaseTestCase):
    def test_creates_backup_directory_lazily(self):
        self._init_db()
        self.assertFalse(self.backup_dir.exists())

        create_backup(str(self.db_path), backup_dir=str(self.backup_dir))

        self.assertTrue(self.backup_dir.is_dir())

    def test_backup_of_closed_database_is_standalone_and_verified(self):
        self._init_db()
        self._insert_source()

        result_path = create_backup(str(self.db_path), backup_dir=str(self.backup_dir))

        self.assertTrue(Path(result_path).is_file())
        self.assertEqual(self._source_names(Path(result_path)), {"discord"})
        conn = sqlite3.connect(result_path)
        try:
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
        finally:
            conn.close()

    def test_backup_while_connection_open_on_source_succeeds(self):
        self._init_db()
        self._insert_source()
        open_conn = sqlite3.connect(str(self.db_path))
        try:
            result_path = create_backup(str(self.db_path), backup_dir=str(self.backup_dir))
            self.assertEqual(self._source_names(Path(result_path)), {"discord"})
        finally:
            open_conn.close()

    def test_backup_of_wal_mode_source_is_standalone_with_no_sidecar_copy(self):
        self._init_db()
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("INSERT INTO sources (name) VALUES ('discord')")
            conn.commit()

            result_path = create_backup(str(self.db_path), backup_dir=str(self.backup_dir))

            self.assertEqual(self._source_names(Path(result_path)), {"discord"})
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(Path(result_path + suffix).exists())
        finally:
            conn.close()

    def test_backup_when_database_does_not_exist_raises_and_creates_nothing(self):
        with self.assertRaises(FileNotFoundError):
            create_backup(str(self.db_path), backup_dir=str(self.backup_dir))

        self.assertFalse(self.backup_dir.exists())

    def test_backup_of_empty_schema_only_database_succeeds(self):
        self._init_db()

        result_path = create_backup(str(self.db_path), backup_dir=str(self.backup_dir))

        self.assertEqual(self._source_names(Path(result_path)), set())

    def test_backup_filename_matches_expected_pattern(self):
        self._init_db()

        result_path = create_backup(str(self.db_path), backup_dir=str(self.backup_dir))

        name = Path(result_path).name
        self.assertTrue(name.startswith("discord_traders_"))
        self.assertTrue(name.endswith(".db"))
        stem = name[len("discord_traders_") : -len(".db")]
        date_part, time_part, micro_part = stem.split("_")
        self.assertEqual(len(date_part), 8)
        self.assertEqual(len(time_part), 6)
        self.assertEqual(len(micro_part), 6)

    def test_unexpected_filename_collision_is_refused(self):
        self._init_db()
        with patch(
            "database.backup._generate_backup_filename",
            return_value="discord_traders_20260101_000000_000000.db",
        ):
            first = create_backup(str(self.db_path), backup_dir=str(self.backup_dir))
            self.assertTrue(Path(first).exists())

            with self.assertRaises(FileExistsError):
                create_backup(str(self.db_path), backup_dir=str(self.backup_dir))

    def test_failed_integrity_check_leaves_no_temp_or_final_file(self):
        self._init_db()
        with patch(
            "database.backup._verify_integrity",
            side_effect=BackupIntegrityError("simulated"),
        ):
            with self.assertRaises(BackupIntegrityError):
                create_backup(str(self.db_path), backup_dir=str(self.backup_dir))

        self.assertEqual(list(self.backup_dir.glob("*")), [])


class RetentionTests(_TempDatabaseTestCase):
    def _make_dated_backup(self, index: int) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        name = f"discord_traders_202601{index:02d}_000000_000000.db"
        path = self.backup_dir / name
        conn = sqlite3.connect(str(path))
        conn.close()
        return path

    def test_retention_keeps_latest_ten_oldest_deleted_first(self):
        self._init_db()
        for index in range(1, 11):
            self._make_dated_backup(index)

        create_backup(str(self.db_path), backup_dir=str(self.backup_dir))

        remaining = sorted(p.name for p in self.backup_dir.glob("discord_traders_*.db"))
        self.assertEqual(len(remaining), 10)
        self.assertNotIn("discord_traders_20260101_000000_000000.db", remaining)

    def test_retention_runs_only_after_verified_backup(self):
        self._init_db()
        for index in range(1, 11):
            self._make_dated_backup(index)
        before = sorted(p.name for p in self.backup_dir.glob("discord_traders_*.db"))

        with patch(
            "database.backup._verify_integrity",
            side_effect=BackupIntegrityError("simulated"),
        ):
            with self.assertRaises(BackupIntegrityError):
                create_backup(str(self.db_path), backup_dir=str(self.backup_dir))

        after = sorted(p.name for p in self.backup_dir.glob("discord_traders_*.db"))
        self.assertEqual(before, after)

    def test_retention_deletion_failure_is_non_fatal(self):
        self._init_db()
        for index in range(1, 11):
            self._make_dated_backup(index)
        target_name = "discord_traders_20260101_000000_000000.db"
        real_unlink = Path.unlink

        def flaky_unlink(self, *args, **kwargs):
            if self.name == target_name:
                raise OSError("simulated deletion failure")
            return real_unlink(self, *args, **kwargs)

        with patch.object(Path, "unlink", flaky_unlink):
            with self.assertLogs("discord_traders", level="WARNING") as captured:
                result_path = create_backup(
                    str(self.db_path), backup_dir=str(self.backup_dir)
                )

        self.assertTrue(Path(result_path).exists())
        self.assertTrue((self.backup_dir / target_name).exists())
        self.assertTrue(
            any("retention" in line.lower() for line in captured.output)
        )

    def test_temp_and_invalid_files_excluded_from_retention(self):
        self._init_db()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        for index in range(1, 10):
            self._make_dated_backup(index)
        junk_tmp = self.backup_dir / "discord_traders_20260101_000000_000000.db.tmp"
        junk_tmp.write_bytes(b"junk")
        junk_invalid = self.backup_dir / "discord_traders_not_a_timestamp.db"
        junk_invalid.write_bytes(b"junk")

        create_backup(str(self.db_path), backup_dir=str(self.backup_dir))

        self.assertTrue(junk_tmp.exists())
        self.assertTrue(junk_invalid.exists())
        verified = list_backups(str(self.backup_dir))
        self.assertEqual(len(verified), 10)


class RestoreCandidateValidationTests(_TempDatabaseTestCase):
    def test_missing_candidate_raises_file_not_found(self):
        self._init_db()
        missing = self.tmp_dir / "does_not_exist.db"

        with self.assertRaises(FileNotFoundError):
            restore_backup(
                str(self.db_path), str(missing), backup_dir=str(self.backup_dir)
            )

    def test_non_sqlite_file_rejected(self):
        self._init_db()
        not_sqlite = self.tmp_dir / "not_sqlite.db"
        not_sqlite.write_text("not a database")

        with self.assertRaises(ValueError):
            restore_backup(
                str(self.db_path), str(not_sqlite), backup_dir=str(self.backup_dir)
            )

    def test_failed_integrity_check_rejected(self):
        self._init_db()
        candidate = create_backup(str(self.db_path), backup_dir=str(self.backup_dir / "candidates"))

        with patch(
            "database.backup._verify_integrity",
            side_effect=BackupIntegrityError("simulated"),
        ):
            with self.assertRaises(BackupIntegrityError):
                restore_backup(str(self.db_path), candidate, backup_dir=str(self.backup_dir))

    def test_missing_required_table_rejected(self):
        candidate_path = self.tmp_dir / "missing_table.db"
        conn = sqlite3.connect(str(candidate_path))
        try:
            conn.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT)")
            conn.commit()
        finally:
            conn.close()
        self._init_db()

        with self.assertRaises(SchemaCompatibilityError):
            restore_backup(
                str(self.db_path), str(candidate_path), backup_dir=str(self.backup_dir)
            )

    def test_missing_required_column_rejected(self):
        candidate_path = self.tmp_dir / "missing_column.db"
        conn = sqlite3.connect(str(candidate_path))
        try:
            conn.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute(
                "CREATE TABLE traders (id INTEGER PRIMARY KEY, source_id INTEGER)"
            )
            conn.execute(
                "CREATE TABLE raw_messages (id INTEGER PRIMARY KEY, source_id INTEGER, "
                "raw_text TEXT, content_hash TEXT)"
            )
            conn.execute(
                "CREATE TABLE trade_signals (id INTEGER PRIMARY KEY, raw_message_id INTEGER, "
                "trader_id INTEGER, symbol TEXT, action TEXT)"
            )
            conn.execute(
                "CREATE TABLE trade_signal_edits (id INTEGER PRIMARY KEY, "
                "trade_signal_id INTEGER, previous_values TEXT)"
            )
            conn.commit()
        finally:
            conn.close()
        self._init_db()

        with self.assertRaises(SchemaCompatibilityError):
            restore_backup(
                str(self.db_path), str(candidate_path), backup_dir=str(self.backup_dir)
            )

    def test_no_modification_when_candidate_invalid(self):
        self._init_db()
        self._insert_source()
        stray_journal = Path(str(self.db_path) + "-journal")
        stray_journal.write_bytes(b"stray")
        original_bytes = self.db_path.read_bytes()

        not_sqlite = self.tmp_dir / "not_sqlite.db"
        not_sqlite.write_text("not a database")

        with self.assertRaises(ValueError):
            restore_backup(
                str(self.db_path), str(not_sqlite), backup_dir=str(self.backup_dir)
            )

        self.assertEqual(self.db_path.read_bytes(), original_bytes)
        self.assertTrue(stray_journal.exists())
        self.assertFalse((self.backup_dir / "prerestore_safety").exists())


class RestoreWorkflowTests(_TempDatabaseTestCase):
    def _make_candidate(self, source_name: str = "candidate_source") -> str:
        source_dir = self.tmp_dir / "source_for_candidate"
        source_dir.mkdir()
        source_db = source_dir / "discord_traders.db"
        initialize_database(DatabaseConfig(db_path=str(source_db)))
        self._insert_source(source_db, name=source_name)
        return create_backup(str(source_db), backup_dir=str(self.tmp_dir / "candidates"))

    def _stage_sidecars_after_safety_backup(self, sidecar_contents: dict[str, bytes]):
        """Patch database.backup.create_backup so the given sidecar files
        are (re)written immediately after the pre-restore safety backup
        step completes, simulating a sidecar that survives up to the
        moment quarantine runs.

        A genuinely stale sidecar left beside db_path would normally be
        cleaned up by SQLite itself as part of its own hot-journal/
        stale-wal recovery the moment any connection is opened against
        db_path - including the exclusive-access probe and the safety
        backup's own connection, both of which necessarily run before
        quarantine. Writing the sidecar at this exact point isolates
        quarantine's own failure/recovery behavior from that unrelated,
        real SQLite cleanup behavior.

        Returns the patch context manager; the caller is responsible for
        entering/exiting it (typically via `with`).
        """
        import database.backup as backup_mod

        real_create_backup = backup_mod.create_backup

        def fake_create_backup(db_path, backup_dir=None, **kwargs):
            result = real_create_backup(db_path, backup_dir=backup_dir, **kwargs)
            if kwargs.get("target_subdir") == "prerestore_safety":
                for suffix, content in sidecar_contents.items():
                    Path(str(db_path) + suffix).write_bytes(content)
            return result

        return patch("database.backup.create_backup", side_effect=fake_create_backup)

    def test_valid_restore_end_to_end(self):
        self._init_db()
        self._insert_source(name="original")
        candidate = self._make_candidate()

        result_path = restore_backup(
            str(self.db_path), candidate, backup_dir=str(self.backup_dir)
        )

        self.assertEqual(result_path, str(self.db_path))
        self.assertEqual(self._source_names(self.db_path), {"candidate_source"})

    def test_restore_over_existing_production_database(self):
        self._init_db()
        self._insert_source(name="original")
        candidate = self._make_candidate()

        restore_backup(str(self.db_path), candidate, backup_dir=str(self.backup_dir))

        self.assertNotIn("original", self._source_names(self.db_path))

    def test_restore_when_no_current_database_exists(self):
        candidate = self._make_candidate()
        self.assertFalse(self.db_path.exists())

        restore_backup(str(self.db_path), candidate, backup_dir=str(self.backup_dir))

        self.assertTrue(self.db_path.exists())
        self.assertEqual(self._source_names(self.db_path), {"candidate_source"})
        self.assertFalse((self.backup_dir / "prerestore_safety").exists())

    def test_safety_backup_created_and_verified_before_replacement(self):
        self._init_db()
        self._insert_source(name="original")
        candidate = self._make_candidate()

        restore_backup(str(self.db_path), candidate, backup_dir=str(self.backup_dir))

        promoted = list(self.backup_dir.glob("discord_traders_*.db"))
        self.assertEqual(len(promoted), 1)
        self.assertEqual(self._source_names(promoted[0]), {"original"})

    def test_safety_backup_excluded_from_retention_until_success(self):
        self._init_db()
        self._insert_source(name="original")
        candidate = self._make_candidate()

        with patch(
            "database.backup._verify_schema_compatibility",
            side_effect=[None, SchemaCompatibilityError("simulated post-restore failure")],
        ):
            with self.assertRaises(SchemaCompatibilityError):
                restore_backup(str(self.db_path), candidate, backup_dir=str(self.backup_dir))

        self.assertEqual(list(self.backup_dir.glob("discord_traders_*.db")), [])

    def test_wal_and_shm_sidecars_quarantined_and_removed_after_success(self):
        self._init_db()
        self._insert_source(name="original")
        candidate = self._make_candidate()
        wal_path = Path(str(self.db_path) + "-wal")
        shm_path = Path(str(self.db_path) + "-shm")
        wal_path.write_bytes(b"stray-wal")
        shm_path.write_bytes(b"stray-shm")

        restore_backup(str(self.db_path), candidate, backup_dir=str(self.backup_dir))

        self.assertFalse(wal_path.exists())
        self.assertFalse(shm_path.exists())

    def test_journal_sidecar_quarantined_and_removed_after_success(self):
        self._init_db()
        self._insert_source(name="original")
        candidate = self._make_candidate()
        journal_path = Path(str(self.db_path) + "-journal")
        journal_path.write_bytes(b"stray-journal")

        restore_backup(str(self.db_path), candidate, backup_dir=str(self.backup_dir))

        self.assertFalse(journal_path.exists())

    def test_no_stale_sidecar_remains_after_successful_restore(self):
        self._init_db()
        self._insert_source(name="original")
        candidate = self._make_candidate()
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(self.db_path) + suffix).write_bytes(b"stray")

        restore_backup(str(self.db_path), candidate, backup_dir=str(self.backup_dir))

        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(str(self.db_path) + suffix).exists())
        quarantine_dir = self.backup_dir / "prerestore_safety" / "sidecars"
        self.assertFalse(quarantine_dir.exists())

    def test_sidecar_quarantine_failure_aborts_before_replacement(self):
        self._init_db()
        self._insert_source(name="original")
        candidate = self._make_candidate()
        wal_path = Path(str(self.db_path) + "-wal")
        original_bytes = self.db_path.read_bytes()

        with self._stage_sidecars_after_safety_backup({"-wal": b"stray-wal"}), patch(
            "database.backup.shutil.move", side_effect=OSError("simulated lock")
        ):
            with self.assertRaises(SidecarQuarantineError):
                restore_backup(
                    str(self.db_path), candidate, backup_dir=str(self.backup_dir)
                )

        self.assertEqual(self.db_path.read_bytes(), original_bytes)
        self.assertTrue(wal_path.exists())
        self.assertEqual(wal_path.read_bytes(), b"stray-wal")
        self.assertEqual(list(self.backup_dir.glob("discord_traders_*.db")), [])

    def test_exclusivity_probe_failure_aborts_restore(self):
        self._init_db()
        self._insert_source(name="original")
        candidate = self._make_candidate()
        original_bytes = self.db_path.read_bytes()

        locker = sqlite3.connect(str(self.db_path), timeout=1.0)
        try:
            locker.execute("BEGIN EXCLUSIVE")
            with self.assertRaises(ExclusiveAccessError):
                restore_backup(
                    str(self.db_path), candidate, backup_dir=str(self.backup_dir)
                )
        finally:
            locker.rollback()
            locker.close()

        self.assertEqual(self.db_path.read_bytes(), original_bytes)
        self.assertEqual(list(self.backup_dir.glob("discord_traders_*.db")), [])

    def test_post_restore_validation_failure_recovers_original_database_and_sidecars(self):
        self._init_db()
        self._insert_source(name="original")
        candidate = self._make_candidate()
        journal_path = Path(str(self.db_path) + "-journal")

        with self._stage_sidecars_after_safety_backup(
            {"-journal": b"stray-journal"}
        ), patch(
            "database.backup._verify_schema_compatibility",
            side_effect=[None, SchemaCompatibilityError("simulated post-restore failure")],
        ):
            with self.assertRaises(SchemaCompatibilityError):
                restore_backup(
                    str(self.db_path), candidate, backup_dir=str(self.backup_dir)
                )

        # Checked before any further connection is opened against
        # self.db_path (including via _source_names() below): SQLite
        # itself treats a "-journal" file beside a database as a hot
        # journal and cleans it up on connect, so this ordering avoids
        # the test's own verification erasing what recovery just restored.
        self.assertTrue(journal_path.exists())
        self.assertEqual(journal_path.read_bytes(), b"stray-journal")

        # The recovered file is written via the same backup API used for
        # the safety backup, so it is a logically identical (not
        # necessarily byte-identical) copy - the requirement is that the
        # original data, not the failed candidate's data, is present.
        self.assertEqual(self._source_names(self.db_path), {"original"})


class LoggingSanitizationTests(_TempDatabaseTestCase):
    def test_backup_failure_logs_sanitized_no_path_or_exception_text(self):
        # create_backup() itself only logs on success (INFO); a raised
        # exception is the caller's responsibility to log via
        # log_operation_failure() (see the two tests below). This test
        # documents that create_backup() emits no log record of its own
        # mentioning the rejected path when it fails.
        sentinel_path = str(self.tmp_dir / "SENTINEL_MISSING_PATH_991" / "discord_traders.db")
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger("discord_traders")
        logger.addHandler(handler)
        try:
            with self.assertRaises(FileNotFoundError):
                create_backup(sentinel_path, backup_dir=str(self.backup_dir))
        finally:
            logger.removeHandler(handler)

        joined = "\n".join(record.getMessage() for record in records)
        self.assertNotIn("SENTINEL_MISSING_PATH_991", joined)

    def test_restore_failure_via_cli_logs_sanitized_no_path_or_exception_text(self):
        self._init_db()
        candidate = self.tmp_dir / "not_sqlite.db"
        candidate.write_text("not a database")
        logger = logging.getLogger("discord_traders.backup")

        with self.assertLogs("discord_traders", level="ERROR") as captured:
            try:
                restore_backup(
                    str(self.db_path), str(candidate), backup_dir=str(self.backup_dir)
                )
            except ValueError as exc:
                log_operation_failure(logger, "database restore", exc)

        joined = "\n".join(captured.output)
        self.assertIn("database restore failed", joined)
        self.assertIn("ValueError", joined)
        self.assertNotIn(str(self.db_path), joined)
        self.assertNotIn(str(candidate), joined)

    def test_no_critical_logging_for_backup_or_restore_failures(self):
        self._init_db()
        logger = logging.getLogger("discord_traders.backup")

        with self.assertLogs("discord_traders", level="ERROR") as captured:
            try:
                create_backup(
                    str(self.tmp_dir / "missing.db"), backup_dir=str(self.backup_dir)
                )
            except FileNotFoundError as exc:
                log_operation_failure(logger, "database backup", exc)

        self.assertTrue(
            all(record.levelno < logging.CRITICAL for record in captured.records)
        )


if __name__ == "__main__":
    unittest.main()
