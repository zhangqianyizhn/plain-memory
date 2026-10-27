import time

import httpx
import numpy as np
import pytest

from plain_memory.config import EmbeddingConfig
from plain_memory.embedding import EmbeddingError, HTTPEmbedder, normalize_vectors


def config():
    return EmbeddingConfig(
        base_url="https://test.invalid/v1",
        model="text-embedding-v4",
        api_key="synthetic-test-only",
        batch_size=2,
        dimensions=None,
    )


def test_base_url_is_normalized_and_rejects_endpoint_paths():
    trailing = config()
    trailing.base_url = "https://test.invalid/v1/"
    trailing = EmbeddingConfig.model_validate(trailing.model_dump())
    assert trailing.base_url == "https://test.invalid/v1"
    for invalid in (
        "https://test.invalid/v1/embeddings",
        "https://test.invalid/v1?tenant=secret",
        "test.invalid/v1",
    ):
        with pytest.raises(ValueError):
            EmbeddingConfig(
                base_url=invalid,
                model="text-embedding-v4",
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


def test_aliyun_v4_uses_workspace_endpoint_float_vectors_and_ten_text_batches():
    import json

    calls = []
    v4 = EmbeddingConfig(
        base_url="https://ws-synthetic.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        model="text-embedding-v4",
        api_key="synthetic-test-only",
        dimensions=1024,
        batch_size=10,
    )

    def handle(request):
        assert str(request.url) == v4.base_url + "/embeddings"
        assert request.headers["Authorization"] == "Bearer synthetic-test-only"
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["encoding_format"] == "float"
        assert payload["dimensions"] == 1024
        assert payload["model"] == "text-embedding-v4"
        assert 1 <= len(payload["input"]) <= 10
        data = []
        for index, text in enumerate(payload["input"]):
            vector = [float(int(text) + 1), 1.0] + [0.0] * 1022
            data.append({"index": index, "embedding": vector})
        return httpx.Response(200, json={"data": list(reversed(data))})

    embedder = HTTPEmbedder(v4, transport=httpx.MockTransport(handle))
    try:
        texts = [str(i) for i in range(23)]
        actual = embedder.embed(texts, time.monotonic() + 10)
        assert [len(call["input"]) for call in calls] == [10, 10, 3]
        assert [text for call in calls for text in call["input"]] == texts
        expected = normalize_vectors(
            [[float(i + 1), 1.0] + [0.0] * 1022 for i in range(23)], 23, 1024
        )
        np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-7)
    finally:
        embedder.close()


def test_v4_config_rejects_oversized_batches_and_different_models():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EmbeddingConfig(batch_size=32)
    with pytest.raises(ValidationError):
        EmbeddingConfig(model="different-model")


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
