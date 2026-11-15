"""This month's Modal spend of agentix, by sandbox name. See docs/cost.md.

Modal bills the whole `agentix` app, not each sandbox, so only the app's
total is Modal's own number. The share of each name is an estimate: how long
its sandboxes ran, times what they asked for, times the price of sandbox CPU
and memory. Everything here only reads; nothing is created or changed.
"""

import asyncio
import contextlib
import datetime as dt
import json
import warnings
from collections.abc import Iterable
from decimal import Decimal
from functools import reduce
from typing import Any

import modal

from . import store
from .defaults import APP_NAME, CACHE_NAME, LEGACY_STORE_NAME, STORE_NAME
from .image import BUILD_CPU, BUILD_MEMORY_MIB
from .runner import APP_NAME as RUNNER_APP_NAME
from .sandbox import NAME_TAG

# The one price table. Modal's list prices for sandboxes, which run at the
# non-preemptible rate, 3x that of functions. Used when the workspace's own
# rates (Workspace.billing.rates) cannot be read.
PRICES = {
    "source": "https://modal.com/pricing",
    "checked": "2026-11-15",
    "cpu_core_second": Decimal("0.00003942"),
    "memory_gib_second": Decimal("0.00000667"),
    # Modal bills at least this much CPU for a container.
    "min_cpu_cores": Decimal("0.125"),
}
# Modal's billing has no line for snapshot or image storage: the monthly
# summary splits metered cost into apps, volumes, egress, and tokens only.
SNAPSHOT_STORAGE = "not metered by Modal (no billing line for snapshot storage)"

IMAGE_BUILDS = "(image builds)"
UNNAMED = "(unnamed)"


async def collect(now: dt.datetime | None = None) -> dict[str, Any]:
    """Read what Modal and the records hold for this month, and summarize it."""
    now = now or dt.datetime.now(dt.UTC)
    start = month_start(now)
    app_id = await _app_id(APP_NAME)
    runner_id = await _app_id(RUNNER_APP_NAME)
    (
        (listed, listing),
        records,
        (rates, rates_source),
        (billed, billing),
    ) = await asyncio.gather(
        _listed(app_id),
        _records(),
        _rates(),
        _billing(start, now, {i for i in (app_id, runner_id) if i}),
    )
    return summarize(
        start=start,
        now=now,
        listed=listed,
        records=records,
        prices=rates,
        sources={"listing": listing, "rates": rates_source, "billing": billing},
        billed=billed,
        app_ids={app_id: APP_NAME, runner_id: RUNNER_APP_NAME},
    )


def month_start(now: dt.datetime) -> dt.datetime:
    return now.astimezone(dt.UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )


def summarize(
    *,
    start: dt.datetime,
    now: dt.datetime,
    listed: list[dict[str, Any]],
    records: list[store.Record],
    prices: dict[str, Decimal],
    sources: dict[str, str],
    billed: list[dict[str, Any]] | None,
    app_ids: dict[str | None, str],
) -> dict[str, Any]:
    """Per-name estimates, and what Modal billed the apps, as plain values.

    `listed` holds sandboxes as Modal lists them (see `_listed`); `billed`
    holds the rows of Modal's billing report, or None if it could not be read.
    """
    runs: dict[str, dict[str, Any]] = {}
    for item in listed:
        runs[item["id"]] = {**item, "source": "modal"}
    for record in records:
        for sandbox in record["sandboxes"]:
            if sandbox["id"] in runs:
                # Modal's listing knows when it really started and ended.
                run = runs[sandbox["id"]]
                run["name"] = run["name"] or record["name"]
                continue
            runs[sandbox["id"]] = _from_record(record, sandbox, now)
    names: dict[str, dict[str, Any]] = {}
    for run in runs.values():
        seconds = _overlap(run["start"], run["end"], start, now)
        if run["name"]:
            label = run["name"]
        elif run["cpu"] == BUILD_CPU and run["memory_mib"] == BUILD_MEMORY_MIB:
            label = IMAGE_BUILDS
        else:
            label = UNNAMED
        row = names.setdefault(label, _empty_row(label))
        if seconds <= 0:
            continue
        cores = max(Decimal(str(run["cpu"])), prices["min_cpu_cores"])
        gib = Decimal(run["memory_mib"]) / 1024
        row["sandboxes"] += 1
        row["seconds"] += seconds
        row["cpu"] += cores * Decimal(seconds) * prices["cpu_core_second"]
        row["memory"] += gib * Decimal(seconds) * prices["memory_gib_second"]
        row["sources"][run["source"]] = row["sources"].get(run["source"], 0) + 1
    for record in records:
        row = names.setdefault(record["name"], _empty_row(record["name"]))
        live = [s for s in record["snapshots"] if store.expires_at(s) > _iso(now)]
        row["snapshots"] = len(live)
        row["snapshots_this_month"] = sum(
            store.parse_time(s["created_at"]) >= start for s in record["snapshots"]
        )
    rows = [
        _finish(row)
        for row in sorted(
            names.values(), key=lambda r: (r["name"].startswith("("), r["name"])
        )
        if row["sandboxes"] or row["snapshots"]
    ]
    estimated = sum((Decimal(r["total"]) for r in rows), Decimal(0))
    result: dict[str, Any] = {
        "month": start.strftime("%Y-%m"),
        "from": _iso(start),
        "to": _iso(now),
        "rows": rows,
        "estimated_total": str(_round(estimated)),
        "prices": {
            "cpu_core_second": f"{prices['cpu_core_second']:.8g}",
            "memory_gib_second": f"{prices['memory_gib_second']:.8g}",
            "min_cpu_cores": str(prices["min_cpu_cores"]),
            "source": sources["rates"],
        },
        "snapshot_storage": SNAPSHOT_STORAGE,
        "sources": {
            "listing": sources["listing"],
            "records": f"agentix records on the Modal Volume {STORE_NAME}",
            "billing": sources["billing"],
        },
        "billed": None,
    }
    if billed is not None:
        apps: dict[str, dict[str, Decimal]] = {}
        last = None
        for item in billed:
            app = app_ids.get(item["object_id"]) or item["description"]
            costs = apps.setdefault(app, {})
            for resource, cost in item["cost_by_resource"].items():
                costs[resource] = costs.get(resource, Decimal(0)) + Decimal(cost)
            last = max(last or item["interval_end"], item["interval_end"])
        total = sum((sum(c.values(), Decimal(0)) for c in apps.values()), Decimal(0))
        result["billed"] = {
            "total": str(_round(total)),
            "apps": {
                app: {
                    "total": str(_round(sum(costs.values(), Decimal(0)))),
                    "by_resource": {
                        k: str(_round(v)) for k, v in sorted(costs.items())
                    },
                }
                for app, costs in sorted(apps.items())
            },
            "through": _iso(min(last, now)) if last else _iso(start),
            "not_attributed": str(_round(total - estimated)),
        }
    return result


def render(summary: dict[str, Any]) -> str:
    """The summary as a table, with where each number comes from."""
    lines = [
        f"Modal spend of agentix for {summary['month']}, "
        f"{summary['from']} to {summary['to']}.",
        "",
        "Per name: ESTIMATES (~), from runtimes times sandbox prices; Modal does "
        "not bill per sandbox.",
    ]
    table = [("NAME", "SANDBOXES", "HOURS", "CPU $", "MEMORY $", "TOTAL $")]
    table[0] += ("SNAPSHOTS", "SOURCE")
    for row in summary["rows"]:
        table.append(
            (
                row["name"],
                str(row["sandboxes"]),
                f"{row['seconds'] / 3600:.2f}",
                f"~{row['cpu']}",
                f"~{row['memory']}",
                f"~{row['total']}",
                f"{row['snapshots']} ({row['snapshots_this_month']} new)",
                row["source"],
            )
        )
    table.append(
        ("Estimated total", "", "", "", "", f"~{summary['estimated_total']}", "", "")
    )
    widths = [max(len(r[i]) for r in table) for i in range(len(table[0]))]
    lines += [
        "  ".join(cell.ljust(w) for cell, w in zip(r, widths)).rstrip() for r in table
    ]
    lines.append("")
    billed = summary["billed"]
    if billed is None:
        lines.append(f"Billed by Modal: unavailable ({summary['sources']['billing']}).")
    else:
        lines.append(
            f"Billed by Modal (exact, {summary['sources']['billing']}), "
            f"through {billed['through']}: ${billed['total']}"
        )
        for app, costs in billed["apps"].items():
            parts = ", ".join(f"{k} {v}" for k, v in costs["by_resource"].items())
            lines.append(f"  app {app}: ${costs['total']} ({parts})")
        lines.append(
            f"  Not attributed to a row above (billed minus estimated): "
            f"${billed['not_attributed']}"
        )
    prices = summary["prices"]
    lines += [
        "",
        f"Snapshot storage: {summary['snapshot_storage']}; counts are snapshots "
        "that have not expired (and those taken this month).",
        f"Prices: sandbox CPU ${prices['cpu_core_second']} per core-second (at "
        f"least {prices['min_cpu_cores']} core), memory "
        f"${prices['memory_gib_second']} per GiB-second; {prices['source']}.",
        f"Runtimes: {summary['sources']['listing']}; {summary['sources']['records']}.",
        "SOURCE: modal = start and end from Modal's sandbox listing; record = "
        "start from the agentix record, end from its exit snapshot; "
        "record-timeout = no end recorded, so its timeout (an upper bound).",
        f"{IMAGE_BUILDS}: unnamed sandboxes with the builder's {BUILD_CPU:g} CPU "
        f"and {BUILD_MEMORY_MIB} MiB. {UNNAMED}: other sandboxes with no name "
        "(--ephemeral, flake update).",
    ]
    return "\n".join(lines)


def _from_record(
    record: store.Record, sandbox: store.SandboxRecord, now: dt.datetime
) -> dict[str, Any]:
    begun = store.parse_time(sandbox["started_at"])
    ends = [
        store.parse_time(s["created_at"])
        for s in record["snapshots"]
        if s["sandbox_id"] == sandbox["id"] and s["kind"] != "manual"
    ]
    options = record["options"]
    if ends:
        end, source = min(ends), "record"
    else:
        timeout = dt.timedelta(seconds=options["timeout"])
        end, source = min(begun + timeout, now), "record-timeout"
    return {
        "id": sandbox["id"],
        "name": record["name"],
        "start": begun,
        "end": end,
        "cpu": float(options["cpu"]),
        "memory_mib": int(options["memory"]),
        "source": source,
    }


def _overlap(
    begun: dt.datetime, end: dt.datetime, start: dt.datetime, now: dt.datetime
) -> float:
    return max(0.0, (min(end, now) - max(begun, start)).total_seconds())


def _empty_row(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "sandboxes": 0,
        "seconds": 0.0,
        "cpu": Decimal(0),
        "memory": Decimal(0),
        "snapshots": 0,
        "snapshots_this_month": 0,
        "sources": {},
    }


def _finish(row: dict[str, Any]) -> dict[str, Any]:
    total = row["cpu"] + row["memory"]
    sources = ", ".join(f"{k} {v}" for k, v in sorted(row["sources"].items()))
    return {
        **row,
        "seconds": round(row["seconds"]),
        "cpu": str(_round(row["cpu"])),
        "memory": str(_round(row["memory"])),
        "total": str(_round(total)),
        "estimate": True,
        "source": f"estimate: {sources}" if sources else "no runtime this month",
    }


def _round(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))


def _iso(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).isoformat(timespec="seconds")


async def _app_id(name: str) -> str | None:
    """The ID of the app `name`, or None. It is never created here."""
    try:
        return (await modal.App.lookup.aio(name=name)).app_id
    except modal.exception.NotFoundError:
        return None


async def _listed(app_id: str | None) -> tuple[list[dict[str, Any]], str]:
    """The app's sandboxes as Modal lists them, and what that listing covers.

    Modal's public `Sandbox.list` shows only running sandboxes. Its API also
    lists ended ones, as far back as Modal keeps them (about two days were
    seen), with their start, end, and resources.
    """
    if app_id is None:
        return [], f"no Modal app named {APP_NAME}"
    try:
        infos = await _list_sandboxes(app_id)
        oldest = min((info.created_at for info in infos), default=None)
        since = (
            f" back to {_iso(dt.datetime.fromtimestamp(oldest, dt.UTC))}"
            if oldest
            else ""
        )
        return [_listed_item(info) for info in infos], (
            f"Modal's sandbox listing, ended sandboxes included{since}"
        )
    except Exception as error:
        # The records still give the runtimes of named sandboxes.
        return [], (
            f"Modal's listing of ended sandboxes failed ({type(error).__name__}), "
            "so unnamed sandboxes are not counted"
        )


def _listed_item(info: Any) -> dict[str, Any]:
    tags = {tag.tag_name: tag.tag_value for tag in info.tags}
    begun = info.task_info.started_at or info.created_at
    finished = info.task_info.finished_at
    return {
        "id": info.id,
        "name": tags.get(NAME_TAG, ""),
        "start": dt.datetime.fromtimestamp(begun, dt.UTC),
        "end": dt.datetime.fromtimestamp(finished, dt.UTC)
        if finished
        else dt.datetime.max.replace(tzinfo=dt.UTC),
        "cpu": info.resource_info.milli_cpu.value / 1000,
        "memory_mib": info.resource_info.memory_mb.value,
    }


async def _list_finished(app_id: str) -> list[Any]:
    from modal.client import _Client
    from modal_proto import api_pb2

    client = await _Client.from_env()
    infos, before = [], None
    with warnings.catch_warnings():
        # The listing of ended sandboxes has no public method.
        warnings.simplefilter("ignore")
        while True:
            request = api_pb2.SandboxListRequest(
                app_id=app_id, before_timestamp=before, include_finished=True
            )
            response = await client.stub.SandboxList(request)
            if not response.sandboxes:
                return infos
            infos += response.sandboxes
            before = response.sandboxes[-1].created_at


async def _list_sandboxes(app_id: str) -> list[Any]:
    from modal._utils.async_utils import synchronizer

    # Run on Modal's own event loop, where its connection lives; called from
    # another loop, each page took a minute.
    return await synchronizer.create_blocking(_list_finished).aio(app_id)


async def _records() -> list[store.Record]:
    """Every record, read without writing anything back.

    `store.records` stores what it merges and refreshes a cache; this does
    neither, and finds nothing where no Volume or Dict exists.
    """
    volume = modal.Volume.from_name(STORE_NAME, version=2)
    try:
        await volume.hydrate.aio()
        entries = await volume.listdir.aio("/records", recursive=True)
    except modal.exception.NotFoundError:
        entries = []
    by_name: dict[str, set[str]] = {}
    for entry in entries:
        parts = entry.path.split("/")
        if entry.path.endswith(".json") and len(parts) == 3:
            by_name.setdefault(parts[1], set()).add(entry.path)
    # The cache holds the files of each name, as recordfiles.py keeps them;
    # a copy whose files match the listing saves reading them.
    cached: dict[str, dict[str, Any]] = {}
    cache = modal.Dict.from_name(CACHE_NAME)
    with contextlib.suppress(Exception):
        await cache.hydrate.aio()
        cached = {name: files async for name, files in cache.items.aio()}
    found: dict[str, list[store.Record]] = {}
    unread = []
    for name, paths in by_name.items():
        if set(cached.get(name) or {}) == paths:
            found[name] = list(cached[name].values())
        else:
            unread += sorted(paths)

    async def read(path: str) -> tuple[str, store.Record] | None:
        # A file merged away meanwhile is in the newer file.
        with contextlib.suppress(FileNotFoundError):
            data = b"".join([c async for c in volume.read_file.aio(f"/{path}")])
            return path.split("/")[1], json.loads(data)
        return None

    for item in await asyncio.gather(*(read(p) for p in unread)):
        if item is not None:
            found.setdefault(item[0], []).append(item[1])
    legacy = modal.Dict.from_name(LEGACY_STORE_NAME)
    with contextlib.suppress(modal.exception.NotFoundError):
        await legacy.hydrate.aio()
        async for name, record in legacy.items.aio():
            found.setdefault(name, []).append(record)
    merged = [reduce(store.merge, copies[1:], copies[0]) for copies in found.values()]
    return [r for r in merged if r["sandboxes"] or r["snapshots"]]


async def _rates() -> tuple[dict[str, Decimal], str]:
    try:
        rates = await modal.Workspace.from_context().billing.rates.aio()
        prices = dict(PRICES) | {
            "cpu_core_second": rates["cpu_hour_cost_sandbox"] / 3600,
            "memory_gib_second": rates["mem_gib_hour_cost_sandbox"] / 3600,
        }
        return prices, "the workspace's rates from Modal (Workspace.billing.rates)"
    except Exception as error:
        return dict(PRICES), (
            f"the table in agentix/cost.py ({PRICES['source']}, checked "
            f"{PRICES['checked']}); Modal's rates were unavailable "
            f"({type(error).__name__})"
        )


async def _billing(
    start: dt.datetime, now: dt.datetime, app_ids: Iterable[str]
) -> tuple[list[dict[str, Any]] | None, str]:
    """The billing report rows of `app_ids` from `start` to the last full hour.

    Modal reports whole intervals only, and an hourly report spans at most 7
    days, so whole days come from a daily report and today from an hourly one.
    """
    app_ids = set(app_ids)
    today = now.astimezone(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    parts = [(start, today, "d", dt.timedelta(days=1))] if today > start else []
    parts.append((max(start, today), now, "h", dt.timedelta(hours=1)))
    billing = modal.Workspace.from_context().billing
    rows = []
    try:
        for begin, end, resolution, length in parts:
            report = await billing.report.aio(
                start=begin, end=end, resolution=resolution
            )
            rows += [
                {
                    "object_id": item.object_id,
                    "description": item.description,
                    "interval_start": item.interval_start,
                    "interval_end": item.interval_start + length,
                    "cost_by_resource": dict(item.cost_by_resource),
                }
                for item in report
                if item.object_id in app_ids
            ]
    except Exception as error:
        return None, f"Modal's billing report failed: {type(error).__name__}: " + (
            str(error)[:200]
        )
    return rows, "Modal's billing report (Workspace.billing.report), by app"
