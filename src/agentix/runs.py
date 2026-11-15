"""Detached runs: commands that go on after the command that started them exits.

A run is one call of the deployed function in `runner.py`, started with
`.spawn()`. Its FunctionCall ID is the run ID: Modal keeps the call's log,
which `logs` follows, and its result, which `wait` returns. agentix adds one
small file per run on the record Volume, runs/SANDBOX_ID/RUN_ID.json, written
once when the run starts, so that the runs of a sandbox can be listed. See
docs/detached-runs.md.
"""

import asyncio
import contextlib
import io
import json
import re
import shlex
from collections.abc import Callable, Sequence
from typing import Any

import modal
from modal.volume import FileEntryType

from . import lifecycle, names, runner, sandbox, store
from .errors import InvalidRequest, NotFound, OperationFailed
from .session import Session

ROOT = "runs"
# IDs go into paths on the Volume and in the sandbox, `rm -rf` among them, so
# they are checked whole, not by their prefix.
_RUN_ID = re.compile(r"fc-[A-Za-z0-9]+")
_SANDBOX_ID = re.compile(r"sb-[A-Za-z0-9]+")
# States of a run that has surely ended, which --clean may forget. "unknown"
# (Modal could not say) and "invalid" (a run file with bad IDs) are kept.
ENDED = ("exited", "sandbox ended", "timed out", "expired")


def check_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise InvalidRequest(
            f"Invalid run ID: {run_id!r}. Run IDs are fc- and letters and "
            "digits; see agentix sandbox runs."
        )
    return run_id


def _valid(run: dict[str, Any]) -> bool:
    return bool(
        isinstance(run.get("run_id"), str)
        and _RUN_ID.fullmatch(run["run_id"])
        and isinstance(run.get("sandbox_id"), str)
        and _SANDBOX_ID.fullmatch(run["sandbox_id"])
    )


async def deploy() -> dict[str, str]:
    """Deploy the runner app. Deploying the same code again changes nothing."""
    await runner.app.deploy.aio()
    return {"app": runner.APP_NAME, "function": runner.FUNCTION_NAME}


async def _function() -> modal.Function:
    function = modal.Function.from_name(runner.APP_NAME, runner.FUNCTION_NAME)
    try:
        await function.hydrate.aio()
    except modal.exception.NotFoundError:
        raise NotFound(
            f"The runner app {runner.APP_NAME} is not deployed. Deploy it once "
            "with: agentix runner deploy"
        ) from None
    return function


async def start(session: Session, target: str, args: Sequence[str]) -> dict[str, Any]:
    """Start `args` in the running sandbox `target` and return the run at once."""
    if not args:
        raise InvalidRequest("Provide a command to run.")
    sandbox_id = await lifecycle.resolve(session, target)
    if not _SANDBOX_ID.fullmatch(sandbox_id):
        raise InvalidRequest(f"Invalid sandbox ID: {sandbox_id!r}.")
    function, running = await asyncio.gather(
        _function(), sandbox.is_running(session, sandbox_id)
    )
    if not running:
        raise InvalidRequest(f"{target} is not running. Start or restore it first.")
    call = await function.spawn.aio(sandbox_id, list(args))
    run = {
        "run_id": call.object_id,
        "sandbox_id": sandbox_id,
        "name": "" if names.is_sandbox_id(target) else target,
        "command": list(args),
        "started_at": store.now(),
    }
    volume = await session.record_volume()
    try:
        async with volume.batch_upload.aio(force=True) as batch:
            batch.put_file(io.BytesIO(json.dumps(run).encode()), f"/{_path(run)}")
    except Exception as error:
        raise OperationFailed(
            f"Run {call.object_id} started, but it was not recorded, so "
            f"sandbox runs will not list it ({error}). Follow it with: "
            f"agentix sandbox logs {call.object_id}"
        ) from error
    return run


async def follow(
    run_id: str, write: Callable[[str, str], None], timeout: float | None = None
) -> None:
    """Pass the output of `run_id`, from its start, to `write(source, text)`.

    `source` is "stdout" or "stderr". Returns when the run has ended, or when
    nothing new came for `timeout` seconds.

    The stream stops soon after the call ends, and lines that Modal stored
    later would be missed. So once it stops, the whole log is read once more
    and what comes after the entries already passed on is passed on too.
    """
    call = modal.FunctionCall.from_id(check_run_id(run_id))
    shown = 0
    async for entry in call.logs.stream.aio(timeout=timeout):
        if entry.source in ("stdout", "stderr"):
            shown += 1
            write(entry.source, entry.message)
    if timeout is not None:
        return
    stored = [
        entry
        async for entry in call.logs.fetch.aio()
        if entry.source in ("stdout", "stderr")
    ]
    for entry in stored[shown:]:
        write(entry.source, entry.message)


# A poll of a call that has not ended raises the built-in TimeoutError. A call
# that reached its own timeout raises modal.exception.FunctionTimeoutError,
# which is not a built-in TimeoutError: that run has ended.


async def wait(run_id: str, timeout: float | None = None) -> dict[str, Any]:
    """The result of `run_id` once it has ended. See `runner.run`."""
    call = modal.FunctionCall.from_id(check_run_id(run_id))
    try:
        return await call.get.aio(timeout=timeout)
    except modal.exception.OutputExpiredError:
        raise NotFound(f"Modal no longer keeps the result of {run_id}.") from None
    except modal.exception.FunctionTimeoutError:
        raise OperationFailed(
            f"{run_id} reached the runner's limit of {runner.CALL_TIMEOUT // 3600} "
            "hours, so its exit code is not known. The command may still run "
            "in the sandbox."
        ) from None
    except TimeoutError:
        raise OperationFailed(f"{run_id} is still running.") from None


async def status(run_id: str) -> dict[str, Any]:
    """Whether `run_id` runs, without waiting: `state` and, once ended, `exit_code`.

    `state` is "running", "exited", "sandbox ended" (the sandbox ended first),
    "timed out" (the call reached its 24-hour limit), "expired" (Modal no
    longer keeps the result), or "unknown" with `error`, when asking failed:
    the run may still be going.
    """
    call = modal.FunctionCall.from_id(check_run_id(run_id))
    try:
        result = await call.get.aio(timeout=0)
    except modal.exception.OutputExpiredError:
        return {"state": "expired"}
    except modal.exception.FunctionTimeoutError:
        return {"state": "timed out"}
    except TimeoutError:
        return {"state": "running"}
    except Exception as error:
        return {"state": "unknown", "error": f"{type(error).__name__}: {error}"}
    ended = result.get("sandbox_ended") and result.get("exit_code") in (None, 128)
    return {
        "state": "sandbox ended" if ended else "exited",
        "exit_code": result.get("exit_code"),
    }


async def ls(session: Session, target: str = "") -> list[dict[str, Any]]:
    """The runs of `target` (a sandbox ID, or every sandbox of a name), or all."""
    volume = await session.record_volume()
    if not target:
        folders = [ROOT]
    elif names.is_sandbox_id(target):
        if not _SANDBOX_ID.fullmatch(target):
            raise InvalidRequest(f"Invalid sandbox ID: {target!r}.")
        folders = [f"{ROOT}/{target}"]
    else:
        record = await store.load(session, names.check_name(target))
        folders = [
            f"{ROOT}/{item['id']}"
            for item in record["sandboxes"]
            if _SANDBOX_ID.fullmatch(item["id"])
        ]
    listed = await asyncio.gather(*(_list(volume, folder) for folder in folders))
    paths = [path for found in listed for path in found]
    runs = await asyncio.gather(*(_read(volume, path) for path in paths))
    states = await asyncio.gather(*(_status(run) for run in runs))
    found = [
        {**run, "command": shlex.join(run["command"]), **state}
        for run, state in zip(runs, states)
    ]
    return sorted(found, key=lambda run: (run["started_at"], run["run_id"]))


async def _status(run: dict[str, Any]) -> dict[str, Any]:
    if not _valid(run):
        return {"state": "invalid", "error": "the run file holds invalid IDs"}
    return await status(run["run_id"])


async def clean(session: Session, target: str = "") -> list[dict[str, Any]]:
    """Forget the runs of `target` (or all) that have ended; return them.

    Their files on the Volume go, and so do their log files in sandboxes that
    still run. A run that still runs, or whose state is not known, is kept.
    Modal keeps the call's log and
    result for as long as it keeps them, whatever this does.
    """
    ended = [
        run
        for run in await ls(session, target)
        if run["state"] in ENDED and _valid(run)
    ]
    volume = await session.record_volume()
    by_sandbox: dict[str, list[str]] = {}
    for run in ended:
        by_sandbox.setdefault(run["sandbox_id"], []).append(run["run_id"])

    async def clear_sandbox(sandbox_id: str, run_ids: list[str]) -> None:
        with contextlib.suppress(Exception):
            if await sandbox.is_running(session, sandbox_id):
                dirs = [f"{runner.RUNS_DIR}/{run_id}" for run_id in run_ids]
                await sandbox.execute(session, sandbox_id, ["rm", "-rf", "--", *dirs])

    await asyncio.gather(
        *(volume.remove_file.aio(f"/{_path(run)}") for run in ended),
        *(clear_sandbox(sid, ids) for sid, ids in by_sandbox.items()),
    )
    return ended


def _path(run: dict[str, Any]) -> str:
    return f"{ROOT}/{run['sandbox_id']}/{run['run_id']}.json"


async def _list(volume: modal.Volume, folder: str) -> list[str]:
    try:
        entries = await volume.listdir.aio(f"/{folder}", recursive=True)
    except modal.exception.NotFoundError:
        return []
    return [
        entry.path
        for entry in entries
        if entry.type == FileEntryType.FILE and entry.path.endswith(".json")
    ]


async def _read(volume: modal.Volume, path: str) -> dict[str, Any]:
    data = b"".join([chunk async for chunk in volume.read_file.aio(f"/{path}")])
    return json.loads(data)
