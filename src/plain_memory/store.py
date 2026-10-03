"""单数据库持久化；请求和全部切片原子提交。"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import numpy as np

from plain_memory.config import Settings


class RequestConflict(Exception):
    pass


class StoreMismatch(RuntimeError):
    pass


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(canonical(payload).encode()).hexdigest()


class Store:
    def __init__(self, settings: Settings):
        self.path = settings.database
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS add_requests (
                    user_id TEXT NOT NULL, request_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, messages_json TEXT NOT NULL,
                    committed_at TEXT NOT NULL, PRIMARY KEY (user_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    request_id TEXT NOT NULL, message_index INTEGER NOT NULL,
                    chunk_index INTEGER NOT NULL, role TEXT NOT NULL,
                    source_timestamp_ms INTEGER, content TEXT NOT NULL,
                    terms_json TEXT NOT NULL, embedding BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id, request_id)
                        REFERENCES add_requests(user_id, request_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS chunks_user ON chunks(user_id);
            """)
            expected = canonical(settings.store_identity())
            with conn:
                conn.execute("INSERT OR IGNORE INTO meta VALUES ('identity', ?)", (expected,))
                actual = conn.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0]
                if actual != expected:
                    raise StoreMismatch(
                        "Database model/chunk configuration differs; use a new database."
                    )
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _completed(conn, user_id: str, request_id: str, digest: str) -> bool:
        record = conn.execute(
            "SELECT payload_hash FROM add_requests WHERE user_id=? AND request_id=?",
            (user_id, request_id),
        ).fetchone()
        if record is not None and record[0] != digest:
            raise RequestConflict("request_id already exists with different content or session_id.")
        return record is not None

    def completed(self, payload: dict, digest: str) -> bool:
        with self.connection() as conn:
            return self._completed(conn, payload["user_id"], payload["request_id"], digest)

    def commit(self, payload: dict, digest: str, prepared: list[dict], vectors: np.ndarray | None):
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connection() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if self._completed(conn, payload["user_id"], payload["request_id"], digest):
                return
            if vectors is not None:
                dimension = str(vectors.shape[1])
                conn.execute("INSERT OR IGNORE INTO meta VALUES ('dimensions', ?)", (dimension,))
                stored_dimension = conn.execute(
                    "SELECT value FROM meta WHERE key='dimensions'"
                ).fetchone()[0]
                if stored_dimension != dimension:
                    raise StoreMismatch("Embedding dimension changed; use the configured model.")
            conn.execute(
                "INSERT INTO add_requests VALUES (?, ?, ?, ?, ?, ?)",
                (
                    payload["user_id"],
                    payload["request_id"],
                    payload["session_id"],
                    digest,
                    canonical(payload["messages"]),
                    now,
                ),
            )
            for index, chunk in enumerate(prepared):
                identity = [
                    payload["user_id"],
                    payload["session_id"],
                    payload["request_id"],
                    chunk["message_index"],
                    chunk["chunk_index"],
                ]
                chunk_id = "mem_" + hashlib.sha256(canonical(identity).encode()).hexdigest()
                timestamp = chunk["timestamp"]
                created_at = now
                if timestamp is not None:
                    # 输入已限制在可表示的 UTC 日期范围内。
                    source_date = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
                        milliseconds=timestamp
                    )
                    created_at = source_date.isoformat().replace("+00:00", "Z")
                conn.execute(
                    "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk_id,
                        payload["user_id"],
                        payload["session_id"],
                        payload["request_id"],
                        chunk["message_index"],
                        chunk["chunk_index"],
                        chunk["role"],
                        timestamp,
                        chunk["content"],
                        canonical(chunk["terms"]),
                        vectors[index].tobytes(),
                        created_at,
                    ),
                )

    def snapshot(self, user_id: str) -> list[dict]:
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM chunks WHERE user_id=? ORDER BY rowid", (user_id,)
                ).fetchall()
            ]

    def health(self):
        with self.connection() as conn:
            conn.execute("SELECT 1 FROM meta LIMIT 1").fetchone()

    def purge(self, user_id: str) -> int:
        with self.connection() as conn, conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("DELETE FROM add_requests WHERE user_id=?", (user_id,))
        # 停止服务后运维执行：清理 WAL 和未使用页；外部备份仍需另行清理。
        with self.connection() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return count
