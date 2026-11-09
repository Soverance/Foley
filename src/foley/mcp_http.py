"""The shared MCP endpoint: `foley browse` serves the MCP tools at /mcp.

docs/design.md, "One shared server". Every Claude Code session on the network talks to
this one process instead of starting its own `foley-mcp`.

CONTRACT:

    MCP_PATH = "/mcp"
    TOKEN_FILE = "mcp_token"           in cfg.data_dir
    MAX_BODY_BYTES                     larger POST bodies are refused (413) by browse
    CALL_TIMEOUT_S                     a request still unanswered after this gets 504

    token_path(cfg) -> Path
    load_or_create_token(cfg) -> str
        The bearer token, read from TOKEN_FILE; created (random, URL-safe) on first use.
    authorized(header: str | None, token: str) -> bool
        Whether an Authorization header is exactly "Bearer <token>" (constant-time).

    class McpEndpoint(cfg)
        start()   idempotent; starts the worker thread and waits until it is ready.
                  Raises what the worker raised when it could not start.
        handle(method, headers: list[(name, value)], body: bytes, client=(host, port))
                  -> (status: int, headers: list[(name, value)], body: bytes)
                  Runs one HTTP request through the MCP SDK's Streamable HTTP handler in
                  stateless JSON mode and waits for its response. Starts the worker when
                  needed. Never raises for protocol errors: those are JSON-RPC errors or
                  4xx statuses from the SDK.
        stop()    stops the worker (in-flight calls are abandoned) and closes its
                  catalog connection. A later handle() starts a new one.

    Routing, Host/Origin checks and the token check are browse's job (browse.py); this
    module only speaks MCP.

DESIGN NOTES:

    One worker thread owns an asyncio loop, the SDK server built by
    mcp_server.build_server(shared=True), and (through mcp_server's module state) the
    catalog connection, so every tool runs on that thread in arrival order. SQLite
    connections are thread-bound; this is the simplest correct sharing. Browse request
    threads block on a concurrent future until their call is answered.
    The SDK's own DNS-rebinding check is off because browse already checks Host and
    Origin against its own allow-list, which knows the configured studio host names.
    The SDK app is a Starlette app; it is called directly as ASGI, without uvicorn and
    without a lifespan, so the session manager is run here instead.
    Importing this module imports neither the SDK server nor torch.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
import threading
from pathlib import Path

MCP_PATH = "/mcp"
TOKEN_FILE = "mcp_token"
MAX_BODY_BYTES = 4 * 1024 * 1024
CALL_TIMEOUT_S = 600.0
START_TIMEOUT_S = 60.0

log = logging.getLogger("foley.mcp")


def token_path(cfg) -> Path:
    return cfg.data_dir / TOKEN_FILE


def load_or_create_token(cfg) -> str:
    path = token_path(cfg)
    try:
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # created meanwhile by another process: use that one
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    return token


def authorized(header: str | None, token: str) -> bool:
    if not header or not token:
        return False
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(value.strip().encode("utf-8"), token.encode("utf-8"))


class McpEndpoint:
    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app = None
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self._error: BaseException | None = None

    # ----------------------------------------------------------------- lifecycle

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and self._app is not None:
                return
            self._ready = threading.Event()
            self._error = None
            self._app = None
            self._thread = threading.Thread(target=self._run, name="foley-mcp", daemon=True)
            self._thread.start()
            if not self._ready.wait(START_TIMEOUT_S):
                raise RuntimeError("the MCP worker did not start in time")
            if self._error is not None:
                raise RuntimeError(f"the MCP worker could not start: {self._error}")

    def stop(self) -> None:
        with self._lock:
            loop, stop_event, thread = self._loop, self._stop_event, self._thread
            if loop is not None and stop_event is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(stop_event.set)
                except RuntimeError:  # loop already closed
                    pass
            if thread is not None:
                thread.join(timeout=10)
            self._thread = None
            self._app = None

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except BaseException as e:  # noqa: BLE001 - reported through start()
            if not self._ready.is_set():
                self._error = e
            else:
                log.exception("the MCP worker stopped")
        finally:
            self._ready.set()
            loop.close()

    async def _main(self) -> None:
        from mcp.server.transport_security import TransportSecuritySettings

        from foley import mcp_server

        mcp_server.set_config(self.cfg)
        try:
            server = mcp_server.build_server(shared=True)
            app = server.streamable_http_app(
                streamable_http_path=MCP_PATH,
                json_response=True,
                stateless_http=True,
                transport_security=TransportSecuritySettings(
                    enable_dns_rebinding_protection=False),
            )
            self._stop_event = asyncio.Event()
            async with server.session_manager.run():
                self._app = app
                self._ready.set()
                log.info("foley browse: MCP endpoint ready at %s", MCP_PATH)
                await self._stop_event.wait()
        finally:
            mcp_server.close()

    # ----------------------------------------------------------------- requests

    def handle(self, method: str, headers: list[tuple[str, str]], body: bytes,
               client: tuple[str, int] = ("127.0.0.1", 0)) -> tuple[int, list, bytes]:
        self.start()
        loop = self._loop
        future = asyncio.run_coroutine_threadsafe(
            self._call(method, headers, body, client), loop)
        try:
            return future.result(timeout=CALL_TIMEOUT_S)
        except TimeoutError:
            future.cancel()
            return 504, [("content-type", "application/json")], \
                b'{"error": "the MCP call took too long"}'

    async def _call(self, method: str, headers: list[tuple[str, str]], body: bytes,
                    client: tuple[str, int]) -> tuple[int, list, bytes]:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": MCP_PATH,
            "raw_path": MCP_PATH.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [(k.lower().encode("latin-1"), v.encode("latin-1"))
                        for k, v in headers],
            "client": client,
            "server": ("127.0.0.1", 0),
        }
        done = asyncio.Event()
        delivered = False
        status = 500
        out_headers: list[tuple[str, str]] = []
        chunks: list[bytes] = []

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal status, out_headers
            if message["type"] == "http.response.start":
                status = int(message["status"])
                out_headers = [(k.decode("latin-1"), v.decode("latin-1"))
                               for k, v in message.get("headers", [])]
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    done.set()

        try:
            await self._app(scope, receive, send)
        finally:
            done.set()
        return status, out_headers, b"".join(chunks)
