import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from fastapi.testclient import TestClient

from plain_memory.api import create_app
from plain_memory.embedding import EmbeddingError


def memory(user="run:1:user:1", session="same-session", request="req-1", text="Orchid garden"):
    return {
        "request_id": request,
        "user_id": user,
        "session_id": session,
        "messages": [{"role": "user", "content": text, "timestamp": 1704067200000}],
    }


def search(client, headers, user="run:1:user:1", query="orchid", top_k=100, **extra):
    return client.post(
        "/search", headers=headers, json={"query": query, "user_id": user, "top_k": top_k, **extra}
    )


def test_sync_retry_restart_and_timestamps(settings, fake, headers):
    app = create_app(settings, embedder=fake)
    with TestClient(app) as client:
        payload = memory()
        response = client.post("/add", headers=headers, json=payload)
        assert response.json() == {
            "success": True,
            "request_id": "req-1",
            "user_id": "run:1:user:1",
            "session_id": "same-session",
        }
        result = search(client, headers).json()["data"]
        assert len(result) == 1 and "Orchid garden" in result[0]["content"]
        assert result[0]["created_at"] == "2024-01-01T00:00:00Z"
        assert "score" not in result[0]
        calls = len(fake.calls)
        assert client.post("/add", headers=headers, json=payload).status_code == 200
        assert len(fake.calls) == calls
    with TestClient(create_app(settings, embedder=fake)) as client:
        assert search(client, headers).json()["data"] == result
        assert client.post("/add", headers=headers, json=payload).status_code == 200
        payload["session_id"] = "different-session"
        assert client.post("/add", headers=headers, json=payload).status_code == 422


def test_user_run_isolation_and_cross_session(settings, fake, headers):
    with TestClient(create_app(settings, embedder=fake)) as client:
        for payload in (
            memory(text="orchid alpha"),
            memory(session="other-session", request="req-2", text="orchid beta"),
            memory(user="run:1:user:2", text="private cactus"),
            memory(user="run:2:user:1", text="other run cedar"),
        ):
            assert client.post("/add", headers=headers, json=payload).status_code == 200
        hits = search(client, headers).json()["data"]
        assert len(hits) == 2
        assert all("orchid" in item["content"] for item in hits)
        assert len({item["id"] for item in hits}) == 2
        calls = len(fake.calls)
        assert search(client, headers, user="unknown-user").json() == {"data": []}
        assert len(fake.calls) == calls


def test_options_top_k_and_message_order(settings, fake, headers):
    app = create_app(settings, embedder=fake)
    with TestClient(app) as client:
        payload = memory()
        payload["messages"] = [
            {"role": "assistant", "content": "alpha orchid", "timestamp": 2000},
            {"role": "user", "content": "beta orchid", "timestamp": 1000},
        ]
        assert client.post("/add", headers=headers, json=payload).status_code == 200
        assert fake.calls[0] == ["alpha orchid", "beta orchid"]
        response = search(client, headers, top_k=1, options=["A. orchid", "B. cactus"])
        assert response.status_code == 200 and len(response.json()["data"]) == 1
        assert fake.calls[-1] == ["orchid\nOptions:\nA. orchid\nB. cactus"]
        assert len(search(client, headers, top_k=100).json()["data"]) == 2
        rows = app.state.store.snapshot(payload["user_id"])
        assert [row["source_timestamp_ms"] for row in rows] == [2000, 1000]


def test_auth_health_invalid_payload_and_privacy(settings, fake, headers):
    with TestClient(create_app(settings, embedder=fake)) as client:
        for path in ("/health", "/", "/add", "/search"):
            assert client.get(path).status_code == 200
        assert client.post("/add", json=memory()).status_code == 401
        assert (
            client.post(
                "/add", headers={"Authorization": "Bearer wrong"}, json=memory()
            ).status_code
            == 401
        )
        payload = memory()
        payload["messages"][0]["content"] = {"private": "DO_NOT_ECHO_THIS"}
        response = client.post("/add", headers=headers, json=payload)
        assert response.status_code == 422 and "DO_NOT_ECHO_THIS" not in response.text
        assert search(client, headers, top_k="100").status_code == 422
        assert search(client, headers, query=" ").status_code == 422
        assert search(client, headers, top_k=0).status_code == 422
        assert search(client, headers, options=[123]).status_code == 422


def test_body_limit_including_streamed_body(settings, fake, headers):
    settings.max_body_bytes = 128
    with TestClient(create_app(settings, embedder=fake)) as client:
        response = client.post(
            "/add",
            headers=headers,
            content=iter([b'{"messages": "', b"x" * 100, b"x" * 100, b'"}']),
        )
        assert response.status_code == 413 and not fake.calls


def test_whitespace_messages_remain_durable_without_empty_search_hits(settings, fake, headers):
    app = create_app(settings, embedder=fake)
    with TestClient(app) as client:
        payload = memory(text=" \n\t")
        assert client.post("/add", headers=headers, json=payload).status_code == 200
        assert search(client, headers).json() == {"data": []}
        assert not fake.calls
        with app.state.store.connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM add_requests").fetchone()[0] == 1


def test_embedding_failure_is_atomic_then_retry_succeeds(settings, fake, headers):
    original = fake.embed

    def failure(texts, deadline):
        raise EmbeddingError("Synthetic upstream failure")

    fake.embed = failure
    app = create_app(settings, embedder=fake)
    with TestClient(app) as client:
        payload = memory()
        assert client.post("/add", headers=headers, json=payload).status_code == 503
        assert search(client, headers).json() == {"data": []}
        with app.state.store.connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM add_requests").fetchone()[0] == 0
        fake.embed = original
        assert client.post("/add", headers=headers, json=payload).status_code == 200


def test_database_failure_rolls_back_requests_chunks_and_dimension(settings, fake, headers):
    settings.chunk_tokens, settings.overlap_tokens = 8, 2
    app = create_app(settings, embedder=fake)
    with TestClient(app) as client:
        with app.state.store.connection() as conn:
            conn.execute("""CREATE TRIGGER synthetic_failure BEFORE INSERT ON chunks
                            WHEN NEW.chunk_index=1 BEGIN SELECT RAISE(ABORT, 'synthetic'); END""")
        payload = memory(text="orchid " * 30)
        assert client.post("/add", headers=headers, json=payload).status_code == 503
        with app.state.store.connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM add_requests").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
            assert (
                conn.execute("SELECT COUNT(*) FROM meta WHERE key='dimensions'").fetchone()[0] == 0
            )
            conn.execute("DROP TRIGGER synthetic_failure")
        assert client.post("/add", headers=headers, json=payload).status_code == 200


def test_changed_dimension_fails_without_partial_write(settings, fake, headers):
    app = create_app(settings, embedder=fake)
    with TestClient(app) as client:
        assert client.post("/add", headers=headers, json=memory()).status_code == 200
        fake.embed = lambda texts, deadline: np.ones((len(texts), 8))
        assert client.post("/add", headers=headers, json=memory(request="req-2")).status_code == 503
        assert len(app.state.store.snapshot("run:1:user:1")) == 1
        assert search(client, headers).status_code == 503


def test_concurrent_add_overload_and_completed_retry(settings, fake, headers):
    entered, release = threading.Event(), threading.Event()
    original = fake.embed

    def block(texts, deadline):
        entered.set()
        assert release.wait(5)
        return original(texts, deadline)

    fake.embed = block
    app = create_app(settings, embedder=fake)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(client.post, "/add", headers=headers, json=memory())
        assert entered.wait(5)
        try:
            overload = client.post("/add", headers=headers, json=memory())
            assert overload.status_code == 429 and overload.headers["retry-after"] == "1"
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 200
        assert client.post("/add", headers=headers, json=memory()).status_code == 200
        assert len(app.state.store.snapshot("run:1:user:1")) == 1


def test_concurrent_same_request_commits_once(settings, fake, headers):
    settings.add_concurrency = 2
    barrier = threading.Barrier(2)
    original = fake.embed

    def both(texts, deadline):
        barrier.wait(timeout=5)
        return original(texts, deadline)

    fake.embed = both
    app = create_app(settings, embedder=fake)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(client.post, "/add", headers=headers, json=memory()) for _ in range(2)
        ]
        assert [future.result(timeout=10).status_code for future in futures] == [200, 200]
        assert len(app.state.store.snapshot("run:1:user:1")) == 1


def test_expired_add_never_commits(settings, fake, headers):
    settings.add_timeout_seconds = 0.001
    original = fake.embed

    def slow(texts, deadline):
        time.sleep(0.01)
        return original(texts, deadline)

    fake.embed = slow
    app = create_app(settings, embedder=fake)
    with TestClient(app) as client:
        assert client.post("/add", headers=headers, json=memory()).status_code == 503
        assert app.state.store.snapshot("run:1:user:1") == []


def test_search_overload_releases_capacity(settings, fake, headers):
    settings.search_concurrency = 1
    entered, release = threading.Event(), threading.Event()
    app = create_app(settings, embedder=fake)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        assert client.post("/add", headers=headers, json=memory()).status_code == 200
        original = fake.embed

        def block(texts, deadline):
            entered.set()
            assert release.wait(5)
            return original(texts, deadline)

        fake.embed = block
        pending = pool.submit(search, client, headers)
        assert entered.wait(5)
        try:
            assert search(client, headers).status_code == 429
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 200
        fake.embed = original
        assert search(client, headers).status_code == 200
