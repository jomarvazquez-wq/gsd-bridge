"""Structured logging for GSD Bridge.

Usage:
    from gsd_bridge.log import get_logger, configure_logging, new_run_id

    # In cli.main(), before dispatch:
    run_id = new_run_id()
    configure_logging(run_id=run_id, command=args.command)
    log = get_logger(__name__)
    log.debug("state_read", extra={"state_path": str(state_path)})

Environment variables:
    GSD_BRIDGE_DEBUG=1           Enable DEBUG level
    LOG_LEVEL=debug              Alternate debug toggle
    GSD_BRIDGE_LOG_FORMAT=json   Switch to JSON format (default: human)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

_run_id: str = ""
_command: str = ""

# Fields that belong to LogRecord internals and should not be merged into JSON output.
_LOGRECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
    | {"message", "asctime", "relativeCreated", "taskName"}
)


def new_run_id() -> str:
    """Generate a short correlation ID for one CLI invocation."""
    return uuid.uuid4().hex[:12]


def get_run_id() -> str:
    """Return the current invocation's run_id."""
    return _run_id


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "run_id": _run_id,
            "command": _command,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in record.__dict__:
            if key not in _LOGRECORD_ATTRS:
                payload[key] = record.__dict__[key]
        if record.exc_info and record.exc_info[1] is not None:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _HumanFormatter(logging.Formatter):
    """Compact human-readable format for terminal output."""

    _LEVEL_PREFIX = {
        "DEBUG": "DBG",
        "INFO": "INF",
        "WARNING": "WRN",
        "ERROR": "ERR",
        "CRITICAL": "CRT",
    }

    def format(self, record: logging.LogRecord) -> str:
        prefix = self._LEVEL_PREFIX.get(record.levelname, record.levelname[:3])
        msg = record.getMessage()
        if _run_id:
            return f"[{prefix}] {msg} (run={_run_id[:8]})"
        return f"[{prefix}] {msg}"


def configure_logging(*, run_id: str = "", command: str = "") -> None:
    """Initialize logging for one CLI invocation. Call once in cli.main()."""
    global _run_id, _command  # noqa: PLW0603
    _run_id = run_id or new_run_id()
    _command = command

    debug_mode = (
        os.getenv("GSD_BRIDGE_DEBUG", "").strip() in {"1", "true", "yes"}
        or os.getenv("LOG_LEVEL", "").strip().lower() == "debug"
    )
    level = logging.DEBUG if debug_mode else logging.WARNING

    use_json = os.getenv("GSD_BRIDGE_LOG_FORMAT", "").strip().lower() == "json"
    formatter: logging.Formatter = _JsonFormatter() if use_json else _HumanFormatter()

    logger = logging.getLogger("gsd_bridge")
    logger.setLevel(level)
    logger.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Get a child logger under the gsd_bridge namespace."""
    if not name.startswith("gsd_bridge"):
        name = f"gsd_bridge.{name}"
    return logging.getLogger(name)
