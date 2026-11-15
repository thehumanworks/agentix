import json
import re
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from agentix import image, layout, sandbox, ssh

ROOT = Path(__file__).resolve().parents[1]
SHARED = tomllib.loads((ROOT / "nix/sandbox.toml").read_text())
WORKDIR_PROFILE = ROOT / "nix/home-manager/modules/scripts/workdir-profile.sh"


class SharedFileTests(unittest.TestCase):
    def test_paths_come_from_the_file_nix_reads(self):
        self.assertEqual(layout.PATHS, SHARED["paths"])
        self.assertEqual(
            (
                layout.WORKDIR,
                layout.OP_TOKEN_FILE,
                layout.WORKDIR_FILE,
                layout.READY_FILE,
            ),
            (
                SHARED["paths"]["workdir"],
                SHARED["paths"]["opTokenFile"],
                SHARED["paths"]["workdirFile"],
                SHARED["paths"]["readyFile"],
            ),
        )
        self.assertEqual(layout.MANIFEST, SHARED["manifest"])

    def test_the_nix_options_read_the_same_file(self):
        options = (ROOT / "nix/home-manager/modules/sandbox/options.nix").read_text()
        self.assertIn("lib.importTOML ../../../sandbox.toml", options)
        for name in SHARED["paths"]:
            self.assertIn(f'sharedPath "{name}"', options)

    def test_the_sandbox_writes_the_shared_paths(self):
        self.assertEqual(
            (sandbox.OP_TOKEN_FILE, sandbox.WORKDIR_FILE, sandbox.READY_FILE),
            (layout.OP_TOKEN_FILE, layout.WORKDIR_FILE, layout.READY_FILE),
        )

    def test_no_source_spells_a_shared_path(self):
        """Code reaches the paths through layout.py or agentix.sandbox.paths."""
        sources = [
            path
            for directory in ("src/agentix", "nix/home-manager")
            for path in (ROOT / directory).rglob("*")
            if path.suffix in {".py", ".sh", ".nix", ".md"}
        ]
        self.assertTrue(sources)
        for path in sources:
            text = path.read_text()
            for value in SHARED["paths"].values():
                with self.subTest(path=path.relative_to(ROOT), value=value):
                    pattern = r"(?<![\w.])" + re.escape(value) + r"(?![\w.-])"
                    self.assertIsNone(re.search(pattern, text))


class ImageCheckTests(unittest.TestCase):
    def test_commands_come_from_the_manifest_of_the_generation(self):
        checks = image._checks()
        manifest = f"{image.GENERATION}/home-path/{layout.MANIFEST}"
        self.assertEqual(image.MANIFEST_FILE, manifest)
        loop = next(check for check in checks if "command -v" in check)
        self.assertIn(f"'.commands | join(\" \")' {manifest}", loop)
        self.assertFalse(hasattr(image, "REQUIRED_COMMANDS"))

    def test_manifest_paths_must_match_what_the_host_reads(self):
        check = next(check for check in image._checks() if "--argjson paths" in check)
        words = shlex.split(check)
        paths = json.loads(words[words.index("--argjson") + 2])
        self.assertEqual(paths, layout.PATHS)
        self.assertIn(".commands | length > 0", check)

    def test_claude_deny_rule_names_the_token_file(self):
        """The OCI build checks the rule, with the path from sandbox.toml."""
        oci = (ROOT / "nix/oci/default.nix").read_text()
        self.assertIn('--arg rule "Read(/${paths.opTokenFile})"', oci)
        self.assertIn("'.permissions.deny | index($rule)'", oci)


class OpenSSHScriptTests(unittest.TestCase):
    def test_sftp_starts_in_the_shared_working_directory(self):
        script = ssh._script("openssh")
        self.assertNotIn("@workdir", script)
        self.assertIn(
            f"workdir=$(cat {layout.WORKDIR_FILE} 2>/dev/null || echo {layout.WORKDIR})",
            script,
        )


class WorkdirProfileTests(unittest.TestCase):
    """Run the login script as Home Manager renders it, with its paths in a directory."""

    def setUp(self):
        # Resolved, as the shell's $PWD is: macOS keeps temporary files behind
        # a symbolic link.
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        self.home = self.root / "home"
        self.workdir = self.root / "workdir"
        self.override = self.root / "srv project"
        self.workdir_file = self.root / "run/workdir"
        for directory in (self.home, self.workdir, self.override):
            directory.mkdir()
        # The same substitution as pkgs.replaceVars in sandbox/workdir.nix.
        self.script = (
            WORKDIR_PROFILE.read_text()
            .replace("@workdirFile@", shlex.quote(str(self.workdir_file)))
            .replace("@workdir@", shlex.quote(str(self.workdir)))
        )

    def directory_after_login(self, start=None, ssh=True):
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}
        if ssh:
            environment["SSH_CONNECTION"] = "10.0.0.1 1 10.0.0.2 22"
        result = subprocess.run(
            ["sh", "-c", self.script + '\nprintf %s "$PWD"'],
            cwd=start or self.home,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(result.stdout).resolve()

    def test_every_placeholder_is_one_nix_fills_in(self):
        placeholders = set(re.findall(r"@\w+@", WORKDIR_PROFILE.read_text()))
        self.assertEqual(placeholders, {"@workdir@", "@workdirFile@"})

    def test_ssh_login_starts_in_the_working_directory(self):
        self.assertEqual(self.directory_after_login(), self.workdir.resolve())

    def test_ssh_login_starts_in_the_override(self):
        self.workdir_file.parent.mkdir()
        self.workdir_file.write_text(f"{self.override}\n")
        self.assertEqual(self.directory_after_login(), self.override.resolve())

    def test_other_sessions_and_other_directories_stay(self):
        self.assertEqual(self.directory_after_login(ssh=False), self.home.resolve())
        self.assertEqual(
            self.directory_after_login(start=self.override), self.override.resolve()
        )

    def test_missing_directory_leaves_the_session_at_home(self):
        self.workdir.rmdir()
        self.assertEqual(self.directory_after_login(), self.home.resolve())


if __name__ == "__main__":
    unittest.main()
