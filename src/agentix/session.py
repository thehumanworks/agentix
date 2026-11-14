"""Modal state that is slow to build and safe to reuse.

Most of the time of a short operation goes to setup: the connection to Modal,
the app lookup, and, for each sandbox, a lookup plus a separate connection to
its command router. A `Session` builds each of these once and shares it between
all later operations. A long-lived process, such as the server, keeps one `Session`
for its lifetime.
"""

import asyncio
import contextlib
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable

import modal

from .defaults import APP_NAME
from .errors import AgentixError, InvalidRequest, NotFound, OperationFailed


class Session:
    def __init__(self, max_sandboxes: int = 128):
        """Keep at most `max_sandboxes` sandbox handles open."""
        self._app: dict[str, asyncio.Future[modal.App]] = {}
        self._sandboxes: OrderedDict[str, asyncio.Future[modal.Sandbox]] = OrderedDict()
        self._max_sandboxes = max_sandboxes
        self._background: set[asyncio.Task] = set()

    async def app(self) -> modal.App:
        return await _shared(
            self._app,
            APP_NAME,
            lambda: modal.App.lookup.aio(name=APP_NAME, create_if_missing=True),
        )

    @contextlib.asynccontextmanager
    async def sandbox(self, sandbox_id: str) -> AsyncIterator[modal.Sandbox]:
        """Lend the shared handle for `sandbox_id`.

        A failure that is not ours may mean that the handle is no longer usable,
        for example because the sandbox ended. The handle is then dropped, and
        the next use builds a new one.
        """
        handle = await _shared(
            self._sandboxes,
            sandbox_id,
            lambda: modal.Sandbox.from_id.aio(sandbox_id=sandbox_id),
        )
        self._sandboxes.move_to_end(sandbox_id)
        self._trim()
        try:
            yield handle
        except AgentixError:
            raise
        except Exception:
            self.forget(sandbox_id)
            raise

    def adopt(self, handle: modal.Sandbox) -> None:
        """Share the handle of a sandbox that this process just created."""
        ready: asyncio.Future[modal.Sandbox] = (
            asyncio.get_running_loop().create_future()
        )
        ready.set_result(handle)
        self._sandboxes[handle.object_id] = ready
        self._trim()

    def forget(self, sandbox_id: str) -> None:
        """Drop the handle for `sandbox_id` and release its connections."""
        handle = self._sandboxes.pop(sandbox_id, None)
        if handle is not None:
            self._in_background(_release(handle))

    async def close(self) -> None:
        for sandbox_id in list(self._sandboxes):
            self.forget(sandbox_id)
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    def _trim(self) -> None:
        while len(self._sandboxes) > self._max_sandboxes:
            self.forget(next(iter(self._sandboxes)))

    def _in_background(self, work: Awaitable[None]) -> None:
        task = asyncio.ensure_future(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)


async def _shared[K: Hashable, V](
    cache: dict[K, asyncio.Future[V]], key: K, build: Callable[[], Awaitable[V]]
) -> V:
    """Build the value for `key` once, also when several callers ask at once."""
    pending = cache.get(key)
    if pending is None:
        pending = cache[key] = asyncio.ensure_future(build())
    try:
        # A caller that goes away must not cancel the build for the others.
        return await asyncio.shield(pending)
    except BaseException:
        failed = pending.done() and (
            pending.cancelled() or pending.exception() is not None
        )
        if failed and cache.get(key) is pending:
            del cache[key]
        raise


async def _release(pending: asyncio.Future[modal.Sandbox]) -> None:
    with contextlib.suppress(Exception):
        handle = await pending
        await handle.detach.aio()


def describe(error: Exception) -> AgentixError:
    """Give a Modal failure the form that every adapter reports."""
    if isinstance(error, AgentixError):
        return error
    if isinstance(error, modal.exception.NotFoundError):
        return NotFound(str(error))
    if isinstance(error, modal.exception.InvalidError):
        return InvalidRequest(str(error))
    return OperationFailed(f"{type(error).__name__}: {error}")
