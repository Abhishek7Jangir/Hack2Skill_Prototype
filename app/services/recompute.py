"""Debounced hotspot recompute.

request_recompute() is called after every complaint insert and status change. It
returns immediately. A single background worker waits RECOMPUTE_DEBOUNCE_SECONDS,
runs the hotspot job once for everything that arrived meanwhile, and repeats while
new requests keep coming. Across multiple processes the job is serialised by a
Postgres advisory lock inside run_hotspot_job().

Render free tier note: the instance sleeps after ~15 min idle; a pending recompute is
at most a few seconds old, and POST /api/v1/admin/recompute forces one on demand.
"""
import logging
import threading
import time

from ..config import settings
from ..db import get_conn
from .scoring import run_hotspot_job

log = logging.getLogger(__name__)

_lock = threading.Lock()
_dirty = False
_running = False
last_result: dict | None = None
last_error: str | None = None


def request_recompute() -> None:
    global _dirty, _running
    with _lock:
        _dirty = True
        if _running:
            return
        _running = True
    threading.Thread(target=_worker, name="hotspot-recompute", daemon=True).start()


def _worker() -> None:
    global _dirty, _running
    while True:
        time.sleep(settings.recompute_debounce_seconds)
        with _lock:
            if not _dirty:
                _running = False
                return
            _dirty = False
        try:
            run_now()
        except Exception:  # already logged; keep the worker alive for the next request
            pass


def run_now() -> dict:
    """Run the job synchronously (used by the worker and by POST /admin/recompute)."""
    global last_result, last_error
    try:
        with get_conn() as conn:
            result = run_hotspot_job(conn)
        last_result, last_error = {**result, "finished_at": time.time()}, None
        return result
    except Exception as exc:
        last_error = f"{type(exc).__name__}: {exc}"
        log.exception("Hotspot recompute failed")
        raise
