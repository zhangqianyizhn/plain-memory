import time

import httpx
import numpy as np
import pytest

from plain_memory.config import EmbeddingConfig
from plain_memory.embedding import EmbeddingError, HTTPEmbedder, normalize_vectors


def config(protocol="openai_compatible"):
    return EmbeddingConfig(
        protocol=protocol,
        base_url="https://test.invalid/v1",
        model="synthetic",
        api_key="synthetic-test-only",
        batch_size=2,
    )


def test_base_url_is_normalized_and_rejects_endpoint_paths():
    trailing = config()
    trailing.base_url = "https://test.invalid/v1/"
    trailing = EmbeddingConfig.model_validate(trailing.model_dump())
    assert trailing.base_url == "https://test.invalid/v1"
    for invalid in (
        "https://test.invalid/v1/embeddings",
        "https://test.invalid/v1/embeddings/multimodal",
        "https://test.invalid/v1?tenant=secret",
        "test.invalid/v1",
    ):
        with pytest.raises(ValueError):
            EmbeddingConfig(
                protocol="openai_compatible",
                base_url=invalid,
                model="synthetic",
                api_key="synthetic-test-only",
            )


def test_standard_batch_respects_response_indexes():
    calls = []

    def handle(request):
        import json

        payload = json.loads(request.content)
        calls.append(payload)
        assert request.url.path == "/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer synthetic-test-only"
        data = [
            {"index": i, "embedding": [int(text), 1]} for i, text in enumerate(payload["input"])
        ]
        return httpx.Response(200, json={"data": list(reversed(data))})

    embedder = HTTPEmbedder(config(), transport=httpx.MockTransport(handle))
    try:
        vectors = embedder.embed(["1", "2", "3"], time.monotonic() + 10)
        assert [len(call["input"]) for call in calls] == [2, 1]
        np.testing.assert_allclose(vectors, normalize_vectors([[1, 1], [2, 1], [3, 1]], 3))
    finally:
        embedder.close()


def test_doubao_multimodal_encodes_each_chunk_independently():
    calls = []

    def handle(request):
        import json

        payload = json.loads(request.content)
        calls.append(payload)
        assert request.url.path == "/v1/embeddings/multimodal"
        assert len(payload["input"]) == 1
        return httpx.Response(200, json={"data": {"embedding": [1, 2, 3]}})

    embedder = HTTPEmbedder(config("doubao_multimodal"), transport=httpx.MockTransport(handle))
    try:
        assert embedder.embed(["first", "second"], time.monotonic() + 10).shape == (2, 3)
        assert [call["input"][0]["text"] for call in calls] == ["first", "second"]
    finally:
        embedder.close()


@pytest.mark.parametrize(
    "data",
    [
        [{"index": 0, "embedding": [1, 1]}, {"index": 0, "embedding": [1, 1]}],
        [{"index": 1, "embedding": [1, 1]}],
        [{"index": 0, "embedding": [0, 0]}, {"index": 1, "embedding": [1, 1]}],
    ],
)
def test_invalid_vectors_or_indexes_fail_explicitly(data):
    embedder = HTTPEmbedder(
        config(),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": data})),
    )
    try:
        with pytest.raises(EmbeddingError):
            embedder.embed(["first", "second"], time.monotonic() + 10)
    finally:
        embedder.close()


def test_provider_errors_do_not_expose_private_response():
    embedder = HTTPEmbedder(
        config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"detail": "PRIVATE_KEY_OR_CONTENT"})
        ),
    )
    try:
        with pytest.raises(EmbeddingError) as caught:
            embedder.embed(["private memory"], time.monotonic() + 10)
        assert "PRIVATE_KEY_OR_CONTENT" not in str(caught.value)
        assert caught.value.status == 503
    finally:
        embedder.close()
