import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from support import ModalTestCase

from agentix import ssh, ssh_input
from agentix.errors import PublicKeyRequired
from agentix.sandbox import NAME_TAG

TUNNEL = {22: Mock(tcp_socket=("example.modal.host", 12345))}


class SSHTests(ModalTestCase):
    def setUp(self):
        super().setUp()
        self.sandbox.object_id = "sb-01M7DN105MG5PTYJHDWFFWCH54"
        self.process = self.sandbox.process
        self.process.stdout.read.aio.return_value = json.dumps(
            {
                "status": "running",
                "magicdns": "sandbox.test.ts.net",
                "ipv4": "100.64.0.1",
            }
        )
        # Tests that reach the key check do not depend on a local ssh-keygen.
        checked = patch("agentix.ssh.check_public_key", side_effect=lambda key: key)
        self.check_key = checked.start()
        self.addCleanup(checked.stop)

    def manage(self, action, *args, **kwargs):
        return self.run_core(ssh.manage, action, "sb-test", *args, **kwargs)

    def test_cli_routes_sandbox_and_defaults_to_tailscale(self):
        output = self.run_cli("sandbox", "ssh", "start", "--sandbox-id=sb-test")
        self.lookup.aio.assert_called_once_with(sandbox_id="sb-test")
        self.assertIn("sandbox.test.ts.net", output)
        self.assertIn("100.64.0.1", output)
        self.assertEqual(
            self.sandbox.exec.aio.call_args.args[-2:],
            ("start", "sb-test"),
        )

    def test_distinct_sandboxes_get_distinct_stable_names(self):
        for sandbox_id in ("sb-FIRST", "sb-SECOND", "sb-FIRST"):
            self.sandbox.object_id = sandbox_id
            self.run_core(ssh.manage, "start", sandbox_id)
            self.assertEqual(
                self.sandbox.exec.aio.call_args.args[-1], sandbox_id.lower()
            )

    def test_named_sandbox_given_by_id_gets_its_name(self):
        self.sandbox.get_tags.aio.return_value = {NAME_TAG: "dev"}
        self.manage("start")
        self.assertEqual(self.sandbox.exec.aio.call_args.args[-2:], ("start", "dev"))

    def test_known_name_needs_no_lookup(self):
        self.run_core(ssh.manage, "start", "sb-test", name="dev")
        self.assertEqual(self.sandbox.exec.aio.call_args.args[-1], "dev")
        self.sandbox.get_tags.aio.assert_not_called()

    def test_name_that_is_no_hostname_falls_back_to_the_id(self):
        # Names from before the hostname rule can end with a dash, which
        # Tailscale would drop from the DNS name.
        self.run_core(ssh.manage, "start", "sb-01M7DN105MG5PTYJHDWFFWCH54", name="dev-")
        self.assertEqual(
            self.sandbox.exec.aio.call_args.args[-1], "sb-01m7dn105mg5ptyjhdwffwch54"
        )

    def test_status_and_stop_ask_for_no_name(self):
        for action in ("status", "stop"):
            self.manage(action)
        self.sandbox.get_tags.aio.assert_not_called()

    def test_node_named_differently_by_tailscale_is_reported(self):
        self.process.stdout.read.aio.return_value = json.dumps(
            {
                "status": "running",
                "magicdns": "dev-1.test.ts.net",
                "ipv4": "100.64.0.1",
            }
        )
        result = self.run_core(ssh.manage, "start", "sb-test", name="dev")
        self.assertEqual(result["ssh_command"], "ssh root@dev-1.test.ts.net")
        self.assertIn("named this node dev-1", result["warning"])
        self.assertIn("ssh root@dev reaches that node", result["warning"])
        self.assertIn("agentix sandbox ssh start dev takes the name", result["warning"])
        self.process.stdout.read.aio.return_value = json.dumps(
            {"status": "running", "magicdns": "box-2.test.ts.net", "ipv4": ""}
        )
        result = self.run_core(
            ssh.manage, "start", "sb-test", hostname="box", name="dev"
        )
        self.assertIn(
            "agentix sandbox ssh start dev --hostname=box takes", result["warning"]
        )
        # Only a suffix means that the name was taken.
        self.process.stdout.read.aio.return_value = json.dumps(
            {"status": "running", "magicdns": "dev-box.test.ts.net", "ipv4": ""}
        )
        result = self.run_core(ssh.manage, "start", "sb-test", name="dev")
        self.assertNotIn("warning", result)
        self.process.stdout.read.aio.return_value = json.dumps(
            {"status": "running", "magicdns": "dev.test.ts.net", "ipv4": "100.64.0.1"}
        )
        result = self.run_core(ssh.manage, "start", "sb-test", name="dev")
        self.assertNotIn("warning", result)

    def test_hostname_is_a_separate_argument(self):
        hostname = "literal; $(echo unsafe)"
        self.manage("start", hostname=hostname)
        self.assertEqual(self.sandbox.exec.aio.call_args.args[-1], hostname)

    def test_invalid_engine_does_not_contact_sandbox(self):
        with self.assertRaises(ValueError):
            self.manage("start", engine="unknown")
        self.lookup.aio.assert_not_called()

    def test_remote_failure_is_reported(self):
        self.process.wait.aio.return_value = 1
        self.process.stderr.read.aio.return_value = "Enrollment failed"
        with self.assertRaisesRegex(RuntimeError, "Enrollment failed"):
            self.manage("start")

    def test_start_checks_result(self):
        self.process.stdout.read.aio.return_value = '{"status":"stopped"}'
        with self.assertRaisesRegex(RuntimeError, "did not start"):
            self.manage("start")

    def test_status_and_stop(self):
        self.process.stdout.read.aio.return_value = (
            '{"status":"stopped","magicdns":"","ipv4":""}'
        )
        for action in ("status", "stop"):
            with self.subTest(action=action):
                result = self.manage(action)
                self.assertEqual(result["status"], "stopped")
                self.assertEqual(
                    self.sandbox.exec.aio.call_args.args[-2:], (action, "")
                )

    def test_openssh_start_is_one_command_that_carries_the_key(self):
        self.sandbox.tunnels.aio.return_value = TUNNEL
        result = self.manage(
            "start", engine="openssh", public_key="ssh-ed25519 public-data"
        )
        self.assertEqual(self.sandbox.exec.aio.call_count, 1)
        self.assertEqual(
            self.sandbox.exec.aio.call_args.args[-2:],
            ("start", "ssh-ed25519 public-data"),
        )
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["ssh_command"], "ssh -p 12345 root@example.modal.host")
        self.assertEqual(result["user"], "root")

    def test_openssh_repeated_start_does_not_need_key(self):
        self.sandbox.tunnels.aio.return_value = TUNNEL
        result = self.manage("start", engine="openssh")
        self.check_key.assert_not_called()
        self.assertEqual(self.sandbox.exec.aio.call_args.args[-2:], ("start", ""))
        self.assertEqual(result["port"], 12345)

    def test_openssh_first_start_without_key_asks_for_one(self):
        self.sandbox.tunnels.aio.return_value = TUNNEL
        self.process.wait.aio.return_value = 1
        self.process.stderr.read.aio.return_value = "An SSH public key is required.\n"
        with self.assertRaisesRegex(PublicKeyRequired, "--public-key=PATH"):
            self.manage("start", engine="openssh")

    def test_openssh_script_still_asks_for_the_key_with_the_expected_text(self):
        script = (ssh._SCRIPTS / "openssh-ssh.sh").read_text()
        self.assertIn(f'echo "{ssh._KEY_REQUIRED}" >&2', script)

    def test_openssh_missing_tunnel_fails_before_remote_changes(self):
        with self.assertRaisesRegex(ValueError, "no SSH tunnel"):
            self.manage("start", engine="openssh")
        self.sandbox.exec.aio.assert_not_called()

    def test_engine_specific_arguments_rejected_before_remote_changes(self):
        for kwargs in (
            {"engine": "openssh", "hostname": "name"},
            {"public_key": "ssh-ed25519 public-data"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.manage("start", **kwargs)
        self.lookup.aio.assert_not_called()

    def test_invalid_key_text_is_rejected_before_remote_changes(self):
        self.check_key.side_effect = ssh_input.check_public_key
        with self.assertRaises(ValueError):
            self.manage("start", engine="openssh", public_key="not a key")
        self.lookup.aio.assert_not_called()

    def test_openssh_stop_uses_selected_engine(self):
        self.process.stdout.read.aio.return_value = '{"status":"stopped"}'
        result = self.manage("stop", engine="openssh")
        self.assertEqual(result["status"], "stopped")
        args = self.sandbox.exec.aio.call_args.args
        self.assertIn("agentix/openssh", args[2])
        self.assertEqual(args[-2], "stop")
        self.assertNotIn("ssh_command", result)


class SSHCommandLineKeyTests(ModalTestCase):
    """The command line reads the key file. The sandbox decides if it needs one."""

    def setUp(self):
        super().setUp()
        self.sandbox.tunnels.aio.return_value = TUNNEL
        self.sandbox.process.stdout.read.aio.return_value = '{"status":"running"}'
        checked = patch("agentix.ssh.check_public_key", side_effect=lambda key: key)
        checked.start()
        self.addCleanup(checked.stop)

    def start(self, *options):
        return self.run_cli(
            "sandbox", "ssh", "start", "sb-test", "--engine=openssh", *options
        )

    def test_key_file_content_is_sent(self):
        with patch(
            "agentix.ssh_input.read_public_key", return_value="ssh-ed25519 data"
        ) as read_key:
            self.start("--public-key=key.pub")
        read_key.assert_called_once_with("key.pub")
        self.assertEqual(
            self.sandbox.exec.aio.call_args.args[-2:], ("start", "ssh-ed25519 data")
        )

    def test_repeated_start_works_without_a_readable_key(self):
        with patch("agentix.ssh_input.read_public_key", side_effect=ValueError("none")):
            output = self.start()
        self.assertIn("ssh -p 12345 root@example.modal.host", output)

    def test_first_start_reports_the_local_key_problem(self):
        self.sandbox.process.wait.aio.return_value = 1
        self.sandbox.process.stderr.read.aio.return_value = (
            "An SSH public key is required."
        )
        with (
            patch(
                "agentix.ssh_input.read_public_key",
                side_effect=ValueError("Invalid SSH public key: key.pub"),
            ),
            self.assertRaisesRegex(ValueError, "Invalid SSH public key: key.pub"),
        ):
            self.start("--public-key=key.pub")


class PublicKeyTests(unittest.TestCase):
    def test_private_keys_and_multiple_authorized_keys_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key"
            for content in (
                "-----BEGIN OPENSSH PRIVATE KEY-----",
                "ssh-ed25519 one\nssh-ed25519 two",
            ):
                key.write_text(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    ssh_input.read_public_key(str(key))

    def test_missing_key_file_asks_for_a_path(self):
        with self.assertRaisesRegex(ValueError, "--public-key=PATH"):
            ssh_input.read_public_key("/nonexistent/key.pub")


if __name__ == "__main__":
    unittest.main()
