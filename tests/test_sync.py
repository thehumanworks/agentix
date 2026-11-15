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

from agentix import idle, sync
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

    def test_a_file_synced_before_and_changed_since_is_no_conflict(self):
        (self.host / "notes.txt").write_text("first\n")
        (self.host / "a.txt").write_text("one\ntwo\nthree\nhost\n")
        self.sync()
        # Edits on both sides since: the untracked file on the host, a
        # tracked file in the sandbox.
        (self.host / "notes.txt").write_text("first\nsecond\n")
        (self.box / "b.txt").write_text("box\n")
        self.sync()
        self.assertEqual((self.box / "notes.txt").read_text(), "first\nsecond\n")
        self.assertEqual((self.box / "b.txt").read_text(), "box\n")
        (self.box / "notes.txt").write_text("first\nsecond\nthird\n")
        self.sync(pull=True)
        self.assertEqual(
            (self.host / "notes.txt").read_text(), "first\nsecond\nthird\n"
        )
        self.assertEqual((self.host / "b.txt").read_text(), "box\n")
        self.assertEqual((self.host / "a.txt").read_text(), "one\ntwo\nthree\nhost\n")

    def test_after_undo_a_pull_does_not_take_back_what_was_sent(self):
        (self.host / "new.txt").write_text("new\n")
        self.sync()
        subprocess.run(
            [*SCRIPT, "undo", str(self.box)], check=True, capture_output=True
        )
        self.assertFalse((self.box / "new.txt").exists())
        self.assertEqual(self.sync(pull=True)["result"], "unchanged")
        self.assertEqual((self.host / "new.txt").read_text(), "new\n")

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


class FailureTests(SyncTestCase):
    """Whatever fails, uncommitted work survives on both sides."""

    def backups(self, repo: Path) -> list[str]:
        out = git(
            repo, "for-each-ref", "--format=%(refname)", "refs/agentix-sync/backup"
        )
        return out.split()

    def tree_state(self, repo: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(repo)): path.read_bytes()
            for path in sorted(repo.rglob("*"))
            if ".git" not in path.parts and path.is_file()
        }

    def test_a_working_tree_git_cannot_read_stops_before_any_write(self):
        # A clean filter that fails, as a required LFS filter does without
        # git-lfs: the sandbox's edit must not be taken as unchanged.
        git(self.box, "config", "filter.broken.clean", "false")
        git(self.box, "config", "filter.broken.required", "true")
        (self.box / ".git" / "info" / "attributes").write_text("a.txt filter=broken\n")
        (self.box / "a.txt").write_text("one\ntwo\nbox\n")
        (self.host / "a.txt").write_text("host\ntwo\nthree\n")
        (self.host / "new.txt").write_text("new\n")
        before = self.tree_state(self.box)
        with self.assertRaisesRegex(OperationFailed, "could not read the working tree"):
            self.sync()
        self.assertEqual(self.tree_state(self.box), before)
        self.assertEqual(self.backups(self.box), [])

    def test_new_files_that_cannot_be_prepared_stop_before_any_write(self):
        git(self.box, "config", "filter.broken.smudge", "false")
        git(self.box, "config", "filter.broken.required", "true")
        (self.box / ".git" / "info" / "attributes").write_text(
            "new.txt filter=broken\n"
        )
        (self.host / "b.txt").write_text("host\n")
        (self.host / "new.txt").write_text("new\n")
        before = self.tree_state(self.box)
        with self.assertRaisesRegex(OperationFailed, "could not prepare the new files"):
            self.sync()
        self.assertEqual(self.tree_state(self.box), before)
        self.assertEqual(self.backups(self.box), [])

    def test_an_edit_made_while_syncing_stops_it(self):
        # The smudge filter runs while the new files are prepared, after the
        # snapshot: it stands in for an editor saving at that moment.
        git(self.box, "config", "filter.late.smudge", "sh -c 'echo late >> b.txt; cat'")
        (self.box / ".git" / "info" / "attributes").write_text("new.txt filter=late\n")
        (self.host / "b.txt").write_text("host\n")
        (self.host / "new.txt").write_text("new\n")
        with self.assertRaisesRegex(OperationFailed, "changed while syncing"):
            self.sync()
        self.assertEqual((self.box / "b.txt").read_text(), "bee\nlate\n")
        self.assertFalse((self.box / "new.txt").exists())
        self.assertEqual(self.backups(self.box), [])

    def test_a_write_that_fails_part_way_is_put_back(self):
        (self.box / "a.txt").write_text("one\ntwo\nthree\nbox\n")
        (self.box / "mine.txt").write_text("mine\n")
        (self.host / "b.txt").unlink()
        (self.host / "c.txt").write_text("c\n")
        (self.host / "sub").mkdir()
        (self.host / "sub" / "z.txt").write_text("z\n")
        before = self.tree_state(self.box)
        status = self.status(self.box)
        with patch.dict(os.environ, {"AGENTIX_SYNC_FAIL_AT": "sub/z.txt"}):
            with self.assertRaises(OperationFailed) as caught:
                self.sync()
        message = str(caught.exception)
        self.assertIn("put back as it was", message)
        # The backup and how to use it, although nothing needs undoing.
        self.assertIn("refs/agentix-sync/backup/000001-", message)
        self.assertIn(f"agentix-sync undo {self.box}", message)
        self.assertEqual(self.tree_state(self.box), before)
        self.assertEqual(self.status(self.box), status)
        self.assertFalse((self.box / "sub").exists())

    def test_ignored_files_in_a_directory_a_file_replaces_are_kept(self):
        (self.host / "d").mkdir()
        (self.host / "d" / "x.txt").write_text("x\n")
        self.sync()
        (self.box / ".git" / "info" / "exclude").write_text("*.o\n")
        (self.box / "d" / "cache.o").write_text("precious\n")
        shutil.rmtree(self.host / "d")
        (self.host / "d").write_text("now a file\n")
        with self.assertRaisesRegex(
            OperationFailed, "file Git ignores(.|\n)*  d/cache.o"
        ):
            self.sync()
        self.assertEqual((self.box / "d" / "cache.o").read_text(), "precious\n")
        self.assertEqual((self.box / "d" / "x.txt").read_text(), "x\n")

    def test_an_ignored_file_where_a_new_directory_goes_is_kept(self):
        (self.box / ".git" / "info" / "exclude").write_text("build\n")
        (self.box / "build").write_text("precious\n")
        (self.host / "build").mkdir()
        (self.host / "build" / "out").write_text("out\n")
        with self.assertRaisesRegex(OperationFailed, "file Git ignores(.|\n)*  build"):
            self.sync()
        self.assertEqual((self.box / "build").read_text(), "precious\n")

    def test_backups_are_numbered_in_order(self):
        for text in ("1\n", "2\n", "3\n"):
            (self.host / "a.txt").write_text(text)
            self.sync()
        names = [ref.rsplit("/", 1)[1][:6] for ref in self.backups(self.box)]
        self.assertEqual(names, ["000001", "000002", "000003"])
        subprocess.run(
            [*SCRIPT, "undo", str(self.box)], check=True, capture_output=True
        )
        self.assertEqual((self.box / "a.txt").read_text(), "2\n")

    def test_a_bundle_too_large_is_refused(self):
        (self.host / "a.txt").write_text("changed\n")
        with patch.object(sync, "MAX_BUNDLE", 100):
            with self.assertRaisesRegex(OperationFailed, "nothing was sent"):
                self.sync()
        self.assertEqual((self.box / "a.txt").read_text(), "one\ntwo\nthree\n")
        with patch.object(sync, "WARN_BUNDLE", 100):
            self.assertIn(".gitignore", self.sync()["warning"])


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

    def test_an_older_command_is_replaced_by_the_repository_copy(self):
        (self.host / "a.txt").write_text("newer\n")
        old = ["sh", "-c", 'echo "protocol 1"', "agentix-sync"]
        self.sync(command=old)
        self.assertEqual((self.box / "a.txt").read_text(), "newer\n")

    def test_without_the_command_the_repository_copy_runs(self):
        # An image from before agentix-sync: the sandbox has no such command.
        (self.host / "a.txt").write_text("fallback\n")
        self.sync(command=["agentix-sync-missing"])
        self.assertEqual((self.box / "a.txt").read_text(), "fallback\n")


class IdleStopTests(SyncTestCase):
    """Sync through the wrapper that execute_bytes puts around each command."""

    def setUp(self):
        super().setUp()
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        self.wrapper = idle.EXEC_WRAPPER.replace(idle.RUN_DIR, str(self.run_dir))
        self.calls = 0

    async def wrapped(self, args, stdin):
        # What execute_bytes sends to Modal, with the sandbox's /run here.
        sent = idle.wrap(args)
        self.assertEqual(sent[4:], list(args))
        self.calls += 1
        return await sync.run_here([*sent[:2], self.wrapper, *sent[3:]], stdin)

    def marks(self):
        return list(self.run_dir.glob("used.*"))

    def edit_and_sync(self):
        (self.host / "a.txt").write_text("one\nTWO\nthree\n")
        self.sync(runner=self.wrapped)
        self.assertEqual((self.box / "a.txt").read_text(), "one\nTWO\nthree\n")
        self.assertGreater(self.calls, 0)

    def test_with_an_idle_stop_each_sync_command_leaves_a_mark(self):
        (self.run_dir / "idle-stop").touch()
        self.edit_and_sync()
        self.assertEqual(len(self.marks()), self.calls)

    def test_without_an_idle_stop_sync_is_unchanged(self):
        self.edit_and_sync()
        self.assertEqual(self.marks(), [])
        self.assertEqual(self.status(self.box), " M a.txt\n")


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
        handle.exec.aio.assert_awaited_once_with(*idle.wrap(["x"]), text=False)
        written = b"".join(call.args[0] for call in process.stdin.write.call_args_list)
        self.assertEqual(written, data)
        process.stdin.write_eof.assert_called_once()

    def test_flags_go_before_or_after_the_name(self):
        with patch("agentix.__main__._backend") as backend:
            main(["sandbox", "sync", "--pull", "box"])
            main(["sandbox", "sync", "box", "--path=12"])
        self.assertEqual(
            [call.args[::2] for call in backend.return_value.sync.call_args_list],
            [("box", ""), ("box", "12")],
        )
        self.assertEqual(
            [call.args[3] for call in backend.return_value.sync.call_args_list],
            [True, False],
        )

    def test_a_value_for_pull_is_refused_before_modal(self):
        with patch("agentix.__main__._backend") as backend:
            with self.assertRaises(SystemExit) as raised:
                main(["sandbox", "sync", "box", "--pull=yes"])
            self.assertEqual(raised.exception.code, 2)
            backend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
