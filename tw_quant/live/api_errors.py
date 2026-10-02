from __future__ import annotations

from fastapi import HTTPException

from .application import (
    ApplicationError,
    BadRequestError,
    InvalidInputError,
    ResourceConflictError,
    ResourceGoneError,
    ResourceNotFoundError,
    ServiceUnavailableError,
)


def application_http_error(exc: ApplicationError) -> HTTPException:
    """Translate application failures at the HTTP boundary."""
    status_code = 500
    if isinstance(exc, BadRequestError):
        status_code = 400
    elif isinstance(exc, ResourceNotFoundError):
        status_code = 404
    elif isinstance(exc, ResourceConflictError):
        status_code = 409
    elif isinstance(exc, ResourceGoneError):
        status_code = 410
    elif isinstance(exc, InvalidInputError):
        status_code = 422
    elif isinstance(exc, ServiceUnavailableError):
        status_code = 503
    return HTTPException(status_code=status_code, detail=str(exc))
