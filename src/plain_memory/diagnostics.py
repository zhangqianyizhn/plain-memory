"""Private, bounded-lifetime request diagnostics; never record HTTP headers or credentials."""

import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from anyio import to_thread

from plain_memory.config import Settings

logger = logging.getLogger(__name__)


class Diagnostics:
    def __init__(self, settings: Settings):
        self.config = settings.diagnostics
        self.key = settings.api_token.get_secret_value().encode()
        self.store_identity_hash = hashlib.sha256(
            json.dumps(settings.store_identity(), sort_keys=True).encode()
        ).hexdigest()
        self.lock = threading.Lock()
        self.last_cleanup = 0.0
        self.write_failures = 0
        if self.config.enabled:
            self.path = self.config.database
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self.connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, user_ref TEXT,
                    metadata_json TEXT NOT NULL, content_json TEXT
                )""")
                conn.execute("CREATE INDEX IF NOT EXISTS events_expiry ON events(expires_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS events_user ON events(user_ref)")
            self.path.chmod(0o600)
            self.cleanup()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=0.5)
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def ref(self, value: str) -> str:
        return hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()

    def capture_active(self) -> bool:
        return bool(
            self.config.enabled
            and self.config.capture_content
            and self.config.capture_until
            and datetime.now(timezone.utc) < self.config.capture_until
        )

    def cleanup(self):
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            deleted = conn.execute("DELETE FROM events WHERE expires_at <= ?", (now,)).rowcount
        if deleted:
            with self.connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.last_cleanup = time.monotonic()

    def prune(self):
        with self.lock:
            self.cleanup()

    def failed_write(self):
        self.write_failures += 1
        logger.error("Private diagnostics write failed; inspect local storage.")

    def record(self, event: dict):
        # Failure of optional diagnostics must never undo an already committed Add.
        try:
            with self.lock:
                if time.monotonic() - self.last_cleanup >= 60:
                    self.cleanup()
                metadata = {k: v for k, v in event.items() if k != "content"}
                content = event.get("content")
                expires = datetime.now(timezone.utc) + timedelta(days=self.config.retention_days)
                with self.connect() as conn:
                    conn.execute(
                        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            event["event_id"],
                            event["started_at"],
                            expires.isoformat(),
                            event.get("user_ref"),
                            json.dumps(metadata, ensure_ascii=False),
                            json.dumps(content, ensure_ascii=False)
                            if content is not None
                            else None,
                        ),
                    )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            self.failed_write()

    def purge(self, user_id: str):
        if self.config.enabled:
            with self.connect() as conn:
                conn.execute("DELETE FROM events WHERE user_ref=?", (self.ref(user_id),))
            with self.connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


class DiagnosticMiddleware:
    def __init__(self, app, recorder: Diagnostics):
        self.app, self.recorder = app, recorder

    async def __call__(self, scope, receive, send):
        if not self.recorder.config.enabled or scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["method"] != "POST" or scope["path"] not in {"/add", "/search"}:
            return await self.app(scope, receive, send)
        event = {
            "event_id": uuid.uuid4().hex,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "operation": scope["path"][1:],
            "method_version": "hybrid-interleave-v1",
            "store_identity_hash": self.recorder.store_identity_hash,
            "status": 500,
            "authenticated": False,
            "request_bytes": 0,
            "response_bytes": 0,
        }
        scope.setdefault("state", {})["diagnostic"] = event
        start = time.monotonic()

        async def counted_receive():
            message = await receive()
            event["request_bytes"] += len(message.get("body", b""))
            return message

        async def counted_send(message):
            if message["type"] == "http.response.start":
                event["status"] = message["status"]
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-plainmemory-trace-id", event["event_id"].encode())
                ]
            elif message["type"] == "http.response.body":
                event["response_bytes"] += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, counted_receive, counted_send)
        except Exception as exc:
            event["error_kind"] = type(exc).__name__
            raise
        finally:
            event["duration_ms"] = round((time.monotonic() - start) * 1000, 3)
            await to_thread.run_sync(self.recorder.record, event)
