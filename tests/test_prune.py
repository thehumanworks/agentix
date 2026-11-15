import contextlib
import datetime as dt
import io
import unittest
from unittest.mock import AsyncMock, patch

import modal
from support import ModalTestCase, fake_sandbox
from test_lifecycle import OPTIONS

from agentix import durations, lifecycle, store
from agentix.errors import InvalidRequest, OperationFailed


def days_ago(days: float) -> str:
    when = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    return when.isoformat(timespec="seconds")


class PruneTestCase(ModalTestCase):
    def put(self, name, sandbox_id, snaps=(), started=40, exit="saved"):
        """A stopped name whose sandbox started `started` days ago."""
        record = store.new_record(name, dict(OPTIONS))
        record["created_at"] = days_ago(started)
        store.add_sandbox(record, sandbox_id)
        record["sandboxes"][-1]["started_at"] = days_ago(started)
        record["sandboxes"][-1]["exit"] = exit
        for snapshot_id, age in snaps:
            store.add_snapshot(record, snapshot_id, sandbox_id, "down", days_ago(age))
        self.write_record(record)
        return record

    def prune(self, *flags, tty=False, answer=""):
        stdin = io.StringIO(answer)
        stdin.isatty = lambda: tty
        errors = io.StringIO()
        with patch("sys.stdin", stdin), contextlib.redirect_stderr(errors):
            output = self.run_cli("sandbox", "prune", *flags)
        return output, errors.getvalue()

    def deleted(self):
        return sorted(call.args[0] for call in self.image_delete.aio.call_args_list)

    def snapshot_ids(self, name):
        record = self.stored(name)
        return None if record is None else [s["id"] for s in record["snapshots"]]


class ListingTests(PruneTestCase):
    def test_default_is_a_dry_listing_with_totals(self):
        self.put("old", "sb-old", snaps=[("im-1", 20), ("im-2", 16)], started=25)
        self.put("new", "sb-new", snaps=[("im-3", 1)], started=2)
        output, errors = self.prune("--older-than=14d")
        lines = output.splitlines()
        self.assertEqual(lines[0].split(), ["NAME", "KIND", "ID", "AGE", "DATE"])
        self.assertIn("im-1", lines[1])
        self.assertIn("snapshot (down)", lines[1])
        self.assertIn("20d 0h", lines[1])
        self.assertIn("im-2", lines[2])
        self.assertIn("stopped sandbox", lines[3])
        self.assertIn("sb-old", lines[3])
        self.assertIn(
            "Total: 3 items: 2 snapshots and 1 stopped sandbox, across 1 name",
            output,
        )
        self.assertIn("does not report snapshot sizes", output)
        self.assertNotIn("im-3", output)
        self.assertIn("Nothing was deleted", errors)
        self.assertIn("--yes", errors)
        self.image_delete.aio.assert_not_called()
        self.assertEqual(self.snapshot_ids("old"), ["im-1", "im-2"])

    def test_nothing_old_enough(self):
        self.put("new", "sb-new", snaps=[("im-3", 1)], started=2)
        output, _ = self.prune("--older-than=14d", "--yes")
        self.assertIn("Nothing is old enough to prune.", output)
        self.image_delete.aio.assert_not_called()

    def test_expired_snapshot_is_marked(self):
        self.put("old", "sb-old", snaps=[("im-1", 35)], started=40)
        output, _ = self.prune("--older-than=14d")
        self.assertIn("snapshot (down), expired", output)


class ConfirmationTests(PruneTestCase):
    def setUp(self):
        super().setUp()
        self.put("old", "sb-old", snaps=[("im-1", 20), ("im-2", 3)], started=25)
        self.put("gone", "sb-gone", snaps=[("im-9", 30)], started=31)

    def test_yes_deletes_old_snapshots_and_names_left_empty(self):
        output, _ = self.prune("--older-than=14d", "--yes")
        self.assertEqual(self.deleted(), ["im-1", "im-9"])
        self.assertEqual(self.snapshot_ids("old"), ["im-2"])
        self.assertIsNone(self.stored("gone"))
        self.assertIn("Deleted 2 snapshots and removed 1 stopped sandbox.", output)

    def test_typed_yes_on_a_terminal_deletes(self):
        _, errors = self.prune("--older-than=14d", tty=True, answer="yes\n")
        self.assertIn("Type yes to delete", errors)
        self.assertEqual(self.deleted(), ["im-1", "im-9"])

    def test_any_other_answer_deletes_nothing(self):
        for answer in ("no\n", "\n", ""):
            with self.subTest(answer=answer):
                _, errors = self.prune("--older-than=14d", tty=True, answer=answer)
                self.assertIn("Nothing was deleted", errors)
        self.image_delete.aio.assert_not_called()
        self.assertIsNotNone(self.stored("gone"))

    def test_no_terminal_and_no_yes_deletes_nothing(self):
        self.prune("--older-than=14d", tty=False, answer="yes\n")
        self.image_delete.aio.assert_not_called()

    def test_keep_latest_keeps_the_newest_snapshot_of_each_name(self):
        self.put("many", "sb-many", snaps=[("im-a", 20), ("im-b", 18)], started=21)
        self.prune("--older-than=14d", "--keep-latest", "--yes")
        self.assertEqual(self.deleted(), ["im-1", "im-a"])
        self.assertEqual(self.snapshot_ids("many"), ["im-b"])
        self.assertEqual(self.snapshot_ids("gone"), ["im-9"])
        self.assertEqual(self.snapshot_ids("old"), ["im-2"])

    def test_name_filter_limits_what_is_pruned(self):
        output, _ = self.prune("--older-than=14d", "--name=go*", "--yes")
        self.assertNotIn("im-1", output)
        self.assertEqual(self.deleted(), ["im-9"])
        self.assertEqual(self.snapshot_ids("old"), ["im-1", "im-2"])

    def test_failed_delete_stays_recorded(self):
        async def delete(snapshot_id):
            if snapshot_id == "im-9":
                raise ConnectionError("no network")

        self.image_delete.aio.side_effect = delete
        with self.assertRaisesRegex(OperationFailed, "im-9"):
            self.prune("--older-than=14d", "--yes")
        self.assertEqual(self.snapshot_ids("gone"), ["im-9"])
        self.assertEqual(self.snapshot_ids("old"), ["im-2"])


class SafetyTests(PruneTestCase):
    def test_running_sandbox_is_never_touched(self):
        # The default listing has sb-test running.
        self.put("live", "sb-test", snaps=[("im-1", 20)], exit="pending")
        output, _ = self.prune("--older-than=1d", "--yes")
        self.assertIn("Skipped live: running.", output)
        self.image_delete.aio.assert_not_called()
        self.assertEqual(self.snapshot_ids("live"), ["im-1"])
        self.lookup.aio.assert_not_called()

    def test_snapshot_still_saving_skips_the_name(self):
        self.put("saving", "sb-saving", snaps=[("im-1", 20)], exit="pending")
        ended = fake_sandbox("sb-saving")
        ended.poll.aio.return_value = 137
        ended._experimental_get_exit_snapshot.aio.side_effect = (
            modal.exception.TimeoutError("still saving")
        )
        self.lookup.aio = AsyncMock(return_value=ended)
        output, _ = self.prune("--older-than=1d", "--yes")
        self.assertIn("Skipped saving: Modal is still saving", output)
        self.image_delete.aio.assert_not_called()

    def test_name_that_starts_again_after_the_listing_is_skipped(self):
        self.put("old", "sb-old", snaps=[("im-1", 20)], started=25)

        def confirm(plan):
            # Someone restores the name while the listing is read.
            record = self.stored("old")
            store.add_sandbox(record, "sb-again", restored_from="im-1")
            self.write_record(record, "00000000T000001-again.json")
            return True

        outcome = self.run_core(lifecycle.prune, dt.timedelta(days=14), confirm=confirm)
        self.assertEqual(outcome["result"]["skipped"][0]["name"], "old")
        self.image_delete.aio.assert_not_called()
        self.assertEqual(self.snapshot_ids("old"), ["im-1"])

    def test_without_confirm_nothing_is_deleted(self):
        self.put("old", "sb-old", snaps=[("im-1", 20)], started=25)
        outcome = self.run_core(lifecycle.prune, dt.timedelta(days=14))
        self.assertIsNone(outcome["result"])
        self.assertEqual([item["id"] for item in outcome["items"]], ["im-1", "sb-old"])
        self.image_delete.aio.assert_not_called()

    def test_unparseable_older_than_is_refused_before_modal(self):
        for value in ("14", "abc", "14 days", "0d", "-3d", "1.5d"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(InvalidRequest, "--older-than"):
                    self.prune(f"--older-than={value}", "--yes")
        with self.assertRaisesRegex(InvalidRequest, "Pass --older-than"):
            self.prune("--yes")
        self.volume_lookup.assert_not_called()
        self.image_delete.aio.assert_not_called()


class DurationTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(durations.parse("14d"), dt.timedelta(days=14))
        self.assertEqual(durations.parse("12h"), dt.timedelta(hours=12))
        self.assertEqual(durations.parse("30m"), dt.timedelta(minutes=30))
        self.assertEqual(durations.parse("2w"), dt.timedelta(weeks=2))
        self.assertEqual(durations.parse("1d12h"), dt.timedelta(hours=36))
        self.assertEqual(durations.parse("45S"), dt.timedelta(seconds=45))

    def test_describe(self):
        self.assertEqual(durations.describe(dt.timedelta(days=16, hours=3)), "16d 3h")
        self.assertEqual(durations.describe(dt.timedelta(hours=5, minutes=2)), "5h 2m")
        self.assertEqual(durations.describe(dt.timedelta(seconds=42)), "42s")


if __name__ == "__main__":
    unittest.main()
