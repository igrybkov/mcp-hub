"""Cold-start enumeration + persistent recovery daemon.

For each server with `expose_prompts` or `expose_resources`, runs a recovery
daemon that:

1. Attempts to connect + enumerate, bounded by the server's
   `connect_timeout_seconds`.
2. On success, upserts the catalog and emits `list_changed` to the host (if the
   host session has been captured).
3. On failure, marks the server degraded and retries with exponential backoff
   (5s → 10s → … capped at 5min, with up to 1s jitter). This covers slow
   Docker cold starts, network hiccups, and crashed children.

`mark_enumeration_done()` is signalled once every exposed server has completed
its first attempt (success OR final failure), unblocking the cold-start soft
timeout in handlers.
"""

from __future__ import annotations

import asyncio
import logging
import random

from mcp_hub.config import ServerSpec
from mcp_hub.state import HubState

logger = logging.getLogger(__name__)

_INITIAL_BACKOFF = 5.0
_MAX_BACKOFF = 300.0


async def run_startup(state: HubState) -> None:
    """Launch recovery daemons for every exposed server.

    Returns immediately; daemons run until the hub shuts down. Signals
    `mark_enumeration_done()` once all first-attempts have finished so the
    cold-start soft timeout can unblock as soon as there's nothing more to
    wait for.
    """
    exposed = [s for s in state.servers.values() if s.is_exposed]
    if not exposed:
        state.mark_enumeration_done()
        return

    first_attempt_flags = [asyncio.Event() for _ in exposed]
    for spec, flag in zip(exposed, first_attempt_flags, strict=True):
        task = asyncio.create_task(
            _recovery_daemon(state, spec, flag),
            name=f"mcp-hub-recovery:{spec.name}",
        )
        state.track_daemon(task)

    # Signal cold-start-done as soon as every server has attempted once.
    async def _signal_when_all_done() -> None:
        await asyncio.gather(*(f.wait() for f in first_attempt_flags))
        state.mark_enumeration_done()

    state.track_daemon(
        asyncio.create_task(_signal_when_all_done(), name="mcp-hub-cold-start-signal")
    )


async def _recovery_daemon(
    state: HubState, spec: ServerSpec, first_attempt_done: asyncio.Event
) -> None:
    """Persistent per-server enumerate-with-retry loop."""
    delay = _INITIAL_BACKOFF
    while True:
        try:
            await enumerate_once(state, spec)
            delay = _INITIAL_BACKOFF  # reset on success
            first_attempt_done.set()
            # Success — keep the session alive (cached in ProxyClient) and
            # exit the retry loop. Disconnect detection / subscription to
            # list_changed notifications is a follow-up (Phase 2).
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "enumeration failed for %r: %s (retrying in %.1fs)",
                spec.name,
                exc,
                delay,
            )
            state.catalog.mark_degraded(spec.name, str(exc))
            await _emit_list_changed(state, spec)
            first_attempt_done.set()  # unblock cold-start even on failure
            jitter = random.uniform(0.0, 1.0)
            await asyncio.sleep(min(delay + jitter, _MAX_BACKOFF))
            delay = min(delay * 2.0, _MAX_BACKOFF)


async def enumerate_once(state: HubState, spec: ServerSpec) -> None:
    """Connect + enumerate prompts/resources for a single server.

    Bounded by `spec.connect_timeout_seconds`. Raises on timeout or transport
    error — caller's retry loop handles backoff.
    """
    async with asyncio.timeout(spec.connect_timeout_seconds):
        prompts = None
        resources = None
        templates = None
        if spec.expose_prompts:
            prompts = await state.proxy.list_prompts(spec.name)
        if spec.expose_resources:
            resources = await state.proxy.list_resources(spec.name)
            templates = await state.proxy.list_resource_templates(spec.name)

    changed = state.catalog.upsert_server(
        spec.name,
        status="ok",
        prompts=prompts,
        resources=resources,
        resource_templates=templates,
        error=None,
    )
    logger.info(
        "enumerated %r: prompts=%d resources=%d templates=%d changed=%s",
        spec.name,
        len(prompts or []),
        len(resources or []),
        len(templates or []),
        changed,
    )
    if changed:
        await _emit_list_changed(state, spec)


async def _emit_list_changed(state: HubState, spec: ServerSpec) -> None:
    """Notify the host that one of the hub's catalogs has updated.

    Uses `enqueue_or_send` so notifications emitted before the host has been
    captured (common on cold start — enumeration often finishes before any
    handler runs) are buffered and flushed when the host connects.
    """
    if spec.expose_prompts:
        state.enqueue_or_send(lambda s: s.send_prompt_list_changed())
    if spec.expose_resources:
        state.enqueue_or_send(lambda s: s.send_resource_list_changed())
