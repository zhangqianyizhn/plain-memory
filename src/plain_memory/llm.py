"""NoneLinear gpt-4o-mini, non-streaming Chat Completions with no automatic retries."""

import time
from dataclasses import dataclass

import httpx

from plain_memory.config import LLMConfig


class LLMError(Exception):
    def __init__(self, kind: str, upstream_status: int | None = None):
        super().__init__(f"NoneLinear request failed ({kind}).")
        self.kind, self.upstream_status = kind, upstream_status


@dataclass(frozen=True)
class Completion:
    content: str
    model: str
    usage: dict


class NoneLinearLLM:
    def __init__(self, config: LLMConfig, *, transport=None):
        key = config.api_key.get_secret_value().strip()
        if not key or key.startswith("REPLACE_"):
            raise ValueError("Set llm.api_key in config.local.yaml.")
        self.config = config
        self.client = httpx.Client(
            headers={"Authorization": f"Bearer {key}"},
            follow_redirects=False,
            transport=transport,
        )

    def close(self):
        self.client.close()

    def complete(self, messages: list[dict], *, deadline: float | None = None) -> Completion:
        remaining = self.config.timeout_seconds
        if deadline is not None:
            remaining = min(remaining, deadline - time.monotonic())
        if remaining <= 0:
            raise LLMError("deadline")
        try:
            response = self.client.post(
                self.config.base_url + "/chat/completions",
                json={
                    "model": self.config.model,
                    "messages": messages,
                    "temperature": self.config.temperature,
                    "max_tokens": self.config.max_tokens,
                    "stream": False,
                },
                timeout=httpx.Timeout(remaining, connect=min(5, remaining)),
            )
            response.raise_for_status()
            result = response.json()
            choice = result["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise LLMError("empty_content")
            if choice.get("finish_reason") != "stop":
                raise LLMError("incomplete_output")
            if result.get("model") not in {"gpt-4o-mini", "openai/gpt-4o-mini"}:
                raise LLMError("unexpected_model")
            if deadline is not None and time.monotonic() >= deadline:
                raise LLMError("deadline")
            return Completion(
                content=content, model=result.get("model", ""), usage=result.get("usage") or {}
            )
        except LLMError:
            raise
        except httpx.HTTPStatusError as exc:
            raise LLMError("upstream_http", exc.response.status_code) from None
        except httpx.TimeoutException:
            raise LLMError("upstream_timeout") from None
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
            raise LLMError("invalid_response") from None
