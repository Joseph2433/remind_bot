from __future__ import annotations

import logging


def configure_logging(level: str) -> None:
    """Configure the process-wide log format used by CLI and daemon entrypoints."""

    logging.basicConfig(
        level=level.upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
