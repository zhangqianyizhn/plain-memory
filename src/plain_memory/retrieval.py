"""同一用户快照内的向量/BM25 两路召回，以及稳定交替去重。"""

import json

import numpy as np
from rank_bm25 import BM25Okapi

from plain_memory.embedding import EmbeddingError
from plain_memory.text import lexical_terms


def interleave(vector_ids: list[str], bm25_ids: list[str], top_k: int) -> list[str]:
    result, seen = [], set()
    for index in range(max(len(vector_ids), len(bm25_ids))):
        for candidates in (vector_ids, bm25_ids):
            if index < len(candidates) and candidates[index] not in seen:
                seen.add(candidates[index])
                result.append(candidates[index])
                if len(result) == top_k:
                    return result
    return result


def retrieve(rows: list[dict], query: str, query_vector: np.ndarray, top_k: int) -> list[dict]:
    try:
        matrix = np.stack([np.frombuffer(row["embedding"], dtype="<f4") for row in rows])
        if matrix.shape[1] != len(query_vector) or not np.isfinite(matrix).all():
            raise ValueError
        similarities = matrix @ query_vector
    except (ValueError, TypeError):
        raise EmbeddingError("Stored vectors are incompatible with the query model.") from None
    vector_order = np.argsort(-similarities, kind="stable")[:top_k]
    vector_ids = [rows[i]["id"] for i in vector_order]
    term_lists = [json.loads(row["terms_json"]) for row in rows]
    # 无词项的片段不进入 BM25，避免空语料/平均长度为零。
    indexes = [i for i, terms in enumerate(term_lists) if terms]
    query_terms = lexical_terms(query)
    bm25_ids = []
    if indexes and query_terms:
        corpus = [term_lists[i] for i in indexes]
        scores = BM25Okapi(corpus).get_scores(query_terms)
        query_set = set(query_terms)
        matches = [j for j, terms in enumerate(corpus) if query_set.intersection(terms)]
        matches.sort(key=lambda j: (-scores[j], j))
        bm25_ids = [rows[indexes[j]]["id"] for j in matches[:top_k]]
    ordered = interleave(vector_ids, bm25_ids, top_k)
    by_id = {row["id"]: row for row in rows}
    result = []
    for chunk_id in ordered:
        row = by_id[chunk_id]
        label = f"[role={row['role']}"
        if row["source_timestamp_ms"] is not None:
            label += f"; source_time={row['created_at']}"
        result.append(
            {
                "id": chunk_id,
                "content": label + "]\n" + row["content"],
                "created_at": row["created_at"],
            }
        )
    return result
