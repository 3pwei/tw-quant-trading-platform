from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    """Reject oversized HTTP bodies, including chunked requests."""

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError:
                await self._reject(
                    scope, receive, send, 400, "invalid Content-Length header"
                )
                return
            if content_length < 0:
                await self._reject(
                    scope, receive, send, 400, "invalid Content-Length header"
                )
                return
            if content_length > self.max_body_bytes:
                await self._too_large(scope, receive, send)
                return

        received = 0
        messages: list[Message] = []
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    await self._too_large(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break

        message_index = 0

        async def replay_receive() -> Message:
            nonlocal message_index
            if message_index < len(messages):
                message = messages[message_index]
                message_index += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _too_large(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        await JSONResponse(
            {
                "detail": "request body too large",
                "max_bytes": self.max_body_bytes,
            },
            status_code=413,
            headers={"Cache-Control": "no-store"},
        )(scope, receive, send)

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        detail: str,
    ) -> None:
        await JSONResponse(
            {"detail": detail},
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )(scope, receive, send)
