"""Named sandboxes: start, stop, snapshot, restore, and purge them.

Each operation keeps the record of a name (see `store.py`) in step with Modal.
Modal saves a snapshot whenever a sandbox ends, also at its timeout, but tells
no one. Commands that read a record therefore first ask Modal about every
sandbox of that record whose snapshot is not recorded yet.
"""

import asyncio
import contextlib
import datetime as dt
import fnmatch
from collections.abc import Callable, Collection
from typing import Any

import modal

from . import idle, names, repo, sandbox, ssh, store
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
        details = await ssh.manage(
            session, "start", sandbox_id, "tailscale", hostname, name=name
        )
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
    clone: str = "",
    idle_stop: int = 0,
) -> dict[str, Any]:
    """Start a sandbox under `name`, or under a new name if none is given.

    A name that has a record already is refused, whether its sandbox runs or
    not: restore it, or purge it first. An `ephemeral` sandbox has no name, no
    record, and no exit snapshot, so stopping it leaves nothing behind.
    `clone`, OWNER/NAME[@REF], is cloned into NAME in the image's workdir,
    which becomes the sandbox's workdir; a sandbox whose clone fails is removed.
    `idle_stop`, in seconds, stops the sandbox once nothing has used it for
    that long; it is recorded with the sandbox, so restores keep it.
    """
    names.check_ephemeral(name, ephemeral)
    check_tailscale_start(tailscale, hostname, options["secret_name"])
    if clone:
        workdir = repo.parse(clone)[1]
        if options["workdir"]:
            raise InvalidRequest("--repo sets the workdir; leave out --workdir.")
        options = {**options, "workdir": workdir}
    if ephemeral:
        options = {**options, "exit_snapshot": False}
        sandbox_id = await sandbox.create(session, **options, idle_stop=idle_stop)
        await _clone(session, sandbox_id, clone)
        return await _started(session, sandbox_id, "", tailscale, hostname)
    if name:
        if await store.find(session, names.check_new_name(name)) is not None:
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
    sandbox_id = await sandbox.create(
        session, **options, name=name, idle_stop=idle_stop
    )
    await _clone(session, sandbox_id, clone)
    record = store.new_record(name, options)
    store.add_sandbox(record, sandbox_id, idle_stop=idle_stop)
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


async def _clone(session: Session, sandbox_id: str, spec: str) -> None:
    """Clone `spec` in a new sandbox that no record holds yet, or remove it."""
    if not spec:
        return
    try:
        result = await sandbox.execute(session, sandbox_id, repo.command(spec))
    except BaseException:
        await sandbox.discard(session, sandbox_id)
        raise
    if result["exit_code"] != 0:
        await sandbox.discard(session, sandbox_id)
        raise repo.failed(spec, result["exit_code"], result["stderr"])


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
            "idle_stop": idle.describe(store.idle_stop(record)),
        }
        for record in sorted(records, key=lambda record: record["name"])
    ]
    named = {record["sandbox_id"] for record in records}
    unnamed = [sandbox_id for sandbox_id in running_ids if sandbox_id not in named]
    # Only their tags say; a listing that cannot ask them still lists them.
    stops: dict[str, int] = {}
    if unnamed:
        with contextlib.suppress(Exception):
            stops = await sandbox.idle_stops(session, unnamed)
    rows += [
        {
            "name": "",
            "sandbox_id": sandbox_id,
            "status": "running",
            "snapshots": 0,
            "idle_stop": idle.describe(stops[sandbox_id])
            if sandbox_id in stops
            else "",
        }
        for sandbox_id in unnamed
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
    idle_stop: int | None = None,
) -> dict[str, Any]:
    """Start a new sandbox for a name from a snapshot, the latest by default.

    The sandbox the name has now must have ended. The new one gets the options
    the name was created with, and the idle stop of the sandbox it had last,
    unless `idle_stop` (seconds; 0 for none) gives a new one, which later
    restores keep.
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
    if idle_stop is None:
        idle_stop = store.idle_stop(record)
    try:
        sandbox_id = await sandbox.create(
            session,
            **record["options"],
            name=name,
            snapshot_id=snapshot_id,
            idle_stop=idle_stop,
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
        store.add_sandbox(
            latest, sandbox_id, restored_from=snapshot_id, idle_stop=idle_stop
        )

    await store.update(session, name, change)
    return await _started(
        session, sandbox_id, name, tailscale, hostname, snapshot=snapshot_id
    )


async def running(session: Session, target: str = "") -> tuple[str, str]:
    """The ID and name of the running sandbox that `target` names.

    `target` is a name or a sandbox ID; without one, the only sandbox that
    runs is meant. A sandbox that has ended is refused with what to do
    instead: it is not restored here.
    """
    if not target:
        rows = [row for row in await ls(session) if row["status"] == "running"]
        if len(rows) == 1:
            return rows[0]["sandbox_id"], rows[0]["name"]
        if not rows:
            raise NotFound(
                "No sandbox is running. Start one with agentix sandbox up, or "
                "continue one with agentix sandbox restore NAME; agentix "
                "sandbox ls lists them."
            )
        listed = "\n".join(
            f"  {row['name'] or row['sandbox_id']}"
            + (f" ({row['sandbox_id']})" if row["name"] else "")
            for row in rows
        )
        raise InvalidRequest(
            f"{len(rows)} sandboxes are running. Name one, as in agentix "
            f"sandbox shell NAME:\n{listed}"
        )
    if names.is_sandbox_id(target):
        if await sandbox.is_running(session, target):
            return target, await sandbox.name_of(session, target)
        name = await sandbox.name_of(session, target)
        hint = f" It was {name}; see agentix sandbox ls." if name else ""
        raise InvalidRequest(f"{target} is not running.{hint}")
    record = await store.load(session, names.check_name(target))
    record, outcomes = await _reconcile(session, record, 0)
    if _is_running(record, outcomes):
        return record["sandbox_id"], record["name"]
    raise InvalidRequest(_stopped(record))


def _stopped(record: store.Record) -> str:
    name = record["name"]
    if store.pending(record):
        return (
            f"{name} is stopped, and Modal is still saving its snapshot. "
            f"Shortly, continue it with agentix sandbox restore {name}."
        )
    if record["snapshots"]:
        latest = store.ordered(record)[-1]
        return (
            f"{name} is stopped. Continue it from its latest snapshot, of "
            f"{latest['created_at']}, with agentix sandbox restore {name}, "
            f"then run agentix sandbox shell {name} again."
        )
    reason = (
        ""
        if record["options"].get("exit_snapshot", True)
        else ", because it was started with --no-snapshot"
    )
    return (
        f"{name} is stopped and has no snapshot to continue from{reason}. Start "
        f"it afresh with agentix sandbox purge {name}, then agentix sandbox up "
        f"--name={name}."
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


async def prune(
    session: Session,
    older_than: dt.timedelta,
    keep_latest: bool = False,
    pattern: str = "",
    confirm: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Delete snapshots older than `older_than`, and names left with nothing.

    Only what the records hold is considered, and only names whose sandbox
    has ended: a running one, or one whose exit snapshot Modal is still
    saving, is skipped whole. `keep_latest` keeps the newest snapshot of each
    name. `pattern` limits it to the names that match, as in a shell glob. A
    stopped name loses its record too once it has no snapshot left and
    nothing of it is newer than `older_than`.

    `confirm` is shown the plan and says whether to go ahead; without it,
    nothing is deleted. Returns the plan, and under `result` what was done,
    or None if nothing was.
    """
    plan = await _prune_plan(session, older_than, keep_latest, pattern)
    # Shown also when there is nothing to delete, so the caller can say so.
    if confirm is None or not confirm(plan) or not plan["items"]:
        return {**plan, "result": None}
    # One name that fails must not stop the others half way.
    targets = list(plan["targets"].items())
    outcomes = await asyncio.gather(
        *(_prune_name(session, name, target) for name, target in targets),
        return_exceptions=True,
    )
    result: dict[str, Any] = {
        "deleted_snapshots": [],
        "removed_names": [],
        "failed": [],
        "skipped": [],
    }
    for (name, _), outcome in zip(targets, outcomes):
        if isinstance(outcome, BaseException):
            outcome = {"failed": [{"name": name, "id": "", "error": str(outcome)}]}
        for key in result:
            result[key] += outcome.get(key, [])
    return {**plan, "result": result}


async def _prune_plan(
    session: Session, older_than: dt.timedelta, keep_latest: bool, pattern: str
) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    cutoff = now - older_than
    running_ids, records = await asyncio.gather(
        sandbox.list_ids(session), store.records(session)
    )
    running = set(running_ids)
    chosen = [
        record
        for record in records
        if not pattern or fnmatch.fnmatchcase(record["name"], pattern)
    ]

    async def settle(record: store.Record) -> tuple[store.Record, str]:
        if record["sandbox_id"] in running:
            return record, "running"
        if not store.pending(record):
            return record, ""
        try:
            record, outcomes = await _reconcile(session, record, 0)
        except Exception as error:
            return record, f"its sandbox could not be checked ({error})"
        if _is_running(record, outcomes):
            return record, "running"
        if store.pending(record):
            return record, "Modal is still saving its exit snapshot"
        return record, ""

    settled = await asyncio.gather(*(settle(record) for record in chosen))
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    targets: dict[str, dict[str, Any]] = {}
    for record, reason in sorted(settled, key=lambda pair: pair[0]["name"]):
        name = record["name"]
        if reason:
            skipped.append({"name": name, "reason": reason})
            continue
        snapshots = store.ordered(record)
        kept = snapshots[-1:] if keep_latest else []
        old = [
            snapshot
            for snapshot in snapshots
            if snapshot not in kept
            and store.parse_time(snapshot["created_at"]) <= cutoff
        ]
        for snapshot in old:
            taken = store.parse_time(snapshot["created_at"])
            items.append(
                {
                    "name": name,
                    "kind": "snapshot",
                    "snapshot_kind": snapshot["kind"],
                    "id": snapshot["id"],
                    "created_at": snapshot["created_at"],
                    "age": (now - taken).total_seconds(),
                    "expired": store.parse_time(store.expires_at(snapshot)) <= now,
                }
            )
        # The last time anything happened to the name.
        active = max(
            store.parse_time(when)
            for when in [
                record["created_at"],
                *(item["started_at"] for item in record["sandboxes"]),
                *(snapshot["created_at"] for snapshot in snapshots),
            ]
        )
        remove = len(old) == len(snapshots) and active <= cutoff
        if remove:
            items.append(
                {
                    "name": name,
                    "kind": "sandbox",
                    "id": record["sandbox_id"],
                    "sandboxes": [item["id"] for item in record["sandboxes"]],
                    "created_at": active.isoformat(timespec="seconds"),
                    "age": (now - active).total_seconds(),
                }
            )
        if old or remove:
            targets[name] = {
                "sandbox_id": record["sandbox_id"],
                "sandboxes": [item["id"] for item in record["sandboxes"]],
                "snapshots": [snapshot["id"] for snapshot in old],
                "remove": remove,
            }
    return {
        "older_than": older_than.total_seconds(),
        "keep_latest": keep_latest,
        "pattern": pattern,
        "items": items,
        "skipped": skipped,
        "targets": targets,
    }


async def _prune_name(
    session: Session, name: str, target: dict[str, Any]
) -> dict[str, list]:
    """Carry out the plan for one name, unless it changed since the listing.

    Once snapshots are deleted, the record is brought in step whatever else
    fails, so that it never lists a snapshot that is gone.
    """
    latest = await store.find(session, name)
    if latest is None:
        return {"skipped": [{"name": name, "reason": "purged meanwhile"}]}
    if latest["sandbox_id"] != target["sandbox_id"] or any(
        item["id"] not in target["sandboxes"] for item in latest["sandboxes"]
    ):
        return {"skipped": [{"name": name, "reason": _CHANGED}]}
    if running := await sandbox.running_named(session, name):
        return {"skipped": [{"name": name, "reason": f"running as {running[0]}"}]}
    recorded = {snapshot["id"] for snapshot in latest["snapshots"]}
    snapshot_ids = [item for item in target["snapshots"] if item in recorded]
    results = await asyncio.gather(
        *(sandbox.delete_snapshot(snapshot_id) for snapshot_id in snapshot_ids),
        return_exceptions=True,
    )
    failed = [
        {"name": name, "id": snapshot_id, "error": str(result)}
        for snapshot_id, result in zip(snapshot_ids, results)
        if isinstance(result, BaseException)
    ]
    deleted = [
        snapshot_id
        for snapshot_id, result in zip(snapshot_ids, results)
        if not isinstance(result, BaseException)
    ]
    outcome: dict[str, list] = {"deleted_snapshots": deleted, "failed": failed}
    if target["remove"] and not failed:
        try:
            # A restore may have started a sandbox since the check above.
            if running := await sandbox.running_named(session, name):
                raise OperationFailed(f"running as {running[0]}")
            await store.remove(session, name, deleted, target["sandboxes"])
        except OperationFailed as error:
            # A sandbox or snapshot was added meanwhile: the name stays.
            outcome["skipped"] = [{"name": name, "reason": str(error)}]
        except Exception as error:
            failed.append({"name": name, "id": "", "error": f"not removed ({error})"})
        else:
            outcome["removed_names"] = [name]
            return outcome
    if deleted:
        try:
            await store.update(
                session, name, lambda record: store.remove_snapshots(record, deleted)
            )
        except Exception as error:
            failed.append(
                {
                    "name": name,
                    "id": "",
                    "error": "its record still lists deleted snapshots "
                    f"{', '.join(deleted)} ({error})",
                }
            )
    return outcome


_CHANGED = "it has a new sandbox since the listing"
