import os
import subprocess
import sys
from pathlib import Path

import yaml

from plain_memory.config import load_settings


def test_single_generated_config_has_both_models_and_never_overwrites(tmp_path):
    project = Path(__file__).resolve().parents[1]
    config = tmp_path / "config.local.yaml"
    command = [sys.executable, "scripts/init_config.py", "--output", str(config)]
    run = subprocess.run(command, cwd=project, capture_output=True, text=True)
    assert run.returncode == 0
    settings = load_settings(config)
    assert settings.embedding.model == "text-embedding-v4"
    assert settings.embedding.batch_size == 10 and settings.embedding.dimensions == 1024
    assert settings.llm.model == "gpt-4o-mini"
    assert "NONELINEAR" in settings.llm.api_key.get_secret_value()
    assert len(settings.api_token.get_secret_value()) >= 32
    assert settings.api_token.get_secret_value() not in run.stdout
    assert config.stat().st_mode & 0o777 == 0o600
    contents = config.read_bytes()
    assert subprocess.run(command, cwd=project, capture_output=True).returncode != 0
    assert config.read_bytes() == contents
    assert list(tmp_path.glob("*.yaml")) == [config]


def test_model_check_config_uses_only_unified_file_and_masks_errors(tmp_path):
    project = Path(__file__).resolve().parents[1]
    config = tmp_path / "config.local.yaml"
    data = {
        "api_token": "synthetic-public-token-at-least-32-bytes",
        "database": "memory.sqlite3",
        "embedding": {"base_url": "https://test.invalid/v1", "api_key": "synthetic-key"},
        "llm": {"api_key": "synthetic-nonelinear-key"},
    }
    config.write_text(yaml.safe_dump(data))
    command = [
        sys.executable,
        "-m",
        "plain_memory",
        "--config",
        str(config),
        "check-models",
        "--config-only",
    ]
    environment = {**os.environ, "NONELINEAR_API_KEY": "MUST_NOT_BE_USED"}
    run = subprocess.run(command, cwd=project, env=environment, capture_output=True, text=True)
    assert run.returncode == 0 and "PASS" in run.stdout
    data["llm"]["api_key"] = "REPLACE_WITH_YOUR_NONELINEAR_KEY"
    config.write_text(yaml.safe_dump(data))
    run = subprocess.run(command, cwd=project, env=environment, capture_output=True, text=True)
    assert run.returncode == 1 and "llm.api_key" in run.stderr
    assert "synthetic-key" not in run.stderr and "MUST_NOT_BE_USED" not in run.stderr
    assert not (tmp_path / "memory.sqlite3").exists()
