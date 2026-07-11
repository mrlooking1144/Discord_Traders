"""Version 1 database configuration for Discord Traders.

Provides a single, overridable source of truth for database settings
so no other module hardcodes a database path or connection setting.
"""

from dataclasses import dataclass


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
