from __future__ import annotations


class ApplicationError(RuntimeError):
    """Stable application-layer failure without an HTTP dependency."""


class InvalidInputError(ApplicationError):
    pass


class BadRequestError(ApplicationError):
    pass


class ResourceNotFoundError(ApplicationError):
    pass


class ResourceConflictError(ApplicationError):
    pass


class ResourceGoneError(ApplicationError):
    pass


class ServiceUnavailableError(ApplicationError):
    pass
