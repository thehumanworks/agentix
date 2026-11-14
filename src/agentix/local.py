"""Run operations in this process, for a caller that makes one and exits."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from . import sandbox, ssh
from .session import Session, describe


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
    ) -> dict[str, str]:
        sandbox_id = _run(
            lambda session: sandbox.create(
                session, image_name, secret_name, timeout, workdir, memory, cpu
            )
        )
        return {"sandbox_id": sandbox_id}

    def list_sandboxes(self) -> list[str]:
        return _run(sandbox.list_ids)

    def execute(self, sandbox_id: str, args: Sequence[str]) -> dict[str, Any]:
        return dict(_run(lambda session: sandbox.execute(session, sandbox_id, args)))

    def terminate(self, sandbox_id: str) -> dict[str, str]:
        return dict(_run(lambda session: sandbox.terminate(session, sandbox_id)))

    def ssh(
        self,
        action: str,
        sandbox_id: str,
        engine: str,
        hostname: str = "",
        public_key: str = "",
    ) -> dict[str, Any]:
        return _run(
            lambda session: ssh.manage(
                session, action, sandbox_id, engine, hostname, public_key
            )
        )
