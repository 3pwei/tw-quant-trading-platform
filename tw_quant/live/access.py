"""Backward-compatible imports for the application authentication module."""

from ..auth.access import (
    AccessIdentity,
    AccessTokenError,
    AccessValidator,
    CloudflareAccessValidator,
    DisabledAccessValidator,
)

__all__ = [
    "AccessIdentity",
    "AccessTokenError",
    "AccessValidator",
    "CloudflareAccessValidator",
    "DisabledAccessValidator",
]
