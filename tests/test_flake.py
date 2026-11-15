import io
import json
import tarfile
import tempfile
import unittest
import urllib.error
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

    def test_checks_the_new_lock_before_writing_it(self):
        self.run_nix.return_value = lock(nixpkgs="a2", mods="b1")

        flake.update(Mock(), "image", ())

        self.assertTrue(self.run_nix.call_args.kwargs["check"])
        self.assertIsNone(self.run_nix.call_args.kwargs["flake_nix"])

    def test_a_lock_that_fails_the_check_is_not_written(self):
        self.run_nix.side_effect = OperationFailed("nix eval failed")
        original = self.lockfile.read_text()

        with self.assertRaisesRegex(OperationFailed, "nix eval failed"):
            flake.update(Mock(), "image", ())

        self.assertEqual(self.lockfile.read_text(), original)


REX = "https://pkgs.superlogical.com/rex/nix/snapshot"
FLAKE_NIX = f"""{{
  inputs = {{
    # The channel, {REX}.tar.gz, serves the newest.
    rex = {{
      url = "{REX}/0.0.1-gaaaaaa.tar.gz";
      inputs.nixpkgs.follows = "nixpkgs";
    }};
  }};
}}
"""


def rex_lock(url, nar_hash, original=None):
    nodes = json.loads(lock(nixpkgs="a1"))["nodes"]
    nodes["rex"] = {
        "locked": {"narHash": nar_hash, "type": "tarball", "url": url},
        "original": {"type": "tarball", "url": original or url},
    }
    return json.dumps({"nodes": nodes})


class RexUpdateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.nix_dir = Path(directory.name)
        (self.nix_dir / "flake.nix").write_text(FLAKE_NIX)
        (self.nix_dir / "flake.lock").write_text(
            rex_lock(f"{REX}/0.0.1-gaaaaaa.tar.gz", "h1")
        )
        self._patch("agentix.flake.NIX_DIR", self.nix_dir)
        self._patch("agentix.flake.modal.Image.from_name")
        self.run_nix = self._patch("agentix.flake.lock_in_sandbox")
        # What the channel and each versioned tarball say they hold.
        self.served = {
            f"{REX}.tar.gz": "0.0.2-gbbbbbb",
            f"{REX}/0.0.2-gbbbbbb.tar.gz": "0.0.2-gbbbbbb",
        }
        self.rex_version = self._patch(
            "agentix.flake._rex_version", side_effect=self._served
        )

    def _patch(self, target, *args, **kwargs):
        patcher = patch(target, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _served(self, url):
        version = self.served.get(url)
        if version is None:
            raise OperationFailed(f"no Rex version at {url}: HTTP Error 404")
        return version

    def _files(self):
        return [
            (self.nix_dir / name).read_text() for name in ("flake.nix", "flake.lock")
        ]

    def test_naming_rex_points_flake_nix_at_the_newest_snapshot(self):
        new = f"{REX}/0.0.2-gbbbbbb.tar.gz"
        self.run_nix.return_value = rex_lock(new, "h2")

        result = flake.update(Mock(), "image", ("rex",))

        self.assertEqual(
            result["rex"], {"from": "0.0.1-gaaaaaa", "to": "0.0.2-gbbbbbb"}
        )
        self.assertEqual(result["changed"], {"rex": {"from": "h1", "to": "h2"}})
        moved = FLAKE_NIX.replace(f"{REX}/0.0.1-gaaaaaa.tar.gz", new)
        self.assertEqual(self.run_nix.call_args.args[2:], ("update", "rex"))
        self.assertEqual(self.run_nix.call_args.kwargs["flake_nix"], moved)
        self.assertEqual(self._files(), [moved, rex_lock(new, "h2")])

    def test_other_updates_leave_rex_where_it_is(self):
        self.run_nix.return_value = rex_lock(f"{REX}/0.0.1-gaaaaaa.tar.gz", "h1")

        flake.update(Mock(), "image", ())
        flake.update(Mock(), "image", ("nixpkgs",))

        self.rex_version.assert_not_called()
        self.assertIsNone(self.run_nix.call_args.kwargs["flake_nix"])
        self.assertEqual(self._files()[0], FLAKE_NIX)

    def test_a_lock_that_names_another_rex_download_is_not_written(self):
        # As when a server answers with a Link header naming another snapshot.
        self.run_nix.return_value = rex_lock(
            f"{REX}/0.0.1-gaaaaaa.tar.gz", "h2", original=f"{REX}/0.0.2-gbbbbbb.tar.gz"
        )
        original = self._files()

        with self.assertRaisesRegex(OperationFailed, "Nix locked Rex as"):
            flake.update(Mock(), "image", ("rex",))

        self.assertEqual(self._files(), original)

    def test_a_failed_check_writes_neither_file(self):
        self.run_nix.side_effect = OperationFailed("nix eval failed")
        original = self._files()

        with self.assertRaisesRegex(OperationFailed, "nix eval failed"):
            flake.update(Mock(), "image", ("rex",))

        self.assertEqual(self._files(), original)

    def test_waits_for_the_versioned_tarball_of_the_newest_snapshot(self):
        del self.served[f"{REX}/0.0.2-gbbbbbb.tar.gz"]
        original = self._files()

        with self.assertRaisesRegex(OperationFailed, "serves Rex 0.0.2.*try again"):
            flake.update(Mock(), "image", ("rex",))

        self.run_nix.assert_not_called()
        self.assertEqual(self._files(), original)

    def test_refuses_a_versioned_tarball_that_holds_another_version(self):
        self.served[f"{REX}/0.0.2-gbbbbbb.tar.gz"] = "0.0.1-gaaaaaa"

        with self.assertRaisesRegex(OperationFailed, "holds Rex 0.0.1-gaaaaaa"):
            flake.update(Mock(), "image", ("rex",))

        self.run_nix.assert_not_called()

    def test_flake_nix_must_name_one_versioned_rex_url(self):
        for text in (
            FLAKE_NIX.replace("/0.0.1-gaaaaaa.tar.gz", ".tar.gz"),
            FLAKE_NIX + FLAKE_NIX,
        ):
            (self.nix_dir / "flake.nix").write_text(text)
            with self.assertRaisesRegex(OperationFailed, "must name Rex once"):
                flake.update(Mock(), "image", ("rex",))

        self.run_nix.assert_not_called()


def flake_tarball(sources):
    """A Rex flake tarball, with no sources.json when SOURCES is None."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        name, data = "rex/flake.nix", b"{ }"
        if sources is not None:
            name, data = "rex/sources.json", json.dumps(sources).encode()
        info = tarfile.TarInfo(name)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class RexVersionTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("agentix.flake.urllib.request.urlopen")
        self.addCleanup(patcher.stop)
        self.urlopen = patcher.start()

    def _serve(self, body):
        self.urlopen.side_effect = lambda url, timeout: io.BytesIO(body)

    def test_reads_the_version_from_sources_json(self):
        self._serve(flake_tarball({"version": "0.0.1794719511-gf91cd7"}))

        self.assertEqual(flake._rex_version(f"{REX}.tar.gz"), "0.0.1794719511-gf91cd7")

    def test_rejects_a_version_that_could_change_flake_nix(self):
        for sources in (
            {"version": '1.0.tar.gz"; x = "'},
            {"version": ""},
            {"version": "../1.0"},
            {"version": 7},
            ["version"],
        ):
            self._serve(flake_tarball(sources))
            with self.assertRaisesRegex(OperationFailed, "unexpected Rex version"):
                flake._rex_version(f"{REX}.tar.gz")

    def test_reports_what_it_could_not_read(self):
        for body in (b"Blob not found", flake_tarball(None)):
            self._serve(body)
            with self.assertRaisesRegex(OperationFailed, "no Rex version at"):
                flake._rex_version(f"{REX}.tar.gz")
        self.urlopen.side_effect = urllib.error.HTTPError(
            f"{REX}.tar.gz", 404, "Not Found", None, None
        )
        with self.assertRaisesRegex(OperationFailed, "no Rex version at.*404"):
            flake._rex_version(f"{REX}.tar.gz")


class LockInSandboxTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        nix_dir = Path(directory.name)
        (nix_dir / "flake.nix").write_text("{ }")
        (nix_dir / "home.nix").write_text("{ }")
        patcher = patch("agentix.flake.NIX_DIR", nix_dir)
        self.addCleanup(patcher.stop)
        patcher.start()
        patcher = patch("agentix.flake.modal.Sandbox.create")
        self.addCleanup(patcher.stop)
        self.sandbox = patcher.start().return_value
        self.process = self.sandbox.exec.return_value
        self.process.stdout.read.return_value = ""
        self.process.stderr.read.return_value = ""
        self.process.wait.return_value = 0
        self.sandbox.filesystem.read_text.return_value = "{}"

    def test_sends_all_of_nix_and_evaluates_the_profile_with_the_new_lock(self):
        flake.lock_in_sandbox(
            Mock(), Mock(), "update", "rex", flake_nix="new", check=True
        )

        archive = self.sandbox.filesystem.write_bytes.call_args.args[0]
        with tarfile.open(fileobj=io.BytesIO(archive)) as sent:
            self.assertEqual(
                sorted(sent.getnames()), [".", "./flake.nix", "./home.nix"]
            )
        self.sandbox.filesystem.write_text.assert_called_once_with(
            "new", "/tmp/agentix-flake/flake.nix"
        )
        commands = [call.args for call in self.sandbox.exec.call_args_list]
        self.assertEqual(
            commands[1:],
            [
                ("nix", "flake", "update", "rex"),
                (
                    "nix",
                    "eval",
                    "--raw",
                    ".#homeConfigurations.modal.activationPackage.drvPath",
                ),
            ],
        )
        self.sandbox.terminate.assert_called_once()

    def test_a_failed_command_fails_and_stops_the_sandbox(self):
        self.process.wait.side_effect = [0, 0, 1]

        with self.assertRaisesRegex(OperationFailed, "nix eval --raw .* failed"):
            flake.lock_in_sandbox(Mock(), Mock(), "update", check=True)

        self.sandbox.terminate.assert_called_once()


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
