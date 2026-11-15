import subprocess
import tempfile
import unittest
from pathlib import Path

from support import ModalTestCase
from test_lifecycle import OPTIONS

from agentix import lifecycle, repo, store
from agentix.errors import InvalidRequest, OperationFailed

NO_WORKDIR = {**OPTIONS, "workdir": ""}


class ParseTests(unittest.TestCase):
    def test_owner_name_and_ref(self):
        self.assertEqual(
            repo.parse("thehumanworks/agentix"),
            ("https://github.com/thehumanworks/agentix.git", "/workdir/agentix", ""),
        )
        self.assertEqual(
            repo.parse("o/my.repo.git@feature/x"),
            ("https://github.com/o/my.repo.git", "/workdir/my.repo", "feature/x"),
        )

    def test_bad_specs_are_refused(self):
        for spec in ("agentix", "a/b/c", "a/..", "a/.git", "a/b@", "a/b@-x", "a b/c"):
            with self.subTest(spec=spec), self.assertRaises(InvalidRequest):
                repo.parse(spec)

    def test_error_names_the_step_that_failed(self):
        clone = str(repo.failed("o/r@v1", 128, "fatal: not found"))
        self.assertIn("Could not clone o/r@v1", clone)
        self.assertIn("credentials", clone)
        for code, step in ((3, "check out v1"), (4, "run direnv allow")):
            with self.subTest(code=code):
                message = str(repo.failed("o/r@v1", code, "error: x"))
                self.assertIn(f"Cloned o/r@v1, but could not {step}", message)
                self.assertNotIn("credentials", message)
                self.assertIn("error: x", message)


class CloneScriptTests(unittest.TestCase):
    """The clone script, run with real Git against a local repository."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin"
        git = ["git", "-C", str(self.origin), "-c", "user.name=t", "-c", "user.email=t"]
        subprocess.run(["git", "init", "-q", "-b", "main", self.origin], check=True)
        (self.origin / ".envrc").write_text("export X=1\n")
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-qm", "one"], check=True)
        subprocess.run([*git, "tag", "v1"], check=True)
        subprocess.run([*git, "checkout", "-qb", "feature"], check=True)
        subprocess.run([*git, "commit", "-qm", "two", "--allow-empty"], check=True)
        subprocess.run([*git, "checkout", "-q", "main"], check=True)
        # Modal creates the workdir, empty, before the script runs.
        self.target = self.tmp / "workdir" / "origin"
        self.target.mkdir(parents=True)

    def clone(self, ref=""):
        # direnv is stubbed: the test checks only that it is called.
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        status = getattr(self, "direnv_exit", 0)
        (bin_dir / "direnv").write_text(
            f'#!/bin/sh\necho "$@" > "$DIRENV_LOG"\nexit {status}\n'
        )
        (bin_dir / "direnv").chmod(0o755)
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "DIRENV_LOG": str(self.tmp / "direnv.log"),
            "HOME": str(self.tmp),
        }
        url = str(self.origin)
        return subprocess.run(
            ["sh", "-c", repo.CLONE, "x", url, str(self.target), ref],
            env=env,
            capture_output=True,
            text=True,
        )

    def head(self):
        return subprocess.run(
            ["git", "-C", self.target, "log", "-1", "--format=%s"],
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_default_branch_and_direnv_allow(self):
        self.assertEqual(self.clone().returncode, 0)
        self.assertEqual(self.head(), "one")
        log = (self.tmp / "direnv.log").read_text().split()
        self.assertEqual(log, ["allow", str(self.target)])

    def test_branch_tag_and_commit(self):
        sha = subprocess.run(
            ["git", "-C", self.origin, "rev-parse", "feature"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        for ref, subject in (("feature", "two"), ("v1", "one"), (sha, "two")):
            with self.subTest(ref=ref):
                subprocess.run(["rm", "-rf", self.target], check=True)
                self.target.mkdir()
                result = self.clone(ref)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.head(), subject)

    def test_each_failed_step_has_its_exit_code(self):
        self.assertEqual(self.clone("nope").returncode, 3)
        subprocess.run(["rm", "-rf", self.target], check=True)
        self.target.mkdir()
        (self.tmp / "bin").mkdir(exist_ok=True)
        self.direnv_exit = 1
        self.assertEqual(self.clone().returncode, 4)
        subprocess.run(["rm", "-rf", self.target], check=True)
        self.target.mkdir()
        self.origin = self.tmp / "missing"
        self.assertNotIn(self.clone().returncode, (0, 3, 4))

    def test_no_envrc_no_direnv(self):
        subprocess.run(["git", "-C", self.origin, "rm", "-q", ".envrc"], check=True)
        subprocess.run(
            ["git", "-C", self.origin, "-c", "user.name=t", "-c", "user.email=t"]
            + ["commit", "-qm", "no envrc"],
            check=True,
        )
        self.assertEqual(self.clone().returncode, 0)
        self.assertFalse((self.tmp / "direnv.log").exists())


class UpWithRepoTests(ModalTestCase):
    def test_named_sandbox_starts_in_the_clone_and_records_it_as_workdir(self):
        result = self.run_core(
            lifecycle.up, NO_WORKDIR, "dev", clone="thehumanworks/agentix@v1"
        )
        self.assertEqual(result, {"sandbox_id": "sb-test", "name": "dev"})
        self.assertEqual(
            self.create.aio.call_args.kwargs["workdir"], "/workdir/agentix"
        )
        args = self.sandbox.exec.aio.call_args.args
        self.assertEqual(
            args[-3:],
            ("https://github.com/thehumanworks/agentix.git", "/workdir/agentix", "v1"),
        )
        # A restore starts with these options, in the clone, and does not clone.
        self.assertEqual(self.stored("dev")["options"]["workdir"], "/workdir/agentix")

    def test_cli_flag_reaches_the_sandbox(self):
        self.run_cli("sandbox", "up", "--ephemeral", "--repo=o/r")
        self.assertEqual(self.create.aio.call_args.kwargs["workdir"], "/workdir/r")
        self.assertIn(
            "https://github.com/o/r.git", self.sandbox.exec.aio.call_args.args
        )

    def test_failed_clone_removes_the_sandbox_and_leaves_no_record(self):
        self.sandbox.process.wait.aio.return_value = 128
        self.sandbox.process.stderr.read.aio.return_value = (
            "[ERROR] op: no account\n - sign in\nremote: Repository not found.\n"
        )
        for name, ephemeral in (("dev", False), ("", True)):
            with self.subTest(ephemeral=ephemeral):
                self.sandbox.terminate.aio.reset_mock()
                with self.assertRaisesRegex(
                    OperationFailed, r"(?s)Could not clone o/r.*removed.*not found"
                ):
                    self.run_core(
                        lifecycle.up, NO_WORKDIR, name, ephemeral=ephemeral, clone="o/r"
                    )
                self.sandbox.terminate.aio.assert_awaited_once()
        with self.assertRaises(OperationFailed) as caught:
            self.run_core(lifecycle.up, NO_WORKDIR, ephemeral=True, clone="o/r")
        self.assertNotIn("op:", str(caught.exception))
        self.assertIsNone(self.stored("dev"))

    def test_bad_spec_and_workdir_are_refused_before_a_sandbox_starts(self):
        with self.assertRaises(InvalidRequest):
            self.run_core(lifecycle.up, NO_WORKDIR, "dev", clone="nope")
        with self.assertRaisesRegex(InvalidRequest, "--workdir"):
            self.run_core(lifecycle.up, OPTIONS, "dev", clone="o/r")
        self.create.aio.assert_not_called()

    def test_restore_does_not_clone(self):
        self.run_core(lifecycle.up, NO_WORKDIR, "dev", clone="o/r")
        record = self.stored("dev")
        record["sandboxes"][-1]["exit"] = "saved"
        store.add_snapshot(record, "im-1", "sb-test", "down", "2026-02-01")
        self.write_record(record)
        self.sandbox.poll.aio.return_value = 0
        self.sandbox.exec.aio.reset_mock()
        self.run_core(lifecycle.restore, "dev")
        self.assertEqual(self.create.aio.call_args.kwargs["workdir"], "/workdir/r")
        self.sandbox.exec.aio.assert_not_called()


class RepoAndConfigTests(ModalTestCase):
    def test_config_workdir_gives_way_to_repo_but_a_workdir_flag_is_refused(self):
        file = Path(self.config_home) / "agentix" / "config.toml"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('[sandbox.up]\nworkdir = "/srv/app"\n')
        self.run_cli("sandbox", "up", "--ephemeral", "--repo=o/r")
        self.assertEqual(self.create.aio.call_args.kwargs["workdir"], "/workdir/r")
        self.create.aio.reset_mock()
        with self.assertRaisesRegex(InvalidRequest, "--workdir"):
            self.run_cli("sandbox", "up", "--ephemeral", "--repo=o/r", "--workdir=/x")
        self.create.aio.assert_not_called()

    def test_bare_repo_flag_is_refused_before_anything_starts(self):
        errors = self.usage_error("sandbox", "up", "--ephemeral", "--repo")
        self.assertIn("'--repo' requires an argument", errors)
        self.create.aio.assert_not_called()
