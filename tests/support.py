"""Stand-ins for Modal, shared by the tests. No test contacts Modal."""

import asyncio
import contextlib
import io
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fire import Fire

from agentix.__main__ import main
from agentix.session import Session


def fake_sandbox(sandbox_id="sb-test", stdout="", stderr="", exit_code=0, tunnels=None):
    sandbox = Mock()
    sandbox.object_id = sandbox_id
    process = Mock()
    process.stdout.read.aio = AsyncMock(return_value=stdout)
    process.stderr.read.aio = AsyncMock(return_value=stderr)
    process.wait.aio = AsyncMock(return_value=exit_code)
    sandbox.process = process
    sandbox.exec.aio = AsyncMock(return_value=process)
    sandbox.tunnels.aio = AsyncMock(return_value=tunnels or {})
    sandbox.wait_until_ready.aio = AsyncMock()
    sandbox.terminate.aio = AsyncMock()
    sandbox.detach.aio = AsyncMock()
    return sandbox


class ModalTestCase(unittest.TestCase):
    """Replace every Modal call that the core makes."""

    def setUp(self):
        self.sandbox = fake_sandbox()
        self.app_lookup = self._patch("agentix.session.modal.App.lookup")
        self.app_lookup.aio = AsyncMock(return_value=Mock(app_id="ap-test"))
        self.lookup = self._patch("agentix.session.modal.Sandbox.from_id")
        self.lookup.aio = AsyncMock(return_value=self.sandbox)
        self.create = self._patch("agentix.sandbox.modal.Sandbox.create")
        self.create.aio = AsyncMock(return_value=self.sandbox)
        self.listing = self._patch("agentix.sandbox.modal.Sandbox.list")
        self.listing.aio = Mock(side_effect=lambda **_: _each([self.sandbox]))
        self.secret = self._patch("agentix.sandbox.modal.Secret.from_name")
        self._patch("agentix.sandbox.modal.Image.from_name")
        # The command line must not find a server of the person who runs the tests.
        environment = patch.dict(os.environ, {"AGENTIX_SERVER": "off"})
        environment.start()
        self.addCleanup(environment.stop)

    def _patch(self, target):
        patcher = patch(target)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def run_core(self, operation, *args, **kwargs):
        """Run one core operation with a new session, as a one-shot caller does."""

        async def run():
            return await operation(Session(), *args, **kwargs)

        return asyncio.run(run())

    def run_cli(self, *command):
        real_fire = Fire
        with (
            patch(
                "fire.Fire",
                side_effect=lambda commands: real_fire(commands, command=list(command)),
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            main()
        return output.getvalue()


async def _each(items):
    for item in items:
        yield item
