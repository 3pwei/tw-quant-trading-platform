#!/usr/bin/env python3
"""Bounded loopback ingress checks. Never log response bodies or environment."""
import argparse
import http.client
import json
import signal
import socket


def probe(port: int = 18080) -> bool:
    for path, exact_body, check in (
        ("/healthz", b"ok", "gateway-ingress-health-failed"),
        ("/health/live", None, "gateway-upstream-health-failed"),
    ):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        status = "unavailable"
        actual = "connection-failed"
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            status = response.status
            body = response.read(4097)
            actual = "response-mismatch"
            if status == 200 and len(body) <= 4096 and (exact_body is None or body == exact_body):
                continue
        except (OSError, socket.timeout, http.client.HTTPException):
            pass
        finally:
            conn.close()
        print(f"P8_VERIFY_FAIL check={check} service=gateway expected=http200"
              f"{'-exact-ok' if exact_body is not None else ''} actual={actual} http_status={status}")
        return False
    print(json.dumps({"P8_INGRESS": "PASS", "host": "127.0.0.1", "port": port}))
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("loopback port must be unprivileged")
    def deadline(signum, frame):
        raise TimeoutError("ingress deadline")
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(8)
    try:
        raise SystemExit(0 if probe(args.port) else 1)
    finally:
        signal.alarm(0)
