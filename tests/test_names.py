import datetime as dt
import unittest

from agentix import names, store
from agentix.errors import InvalidRequest

OPTIONS: store.Options = {
    "image_name": "determinate-nix:latest",
    "secret_name": "",
    "timeout": 600,
    "workdir": "",
    "memory": 8192,
    "cpu": 0,
}


class NameTests(unittest.TestCase):
    def test_lowercase_dns_labels_are_names(self):
        for name in ("dev", "brisk-otter", "a1", "x" * 63):
            with self.subTest(name=name):
                self.assertEqual(names.check_name(name), name)

    def test_other_names_are_refused(self):
        for name in ("", "Dev", "-dev", "1dev", "dev_box", "dev.box", "x" * 64):
            with self.subTest(name=name), self.assertRaises(InvalidRequest):
                names.check_name(name)

    def test_names_cannot_look_like_sandbox_ids(self):
        self.assertTrue(names.is_sandbox_id("sb-01M7E2QYVQSTRC7J9YK9CBSQ5C"))
        self.assertFalse(names.is_sandbox_id("brisk-otter"))
        with self.assertRaisesRegex(InvalidRequest, "sb-"):
            names.check_name("sb-dev")

    def test_generated_names_are_valid(self):
        for _ in range(50):
            name = names.generate()
            self.assertEqual(names.check_name(name), name)


class RecordExitTests(unittest.TestCase):
    def setUp(self):
        self.record = store.new_record("dev", dict(OPTIONS))
        store.add_sandbox(self.record, "sb-1")
        self.sandbox = self.record["sandboxes"][0]

    def test_snapshot_taken_by_this_command_is_from_now(self):
        changed = store.record_exit(
            self.record, self.sandbox, "saved", "im-1", 137, stopped_now=True
        )
        self.assertTrue(changed)
        [snapshot] = self.record["snapshots"]
        self.assertEqual(
            (snapshot["id"], snapshot["sandbox_id"], snapshot["kind"]),
            ("im-1", "sb-1", "down"),
        )
        self.assertEqual(self.sandbox["exit"], "saved")

    def test_timeout_snapshot_is_dated_at_the_deadline(self):
        started = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
        self.sandbox["started_at"] = started.isoformat(timespec="seconds")
        store.record_exit(self.record, self.sandbox, "saved", "im-1", 124)
        [snapshot] = self.record["snapshots"]
        self.assertEqual(snapshot["kind"], "timeout")
        self.assertEqual(snapshot["created_at"], "2026-01-01T12:10:00+00:00")

    def test_timeout_deadline_is_never_in_the_future(self):
        self.record["options"]["timeout"] = 86400
        store.record_exit(self.record, self.sandbox, "saved", "im-1", 124)
        self.assertLessEqual(self.record["snapshots"][0]["created_at"], store.now())

    def test_other_ends_are_dated_when_first_seen(self):
        store.record_exit(self.record, self.sandbox, "saved", "im-1", 137)
        self.assertEqual(self.record["snapshots"][0]["kind"], "exit")

    def test_missing_snapshot_is_recorded_once(self):
        self.assertTrue(store.record_exit(self.record, self.sandbox, "none"))
        self.assertEqual(self.sandbox["exit"], "none")
        self.assertEqual(store.pending(self.record), [])
        self.assertEqual(self.record["snapshots"], [])

    def test_running_or_pending_changes_nothing(self):
        for status in ("running", "pending"):
            self.assertFalse(store.record_exit(self.record, self.sandbox, status))
        self.assertEqual(store.pending(self.record), [self.sandbox])

    def test_snapshots_are_ordered_by_time(self):
        store.add_snapshot(self.record, "im-new", "sb-1", "manual", "2026-02-01")
        store.add_snapshot(self.record, "im-old", "sb-1", "manual", "2026-01-01")
        self.assertEqual(
            [snapshot["id"] for snapshot in store.ordered(self.record)],
            ["im-old", "im-new"],
        )


if __name__ == "__main__":
    unittest.main()
