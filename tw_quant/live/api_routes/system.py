from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from ...auth import AccessTokenError, AuthorizationError
from ..api_context import ApiDependencies
from ..api_security import authorization_denied_response, page_permission


def build_system_router(deps: ApiDependencies) -> APIRouter:
    router = APIRouter()

    @router.get("/health/live", include_in_schema=False)
    async def liveness():
        return {"status": "ok"}

    @router.get("/internal/auth/cloudflare", include_in_schema=False)
    def cloudflare_origin_auth(request: Request):
        try:
            identity = deps.validator.authenticate(
                request.headers.get("cf-access-jwt-assertion")
            )
        except AccessTokenError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        original_uri = request.headers.get("x-original-uri", "/")
        original_path = original_uri.split("?", 1)[0]
        headers = {"X-Authenticated-Subject": identity.subject}
        if identity.email:
            headers["X-Authenticated-Email"] = identity.email
        if original_path == "/api/access-requests":
            return Response(status_code=204, headers=headers)
        try:
            user = deps.auth_service.identify(identity)
            permission = page_permission(original_path)
            if permission:
                deps.auth_service.require_permission(user, permission)
        except AuthorizationError as exc:
            return authorization_denied_response(original_uri, exc)
        if user.registered:
            headers["X-Authenticated-User-ID"] = user.user_id
            headers["X-Authenticated-Role"] = user.role.value
        return Response(status_code=204, headers=headers)

    @router.post("/api/access-requests", status_code=201)
    def submit_access_request(request: Request):
        try:
            access_request = deps.identity_repo.submit_access_request(
                request.state.access_identity
            )
        except ValueError as exc:
            status_code = 409 if "already registered" in str(exc) else 422
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        return {"request": access_request.to_message()}

    @router.get("/api/me")
    def current_user(request: Request):
        return request.state.auth_user.to_message(deps.auth_service.enforced)

    return router
