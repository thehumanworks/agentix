import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import modal
from support import ModalTestCase, _each

from agentix import runner, store
from agentix.errors import InvalidRequest, NotFound

RUN = "fc-run1"
SB = "sb-test"


def entry(source, message):
    return Mock(source=source, message=message)


class RunsTestCase(ModalTestCase):
    def setUp(self):
        super().setUp()
        self.function = Mock()
        self.function.hydrate.aio = AsyncMock()
        self.function.spawn.aio = AsyncMock(return_value=Mock(object_id=RUN))
        self.function_lookup = self._patch("agentix.runs.modal.Function.from_name")
        self.function_lookup.return_value = self.function
        # Each call ID has its own stand-in, made when a test first asks.
        self.calls = {}
        self.call_lookup = self._patch("agentix.runs.modal.FunctionCall.from_id")
        self.call_lookup.side_effect = lambda run_id: self.calls.setdefault(
            run_id, self.make_call()
        )

    def make_call(self, result=None, error=None, logs=()):
        call = Mock()
        call.get.aio = AsyncMock(return_value=result, side_effect=error)
        call.logs.stream.aio = Mock(side_effect=lambda **_: _each(list(logs)))
        return call

    def store_run(self, run_id, sandbox_id=SB, started_at="2026-11-15T10:00:00+00:00"):
        run = {
            "run_id": run_id,
            "sandbox_id": sandbox_id,
            "name": "",
            "command": ["sleep", "9"],
            "started_at": started_at,
        }
        self.volume.files[f"runs/{sandbox_id}/{run_id}.json"] = json.dumps(run).encode()

    def runs_listed(self, *args):
        return [
            json.loads(line)
            for line in self.run_cli("sandbox", "runs", *args).split("\n")
            if line
        ]

    def run_exit(self, *command):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                self.run_cli(*command)
        return raised.exception.code, stderr.getvalue()


class DetachTest(RunsTestCase):
    def test_detach_spawns_the_runner_and_records_the_run(self):
        output = self.run_cli("sandbox", "exec", SB, "make test 'a b'", "--detach")
        self.assertEqual(output.strip(), RUN)
        self.function_lookup.assert_called_once_with(
            runner.APP_NAME, runner.FUNCTION_NAME
        )
        self.function.spawn.aio.assert_awaited_once_with(SB, ["make", "test", "a b"])
        # Nothing runs in the sandbox from here: the runner does that.
        self.sandbox.exec.aio.assert_not_awaited()
        stored = json.loads(self.volume.files[f"runs/{SB}/{RUN}.json"])
        self.assertEqual(stored["command"], ["make", "test", "a b"])
        self.assertEqual(stored["sandbox_id"], SB)

    def test_a_name_is_resolved_to_its_current_sandbox(self):
        record = store.new_record(
            "dev",
            {
                "image_name": "i",
                "secret_name": "",
                "timeout": 60,
                "workdir": "",
                "memory": 1,
                "cpu": 0,
            },
        )
        store.add_sandbox(record, SB)
        self.write_record(record)
        self.run_cli("sandbox", "exec", "dev", "true", "--detach")
        self.function.spawn.aio.assert_awaited_once_with(SB, ["true"])
        stored = json.loads(self.volume.files[f"runs/{SB}/{RUN}.json"])
        self.assertEqual(stored["name"], "dev")

    def test_without_a_deployed_runner_it_says_how_to_deploy(self):
        self.function.hydrate.aio.side_effect = modal.exception.NotFoundError("no")
        with self.assertRaisesRegex(NotFound, "agentix runner deploy"):
            self.run_cli("sandbox", "exec", SB, "true", "--detach")
        self.function.spawn.aio.assert_not_awaited()

    def test_a_stopped_sandbox_is_refused(self):
        self.sandbox.poll.aio.return_value = 0
        with self.assertRaisesRegex(InvalidRequest, "not running"):
            self.run_cli("sandbox", "exec", SB, "true", "--detach")
        self.function.spawn.aio.assert_not_awaited()

    def test_detach_does_not_go_with_resume(self):
        with self.assertRaisesRegex(InvalidRequest, "--resume"):
            self.run_cli("sandbox", "exec", SB, "true", "--detach", "--resume")
        self.function.spawn.aio.assert_not_awaited()


class LogsAndWaitTest(RunsTestCase):
    def test_logs_prints_each_stream_and_skips_modal_messages(self):
        self.calls[RUN] = self.make_call(
            logs=[
                entry("system", "container started\n"),
                entry("stdout", "one\n"),
                entry("stderr", "oops\n"),
                entry("stdout", "two\n"),
            ]
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            output = self.run_cli("sandbox", "logs", RUN)
        self.assertEqual(output, "one\ntwo\n")
        self.assertEqual(stderr.getvalue(), "oops\n")

    def test_wait_prints_and_exits_with_the_exit_code(self):
        self.calls[RUN] = self.make_call({"exit_code": 7, "sandbox_ended": False})
        code, _ = self.run_exit("sandbox", "wait", RUN)
        self.assertEqual(code, 7)
        self.calls[RUN].get.aio.assert_awaited_once_with(timeout=None)

    def test_wait_says_when_the_sandbox_ended_first(self):
        self.calls[RUN] = self.make_call({"exit_code": None, "sandbox_ended": True})
        code, stderr = self.run_exit("sandbox", "wait", RUN)
        self.assertEqual(code, 128)
        self.assertIn("ended first", stderr)

    def test_an_expired_result_is_not_found(self):
        self.calls[RUN] = self.make_call(error=modal.exception.OutputExpiredError())
        with self.assertRaisesRegex(NotFound, "no longer keeps"):
            self.run_cli("sandbox", "wait", RUN)

    def test_a_run_id_must_be_a_call_id(self):
        for command in ("logs", "wait"):
            with self.assertRaisesRegex(InvalidRequest, "Invalid run ID"):
                self.run_cli("sandbox", command, "sb-123")
        self.call_lookup.assert_not_called()


class ListAndCleanTest(RunsTestCase):
    def setUp(self):
        super().setUp()
        self.store_run("fc-a", started_at="2026-11-15T10:00:00+00:00")
        self.store_run("fc-b", started_at="2026-11-15T11:00:00+00:00")
        self.store_run("fc-c", sandbox_id="sb-other")
        self.calls["fc-a"] = self.make_call({"exit_code": 0, "sandbox_ended": False})
        # A poll of a call that has not ended raises the built-in TimeoutError.
        self.calls["fc-b"] = self.make_call(error=TimeoutError())
        self.calls["fc-c"] = self.make_call(error=modal.exception.OutputExpiredError())

    def test_runs_of_a_sandbox_with_their_state(self):
        listed = self.runs_listed(SB)
        self.assertEqual([run["run_id"] for run in listed], ["fc-a", "fc-b"])
        self.assertEqual(listed[0]["state"], "exited")
        self.assertEqual(listed[0]["exit_code"], 0)
        self.assertEqual(listed[0]["command"], "sleep 9")
        self.assertEqual(listed[1]["state"], "running")
        for call in ("fc-a", "fc-b"):
            self.calls[call].get.aio.assert_awaited_once_with(timeout=0)

    def test_without_a_target_every_run_is_listed(self):
        states = {run["run_id"]: run["state"] for run in self.runs_listed()}
        self.assertEqual(
            states, {"fc-a": "exited", "fc-b": "running", "fc-c": "expired"}
        )

    def test_a_name_lists_the_runs_of_all_its_sandboxes(self):
        record = store.new_record(
            "dev",
            {
                "image_name": "i",
                "secret_name": "",
                "timeout": 60,
                "workdir": "",
                "memory": 1,
                "cpu": 0,
            },
        )
        store.add_sandbox(record, SB)
        store.add_sandbox(record, "sb-other")
        self.write_record(record)
        self.assertEqual(len(self.runs_listed("dev")), 3)

    def test_clean_forgets_ended_runs_and_keeps_running_ones(self):
        self.listing.aio.side_effect = None
        cleaned = self.runs_listed("--clean")
        self.assertEqual({run["run_id"] for run in cleaned}, {"fc-a", "fc-c"})
        self.assertEqual(
            sorted(self.volume.files),
            [f"runs/{SB}/fc-b.json"],
        )
        # Their logs go from the sandboxes that still run.
        removed = [call.args for call in self.sandbox.exec.aio.await_args_list]
        self.assertIn(("rm", "-rf", "--", f"{runner.RUNS_DIR}/fc-a"), removed)
        self.assertIn(("rm", "-rf", "--", f"{runner.RUNS_DIR}/fc-c"), removed)

    def test_clean_leaves_ended_sandboxes_alone(self):
        self.sandbox.poll.aio.return_value = 0
        self.runs_listed("--clean")
        self.sandbox.exec.aio.assert_not_awaited()
        self.assertEqual(sorted(self.volume.files), [f"runs/{SB}/fc-b.json"])

    def test_no_runs_is_an_empty_list(self):
        self.volume.files.clear()
        self.assertEqual(self.runs_listed(), [])


@unittest.skipUnless(
    shutil.which("sh")
    and subprocess.run(
        ["tail", "--pid=1", "-n", "0", "/dev/null"], capture_output=True, timeout=5
    ).returncode
    == 0,
    "needs sh and a tail with --pid",
)
class WrapperTest(unittest.TestCase):
    """The script that the runner runs in the sandbox, run here."""

    def run_wrapper(self, run_dir, *args):
        return subprocess.run(
            ["sh", "-c", runner.WRAPPER, "agentix-run", str(run_dir), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_first_call_runs_and_a_second_follows_without_running_again(self):
        with tempfile.TemporaryDirectory() as home:
            run_dir = Path(home, "runs", "fc-1")
            counter = Path(home, "count")
            command = f"echo started >> {counter}; echo out; echo err >&2; exit 3"
            first = self.run_wrapper(run_dir, "sh", "-c", command)
            self.assertEqual(first.returncode, 3)
            self.assertEqual(first.stdout, "out\nerr\n")
            again = self.run_wrapper(run_dir, "sh", "-c", command)
            self.assertEqual(again.returncode, 3)
            self.assertEqual(again.stdout, "out\nerr\n")
            self.assertEqual(counter.read_text(), "started\n")
            self.assertEqual((run_dir / "exit").read_text(), "3\n")

    def test_a_killed_command_has_no_exit_code(self):
        with tempfile.TemporaryDirectory() as home:
            run_dir = Path(home, "fc-2")
            result = self.run_wrapper(run_dir, "sh", "-c", "kill -9 $PPID")
            self.assertEqual(result.returncode, runner.NO_EXIT_CODE)
            self.assertIn("without an exit code", result.stderr)


if __name__ == "__main__":
    unittest.main()
