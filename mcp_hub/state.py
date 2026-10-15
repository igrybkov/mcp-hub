"""Shared hub runtime state — catalog, proxy, host-session reference.

The host's ServerSession is created inside `Server.run()` by the SDK and not
directly exposed. To send background notifications (prompts/list_changed,
resources/list_changed), we capture the session from the `ServerRequestContext`
the SDK hands each handler, and reuse that reference for subsequent
notifications. `server.py` does the capture at the handler boundary, so the
handler modules themselves stay free of SDK context plumbing.

The cold-start event gates the first `list_prompts` / `list_resources` call:

- Warm start (catalog hit): event is pre-set, handlers return immediately.
- Cold start (no cache): handlers wait up to `cold_start_soft_timeout` for the
  event, then return whatever the catalog has — which may be partial or empty.
  The recovery daemon keeps enumerating in the background and emits
  `list_changed` whenever a late server arrives.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from mcp_hub.catalog import Catalog
from mcp_hub.config import ServerSpec
from mcp_hub.proxy import ProxyClient

if TYPE_CHECKING:
    from mcp import types
    from mcp.server.session import ServerSession

logger = logging.getLogger(__name__)

# Bounded buffer for pre-connection notifications. If more than this arrive
# before the host is known, oldest are dropped — better than an unbounded
# buffer leaking memory when the host never connects.
PENDING_BUFFER_MAX = 128

PendingFn = Callable[["ServerSession"], Awaitable[Any]]


class HubState:
    def __init__(
        self,
        servers: dict[str, ServerSpec],
        catalog: Catalog,
        proxy: ProxyClient,
        cold_start_soft_timeout: float = 3.0,
    ) -> None:
        self.servers = servers
        self.catalog = catalog
        self.proxy = proxy
        self.cold_start_soft_timeout = cold_start_soft_timeout
        self.current_log_level: types.LoggingLevel | None = None
        self._host_session: ServerSession | None = None
        self._first_enumeration_done = asyncio.Event()
        self._daemon_tasks: list[asyncio.Task] = []
        # Notifications that arrive before the host session is captured. Flushed
        # (in order) as soon as we have a session. Bounded so a host that never
        # connects can't leak memory.
        self._pending: deque[PendingFn] = deque(maxlen=PENDING_BUFFER_MAX)

    @property
    def host_session(self) -> ServerSession | None:
        return self._host_session

    def capture_host_session(self, session: ServerSession) -> None:
        """Record the host ServerSession on first handler invocation.

        Drains any notifications that were buffered while we were waiting.
        """
        if self._host_session is not None:
            return
        self._host_session = session
        logger.debug("captured host ServerSession for background notifications")
        if self._pending:
            logger.info("flushing %d buffered notification(s) to host", len(self._pending))
            pending = list(self._pending)
            self._pending.clear()
            for fn in pending:
                asyncio.create_task(self._invoke_pending(fn, session))

    async def _invoke_pending(self, fn: PendingFn, session: ServerSession) -> None:
        try:
            await fn(session)
        except Exception as exc:
            logger.debug("buffered notification failed during flush: %s", exc)

    def enqueue_or_send(self, fn: PendingFn) -> None:
        """If the host is known, invoke `fn(session)` now; otherwise buffer it.

        `fn` should be an async function that takes a ServerSession and
        performs the notification call (e.g., `send_log_message`,
        `send_prompt_list_changed`). Callers don't need to special-case the
        "host not yet connected" path themselves.
        """
        if self._host_session is not None:
            asyncio.create_task(self._invoke_pending(fn, self._host_session))
        else:
            self._pending.append(fn)

    def mark_enumeration_done(self) -> None:
        self._first_enumeration_done.set()

    async def wait_for_cold_start_settle(self) -> None:
        """Block up to the soft timeout for the first enumeration to finish.

        On warm start, `mark_enumeration_done()` is called before any handler
        runs and this returns immediately. On cold start, enumeration is in
        progress in the background; this waits, but never longer than
        `cold_start_soft_timeout`. After the deadline, handlers return the
        partial catalog as-is — late servers surface via list_changed later.
        """
        try:
            await asyncio.wait_for(
                self._first_enumeration_done.wait(),
                timeout=self.cold_start_soft_timeout,
            )
        except TimeoutError:
            logger.info(
                "cold-start soft timeout (%.1fs) elapsed — serving partial catalog",
                self.cold_start_soft_timeout,
            )

    def track_daemon(self, task: asyncio.Task) -> None:
        self._daemon_tasks.append(task)

    async def shutdown(self) -> None:
        for task in self._daemon_tasks:
            task.cancel()
        for task in self._daemon_tasks:
            try:
                await task
            except BaseException:
                # CancelledError on normal shutdown, or a daemon-level error
                # surfaced at teardown — either way, cleanup continues.
                pass
        self._daemon_tasks.clear()
