"""Utilidades de logging compartidas por el bootstrap de la app."""

from __future__ import annotations

import logging


class SinRuidoDeHealthCheck(logging.Filter):
    """Descarta el access log de uvicorn para ``/health``."""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access llama a logger.info(fmt, client_addr, method,
        # full_path, http_version, status_code) — full_path es args[2].
        return not (record.args and len(record.args) >= 3 and record.args[2] == "/health")
