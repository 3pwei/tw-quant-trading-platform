"""Gateway behavior with the real Caddy binary; CI extracts it from the built image."""
from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "deploy/staging"


class GatewayHealthcheckTests(unittest.TestCase):
    def test_compose_healthcheck_rejects_html_errors_and_extra_bytes(self):
        line = next(line.strip() for line in (STAGING / "docker-compose.yml").read_text().splitlines()
                    if 'test: ["CMD-SHELL"' in line)
        command = json.loads(line.removeprefix("test: "))[1].replace("$$", "$")
        with tempfile.TemporaryDirectory() as directory:
            curl = Path(directory) / "curl"
            curl.write_text('#!/bin/sh\nprintf "%s" "$TEST_RESPONSE"\nexit "$TEST_EXIT"\n')
            curl.chmod(0o755)
            for response, exit_code, expected in (
                ("ok200", 0, True), ("<html>404</html>200", 0, False),
                ("ok\n200", 0, False), ("ok\nextra200", 0, False),
                ("ok302", 0, False), ("ok500", 22, False), ("ok200", 28, False),
            ):
                with self.subTest(response=response, exit_code=exit_code):
                    result = subprocess.run(["sh", "-c", command], env={
                        **os.environ, "PATH": directory + os.pathsep + os.environ["PATH"],
                        "TEST_RESPONSE": response, "TEST_EXIT": str(exit_code),
                    }, capture_output=True)
                    self.assertEqual(result.returncode == 0, expected)

    def test_smoke_preserves_failure_and_diagnoses_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            docker = Path(directory) / "docker"
            log = Path(directory) / "calls"
            docker.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$TEST_CALLS"
case "$1" in
  run) exit 0 ;;
  inspect) printf 'exited\\n' ;;
  logs) printf 'fixture startup error\\n' >&2 ;;
  rm) exit 0 ;;
esac
''')
            docker.chmod(0o755)
            result = subprocess.run(["bash", str(STAGING / "smoke-gateway.sh"), "fixture"],
                                    capture_output=True, text=True, timeout=5, env={
                                        **os.environ, "PATH": directory + os.pathsep + os.environ["PATH"],
                                        "TEST_CALLS": str(log),
                                    })
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("P8_GATEWAY_SMOKE_FAIL check=container-running", result.stderr)
            self.assertIn("fixture startup error", result.stderr)
            calls = log.read_text()
            self.assertLess(calls.index("logs --tail"), calls.index("rm --force"))
            self.assertNotIn("Config.Env", calls)


@unittest.skipUnless(os.environ.get("CADDY_BIN"), "CADDY_BIN supplied by gateway image CI")
class GatewayRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        directory = Path(cls.temp.name)
        web = directory / "www"
        web.mkdir()
        (web / "404.html").write_text("fixture-not-found")
        (web / "index.html").write_text("fixture-home")
        (web / "demo").mkdir()
        (web / "demo/index.html").write_text("fixture-demo")
        # Backend routing must also win over a colliding static asset.
        (web / "api").mkdir()
        (web / "api/test").write_text("must-not-shadow-backend")
        cls.hits = []

        class Backend(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                cls.hits.append(self.path)
                if self.headers.get("Upgrade", "").lower() == "websocket":
                    value = self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                    accept = base64.b64encode(hashlib.sha1(value.encode()).digest()).decode()
                    self.send_response(101)
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Sec-WebSocket-Accept", accept)
                    self.end_headers()
                    self.close_connection = True
                    return
                body = ("backend:" + self.path).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        cls.backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        cls.addClassCleanup(cls.backend.server_close)
        threading.Thread(target=cls.backend.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.backend.shutdown)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        config = (STAGING / "Caddyfile").read_text()
        # Only host paths/listen address/backend endpoint differ from the image.
        config = config.replace(":8080 {", f"http://127.0.0.1:{cls.port} {{")
        config = config.replace("root * /srv", f"root * {web}")
        config = config.replace("market-api:8000", f"127.0.0.1:{cls.backend.server_port}")
        config_path = directory / "Caddyfile"
        config_path.write_text(config)
        cls.logs = (directory / "caddy.log").open("w+")
        cls.addClassCleanup(cls.logs.close)
        cls.process = subprocess.Popen(
            [os.environ["CADDY_BIN"], "run", "--config", str(config_path), "--adapter", "caddyfile"],
            stdout=cls.logs, stderr=cls.logs, env={
                **os.environ, "XDG_CONFIG_HOME": str(directory / "config"),
                "XDG_DATA_HOME": str(directory / "data"),
            },
        )
        cls.addClassCleanup(cls.stop_caddy)
        for _ in range(100):
            if cls.process.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=.1):
                    return
            except OSError:
                time.sleep(.05)
        cls.logs.seek(0)
        raise AssertionError("Caddy did not start: " + cls.logs.read())

    @classmethod
    def stop_caddy(cls):
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)

    def test_health_is_exact_and_static_routes_still_work(self):
        for path, expected in (("/healthz", b"ok"), ("/", b"fixture-home"),
                               ("/demo/", b"fixture-demo"), ("/missing", b"fixture-not-found")):
            with self.subTest(path=path):
                count = len(self.hits)
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), expected)
                    self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                    self.assertIn("Content-Security-Policy", response.headers)
                self.assertEqual(len(self.hits), count)

    def test_backend_paths_and_query_are_preserved(self):
        for path in ("/api/test?probe=1", "/ws/test", "/docs", "/openapi.json", "/health/live"):
            with self.subTest(path=path):
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), ("backend:" + path).encode())
                self.assertEqual(self.hits[-1], path)

    def test_websocket_upgrade_reaches_backend(self):
        nonce = base64.b64encode(os.urandom(16)).decode()
        expected_accept = base64.b64encode(hashlib.sha1(
            (nonce + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest())
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as connection:
            connection.sendall((
                f"GET /ws/test HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nConnection: Upgrade\r\n"
                "Upgrade: websocket\r\nSec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Key: {nonce}\r\n\r\n"
            ).encode())
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = connection.recv(4096)
                self.assertTrue(chunk, response)
                response += chunk
            self.assertTrue(response.startswith(b"HTTP/1.1 101 "), response)
            self.assertIn(expected_accept, response)
            self.assertEqual(self.hits[-1], "/ws/test")


if __name__ == "__main__":
    unittest.main()
