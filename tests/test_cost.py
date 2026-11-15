import asyncio
import datetime as dt
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import modal
from support import ModalTestCase
from test_lifecycle import OPTIONS

from agentix import cost, store
from agentix.errors import InvalidRequest
from agentix.image import BUILD_CPU, BUILD_MEMORY_MIB
from agentix.sandbox import NAME_TAG

NOW = dt.datetime(2026, 11, 15, 12, 30, tzinfo=dt.UTC)
START = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
PRICES = {
    "cpu_core_second": Decimal("0.00004"),
    "memory_gib_second": Decimal("0.000007"),
    "min_cpu_cores": Decimal("0.125"),
}
SOURCES = {"listing": "listing", "rates": "rates", "billing": "billing"}


def at(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


def listed(sandbox_id, name, begun, end, cpu=0.125, memory_mib=1024):
    return {
        "id": sandbox_id,
        "name": name,
        "start": at(begun),
        "end": at(end) if end else dt.datetime.max.replace(tzinfo=dt.UTC),
        "cpu": cpu,
        "memory_mib": memory_mib,
    }


def record(name, *sandboxes, snapshots=()):
    """A record whose sandboxes are (id, started_at) pairs."""
    result = store.new_record(name, dict(OPTIONS))
    for sandbox_id, started_at in sandboxes:
        store.add_sandbox(result, sandbox_id)
        result["sandboxes"][-1]["started_at"] = started_at
        result["sandboxes"][-1]["exit"] = "saved"
    for snapshot_id, sandbox_id, kind, created_at in snapshots:
        store.add_snapshot(result, snapshot_id, sandbox_id, kind, created_at)
    return result


def summarize(listed=(), records=(), billed=None):
    return cost.summarize(
        start=START,
        now=NOW,
        listed=list(listed),
        records=list(records),
        prices=PRICES,
        sources=SOURCES,
        billed=billed,
        app_ids={"ap-test": "agentix"},
    )


def rows(summary):
    return {row["name"]: row for row in summary["rows"]}


class SummarizeTest(ModalTestCase):
    def test_listed_runtime_is_priced_by_cpu_and_memory(self):
        summary = summarize(
            [listed("sb-1", "dev", "2026-11-15T10:00:00+00:00", None, 2, 4096)]
        )
        row = rows(summary)["dev"]
        # Running: counted up to now, 2.5 hours.
        self.assertEqual(row["seconds"], 9000)
        self.assertEqual(row["cpu"], "0.7200")  # 2 cores * 9000 s * 0.00004
        self.assertEqual(row["memory"], "0.2520")  # 4 GiB * 9000 s * 0.000007
        self.assertEqual(row["total"], "0.9720")
        self.assertTrue(row["estimate"])
        self.assertEqual(row["source"], "estimate: modal 1")
        self.assertEqual(summary["estimated_total"], "0.9720")

    def test_cpu_below_the_minimum_is_billed_at_the_minimum(self):
        summary = summarize(
            [
                listed(
                    "sb-1",
                    "dev",
                    "2026-11-15T10:00:00+00:00",
                    "2026-11-15T11:00:00+00:00",
                    0.05,
                    0,
                )
            ]
        )
        self.assertEqual(rows(summary)["dev"]["cpu"], "0.0180")  # 0.125 * 3600 s

    def test_runtime_before_the_month_is_left_out(self):
        summary = summarize(
            [
                listed(
                    "sb-1",
                    "dev",
                    "2026-10-31T23:00:00+00:00",
                    "2026-11-01T01:00:00+00:00",
                ),
                listed(
                    "sb-2",
                    "old",
                    "2026-10-30T00:00:00+00:00",
                    "2026-10-30T01:00:00+00:00",
                ),
            ]
        )
        self.assertEqual(rows(summary)["dev"]["seconds"], 3600)
        self.assertNotIn("old", rows(summary))

    def test_record_runtime_ends_at_the_exit_snapshot(self):
        named = record(
            "dev",
            ("sb-1", "2026-11-10T00:00:00+00:00"),
            snapshots=[
                ("im-m", "sb-1", "manual", "2026-11-10T00:01:00+00:00"),
                ("im-d", "sb-1", "down", "2026-11-10T00:05:00+00:00"),
            ],
        )
        row = rows(summarize(records=[named]))["dev"]
        self.assertEqual(row["seconds"], 300)
        self.assertEqual(row["source"], "estimate: record 1")
        self.assertEqual((row["snapshots"], row["snapshots_this_month"]), (2, 2))

    def test_record_without_an_end_counts_up_to_its_timeout(self):
        named = record("dev", ("sb-1", "2026-11-10T00:00:00+00:00"))
        row = rows(summarize(records=[named]))["dev"]
        self.assertEqual(row["seconds"], OPTIONS["timeout"])
        self.assertEqual(row["source"], "estimate: record-timeout 1")

    def test_modal_listing_wins_over_the_record(self):
        named = record("dev", ("sb-1", "2026-11-10T00:00:00+00:00"))
        summary = summarize(
            [
                listed(
                    "sb-1",
                    "",
                    "2026-11-10T00:00:00+00:00",
                    "2026-11-10T00:01:00+00:00",
                )
            ],
            [named],
        )
        row = rows(summary)["dev"]
        self.assertEqual((row["seconds"], row["sandboxes"]), (60, 1))
        self.assertEqual(row["source"], "estimate: modal 1")

    def test_builders_and_unnamed_sandboxes_have_rows_of_their_own(self):
        summary = summarize(
            [
                listed(
                    "sb-b",
                    "",
                    "2026-11-02T00:00:00+00:00",
                    "2026-11-02T00:10:00+00:00",
                    BUILD_CPU,
                    BUILD_MEMORY_MIB,
                ),
                listed(
                    "sb-e",
                    "",
                    "2026-11-02T00:00:00+00:00",
                    "2026-11-02T00:10:00+00:00",
                ),
            ]
        )
        self.assertEqual(
            [row["name"] for row in summary["rows"]], [cost.IMAGE_BUILDS, cost.UNNAMED]
        )
        self.assertEqual(rows(summary)[cost.IMAGE_BUILDS]["cpu"], "0.1920")

    def test_expired_snapshots_are_not_counted(self):
        named = record(
            "dev",
            snapshots=[
                ("im-old", "sb-0", "down", "2026-10-01T00:00:00+00:00"),
                ("im-new", "sb-0", "down", "2026-10-20T00:00:00+00:00"),
            ],
        )
        row = rows(summarize(records=[named]))["dev"]
        self.assertEqual((row["snapshots"], row["snapshots_this_month"]), (1, 0))
        self.assertEqual(row["source"], "no runtime this month")

    def test_a_late_exit_snapshot_is_an_upper_bound_capped_at_the_timeout(self):
        # Seen to have ended a week later; the timeout is 600 s.
        named = record(
            "dev",
            ("sb-1", "2026-11-03T00:00:00+00:00"),
            snapshots=[("im-e", "sb-1", "exit", "2026-11-10T00:00:00+00:00")],
        )
        row = rows(summarize(records=[named]))["dev"]
        self.assertEqual(row["seconds"], OPTIONS["timeout"])
        self.assertEqual(row["source"], "estimate: record-upper 1")
        # Even an early exit snapshot is only a bound.
        named["snapshots"][0]["created_at"] = "2026-11-03T00:01:00+00:00"
        row = rows(summarize(records=[named]))["dev"]
        self.assertEqual(row["seconds"], 60)
        self.assertEqual(row["source"], "estimate: record-upper 1")

    def test_a_down_snapshot_past_the_timeout_is_capped(self):
        named = record(
            "dev",
            ("sb-1", "2026-11-03T00:00:00+00:00"),
            snapshots=[("im-d", "sb-1", "down", "2026-11-03T05:00:00+00:00")],
        )
        row = rows(summarize(records=[named]))["dev"]
        self.assertEqual(row["seconds"], OPTIONS["timeout"])
        self.assertEqual(row["source"], "estimate: record-upper 1")

    def test_estimates_above_billed_are_explained(self):
        billed = [
            {
                "object_id": "ap-test",
                "description": "agentix",
                "interval_start": START,
                "interval_end": START + dt.timedelta(days=1),
                "cost_by_resource": {"CPU": Decimal("0.01")},
            }
        ]
        named = record("dev", ("sb-1", "2026-11-10T00:00:00+00:00"))
        summary = cost.summarize(
            start=START,
            now=NOW,
            listed=[],
            records=[named],
            prices={**PRICES, "memory_gib_second": Decimal("0.001")},
            sources=SOURCES,
            billed=billed,
            app_ids={"ap-test": "agentix"},
        )
        self.assertLess(Decimal(summary["billed"]["not_attributed"]), 0)
        text = cost.render(summary)
        self.assertIn("Estimates exceed what was billed by $", text)
        self.assertIn("record-timeout", text)
        self.assertNotIn("Not attributed", text)

    def test_billed_is_summed_by_app_and_compared(self):
        hour = dt.timedelta(hours=1)
        billed = [
            {
                "object_id": "ap-test",
                "description": "agentix",
                "interval_start": START,
                "interval_end": START + dt.timedelta(days=1),
                "cost_by_resource": {"CPU": Decimal("1.5"), "Memory": Decimal("0.5")},
            },
            {
                "object_id": "ap-test",
                "description": "agentix",
                "interval_start": NOW - hour,
                "interval_end": NOW + hour,
                "cost_by_resource": {"CPU": Decimal("0.25")},
            },
        ]
        summary = summarize(
            [
                listed(
                    "sb-1",
                    "dev",
                    "2026-11-15T10:00:00+00:00",
                    "2026-11-15T11:00:00+00:00",
                )
            ],
            billed=billed,
        )
        self.assertEqual(summary["billed"]["total"], "2.2500")
        self.assertEqual(
            summary["billed"]["apps"]["agentix"]["by_resource"],
            {"CPU": "1.7500", "Memory": "0.5000"},
        )
        # Not past the time of the summary.
        self.assertEqual(summary["billed"]["through"], cost._iso(NOW))
        estimated = Decimal(summary["estimated_total"])
        self.assertEqual(
            Decimal(summary["billed"]["not_attributed"]), Decimal("2.25") - estimated
        )
        json.dumps(summary)

    def test_render_marks_estimates_and_names_sources(self):
        summary = summarize(
            [listed("sb-1", "dev", "2026-11-15T10:00:00+00:00", None)],
            billed=[],
        )
        text = cost.render(summary)
        self.assertIn("ESTIMATES (~)", text)
        self.assertIn("~0.", text)
        self.assertIn("Billed by Modal (exact, billing)", text)
        self.assertIn("Snapshot storage: not metered by Modal", text)
        self.assertIn("Prices: sandbox CPU $0.00004 per core-second", text)
        unavailable = cost.render(summarize())
        self.assertIn("Billed by Modal: unavailable (billing).", unavailable)


def info(
    sandbox_id,
    name,
    started,
    finished,
    milli_cpu=125,
    memory_mb=8192,
    timeout=3600,
    ended=None,
    created=None,
):
    """A sandbox as Modal's listing gives it. It has a result once it ended."""
    ended = bool(finished) if ended is None else ended
    return SimpleNamespace(
        id=sandbox_id,
        created_at=created if created is not None else started - 1,
        timeout_secs=timeout,
        tags=[SimpleNamespace(tag_name=NAME_TAG, tag_value=name)] if name else [],
        task_info=SimpleNamespace(
            started_at=started,
            finished_at=finished,
            result=SimpleNamespace() if ended else None,
        ),
        resource_info=SimpleNamespace(
            milli_cpu=SimpleNamespace(value=milli_cpu),
            memory_mb=SimpleNamespace(value=memory_mb),
        ),
    )


class CollectTest(ModalTestCase):
    def setUp(self):
        super().setUp()
        self.without_legacy()
        self.infos = []
        self.list_sandboxes = self._patch("agentix.cost._list_sandboxes")
        self.list_sandboxes.side_effect = self._infos
        self.report_items = []
        workspace = Mock()
        workspace.billing.report.aio = AsyncMock(
            # The rows are those of the daily report; the hourly one has none.
            side_effect=lambda resolution, **_: (
                list(self.report_items) if resolution == "d" else []
            )
        )
        workspace.billing.rates.aio = AsyncMock(
            return_value={
                "cpu_hour_cost_sandbox": Decimal("0.144"),
                "mem_gib_hour_cost_sandbox": Decimal("0.036"),
            }
        )
        self.workspace = workspace
        self._patch(
            "agentix.cost.modal.Workspace.from_context"
        ).return_value = workspace
        self.app_lookup.aio = AsyncMock(
            side_effect=lambda name, **_: Mock(app_id=f"ap-{name}")
        )

    async def _infos(self, app_id, since):
        self.assertEqual(app_id, "ap-agentix")
        self.assertEqual(since, START.timestamp())
        return list(self.infos)

    def collect(self):
        return asyncio.run(cost.collect(NOW))

    def test_reads_listing_records_billing_and_rates(self):
        begun = dt.datetime(2026, 11, 15, 11, tzinfo=dt.UTC).timestamp()
        self.infos = [
            info("sb-1", "dev", begun, begun + 3600),
            info("sb-b", "", begun, begun + 600, 8000, 16384),
        ]
        self.write_record(
            record(
                "kept",
                ("sb-k", "2026-11-14T00:00:00+00:00"),
                snapshots=[("im-k", "sb-k", "down", "2026-11-14T00:05:00+00:00")],
            )
        )
        self.report_items = [
            SimpleNamespace(
                object_id="ap-agentix",
                description="agentix",
                interval_start=START,
                cost_by_resource={"CPU": Decimal("1")},
            ),
            SimpleNamespace(
                object_id="ap-other",
                description="someone-else",
                interval_start=START,
                cost_by_resource={"CPU": Decimal("9")},
            ),
        ]
        files = dict(self.volume.files)
        summary = self.collect()
        found = rows(summary)
        self.assertEqual(found["dev"]["seconds"], 3600)
        self.assertEqual(found["dev"]["cpu"], "0.0180")  # 0.125 core * $0.144 per hour
        self.assertEqual(found["kept"]["source"], "estimate: record 1")
        self.assertEqual(found["kept"]["snapshots"], 1)
        self.assertEqual(found[cost.IMAGE_BUILDS]["sandboxes"], 1)
        # Only the apps of agentix, not the rest of the workspace.
        self.assertEqual(list(summary["billed"]["apps"]), ["agentix"])
        self.assertEqual(summary["billed"]["total"], "1.0000")
        self.assertIn("Workspace.billing.rates", summary["prices"]["source"])
        # Whole days daily, today hourly: an hourly report spans 7 days at most.
        calls = self.workspace.billing.report.aio.call_args_list
        self.assertEqual(
            [
                (c.kwargs["start"], c.kwargs["end"], c.kwargs["resolution"])
                for c in calls
            ],
            [
                (START, dt.datetime(2026, 11, 15, tzinfo=dt.UTC), "d"),
                (dt.datetime(2026, 11, 15, tzinfo=dt.UTC), NOW, "h"),
            ],
        )
        # Nothing was written or created.
        self.assertEqual(self.volume.files, files)
        self.volume.batch_upload.aio.assert_not_called()
        self.volume.remove_file.aio.assert_not_called()
        self.cache.put.aio.assert_not_called()
        self.create.aio.assert_not_called()
        for lookup in (self.app_lookup.aio, self.volume_lookup, self.dict_lookup):
            for call in lookup.call_args_list:
                self.assertNotIn("create_if_missing", call.kwargs)

    def test_first_day_of_the_month_needs_only_the_hourly_report(self):
        asyncio.run(cost.collect(dt.datetime(2026, 11, 1, 5, tzinfo=dt.UTC)))
        calls = self.workspace.billing.report.aio.call_args_list
        self.assertEqual([c.kwargs["resolution"] for c in calls], ["h"])

    def test_cached_records_are_used_without_reading_files(self):
        path = self.write_record(record("dev", ("sb-1", "2026-11-14T00:00:00+00:00")))
        cached = record("dev", ("sb-1", "2026-11-14T00:00:00+00:00"))
        cached["options"]["timeout"] = 60
        self.cache.data["dev"] = {path: cached}
        self.assertEqual(rows(self.collect())["dev"]["seconds"], 60)
        self.volume.read_file.aio.assert_not_called()

    def test_failures_fall_back_and_say_so(self):
        self.list_sandboxes.side_effect = RuntimeError("no")
        self.workspace.billing.report.aio.side_effect = modal.exception.InvalidError(
            "plan"
        )
        self.workspace.billing.rates.aio.side_effect = RuntimeError("no")
        self.write_record(record("dev", ("sb-1", "2026-11-14T00:00:00+00:00")))
        summary = self.collect()
        self.assertIn("failed (RuntimeError)", summary["sources"]["listing"])
        self.assertIsNone(summary["billed"])
        # The type only: Modal's message can name the workspace.
        self.assertEqual(
            summary["sources"]["billing"],
            "Modal's billing report failed (InvalidError)",
        )
        self.assertIn("checked 2026-11-15", summary["prices"]["source"])
        self.assertEqual(
            summary["prices"]["cpu_core_second"],
            f"{cost.PRICES['cpu_core_second']:.8g}",
        )
        self.assertEqual(rows(summary)["dev"]["source"], "estimate: record-timeout 1")

    def test_records_that_cannot_be_read_leave_the_rest(self):
        begun = dt.datetime(2026, 11, 15, 11, tzinfo=dt.UTC).timestamp()
        self.infos = [info("sb-1", "dev", begun, begun + 60)]
        self.volume.listdir.aio.side_effect = RuntimeError("transport")
        summary = self.collect()
        self.assertIn(
            "agentix records were unavailable (RuntimeError)",
            summary["sources"]["records"],
        )
        self.assertIn("records were unavailable", cost.render(summary))
        self.assertEqual(rows(summary)["dev"]["seconds"], 60)

    def test_a_corrupt_record_file_leaves_the_rest(self):
        self.volume.files["records/bad/x.json"] = b"{not json"
        summary = self.collect()
        self.assertIn("unavailable (JSONDecodeError)", summary["sources"]["records"])

    def test_a_listing_without_end_times_is_not_used(self):
        begun = dt.datetime(2026, 11, 15, 11, tzinfo=dt.UTC).timestamp()
        self.infos = [
            info("sb-1", "", begun, 0.0, ended=True),
            info("sb-2", "", begun, 0.0, ended=True),
        ]
        summary = self.collect()
        self.assertEqual(summary["rows"], [])
        self.assertIn("gave no end times", summary["sources"]["listing"])

    def test_billing_rows_of_an_earlier_app_of_the_name_are_kept(self):
        self.report_items = [
            SimpleNamespace(
                object_id="ap-before",
                description="agentix",
                interval_start=START,
                cost_by_resource={"CPU": Decimal("1")},
            )
        ]
        summary = self.collect()
        self.assertEqual(summary["billed"]["apps"]["agentix"]["total"], "1.0000")

    def test_no_app_and_no_records(self):
        self.app_lookup.aio = AsyncMock(
            side_effect=modal.exception.NotFoundError("none")
        )
        summary = self.collect()
        self.assertEqual(summary["rows"], [])
        self.assertEqual(summary["billed"]["total"], "0.0000")
        self.list_sandboxes.assert_not_called()

    def test_legacy_records_are_read(self):
        self.legacy.hydrate.aio.side_effect = None
        self.legacy.data["old"] = record("old", ("sb-o", "2026-11-14T00:00:00+00:00"))
        self.assertIn("old", rows(self.collect()))
        self.legacy.put.aio.assert_not_called()


class CommandTest(ModalTestCase):
    def setUp(self):
        super().setUp()
        self.summary = summarize(
            [listed("sb-1", "dev", "2026-11-15T10:00:00+00:00", None)]
        )
        backend = self._patch("agentix.__main__._backend")
        backend.return_value.cost.return_value = self.summary

    def test_prints_the_table(self):
        output = self.run_cli("cost")
        self.assertEqual(output.strip(), cost.render(self.summary))

    def test_json(self):
        self.assertEqual(json.loads(self.run_cli("cost", "--json")), self.summary)

    def test_refuses_values_and_arguments(self):
        with self.assertRaises(InvalidRequest):
            self.run_cli("cost", "--json=no")
        with self.assertRaises(InvalidRequest):
            self.run_cli("cost", "march")


class ListingTest(ModalTestCase):
    def test_listed_item_from_modal(self):
        item = cost._listed_item(info("sb-1", "dev", 1000.0, 1600.0, 8000, 16384))
        self.assertEqual(
            (item["name"], item["cpu"], item["memory_mib"], item["source"]),
            ("dev", 8.0, 16384, "modal"),
        )
        self.assertEqual((item["end"] - item["start"]).total_seconds(), 600)

    def test_a_running_sandbox_ends_at_its_timeout_at_the_latest(self):
        item = cost._listed_item(info("sb-1", "", 1000.0, 0.0, timeout=600))
        self.assertEqual((item["end"] - item["start"]).total_seconds(), 600)
        self.assertEqual(item["source"], "modal")
        no_timeout = cost._listed_item(info("sb-1", "", 1000.0, 0.0, timeout=0))
        self.assertEqual(no_timeout["end"].year, dt.MAXYEAR)

    def test_a_finish_past_the_timeout_is_capped(self):
        item = cost._listed_item(info("sb-1", "", 1000.0, 9000.0, timeout=600))
        self.assertEqual((item["end"] - item["start"]).total_seconds(), 600)

    def test_ended_without_an_end_time_is_an_upper_bound(self):
        item = cost._listed_item(info("sb-1", "", 1000.0, 0.0, timeout=600, ended=True))
        self.assertEqual((item["end"] - item["start"]).total_seconds(), 600)
        self.assertEqual(item["source"], "modal-upper")

    def test_ended_before_it_started_costs_nothing(self):
        item = cost._listed_item(info("sb-1", "", 0.0, 0.0, ended=True, created=1000.0))
        self.assertEqual(item["end"], item["start"])

    def test_has_result_reads_the_proto(self):
        from modal_proto import api_pb2

        task = api_pb2.TaskInfo()
        self.assertFalse(cost._has_result(task))
        task.result.status = 1
        self.assertTrue(cost._has_result(task))

    def list_finished(self, pages, since=0.0):
        client = Mock()
        client.stub.SandboxList = AsyncMock(side_effect=pages)
        with patch("modal.client._Client.from_env", AsyncMock(return_value=client)):
            found = asyncio.run(cost._list_finished("ap-1", since))
        requests = [c.args[0] for c in client.stub.SandboxList.call_args_list]
        return found, requests

    def page(self, *items):
        return SimpleNamespace(
            sandboxes=[SimpleNamespace(id=i, created_at=t) for i, t in items]
        )

    def test_list_finished_pages_until_empty(self):
        found, requests = self.list_finished(
            [self.page(("sb-2", 5.0), ("sb-1", 3.0)), self.page()]
        )
        self.assertEqual([f.id for f in found], ["sb-2", "sb-1"])
        self.assertTrue(all(r.include_finished for r in requests))
        self.assertIsNone(requests[0].before_timestamp or None)
        # Just after the oldest, so that one created at the same time is kept.
        self.assertAlmostEqual(requests[1].before_timestamp, 3.001, places=5)

    def test_sandboxes_created_at_the_same_time_are_not_skipped(self):
        found, _ = self.list_finished(
            [
                self.page(("sb-3", 5.0), ("sb-2", 3.0)),
                self.page(("sb-2", 3.0), ("sb-1", 3.0)),
                self.page(("sb-2", 3.0), ("sb-1", 3.0)),
            ]
        )
        self.assertEqual(sorted(f.id for f in found), ["sb-1", "sb-2", "sb-3"])

    def test_a_listing_that_ignores_the_page_stops(self):
        # Every page the same: the second adds nothing.
        page = self.page(("sb-2", 5.0), ("sb-1", 3.0))
        found, requests = self.list_finished([page] * 10)
        self.assertEqual(len(found), 2)
        self.assertEqual(len(requests), 2)

    def test_times_that_do_not_decrease_stop_the_listing(self):
        found, requests = self.list_finished(
            [self.page(("sb-2", 5.0)), self.page(("sb-1", 9.0)), self.page()]
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(found), 2)

    def test_listing_stops_once_past_the_month(self):
        found, requests = self.list_finished(
            [self.page(("sb-2", 50.0), ("sb-1", 5.0)), self.page(("sb-0", 1.0))],
            since=10.0,
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(found), 2)

    def test_listing_stops_after_the_page_cap(self):
        pages = [self.page((f"sb-{n}", 1000.0 - n)) for n in range(200)]
        with patch.object(cost, "MAX_PAGES", 5):
            found, requests = self.list_finished(pages)
        self.assertEqual((len(found), len(requests)), (5, 5))
