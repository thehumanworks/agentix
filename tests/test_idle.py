import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from agentix import idle
from agentix.errors import InvalidRequest

PROBE = "test -e /run/agentix/ready"
SHELLS = [shell for shell in ("sh", "dash", "bash") if shutil.which(shell)]


class FakeProc:
    """A stand-in for /proc with the processes a test puts in it."""

    def __init__(self, root: Path):
        self.root = root
        self.add(1, "runch-agent", 0, "/__modal/runch-agent")
        self.add(2, "kthreadd", 0, "")
        self.add(30, "kworker/0:1", 2, "", state="I")

    def add(self, pid, comm, ppid, args, state="S"):
        folder = self.root / str(pid)
        folder.mkdir()
        (folder / "stat").write_text(f"{pid} ({comm}) {state} {ppid} {pid} {pid} 0 -1")
        joined = "".join(f"{word}\0" for word in args.split(" ")) if args else ""
        (folder / "cmdline").write_text(joined)
        return pid


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)
        self.marks = f"{self.run_dir}/used."
        self.proc = FakeProc(self.root)

    def run_watcher(self, mode="check", limit=60, interval=5, shell="sh"):
        return subprocess.run(
            [shell, "-c", idle.WATCHER, "agentix-idle", mode, str(limit)]
            + [str(interval), PROBE, self.marks, str(self.root)],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def check(self):
        outputs = {self.run_watcher(shell=shell).stdout.strip() for shell in SHELLS}
        self.assertEqual(len(outputs), 1, outputs)
        return outputs.pop()

    def test_a_fresh_sandbox_is_idle(self):
        self.assertEqual(self.check(), "idle")

    def test_listeners_probe_agents_and_zombies_are_idle(self):
        self.proc.add(40, "sshd", 1, "sshd: /bin/sshd -f config [listener]")
        self.proc.add(41, "tailscaled", 1, "tailscaled --tun=userspace-networking")
        self.proc.add(42, "test", 1, PROBE)
        self.proc.add(43, "gpg-agent", 1, "gpg-agent --daemon")
        self.proc.add(44, "my (odd) name", 1, "gone", state="Z")
        self.assertEqual(self.check(), "idle")

    def test_nix_wrapped_daemons_are_idle(self):
        # wrapProgram runs tailscaled as .tailscaled-wrapped, which the
        # kernel names .tailscaled-wra; argv[0] stays tailscaled.
        self.proc.add(41, ".tailscaled-wra", 1, "/nix/store/x/bin/tailscaled --tun=u")
        self.proc.add(42, ".gpg-agent-wrap", 1, "/nix/store/y/bin/.gpg-agent-wrapped")
        self.assertEqual(self.check(), "idle")

    def test_a_wrapped_tailscale_ssh_session_is_busy(self):
        self.proc.add(41, ".tailscaled-wra", 1, "/nix/store/x/bin/tailscaled --tun=u")
        self.proc.add(
            52, ".tailscaled-wra", 41, "/nix/store/x/bin/tailscaled be-child ssh"
        )
        self.assertEqual(self.check(), "busy")

    def test_a_process_whose_command_line_cannot_be_read_is_busy(self):
        pid = self.proc.add(50, "secret", 1, "x")
        (self.root / str(pid) / "cmdline").unlink()
        self.assertEqual(self.check(), "busy")

    def test_a_process_with_an_empty_command_line_is_busy(self):
        self.proc.add(50, "odd", 1, "")
        self.assertEqual(self.check(), "busy")

    def test_a_mark_is_busy_and_check_keeps_it(self):
        mark = Path(f"{self.marks}123")
        mark.touch()
        self.assertEqual(self.check(), "busy")
        self.assertTrue(mark.exists())

    def test_watch_removes_marks_and_counts_from_after_them(self):
        mark = Path(f"{self.marks}123")
        mark.touch()
        started = time.monotonic()
        result = self.run_watcher("watch", limit=2, interval=1)
        self.assertEqual(result.returncode, idle.IDLE_EXIT_CODE)
        self.assertFalse(mark.exists())
        # The first look saw the mark, so idle time starts at the second.
        self.assertGreaterEqual(time.monotonic() - started, 3)

    def test_an_exec_command_is_busy(self):
        # `sandbox exec`, --resume, and detached runs: children of PID 1.
        self.proc.add(50, "sh", 1, "sh -c long-task")
        self.assertEqual(self.check(), "busy")

    def test_a_command_with_parentheses_in_its_name_is_busy(self):
        self.proc.add(50, "a) S 2 (b", 1, "./a")
        self.assertEqual(self.check(), "busy")

    def test_an_openssh_session_is_busy(self):
        self.proc.add(40, "sshd", 1, "sshd: /bin/sshd -f config [listener]")
        self.proc.add(51, "sshd-session", 40, "sshd-session: root@pts/0")
        self.assertEqual(self.check(), "busy")

    def test_a_tailscale_ssh_session_is_busy(self):
        self.proc.add(41, "tailscaled", 1, "tailscaled --tun=userspace-networking")
        self.proc.add(52, "tailscaled", 41, "tailscaled be-child ssh --uid=0")
        self.assertEqual(self.check(), "busy")

    def test_a_process_left_by_a_session_is_busy(self):
        self.proc.add(53, "node", 1, "node server.js")
        self.assertEqual(self.check(), "busy")

    def test_watch_exits_with_the_idle_code_once_idle_long_enough(self):
        started = time.monotonic()
        result = self.run_watcher("watch", limit=1, interval=1)
        self.assertEqual(result.returncode, idle.IDLE_EXIT_CODE)
        self.assertIn("stopping the sandbox", result.stderr)
        self.assertLess(time.monotonic() - started, 10)

    def test_watch_keeps_running_while_busy(self):
        self.proc.add(50, "sh", 1, "sh -c long-task")
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run(
                ["sh", "-c", idle.WATCHER, "agentix-idle", "watch", "1", "1"]
                + [PROBE, self.marks, str(self.root)],
                capture_output=True,
                timeout=4,
            )

    def test_the_watcher_does_not_count_itself_on_the_real_proc(self):
        # Something else runs here, so only a crash would print nothing.
        result = subprocess.run(
            ["sh", "-c", idle.WATCHER, "agentix-idle", "check", "60", "5", PROBE]
            + [self.marks],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertIn(result.stdout.strip(), ("busy", "idle"))
        self.assertEqual(result.stderr, "")


class ExecWrapperTests(unittest.TestCase):
    """The wrapper of every command agentix starts, run here with its paths."""

    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir)
        self.wrapper = idle.EXEC_WRAPPER.replace(idle.RUN_DIR, str(self.run_dir))

    def run_wrapped(self, *args, shell="sh"):
        return subprocess.run(
            [shell, "-c", self.wrapper, "agentix-exec", *args],
            capture_output=True,
            text=True,
        )

    def marks(self):
        return sorted(path.name for path in self.run_dir.glob("used.*"))

    def test_command_keeps_its_arguments_output_and_exit_code(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.run_wrapped("sh", "-c", 'echo "$1"; exit 3', "x", "a b")
                self.assertEqual((result.stdout, result.returncode), ("a b\n", 3))
                self.assertEqual(result.stderr, "")

    def test_no_mark_without_an_idle_stop(self):
        self.run_wrapped("true")
        self.assertEqual(self.marks(), [])

    def test_mark_with_an_idle_stop(self):
        (self.run_dir / "idle-stop").touch()
        result = self.run_wrapped("true")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(self.marks()), 1)

    def test_an_unwritable_mark_is_silent(self):
        (self.run_dir / "idle-stop").touch()
        self.wrapper = idle.EXEC_WRAPPER.replace(
            idle.MARKS, f"{self.run_dir}/missing/used."
        ).replace(idle.RUN_DIR, str(self.run_dir))
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.run_wrapped("echo", "ok", shell=shell)
                self.assertEqual((result.stdout, result.stderr), ("ok\n", ""))

    def test_wrap(self):
        self.assertEqual(
            idle.wrap(["ls", "a b"]),
            ["sh", "-c", idle.EXEC_WRAPPER, "agentix-exec", "ls", "a b"],
        )

    def test_detached_runs_mark_the_same_files(self):
        from agentix import runner

        self.assertEqual((runner.IDLE_FLAG, runner.IDLE_MARKS), (idle.FLAG, idle.MARKS))
        self.assertIn(f'"{idle.MARKS}$$"', runner.WRAPPER)


class ParseTests(unittest.TestCase):
    def test_durations_are_seconds(self):
        self.assertEqual(idle.seconds("20m"), 1200)
        self.assertEqual(idle.seconds("1h30m"), 5400)
        self.assertEqual(idle.seconds(" 2M "), 120)

    def test_off_is_zero(self):
        self.assertEqual(idle.seconds("off"), 0)
        self.assertEqual(idle.seconds("OFF"), 0)

    def test_bad_values_are_refused(self):
        for value in ("20", "0m", "soon", "", True, 20, None):
            with self.subTest(value=value), self.assertRaises(InvalidRequest):
                idle.seconds(value)

    def test_less_than_a_minute_is_refused(self):
        with self.assertRaisesRegex(InvalidRequest, "at least 1m"):
            idle.seconds("30s")

    def test_poll_interval_is_bounded(self):
        self.assertEqual(idle.poll_interval(60), 10)
        self.assertEqual(idle.poll_interval(1200), 30)
        self.assertEqual(idle.poll_interval(1), 5)

    def test_describe(self):
        self.assertEqual(idle.describe(0), "off")
        self.assertEqual(idle.describe(1200), "20m")
        self.assertEqual(idle.describe(5400), "1h30m")
        self.assertEqual(idle.describe(90061), "1d1h1m1s")


if __name__ == "__main__":
    unittest.main()
