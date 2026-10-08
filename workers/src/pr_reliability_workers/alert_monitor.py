"""Periodic operational checks with a private, fixed-label Prometheus failure probe."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import psycopg

from .operation_alerts import deliver_alerts, evaluate_alerts, probe_host


class CheckHealth:
    def __init__(self):
        self.last_success = 0.0

    def metrics(self) -> bytes:
        return (
            "# TYPE pr_operations_alert_last_success_seconds gauge\n"
            f"pr_operations_alert_last_success_seconds {self.last_success}\n"
        ).encode("ascii")


def serve_health(health: CheckHealth, port=9108):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            body = health.metrics()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass  # Never log incoming paths or identifiers.

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def check_once(store, owner_id, settings, token, health):
    try:
        snapshot = store.snapshot(owner_id, settings.queue)
        codes = evaluate_alerts(snapshot, settings.stuck_seconds) | probe_host(settings)
    except (psycopg.Error, ConnectionError, TimeoutError):
        # Try the approved receiver even when the application DB cannot be read.
        deliver_alerts(settings, {"alert_check_unavailable"}, token)
        return False
    deliver_alerts(settings, codes, token)
    health.last_success = time.time()
    return True


def run_monitor(store, owner_id, settings, token, interval=30, *, stop=None, health=None):
    health = health or CheckHealth()
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            check_once(store, owner_id, settings, token, health)
        except (OSError, ValueError, psycopg.Error, httpx.HTTPError, RuntimeError):
            # Failed delivery/DB checks never advance the success gauge. Prometheus
            # independently alerts on a stale gauge or an unreachable monitor.
            pass
        stop.wait(interval)
