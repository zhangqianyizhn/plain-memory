"""对运行中的服务做合成数据冒烟测试；只打印结果，不输出密钥/记忆正文。"""

import argparse
import uuid
from pathlib import Path

import httpx

from plain_memory.config import load_settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.local.yaml"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    settings = load_settings(args.config)
    user_id = "plain-memory-synthetic-smoke:" + uuid.uuid4().hex
    headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
    payload = {
        "user_id": user_id,
        "session_id": "synthetic-session",
        "request_id": "synthetic-add",
        "messages": [
            {
                "role": "user",
                "timestamp": 1704067200000,
                "content": "Synthetic test: my pet's name is Orchid.",
            }
        ],
    }
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=150) as client:
        client.get("/health").raise_for_status()
        unauthorized = client.post("/add", json=payload)
        assert unauthorized.status_code == 401, "Unauthenticated Add must return 401"
        added = client.post("/add", headers=headers, json=payload)
        added.raise_for_status()
        assert added.json() == {
            "success": True,
            "request_id": payload["request_id"],
            "user_id": user_id,
            "session_id": payload["session_id"],
        }
        query = {"user_id": user_id, "query": "What is my pet's name?", "top_k": 100}
        found = client.post("/search", headers=headers, json=query)
        found.raise_for_status()
        items = found.json()["data"]
        assert items and len(items) <= 100 and "Orchid" in items[0]["content"]
        assert all(item["id"] and item["content"].strip() for item in items)
        retried = client.post("/add", headers=headers, json=payload)
        retried.raise_for_status()
        again = client.post("/search", headers=headers, json=query)
        again.raise_for_status()
        assert [item["id"] for item in again.json()["data"]] == [item["id"] for item in items]
        query["user_id"] += ":other-user"
        isolated = client.post("/search", headers=headers, json=query)
        isolated.raise_for_status()
        assert isolated.json() == {"data": []}
    print("PASS: health, Bearer, synchronous Add, Search, retry, user isolation.")
    print("Synthetic data scope (safe to purge after stopping service): " + user_id)


if __name__ == "__main__":
    main()
