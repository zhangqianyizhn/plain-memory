"""只在本机创建配置及随机服务密钥，不打印凭证。"""

import argparse
import os
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, default=Path("config.example.yaml"))
    parser.add_argument("--output", type=Path, default=Path("config.local.yaml"))
    args = parser.parse_args()
    contents = args.template.read_text(encoding="utf-8").replace(
        "GENERATE_LOCALLY", secrets.token_urlsafe(32)
    )
    try:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        parser.error("Local config already exists; it was not overwritten.")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(contents)
    print("Private config created. Set embedding.api_key locally before serving.")


if __name__ == "__main__":
    main()
