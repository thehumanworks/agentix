import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import modal
import uvicorn
from support import ModalTestCase

from agentix import backend, server
from agentix.client import Remote
from agentix.errors import (
    InvalidRequest,
    NotFound,
    OperationFailed,
    PublicKeyRequired,
    ServerUnavailable,
)
from agentix.local import Local


class ServerTestCase(ModalTestCase):
    """Run the real server on a Unix socket and call it with the real client."""

    def setUp(self):
        super().setUp()
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        self.socket_path = os.path.join(directory, "server.sock")
        self.server = uvicorn.Server(
            uvicorn.Config(
                server.create_app(),
                uds=self.socket_path,
                log_level="warning",
            )
        )
        thread = threading.Thread(target=self.server.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(setattr, self.server, "should_exit", True)
        deadline = time.monotonic() + 10
        while not self.server.started:
            self.assertLess(time.monotonic(), deadline, "The server did not start.")
            time.sleep(0.01)
        self.remote = Remote(self.socket_path)
        self.addCleanup(self.remote.close)


class ServerTests(ServerTestCase):
    def test_sandbox_operations(self):
        created = self.remote.create_sandbox(
            "image:tag", "", 600, "/srv/project", 16384, 4
        )
        self.assertEqual(created, {"sandbox_id": "sb-test"})
        options = self.create.aio.call_args.kwargs
        self.assertEqual(
            (options["timeout"], options["workdir"]), (600, "/srv/project")
        )
        self.assertEqual((options["memory"], options["cpu"]), (16384, 4))
        self.assertEqual(self.remote.list_sandboxes(), ["sb-test"])
        self.assertEqual(
            self.remote.terminate("sb-test"), {"id": "sb-test", "status": "terminated"}
        )

    def test_failed_command_is_a_result(self):
        self.sandbox.process.stderr.read.aio.return_value = "no such file"
        self.sandbox.process.wait.aio.return_value = 2
        self.assertEqual(
            self.remote.execute("sb-test", ["cat", "a b"]),
            {"stdout": "", "stderr": "no such file", "exit_code": 2},
        )
        self.sandbox.exec.aio.assert_called_once_with("cat", "a b")

    def test_requests_share_the_handle_and_the_app(self):
        for _ in range(3):
            self.remote.execute("sb-test", ["true"])
            self.remote.list_sandboxes()
        self.lookup.aio.assert_called_once_with(sandbox_id="sb-test")
        self.app_lookup.aio.assert_called_once()

    def test_new_sandbox_is_ready_when_the_request_returns(self):
        self.remote.create_sandbox("image:tag", "", 600, "", 8192, 0)
        self.sandbox.wait_until_ready.aio.assert_awaited_once()

    def test_ssh_operations_and_field_order(self):
        self.sandbox.tunnels.aio.return_value = {
            22: Mock(tcp_socket=("example.modal.host", 12345))
        }
        self.sandbox.process.stdout.read.aio.return_value = (
            '{"status":"running","host_key_fingerprint":"SHA256:abc"}'
        )
        with patch("agentix.ssh.check_public_key", side_effect=lambda key: key):
            started = self.remote.ssh(
                "start", "sb-test", "openssh", "", "ssh-ed25519 k"
            )
        self.assertEqual(
            self.sandbox.exec.aio.call_args.args[-2:], ("start", "ssh-ed25519 k")
        )
        self.assertEqual(
            started,
            {
                "sandbox_id": "sb-test",
                "engine": "openssh",
                "status": "running",
                "host_key_fingerprint": "SHA256:abc",
                "host": "example.modal.host",
                "port": 12345,
                "user": "root",
                "ssh_command": "ssh -p 12345 root@example.modal.host",
            },
        )
        self.assertEqual(list(started)[:3], ["sandbox_id", "engine", "status"])
        for action in ("status", "stop"):
            self.assertEqual(self.remote.ssh(action, "sb-test", "openssh"), started)
            self.assertEqual(self.sandbox.exec.aio.call_args.args[-2], action)

    def test_errors_keep_their_kind_and_message(self):
        with self.assertRaisesRegex(InvalidRequest, "Unsupported SSH engine: bad"):
            self.remote.ssh("status", "sb-test", "bad")
        with self.assertRaisesRegex(InvalidRequest, "args"):
            self.remote.execute("sb-test", [])
        self.sandbox.process.wait.aio.return_value = 1
        self.sandbox.process.stderr.read.aio.return_value = (
            "An SSH public key is required."
        )
        self.sandbox.tunnels.aio.return_value = {22: Mock(tcp_socket=("host", 1))}
        with self.assertRaises(PublicKeyRequired):
            self.remote.ssh("start", "sb-test", "openssh")
        self.sandbox.process.stderr.read.aio.return_value = "Enrollment failed"
        with self.assertRaisesRegex(OperationFailed, "Enrollment failed"):
            self.remote.ssh("start", "sb-test", "tailscale")

    def test_modal_errors_are_reported(self):
        self.lookup.aio.side_effect = modal.exception.NotFoundError("No such sandbox")
        with self.assertRaisesRegex(NotFound, "No such sandbox"):
            self.remote.execute("sb-missing", ["true"])
        self.lookup.aio.side_effect = modal.exception.AuthError("Token missing")
        with self.assertRaisesRegex(OperationFailed, "AuthError: Token missing"):
            self.remote.execute("sb-test", ["true"])

    def test_command_line_uses_the_server(self):
        self.sandbox.process.stdout.read.aio.return_value = "from the server"
        with (
            patch.dict(os.environ, {"AGENTIX_SERVER": self.socket_path}),
            patch("agentix.backend._local", side_effect=AssertionError),
        ):
            output = self.run_cli("sandbox", "exec", "sb-test", "echo 'a b'")
        self.assertIn("from the server", output)
        self.sandbox.exec.aio.assert_called_once_with("echo", "a b")

    def test_second_server_on_the_socket_is_refused(self):
        with self.assertRaisesRegex(InvalidRequest, "already listens"):
            server._listen(self.socket_path)
        self.assertEqual(self.remote.list_sandboxes(), ["sb-test"])


class ConnectTests(ServerTestCase):
    def connect(self, **environment):
        with patch.dict(os.environ, environment):
            return backend.connect()

    def test_named_server_is_used(self):
        self.assertIsInstance(self.connect(AGENTIX_SERVER=self.socket_path), Remote)

    def test_named_server_that_does_not_answer_is_an_error(self):
        with self.assertRaises(ServerUnavailable):
            self.connect(AGENTIX_SERVER=self.socket_path + ".missing")

    def test_off_works_in_this_process(self):
        self.assertIsInstance(self.connect(AGENTIX_SERVER="off"), Local)

    def test_default_socket_is_used_only_if_a_server_answers(self):
        del os.environ["AGENTIX_SERVER"]
        with patch("agentix.backend.SERVER_SOCKET", self.socket_path):
            self.assertIsInstance(backend.connect(), Remote)
        with patch("agentix.backend.SERVER_SOCKET", self.socket_path + ".missing"):
            self.assertIsInstance(backend.connect(), Local)


class ServeTests(unittest.TestCase):
    def test_socket_is_private_and_replaces_one_left_behind(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, "private", "server.sock")
        for _ in range(2):
            listener = server._listen(path)
            listener.close()
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)

    def test_socket_name_without_a_directory_is_accepted(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(directory)
        server._listen("server.sock").close()
        self.assertEqual(os.stat("server.sock").st_mode & 0o777, 0o600)

    def test_other_file_at_the_socket_path_is_kept(self):
        with (
            tempfile.NamedTemporaryFile() as other,
            self.assertRaisesRegex(InvalidRequest, "Cannot listen"),
        ):
            server._listen(other.name)


if __name__ == "__main__":
    unittest.main()
