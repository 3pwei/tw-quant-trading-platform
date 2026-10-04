"""Real HTTP negatives and Caddy -> Unix -> systemd TCP ingress regressions."""
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "deploy/staging"
spec = importlib.util.spec_from_file_location("ingress_probe", STAGING / "ingress_probe.py")
ingress_probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingress_probe)
identity_spec = importlib.util.spec_from_file_location("ingress_identity", STAGING / "prepare_ingress_identity.py")
ingress_identity = importlib.util.module_from_spec(identity_spec)
identity_spec.loader.exec_module(ingress_identity)


class HostIdentityTests(unittest.TestCase):
    def test_absent_exact_and_conflicting_host_identities(self):
        user = SimpleNamespace(pw_name="p8-staging-ingress", pw_uid=10000, pw_gid=10000,
                               pw_dir="/nonexistent", pw_shell="/usr/sbin/nologin")
        group = SimpleNamespace(gr_name="p8-staging-ingress", gr_gid=10000, gr_mem=[])
        foreign_user = SimpleNamespace(**{**vars(user), "pw_name": "existing-owner"})
        foreign_group = SimpleNamespace(**{**vars(group), "gr_name": "existing-owner"})
        login_user = SimpleNamespace(**{**vars(user), "pw_shell": "/bin/bash"})
        cases = (
            ([None, None, None, None], (False, False)),
            ([user, user, group, group], (True, True)),
            ([None, foreign_user, None, None], None),
            ([None, None, None, foreign_group], None),
            ([login_user, login_user, group, group], None),
            ([user, None, group, group], None),
        )
        for records, expected in cases:
            with self.subTest(expected=expected, records=records):
                with patch.object(ingress_identity, "lookup", side_effect=records):
                    if expected is None:
                        with self.assertRaises(RuntimeError):
                            ingress_identity.validate_identity()
                    else:
                        self.assertEqual(ingress_identity.validate_identity(), expected)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class IngressProbeTests(unittest.TestCase):
    def test_http_status_exact_bytes_and_upstream_failure(self):
        class Handler(BaseHTTPRequestHandler):
            health = b"ok"
            status = 200
            upstream_status = 200

            def do_GET(self):
                is_health = self.path == "/healthz"
                body = self.health if is_health else b'{"status":"ok"}'
                self.send_response(self.status if is_health else self.upstream_status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        for body, status, upstream, expected in (
            (b"ok", 200, 200, True), (b"ok\n", 200, 200, False),
            (b"wrong\nok\n", 200, 200, False), (b"ok", 302, 200, False),
            (b"ok", 200, 503, False), (b"x" * 4097, 200, 200, False),
        ):
            with self.subTest(body=body[:16], status=status, upstream=upstream):
                Handler.health, Handler.status, Handler.upstream_status = body, status, upstream
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(ingress_probe.probe(server.server_port), expected)
                if not expected:
                    self.assertIn("P8_VERIFY_FAIL", out.getvalue())
                    self.assertNotIn("wrong\nok", out.getvalue())
                    self.assertIn("gateway-upstream" if upstream != 200 else "gateway-ingress", out.getvalue())

    def test_missing_host_listener_is_rejected(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertFalse(ingress_probe.probe(free_port()))
        self.assertIn("connection-failed", output.getvalue())

    def test_host_units_have_no_outbound_ip_socket_creation(self):
        unit = (STAGING / "p8-staging-ingress.service").read_text()
        for setting in ("User=10000", "Group=10000", "RestrictAddressFamilies=AF_UNIX",
                        "CapabilityBoundingSet=", "NoNewPrivileges=true", "ProtectSystem=strict"):
            self.assertIn(setting, unit)
        sock = (STAGING / "p8-staging-ingress.socket").read_text()
        self.assertEqual([s for s in sock.splitlines() if s.startswith("ListenStream=")],
                         ["ListenStream=127.0.0.1:18080"])


@unittest.skipUnless(os.environ.get("CADDY_BIN") and shutil.which("systemd-socket-activate")
                     and Path("/usr/lib/systemd/systemd-socket-proxyd").is_file(),
                     "real Caddy and systemd socket tools required; mandatory in public-images CI")
class RealUnixIngressTests(unittest.TestCase):
    def setUp(self):
        try:
            with socket.socket(socket.AF_UNIX):
                pass
        except OSError:
            if os.environ.get("P8_ALLOW_UNIX_TEST_SKIP") == "1" and not os.environ.get("CI"):
                self.skipTest("local sandbox forbids AF_UNIX; CI must execute this test")
            raise

    def test_real_socket_proxy_survives_gateway_restart_and_rejects_missing_socket(self):
        with tempfile.TemporaryDirectory(prefix="p8-ingress-") as directory:
            root = Path(directory)
            port, internal = free_port(), free_port()
            sock = root / "gateway.sock"
            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"
                def do_GET(self):
                    if self.path == "/ws/test":
                        key = self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                        self.send_response(101)
                        self.send_header("Upgrade", "websocket")
                        self.send_header("Connection", "Upgrade")
                        self.send_header("Sec-WebSocket-Accept", base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
                        self.end_headers()
                        self.wfile.write(b"\x81\x02ok")
                        self.wfile.flush()
                        self.close_connection = True
                        return
                    body = b'{"status":"ok"}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                def log_message(self, *args):
                    pass
            backend = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=backend.serve_forever, daemon=True).start()
            cfg = (STAGING / "Caddyfile").read_text().replace(":8080 {", f":{internal} {{")
            cfg = cfg.replace("market-api:8000", f"127.0.0.1:{backend.server_port}")
            root.joinpath("Caddyfile").write_text(cfg)
            env = {**os.environ, "STAGING_GATEWAY_SOCKET_BIND": f"unix/{sock}|0600",
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data")}
            caddy = proxy = None
            logs = root.joinpath("caddy.log").open("w+")
            def stop(process):
                if process is not None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            try:
                proxy = subprocess.Popen(["systemd-socket-activate", "-l", f"127.0.0.1:{port}",
                    "/usr/lib/systemd/systemd-socket-proxyd", str(sock)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(50):
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=.1):
                            break
                    except OSError:
                        time.sleep(.02)
                with redirect_stdout(io.StringIO()):
                    self.assertFalse(ingress_probe.probe(port))
                for _ in range(2):
                    caddy = subprocess.Popen([os.environ["CADDY_BIN"], "run", "--config", str(root / "Caddyfile"),
                        "--adapter", "caddyfile"], env=env, stdout=logs, stderr=logs)
                    success = False
                    for _ in range(60):
                        with redirect_stdout(io.StringIO()):
                            success = ingress_probe.probe(port)
                        if success or caddy.poll() is not None:
                            break
                        time.sleep(.05)
                    logs.flush()
                    self.assertTrue(success, root.joinpath("caddy.log").read_text())
                    self.assertTrue(sock.is_socket())
                    self.assertEqual(sock.stat().st_mode & 0o777, 0o600)
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/test", timeout=3) as response:
                        self.assertEqual(response.status, 200)
                    with socket.create_connection(("127.0.0.1", port), timeout=3) as websocket:
                        handshake = base64.b64encode(os.urandom(16)).decode()
                        websocket.sendall((f"GET /ws/test HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                            "Connection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\n"
                            f"Sec-WebSocket-Key: {handshake}\r\n\r\n").encode())
                        response = b""
                        while b"\r\n\r\n" not in response:
                            chunk = websocket.recv(4096)
                            self.assertTrue(chunk)
                            response += chunk
                        headers, body = response.split(b"\r\n\r\n", 1)
                        self.assertIn(b"101", headers.split(b"\r\n")[0])
                        while len(body) < 4:
                            chunk = websocket.recv(4 - len(body))
                            self.assertTrue(chunk)
                            body += chunk
                        self.assertEqual(body[:4], b"\x81\x02ok")
                    stop(caddy)
                    caddy = None
                    with redirect_stdout(io.StringIO()):
                        self.assertFalse(ingress_probe.probe(port))
            finally:
                stop(caddy)
                stop(proxy)
                backend.shutdown()
                backend.server_close()
                logs.close()


if __name__ == "__main__":
    unittest.main()
