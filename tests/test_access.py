from datetime import datetime, timedelta, timezone
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from tw_quant.live.access import AccessTokenError, CloudflareAccessValidator


class _SigningKey:
    def __init__(self, key):
        self.key = key


class _StaticKeyClient:
    def __init__(self, key):
        self.key = key

    def get_signing_key_from_jwt(self, token):
        return _SigningKey(self.key)


class CloudflareAccessValidatorTests(unittest.TestCase):
    def setUp(self):
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.validator = CloudflareAccessValidator(
            "team.cloudflareaccess.com", "expected-audience"
        )
        self.validator._keys = _StaticKeyClient(self.private_key.public_key())

    def token(self, **overrides):
        now = datetime.now(timezone.utc)
        claims = {
            "iss": "https://team.cloudflareaccess.com",
            "aud": ["expected-audience"],
            "sub": "user-123",
            "email": "owner@example.com",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        }
        claims.update(overrides)
        return jwt.encode(claims, self.private_key, algorithm="RS256")

    def test_accepts_valid_signed_assertion(self):
        identity = self.validator.authenticate(self.token())
        self.assertEqual(identity.subject, "user-123")
        self.assertEqual(identity.email, "owner@example.com")

    def test_rejects_missing_assertion(self):
        with self.assertRaisesRegex(AccessTokenError, "missing"):
            self.validator.authenticate(None)

    def test_rejects_wrong_audience(self):
        with self.assertRaisesRegex(AccessTokenError, "invalid"):
            self.validator.authenticate(self.token(aud=["other-application"]))


if __name__ == "__main__":
    unittest.main()
