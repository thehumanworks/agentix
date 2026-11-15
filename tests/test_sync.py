"""`sandbox sync` and agentix-sync, run with real Git between two local clones.

The sandbox side runs the same script through the same Runner interface that
Modal's exec fills in, so only the transport is left out.
"""

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from agentix import sync
from agentix.__main__ import main
from agentix.errors import InvalidRequest, NotFound, OperationFailed

SCRIPT = ["bash", str(sync.SCRIPT)]
ORIGIN = "https://github.com/acme/widget.git"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        origin = self.tmp / "origin"
        subprocess.run(["git", "init", "-q", "-b", "main", origin], check=True)
        (origin / "a.txt").write_text("one\ntwo\nthree\n")
        (origin / "b.txt").write_text("bee\n")
        (origin / "big.bin").write_bytes(os.urandom(1 << 20))
        (origin / ".gitignore").write_text(".env\n")
        git(origin, "add", ".")
        git(origin, "commit", "-qm", "init")
        self.host = self.tmp / "host"
        self.box = self.tmp / "workdir" / "widget"
        for clone in (self.host, self.box):
            subprocess.run(["git", "clone", "-q", origin, clone], check=True)
            git(clone, "remote", "set-url", "origin", ORIGIN)
        self.workdir = patch.object(sync, "WORKDIR", str(self.tmp / "workdir"))
        self.workdir.start()
        self.addCleanup(self.workdir.stop)

    def sync(self, pull=False, path="", command=SCRIPT, runner=None):
        here = sync.Side("this machine", sync.run_here, str(self.host), SCRIPT)
        there = sync.Side("sandbox t", runner or sync.run_here, path, command)
        return asyncio.run(sync.sync(here, there, pull))

    def status(self, repo: Path) -> str:
        return git(repo, "status", "--porcelain")


class DirectionTests(SyncTestCase):
    def test_host_changes_reach_the_sandbox(self):
        (self.host / "a.txt").write_text("one\nTWO\nthree\n")
        (self.host / "b.txt").unlink()
        (self.host / "new.txt").write_text("new\n")
        (self.host / "big.bin").write_bytes(b"\x00\xff" * 10)
        (self.host / "run.sh").write_text("#!/bin/sh\n")
        (self.host / "run.sh").chmod(0o755)
        (self.host / "link").symlink_to("a.txt")
        result = self.sync()
        self.assertEqual(result["result"], "applied")
        self.assertEqual((self.box / "a.txt").read_text(), "one\nTWO\nthree\n")
        self.assertFalse((self.box / "b.txt").exists())
        self.assertEqual((self.box / "new.txt").read_text(), "new\n")
        self.assertEqual((self.box / "big.bin").read_bytes(), b"\x00\xff" * 10)
        self.assertTrue(os.access(self.box / "run.sh", os.X_OK))
        self.assertEqual(os.readlink(self.box / "link"), "a.txt")
        # Unstaged and untracked, as on the host; the host is untouched.
        self.assertEqual(self.status(self.box), self.status(self.host))
        self.assertIn("M a.txt", result["changed"])
        self.assertIn("refs/agentix-sync/backup/", result["backup"])
        self.assertNotIn("note", result)

    def test_sandbox_changes_come_back_with_pull(self):
        (self.box / "a.txt").write_text("one\ntwo\nthree\nfour\n")
        (self.box / "made-in-box.txt").write_text("hi\n")
        result = self.sync(pull=True)
        self.assertEqual(result["to"], f"this machine ({self.host})")
        self.assertEqual((self.host / "a.txt").read_text(), "one\ntwo\nthree\nfour\n")
        self.assertEqual((self.host / "made-in-box.txt").read_text(), "hi\n")

    def test_a_second_sync_changes_nothing(self):
        (self.host / "a.txt").write_text("x\n")
        self.sync()
        self.assertEqual(self.sync()["result"], "unchanged")

    def test_only_what_the_other_side_lacks_is_sent(self):
        (self.host / "a.txt").write_text("small change\n")
        sizes = []

        async def measure(args, stdin):
            sizes.append(len(stdin))
            return await sync.run_here(args, stdin)

        self.sync(runner=measure)
        # The bundle that apply reads; the 1 MiB file is not in it.
        self.assertLess(max(sizes), 4096)

    def test_ignored_files_stay(self):
        (self.host / ".env").write_text("SECRET=1\n")
        self.sync()
        self.assertFalse((self.box / ".env").exists())


class SafetyTests(SyncTestCase):
    def test_conflict_is_refused_and_nothing_changes(self):
        (self.host / "a.txt").write_text("one\nhost\nthree\n")
        (self.host / "b.txt").write_text("host bee\n")
        (self.box / "a.txt").write_text("one\nbox\nthree\n")
        (self.box / "other.txt").write_text("untracked in box\n")
        before = self.status(self.box)
        with self.assertRaises(OperationFailed) as caught:
            self.sync()
        message = str(caught.exception)
        self.assertIn("Nothing changed", message)
        self.assertIn("  a.txt", message)
        self.assertNotIn("b.txt", message)
        self.assertEqual((self.box / "a.txt").read_text(), "one\nbox\nthree\n")
        self.assertEqual((self.box / "b.txt").read_text(), "bee\n")
        self.assertEqual(self.status(self.box), before)
        self.assertEqual(git(self.box, "for-each-ref", "refs/agentix-sync/backup"), "")

    def test_changes_to_different_lines_merge(self):
        (self.host / "a.txt").write_text("ONE\ntwo\nthree\n")
        (self.box / "a.txt").write_text("one\ntwo\nTHREE\n")
        self.sync()
        self.assertEqual((self.box / "a.txt").read_text(), "ONE\ntwo\nTHREE\n")

    def test_ignored_file_in_the_way_is_refused(self):
        (self.host / ".gitignore").write_text("")
        (self.host / ".env").write_text("from host\n")
        (self.box / ".env").write_text("box secret\n")
        with self.assertRaisesRegex(OperationFailed, "file Git ignores(.|\n)*  .env"):
            self.sync()
        self.assertEqual((self.box / ".env").read_text(), "box secret\n")

    def test_undo_puts_the_working_tree_back(self):
        (self.box / "a.txt").write_text("one\ntwo\nthree\nbox\n")
        (self.box / "mine.txt").write_text("mine\n")
        (self.host / "b.txt").write_text("host\n")
        (self.host / "theirs.txt").write_text("theirs\n")
        self.sync()
        self.assertEqual((self.box / "theirs.txt").read_text(), "theirs\n")
        subprocess.run(
            [*SCRIPT, "undo", str(self.box)], check=True, capture_output=True
        )
        self.assertFalse((self.box / "theirs.txt").exists())
        self.assertEqual((self.box / "b.txt").read_text(), "bee\n")
        self.assertEqual((self.box / "a.txt").read_text(), "one\ntwo\nthree\nbox\n")
        self.assertEqual((self.box / "mine.txt").read_text(), "mine\n")

    def test_different_base_commits(self):
        # The sandbox has a commit the host lacks, and the host one the
        # sandbox lacks; only the uncommitted changes move.
        (self.box / "b.txt").write_text("box commit\n")
        git(self.box, "commit", "-qam", "box")
        (self.host / "c.txt").write_text("host commit\n")
        git(self.host, "add", "c.txt")
        git(self.host, "commit", "-qm", "host")
        (self.host / "a.txt").write_text("one\ntwo\nthree\nuncommitted\n")
        result = self.sync()
        self.assertEqual(
            (self.box / "a.txt").read_text(), "one\ntwo\nthree\nuncommitted\n"
        )
        self.assertEqual((self.box / "b.txt").read_text(), "box commit\n")
        self.assertFalse((self.box / "c.txt").exists())
        self.assertIn("only the uncommitted changes", result["note"])
        host_head = git(self.host, "rev-parse", "HEAD").strip()
        self.assertEqual(
            git(self.box, "rev-parse", "refs/agentix-sync/incoming^").strip(),
            host_head,
        )


class MatchTests(SyncTestCase):
    def test_missing_checkout_says_how_to_make_one(self):
        shutil.rmtree(self.box)
        with self.assertRaisesRegex(NotFound, "does not exist.*sandbox up --repo"):
            self.sync()

    def test_other_repository_is_refused(self):
        git(self.box, "remote", "set-url", "origin", "git@github.com:acme/gadget.git")
        with self.assertRaisesRegex(InvalidRequest, "acme/gadget.*acme/widget"):
            self.sync()

    def test_unrelated_history_is_refused(self):
        other = self.tmp / "workdir" / "other"
        subprocess.run(["git", "init", "-q", other], check=True)
        git(other, "commit", "-q", "--allow-empty", "-m", "x")
        with self.assertRaisesRegex(InvalidRequest, "share no history"):
            self.sync(path=str(other))

    def test_path_names_the_checkout(self):
        moved = self.tmp / "elsewhere"
        self.box.rename(moved)
        (self.host / "a.txt").write_text("moved\n")
        self.sync(path=str(moved))
        self.assertEqual((moved / "a.txt").read_text(), "moved\n")

    def test_checkout_name_comes_from_origin(self):
        for url, name in (
            ("https://github.com/o/r.git", "r"),
            ("git@github.com:o/r", "r"),
            ("ssh://git@github.com/o/r.git/", "r"),
            ("https://x:y@github.com/o/r.git", "r"),
            ("/srv/r.git", "dir"),
            ("", "dir"),
        ):
            with self.subTest(url=url):
                info = {"origin": url, "toplevel": "/a/dir"}
                self.assertEqual(sync.checkout_name(info), name)

    def test_without_the_command_the_repository_copy_runs(self):
        # An image from before agentix-sync: the sandbox has no such command.
        (self.host / "a.txt").write_text("fallback\n")
        self.sync(command=["agentix-sync-missing"])
        self.assertEqual((self.box / "a.txt").read_text(), "fallback\n")


class TransportTests(unittest.TestCase):
    def test_execute_bytes_feeds_input_and_returns_bytes(self):
        from agentix import sandbox

        process = Mock()
        process.stdout.read.aio = AsyncMock(return_value=b"\x00out")
        process.stderr.read.aio = AsyncMock(return_value=b"err")
        process.wait.aio = AsyncMock(return_value=3)
        process.stdin.drain.aio = AsyncMock()
        handle = Mock()
        handle.exec.aio = AsyncMock(return_value=process)
        session = Mock()
        session.sandbox.return_value.__aenter__ = AsyncMock(return_value=handle)
        session.sandbox.return_value.__aexit__ = AsyncMock(return_value=False)
        data = os.urandom((1 << 20) + 5)
        result = asyncio.run(sandbox.execute_bytes(session, "sb-1", ["x"], data))
        self.assertEqual(result, (3, b"\x00out", b"err"))
        handle.exec.aio.assert_awaited_once_with("x", text=False)
        written = b"".join(call.args[0] for call in process.stdin.write.call_args_list)
        self.assertEqual(written, data)
        process.stdin.write_eof.assert_called_once()

    def test_flag_values_are_refused_before_modal(self):
        with patch("agentix.__main__._backend") as backend:
            with self.assertRaisesRegex(InvalidRequest, "--pull takes no value"):
                main(["sandbox", "sync", "--pull", "box"])
            with self.assertRaisesRegex(InvalidRequest, "--path"):
                main(["sandbox", "sync", "box", "--path=12"])
            backend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
