"""Run operations in this process, for a caller that makes one and exits.

Every operation that takes a sandbox also takes the name of one.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from . import lifecycle, sandbox, ssh
from .session import Session, describe
from .store import Options


def _run[T](operation: Callable[[Session], Awaitable[T]]) -> T:
    async def run() -> T:
        try:
            return await operation(Session())
        except Exception as error:
            raise describe(error) from error

    return asyncio.run(run())


class Local:
    def create_sandbox(
        self,
        image_name: str,
        secret_name: str,
        timeout: int,
        workdir: str,
        memory: int,
        cpu: float,
        name: str = "",
        tailscale: bool = False,
        hostname: str = "",
    ) -> dict[str, Any]:
        options: Options = {
            "image_name": image_name,
            "secret_name": secret_name,
            "timeout": timeout,
            "workdir": workdir,
            "memory": memory,
            "cpu": cpu,
        }
        return _run(
            lambda session: lifecycle.up(session, options, name, tailscale, hostname)
        )

    def list_sandboxes(self) -> list[dict[str, Any]]:
        return _run(lifecycle.ls)

    def execute(self, target: str, args: Sequence[str]) -> dict[str, Any]:
        async def run(session: Session):
            sandbox_id = await lifecycle.resolve(session, target)
            return dict(await sandbox.execute(session, sandbox_id, args))

        return _run(run)

    def terminate(self, target: str) -> dict[str, Any]:
        return _run(lambda session: lifecycle.down(session, target))

    def ssh(
        self,
        action: str,
        target: str,
        engine: str,
        hostname: str = "",
        public_key: str = "",
    ) -> dict[str, Any]:
        async def run(session: Session):
            sandbox_id = await lifecycle.resolve(session, target)
            return await ssh.manage(
                session, action, sandbox_id, engine, hostname, public_key
            )

        return _run(run)

    def snapshot(self, target: str) -> dict[str, Any]:
        return _run(lambda session: lifecycle.take_snapshot(session, target))

    def snapshots(self, target: str) -> list[dict[str, Any]]:
        return [dict(item) for item in _run(lambda s: lifecycle.snapshots(s, target))]

    def restore(
        self, target: str, snapshot_id: str, tailscale: bool, hostname: str
    ) -> dict[str, Any]:
        return _run(
            lambda session: lifecycle.restore(
                session, target, snapshot_id, tailscale, hostname
            )
        )

    def purge(self, target: str, terminate: bool) -> dict[str, Any]:
        return _run(lambda session: lifecycle.purge(session, target, terminate))
