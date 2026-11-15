import asyncio
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from support import ModalTestCase, fake_sandbox

from agentix import lifecycle, ssh, store
from agentix.__main__ import main
from agentix.errors import InvalidRequest, NotFound, OperationFailed
from agentix.sandbox import NAME_TAG

OPTIONS: store.Options = {
    "image_name": "determinate-nix:latest",
    "secret_name": "1password",
    "timeout": 600,
    "workdir": "",
    "memory": 8192,
    "cpu": 0,
}
ED25519 = (
    "ssh-ed25519 " + base64.b64encode(b"\0\0\0\x0bssh-ed25519" + b"k" * 36).decode()
)
RSA = "ssh-rsa " + base64.b64encode(b"\0\0\0\x07ssh-rsa" + b"r" * 64).decode()


def tailscale(status="running", magicdns="dev.test.ts.net", keys=(RSA, ED25519)):
    return json.dumps(
        {
            "status": status,
            "magicdns": magicdns,
            "ipv4": "100.64.0.1",
            "host_keys": list(keys),
        }
    )


class ShellTestCase(ModalTestCase):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = patch.dict(os.environ, {"XDG_STATE_HOME": self.directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.reachable = self._patch("agentix.ssh.wait_until_reachable")
        self.execvp = self._patch("agentix.__main__.os.execvp")
        self.output = self.sandbox.process.stdout.read.aio

    def put_record(self, name="dev", sandbox_id="sb-test", exit="pending", snaps=()):
        record = store.new_record(name, dict(OPTIONS))
        store.add_sandbox(record, sandbox_id)
        record["sandboxes"][-1]["started_at"] = "2026-01-01T00:00:00+00:00"
        record["sandboxes"][-1]["exit"] = exit
        for snapshot_id, created_at in snaps:
            store.add_snapshot(record, snapshot_id, sandbox_id, "down", created_at)
        self.write_record(record)

    def shell(self, *arguments):
        """Run `agentix sandbox shell ARGUMENTS` as the shell would, via sys.argv."""
        with (
            patch.object(sys, "argv", ["agentix", "sandbox", "shell", *arguments]),
            patch("sys.stderr") as stderr,
        ):
            main()
        self.execvp.assert_called_once()
        file, argv = self.execvp.call_args.args
        self.assertEqual(file, "ssh")
        self.stderr = "".join(call.args[0] for call in stderr.write.call_args_list)
        return argv

    def known_hosts(self, alias):
        return (Path(self.directory.name) / "agentix/known_hosts" / alias).read_text()


class ShellTests(ShellTestCase):
    def test_running_ssh_is_used_with_the_host_keys_the_sandbox_reported(self):
        self.put_record()
        self.output.return_value = tailscale()
        argv = self.shell("dev")
        # Only a status: SSH runs already, and keeps its hostname.
        self.assertEqual(self.sandbox.exec.aio.call_count, 1)
        self.assertEqual(self.sandbox.exec.aio.call_args.args[-2:], ("status", ""))
        self.assertEqual(
            argv,
            [
                "ssh",
                "-o",
                "HostName=100.64.0.1",
                "-o",
                "HostKeyAlias=agentix-dev-tailscale",
                "-o",
                f'UserKnownHostsFile="{self.directory.name}/agentix/known_hosts/agentix-dev-tailscale"',
                "-o",
                "StrictHostKeyChecking=yes",
                "root@dev.test.ts.net",
            ],
        )
        self.assertEqual(
            self.known_hosts("agentix-dev-tailscale"),
            f"agentix-dev-tailscale {RSA}\nagentix-dev-tailscale {ED25519}\n",
        )
        self.reachable.assert_awaited_once_with("100.64.0.1", 22, "tailscale")
        self.assertIn(ssh.fingerprint(ED25519), self.stderr)

    def test_command_after_the_separator_runs_in_the_sandbox(self):
        self.put_record()
        self.output.return_value = tailscale()
        argv = self.shell("dev", "--", "ls", "-la", "--color")
        self.assertEqual(
            argv[-5:], ["root@dev.test.ts.net", "--", "ls", "-la", "--color"]
        )
        self.assertNotIn("-t", argv)

    def test_command_gets_a_terminal_when_there_is_one(self):
        self.put_record()
        self.output.return_value = tailscale()
        with patch("sys.stdin") as stdin, patch("sys.stdout") as stdout:
            stdin.isatty.return_value = stdout.isatty.return_value = True
            argv = self.shell("dev", "--", "nvim")
        self.assertEqual(argv[-4:], ["-t", "root@dev.test.ts.net", "--", "nvim"])

    def test_stopped_ssh_is_started_under_the_name(self):
        self.put_record()
        self.output.side_effect = [tailscale("stopped", "", ()), tailscale()]
        self.shell("dev")
        self.assertEqual(
            [call.args[-2:] for call in self.sandbox.exec.aio.call_args_list],
            [("status", ""), ("start", "dev")],
        )

    def test_running_node_that_is_offline_is_started_again(self):
        self.put_record()
        offline = json.loads(tailscale())
        offline["online"] = False
        self.output.side_effect = [json.dumps(offline), tailscale()]
        self.shell("dev")
        self.assertEqual(self.sandbox.exec.aio.call_args.args[-2:], ("start", "dev"))

    def test_hostname_is_applied_with_a_start(self):
        self.put_record()
        self.output.return_value = tailscale(magicdns="box.test.ts.net")
        argv = self.shell("dev", "--hostname=box")
        self.assertEqual(self.sandbox.exec.aio.call_args.args[-2:], ("start", "box"))
        self.assertEqual(argv[-1], "root@box.test.ts.net")

    def test_renamed_node_is_reached_at_its_real_name_with_a_warning(self):
        self.put_record()
        self.output.side_effect = [
            tailscale("stopped", "", ()),
            tailscale(magicdns="dev-1.test.ts.net"),
        ]
        argv = self.shell("dev")
        self.assertEqual(argv[-1], "root@dev-1.test.ts.net")
        self.assertIn("Warning: Tailscale named this node dev-1", self.stderr)

    def test_sandbox_by_id_uses_its_name_for_the_alias(self):
        self.sandbox.get_tags.aio.return_value = {NAME_TAG: "dev"}
        self.output.return_value = tailscale()
        argv = self.shell("sb-test")
        self.assertIn("HostKeyAlias=agentix-dev-tailscale", argv)

    def test_no_host_key_is_refused(self):
        self.put_record()
        self.output.return_value = tailscale(keys=())
        with self.assertRaisesRegex(OperationFailed, "identity cannot be checked"):
            self.shell("dev")
        self.execvp.assert_not_called()

    def test_unreachable_sandbox_is_not_logged_in_to(self):
        self.put_record()
        self.output.return_value = tailscale()
        self.reachable.side_effect = OperationFailed("did not answer")
        with self.assertRaisesRegex(OperationFailed, "did not answer"):
            self.shell("dev")
        self.execvp.assert_not_called()

    def test_engine_arguments_are_checked_before_modal(self):
        with self.assertRaises(InvalidRequest):
            self.shell("dev", "--engine=openssh", "--hostname=box")
        self.volume_lookup.assert_not_called()
        self.lookup.aio.assert_not_called()


class OpenSSHShellTests(ShellTestCase):
    def setUp(self):
        super().setUp()
        self.put_record()
        self.sandbox.tunnels.aio.return_value = {
            22: Mock(tcp_socket=("r1.modal.host", 41234))
        }
        self.output.return_value = json.dumps(
            {"status": "running", "host_key_fingerprint": ssh.fingerprint(ED25519)}
        )
        self.scan = self._patch("agentix.ssh.subprocess.run")
        self.scan.return_value = Mock(
            stdout=f"# r1.modal.host:41234 SSH-2.0-OpenSSH\n[r1.modal.host]:41234 {ED25519}\n"
        )

    def test_scanned_key_with_the_reported_fingerprint_is_trusted(self):
        argv = self.shell("dev", "--engine=openssh")
        self.assertEqual(
            self.scan.call_args.args[0],
            ["ssh-keyscan", "-T", "10", "-p", "41234", "r1.modal.host"],
        )
        self.assertEqual(argv[1:3], ["-p", "41234"])
        self.assertEqual(argv[-1], "root@r1.modal.host")
        self.assertEqual(
            self.known_hosts("agentix-dev-openssh"), f"agentix-dev-openssh {ED25519}\n"
        )

    def test_other_key_is_refused(self):
        self.scan.return_value = Mock(stdout=f"[r1.modal.host]:41234 {RSA}\n")
        with self.assertRaisesRegex(OperationFailed, "did not present the host key"):
            self.shell("dev", "--engine=openssh")
        self.execvp.assert_not_called()

    def test_first_start_reads_the_local_key_and_uses_its_private_half(self):
        directory = Path(self.directory.name)
        (directory / "id_test").write_text("private")
        (directory / "id_test.pub").write_text("ssh-ed25519 local")
        self.output.side_effect = [
            json.dumps({"status": "stopped", "host_key_fingerprint": ""}),
            self.output.return_value,
        ]
        with (
            patch("agentix.ssh.check_public_key", side_effect=lambda key: key),
            patch(
                "agentix.ssh_input.check_public_key",
                side_effect=lambda key, source: key.strip(),
            ),
        ):
            argv = self.shell(
                "dev", "--engine=openssh", f"--public-key={directory}/id_test.pub"
            )
        self.assertEqual(
            self.sandbox.exec.aio.call_args.args[-2:], ("start", "ssh-ed25519 local")
        )
        self.assertIn(str(directory / "id_test"), argv[argv.index("-i") + 1])


class TargetTests(ShellTestCase):
    def test_the_only_running_sandbox_is_used(self):
        self.put_record()
        self.output.return_value = tailscale()
        self.assertEqual(self.shell()[-1], "root@dev.test.ts.net")

    def test_several_running_sandboxes_are_listed(self):
        self.put_record("dev", "sb-test")
        other = fake_sandbox("sb-other")
        self.listing.aio = Mock(side_effect=lambda **_: _each([self.sandbox, other]))
        with self.assertRaisesRegex(
            InvalidRequest, r"2 sandboxes are running.*\n  dev \(sb-test\)\n  sb-other"
        ):
            self.run_core(lifecycle.running)

    def test_no_running_sandbox_is_reported(self):
        self.listing.aio = Mock(side_effect=lambda **_: _each([]))
        with self.assertRaisesRegex(NotFound, "No sandbox is running"):
            self.run_core(lifecycle.running)

    def test_stopped_sandbox_with_a_snapshot_is_not_restored(self):
        self.put_record(exit="saved", snaps=[("im-1", "2026-01-02T00:00:00+00:00")])
        with self.assertRaisesRegex(
            InvalidRequest,
            "dev is stopped.*of 2026-01-02T00:00:00\\+00:00, with agentix sandbox "
            "restore dev",
        ):
            self.run_core(lifecycle.running, "dev")
        self.create.aio.assert_not_called()

    def test_sandbox_that_ended_by_itself_is_recorded_and_reported(self):
        self.put_record()
        self.sandbox.poll.aio = AsyncMock(return_value=124)
        with self.assertRaisesRegex(InvalidRequest, "restore dev"):
            self.run_core(lifecycle.running, "dev")
        self.assertEqual(len(self.stored("dev")["snapshots"]), 1)

    def test_stopped_sandbox_without_snapshots_is_explained(self):
        self.put_record(exit="none")
        with self.assertRaisesRegex(InvalidRequest, "no snapshot to continue from"):
            self.run_core(lifecycle.running, "dev")

    def test_stopped_sandbox_by_id_is_reported(self):
        self.sandbox.poll.aio = AsyncMock(return_value=0)
        self.sandbox.get_tags.aio.return_value = {NAME_TAG: "dev"}
        with self.assertRaisesRegex(
            InvalidRequest, "sb-test is not running. It was dev"
        ):
            self.run_core(lifecycle.running, "sb-test")


class ReachableTests(unittest.TestCase):
    def test_waits_for_the_ssh_greeting(self):
        async def scenario():
            answers = [b"", b"SSH-2.0-test\r\n"]

            async def greet(reader, writer):
                writer.write(answers.pop(0))
                await writer.drain()
                writer.close()

            server = await asyncio.start_server(greet, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            async with server:
                await ssh.wait_until_reachable("127.0.0.1", port, "tailscale", 10)
            return answers

        self.assertEqual(asyncio.run(scenario()), [])

    def test_gives_up_with_a_hint(self):
        async def scenario():
            server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            async with server:
                await ssh.wait_until_reachable("127.0.0.1", port, "tailscale", 0)

        with self.assertRaisesRegex(OperationFailed, "on the tailnet"):
            asyncio.run(scenario())


async def _each(items):
    for item in items:
        yield item
