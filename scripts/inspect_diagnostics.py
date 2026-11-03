"""Summarize private diagnostics, or explicitly export them to a new local private file."""

import argparse
import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from plain_memory.config import load_settings
from plain_memory.diagnostics import Diagnostics


def inspect(config: Path, output: Path | None, include_content: bool, user_id: str | None):
    settings = load_settings(config)
    path = settings.diagnostics.database
    if not path.exists():
        raise ValueError("No diagnostics database yet; start the updated service first.")
    user_ref = None
    if user_id:
        import hashlib
        import hmac

        user_ref = hmac.new(
            settings.api_token.get_secret_value().encode(), user_id.encode(), hashlib.sha256
        ).hexdigest()
    query = "SELECT metadata_json, content_json FROM events WHERE expires_at > ?"
    params = [datetime.now(timezone.utc).isoformat()]
    if user_ref:
        query += " AND user_ref=?"
        params.append(user_ref)
    query += " ORDER BY started_at, event_id"
    summary = {
        "requests": 0,
        "content_records": 0,
        "status_counts": Counter(),
        "operations": {},
        "errors": Counter(),
        "latest_at": None,
        "capture_content_active": bool(
            settings.diagnostics.capture_content
            and settings.diagnostics.capture_until
            and settings.diagnostics.capture_until > datetime.now(timezone.utc)
        ),
    }
    durations, counts, captured = defaultdict(list), Counter(), Counter()
    fd = None
    try:
        if output:
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
            for metadata_json, content_json in conn.execute(query, params):
                event = json.loads(metadata_json)
                operation = event["operation"]
                counts[operation] += 1
                durations[operation].append(event["duration_ms"])
                summary["requests"] += 1
                summary["status_counts"][str(event["status"])] += 1
                summary["content_records"] += content_json is not None
                captured[operation] += content_json is not None
                summary["latest_at"] = event["started_at"]
                if event.get("error_kind"):
                    summary["errors"][event["error_kind"]] += 1
                if fd:
                    if include_content and content_json is not None:
                        event["content"] = json.loads(content_json)
                    fd.write(json.dumps(event, ensure_ascii=False) + "\n")
        for operation, values in durations.items():
            values.sort()
            summary["operations"][operation] = {
                "count": counts[operation],
                "content_records": captured[operation],
                "p50_ms": values[(len(values) - 1) // 2],
                "p95_ms": values[min(len(values) - 1, int(len(values) * 0.95))],
                "max_ms": values[-1],
            }
    finally:
        if fd:
            fd.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.local.yaml"))
    parser.add_argument("--output", type=Path, help="New private JSONL file; never overwritten")
    parser.add_argument("--include-content", action="store_true")
    parser.add_argument("--user-id", help="Limit export to one exact user scope")
    parser.add_argument("--prune-expired", action="store_true")
    args = parser.parse_args()
    if args.include_content and args.output is None:
        parser.error("--include-content requires --output; contents are never printed to stdout")
    try:
        if args.prune_expired:
            Diagnostics(load_settings(args.config)).cleanup()
        inspect(args.config, args.output, args.include_content, args.user_id)
    except (OSError, sqlite3.Error, ValueError):
        parser.exit(1, "Cannot inspect/export diagnostics; check config, paths and permissions.\n")


if __name__ == "__main__":
    main()
