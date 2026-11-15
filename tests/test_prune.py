import contextlib
import datetime as dt
import io
import unittest
from unittest.mock import AsyncMock, patch

import modal
from support import ModalTestCase, _each, fake_sandbox
from test_lifecycle import OPTIONS

from agentix import durations, lifecycle, store
from agentix.errors import InvalidRequest, OperationFailed
from agentix.sandbox import NAME_TAG


def days_ago(days: float) -> str:
    when = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    return when.isoformat(timespec="seconds")


class PruneTestCase(ModalTestCase):
    def setUp(self):
        super().setUp()
        # Running sandboxes by the name they carry. sb-test runs without one.
        self.named = {}

        def listing(app_id=None, tags=None):
            if tags:
                return _each(self.named.get(tags[NAME_TAG], []))
            return _each([self.sandbox, *(h for hs in self.named.values() for h in hs)])

        self.listing.aio = unittest.mock.Mock(side_effect=listing)

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

    def test_flags_with_separate_values(self):
        self.prune("--older-than", "14d", "--name", "gone", "--yes")
        self.assertEqual(self.deleted(), ["im-9"])

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


class FlagTests(PruneTestCase):
    def setUp(self):
        super().setUp()
        self.put("old", "sb-old", snaps=[("im-1", 20)], started=25)

    def test_yes_with_a_value_is_refused(self):
        for flag in ("--yes=false", "--yes=no", "--yes=0", "--keep-latest=no"):
            with self.subTest(flag=flag):
                with self.assertRaisesRegex(InvalidRequest, "takes no value"):
                    self.prune("--older-than=14d", flag, tty=True, answer="no\n")
        self.image_delete.aio.assert_not_called()
        self.assertEqual(self.snapshot_ids("old"), ["im-1"])

    def test_positional_arguments_are_refused(self):
        for args in (["14d", "x", "y", "z"], ["--older-than=14d", "z"]):
            with self.subTest(args=args):
                with self.assertRaisesRegex(InvalidRequest, "only flags"):
                    self.prune(*args)
        self.image_delete.aio.assert_not_called()
        self.volume_lookup.assert_not_called()

    def test_name_must_be_text(self):
        for flag in ("--name", "--name=123"):
            with self.subTest(flag=flag):
                with self.assertRaisesRegex(InvalidRequest, "--name"):
                    self.prune("--older-than=14d", flag, "--yes")
        self.image_delete.aio.assert_not_called()

    def test_huge_ages_are_refused_cleanly(self):
        for value in ("3000000d", "3651d", "9" * 5000 + "d"):
            with self.subTest(value=value[:10]):
                with self.assertRaisesRegex(InvalidRequest, "--older-than"):
                    self.prune(f"--older-than={value}", "--yes")
        self.assertEqual(durations.parse("3650d"), durations.MAXIMUM)


class RaceTests(PruneTestCase):
    def setUp(self):
        super().setUp()
        self.put("old", "sb-old", snaps=[("im-1", 20)], started=25)

    def restore_meanwhile(self, running):
        """A restore lands once prune has checked Modal for the name."""
        calls = []

        def listing(app_id=None, tags=None):
            if not tags:
                return _each([self.sandbox])
            calls.append(tags)
            if len(calls) == 2:
                record = self.stored("old")
                store.add_sandbox(record, "sb-again", restored_from="im-1")
                self.write_record(record, "00000000T000001-again.json")
                return _each([fake_sandbox("sb-again")] if running else [])
            return _each([])

        self.listing.aio = unittest.mock.Mock(side_effect=listing)

    def test_restore_recorded_before_the_purge_keeps_the_name(self):
        self.restore_meanwhile(running=False)
        output, _ = self.prune("--older-than=14d", "--yes")
        self.assertIn("Skipped old: old has new sandboxes (sb-again)", output)
        record = self.stored("old")
        self.assertEqual(record["sandbox_id"], "sb-again")
        self.assertEqual(record["snapshots"], [])
        self.assertNotIn("sb-again", record["removed"])

    def test_sandbox_running_under_the_name_keeps_the_name(self):
        self.restore_meanwhile(running=True)
        output, _ = self.prune("--older-than=14d", "--yes")
        self.assertIn("Skipped old: running as sb-again", output)
        self.assertEqual(self.stored("old")["sandbox_id"], "sb-again")

    def test_running_before_any_delete_skips_the_name(self):
        self.named["old"] = [fake_sandbox("sb-again")]
        output, _ = self.prune("--older-than=14d", "--yes")
        self.assertIn("Skipped old: running as sb-again", output)
        self.image_delete.aio.assert_not_called()

    def test_store_remove_refuses_an_unplanned_sandbox(self):
        record = self.stored("old")
        store.add_sandbox(record, "sb-again")
        self.write_record(record, "00000000T000001-again.json")
        with self.assertRaisesRegex(OperationFailed, "sb-again"):
            self.run_core(store.remove, "old", ["im-1"], ["sb-old"])
        self.assertIsNotNone(self.stored("old"))


class FailureTests(PruneTestCase):
    def test_one_failing_name_does_not_stop_the_others(self):
        self.put("bad", "sb-bad", snaps=[("im-b", 20)], started=25)
        self.put("good", "sb-good", snaps=[("im-g", 20), ("im-n", 1)], started=25)
        find = store.find

        async def failing(session, name):
            if name == "bad":
                raise ConnectionError("no network")
            return await find(session, name)

        with patch("agentix.lifecycle.store.find", side_effect=failing):
            with self.assertRaisesRegex(OperationFailed, "bad: no network"):
                self.prune("--older-than=14d", "--yes")
        self.assertEqual(self.deleted(), ["im-g"])
        self.assertEqual(self.snapshot_ids("good"), ["im-n"])
        self.assertEqual(self.snapshot_ids("bad"), ["im-b"])

    def test_record_update_that_fails_is_reported(self):
        self.put("good", "sb-good", snaps=[("im-g", 20), ("im-n", 1)], started=25)
        with patch(
            "agentix.lifecycle.store.update", side_effect=ConnectionError("down")
        ):
            with self.assertRaisesRegex(OperationFailed, "still lists deleted.*im-g"):
                self.prune("--older-than=14d", "--yes")


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
