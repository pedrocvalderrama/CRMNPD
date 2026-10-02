"""Logging estruturado em uma linha por evento."""

from __future__ import annotations

import logging
import os
import sys


def configurar_logging(nivel: str | None = None) -> None:
    """Configura o logger raiz do CRM.

    Args:
        nivel: Nome do nivel (INFO, DEBUG, WARNING). None le CRM_NPD_LOG_LEVEL.
    """
    escolhido = (nivel or os.environ.get("CRM_NPD_LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, escolhido, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
