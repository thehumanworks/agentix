import json
import unittest
from unittest.mock import AsyncMock, patch

import modal
from support import ModalTestCase, fake_sandbox

from agentix import lifecycle, store
from agentix.errors import InvalidRequest, NotFound, OperationFailed
from agentix.sandbox import NAME_TAG

OPTIONS: store.Options = {
    "image_name": "determinate-nix:latest",
    "secret_name": "1password",
    "timeout": 600,
    "workdir": "/srv/app",
    "memory": 16384,
    "cpu": 2,
}
TAILSCALE = json.dumps(
    {"status": "running", "magicdns": "dev.test.ts.net", "ipv4": "100.64.0.1"}
)


class LifecycleTestCase(ModalTestCase):
    def put_record(self, name="dev", sandbox_id="sb-test", exit="pending", snaps=()):
        record = store.new_record(name, dict(OPTIONS))
        store.add_sandbox(record, sandbox_id)
        # Before any sandbox that a test starts, as with real sandboxes.
        record["sandboxes"][-1]["started_at"] = "2026-01-01T00:00:00+00:00"
        record["sandboxes"][-1]["exit"] = exit
        for snapshot_id, created_at in snaps:
            store.add_snapshot(record, snapshot_id, sandbox_id, "manual", created_at)
        self.write_record(record)
        return record

    def sandboxes(self, **handles):
        """Look up each sandbox ID in `handles`, by its ID with dashes as underscores."""
        handles = {key.replace("_", "-"): handle for key, handle in handles.items()}
        self.lookup.aio = AsyncMock(side_effect=lambda sandbox_id: handles[sandbox_id])


class UpTests(LifecycleTestCase):
    def test_name_is_claimed_recorded_and_given_to_modal(self):
        result = self.run_core(lifecycle.up, OPTIONS, "dev")
        self.assertEqual(result, {"sandbox_id": "sb-test", "name": "dev"})
        options = self.create.aio.call_args.kwargs
        self.assertEqual(options["name"], "dev")
        self.assertEqual(options["tags"], {NAME_TAG: "dev"})
        self.assertEqual(options["memory"], 16384)
        record = self.stored("dev")
        self.assertEqual(record["sandbox_id"], "sb-test")
        self.assertEqual(record["options"], OPTIONS)
        self.assertEqual(record["sandboxes"][0]["exit"], "pending")

    def test_cli_prints_the_id_and_a_generated_name(self):
        output = self.run_cli("sandbox", "up")
        [path] = self.volume.files
        name = path.split("/")[1]
        self.assertIn("sandbox_id: sb-test", output)
        self.assertIn(f"name:       {name}", output)
        self.assertEqual(self.create.aio.call_args.kwargs["name"], name)

    def test_generated_name_skips_taken_ones(self):
        self.put_record("brisk-otter")
        with patch(
            "agentix.lifecycle.names.generate",
            side_effect=["brisk-otter", "calm-heron"],
        ):
            result = self.run_core(lifecycle.up, OPTIONS)
        self.assertEqual(result["name"], "calm-heron")

    def test_taken_name_is_refused_before_a_sandbox_starts(self):
        record = self.put_record("dev", "sb-old", exit="saved")
        with self.assertRaisesRegex(InvalidRequest, "restore dev"):
            self.run_core(lifecycle.up, OPTIONS, "dev")
        self.create.aio.assert_not_called()
        self.assertEqual(self.stored("dev"), store.merge(None, record))

    def test_failed_start_leaves_no_record(self):
        self.create.aio.side_effect = RuntimeError("no capacity")
        with self.assertRaisesRegex(RuntimeError, "no capacity"):
            self.run_core(lifecycle.up, OPTIONS, "dev")
        self.assertIsNone(self.stored("dev"))

    def test_record_is_written_once_with_the_sandbox(self):
        self.run_core(lifecycle.up, OPTIONS, "dev")
        self.volume.batch_upload.aio.assert_called_once()

    def test_sandbox_that_cannot_be_recorded_is_removed_with_its_snapshot(self):
        self.volume.batch_upload.aio.side_effect = ConnectionError("no volume")
        with self.assertRaisesRegex(ConnectionError, "no volume"):
            self.run_core(lifecycle.up, OPTIONS, "dev")
        self.sandbox.terminate.aio.assert_awaited_once()
        self.image_delete.aio.assert_awaited_once_with("im-exit")

    def test_name_taken_while_the_sandbox_started_is_refused(self):
        async def started_elsewhere(*args, **kwargs):
            self.put_record("dev", "sb-other")
            return self.sandbox

        self.create.aio.side_effect = started_elsewhere
        with self.assertRaisesRegex(InvalidRequest, "taken"):
            self.run_core(lifecycle.up, OPTIONS, "dev")
        self.sandbox.terminate.aio.assert_awaited_once()
        self.image_delete.aio.assert_awaited_once_with("im-exit")
        self.assertEqual(self.stored("dev")["sandbox_id"], "sb-other")

    def test_invalid_name_is_refused_before_modal_is_contacted(self):
        for name in ("sb-dev", "Dev", "dev-"):
            with self.subTest(name=name), self.assertRaises(InvalidRequest):
                self.run_cli("sandbox", "up", f"--name={name}")
        self.app_lookup.aio.assert_not_called()
        self.volume_lookup.assert_not_called()
        self.dict_lookup.assert_not_called()

    def test_tailscale_without_the_secret_is_refused_before_anything(self):
        for command in (
            ("sandbox", "up", "--tailscale", "--secret-name="),
            ("sandbox", "up", "--hostname=dev"),
        ):
            with self.subTest(command=command), self.assertRaises(InvalidRequest):
                self.run_cli(*command)
        with self.assertRaisesRegex(InvalidRequest, "1Password"):
            self.run_core(
                lifecycle.up, {**OPTIONS, "secret_name": ""}, "dev", tailscale=True
            )
        self.create.aio.assert_not_called()
        self.volume_lookup.assert_not_called()
        self.dict_lookup.assert_not_called()

    def test_tailscale_starts_after_the_sandbox(self):
        self.sandbox.process.stdout.read.aio.return_value = TAILSCALE
        result = self.run_core(lifecycle.up, OPTIONS, "dev", tailscale=True)
        self.assertEqual(
            result,
            {
                "sandbox_id": "sb-test",
                "name": "dev",
                "engine": "tailscale",
                "status": "running",
                "magicdns": "dev.test.ts.net",
                "ipv4": "100.64.0.1",
                "ssh_command": "ssh root@dev.test.ts.net",
            },
        )
        # The name is the hostname, so a restored sandbox gets the address again.
        self.assertEqual(self.sandbox.exec.aio.call_args.args[-2:], ("start", "dev"))
        # One session: the new handle is used without a lookup, nor its tags.
        self.lookup.aio.assert_not_called()
        self.sandbox.get_tags.aio.assert_not_called()

    def test_cli_passes_the_tailscale_hostname(self):
        self.sandbox.process.stdout.read.aio.return_value = TAILSCALE
        output = self.run_cli("sandbox", "up", "--tailscale", "--hostname=box")
        self.assertEqual(self.sandbox.exec.aio.call_args.args[-2:], ("start", "box"))
        self.assertIn("ssh root@dev.test.ts.net", output)

    def test_failed_tailscale_reports_the_sandbox_it_started(self):
        self.sandbox.process.wait.aio.return_value = 1
        self.sandbox.process.stderr.read.aio.return_value = "Enrollment failed"
        with self.assertRaisesRegex(
            OperationFailed, r"sb-test \(dev\) is running.*Enrollment failed"
        ):
            self.run_core(lifecycle.up, OPTIONS, "dev", tailscale=True)
        self.assertEqual(self.stored("dev")["sandbox_id"], "sb-test")
        self.sandbox.terminate.aio.assert_not_called()


class DownTests(LifecycleTestCase):
    def test_down_by_name_records_the_exit_snapshot(self):
        self.put_record()
        self.sandbox.poll.aio.side_effect = [None, 137]
        result = self.run_core(lifecycle.down, "dev")
        self.assertEqual(
            result,
            {
                "id": "sb-test",
                "status": "terminated",
                "name": "dev",
                "snapshot": "im-exit",
            },
        )
        self.sandbox.terminate.aio.assert_awaited_once_with(wait=True)
        record = self.stored("dev")
        self.assertEqual(record["snapshots"][0]["kind"], "down")
        self.assertEqual(record["sandboxes"][0]["exit"], "saved")

    def test_down_by_id_finds_the_name_from_its_tag(self):
        self.put_record()
        self.sandbox.get_tags.aio.return_value = {NAME_TAG: "dev"}
        self.sandbox.poll.aio.side_effect = [None, 137]
        output = self.run_cli("sandbox", "down", "sb-test")
        self.assertIn("im-exit", output)
        self.assertEqual(self.stored("dev")["snapshots"][0]["id"], "im-exit")

    def test_unnamed_sandbox_reports_as_before(self):
        result = self.run_core(lifecycle.down, "sb-test")
        self.assertEqual(result, {"id": "sb-test", "status": "terminated"})
        self.volume_lookup.assert_not_called()
        self.dict_lookup.assert_not_called()

    def test_snapshot_still_saving_is_recorded_later(self):
        self.put_record()
        self.sandbox.poll.aio.side_effect = [None, 137]
        self.sandbox._experimental_get_exit_snapshot.aio.side_effect = (
            modal.exception.TimeoutError("still saving")
        )
        self.assertEqual(self.run_core(lifecycle.down, "dev")["snapshot"], "")
        self.assertEqual(store.pending(self.stored("dev"))[0]["id"], "sb-test")


class ListTests(LifecycleTestCase):
    def test_lists_named_sandboxes_and_running_ones_without_a_name(self):
        self.put_record("dev", "sb-test")
        self.put_record("old", "sb-old", exit="saved", snaps=[("im-1", "2026-01-01")])
        other = fake_sandbox("sb-other")
        self.listing.aio.side_effect = lambda **_: _each([self.sandbox, other])
        rows = [json.loads(line) for line in self.run_cli("sandbox", "ls").splitlines()]
        self.assertEqual(
            rows,
            [
                {
                    "name": "dev",
                    "sandbox_id": "sb-test",
                    "status": "running",
                    "snapshots": 0,
                },
                {
                    "name": "old",
                    "sandbox_id": "sb-old",
                    "status": "stopped",
                    "snapshots": 1,
                },
                {
                    "name": "",
                    "sandbox_id": "sb-other",
                    "status": "running",
                    "snapshots": 0,
                },
            ],
        )
        # Running sandboxes and recorded snapshots need no further questions.
        self.lookup.aio.assert_not_called()

    def test_snapshot_of_a_sandbox_that_timed_out_is_recorded(self):
        self.put_record("old", "sb-old")
        ended = fake_sandbox("sb-old")
        ended.poll.aio.return_value = 124
        self.sandboxes(sb_old=ended)
        rows = self.run_core(lifecycle.ls)
        self.assertEqual(rows[0]["snapshots"], 1)
        ended._experimental_get_exit_snapshot.aio.assert_awaited_once_with(timeout=0)
        self.assertEqual(self.stored("old")["snapshots"][0]["kind"], "timeout")

    def test_one_failing_check_does_not_break_the_list(self):
        self.put_record("old", "sb-old")
        self.lookup.aio.side_effect = ConnectionError("no network")
        rows = self.run_core(lifecycle.ls)
        self.assertEqual(rows[0]["status"], "stopped")


class SnapshotTests(LifecycleTestCase):
    def test_snapshots_are_listed_oldest_first_with_the_exit_snapshot(self):
        self.put_record(snaps=[("im-2", "2026-02-01"), ("im-1", "2026-01-01")])
        self.sandbox.poll.aio.return_value = 137
        output = self.run_cli("sandbox", "snapshots", "dev")
        rows = [json.loads(line) for line in output.splitlines()]
        self.assertEqual([row["id"] for row in rows], ["im-1", "im-2", "im-exit"])
        self.assertEqual(rows[-1]["kind"], "exit")
        # Modal keeps a snapshot for 30 days.
        self.assertEqual(rows[0]["expires_at"], "2026-01-31T00:00:00+00:00")
        self.assertEqual([row["expired"] for row in rows], [True, True, False])

    def test_manual_snapshot_is_recorded(self):
        self.put_record()
        result = self.run_core(lifecycle.take_snapshot, "dev")
        self.assertEqual((result["id"], result["kind"]), ("im-manual", "manual"))
        self.assertEqual(self.stored("dev")["snapshots"][0]["id"], "im-manual")

    def test_unknown_name_is_reported(self):
        with self.assertRaisesRegex(NotFound, "No sandbox is named nope"):
            self.run_core(lifecycle.snapshots, "nope")


class RestoreTests(LifecycleTestCase):
    def setUp(self):
        super().setUp()
        self.put_record(
            sandbox_id="sb-old",
            exit="saved",
            snaps=[("im-new", "2026-02-01"), ("im-older", "2026-01-01")],
        )
        self.create.aio.return_value = fake_sandbox("sb-new")

    def test_latest_snapshot_starts_with_the_saved_options(self):
        result = self.run_core(lifecycle.restore, "dev")
        self.assertEqual(
            result, {"sandbox_id": "sb-new", "name": "dev", "snapshot": "im-new"}
        )
        self.snapshot_image.assert_called_once_with("im-new")
        options = self.create.aio.call_args.kwargs
        self.assertEqual(
            (options["memory"], options["cpu"], options["workdir"], options["name"]),
            (16384, 2, "/srv/app", "dev"),
        )
        self.assertEqual(self.create.aio.call_args.args[-1], "/srv/app")
        record = self.stored("dev")
        self.assertEqual(record["sandbox_id"], "sb-new")
        self.assertEqual(
            [item["id"] for item in record["sandboxes"]], ["sb-old", "sb-new"]
        )
        self.assertEqual(record["sandboxes"][-1]["restored_from"], "im-new")

    def test_chosen_snapshot_is_used(self):
        self.run_cli("sandbox", "restore", "dev", "--snapshot=im-older")
        self.snapshot_image.assert_called_once_with("im-older")

    def test_tailscale_keeps_the_name_as_hostname(self):
        restored = self.create.aio.return_value
        restored.process.stdout.read.aio.return_value = TAILSCALE
        result = self.run_core(lifecycle.restore, "dev", tailscale=True)
        self.assertEqual(restored.exec.aio.call_args.args[-2:], ("start", "dev"))
        self.assertEqual(result["ssh_command"], "ssh root@dev.test.ts.net")
        self.assertNotIn("warning", result)

    def test_snapshot_of_another_name_is_refused(self):
        with self.assertRaisesRegex(NotFound, "not a snapshot of dev"):
            self.run_core(lifecycle.restore, "dev", "im-elsewhere")
        self.create.aio.assert_not_called()

    def test_running_sandbox_is_refused(self):
        self.put_record()
        with self.assertRaisesRegex(InvalidRequest, "running as sb-test"):
            self.run_core(lifecycle.restore, "dev")
        self.create.aio.assert_not_called()

    def test_waits_for_the_snapshot_of_the_sandbox_that_ended(self):
        self.put_record(snaps=[("im-1", "2026-01-01")])
        self.sandbox.poll.aio.return_value = 137
        self.run_core(lifecycle.restore, "dev")
        self.snapshot_image.assert_called_once_with("im-exit")

    def test_snapshot_still_saving_is_not_skipped(self):
        self.put_record(snaps=[("im-1", "2026-01-01")])
        self.sandbox.poll.aio.return_value = 137
        self.sandbox._experimental_get_exit_snapshot.aio.side_effect = (
            modal.exception.TimeoutError("still saving")
        )
        with self.assertRaisesRegex(OperationFailed, "still saving the snapshot"):
            self.run_core(lifecycle.restore, "dev")
        self.create.aio.assert_not_called()

    def test_expired_snapshot_is_reported(self):
        self.create.aio.side_effect = modal.exception.NotFoundError(
            "Could not find image with ID im-new"
        )
        with self.assertRaisesRegex(NotFound, "im-new of dev no longer exists"):
            self.run_core(lifecycle.restore, "dev")

    def test_name_without_snapshots_is_reported(self):
        self.put_record(exit="none")
        with self.assertRaisesRegex(NotFound, "no snapshots"):
            self.run_core(lifecycle.restore, "dev")


class PurgeTests(LifecycleTestCase):
    def test_running_sandbox_is_refused(self):
        self.put_record(snaps=[("im-1", "2026-01-01")])
        with self.assertRaisesRegex(InvalidRequest, "--terminate"):
            self.run_cli("sandbox", "purge", "dev")
        self.image_delete.aio.assert_not_called()
        self.assertIsNotNone(self.stored("dev"))

    def test_terminate_deletes_the_exit_snapshot_too(self):
        self.put_record(snaps=[("im-1", "2026-01-01")])
        self.sandbox.poll.aio.side_effect = [None, None, 137]
        result = self.run_core(lifecycle.purge, "dev", terminate=True)
        self.assertEqual(
            result,
            {
                "name": "dev",
                "sandboxes": ["sb-test"],
                "deleted_snapshots": ["im-1", "im-exit"],
            },
        )
        self.sandbox.terminate.aio.assert_awaited_once_with(wait=True)
        self.assertEqual(
            sorted(call.args[0] for call in self.image_delete.aio.call_args_list),
            ["im-1", "im-exit"],
        )
        self.assertIsNone(self.stored("dev"))

    def test_snapshots_not_deleted_stay_recorded(self):
        self.put_record(
            "dev", "sb-old", exit="saved", snaps=[("im-1", "1"), ("im-2", "2")]
        )

        async def delete(snapshot_id):
            if snapshot_id == "im-2":
                raise ConnectionError("no network")

        self.image_delete.aio.side_effect = delete
        with self.assertRaisesRegex(OperationFailed, "im-2"):
            self.run_core(lifecycle.purge, "dev")
        self.assertEqual(
            [snapshot["id"] for snapshot in self.stored("dev")["snapshots"]],
            ["im-2"],
        )

    def test_purge_by_id_of_an_unnamed_sandbox_is_refused(self):
        with self.assertRaisesRegex(InvalidRequest, "has no name"):
            self.run_core(lifecycle.purge, "sb-test")


class NoSnapshotTests(LifecycleTestCase):
    def no_snapshot_record(self, snaps=(), exit="pending"):
        record = self.put_record(sandbox_id="sb-old", exit=exit, snaps=snaps)
        record["options"]["exit_snapshot"] = False
        self.write_record(record)

    def test_default_sandbox_saves_an_exit_snapshot(self):
        self.run_cli("sandbox", "up", "--name=dev")
        options = self.create.aio.call_args.kwargs
        self.assertEqual(
            options["experimental_options"], {"enable_exit_snapshot": True}
        )
        self.assertTrue(self.stored("dev")["options"]["exit_snapshot"])

    def test_no_snapshot_is_named_and_recorded_without_an_exit_snapshot(self):
        output = self.run_cli("sandbox", "up", "--name=dev", "--no-snapshot")
        self.assertIn("name:       dev", output)
        options = self.create.aio.call_args.kwargs
        self.assertIsNone(options["experimental_options"])
        self.assertEqual(options["tags"], {NAME_TAG: "dev"})
        self.assertFalse(self.stored("dev")["options"]["exit_snapshot"])

    def test_down_records_that_nothing_was_saved(self):
        self.no_snapshot_record()
        self.sandboxes(sb_old=self.sandbox)
        self.sandbox.poll.aio.side_effect = [None, 137]
        self.sandbox._experimental_get_exit_snapshot.aio.side_effect = (
            modal.exception.InvalidError("Exit snapshot is not enabled")
        )
        result = self.run_core(lifecycle.down, "dev")
        self.assertEqual(result["snapshot"], "")
        record = self.stored("dev")
        self.assertEqual(record["sandboxes"][0]["exit"], "none")
        self.assertEqual(record["snapshots"], [])
        self.image_delete.aio.assert_not_called()

    def test_restore_explains_why_there_is_nothing_to_restore(self):
        self.no_snapshot_record(exit="none")
        with self.assertRaisesRegex(NotFound, "started with --no-snapshot"):
            self.run_core(lifecycle.restore, "dev")
        self.create.aio.assert_not_called()

    def test_a_manual_snapshot_restores_without_an_exit_snapshot(self):
        self.no_snapshot_record(exit="none", snaps=[("im-1", "2026-11-01")])
        self.run_core(lifecycle.restore, "dev")
        self.snapshot_image.assert_called_once_with("im-1")
        self.assertIsNone(self.create.aio.call_args.kwargs["experimental_options"])


class EphemeralTests(LifecycleTestCase):
    def test_ephemeral_has_no_name_record_or_snapshot(self):
        output = self.run_cli("sandbox", "up", "--ephemeral", "--secret-name=")
        self.assertEqual(output.strip(), "sandbox_id: sb-test")
        options = self.create.aio.call_args.kwargs
        self.assertEqual(
            (options["name"], options["tags"], options["experimental_options"]),
            (None, None, None),
        )
        self.volume_lookup.assert_not_called()
        self.dict_lookup.assert_not_called()

    def test_no_snapshot_with_ephemeral_is_accepted(self):
        self.run_cli("sandbox", "up", "--ephemeral", "--no-snapshot")
        self.assertIsNone(self.create.aio.call_args.kwargs["experimental_options"])

    def test_a_name_is_refused_before_anything_starts(self):
        with self.assertRaisesRegex(InvalidRequest, "--ephemeral keeps no record"):
            self.run_cli("sandbox", "up", "--ephemeral", "--name=dev")
        with self.assertRaisesRegex(InvalidRequest, "--ephemeral keeps no record"):
            self.run_core(lifecycle.up, OPTIONS, "dev", ephemeral=True)
        self.app_lookup.aio.assert_not_called()
        self.create.aio.assert_not_called()
        self.volume_lookup.assert_not_called()

    def test_down_leaves_nothing_behind(self):
        self.run_cli("sandbox", "up", "--ephemeral")
        output = self.run_cli("sandbox", "down", "sb-test")
        self.assertNotIn("snapshot", output)
        self.sandbox.terminate.aio.assert_awaited_once_with(wait=True)
        self.volume_lookup.assert_not_called()
        self.image_delete.aio.assert_not_called()

    def test_failed_tailscale_names_the_sandbox_id(self):
        self.sandbox.process.wait.aio.return_value = 1
        with self.assertRaisesRegex(
            OperationFailed, "sb-test is running.*ssh start sb-test"
        ):
            self.run_core(lifecycle.up, OPTIONS, tailscale=True, ephemeral=True)

    def test_tailscale_hostname_is_the_sandbox_id(self):
        self.sandbox.process.stdout.read.aio.return_value = TAILSCALE
        self.run_core(lifecycle.up, OPTIONS, tailscale=True, ephemeral=True)
        self.assertEqual(
            self.sandbox.exec.aio.call_args.args[-2:], ("start", "sb-test")
        )


class NameResolutionTests(LifecycleTestCase):
    def test_exec_and_ssh_accept_a_name(self):
        self.put_record("dev", "sb-01M7DN105MG5PTYJHDWFFWCH54")
        self.sandbox.process.stdout.read.aio.return_value = TAILSCALE
        self.run_cli("sandbox", "exec", "dev", "true")
        self.lookup.aio.assert_called_with(sandbox_id="sb-01M7DN105MG5PTYJHDWFFWCH54")
        output = self.run_cli("sandbox", "ssh", "status", "--sandbox-id=dev")
        self.assertIn("sb-01M7DN105MG5PTYJHDWFFWCH54", output)

    def test_ids_need_no_record(self):
        self.run_cli("sandbox", "exec", "sb-test", "true")
        self.volume_lookup.assert_not_called()
        self.dict_lookup.assert_not_called()

    def test_unknown_name_is_reported(self):
        with self.assertRaisesRegex(NotFound, "nope"):
            self.run_cli("sandbox", "exec", "nope", "true")


async def _each(items):
    for item in items:
        yield item


if __name__ == "__main__":
    unittest.main()
