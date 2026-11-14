import contextlib
import io
import unittest
from unittest.mock import patch

from fire import Fire

from agentix.__main__ import main
from agentix.sandbox import OP_TOKEN_FILE, WORKDIR_FILE, create_sandbox


class SandboxWorkdirTests(unittest.TestCase):
    def setUp(self):
        self.create = patch("agentix.sandbox.modal.Sandbox.create").start()
        patch("agentix.sandbox.modal.Image.from_name").start()
        patch("agentix.sandbox.modal.Secret.from_name").start()
        patch("agentix.sandbox.get_app").start()
        self.addCleanup(patch.stopall)
        self.create.return_value.object_id = "sb-test"
        self.create.return_value.exec.return_value.wait.return_value = 0

    def test_default_keeps_image_workdir(self):
        create_sandbox()
        self.assertIsNone(self.create.call_args.kwargs["workdir"])

    def test_override_is_passed_to_modal(self):
        create_sandbox(workdir="/srv/project")
        self.assertEqual(self.create.call_args.kwargs["workdir"], "/srv/project")

    def test_cli_accepts_workdir(self):
        real_fire = Fire
        with (
            patch(
                "fire.Fire",
                side_effect=lambda commands: real_fire(
                    commands, command=["sandbox", "up", "--workdir=/srv/project"]
                ),
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            main()
        self.assertEqual(self.create.call_args.kwargs["workdir"], "/srv/project")
        self.assertIn("sb-test", output.getvalue())

    def test_override_is_recorded_for_ssh_logins(self):
        create_sandbox(secret_name="", workdir="/srv/project")
        command = self.create.return_value.exec.call_args.args
        self.assertEqual(command[:2], ("sh", "-c"))
        self.assertIn(f"> {WORKDIR_FILE}", command[2])
        self.assertEqual(command[-1], "/srv/project")

    def test_default_records_no_override(self):
        create_sandbox(secret_name="")
        self.create.return_value.exec.assert_not_called()

    def test_failed_record_removes_the_sandbox(self):
        self.create.return_value.exec.return_value.wait.return_value = 1
        with self.assertRaisesRegex(RuntimeError, "working directory"):
            create_sandbox(secret_name="", workdir="/srv/project")
        self.create.return_value.terminate.assert_called_once()


class SandboxOpTokenTests(unittest.TestCase):
    def setUp(self):
        self.create = patch("agentix.sandbox.modal.Sandbox.create").start()
        patch("agentix.sandbox.modal.Image.from_name").start()
        patch("agentix.sandbox.modal.Secret.from_name").start()
        patch("agentix.sandbox.get_app").start()
        self.addCleanup(patch.stopall)
        self.sandbox = self.create.return_value
        self.sandbox.exec.return_value.wait.return_value = 0

    def test_token_is_stored_in_a_root_only_file(self):
        create_sandbox()
        command = self.sandbox.exec.call_args.args
        self.assertEqual(command[:2], ("sh", "-c"))
        self.assertIn("umask 077", command[2])
        self.assertIn(f'"$OP_SERVICE_ACCOUNT_TOKEN" > {OP_TOKEN_FILE}', command[2])
        self.sandbox.terminate.assert_not_called()

    def test_no_secret_stores_no_token(self):
        create_sandbox(secret_name="")
        self.sandbox.exec.assert_not_called()

    def test_failed_store_removes_the_sandbox(self):
        self.sandbox.exec.return_value.wait.return_value = 1
        with self.assertRaisesRegex(RuntimeError, "1Password token"):
            create_sandbox()
        self.sandbox.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
