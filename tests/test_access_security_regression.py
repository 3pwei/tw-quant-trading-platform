"""Exercise the real JWT/JWKS boundary using generated keys and no network."""

from datetime import datetime, timedelta, timezone
from email.message import Message
from io import BytesIO
import json
import unittest
from unittest.mock import patch
import urllib.request
from urllib.response import addinfourl

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from tw_quant.auth.access import AccessTokenError, CloudflareAccessValidator


class AccessSecurityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.validator = CloudflareAccessValidator(
            "team.cloudflareaccess.com", "expected-audience"
        )
        now = datetime.now(timezone.utc)
        self.claims = {
            "iss": self.validator.issuer, "aud": ["expected-audience"],
            "sub": "synthetic-user", "email": "reader@example.com",
            "iat": now, "exp": now + timedelta(minutes=5),
        }
        self.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwk.update(kid="current-key", use="sig", alg="RS256")

    def token(self, claims=None, kid="current-key", key=None):
        return jwt.encode(
            self.claims if claims is None else claims,
            self.key if key is None else key,
            algorithm="RS256", headers={"kid": kid},
        )

    def authenticate(self, token, keys=None):
        payload = {"keys": [self.jwk] if keys is None else keys}
        with patch("jwt.jwks_client.urllib.request.build_opener") as factory:
            factory.return_value.open.return_value = BytesIO(json.dumps(payload).encode())
            return self.validator.authenticate(token)

    def test_valid_signature_and_cached_jwks_preserve_identity(self):
        self.assertEqual(self.authenticate(self.token()).subject, "synthetic-user")
        with patch("jwt.jwks_client.urllib.request.build_opener") as factory:
            self.assertEqual(self.validator.authenticate(self.token()).email, "reader@example.com")
            factory.assert_not_called()

    def test_invalid_signature_is_denied(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaises(AccessTokenError):
            self.authenticate(self.token(key=other))

    def test_wrong_issuer_expired_and_future_assertions_are_denied(self):
        now = datetime.now(timezone.utc)
        for override in (
            {"iss": "https://other.cloudflareaccess.com"},
            {"exp": now - timedelta(minutes=1)},
            {"nbf": now + timedelta(minutes=1)},
            {"iat": now + timedelta(minutes=1)},
        ):
            with self.subTest(override=tuple(override)):
                with self.assertRaises(AccessTokenError):
                    self.authenticate(self.token({**self.claims, **override}))

    def test_required_claims_cannot_be_omitted(self):
        for claim in ("exp", "iat", "sub", "aud", "iss"):
            claims = dict(self.claims)
            del claims[claim]
            with self.subTest(claim=claim), self.assertRaises(AccessTokenError):
                self.authenticate(self.token(claims))

    def test_malformed_time_claims_fail_closed(self):
        for claim in ("exp", "iat", "nbf"):
            for value in (None, [], {}):
                with self.subTest(claim=claim, value=value), self.assertRaises(AccessTokenError):
                    self.authenticate(self.token({**self.claims, claim: value}))

    def test_hmac_and_unsigned_tokens_cannot_cross_rs256_boundary(self):
        for algorithm, key in (("HS256", b"synthetic-test-key-material-32bytes"), ("none", None)):
            token = jwt.encode(self.claims, key, algorithm=algorithm, headers={"kid": "current-key"})
            with self.subTest(algorithm=algorithm), self.assertRaises(AccessTokenError):
                self.authenticate(token)

    def test_bad_jwks_member_does_not_discard_valid_key(self):
        bad_key = {"kty": "RSA", "kid": "broken", "n": [], "e": "AQAB"}
        self.assertEqual(
            self.authenticate(self.token(), [None, bad_key, self.jwk]).subject,
            "synthetic-user",
        )

    def test_unknown_kids_are_denied_without_repeated_refresh(self):
        payload = json.dumps({"keys": [self.jwk]}).encode()
        with patch("jwt.jwks_client.urllib.request.build_opener") as factory:
            factory.return_value.open.side_effect = lambda *a, **kw: BytesIO(payload)
            self.validator.authenticate(self.token())
            for kid in ("unknown-1", "unknown-2", "unknown-3"):
                with self.assertRaises(AccessTokenError):
                    self.validator.authenticate(self.token(kid=kid))
            self.assertEqual(factory.return_value.open.call_count, 1)

    def test_new_key_is_accepted_after_refresh_cooldown(self):
        rotated = {**self.jwk, "kid": "rotated-key"}
        payloads = [{"keys": [self.jwk]}, {"keys": [self.jwk, rotated]}]
        with patch("jwt.jwks_client.time.monotonic", return_value=100) as clock:
            with patch("jwt.jwks_client.urllib.request.build_opener") as factory:
                factory.return_value.open.side_effect = [
                    BytesIO(json.dumps(payload).encode()) for payload in payloads
                ]
                self.validator.authenticate(self.token())
                clock.return_value = 131
                identity = self.validator.authenticate(self.token(kid="rotated-key"))
                self.assertEqual(identity.subject, "synthetic-user")
                self.assertEqual(factory.return_value.open.call_count, 2)

    def test_jwks_redirect_is_rejected_without_contacting_destination(self):
        requested = []

        class RedirectFixture(urllib.request.HTTPSHandler):
            def https_open(self, request):
                requested.append(request.full_url)
                headers = Message()
                headers["Location"] = "https://untrusted.example.com/keys"
                response = addinfourl(BytesIO(b""), headers, request.full_url, 302)
                response.msg = "Found"
                return response

        real_build_opener = urllib.request.build_opener
        with patch(
            "jwt.jwks_client.urllib.request.build_opener",
            side_effect=lambda *handlers: real_build_opener(*handlers, RedirectFixture()),
        ):
            with self.assertRaises(AccessTokenError):
                self.validator.authenticate(self.token())
        self.assertEqual(requested, [f"{self.validator.issuer}/cdn-cgi/access/certs"])
