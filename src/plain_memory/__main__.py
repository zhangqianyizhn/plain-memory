import argparse
import sys
import time
from pathlib import Path

import uvicorn

from plain_memory.api import create_app
from plain_memory.config import load_settings
from plain_memory.diagnostics import Diagnostics
from plain_memory.embedding import EmbeddingError, HTTPEmbedder
from plain_memory.llm import LLMError, NoneLinearLLM
from plain_memory.store import Store


def check_models(settings, config_only=False):
    for name, config in (("embedding", settings.embedding), ("llm", settings.llm)):
        key = config.api_key.get_secret_value().strip()
        if not key or key.startswith("REPLACE_"):
            raise ValueError(f"Set {name}.api_key in config.local.yaml.")
    if "YOUR_WORKSPACE_ID" in settings.embedding.base_url:
        raise ValueError("Set embedding.base_url to your Alibaba Cloud workspace address.")
    if config_only:
        print("PASS: both model configurations have keys; no network requests were made.")
        return
    print("Checking Alibaba Cloud text-embedding-v4...", flush=True)
    embedder = HTTPEmbedder(settings.embedding)
    try:
        vectors = embedder.embed(["Synthetic model connection check."], time.monotonic() + 30)
        print(f"PASS: text-embedding-v4, dimensions={vectors.shape[1]}.", flush=True)
    finally:
        embedder.close()
    llm_config = settings.llm.model_copy(update={"temperature": 0, "max_tokens": 64})
    print(
        f"Checking NoneLinear gpt-4o-mini (timeout {llm_config.timeout_seconds:g}s)...", flush=True
    )
    llm = NoneLinearLLM(llm_config)
    try:
        result = llm.complete([{"role": "user", "content": "Reply with exactly: NONELINEAR_OK"}])
        if result.content.strip() != "NONELINEAR_OK":
            raise LLMError("unexpected_check_output")
        print("PASS: NoneLinear gpt-4o-mini, expected short response received.")
    finally:
        llm.close()


def main():
    parser = argparse.ArgumentParser(description="PlainMemory AML Add/Search service")
    parser.add_argument("--config", type=Path, default=Path("config.local.yaml"))
    subcommands = parser.add_subparsers(dest="command", required=True)
    serve = subcommands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    purge = subcommands.add_parser("purge", help="Stop service first; purge one exact user scope")
    purge.add_argument("--user-id", required=True)
    purge.add_argument("--yes", action="store_true", help="Confirm deleting this exact user scope")
    check = subcommands.add_parser(
        "check-models", help="Check Alibaba embedding and NoneLinear LLM"
    )
    check.add_argument("--config-only", action="store_true", help="Validate keys without API calls")
    args = parser.parse_args()
    try:
        settings = load_settings(args.config)
        if args.command == "check-models":
            check_models(settings, config_only=args.config_only)
        elif args.command == "purge":
            if not args.yes:
                parser.error("purge requires --yes; stop the service and check the exact user_id")
            count = Store(settings).purge(args.user_id)
            Diagnostics(settings).purge(args.user_id)
            print(f"Removed {count} chunks and matching diagnostics; delete any external backups.")
        else:
            app = create_app(settings)
            # access log 包含可控路径/参数，避免记录凭证或记忆文本。
            uvicorn.run(
                app,
                host=args.host,
                port=args.port,
                access_log=False,
                log_level="warning",
                workers=1,
            )
    except (EmbeddingError, LLMError) as exc:
        print(f"Model check failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except (ValueError, RuntimeError) as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
