import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fire import Fire

from agentix import ssh
from agentix.__main__ import main


class SSHTests(unittest.TestCase):
    def setUp(self):
        self.lookup = patch("agentix.ssh.modal.Sandbox.from_id").start()
        self.addCleanup(patch.stopall)
        self.process = self.lookup.return_value.exec.return_value
        self.process.wait.return_value = 0
        self.process.stderr.read.return_value = ""
        self.process.stdout.read.return_value = json.dumps(
            {
                "status": "running",
                "magicdns": "sandbox.test.ts.net",
                "ipv4": "100.64.0.1",
            }
        )

    def test_cli_routes_sandbox_and_defaults_to_tailscale(self):
        real_fire = Fire
        with (
            patch(
                "fire.Fire",
                side_effect=lambda commands: real_fire(
                    commands,
                    command=["sandbox", "ssh", "start", "--sandbox-id=sb-test"],
                ),
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            main()
        self.lookup.assert_called_once_with(sandbox_id="sb-test")
        self.assertIn("sandbox.test.ts.net", output.getvalue())
        self.assertIn("100.64.0.1", output.getvalue())
        self.assertEqual(
            self.lookup.return_value.exec.call_args.args[-2:], ("start", "")
        )

    def test_hostname_is_a_separate_argument(self):
        hostname = "literal; $(echo unsafe)"
        ssh.start("sb-test", hostname=hostname)
        self.assertEqual(self.lookup.return_value.exec.call_args.args[-1], hostname)

    def test_invalid_engine_does_not_contact_sandbox(self):
        with self.assertRaises(ValueError):
            ssh.start("sb-test", engine="unknown")
        self.lookup.assert_not_called()

    def test_remote_failure_is_reported(self):
        self.process.wait.return_value = 1
        self.process.stderr.read.return_value = "Enrollment failed"
        with self.assertRaisesRegex(RuntimeError, "Enrollment failed"):
            ssh.start("sb-test")

    def test_start_checks_result(self):
        self.process.stdout.read.return_value = '{"status":"stopped"}'
        with self.assertRaisesRegex(RuntimeError, "did not start"):
            ssh.start("sb-test")

    def test_status_and_stop(self):
        self.process.stdout.read.return_value = (
            '{"status":"stopped","magicdns":"","ipv4":""}'
        )
        for action in (ssh.status, ssh.stop):
            with self.subTest(action=action.__name__):
                result = action("sb-test")
                self.assertEqual(result["status"], "stopped")
                self.assertEqual(
                    self.lookup.return_value.exec.call_args.args[-2], action.__name__
                )

    def test_openssh_repeated_start_does_not_need_key_or_restart(self):
        self.lookup.return_value.tunnels.return_value = {
            22: Mock(tcp_socket=("example.modal.host", 12345))
        }
        with patch("agentix.ssh._public_key") as read_key:
            result = ssh.start("sb-test", engine="openssh")
        read_key.assert_not_called()
        self.assertEqual(self.lookup.return_value.exec.call_count, 1)
        self.assertEqual(self.lookup.return_value.exec.call_args.args[-2], "status")
        self.assertEqual(
            result["ssh_command"],
            "ssh -p 12345 root@example.modal.host",
        )

    def test_openssh_starts_with_public_key_after_status_check(self):
        self.lookup.return_value.tunnels.return_value = {
            22: Mock(tcp_socket=("example.modal.host", 12345))
        }
        self.process.stdout.read.side_effect = [
            '{"status":"stopped"}',
            '{"status":"running"}',
        ]
        with patch("agentix.ssh._public_key", return_value="ssh-ed25519 public-data"):
            result = ssh.start("sb-test", engine="openssh", public_key="key.pub")
        self.assertEqual(result["status"], "running")
        self.assertEqual(
            self.lookup.return_value.exec.call_args.args[-2:],
            ("start", "ssh-ed25519 public-data"),
        )

    def test_openssh_missing_tunnel_fails_before_remote_changes(self):
        self.lookup.return_value.tunnels.return_value = {}
        with self.assertRaisesRegex(ValueError, "no SSH tunnel"):
            ssh.start("sb-test", engine="openssh")
        self.lookup.return_value.exec.assert_not_called()

    def test_engine_specific_arguments_rejected_before_remote_changes(self):
        for kwargs in (
            {"engine": "openssh", "hostname": "name"},
            {"public_key": "key.pub"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ssh.start("sb-test", **kwargs)
        self.lookup.assert_not_called()

    def test_private_keys_and_multiple_authorized_keys_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key"
            for content in (
                "-----BEGIN OPENSSH PRIVATE KEY-----",
                "ssh-ed25519 one\nssh-ed25519 two",
            ):
                key.write_text(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    ssh._public_key(str(key))

    def test_openssh_stop_uses_selected_engine(self):
        self.lookup.return_value.tunnels.return_value = {}
        self.process.stdout.read.return_value = '{"status":"stopped"}'
        result = ssh.stop("sb-test", engine="openssh")
        self.assertEqual(result["status"], "stopped")
        args = self.lookup.return_value.exec.call_args.args
        self.assertIn("agentix/openssh", args[2])
        self.assertEqual(args[-2], "stop")


if __name__ == "__main__":
    unittest.main()
