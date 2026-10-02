from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable


def mask_account(account_id: str | None) -> str | None:
    value = (account_id or "").strip()
    if not value:
        return None
    return f"****{value[-4:]}"


class SecretRedactionFilter(logging.Filter):
    """Redact known credential values from messages and structured arguments."""

    def __init__(self, secrets: Iterable[str | Path]):
        super().__init__()
        self._secrets = tuple(
            str(value) for value in secrets if str(value).strip()
        )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "[REDACTED]")
        record.msg = message
        record.args = ()
        return True
