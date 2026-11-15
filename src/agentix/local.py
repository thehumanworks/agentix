"""Run operations in this process, for a caller that makes one and exits.

Every operation that takes a sandbox also takes the name of one.
"""

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from . import lifecycle, names, runs, sandbox, ssh
from . import resume as resume_module
from . import sync as sync_module
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
        exit_snapshot: bool = True,
        ephemeral: bool = False,
        repo: str = "",
        idle_stop: int = 0,
    ) -> dict[str, Any]:
        options: Options = {
            "image_name": image_name,
            "secret_name": secret_name,
            "timeout": timeout,
            "workdir": workdir,
            "memory": memory,
            "cpu": cpu,
            "exit_snapshot": exit_snapshot,
        }
        return _run(
            lambda session: lifecycle.up(
                session, options, name, tailscale, hostname, ephemeral, repo, idle_stop
            )
        )

    def list_sandboxes(self) -> list[dict[str, Any]]:
        return _run(lifecycle.ls)

    def execute(self, target: str, args: Sequence[str]) -> dict[str, Any]:
        async def run(session: Session):
            sandbox_id = await lifecycle.resolve(session, target)
            return dict(await sandbox.execute(session, sandbox_id, args))

        return _run(run)

    def execute_resumable(
        self,
        target: str,
        args: Sequence[str],
        resume: object,
        notify: resume_module.Notify,
        output: resume_module.Output,
    ) -> dict[str, Any]:
        return _run(
            lambda session: resume_module.start(
                session, target, args, resume, notify, output
            )
        )

    def resume(
        self, target: str, notify: resume_module.Notify, output: resume_module.Output
    ) -> dict[str, Any]:
        return _run(
            lambda session: resume_module.resume(session, target, notify, output)
        )

    def start_run(self, target: str, args: Sequence[str]) -> dict[str, Any]:
        return _run(lambda session: runs.start(session, target, args))

    def follow_run(self, run_id: str, write: Callable[[str, str], None]) -> None:
        return _run(lambda _: runs.follow(run_id, write))

    def wait_run(self, run_id: str) -> dict[str, Any]:
        return _run(lambda _: runs.wait(run_id))

    def runs(self, target: str, clean: bool) -> list[dict[str, Any]]:
        operation = runs.clean if clean else runs.ls
        return _run(lambda session: operation(session, target))

    def deploy_runner(self) -> dict[str, str]:
        return _run(lambda _: runs.deploy())

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
            # A sandbox given by its ID is asked for its name, if it needs one.
            name = None if names.is_sandbox_id(target) else target
            return await ssh.manage(
                session, action, sandbox_id, engine, hostname, public_key, name
            )

        return _run(run)

    def shell(
        self, target: str, engine: str, hostname: str = "", public_key: str = ""
    ) -> dict[str, Any]:
        """How to log in to a running sandbox with ssh, once it answers.

        Without a target, the only sandbox that runs. See `ssh.connect`.
        """

        async def run(session: Session):
            sandbox_id, name = await lifecycle.running(session, target)
            return await ssh.connect(
                session, sandbox_id, name, engine, hostname, public_key
            )

        return _run(run)

    def snapshot(self, target: str) -> dict[str, Any]:
        return _run(lambda session: lifecycle.take_snapshot(session, target))

    def snapshots(self, target: str) -> list[dict[str, Any]]:
        return [dict(item) for item in _run(lambda s: lifecycle.snapshots(s, target))]

    def restore(
        self,
        target: str,
        snapshot_id: str,
        tailscale: bool,
        hostname: str,
        idle_stop: int | None = None,
    ) -> dict[str, Any]:
        return _run(
            lambda session: lifecycle.restore(
                session, target, snapshot_id, tailscale, hostname, idle_stop
            )
        )

    def purge(self, target: str, terminate: bool) -> dict[str, Any]:
        return _run(lambda session: lifecycle.purge(session, target, terminate))

    def prune(
        self,
        older_than: dt.timedelta,
        keep_latest: bool,
        pattern: str,
        confirm: Callable[[dict[str, Any]], bool],
    ) -> dict[str, Any]:
        return _run(
            lambda session: lifecycle.prune(
                session, older_than, keep_latest, pattern, confirm
            )
        )

    def sync(
        self, target: str, local_dir: str, path: str, pull: bool
    ) -> dict[str, Any]:
        """Copy uncommitted changes between `local_dir` and the checkout at
        `path` in a running sandbox; see `sync.sync`."""

        async def run(session: Session):
            sandbox_id, name = await lifecycle.running(session, target)

            async def remote(args: Sequence[str], stdin: bytes):
                return await sandbox.execute_bytes(session, sandbox_id, args, stdin)

            here = sync_module.Side(
                "this machine",
                sync_module.run_here,
                local_dir,
                ["bash", str(sync_module.SCRIPT)],
            )
            there = sync_module.Side(f"sandbox {name or sandbox_id}", remote, path)
            return await sync_module.sync(here, there, pull)

        return _run(run)
