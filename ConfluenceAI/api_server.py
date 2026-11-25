"""Read-only JSON API + static dashboard server (stdlib only, zero extra dependencies).

Serves the installable PWA dashboard from ./web and exposes:
    GET /api/health
    GET /api/summary?days=7
    GET /api/trades?status=ACTIVE|PENDING|CLOSED&limit=50
    GET /api/equity?days=90
    GET /api/breakdown?days=90

The API never mutates state and never exposes secrets. When bound beyond localhost an
API_TOKEN is mandatory (enforced by config validation) and checked with a constant-time compare.
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from config import BotConfig
from database import TradeDatabase

logger = logging.getLogger(__name__)

WEB_ROOT = (Path(__file__).resolve().parent / "web").resolve()


def _clamp(value: str | None, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value))) if value is not None else default
    except ValueError:
        return default


def make_handler(cfg: BotConfig, db: TradeDatabase) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ConfluenceAI/2.0"

        # -- helpers -----------------------------------------------------
        def log_message(self, fmt: str, *args: object) -> None:  # route to our logger
            logger.debug("api: " + fmt, *args)

        def _authorized(self, query: dict[str, list[str]]) -> bool:
            if not cfg.api_token:
                return True
            supplied = ""
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
            elif "token" in query:
                supplied = query["token"][0]
            return hmac.compare_digest(supplied.encode(), cfg.api_token.encode())

        def _send_json(self, payload: object, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, rel: str) -> None:
            rel = rel.lstrip("/") or "index.html"
            target = (WEB_ROOT / rel).resolve()
            if WEB_ROOT not in target.parents and target != WEB_ROOT:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = target.read_bytes()
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if target.suffix == ".webmanifest":
                ctype = "application/manifest+json"
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8" if ctype.startswith("text/") or "json" in ctype or "javascript" in ctype else ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            # service worker must always be revalidated
            self.send_header("Cache-Control", "no-cache" if target.name == "sw.js" else "public, max-age=300")
            self.end_headers()
            self.wfile.write(data)

        # -- routes ------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            path = parsed.path

            if not path.startswith("/api/"):
                self._send_static(path)
                return

            if path == "/api/health":
                self._send_json({"ok": True})
                return

            if not self._authorized(query):
                self._send_json({"error": "unauthorized"}, 401)
                return

            try:
                if path == "/api/summary":
                    days = _clamp(query.get("days", [None])[0], 7, 1, 365)
                    self._send_json(
                        {
                            "stats": db.get_performance_stats(days=days),
                            "today": db.get_performance_stats(days=1),
                            "open_trades": db.count_open_trades(),
                        }
                    )
                elif path == "/api/trades":
                    status = (query.get("status", [None])[0] or "").upper() or None
                    limit = _clamp(query.get("limit", [None])[0], 50, 1, 500)
                    self._send_json(
                        {"trades": [t.to_dict() for t in db.get_recent_trades(limit, status)]}
                    )
                elif path == "/api/equity":
                    days = _clamp(query.get("days", [None])[0], 90, 1, 730)
                    self._send_json({"curve": db.get_equity_curve(days=days)})
                elif path == "/api/breakdown":
                    days = _clamp(query.get("days", [None])[0], 90, 1, 730)
                    self._send_json(db.get_breakdown(days=days))
                else:
                    self._send_json({"error": "not_found"}, 404)
            except Exception as exc:  # never leak internals to the client
                logger.exception("API error on %s: %s", path, exc)
                self._send_json({"error": "internal_error"}, 500)

    return Handler


def start_api_server(cfg: BotConfig, db: TradeDatabase) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((cfg.api_host, cfg.api_port), make_handler(cfg, db))
    thread = threading.Thread(target=server.serve_forever, name="api-server", daemon=True)
    thread.start()
    logger.info("Dashboard & API listening on http://%s:%d", cfg.api_host, cfg.api_port)
    return server
