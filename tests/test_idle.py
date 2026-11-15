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
        self.proc = FakeProc(self.root)

    def run_watcher(self, mode="check", limit=60, interval=5, shell="sh"):
        return subprocess.run(
            [shell, "-c", idle.WATCHER, "agentix-idle", mode, str(limit)]
            + [str(interval), PROBE, str(self.root)],
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
                + [PROBE, str(self.root)],
                capture_output=True,
                timeout=4,
            )

    def test_the_watcher_does_not_count_itself_on_the_real_proc(self):
        # Something else runs here, so only a crash would print nothing.
        result = subprocess.run(
            ["sh", "-c", idle.WATCHER, "agentix-idle", "check", "60", "5", PROBE],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertIn(result.stdout.strip(), ("busy", "idle"))
        self.assertEqual(result.stderr, "")


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
