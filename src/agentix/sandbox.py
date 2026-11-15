"""Sandbox operations. Each one takes the `Session` that holds the shared state."""

import asyncio
import contextlib
from collections.abc import Sequence
from typing import Literal, NotRequired, TypedDict

import modal
import modal.experimental

from .defaults import IMAGE_NAME, SANDBOX_MEMORY_MIB, SANDBOX_TIMEOUT, SECRET_NAME
from .errors import InvalidRequest, OperationFailed
from .layout import OP_TOKEN_FILE, READY_FILE, WORKDIR_FILE
from .session import Session

# The start command writes the token file, the --workdir override, and, last,
# the ready file, which `create` waits for. The paths are in layout.py.

# A sandbox is normally ready in about a second.
READY_TIMEOUT = 60

# With this option, Modal saves the filesystem of a sandbox when it ends,
# however it ends, as an Image that a new sandbox can start from, and keeps it
# for 30 days. The option is experimental; see "Sandbox Exit Snapshots" in
# Modal's guide and Sandbox._experimental_get_exit_snapshot.
# /run is a tmpfs, so the files above, the 1Password token among them, are not
# in the snapshot. A restored sandbox writes them again with its start command.
_EXIT_SNAPSHOT = {"enable_exit_snapshot": True}
# Tags a named sandbox, so a command given only its ID can find its record.
NAME_TAG = "agentix-name"
# How long to wait for a snapshot. Small ones are ready in well under a second.
SNAPSHOT_TIMEOUT = 120

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


class ExitSnapshot(TypedDict):
    """What became of the exit snapshot of a sandbox.

    `status` is "running" while the sandbox runs, "pending" while Modal is
    still saving, "saved" with `image_id`, or "none" if Modal saved nothing.
    """

    status: Literal["running", "pending", "saved", "none"]
    exit_code: NotRequired[int]
    image_id: NotRequired[str]
    error: NotRequired[str]


async def create(
    session: Session,
    image_name: str = IMAGE_NAME,
    secret_name: str = SECRET_NAME,
    timeout: int = SANDBOX_TIMEOUT,
    workdir: str = "",
    memory: int = SANDBOX_MEMORY_MIB,
    cpu: float = 0,
    name: str = "",
    snapshot_id: str = "",
) -> str:
    """Start a sandbox in the image's working directory, or in `workdir` if given.

    `memory` is the reservation in MiB. `cpu` reserves that many cores; zero
    keeps Modal's default. `snapshot_id` starts the sandbox from that snapshot
    instead of `image_name`. Modal refuses a `name` that a running sandbox has.

    Returns the sandbox ID when the sandbox runs and its files are in place,
    so a command sent immediately afterwards finds them.
    """
    image = (
        modal.Image.from_id(snapshot_id)
        if snapshot_id
        else modal.Image.from_name(image_name)
    )
    handle = await modal.Sandbox.create.aio(
        "sh",
        "-c",
        _INIT,
        "agentix-init",
        workdir,
        app=await session.app(),
        name=name or None,
        tags={NAME_TAG: name} if name else None,
        image=image,
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
        experimental_options=_EXIT_SNAPSHOT,
    )
    try:
        await handle.wait_until_ready.aio(timeout=READY_TIMEOUT)
    except Exception as error:
        await _discard(handle)
        raise OperationFailed(
            f"The sandbox did not become ready, so it was removed: {error}"
        ) from error
    session.adopt(handle)
    return handle.object_id


async def discard(session: Session, sandbox_id: str) -> None:
    """End a sandbox that no record holds, and delete its exit snapshot.

    Nothing else would find that snapshot. Failures are ignored, so that the
    caller can report what made it discard the sandbox.
    """
    with contextlib.suppress(Exception):
        async with session.sandbox(sandbox_id) as handle:
            await _discard(handle)
    session.forget(sandbox_id)


async def _discard(handle: modal.Sandbox) -> None:
    with contextlib.suppress(Exception):
        await handle.terminate.aio()
        image = await handle._experimental_get_exit_snapshot.aio(
            timeout=SNAPSHOT_TIMEOUT
        )
        await delete_snapshot(image.object_id)


async def list_ids(session: Session) -> list[str]:
    """IDs of the running sandboxes of the app."""
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


async def stop(session: Session, sandbox_id: str) -> bool:
    """End a sandbox and wait until it has ended. False if it had already ended.

    The handle stays shared, so its exit snapshot can be read next.
    """
    async with session.sandbox(sandbox_id) as handle:
        if await handle.poll.aio() is not None:
            return False
        await handle.terminate.aio(wait=True)
    return True


async def terminate(session: Session, sandbox_id: str) -> Terminated:
    await stop(session, sandbox_id)
    session.forget(sandbox_id)
    return {"id": sandbox_id, "status": "terminated"}


async def name_of(session: Session, sandbox_id: str) -> str:
    """The name of a sandbox, or "" for one started without a name."""
    async with session.sandbox(sandbox_id) as handle:
        tags = await handle.get_tags.aio()
    return tags.get(NAME_TAG, "")


async def exit_snapshot(session: Session, sandbox_id: str, wait: float) -> ExitSnapshot:
    """Check a sandbox for its exit snapshot, waiting up to `wait` seconds."""
    try:
        async with session.sandbox(sandbox_id) as handle:
            # For a running sandbox, the wait would last until the sandbox ends.
            exit_code = await handle.poll.aio()
            if exit_code is None:
                return {"status": "running"}
            try:
                image = await handle._experimental_get_exit_snapshot.aio(timeout=wait)
            except modal.exception.TimeoutError:
                return {"status": "pending", "exit_code": exit_code}
            except modal.exception.SnapshotCreationError as error:
                return {"status": "none", "exit_code": exit_code, "error": str(error)}
    except (modal.exception.InvalidError, modal.exception.NotFoundError) as error:
        # Modal no longer knows the sandbox, or it was started without snapshots.
        return {"status": "none", "error": str(error)}
    return {"status": "saved", "image_id": image.object_id, "exit_code": exit_code}


async def snapshot(session: Session, sandbox_id: str) -> str:
    """Save the filesystem of a running sandbox and return the snapshot ID."""
    async with session.sandbox(sandbox_id) as handle:
        image = await handle.snapshot_filesystem.aio(timeout=SNAPSHOT_TIMEOUT)
    return image.object_id


async def delete_snapshot(snapshot_id: str) -> None:
    """Delete a snapshot. Deleting one that is already gone succeeds.

    A snapshot of a restored sandbox does not depend on the snapshot it was
    restored from, so snapshots can be deleted in any order.
    """
    await modal.experimental.image_delete.aio(snapshot_id)
