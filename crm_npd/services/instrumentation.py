from __future__ import annotations
import time
from flask import request, g
from ..config import SLOW_REQUEST_MS


def install_request_timing(app) -> None:
    @app.before_request
    def _mark_start():
        g._request_started_at = time.perf_counter()

    @app.after_request
    def _log_slow(response):
        started = getattr(g, '_request_started_at', None)
        if started is None:
            return response
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if elapsed_ms >= SLOW_REQUEST_MS:
            print(f"[CRM][LENTO] {request.method} {request.path} -> {elapsed_ms:.0f} ms")
        return response
