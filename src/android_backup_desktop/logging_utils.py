from __future__ import annotations

import logging
from pathlib import Path


LOG_FILE = Path("backup.log")
_CONFIGURED = False


def configure_file_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )

    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    _CONFIGURED = True
