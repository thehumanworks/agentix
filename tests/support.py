"""Stand-ins for Modal, shared by the tests. No test contacts Modal."""

import asyncio
import contextlib
import copy
import io
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
    # Running until a test says otherwise.
    sandbox.poll.aio = AsyncMock(return_value=None)
    sandbox.get_tags.aio = AsyncMock(return_value={})
    sandbox._experimental_get_exit_snapshot.aio = AsyncMock(
        return_value=Mock(object_id="im-exit")
    )
    sandbox.snapshot_filesystem.aio = AsyncMock(
        return_value=Mock(object_id="im-manual")
    )
    return sandbox


def fake_store():
    """A Modal Dict that keeps copies, as the real one keeps pickles."""
    data = {}
    store = Mock()
    store.data = data

    def put(key, value, *, skip_if_exists=False):
        if skip_if_exists and key in data:
            return False
        data[key] = copy.deepcopy(value)
        return True

    store.hydrate.aio = AsyncMock()
    store.put.aio = AsyncMock(side_effect=put)
    store.get.aio = AsyncMock(
        side_effect=lambda key, default=None: copy.deepcopy(data.get(key, default))
    )
    store.pop.aio = AsyncMock(side_effect=lambda key, *default: data.pop(key, *default))
    store.items.aio = Mock(side_effect=lambda: _each(copy.deepcopy(list(data.items()))))
    return store


class ModalTestCase(unittest.TestCase):
    """Replace every Modal call that the core makes."""

    def setUp(self):
        # A Modal call that no stand-in replaces fails here, rather than reaching
        # the real workspace and its records.
        self._patch("modal.client._Client.from_env").side_effect = AssertionError(
            "A test tried to contact Modal."
        )
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
        self.image = self._patch("agentix.sandbox.modal.Image.from_name")
        self.snapshot_image = self._patch("agentix.sandbox.modal.Image.from_id")
        self.store = fake_store()
        self.store_lookup = self._patch("agentix.session.modal.Dict.from_name")
        self.store_lookup.return_value = self.store
        self.image_delete = self._patch(
            "agentix.sandbox.modal.experimental.image_delete"
        )
        self.image_delete.aio = AsyncMock()

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
