"""Tests for app/logging_config.py.

Covers Milestone 2D.2: the two independently idempotent handler-
configuration functions, the log-path resolver (fully independent of
database.config.resolve_database_path()/DISCORD_TRADERS_DB_PATH), and
the sanitized diagnostic helpers. Only environment variables, a private
handler-close spy, and temporary directories are used - no real
LOCALAPPDATA or home directory is ever read or written.
"""

import logging
import logging.handlers
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import logging_config
from app.logging_config import (
    configure_console_logging,
    configure_file_logging,
    log_operation_failure,
    resolve_log_path,
    sanitized_traceback,
)


class _LoggerIsolationTestCase(unittest.TestCase):
    """Base for tests that inspect the discord_traders logger's handler
    list directly. Detaches any handlers already present (e.g. attached
    for real by app.py's module-level configure_console_logging() call
    during other test files' AppTest runs) before each test and
    reattaches them unmodified afterward, so each test starts from a
    deterministic, empty handler list without disturbing any other test
    file's logging state.
    """

    def setUp(self):
        self._logger = logging.getLogger(logging_config._LOGGER_NAME)
        self._original_handlers = list(self._logger.handlers)
        self._original_level = self._logger.level
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)
        self._logger.setLevel(logging.NOTSET)

    def tearDown(self):
        for handler in list(self._logger.handlers):
            if handler not in self._original_handlers:
                handler.close()
            self._logger.removeHandler(handler)
        for handler in self._original_handlers:
            self._logger.addHandler(handler)
        self._logger.setLevel(self._original_level)


class ConsoleLoggingTests(_LoggerIsolationTestCase):
    def test_adds_exactly_one_console_handler(self):
        configure_console_logging()

        console_handlers = [
            h
            for h in self._logger.handlers
            if getattr(h, logging_config._CONSOLE_HANDLER_MARKER, False)
        ]
        self.assertEqual(len(console_handlers), 1)
        self.assertIsInstance(console_handlers[0], logging.StreamHandler)

    def test_idempotent_across_repeated_calls(self):
        configure_console_logging()
        configure_console_logging()
        configure_console_logging()

        console_handlers = [
            h
            for h in self._logger.handlers
            if getattr(h, logging_config._CONSOLE_HANDLER_MARKER, False)
        ]
        self.assertEqual(len(console_handlers), 1)

    def test_sets_default_level_info(self):
        configure_console_logging()
        self.assertEqual(self._logger.level, logging.INFO)

    def test_format_and_datefmt(self):
        configure_console_logging()

        handler = self._logger.handlers[0]
        self.assertEqual(handler.formatter._fmt, "%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        self.assertEqual(handler.formatter.datefmt, "%Y-%m-%d %H:%M:%S")


class FileLoggingTests(_LoggerIsolationTestCase):
    def _file_handlers(self):
        return [
            h
            for h in self._logger.handlers
            if getattr(h, logging_config._FILE_HANDLER_MARKER, False)
        ]

    def _console_handlers(self):
        return [
            h
            for h in self._logger.handlers
            if getattr(h, logging_config._CONSOLE_HANDLER_MARKER, False)
        ]

    def test_idempotent_across_repeated_calls(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = str(Path(tmp_dir) / "discord_traders.log")
            with patch.dict(os.environ, {"DISCORD_TRADERS_LOG_PATH": log_path}, clear=False):
                configure_file_logging()
                configure_file_logging()

                self.assertEqual(len(self._file_handlers()), 1)

    def test_existing_console_handler_does_not_prevent_file_handler_attachment(self):
        configure_console_logging()
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = str(Path(tmp_dir) / "discord_traders.log")
            with patch.dict(os.environ, {"DISCORD_TRADERS_LOG_PATH": log_path}, clear=False):
                configure_file_logging()

                self.assertEqual(len(self._console_handlers()), 1)
                self.assertEqual(len(self._file_handlers()), 1)

    def test_existing_file_handler_does_not_cause_duplicate_attachment(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = str(Path(tmp_dir) / "discord_traders.log")
            with patch.dict(os.environ, {"DISCORD_TRADERS_LOG_PATH": log_path}, clear=False):
                configure_file_logging()
                configure_console_logging()
                configure_file_logging()

                self.assertEqual(len(self._file_handlers()), 1)

    def test_configures_console_logging_as_a_guarantee(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = str(Path(tmp_dir) / "discord_traders.log")
            with patch.dict(os.environ, {"DISCORD_TRADERS_LOG_PATH": log_path}, clear=False):
                configure_file_logging()

                self.assertEqual(len(self._console_handlers()), 1)

    def test_exact_rotation_parameters(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = str(Path(tmp_dir) / "discord_traders.log")
            with patch.dict(os.environ, {"DISCORD_TRADERS_LOG_PATH": log_path}, clear=False):
                with patch(
                    "logging.handlers.RotatingFileHandler",
                    wraps=logging.handlers.RotatingFileHandler,
                ) as mock_handler_cls:
                    configure_file_logging()

                mock_handler_cls.assert_called_once_with(
                    log_path,
                    maxBytes=1_000_000,
                    backupCount=3,
                    encoding="utf-8",
                    mode="a",
                    delay=True,
                )

    def test_oserror_during_directory_creation_falls_back_to_console(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = str(Path(tmp_dir) / "sub" / "discord_traders.log")
            with patch.dict(os.environ, {"DISCORD_TRADERS_LOG_PATH": log_path}, clear=False):
                with patch.object(Path, "mkdir", side_effect=OSError("disk full")):
                    configure_file_logging()

                self.assertEqual(len(self._console_handlers()), 1)
                self.assertEqual(len(self._file_handlers()), 0)

    def test_oserror_during_handler_construction_falls_back_to_console(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = str(Path(tmp_dir) / "discord_traders.log")
            with patch.dict(os.environ, {"DISCORD_TRADERS_LOG_PATH": log_path}, clear=False):
                with patch(
                    "logging.handlers.RotatingFileHandler",
                    side_effect=OSError("cannot open file"),
                ):
                    configure_file_logging()

                self.assertEqual(len(self._console_handlers()), 1)
                self.assertEqual(len(self._file_handlers()), 0)

    def test_non_oserror_exception_during_path_resolution_falls_back_to_console(self):
        with patch(
            "app.logging_config.resolve_log_path",
            side_effect=RuntimeError("cannot resolve"),
        ):
            configure_file_logging()

        self.assertEqual(len(self._console_handlers()), 1)
        self.assertEqual(len(self._file_handlers()), 0)

    def test_fallback_warning_is_fixed_with_no_exception_or_path(self):
        with patch(
            "app.logging_config.resolve_log_path",
            side_effect=RuntimeError("SENTINEL_RESOLUTION_EXC_667"),
        ):
            with self.assertLogs(logging_config._LOGGER_NAME, level="WARNING") as captured:
                configure_file_logging()

        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        self.assertEqual(
            message,
            "File logging unavailable; continuing with console logging only.",
        )
        self.assertNotIn("SENTINEL_RESOLUTION_EXC_667", message)
        self.assertNotIn("RuntimeError", message)

    def test_oserror_fallback_warning_contains_no_exception_or_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = str(Path(tmp_dir) / "discord_traders.log")
            with patch.dict(os.environ, {"DISCORD_TRADERS_LOG_PATH": log_path}, clear=False):
                with patch(
                    "logging.handlers.RotatingFileHandler",
                    side_effect=OSError("SENTINEL_OSERROR_EXC_778"),
                ):
                    with self.assertLogs(
                        logging_config._LOGGER_NAME, level="WARNING"
                    ) as captured:
                        configure_file_logging()

        message = captured.records[0].getMessage()
        self.assertEqual(
            message,
            "File logging unavailable; continuing with console logging only.",
        )
        self.assertNotIn("SENTINEL_OSERROR_EXC_778", message)
        self.assertNotIn(log_path, message)

    def test_partially_created_handler_is_closed_after_later_setup_failure(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = str(Path(tmp_dir) / "discord_traders.log")
            with patch.dict(os.environ, {"DISCORD_TRADERS_LOG_PATH": log_path}, clear=False):
                with patch.object(
                    logging.handlers.RotatingFileHandler,
                    "setFormatter",
                    side_effect=OSError("format failure"),
                ), patch.object(
                    logging.handlers.RotatingFileHandler, "close", autospec=True
                ) as mock_close:
                    configure_file_logging()

                mock_close.assert_called_once()

    def test_no_partial_handler_remains_attached_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = str(Path(tmp_dir) / "discord_traders.log")
            with patch.dict(os.environ, {"DISCORD_TRADERS_LOG_PATH": log_path}, clear=False):
                with patch.object(
                    logging.handlers.RotatingFileHandler,
                    "setFormatter",
                    side_effect=OSError("format failure"),
                ):
                    configure_file_logging()

                self.assertEqual(len(self._file_handlers()), 0)


class ResolveLogPathTests(unittest.TestCase):
    def test_override_absolute(self):
        absolute = str(Path("C:/somewhere/custom.log").resolve())
        with patch.dict("os.environ", {"DISCORD_TRADERS_LOG_PATH": absolute}, clear=False):
            self.assertEqual(resolve_log_path(), absolute)

    def test_override_relative_becomes_absolute(self):
        with patch.dict("os.environ", {"DISCORD_TRADERS_LOG_PATH": "dev.log"}, clear=False):
            result = resolve_log_path()
        self.assertTrue(Path(result).is_absolute())
        self.assertEqual(result, str(Path("dev.log").resolve()))

    def test_override_whitespace_is_stripped(self):
        with patch.dict(
            "os.environ", {"DISCORD_TRADERS_LOG_PATH": "   dev.log   "}, clear=False
        ):
            result = resolve_log_path()
        self.assertEqual(result, str(Path("dev.log").resolve()))

    def test_empty_override_falls_back_to_localappdata_default(self):
        with patch.dict(
            "os.environ",
            {
                "DISCORD_TRADERS_LOG_PATH": "",
                "LOCALAPPDATA": "C:/Users/tester/AppData/Local",
            },
            clear=False,
        ):
            result = resolve_log_path()
        self.assertEqual(
            result,
            str(
                Path("C:/Users/tester/AppData/Local")
                / "DiscordTraders"
                / "logs"
                / "discord_traders.log"
            ),
        )

    def test_whitespace_only_override_falls_back_to_default(self):
        with patch.dict(
            "os.environ",
            {
                "DISCORD_TRADERS_LOG_PATH": "   ",
                "LOCALAPPDATA": "C:/Users/tester/AppData/Local",
            },
            clear=False,
        ):
            result = resolve_log_path()
        self.assertEqual(
            result,
            str(
                Path("C:/Users/tester/AppData/Local")
                / "DiscordTraders"
                / "logs"
                / "discord_traders.log"
            ),
        )

    def test_default_resolves_to_exact_localappdata_logs_path(self):
        env = dict(os.environ)
        env.pop("DISCORD_TRADERS_LOG_PATH", None)
        env["LOCALAPPDATA"] = "C:/Users/tester/AppData/Local"
        with patch.dict("os.environ", env, clear=True):
            result = resolve_log_path()
        self.assertEqual(
            result,
            str(
                Path("C:/Users/tester/AppData/Local")
                / "DiscordTraders"
                / "logs"
                / "discord_traders.log"
            ),
        )

    def test_missing_localappdata_uses_home_fallback(self):
        env = dict(os.environ)
        env.pop("DISCORD_TRADERS_LOG_PATH", None)
        env.pop("LOCALAPPDATA", None)
        fake_home = Path("C:/Users/tester")
        with patch.dict("os.environ", env, clear=True):
            with patch("app.logging_config.Path.home", return_value=fake_home):
                result = resolve_log_path()
        self.assertEqual(
            result,
            str(fake_home / "AppData" / "Local" / "DiscordTraders" / "logs" / "discord_traders.log"),
        )

    def test_empty_localappdata_uses_home_fallback(self):
        env = dict(os.environ)
        env.pop("DISCORD_TRADERS_LOG_PATH", None)
        env["LOCALAPPDATA"] = ""
        fake_home = Path("C:/Users/tester")
        with patch.dict("os.environ", env, clear=True):
            with patch("app.logging_config.Path.home", return_value=fake_home):
                result = resolve_log_path()
        self.assertEqual(
            result,
            str(fake_home / "AppData" / "Local" / "DiscordTraders" / "logs" / "discord_traders.log"),
        )

    def test_discord_traders_db_path_has_no_effect(self):
        env = dict(os.environ)
        env.pop("DISCORD_TRADERS_LOG_PATH", None)
        env["LOCALAPPDATA"] = "C:/Users/tester/AppData/Local"
        env["DISCORD_TRADERS_DB_PATH"] = "C:/completely/different/db/location.db"
        with patch.dict("os.environ", env, clear=True):
            result = resolve_log_path()
        self.assertEqual(
            result,
            str(
                Path("C:/Users/tester/AppData/Local")
                / "DiscordTraders"
                / "logs"
                / "discord_traders.log"
            ),
        )
        self.assertNotIn("completely", result)
        self.assertNotIn("different", result)


class SanitizedTracebackTests(unittest.TestCase):
    def _raise_and_capture(self):
        try:
            raise ValueError("SENTINEL_TB_EXC_889")
        except ValueError as exc:
            return exc

    def test_never_includes_absolute_source_path(self):
        tmp_dir = tempfile.mkdtemp(prefix="distinctive_sentinel_dir_")
        module_path = Path(tmp_dir) / "sentinel_module.py"
        module_path.write_text(
            "def raise_something():\n    raise ValueError('boom')\n",
            encoding="utf-8",
        )
        code = compile(module_path.read_text(encoding="utf-8"), str(module_path), "exec")
        namespace: dict = {}
        exec(code, namespace)

        try:
            namespace["raise_something"]()
        except ValueError as exc:
            result = sanitized_traceback(exc)

        self.assertNotIn(tmp_dir, result)
        self.assertNotIn(Path(tmp_dir).name, result)
        self.assertIn("sentinel_module.py", result)
        self.assertIn("raise_something", result)

    def test_retains_basename_function_name_line_number_and_source_line(self):
        exc = self._raise_and_capture()
        result = sanitized_traceback(exc)

        self.assertIn("test_logging_config.py", result)
        self.assertIn("_raise_and_capture", result)
        self.assertIn('raise ValueError("SENTINEL_TB_EXC_889")', result)

    def test_appends_only_exception_class_name(self):
        exc = self._raise_and_capture()
        result = sanitized_traceback(exc)

        self.assertTrue(result.rstrip().endswith("ValueError"))

    def test_never_includes_exception_message(self):
        exc = self._raise_and_capture()
        result = sanitized_traceback(exc)

        # The message text appears only inside the static source line
        # (the literal `raise ValueError("...")` code), never as a
        # standalone "ValueError: SENTINEL_TB_EXC_889" summary line.
        self.assertNotIn("ValueError: SENTINEL_TB_EXC_889", result)


class LogOperationFailureTests(unittest.TestCase):
    def test_logs_operation_and_exception_class_at_error(self):
        logger = logging.getLogger("discord_traders.test_logging_config")
        try:
            raise ValueError("SENTINEL_OP_EXC_990")
        except ValueError as exc:
            with self.assertLogs("discord_traders", level="ERROR") as captured:
                log_operation_failure(logger, "test operation", exc)

        joined = "\n".join(captured.output)
        self.assertIn("test operation failed (ValueError)", joined)

    def test_never_logs_exception_message(self):
        logger = logging.getLogger("discord_traders.test_logging_config")
        # The sentinel is built from a variable, not a literal, so it never
        # appears in the static source line either - only in the runtime
        # exception message, which must never be logged.
        sentinel = "SENTINEL_OP_EXC_" + "991"
        try:
            raise ValueError(sentinel)
        except ValueError as exc:
            with self.assertLogs("discord_traders", level="ERROR") as captured:
                log_operation_failure(logger, "test operation", exc)

        joined = "\n".join(captured.output)
        self.assertNotIn(sentinel, joined)


if __name__ == "__main__":
    unittest.main()
