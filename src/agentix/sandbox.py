"""Sandbox operations. Each one takes the `Session` that holds the shared state."""

import asyncio
import contextlib
from collections.abc import Sequence
from typing import TypedDict

import modal

from .defaults import IMAGE_NAME, SANDBOX_MEMORY_MIB, SANDBOX_TIMEOUT, SECRET_NAME
from .errors import InvalidRequest, OperationFailed
from .session import Session

# Modal gives the token only to the processes it starts. SSH sessions do not
# have it, so the image's `op` wrapper reads it from this root-only file.
# Keep the path in step with nix/home-manager/modules/sandbox/op-token.nix.
OP_TOKEN_FILE = "/run/agentix/op-token"
# SSH logins read this to start in the override instead of /workdir.
# Keep the path in step with scripts/workdir-profile.sh.
WORKDIR_FILE = "/run/agentix/workdir"

# The last thing the start command writes. `create` waits for it.
READY_FILE = "/run/agentix/ready"
# A sandbox is normally ready in about a second.
READY_TIMEOUT = 60

# The sandbox runs this as its main command. Writing the files here, and not
# with commands sent after creation, saves a round trip for each file.
# A failed write ends the sandbox, so no sandbox runs without its files.
# Only the token is private. The other files keep the default mode.
_INIT = f"""set -eu
if [ -n "${{OP_SERVICE_ACCOUNT_TOKEN:-}}" ]; then
  (
    umask 077
    mkdir -p "$(dirname {OP_TOKEN_FILE})"
    printf %s "$OP_SERVICE_ACCOUNT_TOKEN" > {OP_TOKEN_FILE}
  )
fi
mkdir -p "$(dirname {READY_FILE})"
if [ -n "$1" ]; then
  printf '%s\\n' "$1" > {WORKDIR_FILE}
fi
: > {READY_FILE}
exec sleep infinity
"""


class ExecResult(TypedDict):
    stdout: str
    stderr: str
    exit_code: int


class Terminated(TypedDict):
    id: str
    status: str


async def create(
    session: Session,
    image_name: str = IMAGE_NAME,
    secret_name: str = SECRET_NAME,
    timeout: int = SANDBOX_TIMEOUT,
    workdir: str = "",
    memory: int = SANDBOX_MEMORY_MIB,
    cpu: float = 0,
) -> str:
    """Start a sandbox in the image's working directory, or in `workdir` if given.

    `memory` is the reservation in MiB. `cpu` reserves that many cores; zero
    keeps Modal's default.

    Returns the sandbox ID when the sandbox runs and its files are in place,
    so a command sent immediately afterwards finds them.
    """
    handle = await modal.Sandbox.create.aio(
        "sh",
        "-c",
        _INIT,
        "agentix-init",
        workdir,
        app=await session.app(),
        image=modal.Image.from_name(image_name),
        env={"IS_SANDBOX": "1"},
        secrets=[
            modal.Secret.from_name(
                secret_name, required_keys=["OP_SERVICE_ACCOUNT_TOKEN"]
            )
        ]
        if secret_name
        else [],
        timeout=timeout,
        workdir=workdir or None,
        memory=memory,
        cpu=cpu or None,
        runtime="vm",
        unencrypted_ports=[22],
        readiness_probe=modal.Probe.with_exec("test", "-e", READY_FILE),
    )
    try:
        await handle.wait_until_ready.aio(timeout=READY_TIMEOUT)
    except Exception as error:
        with contextlib.suppress(Exception):
            await handle.terminate.aio()
        raise OperationFailed(
            f"The sandbox did not become ready, so it was removed: {error}"
        ) from error
    session.adopt(handle)
    return handle.object_id


async def list_ids(session: Session) -> list[str]:
    app = await session.app()
    return [
        handle.object_id async for handle in modal.Sandbox.list.aio(app_id=app.app_id)
    ]


async def execute(session: Session, sandbox_id: str, args: Sequence[str]) -> ExecResult:
    """Run a command and return its output. A non-zero exit code is a result."""
    if not args:
        raise InvalidRequest("Provide a command to run.")
    async with session.sandbox(sandbox_id) as handle:
        process = await handle.exec.aio(*args)
        stdout, stderr, exit_code = await asyncio.gather(
            process.stdout.read.aio(), process.stderr.read.aio(), process.wait.aio()
        )
    return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code}


async def terminate(session: Session, sandbox_id: str) -> Terminated:
    async with session.sandbox(sandbox_id) as handle:
        await handle.terminate.aio(wait=True)
    session.forget(sandbox_id)
    return {"id": sandbox_id, "status": "terminated"}
