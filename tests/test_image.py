import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import modal
from support import ModalTestCase

from agentix import image, layout
from agentix.errors import OperationFailed

ROOT = Path(__file__).resolve().parents[1]


class BuilderPinTests(ModalTestCase):
    def test_the_builder_is_a_nix_release_pinned_by_digest(self):
        self.assertRegex(
            image.NIX_BUILDER, r"^nixos/nix:[0-9.]+-amd64@sha256:[0-9a-f]{64}$"
        )

    def test_ci_installs_the_release_the_builder_runs(self):
        """.github/actions/nix reads the release from this line of image.py."""
        source = (ROOT / "src/agentix/image.py").read_text()
        (version,) = re.findall(r'^ *"nixos/nix:([0-9][0-9.]*)-amd64"$', source, re.M)
        self.assertTrue(image.NIX_BUILDER.startswith(f"nixos/nix:{version}-amd64@"))
        action = (ROOT / ".github/actions/nix/action.yml").read_text()
        self.assertIn('"nixos/nix:\\([0-9][0-9.]*\\)-amd64"', action)
        self.assertIn(
            "releases.nixos.org/nix/nix-${{ steps.pin.outputs.version }}", action
        )

    def test_flakes_are_on_in_the_builder(self):
        from_registry = self._patch("agentix.image.modal.Image.from_registry")
        image.builder_image()
        from_registry.assert_called_once_with(image.NIX_BUILDER)
        (env,) = from_registry.return_value.env.call_args.args
        self.assertIn("flakes", env["NIX_CONFIG"])


class SourceArchiveTests(ModalTestCase):
    """What the build sandbox receives, and the cache key made from it."""

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.nix = Path(directory.name) / "nix"
        shutil.copytree(layout.NIX_DIR, self.nix, symlinks=True)
        patcher = patch("agentix.image.NIX_DIR", self.nix)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_archive_holds_the_flake_and_its_lock(self):
        archive = tarfile.open(fileobj=io.BytesIO(image._source_archive()))
        names = archive.getnames()
        for name in ("flake.nix", "flake.lock", "sandbox.toml", "oci/default.nix"):
            self.assertIn(name, names)
        self.assertEqual({member.mtime for member in archive}, {0})

    def test_the_key_follows_the_contents_and_not_the_times(self):
        key = image._cache_key(image._source_archive())
        (self.nix / "flake.nix").touch()
        self.assertEqual(image._cache_key(image._source_archive()), key)
        with (self.nix / "flake.nix").open("a") as file:
            file.write("\n")
        self.assertNotEqual(image._cache_key(image._source_archive()), key)

    def test_the_key_follows_the_builder_and_the_recipe(self):
        source = image._source_archive()
        key = image._cache_key(source)
        with patch("agentix.image.NIX_BUILDER", "nixos/nix:0-amd64@sha256:" + "0" * 64):
            self.assertNotEqual(image._cache_key(source), key)
        with patch("agentix.image._BUILD", image._BUILD + "true\n"):
            self.assertNotEqual(image._cache_key(source), key)


class BuildImageTests(ModalTestCase):
    """The steps of `image build`, read back from stand-ins for modal.Image."""

    def setUp(self):
        super().setUp()
        self._patch("agentix.image.get_app")
        self._patch("agentix.image._ensure_lockfile")
        self._patch("agentix.image.modal.enable_output")
        self._patch("agentix.image._source_archive").return_value = b"source"
        self.find = self._patch("agentix.image._find_rootfs")
        self.build_rootfs = self._patch("agentix.image._build_rootfs")
        self.from_id = self._patch("agentix.image.modal.Image.from_id")
        self.from_registry = self._patch("agentix.image.modal.Image.from_registry")
        # Each step returns the stand-in for the next one.
        self.with_env = self.from_id.return_value.env.return_value
        self.with_workdir = self.with_env.workdir.return_value
        self.published = self.with_workdir.cmd.return_value
        self.checked = self.published.run_commands.return_value

    def test_a_cached_root_is_used_without_building(self):
        self.find.return_value = Mock(object_id="im-root")

        result = image.build_image(image_name="scratch:latest")

        self.find.assert_called_once_with(
            f"{image.ROOTFS_CACHE}:{image._cache_key(b'source')}"
        )
        self.build_rootfs.assert_not_called()
        self.from_id.assert_called_once_with("im-root")
        self.assertIs(result, self.published)
        self.published.publish.assert_called_once_with(name="scratch:latest")

    def test_a_new_root_is_built_and_kept_under_its_key(self):
        self.find.return_value = None
        root = self.build_rootfs.return_value
        root.object_id = "im-new"

        image.build_image(image_name="scratch:latest")

        self.build_rootfs.assert_called_once_with(b"source")
        root.publish.assert_called_once_with(
            name=f"{image.ROOTFS_CACHE}:{image._cache_key(b'source')}"
        )
        self.from_id.assert_called_once_with("im-new")

    def test_a_deleted_cached_root_is_built_again(self):
        """image_delete leaves the name, and Modal refuses to build on the root."""
        self.find.return_value = Mock(object_id="im-deleted")
        self.build_rootfs.return_value.object_id = "im-new"
        self.checked.build.side_effect = [modal.exception.NotFoundError("gone"), None]

        result = image.build_image(image_name="scratch:latest")

        self.build_rootfs.assert_called_once_with(b"source")
        self.build_rootfs.return_value.publish.assert_called_once()
        self.assertEqual(
            [call.args for call in self.from_id.call_args_list],
            [("im-deleted",), ("im-new",)],
        )
        self.assertIs(result, self.published)
        self.published.publish.assert_called_once_with(name="scratch:latest")

    def test_force_build_skips_the_cache(self):
        image.build_image(force_build=True, image_name="scratch:latest")

        self.find.assert_not_called()
        self.build_rootfs.assert_called_once()
        self.assertTrue(self.published.run_commands.call_args.kwargs["force_build"])

    def test_the_image_has_the_shared_environment_and_working_directory(self):
        image.build_image(image_name="scratch:latest")

        self.from_id.return_value.env.assert_called_once_with(layout.IMAGE_ENV)
        self.with_env.workdir.assert_called_once_with(layout.WORKDIR)
        # A sandbox started without a command stays up.
        self.with_workdir.cmd.assert_called_once_with(["sleep", "infinity"])
        self.with_workdir.entrypoint.assert_not_called()

    def test_the_checks_run_in_a_layer_that_is_not_published(self):
        image.build_image(image_name="scratch:latest")

        self.assertEqual(
            list(self.published.run_commands.call_args.args), image._checks()
        )
        self.checked.build.assert_called_once()
        self.checked.publish.assert_not_called()
        self.published.publish.assert_called_once()

    def test_no_base_image_and_no_registry(self):
        image.build_image(image_name="scratch:latest")

        self.from_registry.assert_not_called()


class BuildRootfsTests(ModalTestCase):
    """The build sandbox, with a stand-in for modal.Sandbox."""

    def setUp(self):
        super().setUp()
        self._patch("agentix.image.get_app")
        self.builder = self._patch("agentix.image.builder_image")
        self.sandbox = Mock(object_id="sb-build")
        self.sandbox.exec.return_value.stdout = iter(["building\n"])
        self.sandbox.exec.return_value.wait.return_value = 0
        self.create = self._patch("agentix.image.modal.Sandbox.create")
        self.create.return_value = self.sandbox

    def test_builds_in_a_vm_from_the_pinned_nix_and_keeps_the_snapshot(self):
        root = image._build_rootfs(b"source")

        kwargs = self.create.call_args.kwargs
        self.assertEqual(kwargs["runtime"], "vm")
        self.assertIs(kwargs["image"], self.builder.return_value)
        self.sandbox.filesystem.write_bytes.assert_called_once_with(
            b"source", f"{image._SOURCE}.tar"
        )
        self.assertEqual(
            self.sandbox.exec.call_args.args[:3], ("bash", "-c", image._BUILD)
        )
        self.sandbox.snapshot_directory.assert_called_once()
        self.assertEqual(
            self.sandbox.snapshot_directory.call_args.args, (image._ROOTFS,)
        )
        # The published image is built on it, so it must not expire.
        self.assertIsNone(self.sandbox.snapshot_directory.call_args.kwargs["ttl"])
        self.assertIs(root, self.sandbox.snapshot_directory.return_value)
        self.sandbox.terminate.assert_called_once()

    def test_a_failed_build_ends_the_sandbox_and_saves_nothing(self):
        self.sandbox.exec.return_value.wait.return_value = 1

        with self.assertRaisesRegex(OperationFailed, "sb-build"):
            image._build_rootfs(b"source")

        self.sandbox.snapshot_directory.assert_not_called()
        self.sandbox.terminate.assert_called_once()

    def test_the_recipe_builds_the_flakes_oci_image_into_the_snapshot_directory(self):
        self.assertIn(f"path:{image._SOURCE}#packages.x86_64-linux.oci", image._BUILD)
        self.assertIn(f"-C {image._ROOTFS}", image._BUILD)


class SharedEnvironmentTests(ModalTestCase):
    def test_the_oci_image_takes_its_environment_from_the_same_file(self):
        shared = tomllib.loads((ROOT / "nix/sandbox.toml").read_text())
        self.assertEqual(layout.IMAGE_ENV, shared["env"])
        oci = (ROOT / "nix/oci/default.nix").read_text()
        self.assertIn("shared = lib.importTOML ../sandbox.toml;", oci)
        self.assertIn("Env = lib.mapAttrsToList", oci)
        self.assertIn("shared.env", oci)
        self.assertIn("/root/.nix-profile/bin", layout.IMAGE_ENV["PATH"])


@unittest.skipUnless(shutil.which("jq"), "jq reads the manifest in these checks")
class ModuleCheckTests(unittest.TestCase):
    """Checks of a module that a profile may turn off follow the manifest."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manifest = Path(directory.name) / "manifest.json"
        patcher = patch("agentix.image.MANIFEST_FILE", str(self.manifest))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_check(self, commands, check):
        self.manifest.write_text(json.dumps({"commands": commands}))
        return subprocess.run(
            ["bash", "-c", image._if_listed("sshd", check)],
            capture_output=True,
            text=True,
        )

    def test_the_check_runs_when_the_manifest_lists_the_command(self):
        result = self.run_check(["ssh", "sshd"], "echo ran; exit 3")
        self.assertEqual((result.returncode, result.stdout), (3, "ran\n"))

    def test_the_check_is_skipped_when_the_module_is_off(self):
        result = self.run_check(["ts", "tailscale"], "echo ran; exit 3")
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_openssh_checks_run_only_with_openssh(self):
        checks = image._checks()
        self.assertNotIn("id sshd > /dev/null", checks)
        # sshd -t fails without the sshd account or /var/empty.
        (sshd,) = [check for check in checks if "-t -f /dev/null" in check]
        self.assertTrue(sshd.startswith("if jq -e --arg name sshd "))
        # Rex's ssh guard is checked with the client that OpenSSH installs.
        (guard,) = [check for check in checks if "ssh -G" in check]
        self.assertTrue(guard.startswith("if jq -e --arg name ssh "))
