"""Where records live: JSON files on a Modal Volume, cached in a Modal Dict.

A Modal Dict drops an entry that nothing reads or writes for 7 days, and the
snapshots of that name could then no longer be found or deleted. A Volume
keeps its files until they are deleted, so the record of a name lives in
records/NAME/ on the Volume, and no job has to keep it alive.

Modal cannot write a file only if nobody changed it since it was read. Not
even creating a new path is exclusive: two processes that created one path
at once with `batch_upload(force=False)` were both seen to succeed. So a
change never rewrites a file. It writes the record, with the change made, to
a new file with a unique name, and then deletes the files it read. Two
commands that change a record at once leave a file each, and the next read
merges them (see `store.merge`), so neither change is lost.

Listing the Volume takes about 0.3 s and reading a file about 0.8 s, so the
Dict keeps a copy of the files of each name. The listing shows whether a copy
is current; a stale or expired copy costs only a read. A purge leaves a file
that ends in .purged.json, so a listing shows a purged name without a read.
"""

import asyncio
import contextlib
import datetime as dt
import io
import json
import secrets
from typing import Any

import modal
from modal.volume import FileEntryType

from .errors import OperationFailed
from .session import Session

ROOT = "records"
_SUFFIX = ".json"
_PURGED = ".purged.json"
# A command that merges a file into a newer one deletes it, also between the
# listing of another command and its read. That read then starts again.
_ATTEMPTS = 3

# The files of one name, by path, each with the record it holds.
Files = dict[str, dict[str, Any]]


async def read(session: Session, name: str) -> Files:
    """The files of `name`, or {} if it has none."""
    volume, cache = await _handles(session)
    paths, cached = await asyncio.gather(
        _list(volume, f"{ROOT}/{name}"), _cached(cache, name)
    )
    return await _read(volume, cache, name, paths, cached)


async def read_all(session: Session) -> dict[str, Files]:
    """The files of every name that was not purged, by name."""
    volume, cache = await _handles(session)
    paths, cached = await asyncio.gather(
        _list(volume, ROOT, recursive=True), _cached_all(cache)
    )
    by_name: dict[str, list[str]] = {}
    for path in paths:
        parts = path.split("/")
        if len(parts) == 3:
            by_name.setdefault(parts[1], []).append(path)
    live = {
        name: found
        for name, found in by_name.items()
        if not all(path.endswith(_PURGED) for path in found)
    }
    results = await asyncio.gather(
        *(
            _read(volume, cache, name, found, cached.get(name))
            for name, found in live.items()
        )
    )
    return dict(zip(live, results))


async def write(
    session: Session,
    name: str,
    record: dict[str, Any],
    replaces: list[str],
    purged: bool = False,
) -> str:
    """Store `record` as the record of `name`, in place of the files `replaces`.

    Those files are deleted, so `record` must hold all that they held.
    `purged` marks the record of a purged name. Returns the new file's path.
    """
    volume, cache = await _handles(session)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
    suffix = _PURGED if purged else _SUFFIX
    path = f"{ROOT}/{name}/{stamp}-{secrets.token_hex(4)}{suffix}"
    # The name is unique; force=False only makes sure that nothing is replaced.
    async with volume.batch_upload.aio(force=False) as batch:
        batch.put_file(io.BytesIO(json.dumps(record).encode()), f"/{path}")
    # Neither step is needed for correctness: a file left behind is merged by
    # the next read, and a stale copy is noticed. Failures are ignored.
    await asyncio.gather(
        *(volume.remove_file.aio(f"/{old}") for old in replaces),
        cache.pop.aio(name, None) if purged else cache.put.aio(name, {path: record}),
        return_exceptions=True,
    )
    return path


async def _handles(session: Session) -> tuple[modal.Volume, modal.Dict]:
    return await asyncio.gather(session.record_volume(), session.record_cache())


async def _read(
    volume: modal.Volume,
    cache: modal.Dict,
    name: str,
    paths: list[str],
    cached: Files | None,
) -> Files:
    for _ in range(_ATTEMPTS):
        if not paths:
            return {}
        if cached is not None and set(cached) == set(paths):
            return cached
        try:
            records = await asyncio.gather(*(_read_file(volume, p) for p in paths))
        except FileNotFoundError:
            paths, cached = await _list(volume, f"{ROOT}/{name}"), None
            continue
        found = dict(zip(paths, records))
        with contextlib.suppress(Exception):
            await cache.put.aio(name, found)
        return found
    raise OperationFailed(f"The record of {name} kept changing while it was read.")


async def _list(volume: modal.Volume, path: str, recursive: bool = False) -> list[str]:
    try:
        entries = await volume.listdir.aio(f"/{path}", recursive=recursive)
    except modal.exception.NotFoundError:
        # Nothing was ever stored there.
        return []
    return [
        entry.path
        for entry in entries
        if entry.type == FileEntryType.FILE and entry.path.endswith(_SUFFIX)
    ]


async def _read_file(volume: modal.Volume, path: str) -> dict[str, Any]:
    data = b"".join([chunk async for chunk in volume.read_file.aio(f"/{path}")])
    return json.loads(data)


async def _cached(cache: modal.Dict, name: str) -> Files | None:
    # A cache that fails is only slower: the Volume answers instead.
    with contextlib.suppress(Exception):
        return await cache.get.aio(name)
    return None


async def _cached_all(cache: modal.Dict) -> dict[str, Files]:
    with contextlib.suppress(Exception):
        return {name: files async for name, files in cache.items.aio()}
    return {}
