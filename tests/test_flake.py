import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from support import ModalTestCase

from agentix import flake
from agentix.errors import OperationFailed


def lock(**revisions):
    nodes = {name: {"locked": {"rev": rev}} for name, rev in revisions.items()}
    return json.dumps({"nodes": {"root": {}, **nodes}})


class FlakeUpdateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.lockfile = Path(directory.name) / "flake.lock"
        self.lockfile.write_text(lock(nixpkgs="a1", mods="b1"))
        self._patch("agentix.flake.NIX_DIR", Path(directory.name))
        self._patch("agentix.flake.modal.Image.from_name")
        self.run_nix = self._patch("agentix.flake.lock_in_sandbox")

    def _patch(self, target, *args):
        patcher = patch(target, *args)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_writes_the_lock_and_reports_the_inputs_that_moved(self):
        self.run_nix.return_value = lock(nixpkgs="a1", mods="b2")

        result = flake.update(Mock(), "image", ("mods",))

        self.assertEqual(result["changed"], {"mods": {"from": "b1", "to": "b2"}})
        self.assertEqual(self.run_nix.call_args.args[2:], ("update", "mods"))
        self.assertEqual(self.lockfile.read_text(), lock(nixpkgs="a1", mods="b2"))

    def test_leaves_the_lock_alone_when_nothing_moved(self):
        self.run_nix.return_value = json.dumps(
            json.loads(self.lockfile.read_text()), indent=2
        )
        original = self.lockfile.read_text()

        result = flake.update(Mock(), "image", ())

        self.assertEqual(result["changed"], {})
        self.assertEqual(self.lockfile.read_text(), original)

    def test_rejects_an_input_the_lock_does_not_have(self):
        with self.assertRaisesRegex(OperationFailed, "typo.*Inputs: mods, nixpkgs"):
            flake.update(Mock(), "image", ("typo",))

        self.run_nix.assert_not_called()


class FlakeUpdateBuildFlagTests(ModalTestCase):
    def setUp(self):
        super().setUp()
        self._patch("agentix.image.get_app")
        self.update = self._patch("agentix.flake.update")
        self.build = self._patch("agentix.image.build_image")
        self.build.return_value.object_id = "im-new"

    def test_builds_the_image_when_an_input_moved(self):
        self.update.return_value = {"changed": {"mods": {"from": "b1", "to": "b2"}}}

        output = self.run_cli("flake", "update", "mods", "--build")

        self.assertEqual(self.update.call_args.args[2], ("mods",))
        self.build.assert_called_once()
        self.assertIn("im-new", output)

    def test_skips_the_build_when_nothing_moved(self):
        self.update.return_value = {"changed": {}}

        self.run_cli("flake", "update", "--build")

        self.build.assert_not_called()

    def test_does_not_build_unless_asked(self):
        self.update.return_value = {"changed": {"mods": {"from": "b1", "to": "b2"}}}

        self.run_cli("flake", "update")

        self.build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
