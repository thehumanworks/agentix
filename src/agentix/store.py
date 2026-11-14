"""The record of each named sandbox, one Modal Dict entry per name.

A name outlives its sandboxes. Its record holds the current sandbox, every
sandbox the name has had, the snapshots they left, and the options to start
the next one with. Modal cannot list snapshots, so this record is the only way
to find them again, and to delete them.

Records hold only plain values, because the Dict pickles what it stores.
Modal drops a Dict entry that nothing reads or writes for 7 days.
"""

import datetime as dt
from typing import Literal, TypedDict

from .errors import NotFound
from .session import Session

# Modal ends a sandbox at its timeout with this exit code.
TIMEOUT_EXIT_CODE = 124


class Options(TypedDict):
    image_name: str
    secret_name: str
    timeout: int
    workdir: str
    memory: int
    cpu: float


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
    sandbox_id: str
    sandboxes: list[SandboxRecord]
    snapshots: list[SnapshotRecord]


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
    }


async def claim(session: Session, record: Record) -> bool:
    """Store a new record. False if the name has one already."""
    store = await session.store()
    return await store.put.aio(record["name"], record, skip_if_exists=True)


async def load(session: Session, name: str) -> Record:
    store = await session.store()
    record = await store.get.aio(name)
    if record is None:
        raise NotFound(f"No sandbox is named {name}. See agentix sandbox ls.")
    return record


async def save(session: Session, record: Record) -> None:
    store = await session.store()
    await store.put.aio(record["name"], record)


async def remove(session: Session, name: str) -> None:
    store = await session.store()
    await store.pop.aio(name, None)


async def records(session: Session) -> list[Record]:
    store = await session.store()
    return [record async for _, record in store.items.aio()]


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


def pending(record: Record) -> list[SandboxRecord]:
    """Sandboxes whose exit snapshot is not recorded yet."""
    return [sandbox for sandbox in record["sandboxes"] if sandbox["exit"] == "pending"]


def ordered(record: Record) -> list[SnapshotRecord]:
    """Snapshots from oldest to newest. Restore takes the last by default."""
    return sorted(record["snapshots"], key=lambda snapshot: snapshot["created_at"])


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
