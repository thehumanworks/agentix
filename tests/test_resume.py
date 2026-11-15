import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from agentix import resume
from agentix.errors import InvalidRequest, OperationFailed

SNAPSHOT = {"id": "im-1", "sandbox_id": "sb-old", "kind": "timeout"}


P = resume.CONTINUE_PROMPT


class PlanTest(unittest.TestCase):
    def setUp(self):
        patcher = patch("agentix.resume.uuid.uuid4", return_value="U")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_claude_gets_its_own_session_and_a_neutral_prompt(self):
        first, then, how = resume.plan(["claude", "-p", "fix it", "--model", "m"], True)
        self.assertEqual(
            first, ["claude", "--session-id", "U", "-p", "fix it", "--model", "m"]
        )
        # The task is not given again, so it cannot be done twice.
        self.assertEqual(then, ["claude", "--resume", "U", "-p", P, "--model", "m"])
        self.assertEqual(how, "the claude rule")

    def test_first_word_matches_by_base_name_and_is_kept(self):
        first, then, _ = resume.plan(["/opt/bin/claude", "--print", "x"], True)
        self.assertEqual(
            first, ["/opt/bin/claude", "--session-id", "U", "--print", "x"]
        )
        self.assertEqual(then, ["/opt/bin/claude", "--resume", "U", "--print", P])

    def test_claude_without_a_prompt_after_p_is_refused(self):
        for argv in (
            ["claude"],
            ["claude", "fix", "-p"],
            ["claude", "-p", "--model", "m", "x"],
        ):
            with self.assertRaisesRegex(InvalidRequest, "prompt is not right after -p"):
                resume.plan(argv, True)

    def test_claude_that_names_a_session_is_refused(self):
        for word in ("-c", "--continue", "--resume", "--session-id=abc"):
            with self.assertRaisesRegex(InvalidRequest, "which session"):
                resume.plan(["claude", word, "-p", "x"], True)

    def test_rules_can_match_a_subcommand(self):
        first, then, how = resume.plan(["codex", "exec", "do it", "-m", "m"], True)
        self.assertEqual(first, ["codex", "exec", "do it", "-m", "m"])
        self.assertEqual(how, "the codex rule")
        # `codex exec resume` refuses most options of `codex exec`, so they go
        # before the subcommand and only the prompt after it.
        self.assertEqual(then, ["codex", "exec", "-m", "m", "resume", "--last", P])
        with self.assertRaises(InvalidRequest):
            resume.plan(["codex", "login"], True)

    def test_codex_keeps_options_in_order_before_resume(self):
        _, then, _ = resume.plan(
            ["codex", "exec", "x", "-s", "workspace-write", "-C", "/w", "--json"], True
        )
        self.assertEqual(
            then,
            [
                *("codex", "exec", "-s", "workspace-write", "-C", "/w", "--json"),
                *("resume", "--last", P),
            ],
        )

    def test_codex_without_a_session_to_resume_is_refused(self):
        for argv in (
            ["codex", "exec", "resume", "x"],
            ["codex", "exec", "review"],
            ["codex", "exec", "fork"],
            ["codex", "exec", "x", "--ephemeral"],
            # -i takes several files, so it would take `resume` as one.
            ["codex", "exec", "x", "-i", "a.png"],
            ["codex", "exec", "x", "--image=a.png"],
        ):
            with self.assertRaisesRegex(InvalidRequest, "which session"):
                resume.plan(argv, True)

    def test_codex_prompt_must_follow_exec(self):
        for argv in (["codex", "exec"], ["codex", "exec", "-m", "m", "x"]):
            with self.assertRaisesRegex(InvalidRequest, "right after codex exec"):
                resume.plan(argv, True)

    def test_pi_gets_its_own_session_and_a_neutral_prompt(self):
        first, then, how = resume.plan(["pi", "-p", "fix it", "--model", "m"], True)
        self.assertEqual(
            first, ["pi", "--session-id", "U", "-p", "fix it", "--model", "m"]
        )
        self.assertEqual(then, ["pi", "--session-id", "U", "-p", P, "--model", "m"])
        self.assertEqual(how, "the pi rule")

    def test_pi_that_names_or_drops_a_session_is_refused(self):
        for word in (
            "-c",
            "--continue",
            "-r",
            "--session=a",
            "--session-id=b",
            "--fork",
            "--no-session",
        ):
            with self.assertRaisesRegex(InvalidRequest, "which session"):
                resume.plan(["pi", word, "-p", "x"], True)
        with self.assertRaisesRegex(InvalidRequest, "prompt is not right after -p"):
            resume.plan(["pi", "x", "-p"], True)

    def test_a_command_without_a_rule_is_refused(self):
        for argv in (["bash", "-c", "claude -p x"], ["claudette"], ["make"]):
            with self.assertRaisesRegex(InvalidRequest, "No resume rule"):
                resume.plan(argv, True)

    def test_a_given_resume_command_is_used_for_any_command(self):
        first, then, how = resume.plan(["./build.sh"], "./build.sh --continue 'a b'")
        self.assertEqual(first, ["./build.sh"])
        self.assertEqual(then, ["./build.sh", "--continue", "a b"])
        self.assertEqual(how, "--resume")
        with self.assertRaises(InvalidRequest):
            resume.plan(["x"], "")

    def test_a_value_that_is_not_text_is_refused(self):
        # Fire reads --resume=7 as a number and --resume=[a] as a list.
        for value in (7, ["a"], False, None):
            with self.assertRaisesRegex(InvalidRequest, "a command in quotes"):
                resume.plan(["claude", "-p", "x"], value)


class WrapperTest(unittest.TestCase):
    """The scripts that run in the sandbox, run here with sh."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name) / "runs"

    def wrap(self, *command):
        return subprocess.run(
            ["sh", "-c", resume.WRAPPER, "agentix-run", str(self.dir), "r1", '{"a": 1}']
            + list(command),
            capture_output=True,
            text=True,
        )

    def test_marker_exists_while_the_command_runs_and_goes_after(self):
        marker = self.dir / "r1.json"
        result = self.wrap("sh", "-c", f'cat "{marker}"; echo "$1"; exit 3', "_", "x y")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, '{"a": 1}\nx y\n')
        self.assertFalse(marker.exists())

    def test_a_killed_command_leaves_its_marker(self):
        # As at the timeout: the wrapper never gets to remove the marker.
        result = self.wrap("sh", "-c", "kill -9 $PPID")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.dir / "r1.json").exists())

    def test_reading_markers(self):
        self.dir.mkdir()
        (self.dir / "a.json").write_text('{"id": "a"}\n')
        (self.dir / "b.json").write_text('{"id": "b"}\n')
        (self.dir / "c.tmp").write_text("partial")
        read = ["sh", "-c", resume.READ_MARKERS, "agentix-runs"]
        found = subprocess.run(read + [str(self.dir)], capture_output=True, text=True)
        self.assertEqual(found.stdout, '{"id": "a"}\n{"id": "b"}\n')
        empty = subprocess.run(read + [str(self.dir / "none")], capture_output=True)
        self.assertEqual((empty.returncode, empty.stdout), (0, b""))


START = ["claude", "--session-id", "U", "-p", "x"]
CONTINUE = ["claude", "--resume", "U", "-p", resume.CONTINUE_PROMPT]


def marker(run_id, sandbox_id, command, then, resumes=0):
    return json.dumps(
        {
            "id": run_id,
            "sandbox_id": sandbox_id,
            "command": command,
            "resume": then,
            "started_at": "2026-01-01T00:00:00+00:00",
            "resumes": resumes,
        }
    )


class WatchTest(unittest.TestCase):
    """`start` and `resume`, with the sandboxes and records replaced."""

    def setUp(self):
        self.notices: list[str] = []
        self.outputs: list[tuple[str, str]] = []
        # Each sandbox's exit codes as poll reports them, in turn; None runs.
        self.exits: dict[str, list] = {}
        # Each sandbox's command results, in turn, by the command's first word.
        self.results: dict[str, list] = {}
        self.markers: dict[str, str] = {}
        self.executed: list[tuple[str, list[str]]] = []
        self.restored: list[str] = []
        self.options = {"exit_snapshot": True}
        patches = {
            "agentix.lifecycle.running": AsyncMock(side_effect=self.running),
            "agentix.lifecycle.restore": AsyncMock(side_effect=self.restore),
            "agentix.store.load": AsyncMock(side_effect=self.load),
            "agentix.sandbox.execute": AsyncMock(side_effect=self.execute),
            "agentix.resume.POLL_INTERVAL": 0,
            "agentix.resume.END_WAIT": 0.2,
            "agentix.resume.uuid.uuid4": Mock(return_value="U"),
        }
        for target, value in patches.items():
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.snapshots = [dict(SNAPSHOT)]

    async def running(self, session, target):
        return ("sb-1", "" if target.startswith("sb-") else target)

    async def restore(self, session, target):
        new = f"sb-new{len(self.restored) + 1}"
        self.restored.append(new)
        return {"name": "dev", "sandbox_id": new, "snapshot": self.snapshots[-1]["id"]}

    async def load(self, session, name):
        return {"options": self.options, "snapshots": self.snapshots}

    async def execute(self, session, sandbox_id, args):
        if args[3] == "agentix-runs":
            return {
                "stdout": self.markers.get(sandbox_id, ""),
                "stderr": "",
                "exit_code": 0,
            }
        command = args[7:]
        self.executed.append((sandbox_id, command))
        outcome = self.results[sandbox_id].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return {"stdout": f"out {command[0]}\n", "stderr": "", "exit_code": outcome}

    def session(self):
        session = Mock()

        def lend(sandbox_id):
            handle = Mock()
            exits = self.exits[sandbox_id]
            handle.poll.aio = AsyncMock(
                side_effect=lambda: exits.pop(0) if len(exits) > 1 else exits[0]
            )
            context = Mock()
            context.__aenter__ = AsyncMock(return_value=handle)
            context.__aexit__ = AsyncMock(return_value=False)
            return context

        session.sandbox = lend
        return session

    def notify(self, message):
        self.notices.append(message)

    def output(self, run, result):
        self.outputs.append((run["command"][0], result["stdout"]))

    def start(self, argv, given=True, target="dev"):
        return asyncio.run(
            resume.start(self.session(), target, argv, given, self.notify, self.output)
        )

    def test_a_command_that_finishes_is_not_resumed(self):
        self.results = {"sb-1": [0]}
        self.exits = {"sb-1": [None]}
        result = self.start(["claude", "-p", "x"])
        self.assertEqual(result["runs"], [{"command": START, "exit_code": 0}])
        self.assertEqual(self.restored, [])
        self.assertIn("Running `claude --session-id U -p x`", self.notices[0])
        self.assertIn("continues with `claude --resume U -p", self.notices[0])
        self.assertIn("(the claude rule)", self.notices[0])
        self.assertEqual(len(self.notices), 1)

    def test_a_command_that_fails_by_itself_is_not_resumed(self):
        # No poll either: exits[] is empty, so a poll would fail.
        self.results = {"sb-1": [1]}
        result = self.start(["claude", "-p", "x"])
        self.assertEqual(result["runs"][0]["exit_code"], 1)
        self.assertEqual(self.restored, [])

    def test_the_wrapper_gets_the_marker(self):
        self.results = {"sb-1": [0]}
        self.exits = {"sb-1": [None]}
        calls = []

        async def spy(session, sandbox_id, args):
            calls.append(args)
            return await self.execute(session, sandbox_id, args)

        with patch("agentix.sandbox.execute", AsyncMock(side_effect=spy)):
            self.start(["claude", "-p", "x"])
        args = calls[0]
        self.assertEqual(
            args[:5], ["sh", "-c", resume.WRAPPER, "agentix-run", resume.RUNS_DIR]
        )
        run = json.loads(args[6])
        self.assertEqual(run["id"], args[5])
        self.assertEqual(run["sandbox_id"], "sb-1")
        self.assertEqual(run["resume"], CONTINUE)
        self.assertEqual(run["command"], START)
        self.assertEqual(args[7:], START)

    def test_a_timeout_restores_and_resumes(self):
        then = CONTINUE
        self.results = {"sb-1": [128], "sb-new1": [0]}
        # The sandbox reports its exit a moment after the command returns.
        self.exits = {"sb-1": [None, None, 124], "sb-new1": [None]}
        self.snapshots = [{"id": "im-1", "sandbox_id": "sb-1", "kind": "timeout"}]
        self.markers = {
            "sb-new1": marker("r1", "sb-1", START, then)
            + "\n"
            + marker("old", "sb-0", ["claude", "-p", "y"], ["claude", "--continue"])
        }
        result = self.start(["claude", "-p", "x"])
        self.assertEqual(self.restored, ["sb-new1"])
        # The marker of an earlier sandbox is not resumed.
        self.assertEqual(self.executed, [("sb-1", START), ("sb-new1", then)])
        self.assertEqual(result["sandbox_id"], "sb-new1")
        self.assertEqual(result["resumes"], 1)
        self.assertEqual(result["runs"], [{"command": then, "exit_code": 0}])
        self.assertIn(
            "`claude --session-id U -p x` was running when sb-1 reached its timeout",
            self.notices[1],
        )
        self.assertIn(
            "as sb-new1; continuing with `claude --resume U -p",
            self.notices[1],
        )
        self.assertIn("(resume 1 of 3)", self.notices[1])
        self.assertEqual(len(self.outputs), 2)

    def test_an_end_that_is_not_the_timeout_is_not_resumed(self):
        self.results = {"sb-1": [OperationFailed("gone")]}
        self.exits = {"sb-1": [137]}
        result = self.start(["claude", "-p", "x"])
        self.assertTrue(result["stopped"])
        self.assertEqual(self.restored, [])
        self.assertIn("Lost `claude --session-id U -p x`", self.notices[1])
        self.assertIn("gone. It may still be running", self.notices[1])
        self.assertIn("exit code 137, not at its timeout", self.notices[-1])

    def test_lost_contact_with_a_running_sandbox_says_so(self):
        self.results = {"sb-1": [OperationFailed("network")]}
        self.exits = {"sb-1": [None]}
        result = self.start(["claude", "-p", "x"])
        self.assertTrue(result["stopped"])
        self.assertEqual(result["runs"][0]["exit_code"], None)
        self.assertEqual(self.restored, [])
        self.assertIn("It may still be running there", self.notices[-1])
        self.assertIn("agentix sandbox resume dev", self.notices[-1])

    def test_resumes_stop_at_the_limit(self):
        then = ["claude", "--continue"]
        ids = ["sb-1"] + [f"sb-new{n}" for n in range(1, resume.MAX_RESUMES + 1)]
        self.results = {sandbox_id: [128] for sandbox_id in ids}
        self.exits = {sandbox_id: [124] for sandbox_id in ids}
        self.markers = {
            new: marker("r1", old, ["claude"], then) for old, new in zip(ids, ids[1:])
        }

        async def snapshot_of_current(session, target):
            self.snapshots.append(
                {
                    "id": f"im-t{len(self.restored)}",
                    "sandbox_id": ids[len(self.restored)],
                    "kind": "timeout",
                }
            )
            return await self.restore(session, target)

        with patch(
            "agentix.lifecycle.restore", AsyncMock(side_effect=snapshot_of_current)
        ):
            result = self.start(["claude", "-p", "x"])
        self.assertEqual(len(self.restored), resume.MAX_RESUMES)
        self.assertTrue(result["stopped"])
        self.assertIn("after 3 resumes", self.notices[-1])

    def test_a_restore_that_fails_is_retried(self):
        self.results = {"sb-1": [128], "sb-new1": [0]}
        self.exits = {"sb-1": [124], "sb-new1": [None]}
        self.snapshots = [{"id": "im-1", "sandbox_id": "sb-1", "kind": "timeout"}]
        self.markers = {
            "sb-new1": marker("r1", "sb-1", ["claude"], ["claude", "--continue"])
        }
        failures = [OperationFailed("still saving")]

        async def flaky(session, target):
            if failures:
                raise failures.pop()
            return await self.restore(session, target)

        with patch("agentix.lifecycle.restore", AsyncMock(side_effect=flaky)):
            result = self.start(["claude", "-p", "x"])
        self.assertEqual(result["resumes"], 1)

    def test_refuses_sandboxes_without_a_snapshot(self):
        with self.assertRaisesRegex(InvalidRequest, "has no name"):
            self.start(["claude", "-p", "x"], target="sb-1")
        self.options = {"exit_snapshot": False}
        with self.assertRaisesRegex(InvalidRequest, "--no-snapshot"):
            self.start(["claude", "-p", "x"])
        with self.assertRaisesRegex(InvalidRequest, "No resume rule"):
            self.start(["make"])
        self.assertEqual(self.executed, [])

    def resume(self):
        return asyncio.run(
            resume.resume(self.session(), "dev", self.notify, self.output)
        )

    def test_resume_by_hand(self):
        self.snapshots = [{"id": "im-1", "sandbox_id": "sb-1", "kind": "down"}]
        self.markers = {
            "sb-new1": marker(
                "r1",
                "sb-1",
                ["pi", "-p", "x"],
                ["pi", "--continue", "-p", "x"],
                resumes=1,
            )
        }
        self.results = {"sb-new1": [0]}
        self.exits = {"sb-new1": [None]}
        result = self.resume()
        self.assertEqual(self.executed, [("sb-new1", ["pi", "--continue", "-p", "x"])])
        self.assertEqual(result["runs"][0]["exit_code"], 0)
        self.assertIn("was stopped with agentix sandbox down", self.notices[0])
        self.assertIn("(resume 2 of this run)", self.notices[0])

    def test_resume_with_nothing_cut_off_only_restores(self):
        self.snapshots = [{"id": "im-1", "sandbox_id": "sb-1", "kind": "timeout"}]
        self.markers = {
            "sb-new1": marker("r0", "sb-0", ["claude"], ["claude", "--continue"])
        }
        result = self.resume()
        self.assertEqual(
            result, {"name": "dev", "sandbox_id": "sb-new1", "snapshot": "im-1"}
        )
        self.assertEqual(self.executed, [])
        self.assertIn("nothing was resumed", self.notices[0])


class CommandLineTest(unittest.TestCase):
    def run_cli(self, *argv, result=None):
        backend = Mock()
        backend.execute_resumable.return_value = result or {"runs": []}
        backend.resume.return_value = result or {"runs": []}
        with patch("agentix.__main__._backend", return_value=backend):
            from agentix.__main__ import main

            main(list(argv))
        return backend

    def test_exec_resume_flag(self):
        backend = self.run_cli("sandbox", "exec", "dev", "claude -p 'a b'", "--resume")
        args = backend.execute_resumable.call_args.args
        self.assertEqual(args[:3], ("dev", ["claude", "-p", "a b"], True))
        backend = self.run_cli("sandbox", "exec", "dev", "./x", "--resume=./x -c")
        self.assertEqual(backend.execute_resumable.call_args.args[2], "./x -c")
        backend.execute.assert_not_called()

    def test_exec_without_resume_is_unchanged(self):
        backend = Mock()
        backend.execute.return_value = {"stdout": "", "stderr": "", "exit_code": 0}
        with patch("agentix.__main__._backend", return_value=backend):
            from agentix.__main__ import main

            main(["sandbox", "exec", "dev", "true"])
        backend.execute_resumable.assert_not_called()

    def test_runs_that_all_exit_0_succeed(self):
        done = {"runs": [{"command": ["claude"], "exit_code": 0}], "stopped": True}
        self.run_cli("sandbox", "resume", "dev", result=done)

    def test_a_command_that_did_not_finish_fails(self):
        failed = {"runs": [{"command": ["claude"], "exit_code": 1}]}
        with self.assertRaisesRegex(RuntimeError, r"`claude` \(1\)"):
            self.run_cli("sandbox", "exec", "dev", "claude", "--resume", result=failed)
        lost = {"runs": [{"command": ["claude"], "exit_code": None}], "stopped": True}
        with self.assertRaises(RuntimeError):
            self.run_cli("sandbox", "resume", "dev", result=lost)


if __name__ == "__main__":
    unittest.main()
