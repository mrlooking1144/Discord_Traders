"""Standard-library operational logging configuration for Discord Traders.

Milestone 2D.2: two independently idempotent handler-configuration
functions (console, file) attached to the "discord_traders" logger tree,
a log-path resolver fully independent of database.config's database-path
resolution, and sanitized diagnostic helpers that never expose exception
message text, absolute source paths, or sensitive application data
(raw message text, trader identifiers, parsed signal values, database or
log paths, environment values, SQL values).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import traceback
from pathlib import Path

_LOGGER_NAME = "discord_traders"
_CONSOLE_HANDLER_MARKER = "_discord_traders_console_handler"
_FILE_HANDLER_MARKER = "_discord_traders_file_handler"
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_FILE_LOGGING_UNAVAILABLE_MESSAGE = (
    "File logging unavailable; continuing with console logging only."
)


def configure_console_logging() -> None:
    """Attach exactly one console handler to the discord_traders logger.

    Idempotent: identifies its own handler via a private marker attribute
    (not a generic "any handler exists" check), so repeated calls -
    including across Streamlit reruns, which re-execute app.py's module
    body - never attach a second console handler.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    if any(
        getattr(handler, _CONSOLE_HANDLER_MARKER, False)
        for handler in logger.handlers
    ):
        return

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    setattr(handler, _CONSOLE_HANDLER_MARKER, True)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def configure_file_logging() -> None:
    """Attach exactly one rotating file handler to the discord_traders logger.

    Always calls configure_console_logging() first, so console fallback
    is guaranteed even when this function is invoked independently.
    Idempotent via its own private marker attribute, checked independently
    of the console handler's marker, so an existing console handler never
    blocks file-handler attachment and an existing file handler is never
    duplicated.

    Never propagates an exception: any failure during log-path resolution,
    directory creation, handler construction, formatter configuration, or
    handler attachment is caught (Exception, not BaseException), any
    partially constructed handler is closed and discarded rather than left
    attached, and exactly one fixed warning - with no exception or path
    interpolated into it - is emitted through the console handler.
    """
    configure_console_logging()

    logger = logging.getLogger(_LOGGER_NAME)
    if any(
        getattr(handler, _FILE_HANDLER_MARKER, False) for handler in logger.handlers
    ):
        return

    handler = None
    try:
        log_path = resolve_log_path()
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
            mode="a",
            delay=True,
        )
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
        setattr(handler, _FILE_HANDLER_MARKER, True)
        logger.addHandler(handler)
    except Exception:
        if handler is not None:
            try:
                handler.close()
            except Exception:
                pass
        logger.warning(_FILE_LOGGING_UNAVAILABLE_MESSAGE)


def resolve_log_path() -> str:
    """Resolve the runtime log-file path from the environment.

    Fully independent of database.config.resolve_database_path() and
    DISCORD_TRADERS_DB_PATH - neither is read or called here. Precedence:
        1. DISCORD_TRADERS_LOG_PATH, if set to a non-empty value after
           stripping surrounding whitespace. Normalized via
           Path(value).expanduser().resolve().
        2. Path(LOCALAPPDATA) / "DiscordTraders" / "logs" /
           "discord_traders.log", if LOCALAPPDATA is set to a non-empty
           value after stripping surrounding whitespace.
        3. Path.home() / "AppData" / "Local" / "DiscordTraders" / "logs" /
           "discord_traders.log", used only when LOCALAPPDATA is missing
           or empty/whitespace-only.

    Returns:
        The resolved log-file path as a string.
    """
    override = os.environ.get("DISCORD_TRADERS_LOG_PATH", "").strip()
    if override:
        return str(Path(override).expanduser().resolve())

    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        return str(Path(local_appdata) / "DiscordTraders" / "logs" / "discord_traders.log")

    return str(
        Path.home() / "AppData" / "Local" / "DiscordTraders" / "logs" / "discord_traders.log"
    )


def sanitized_traceback(exc: BaseException) -> str:
    """Format a traceback preserving frame identity, never sensitive detail.

    Retains, per frame, only: the source file's basename (never the
    absolute path or any parent directory component), the line number,
    the function name, and the static source-code line (the literal text
    as written in the .py file, never an evaluated runtime value).
    Appends only the exception's class name - never str(exc) or repr(exc).

    Args:
        exc: The exception to format a sanitized traceback for.

    Returns:
        A sanitized, multi-line traceback string.
    """
    lines: list[str] = []
    for frame in traceback.extract_tb(exc.__traceback__):
        filename = Path(frame.filename).name
        lines.append(f'  File "{filename}", line {frame.lineno}, in {frame.name}\n')
        if frame.line:
            lines.append(f"    {frame.line}\n")
    lines.append(f"{type(exc).__name__}\n")
    return "".join(lines)


def log_operation_failure(logger: logging.Logger, operation: str, exc: BaseException) -> None:
    """Log an operational failure at ERROR with only safe, sanitized detail.

    Logs a fixed, developer-authored operation label and the exception's
    class name, followed by a sanitized traceback. Never logs str(exc),
    repr(exc), or uses logger.exception()/exc_info=True - callers must
    never pass anything derived from user input as `operation`.

    Args:
        logger: The logger to record the failure on.
        operation: A fixed, developer-authored description of what was
            being attempted (e.g. "message submission"), never built
            from user input.
        exc: The exception that caused the failure.
    """
    logger.error("%s failed (%s)", operation, type(exc).__name__)
    logger.error(sanitized_traceback(exc))
