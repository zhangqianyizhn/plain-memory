import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from plain_memory.api import create_app
from plain_memory.config import DiagnosticsConfig, load_settings
from plain_memory.embedding import EmbeddingError


def payload(user="synthetic-user"):
    return {
        "user_id": user,
        "session_id": "session-A",
        "request_id": "request-A",
        "messages": [{"role": "user", "content": "PRIVATE orchid evidence"}],
    }


def events(settings):
    with sqlite3.connect(settings.diagnostics.database) as conn:
        return [
            (json.loads(meta), json.loads(content) if content else None)
            for meta, content in conn.execute(
                "SELECT metadata_json,content_json FROM events ORDER BY started_at"
            )
        ]


def enable_capture(settings):
    settings.diagnostics = DiagnosticsConfig(
        database=settings.diagnostics.database,
        capture_content=True,
        capture_until=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def test_default_metadata_does_not_record_text_or_credentials(settings, fake, headers):
    with TestClient(create_app(settings, embedder=fake)) as client:
        response = client.post("/add", json=payload(), headers=headers)
        assert response.status_code == 200
        trace_id = response.headers["x-plainmemory-trace-id"]
        response = client.post(
            "/search",
            headers=headers,
            json={
                "query": "PRIVATE QUERY",
                "user_id": "synthetic-user",
                "top_k": 100,
            },
        )
        assert response.status_code == 200
    rows = events(settings)
    assert rows[0][0]["event_id"] == trace_id
    assert rows[0][0]["chunk_count"] == 1
    assert rows[1][0]["snapshot_count"] == 1
    assert rows[1][0]["snapshot_max_rowid"] == 1
    assert rows[1][0]["retrieval"]["fused_ids"] == [response.json()["data"][0]["id"]]
    assert all(content is None for _, content in rows)
    raw = json.dumps(rows)
    for value in (
        "PRIVATE",
        "synthetic-user",
        "session-A",
        "request-A",
        settings.api_token.get_secret_value(),
        settings.embedding.api_key.get_secret_value(),
    ):
        assert value not in raw


def test_private_capture_has_request_response_ranks_and_retry(settings, fake, headers):
    enable_capture(settings)
    app = create_app(settings, embedder=fake)
    with TestClient(app) as client:
        assert client.post("/add", json=payload(), headers=headers).status_code == 200
        assert client.post("/add", json=payload(), headers=headers).status_code == 200
        query = {
            "query": "orchid",
            "options": ["A", "B"],
            "user_id": "synthetic-user",
            "top_k": 100,
        }
        response = client.post("/search", headers=headers, json=query)
    rows = events(settings)
    assert rows[0][1]["request"] == payload()
    assert rows[1][0]["idempotent"] is True
    assert rows[1][1]["response"]["success"] is True
    assert rows[2][1] == {"request": query, "response": response.json()}
    assert rows[2][0]["retrieval"]["vector"][0]["rank"] == 1
    assert rows[2][0]["retrieval"]["bm25"][0]["rank"] == 1
    assert settings.diagnostics.database.stat().st_mode & 0o777 == 0o600


def test_auth_validation_body_limit_and_upstream_failure(settings, fake, headers):
    enable_capture(settings)
    app = create_app(settings, embedder=fake)
    with TestClient(app) as client:
        assert client.post("/add", json=payload()).status_code == 401
        assert (
            client.post("/add", json={"PRIVATE_BAD": "SECRET"}, headers=headers).status_code == 422
        )

        def fail(*args):
            raise EmbeddingError("Synthetic failure")

        fake.embed = fail
        assert client.post("/add", json=payload(), headers=headers).status_code == 503
    rows = events(settings)
    assert [meta["status"] for meta, _ in rows] == [401, 422, 503]
    assert rows[0][1] is None and rows[1][1] is None
    assert rows[2][0]["error_kind"] == "EmbeddingError"
    assert "PRIVATE_BAD" not in json.dumps(rows)
    assert not app.state.store.snapshot("synthetic-user")
    settings.max_body_bytes = 16
    with TestClient(create_app(settings, embedder=fake)) as client:
        assert client.post("/add", content=b"x" * 17).status_code == 413
    assert events(settings)[-1][0]["status"] == 413


def test_capture_requires_expiry_and_stops_after_expiry(settings, fake, headers):
    with pytest.raises(ValidationError):
        DiagnosticsConfig(capture_content=True)
    enable_capture(settings)
    settings.diagnostics.capture_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    with TestClient(create_app(settings, embedder=fake)) as client:
        assert client.post("/add", json=payload(), headers=headers).status_code == 200
    assert events(settings)[0][1] is None


def test_diagnostic_disk_failure_does_not_change_committed_add(settings, fake, headers):
    app = create_app(settings, embedder=fake)
    app.state.diagnostics.path = Path("/dev/null/not-a-database")
    with TestClient(app) as client:
        assert client.post("/add", json=payload(), headers=headers).status_code == 200
    assert len(app.state.store.snapshot("synthetic-user")) == 1
    assert app.state.diagnostics.write_failures == 1


def test_concurrent_requests_keep_exact_user_records_and_purge(settings, fake, headers):
    enable_capture(settings)
    app = create_app(settings, embedder=fake)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(
                lambda i: client.post("/add", headers=headers, json=payload(f"user-{i}")),
                range(8),
            )
        )
        assert all(response.status_code == 200 for response in responses)
    rows = events(settings)
    assert len(rows) == 8 and len({meta["event_id"] for meta, _ in rows}) == 8
    assert {content["request"]["user_id"] for _, content in rows} == {f"user-{i}" for i in range(8)}
    app.state.diagnostics.purge("user-1")
    assert len(events(settings)) == 7
    assert all(content["request"]["user_id"] != "user-1" for _, content in events(settings))


def test_expired_records_pruned_and_disabled_creates_no_file(settings, fake, headers):
    app = create_app(settings, embedder=fake)
    with TestClient(app) as client:
        assert client.post("/add", json=payload(), headers=headers).status_code == 200
    with sqlite3.connect(settings.diagnostics.database) as conn:
        conn.execute("UPDATE events SET expires_at='2000-01-01T00:00:00+00:00'")
    app.state.diagnostics.cleanup()
    assert not events(settings)
    settings.diagnostics.enabled = False
    settings.diagnostics.database = settings.database.parent / "disabled.sqlite3"
    with TestClient(create_app(settings, embedder=fake)) as client:
        assert client.post("/add", json=payload(), headers=headers).status_code == 200
    assert not settings.diagnostics.database.exists()


def test_config_paths_resolved_and_same_database_rejected(tmp_path):
    import yaml

    path = tmp_path / "config.local.yaml"
    config = {
        "api_token": "synthetic-test-token-at-least-32-bytes",
        "database": "data/memory.sqlite3",
        "embedding": {
            "base_url": "https://test.invalid/v1",
            "model": "text-embedding-v4",
            "api_key": "synthetic-test-only",
        },
    }
    path.write_text(yaml.safe_dump(config))
    assert load_settings(path).diagnostics.database == tmp_path / "data/diagnostics.sqlite3"
    config["diagnostics"] = {"database": "data/memory.sqlite3"}
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError):
        load_settings(path)


def test_private_export_has_no_stdout_content_and_never_overwrites(settings, fake, headers):
    import yaml

    enable_capture(settings)
    with TestClient(create_app(settings, embedder=fake)) as client:
        assert client.post("/add", json=payload(), headers=headers).status_code == 200
    config_path = settings.database.parent / "config.local.yaml"
    serialized = settings.model_dump(mode="json")
    serialized["api_token"] = settings.api_token.get_secret_value()
    config_path.write_text(yaml.safe_dump(serialized))
    output = settings.database.parent / "private-export.jsonl"
    project = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        "scripts/inspect_diagnostics.py",
        "--config",
        str(config_path),
        "--output",
        str(output),
        "--include-content",
        "--user-id",
        "synthetic-user",
    ]
    run = subprocess.run(command, cwd=project, capture_output=True, text=True)
    assert run.returncode == 0
    assert json.loads(run.stdout)["content_records"] == 1
    assert "PRIVATE" not in run.stdout and "synthetic-user" not in run.stdout
    assert json.loads(output.read_text())["content"]["request"] == payload()
    assert output.stat().st_mode & 0o777 == 0o600
    original = output.read_bytes()
    assert subprocess.run(command, cwd=project, capture_output=True).returncode == 1
    assert output.read_bytes() == original
