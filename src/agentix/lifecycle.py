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
) -> tuple[store.Record, dict[str, sandbox.ExitSnapshot]]:
    """Record the exit snapshots of sandboxes of `record` that have ended.

    Returns the record as it is stored afterwards, and what Modal reported for
    each sandbox that was checked, by sandbox ID.
    """
    waiting = store.pending(record)
    outcomes = await asyncio.gather(
        *(sandbox.exit_snapshot(session, item["id"], wait) for item in waiting)
    )
    found = {item["id"]: outcome for item, outcome in zip(waiting, outcomes)}

    def change(latest: store.Record) -> bool:
        # Skips what another command recorded meanwhile.
        changed = [
            store.record_exit(
                latest,
                item,
                found[item["id"]]["status"],
                found[item["id"]].get("image_id", ""),
                found[item["id"]].get("exit_code"),
                stopped_now=item["id"] in stopped_now,
            )
            for item in store.pending(latest)
            if item["id"] in found
        ]
        return any(changed)

    if any(outcome["status"] in ("saved", "none") for outcome in outcomes):
        record = await store.update(session, record["name"], change)
    return record, found


def _is_running(record: store.Record, outcomes: dict[str, sandbox.ExitSnapshot]):
    current = outcomes.get(record["sandbox_id"])
    return current is not None and current["status"] == "running"


def _taken(name: str) -> str:
    return (
        f"The name {name} is taken. Continue it with agentix sandbox restore "
        f"{name}, remove it with agentix sandbox purge {name}, or choose "
        "another name."
    )


async def _started(
    session: Session,
    sandbox_id: str,
    name: str,
    tailscale: bool,
    hostname: str,
    **extra: Any,
) -> dict[str, Any]:
    """Report a started sandbox, after starting Tailscale SSH if asked to.

    The sandbox is recorded already, if it has a name. If Tailscale fails,
    the error names the sandbox, so it is not lost.
    """
    result = {"sandbox_id": sandbox_id, **({"name": name} if name else {}), **extra}
    if not tailscale:
        return result
    try:
        details = await ssh.manage(session, "start", sandbox_id, "tailscale", hostname)
    except Exception as error:
        label = f"{sandbox_id} ({name})" if name else sandbox_id
        raise OperationFailed(
            f"Sandbox {label} is running, but Tailscale SSH did not start "
            f"({error}). Retry with: agentix sandbox ssh start {name or sandbox_id}"
        ) from error
    details.pop("sandbox_id", None)
    return result | details


async def up(
    session: Session,
    options: store.Options,
    name: str = "",
    tailscale: bool = False,
    hostname: str = "",
    ephemeral: bool = False,
) -> dict[str, Any]:
    """Start a sandbox under `name`, or under a new name if none is given.

    A name that has a record already is refused, whether its sandbox runs or
    not: restore it, or purge it first. An `ephemeral` sandbox has no name, no
    record, and no exit snapshot, so stopping it leaves nothing behind.
    """
    names.check_ephemeral(name, ephemeral)
    check_tailscale_start(tailscale, hostname, options["secret_name"])
    if ephemeral:
        options = {**options, "exit_snapshot": False}
        sandbox_id = await sandbox.create(session, **options)
        return await _started(session, sandbox_id, "", tailscale, hostname)
    if name:
        if await store.find(session, names.check_name(name)) is not None:
            raise InvalidRequest(_taken(name))
    else:
        for _ in range(_NAME_ATTEMPTS):
            name = names.generate()
            if await store.find(session, name) is None:
                break
        else:
            raise OperationFailed("No free name was found. Pass --name=NAME.")
    # Modal refuses a name that a running sandbox has, so of two commands that
    # start one name at once, only one gets this far. The record is written
    # once, with the sandbox in it, because each write takes over a second.
    sandbox_id = await sandbox.create(session, **options, name=name)
    record = store.new_record(name, options)
    store.add_sandbox(record, sandbox_id)
    try:
        claimed = await store.claim(session, record)
    except Exception:
        # No record holds the sandbox, so nothing would find it again.
        await sandbox.discard(session, sandbox_id)
        raise
    if not claimed:
        await sandbox.discard(session, sandbox_id)
        raise InvalidRequest(_taken(name))
    return await _started(session, sandbox_id, name, tailscale, hostname)


async def down(session: Session, target: str) -> dict[str, Any]:
    """End a sandbox and record its exit snapshot."""
    record = None
    if names.is_sandbox_id(target):
        sandbox_id, name = target, await sandbox.name_of(session, target)
    else:
        record = await store.load(session, names.check_name(target))
        sandbox_id, name = record["sandbox_id"], record["name"]
        if not sandbox_id:
            raise NotFound(f"{name} has no sandbox yet.")
    stopped_now = await sandbox.stop(session, sandbox_id)
    result: dict[str, Any] = {"id": sandbox_id, "status": "terminated"}
    # The record may have been purged; there is nothing to keep in step then.
    with contextlib.suppress(NotFound):
        if name and record is None:
            record = await store.load(session, name)
        if record is not None:
            record, _ = await _reconcile(
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

    async def settle(record: store.Record) -> store.Record:
        if record["sandbox_id"] in running or not store.pending(record):
            return record
        # Recording a snapshot is not needed to list; one that fails can wait.
        try:
            record, _ = await _reconcile(session, record, 0)
        except Exception:
            pass
        return record

    records = await asyncio.gather(*(settle(record) for record in records))
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


async def snapshots(session: Session, target: str) -> list[dict[str, Any]]:
    """The snapshots of a name, oldest first.

    Each has `expires_at`, when Modal is expected to delete it, and `expired`
    once that has passed. Modal cannot say whether one still exists.
    """
    record = await _record_for(session, target)
    record, _ = await _reconcile(session, record, EXIT_WAIT)
    rows, now = [], store.now()
    for snapshot in store.ordered(record):
        expires = store.expires_at(snapshot)
        rows.append({**snapshot, "expires_at": expires, "expired": expires <= now})
    return rows


async def take_snapshot(session: Session, target: str) -> dict[str, Any]:
    """Save the filesystem of the running sandbox of a name now."""
    record = await _record_for(session, target)
    name, sandbox_id = record["name"], record["sandbox_id"]
    if not sandbox_id:
        raise NotFound(f"{name} has no sandbox yet.")
    snapshot_id = await sandbox.snapshot(session, sandbox_id)
    created_at = store.now()

    def change(latest: store.Record) -> None:
        store.add_snapshot(latest, snapshot_id, sandbox_id, "manual", created_at)

    try:
        await store.update(session, name, change)
    except NotFound:
        # Purged meanwhile: nothing would find the snapshot again.
        with contextlib.suppress(Exception):
            await sandbox.delete_snapshot(snapshot_id)
        raise
    return {
        "name": name,
        "id": snapshot_id,
        "created_at": created_at,
        "sandbox_id": sandbox_id,
        "kind": "manual",
    }


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
    record, outcomes = await _reconcile(session, record, EXIT_WAIT)
    if _is_running(record, outcomes):
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
            raise NotFound(_nothing_to_restore(record))
        snapshot_id = store.ordered(record)[-1]["id"]
    try:
        sandbox_id = await sandbox.create(
            session, **record["options"], name=name, snapshot_id=snapshot_id
        )
    except modal.exception.NotFoundError as error:
        if snapshot_id not in str(error):
            raise
        raise NotFound(_gone(record, snapshot_id)) from error
    except modal.exception.AlreadyExistsError as error:
        raise InvalidRequest(
            f"{name} is running already; another restore may have started it."
        ) from error

    def change(latest: store.Record) -> None:
        store.add_sandbox(latest, sandbox_id, restored_from=snapshot_id)

    await store.update(session, name, change)
    return await _started(
        session, sandbox_id, name, tailscale, hostname, snapshot=snapshot_id
    )


def _nothing_to_restore(record: store.Record) -> str:
    name = record["name"]
    if record["options"].get("exit_snapshot", True):
        return f"{name} has no snapshots."
    return (
        f"{name} has no snapshots: it was started with --no-snapshot, so Modal "
        "saves nothing when its sandbox ends. Save one while it runs with "
        f"agentix sandbox snapshot {name}."
    )


def _gone(record: store.Record, snapshot_id: str) -> str:
    [snapshot] = [item for item in record["snapshots"] if item["id"] == snapshot_id]
    return (
        f"Snapshot {snapshot_id} of {record['name']} no longer exists: it was "
        "deleted, or it expired. Modal keeps a snapshot for 30 days, and this one "
        f"is from {snapshot['created_at']}. See agentix sandbox snapshots "
        f"{record['name']}."
    )


async def purge(
    session: Session, target: str, terminate: bool = False
) -> dict[str, Any]:
    """Delete every snapshot of a name, then its record.

    A running sandbox is refused, unless `terminate` ends it first. Its exit
    snapshot is then deleted too.
    """
    record = await _record_for(session, target)
    name = record["name"]
    record, outcomes = await _reconcile(session, record, EXIT_WAIT)
    if _is_running(record, outcomes):
        current = record["sandbox_id"]
        if not terminate:
            raise InvalidRequest(
                f"{name} is running as {current}. Stop it with agentix sandbox "
                f"down {name}, or pass --terminate."
            )
        await sandbox.stop(session, current)
        record, _ = await _reconcile(session, record, EXIT_WAIT, [current])
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
    deleted = [
        snapshot_id for snapshot_id in snapshot_ids if snapshot_id not in failures
    ]
    if failures:
        await store.update(
            session, name, lambda latest: store.remove_snapshots(latest, deleted)
        )
        first = next(iter(failures.values()))
        raise OperationFailed(
            f"Could not delete {', '.join(failures)} ({first}). The record keeps "
            "them; retry the purge."
        )
    await store.remove(session, name, deleted)
    return {
        "name": name,
        "sandboxes": [item["id"] for item in record["sandboxes"]],
        "deleted_snapshots": snapshot_ids,
    }
