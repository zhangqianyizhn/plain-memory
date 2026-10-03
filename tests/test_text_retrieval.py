import json

import numpy as np
import pytest

from plain_memory.api import create_app
from plain_memory.retrieval import interleave, retrieve
from plain_memory.store import StoreMismatch
from plain_memory.text import chunks, encoding, lexical_terms


def test_unicode_chunks_are_original_substrings_and_cover_message():
    original = "".join(f"中文👩🏽‍💻 café agent第{i}条记忆测试。" for i in range(20))
    fragments = chunks(original, 32, 4)
    covered = 0
    previous_start = -1
    for fragment in fragments:
        start = original.find(fragment, previous_start + 1)
        assert start >= 0 and start <= covered
        assert "\ufffd" not in fragment
        assert len(encoding().encode(fragment)) <= 32
        covered = max(covered, start + len(fragment))
        previous_start = start
    assert covered == len(original)
    assert chunks("short 中文", 512, 64) == ["short 中文"]
    assert chunks("literal <|endoftext|>", 512, 64) == ["literal <|endoftext|>"]


def test_no_overlap_covers_exact_original():
    text = "中英文 testing 🤖 " * 20
    assert "".join(chunks(text, 16, 0)) == text


def test_interleaving_keeps_first_occurrence_and_fills_from_remaining_route():
    assert interleave(["A", "B", "C", "D"], ["B", "E", "A", "F"], 4) == ["A", "B", "E", "C"]
    assert interleave(["A"], ["A", "B", "C"], 10) == ["A", "B", "C"]
    assert interleave([], [], 100) == []


def test_chinese_lexical_terms_and_common_term_small_corpus():
    assert "北京" in lexical_terms("我住在北京")
    rows = [
        {
            "id": "one",
            "content": "北京 orchid",
            "role": "user",
            "source_timestamp_ms": None,
            "created_at": "2026-01-01T00:00:00Z",
            "terms_json": json.dumps(lexical_terms("北京 orchid")),
            "embedding": np.ones(4, dtype="<f4").tobytes(),
        }
    ]
    assert retrieve(rows, "北京 orchid", np.ones(4), 100)[0]["id"] == "one"


def test_model_switch_rejected_on_existing_database(settings, fake):
    app = create_app(settings, embedder=fake)
    app.state.store.health()
    settings.embedding.model = "text-embedding-v4"
    with pytest.raises(StoreMismatch):
        create_app(settings, embedder=fake)
