from support import ModalTestCase

from agentix import image


class ImageBaseTests(ModalTestCase):
    """What the build starts from, read back from a stand-in for modal.Image."""

    def setUp(self):
        super().setUp()
        self._patch("agentix.image.get_app")
        self._patch("agentix.image._ensure_lockfile")
        self._patch("agentix.image.modal.enable_output")
        self.debian_slim = self._patch("agentix.image.modal.Image.debian_slim")
        self.from_registry = self._patch("agentix.image.modal.Image.from_registry")
        # Every step returns the same stand-in, which records them all.
        self.steps = self.from_registry.return_value
        for step in ("env", "run_commands", "dockerfile_commands", "add_local_dir"):
            getattr(self.steps, step).return_value = self.steps
        self.steps.workdir.return_value = self.steps
        self.steps.build.return_value = self.steps
        image.build_image(image_name="scratch:latest")
        self.commands = [
            command
            for call in self.steps.run_commands.call_args_list
            for command in call.args
        ]

    def test_the_base_image_is_pinned_by_digest(self):
        self.assertRegex(image.BASE_IMAGE, r"^[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}$")
        self.assertEqual(self.from_registry.call_args.args, (image.BASE_IMAGE,))
        self.debian_slim.assert_not_called()

    def test_nothing_is_installed_with_apt(self):
        self.steps.apt_install.assert_not_called()
        self.assertFalse([command for command in self.commands if "apt" in command])

    def test_the_installer_runs_only_after_its_digest_matches(self):
        self.assertRegex(image.NIX_INSTALLER_SHA256, r"^[0-9a-f]{64}$")
        self.assertIn(image.NIX_INSTALLER_VERSION, image.NIX_INSTALLER_URL)
        (install,) = [c for c in self.commands if image.NIX_INSTALLER_URL in c]
        check = f"echo '{image.NIX_INSTALLER_SHA256}  /tmp/nix-installer' | "
        self.assertLess(
            install.index(check), install.index("/tmp/nix-installer install")
        )
        # The binary itself, not a script piped into a shell.
        self.assertNotIn("sh -s", install)
