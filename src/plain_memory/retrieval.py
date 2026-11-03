"""User-scoped vector/BM25 ranking, optional regex quota, and stable deduplicated fusion."""

import json

import numpy as np
from rank_bm25 import BM25Okapi

from plain_memory.embedding import EmbeddingError
from plain_memory.text import lexical_terms


def interleave_routes(routes: list[list[str]], top_k: int) -> list[str]:
    if top_k <= 0:
        return []
    result, seen = [], set()
    for index in range(max(map(len, routes), default=0)):
        for candidates in routes:
            if index < len(candidates) and candidates[index] not in seen:
                seen.add(candidates[index])
                result.append(candidates[index])
                if len(result) == top_k:
                    return result
    return result


def interleave(vector_ids: list[str], bm25_ids: list[str], top_k: int) -> list[str]:
    return interleave_routes([vector_ids, bm25_ids], top_k)


def rank_candidates(rows: list[dict], query: str, query_vector: np.ndarray, top_k: int) -> dict:
    try:
        matrix = np.stack([np.frombuffer(row["embedding"], dtype="<f4") for row in rows])
        if matrix.shape[1] != len(query_vector) or not np.isfinite(matrix).all():
            raise ValueError
        similarities = matrix @ query_vector
    except (ValueError, TypeError):
        raise EmbeddingError("Stored vectors are incompatible with the query model.") from None
    vector_order = np.argsort(-similarities, kind="stable")[:top_k]
    term_lists = [json.loads(row["terms_json"]) for row in rows]
    # 无词项的片段不进入 BM25，避免空语料/平均长度为零。
    indexes = [i for i, terms in enumerate(term_lists) if terms]
    query_terms = lexical_terms(query)
    bm25_ranking = []
    if indexes and query_terms:
        corpus = [term_lists[i] for i in indexes]
        scores = BM25Okapi(corpus).get_scores(query_terms)
        query_set = set(query_terms)
        matches = [j for j, terms in enumerate(corpus) if query_set.intersection(terms)]
        matches.sort(key=lambda j: (-scores[j], j))
        bm25_ranking = [
            {"id": rows[indexes[j]]["id"], "rank": rank + 1, "score": float(scores[j])}
            for rank, j in enumerate(matches[:top_k])
        ]
    return {
        "vector": [
            {"id": rows[i]["id"], "rank": rank + 1, "score": float(similarities[i])}
            for rank, i in enumerate(vector_order)
        ],
        "bm25": bm25_ranking,
    }


def merge_results(rows, rankings, top_k, *, regex_ids=None, trace=None):
    three_routes = regex_ids is not None and top_k >= 3
    limit = top_k // 3 if three_routes else top_k
    vector, bm25 = rankings["vector"][:limit], rankings["bm25"][:limit]
    routes = [[item["id"] for item in route] for route in (vector, bm25)]
    if three_routes:
        routes.append(regex_ids[:limit])
    ordered = interleave_routes(routes, top_k)
    if trace is not None:
        trace.update(
            vector=vector,
            bm25=bm25,
            regex_ids=routes[2] if three_routes else [],
            fusion_mode="three_routes" if three_routes else "two_routes",
            route_limit=limit,
            fused_ids=ordered,
        )
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


def retrieve(rows, query, query_vector, top_k, *, regex_ids=None, trace=None):
    return merge_results(
        rows,
        rank_candidates(rows, query, query_vector, top_k),
        top_k,
        regex_ids=regex_ids,
        trace=trace,
    )
