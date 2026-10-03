"""真实 loopback HTTP 和 CLI 子进程联调；embedding 上游仅用合成数据。"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import yaml


def test_real_cli_http_smoke_and_restart(tmp_path):
    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            data = [
                {"index": i, "embedding": [1, 2, 3, 4]} for i, text in enumerate(payload["input"])
            ]
            contents = json.dumps({"data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(contents)))
            self.end_headers()
            self.wfile.write(contents)

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = tmp_path / "config.local.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "api_token": "synthetic-public-token-at-least-32-bytes",
                "database": "memory.sqlite3",
                "embedding": {
                    "protocol": "openai_compatible",
                    "model": "synthetic-test-only",
                    "base_url": f"http://127.0.0.1:{upstream.server_port}/v1",
                    "api_key": "synthetic-test-only",
                },
            }
        )
    )
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": "Bearer synthetic-public-token-at-least-32-bytes"}
    project = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["NO_PROXY"] = "127.0.0.1,localhost"

    def start():
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "plain_memory",
                "--config",
                str(config),
                "serve",
                "--port",
                str(port),
            ],
            cwd=project,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError("CLI exited before becoming ready")
                try:
                    if httpx.get(base + "/health", timeout=0.5, trust_env=False).status_code == 200:
                        return process
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            raise AssertionError("CLI readiness timeout")
        except BaseException:
            stop(process)
            raise

    def stop(process):
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()

    process = None
    try:
        process = start()
        smoke = subprocess.run(
            [sys.executable, "scripts/smoke.py", "--config", str(config), "--base-url", base],
            cwd=project,
            env=environment,
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert smoke.returncode == 0, "Synthetic HTTP smoke failed"
        assert "PASS:" in smoke.stdout
        user_id = smoke.stdout.split("scope (safe to purge after stopping service): ")[1].strip()
        query = {"user_id": user_id, "query": "pet", "top_k": 100}
        with httpx.Client(trust_env=False) as client:
            previous = client.post(base + "/search", headers=headers, json=query).json()
            assert previous["data"]
        stop(process)
        process = start()
        with httpx.Client(trust_env=False) as client:
            assert client.post(base + "/search", headers=headers, json=query).json() == previous
        stop(process)
        process = None
        purged = subprocess.run(
            [
                sys.executable,
                "-m",
                "plain_memory",
                "--config",
                str(config),
                "purge",
                "--user-id",
                user_id,
                "--yes",
            ],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert purged.returncode == 0
        process = start()
        with httpx.Client(trust_env=False) as client:
            assert client.post(base + "/search", headers=headers, json=query).json() == {"data": []}
    finally:
        if process is not None:
            stop(process)
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)
