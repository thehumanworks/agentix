import asyncio
import copy
import itertools
import json
import unittest

from support import ModalTestCase

from agentix import lifecycle, store
from agentix.errors import InvalidRequest, NotFound, OperationFailed
from agentix.session import Session

OPTIONS: store.Options = {
    "image_name": "determinate-nix:latest",
    "secret_name": "",
    "timeout": 600,
    "workdir": "",
    "memory": 8192,
    "cpu": 0,
}


def record(name="dev", sandboxes=(), snapshots=(), created_at="2026-01-01T00:00:00"):
    """A record with sandboxes given as (id, exit) and snapshots as (id, sandbox)."""
    result = store.new_record(name, dict(OPTIONS))
    result["created_at"] = created_at
    for number, (sandbox_id, exit) in enumerate(sandboxes):
        store.add_sandbox(result, sandbox_id)
        result["sandboxes"][-1]["started_at"] = f"2026-01-0{number + 1}T00:00:00"
        result["sandboxes"][-1]["exit"] = exit
    for number, (snapshot_id, sandbox_id) in enumerate(snapshots):
        store.add_snapshot(
            result, snapshot_id, sandbox_id, "down", f"2026-02-0{number + 1}T00:00:00"
        )
    return result


def legacy(name="dev", **kwargs):
    """A record as an older version kept it in the Dict."""
    result = record(name, **kwargs)
    del result["removed"]
    return result


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.base = record(sandboxes=[("sb-1", "saved")], snapshots=[("im-1", "sb-1")])
        self.restored = copy.deepcopy(self.base)
        store.add_sandbox(self.restored, "sb-2", restored_from="im-1")
        self.snapped = copy.deepcopy(self.base)
        store.add_snapshot(
            self.snapped, "im-m", "sb-1", "manual", "2026-02-05T00:00:00"
        )
        self.purged = copy.deepcopy(self.base)
        store.remove_snapshots(self.purged, ["im-1"])

    def test_concurrent_changes_are_both_kept(self):
        merged = store.merge(self.restored, self.snapped)
        self.assertEqual([s["id"] for s in merged["sandboxes"]], ["sb-1", "sb-2"])
        self.assertEqual([s["id"] for s in merged["snapshots"]], ["im-1", "im-m"])
        self.assertEqual(merged["sandbox_id"], "sb-2")

    def test_order_and_repetition_do_not_matter(self):
        copies = [self.base, self.restored, self.snapped, self.purged]
        results = set()
        for order in itertools.permutations(copies):
            merged = None
            for item in [*order, order[0]]:
                merged = store.merge(merged, item)
            results.add(json.dumps(merged, sort_keys=True))
        self.assertEqual(len(results), 1)

    def test_a_recorded_exit_wins_over_pending(self):
        pending = record(sandboxes=[("sb-1", "pending")])
        merged = store.merge(pending, self.base)
        self.assertEqual(merged["sandboxes"][0]["exit"], "saved")
        self.assertEqual(store.merge(self.base, pending), merged)

    def test_a_removed_snapshot_stays_removed(self):
        merged = store.merge(self.purged, self.snapped)
        self.assertEqual([s["id"] for s in merged["snapshots"]], ["im-m"])
        self.assertEqual(merged["removed"], ["im-1"])

    def test_a_name_taken_again_keeps_its_new_options(self):
        new = record(sandboxes=[("sb-9", "pending")], created_at="2026-03-01T00:00:00")
        new["options"]["memory"] = 4096
        merged = store.merge(self.base, new)
        self.assertEqual(merged["options"]["memory"], 4096)
        self.assertEqual(merged["sandbox_id"], "sb-9")

    def test_an_older_record_gains_what_newer_ones_have(self):
        merged = store.merge(None, legacy(sandboxes=[("sb-1", "saved")]))
        self.assertEqual(merged["removed"], [])

    def test_expiry_is_thirty_days_after_the_snapshot(self):
        snapshot = self.base["snapshots"][0]
        self.assertEqual(store.expires_at(snapshot), "2026-03-03T00:00:00+00:00")


class StoreTestCase(ModalTestCase):
    def run_all(self, *operations):
        """Run operations at once in one session, as concurrent commands would."""

        async def run():
            session = Session()
            return await asyncio.gather(
                *(operation(session) for operation in operations)
            )

        return asyncio.run(run())


class ConcurrencyTests(StoreTestCase):
    def test_two_commands_that_change_a_record_at_once_lose_nothing(self):
        self.write_record(record(sandboxes=[("sb-1", "pending")]))

        def add(snapshot_id):
            return lambda session: store.update(
                session,
                "dev",
                lambda latest: store.add_snapshot(
                    latest, snapshot_id, "sb-1", "manual"
                ),
            )

        self.run_all(add("im-a"), add("im-b"))
        # Each wrote its own file and deleted only the one it read.
        self.assertEqual(len(self.files_of("dev")), 2)
        self.assertEqual(
            sorted(s["id"] for s in self.stored("dev")["snapshots"]), ["im-a", "im-b"]
        )
        # The next change writes one file with both.
        self.run_core(store.update, "dev", lambda latest: None)
        [path] = self.files_of("dev")
        on_disk = json.loads(self.volume.files[path])
        self.assertEqual(
            sorted(s["id"] for s in on_disk["snapshots"]), ["im-a", "im-b"]
        )

    def test_a_file_merged_away_during_a_read_is_read_again(self):
        old = self.write_record(record(sandboxes=[("sb-1", "saved")]), "a.json")
        newer = record(sandboxes=[("sb-1", "saved"), ("sb-2", "pending")])
        listdir = self.volume.listdir.aio.side_effect

        async def merged_meanwhile(path, recursive=False):
            entries = await listdir(path, recursive)
            if old in self.volume.files:
                # Another command merges the file into a newer one.
                del self.volume.files[old]
                self.write_record(newer, "b.json")
            return entries

        self.volume.listdir.aio.side_effect = merged_meanwhile
        found = self.run_core(store.load, "dev")
        self.assertEqual(found["sandbox_id"], "sb-2")


class CacheTests(StoreTestCase):
    def test_a_current_copy_saves_reading_the_files(self):
        self.write_record(record(sandboxes=[("sb-1", "saved")]))
        self.run_core(store.load, "dev")
        self.assertEqual(self.volume.read_file.aio.call_count, 1)
        self.run_core(store.load, "dev")
        self.run_core(store.records)
        self.assertEqual(self.volume.read_file.aio.call_count, 1)

    def test_a_write_by_another_command_is_noticed(self):
        self.write_record(record(sandboxes=[("sb-1", "saved")]))
        self.run_core(store.load, "dev")
        # Another command, whose copy of the cache is lost, writes a new file.
        self.write_record(
            record(sandboxes=[("sb-1", "saved"), ("sb-2", "pending")]), "b.json"
        )
        self.assertEqual(self.run_core(store.load, "dev")["sandbox_id"], "sb-2")
        [row] = self.run_core(store.records)
        self.assertEqual(row["sandbox_id"], "sb-2")

    def test_an_expired_copy_costs_only_a_read(self):
        self.write_record(record(sandboxes=[("sb-1", "saved")]))
        self.run_core(store.load, "dev")
        self.cache.data.clear()
        self.assertEqual(self.run_core(store.load, "dev")["sandbox_id"], "sb-1")
        self.assertIn("dev", self.cache.data)

    def test_a_failing_cache_only_slows_reads(self):
        self.write_record(record(sandboxes=[("sb-1", "saved")]))
        self.cache.get.aio.side_effect = ConnectionError("no cache")
        self.cache.items.aio.side_effect = ConnectionError("no cache")
        self.cache.put.aio.side_effect = ConnectionError("no cache")
        self.assertEqual(self.run_core(store.load, "dev")["sandbox_id"], "sb-1")
        self.assertEqual(len(self.run_core(store.records)), 1)
        self.run_core(
            store.update,
            "dev",
            lambda r: store.add_snapshot(r, "im-1", "sb-1", "manual"),
        )
        self.assertEqual(self.stored("dev")["snapshots"][0]["id"], "im-1")

    def test_an_empty_volume_has_no_records(self):
        self.without_legacy()
        self.assertEqual(self.run_core(store.records), [])
        self.assertIsNone(self.run_core(store.find, "dev"))


class LegacyTests(StoreTestCase):
    def test_ls_brings_over_every_entry_and_keeps_it(self):
        self.legacy.data["old"] = legacy(
            "old", sandboxes=[("sb-1", "saved")], snapshots=[("im-1", "sb-1")]
        )
        self.listing.aio.side_effect = lambda **_: _each([])
        rows = self.run_core(lifecycle.ls)
        self.assertEqual(
            rows,
            [
                {
                    "name": "old",
                    "sandbox_id": "sb-1",
                    "status": "stopped",
                    "snapshots": 1,
                }
            ],
        )
        self.assertEqual(self.stored("old")["snapshots"][0]["id"], "im-1")
        # An older version still finds it.
        self.assertIn("old", self.legacy.data)
        # Bringing it over again writes nothing.
        files = dict(self.volume.files)
        self.run_core(lifecycle.ls)
        self.run_core(store.load, "old")
        self.assertEqual(self.volume.files, files)

    def test_a_name_is_brought_over_when_it_is_used(self):
        self.legacy.data["old"] = legacy("old", sandboxes=[("sb-1", "saved")])
        self.assertEqual(self.run_core(lifecycle.resolve, "old"), "sb-1")
        self.assertEqual(self.stored("old")["sandbox_id"], "sb-1")

    def test_what_an_older_version_adds_later_is_merged_in(self):
        self.legacy.data["old"] = legacy("old", sandboxes=[("sb-1", "saved")])
        self.run_core(store.load, "old")
        self.run_core(
            store.update,
            "old",
            lambda r: store.add_snapshot(r, "im-new", "sb-1", "manual"),
        )
        # The older version records a snapshot in its own copy.
        store.add_snapshot(self.legacy.data["old"], "im-old", "sb-1", "manual")
        found = self.run_core(store.load, "old")
        self.assertEqual(
            sorted(s["id"] for s in found["snapshots"]), ["im-new", "im-old"]
        )

    def test_a_legacy_name_is_taken(self):
        self.legacy.data["old"] = legacy("old", sandboxes=[("sb-1", "saved")])
        with self.assertRaisesRegex(InvalidRequest, "taken"):
            self.run_core(lifecycle.up, OPTIONS, "old")
        self.create.aio.assert_not_called()

    def test_purge_removes_the_entry_and_the_name_stays_purged(self):
        self.legacy.data["old"] = legacy(
            "old", sandboxes=[("sb-old", "saved")], snapshots=[("im-1", "sb-old")]
        )
        self.run_core(lifecycle.purge, "old")
        self.assertNotIn("old", self.legacy.data)
        self.assertIsNone(self.stored("old"))
        # Even if an older version writes its copy back.
        self.legacy.data["old"] = legacy(
            "old", sandboxes=[("sb-old", "saved")], snapshots=[("im-1", "sb-old")]
        )
        self.assertEqual(self.run_core(store.records), [])
        with self.assertRaises(NotFound):
            self.run_core(store.load, "old")

    def test_no_legacy_dict_is_no_legacy_entries(self):
        self.without_legacy()
        self.write_record(record(sandboxes=[("sb-1", "saved")]))
        self.assertEqual(self.run_core(store.load, "dev")["sandbox_id"], "sb-1")
        self.run_core(lifecycle.purge, "dev")
        self.assertIsNone(self.stored("dev"))


class PurgeTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.old = record(sandboxes=[("sb-1", "saved")], snapshots=[("im-1", "sb-1")])
        self.write_record(self.old)

    def test_a_purged_name_is_listed_without_reading_it(self):
        self.run_core(store.remove, "dev", ["im-1"])
        [path] = self.files_of("dev")
        self.assertTrue(path.endswith(".purged.json"))
        self.volume.read_file.aio.reset_mock()
        self.assertEqual(self.run_core(store.records), [])
        self.volume.read_file.aio.assert_not_called()

    def test_an_older_copy_does_not_bring_a_purged_name_back(self):
        self.run_core(store.remove, "dev", ["im-1"])
        # A command that read the record before the purge writes it back.
        self.write_record(self.old, "stale.json")
        self.assertIsNone(self.run_core(store.find, "dev"))
        self.assertEqual(self.run_core(store.records), [])

    def test_a_snapshot_recorded_meanwhile_stops_the_purge(self):
        self.run_core(
            store.update,
            "dev",
            lambda r: store.add_snapshot(r, "im-2", "sb-1", "manual"),
        )
        with self.assertRaisesRegex(OperationFailed, "im-2"):
            self.run_core(store.remove, "dev", ["im-1"])
        self.assertEqual(len(self.stored("dev")["snapshots"]), 2)

    def test_a_purged_name_can_be_taken_again(self):
        self.run_core(store.remove, "dev", ["im-1"])
        self.create.aio.return_value.object_id = "sb-new"
        self.run_core(lifecycle.up, OPTIONS, "dev")
        found = self.stored("dev")
        self.assertEqual([s["id"] for s in found["sandboxes"]], ["sb-new"])
        self.assertEqual(found["removed"], ["im-1", "sb-1"])
        self.assertEqual(len(self.files_of("dev")), 1)


async def _each(items):
    for item in items:
        yield item


if __name__ == "__main__":
    unittest.main()
