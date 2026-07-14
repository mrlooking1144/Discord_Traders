"""Version 1 database configuration for Discord Traders.

Provides a single, overridable source of truth for database settings
so no other module hardcodes a database path or connection setting.
"""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DatabaseConfig:
    """Configuration values for the Version 1 SQLite database.

    Attributes:
        db_path: Filesystem path to the SQLite database file. Required,
            and must be supplied by the caller (no hidden default path).
        probable_duplicate_window_seconds: Time window, in seconds, used
            later by duplicate-detection logic to consider two trade
            signals as probable duplicates. Defaults to 86400 (24 hours).
        busy_timeout_seconds: Timeout, in seconds, used later when opening
            SQLite connections to wait on a locked database. Defaults to 5.0.
    """

    db_path: str
    probable_duplicate_window_seconds: int = 86400
    busy_timeout_seconds: float = 5.0


def resolve_database_path() -> str:
    """Resolve the runtime SQLite database path from the environment.

    Precedence:
        1. ``DISCORD_TRADERS_DB_PATH``, if set to a non-empty value after
           stripping surrounding whitespace. Normalized via
           ``Path(value).expanduser().resolve()`` so both absolute and
           relative overrides return a normalized absolute path; a
           relative override is interpreted relative to the current
           working directory at the moment this function runs.
        2. ``Path(LOCALAPPDATA) / "DiscordTraders" / "discord_traders.db"``,
           if ``LOCALAPPDATA`` is set to a non-empty value after stripping
           surrounding whitespace.
        3. ``Path.home() / "AppData" / "Local" / "DiscordTraders" /
           "discord_traders.db"``, used only when ``LOCALAPPDATA`` is
           missing or empty/whitespace-only.

    Returns:
        The resolved database path as a string, suitable for
        ``DatabaseConfig(db_path=...)``.
    """
    override = os.environ.get("DISCORD_TRADERS_DB_PATH", "").strip()
    if override:
        return str(Path(override).expanduser().resolve())

    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        return str(Path(local_appdata) / "DiscordTraders" / "discord_traders.db")

    return str(Path.home() / "AppData" / "Local" / "DiscordTraders" / "discord_traders.db")
