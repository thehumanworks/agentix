"""The record of each named sandbox, and how it changes.

A name outlives its sandboxes. Its record holds the current sandbox, every
sandbox the name has had, the snapshots they left, and the options to start
the next one with. Modal cannot list snapshots, so this record is the only way
to find them again, and to delete them. `recordfiles.py` keeps it on a Volume.

Two commands can change one record at once, and Modal cannot make the second
wait, so copies of a record are merged, never chosen between: sandboxes and
snapshots are only added, an exit is only recorded, and a purge hides IDs in
`removed` instead of dropping them. Copies merged in any order give one
record, and no snapshot ID is lost.

Records hold only plain values, because they are stored as JSON.
"""

import asyncio
import copy
import datetime as dt
import json
from collections.abc import Callable, Collection, Iterable
from typing import Literal, NotRequired, TypedDict

from . import recordfiles
from .errors import NotFound, OperationFailed
from .session import Session

# Modal ends a sandbox at its timeout with this exit code.
TIMEOUT_EXIT_CODE = 124
# Modal deletes a snapshot 30 days after it is taken: an exit snapshot always,
# and one from `sandbox snapshot` at the default `ttl` of snapshot_filesystem.
# Modal cannot tell whether a snapshot still exists short of starting from it.
SNAPSHOT_RETENTION = dt.timedelta(days=30)


class Options(TypedDict):
    image_name: str
    secret_name: str
    timeout: int
    workdir: str
    memory: int
    cpu: float
    # False after `sandbox up --no-snapshot`: Modal saves nothing when a
    # sandbox of the name ends. Older records lack it.
    exit_snapshot: NotRequired[bool]


class SandboxRecord(TypedDict):
    id: str
    started_at: str
    # The snapshot the sandbox started from, or "" for a new one.
    restored_from: str
    # "pending" until Modal reports the exit snapshot, then "saved" or "none".
    exit: Literal["pending", "saved", "none"]


class SnapshotRecord(TypedDict):
    id: str
    created_at: str
    sandbox_id: str
    # "manual" from `sandbox snapshot`; "down" when agentix ended the sandbox;
    # "timeout" when it reached its timeout; "exit" when it ended otherwise,
    # and `created_at` is when agentix first saw it had ended.
    kind: Literal["manual", "down", "timeout", "exit"]


class Record(TypedDict):
    name: str
    created_at: str
    options: Options
    # The sandbox that started last.
    sandbox_id: str
    sandboxes: list[SandboxRecord]
    snapshots: list[SnapshotRecord]
    # IDs of sandboxes and snapshots that a purge removed, so that an older
    # copy cannot bring them back. Records of older versions lack it.
    removed: list[str]


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def new_record(name: str, options: Options) -> Record:
    return {
        "name": name,
        "created_at": now(),
        "options": options,
        "sandbox_id": "",
        "sandboxes": [],
        "snapshots": [],
        "removed": [],
    }


async def find(session: Session, name: str) -> Record | None:
    """The record of `name`, or None if it has none."""
    record, _ = await _current(session, name)
    return record if _exists(record) else None


async def load(session: Session, name: str) -> Record:
    record = await find(session, name)
    if record is None:
        raise NotFound(f"No sandbox is named {name}. See agentix sandbox ls.")
    return record


async def update(
    session: Session, name: str, change: Callable[[Record], bool | None]
) -> Record:
    """Make `change` to the record of `name` and store the result.

    `change` edits the record it is given and returns False if it changed
    nothing. The record is read again first, so what another command stored
    meanwhile is kept. Returns the stored record.
    """
    record, files = await _current(session, name)
    if not _exists(record):
        raise NotFound(f"No sandbox is named {name}. See agentix sandbox ls.")
    if change(record) is not False:
        await recordfiles.write(session, name, record, files)
    return record


async def claim(session: Session, record: Record) -> bool:
    """Store the record of a new name. False if the name has one already."""
    name = record["name"]
    current, files = await _current(session, name)
    if _exists(current):
        return False
    # A purged name keeps what it removed.
    await recordfiles.write(session, name, merge(current, record), files)
    return True


async def remove(session: Session, name: str, deleted: Collection[str]) -> None:
    """Purge `name`, whose snapshots `deleted` were deleted.

    Refused if the record holds a snapshot that is not in `deleted` by now,
    because nothing else would find that snapshot again.
    """
    legacy = await session.legacy_records()
    if legacy is not None:
        # An older version would still see the name there.
        await legacy.pop.aio(name, None)
    record, files = await _current(session, name)
    if not _exists(record):
        return
    if left := [s["id"] for s in record["snapshots"] if s["id"] not in deleted]:
        raise OperationFailed(
            f"{name} has new snapshots ({', '.join(left)}). Retry the purge."
        )
    gone = {item["id"] for item in [*record["sandboxes"], *record["snapshots"]]}
    purged: Record = {
        **record,
        "sandbox_id": "",
        "sandboxes": [],
        "snapshots": [],
        "removed": sorted({*record["removed"], *gone}),
    }
    await recordfiles.write(session, name, purged, files, purged=True)


async def records(session: Session) -> list[Record]:
    """The record of every name."""
    stored, legacy = await asyncio.gather(
        recordfiles.read_all(session), _legacy_items(session)
    )
    found = {name: _combine(files.values()) for name, files in stored.items()}
    # Read again, with the legacy entry, each name that it adds something to.
    behind = [
        name
        for name, entry in legacy.items()
        if name not in found or merge(found[name], entry) != found[name]
    ]
    for name, (record, _) in zip(
        behind, await asyncio.gather(*(_current(session, n) for n in behind))
    ):
        found[name] = record
    return [record for record in found.values() if _exists(record)]


def merge(record: Record | None, other: Record) -> Record:
    """One record that holds all that `record` and `other` hold.

    The name, creation time, and options come from whichever was created
    last, so a name that was purged and taken again keeps its new options.
    """
    first, last = sorted([record or other, other], key=_created)
    removed = {*first.get("removed", []), *last.get("removed", [])}
    sandboxes: dict[str, SandboxRecord] = {}
    for sandbox in [*first["sandboxes"], *last["sandboxes"]]:
        known = sandboxes.get(sandbox["id"])
        if known is None or _EXIT_ORDER[sandbox["exit"]] > _EXIT_ORDER[known["exit"]]:
            sandboxes[sandbox["id"]] = sandbox
    snapshots: dict[str, SnapshotRecord] = {}
    for snapshot in [*first["snapshots"], *last["snapshots"]]:
        known = snapshots.get(snapshot["id"])
        # Two commands can see one exit; keep the earlier account of it.
        if known is None or _taken(snapshot) < _taken(known):
            snapshots[snapshot["id"]] = snapshot
    merged = copy.deepcopy(last)
    merged["removed"] = sorted(removed)
    merged["sandboxes"] = copy.deepcopy(
        sorted(
            (item for item in sandboxes.values() if item["id"] not in removed),
            key=lambda item: (item["started_at"], item["id"]),
        )
    )
    merged["snapshots"] = copy.deepcopy(
        sorted(
            (item for item in snapshots.values() if item["id"] not in removed),
            key=lambda item: (item["created_at"], item["id"]),
        )
    )
    merged["sandbox_id"] = merged["sandboxes"][-1]["id"] if merged["sandboxes"] else ""
    return merged


def add_sandbox(record: Record, sandbox_id: str, restored_from: str = "") -> None:
    record["sandbox_id"] = sandbox_id
    record["sandboxes"].append(
        {
            "id": sandbox_id,
            "started_at": now(),
            "restored_from": restored_from,
            "exit": "pending",
        }
    )


def add_snapshot(
    record: Record,
    snapshot_id: str,
    sandbox_id: str,
    kind: Literal["manual", "down", "timeout", "exit"],
    created_at: str = "",
) -> SnapshotRecord:
    snapshot: SnapshotRecord = {
        "id": snapshot_id,
        "created_at": created_at or now(),
        "sandbox_id": sandbox_id,
        "kind": kind,
    }
    record["snapshots"].append(snapshot)
    return snapshot


def remove_snapshots(record: Record, snapshot_ids: Collection[str]) -> bool:
    """Forget snapshots that were deleted. False if none of them was recorded."""
    gone = {s["id"] for s in record["snapshots"]} & set(snapshot_ids)
    record["snapshots"] = [s for s in record["snapshots"] if s["id"] not in gone]
    record["removed"] = sorted({*record["removed"], *gone})
    return bool(gone)


def pending(record: Record) -> list[SandboxRecord]:
    """Sandboxes whose exit snapshot is not recorded yet."""
    return [sandbox for sandbox in record["sandboxes"] if sandbox["exit"] == "pending"]


def ordered(record: Record) -> list[SnapshotRecord]:
    """Snapshots from oldest to newest. Restore takes the last by default."""
    return sorted(record["snapshots"], key=lambda snapshot: snapshot["created_at"])


def expires_at(snapshot: SnapshotRecord) -> str:
    """When Modal is expected to delete `snapshot`, from when it was taken.

    For an `exit` snapshot that is when agentix saw it, so the real date can
    be earlier.
    """
    taken = dt.datetime.fromisoformat(snapshot["created_at"])
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=dt.UTC)
    return (taken + SNAPSHOT_RETENTION).isoformat(timespec="seconds")


def record_exit(
    record: Record,
    sandbox: SandboxRecord,
    status: str,
    snapshot_id: str = "",
    exit_code: int | None = None,
    stopped_now: bool = False,
) -> bool:
    """Record what Modal reported for the exit snapshot of `sandbox`.

    `status` is that of `sandbox.ExitSnapshot`. `stopped_now` means this
    command ended the sandbox, so the snapshot is from now. Returns whether the
    record changed.
    """
    if status == "none":
        sandbox["exit"] = "none"
        return True
    if status != "saved":
        return False
    if stopped_now:
        kind, created_at = "down", now()
    elif exit_code == TIMEOUT_EXIT_CODE:
        started = dt.datetime.fromisoformat(sandbox["started_at"])
        deadline = started + dt.timedelta(seconds=record["options"]["timeout"])
        kind, created_at = "timeout", min(deadline.isoformat(timespec="seconds"), now())
    else:
        kind, created_at = "exit", now()
    add_snapshot(record, snapshot_id, sandbox["id"], kind, created_at)
    sandbox["exit"] = "saved"
    return True


_EXIT_ORDER = {"pending": 0, "none": 1, "saved": 2}


def _created(record: Record) -> tuple[str, str]:
    # The options break a tie, so the order of the arguments never matters.
    return record["created_at"], json.dumps(record["options"], sort_keys=True)


def _taken(snapshot: SnapshotRecord) -> tuple[str, str, str]:
    return snapshot["created_at"], snapshot["kind"], snapshot["sandbox_id"]


def _combine(records: Iterable[Record]) -> Record | None:
    combined = None
    for record in records:
        combined = merge(combined, record)
    return combined


def _exists(record: Record | None) -> bool:
    # A purge leaves a record with neither.
    return record is not None and bool(record["sandboxes"] or record["snapshots"])


async def _current(session: Session, name: str) -> tuple[Record | None, list[str]]:
    """The record of `name`, and the files it was read from.

    A record that an older version keeps in the legacy Dict is merged in, and
    the merge stored, whenever that entry adds something. The entry stays, so
    an older version still finds it.
    """
    files, entry = await asyncio.gather(
        recordfiles.read(session, name), _legacy_get(session, name)
    )
    record, paths = _combine(files.values()), list(files)
    if entry is not None and (merged := merge(record, entry)) != record:
        paths = [await recordfiles.write(session, name, merged, paths)]
        record = merged
    return record, paths


async def _legacy_get(session: Session, name: str) -> Record | None:
    legacy = await session.legacy_records()
    return None if legacy is None else await legacy.get.aio(name)


async def _legacy_items(session: Session) -> dict[str, Record]:
    legacy = await session.legacy_records()
    if legacy is None:
        return {}
    return {name: record async for name, record in legacy.items.aio()}
