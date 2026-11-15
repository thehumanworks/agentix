"""Named sandboxes: start, stop, snapshot, restore, and purge them.

Each operation keeps the record of a name (see `store.py`) in step with Modal.
Modal saves a snapshot whenever a sandbox ends, also at its timeout, but tells
no one. Commands that read a record therefore first ask Modal about every
sandbox of that record whose snapshot is not recorded yet.
"""

import asyncio
import contextlib
from collections.abc import Collection
from typing import Any

import modal

from . import names, sandbox, ssh, store
from .errors import InvalidRequest, NotFound, OperationFailed
from .session import Session
from .ssh_input import check_tailscale_start

# How long a command that needs an exit snapshot waits for Modal to save it.
EXIT_WAIT = 60
_NAME_ATTEMPTS = 20


async def resolve(session: Session, target: str) -> str:
    """The sandbox ID for `target`, which is a sandbox ID or a name."""
    if names.is_sandbox_id(target):
        return target
    record = await store.load(session, names.check_name(target))
    if not record["sandbox_id"]:
        raise NotFound(f"{target} has no sandbox yet.")
    return record["sandbox_id"]


async def _record_for(session: Session, target: str) -> store.Record:
    if not names.is_sandbox_id(target):
        return await store.load(session, names.check_name(target))
    name = await sandbox.name_of(session, target)
    if not name:
        raise InvalidRequest(
            f"{target} has no name, so it has no snapshots. "
            "Stop it with agentix sandbox down."
        )
    return await store.load(session, name)


async def _reconcile(
    session: Session,
    record: store.Record,
    wait: float,
    stopped_now: Collection[str] = (),
) -> dict[str, sandbox.ExitSnapshot]:
    """Record the exit snapshots of sandboxes of `record` that have ended.

    Saves the record if it changed. Returns what Modal reported for each
    sandbox that was checked, by sandbox ID.
    """
    waiting = store.pending(record)
    outcomes = await asyncio.gather(
        *(sandbox.exit_snapshot(session, item["id"], wait) for item in waiting)
    )
    changed = [
        store.record_exit(
            record,
            item,
            outcome["status"],
            outcome.get("image_id", ""),
            outcome.get("exit_code"),
            stopped_now=item["id"] in stopped_now,
        )
        for item, outcome in zip(waiting, outcomes)
    ]
    if any(changed):
        await store.save(session, record)
    return {item["id"]: outcome for item, outcome in zip(waiting, outcomes)}


def _is_running(record: store.Record, outcomes: dict[str, sandbox.ExitSnapshot]):
    current = outcomes.get(record["sandbox_id"])
    return current is not None and current["status"] == "running"


async def _started(
    session: Session,
    record: store.Record,
    tailscale: bool,
    hostname: str,
    **extra: Any,
) -> dict[str, Any]:
    """Report a started sandbox, after starting Tailscale SSH if asked to.

    The sandbox is recorded already. If Tailscale fails, the error names the
    sandbox, so it is not lost.
    """
    sandbox_id, name = record["sandbox_id"], record["name"]
    result = {"sandbox_id": sandbox_id, "name": name, **extra}
    if not tailscale:
        return result
    try:
        details = await ssh.manage(session, "start", sandbox_id, "tailscale", hostname)
    except Exception as error:
        raise OperationFailed(
            f"Sandbox {sandbox_id} ({name}) is running, but Tailscale SSH did not "
            f"start ({error}). Retry with: agentix sandbox ssh start {name}"
        ) from error
    details.pop("sandbox_id", None)
    return result | details


async def up(
    session: Session,
    options: store.Options,
    name: str = "",
    tailscale: bool = False,
    hostname: str = "",
) -> dict[str, Any]:
    """Start a sandbox under `name`, or under a new name if none is given.

    A name that has a record already is refused, whether its sandbox runs or
    not: restore it, or purge it first.
    """
    check_tailscale_start(tailscale, hostname, options["secret_name"])
    if name:
        record = store.new_record(names.check_name(name), options)
        if not await store.claim(session, record):
            raise InvalidRequest(
                f"The name {name} is taken. Continue it with agentix sandbox "
                f"restore {name}, remove it with agentix sandbox purge {name}, "
                "or choose another name."
            )
    else:
        for _ in range(_NAME_ATTEMPTS):
            record = store.new_record(names.generate(), options)
            if await store.claim(session, record):
                break
        else:
            raise OperationFailed("No free name was found. Pass --name=NAME.")
    try:
        sandbox_id = await sandbox.create(session, **options, name=record["name"])
    except Exception:
        # Free the name. If that fails too, report the first failure.
        with contextlib.suppress(Exception):
            await store.remove(session, record["name"])
        raise
    store.add_sandbox(record, sandbox_id)
    await store.save(session, record)
    return await _started(session, record, tailscale, hostname)


async def down(session: Session, target: str) -> dict[str, Any]:
    """End a sandbox and record its exit snapshot."""
    if names.is_sandbox_id(target):
        sandbox_id, name = target, await sandbox.name_of(session, target)
    else:
        record = await store.load(session, names.check_name(target))
        sandbox_id, name = record["sandbox_id"], record["name"]
        if not sandbox_id:
            raise NotFound(f"{name} has no sandbox yet.")
    stopped_now = await sandbox.stop(session, sandbox_id)
    result: dict[str, Any] = {"id": sandbox_id, "status": "terminated"}
    if name:
        try:
            record = await store.load(session, name)
        except NotFound:
            # The record was purged; there is nothing to keep in step.
            record = None
        if record is not None:
            await _reconcile(
                session, record, EXIT_WAIT, [sandbox_id] if stopped_now else []
            )
            saved = [
                snapshot["id"]
                for snapshot in record["snapshots"]
                if snapshot["sandbox_id"] == sandbox_id and snapshot["kind"] != "manual"
            ]
            result |= {"name": name, "snapshot": saved[-1] if saved else ""}
    session.forget(sandbox_id)
    return result


async def ls(session: Session) -> list[dict[str, Any]]:
    """Every named sandbox, and running sandboxes that have no name."""
    running_ids, records = await asyncio.gather(
        sandbox.list_ids(session), store.records(session)
    )
    running = set(running_ids)
    # Recording a snapshot is not needed to list; one that fails can wait.
    await asyncio.gather(
        *(
            _reconcile(session, record, 0)
            for record in records
            if record["sandbox_id"] not in running and store.pending(record)
        ),
        return_exceptions=True,
    )
    rows = [
        {
            "name": record["name"],
            "sandbox_id": record["sandbox_id"],
            "status": "running"
            if record["sandbox_id"] in running
            else "stopped"
            if record["sandbox_id"]
            else "none",
            "snapshots": len(record["snapshots"]),
        }
        for record in sorted(records, key=lambda record: record["name"])
    ]
    named = {record["sandbox_id"] for record in records}
    rows += [
        {"name": "", "sandbox_id": sandbox_id, "status": "running", "snapshots": 0}
        for sandbox_id in running_ids
        if sandbox_id not in named
    ]
    return rows


async def snapshots(session: Session, target: str) -> list[store.SnapshotRecord]:
    """The snapshots of a name, oldest first."""
    record = await _record_for(session, target)
    await _reconcile(session, record, EXIT_WAIT)
    return store.ordered(record)


async def take_snapshot(session: Session, target: str) -> dict[str, Any]:
    """Save the filesystem of the running sandbox of a name now."""
    record = await _record_for(session, target)
    if not record["sandbox_id"]:
        raise NotFound(f"{record['name']} has no sandbox yet.")
    snapshot_id = await sandbox.snapshot(session, record["sandbox_id"])
    # Read the record again, so changes made while the snapshot ran are kept.
    record = await store.load(session, record["name"])
    entry = store.add_snapshot(record, snapshot_id, record["sandbox_id"], "manual")
    await store.save(session, record)
    return {"name": record["name"], **entry}


async def restore(
    session: Session,
    target: str,
    snapshot_id: str = "",
    tailscale: bool = False,
    hostname: str = "",
) -> dict[str, Any]:
    """Start a new sandbox for a name from a snapshot, the latest by default.

    The sandbox the name has now must have ended. The new one gets the options
    the name was created with.
    """
    if hostname and not tailscale:
        raise InvalidRequest("--hostname is only used with --tailscale.")
    record = await _record_for(session, target)
    name = record["name"]
    if _is_running(record, await _reconcile(session, record, EXIT_WAIT)):
        raise InvalidRequest(
            f"{name} is running as {record['sandbox_id']}. "
            f"Stop it first with agentix sandbox down {name}."
        )
    if snapshot_id:
        if all(snapshot["id"] != snapshot_id for snapshot in record["snapshots"]):
            raise NotFound(
                f"{snapshot_id} is not a snapshot of {name}. "
                f"See agentix sandbox snapshots {name}."
            )
    else:
        if waiting := store.pending(record):
            raise OperationFailed(
                f"Modal is still saving the snapshot of {waiting[-1]['id']}. "
                "Retry shortly, or choose a snapshot with --snapshot=ID."
            )
        if not record["snapshots"]:
            raise NotFound(f"{name} has no snapshots.")
        snapshot_id = store.ordered(record)[-1]["id"]
    try:
        sandbox_id = await sandbox.create(
            session, **record["options"], name=name, snapshot_id=snapshot_id
        )
    except modal.exception.NotFoundError as error:
        if snapshot_id not in str(error):
            raise
        raise NotFound(
            f"Snapshot {snapshot_id} no longer exists: it expired or was deleted."
        ) from error
    except modal.exception.AlreadyExistsError as error:
        raise InvalidRequest(
            f"{name} is running already; another restore may have started it."
        ) from error
    # Read the record again, so changes made while the sandbox started are kept.
    record = await store.load(session, name)
    store.add_sandbox(record, sandbox_id, restored_from=snapshot_id)
    await store.save(session, record)
    return await _started(session, record, tailscale, hostname, snapshot=snapshot_id)


async def purge(
    session: Session, target: str, terminate: bool = False
) -> dict[str, Any]:
    """Delete every snapshot of a name, then its record.

    A running sandbox is refused, unless `terminate` ends it first. Its exit
    snapshot is then deleted too.
    """
    record = await _record_for(session, target)
    name, current = record["name"], record["sandbox_id"]
    if _is_running(record, await _reconcile(session, record, EXIT_WAIT)):
        if not terminate:
            raise InvalidRequest(
                f"{name} is running as {current}. Stop it with agentix sandbox "
                f"down {name}, or pass --terminate."
            )
        await sandbox.stop(session, current)
        await _reconcile(session, record, EXIT_WAIT, [current])
        session.forget(current)
    if waiting := store.pending(record):
        # Its snapshot would be left behind, with no record to find it by.
        raise OperationFailed(
            f"Modal is still saving the snapshot of {waiting[-1]['id']}. "
            "Retry the purge shortly."
        )
    snapshot_ids = [snapshot["id"] for snapshot in record["snapshots"]]
    results = await asyncio.gather(
        *(sandbox.delete_snapshot(snapshot_id) for snapshot_id in snapshot_ids),
        return_exceptions=True,
    )
    failures = {
        snapshot_id: result
        for snapshot_id, result in zip(snapshot_ids, results)
        if isinstance(result, BaseException)
    }
    if failures:
        record["snapshots"] = [
            snapshot for snapshot in record["snapshots"] if snapshot["id"] in failures
        ]
        await store.save(session, record)
        first = next(iter(failures.values()))
        raise OperationFailed(
            f"Could not delete {', '.join(failures)} ({first}). The record keeps "
            "them; retry the purge."
        )
    await store.remove(session, name)
    return {
        "name": name,
        "sandboxes": [item["id"] for item in record["sandboxes"]],
        "deleted_snapshots": snapshot_ids,
    }
