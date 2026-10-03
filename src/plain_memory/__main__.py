import argparse
import sys
from pathlib import Path

import uvicorn

from plain_memory.api import create_app
from plain_memory.config import load_settings
from plain_memory.store import Store


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
    args = parser.parse_args()
    try:
        settings = load_settings(args.config)
        if args.command == "purge":
            if not args.yes:
                parser.error("purge requires --yes; stop the service and check the exact user_id")
            count = Store(settings).purge(args.user_id)
            print(f"Removed {count} chunks; separately delete any backups.")
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
    except (ValueError, RuntimeError) as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
