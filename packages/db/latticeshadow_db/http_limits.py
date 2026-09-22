"""Small HTTP guards shared by the two DB package servers."""

import ipaddress
from collections import deque

from starlette.responses import PlainTextResponse


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class NonlocalTlsGuard:
    """Reject direct HTTP requests from nonlocal clients, including direct Uvicorn use."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("scheme") != "https":
            client = scope.get("client")
            if client:
                try:
                    address = ipaddress.ip_address(client[0])
                except ValueError:
                    pass  # In-process ASGI test clients can use a symbolic name.
                else:
                    if not address.is_loopback:
                        await PlainTextResponse("HTTPS required", status_code=403)(scope, receive, send)
                        return
        await self.app(scope, receive, send)


class RequestBodyLimit:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared_length = int(value)
                except ValueError:
                    await PlainTextResponse("Invalid Content-Length", status_code=400)(scope, receive, send)
                    return
                if declared_length < 0:
                    await PlainTextResponse("Invalid Content-Length", status_code=400)(scope, receive, send)
                    return
                if declared_length > self.max_bytes:
                    await PlainTextResponse("Request body too large", status_code=413)(scope, receive, send)
                    return

        messages = deque()
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            total += len(message.get("body", b""))
            if total > self.max_bytes:
                await PlainTextResponse("Request body too large", status_code=413)(scope, receive, send)
                return
            messages.append(message)
            if not message.get("more_body", False):
                break

        async def replay():
            if messages:
                return messages.popleft()
            return await receive()

        await self.app(scope, replay, send)
