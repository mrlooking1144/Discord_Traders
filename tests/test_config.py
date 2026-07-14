"""Tests for database/config.py.

Covers Milestone 2D.1: resolve_database_path(), the explicit runtime
database-path resolution function. Only environment variables and
pathlib.Path.home() are patched here - no real LOCALAPPDATA or home
directory is ever read or written.
"""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from database.config import DatabaseConfig, resolve_database_path


class ResolveDatabasePathOverrideTests(unittest.TestCase):
    """DISCORD_TRADERS_DB_PATH precedence and normalization."""

    def test_absolute_override_is_used(self):
        absolute = str(Path("C:/somewhere/custom.db").resolve())
        with patch.dict(
            "os.environ", {"DISCORD_TRADERS_DB_PATH": absolute}, clear=False
        ):
            result = resolve_database_path()

        self.assertEqual(result, absolute)
        self.assertTrue(Path(result).is_absolute())

    def test_relative_override_becomes_absolute(self):
        with patch.dict(
            "os.environ", {"DISCORD_TRADERS_DB_PATH": "dev.db"}, clear=False
        ):
            result = resolve_database_path()

        self.assertTrue(Path(result).is_absolute())
        self.assertEqual(result, str(Path("dev.db").resolve()))

    def test_tilde_override_is_expanded(self):
        with patch.dict(
            "os.environ",
            {"DISCORD_TRADERS_DB_PATH": "~/discord_traders_dev.db"},
            clear=False,
        ):
            result = resolve_database_path()

        self.assertNotIn("~", result)
        self.assertEqual(
            result, str(Path("~/discord_traders_dev.db").expanduser().resolve())
        )

    def test_redundant_segments_are_normalized(self):
        with patch.dict(
            "os.environ",
            {"DISCORD_TRADERS_DB_PATH": "a/../b/dev.db"},
            clear=False,
        ):
            result = resolve_database_path()

        self.assertEqual(result, str(Path("a/../b/dev.db").resolve()))
        self.assertNotIn("..", result)

    def test_surrounding_whitespace_is_stripped_before_resolution(self):
        with patch.dict(
            "os.environ", {"DISCORD_TRADERS_DB_PATH": "   dev.db   "}, clear=False
        ):
            result = resolve_database_path()

        self.assertEqual(result, str(Path("dev.db").resolve()))

    def test_empty_override_falls_back_to_default(self):
        with patch.dict(
            "os.environ",
            {"DISCORD_TRADERS_DB_PATH": "", "LOCALAPPDATA": "C:/Users/tester/AppData/Local"},
            clear=False,
        ):
            result = resolve_database_path()

        self.assertEqual(
            result,
            str(Path("C:/Users/tester/AppData/Local") / "DiscordTraders" / "discord_traders.db"),
        )

    def test_whitespace_only_override_falls_back_to_default(self):
        with patch.dict(
            "os.environ",
            {
                "DISCORD_TRADERS_DB_PATH": "   ",
                "LOCALAPPDATA": "C:/Users/tester/AppData/Local",
            },
            clear=False,
        ):
            result = resolve_database_path()

        self.assertEqual(
            result,
            str(Path("C:/Users/tester/AppData/Local") / "DiscordTraders" / "discord_traders.db"),
        )


class ResolveDatabasePathDefaultTests(unittest.TestCase):
    """LOCALAPPDATA-based default and Path.home() fallback."""

    def test_default_resolves_to_exact_localappdata_path(self):
        env = dict(os.environ)
        env.pop("DISCORD_TRADERS_DB_PATH", None)
        env["LOCALAPPDATA"] = "C:/Users/tester/AppData/Local"
        with patch.dict("os.environ", env, clear=True):
            result = resolve_database_path()

        self.assertEqual(
            result,
            str(Path("C:/Users/tester/AppData/Local") / "DiscordTraders" / "discord_traders.db"),
        )

    def test_missing_localappdata_uses_home_fallback(self):
        env = dict(os.environ)
        env.pop("DISCORD_TRADERS_DB_PATH", None)
        env.pop("LOCALAPPDATA", None)
        fake_home = Path("C:/Users/tester")
        with patch.dict("os.environ", env, clear=True):
            with patch("database.config.Path.home", return_value=fake_home):
                result = resolve_database_path()

        self.assertEqual(
            result,
            str(fake_home / "AppData" / "Local" / "DiscordTraders" / "discord_traders.db"),
        )

    def test_empty_localappdata_uses_home_fallback(self):
        env = dict(os.environ)
        env.pop("DISCORD_TRADERS_DB_PATH", None)
        env["LOCALAPPDATA"] = ""
        fake_home = Path("C:/Users/tester")
        with patch.dict("os.environ", env, clear=True):
            with patch("database.config.Path.home", return_value=fake_home):
                result = resolve_database_path()

        self.assertEqual(
            result,
            str(fake_home / "AppData" / "Local" / "DiscordTraders" / "discord_traders.db"),
        )

    def test_whitespace_only_localappdata_uses_home_fallback(self):
        env = dict(os.environ)
        env.pop("DISCORD_TRADERS_DB_PATH", None)
        env["LOCALAPPDATA"] = "   "
        fake_home = Path("C:/Users/tester")
        with patch.dict("os.environ", env, clear=True):
            with patch("database.config.Path.home", return_value=fake_home):
                result = resolve_database_path()

        self.assertEqual(
            result,
            str(fake_home / "AppData" / "Local" / "DiscordTraders" / "discord_traders.db"),
        )

    def test_result_is_str_suitable_for_database_config(self):
        with patch.dict(
            "os.environ",
            {"LOCALAPPDATA": "C:/Users/tester/AppData/Local"},
            clear=False,
        ):
            result = resolve_database_path()

        self.assertIsInstance(result, str)
        config = DatabaseConfig(db_path=result)
        self.assertEqual(config.db_path, result)


if __name__ == "__main__":
    unittest.main()
