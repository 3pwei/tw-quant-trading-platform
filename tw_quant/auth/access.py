from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import jwt
from jwt import PyJWKClient


@dataclass(frozen=True)
class AccessIdentity:
    subject: str
    email: str | None = None


class AccessTokenError(ValueError):
    """Raised when a Cloudflare Access assertion cannot be trusted."""


class AccessValidator(Protocol):
    def authenticate(self, token: str | None) -> AccessIdentity: ...


class DisabledAccessValidator:
    def authenticate(self, token: str | None) -> AccessIdentity:
        return AccessIdentity(subject="access-disabled")


class CloudflareAccessValidator:
    """Validate Cloudflare Access JWT assertions at the origin."""

    def __init__(self, team_domain: str, audience: str) -> None:
        host = team_domain.removeprefix("https://").rstrip("/")
        self.issuer = f"https://{host}"
        self.audience = audience
        self._keys = PyJWKClient(
            f"{self.issuer}/cdn-cgi/access/certs",
            cache_keys=True,
            lifespan=300,
        )

    def authenticate(self, token: str | None) -> AccessIdentity:
        if not token:
            raise AccessTokenError("missing Cloudflare Access assertion")
        try:
            signing_key = self._keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise AccessTokenError("invalid Cloudflare Access assertion") from exc

        subject = claims.get("sub")
        email = claims.get("email")
        if not isinstance(subject, str) or not subject:
            raise AccessTokenError("Cloudflare Access assertion has no subject")
        if email is not None and not isinstance(email, str):
            raise AccessTokenError("Cloudflare Access email claim is invalid")
        return AccessIdentity(subject=subject, email=email)
