"""固定 token 窗口与轻量中英文 BM25 分词。"""

import re
from functools import lru_cache

import tiktoken


@lru_cache(maxsize=1)
def encoding():
    return tiktoken.get_encoding("cl100k_base")


def chunks(text: str, size: int, overlap: int) -> list[str]:
    if not text.strip():
        return []
    tokens = encoding().encode(text, disallowed_special=())
    if len(tokens) <= size:
        return [text]
    # Token 可以只包含 UTF-8 字符的一部分。只在合法字符边界结束/开始窗口。
    pieces = [encoding().decode_single_token_bytes(t) for t in tokens]
    raw = text.encode("utf-8")
    offsets = [0]
    for piece in pieces:
        offsets.append(offsets[-1] + len(piece))
    safe = [i for i, pos in enumerate(offsets) if pos == len(raw) or raw[pos] & 0xC0 != 0x80]
    safe_set = set(safe)
    result = []
    start = 0
    while start < len(tokens):
        end = min(start + size, len(tokens))
        while end > start and end not in safe_set:
            end -= 1
        if end == start:
            raise ValueError("chunk_tokens is too small for a Unicode character")
        fragment = raw[offsets[start] : offsets[end]].decode("utf-8")
        if fragment.strip():
            result.append(fragment)
        if end == len(tokens):
            break
        next_start = max(start + 1, end - overlap)
        while next_start not in safe_set:
            next_start += 1
        start = next_start
    return result


def lexical_terms(text: str) -> list[str]:
    terms = re.findall(r"[a-z0-9]+(?:['_-][a-z0-9]+)*", text.lower())
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", text):
        terms.extend(run)
        terms.extend(run[i : i + 2] for i in range(len(run) - 1))
    return terms
