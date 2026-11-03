import asyncio
import json
import time

import httpx
import pytest
import regex
from fastapi.testclient import TestClient

from plain_memory.api import create_app
from plain_memory.config import LLMConfig, RegexSearchConfig
from plain_memory.llm import Completion, LLMError, NoneLinearLLM
from plain_memory.regex_search import RegexRetriever, compile_plan, ordered_matches
from plain_memory.retrieval import interleave_routes, merge_results


class Planner:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def complete(self, messages, *, deadline):
        self.calls.append((messages, deadline))
        result = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        if isinstance(result, Exception):
            raise result
        return Completion(content=result, model="gpt-4o-mini", usage={})

    def close(self):
        pass


def plan(*patterns):
    return json.dumps({"patterns": list(patterns)})


def run(planner, *, rows=None, top_k=100, deadline=None, config=None, private_trace=None):
    trace = {}
    result = RegexRetriever(config or RegexSearchConfig(), planner).search(
        rows or [{"id": "one", "content": "orchid PRIVATE_CORPUS"}],
        "When did Orchid move?",
        top_k,
        deadline or time.monotonic() + 30,
        trace,
        private_trace=private_trace,
    )
    return result, trace


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"patterns":[]}',
        '{"patterns":[1]}',
        plan("["),
        plan(".*"),
        plan("x" * 257),
        plan("a", "b", "c", "d", "e", "f"),
        '{"patterns":["orchid"],"answer":"not allowed"}',
    ],
)
def test_invalid_plans_are_rejected(content):
    with pytest.raises((ValueError, regex.error)):
        compile_plan(content, 5)


def test_ordered_patterns_deduplicate_without_reordering():
    patterns, _ = compile_plan(plan("Orchid", r"202[0-9]", "Orchid"), 5)
    assert patterns == ["Orchid", r"202[0-9]"]


def test_five_pattern_quotas_and_three_route_limits_for_top_100():
    rows = [
        {"id": f"r{group}-{i}", "content": f"group-{group} item {i}"}
        for i in range(10)
        for group in range(5)
    ]
    patterns, compiled = compile_plan(plan(*[f"group-{g}" for g in range(5)]), 5)
    ids, per_pattern = ordered_matches(rows, compiled, 100, time.monotonic() + 5)
    assert len(patterns) == 5
    assert list(map(len, per_pattern)) == [6] * 5
    assert len(ids) == 30 and ids[:5] == [f"r{g}-0" for g in range(5)]
    assert ids[-5:] == [f"r{g}-5" for g in range(5)]


def test_regex_overlap_is_deduplicated_without_refilling_any_pattern():
    rows = [
        {"id": "a", "content": "alpha beta"},
        {"id": "b", "content": "alpha"},
        {"id": "c", "content": "beta"},
        {"id": "d", "content": "alpha beta"},
    ]
    _, compiled = compile_plan(plan("alpha", "beta"), 5)
    ids, groups = ordered_matches(rows, compiled, 15, time.monotonic() + 5)
    assert groups == [["a", "b"], ["a", "c"]]
    assert ids == ["a", "b", "c"]  # 'd' cannot refill duplicates.


def test_three_route_order_caps_and_no_cross_route_refill():
    ids = [f"{prefix}{i}" for prefix in ("v", "b", "r") for i in range(6)]
    rows = [
        {
            "id": i,
            "role": "user",
            "source_timestamp_ms": None,
            "created_at": "2026-01-01T00:00:00Z",
            "content": i,
        }
        for i in ids
    ]
    rankings = {
        route: [{"id": f"{prefix}{i}", "rank": i + 1, "score": 1} for i in range(6)]
        for route, prefix in (("vector", "v"), ("bm25", "b"))
    }
    trace = {}
    result = merge_results(rows, rankings, 10, regex_ids=[f"r{i}" for i in range(6)], trace=trace)
    assert [item["id"] for item in result] == [f"{p}{i}" for i in range(3) for p in "vbr"]
    assert trace["route_limit"] == 3 and len(result) == 9
    result = merge_results(rows, rankings, 9, regex_ids=["v0", "b0", "r0"])
    assert [item["id"] for item in result] == ["v0", "b0", "v1", "b1", "v2", "b2", "r0"]
    assert len(merge_results(rows, rankings, 10, regex_ids=None)) == 10
    assert interleave_routes([["a"], ["b"]], 0) == []


def test_prompt_sends_query_only_and_patterns_are_private():
    planner = Planner(plan("orchid"))
    private = {}
    result, trace = run(planner, private_trace=private)
    assert result == ["one"] and trace["status"] == "ok"
    assert "PRIVATE_CORPUS" not in json.dumps(planner.calls)
    assert "When did Orchid move?" in json.dumps(planner.calls)
    assert "orchid" not in json.dumps(trace)
    assert private["regex_plan"]["patterns"] == ["orchid"]


def test_transient_failure_retries_then_succeeds_and_deadlines_are_bounded():
    planner = Planner(LLMError("upstream_timeout"), plan("orchid"))
    result, trace = run(planner)
    assert result == ["one"] and trace["attempts"] == 2
    assert trace["reason"] is None
    for _, deadline in planner.calls:
        assert deadline <= time.monotonic() + 8


def test_bad_json_retries_then_succeeds():
    result, trace = run(Planner("not json", plan("orchid")))
    assert result == ["one"] and trace["attempts"] == 2


def test_rate_limit_retries_but_authentication_failure_does_not():
    result, trace = run(Planner(LLMError("upstream_http", 429), plan("orchid")))
    assert result == ["one"] and trace["attempts"] == 2
    result, trace = run(Planner(LLMError("upstream_http", 401)))
    assert result is None and trace["attempts"] == 1 and trace["upstream_status"] == 401


def test_final_failure_budget_exhaustion_and_disabled_mode_fall_back():
    result, trace = run(Planner(LLMError("upstream_timeout")))
    assert result is None and trace["attempts"] == 2
    planner = Planner(plan("orchid"))
    for kwargs in (
        {"deadline": time.monotonic() + 0.5},
        {"top_k": 2},
        {"config": RegexSearchConfig(enabled=False)},
    ):
        result, trace = run(planner, **kwargs)
        assert result is None and trace["attempts"] == 0
    assert not planner.calls


def test_no_regex_matches_keeps_three_route_caps():
    result, trace = run(Planner(plan("nonexistent")))
    assert result == [] and trace["status"] == "ok"


def test_regex_matching_is_time_bounded_and_falls_back(monkeypatch):
    _, expressions = compile_plan(plan(r"(a+)+$"), 5)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        ordered_matches(
            [{"id": "one", "content": "a" * 50000 + "!"}],
            expressions,
            100,
            time.monotonic() + 0.005,
        )
    assert time.monotonic() - start < 1

    def expired(*args):
        raise TimeoutError

    monkeypatch.setattr("plain_memory.regex_search.ordered_matches", expired)
    result, trace = run(Planner(plan("orchid")))
    assert result is None and trace["reason"] == "matching_timeout_or_invalid_pattern"


def test_api_three_routes_and_user_isolation(settings, fake, headers):
    settings.regex_search = RegexSearchConfig()
    planner = Planner(plan("orchid", "schedule"))
    app = create_app(settings, embedder=fake, llm=planner)
    with TestClient(app) as client:
        for user, text in (("one", "Orchid schedule evidence"), ("other", "PRIVATE_OTHER_USER")):
            response = client.post(
                "/add",
                headers=headers,
                json={
                    "request_id": "request",
                    "user_id": user,
                    "session_id": "session",
                    "messages": [{"role": "user", "content": f"{text} {i}"} for i in range(20)],
                },
            )
            assert response.status_code == 200
        result = client.post(
            "/search",
            headers=headers,
            json={
                "user_id": "one",
                "query": "orchid schedule",
                "top_k": 12,
            },
        )
        assert result.status_code == 200
        assert 1 <= len(result.json()["data"]) <= 12
        assert "PRIVATE_OTHER_USER" not in result.text
        assert "PRIVATE_OTHER_USER" not in json.dumps(planner.calls)
        import sqlite3

        with sqlite3.connect(settings.diagnostics.database) as conn:
            raw = conn.execute(
                "SELECT metadata_json FROM events WHERE "
                "json_extract(metadata_json,'$.operation')='search'"
            ).fetchone()[0]
        event = json.loads(raw)
        assert event["regex_search"]["status"] == "ok"
        assert event["regex_search"]["per_pattern_limit"] == 2
        assert len(event["retrieval"]["vector"]) == 4
        assert len(event["retrieval"]["bm25"]) == 4
        assert len(event["retrieval"]["regex_ids"]) <= 4
        assert "orchid" not in raw
        tiny = client.post(
            "/search",
            headers=headers,
            json={
                "user_id": "one",
                "query": "orchid schedule",
                "top_k": 1,
            },
        )
        assert len(tiny.json()["data"]) == 1 and len(planner.calls) == 1


def test_api_llm_failure_returns_full_baseline_capacity(settings, fake, headers):
    settings.regex_search = RegexSearchConfig()
    app = create_app(settings, embedder=fake, llm=Planner(LLMError("upstream_http", 503)))
    with TestClient(app) as client:
        assert (
            client.post(
                "/add",
                headers=headers,
                json={
                    "request_id": "r",
                    "session_id": "s",
                    "user_id": "u",
                    "messages": [
                        {"role": "user", "content": f"orchid item {i}"} for i in range(20)
                    ],
                },
            ).status_code
            == 200
        )
        response = client.post(
            "/search",
            headers=headers,
            json={
                "user_id": "u",
                "query": "orchid",
                "top_k": 12,
            },
        )
        assert response.status_code == 200 and len(response.json()["data"]) == 12


def test_api_timeouts_cancel_both_attempts_and_return_baseline(settings, fake, headers):
    cancelled = []

    async def stalled(request):
        try:
            await asyncio.sleep(2)
        finally:
            cancelled.append(True)

    settings.regex_search = RegexSearchConfig(attempt_timeout_seconds=0.03, timeout_seconds=1)
    llm = NoneLinearLLM(LLMConfig(api_key="synthetic-key"), transport=httpx.MockTransport(stalled))
    with TestClient(create_app(settings, embedder=fake, llm=llm)) as client:
        assert (
            client.post(
                "/add",
                headers=headers,
                json={
                    "request_id": "r",
                    "session_id": "s",
                    "user_id": "u",
                    "messages": [{"role": "user", "content": "orchid evidence"}],
                },
            ).status_code
            == 200
        )
        start = time.monotonic()
        response = client.post(
            "/search",
            headers=headers,
            json={
                "user_id": "u",
                "query": "orchid",
                "top_k": 100,
            },
        )
        assert response.status_code == 200 and response.json()["data"]
        assert time.monotonic() - start < 1.5
        assert cancelled == [True, True]
