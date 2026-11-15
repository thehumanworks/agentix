import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import ModalTestCase

from agentix import idle, sandbox
from agentix.defaults import SANDBOX_MEMORY_MIB
from agentix.errors import InvalidRequest, OperationFailed
from agentix.sandbox import OP_TOKEN_FILE, READY_FILE, WORKDIR_FILE


class SandboxCreateTests(ModalTestCase):
    def test_default_keeps_image_workdir(self):
        self.run_core(sandbox.create)
        self.assertIsNone(self.create.aio.call_args.kwargs["workdir"])

    def test_override_is_passed_to_modal(self):
        self.run_core(sandbox.create, workdir="/srv/project")
        self.assertEqual(self.create.aio.call_args.kwargs["workdir"], "/srv/project")

    def test_cli_accepts_workdir(self):
        output = self.run_cli("sandbox", "up", "--workdir=/srv/project")
        self.assertEqual(self.create.aio.call_args.kwargs["workdir"], "/srv/project")
        self.assertIn("sb-test", output)

    def test_default_reserves_memory_and_keeps_modal_cpu(self):
        self.run_core(sandbox.create)
        options = self.create.aio.call_args.kwargs
        self.assertEqual(options["memory"], SANDBOX_MEMORY_MIB)
        self.assertIsNone(options["cpu"])

    def test_cli_accepts_memory_and_cpu(self):
        self.run_cli("sandbox", "up", "--memory=16384", "--cpu=4")
        options = self.create.aio.call_args.kwargs
        self.assertEqual((options["memory"], options["cpu"]), (16384, 4))

    def test_files_are_written_by_the_main_command_without_extra_commands(self):
        self.run_core(sandbox.create, workdir="/srv/project")
        command = self.create.aio.call_args.args
        self.assertEqual(command[:2], ("sh", "-c"))
        self.assertIn("umask 077", command[2])
        self.assertIn(f'"$OP_SERVICE_ACCOUNT_TOKEN" > {OP_TOKEN_FILE}', command[2])
        self.assertIn(f"> {WORKDIR_FILE}", command[2])
        self.assertEqual(command[3:], ("agentix-init", "/srv/project", "", "", ""))
        self.sandbox.exec.aio.assert_not_called()

    def test_returns_only_when_the_main_command_has_written_its_files(self):
        self.run_core(sandbox.create)
        probe = self.create.aio.call_args.kwargs["readiness_probe"]
        self.assertEqual(probe.exec_argv, ("test", "-e", READY_FILE))
        self.sandbox.wait_until_ready.aio.assert_awaited_once()

    def test_sandbox_that_does_not_become_ready_is_removed(self):
        self.sandbox.wait_until_ready.aio.side_effect = TimeoutError("too slow")
        with self.assertRaisesRegex(OperationFailed, "did not become ready"):
            self.run_core(sandbox.create)
        self.sandbox.terminate.aio.assert_awaited_once()
        # No record holds it, so its exit snapshot is deleted with it.
        self.image_delete.aio.assert_awaited_once_with("im-exit")

    def test_every_sandbox_saves_its_filesystem_when_it_ends(self):
        self.run_core(sandbox.create)
        options = self.create.aio.call_args.kwargs
        self.assertEqual(
            options["experimental_options"], {"enable_exit_snapshot": True}
        )
        self.assertIsNone(options["name"])
        self.assertIsNone(options["tags"])

    def test_name_is_given_to_modal_and_tagged(self):
        self.run_core(sandbox.create, name="dev")
        options = self.create.aio.call_args.kwargs
        self.assertEqual(options["name"], "dev")
        self.assertEqual(options["tags"], {sandbox.NAME_TAG: "dev"})

    def test_idle_stop_hands_the_main_command_to_the_watcher(self):
        self.run_core(sandbox.create, name="dev", idle_stop=1200)
        args = self.create.aio.call_args.args
        self.assertEqual(args[4:], ("", "1200", "30", idle.WATCHER))
        self.assertEqual(
            self.create.aio.call_args.kwargs["tags"],
            {sandbox.NAME_TAG: "dev", idle.IDLE_TAG: "1200"},
        )

    def test_idle_stop_tags_a_sandbox_without_a_name(self):
        self.run_core(sandbox.create, idle_stop=600)
        self.assertEqual(
            self.create.aio.call_args.kwargs["tags"], {idle.IDLE_TAG: "600"}
        )

    def test_snapshot_replaces_the_image(self):
        self.run_core(sandbox.create, snapshot_id="im-saved")
        self.snapshot_image.assert_called_once_with("im-saved")
        self.image.assert_not_called()
        self.assertIs(
            self.create.aio.call_args.kwargs["image"], self.snapshot_image.return_value
        )

    def test_default_passes_no_override_to_the_main_command(self):
        self.run_core(sandbox.create)
        self.assertEqual(self.create.aio.call_args.args[4], "")

    def test_secret_must_hold_the_token(self):
        self.run_core(sandbox.create)
        self.secret.assert_called_once_with(
            "1password", required_keys=["OP_SERVICE_ACCOUNT_TOKEN"]
        )
        self.assertEqual(len(self.create.aio.call_args.kwargs["secrets"]), 1)

    def test_no_secret_is_attached_when_none_is_named(self):
        self.run_core(sandbox.create, secret_name="")
        self.assertEqual(self.create.aio.call_args.kwargs["secrets"], [])


class SandboxInitScriptTests(unittest.TestCase):
    """Run the main command of a sandbox locally, with its paths in a directory."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.token_file = self.root / "run/op-token"
        self.workdir_file = self.root / "run/workdir"
        self.ready_file = self.root / "run/ready"
        self.script = (
            sandbox._INIT.replace(OP_TOKEN_FILE, str(self.token_file))
            .replace(WORKDIR_FILE, str(self.workdir_file))
            .replace(READY_FILE, str(self.ready_file))
            .replace("exec sleep infinity", "exit 0")
        )

    def run_script(self, workdir="", token=None, idle_args=()):
        environment = {"PATH": "/usr/bin:/bin"}
        if token is not None:
            environment["OP_SERVICE_ACCOUNT_TOKEN"] = token
        return subprocess.run(
            ["sh", "-c", self.script, "agentix-init", workdir, *idle_args],
            env=environment,
            capture_output=True,
            text=True,
            umask=0o022,
        )

    def test_script_ends_in_the_command_that_keeps_the_sandbox_alive(self):
        self.assertTrue(sandbox._INIT.rstrip().endswith("exec sleep infinity"))

    def test_idle_stop_execs_the_watcher_after_the_files(self):
        watcher = 'test -e "${4#test -e }" || exit 9; echo "$0 $*"; exit 75'
        result = self.run_script(idle_args=("120", "20", watcher))
        self.assertEqual(result.returncode, 75)
        self.assertEqual(
            result.stdout, f"agentix-idle watch 120 20 test -e {self.ready_file}\n"
        )

    def test_no_idle_stop_keeps_the_sandbox_alive(self):
        result = self.run_script(idle_args=("", "", ""))
        self.assertEqual(result.returncode, 0)
        self.assertTrue(self.ready_file.exists())

    def test_token_is_stored_in_a_root_only_file(self):
        self.assertEqual(self.run_script(token="secret-value").returncode, 0)
        self.assertEqual(self.token_file.read_text(), "secret-value")
        self.assertEqual(self.token_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.token_file.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.workdir_file.exists())

    def test_no_token_stores_no_file(self):
        self.assertEqual(self.run_script().returncode, 0)
        self.assertFalse(self.token_file.exists())
        self.assertTrue(self.ready_file.exists())

    def test_ready_is_the_last_file_written(self):
        script = sandbox._INIT
        self.assertGreater(script.index(READY_FILE + "\n"), script.index(WORKDIR_FILE))
        self.assertGreater(script.index(READY_FILE + "\n"), script.index(OP_TOKEN_FILE))

    def test_override_is_recorded_for_ssh_logins(self):
        self.assertEqual(self.run_script(workdir="/srv/my project").returncode, 0)
        self.assertEqual(self.workdir_file.read_text(), "/srv/my project\n")
        self.assertEqual(self.workdir_file.stat().st_mode & 0o777, 0o644)

    def test_token_and_override_are_stored_together(self):
        result = self.run_script(workdir="/srv/app", token="secret-value")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.token_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.workdir_file.stat().st_mode & 0o777, 0o644)

    def test_failed_write_ends_the_sandbox(self):
        self.token_file.parent.mkdir(parents=True)
        self.token_file.mkdir()
        self.assertNotEqual(self.run_script(token="secret-value").returncode, 0)
        self.assertFalse(self.ready_file.exists())


class SandboxUseTests(ModalTestCase):
    def test_list_returns_ids_of_the_app(self):
        self.assertEqual(self.run_core(sandbox.list_ids), ["sb-test"])
        self.listing.aio.assert_called_once_with(app_id="ap-test")
        self.assertEqual(
            json.loads(self.run_cli("sandbox", "ls")),
            {
                "name": "",
                "sandbox_id": "sb-test",
                "status": "running",
                "snapshots": 0,
                "idle_stop": "off",
            },
        )

    def test_execute_returns_output_and_exit_code(self):
        self.sandbox.process.stdout.read.aio.return_value = "out"
        self.sandbox.process.stderr.read.aio.return_value = "problem"
        self.sandbox.process.wait.aio.return_value = 3
        result = self.run_core(sandbox.execute, "sb-test", ["ls", "a b"])
        self.assertEqual(result, {"stdout": "out", "stderr": "problem", "exit_code": 3})
        self.sandbox.exec.aio.assert_called_once_with("ls", "a b")

    def test_execute_needs_a_command(self):
        with self.assertRaises(InvalidRequest):
            self.run_core(sandbox.execute, "sb-test", [])
        self.lookup.aio.assert_not_called()

    def test_cli_exec_splits_the_command_and_prints_output(self):
        self.sandbox.process.stdout.read.aio.return_value = "hello"
        output = self.run_cli("sandbox", "exec", "sb-test", "echo 'a b'")
        self.sandbox.exec.aio.assert_called_once_with("echo", "a b")
        self.assertIn("hello", output)

    def test_cli_exec_reports_a_failed_command(self):
        self.sandbox.process.stderr.read.aio.return_value = "no such file"
        self.sandbox.process.wait.aio.return_value = 1
        with self.assertRaisesRegex(RuntimeError, "no such file"):
            self.run_cli("sandbox", "exec", "sb-test", "cat missing")

    def test_terminate_waits_and_reports(self):
        result = self.run_core(sandbox.terminate, "sb-test")
        self.assertEqual(result, {"id": "sb-test", "status": "terminated"})
        self.sandbox.terminate.aio.assert_called_once_with(wait=True)


if __name__ == "__main__":
    unittest.main()
