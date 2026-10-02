#!/usr/bin/env python3
"""Verify the public user path without disclosing origin details."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit


REQUIRED_HEADERS = {
    "content-security-policy": None,
    "permissions-policy": None,
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "x-content-type-options": "nosniff",
}


class VerificationError(RuntimeError):
    def __init__(self, layer: str, reason: str) -> None:
        super().__init__(reason)
        self.layer = layer
        self.reason = reason


@dataclass(frozen=True)
class ProbeResult:
    status: int
    body: str
    headers: dict[str, str]
    redirects: int


def validate_public_base_url(raw: str) -> tuple[str, str, int]:
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.hostname:
        raise VerificationError("configuration", "public_target_must_be_https_hostname")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise VerificationError("configuration", "public_target_contains_forbidden_components")
    if parsed.path not in ("", "/"):
        raise VerificationError("configuration", "public_target_must_not_contain_path")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise VerificationError("configuration", "loopback_target_forbidden")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        raise VerificationError("configuration", "ip_literal_target_forbidden")
    return host, f"https://{host}{':' + str(parsed.port) if parsed.port else ''}/healthz", parsed.port or 443


def resolve_families(host: str, port: int) -> dict[str, bool]:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise VerificationError("dns", "hostname_resolution_failed") from exc
    for answer in answers:
        address = ipaddress.ip_address(answer[4][0])
        if not address.is_global:
            raise VerificationError("dns", "hostname_resolved_to_non_public_address")
    return {
        "a": any(item[0] == socket.AF_INET for item in answers),
        "aaaa": any(item[0] == socket.AF_INET6 for item in answers),
    }


def classify(status: int, headers: dict[str, str]) -> str:
    if status != 502:
        return "public_origin"
    server = headers.get("server", "").lower()
    if "cloudflare" in server or "cf-ray" in headers:
        return "edge_proxy"
    return "gateway_or_origin"


def probe(url: str, expected_host: str, timeout: float, max_redirects: int = 2) -> ProbeResult:
    current = url
    redirects = 0
    context = ssl.create_default_context()
    while True:
        parsed = urlsplit(current)
        if parsed.scheme != "https" or parsed.hostname != expected_host:
            raise VerificationError("redirect", "redirect_left_formal_hostname")
        connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout, context=context)
        try:
            connection.request("GET", parsed.path or "/", headers={"User-Agent": "tw-quant-deployment-verifier/1"})
            response = connection.getresponse()
            body = response.read(4096).decode("utf-8", "replace").strip()
            headers = {key.lower(): value.strip() for key, value in response.getheaders()}
        except ssl.SSLError as exc:
            raise VerificationError("tls", "tls_verification_failed") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise VerificationError("transport", "public_connection_failed") from exc
        finally:
            connection.close()
        if response.status not in (301, 302, 303, 307, 308):
            return ProbeResult(response.status, body, headers, redirects)
        location = headers.get("location")
        redirects += 1
        if not location or redirects > max_redirects:
            raise VerificationError("redirect", "redirect_policy_failed")
        current = urljoin(current, location)


def validate_result(result: ProbeResult) -> None:
    if result.status != 200:
        raise VerificationError(classify(result.status, result.headers), f"unexpected_http_{result.status}")
    if result.body != "ok":
        raise VerificationError("public_origin", "health_body_mismatch")
    for header, expected in REQUIRED_HEADERS.items():
        value = result.headers.get(header, "")
        if not value or (expected is not None and value.lower() != expected):
            raise VerificationError("security_headers", f"invalid_{header}")


def sanitized_event(**values: object) -> str:
    allowed = {"attempt", "status", "layer", "reason", "a", "aaaa", "redirects"}
    return json.dumps({key: values[key] for key in values if key in allowed}, sort_keys=True)


def verify(base_url: str, attempts: int, timeout: float, retry_delay: float) -> None:
    host, health_url, port = validate_public_base_url(base_url)
    families = resolve_families(host, port)
    print(sanitized_event(layer="dns", **families))
    last_error: VerificationError | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = probe(health_url, host, timeout)
            validate_result(result)
            print(sanitized_event(layer="external_public", attempt=attempt, status=200, redirects=result.redirects))
            return
        except VerificationError as exc:
            last_error = exc
            print(sanitized_event(layer=exc.layer, attempt=attempt, reason=exc.reason))
            if attempt < attempts:
                time.sleep(retry_delay)
    assert last_error is not None
    raise last_error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--attempts", type=int, default=9)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--retry-delay", type=float, default=5.0)
    args = parser.parse_args()
    if args.attempts < 1 or args.attempts > 12 or args.timeout <= 0 or args.timeout > 30 or args.retry_delay < 0 or args.retry_delay > 30:
        parser.error("retry and timeout bounds exceeded")
    try:
        verify(args.base_url, args.attempts, args.timeout, args.retry_delay)
    except VerificationError as exc:
        print(sanitized_event(layer=exc.layer, reason=exc.reason))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
