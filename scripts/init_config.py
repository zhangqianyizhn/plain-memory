"""只在本机创建配置及随机服务密钥，不打印凭证。"""

import argparse
import os
import secrets
from pathlib import Path

import yaml

from plain_memory.config import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("config.local.yaml"))
    args = parser.parse_args()
    settings = Settings(
        api_token=secrets.token_urlsafe(32), database=Path("data/text-embedding-v4.sqlite3")
    )
    payload = settings.model_dump(mode="json")
    payload["api_token"] = settings.api_token.get_secret_value()
    payload["embedding"]["api_key"] = settings.embedding.api_key.get_secret_value()
    payload["llm"]["api_key"] = settings.llm.api_key.get_secret_value()
    contents = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    try:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        parser.error("Local config already exists; it was not overwritten.")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(contents)
    print(
        "Private config created. Set embedding.base_url, embedding.api_key and llm.api_key locally."
    )


if __name__ == "__main__":
    main()
