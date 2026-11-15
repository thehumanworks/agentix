"""Stand-ins for Modal, shared by the tests. No test contacts Modal."""

import asyncio
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import modal
from modal.volume import FileEntry, FileEntryType

from agentix import session as session_module
from agentix import store
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


def fake_volume():
    """A v2 Modal Volume as its client methods see it, with files in `files`.

    Paths in `files` have no leading slash, as in Modal's listings.
    """
    files: dict[str, bytes] = {}
    volume = Mock()
    volume.files = files

    async def listdir(path, recursive=False):
        prefix = path.strip("/") + "/"
        found = [
            key
            for key in sorted(files)
            if key.startswith(prefix) and (recursive or "/" not in key[len(prefix) :])
        ]
        if not found:
            raise modal.exception.NotFoundError(f'path "{path}" does not exist')
        directories = sorted(
            {key.rsplit("/", 1)[0] for key in found} - {prefix.rstrip("/")}
        )
        return [
            FileEntry(path=key, type=FileEntryType.DIRECTORY, mtime=0, size=0)
            for key in directories
        ] + [
            FileEntry(path=key, type=FileEntryType.FILE, mtime=0, size=len(files[key]))
            for key in found
        ]

    async def read_file(path):
        key = path.lstrip("/")
        if key not in files:
            raise FileNotFoundError(f'path "{path}" does not exist')
        yield files[key]

    async def remove_file(path, recursive=False):
        key = path.lstrip("/")
        if key not in files:
            raise FileNotFoundError(f'path "{path}" does not exist')
        del files[key]

    @contextlib.asynccontextmanager
    async def batch_upload(force=False):
        staged = {}
        batch = Mock()
        batch.put_file.side_effect = lambda data, path: staged.__setitem__(
            path.lstrip("/"), data.read()
        )
        yield batch
        for key in staged:
            if not force and key in files:
                # What a v2 Volume raises, rather than FileExistsError.
                raise modal.exception.InvalidError(f'path "/{key}" already exists')
        files.update(staged)

    volume.hydrate.aio = AsyncMock()
    volume.listdir.aio = AsyncMock(side_effect=listdir)
    volume.read_file.aio = Mock(side_effect=read_file)
    volume.remove_file.aio = AsyncMock(side_effect=remove_file)
    volume.batch_upload.aio = Mock(side_effect=batch_upload)
    return volume


class ModalTestCase(unittest.TestCase):
    """Replace every Modal call that the core makes."""

    def setUp(self):
        # No test reads the config file of whoever runs the tests.
        config_home = tempfile.TemporaryDirectory()
        self.addCleanup(config_home.cleanup)
        environ = patch.dict(os.environ, {"XDG_CONFIG_HOME": config_home.name})
        environ.start()
        self.addCleanup(environ.stop)
        self.config_home = config_home.name
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
        self.volume = fake_volume()
        self.volume_lookup = self._patch("agentix.session.modal.Volume.from_name")
        self.volume_lookup.return_value = self.volume
        self.cache = fake_store()
        # The Dict of older versions. Present but empty unless a test fills it.
        self.legacy = fake_store()
        self.dict_lookup = self._patch("agentix.session.modal.Dict.from_name")
        self.dict_lookup.side_effect = lambda name, **_: {
            session_module.CACHE_NAME: self.cache,
            session_module.LEGACY_STORE_NAME: self.legacy,
        }[name]
        self.image_delete = self._patch(
            "agentix.sandbox.modal.experimental.image_delete"
        )
        self.image_delete.aio = AsyncMock()

    def _patch(self, target):
        patcher = patch(target)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def without_legacy(self):
        """A workspace where older versions never kept records."""
        self.legacy.hydrate.aio.side_effect = modal.exception.NotFoundError("none")

    def write_record(self, record, file="00000000T000000-test.json"):
        """Store `record` as one file, as `recordfiles.write` would."""
        path = f"records/{record['name']}/{file}"
        self.volume.files[path] = json.dumps(record).encode()
        return path

    def stored(self, name):
        """The record of `name` that the files on the Volume hold, or None."""
        record = None
        for path, data in sorted(self.volume.files.items()):
            if path.startswith(f"records/{name}/"):
                record = store.merge(record, json.loads(data))
        if record is None or not (record["sandboxes"] or record["snapshots"]):
            return None
        return record

    def files_of(self, name):
        return sorted(p for p in self.volume.files if p.startswith(f"records/{name}/"))

    def run_core(self, operation, *args, **kwargs):
        """Run one core operation with a new session, as a one-shot caller does."""

        async def run():
            return await operation(Session(), *args, **kwargs)

        return asyncio.run(run())

    def run_cli(self, *command):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            main(list(command))
        return output.getvalue()


async def _each(items):
    for item in items:
        yield item
