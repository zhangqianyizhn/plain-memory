"""私密配置独立于源码；相对数据路径基于配置文件所在目录。"""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator


class EmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: Literal["doubao_multimodal", "openai_compatible"]
    base_url: str
    model: str = Field(min_length=1)
    api_key: SecretStr
    dimensions: int | None = Field(default=None, gt=0)
    batch_size: int = Field(default=32, ge=1)

    @model_validator(mode="after")
    def check_base_url(self):
        normalized = self.base_url.rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not include a query or fragment")
        if parsed.path.endswith(("/embeddings", "/embeddings/multimodal")):
            raise ValueError("base_url must not include the embedding endpoint path")
        self.base_url = normalized
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_token: SecretStr
    database: Path
    embedding: EmbeddingConfig
    chunk_tokens: int = Field(default=512, ge=4)
    overlap_tokens: int = Field(default=64, ge=0)
    max_body_bytes: int = Field(default=16 * 1024 * 1024, ge=1)
    add_concurrency: int = Field(default=16, ge=16, le=64)
    search_concurrency: int = Field(default=2, ge=1)
    add_timeout_seconds: float = Field(default=120, gt=0)
    search_timeout_seconds: float = Field(default=30, gt=0)

    @model_validator(mode="after")
    def check_overlap(self):
        if self.overlap_tokens >= self.chunk_tokens:
            raise ValueError("overlap_tokens must be smaller than chunk_tokens")
        return self

    def store_identity(self) -> dict:
        return {
            "schema": 2,
            "model": self.embedding.model,
            "base_url": self.embedding.base_url,
            "protocol": self.embedding.protocol,
            "configured_dimensions": self.embedding.dimensions,
            "tokenizer": "cl100k_base",
            "chunk_tokens": self.chunk_tokens,
            "overlap_tokens": self.overlap_tokens,
            "lexical_version": "english-words-han-unigrams-bigrams-v1",
        }


def load_settings(path: Path) -> Settings:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        settings = Settings.model_validate(payload)
    except (OSError, yaml.YAMLError, ValidationError):
        # Pydantic/YAML 的默认错误包含输入值；不得输出配置中的密钥。
        raise ValueError("Cannot load config; check file, field names and types.") from None
    settings.database = (path.resolve().parent / settings.database).resolve()
    return settings
