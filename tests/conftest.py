import hashlib

import numpy as np
import pytest

from plain_memory.config import Settings
from plain_memory.text import lexical_terms


class FakeEmbedder:
    """测试专用合成向量；生产配置中没有 fake 模型选项。"""

    def __init__(self):
        self.calls = []

    def embed(self, texts, deadline):
        self.calls.append(texts)
        result = []
        for text in texts:
            vector = np.zeros(32, dtype=np.float32)
            for term in lexical_terms(text) or [text]:
                position = int.from_bytes(hashlib.sha256(term.encode()).digest()[:4], "little") % 32
                vector[position] += 1
            result.append(vector)
        return np.array(result)

    def close(self):
        pass


@pytest.fixture
def settings(tmp_path):
    return Settings(
        api_token="test-token-is-public-and-at-least-32-bytes",
        database=tmp_path / "memory.sqlite3",
        embedding={
            "protocol": "openai_compatible",
            "base_url": "https://synthetic.invalid/v1",
            "model": "synthetic-test-only",
            "api_key": "synthetic-test-only",
        },
    )


@pytest.fixture
def fake():
    return FakeEmbedder()


@pytest.fixture
def headers(settings):
    return {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
