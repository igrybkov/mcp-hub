"""Lazy proxy client — maintains ClientSession per child server.

Each connection is owned by a dedicated holder task. The holder opens its own
`stdio_client` / `sse_client` / `streamablehttp_client` context and publishes
the ready `ClientSession` back to the pool. It waits on a shutdown event and
tears the contexts down cleanly in its own task when that event fires.

This design sidesteps a subtle anyio constraint: transport contexts
(`stdio_client`, etc.) use `anyio.create_task_group()` internally, which must
be entered and exited in the same task. If we smuggled those contexts into a
shared `AsyncExitStack` entered by one task and exited by another, anyio
raises `RuntimeError: Attempted to exit cancel scope in a different task` and
child processes can be orphaned.

Public API is unchanged from the original tools-only `ProxyClient` — callers
still get a `ClientSession` back from `session(name)`, and the class is used
as an async context manager bounded to the main task.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from mcp import ClientSession, McpError, StdioServerParameters, types
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from mcp_hub.config import ServerSpec

logger = logging.getLogger(__name__)


METHOD_NOT_FOUND = -32601  # JSON-RPC standard code

# Factory returning ClientSession kwargs (callbacks) for a given server name.
# Example: logging_callback, sampling_callback, elicitation_callback, etc.
SessionCallbackFactory = Callable[[str], dict[str, Any]]


class _SessionHolder:
    """Per-server connection lifecycle — lives in its own task."""

    def __init__(
        self,
        spec: ServerSpec,
        session_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.spec = spec
        self.session_kwargs = session_kwargs or {}
        self.ready = asyncio.Event()
        self.shutdown = asyncio.Event()
        self.session: ClientSession | None = None
        self.error: BaseException | None = None
        self.task: asyncio.Task | None = None

    async def run(self) -> None:
        """Open transport + ClientSession, park until shutdown, then close."""
        try:
            async with _open_transport(self.spec) as streams:
                read, write = streams
                async with ClientSession(read, write, **self.session_kwargs) as session:
                    await session.initialize()
                    self.session = session
                    self.ready.set()
                    await self.shutdown.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.error = exc
            self.ready.set()  # unblock waiter so it can see the error
            raise


def _open_transport(spec: ServerSpec):
    """Return the async context manager for the spec's transport layer."""
    if spec.transport == "stdio":
        if not spec.command:
            raise ValueError(f"server {spec.name!r} missing 'command'")
        from mcp_hub.auth import resolve_auth, resolve_secrets

        injected: dict[str, str] = {}
        auth = resolve_auth(spec.name, spec.auth)
        if auth:
            injected = resolve_secrets(spec.name, auth)
        env = {**os.environ, **spec.env, **injected}
        params = StdioServerParameters(
            command=spec.command,
            args=list(spec.args),
            env=env,
        )
        return _StdioAdapter(stdio_client(params))
    if spec.transport == "streamable-http":
        if not spec.url:
            raise ValueError(f"server {spec.name!r} missing 'url'")
        return _HttpAdapter(streamablehttp_client(spec.url, headers=spec.headers or None))
    if spec.transport == "sse":
        if not spec.url:
            raise ValueError(f"server {spec.name!r} missing 'url'")
        return _SseAdapter(sse_client(spec.url, headers=spec.headers or None))
    raise ValueError(f"unsupported transport: {spec.transport}")


class _StdioAdapter:
    """Normalize (read, write) yielding across transports."""

    def __init__(self, cm) -> None:
        self._cm = cm

    async def __aenter__(self):
        self._streams = await self._cm.__aenter__()
        return self._streams

    async def __aexit__(self, *exc):
        return await self._cm.__aexit__(*exc)


class _SseAdapter(_StdioAdapter):
    """Same shape as stdio — already yields (read, write)."""


class _HttpAdapter:
    """`streamablehttp_client` yields (read, write, session_id_fn)."""

    def __init__(self, cm) -> None:
        self._cm = cm

    async def __aenter__(self):
        read, write, _ = await self._cm.__aenter__()
        return read, write

    async def __aexit__(self, *exc):
        return await self._cm.__aexit__(*exc)


class ProxyClient:
    """Manages per-server session-holder tasks for a set of configured servers."""

    def __init__(
        self,
        servers: dict[str, ServerSpec],
        session_callbacks: SessionCallbackFactory | None = None,
    ) -> None:
        self._servers = servers
        self._session_callbacks = session_callbacks
        self._holders: dict[str, _SessionHolder] = {}
        self._tool_cache: dict[str, list[types.Tool]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    def set_session_callbacks(self, factory: SessionCallbackFactory | None) -> None:
        """Install/replace the per-session callback factory.

        Useful when the factory needs to close over state that isn't available
        at ProxyClient construction time (e.g., HubState). Must be set before
        the first `session()` call — existing holders don't pick up changes.
        """
        self._session_callbacks = factory

    async def __aenter__(self) -> ProxyClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._closed = True
        # Signal shutdown, then cancel any holders still parked.
        for holder in self._holders.values():
            holder.shutdown.set()
        for holder in self._holders.values():
            task = holder.task
            if task is None or task.done():
                continue
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except TimeoutError:
                logger.warning("holder %r slow to close, cancelling", holder.spec.name)
                task.cancel()
                try:
                    await task
                except BaseException:
                    # Either CancelledError (normal) or a child exception — both
                    # are already logged elsewhere; cleanup continues regardless.
                    pass
            except BaseException:
                pass

    def _lock_for(self, name: str) -> asyncio.Lock:
        lock = self._locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[name] = lock
        return lock

    async def invalidate_server(self, name: str) -> None:
        """Tear down the live session for `name` and drop its tool cache. In-flight callers may see I/O errors as the transport closes."""
        self._tool_cache.pop(name, None)
        async with self._lock_for(name):
            holder = self._holders.pop(name, None)
        if holder is None:
            return
        holder.shutdown.set()
        task = holder.task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except TimeoutError:
            logger.warning("holder %r slow to invalidate, cancelling", name)
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        except BaseException:
            pass

    async def session(self, name: str) -> ClientSession:
        """Return a live session for `name`, spawning the holder task on first call."""
        if self._closed:
            raise RuntimeError("ProxyClient is closed")
        spec = self._servers.get(name)
        if spec is None:
            raise KeyError(f"unknown server: {name!r}")
        async with self._lock_for(name):
            holder = self._holders.get(name)
            if holder is None:
                kwargs = (
                    self._session_callbacks(name) if self._session_callbacks is not None else {}
                )
                holder = _SessionHolder(spec, session_kwargs=kwargs)
                holder.task = asyncio.create_task(holder.run(), name=f"mcp-hub-holder:{name}")
                self._holders[name] = holder
                logger.info("Connecting to server %r (%s)", name, spec.transport)
        await holder.ready.wait()
        if holder.error is not None:
            # Drop the holder so the next call can retry with a fresh task.
            self._holders.pop(name, None)
            raise holder.error
        assert holder.session is not None
        return holder.session

    # --- public API ---

    async def list_tools(self, name: str) -> list[types.Tool]:
        cached = self._tool_cache.get(name)
        if cached is not None:
            return cached
        session = await self.session(name)
        result = await session.list_tools()
        self._tool_cache[name] = list(result.tools)
        return self._tool_cache[name]

    async def call_tool(
        self, name: str, tool: str, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        session = await self.session(name)
        return await session.call_tool(tool, arguments=arguments)

    # --- prompts ---

    async def list_prompts(self, name: str) -> list[types.Prompt]:
        """Return child prompts, or [] if the child doesn't advertise any."""
        session = await self.session(name)
        try:
            result = await session.list_prompts()
        except McpError as exc:
            if _is_method_not_found(exc):
                return []
            raise
        return list(result.prompts)

    async def get_prompt(
        self, name: str, prompt_name: str, arguments: dict[str, str] | None
    ) -> types.GetPromptResult:
        session = await self.session(name)
        return await session.get_prompt(prompt_name, arguments=arguments)

    # --- resources ---

    async def list_resources(self, name: str) -> list[types.Resource]:
        session = await self.session(name)
        try:
            result = await session.list_resources()
        except McpError as exc:
            if _is_method_not_found(exc):
                return []
            raise
        return list(result.resources)

    async def list_resource_templates(self, name: str) -> list[types.ResourceTemplate]:
        session = await self.session(name)
        try:
            result = await session.list_resource_templates()
        except McpError as exc:
            if _is_method_not_found(exc):
                return []
            raise
        return list(result.resourceTemplates)

    async def read_resource(self, name: str, uri: str) -> types.ReadResourceResult:
        session = await self.session(name)
        from pydantic import AnyUrl

        return await session.read_resource(AnyUrl(uri))


def _is_method_not_found(err: McpError) -> bool:
    """Treat `method not found` as "this server doesn't support the primitive.\""""
    data = getattr(err, "error", None)
    code = getattr(data, "code", None) if data is not None else None
    return code == METHOD_NOT_FOUND
